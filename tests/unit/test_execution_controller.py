from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pa_agent.config.settings import Settings
from pa_agent.execution.controller import ExecutionController
from pa_agent.execution.errors import BrokerRejected, LiveTradingDisabled
from pa_agent.execution.models import (
    AccountSnapshot,
    ExecutionPlan,
    ExecutionState,
    utc_now_iso,
)
from pa_agent.execution.service import ExecutionService
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker import ExecutionWorker, WorkerNewRiskAuthority
from pa_agent.execution.worker_protocol import (
    WorkerCommandAction,
    WorkerCommandStatus,
    WorkerState,
)
from pa_agent.execution.worker_store import WorkerStore
from pa_agent.records.schema import AnalysisRecord, RecordMeta
from tests.unit.test_execution_service import FakeAdapter


class _PendingWriter:
    def __init__(self, path: Path) -> None:
        self.path = path

    def full_path(self, _record: AnalysisRecord) -> Path:
        return self.path


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 20, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class _EventBus:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.accounts = []

    def emit_execution_error(self, message: str) -> None:
        self.errors.append(message)

    def emit_account_update(self, snapshot) -> None:
        self.accounts.append(snapshot)


def _settings() -> Settings:
    settings = Settings()
    settings.execution.enabled = True
    settings.execution.auto_execute = False
    settings.execution.selected_broker = "okx"
    settings.execution.okx.simulated = True
    settings.execution.okx.source_symbol = "XAU-USDT-SWAP"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    settings.execution.okx.product = "swap"
    settings.execution.okx.quantity = "1"
    return settings


def _record() -> AnalysisRecord:
    return AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-07-20T00:00:00+08:00",
            timestamp_local_ms=1784476800000,
            symbol="XAU-USDT-SWAP",
            timeframe="30m",
            data_source="okx",
            bar_count=100,
            ai_provider={"model": "test"},
            decision_stance="balanced",
        ),
        kline_data=[],
        htf_text="",
        stage1_messages=[],
        stage1_response={},
        stage1_diagnosis={"gate_result": "proceed"},
        stage2_messages=[],
        stage2_response={},
        stage2_decision={
            "decision": {
                "order_direction": "做多",
                "order_type": "限价单",
                "entry_price": 100,
                "take_profit_price": 110,
                "take_profit_price_2": 120,
                "stop_loss_price": 95,
                "trade_confidence": 88,
            }
        },
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )


def _controller(tmp_path: Path, monkeypatch):
    records = tmp_path / "pending"
    records.mkdir()
    record = _record()
    record_path = records / "record.json"
    record_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr(
        "pa_agent.config.paths.RECORDS_PENDING_DIR",
        records,
    )
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    worker_store = WorkerStore(tmp_path / "control.sqlite3")
    settings = _settings()
    controller = ExecutionController(
        settings=settings,
        pending_writer=_PendingWriter(record_path),
        store=execution_store,
        worker_store=worker_store,
        worker_launcher=lambda: None,
        gate_checker=lambda: False,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: False,
    )
    worker_store.record_heartbeat(
        worker_id="worker-a",
        pid=123,
        state=WorkerState.RUNNING,
        last_successful_reconcile_at=datetime.now(UTC),
    )
    return controller, worker_store, settings, record


def _controller_without_worker(
    tmp_path: Path,
    *,
    event_bus=None,
) -> ExecutionController:
    return ExecutionController(
        settings=_settings(),
        pending_writer=_PendingWriter(tmp_path / "unused.json"),
        event_bus=event_bus,
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        worker_store=WorkerStore(tmp_path / "control.sqlite3"),
        gate_checker=lambda: False,
        paper_gate_checker=lambda: False,
        okx_live_gate_checker=lambda: False,
    )


def test_spawn_worker_uses_installed_windows_service_without_python_fallback(
    tmp_path,
    monkeypatch,
):
    controller = _controller_without_worker(tmp_path)
    monkeypatch.setattr(
        controller,
        "_request_installed_windows_service_start",
        lambda: True,
    )
    popen_calls = []
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)),
    )

    controller._spawn_worker()

    assert popen_calls == []


def test_spawn_worker_keeps_python_fallback_without_installed_service(
    tmp_path,
    monkeypatch,
):
    controller = _controller_without_worker(tmp_path)
    monkeypatch.setattr(
        controller,
        "_request_installed_windows_service_start",
        lambda: False,
    )
    popen_calls = []
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)),
    )

    controller._spawn_worker()

    assert len(popen_calls) == 1
    command = popen_calls[0][0][0]
    assert command[-2:] == ["-m", "pa_agent.execution.worker"]


def _enable_fake_windows_service_control(
    controller: ExecutionController,
    monkeypatch,
    tmp_path: Path,
) -> Path:
    service_control = tmp_path / "sc.exe"
    service_control.touch()
    monkeypatch.setattr(
        controller,
        "_is_windows_service_platform",
        lambda: True,
    )
    monkeypatch.setattr(
        controller,
        "_windows_service_control_path",
        lambda: service_control,
    )
    return service_control


def test_missing_windows_service_control_fails_closed(
    tmp_path,
    monkeypatch,
):
    bus = _EventBus()
    controller = _controller_without_worker(tmp_path, event_bus=bus)
    monkeypatch.setattr(
        controller,
        "_is_windows_service_platform",
        lambda: True,
    )
    monkeypatch.setattr(
        controller,
        "_windows_service_control_path",
        lambda: tmp_path / "missing-sc.exe",
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("不得启动备用 Python Worker"),
    )

    controller._spawn_worker()

    assert bus.errors


@pytest.mark.parametrize(
    ("query_result", "query_error"),
    [
        (
            subprocess.CompletedProcess(
                args=[],
                returncode=5,
                stdout=b"access denied",
                stderr=b"",
            ),
            None,
        ),
        (None, OSError("query failed")),
        (None, subprocess.TimeoutExpired(cmd="sc query", timeout=10)),
    ],
)
def test_uncertain_windows_service_query_fails_closed(
    tmp_path,
    monkeypatch,
    query_result,
    query_error,
):
    bus = _EventBus()
    controller = _controller_without_worker(tmp_path, event_bus=bus)
    _enable_fake_windows_service_control(controller, monkeypatch, tmp_path)

    def fake_run(*_args, **_kwargs):
        if query_error is not None:
            raise query_error
        return query_result

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("不得启动备用 Python Worker"),
    )

    controller._spawn_worker()

    assert bus.errors


def test_explicit_missing_windows_service_allows_python_fallback(
    tmp_path,
    monkeypatch,
):
    controller = _controller_without_worker(tmp_path)
    _enable_fake_windows_service_control(controller, monkeypatch, tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=b"OpenService FAILED 1060",
            stderr=b"",
        ),
    )
    popen_calls = []
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)),
    )

    controller._spawn_worker()

    assert len(popen_calls) == 1


@pytest.mark.parametrize(
    ("start_result", "start_error", "expect_error"),
    [
        (
            subprocess.CompletedProcess(
                args=[],
                returncode=5,
                stdout=b"access denied",
                stderr=b"",
            ),
            None,
            True,
        ),
        (
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=b"StartService FAILED 1056",
                stderr=b"",
            ),
            None,
            False,
        ),
        (None, OSError("start failed"), True),
        (None, subprocess.TimeoutExpired(cmd="sc start", timeout=10), True),
    ],
)
def test_windows_service_start_outcomes_never_fall_back_to_python(
    tmp_path,
    monkeypatch,
    start_result,
    start_error,
    expect_error,
):
    bus = _EventBus()
    controller = _controller_without_worker(tmp_path, event_bus=bus)
    _enable_fake_windows_service_control(controller, monkeypatch, tmp_path)
    calls = 0

    def fake_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b"service exists",
                stderr=b"",
            )
        if start_error is not None:
            raise start_error
        return start_result

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("不得启动备用 Python Worker"),
    )

    controller._spawn_worker()

    assert bool(bus.errors) is expect_error


def test_submit_is_durable_command_and_never_calls_broker(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, _settings_obj, record = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    execution = controller.prepare_analysis(record)

    command = controller.submit(execution.id)

    assert controller.get_execution(execution.id).state is ExecutionState.READY
    commands = worker_store.list_commands()
    assert len(commands) == 1
    assert command.id == commands[0].id
    assert commands[0].action is WorkerCommandAction.SUBMIT
    assert commands[0].status is WorkerCommandStatus.PENDING
    assert commands[0].new_risk_lease_id


def test_disarm_blocks_new_risk_but_not_de_risk_commands(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, _settings_obj, record = _controller(
        tmp_path,
        monkeypatch,
    )
    execution = controller.prepare_analysis(record)
    controller.arm("启用模拟交易")
    controller.disarm()

    with pytest.raises(LiveTradingDisabled, match="新增风险"):
        controller.submit(execution.id)

    controller.cancel_entry(execution.id)
    controller.request_exit(execution.id)
    actions = [command.action for command in worker_store.list_commands()]
    assert actions == [
        WorkerCommandAction.CANCEL_ENTRY,
        WorkerCommandAction.REQUEST_EXIT,
    ]


def test_worker_restart_invalidates_existing_lease(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, _settings_obj, _record_obj = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    assert controller.is_armed is True

    worker_store.record_heartbeat(
        worker_id="worker-z",
        pid=456,
        state=WorkerState.RUNNING,
    )

    assert controller.is_armed is False


def test_reload_settings_revokes_lease_and_ready_plan_can_expire_locally(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, settings, record = _controller(
        tmp_path,
        monkeypatch,
    )
    execution = controller.prepare_analysis(record)
    controller.arm("启用模拟交易")

    controller.reload_settings(settings)
    expired = controller.expire_unsubmitted(
        execution.id,
        reason="实验到期",
    )

    assert controller.is_armed is False
    assert worker_store.current_new_risk_lease() is None
    assert expired.state is ExecutionState.CANCELED
    assert controller.events(execution.id)[-1].kind == "ready_expired"


def test_gui_and_campaign_cannot_import_broker_writer_service():
    project_root = Path(__file__).resolve().parents[2]
    paths = [
        project_root / "pa_agent" / "app_context.py",
        project_root / "pa_agent" / "gui" / "main_window.py",
        project_root / "pa_agent" / "gui" / "trading_dialog.py",
        project_root / "pa_agent" / "okx_demo_campaign.py",
    ]
    forbidden = (
        "pa_agent.execution.service",
        "pa_agent.execution.okx_adapter",
        "pa_agent.execution.longbridge_adapter",
    )

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert not any(name in source for name in forbidden), path


def test_controller_queue_worker_is_the_only_broker_write_path(
    tmp_path,
    monkeypatch,
):
    records = tmp_path / "pending"
    records.mkdir()
    record = _record()
    record_path = records / "record.json"
    record_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr(
        "pa_agent.config.paths.RECORDS_PENDING_DIR",
        records,
    )
    settings = _settings()
    worker_settings = Settings()
    worker_settings.execution.enabled = False
    worker_settings.execution.selected_broker = "longbridge"
    worker_settings.execution.longbridge.preferred_account = "comprehensive"
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    worker_store = WorkerStore(tmp_path / "control.sqlite3")
    worker_id = "worker-e2e"
    authority = WorkerNewRiskAuthority(worker_store, worker_id)
    adapter = FakeAdapter()
    service = ExecutionService(
        settings=worker_settings,
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
        service=service,
        settings=worker_settings,
        lock_path=tmp_path / "worker.lock",
        worker_id=worker_id,
        new_risk_authority=authority,
    )
    controller = ExecutionController(
        settings=settings,
        pending_writer=_PendingWriter(record_path),
        store=execution_store,
        worker_store=worker_store,
        worker_launcher=lambda: None,
        gate_checker=lambda: False,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: False,
    )

    worker.start()
    try:
        controller.arm("启用模拟交易")
        execution = controller.prepare_analysis(record)
        submit_command = controller.submit(execution.id)

        finished_submit = worker.run_once()

        assert finished_submit.id == submit_command.id
        assert finished_submit.status is WorkerCommandStatus.SUCCEEDED
        submitted = controller.get_execution(execution.id)
        assert submitted.state is ExecutionState.ENTRY_PENDING
        assert [call for call in adapter.calls if call[0] == "submit_entry"]
        assert worker_settings.execution.selected_broker == "longbridge"

        cancel_command = controller.cancel_entry(execution.id)
        controller.stop_monitoring()
        assert controller.is_armed is False

        finished_cancel = worker.run_once()

        assert finished_cancel.id == cancel_command.id
        assert finished_cancel.status is WorkerCommandStatus.SUCCEEDED
        canceled = controller.get_execution(execution.id)
        assert canceled.broker_state["cancel"] is True
    finally:
        worker.close()


@pytest.mark.parametrize("action", ["cancel", "exit"])
def test_rejected_risk_reduction_stops_worker_and_immediate_rearm(
    tmp_path,
    monkeypatch,
    action,
):
    records = tmp_path / "pending"
    records.mkdir()
    record = _record()
    record_path = records / "record.json"
    record_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr(
        "pa_agent.config.paths.RECORDS_PENDING_DIR",
        records,
    )
    settings = _settings()
    worker_settings = Settings()
    worker_settings.execution.enabled = False
    worker_settings.execution.selected_broker = "longbridge"
    worker_settings.execution.longbridge.preferred_account = "comprehensive"
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    worker_store = WorkerStore(tmp_path / "control.sqlite3")
    worker_id = f"worker-rejected-{action}"
    authority = WorkerNewRiskAuthority(worker_store, worker_id)
    adapter = FakeAdapter()
    service = ExecutionService(
        settings=worker_settings,
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
        service=service,
        settings=worker_settings,
        lock_path=tmp_path / "worker.lock",
        worker_id=worker_id,
        new_risk_authority=authority,
    )
    controller = ExecutionController(
        settings=settings,
        pending_writer=_PendingWriter(record_path),
        store=execution_store,
        worker_store=worker_store,
        worker_launcher=lambda: None,
        gate_checker=lambda: False,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: False,
    )

    worker.start()
    try:
        controller.arm("启用模拟交易")
        execution = controller.prepare_analysis(record)
        submit_command = controller.submit(execution.id)
        submit_result = worker.run_once()
        assert submit_result.id == submit_command.id
        assert submit_result.status is WorkerCommandStatus.SUCCEEDED
        if action == "exit":
            service.reconcile_once()
            adapter.exit_error = BrokerRejected("exit rejected")
            command = controller.request_exit(execution.id)
        else:
            adapter.cancel_error = BrokerRejected("cancel rejected")
            command = controller.cancel_entry(execution.id)

        result = worker.run_once()
        persisted = controller.get_execution(execution.id)
        heartbeat = worker_store.get_heartbeat(worker_id)

        assert result.id == command.id
        assert result.status is WorkerCommandStatus.FAILED
        assert persisted.needs_attention is True
        assert heartbeat.state is WorkerState.NEEDS_ATTENTION
        assert worker_store.current_new_risk_lease() is None
        with pytest.raises(
            LiveTradingDisabled,
            match="尚不能新增风险",
        ):
            controller.arm("启用模拟交易")
    finally:
        worker.close()


def test_live_heartbeat_does_not_hide_stalled_reconciliation(tmp_path):
    clock = _Clock()
    worker_store = WorkerStore(
        tmp_path / "control.sqlite3",
        clock=clock,
    )
    worker_store.record_heartbeat(
        worker_id="worker-health",
        pid=123,
        state=WorkerState.RUNNING,
        last_successful_reconcile_at=clock(),
    )
    clock.advance(seconds=31)
    worker_store.record_heartbeat(
        worker_id="worker-health",
        pid=123,
        state=WorkerState.RUNNING,
    )
    bus = _EventBus()
    controller = ExecutionController(
        settings=_settings(),
        pending_writer=_PendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        worker_store=worker_store,
        worker_launcher=lambda: None,
        event_bus=bus,
    )

    controller._poll_worker_health()
    health = controller.worker_health_snapshot()

    assert bus.errors == [
        "交易后台进程仍在运行，但券商对账已长时间没有成功"
    ]
    assert health["process_healthy"] is True
    assert health["reconcile_healthy"] is False
    assert health["state"] == "running"
    assert health["last_successful_reconcile_at"] == clock.value - timedelta(
        seconds=31
    )


def test_current_route_account_snapshot_is_emitted_with_empty_execution_ledger(
    tmp_path,
):
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    snapshot = AccountSnapshot(
        broker="okx",
        account_profile="okx-demo",
        equity=Decimal("1000"),
    )
    execution_store.save_account_snapshot(snapshot)
    bus = _EventBus()
    controller = ExecutionController(
        settings=_settings(),
        pending_writer=_PendingWriter(tmp_path / "unused.json"),
        store=execution_store,
        worker_store=WorkerStore(tmp_path / "control.sqlite3"),
        worker_launcher=lambda: None,
        event_bus=bus,
    )

    controller._poll_records()

    assert bus.accounts == [snapshot]


def test_stale_reconcile_revokes_existing_new_risk_lease(tmp_path):
    clock = _Clock()
    worker_store = WorkerStore(
        tmp_path / "control.sqlite3",
        clock=clock,
    )
    worker_store.record_heartbeat(
        worker_id="worker-health",
        pid=123,
        state=WorkerState.RUNNING,
        last_successful_reconcile_at=clock(),
    )
    controller = ExecutionController(
        settings=_settings(),
        pending_writer=_PendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        worker_store=worker_store,
        worker_launcher=lambda: None,
        paper_gate_checker=lambda: True,
    )
    controller.arm("启用模拟交易")
    assert controller.is_armed is True

    clock.advance(seconds=31)
    worker_store.record_heartbeat(
        worker_id="worker-health",
        pid=123,
        state=WorkerState.RUNNING,
    )
    controller._renew_lease()

    assert controller.is_armed is False
    assert worker_store.current_new_risk_lease() is None


def test_startup_reconciling_without_first_success_cannot_arm(tmp_path):
    worker_store = WorkerStore(tmp_path / "control.sqlite3")
    worker_store.record_heartbeat(
        worker_id="worker-starting",
        pid=123,
        state=WorkerState.RECONCILING,
    )
    controller = ExecutionController(
        settings=_settings(),
        pending_writer=_PendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        worker_store=worker_store,
        worker_launcher=lambda: None,
        paper_gate_checker=lambda: True,
    )

    with pytest.raises(LiveTradingDisabled, match="首次券商对账"):
        controller.arm("启用模拟交易")


def test_longbridge_fallback_authority_is_limited_to_intraday_to_comprehensive(
    tmp_path,
):
    worker_store = WorkerStore(tmp_path / "control.sqlite3")
    now = datetime.now(UTC)
    worker_store.record_heartbeat(
        worker_id="worker-lb",
        pid=123,
        state=WorkerState.RUNNING,
        last_successful_reconcile_at=now,
    )
    authority = WorkerNewRiskAuthority(worker_store, "worker-lb")
    plan = ExecutionPlan(
        id="lb-plan",
        analysis_digest="digest",
        analysis_record_path="records/lb.json",
        broker="longbridge",
        environment="live",
        product="securities",
        requested_account="intraday",
        allow_account_fallback=True,
        source_symbol="GLD.US",
        instrument="GLD.US",
        direction="long",
        entry_type="limit",
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        take_profit_1=Decimal("110"),
        take_profit_2=Decimal("120"),
        stop_loss=Decimal("95"),
        trade_confidence=80,
        created_at=utc_now_iso(),
        config_fingerprint="lb-fingerprint",
    )
    lease = worker_store.grant_new_risk_lease(
        worker_id="worker-lb",
        config_fingerprint=plan.config_fingerprint,
        requester="gui-lb",
        broker="longbridge",
        environment="live",
        account="intraday",
        ttl_seconds=60,
    )
    command, _ = worker_store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id=plan.id,
        requester="gui-lb",
        broker="longbridge",
        environment="live",
        account="intraday",
        new_risk_lease_id=lease.lease_id,
    )
    claimed = worker_store.claim_next(worker_id="worker-lb")
    authority.bind(claimed)
    try:
        assert authority.is_authorized(plan, "comprehensive") is True
        assert authority.is_authorized(plan, "paper") is False
        assert authority.is_authorized(
            plan.model_copy(update={"allow_account_fallback": False}),
            "comprehensive",
        ) is False
    finally:
        authority.clear()
