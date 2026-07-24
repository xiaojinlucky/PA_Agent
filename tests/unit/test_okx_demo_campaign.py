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
from pa_agent.execution.worker_protocol import WorkerCommandStatus
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
    CAMPAIGN_STANCE,
    CAMPAIGN_SYMBOL,
    CAMPAIGN_TIMEFRAME,
    CANARY_ORIGIN,
    CANARY_TIMEFRAME,
    CampaignError,
    CampaignProcessLock,
    CampaignRiskBlocked,
    CampaignRuntime,
    CampaignSizing,
    CampaignStateStore,
    OkxCampaignSource,
    OkxDemoCampaign,
    _canary_price_triplet,
    _wait_for_execution_state,
    build_campaign_settings,
    build_controlled_demo_s_record,
    build_demo_canary_record,
    campaign_config_fingerprint,
    find_latest_natural_campaign_record,
    okx_demo_private_preflight,
    resolve_campaign_sizing,
    validate_campaign_settings,
)
from pa_agent.records.analysis_history import find_latest_successful_record
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.records.supervisor_writer import SupervisorWriter
from tests.unit.test_execution_plan_builder import _persist, _record


def _settings(quantity="120"):
    return build_campaign_settings(Settings(), quantity=quantity)


def _sizing(_record=None, quantity="120"):
    del _record
    resolved = Decimal(quantity)
    return CampaignSizing(
        quantity=resolved,
        equity_usdt=Decimal("5000"),
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


class _FakeSource:
    def latest_snapshot(self, count):
        del count
        return [object()]

    def disconnect(self):
        return None


class _FakeOrchestrator:
    def __init__(self, record):
        self.record = record
        self.calls = 0

    def submit(self, frame, token, on_event):
        del frame, token, on_event
        self.calls += 1
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

    def wait_for_command(self, command_id, *, timeout):
        del timeout
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
        self.is_armed = False
        self.disarm_calls += 1

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
    record = _record(symbol=CAMPAIGN_SYMBOL).model_copy(
        update={
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
    state = store.create_or_resume(now=now)
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

    def _sizing(_client, *, entry_price, stop_loss_price, side):
        del _client, entry_price
        assert side == "long"
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


def test_completed_campaign_cannot_restart_automatically(tmp_path):
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = store.create_or_resume(now=datetime(2026, 7, 17, tzinfo=UTC))
    store.save(state.model_copy(update={"status": "completed"}))

    with pytest.raises(CampaignError, match="禁止自动重新计时"):
        store.create_or_resume(now=datetime(2026, 7, 18, tzinfo=UTC))


def test_explicit_restart_archives_an_idle_campaign(monkeypatch, tmp_path):
    store = CampaignStateStore(tmp_path / "campaign.json")
    original = store.create_or_resume(now=datetime(2026, 7, 17, tzinfo=UTC))
    store.save(original)
    history = tmp_path / "history"
    monkeypatch.setattr(campaign_module, "CAMPAIGN_HISTORY_DIR", history)

    restarted = store.restart(
        reason="configuration changed",
        now=datetime(2026, 7, 17, 1, tzinfo=UTC),
    )

    assert restarted.campaign_id != original.campaign_id
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
            update={"execution_ids": ["closed-id", "canceled-id"]}
        )
    )
    history = tmp_path / "history"
    monkeypatch.setattr(campaign_module, "CAMPAIGN_HISTORY_DIR", history)
    executions = {
        "closed-id": SimpleNamespace(state=ExecutionState.CLOSED),
        "canceled-id": SimpleNamespace(state=ExecutionState.CANCELED),
    }

    restarted = store.restart(
        reason="configuration changed",
        now=datetime(2026, 7, 17, 1, tzinfo=UTC),
        execution_lookup=executions.get,
    )

    assert restarted.campaign_id != original.campaign_id
    assert len(list(history.glob("*.json"))) == 1


@pytest.mark.parametrize(
    "execution",
    [
        None,
        SimpleNamespace(state=ExecutionState.READY),
        SimpleNamespace(state=ExecutionState.UNKNOWN),
        SimpleNamespace(state=ExecutionState.ERROR),
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
                    "minSz": "1",
                    "lotSz": "1",
                    "ctVal": "0.001",
                    "ctMult": "1",
                }
            ]

        def balance(self):
            return [{"details": [{"ccy": "USDT", "eq": "5000"}]}]

        def ticker(self, instrument):
            assert instrument == CAMPAIGN_INSTRUMENT
            return {"last": "4000"}

        def max_order_size(self, *, instrument, trade_mode):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert trade_mode == "cross"
            return {"maxBuy": "100000", "maxSell": "100000"}

    sizing = resolve_campaign_sizing(
        _Client(),
        entry_price="4000",
        stop_loss_price="3990",
        side="long",
    )

    assert sizing.equity_usdt == Decimal("5000")
    assert sizing.risk_budget_usdt == Decimal("500")
    assert sizing.contract_notional_usdt == Decimal("4")
    assert sizing.stop_distance_usdt == Decimal("10")
    assert sizing.quantity == Decimal("22742")


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
            return [{"details": [{"ccy": "USDT", "eq": "5000"}]}]

        def ticker(self, instrument):
            assert instrument == CAMPAIGN_INSTRUMENT
            return {"last": "4000"}

        def max_order_size(self, *, instrument, trade_mode):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert trade_mode == "cross"
            return {"maxBuy": "21000", "maxSell": "21000"}

    record, sizing = build_controlled_demo_s_record(
        base,
        client=_Client(),
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
    assert record.stage1_response["base_stage1_diagnosis"] == (
        base.stage1_diagnosis
    )
    assert record.stage2_response["base_stage2_decision"] == (
        base.stage2_decision
    )
    assert Decimal(decision["entry_price"]) - Decimal(
        decision["stop_loss_price"]
    ) > Decimal("10.8")
    assert sizing.quantity <= sizing.max_buy
    assert sizing.quantity != sizing.max_buy
    assert record.stage2_response["risk_sizing"] == {
        "equity_basis": "usdt_equity",
        "equity_usdt": "5000",
        "risk_percent": "0.10",
        "risk_budget_usdt": "500.00",
        "risk_used_usdt": str(sizing.risk_used_usdt),
        "reference_price_usdt": decision["entry_price"],
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
    natural = _record(symbol=CAMPAIGN_SYMBOL).model_copy(deep=True)
    natural.meta = natural.meta.model_copy(
        update={
            "timeframe": CAMPAIGN_TIMEFRAME,
            "data_source": "okx",
            "market_data_provenance": "okx_5m_utc_pair_aggregation",
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
    writer = PendingWriter(tmp_path)
    writer.save_full_durable(natural)
    writer.save_full_durable(controlled)
    monkeypatch.setattr(campaign_module, "RECORDS_PENDING_DIR", tmp_path)

    selected = find_latest_natural_campaign_record()

    assert selected is not None
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

        def max_order_size(self, *, instrument, trade_mode):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert trade_mode == "cross"
            return {"maxBuy": "500", "maxSell": "500"}

        def balance(self):
            return [{"details": [{"ccy": "USDT", "eq": "5000"}]}]

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

    bars = source.latest_snapshot(150)
    bars_1h = source.latest_snapshot_for_timeframe("1h", 150)
    bars_4h = source.latest_snapshot_for_timeframe("4h", 150)

    assert client.calls == [
        (CAMPAIGN_INSTRUMENT, "5m", 300),
        (CAMPAIGN_INSTRUMENT, "1H", 150),
        (CAMPAIGN_INSTRUMENT, "4H", 150),
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


def test_new_bar_passes_thin_higher_timeframe_context(
    monkeypatch,
    tmp_path,
):
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

        def latest_snapshot_for_timeframe(self, timeframe, count):
            self.calls.append((timeframe, count))
            return [object()]

        def disconnect(self):
            return None

    class _MtfOrchestrator:
        def __init__(self):
            self.context = ""

        def submit(self, frame, token, on_event, **kwargs):
            del frame, token, on_event
            self.context = kwargs["higher_timeframe_text"]
            return SimpleNamespace(exception=None)

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


def test_supervisor_block_prevents_plan_and_worker_command(monkeypatch, tmp_path):
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
    assert client.calls
    assert service.prepared == []
    assert service.submitted == []
    assert runner.state.last_plan_result == "blocked:supervisor:primary"
    persisted = list((tmp_path / "supervisor").glob("*.json"))
    assert len(persisted) == 1
    assert json.loads(persisted[0].read_text(encoding="utf-8"))["action"] == "block_entry"
    assert record.stage2_decision["decision"]["order_type"] == "限价单"


def test_supervisor_allow_creates_one_plan_and_restart_reuses_conclusion(
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
    assert len(client.calls) == 1

    restarted_state = store.load()
    assert restarted_state is not None
    restarted = OkxDemoCampaign(runtime, store, restarted_state)
    assert restarted.process_latest_closed_bar() is False
    assert len(service.prepared) == 1
    assert service.submitted == ["execution-1"]
    assert len(client.calls) == 1


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


def test_risk_size_exceeded_blocks_current_bar_and_next_closed_bar_continues(
    monkeypatch,
    tmp_path,
):
    first_bar_ms = 1_784_300_400_000
    second_bar_ms = first_bar_ms + 15 * 60 * 1000
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
            )
        return _sizing(record)

    runtime.sizing_resolver = _resolve_sizing
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, datetime(2026, 7, 17, tzinfo=UTC))
    runner = OkxDemoCampaign(runtime, store, state)

    assert runner.process_latest_closed_bar() is True
    assert runner.state.status == "active"
    assert runner.state.inflight_bar_ms is None
    assert runner.state.last_completed_bar_ms == first_bar_ms
    assert runner.state.last_plan_result == "blocked:risk:max_size_exceeded"
    assert "max_size_exceeded" in runner.state.last_error
    assert supervisor_client.calls == []
    assert service.prepared == []
    assert service.submitted == []

    current_bar_ms["value"] = second_bar_ms

    assert runner.process_latest_closed_bar() is True
    assert runner.state.status == "active"
    assert runner.state.last_completed_bar_ms == second_bar_ms
    assert runner.state.last_plan_result == "execution:entry_pending"
    assert len(supervisor_client.calls) == 1
    assert service.prepared
    assert service.submitted == ["execution-1"]


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


def test_inflight_bar_reuses_durable_record(monkeypatch, tmp_path):
    bar_ms = 1_784_300_400_000
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: _frame(bar_ms),
    )
    started = datetime(2026, 7, 17, tzinfo=UTC)
    durable_record = SimpleNamespace(
        exception=None,
        meta=SimpleNamespace(
            decision_stance=CAMPAIGN_STANCE,
            timestamp_local_ms=int((started + timedelta(minutes=1)).timestamp() * 1000),
        ),
        kline_data=[{"ts_open": bar_ms}],
    )
    monkeypatch.setattr(
        campaign_module,
        "find_latest_successful_record",
        lambda **kwargs: durable_record,
    )
    orchestrator = _FakeOrchestrator(SimpleNamespace(exception=None))
    service = _FakeExecutionService()
    store = CampaignStateStore(tmp_path / "campaign.json")
    state = _state(store, started).model_copy(update={"inflight_bar_ms": bar_ms})
    store.save(state)
    runner = OkxDemoCampaign(
        _runtime(orchestrator, service),
        store,
        state,
    )

    assert runner.process_latest_closed_bar() is True
    assert orchestrator.calls == 0
    assert service.prepared == [durable_record]
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
        ]
    )
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
