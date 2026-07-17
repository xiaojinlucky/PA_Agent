from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from pa_agent.config.settings import Settings
from pa_agent.execution.errors import (
    BrokerApiError,
    BrokerRejected,
    CredentialError,
    LiveTradingDisabled,
    PreflightError,
    SubmissionUnknown,
)
from pa_agent.execution.models import (
    AccountSnapshot,
    ExecutionState,
    PreflightResult,
)
from pa_agent.execution.service import ExecutionService
from pa_agent.execution.store import ExecutionStore
from tests.unit.test_execution_plan_builder import _persist, _record


class FakePendingWriter:
    def __init__(self, path):
        self.path = path

    def full_path(self, _record):
        return self.path


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.submit_error = None
        self.preflight_error = None
        self.reconcile_error = None
        self.cancel_error = None
        self.reconcile_write_unknown = False
        self.runtime_id = ""
        self.identity = "okx-account-a"
        self.write_executor = lambda operation: operation()

    def bind_runtime_id(self, runtime_id):
        self.runtime_id = runtime_id

    def bind_write_executor(self, executor):
        self.write_executor = executor

    def account_identity(self, _plan, *, account_profile=None):
        return self.identity

    def preflight(self, plan):
        self.calls.append(("preflight", plan.id))
        if self.preflight_error:
            raise self.preflight_error
        return PreflightResult(
            selected_account="okx",
            account_identity=self.identity,
            quantity=plan.quantity,
            entry_price=plan.entry_price,
            take_profit_1=plan.take_profit_1,
            take_profit_2=plan.take_profit_2,
            stop_loss=plan.stop_loss,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
        )

    def prepare_submit(self, record):
        self.calls.append(("prepare_submit", record.id))
        return record.model_copy(
            update={
                "state": ExecutionState.SUBMITTING,
                "selected_account": "okx",
                "client_order_id": "client-entry",
                "broker_state": {
                    "entry_kind": "regular",
                    "entry_submitted_at": "2026-07-17T00:00:00+00:00",
                },
            }
        )

    def submit_entry(self, record):
        self.calls.append(("submit_entry", record.client_order_id))
        if self.submit_error:
            raise self.submit_error
        return record.model_copy(
            update={
                "state": ExecutionState.ENTRY_PENDING,
                "broker_order_id": "broker-entry",
            }
        )

    def reconcile(self, record, *, allow_writes):
        self.calls.append(("reconcile", record.state, allow_writes))
        if self.reconcile_error:
            raise self.reconcile_error
        if self.reconcile_write_unknown:
            return record.model_copy(
                update={
                    "broker_state": {
                        **record.broker_state,
                        "write_unknown": "protection",
                    },
                    "needs_attention": True,
                }
            )
        if record.state == ExecutionState.ENTRY_PENDING:
            return record.model_copy(
                update={
                    "state": ExecutionState.OPEN,
                    "filled_quantity": record.plan.quantity,
                    "remaining_quantity": record.plan.quantity,
                }
            )
        return record

    def request_exit(self, record, *, reason):
        self.calls.append(("request_exit", reason))
        return record.model_copy(update={"state": ExecutionState.EXIT_PENDING})

    def cancel_entry(self, record):
        if self.cancel_error:
            raise self.cancel_error
        return record.model_copy(
            update={"broker_state": {**record.broker_state, "cancel": True}}
        )

    def account_snapshot(
        self,
        _plan,
        *,
        account_profile=None,
        broker_metadata=None,
    ):
        del broker_metadata
        self.calls.append(("account_snapshot", account_profile))
        return AccountSnapshot(
            broker="okx",
            account_profile=account_profile or "okx-live",
            equity=Decimal("1000"),
        )


def _settings():
    settings = Settings()
    settings.execution.enabled = True
    settings.execution.selected_broker = "okx"
    settings.execution.okx.source_symbol = "XAUUSD"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    settings.execution.okx.quantity = "2"
    settings.execution.okx.product = "swap"
    return settings


def _service(
    tmp_path,
    monkeypatch,
    adapter,
    *,
    gate=True,
    paper_gate=True,
    settings=None,
):
    record = _record()
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    path = _persist(record, tmp_path)
    service = ExecutionService(
        settings=settings or _settings(),
        pending_writer=FakePendingWriter(path),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        adapter_factories={"okx": lambda _plan: adapter},
        gate_checker=lambda: gate,
        paper_gate_checker=lambda: paper_gate,
        okx_live_gate_checker=lambda: True,
    )
    return service, record


def test_session_gate_requires_hard_gate_and_exact_confirmation(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, monkeypatch, FakeAdapter(), gate=False)

    with pytest.raises(LiveTradingDisabled):
        service.arm("启用实盘交易")

    assert service.is_armed is False


def test_disabled_execution_module_cannot_be_armed(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, monkeypatch, FakeAdapter())
    service._settings.execution.enabled = False

    with pytest.raises(LiveTradingDisabled, match="执行模块"):
        service.arm("启用实盘交易")

    assert service.is_armed is False


def test_okx_live_requires_its_independent_hard_gate(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, monkeypatch, FakeAdapter())
    service._okx_live_gate_checker = lambda: False

    with pytest.raises(LiveTradingDisabled, match="OKX_LIVE_ENABLED"):
        service.arm("启用实盘交易")

    service._settings.execution.okx.simulated = True
    service.arm("启用实盘交易")
    assert service.is_armed is True


def test_longbridge_paper_uses_independent_gate_and_rearms_after_switch(
    tmp_path,
    monkeypatch,
):
    settings = _settings()
    settings.execution.selected_broker = "longbridge"
    settings.execution.longbridge.source_symbol = "XAUUSD"
    settings.execution.longbridge.instrument = "GLD.US"
    settings.execution.longbridge.quantity = "1"
    settings.execution.longbridge.preferred_account = "paper"
    service, _ = _service(
        tmp_path,
        monkeypatch,
        FakeAdapter(),
        gate=False,
        paper_gate=True,
        settings=settings,
    )

    assert service.arm_confirmation_text() == "启用模拟交易"
    with pytest.raises(LiveTradingDisabled, match="确认文字"):
        service.arm("启用实盘交易")

    service.arm("启用模拟交易")
    assert service.is_armed is True

    settings.execution.longbridge.preferred_account = "comprehensive"
    service.reload_settings(settings)
    assert service.is_armed is False
    assert service.arm_confirmation_text() == "启用实盘交易"
    with pytest.raises(LiveTradingDisabled, match="PA_AGENT_LIVE"):
        service.arm("启用实盘交易")


def test_prepare_is_idempotent_and_never_auto_submits_when_disarmed(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)

    first = service.prepare_analysis(record)
    second = service.prepare_analysis(record)

    assert first.id == second.id
    assert first.state == ExecutionState.READY
    assert not adapter.calls
    assert len(service.store.list_recent()) == 1


def test_submit_persists_intent_before_broker_call(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")

    submitted = service.submit(execution.id)

    assert submitted.state == ExecutionState.ENTRY_PENDING
    assert submitted.client_order_id == "client-entry"
    assert submitted.broker_order_id == "broker-entry"
    assert [event.kind for event in service.store.events(execution.id)] == [
        "plan_created",
        "preflight_passed",
        "submit_intent",
        "entry_accepted",
    ]


def test_unknown_submit_is_not_retried_and_disarms_session(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    adapter.submit_error = SubmissionUnknown("timeout")
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")

    unknown = service.submit(execution.id)

    assert unknown.state == ExecutionState.UNKNOWN
    assert unknown.client_order_id == "client-entry"
    assert service.is_armed is False
    assert [call[0] for call in adapter.calls].count("submit_entry") == 1


def test_unknown_submit_save_failure_rotates_runtime_and_never_resubmits(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    adapter.submit_error = SubmissionUnknown("timeout")
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    old_runtime = service._runtime_id
    original_save = service.store.save

    def fail_unknown_save(record, *, event_kind, event_payload=None):
        if event_kind == "entry_submit_unknown":
            raise RuntimeError("simulated sqlite commit failure")
        return original_save(
            record,
            event_kind=event_kind,
            event_payload=event_payload,
        )

    monkeypatch.setattr(service.store, "save", fail_unknown_save)
    with pytest.raises(RuntimeError, match="sqlite commit failure"):
        service.submit(execution.id)

    assert service.is_armed is False
    assert service._runtime_id != old_runtime
    assert [call[0] for call in adapter.calls].count("submit_entry") == 1

    monkeypatch.setattr(service.store, "save", original_save)
    adapter.submit_error = None
    service.arm("启用实盘交易")
    service.reconcile_once()

    assert [call[0] for call in adapter.calls].count("submit_entry") == 1
    persisted = service.store.get(execution.id)
    assert persisted is not None
    assert persisted.state == ExecutionState.UNKNOWN
    assert "submit_interrupted" in [
        event.kind for event in service.store.events(execution.id)
    ]


def test_disarmed_reconcile_is_read_only_but_still_updates_state(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    service.disarm()

    updates = service.reconcile_once()

    assert updates[0].state == ExecutionState.OPEN
    assert ("reconcile", submitted.state, False) in adapter.calls


def test_account_refresh_is_read_only_and_persisted(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)

    snapshot = service.refresh_account(execution.id)

    assert snapshot.equity == Decimal("1000")
    persisted = service.store.latest_account_snapshot("okx", "okx-live")
    assert persisted is not None
    assert persisted.equity == Decimal("1000")


def test_legacy_active_execution_without_identity_cannot_refresh_account(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    legacy_active = service.store.save(
        execution.model_copy(
            update={
                "state": ExecutionState.ENTRY_PENDING,
                "selected_account": "okx",
                "account_identity": "",
            }
        ),
        event_kind="legacy_active_fixture",
    )
    service.arm("启用实盘交易")

    with pytest.raises(CredentialError, match="身份指纹"):
        service.refresh_account(legacy_active.id)

    assert service.is_armed is False
    assert not [
        call for call in adapter.calls if call[0] == "account_snapshot"
    ]


def test_monitor_once_refreshes_actual_selected_account(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    service.store.save(
        submitted.model_copy(update={"selected_account": "comprehensive"}),
        event_kind="test_account_fallback",
    )
    adapter.calls.clear()

    service._monitor_once()

    assert ("account_snapshot", "comprehensive") in adapter.calls


def test_unknown_reconcile_write_disarms_entire_session(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    service.submit(execution.id)
    adapter.reconcile_write_unknown = True

    updates = service.reconcile_once()

    assert updates[0].broker_state["write_unknown"] == "protection"
    assert service.is_armed is False


def test_disarm_is_linearized_with_every_adapter_broker_write(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, analysis = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(analysis)
    service._adapter(execution.plan)
    service.arm("启用实盘交易")
    write_started = threading.Event()
    release_write = threading.Event()
    disarm_started = threading.Event()
    disarm_finished = threading.Event()
    errors: list[BaseException] = []

    def broker_write():
        write_started.set()
        if not release_write.wait(2):
            raise TimeoutError("test write release timeout")
        adapter.calls.append(("guarded_write",))

    def run_write():
        try:
            adapter.write_executor(broker_write)
        except BaseException as exc:
            errors.append(exc)

    def run_disarm():
        disarm_started.set()
        service.disarm()
        disarm_finished.set()

    writer = threading.Thread(target=run_write)
    writer.start()
    assert write_started.wait(2)
    disarmer = threading.Thread(target=run_disarm)
    disarmer.start()
    assert disarm_started.wait(2)
    assert disarm_finished.wait(0.1) is False

    release_write.set()
    writer.join(2)
    disarmer.join(2)

    assert errors == []
    assert disarm_finished.is_set()
    assert service.is_armed is False
    assert [call for call in adapter.calls if call[0] == "guarded_write"] == [
        ("guarded_write",)
    ]
    with pytest.raises(LiveTradingDisabled):
        adapter.write_executor(lambda: adapter.calls.append(("late_write",)))
    assert not [call for call in adapter.calls if call[0] == "late_write"]


def test_active_execution_blocks_account_switch_before_read_or_write(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    active = service.submit(execution.id)
    adapter.calls.clear()
    adapter.identity = "okx-account-b"

    updates = service.reconcile_once()

    assert updates[0].id == active.id
    assert updates[0].needs_attention is True
    assert updates[0].broker_state["identity_or_route_blocked"] is True
    assert service.is_armed is False
    assert not [call for call in adapter.calls if call[0] == "reconcile"]
    with pytest.raises(CredentialError, match="实际账户"):
        service.refresh_account(active.id)
    assert not [call for call in adapter.calls if call[0] == "account_snapshot"]


def test_manual_exit_account_switch_disarms_before_exit_intent(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    opened = service.reconcile_once()[0]
    assert opened.id == submitted.id
    assert opened.state == ExecutionState.OPEN
    adapter.calls.clear()
    adapter.identity = "okx-account-b"

    with pytest.raises(CredentialError, match="实际账户"):
        service.request_exit(opened.id, reason="manual")

    assert service.is_armed is False
    assert not [call for call in adapter.calls if call[0] == "request_exit"]


def test_restart_turns_persisted_submitting_intent_into_unknown_without_resubmit(
    tmp_path,
    monkeypatch,
):
    first_adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, first_adapter)
    execution = service.prepare_analysis(record)
    interrupted = service.store.save(
        execution.model_copy(
            update={
                "state": ExecutionState.SUBMITTING,
                "selected_account": "okx",
                "client_order_id": "persisted-client-id",
                "broker_state": {"entry_kind": "regular"},
            }
        ),
        event_kind="submit_intent",
    )
    restarted_adapter = FakeAdapter()
    restarted = ExecutionService(
        settings=_settings(),
        pending_writer=FakePendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(service.store.path),
        adapter_factories={"okx": lambda _plan: restarted_adapter},
        gate_checker=lambda: True,
        okx_live_gate_checker=lambda: True,
    )

    restarted.reconcile_once()
    recovered = restarted.store.get(interrupted.id)

    assert restarted.is_armed is False
    assert recovered is not None
    assert recovered.state == ExecutionState.UNKNOWN
    assert recovered.client_order_id == "persisted-client-id"
    assert not [call for call in restarted_adapter.calls if call[0] == "submit_entry"]
    assert "submit_interrupted" in [
        event.kind for event in restarted.store.events(interrupted.id)
    ]


def test_write_result_save_failure_rotates_runtime_and_prevents_reconcile_resubmit(
    tmp_path,
    monkeypatch,
):
    class MarkerAdapter(FakeAdapter):
        def reconcile(self, record, *, allow_writes):
            self.calls.append(("reconcile", record.state, allow_writes))
            marker = str(record.broker_state.get("submit_runtime_id") or "")
            if marker and marker != self.runtime_id:
                return record.model_copy(
                    update={
                        "broker_state": {
                            **record.broker_state,
                            "write_unknown": "protection",
                        },
                        "needs_attention": True,
                    }
                )
            if allow_writes:
                self.write_executor(
                    lambda: self.calls.append(("broker_write", marker))
                )
                return record.model_copy(update={"state": ExecutionState.OPEN})
            return record

    adapter = MarkerAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    old_runtime = adapter.runtime_id
    service.store.save(
        submitted.model_copy(
            update={
                "state": ExecutionState.PROTECTING,
                "broker_state": {
                    **submitted.broker_state,
                    "submit_runtime_id": old_runtime,
                },
            }
        ),
        event_kind="protection_intent_fixture",
    )
    original_save = service.store.save
    failed = False

    def fail_first_reconciled_save(record, *, event_kind, event_payload=None):
        nonlocal failed
        if event_kind == "reconciled" and not failed:
            failed = True
            raise RuntimeError("simulated sqlite commit failure")
        return original_save(
            record,
            event_kind=event_kind,
            event_payload=event_payload,
        )

    monkeypatch.setattr(service.store, "save", fail_first_reconciled_save)
    with pytest.raises(RuntimeError, match="sqlite commit failure"):
        service.reconcile_once()

    assert service.is_armed is False
    assert service._runtime_id != old_runtime
    assert len([call for call in adapter.calls if call[0] == "broker_write"]) == 1

    monkeypatch.setattr(service.store, "save", original_save)
    service.arm("启用实盘交易")
    service.reconcile_once()

    assert len([call for call in adapter.calls if call[0] == "broker_write"]) == 1
    persisted = service.store.get(submitted.id)
    assert persisted is not None
    assert persisted.broker_state["write_unknown"] == "protection"


def test_cancel_result_save_failure_rotates_runtime_and_prevents_cancel_resubmit(
    tmp_path,
    monkeypatch,
):
    class CancelMarkerAdapter(FakeAdapter):
        def cancel_entry(self, record):
            self.write_executor(
                lambda: self.calls.append(("broker_cancel", record.id))
            )
            return record.model_copy(
                update={
                    "broker_state": {
                        **record.broker_state,
                        "entry_cancel_requested": True,
                    }
                }
            )

        def reconcile(self, record, *, allow_writes):
            self.calls.append(("reconcile", record.state, allow_writes))
            marker = str(
                record.broker_state.get("entry_cancel_runtime_id") or ""
            )
            if marker and marker != self.runtime_id:
                return record.model_copy(
                    update={
                        "broker_state": {
                            **record.broker_state,
                            "entry_cancel_status": "unknown",
                            "write_unknown": "cancel_entry",
                        },
                        "needs_attention": True,
                    }
                )
            return record

    adapter = CancelMarkerAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    old_runtime = adapter.runtime_id
    original_save = service.store.save
    failed = False

    def fail_first_cancel_save(record, *, event_kind, event_payload=None):
        nonlocal failed
        if event_kind == "cancel_entry_requested" and not failed:
            failed = True
            raise RuntimeError("simulated sqlite commit failure")
        return original_save(
            record,
            event_kind=event_kind,
            event_payload=event_payload,
        )

    monkeypatch.setattr(service.store, "save", fail_first_cancel_save)
    with pytest.raises(RuntimeError, match="sqlite commit failure"):
        service.cancel_entry(submitted.id)

    assert service.is_armed is False
    assert service._runtime_id != old_runtime
    assert len([call for call in adapter.calls if call[0] == "broker_cancel"]) == 1

    monkeypatch.setattr(service.store, "save", original_save)
    service.arm("启用实盘交易")
    service.reconcile_once()

    assert len([call for call in adapter.calls if call[0] == "broker_cancel"]) == 1
    persisted = service.store.get(submitted.id)
    assert persisted is not None
    assert persisted.broker_state["write_unknown"] == "cancel_entry"


def test_private_preflight_api_error_is_persisted_as_blocked(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    adapter.preflight_error = BrokerApiError("50120", "permission denied")
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")

    blocked = service.submit(execution.id)

    assert blocked.state == ExecutionState.BLOCKED
    assert blocked.needs_attention is True
    assert not [call for call in adapter.calls if call[0] == "submit_entry"]


def test_ready_plan_is_blocked_when_route_changes_before_submit(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service._settings.execution.okx.api_base_url = "https://other.example"
    service.arm("启用实盘交易")

    blocked = service.submit(execution.id)

    assert blocked.state == ExecutionState.BLOCKED
    assert "配置已变化" in blocked.state_reason
    assert not [call for call in adapter.calls if call[0] == "preflight"]


def test_active_okx_execution_rebuilds_adapter_from_plan_snapshot(
    tmp_path,
    monkeypatch,
):
    service, record = _service(tmp_path, monkeypatch, FakeAdapter())
    execution = service.prepare_analysis(record)
    captured = {}

    class CapturingClient:
        def __init__(self, _credentials, *, base_url, simulated):
            captured["base_url"] = base_url
            captured["simulated"] = simulated

    class CapturingAdapter:
        def __init__(
            self,
            _client,
            *,
            margin_mode,
            entry_timeout_seconds,
            runtime_id,
            write_executor,
        ):
            captured["margin_mode"] = margin_mode
            captured["entry_timeout_seconds"] = entry_timeout_seconds
            captured["runtime_id_set"] = bool(runtime_id)
            captured["write_executor_set"] = callable(write_executor)

        def bind_runtime_id(self, _runtime_id):
            pass

        def bind_write_executor(self, _write_executor):
            pass

    monkeypatch.setattr(
        "pa_agent.execution.service.load_okx_credentials",
        lambda: object(),
    )
    monkeypatch.setattr(
        "pa_agent.execution.service.OkxRestClient",
        CapturingClient,
    )
    monkeypatch.setattr(
        "pa_agent.execution.service.OkxAdapter",
        CapturingAdapter,
    )
    service._adapter_factories = {}
    service._settings.execution.okx.api_base_url = "https://new.example"
    service._settings.execution.okx.margin_mode = "isolated"
    service._settings.execution.okx.simulated = True
    service._settings.execution.entry_timeout_seconds = 999
    service.reload_settings(service._settings)

    service._adapter(execution.plan)

    assert captured == {
        "base_url": "https://www.okx.com",
        "simulated": False,
        "margin_mode": "cross",
        "entry_timeout_seconds": 120,
        "runtime_id_set": True,
        "write_executor_set": True,
    }

    service._adapters.clear()
    legacy = execution.plan.model_copy(update={"okx_api_base_url": ""})
    with pytest.raises(PreflightError, match="不可变路由快照"):
        service._adapter(legacy)


def test_post_entry_rejection_keeps_position_monitored_and_disarms(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    service.store.save(
        submitted.model_copy(
            update={
                "state": ExecutionState.PROTECTING,
                "filled_quantity": submitted.plan.quantity,
                "remaining_quantity": submitted.plan.quantity,
            }
        ),
        event_kind="filled",
    )
    adapter.reconcile_error = BrokerRejected("protection rejected")

    service.reconcile_once()
    current = service.store.get(submitted.id)

    assert current is not None
    assert current.state == ExecutionState.PROTECTING
    assert current.needs_attention is True
    assert service.is_armed is False


@pytest.mark.parametrize(
    "active_state",
    [ExecutionState.ENTRY_PENDING, ExecutionState.PARTIALLY_FILLED],
)
def test_reconcile_cancel_rejection_keeps_entry_active_and_disarms(
    tmp_path,
    monkeypatch,
    active_state,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    if active_state == ExecutionState.PARTIALLY_FILLED:
        submitted = service.store.save(
            submitted.model_copy(
                update={
                    "state": active_state,
                    "filled_quantity": Decimal("1"),
                    "remaining_quantity": Decimal("1"),
                }
            ),
            event_kind="test_partial_fill",
        )
    adapter.reconcile_error = BrokerRejected("cancel rejected")

    updated = service.reconcile_once()[0]

    assert updated.state == active_state
    assert updated.needs_attention is True
    assert service.is_armed is False
    assert service.store.route_claim_owner(updated) == updated.id

    adapter.reconcile_error = None
    adapter.calls.clear()
    service.reconcile_once()
    assert ("reconcile", active_state, False) in adapter.calls


def test_manual_cancel_rejection_keeps_entry_monitored_and_disarms(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    adapter.cancel_error = BrokerRejected("cancel rejected")

    updated = service.cancel_entry(submitted.id)

    assert updated.state == ExecutionState.ENTRY_PENDING
    assert updated.needs_attention is True
    assert service.is_armed is False
