from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import pa_agent.okx_demo_campaign as campaign_module
from pa_agent.agents.supervisor import SupervisorAgent, SupervisorGate
from pa_agent.config.settings import Settings
from pa_agent.data.base import (
    DataSourceTransientError,
    IndicatorBundle,
    KlineBar,
    KlineFrame,
)
from pa_agent.execution.errors import (
    BrokerTransportError,
    LiveTradingDisabled,
    NewRiskLeaseUnavailable,
    PlanBlocked,
)
from pa_agent.execution.models import ExecutionState
from pa_agent.execution.plan_builder import build_execution_plan
from pa_agent.execution.worker_protocol import (
    SetLeverageParameters,
    WorkerCommandStatus,
)
from pa_agent.okx_demo_campaign import (
    CAMPAIGN_BOOTSTRAP_QUANTITY,
    CAMPAIGN_DURATION,
    CAMPAIGN_ENTRY_TIMEOUT_SECONDS,
    CAMPAIGN_EQUITY_FRACTION,
    CAMPAIGN_EXECUTION_STYLE,
    CAMPAIGN_FAST_EXECUTION_GUIDANCE,
    CAMPAIGN_HIGHER_TIMEFRAMES,
    CAMPAIGN_INSTRUMENT,
    CAMPAIGN_MIN_CONFIDENCE,
    CAMPAIGN_OKX_API_BASE_URL,
    CAMPAIGN_RECONCILE_TIMEOUT_RESULT,
    CAMPAIGN_RECONCILE_WORKER_ATTENTION_RESULT,
    CAMPAIGN_STANCE,
    CAMPAIGN_SYMBOL,
    CAMPAIGN_TIMEFRAME,
    CANARY_ORIGIN,
    CANARY_TIMEFRAME,
    CampaignError,
    CampaignLeverageCandidate,
    CampaignProcessLock,
    CampaignRiskBlocked,
    CampaignRuntime,
    CampaignSizing,
    CampaignStateStore,
    OkxCampaignSource,
    OkxDemoCampaign,
    _attempt_canary_cleanup,
    _canary_price_triplet,
    _wait_for_execution_state,
    build_campaign_settings,
    build_controlled_demo_s_record,
    build_demo_canary_record,
    campaign_config_fingerprint,
    find_latest_natural_campaign_record,
    okx_demo_private_preflight,
    resolve_campaign_sizing,
    resolve_record_campaign_sizing,
    validate_campaign_settings,
)
from pa_agent.records.analysis_history import find_latest_successful_record
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.records.supervisor_writer import SupervisorWriter
from pa_agent.risk.leverage import LeveragePlanningFailure
from tests.unit.test_execution_plan_builder import _persist, _record


def _settings(quantity="120"):
    settings = Settings()
    settings.execution.okx.risk_capital_cap_usdt = Decimal("5000")
    settings.execution.okx.risk_percent = Decimal("0.10")
    settings.execution.okx.maximum_leverage = Decimal("20")
    return build_campaign_settings(settings, quantity=quantity)


def _sizing(_record=None, quantity="120"):
    del _record
    resolved = Decimal(quantity)
    return CampaignSizing(
        sizing_mode="risk_budget",
        quantity=resolved,
        account_total_equity_usd=Decimal("5000"),
        equity_usdt=Decimal("5000"),
        risk_capital_cap_usdt=Decimal("5000"),
        effective_risk_capital_usdt=Decimal("5000"),
        risk_percent=Decimal("0.10"),
        risk_budget_usdt=Decimal("500"),
        risk_used_usdt=Decimal("12"),
        reference_price_usdt=Decimal("4000"),
        contract_notional_usdt=Decimal("4"),
        stop_distance_usdt=Decimal("10"),
        worst_case_loss_per_contract_usdt=Decimal("0.1"),
        fee_per_contract_usdt=Decimal("0.01"),
        slippage_per_contract_usdt=Decimal("0.01"),
        fee_rate=Decimal("0.0005"),
        slippage_rate=Decimal("0.001"),
        minimum_quantity=Decimal("1"),
        quantity_step=Decimal("1"),
        max_buy=Decimal("10000"),
        max_sell=Decimal("10000"),
    )


def _leverage_candidate(sizing: CampaignSizing) -> CampaignLeverageCandidate:
    return CampaignLeverageCandidate(
        parameters=SetLeverageParameters(
            analysis_digest="a" * 64,
            config_fingerprint="pending_campaign_sizing",
            instrument=CAMPAIGN_INSTRUMENT,
            direction="long",
            margin_mode="cross",
            position_mode="net_mode",
            current_leverage=Decimal("20"),
            target_leverage=Decimal("30"),
            current_capacity=Decimal("120000"),
            target_capacity=sizing.quantity,
            maximum_leverage=Decimal("30"),
            maximum_capacity=sizing.quantity,
            planning_method="bounded_sequential_policy_grid_v1",
            policy_grid_step=Decimal("10"),
            verified_grid=(
                {"leverage": "20", "capacity": "120000"},
                {"leverage": "30", "capacity": str(sizing.quantity)},
            ),
            required_quantity=sizing.quantity,
            entry_price=Decimal("100"),
            expected_account_identity="b" * 64,
            okx_api_base_url=CAMPAIGN_OKX_API_BASE_URL,
        ),
        sizing=sizing,
    )


def _frame(bar_ms: int) -> KlineFrame:
    bar = KlineBar(
        seq=1,
        ts_open=float(bar_ms),
        open=4000,
        high=4010,
        low=3990,
        close=4005,
        volume=100,
        closed=True,
    )
    indicators = IndicatorBundle(ema20=(4000.0,), atr14=(10.0,))
    return KlineFrame(
        symbol=CAMPAIGN_SYMBOL,
        timeframe=CAMPAIGN_TIMEFRAME,
        bars=(bar,),
        indicators=indicators,
        snapshot_ts_local_ms=bar_ms,
        price_tick="0.1",
    )


def test_wait_for_execution_state_tolerates_transient_worker_attention():
    execution_id = "execution-transient-attention"

    class _TransientAttentionService:
        def __init__(self):
            self.state = ExecutionState.EXIT_PENDING
            self.waits = 0

        def latest_successful_reconcile_at(self):
            return datetime(2026, 7, 24, tzinfo=UTC)

        def get_execution(self, requested_id):
            assert requested_id == execution_id
            return SimpleNamespace(
                id=execution_id,
                state=self.state,
                state_reason="",
                last_error="",
            )

        def wait_for_reconcile(self, *, after, timeout):
            del after, timeout
            self.waits += 1
            if self.waits == 1:
                raise LiveTradingDisabled("交易后台需要人工处理：RuntimeError")
            self.state = ExecutionState.CLOSED
            return datetime(2026, 7, 24, 0, 0, 1, tzinfo=UTC)

    service = _TransientAttentionService()
    closed = _wait_for_execution_state(
        service,
        execution_id,
        accepted={ExecutionState.CLOSED},
        timeout=2,
    )

    assert closed.state is ExecutionState.CLOSED
    assert service.waits == 2


def test_canary_canceled_limit_entry_fails_without_market_fallback():
    execution_id = "canceled-limit-entry"

    class _CanceledEntryService:
        def __init__(self):
            self.reconcile_waits = 0

        def latest_successful_reconcile_at(self):
            return datetime(2026, 7, 24, tzinfo=UTC)

        def get_execution(self, requested_id):
            assert requested_id == execution_id
            return SimpleNamespace(
                id=execution_id,
                state=ExecutionState.CANCELED,
                state_reason="限价入场超时并已撤单",
                last_error="",
                needs_attention=False,
            )

        def wait_for_reconcile(self, *, after, timeout):
            del after, timeout
            self.reconcile_waits += 1
            raise AssertionError("已撤销的限价入场不能继续等待或改走市价")

    service = _CanceledEntryService()

    with pytest.raises(CampaignError, match="canceled"):
        _wait_for_execution_state(
            service,
            execution_id,
            accepted={ExecutionState.OPEN},
            timeout=2,
        )

    assert service.reconcile_waits == 0


def test_demo_cleanup_waits_for_existing_exit_pending_to_reach_closed():
    execution_id = "exit-pending-cleanup"

    class _Service:
        def __init__(self):
            self.closed = False
            self.reconcile_waits = 0

        def get_execution(self, requested_id):
            assert requested_id == execution_id
            return SimpleNamespace(
                id=execution_id,
                state=(
                    ExecutionState.CLOSED
                    if self.closed
                    else ExecutionState.EXIT_PENDING
                ),
                state_reason="",
                last_error="",
                needs_attention=False,
            )

        def latest_successful_reconcile_at(self):
            return datetime(2026, 7, 24, tzinfo=UTC)

        def wait_for_reconcile(self, *, after, timeout):
            del timeout
            self.reconcile_waits += 1
            self.closed = True
            return after + timedelta(seconds=1)

    service = _Service()

    closed = _attempt_canary_cleanup(service, execution_id)

    assert closed.state is ExecutionState.CLOSED
    assert service.reconcile_waits == 1


def test_demo_cleanup_switches_from_cancel_to_exit_after_partial_fill():
    execution_id = "cancel-race-partial-fill"

    class _Service:
        def __init__(self):
            self.state = ExecutionState.ENTRY_PENDING
            self.actions = []

        def get_execution(self, requested_id):
            assert requested_id == execution_id
            return SimpleNamespace(id=execution_id, state=self.state)

        def cancel_entry(self, requested_id):
            assert requested_id == execution_id
            self.actions.append("cancel")
            return SimpleNamespace(id="cancel-command")

        def request_exit(self, requested_id, *, reason):
            assert requested_id == execution_id
            assert reason
            self.actions.append("exit")
            return SimpleNamespace(id="exit-command")

        def wait_for_command(self, command_id, *, timeout):
            assert timeout == 30.0
            if command_id == "cancel-command":
                self.state = ExecutionState.PARTIALLY_FILLED
            else:
                assert command_id == "exit-command"
                self.state = ExecutionState.CLOSED
            return SimpleNamespace(status=WorkerCommandStatus.SUCCEEDED)

    service = _Service()

    closed = _attempt_canary_cleanup(service, execution_id)

    assert closed.state is ExecutionState.CLOSED
    assert service.actions == ["cancel", "exit"]


def test_demo_cleanup_timeout_is_a_hard_nonterminal_failure():
    execution_id = "cleanup-timeout"

    class _Service:
        def get_execution(self, requested_id):
            assert requested_id == execution_id
            return SimpleNamespace(
                id=execution_id,
                state=ExecutionState.EXIT_PENDING,
            )

    with pytest.raises(CampaignError, match="未达到安全终态: exit_pending"):
        _attempt_canary_cleanup(_Service(), execution_id, timeout=0)


class _FakeSource:
    def price_tick(self):
        return "0.1"

    def latest_snapshot(self, count):
        del count
        return [object()]

    def disconnect(self):
        return None


class _FakeOrchestrator:
    def __init__(self, record):
        self.record = record
        self.calls = 0

    def submit(self, frame, token, on_event, *, campaign_id=None, **kwargs):
        del token, on_event, kwargs
        self.calls += 1
        if not hasattr(self.record, "meta"):
            self.record.meta = SimpleNamespace(
                symbol=CAMPAIGN_SYMBOL,
                timeframe=CAMPAIGN_TIMEFRAME,
                data_source="okx",
                market_data_provenance="okx_5m_utc_pair_aggregation",
                decision_stance=CAMPAIGN_STANCE,
                campaign_id=campaign_id,
            )
        elif hasattr(self.record.meta, "model_copy"):
            self.record = self.record.model_copy(
                update={
                    "meta": self.record.meta.model_copy(
                        update={"campaign_id": campaign_id}
                    )
                }
            )
        else:
            self.record.meta.campaign_id = campaign_id
        if not hasattr(self.record, "kline_data"):
            self.record.kline_data = [
                {
                    "ts_open": frame.bars[0].ts_open,
                    "closed": True,
                }
            ]
        return self.record


class _FakeStore:
    def __init__(self, active_batches=None, records=None):
        self.active_batches = list(active_batches or [[]])
        self.records = dict(records or {})

    def list_active(self):
        if len(self.active_batches) > 1:
            return self.active_batches.pop(0)
        return self.active_batches[0]

    def get(self, execution_id):
        record = self.records.get(execution_id)
        if record is not None:
            return record
        for batch in self.active_batches:
            for item in batch:
                if item.id == execution_id:
                    return item
        return None


class _FakeWorkerStore:
    def __init__(self):
        self.risk_state = None

    def get_risk_runtime_state(self, route_key):
        assert route_key == "okx:demo:okx"
        return self.risk_state


class _FakeExecutionService:
    def __init__(
        self,
        *,
        block: PlanBlocked | None = None,
        active_batches=None,
        refresh_error: Exception | None = None,
        records=None,
    ):
        self.block = block
        self.refresh_error = refresh_error
        self.store = _FakeStore(active_batches, records)
        self.prepared = []
        self.submitted = []
        self.expired = []
        self.canceled = []
        self.exited = []
        self.leverage_parameters = []
        self.refreshed = 0
        self.reconciled_execution_ids = []
        self.reconcile_commands = 0
        self.reconcile_waits = 0
        self.refreshed_execution_ids = []
        self.is_armed = False
        self.arm_calls = []
        self.disarm_calls = 0
        self.started = 0
        self._commands = {}
        self._last_reconcile_at = datetime(2026, 7, 17, tzinfo=UTC)
        self.worker_store = _FakeWorkerStore()
        self.transient_risk_recoveries = 0
        self.transient_risk_recovery_status = WorkerCommandStatus.SUCCEEDED
        self.transient_risk_recovery_failure_code = ""
        self.waited_command_ids = []
        self.wait_error = None
        self.disarm_error = None

    def _command(
        self,
        action,
        *,
        status=WorkerCommandStatus.SUCCEEDED,
        failure_code="",
    ):
        command = SimpleNamespace(
            id=f"{action}-{len(self._commands) + 1}",
            status=status,
            failure_code=failure_code,
        )
        self._commands[command.id] = command
        return command

    def prepare_analysis(self, record):
        self.prepared.append(record)
        if self.block is not None:
            raise self.block
        execution = SimpleNamespace(id="execution-1", state=ExecutionState.READY)
        self.store.records[execution.id] = execution
        return execution

    def submit(self, execution_id):
        self.submitted.append(execution_id)
        execution = SimpleNamespace(
            id=execution_id,
            state=ExecutionState.ENTRY_PENDING,
        )
        self.store.records[execution_id] = execution
        return self._command("submit")

    def set_leverage(self, parameters):
        self.leverage_parameters.append(parameters)
        return self._command("set_leverage")

    def expire_unsubmitted(self, execution_id, *, reason):
        self.expired.append((execution_id, reason))
        current = self.store.records[execution_id]
        self.store.records[execution_id] = SimpleNamespace(
            id=execution_id,
            state=ExecutionState.CANCELED,
        )
        return current

    def reconcile(self):
        self.reconcile_commands += 1
        self.reconciled_execution_ids.append([])
        return self._command("reconcile")

    def refresh_account(self, execution_id=None):
        self.refreshed_execution_ids.append(
            [execution_id] if execution_id else []
        )
        if self.refresh_error is not None:
            return self._command(
                "refresh",
                status=WorkerCommandStatus.FAILED,
                failure_code=type(self.refresh_error).__name__,
            )
        self.refreshed += 1
        return self._command("refresh")

    def recover_transient_risk_stop(self):
        self.transient_risk_recoveries += 1
        return self._command(
            "recover_transient_risk_stop",
            status=self.transient_risk_recovery_status,
            failure_code=self.transient_risk_recovery_failure_code,
        )

    def wait_for_command(self, command_id, *, timeout):
        del timeout
        self.waited_command_ids.append(command_id)
        if self.wait_error is not None:
            raise self.wait_error
        return self._commands[command_id]

    def get_execution(self, execution_id):
        return self.store.get(execution_id)

    def list_active(self):
        return self.store.list_active()

    def start_monitoring(self):
        self.started += 1

    def wait_for_worker(self, *, timeout):
        del timeout
        return SimpleNamespace()

    def latest_successful_reconcile_at(self):
        return self._last_reconcile_at

    def wait_for_reconcile(self, *, after, timeout):
        del timeout
        self.reconcile_waits += 1
        self.reconciled_execution_ids.append([])
        self._last_reconcile_at = (after or self._last_reconcile_at) + timedelta(
            seconds=1
        )
        return self._last_reconcile_at

    def arm_confirmation_text(self):
        return "启用模拟交易"

    def arm(self, confirmation):
        self.arm_calls.append(confirmation)
        self.is_armed = True

    def disarm(self):
        self.disarm_calls += 1
        if self.disarm_error is not None:
            raise self.disarm_error
        self.is_armed = False

    def cancel_entry(self, execution_id):
        self.canceled.append(execution_id)
        return self._command("cancel")

    def request_exit(self, execution_id, *, reason):
        self.exited.append((execution_id, reason))
        return self._command("exit")

    def stop_monitoring(self):
        return None


class _FakeSupervisor:
    def __init__(self, action="allow_entry"):
        self.action = action
        self.calls = []

    def review(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            record_id=f"supervisor-{len(self.calls)}",
            action=self.action,
            fallback_level="primary",
            reason="测试监督结论",
        )


class _SupervisorClient:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(content=self.content)


def _supervised_runtime(tmp_path, decision):
    base_record = _record(symbol=CAMPAIGN_SYMBOL)
    record = base_record.model_copy(
        update={
            "meta": base_record.meta.model_copy(
                update={
                    "timeframe": CAMPAIGN_TIMEFRAME,
                    "data_source": "okx",
                    "market_data_provenance": (
                        "okx_5m_utc_pair_aggregation"
                    ),
                    "decision_stance": CAMPAIGN_STANCE,
                }
            ),
            "kline_data": [
                {
                    "seq": 1,
                    "ts_open": 1_784_300_400_000,
                    "open": 4000,
                    "high": 4010,
                    "low": 3990,
                    "close": 4005,
                    "volume": 100,
                    "closed": True,
                }
            ]
        }
    )
    client = _SupervisorClient(
        json.dumps({"action": decision, "reason": "测试监督结论"})
    )
    agent = SupervisorAgent(
        primary_client=client,
        primary_profile_id="test-supervisor",
        primary_model_id="test-model",
        prompt_text="只返回严格 JSON。",
    )
    writer = PendingWriter(tmp_path / "pending")
    gate = SupervisorGate(agent, SupervisorWriter(tmp_path / "supervisor"))
    service = _FakeExecutionService()
    service.is_armed = True
    runtime = CampaignRuntime(
        settings=_settings(),
        source=_FakeSource(),
        writer=writer,
        orchestrator=_FakeOrchestrator(record),
        execution_service=service,
        supervisor=gate,
        sizing_resolver=_sizing,
    )
    return runtime, service, client, record


def _state(store: CampaignStateStore, now: datetime):
    state = store.create_or_resume(now=now, settings=_settings())
    store.save(state)
    return state


def _runtime(orchestrator, service):
    return CampaignRuntime(
        settings=_settings(),
        source=_FakeSource(),
        writer=SimpleNamespace(),
        orchestrator=orchestrator,
        execution_service=service,
        supervisor=_FakeSupervisor(),
        sizing_resolver=_sizing,
    )


@pytest.fixture(autouse=True)
def _stable_campaign_clock(monkeypatch):
    """测试默认固定在实验有效期内，避免随真实日期推移而失效。"""
    current = datetime(2026, 7, 17, 1, tzinfo=UTC)
    monkeypatch.setattr(campaign_module, "_utc_now", lambda: current)


def test_campaign_settings_are_isolated_and_exact():
    base = Settings()
    base.execution.okx.risk_capital_cap_usdt = Decimal("5000")
    base.execution.okx.risk_percent = Decimal("0.10")
    base.execution.okx.maximum_leverage = Decimal("20")
    original_broker = base.execution.selected_broker
    original_threshold = base.execution.min_trade_confidence
    base.execution.okx.api_base_url = "https://attacker.invalid"

    settings = build_campaign_settings(base)

    assert base.execution.selected_broker == original_broker
    assert base.execution.min_trade_confidence == original_threshold
    assert settings.general.decision_stance == CAMPAIGN_STANCE
    assert settings.provider.reasoning_effort == "medium"
    assert settings.general.last_symbol == CAMPAIGN_SYMBOL
    assert settings.general.last_timeframe == CAMPAIGN_TIMEFRAME
    assert settings.execution.min_trade_confidence == CAMPAIGN_MIN_CONFIDENCE
    assert settings.execution.entry_timeout_seconds == CAMPAIGN_ENTRY_TIMEOUT_SECONDS
    assert settings.execution.selected_broker == "okx"
    assert settings.execution.auto_execute is False
    assert settings.execution.okx.instrument == CAMPAIGN_INSTRUMENT
    assert settings.execution.okx.quantity == CAMPAIGN_BOOTSTRAP_QUANTITY
    assert settings.execution.okx.product == "swap"
    assert settings.execution.okx.simulated is True
    assert settings.execution.okx.api_base_url == CAMPAIGN_OKX_API_BASE_URL
    assert base.execution.okx.api_base_url == "https://attacker.invalid"


def test_campaign_config_records_its_fast_execution_style():
    payload = campaign_module._campaign_config_payload()

    assert CAMPAIGN_TIMEFRAME == "10m"
    assert CAMPAIGN_HIGHER_TIMEFRAMES == ("1h", "4h")
    assert CAMPAIGN_STANCE == "extreme_aggressive"
    assert CAMPAIGN_MIN_CONFIDENCE == 20
    assert payload["timeframe"] == "10m"
    assert payload["decision_stance"] == "extreme_aggressive"
    assert payload["min_trade_confidence"] == 20
    assert payload["entry_order_mode"] == "limit_with_slippage"
    assert payload["exit_order_mode"] == "limit_with_slippage"
    assert payload["execution_style"] == CAMPAIGN_EXECUTION_STYLE
    assert "limit_with_slippage" in CAMPAIGN_FAST_EXECUTION_GUIDANCE
    assert "0.50 ATR" in CAMPAIGN_FAST_EXECUTION_GUIDANCE


def test_campaign_settings_reject_live_route():
    settings = _settings()
    settings.execution.okx.simulated = False

    with pytest.raises(CampaignError, match=r"okx\.simulated"):
        validate_campaign_settings(settings)


def test_campaign_plan_uses_fixed_official_demo_endpoint(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    settings = _settings()
    record = _record()
    record = record.model_copy(
        update={
            "meta": record.meta.model_copy(
                update={
                    "symbol": CAMPAIGN_SYMBOL,
                    "timeframe": CAMPAIGN_TIMEFRAME,
                }
            )
        }
    )
    path = _persist(record, tmp_path)

    plan = build_execution_plan(record, settings, record_path=path)

    assert plan.environment == "demo"
    assert plan.okx_api_base_url == CAMPAIGN_OKX_API_BASE_URL


def test_demo_canary_record_is_explicitly_non_strategy_and_builds_demo_plan(
    tmp_path,
    monkeypatch,
):
    bar = KlineBar(
        seq=1,
        ts_open=1_784_304_000_000,
        open=4000,
        high=4010,
        low=3990,
        close=4005,
        volume=100,
        amount=400500,
        closed=True,
    )
    record = build_demo_canary_record(
        entry=Decimal("4005.0"),
        tp1=Decimal("4025.1"),
        tp2=Decimal("4045.2"),
        stop=Decimal("3984.9"),
        bar=bar,
        analysis_atr14=8.0,
        now=datetime(2026, 7, 17, tzinfo=UTC),
    )
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    writer = PendingWriter(pending_dir=tmp_path)
    writer.save_full_durable(record)

    plan = build_execution_plan(
        record,
        _settings(),
        record_path=writer.full_path(record),
    )

    assert record.meta.timeframe == CANARY_TIMEFRAME
    assert record.stage2_decision["origin"] == CANARY_ORIGIN
    assert "不是 PA 策略信号" in record.stage2_decision["decision"]["reason"]
    assert plan.environment == "demo"
    assert plan.entry_type == "limit"
    assert plan.entry_order_mode == "limit_with_slippage"
    assert str(plan.quantity) == "120"


def test_demo_canary_record_is_never_reused_as_a_10m_strategy_record(tmp_path):
    bar = KlineBar(
        seq=1,
        ts_open=1_784_304_000_000,
        open=4000,
        high=4010,
        low=3990,
        close=4005,
        volume=100,
        amount=400500,
        closed=True,
    )
    record = build_demo_canary_record(
        entry=Decimal("4005.0"),
        tp1=Decimal("4025.1"),
        tp2=Decimal("4045.2"),
        stop=Decimal("3984.9"),
        bar=bar,
        now=datetime(2026, 7, 17, tzinfo=UTC),
    )
    writer = PendingWriter(pending_dir=tmp_path)
    writer.save_full_durable(record)

    latest = find_latest_successful_record(
        symbol=CAMPAIGN_SYMBOL,
        timeframe=CAMPAIGN_TIMEFRAME,
        directory=tmp_path,
    )

    assert latest is None


def test_demo_canary_prices_use_closed_bar_and_okx_tick(monkeypatch):
    bar = KlineBar(
        seq=1,
        ts_open=1_784_304_000_000,
        open=4000,
        high=4010,
        low=3990,
        close=4005.13,
        volume=100,
        amount=400500,
        closed=True,
    )

    class _Source:
        def latest_snapshot(self, count):
            assert count == 3
            return [bar]

    class _Client:
        def __init__(self, credentials, *, base_url, simulated):
            del credentials
            assert base_url == CAMPAIGN_OKX_API_BASE_URL
            assert simulated is True

        def instruments(self, inst_type):
            assert inst_type == "SWAP"
            return [{"instId": CAMPAIGN_INSTRUMENT, "tickSz": "0.1"}]

    monkeypatch.setattr(campaign_module, "load_okx_credentials", lambda _: object())
    monkeypatch.setattr(campaign_module, "OkxRestClient", _Client)

    entry, tp1, tp2, stop, returned_bar, analysis_atr14 = _canary_price_triplet(
        SimpleNamespace(source=_Source())
    )

    assert returned_bar is bar
    assert entry == Decimal("4005.1")
    assert stop < entry < tp1 < tp2
    assert all(value % Decimal("0.1") == 0 for value in (entry, tp1, tp2, stop))
    assert analysis_atr14 is None


def test_demo_market_entry_expands_stop_until_risk_size_fits_max_market_order(
    monkeypatch,
):
    bar = KlineBar(
        seq=1,
        ts_open=1_784_304_000_000,
        open=4000,
        high=4010,
        low=3990,
        close=4005.13,
        volume=100,
        amount=400500,
        closed=True,
    )

    class _Source:
        def latest_snapshot(self, count):
            assert count == 3
            return [bar]

    class _Client:
        def __init__(self, credentials, *, base_url, simulated):
            del credentials
            assert base_url == CAMPAIGN_OKX_API_BASE_URL
            assert simulated is True

        def instruments(self, inst_type):
            assert inst_type == "SWAP"
            return [
                {
                    "instId": CAMPAIGN_INSTRUMENT,
                    "tickSz": "0.1",
                    "maxMktSz": "20000",
                }
            ]

    quantities = iter((Decimal("25000"), Decimal("15000")))
    stops = []

    def _sizing(
        _client,
        *,
        entry_price,
        stop_loss_price,
        side,
        risk_capital_cap_usdt,
        risk_percent,
    ):
        del _client, entry_price
        assert side == "long"
        assert risk_capital_cap_usdt == Decimal("5000")
        assert risk_percent == Decimal("0.10")
        stops.append(stop_loss_price)
        return SimpleNamespace(quantity=next(quantities))

    monkeypatch.setattr(campaign_module, "load_okx_credentials", lambda _: object())
    monkeypatch.setattr(campaign_module, "OkxRestClient", _Client)
    monkeypatch.setattr(campaign_module, "resolve_campaign_sizing", _sizing)
    runtime = SimpleNamespace(
        source=_Source(),
        settings=SimpleNamespace(
            execution=SimpleNamespace(
                entry_order_mode="market",
                exit_order_mode="limit",
                okx=SimpleNamespace(
                    risk_capital_cap_usdt=Decimal("5000"),
                    risk_percent=Decimal("0.10"),
                ),
            )
        ),
    )

    entry, tp1, tp2, stop, returned_bar, analysis_atr14 = _canary_price_triplet(
        runtime
    )

    assert returned_bar is bar
    assert analysis_atr14 is None
    assert len(stops) == 2
    assert stop < stops[0] < entry < tp1 < tp2


def test_campaign_state_resume_never_extends_deadline(tmp_path):
    store = CampaignStateStore(tmp_path / "campaign.json")
    started = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)
    state = store.create_or_resume(now=started)
    store.save(state)

    resumed = store.create_or_resume(now=started + timedelta(hours=5))

    assert resumed.campaign_id == state.campaign_id
    assert resumed.config_fingerprint == campaign_config_fingerprint()
    assert resumed.started_at == state.started_at
    assert resumed.expires_at == state.expires_at
    assert resumed.expires_at_utc - resumed.started_at_utc == CAMPAIGN_DURATION


def test_campaign_state_freezes_actual_risk_settings(tmp_path):
    settings = Settings()
    settings.execution.okx.risk_capital_cap_usdt = Decimal("20000")
    settings.execution.okx.risk_percent = Decimal("0.10")
    settings.execution.okx.maximum_leverage = Decimal("20")
    store = CampaignStateStore(tmp_path / "campaign.json")

    state = store.create_or_resume(
        now=datetime(2026, 7, 17, tzinfo=UTC),
        settings=settings,
    )
    store.save(state)
    persisted = store.load()

    assert persisted is not None
    assert persisted.frozen_risk_capital_cap_usdt == Decimal("20000")
    assert persisted.frozen_risk_percent == Decimal("0.10")
    assert persisted.frozen_maximum_leverage == Decimal("20")
    assert persisted.frozen_sizing_mode == "risk_budget"
    assert persisted.frozen_fixed_quantity is None
    status = campaign_module._safe_status(store)
    assert status["config"] == {
        "sizing_mode": "risk_budget",
        "fixed_quantity": None,
        "risk_capital_cap_usdt": "20000",
        "risk_percent": "0.10",
        "maximum_leverage": "20",
    }
    campaign_module.json.dumps(status)


def test_campaign_state_rejects_frozen_risk_values_that_disagree_with_hash(
    tmp_path,
):
    settings = Settings()
    settings.execution.okx.risk_capital_cap_usdt = Decimal("20000")
    settings.execution.okx.risk_percent = Decimal("0.10")
    settings.execution.okx.maximum_leverage = Decimal("20")
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = store.create_or_resume(
        now=datetime(2026, 7, 17, tzinfo=UTC),
        settings=settings,
    )
    store.save(
        state.model_copy(
            update={"frozen_maximum_leverage": Decimal("25")}
        )
    )

    with pytest.raises(CampaignError, match="冻结风险参数与指纹不一致"):
        store.create_or_resume(
            now=datetime(2026, 7, 17, 1, tzinfo=UTC),
            settings=settings,
        )


def test_campaign_state_save_retries_transient_windows_replace_lock(
    monkeypatch,
    tmp_path,
):
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = store.create_or_resume(now=datetime(2026, 7, 17, tzinfo=UTC))
    real_replace = campaign_module.os.replace
    replace_calls = []

    def _flaky_replace(source, destination):
        replace_calls.append((source, destination))
        if len(replace_calls) < 3:
            raise PermissionError(5, "测试中的短暂文件占用")
        return real_replace(source, destination)

    monkeypatch.setattr(campaign_module.os, "replace", _flaky_replace)
    monkeypatch.setattr(campaign_module.time, "sleep", lambda _seconds: None)

    store.save(state)

    assert len(replace_calls) == 3
    persisted = store.load()
    assert persisted is not None
    assert persisted.campaign_id == state.campaign_id
    assert persisted.status == state.status


def test_completed_campaign_cannot_restart_automatically(tmp_path):
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = store.create_or_resume(now=datetime(2026, 7, 17, tzinfo=UTC))
    store.save(state.model_copy(update={"status": "completed"}))

    with pytest.raises(CampaignError, match="禁止自动重新计时"):
        store.create_or_resume(now=datetime(2026, 7, 18, tzinfo=UTC))


@pytest.mark.parametrize("status", ["stopping", "needs_attention"])
def test_campaign_state_rejects_automatic_resume_after_closeout_started(
    status,
    tmp_path,
):
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = store.create_or_resume(now=datetime(2026, 7, 17, tzinfo=UTC))
    store.save(
        state.model_copy(
            update={
                "status": status,
                "last_error": "收口状态等待人工核对",
            }
        )
    )

    with pytest.raises(CampaignError, match="禁止自动恢复"):
        store.create_or_resume(now=datetime(2026, 7, 17, 1, tzinfo=UTC))

    persisted = store.load()
    assert persisted is not None
    assert persisted.status == status
    assert persisted.last_error == "收口状态等待人工核对"


def test_explicit_restart_archives_an_idle_campaign(monkeypatch, tmp_path):
    store = CampaignStateStore(tmp_path / "campaign.json")
    original = store.create_or_resume(now=datetime(2026, 7, 17, tzinfo=UTC))
    original = original.model_copy(update={"last_completed_bar_ms": 123456})
    store.save(original)
    history = tmp_path / "history"
    monkeypatch.setattr(campaign_module, "CAMPAIGN_HISTORY_DIR", history)

    restarted = store.restart(
        reason="configuration changed",
        now=datetime(2026, 7, 17, 1, tzinfo=UTC),
    )

    assert restarted.campaign_id != original.campaign_id
    assert restarted.last_completed_bar_ms == 123456
    archives = list(history.glob("*.json"))
    assert len(archives) == 1
    archived = campaign_module.json.loads(archives[0].read_text(encoding="utf-8"))
    assert archived["reason"] == "configuration changed"
    assert archived["state"]["campaign_id"] == original.campaign_id


def test_explicit_restart_allows_only_durable_terminal_owned_executions(
    monkeypatch,
    tmp_path,
):
    store = CampaignStateStore(tmp_path / "campaign.json")
    original = store.create_or_resume(now=datetime(2026, 7, 17, tzinfo=UTC))
    store.save(
        original.model_copy(
            update={
                "execution_ids": [
                    "closed-id",
                    "blocked-id",
                    "canceled-id",
                ]
            }
        )
    )
    history = tmp_path / "history"
    monkeypatch.setattr(campaign_module, "CAMPAIGN_HISTORY_DIR", history)
    executions = {
        "closed-id": SimpleNamespace(state=ExecutionState.CLOSED),
        "blocked-id": SimpleNamespace(state=ExecutionState.BLOCKED),
        "canceled-id": SimpleNamespace(state=ExecutionState.CANCELED),
    }

    restarted = store.restart(
        reason="configuration changed",
        now=datetime(2026, 7, 17, 1, tzinfo=UTC),
        execution_lookup=executions.get,
    )

    assert restarted.campaign_id != original.campaign_id
    assert len(list(history.glob("*.json"))) == 1


def test_restart_carries_forward_terminal_inflight_execution_bar(
    monkeypatch,
    tmp_path,
):
    previous_bar_ms = 1_784_913_800_000
    inflight_bar_ms = 1_784_918_400_000
    store = CampaignStateStore(tmp_path / "campaign.json")
    original = store.create_or_resume(now=datetime(2026, 7, 17, tzinfo=UTC))
    store.save(
        original.model_copy(
            update={
                "inflight_bar_ms": inflight_bar_ms,
                "last_completed_bar_ms": previous_bar_ms,
                "execution_ids": ["blocked-id"],
                "last_execution_id": "blocked-id",
            }
        )
    )
    monkeypatch.setattr(
        campaign_module,
        "CAMPAIGN_HISTORY_DIR",
        tmp_path / "history",
    )
    monkeypatch.setattr(
        campaign_module,
        "_campaign_execution_bar_ms",
        lambda _execution: inflight_bar_ms,
    )

    restarted = store.restart(
        reason="configuration changed",
        now=datetime(2026, 7, 17, 1, tzinfo=UTC),
        execution_lookup=lambda _execution_id: SimpleNamespace(
            state=ExecutionState.BLOCKED
        ),
    )

    assert restarted.last_completed_bar_ms == inflight_bar_ms


@pytest.mark.parametrize(
    "execution",
    [
        None,
        SimpleNamespace(state=ExecutionState.READY),
        SimpleNamespace(state=ExecutionState.UNKNOWN),
        SimpleNamespace(state=ExecutionState.ERROR),
        SimpleNamespace(
            state=ExecutionState.CLOSED,
            needs_attention=True,
        ),
        SimpleNamespace(
            state=ExecutionState.BLOCKED,
            needs_attention=True,
        ),
        SimpleNamespace(
            state=ExecutionState.CANCELED,
            needs_attention=True,
        ),
        SimpleNamespace(
            state=ExecutionState.REJECTED,
            needs_attention=True,
        ),
    ],
)
def test_explicit_restart_rejects_missing_or_nonterminal_owned_execution(
    monkeypatch,
    tmp_path,
    execution,
):
    store = CampaignStateStore(tmp_path / "campaign.json")
    original = store.create_or_resume(now=datetime(2026, 7, 17, tzinfo=UTC))
    store.save(original.model_copy(update={"execution_ids": ["owned-id"]}))
    monkeypatch.setattr(
        campaign_module,
        "CAMPAIGN_HISTORY_DIR",
        tmp_path / "history",
    )

    with pytest.raises(CampaignError, match="未确认终态"):
        store.restart(
            reason="configuration changed",
            now=datetime(2026, 7, 17, 1, tzinfo=UTC),
            execution_lookup=lambda _execution_id: execution,
        )


def test_campaign_process_lock_rejects_second_runner(tmp_path):
    path = tmp_path / "campaign.lock"

    with (
        CampaignProcessLock(path),
        pytest.raises(CampaignError, match="已有 OKX Demo"),
        CampaignProcessLock(path),
    ):
        raise AssertionError("第二个实验进程不应取得文件锁")


def test_dynamic_sizing_uses_stop_loss_risk_and_contract_spec():
    class _Client:
        def account_config(self):
            return {"posMode": "net_mode"}

        def instruments(self, inst_type):
            assert inst_type == "SWAP"
            return [
                {
                    "instId": CAMPAIGN_INSTRUMENT,
                    "state": "live",
                    "tickSz": "0.1",
                    "minSz": "1",
                    "lotSz": "1",
                    "ctVal": "0.001",
                    "ctMult": "1",
                }
            ]

        def balance(self):
            return [
                {
                    "totalEq": "5000",
                    "details": [{"ccy": "USDT", "eq": "5000"}],
                }
            ]

        def ticker(self, instrument):
            assert instrument == CAMPAIGN_INSTRUMENT
            return {"last": "4000"}

        def max_order_size(
            self,
            *,
            instrument,
            trade_mode,
            price=None,
            leverage=None,
        ):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert trade_mode == "cross"
            return {"maxBuy": "100000", "maxSell": "100000"}

    sizing = resolve_campaign_sizing(
        _Client(),
        entry_price="4000",
        stop_loss_price="3990",
        side="long",
        risk_capital_cap_usdt="5000",
        risk_percent="0.10",
    )

    assert sizing.equity_usdt == Decimal("5000")
    assert sizing.risk_budget_usdt == Decimal("500")
    assert sizing.contract_notional_usdt == Decimal("4")
    assert sizing.stop_distance_usdt == Decimal("10")
    assert sizing.quantity == Decimal("22742")


def test_fixed_quantity_campaign_keeps_quantity_and_derives_risk():
    class _Client:
        def account_config(self):
            return {"posMode": "net_mode"}

        def instruments(self, inst_type):
            assert inst_type == "SWAP"
            return [
                {
                    "instId": CAMPAIGN_INSTRUMENT,
                    "state": "live",
                    "tickSz": "0.1",
                    "minSz": "1",
                    "lotSz": "1",
                    "ctVal": "0.001",
                    "ctMult": "1",
                }
            ]

        def balance(self):
            return [
                {
                    "totalEq": "5000",
                    "details": [{"ccy": "USDT", "eq": "5000"}],
                }
            ]

        def max_order_size(self, **_kwargs):
            return {"maxBuy": "100000", "maxSell": "100000"}

    sizing = resolve_campaign_sizing(
        _Client(),
        entry_price="4000",
        stop_loss_price="3990",
        side="long",
        risk_capital_cap_usdt="5000",
        risk_percent="0.10",
        sizing_mode="fixed_quantity",
        fixed_quantity="120",
    )

    assert sizing.sizing_mode == "fixed_quantity"
    assert sizing.quantity == Decimal("120")
    assert sizing.risk_budget_usdt == sizing.risk_used_usdt
    assert sizing.risk_percent == (
        sizing.risk_used_usdt / Decimal("5000")
    )


def test_higher_timeframe_text_cannot_change_gate_or_risk_quantity():
    class _Client:
        def account_config(self):
            return {"posMode": "net_mode"}

        def instruments(self, inst_type):
            assert inst_type == "SWAP"
            return [
                {
                    "instId": CAMPAIGN_INSTRUMENT,
                    "state": "live",
                    "tickSz": "0.1",
                    "minSz": "1",
                    "lotSz": "1",
                    "ctVal": "0.001",
                    "ctMult": "1",
                }
            ]

        def balance(self):
            return [
                {
                    "totalEq": "5000",
                    "details": [{"ccy": "USDT", "eq": "5000"}],
                }
            ]

        def max_order_size(
            self,
            *,
            instrument,
            trade_mode,
            price=None,
            leverage=None,
        ):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert trade_mode == "cross"
            return {"maxBuy": "100000", "maxSell": "100000"}

    without_htf = _record(symbol=CAMPAIGN_SYMBOL).model_copy(
        update={"htf_text": "", "analysis_atr14": 5.4},
        deep=True,
    )
    with_htf = without_htf.model_copy(
        update={
            "htf_text": (
                "1h 方向：bearish；4h 方向：bearish。"
                "仅作背景，不直接否决 10m。"
            )
        },
        deep=True,
    )

    plain = resolve_record_campaign_sizing(
        without_htf,
        _Client(),
        risk_capital_cap_usdt="5000",
        risk_percent="0.10",
    )
    contextual = resolve_record_campaign_sizing(
        with_htf,
        _Client(),
        risk_capital_cap_usdt="5000",
        risk_percent="0.10",
    )
    decision = without_htf.stage2_decision["decision"]
    signal_entry = Decimal(str(decision["entry_price"]))
    expected_entry = (
        signal_entry + Decimal("2.7")
        if decision["order_direction"] == "做多"
        else signal_entry - Decimal("2.7")
    )

    assert without_htf.stage1_diagnosis["gate_result"] == "proceed"
    assert with_htf.stage1_diagnosis["gate_result"] == "proceed"
    assert plain.reference_price_usdt == expected_entry
    assert contextual.quantity == plain.quantity
    assert contextual.risk_used_usdt == plain.risk_used_usdt


def test_controlled_demo_s_uses_real_10m_record_and_expands_stop_for_capacity():
    base = _record(symbol=CAMPAIGN_SYMBOL).model_copy(deep=True)
    base.meta = base.meta.model_copy(
        update={
            "timeframe": CAMPAIGN_TIMEFRAME,
            "data_source": "okx",
            "market_data_provenance": "okx_5m_utc_pair_aggregation",
        }
    )
    base.kline_data = [
        {
            "seq": 1,
            "ts_open": 1_784_826_000_000,
            "open": 4000,
            "high": 4010,
            "low": 3990,
            "close": 4005,
            "volume": 100,
            "closed": True,
        }
    ]
    base.analysis_atr14 = 5.4
    base.stage1_diagnosis = {
        **base.stage1_diagnosis,
        "direction": "bullish",
        "support_levels": [3980],
    }

    class _Client:
        def account_config(self):
            return {"posMode": "net_mode"}

        def instruments(self, inst_type):
            assert inst_type == "SWAP"
            return [
                {
                    "instId": CAMPAIGN_INSTRUMENT,
                    "state": "live",
                    "tickSz": "0.1",
                    "minSz": "1",
                    "lotSz": "1",
                    "ctVal": "0.001",
                    "ctMult": "1",
                }
            ]

        def balance(self):
            return [
                {
                    "totalEq": "5000",
                    "details": [{"ccy": "USDT", "eq": "5000"}],
                }
            ]

        def ticker(self, instrument):
            assert instrument == CAMPAIGN_INSTRUMENT
            return {
                "last": "4000",
                "askPx": "4000.1",
                "bidPx": "3999.9",
            }

        def max_order_size(
            self,
            *,
            instrument,
            trade_mode,
            price=None,
            leverage=None,
        ):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert trade_mode == "cross"
            return {"maxBuy": "21000", "maxSell": "21000"}

    record, sizing = build_controlled_demo_s_record(
        base,
        client=_Client(),
        risk_capital_cap_usdt="5000",
        risk_percent="0.10",
        now=datetime(2026, 7, 24, 1, 12, tzinfo=UTC),
    )

    decision = record.stage2_decision["decision"]
    assert record.meta.timeframe == "10m"
    assert record.meta.market_data_provenance == (
        "okx_public_5m_utc_pair_aggregation_controlled_reproducible"
    )
    assert record.stage2_decision["origin"] == "controlled_reproducible_demo_s"
    assert "controlled_reproducible_demo_s" in record.strategy_files_used
    assert decision["trade_confidence"] == 20
    assert record.stage1_diagnosis["input_mode"] == "controlled_reproducible"
    assert record.stage1_diagnosis["gate_result"] == "proceed"
    assert record.stage1_diagnosis["direction"] == "bullish"
    assert decision["order_direction"] == "做多"
    assert record.stage1_diagnosis["controlled_basis"]["analysis_atr14"] == "5.4"
    assert record.stage1_diagnosis["controlled_basis"][
        "executable_reference_price"
    ] == "4000.1"
    assert record.stage1_diagnosis["controlled_basis"][
        "effective_limit_price"
    ] == "4000.1"
    assert (
        record.stage1_diagnosis["controlled_basis"]["entry_level_source"]
        == "okx_live_ask_effective_limit"
    )
    assert Decimal(decision["entry_price"]) == Decimal("3997.4")
    assert record.stage1_response["base_stage1_diagnosis"] == (
        base.stage1_diagnosis
    )
    assert record.stage2_response["base_stage2_decision"] == (
        base.stage2_decision
    )
    assert Decimal(
        record.stage1_diagnosis["controlled_basis"]["effective_limit_price"]
    ) - Decimal(
        decision["stop_loss_price"]
    ) > Decimal("10.8")
    assert sizing.quantity <= sizing.max_buy
    assert sizing.quantity != sizing.max_buy
    assert record.stage2_response["risk_sizing"] == {
        "sizing_mode": "risk_budget",
        "equity_basis": "fixed_cap_or_usdt_equity_whichever_lower",
        "account_total_equity_usd": "5000",
        "equity_usdt": "5000",
        "risk_capital_cap_usdt": "5000",
        "effective_risk_capital_usdt": "5000",
        "risk_percent": "0.10",
        "risk_budget_usdt": "500.00",
        "risk_used_usdt": str(sizing.risk_used_usdt),
        "reference_price_usdt": "4000.1",
        "stop_distance_usdt": str(sizing.stop_distance_usdt),
        "contract_notional_usdt": str(sizing.contract_notional_usdt),
        "worst_case_loss_per_contract_usdt": str(
            sizing.worst_case_loss_per_contract_usdt
        ),
        "fee_per_contract_usdt": str(sizing.fee_per_contract_usdt),
        "slippage_per_contract_usdt": str(
            sizing.slippage_per_contract_usdt
        ),
        "fee_rate": "0.0005",
        "slippage_rate": "0.0010",
        "minimum_quantity": "1",
        "quantity_step": "1",
        "max_buy": "21000",
        "max_sell": "21000",
        "target_quantity": str(sizing.quantity),
    }


def test_demo_s_skips_newer_controlled_record_and_selects_natural_10m(
    monkeypatch,
    tmp_path,
):
    campaign_id = "11111111-1111-4111-8111-111111111111"
    natural = _record(symbol=CAMPAIGN_SYMBOL).model_copy(deep=True)
    natural.meta = natural.meta.model_copy(
        update={
            "timeframe": CAMPAIGN_TIMEFRAME,
            "data_source": "okx",
            "market_data_provenance": "okx_5m_utc_pair_aggregation",
            "campaign_id": campaign_id,
        }
    )
    natural.kline_data = [{"ts_open": 1_784_826_600_000, "closed": True}]
    controlled = natural.model_copy(deep=True)
    controlled.meta = controlled.meta.model_copy(
        update={
            "timestamp_local_iso": "2026-07-24T01:30:00+08:00",
            "timestamp_local_ms": 1_784_831_400_000,
            "market_data_provenance": (
                "okx_public_5m_utc_pair_aggregation_controlled_reproducible"
            ),
        }
    )
    controlled.stage2_decision = {
        "origin": "controlled_reproducible_demo_s",
        "decision": controlled.stage2_decision["decision"],
    }
    foreign = natural.model_copy(deep=True)
    foreign.meta = foreign.meta.model_copy(
        update={
            "timestamp_local_iso": "2026-07-24T01:31:00+08:00",
            "timestamp_local_ms": 1_784_831_460_000,
            "campaign_id": "22222222-2222-4222-8222-222222222222",
        }
    )
    writer = PendingWriter(tmp_path)
    writer.save_full_durable(natural)
    writer.save_full_durable(controlled)
    writer.save_full_durable(foreign)
    monkeypatch.setattr(campaign_module, "RECORDS_PENDING_DIR", tmp_path)

    selected = find_latest_natural_campaign_record(campaign_id)

    assert selected is not None
    assert selected.meta.campaign_id == campaign_id
    assert selected.meta.market_data_provenance == "okx_5m_utc_pair_aggregation"


def test_dynamic_sizing_rejects_non_net_position_mode():
    class _Client:
        def account_config(self):
            return {"posMode": "long_short_mode"}

        def instruments(self, _inst_type):
            raise AssertionError("非 net_mode 不应读取后续定仓数据")

    with pytest.raises(CampaignError, match="net_mode"):
        resolve_campaign_sizing(
            _Client(),
            entry_price="4000",
            stop_loss_price="3990",
            side="long",
            risk_capital_cap_usdt="5000",
            risk_percent="0.10",
        )


def test_private_preflight_always_uses_demo_header(monkeypatch):
    captured = {}

    class _Client:
        def __init__(self, credentials, *, base_url, simulated):
            del credentials
            captured["base_url"] = base_url
            captured["simulated_arg"] = simulated
            self.simulated = simulated

        def sync_server_time(self):
            return 3

        def account_config(self):
            return {
                "uid": "u",
                "mainUid": "m",
                "type": "1",
                "posMode": "net_mode",
            }

        def instruments(self, inst_type):
            assert inst_type == "SWAP"
            return [
                {
                    "instId": CAMPAIGN_INSTRUMENT,
                    "state": "live",
                    "minSz": "1",
                    "lotSz": "1",
                    "ctVal": "0.001",
                    "ctMult": "1",
                }
            ]

        def max_order_size(
            self,
            *,
            instrument,
            trade_mode,
            price=None,
            leverage=None,
        ):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert trade_mode == "cross"
            return {"maxBuy": "500", "maxSell": "500"}

        def balance(self):
            return [
                {
                    "totalEq": "5000",
                    "details": [{"ccy": "USDT", "eq": "5000"}],
                }
            ]

        def ticker(self, instrument):
            assert instrument == CAMPAIGN_INSTRUMENT
            return {"last": "4000"}

        def positions(self, *, instrument):
            assert instrument == CAMPAIGN_INSTRUMENT
            return []

        def leverage_info(self, *, instrument, margin_mode):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert margin_mode == "cross"
            return [{"lever": "10"}]

        def candles(self, *, instrument, bar, limit):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert bar == "5m"
            assert limit == 4
            return [
                [
                    "1784304300000",
                    "4005",
                    "4012",
                    "4000",
                    "4008",
                    "20",
                    "0.02",
                    "80160",
                    "1",
                ],
                [
                    "1784304000000",
                    "4000",
                    "4010",
                    "3990",
                    "4005",
                    "10",
                    "0.01",
                    "40050",
                    "1",
                ]
            ]

    monkeypatch.setattr(
        campaign_module,
        "load_okx_credentials",
        lambda environment: object(),
    )
    monkeypatch.setattr(campaign_module, "OkxRestClient", _Client)

    result = okx_demo_private_preflight()

    assert captured == {
        "base_url": CAMPAIGN_OKX_API_BASE_URL,
        "simulated_arg": True,
    }
    assert result["simulated"] is True
    assert result["max_buy"] == "500"
    assert result["max_sell"] == "500"
    assert result["risk_percent"] == str(CAMPAIGN_EQUITY_FRACTION)
    assert result["risk_quantity"] == "requires_entry_and_stop"


def test_okx_campaign_source_uses_execution_instrument_prices():
    class _MarketClient:
        def __init__(self):
            self.calls = []
            self.instrument_calls = []

        def public_instruments(self, inst_type, *, instrument=None):
            self.instrument_calls.append((inst_type, instrument))
            return [
                {
                    "instId": CAMPAIGN_INSTRUMENT,
                    "state": "live",
                    "tickSz": "0.1",
                }
            ]

        def candles(self, *, instrument, bar, limit):
            self.calls.append((instrument, bar, limit))
            if bar == "5m":
                return [
                    [
                        "1784306400000",
                        "4008",
                        "4013",
                        "4001",
                        "4010",
                        "5",
                        "0.005",
                        "20050",
                        "0",
                    ],
                    [
                        "1784306100000",
                        "4005",
                        "4012",
                        "4000",
                        "4008",
                        "20",
                        "0.02",
                        "80160",
                        "1",
                    ],
                    [
                        "1784305800000",
                        "4000",
                        "4010",
                        "3990",
                        "4005",
                        "10",
                        "0.01",
                        "40050",
                        "1",
                    ],
                ]
            return [
                [
                    "1784305800000",
                    "4005",
                    "4012",
                    "4000",
                    "4008",
                    "20",
                    "0.02",
                    "80160",
                    "0",
                ],
                [
                    "1784304000000",
                    "4000",
                    "4010",
                    "3990",
                    "4005",
                    "10",
                    "0.01",
                    "40050",
                    "1",
                ],
            ]

    client = _MarketClient()
    source = OkxCampaignSource(client)
    source.subscribe(CAMPAIGN_SYMBOL, CAMPAIGN_TIMEFRAME)

    assert source.price_tick() == "0.1"
    assert source.price_tick() == "0.1"
    bars = source.latest_snapshot(150)
    bars_1h = source.latest_snapshot_for_timeframe("1h", 150)
    bars_4h = source.latest_snapshot_for_timeframe("4h", 150)

    assert client.calls == [
        (CAMPAIGN_INSTRUMENT, "5m", 300),
        (CAMPAIGN_INSTRUMENT, "1H", 150),
        (CAMPAIGN_INSTRUMENT, "4H", 150),
    ]
    assert client.instrument_calls == [
        ("SWAP", CAMPAIGN_INSTRUMENT),
    ]
    assert len(bars) == 1
    assert len(bars_1h) == 2
    assert len(bars_4h) == 2
    assert bars[0].seq == 1
    assert bars[0].closed is True
    assert bars[0].open == 4000.0
    assert bars[0].close == 4008.0


def test_new_bar_is_processed_once(monkeypatch, tmp_path):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    record = SimpleNamespace(exception=None)
    orchestrator = _FakeOrchestrator(record)
    service = _FakeExecutionService()
    # 本用例只验证同一根 K 线幂等，不应在 not-live 套件中调用 OKX 私有预检。
    service.is_armed = True
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(orchestrator, service),
        store,
        state,
    )

    assert runner.process_latest_closed_bar() is True
    assert runner.process_latest_closed_bar() is False
    assert orchestrator.calls == 1
    assert service.prepared == [record]
    assert service.submitted == ["execution-1"]
    assert runner.state.last_completed_bar_ms == bar_ms
    assert runner.state.executions_prepared == 1
    assert runner.state.execution_ids == ["execution-1"]
    assert runner.runtime.settings.execution.okx.quantity == "120"
    assert service.disarm_calls == 1
    assert service.is_armed is False


def test_new_bar_recovers_allowlisted_transient_risk_stop_once(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    record = SimpleNamespace(exception=None)
    orchestrator = _FakeOrchestrator(record)
    service = _FakeExecutionService()
    service.is_armed = True
    service.worker_store.risk_state = SimpleNamespace(
        kill_active=True,
        kill_reason="risk_runtime_BrokerTransportError",
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(orchestrator, service),
        store,
        state,
    )

    assert runner.process_latest_closed_bar() is True
    assert runner.process_latest_closed_bar() is False
    assert service.transient_risk_recoveries == 1
    assert orchestrator.calls == 1
    assert service.submitted == ["execution-1"]


def test_failed_transient_risk_recovery_skips_bar_without_analysis_or_order(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    service = _FakeExecutionService()
    service.worker_store.risk_state = SimpleNamespace(
        kill_active=True,
        kill_reason="risk_runtime_IncompleteRead",
    )
    service.transient_risk_recovery_status = WorkerCommandStatus.FAILED
    service.transient_risk_recovery_failure_code = "BrokerTransportError"
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(orchestrator, service),
        store,
        state,
    )

    assert runner.process_latest_closed_bar() is True
    assert runner.process_latest_closed_bar() is False
    assert service.transient_risk_recoveries == 1
    assert orchestrator.calls == 0
    assert service.prepared == []
    assert service.submitted == []
    assert runner.state.last_completed_bar_ms == bar_ms
    assert (
        runner.state.last_plan_result
        == "blocked:risk:transient_read_unavailable"
    )
    assert runner.state.last_error == "风险账户读取尚未恢复，本轮不下单"


def test_persisted_transient_recovery_command_is_reused_without_second_enqueue(
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    service = _FakeExecutionService()
    command = service._command("recover_transient_risk_stop")
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    state = state.model_copy(
        update={
            "risk_recovery_bar_ms": bar_ms,
            "risk_recovery_command_id": command.id,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace()), service),
        store,
        state,
    )

    assert runner._recover_transient_risk_stop_for_bar(bar_ms)
    assert service.transient_risk_recoveries == 0
    assert service.waited_command_ids == [command.id]
    assert runner.state.risk_recovery_bar_ms is None
    assert runner.state.risk_recovery_command_id == ""


def test_transient_recovery_crash_before_command_id_skips_bar_without_enqueue(
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    service = _FakeExecutionService()
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    state = state.model_copy(
        update={
            "risk_recovery_bar_ms": bar_ms,
            "risk_recovery_command_id": "",
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace()), service),
        store,
        state,
    )

    assert not runner._recover_transient_risk_stop_for_bar(bar_ms)
    assert service.transient_risk_recoveries == 0
    assert service.waited_command_ids == []
    assert runner.state.last_completed_bar_ms == bar_ms
    assert (
        runner.state.last_plan_result
        == "blocked:risk:recovery_command_unconfirmed"
    )


def test_transient_recovery_timeout_keeps_same_durable_command_for_retry(
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    service = _FakeExecutionService()
    service.worker_store.risk_state = SimpleNamespace(
        kill_active=True,
        kill_reason="risk_runtime_BrokerTransportError",
    )
    service.wait_error = TimeoutError("still running")
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace()), service),
        store,
        state,
    )

    with pytest.raises(
        DataSourceTransientError,
        match="同一条命令",
    ):
        runner._recover_transient_risk_stop_for_bar(bar_ms)
    command_id = runner.state.risk_recovery_command_id
    with pytest.raises(
        DataSourceTransientError,
        match="同一条命令",
    ):
        runner._recover_transient_risk_stop_for_bar(bar_ms)

    assert service.transient_risk_recoveries == 1
    assert service.waited_command_ids == [command_id, command_id]
    assert runner.state.risk_recovery_bar_ms == bar_ms


@pytest.mark.parametrize(
    ("execution_state", "needs_attention"),
    [
        (ExecutionState.ERROR, False),
        (ExecutionState.OPEN, True),
    ],
)
def test_campaign_restart_never_advances_execution_requiring_manual_review(
    tmp_path,
    execution_state,
    needs_attention,
):
    bar_ms = 1_784_300_400_000
    execution = SimpleNamespace(
        id="owned-execution",
        state=execution_state,
        needs_attention=needs_attention,
        plan=SimpleNamespace(),
    )
    service = _FakeExecutionService(records={execution.id: execution})
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    state = state.model_copy(update={"execution_ids": [execution.id]})
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace()), service),
        store,
        state,
    )
    runner._execution_bar_ms = lambda _execution: bar_ms

    with pytest.raises(CampaignError, match="需要人工核对"):
        runner._recover_owned_execution_for_bar(bar_ms)
    assert runner.state.last_completed_bar_ms is None


def test_submit_risk_block_is_a_completed_bar_and_campaign_keeps_running(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    record = SimpleNamespace(exception=None)
    orchestrator = _FakeOrchestrator(record)

    class _RiskBlockedSubmitService(_FakeExecutionService):
        def submit(self, execution_id):
            self.submitted.append(execution_id)
            self.store.records[execution_id] = SimpleNamespace(
                id=execution_id,
                state=ExecutionState.BLOCKED,
                state_reason="资金流/回撤风险闸门阻断新增风险",
                last_error="risk_runtime_BrokerTransportError",
            )
            return self._command(
                "submit",
                status=WorkerCommandStatus.FAILED,
                failure_code="submit_result_needs_attention",
            )

    service = _RiskBlockedSubmitService()
    service.is_armed = True
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(orchestrator, service),
        store,
        state,
    )

    assert runner.process_latest_closed_bar() is True
    assert runner.process_latest_closed_bar() is False
    assert runner.state.status == "active"
    assert runner.state.inflight_bar_ms is None
    assert runner.state.last_completed_bar_ms == bar_ms
    assert (
        runner.state.last_plan_result
        == "blocked:submit:submit_result_needs_attention"
    )
    assert (
        runner.state.last_error
        == "资金流/回撤风险闸门阻断新增风险"
    )
    assert service.disarm_calls == 1
    assert service.is_armed is False


def test_submit_releases_new_risk_lease_after_uncertain_terminal(
    monkeypatch, tmp_path
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    service = _FakeExecutionService()
    service.is_armed = True
    original_submit = service.submit

    def _submit(execution_id):
        command = original_submit(execution_id)
        command.status = WorkerCommandStatus.UNCERTAIN
        return command

    service.submit = _submit
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(
            _FakeOrchestrator(SimpleNamespace(exception=None)),
            service,
        ),
        store,
        state,
    )

    with pytest.raises(CampaignError, match="结果不明"):
        runner.process_latest_closed_bar()

    assert service.submitted == ["execution-1"]
    assert service.disarm_calls == 1
    assert service.is_armed is False


@pytest.mark.parametrize(
    "wait_error",
    [
        pytest.param(TimeoutError("测试等待超时"), id="timeout"),
        pytest.param(KeyError("测试命令读取失败"), id="read_error"),
    ],
)
def test_submit_keeps_new_risk_lease_when_wait_has_no_terminal_result(
    wait_error,
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    service = _FakeExecutionService()
    service.is_armed = True
    service.wait_error = wait_error
    original_submit = service.submit

    def _submit(execution_id):
        command = original_submit(execution_id)
        command.status = WorkerCommandStatus.RUNNING
        return command

    service.submit = _submit
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(
            _FakeOrchestrator(SimpleNamespace(exception=None)),
            service,
        ),
        store,
        state,
    )

    with pytest.raises(type(wait_error), match=str(wait_error.args[0])):
        runner.process_latest_closed_bar()

    assert service.submitted == ["execution-1"]
    assert service.disarm_calls == 0
    assert service.is_armed is True


def test_submit_creation_failure_releases_lease_and_preserves_error(
    monkeypatch, tmp_path
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    service = _FakeExecutionService()
    service.is_armed = True

    def _submit(_execution_id):
        raise RuntimeError("submit command creation failed")

    service.submit = _submit
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(
            _FakeOrchestrator(SimpleNamespace(exception=None)),
            service,
        ),
        store,
        state,
    )

    with pytest.raises(
        RuntimeError,
        match="submit command creation failed",
    ):
        runner.process_latest_closed_bar()

    assert service.disarm_calls == 1
    assert service.is_armed is False


def test_uncertain_terminal_preserved_when_lease_release_fails(
    monkeypatch, tmp_path
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    service = _FakeExecutionService()
    service.is_armed = True
    service.disarm_error = RuntimeError("lease revoke failed")
    original_submit = service.submit

    def _submit(execution_id):
        command = original_submit(execution_id)
        command.status = WorkerCommandStatus.UNCERTAIN
        return command

    service.submit = _submit
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(
            _FakeOrchestrator(SimpleNamespace(exception=None)),
            service,
        ),
        store,
        state,
    )

    with pytest.raises(
        CampaignError,
        match=r"命令状态=uncertain.*释放异常=RuntimeError",
    ) as caught:
        runner.process_latest_closed_bar()

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert service.disarm_calls == 1
    assert service.is_armed is True


def test_integrity_risk_stop_is_never_auto_recovered(tmp_path):
    service = _FakeExecutionService()
    service.worker_store.risk_state = SimpleNamespace(
        kill_active=True,
        kill_reason="risk_runtime_account_identity_changed",
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace()), service),
        store,
        state,
    )

    assert runner._recover_transient_risk_stop_for_bar(1_784_300_400_000)
    assert service.transient_risk_recoveries == 0


def test_new_bar_passes_thin_higher_timeframe_context(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda: None,
    )
    bar_ms = 1_784_300_400_000

    def _build_frame(*args, **kwargs):
        del kwargs
        base = _frame(bar_ms)
        return KlineFrame(
            symbol=base.symbol,
            timeframe=args[3],
            bars=base.bars,
            indicators=base.indicators,
            snapshot_ts_local_ms=base.snapshot_ts_local_ms,
        )

    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        _build_frame,
    )

    class _MtfSource:
        def __init__(self):
            self.calls = []

        def latest_snapshot(self, count):
            self.calls.append((CAMPAIGN_TIMEFRAME, count))
            return [object()]

        def price_tick(self):
            return "0.1"

        def latest_snapshot_for_timeframe(self, timeframe, count):
            self.calls.append((timeframe, count))
            return [object()]

        def disconnect(self):
            return None

    class _MtfOrchestrator:
        def __init__(self):
            self.context = ""

        def submit(self, frame, token, on_event, **kwargs):
            del token, on_event
            self.context = kwargs["higher_timeframe_text"]
            return SimpleNamespace(
                exception=None,
                meta=SimpleNamespace(
                    symbol=CAMPAIGN_SYMBOL,
                    timeframe=CAMPAIGN_TIMEFRAME,
                    data_source="okx",
                    market_data_provenance=(
                        "okx_5m_utc_pair_aggregation"
                    ),
                    campaign_id=kwargs["campaign_id"],
                ),
                kline_data=[
                    {
                        "ts_open": frame.bars[0].ts_open,
                        "closed": True,
                    }
                ],
            )

    source = _MtfSource()
    orchestrator = _MtfOrchestrator()
    service = _FakeExecutionService()
    runtime = CampaignRuntime(
        settings=_settings(),
        source=source,
        writer=SimpleNamespace(),
        orchestrator=orchestrator,
        execution_service=service,
        supervisor=_FakeSupervisor(),
        sizing_resolver=_sizing,
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert source.calls == [("10m", 150), ("1h", 100), ("4h", 100)]
    assert "主周期=10m" in orchestrator.context
    assert "背景 1h" in orchestrator.context
    assert "背景 4h" in orchestrator.context


def test_normal_script_does_not_call_monitor_for_routine_entry(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    runtime, service, client, record = _supervised_runtime(tmp_path, "block_entry")
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert client.calls == []
    assert len(service.prepared) == 1
    assert service.submitted == ["execution-1"]
    assert runner.state.last_plan_result == "execution:entry_pending"
    persisted = list((tmp_path / "supervisor").glob("*.json"))
    assert persisted == []
    assert record.stage2_decision["decision"]["order_type"] == "限价单"


def test_normal_script_creates_one_plan_and_restart_reuses_conclusion(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    runtime, service, client, _ = _supervised_runtime(tmp_path, "allow_entry")
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert len(service.prepared) == 1
    assert service.submitted == ["execution-1"]
    assert client.calls == []

    restarted_state = store.load()
    assert restarted_state is not None
    restarted = OkxDemoCampaign(runtime, store, restarted_state)
    assert restarted.process_latest_closed_bar() is False
    assert len(service.prepared) == 1
    assert service.submitted == ["execution-1"]
    assert client.calls == []


def test_normal_script_holds_same_direction_execution_without_new_order(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    runtime, service, monitor_client, record = _supervised_runtime(
        tmp_path,
        "block_entry",
    )
    direction = OkxDemoCampaign._record_order_direction(record)
    active = SimpleNamespace(
        id="existing-open",
        state=ExecutionState.OPEN,
        plan=SimpleNamespace(direction=direction),
    )
    service.store.active_batches = [[active]]
    service.store.records[active.id] = active
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC)).model_copy(
        update={"execution_ids": [active.id], "last_execution_id": active.id}
    )
    store.save(state)
    runner = OkxDemoCampaign(runtime, store, state)
    monkeypatch.setattr(
        runner,
        "_recover_owned_execution_for_bar",
        lambda _bar_ms: False,
    )

    assert runner.process_latest_closed_bar() is True

    assert service.exited == []
    assert service.canceled == []
    assert service.prepared == []
    assert service.submitted == []
    assert monitor_client.calls == []
    assert runner.state.last_plan_result == "script:hold:open"


def test_normal_script_closes_opposite_execution_before_reversal(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    runtime, service, monitor_client, record = _supervised_runtime(
        tmp_path,
        "block_entry",
    )
    proposed = OkxDemoCampaign._record_order_direction(record)
    active = SimpleNamespace(
        id="existing-open",
        state=ExecutionState.OPEN,
        plan=SimpleNamespace(
            direction="short" if proposed == "long" else "long"
        ),
    )
    service.store.active_batches = [[active]]
    service.store.records[active.id] = active
    original_request_exit = service.request_exit

    def _request_exit(execution_id, *, reason):
        command = original_request_exit(execution_id, reason=reason)
        service.store.records[execution_id] = SimpleNamespace(
            id=execution_id,
            state=ExecutionState.CLOSED,
            plan=active.plan,
        )
        return command

    service.request_exit = _request_exit
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC)).model_copy(
        update={"execution_ids": [active.id], "last_execution_id": active.id}
    )
    store.save(state)
    runner = OkxDemoCampaign(runtime, store, state)
    monkeypatch.setattr(
        runner,
        "_recover_owned_execution_for_bar",
        lambda _bar_ms: False,
    )

    assert runner.process_latest_closed_bar() is True

    assert service.exited == [
        (active.id, "PA 已收盘 K 线出现反向可执行信号")
    ]
    assert len(service.prepared) == 1
    assert service.submitted == ["execution-1"]
    assert monitor_client.calls == []
    assert runner.state.last_plan_result == "execution:entry_pending"


def test_normal_script_handles_partial_fill_race_before_reversal(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    runtime, service, monitor_client, record = _supervised_runtime(
        tmp_path,
        "block_entry",
    )
    proposed = OkxDemoCampaign._record_order_direction(record)
    active = SimpleNamespace(
        id="existing-entry-pending",
        state=ExecutionState.ENTRY_PENDING,
        plan=SimpleNamespace(
            direction="short" if proposed == "long" else "long"
        ),
    )
    service.store.active_batches = [[active]]
    service.store.records[active.id] = active
    original_cancel_entry = service.cancel_entry
    original_request_exit = service.request_exit

    def _cancel_entry(execution_id):
        command = original_cancel_entry(execution_id)
        service.store.records[execution_id] = SimpleNamespace(
            id=execution_id,
            state=ExecutionState.PARTIALLY_FILLED,
            plan=active.plan,
        )
        return command

    def _request_exit(execution_id, *, reason):
        command = original_request_exit(execution_id, reason=reason)
        service.store.records[execution_id] = SimpleNamespace(
            id=execution_id,
            state=ExecutionState.CLOSED,
            plan=active.plan,
        )
        return command

    original_wait_for_reconcile = service.wait_for_reconcile

    def _wait_for_reconcile(*, after, timeout):
        service.store.records[active.id] = SimpleNamespace(
            id=active.id,
            state=ExecutionState.OPEN,
            plan=active.plan,
        )
        return original_wait_for_reconcile(after=after, timeout=timeout)

    service.cancel_entry = _cancel_entry
    service.request_exit = _request_exit
    service.wait_for_reconcile = _wait_for_reconcile
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC)).model_copy(
        update={"execution_ids": [active.id], "last_execution_id": active.id}
    )
    store.save(state)
    runner = OkxDemoCampaign(runtime, store, state)
    monkeypatch.setattr(
        runner,
        "_recover_owned_execution_for_bar",
        lambda _bar_ms: False,
    )

    assert runner.process_latest_closed_bar() is True

    assert service.canceled == [active.id]
    assert service.exited == [
        (active.id, "PA 已收盘 K 线出现反向可执行信号")
    ]
    assert service.submitted == ["execution-1"]
    assert monitor_client.calls == []


def test_balance_change_after_plan_creation_expires_old_plan_before_submit(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    runtime, service, _client, _record_value = _supervised_runtime(
        tmp_path,
        "allow_entry",
    )
    sizing_calls = 0

    def _changing_sizing(record):
        nonlocal sizing_calls
        sizing_calls += 1
        initial = _sizing(record)
        if sizing_calls == 2:
            return replace(
                initial,
                equity_usdt=Decimal("2500"),
                risk_budget_usdt=Decimal("250"),
                risk_used_usdt=Decimal("6"),
                quantity=Decimal("60"),
            )
        return initial

    runtime.sizing_resolver = _changing_sizing
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert len(service.prepared) == 1
    assert service.submitted == []
    assert service.expired == [
        ("execution-1", "USDT 风险快照变化，旧计划禁止提交")
    ]
    assert runner.state.last_plan_result == "blocked:risk:stale_risk_sizing"
    assert "旧计划禁止提交" in runner.state.last_error
    assert service.disarm_calls == 2
    assert service.is_armed is False


def test_balance_change_above_fixed_cap_keeps_authorized_plan(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    runtime, service, _client, _record_value = _supervised_runtime(
        tmp_path,
        "allow_entry",
    )
    sizing_calls = 0

    def _changing_raw_equity_only(record):
        nonlocal sizing_calls
        sizing_calls += 1
        initial = replace(
            _sizing(record),
            equity_usdt=Decimal("8000"),
        )
        if sizing_calls == 2:
            return replace(
                initial,
                equity_usdt=Decimal("9000"),
                max_buy=Decimal("700000"),
                max_sell=Decimal("700000"),
            )
        return initial

    runtime.sizing_resolver = _changing_raw_equity_only
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert len(service.prepared) == 1
    assert service.submitted == ["execution-1"]
    assert service.expired == []


def test_risk_size_exceeded_blocks_current_bar_and_next_closed_bar_continues(
    monkeypatch,
    tmp_path,
):
    first_bar_ms = 1_784_300_400_000
    second_bar_ms = first_bar_ms + 10 * 60 * 1000
    current_bar_ms = {"value": first_bar_ms}
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(current_bar_ms["value"]),
    )
    runtime, service, supervisor_client, _ = _supervised_runtime(
        tmp_path,
        "allow_entry",
    )
    sizing_calls = 0

    def _resolve_sizing(record):
        nonlocal sizing_calls
        sizing_calls += 1
        if sizing_calls == 1:
            raise CampaignRiskBlocked(
                "max_size_exceeded",
                "风险定仓失败[max_size_exceeded]: 按止损风险计算出的数量超过 OKX 当前最大可开数量",
                required_size=Decimal("580000"),
                maximum_size=Decimal("120000"),
            )
        return _sizing(record)

    runtime.sizing_resolver = _resolve_sizing

    def _reject_non_monotonic_leverage(_record, _analysis_digest):
        raise LeveragePlanningFailure(
            "non_monotonic_capacity",
            "OKX 容量曲线不是单调递增，禁止自动调整杠杆",
        )

    runtime.leverage_resolver = _reject_non_monotonic_leverage
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert runner.state.status == "active"
    assert runner.state.inflight_bar_ms is None
    assert runner.state.last_completed_bar_ms == first_bar_ms
    assert (
        runner.state.last_plan_result
        == "blocked:risk:leverage:non_monotonic_capacity"
    )
    assert "容量曲线不是单调递增" in runner.state.last_error
    assert supervisor_client.calls == []
    assert service.prepared == []
    assert service.submitted == []

    current_bar_ms["value"] = second_bar_ms
    runtime.orchestrator.record.kline_data[0]["ts_open"] = second_bar_ms
    runtime.orchestrator.record.stage2_response.pop(
        "leverage_intent",
        None,
    )

    assert runner.process_latest_closed_bar() is True
    assert runner.state.status == "active"
    assert runner.state.last_completed_bar_ms == second_bar_ms
    assert runner.state.last_plan_result == "execution:entry_pending"
    assert supervisor_client.calls == []
    assert service.prepared
    assert service.submitted == ["execution-1"]


def test_campaign_changes_leverage_from_durable_script_and_rechecks_sizing(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    runtime, service, supervisor_client, _ = _supervised_runtime(
        tmp_path,
        "allow_entry",
    )
    preflights = []
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda: preflights.append("okx-demo") or {},
    )
    target_sizing = _sizing(quantity="580000")
    sizing_calls = 0

    def _resolve_sizing(record):
        nonlocal sizing_calls
        sizing_calls += 1
        if sizing_calls == 1:
            raise CampaignRiskBlocked(
                "max_size_exceeded",
                "风险目标数量超过当前容量",
                required_size=target_sizing.quantity,
                maximum_size=Decimal("120000"),
            )
        return target_sizing

    runtime.sizing_resolver = _resolve_sizing
    runtime.leverage_resolver = (
        lambda _record, _digest: _leverage_candidate(target_sizing)
    )
    original_set_leverage = service.set_leverage

    def _set_leverage_after_script_authorization(parameters):
        assert supervisor_client.calls == []
        assert parameters.supervisor_record_id == ""
        assert parameters.supervisor_record_path == ""
        assert parameters.supervisor_record_digest == ""
        return original_set_leverage(parameters)

    service.set_leverage = _set_leverage_after_script_authorization
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert sizing_calls == 3
    assert len(service.leverage_parameters) == 1
    assert service.leverage_parameters[0].analysis_digest != "a" * 64
    assert len(service.leverage_parameters[0].analysis_digest) == 64
    assert service.leverage_parameters[0].required_quantity == Decimal("580000")
    assert service.leverage_parameters[0].supervisor_record_id == ""
    assert len(service.prepared) == 1
    assert service.submitted == ["execution-1"]
    assert runner.state.last_plan_result == "execution:entry_pending"
    assert preflights == ["okx-demo"]
    assert service.arm_calls == ["启用模拟交易", "启用模拟交易"]
    assert service.disarm_calls == 3
    assert service.is_armed is False


@pytest.mark.parametrize(
    ("command_status", "wait_error", "expected_error", "expected_message"),
    [
        pytest.param(
            WorkerCommandStatus.FAILED,
            None,
            None,
            "",
            id="failed",
        ),
        pytest.param(
            WorkerCommandStatus.UNCERTAIN,
            None,
            CampaignError,
            "结果不明",
            id="uncertain",
        ),
        pytest.param(
            WorkerCommandStatus.RUNNING,
            TimeoutError("测试等待超时"),
            TimeoutError,
            "测试等待超时",
            id="timeout",
        ),
    ],
)
def test_leverage_releases_only_after_durable_terminal(
    command_status,
    wait_error,
    expected_error,
    expected_message,
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    runtime, service, _supervisor_client, _ = _supervised_runtime(
        tmp_path,
        "allow_entry",
    )
    target_sizing = _sizing(quantity="580000")

    def _resolve_sizing(record):
        raise CampaignRiskBlocked(
            "max_size_exceeded",
            "当前杠杆容量不足",
            required_size=target_sizing.quantity,
            maximum_size=Decimal("120000"),
        )

    runtime.sizing_resolver = _resolve_sizing
    runtime.leverage_resolver = (
        lambda _record, _digest: _leverage_candidate(target_sizing)
    )
    service.wait_error = wait_error
    original_set_leverage = service.set_leverage

    def _set_leverage(parameters):
        command = original_set_leverage(parameters)
        command.status = command_status
        command.failure_code = "test_failure"
        return command

    service.set_leverage = _set_leverage
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(runtime, store, state)

    if expected_error is None:
        assert runner.process_latest_closed_bar() is True
        assert runner.state.last_plan_result == (
            "blocked:risk:leverage:test_failure"
        )
    else:
        with pytest.raises(expected_error, match=expected_message):
            runner.process_latest_closed_bar()

    assert len(service.leverage_parameters) == 1
    assert service.prepared == []
    assert service.submitted == []
    if wait_error is None:
        assert service.disarm_calls == 2
        assert service.is_armed is False
    else:
        assert service.disarm_calls == 1
        assert service.is_armed is True


def test_campaign_balance_jump_after_leverage_blocks_bar_and_keeps_running(
    monkeypatch,
    tmp_path,
):
    first_bar_ms = 1_784_300_400_000
    second_bar_ms = first_bar_ms + 10 * 60 * 1000
    current_bar_ms = {"value": first_bar_ms}
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(current_bar_ms["value"]),
    )
    runtime, service, _supervisor_client, _ = _supervised_runtime(
        tmp_path,
        "allow_entry",
    )
    preflights = []
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda: preflights.append("okx-demo") or {},
    )
    target_sizing = _sizing(quantity="580000")
    sizing_calls = 0

    def _resolve_sizing(record):
        nonlocal sizing_calls
        sizing_calls += 1
        if sizing_calls == 1:
            raise CampaignRiskBlocked(
                "max_size_exceeded",
                "当前杠杆容量不足",
                required_size=target_sizing.quantity,
                maximum_size=Decimal("120000"),
            )
        if sizing_calls == 2:
            raise CampaignRiskBlocked(
                "max_size_exceeded",
                "余额增加后风险目标数量再次超过新容量",
                required_size=Decimal("700000"),
                maximum_size=target_sizing.quantity,
            )
        return _sizing(record)

    runtime.sizing_resolver = _resolve_sizing
    runtime.leverage_resolver = (
        lambda _record, _digest: _leverage_candidate(target_sizing)
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert runner.state.status == "active"
    assert runner.state.last_plan_result == (
        "blocked:risk:after_leverage:max_size_exceeded"
    )
    assert service.prepared == []
    assert service.submitted == []
    assert len(service.leverage_parameters) == 1
    assert preflights == []
    assert service.disarm_calls == 2
    assert service.is_armed is False

    current_bar_ms["value"] = second_bar_ms
    runtime.orchestrator.record.kline_data[0]["ts_open"] = second_bar_ms
    runtime.orchestrator.record.stage2_response.pop(
        "leverage_intent",
        None,
    )

    assert runner.process_latest_closed_bar() is True
    assert runner.state.status == "active"
    assert runner.state.last_completed_bar_ms == second_bar_ms
    assert service.submitted == ["execution-1"]
    assert preflights == ["okx-demo"]
    assert service.arm_calls == ["启用模拟交易", "启用模拟交易"]
    assert service.disarm_calls == 3
    assert service.is_armed is False


def test_campaign_rearms_only_after_demo_read_check(monkeypatch, tmp_path):
    service = _FakeExecutionService()
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )
    monkeypatch.delenv("PA_AGENT_PAPER_TRADING_ENABLED", raising=False)
    preflights = []
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda: preflights.append("okx-demo") or {},
    )

    runner._ensure_demo_write_session()

    assert preflights == ["okx-demo"]
    assert service.arm_calls == ["启用模拟交易"]
    assert service.is_armed is True
    assert "PA_AGENT_PAPER_TRADING_ENABLED" not in campaign_module.os.environ


def test_campaign_does_not_rearm_when_demo_read_check_is_unreachable(
    monkeypatch,
    tmp_path,
):
    service = _FakeExecutionService()
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda: (_ for _ in ()).throw(
            BrokerTransportError(
                "temporary network failure",
                write_may_have_reached=False,
            )
        ),
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )

    with pytest.raises(DataSourceTransientError):
        runner._ensure_demo_write_session()

    assert service.arm_calls == []
    assert service.is_armed is False


def test_campaign_keeps_analyzing_while_new_risk_lease_is_owned_elsewhere(
    monkeypatch,
    tmp_path,
):
    service = _FakeExecutionService()
    arm_attempts = 0

    def arm(confirmation):
        nonlocal arm_attempts
        arm_attempts += 1
        if arm_attempts == 1:
            raise NewRiskLeaseUnavailable("租约暂时被其他会话占用")
        service.is_armed = True

    service.arm = arm
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda: {},
    )
    clock = {"now": datetime(2026, 7, 17, 1, tzinfo=UTC)}
    monkeypatch.setattr(campaign_module, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(
        campaign_module.time,
        "sleep",
        lambda seconds: clock.__setitem__(
            "now", clock["now"] + timedelta(seconds=seconds)
        ),
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, clock["now"])
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )
    monkeypatch.setattr(
        runner,
        "process_latest_closed_bar",
        lambda: clock.__setitem__("now", runner.state.expires_at_utc) or True,
    )
    monkeypatch.setattr(runner, "close_out", lambda: True)

    assert runner.run() is True
    assert arm_attempts == 0


def test_same_or_older_bar_is_never_processed(monkeypatch, tmp_path):
    completed_bar_ms = 1_784_300_400_000
    older_bar_ms = completed_bar_ms - 30 * 60 * 1000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(older_bar_ms),
    )
    record = SimpleNamespace(exception=None)
    orchestrator = _FakeOrchestrator(record)
    service = _FakeExecutionService()
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC)).model_copy(
        update={"last_completed_bar_ms": completed_bar_ms}
    )
    store.save(state)
    runner = OkxDemoCampaign(_runtime(orchestrator, service), store, state)

    assert runner.process_latest_closed_bar() is False
    assert orchestrator.calls == 0
    assert service.prepared == []
    assert service.submitted == []


def test_model_result_after_deadline_never_prepares_or_submits(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    started = datetime(2026, 7, 17, tzinfo=UTC)
    expired_now = started + CAMPAIGN_DURATION + timedelta(seconds=1)
    record = SimpleNamespace(exception=None)
    orchestrator = _FakeOrchestrator(record)
    service = _FakeExecutionService()
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, started)
    runner = OkxDemoCampaign(_runtime(orchestrator, service), store, state)
    monkeypatch.setattr(campaign_module, "_utc_now", lambda: expired_now)

    assert runner.process_latest_closed_bar() is True
    assert orchestrator.calls == 1
    assert service.prepared == []
    assert service.submitted == []
    assert runner.state.last_plan_result == "blocked:campaign_expired"
    assert runner.state.last_completed_bar_ms == bar_ms


def test_no_order_is_recorded_without_execution(monkeypatch, tmp_path):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    service = _FakeExecutionService(
        block=PlanBlocked("no_order", "PA 决策为不下单")
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runtime = _runtime(orchestrator, service)
    sizing_calls = []

    def _unexpected_sizing(record):
        sizing_calls.append(record)
        raise AssertionError("不下单记录不应因余额变化进入风险定仓")

    runtime.sizing_resolver = _unexpected_sizing
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert runner.state.last_plan_result == "blocked:no_order"
    assert runner.state.executions_prepared == 0
    assert service.submitted == []
    assert sizing_calls == []


def test_transient_model_failure_skips_bar_without_stopping_campaign(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    record = SimpleNamespace(
        exception={
            "type": "network_error",
            "stage": "stage2",
            "message": "Codex 订阅服务暂时不可用",
        }
    )
    orchestrator = _FakeOrchestrator(record)
    service = _FakeExecutionService()
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(_runtime(orchestrator, service), store, state)

    assert runner.process_latest_closed_bar() is True
    assert runner.state.analyses_failed == 1
    assert runner.state.last_completed_bar_ms == bar_ms
    assert runner.state.inflight_bar_ms is None
    assert runner.state.last_plan_result == "failed:network_error"
    assert service.prepared == []
    assert service.submitted == []


def test_claim_validation_blocks_one_bar_and_next_bar_continues(
    monkeypatch,
    tmp_path,
):
    first_bar_ms = 1_784_300_400_000
    second_bar_ms = first_bar_ms + 10 * 60 * 1000
    current_bar = {"ms": first_bar_ms}
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(current_bar["ms"]),
    )

    claim_record = SimpleNamespace(
        exception={
            "type": "claim_validation",
            "stage": "stage2",
            "code": "price_out_of_range",
            "message": "TP2 超出真实包络",
            "invalid_fields": [
                "claim_validation:price_out_of_range:"
                "decision.take_profit_price_2:bad price"
            ],
        }
    )
    next_record = SimpleNamespace(exception=None)

    class _SequentialOrchestrator(_FakeOrchestrator):
        def __init__(self):
            super().__init__(claim_record)
            self.records = [claim_record, next_record]

        def submit(self, frame, token, on_event, **kwargs):
            self.record = self.records[self.calls]
            return super().submit(frame, token, on_event, **kwargs)

    orchestrator = _SequentialOrchestrator()
    service = _FakeExecutionService(
        block=PlanBlocked("no_order", "PA 决策为不下单")
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(_runtime(orchestrator, service), store, state)

    assert runner.process_latest_closed_bar() is True
    assert (
        runner.state.last_plan_result
        == "blocked:claim_validation:price_out_of_range"
    )
    assert runner.state.analyses_failed == 1
    assert service.prepared == []
    assert service.submitted == []

    current_bar["ms"] = second_bar_ms
    assert runner.process_latest_closed_bar() is True
    assert orchestrator.calls == 2
    assert runner.state.last_completed_bar_ms == second_bar_ms
    assert runner.state.last_plan_result == "blocked:no_order"
    assert runner.state.analyses_failed == 1


def test_inflight_claim_failure_recovers_durable_partial_without_model_call(
    monkeypatch,
    tmp_path,
):
    from pa_agent.orchestrator.two_stage import _build_empty_record

    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    monkeypatch.setattr(campaign_module, "RECORDS_PENDING_DIR", tmp_path)

    settings = _settings()
    store = CampaignStateStore(tmp_path / "campaign.json")
    started = datetime(2026, 7, 17, tzinfo=UTC)
    state = _state(store, started).model_copy(
        update={"inflight_bar_ms": bar_ms}
    )
    store.save(state)
    record = _build_empty_record(
        _frame(bar_ms),
        settings,
        campaign_id=state.campaign_id,
    ).model_copy(
        update={
            "exception": {
                "type": "claim_validation",
                "stage": "stage1",
                "category": "c",
                "code": "bar_reference_out_of_range",
                "message": "K999 超出当前帧",
                "invalid_fields": [
                    "claim_validation:bar_reference_out_of_range:"
                    "gate_trace[0].bar_range:K999"
                ],
            }
        }
    )
    writer = PendingWriter(tmp_path)
    writer.save_partial_durable(
        record,
        "stage1_claim_validation_bar_reference_out_of_range",
    )
    ownerless = record.model_copy(
        update={
            "meta": record.meta.model_copy(
                update={
                    "timestamp_local_ms": record.meta.timestamp_local_ms + 1,
                    "campaign_id": None,
                }
            )
        }
    )
    writer.save_partial_durable(
        ownerless,
        "stage1_claim_validation_bar_reference_out_of_range",
    )

    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    service = _FakeExecutionService()
    runtime = _runtime(orchestrator, service)
    runtime.settings = settings
    runtime.writer = writer
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert orchestrator.calls == 0
    assert runner.state.analyses_failed == 1
    assert (
        runner.state.last_plan_result
        == "blocked:claim_validation:bar_reference_out_of_range"
    )
    assert service.prepared == []
    assert service.submitted == []


def test_stale_inflight_claim_is_closed_before_latest_bar_analysis(
    monkeypatch,
    tmp_path,
):
    from pa_agent.orchestrator.two_stage import _build_empty_record

    old_bar_ms = 1_784_300_400_000
    latest_bar_ms = old_bar_ms + 10 * 60 * 1000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(latest_bar_ms),
    )
    monkeypatch.setattr(campaign_module, "RECORDS_PENDING_DIR", tmp_path)

    settings = _settings()
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(update={"inflight_bar_ms": old_bar_ms})
    store.save(state)
    record = _build_empty_record(
        _frame(old_bar_ms),
        settings,
        campaign_id=state.campaign_id,
    ).model_copy(
        update={
            "exception": {
                "type": "claim_validation",
                "stage": "stage1",
                "category": "c",
                "code": "bar_reference_out_of_range",
                "message": "K999 超出当前帧",
                "invalid_fields": [
                    "claim_validation:bar_reference_out_of_range:"
                    "gate_trace[0].bar_range:K999"
                ],
            }
        }
    )
    writer = PendingWriter(tmp_path)
    writer.save_partial_durable(
        record,
        "stage1_claim_validation_bar_reference_out_of_range",
    )

    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    service = _FakeExecutionService(
        block=PlanBlocked("no_order", "PA 决策为不下单")
    )
    runtime = _runtime(orchestrator, service)
    runtime.settings = settings
    runtime.writer = writer
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert orchestrator.calls == 0
    assert runner.state.last_completed_bar_ms == old_bar_ms
    assert (
        runner.state.last_plan_result
        == "blocked:claim_validation:bar_reference_out_of_range"
    )

    assert runner.process_latest_closed_bar() is True
    assert orchestrator.calls == 1
    assert runner.state.last_completed_bar_ms == latest_bar_ms
    assert runner.state.last_plan_result == "blocked:no_order"


def test_stale_inflight_success_is_closed_without_execution_or_model_rerun(
    monkeypatch,
    tmp_path,
):
    old_bar_ms = 1_784_300_400_000
    latest_bar_ms = old_bar_ms + 10 * 60 * 1000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(latest_bar_ms),
    )
    monkeypatch.setattr(campaign_module, "RECORDS_PENDING_DIR", tmp_path)

    started = datetime(2026, 7, 17, tzinfo=UTC)
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, started).model_copy(
        update={"inflight_bar_ms": old_bar_ms}
    )
    store.save(state)
    base = _record(symbol=CAMPAIGN_SYMBOL)
    durable_record = base.model_copy(
        update={
            "meta": base.meta.model_copy(
                update={
                    "timestamp_local_ms": int(
                        (started + timedelta(minutes=1)).timestamp() * 1000
                    ),
                    "symbol": CAMPAIGN_SYMBOL,
                    "timeframe": CAMPAIGN_TIMEFRAME,
                    "data_source": "okx",
                    "market_data_provenance": (
                        "okx_5m_utc_pair_aggregation"
                    ),
                    "decision_stance": CAMPAIGN_STANCE,
                    "campaign_id": state.campaign_id,
                }
            ),
            "kline_data": [{"ts_open": old_bar_ms, "closed": True}],
        }
    )
    writer = PendingWriter(tmp_path)
    writer.save_full_durable(durable_record)

    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    service = _FakeExecutionService(
        block=PlanBlocked("no_order", "PA 决策为不下单")
    )
    runtime = _runtime(orchestrator, service)
    runtime.writer = writer
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert orchestrator.calls == 0
    assert service.prepared == []
    assert service.submitted == []
    assert runner.state.last_completed_bar_ms == old_bar_ms
    assert (
        runner.state.last_plan_result
        == "blocked:stale_recovered_analysis"
    )
    assert runner.state.analyses_completed == 0

    assert runner.process_latest_closed_bar() is True
    assert orchestrator.calls == 1
    assert runner.state.last_completed_bar_ms == latest_bar_ms
    assert runner.state.last_plan_result == "blocked:no_order"


def test_campaign_fails_closed_on_ownerless_inflight_claim_record(
    monkeypatch,
    tmp_path,
):
    from pa_agent.orchestrator.two_stage import _build_empty_record

    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    monkeypatch.setattr(campaign_module, "RECORDS_PENDING_DIR", tmp_path)

    settings = _settings()
    interactive = _build_empty_record(
        _frame(bar_ms),
        settings,
    ).model_copy(
        update={
            "exception": {
                "type": "claim_validation",
                "stage": "stage1",
                "category": "c",
                "code": "price_out_of_range",
                "message": "交互式记录",
                "invalid_fields": [
                    "claim_validation:price_out_of_range:"
                    "support_levels[0]:bad"
                ],
            }
        }
    )
    PendingWriter(tmp_path).save_partial_durable(
        interactive,
        "stage1_claim_validation_price_out_of_range",
    )

    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(update={"inflight_bar_ms": bar_ms})
    store.save(state)
    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    service = _FakeExecutionService(
        block=PlanBlocked("no_order", "PA 决策为不下单")
    )
    runner = OkxDemoCampaign(
        _runtime(orchestrator, service),
        store,
        state,
    )

    with pytest.raises(CampaignError, match="缺少 campaign_id"):
        runner.process_latest_closed_bar()

    assert orchestrator.calls == 0
    assert service.prepared == []
    assert runner.state.inflight_bar_ms == bar_ms


def test_campaign_fails_closed_on_ownerless_inflight_success(
    monkeypatch,
    tmp_path,
):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    monkeypatch.setattr(campaign_module, "RECORDS_PENDING_DIR", tmp_path)
    started = datetime(2026, 7, 17, tzinfo=UTC)
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, started).model_copy(
        update={"inflight_bar_ms": bar_ms}
    )
    store.save(state)
    base = _record(symbol=CAMPAIGN_SYMBOL)
    ownerless = base.model_copy(
        update={
            "meta": base.meta.model_copy(
                update={
                    "timestamp_local_ms": int(
                        (started + timedelta(minutes=1)).timestamp() * 1000
                    ),
                    "symbol": CAMPAIGN_SYMBOL,
                    "timeframe": CAMPAIGN_TIMEFRAME,
                    "data_source": "okx",
                    "market_data_provenance": (
                        "okx_5m_utc_pair_aggregation"
                    ),
                    "decision_stance": CAMPAIGN_STANCE,
                    "campaign_id": None,
                }
            ),
            "kline_data": [{"ts_open": bar_ms, "closed": True}],
        }
    )
    PendingWriter(tmp_path).save_full_durable(ownerless)
    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    service = _FakeExecutionService()
    runner = OkxDemoCampaign(
        _runtime(orchestrator, service),
        store,
        state,
    )

    with pytest.raises(CampaignError, match="缺少 campaign_id"):
        runner.process_latest_closed_bar()

    assert orchestrator.calls == 0
    assert service.prepared == []
    assert runner.state.inflight_bar_ms == bar_ms


def test_inflight_bar_reuses_durable_record(monkeypatch, tmp_path):
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda: None,
    )
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    monkeypatch.setattr(campaign_module, "RECORDS_PENDING_DIR", tmp_path)
    started = datetime(2026, 7, 17, tzinfo=UTC)
    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    service = _FakeExecutionService()
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, started).model_copy(update={"inflight_bar_ms": bar_ms})
    store.save(state)
    base = _record(symbol=CAMPAIGN_SYMBOL)
    durable_record = base.model_copy(
        update={
            "meta": base.meta.model_copy(
                update={
                    "timestamp_local_ms": int(
                        (started + timedelta(minutes=1)).timestamp() * 1000
                    ),
                    "symbol": CAMPAIGN_SYMBOL,
                    "timeframe": CAMPAIGN_TIMEFRAME,
                    "data_source": "okx",
                    "market_data_provenance": (
                        "okx_5m_utc_pair_aggregation"
                    ),
                    "decision_stance": CAMPAIGN_STANCE,
                    "campaign_id": state.campaign_id,
                }
            ),
            "kline_data": [{"ts_open": bar_ms, "closed": True}],
        }
    )
    writer = PendingWriter(tmp_path)
    writer.save_full_durable(durable_record)
    runtime = _runtime(orchestrator, service)
    runtime.writer = writer
    runner = OkxDemoCampaign(
        runtime,
        store,
        state,
    )

    assert runner.process_latest_closed_bar() is True
    assert orchestrator.calls == 0
    assert len(service.prepared) == 1
    prepared_record = service.prepared[0]
    assert prepared_record.meta == durable_record.meta
    assert prepared_record.kline_data == durable_record.kline_data
    assert prepared_record.stage2_decision == durable_record.stage2_decision
    assert "risk_sizing" in prepared_record.stage2_response
    assert service.submitted == ["execution-1"]
    assert runner.state.analyses_completed == 0


def test_stale_ready_plan_is_expired_before_new_bar(monkeypatch, tmp_path):
    current_bar_ms = 1_784_302_200_000
    stale_bar_ms = current_bar_ms - 30 * 60 * 1000
    ready = SimpleNamespace(id="ready-old", state=ExecutionState.READY)
    service = _FakeExecutionService(records={ready.id: ready})
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC)).model_copy(
        update={
            "execution_ids": [ready.id],
            "inflight_bar_ms": current_bar_ms,
            "last_completed_bar_ms": stale_bar_ms - 30 * 60 * 1000,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )
    monkeypatch.setattr(runner, "_execution_bar_ms", lambda execution: stale_bar_ms)

    assert runner._recover_owned_ready_for_bar(current_bar_ms) is False
    assert service.submitted == []
    assert service.expired == [
        (ready.id, "新的已收盘 K 线已出现，未提交计划已过期")
    ]
    assert runner.state.last_completed_bar_ms == stale_bar_ms


def test_current_bar_ready_is_submitted_without_new_model_call(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda: None,
    )
    bar_ms = 1_784_302_200_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    ready = SimpleNamespace(id="ready-current", state=ExecutionState.READY)
    service = _FakeExecutionService(records={ready.id: ready})
    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC)).model_copy(
        update={
            "execution_ids": [ready.id],
            "inflight_bar_ms": bar_ms,
            "last_completed_bar_ms": bar_ms - 30 * 60 * 1000,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(_runtime(orchestrator, service), store, state)
    monkeypatch.setattr(runner, "_execution_bar_ms", lambda execution: bar_ms)

    assert runner.process_latest_closed_bar() is True
    assert orchestrator.calls == 0
    assert service.submitted == [ready.id]
    assert runner.state.last_completed_bar_ms == bar_ms
    assert runner.state.inflight_bar_ms is None
    assert service.arm_calls == ["启用模拟交易"]
    assert service.disarm_calls == 1
    assert service.is_armed is False


def test_current_bar_ready_wait_timeout_keeps_new_risk_lease(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda: None,
    )
    bar_ms = 1_784_302_200_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    ready = SimpleNamespace(id="ready-current", state=ExecutionState.READY)
    service = _FakeExecutionService(records={ready.id: ready})
    service.wait_error = TimeoutError("恢复提交等待超时")
    original_submit = service.submit

    def _submit(execution_id):
        command = original_submit(execution_id)
        command.status = WorkerCommandStatus.RUNNING
        return command

    service.submit = _submit
    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    store = CampaignStateStore(tmp_path / "campaign.json")
    previous_bar_ms = bar_ms - 30 * 60 * 1000
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC)).model_copy(
        update={
            "execution_ids": [ready.id],
            "inflight_bar_ms": bar_ms,
            "last_completed_bar_ms": previous_bar_ms,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(_runtime(orchestrator, service), store, state)
    monkeypatch.setattr(runner, "_execution_bar_ms", lambda execution: bar_ms)

    with pytest.raises(TimeoutError, match="恢复提交等待超时"):
        runner.process_latest_closed_bar()

    assert orchestrator.calls == 0
    assert service.submitted == [ready.id]
    assert service.disarm_calls == 0
    assert service.is_armed is True
    assert runner.state.last_completed_bar_ms == previous_bar_ms
    assert runner.state.inflight_bar_ms == bar_ms


def test_closeout_cancels_pending_entry_and_exits_open_position(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(campaign_module.time, "sleep", lambda seconds: None)
    pending = SimpleNamespace(id="pending-1", state=ExecutionState.ENTRY_PENDING)
    opened = SimpleNamespace(id="open-1", state=ExecutionState.OPEN)
    unrelated_demo = SimpleNamespace(
        id="unrelated-demo",
        state=ExecutionState.ENTRY_PENDING,
    )
    unrelated_live = SimpleNamespace(
        id="unrelated-live",
        state=ExecutionState.OPEN,
    )
    service = _FakeExecutionService(
        active_batches=[
            [pending, opened, unrelated_demo, unrelated_live],
            [unrelated_demo, unrelated_live],
        ],
        records={pending.id: pending, opened.id: opened},
    )
    original_wait_for_reconcile = service.wait_for_reconcile
    reconcile_attempts = 0

    def _wait_after_one_timeout(*, after, timeout):
        nonlocal reconcile_attempts
        reconcile_attempts += 1
        if reconcile_attempts == 1:
            raise TimeoutError("等待交易后台完成下一轮券商对账超时")
        if reconcile_attempts == 3:
            service.store.records[pending.id] = SimpleNamespace(
                id=pending.id,
                state=ExecutionState.CANCELED,
            )
            service.store.records[opened.id] = SimpleNamespace(
                id=opened.id,
                state=ExecutionState.CLOSED,
            )
        return original_wait_for_reconcile(after=after, timeout=timeout)

    service.wait_for_reconcile = _wait_after_one_timeout
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(update={"execution_ids": ["pending-1", "open-1"]})
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
        closeout_seconds=60,
    )

    assert runner.close_out() is True
    assert service.canceled == ["pending-1"]
    assert service.exited == [
        ("open-1", "24 小时 OKX Demo 实验到期")
    ]
    assert service.refreshed_execution_ids[-1] == ["open-1"]
    assert service.reconciled_execution_ids
    assert all(ids == [] for ids in service.reconciled_execution_ids)
    assert service.reconcile_commands == 0
    assert service.reconcile_waits > 0
    assert reconcile_attempts == 3
    assert runner.state.status == "completed"


def test_closeout_without_executions_still_writes_final_account_snapshot(
    tmp_path,
):
    service = _FakeExecutionService(active_batches=[[]])
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
        closeout_seconds=60,
    )

    assert runner.close_out() is True
    assert service.refreshed_execution_ids == [[]]
    assert service.refreshed == 1
    assert runner.state.execution_ids == []
    assert runner.state.status == "completed"


@pytest.mark.parametrize(
    "terminal_state",
    [
        ExecutionState.CLOSED,
        ExecutionState.BLOCKED,
        ExecutionState.CANCELED,
        ExecutionState.REJECTED,
    ],
)
def test_monitor_syncs_terminal_execution_without_waiting_for_reconcile(
    terminal_state,
    tmp_path,
):
    execution = SimpleNamespace(
        id=f"demo-s-{terminal_state.value}",
        state=terminal_state,
        needs_attention=terminal_state
        in {ExecutionState.BLOCKED, ExecutionState.REJECTED},
    )
    service = _FakeExecutionService(records={execution.id: execution})

    def _unexpected_wait(*, after, timeout):
        del after, timeout
        raise AssertionError("安全终态不应等待下一轮券商对账")

    service.wait_for_reconcile = _unexpected_wait
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(
        update={
            "execution_ids": [execution.id],
            "last_execution_id": execution.id,
            "last_plan_result": "execution:exit_pending",
            "last_error": "Demo-S 等待终态超时",
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )

    runner._monitor_owned_executions()

    assert runner.state.last_plan_result == f"execution:{terminal_state.value}"
    assert runner.state.last_error == ""
    assert service.reconcile_waits == 0


def test_monitor_timeout_preserves_newer_bar_result(tmp_path):
    active = SimpleNamespace(
        id="open-after-no-order",
        state=ExecutionState.OPEN,
        needs_attention=False,
    )
    service = _FakeExecutionService(records={active.id: active})

    original_wait = service.wait_for_reconcile
    attempts = 0

    def _timeout_then_recover(*, after, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("等待交易后台完成下一轮券商对账超时")
        return original_wait(after=after, timeout=timeout)

    service.wait_for_reconcile = _timeout_then_recover
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(
        update={
            "execution_ids": [active.id],
            "last_execution_id": active.id,
            "last_plan_result": "blocked:no_order",
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )

    with pytest.raises(DataSourceTransientError):
        runner._monitor_owned_executions()

    assert runner.state.last_plan_result == "blocked:no_order"
    assert runner.state.last_error.startswith(
        f"{CAMPAIGN_RECONCILE_TIMEOUT_RESULT}:"
    )

    runner._monitor_owned_executions()

    assert attempts == 2
    assert runner.state.last_plan_result == "blocked:no_order"
    assert runner.state.last_error == ""


def test_monitor_accepts_terminal_state_reached_at_timeout_boundary(tmp_path):
    active = SimpleNamespace(
        id="closing-at-timeout",
        state=ExecutionState.EXIT_PENDING,
        needs_attention=False,
    )
    service = _FakeExecutionService(records={active.id: active})

    def _close_then_timeout(*, after, timeout):
        del after, timeout
        service.store.records[active.id] = SimpleNamespace(
            id=active.id,
            state=ExecutionState.CLOSED,
            needs_attention=False,
        )
        raise TimeoutError("等待交易后台完成下一轮券商对账超时")

    service.wait_for_reconcile = _close_then_timeout
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(
        update={
            "execution_ids": [active.id],
            "last_execution_id": active.id,
            "last_plan_result": "execution:exit_pending",
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )

    runner._monitor_owned_executions()

    assert runner.state.last_plan_result == "execution:closed"
    assert runner.state.last_error == ""


def test_run_keeps_alive_and_writes_nothing_when_active_reconcile_times_out(
    monkeypatch,
    tmp_path,
):
    started = datetime(2026, 7, 17, 1, tzinfo=UTC)
    clock = {"now": started}
    monkeypatch.setattr(campaign_module, "_utc_now", lambda: clock["now"])
    active = SimpleNamespace(
        id="open-timeout",
        state=ExecutionState.OPEN,
        needs_attention=False,
    )
    service = _FakeExecutionService(records={active.id: active})

    def _timeout(*, after, timeout):
        del after, timeout
        service.reconcile_waits += 1
        clock["now"] = runner.state.expires_at_utc
        raise TimeoutError("等待交易后台完成下一轮券商对账超时")

    service.wait_for_reconcile = _timeout
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, started).model_copy(
        update={
            "execution_ids": [active.id],
            "last_execution_id": active.id,
            "last_plan_result": "execution:open",
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )
    processed = []
    monkeypatch.setattr(
        runner,
        "process_latest_closed_bar",
        lambda: processed.append(True) or True,
    )
    monkeypatch.setattr(runner, "close_out", lambda: True)

    assert runner.run() is True
    assert runner.state.status == "active"
    assert runner.state.last_plan_result == CAMPAIGN_RECONCILE_TIMEOUT_RESULT
    assert runner.state.last_error.startswith(
        f"{CAMPAIGN_RECONCILE_TIMEOUT_RESULT}:"
    )
    assert processed == []
    assert service.submitted == []
    assert service.canceled == []
    assert service.exited == []
    assert service.leverage_parameters == []
    assert service.refreshed == 0
    assert service.arm_calls == []
    assert service.reconcile_commands == 0


def test_run_resumes_bar_processing_after_reconcile_recovers(
    monkeypatch,
    tmp_path,
):
    started = datetime(2026, 7, 17, 1, tzinfo=UTC)
    clock = {"now": started}
    monkeypatch.setattr(campaign_module, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(
        campaign_module.time,
        "sleep",
        lambda seconds: clock.__setitem__(
            "now", clock["now"] + timedelta(seconds=seconds)
        ),
    )
    active = SimpleNamespace(
        id="open-recovers",
        state=ExecutionState.OPEN,
        needs_attention=False,
    )
    service = _FakeExecutionService(records={active.id: active})
    original_wait = service.wait_for_reconcile
    attempts = 0

    def _wait_then_recover(*, after, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("等待交易后台完成下一轮券商对账超时")
        return original_wait(after=after, timeout=timeout)

    service.wait_for_reconcile = _wait_then_recover
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, started).model_copy(
        update={
            "execution_ids": [active.id],
            "last_execution_id": active.id,
            "last_plan_result": "execution:open",
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )
    processed = []

    def _process_after_recovery():
        processed.append(True)
        service.store.records[active.id] = SimpleNamespace(
            id=active.id,
            state=ExecutionState.CLOSED,
            needs_attention=False,
        )
        clock["now"] = runner.state.expires_at_utc
        return True

    monkeypatch.setattr(
        runner,
        "process_latest_closed_bar",
        _process_after_recovery,
    )
    monkeypatch.setattr(runner, "close_out", lambda: True)

    assert runner.run() is True
    assert attempts == 2
    assert processed == [True]
    assert runner.state.last_plan_result == "execution:closed"
    assert runner.state.last_error == ""
    assert service.submitted == []
    assert service.canceled == []
    assert service.exited == []
    assert service.leverage_parameters == []


@pytest.mark.parametrize(
    ("execution_state", "needs_attention", "expected_result"),
    [
        (ExecutionState.UNKNOWN, False, "execution:unknown"),
        (ExecutionState.ERROR, False, "execution:error"),
        (
            ExecutionState.OPEN,
            True,
            "blocked:execution:needs_attention",
        ),
    ],
)
def test_monitor_hard_blocks_unsafe_execution_state(
    execution_state,
    needs_attention,
    expected_result,
    tmp_path,
):
    execution = SimpleNamespace(
        id=f"unsafe-{execution_state.value}",
        state=execution_state,
        needs_attention=needs_attention,
    )
    safe_last = SimpleNamespace(
        id="safe-last",
        state=ExecutionState.CLOSED,
        needs_attention=False,
    )
    service = _FakeExecutionService(
        records={
            execution.id: execution,
            safe_last.id: safe_last,
        }
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(
        update={
            "execution_ids": [execution.id, safe_last.id],
            "last_execution_id": safe_last.id,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )

    with pytest.raises(CampaignError, match="需要人工核对"):
        runner._monitor_owned_executions()

    assert runner.state.last_plan_result == expected_result
    assert service.reconcile_waits == 0
    assert service.submitted == []
    assert service.canceled == []
    assert service.exited == []


@pytest.mark.parametrize(
    ("record_state", "expected_result", "expected_message"),
    [
        (None, "blocked:execution:missing", "不存在于执行账本"),
        (
            "corrupted",
            "blocked:execution:invalid_state",
            "不在已核验状态集合",
        ),
    ],
)
def test_monitor_hard_blocks_missing_or_invalid_owned_execution(
    record_state,
    expected_result,
    expected_message,
    tmp_path,
):
    execution_id = "owned-ledger-invalid"
    records = {}
    if record_state is not None:
        records[execution_id] = SimpleNamespace(
            id=execution_id,
            state=record_state,
            needs_attention=False,
        )
    service = _FakeExecutionService(records=records)
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(
        update={
            "execution_ids": [execution_id],
            "last_execution_id": execution_id,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )

    with pytest.raises(CampaignError, match=expected_message):
        runner._monitor_owned_executions()

    persisted = store.load()
    assert persisted is not None
    assert persisted.last_plan_result == expected_result
    assert persisted.last_error == runner.state.last_error
    assert expected_message in persisted.last_error
    assert service.reconcile_waits == 0
    assert service.submitted == []
    assert service.canceled == []
    assert service.exited == []
    assert service.leverage_parameters == []
    assert service.refreshed == 0
    assert service.arm_calls == []


def test_monitor_treats_worker_attention_as_transient_after_ledger_recheck(
    tmp_path,
):
    active = SimpleNamespace(
        id="worker-needs-attention",
        state=ExecutionState.OPEN,
        needs_attention=False,
    )
    service = _FakeExecutionService(records={active.id: active})

    def _worker_attention(*, after, timeout):
        del after, timeout
        raise LiveTradingDisabled("交易后台需要人工处理")

    service.wait_for_reconcile = _worker_attention
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(
        update={
            "execution_ids": [active.id],
            "last_execution_id": active.id,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )

    with pytest.raises(DataSourceTransientError, match="需要人工处理"):
        runner._monitor_owned_executions()

    assert (
        runner.state.last_plan_result
        == CAMPAIGN_RECONCILE_WORKER_ATTENTION_RESULT
    )
    assert service.submitted == []
    assert service.canceled == []
    assert service.exited == []


def test_main_keyboard_interrupt_finishes_closeout_without_stale_stopping(
    monkeypatch,
    tmp_path,
):
    service = _FakeExecutionService(active_batches=[[]])
    store = CampaignStateStore(tmp_path / "campaign.json")
    runtime = _runtime(
        _FakeOrchestrator(SimpleNamespace(exception=None)),
        service,
    )
    settings = _settings()
    lock_state = {"held": False, "entries": 0, "exits": 0}

    class _TrackingCampaignLock:
        def __enter__(self):
            assert lock_state["held"] is False
            lock_state["held"] = True
            lock_state["entries"] += 1
            return self

        def __exit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback
            assert lock_state["held"] is True
            lock_state["held"] = False
            lock_state["exits"] += 1
            return False

    def _interrupt(_runner):
        raise KeyboardInterrupt

    original_disarm = service.disarm

    def _disarm_while_locked():
        assert lock_state["held"] is True
        original_disarm()

    monkeypatch.setattr(service, "disarm", _disarm_while_locked)
    monkeypatch.setattr(campaign_module, "CampaignStateStore", lambda: store)
    monkeypatch.setattr(
        campaign_module,
        "ExecutionStore",
        lambda **_kwargs: SimpleNamespace(get=lambda _execution_id: None),
    )
    monkeypatch.setattr(
        campaign_module,
        "CampaignProcessLock",
        _TrackingCampaignLock,
    )
    monkeypatch.setattr(campaign_module, "load_settings", lambda _path: settings)
    monkeypatch.setattr(campaign_module, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda _settings: {},
    )
    monkeypatch.setattr(
        campaign_module,
        "build_runtime",
        lambda *, base_settings: runtime,
    )
    monkeypatch.setattr(OkxDemoCampaign, "run", _interrupt)

    assert campaign_module.main(["run"]) == 0

    persisted = store.load()
    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.status != "stopping"
    assert persisted.last_error == ""
    assert service.disarm_calls == 1
    assert service.refreshed == 1
    assert lock_state == {"held": False, "entries": 1, "exits": 1}


@pytest.mark.parametrize(
    "status",
    ["stopping", "needs_attention", "completed"],
)
def test_main_preserves_non_active_state_when_resume_is_rejected(
    status,
    monkeypatch,
    tmp_path,
):
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    store.save(
        state.model_copy(
            update={
                "status": status,
                "last_error": "必须保留的原始错误",
            }
        )
    )
    settings = _settings()

    class _NoopCampaignLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback
            return False

    monkeypatch.setattr(campaign_module, "CampaignStateStore", lambda: store)
    monkeypatch.setattr(
        campaign_module,
        "ExecutionStore",
        lambda **_kwargs: SimpleNamespace(get=lambda _execution_id: None),
    )
    monkeypatch.setattr(
        campaign_module,
        "CampaignProcessLock",
        _NoopCampaignLock,
    )
    monkeypatch.setattr(campaign_module, "load_settings", lambda _path: settings)
    monkeypatch.setattr(campaign_module, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda _settings: {},
    )
    monkeypatch.setattr(
        campaign_module,
        "build_runtime",
        lambda **_kwargs: pytest.fail(
            "非 active 状态不得进入运行时构建"
        ),
    )

    assert campaign_module.main(["run"]) == 2

    persisted = store.load()
    assert persisted is not None
    assert persisted.status == status
    assert persisted.last_error == "必须保留的原始错误"


@pytest.mark.parametrize(
    ("execution_state", "interrupt_at"),
    [
        pytest.param(
            ExecutionState.ENTRY_PENDING,
            "cancel",
            id="cancel-entry",
        ),
        pytest.param(
            ExecutionState.OPEN,
            "request_exit",
            id="request-exit",
        ),
        pytest.param(
            None,
            "refresh_wait",
            id="final-refresh-wait",
        ),
    ],
)
def test_main_does_not_repeat_closeout_after_interrupt_during_closeout(
    execution_state,
    interrupt_at,
    monkeypatch,
    tmp_path,
):
    execution = (
        SimpleNamespace(
            id=f"closeout-{interrupt_at}",
            state=execution_state,
            needs_attention=False,
        )
        if execution_state is not None
        else None
    )
    active = [execution] if execution is not None else []
    records = {execution.id: execution} if execution is not None else {}
    service = _FakeExecutionService(
        active_batches=[active],
        records=records,
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    runtime = _runtime(
        _FakeOrchestrator(SimpleNamespace(exception=None)),
        service,
    )
    settings = _settings()
    lock_state = {"held": False, "entries": 0, "exits": 0}
    action_calls = []

    class _TrackingCampaignLock:
        def __enter__(self):
            assert lock_state["held"] is False
            lock_state["held"] = True
            lock_state["entries"] += 1
            return self

        def __exit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback
            assert lock_state["held"] is True
            lock_state["held"] = False
            lock_state["exits"] += 1
            return False

    def _run_closeout(runner):
        if execution is not None:
            runner._save_state(
                execution_ids=[execution.id],
                last_execution_id=execution.id,
            )
        return runner.close_out()

    def _interrupt_cancel(execution_id):
        assert interrupt_at == "cancel"
        assert lock_state["held"] is True
        action_calls.append(("cancel", execution_id))
        service.canceled.append(execution_id)
        raise KeyboardInterrupt("测试收口中断")

    def _interrupt_exit(execution_id, *, reason):
        assert interrupt_at == "request_exit"
        assert lock_state["held"] is True
        action_calls.append(("request_exit", execution_id))
        service.exited.append((execution_id, reason))
        raise KeyboardInterrupt("测试收口中断")

    def _interrupt_refresh_wait(command_id, *, timeout):
        assert interrupt_at == "refresh_wait"
        assert lock_state["held"] is True
        action_calls.append(("refresh_wait", command_id))
        assert timeout == 30.0
        raise KeyboardInterrupt("测试收口中断")

    monkeypatch.setattr(campaign_module, "CampaignStateStore", lambda: store)
    monkeypatch.setattr(
        campaign_module,
        "ExecutionStore",
        lambda **_kwargs: SimpleNamespace(get=lambda _execution_id: None),
    )
    monkeypatch.setattr(
        campaign_module,
        "CampaignProcessLock",
        _TrackingCampaignLock,
    )
    monkeypatch.setattr(campaign_module, "load_settings", lambda _path: settings)
    monkeypatch.setattr(campaign_module, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(
        campaign_module,
        "okx_demo_private_preflight",
        lambda _settings: {},
    )
    monkeypatch.setattr(
        campaign_module,
        "build_runtime",
        lambda *, base_settings: runtime,
    )
    monkeypatch.setattr(OkxDemoCampaign, "run", _run_closeout)
    if interrupt_at == "cancel":
        monkeypatch.setattr(service, "cancel_entry", _interrupt_cancel)
    elif interrupt_at == "request_exit":
        monkeypatch.setattr(service, "request_exit", _interrupt_exit)
    else:
        monkeypatch.setattr(
            service,
            "wait_for_command",
            _interrupt_refresh_wait,
        )

    with pytest.raises(KeyboardInterrupt, match="测试收口中断"):
        campaign_module.main(["run"])

    persisted = store.load()
    assert persisted is not None
    assert persisted.status == "needs_attention"
    assert persisted.last_error == "测试收口中断"
    assert len(action_calls) == 1
    assert service.disarm_calls == 1
    assert service.canceled == (
        [execution.id] if interrupt_at == "cancel" else []
    )
    assert service.exited == (
        [(execution.id, "24 小时 OKX Demo 实验到期")]
        if interrupt_at == "request_exit"
        else []
    )
    assert service.refreshed == (1 if interrupt_at == "refresh_wait" else 0)
    assert lock_state == {"held": False, "entries": 1, "exits": 1}


def test_closeout_write_failure_persists_needs_attention_without_retry(
    monkeypatch,
    tmp_path,
):
    active = SimpleNamespace(
        id="closeout-cancel-failure",
        state=ExecutionState.ENTRY_PENDING,
        needs_attention=False,
    )
    service = _FakeExecutionService(
        active_batches=[[active]],
        records={active.id: active},
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(
        update={
            "execution_ids": [active.id],
            "last_execution_id": active.id,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )
    cancel_calls = []

    def _fail_cancel(execution_id):
        cancel_calls.append(execution_id)
        raise RuntimeError("撤单命令传输失败")

    monkeypatch.setattr(service, "cancel_entry", _fail_cancel)

    with pytest.raises(RuntimeError, match="撤单命令传输失败"):
        runner.close_out()

    persisted = store.load()
    assert persisted is not None
    assert persisted.status == "needs_attention"
    assert persisted.status != "completed"
    assert persisted.last_error == "撤单命令传输失败"
    assert cancel_calls == [active.id]
    assert service.disarm_calls == 1
    assert service.exited == []


@pytest.mark.parametrize(
    ("execution_state", "action"),
    [
        pytest.param(
            ExecutionState.ENTRY_PENDING,
            "cancel",
            id="cancel-entry",
        ),
        pytest.param(
            ExecutionState.OPEN,
            "request_exit",
            id="request-exit",
        ),
    ],
)
@pytest.mark.parametrize(
    ("command_result", "expected_error", "expected_message"),
    [
        pytest.param(
            WorkerCommandStatus.FAILED,
            CampaignError,
            "测试命令失败",
            id="failed",
        ),
        pytest.param(
            WorkerCommandStatus.UNCERTAIN,
            CampaignError,
            "结果不明",
            id="uncertain",
        ),
        pytest.param(
            "timeout",
            TimeoutError,
            "测试等待超时",
            id="timeout",
        ),
    ],
)
def test_closeout_does_not_repeat_failed_or_uncertain_worker_command(
    execution_state,
    action,
    command_result,
    expected_error,
    expected_message,
    monkeypatch,
    tmp_path,
):
    execution = SimpleNamespace(
        id=f"closeout-{action}",
        state=execution_state,
        needs_attention=False,
    )
    service = _FakeExecutionService(
        active_batches=[[execution]],
        records={execution.id: execution},
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(
        update={
            "execution_ids": [execution.id],
            "last_execution_id": execution.id,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )
    action_calls = []

    def _command():
        status = (
            WorkerCommandStatus.RUNNING
            if command_result == "timeout"
            else command_result
        )
        return service._command(
            action,
            status=status,
            failure_code=(
                "测试命令失败"
                if command_result is WorkerCommandStatus.FAILED
                else ""
            ),
        )

    def _cancel(execution_id):
        action_calls.append(("cancel", execution_id))
        service.canceled.append(execution_id)
        return _command()

    def _request_exit(execution_id, *, reason):
        action_calls.append(("request_exit", execution_id))
        service.exited.append((execution_id, reason))
        return _command()

    if command_result == "timeout":
        service.wait_error = TimeoutError("测试等待超时")
    if action == "cancel":
        monkeypatch.setattr(service, "cancel_entry", _cancel)
    else:
        monkeypatch.setattr(service, "request_exit", _request_exit)

    with pytest.raises(expected_error, match=expected_message):
        runner.close_out()

    persisted = store.load()
    assert persisted is not None
    assert persisted.status == "needs_attention"
    assert len(action_calls) == 1
    assert len(service.waited_command_ids) == 1
    assert service.canceled == (
        [execution.id] if action == "cancel" else []
    )
    assert service.exited == (
        [(execution.id, "24 小时 OKX Demo 实验到期")]
        if action == "request_exit"
        else []
    )
    assert service.disarm_calls == 1


@pytest.mark.parametrize(
    ("execution_state", "action"),
    [
        pytest.param(
            ExecutionState.ENTRY_PENDING,
            "cancel",
            id="cancel-entry",
        ),
        pytest.param(
            ExecutionState.OPEN,
            "request_exit",
            id="request-exit",
        ),
    ],
)
def test_closeout_does_not_repeat_succeeded_command_while_state_is_unchanged(
    execution_state,
    action,
    monkeypatch,
    tmp_path,
):
    execution = SimpleNamespace(
        id=f"unchanged-{action}",
        state=execution_state,
        needs_attention=False,
    )
    service = _FakeExecutionService(
        active_batches=[[execution]],
        records={execution.id: execution},
    )
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(
        update={
            "execution_ids": [execution.id],
            "last_execution_id": execution.id,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
        closeout_seconds=1,
    )
    monotonic_values = iter((0.0, 0.0, 0.0, 0.5, 0.5, 2.0))
    monkeypatch.setattr(
        campaign_module.time,
        "monotonic",
        lambda: next(monotonic_values, 2.0),
    )
    monkeypatch.setattr(campaign_module.time, "sleep", lambda _seconds: None)

    assert runner.close_out() is False

    persisted = store.load()
    assert persisted is not None
    assert persisted.status == "needs_attention"
    assert service.canceled == (
        [execution.id] if action == "cancel" else []
    )
    assert service.exited == (
        [(execution.id, "24 小时 OKX Demo 实验到期")]
        if action == "request_exit"
        else []
    )
    assert len(service.waited_command_ids) == 1
    assert service.disarm_calls == 1


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            KeyboardInterrupt("收口再次被人工中断"),
            id="keyboard-interrupt",
        ),
        pytest.param(
            SystemExit("收口收到退出请求"),
            id="system-exit",
        ),
    ],
)
def test_closeout_persists_base_exception_and_reraises(
    failure,
    monkeypatch,
    tmp_path,
):
    service = _FakeExecutionService(active_batches=[[]])
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )

    def _fail_expire(*, reason):
        del reason
        raise failure

    monkeypatch.setattr(runner, "_expire_owned_ready", _fail_expire)

    with pytest.raises(type(failure), match=str(failure)):
        runner.close_out()

    persisted = store.load()
    assert persisted is not None
    assert persisted.status == "needs_attention"
    assert persisted.last_error == str(failure)
    assert service.disarm_calls == 1
    assert service.canceled == []
    assert service.exited == []


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            KeyboardInterrupt("收口被人工中断"),
            id="keyboard-interrupt",
        ),
        pytest.param(
            SystemExit("收口收到退出请求"),
            id="system-exit",
        ),
    ],
)
def test_closeout_state_write_failure_does_not_replace_original_signal(
    failure,
    monkeypatch,
    tmp_path,
):
    service = _FakeExecutionService(active_batches=[[]])
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
    )
    real_save = store.save

    def _fail_expire(*, reason):
        del reason
        raise failure

    def _fail_attention_save(candidate):
        if candidate.status == "needs_attention":
            raise CampaignError("测试状态文件写入失败")
        real_save(candidate)

    monkeypatch.setattr(runner, "_expire_owned_ready", _fail_expire)
    monkeypatch.setattr(store, "save", _fail_attention_save)

    with pytest.raises(type(failure), match=str(failure)):
        runner.close_out()

    persisted = store.load()
    assert persisted is not None
    assert persisted.status == "stopping"
    assert runner.state.status == "needs_attention"
    assert runner.state.last_error == str(failure)
    assert service.disarm_calls == 1
    assert service.canceled == []
    assert service.exited == []


def test_closeout_timeout_is_not_reported_as_completed(tmp_path):
    active = SimpleNamespace(id="unknown-1", state=ExecutionState.UNKNOWN)
    service = _FakeExecutionService(active_batches=[[active]])
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(update={"execution_ids": ["unknown-1"]})
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
        closeout_seconds=0,
    )

    assert runner.close_out() is False
    assert runner.state.status == "needs_attention"
    assert "仍有 1 条活动执行" in runner.state.last_error


@pytest.mark.parametrize(
    ("execution_state", "needs_attention"),
    [
        (ExecutionState.UNKNOWN, False),
        (ExecutionState.OPEN, True),
    ],
)
def test_closeout_persists_needs_attention_for_unsafe_execution(
    execution_state,
    needs_attention,
    tmp_path,
):
    unsafe = SimpleNamespace(
        id=f"closeout-{execution_state.value}",
        state=execution_state,
        needs_attention=needs_attention,
    )
    service = _FakeExecutionService(records={unsafe.id: unsafe})
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(
        store,
        datetime(2026, 7, 17, tzinfo=UTC),
    ).model_copy(
        update={
            "execution_ids": [unsafe.id],
            "last_execution_id": unsafe.id,
        }
    )
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(_FakeOrchestrator(SimpleNamespace(exception=None)), service),
        store,
        state,
        closeout_seconds=60,
    )

    assert runner.close_out() is False
    assert runner.state.status == "needs_attention"
    assert "需要人工核对" in runner.state.last_error
    assert service.submitted == []
    assert service.canceled == []
    assert service.exited == []
