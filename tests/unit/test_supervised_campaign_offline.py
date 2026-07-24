from __future__ import annotations

import dataclasses
import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pa_agent.okx_demo_campaign as campaign_module
from pa_agent.agents.supervisor import SupervisorAgent, SupervisorGate
from pa_agent.config.settings import Settings
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.execution.controller import ExecutionController
from pa_agent.execution.credentials import account_identity_fingerprint
from pa_agent.execution.models import ExecutionState
from pa_agent.execution.okx_adapter import OkxAdapter
from pa_agent.execution.service import ExecutionService
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker import ExecutionWorker, WorkerNewRiskAuthority
from pa_agent.execution.worker_protocol import (
    WorkerCommandAction,
    WorkerCommandStatus,
)
from pa_agent.execution.worker_store import WorkerStore
from pa_agent.okx_demo_campaign import (
    CAMPAIGN_SYMBOL,
    CAMPAIGN_TIMEFRAME,
    CampaignRuntime,
    CampaignStateStore,
    OkxDemoCampaign,
    build_campaign_settings,
)
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.records.supervisor_writer import SupervisorWriter
from pa_agent.risk.runtime import RiskRuntime
from tests.unit.test_execution_plan_builder import _record
from tests.unit.test_execution_service import FakeAdapter
from tests.unit.test_okx_adapter import FakeOkxClient


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
    return KlineFrame(
        symbol=CAMPAIGN_SYMBOL,
        timeframe=CAMPAIGN_TIMEFRAME,
        bars=(bar,),
        indicators=IndicatorBundle(ema20=(4000.0,), atr14=(10.0,)),
        snapshot_ts_local_ms=bar_ms,
    )


class _Source:
    def __init__(self, frame):
        self.frame = frame

    def latest_snapshot(self, count):
        del count
        return [object()]

    def disconnect(self):
        return None


class _ScriptedOrchestrator:
    def __init__(self, writer: PendingWriter):
        self.writer = writer
        self.calls = 0

    def submit(self, frame, token, on_event):
        del token, on_event
        self.calls += 1
        bar = frame.bars[0]
        base = _record(symbol=CAMPAIGN_SYMBOL)
        record = base.model_copy(
            update={
                "meta": base.meta.model_copy(
                    update={
                        "timestamp_local_ms": int(bar.ts_open),
                        "symbol": CAMPAIGN_SYMBOL,
                        "timeframe": CAMPAIGN_TIMEFRAME,
                        "data_source": "okx",
                        "market_data_provenance": (
                            "okx_5m_utc_pair_aggregation"
                        ),
                        "decision_stance": "extreme_aggressive",
                    }
                ),
                "kline_data": [dataclasses.asdict(bar)],
            }
        )
        self.writer.save_full_durable(record)
        return record


class _ScriptedSupervisorClient:
    def __init__(self):
        self.actions = ["block_entry", "allow_entry"]
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        action = self.actions[len(self.calls) - 1]
        return SimpleNamespace(
            content=json.dumps(
                {"action": action, "reason": f"离线验收: {action}"},
                ensure_ascii=False,
            )
        )


class _RiskSizingClient:
    def account_config(self):
        return {"posMode": "net_mode"}

    def instruments(self, inst_type):
        assert inst_type == "SWAP"
        return [
            {
                "instId": CAMPAIGN_SYMBOL,
                "state": "live",
                "minSz": "1",
                "lotSz": "1",
                "tickSz": "0.1",
                "ctVal": "0.001",
                "ctMult": "1",
            }
        ]

    def balance(self):
        return [{"details": [{"ccy": "USDT", "eq": "5000"}]}]

    def max_order_size(
        self,
        *,
        instrument,
        trade_mode,
        price=None,
        leverage=None,
    ):
        assert instrument == CAMPAIGN_SYMBOL
        assert trade_mode == "cross"
        return {"maxBuy": "100000", "maxSell": "100000"}


class _DynamicLeverageClient(FakeOkxClient):
    simulated = True

    def __init__(self):
        super().__init__()
        self.simulated = True
        self.balance_rows = [
            {
                "totalEq": "20",
                "details": [
                    {
                        "ccy": "USDT",
                        "eq": "20",
                        "cashBal": "20",
                        "availBal": "20",
                        "availEq": "20",
                    }
                ],
            }
        ]

    def max_order_size(self, **kwargs):
        self.calls.append(("max_order_size", kwargs))
        raw_leverage = kwargs.get("leverage")
        leverage = (
            Decimal(str(raw_leverage))
            if raw_leverage is not None
            else self.current_leverage
        )
        capacity = leverage * Decimal("4")
        return {"maxBuy": str(capacity), "maxSell": str(capacity)}


def _build_runtime(tmp_path: Path):
    settings = build_campaign_settings(Settings(), quantity="120")
    pending = PendingWriter(tmp_path / "pending")
    source = _Source(_frame(1_784_300_400_000))
    orchestrator = _ScriptedOrchestrator(pending)

    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    worker_store = WorkerStore(tmp_path / "execution_control.sqlite3")
    adapter = FakeAdapter()
    worker_id = "offline-supervised-worker"
    authority = WorkerNewRiskAuthority(worker_store, worker_id)
    worker_service = ExecutionService(
        settings=settings,
        pending_writer=None,
        store=execution_store,
        adapter_factories={"okx": lambda _plan: adapter},
        gate_checker=lambda: False,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: False,
        new_risk_authorizer=authority.is_authorized,
        new_risk_revoker=lambda: worker_store.revoke_current_new_risk_lease(
            failure_code="service_disarmed",
        ),
    )
    worker = ExecutionWorker(
        store=worker_store,
        service=worker_service,
        settings=settings,
        lock_path=tmp_path / "execution.worker.lock",
        worker_id=worker_id,
        poll_interval_seconds=0.005,
        heartbeat_interval_seconds=0.01,
        new_risk_authority=authority,
    )
    controller = ExecutionController(
        settings=settings,
        pending_writer=pending,
        store=execution_store,
        worker_store=worker_store,
        worker_launcher=lambda: None,
        gate_checker=lambda: False,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: False,
    )

    client = _ScriptedSupervisorClient()
    supervisor = SupervisorGate(
        SupervisorAgent(
            primary_client=client,
            primary_profile_id="offline-supervisor",
            primary_model_id="offline-model",
            prompt_text="只返回严格 JSON。",
        ),
        SupervisorWriter(tmp_path / "supervisor"),
    )

    risk_client = _RiskSizingClient()

    def _resolve_risk_sizing(record):
        return campaign_module.resolve_record_campaign_sizing(record, risk_client)

    runtime = CampaignRuntime(
        settings=settings,
        source=source,
        writer=pending,
        orchestrator=orchestrator,
        execution_service=controller,
        supervisor=supervisor,
        sizing_resolver=_resolve_risk_sizing,
    )
    return runtime, worker, worker_store, adapter, client, orchestrator


def _build_dynamic_leverage_runtime(
    tmp_path: Path,
    *,
    supervisor_action: str,
):
    settings = build_campaign_settings(Settings(), quantity="120")
    pending = PendingWriter(tmp_path / "pending")
    source = _Source(_frame(1_784_300_400_000))
    orchestrator = _ScriptedOrchestrator(pending)
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    worker_store = WorkerStore(tmp_path / "execution_control.sqlite3")
    client = _DynamicLeverageClient()
    adapter = OkxAdapter(client, margin_mode="cross")
    worker_id = f"dynamic-leverage-{supervisor_action}"
    authority = WorkerNewRiskAuthority(worker_store, worker_id)
    worker_service = ExecutionService(
        settings=settings,
        pending_writer=None,
        store=execution_store,
        adapter_factories={"okx": lambda _plan: adapter},
        leverage_adapter_factory=lambda _command: adapter,
        risk_runtime=RiskRuntime(worker_store),
        gate_checker=lambda: False,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: False,
        new_risk_authorizer=authority.is_authorized,
        new_risk_revoker=lambda: worker_store.revoke_current_new_risk_lease(
            failure_code="service_disarmed",
        ),
    )
    worker = ExecutionWorker(
        store=worker_store,
        service=worker_service,
        settings=settings,
        lock_path=tmp_path / "execution.worker.lock",
        worker_id=worker_id,
        poll_interval_seconds=0.005,
        heartbeat_interval_seconds=0.01,
        new_risk_authority=authority,
    )
    controller = ExecutionController(
        settings=settings,
        pending_writer=pending,
        store=execution_store,
        worker_store=worker_store,
        worker_launcher=lambda: None,
        gate_checker=lambda: False,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: False,
    )
    supervisor_client = _ScriptedSupervisorClient()
    supervisor_client.actions = [supervisor_action]
    supervisor = SupervisorGate(
        SupervisorAgent(
            primary_client=supervisor_client,
            primary_profile_id="offline-supervisor",
            primary_model_id="offline-model",
            prompt_text="只返回严格 JSON。",
        ),
        SupervisorWriter(tmp_path / "supervisor"),
    )

    runtime = CampaignRuntime(
        settings=settings,
        source=source,
        writer=pending,
        orchestrator=orchestrator,
        execution_service=controller,
        supervisor=supervisor,
        sizing_resolver=lambda record: (
            campaign_module.resolve_record_campaign_sizing(record, client)
        ),
    )
    return (
        runtime,
        worker,
        worker_store,
        client,
        supervisor_client,
    )


def _run_worker_loop(worker: ExecutionWorker):
    errors: list[BaseException] = []
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            try:
                worker.run_once()
            except BaseException as exc:  # pragma: no cover - failure evidence
                errors.append(exc)
                return
            stop.wait(0.005)

    worker.start()
    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return stop, thread, errors


def _dynamic_runner(
    monkeypatch,
    tmp_path: Path,
    *,
    supervisor_action: str,
):
    bar_ms = 1_784_300_400_000
    frame = _frame(bar_ms)
    monkeypatch.setattr(
        campaign_module,
        "_utc_now",
        lambda: datetime(2026, 7, 17, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: frame,
    )
    monkeypatch.setattr(
        "pa_agent.config.paths.RECORDS_PENDING_DIR",
        tmp_path / "pending",
    )
    monkeypatch.setattr(
        campaign_module,
        "RECORDS_PENDING_DIR",
        tmp_path / "pending",
    )
    runtime, worker, worker_store, client, supervisor_client = (
        _build_dynamic_leverage_runtime(
            tmp_path,
            supervisor_action=supervisor_action,
        )
    )
    monkeypatch.setattr(
        campaign_module,
        "load_okx_credentials",
        lambda environment: object(),
    )
    monkeypatch.setattr(
        campaign_module,
        "OkxRestClient",
        lambda *args, **kwargs: client,
    )
    state_store = CampaignStateStore(tmp_path / "campaign.json")
    state = state_store.create_or_resume(
        now=datetime(2026, 7, 17, tzinfo=UTC)
    )
    state_store.save(state)
    return (
        OkxDemoCampaign(runtime, state_store, state),
        runtime,
        worker,
        worker_store,
        client,
        supervisor_client,
    )


def test_supervised_campaign_uses_real_controller_worker_offline(
    monkeypatch,
    tmp_path,
):
    first_bar_ms = 1_784_300_400_000
    second_bar_ms = first_bar_ms + 10 * 60 * 1000
    first_frame = _frame(first_bar_ms)
    second_frame = _frame(second_bar_ms)
    current_frame = {"value": first_frame}
    monkeypatch.setattr(
        campaign_module,
        "_utc_now",
        lambda: datetime(2026, 7, 17, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(
        campaign_module,
        "build_analysis_frame",
        lambda *args, **kwargs: current_frame["value"],
    )
    monkeypatch.setattr(
        "pa_agent.config.paths.RECORDS_PENDING_DIR",
        tmp_path / "pending",
    )
    monkeypatch.setattr(
        campaign_module,
        "RECORDS_PENDING_DIR",
        tmp_path / "pending",
    )

    runtime, worker, worker_store, adapter, client, orchestrator = _build_runtime(
        tmp_path
    )
    worker_errors: list[BaseException] = []
    stop_worker = threading.Event()

    def _worker_loop():
        while not stop_worker.is_set():
            try:
                worker.run_once()
            except BaseException as exc:  # pragma: no cover - failure evidence
                worker_errors.append(exc)
                return
            stop_worker.wait(0.005)

    worker.start()
    worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    worker_thread.start()
    state_store = CampaignStateStore(tmp_path / "campaign.json")
    state = state_store.create_or_resume(
        now=datetime(2026, 7, 17, tzinfo=UTC)
    )
    state_store.save(state)
    runner = OkxDemoCampaign(runtime, state_store, state)

    try:
        runtime.execution_service.arm("启用模拟交易")
        initial_lease = worker_store.current_new_risk_lease()
        assert initial_lease is not None

        assert runner.process_latest_closed_bar() is True
        assert runner.state.last_plan_result == "blocked:supervisor:primary"
        assert runtime.execution_service.list_active() == []
        assert runtime.execution_service.execution_store.list_recent() == []
        assert worker_store.list_commands() == []
        first_sized_lease = worker_store.current_new_risk_lease()
        assert first_sized_lease is not None
        assert first_sized_lease.lease_id != initial_lease.lease_id
        assert first_sized_lease.config_fingerprint != initial_lease.config_fingerprint

        current_frame["value"] = second_frame
        assert runner.process_latest_closed_bar() is True
        commands = worker_store.list_commands()
        submit_commands = [
            item for item in commands if item.action is WorkerCommandAction.SUBMIT
        ]
        assert len(submit_commands) == 1
        assert submit_commands[0].broker == "okx"
        assert submit_commands[0].environment == "demo"
        assert submit_commands[0].status is WorkerCommandStatus.SUCCEEDED
        assert len([call for call in adapter.calls if call[0] == "submit_entry"]) == 1
        executions = runtime.execution_service.list_recent()
        assert len(executions) == 1
        assert executions[0].plan.quantity == 79440
        final_lease = worker_store.current_new_risk_lease()
        assert final_lease is not None
        assert final_lease.lease_id != first_sized_lease.lease_id
        assert executions[0].plan.config_fingerprint == final_lease.config_fingerprint
        assert len(client.calls) == 2
        assert orchestrator.calls == 2

        restarted_state = state_store.load()
        assert restarted_state is not None
        restarted = OkxDemoCampaign(runtime, state_store, restarted_state)
        assert restarted.process_latest_closed_bar() is True
        assert len(worker_store.list_commands()) == 1
        assert len(client.calls) == 2
        assert len([call for call in adapter.calls if call[0] == "submit_entry"]) == 1
    finally:
        runtime.execution_service.disarm()
        stop_worker.set()
        worker_thread.join(timeout=2)
        worker.close()

    assert worker_errors == []


def test_max_size_leverage_supervision_controller_worker_okx_chain(
    monkeypatch,
    tmp_path,
):
    (
        runner,
        runtime,
        worker,
        worker_store,
        client,
        supervisor_client,
    ) = _dynamic_runner(
        monkeypatch,
        tmp_path,
        supervisor_action="allow_entry",
    )
    stop_worker, worker_thread, worker_errors = _run_worker_loop(worker)

    try:
        runtime.execution_service.arm("启用模拟交易")
        assert runner.process_latest_closed_bar() is True

        commands = worker_store.list_commands()
        leverage_commands = [
            command
            for command in commands
            if command.action is WorkerCommandAction.SET_LEVERAGE
        ]
        submit_commands = [
            command
            for command in commands
            if command.action is WorkerCommandAction.SUBMIT
        ]
        assert len(leverage_commands) == 1
        assert len(submit_commands) == 1
        assert leverage_commands[0].status is WorkerCommandStatus.SUCCEEDED
        assert submit_commands[0].status is WorkerCommandStatus.SUCCEEDED
        assert leverage_commands[0].parameters.target_leverage == Decimal("10")
        assert leverage_commands[0].parameters.supervisor_record_id
        assert leverage_commands[0].parameters.supervisor_record_digest

        executions = runtime.execution_service.list_recent()
        assert len(executions) == 1
        assert executions[0].state is ExecutionState.ENTRY_PENDING
        assert executions[0].plan.quantity > Decimal("20")
        assert executions[0].plan.quantity <= Decimal("40")
        assert client.current_leverage == Decimal("10")
        write_calls = [
            call
            for call in client.calls
            if call[0] in {"set_leverage", "place_order"}
        ]
        assert [call[0] for call in write_calls] == [
            "set_leverage",
            "place_order",
        ]
        assert len(supervisor_client.calls) == 1
        assert runner.state.last_plan_result == "execution:entry_pending"
    finally:
        runtime.execution_service.disarm()
        stop_worker.set()
        worker_thread.join(timeout=2)
        worker.close()

    assert worker_errors == []


def test_dynamic_leverage_supervisor_block_has_zero_broker_writes(
    monkeypatch,
    tmp_path,
):
    (
        runner,
        runtime,
        worker,
        worker_store,
        client,
        supervisor_client,
    ) = _dynamic_runner(
        monkeypatch,
        tmp_path,
        supervisor_action="block_entry",
    )
    stop_worker, worker_thread, worker_errors = _run_worker_loop(worker)

    try:
        runtime.execution_service.arm("启用模拟交易")
        assert runner.process_latest_closed_bar() is True
        assert runner.state.last_plan_result == "blocked:supervisor:primary"
        assert worker_store.list_commands() == []
        assert runtime.execution_service.list_recent() == []
        assert [
            call
            for call in client.calls
            if call[0] in {"set_leverage", "place_order"}
        ] == []
        assert len(supervisor_client.calls) == 1
    finally:
        runtime.execution_service.disarm()
        stop_worker.set()
        worker_thread.join(timeout=2)
        worker.close()

    assert worker_errors == []


def test_dynamic_leverage_drawdown_stop_has_zero_broker_writes(
    monkeypatch,
    tmp_path,
):
    (
        runner,
        runtime,
        worker,
        worker_store,
        client,
        _supervisor_client,
    ) = _dynamic_runner(
        monkeypatch,
        tmp_path,
        supervisor_action="allow_entry",
    )
    risk_runtime = RiskRuntime(worker_store)
    identity = account_identity_fingerprint(
        "okx",
        "demo",
        "1001",
        "1001",
        "0",
    )
    risk_runtime.refresh(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=identity,
        total_equity_usd="100",
        bill_rows=[],
    )
    client.balance_rows[0]["totalEq"] = "20"
    stop_worker, worker_thread, worker_errors = _run_worker_loop(worker)

    try:
        runtime.execution_service.arm("启用模拟交易")
        assert runner.process_latest_closed_bar() is True
        commands = worker_store.list_commands()
        assert len(commands) == 1
        assert commands[0].action is WorkerCommandAction.SET_LEVERAGE
        assert commands[0].status is WorkerCommandStatus.FAILED
        assert [
            call
            for call in client.calls
            if call[0] in {"set_leverage", "place_order"}
        ] == []
        assert runtime.execution_service.list_recent() == []
        assert runner.state.last_plan_result.startswith(
            "blocked:risk:leverage:"
        )
    finally:
        runtime.execution_service.disarm()
        stop_worker.set()
        worker_thread.join(timeout=2)
        worker.close()

    assert worker_errors == []
