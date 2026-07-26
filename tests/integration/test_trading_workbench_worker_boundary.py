"""验证 GUI 只耐久入队，只有 ExecutionWorker 能调用券商适配器。"""
from __future__ import annotations

import ast
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import Qt

from pa_agent.config.paths import PROJECT_ROOT
from pa_agent.execution.controller import ExecutionController
from pa_agent.execution.models import AccountSnapshot, ExecutionState
from pa_agent.execution.service import ExecutionService
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker import ExecutionWorker, WorkerNewRiskAuthority
from pa_agent.execution.worker_protocol import (
    WorkerCommandAction,
    WorkerCommandStatus,
)
from pa_agent.execution.worker_store import WorkerStore
from pa_agent.gui.read_models import WorkbenchReadModel
from pa_agent.gui.trading_workbench import TradingWorkbench
from pa_agent.risk.runtime import RiskRuntimeState
from tests.unit.test_execution_controller import (
    _PendingWriter,
    _record,
    _settings,
)
from tests.unit.test_execution_service import FakeAdapter


class _ThreadRecordingAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.write_threads: list[str] = []

    def submit_entry(self, record):
        self.write_threads.append(threading.current_thread().name)
        return super().submit_entry(record)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_gui_enqueues_once_and_only_worker_calls_adapter(
    qtbot,
    tmp_path,
    monkeypatch,
):
    records_dir = tmp_path / "pending"
    records_dir.mkdir()
    analysis_record = _record()
    record_path = records_dir / "record.json"
    record_path.write_text(
        analysis_record.model_dump_json(indent=2),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pa_agent.config.paths.RECORDS_PENDING_DIR",
        records_dir,
    )

    settings = _settings()
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    worker_store = WorkerStore(tmp_path / "control.sqlite3")
    worker_id = "worker-gui-boundary"
    authority = WorkerNewRiskAuthority(worker_store, worker_id)
    adapter = _ThreadRecordingAdapter()
    original_preflight = adapter.preflight
    adapter.preflight = lambda plan: original_preflight(plan).model_copy(
        update={"broker_metadata": {"current_leverage": "20"}}
    )
    service = ExecutionService(
        settings=settings,
        pending_writer=None,
        store=execution_store,
        adapter_factories={
            "okx": lambda _plan: adapter,
            "longbridge": lambda _plan: adapter,
        },
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
        settings=settings,
        settings_path=tmp_path / "settings.json",
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
        now = datetime.now(UTC)
        execution_store.save_account_snapshot(
            AccountSnapshot(
                broker="okx",
                account_profile="okx-demo",
                equity=Decimal("1000"),
                available=Decimal("900"),
            )
        )
        worker_store.save_risk_runtime_state(
            RiskRuntimeState(
                route_key="okx:demo:okx",
                broker="okx",
                environment="demo",
                account="okx",
                account_identity="fixture-okx-demo",
                last_external_cashflow_bill_id="",
                last_account_bill_id="",
                last_account_bill_timestamp_ms=None,
                last_bill_scan_at=now,
                adjusted_high_water_usd=Decimal("1000"),
                last_total_equity_usd=Decimal("1000"),
                drawdown_usd=Decimal("0"),
                drawdown_fraction=Decimal("0"),
                kill_active=False,
                kill_reason="",
                kill_activated_at=None,
                updated_at=now,
            )
        )
        controller.arm("启用模拟交易")
        execution = controller.prepare_analysis(analysis_record)
        read_model = WorkbenchReadModel(
            settings=settings,
            data_source=SimpleNamespace(
                _connected=True,
                _symbol="XAU-USDT-SWAP",
                _timeframe="10m",
            ),
            execution_store=execution_store,
            worker_store=worker_store,
            account_route=("okx", "okx-demo"),
            control_route=("okx", "okx-demo"),
            campaign_state_path=tmp_path / "campaign-does-not-exist.json",
        )
        widget = TradingWorkbench(
            settings=settings,
            settings_path=tmp_path / "settings.json",
            service=controller,
            read_model=read_model,
        )
        qtbot.addWidget(widget)
        widget.show()
        widget.refresh_now()

        snapshot = read_model.capture()
        selected = widget._selected_execution()
        assert selected.id == execution.id
        assert selected.state is ExecutionState.READY
        assert snapshot.account.certainty.value == "confirmed"
        assert snapshot.risk_gate.value == "允许新增风险"
        assert snapshot.risk_gate.certainty.value == "confirmed"
        assert snapshot.route_alignment.value.startswith("路由匹配")
        widget._refresh_action_buttons(selected)
        assert widget._submit_button.isEnabled() is True, (
            controller.is_armed,
            snapshot.account,
            snapshot.risk_gate,
            snapshot.route_alignment,
            widget._configuration_dirty,
            widget._action_in_progress,
        )
        assert worker_store.list_commands() == []
        assert adapter.write_threads == []

        widget._technical_group.setChecked(True)
        qtbot.waitUntil(widget._submit_button.isVisible, timeout=1_000)
        qtbot.mouseClick(
            widget._submit_button,
            Qt.MouseButton.LeftButton,
        )
        qtbot.waitUntil(
            lambda: len(worker_store.list_commands()) == 1,
            timeout=5_000,
        )

        queued = worker_store.list_commands()
        assert len(queued) == 1
        assert queued[0].action is WorkerCommandAction.SUBMIT
        assert queued[0].status is WorkerCommandStatus.PENDING
        assert (
            controller.get_execution(execution.id).state
            is ExecutionState.READY
        )
        assert adapter.write_threads == []

        finished: list[object] = []

        def consume_once() -> None:
            finished.append(worker.run_once())

        worker_thread = threading.Thread(
            target=consume_once,
            name="test-execution-worker",
        )
        worker_thread.start()
        worker_thread.join(timeout=5)
        assert worker_thread.is_alive() is False
        qtbot.waitUntil(
            lambda: not any(
                thread.name == "pa-trading-workbench-action"
                for thread in threading.enumerate()
            ),
            timeout=5_000,
        )

        assert len(finished) == 1
        assert finished[0].id == queued[0].id
        assert finished[0].status is WorkerCommandStatus.SUCCEEDED
        assert len(worker_store.list_commands()) == 1
        assert adapter.write_threads == ["test-execution-worker"]
        submitted = controller.get_execution(execution.id)
        assert submitted.state is ExecutionState.ENTRY_PENDING

        forbidden = {
            "pa_agent.execution.adapter",
            "pa_agent.execution.service",
            "pa_agent.execution.okx_adapter",
            "pa_agent.execution.longbridge_adapter",
        }
        for relative_path in (
            Path("pa_agent/gui/trading_workbench.py"),
            Path("pa_agent/execution/controller.py"),
        ):
            imported = _imported_modules(Path(PROJECT_ROOT) / relative_path)
            assert imported.isdisjoint(forbidden)
    finally:
        controller.stop_monitoring()
        worker.close()
