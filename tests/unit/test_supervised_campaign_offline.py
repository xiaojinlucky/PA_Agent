from __future__ import annotations

import dataclasses
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pa_agent.okx_demo_campaign as campaign_module
from pa_agent.agents.supervisor import SupervisorAgent, SupervisorGate
from pa_agent.config.settings import Settings
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.execution.controller import ExecutionController
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
from tests.unit.test_execution_plan_builder import _record
from tests.unit.test_execution_service import FakeAdapter


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
                        "decision_stance": "aggressive",
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
                "ctVal": "0.001",
                "ctMult": "1",
            }
        ]

    def balance(self):
        return [{"details": [{"ccy": "USDT", "eq": "5000"}]}]

    def max_order_size(self, *, instrument, trade_mode):
        assert instrument == CAMPAIGN_SYMBOL
        assert trade_mode == "cross"
        return {"maxBuy": "100000", "maxSell": "100000"}


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


def test_supervised_campaign_uses_real_controller_worker_offline(
    monkeypatch,
    tmp_path,
):
    first_bar_ms = 1_784_300_400_000
    second_bar_ms = first_bar_ms + 15 * 60 * 1000
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
        assert executions[0].plan.quantity == 94473
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
