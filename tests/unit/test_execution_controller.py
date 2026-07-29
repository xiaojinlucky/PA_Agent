from __future__ import annotations

import hashlib
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pa_agent.config.settings import Settings
from pa_agent.execution.controller import ExecutionController
from pa_agent.execution.errors import BrokerRejected, LiveTradingDisabled
from pa_agent.execution.leverage_authorization import (
    validate_current_leverage_policy,
)
from pa_agent.execution.models import (
    AccountSnapshot,
    ExecutionPlan,
    ExecutionState,
    utc_now_iso,
)
from pa_agent.execution.plan_builder import execution_route_fingerprint
from pa_agent.execution.service import ExecutionService
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker import ExecutionWorker, WorkerNewRiskAuthority
from pa_agent.execution.worker_protocol import (
    SetLeverageParameters,
    WorkerCommandAction,
    WorkerCommandResolutionEvidence,
    WorkerCommandStatus,
    WorkerState,
    leverage_intent_snapshot,
)
from pa_agent.execution.worker_store import WorkerStore
from pa_agent.records.schema import AnalysisRecord, RecordMeta
from tests.unit.leverage_authorization_helpers import (
    authorized_leverage_parameters,
)
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
    settings.execution.okx.risk_capital_cap_usdt = "2"
    settings.execution.okx.risk_percent = "0.10"
    settings.execution.okx.maximum_leverage = "20"
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


def _strict_script_parameters(
    *,
    analysis_path: Path,
    record: AnalysisRecord,
    config_fingerprint: str,
    expected_account_identity: str,
) -> SetLeverageParameters:
    parameters, authorized_record = authorized_leverage_parameters(
        analysis_path=analysis_path,
        record=record,
        config_fingerprint=config_fingerprint,
        expected_account_identity=expected_account_identity,
    )
    payload = parameters.model_dump(mode="python")
    payload.update(
        {
            "planning_method": "bounded_sequential_policy_grid_v2",
            "maximum_leverage": Decimal("20"),
            "exchange_maximum_leverage": Decimal("125"),
            "user_maximum_leverage": Decimal("20"),
            "maximum_capacity": Decimal("20"),
            "verified_grid": (
                {"leverage": "5", "capacity": "10"},
                {"leverage": "10", "capacity": "30"},
                {"leverage": "15", "capacity": "25"},
                {"leverage": "20", "capacity": "20"},
            ),
            "leverage_intent_digest": "",
            "supervisor_record_id": "",
            "supervisor_record_path": "",
            "supervisor_record_digest": "",
        }
    )
    draft = SetLeverageParameters.model_validate(payload)
    response = dict(authorized_record.stage2_response)
    response["risk_sizing"] = {
        "equity_basis": "fixed_cap_or_usdt_equity_whichever_lower",
        "account_total_equity_usd": "2",
        "equity_usdt": "2",
        "risk_capital_cap_usdt": "2",
        "effective_risk_capital_usdt": "2",
        "risk_percent": "0.10",
        "risk_budget_usdt": "0.20",
        "risk_used_usdt": "0.20",
        "reference_price_usdt": "100",
        "stop_distance_usdt": "5",
        "contract_notional_usdt": "0.2",
        "worst_case_loss_per_contract_usdt": "0.01",
        "fee_per_contract_usdt": "0",
        "slippage_per_contract_usdt": "0",
        "fee_rate": "0",
        "slippage_rate": "0",
        "minimum_quantity": "1",
        "quantity_step": "1",
        "max_buy": "30",
        "max_sell": "30",
        "target_quantity": "20",
    }
    response["leverage_intent"] = leverage_intent_snapshot(draft)
    authorized_record = authorized_record.model_copy(
        update={"stage2_response": response}
    )
    analysis_path.write_text(
        authorized_record.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return SetLeverageParameters.model_validate(
        {
            **draft.model_dump(mode="python"),
            "analysis_digest": hashlib.sha256(
                analysis_path.read_bytes()
            ).hexdigest(),
        }
    )


def test_fixed_quantity_leverage_policy_ignores_hidden_risk_percent(
    tmp_path,
):
    settings = _settings()
    settings.execution.okx.sizing_mode = "fixed_quantity"
    settings.execution.okx.quantity = "20"
    # 固定张数模式下，这个保存值不参与杠杆授权。
    settings.execution.okx.risk_percent = Decimal("0.99")
    analysis_path = tmp_path / "fixed-leverage-analysis.json"
    parameters = _strict_script_parameters(
        analysis_path=analysis_path,
        record=_record(),
        config_fingerprint=execution_route_fingerprint(settings, "okx"),
        expected_account_identity="b" * 64,
    )
    record = AnalysisRecord.model_validate_json(analysis_path.read_bytes())
    response = dict(record.stage2_response)
    risk_sizing = dict(response["risk_sizing"])
    risk_sizing["sizing_mode"] = "fixed_quantity"
    response["risk_sizing"] = risk_sizing
    analysis_path.write_text(
        record.model_copy(
            update={"stage2_response": response}
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )

    validate_current_leverage_policy(parameters, settings)


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
    lease = worker_store.current_new_risk_lease()
    assert lease is not None
    assert lease.command_id == command.id
    assert controller.is_armed is False


def test_submit_binding_and_local_command_are_atomic_against_renewal(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, _settings_obj, record = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    execution = controller.prepare_analysis(record)
    committed = threading.Event()
    release_submit = threading.Event()
    real_enqueue = worker_store.enqueue

    def enqueue_then_pause_after_real_commit(*args, **kwargs):
        result = real_enqueue(*args, **kwargs)
        committed.set()
        assert release_submit.wait(5), "测试未释放已提交的 enqueue"
        return result

    monkeypatch.setattr(worker_store, "enqueue", enqueue_then_pause_after_real_commit)
    submitted = []
    submit_errors = []

    def submit() -> None:
        try:
            submitted.append(controller.submit(execution.id))
        except BaseException as exc:
            submit_errors.append(exc)

    submit_thread = threading.Thread(target=submit)
    submit_thread.start()
    assert committed.wait(5), "真实 WorkerStore.enqueue 没有完成提交"

    # 这里只给真实 enqueue 增加提交后的同步点，不伪造安全结果。
    # 修复后，提交线程仍持有 Controller 锁，续租只能等本地 command_id 落定。
    lock_was_free = controller._lock.acquire(blocking=False)
    renew_thread = None
    if lock_was_free:
        controller._lock.release()
        controller._renew_lease()
    else:
        renew_thread = threading.Thread(target=controller._renew_lease)
        renew_thread.start()
    release_submit.set()
    submit_thread.join(timeout=5)
    if renew_thread is not None:
        renew_thread.join(timeout=5)

    assert submit_thread.is_alive() is False
    assert renew_thread is None or renew_thread.is_alive() is False
    assert lock_was_free is False
    assert submit_errors == []
    assert len(submitted) == 1
    lease = worker_store.current_new_risk_lease()
    assert lease is not None
    assert lease.command_id == submitted[0].id
    assert controller._lease_id == lease.lease_id
    assert controller._lease_command_id == submitted[0].id


@pytest.mark.parametrize(
    "terminal_status",
    [WorkerCommandStatus.SUCCEEDED, WorkerCommandStatus.FAILED],
)
def test_terminal_new_risk_command_releases_consumed_lease_without_rearming(
    tmp_path,
    monkeypatch,
    terminal_status,
):
    controller, worker_store, _settings_obj, record = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    execution = controller.prepare_analysis(record)
    command = controller.submit(execution.id)
    claimed = worker_store.claim_next(worker_id="worker-a")
    assert claimed is not None
    result_code = (
        "done"
        if terminal_status is WorkerCommandStatus.SUCCEEDED
        else ""
    )
    failure_code = (
        "rejected"
        if terminal_status is WorkerCommandStatus.FAILED
        else ""
    )
    worker_store.finish_command(
        command.id,
        worker_id="worker-a",
        status=terminal_status,
        result_code=result_code,
        failure_code=failure_code,
    )

    controller._renew_lease()

    assert controller.is_armed is False
    assert worker_store.current_new_risk_lease() is None


def test_terminal_race_during_renewal_still_releases_database_lease(
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
    assert worker_store.claim_next(worker_id="worker-a").id == command.id
    real_renew = worker_store.renew_new_risk_lease

    def finish_then_run_real_renew(*args, **kwargs):
        worker_store.finish_command(
            command.id,
            worker_id="worker-a",
            status=WorkerCommandStatus.SUCCEEDED,
            result_code="done",
        )
        return real_renew(*args, **kwargs)

    monkeypatch.setattr(
        worker_store,
        "renew_new_risk_lease",
        finish_then_run_real_renew,
    )

    controller._renew_lease()

    assert controller.is_armed is False
    assert worker_store.current_new_risk_lease() is None


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


def test_unresolved_broker_write_blocks_rearm_until_durable_resolution(
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
    assert worker_store.claim_next(worker_id="worker-a").id == command.id
    worker_store.recover_inflight(failure_code="worker_restarted")

    assert controller.is_armed is False
    controller._renew_lease()
    assert worker_store.current_new_risk_lease() is None
    with pytest.raises(LiveTradingDisabled, match="未解决"):
        controller.arm("启用模拟交易")

    worker_store.resolve_uncertain_command(
        command.id,
        resolution_code="confirmed_not_written",
        evidence=WorkerCommandResolutionEvidence(
            execution_id=command.execution_id,
            command_action=command.action.value,
            command_failure_code="worker_restarted",
            broker=command.broker,
            environment=command.environment,
            account=command.account,
            instrument=execution.plan.instrument,
            execution_state="canceled",
            broker_order_id_present=False,
            client_order_id_present=False,
            filled_quantity=Decimal("0"),
            event_kinds=("plan_created",),
            active_execution_count=0,
            new_risk_lease_present=False,
            broker_position_count=0,
            broker_pending_order_count=0,
            broker_pending_algo_order_count=0,
            broker_account_identity_digest="d" * 64,
            observed_at=datetime(2026, 7, 24, tzinfo=UTC),
        ),
        resolved_by="operator-audit",
    )
    controller.arm("启用模拟交易")

    assert controller.is_armed is True


def test_controller_enqueues_strict_demo_leverage_command_under_same_lease(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, settings, record_obj = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    parameters, _authorized_record = authorized_leverage_parameters(
        analysis_path=tmp_path / "pending" / "record.json",
        record=record_obj,
        config_fingerprint=execution_route_fingerprint(settings, "okx"),
        expected_account_identity="b" * 64,
    )

    command = controller.set_leverage(parameters)

    assert command.action is WorkerCommandAction.SET_LEVERAGE
    assert command.parameters == parameters
    assert command.new_risk_lease_id
    assert worker_store.get_command(command.id) == command


def test_controller_accepts_scripted_leverage_without_supervisor_record(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, settings, record_obj = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    scripted = _strict_script_parameters(
        analysis_path=tmp_path / "pending" / "record.json",
        record=record_obj,
        config_fingerprint=execution_route_fingerprint(settings, "okx"),
        expected_account_identity="b" * 64,
    )

    command = controller.set_leverage(scripted)

    assert command.parameters == scripted
    assert command.parameters.supervisor_record_id == ""
    assert worker_store.get_command(command.id) == command


def test_route_fingerprint_changes_with_each_okx_risk_setting():
    settings = _settings()
    baseline = execution_route_fingerprint(settings, "okx")

    settings.execution.okx.risk_capital_cap_usdt = "3"
    changed_cap = execution_route_fingerprint(settings, "okx")
    settings.execution.okx.risk_capital_cap_usdt = "2"
    settings.execution.okx.risk_percent = "0.20"
    changed_risk = execution_route_fingerprint(settings, "okx")
    settings.execution.okx.risk_percent = "0.10"
    settings.execution.okx.maximum_leverage = "25"
    changed_leverage = execution_route_fingerprint(settings, "okx")

    assert len({baseline, changed_cap, changed_risk, changed_leverage}) == 4


def test_controller_rejects_script_risk_quantity_tampering(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, settings, record = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    parameters = _strict_script_parameters(
        analysis_path=tmp_path / "pending" / "record.json",
        record=record,
        config_fingerprint=execution_route_fingerprint(settings, "okx"),
        expected_account_identity="b" * 64,
    )
    analysis_path = Path(parameters.analysis_record_path)
    persisted = AnalysisRecord.model_validate_json(analysis_path.read_bytes())
    response = dict(persisted.stage2_response)
    response["risk_sizing"] = {
        **response["risk_sizing"],
        "target_quantity": "21",
    }
    analysis_path.write_text(
        persisted.model_copy(
            update={"stage2_response": response}
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    tampered = parameters.model_copy(
        update={
            "analysis_digest": hashlib.sha256(
                analysis_path.read_bytes()
            ).hexdigest()
        }
    )

    with pytest.raises(
        LiveTradingDisabled,
        match="脚本风险数量与杠杆所需数量不一致",
    ):
        controller.set_leverage(tampered)

    assert worker_store.list_commands() == []


def test_controller_rejects_self_signed_125x_above_current_20x(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, settings, record = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    parameters = _strict_script_parameters(
        analysis_path=tmp_path / "pending" / "record.json",
        record=record,
        config_fingerprint=execution_route_fingerprint(settings, "okx"),
        expected_account_identity="b" * 64,
    )
    payload = parameters.model_dump(mode="python")
    payload.update(
        {
            "target_leverage": Decimal("25"),
            "target_capacity": Decimal("30"),
            "maximum_leverage": Decimal("125"),
            "exchange_maximum_leverage": Decimal("125"),
            "user_maximum_leverage": Decimal("125"),
            "maximum_capacity": Decimal("40"),
            "policy_grid_step": Decimal("100"),
            "verified_grid": (
                {"leverage": "5", "capacity": "10"},
                {"leverage": "25", "capacity": "30"},
                {"leverage": "125", "capacity": "40"},
            ),
            "leverage_intent_digest": "",
        }
    )
    forged = SetLeverageParameters.model_validate(payload)

    with pytest.raises(
        LiveTradingDisabled,
        match="超过当前用户最大杠杆",
    ):
        controller.set_leverage(forged)

    assert worker_store.list_commands() == []


def test_controller_authorizes_effective_limit_distinct_from_pa_signal(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, settings, record_obj = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    signal_entry = Decimal(
        str(record_obj.stage2_decision["decision"]["entry_price"])
    )
    parameters, _authorized_record = authorized_leverage_parameters(
        analysis_path=tmp_path / "pending" / "record.json",
        record=record_obj,
        config_fingerprint=execution_route_fingerprint(settings, "okx"),
        expected_account_identity="b" * 64,
        effective_entry_price=signal_entry + Decimal("2.5"),
    )

    command = controller.set_leverage(parameters)

    assert parameters.entry_price != signal_entry
    assert command.parameters.entry_price == signal_entry + Decimal("2.5")
    assert worker_store.get_command(command.id) == command


def test_controller_rejects_tampered_effective_limit_reference(
    tmp_path,
    monkeypatch,
):
    controller, worker_store, settings, record_obj = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    signal_entry = Decimal(
        str(record_obj.stage2_decision["decision"]["entry_price"])
    )
    parameters, authorized_record = authorized_leverage_parameters(
        analysis_path=tmp_path / "pending" / "record.json",
        record=record_obj,
        config_fingerprint=execution_route_fingerprint(settings, "okx"),
        expected_account_identity="b" * 64,
        effective_entry_price=signal_entry + Decimal("2.5"),
    )
    response = dict(authorized_record.stage2_response)
    response["risk_sizing"] = {"reference_price_usdt": str(signal_entry)}
    tampered_record = authorized_record.model_copy(
        update={"stage2_response": response}
    )
    analysis_path = Path(parameters.analysis_record_path)
    analysis_path.write_text(
        tampered_record.model_dump_json(indent=2),
        encoding="utf-8",
    )
    parameters = parameters.model_copy(
        update={
            "analysis_digest": hashlib.sha256(
                analysis_path.read_bytes()
            ).hexdigest()
        }
    )

    with pytest.raises(
        LiveTradingDisabled,
        match="没有匹配的耐久脚本授权",
    ):
        controller.set_leverage(parameters)

    assert worker_store.list_commands() == []


@pytest.mark.parametrize(
    "updates",
    [
        {
            "target_leverage": Decimal("9"),
            "target_capacity": Decimal("30"),
            "verified_grid": (
                {"leverage": "5", "capacity": "10"},
                {"leverage": "9", "capacity": "30"},
                {"leverage": "10", "capacity": "30"},
            ),
        },
        {"direction": "short"},
        {"required_quantity": Decimal("25")},
    ],
)
def test_controller_rejects_tampered_supervised_leverage_intent(
    tmp_path,
    monkeypatch,
    updates,
):
    controller, worker_store, settings, record = _controller(
        tmp_path,
        monkeypatch,
    )
    controller.arm("启用模拟交易")
    parameters, _authorized_record = authorized_leverage_parameters(
        analysis_path=tmp_path / "pending" / "record.json",
        record=record,
        config_fingerprint=execution_route_fingerprint(settings, "okx"),
        expected_account_identity="b" * 64,
    )
    tampered = SetLeverageParameters.model_validate(
        {
            **parameters.model_dump(mode="python"),
            **updates,
            "leverage_intent_digest": "",
        }
    )

    with pytest.raises(
        LiveTradingDisabled,
        match="没有匹配的耐久脚本授权",
    ):
        controller.set_leverage(tampered)

    assert worker_store.list_commands() == []


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
        project_root / "pa_agent" / "gui" / "trading_workbench.py",
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
    worker_settings.execution.longbridge.instrument = "700.HK"
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    worker_store = WorkerStore(tmp_path / "control.sqlite3")
    worker_id = "worker-e2e"
    authority = WorkerNewRiskAuthority(worker_store, worker_id)
    adapter = FakeAdapter()
    base_preflight = adapter.preflight
    adapter.preflight = lambda plan: base_preflight(plan).model_copy(
        update={"broker_metadata": {"current_leverage": "20"}}
    )
    service = ExecutionService(
        settings=worker_settings,
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
    worker_settings.execution.longbridge.instrument = "700.HK"
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    worker_store = WorkerStore(tmp_path / "control.sqlite3")
    worker_id = f"worker-rejected-{action}"
    authority = WorkerNewRiskAuthority(worker_store, worker_id)
    adapter = FakeAdapter()
    base_preflight = adapter.preflight
    adapter.preflight = lambda plan: base_preflight(plan).model_copy(
        update={"broker_metadata": {"current_leverage": "20"}}
    )
    service = ExecutionService(
        settings=worker_settings,
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
    _command, _ = worker_store.enqueue(
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
