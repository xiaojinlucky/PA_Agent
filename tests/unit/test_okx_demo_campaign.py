from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import pa_agent.okx_demo_campaign as campaign_module
from pa_agent.config.settings import Settings
from pa_agent.data.base import (
    DataSourceTransientError,
    IndicatorBundle,
    KlineBar,
    KlineFrame,
)
from pa_agent.execution.errors import BrokerTransportError, PlanBlocked
from pa_agent.execution.models import ExecutionState
from pa_agent.execution.plan_builder import build_execution_plan
from pa_agent.execution.worker_protocol import WorkerCommandStatus
from pa_agent.okx_demo_campaign import (
    CAMPAIGN_DURATION,
    CAMPAIGN_INSTRUMENT,
    CAMPAIGN_MIN_CONFIDENCE,
    CAMPAIGN_OKX_API_BASE_URL,
    CAMPAIGN_STANCE,
    CAMPAIGN_SYMBOL,
    CAMPAIGN_TIMEFRAME,
    CampaignError,
    CampaignProcessLock,
    CampaignRuntime,
    CampaignStateStore,
    OkxCampaignSource,
    OkxDemoCampaign,
    build_campaign_settings,
    campaign_config_fingerprint,
    okx_demo_private_preflight,
    validate_campaign_settings,
)
from tests.unit.test_execution_plan_builder import _persist, _record


def _settings():
    return build_campaign_settings(Settings())


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
    assert settings.general.last_symbol == CAMPAIGN_SYMBOL
    assert settings.general.last_timeframe == CAMPAIGN_TIMEFRAME
    assert settings.execution.min_trade_confidence == CAMPAIGN_MIN_CONFIDENCE
    assert settings.execution.selected_broker == "okx"
    assert settings.execution.auto_execute is False
    assert settings.execution.okx.instrument == CAMPAIGN_INSTRUMENT
    assert settings.execution.okx.quantity == "1"
    assert settings.execution.okx.product == "swap"
    assert settings.execution.okx.simulated is True
    assert settings.execution.okx.api_base_url == CAMPAIGN_OKX_API_BASE_URL
    assert base.execution.okx.api_base_url == "https://attacker.invalid"


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


def test_campaign_process_lock_rejects_second_runner(tmp_path):
    path = tmp_path / "campaign.lock"

    with (
        CampaignProcessLock(path),
        pytest.raises(CampaignError, match="已有 OKX Demo"),
        CampaignProcessLock(path),
    ):
        raise AssertionError("第二个实验进程不应取得文件锁")


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
            return {"uid": "u", "mainUid": "m", "type": "1"}

        def instruments(self, inst_type):
            assert inst_type == "SWAP"
            return [
                {
                    "instId": CAMPAIGN_INSTRUMENT,
                    "state": "live",
                    "minSz": "1",
                    "lotSz": "1",
                }
            ]

        def max_order_size(self, *, instrument, trade_mode):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert trade_mode == "cross"
            return {"maxBuy": "100", "maxSell": "100"}

        def balance(self):
            return [{"details": []}]

        def positions(self, *, instrument):
            assert instrument == CAMPAIGN_INSTRUMENT
            return []

        def leverage_info(self, *, instrument, margin_mode):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert margin_mode == "cross"
            return [{"lever": "10"}]

        def candles(self, *, instrument, bar, limit):
            assert instrument == CAMPAIGN_INSTRUMENT
            assert bar == CAMPAIGN_TIMEFRAME
            assert limit == 2
            return [
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
    assert result["max_buy_sufficient"] is True
    assert result["max_sell_sufficient"] is True


def test_okx_campaign_source_uses_execution_instrument_prices():
    class _MarketClient:
        def __init__(self):
            self.calls = []

        def candles(self, *, instrument, bar, limit):
            self.calls.append((instrument, bar, limit))
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

    assert client.calls == [
        (CAMPAIGN_INSTRUMENT, CAMPAIGN_TIMEFRAME, 150)
    ]
    assert len(bars) == 2
    assert bars[0].seq == 0
    assert bars[0].closed is False
    assert bars[1].seq == 1
    assert bars[1].closed is True
    assert bars[1].close == 4005.0


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
    runner = OkxDemoCampaign(
        _runtime(orchestrator, service),
        store,
        state,
    )

    assert runner.process_latest_closed_bar() is True
    assert runner.state.last_plan_result == "blocked:no_order"
    assert runner.state.executions_prepared == 0
    assert service.submitted == []


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
