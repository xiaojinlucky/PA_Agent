from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock
from pydantic import ValidationError as PydanticValidationError

from pa_agent.execution.errors import (
    BrokerRejected,
    BrokerTransportError,
    PreflightError,
)
from pa_agent.execution.models import (
    ExecutionPlan,
    ExecutionState,
    utc_now_iso,
)
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker import ExecutionWorker, WorkerAlreadyRunning
from pa_agent.execution.worker_protocol import (
    SetLeverageParameters,
    SetLeverageResult,
    WorkerCommandAction,
    WorkerCommandStatus,
    WorkerState,
)
from pa_agent.execution.worker_store import WorkerStore
from pa_agent.risk.runtime import RiskRuntimeBlocked


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class _FakeService:
    def __init__(self, store: ExecutionStore) -> None:
        self.store = store
        self.calls: list[tuple] = []
        self.failures: dict[str, BaseException] = {}
        self.reloads: list[tuple[object, bool]] = []
        self.reconcile_entered = threading.Event()
        self.reconcile_release: threading.Event | None = None
        self.leverage_reconcile_result = None

    def _fail(self, name: str) -> None:
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def disarm(self, *, revoke_external: bool = True) -> None:
        self.calls.append(("disarm", revoke_external))

    def reload_settings(
        self,
        settings,
        *,
        revoke_new_risk: bool = True,
    ) -> None:
        self.reloads.append((settings, revoke_new_risk))

    def submit(self, execution_id: str):
        self.calls.append(("submit", execution_id))
        self._fail("submit")

    def set_leverage(self, command):
        self.calls.append(("set_leverage", command.parameters))
        self._fail("set_leverage")
        return SetLeverageResult(
            instrument=command.parameters.instrument,
            confirmed_leverage=command.parameters.target_leverage,
            confirmed_max_size=command.parameters.required_quantity,
            broker_position_count=0,
            broker_pending_order_count=0,
            broker_pending_algo_order_count=0,
            account_identity=command.parameters.expected_account_identity,
            confirmed_at=datetime.now(UTC),
        )

    def reconcile_leverage(self, command):
        self.calls.append(("reconcile_leverage", command.parameters))
        if self.leverage_reconcile_result is not None:
            return self.leverage_reconcile_result
        return SetLeverageResult(
            instrument=command.parameters.instrument,
            confirmed_leverage=command.parameters.target_leverage,
            confirmed_max_size=command.parameters.required_quantity,
            broker_position_count=0,
            broker_pending_order_count=0,
            broker_pending_algo_order_count=0,
            account_identity=command.parameters.expected_account_identity,
            confirmed_at=datetime.now(UTC),
        )

    def cancel_entry(self, execution_id: str):
        self.calls.append(("cancel_entry", execution_id))
        self._fail("cancel_entry")

    def request_exit(self, execution_id: str, *, reason: str):
        self.calls.append(("request_exit", execution_id, reason))
        self._fail("request_exit")

    def refresh_account(self, execution_id: str | None = None):
        self.calls.append(("refresh_account", execution_id))
        self._fail("refresh_account")

    def refresh_account_route(self, *, broker, environment, account):
        self.calls.append(
            ("refresh_account_route", broker, environment, account)
        )
        self._fail("refresh_account_route")

    def clear_drawdown_stop(self, *, broker, environment, account):
        self.calls.append(
            ("clear_drawdown_stop", broker, environment, account)
        )
        self._fail("clear_drawdown_stop")

    def recover_transient_risk_stop(self, *, broker, environment, account):
        self.calls.append(
            ("recover_transient_risk_stop", broker, environment, account)
        )
        self._fail("recover_transient_risk_stop")

    def reconcile_once(self):
        self.calls.append(("reconcile",))
        self.reconcile_entered.set()
        release = self.reconcile_release
        if release is not None:
            assert release.wait(5)
        self._fail("reconcile")
        return []


def _plan(execution_id: str = "execution-one") -> ExecutionPlan:
    return ExecutionPlan(
        id=execution_id,
        analysis_digest=f"digest-{execution_id}",
        analysis_record_path="records/analysis.json",
        broker="okx",
        environment="demo",
        product="swap",
        requested_account="okx",
        source_symbol="XAUUSD",
        instrument="XAU-USDT-SWAP",
        direction="long",
        entry_type="market",
        quantity=Decimal("1"),
        entry_price=Decimal("2400"),
        take_profit_1=Decimal("2410"),
        take_profit_2=Decimal("2420"),
        stop_loss=Decimal("2390"),
        trade_confidence=80,
        created_at=utc_now_iso(),
        config_fingerprint=f"fingerprint-{execution_id}",
        okx_api_base_url="https://www.okx.com",
        okx_margin_mode="cross",
    )


def _leverage_parameters() -> SetLeverageParameters:
    return SetLeverageParameters(
        analysis_digest="a" * 64,
        analysis_record_path="records/pending/analysis.json",
        config_fingerprint="fingerprint-execution-one",
        instrument="XAU-USDT-SWAP",
        direction="long",
        margin_mode="cross",
        position_mode="net_mode",
        current_leverage=Decimal("5"),
        target_leverage=Decimal("10"),
        current_capacity=Decimal("10"),
        target_capacity=Decimal("20"),
        maximum_leverage=Decimal("10"),
        maximum_capacity=Decimal("20"),
        planning_method="bounded_sequential_policy_grid_v1",
        policy_grid_step=Decimal("5"),
        verified_grid=(
            {"leverage": "5", "capacity": "10"},
            {"leverage": "10", "capacity": "20"},
        ),
        required_quantity=Decimal("20"),
        entry_price=Decimal("4000"),
        expected_account_identity="b" * 64,
        okx_api_base_url="https://www.okx.com",
        supervisor_record_id="supervisor-record",
        supervisor_record_path="records/supervisor/decision.json",
        supervisor_record_digest="d" * 64,
    )


def _runtime(
    tmp_path: Path,
    *,
    clock=None,
    worker_id: str = "worker-one",
    heartbeat_interval_seconds: float = 0.02,
    settings=None,
    settings_path: Path | None = None,
    settings_loader=None,
):
    database = tmp_path / "execution.sqlite3"
    execution_store = ExecutionStore(database)
    execution_store.create(_plan())
    worker_store = WorkerStore(database, clock=clock) if clock else WorkerStore(database)
    service = _FakeService(execution_store)
    kwargs = {}
    if settings_loader is not None:
        kwargs["settings_loader"] = settings_loader
    worker = ExecutionWorker(
        store=worker_store,
        service=service,
        settings=settings,
        settings_path=settings_path,
        lock_path=tmp_path / "execution.worker.lock",
        worker_id=worker_id,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        clock=clock or (lambda: datetime.now(UTC)),
        **kwargs,
    )
    return worker, worker_store, service


def _grant_submit(
    worker: ExecutionWorker,
    store: WorkerStore,
    *,
    requester: str = "gui-session",
    ttl_seconds: int = 60,
):
    lease = store.grant_new_risk_lease(
        worker_id=worker.worker_id,
        config_fingerprint="fingerprint-execution-one",
        requester=requester,
        broker="okx",
        environment="demo",
        account="okx",
        ttl_seconds=ttl_seconds,
    )
    assert lease is not None
    return lease


def _enqueue_execution(
    store: WorkerStore,
    *,
    action: WorkerCommandAction,
    lease_id: str = "",
    account: str = "okx",
    reason_code: str = "",
):
    command, created = store.enqueue(
        action=action,
        execution_id="execution-one",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account=account,
        new_risk_lease_id=lease_id,
        reason_code=reason_code,
    )
    assert created
    return command


def test_worker_uses_nonblocking_single_instance_lock(tmp_path, monkeypatch):
    worker, store, service = _runtime(tmp_path)
    baseline_calls = []
    backfill = store.backfill_risk_runtime_baselines

    def _backfill(*, worker_lock):
        baseline_calls.append(bool(worker_lock.is_locked))
        return backfill(worker_lock=worker_lock)

    monkeypatch.setattr(store, "backfill_risk_runtime_baselines", _backfill)
    contender = ExecutionWorker(
        store=store,
        service=_FakeService(service.store),
        lock_path=tmp_path / "execution.worker.lock",
        worker_id="worker-two",
        heartbeat_interval_seconds=0.02,
    )

    worker.start()
    try:
        assert baseline_calls == [True]
        with pytest.raises(WorkerAlreadyRunning):
            contender.start()
    finally:
        worker.close()

    contender.start()
    contender.close()


def test_stop_request_keeps_lock_until_inflight_command_finishes(tmp_path):
    worker, store, service = _runtime(tmp_path)
    contender = ExecutionWorker(
        store=store,
        service=_FakeService(service.store),
        lock_path=tmp_path / "execution.worker.lock",
        worker_id="worker-two",
        heartbeat_interval_seconds=0.02,
    )
    entered = threading.Event()
    release = threading.Event()

    def _blocked_cancel(_execution_id):
        entered.set()
        assert release.wait(5)

    service.cancel_entry = _blocked_cancel
    worker.start()
    command = _enqueue_execution(
        store,
        action=WorkerCommandAction.CANCEL_ENTRY,
    )
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert entered.wait(2)

    worker.request_stop()
    with pytest.raises(WorkerAlreadyRunning):
        contender.start()

    release.set()
    thread.join(2)
    assert thread.is_alive() is False
    assert store.get_command(command.id).status is (
        WorkerCommandStatus.SUCCEEDED
    )
    worker.close()
    contender.start()
    contender.close()


def test_v1_worker_schema_migrates_only_after_singleton_lock(tmp_path):
    database = tmp_path / "execution.sqlite3"
    execution_store = ExecutionStore(database)
    execution_store.create(_plan())
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE worker_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO worker_meta(key, value)
            VALUES ('worker_schema_version', '1');
            CREATE TABLE worker_commands (
                id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                action TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                requester TEXT NOT NULL,
                broker TEXT NOT NULL,
                environment TEXT NOT NULL,
                account TEXT NOT NULL,
                new_risk_lease_id TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                status TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                result_code TEXT NOT NULL,
                failure_code TEXT NOT NULL
            );
            """
        )
    store = WorkerStore(database)
    lock_path = tmp_path / "execution.worker.lock"
    worker = ExecutionWorker(
        store=store,
        service=_FakeService(execution_store),
        lock_path=lock_path,
        worker_id="replacement-worker",
        heartbeat_interval_seconds=0.02,
    )
    old_worker_lock = FileLock(str(lock_path))
    old_worker_lock.acquire(timeout=0)
    try:
        with pytest.raises(WorkerAlreadyRunning):
            worker.start()
        with sqlite3.connect(database) as connection:
            version_while_locked = connection.execute(
                """
                SELECT value FROM worker_meta
                WHERE key='worker_schema_version'
                """
            ).fetchone()
        assert version_while_locked == ("1",)
        assert store.schema_version == 1
    finally:
        old_worker_lock.release()

    worker.start()
    try:
        with sqlite3.connect(database) as connection:
            migrated_version = connection.execute(
                """
                SELECT value FROM worker_meta
                WHERE key='worker_schema_version'
                """
            ).fetchone()
            assert migrated_version == ("4",)
        assert store.schema_version == 4
    finally:
        worker.close()


def test_startup_recovers_running_without_replay(tmp_path):
    worker, store, service = _runtime(tmp_path)
    command, _ = store.enqueue(
        action=WorkerCommandAction.RECONCILE,
        requester="system",
        broker="okx",
        environment="demo",
        account="okx",
    )
    assert store.claim_next(worker_id="crashed-worker").id == command.id

    worker.start()
    try:
        assert store.get_command(command.id).status is WorkerCommandStatus.UNCERTAIN
        assert worker.run_once() is None
        assert service.calls.count(("reconcile",)) == 1
    finally:
        worker.close()


def test_execution_route_is_reloaded_and_tampering_is_rejected(tmp_path):
    worker, store, service = _runtime(tmp_path)
    worker.start()
    try:
        command = _enqueue_execution(
            store,
            action=WorkerCommandAction.CANCEL_ENTRY,
            account="tampered-account",
        )
        finished = worker.run_once()
    finally:
        worker.close()

    assert finished.id == command.id
    assert finished.status is WorkerCommandStatus.FAILED
    assert finished.failure_code == "execution_route_mismatch"
    assert not [call for call in service.calls if call[0] == "cancel_entry"]


def test_route_only_account_refresh_uses_immutable_command_route(
    tmp_path,
):
    settings = SimpleNamespace(
        execution=SimpleNamespace(
            poll_interval_seconds=1.0,
            selected_broker="longbridge",
            longbridge=SimpleNamespace(preferred_account="comprehensive"),
            okx=SimpleNamespace(simulated=False),
        )
    )
    worker, store, service = _runtime(tmp_path, settings=settings)
    worker.start()
    try:
        command, _ = store.enqueue(
            action=WorkerCommandAction.REFRESH_ACCOUNT,
            requester="campaign",
            broker="okx",
            environment="demo",
            account="okx",
        )
        result = worker.run_once()
    finally:
        worker.close()

    assert result.id == command.id
    assert result.status is WorkerCommandStatus.SUCCEEDED
    assert (
        "refresh_account_route",
        "okx",
        "demo",
        "okx",
    ) in service.calls
    assert not [call for call in service.calls if call[0] == "refresh_account"]


def test_route_only_account_refresh_rejects_forged_live_route(tmp_path):
    settings = SimpleNamespace(
        execution=SimpleNamespace(
            poll_interval_seconds=1.0,
            selected_broker="longbridge",
            longbridge=SimpleNamespace(preferred_account="comprehensive"),
            okx=SimpleNamespace(simulated=False),
        )
    )
    worker, store, service = _runtime(tmp_path, settings=settings)
    worker.start()
    try:
        command, _ = store.enqueue(
            action=WorkerCommandAction.REFRESH_ACCOUNT,
            requester="campaign",
            broker="okx",
            environment="live",
            account="okx",
        )
        result = worker.run_once()
    finally:
        worker.close()

    assert result.id == command.id
    assert result.status is WorkerCommandStatus.FAILED
    assert result.failure_code == "account_refresh_route_mismatch"
    assert not [
        call
        for call in service.calls
        if call[0] in {"refresh_account", "refresh_account_route"}
    ]


def test_clear_uses_persisted_command_route(tmp_path):
    settings = SimpleNamespace(
        execution=SimpleNamespace(
            poll_interval_seconds=1.0,
            selected_broker="longbridge",
            longbridge=SimpleNamespace(preferred_account="comprehensive"),
            okx=SimpleNamespace(simulated=False),
        )
    )
    worker, store, service = _runtime(tmp_path, settings=settings)
    worker.start()
    try:
        command, _ = store.enqueue(
            action=WorkerCommandAction.CLEAR_DRAWDOWN_STOP,
            requester="controller",
            broker="okx",
            environment="demo",
            account="okx",
        )
        result = worker.run_once()
    finally:
        worker.close()

    assert result.id == command.id
    assert result.status is WorkerCommandStatus.SUCCEEDED
    assert (
        "clear_drawdown_stop",
        "okx",
        "demo",
        "okx",
    ) in service.calls


def test_transient_risk_recovery_uses_persisted_command_route(tmp_path):
    settings = SimpleNamespace(
        execution=SimpleNamespace(
            poll_interval_seconds=1.0,
            selected_broker="longbridge",
            longbridge=SimpleNamespace(preferred_account="comprehensive"),
            okx=SimpleNamespace(simulated=False),
        )
    )
    worker, store, service = _runtime(tmp_path, settings=settings)
    worker.start()
    try:
        command, _ = store.enqueue(
            action=WorkerCommandAction.CLEAR_DRAWDOWN_STOP,
            requester="controller",
            broker="okx",
            environment="demo",
            account="okx",
            reason_code="recover_transient_risk_read_failure",
        )
        result = worker.run_once()
    finally:
        worker.close()

    assert result.id == command.id
    assert result.status is WorkerCommandStatus.SUCCEEDED
    assert (
        "recover_transient_risk_stop",
        "okx",
        "demo",
        "okx",
    ) in service.calls


def test_startup_revokes_old_lease_and_expired_lease_cannot_submit(tmp_path):
    clock = _MutableClock(datetime(2026, 7, 20, 8, 0, tzinfo=UTC))
    worker, store, service = _runtime(tmp_path, clock=clock)
    old_lease = store.grant_new_risk_lease(
        worker_id="old-worker",
        config_fingerprint="fingerprint-execution-one",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="okx",
        ttl_seconds=60,
    )
    old_command = _enqueue_execution(
        store,
        action=WorkerCommandAction.SUBMIT,
        lease_id=old_lease.lease_id,
    )

    worker.start()
    try:
        assert store.get_command(old_command.id).status is WorkerCommandStatus.FAILED
        current = _grant_submit(worker, store, ttl_seconds=5)
        expired_command = _enqueue_execution(
            store,
            action=WorkerCommandAction.SUBMIT,
            lease_id=current.lease_id,
        )
        clock.advance(seconds=6)
        assert worker.run_once() is None
    finally:
        worker.close()

    expired = store.get_command(expired_command.id)
    assert expired.status is WorkerCommandStatus.FAILED
    assert expired.failure_code == "new_risk_expired"
    assert not [call for call in service.calls if call[0] == "submit"]


def test_worker_dispatches_all_commands_with_bound_reason(tmp_path):
    worker, store, service = _runtime(tmp_path)
    worker.start()
    try:
        lease = _grant_submit(worker, store)
        submit = _enqueue_execution(
            store,
            action=WorkerCommandAction.SUBMIT,
            lease_id=lease.lease_id,
        )
        assert worker.run_once().id == submit.id
        cancel = _enqueue_execution(
            store,
            action=WorkerCommandAction.CANCEL_ENTRY,
        )
        assert worker.run_once().id == cancel.id
        exit_command = _enqueue_execution(
            store,
            action=WorkerCommandAction.REQUEST_EXIT,
            reason_code="user_requested",
        )
        assert worker.run_once().id == exit_command.id
        refresh = _enqueue_execution(
            store,
            action=WorkerCommandAction.REFRESH_ACCOUNT,
        )
        assert worker.run_once().id == refresh.id
        reconcile, _ = store.enqueue(
            action=WorkerCommandAction.RECONCILE,
            requester="system",
            broker="okx",
            environment="demo",
            account="okx",
        )
        assert worker.run_once().id == reconcile.id
    finally:
        worker.close()

    assert ("submit", "execution-one") in service.calls
    assert ("cancel_entry", "execution-one") in service.calls
    assert (
        "request_exit",
        "execution-one",
        "user_requested",
    ) in service.calls
    assert ("refresh_account", "execution-one") in service.calls
    assert service.calls.count(("reconcile",)) == 2


def test_worker_persists_set_leverage_readback_without_execution_record(
    tmp_path,
):
    worker, store, service = _runtime(tmp_path)
    worker.start()
    try:
        lease = _grant_submit(worker, store)
        command, created = store.enqueue(
            action=WorkerCommandAction.SET_LEVERAGE,
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="okx",
            new_risk_lease_id=lease.lease_id,
            parameters=_leverage_parameters(),
        )
        assert created is True

        finished = worker.run_once()
    finally:
        worker.close()

    assert finished.id == command.id
    assert finished.status is WorkerCommandStatus.SUCCEEDED
    assert finished.result.confirmed_leverage == Decimal("10")
    assert finished.result.confirmed_max_size == Decimal("20")
    assert service.calls.count(
        ("set_leverage", _leverage_parameters())
    ) == 1


def test_worker_marks_set_leverage_unknown_and_never_replays(tmp_path):
    worker, store, service = _runtime(tmp_path)
    worker.start()
    try:
        lease = _grant_submit(worker, store)
        service.failures["set_leverage"] = BrokerTransportError(
            "readback unavailable",
            write_may_have_reached=True,
        )
        command, _ = store.enqueue(
            action=WorkerCommandAction.SET_LEVERAGE,
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="okx",
            new_risk_lease_id=lease.lease_id,
            parameters=_leverage_parameters(),
        )

        first = worker.run_once()
        second = worker.run_once()
    finally:
        worker.close()

    assert first.id == command.id
    assert first.status is WorkerCommandStatus.UNCERTAIN
    assert second is None
    assert len(
        [call for call in service.calls if call[0] == "set_leverage"]
    ) == 1


def test_worker_marks_prewrite_leverage_transport_failure_as_failed(tmp_path):
    worker, store, service = _runtime(tmp_path)
    worker.start()
    try:
        lease = _grant_submit(worker, store)
        service.failures["set_leverage"] = BrokerTransportError(
            "prewrite read unavailable",
            write_may_have_reached=False,
        )
        command, _ = store.enqueue(
            action=WorkerCommandAction.SET_LEVERAGE,
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="okx",
            new_risk_lease_id=lease.lease_id,
            parameters=_leverage_parameters(),
        )

        finished = worker.run_once()
    finally:
        worker.close()

    assert finished.id == command.id
    assert finished.status is WorkerCommandStatus.FAILED
    assert store.get_command_resolution(command.id) is None


def test_successful_work_and_reconcile_keep_unresolved_write_attention(tmp_path):
    worker, store, service = _runtime(tmp_path)
    worker.start()
    try:
        service.failures["cancel_entry"] = BrokerTransportError(
            "write uncertain",
            write_may_have_reached=True,
        )
        uncertain = _enqueue_execution(
            store,
            action=WorkerCommandAction.CANCEL_ENTRY,
        )
        assert worker.run_once().status is WorkerCommandStatus.UNCERTAIN
        service.failures.pop("cancel_entry")

        reconcile, _ = store.enqueue(
            action=WorkerCommandAction.RECONCILE,
            requester="system",
            broker="okx",
            environment="demo",
            account="okx",
        )
        assert worker.run_once().id == reconcile.id
        heartbeat_after_command = store.get_heartbeat(worker.worker_id)

        worker._run_reconcile()
        worker._set_available_heartbeat()
        heartbeat_after_periodic = store.get_heartbeat(worker.worker_id)
    finally:
        worker.close()

    assert store.get_command(uncertain.id).status is WorkerCommandStatus.UNCERTAIN
    assert heartbeat_after_command.state is WorkerState.NEEDS_ATTENTION
    assert heartbeat_after_command.last_error_code == "unresolved_broker_write"
    assert heartbeat_after_periodic.state is WorkerState.NEEDS_ATTENTION
    assert heartbeat_after_periodic.last_error_code == "unresolved_broker_write"


def test_periodic_reconcile_does_not_revoke_pending_submit(tmp_path):
    worker, store, service = _runtime(tmp_path)
    worker.start()
    reconcile_errors: list[BaseException] = []
    try:
        service.reconcile_entered.clear()
        service.reconcile_release = threading.Event()

        def finish_periodic_reconcile() -> None:
            try:
                worker._run_reconcile()
                worker._set_available_heartbeat()
            except BaseException as exc:  # pragma: no cover - assertion below
                reconcile_errors.append(exc)

        reconcile_thread = threading.Thread(
            target=finish_periodic_reconcile,
            name="test-periodic-reconcile",
        )
        reconcile_thread.start()
        assert service.reconcile_entered.wait(5)

        lease = _grant_submit(worker, store)
        submit = _enqueue_execution(
            store,
            action=WorkerCommandAction.SUBMIT,
            lease_id=lease.lease_id,
        )

        service.reconcile_release.set()
        reconcile_thread.join(timeout=5)
        assert not reconcile_thread.is_alive()
        heartbeat = store.get_heartbeat(worker.worker_id)
        pending = store.get_command(submit.id)
        current_lease = store.current_new_risk_lease()

        finished = worker.run_once()
    finally:
        if service.reconcile_release is not None:
            service.reconcile_release.set()
        worker.close()

    assert reconcile_errors == []
    assert heartbeat.state is WorkerState.RUNNING
    assert heartbeat.last_error_code == ""
    assert pending.status is WorkerCommandStatus.PENDING
    assert current_lease is not None
    assert current_lease.lease_id == lease.lease_id
    assert finished.id == submit.id
    assert finished.status is WorkerCommandStatus.SUCCEEDED
    assert ("submit", "execution-one") in service.calls


def test_worker_restart_resolves_unknown_leverage_by_readback_only(tmp_path):
    worker, store, service = _runtime(tmp_path)
    worker.start()
    try:
        lease = _grant_submit(worker, store)
        service.failures["set_leverage"] = BrokerTransportError(
            "readback unavailable",
            write_may_have_reached=True,
        )
        command, _ = store.enqueue(
            action=WorkerCommandAction.SET_LEVERAGE,
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="okx",
            new_risk_lease_id=lease.lease_id,
            parameters=_leverage_parameters(),
        )
        assert worker.run_once().status is WorkerCommandStatus.UNCERTAIN
    finally:
        worker.close()

    restarted = ExecutionWorker(
        store=store,
        service=service,
        lock_path=tmp_path / "execution.worker.lock",
        worker_id="worker-restarted",
        heartbeat_interval_seconds=0.02,
    )
    restarted.start()
    try:
        resolution = store.get_command_resolution(command.id)
        heartbeat = store.get_heartbeat(restarted.worker_id)
    finally:
        restarted.close()

    assert resolution.resolution_code == (
        "confirmed_applied_by_leverage_readback"
    )
    assert resolution.evidence.confirmed_leverage == Decimal("10")
    assert heartbeat.state is WorkerState.RUNNING
    assert len(
        [call for call in service.calls if call[0] == "set_leverage"]
    ) == 1
    assert len(
        [
            call
            for call in service.calls
            if call[0] == "reconcile_leverage"
        ]
    ) == 1


def test_exception_classification_and_masked_logging(tmp_path, caplog):
    worker, store, service = _runtime(tmp_path)
    caplog.set_level(logging.ERROR)
    worker.start()
    try:
        lease = _grant_submit(worker, store)
        service.failures["submit"] = PreflightError("deterministic")
        submit = _enqueue_execution(
            store,
            action=WorkerCommandAction.SUBMIT,
            lease_id=lease.lease_id,
        )
        assert worker.run_once().status is WorkerCommandStatus.FAILED
        service.failures["cancel_entry"] = BrokerTransportError(
            "token=abcdefghijklmnopqrstuvwxyz0123456789",
            write_may_have_reached=True,
        )
        cancel = _enqueue_execution(
            store,
            action=WorkerCommandAction.CANCEL_ENTRY,
        )
        assert worker.run_once().status is WorkerCommandStatus.UNCERTAIN
        service.failures["refresh_account"] = RuntimeError("read failed")
        refresh = _enqueue_execution(
            store,
            action=WorkerCommandAction.REFRESH_ACCOUNT,
        )
        assert worker.run_once().status is WorkerCommandStatus.FAILED
    finally:
        worker.close()

    assert store.get_command(submit.id).failure_code == "PreflightError"
    assert store.get_command(cancel.id).failure_code == "BrokerTransportError"
    assert store.get_command(refresh.id).failure_code == "RuntimeError"
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in caplog.text
    assert "<redacted>" in caplog.text


def test_masked_logging_includes_safe_transport_cause(tmp_path, caplog):
    worker, _store, service = _runtime(tmp_path)
    caplog.set_level(logging.ERROR)
    failure = RiskRuntimeBlocked(
        "risk_runtime_BrokerTransportError",
        "风险读取失败",
    )
    failure.__cause__ = BrokerTransportError(
        "OKX GET /api/v5/account/bills 网络请求失败（SSLEOFError）",
        write_may_have_reached=False,
    )
    service.failures["reconcile"] = failure
    worker.start()
    try:
        pass
    finally:
        worker.close()

    assert "RiskRuntimeBlocked: 风险读取失败" in caplog.text
    assert "GET /api/v5/account/bills" in caplog.text
    assert "SSLEOFError" in caplog.text


def test_invalid_execution_record_fails_before_broker_write(tmp_path):
    worker, store, service = _runtime(tmp_path)

    def _invalid_record(_execution_id: str):
        try:
            ExecutionPlan.model_validate({})
        except PydanticValidationError as exc:
            raise exc
        raise AssertionError("测试必须产生 Pydantic 字段校验错误")

    service.store.get = _invalid_record
    worker.start()
    try:
        lease = _grant_submit(worker, store)
        command = _enqueue_execution(
            store,
            action=WorkerCommandAction.SUBMIT,
            lease_id=lease.lease_id,
        )
        result = worker.run_once()
    finally:
        worker.close()

    assert result.id == command.id
    assert result.status is WorkerCommandStatus.FAILED
    assert result.failure_code == "execution_record_invalid"
    assert not [call for call in service.calls if call[0] == "submit"]


def test_broker_rejected_exit_is_failed_not_uncertain(tmp_path):
    worker, store, service = _runtime(tmp_path)
    service.failures["request_exit"] = BrokerRejected("broker rejected")
    worker.start()
    try:
        command = _enqueue_execution(
            store,
            action=WorkerCommandAction.REQUEST_EXIT,
            reason_code="manual",
        )
        result = worker.run_once()
    finally:
        worker.close()

    assert result.id == command.id
    assert result.status is WorkerCommandStatus.FAILED
    assert result.failure_code == "BrokerRejected"


def test_account_snapshot_failure_never_advances_reconcile_health(tmp_path):
    worker, store, service = _runtime(tmp_path)

    def _failed_monitor():
        raise RuntimeError("account snapshot unavailable")

    service.monitor_once = _failed_monitor
    worker.start()
    try:
        heartbeat = store.get_heartbeat(worker.worker_id)
    finally:
        worker.close()

    assert heartbeat.state is WorkerState.NEEDS_ATTENTION
    assert heartbeat.last_successful_reconcile_at is None


def test_delayed_heartbeat_cannot_overwrite_newer_attention_state(tmp_path):
    worker, store, _service = _runtime(
        tmp_path,
        heartbeat_interval_seconds=0.01,
    )
    entered = threading.Event()
    release = threading.Event()
    original_record = store.record_heartbeat

    def delayed_record(
        *,
        worker_id,
        pid,
        state,
        last_successful_reconcile_at=None,
        last_error_code="",
    ):
        if (
            threading.current_thread().name.startswith(
                "execution-heartbeat-"
            )
            and state is WorkerState.RECONCILING
            and not entered.is_set()
        ):
            entered.set()
            assert release.wait(timeout=2)
        return original_record(
            worker_id=worker_id,
            pid=pid,
            state=state,
            last_successful_reconcile_at=last_successful_reconcile_at,
            last_error_code=last_error_code,
        )

    store.record_heartbeat = delayed_record
    worker._heartbeat_state = WorkerState.RECONCILING
    worker._start_heartbeat_thread()
    try:
        assert entered.wait(timeout=2)
        setter = threading.Thread(
            target=lambda: worker._set_heartbeat(
                WorkerState.NEEDS_ATTENTION,
                last_error_code="snapshot_failed",
            )
        )
        setter.start()
        time.sleep(0.05)
        assert setter.is_alive()
        release.set()
        setter.join(timeout=2)
        assert not setter.is_alive()
        heartbeat = store.get_heartbeat(worker.worker_id)
    finally:
        release.set()
        worker._stop_heartbeat_thread()

    assert heartbeat.state is WorkerState.NEEDS_ATTENTION
    assert heartbeat.last_error_code == "snapshot_failed"


def test_invalid_settings_do_not_block_pending_exit(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("valid", encoding="utf-8")
    settings = SimpleNamespace(
        execution=SimpleNamespace(poll_interval_seconds=1.0)
    )

    def _invalid_loader(_path):
        raise ValueError("invalid settings")

    worker, store, service = _runtime(
        tmp_path,
        settings=settings,
        settings_path=settings_path,
        settings_loader=_invalid_loader,
    )
    worker.start()
    try:
        command = _enqueue_execution(
            store,
            action=WorkerCommandAction.REQUEST_EXIT,
            reason_code="manual",
        )
        settings_path.write_text("broken", encoding="utf-8")
        result = worker.run_once()
        worker._run_reconcile()
        worker._set_heartbeat(WorkerState.RUNNING)
        heartbeat = store.get_heartbeat(worker.worker_id)
    finally:
        worker.close()

    assert result.id == command.id
    assert result.status is WorkerCommandStatus.SUCCEEDED
    assert ("request_exit", "execution-one", "manual") in service.calls
    assert heartbeat.state is WorkerState.NEEDS_ATTENTION


def test_needs_attention_revokes_lease_and_fails_pending_submit(tmp_path):
    worker, store, service = _runtime(tmp_path)
    worker.start()
    try:
        lease = _grant_submit(worker, store)
        service.failures["cancel_entry"] = BrokerTransportError(
            "write uncertain",
            write_may_have_reached=True,
        )
        cancel = _enqueue_execution(
            store,
            action=WorkerCommandAction.CANCEL_ENTRY,
        )
        submit = _enqueue_execution(
            store,
            action=WorkerCommandAction.SUBMIT,
            lease_id=lease.lease_id,
        )

        cancel_result = worker.run_once()
        submit_result = store.get_command(submit.id)
    finally:
        worker.close()

    assert cancel_result.id == cancel.id
    assert cancel_result.status is WorkerCommandStatus.UNCERTAIN
    assert submit_result.status is WorkerCommandStatus.FAILED
    assert submit_result.failure_code == "worker_needs_attention"
    assert not [call for call in service.calls if call[0] == "submit"]


def test_durable_broker_outcome_controls_command_status(tmp_path):
    worker, store, service = _runtime(tmp_path)
    worker.start()
    try:
        lease = _grant_submit(worker, store)
        service.submit = lambda _execution_id: SimpleNamespace(
            state=ExecutionState.BLOCKED,
            broker_state={},
            needs_attention=True,
        )
        blocked = _enqueue_execution(
            store,
            action=WorkerCommandAction.SUBMIT,
            lease_id=lease.lease_id,
        )
        blocked_result = worker.run_once()
        assert blocked_result.id == blocked.id
        assert blocked_result.status is WorkerCommandStatus.FAILED
        assert (
            blocked_result.failure_code
            == "submit_result_needs_attention"
        )
        assert (
            store.get_heartbeat(worker.worker_id).state
            is WorkerState.NEEDS_ATTENTION
        )

        service.cancel_entry = lambda _execution_id: SimpleNamespace(
            state=ExecutionState.UNKNOWN,
            broker_state={"write_unknown": "cancel_entry"},
            needs_attention=True,
        )
        uncertain = _enqueue_execution(
            store,
            action=WorkerCommandAction.CANCEL_ENTRY,
        )
        uncertain_result = worker.run_once()
        assert uncertain_result.id == uncertain.id
        assert uncertain_result.status is WorkerCommandStatus.UNCERTAIN
        assert (
            uncertain_result.failure_code
            == "cancel_entry_result_uncertain"
        )
    finally:
        worker.close()


def test_active_unknown_keeps_worker_alive_but_blocks_new_risk(tmp_path):
    worker, store, service = _runtime(tmp_path)
    record = service.store.get("execution-one")
    service.store.save(
        record.model_copy(
            update={
                "state": ExecutionState.UNKNOWN,
                "needs_attention": True,
                "last_error": "保护单结果不明",
            }
        ),
        event_kind="test_unknown",
    )

    worker.start()
    try:
        heartbeat = store.get_heartbeat(worker.worker_id)
        assert heartbeat.state.value == "needs_attention"
        assert heartbeat.last_error_code == (
            "_ReconciliationNeedsAttention"
        )
    finally:
        worker.close()


def test_heartbeat_survives_blocked_reconcile_without_false_success(tmp_path):
    worker, store, service = _runtime(
        tmp_path,
        heartbeat_interval_seconds=0.02,
    )
    worker.start()
    startup_success = store.get_heartbeat(
        worker.worker_id
    ).last_successful_reconcile_at
    service.reconcile_entered.clear()
    service.reconcile_release = threading.Event()
    command, _ = store.enqueue(
        action=WorkerCommandAction.RECONCILE,
        requester="system",
        broker="okx",
        environment="demo",
        account="okx",
    )
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    try:
        assert service.reconcile_entered.wait(2)
        first = store.get_heartbeat(worker.worker_id)
        time.sleep(0.08)
        second = store.get_heartbeat(worker.worker_id)
        assert second.last_seen_at > first.last_seen_at
        assert second.last_successful_reconcile_at == startup_success
        service.reconcile_release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        finished = store.get_command(command.id)
        assert finished.status is WorkerCommandStatus.SUCCEEDED
        final = store.get_heartbeat(worker.worker_id)
        assert final.last_successful_reconcile_at >= startup_success
    finally:
        service.reconcile_release.set()
        thread.join(timeout=2)
        worker.close()


def test_settings_change_reloads_and_revokes_worker_owned_new_risk(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("old", encoding="utf-8")
    initial = SimpleNamespace(
        execution=SimpleNamespace(poll_interval_seconds=1.0)
    )
    loaded = SimpleNamespace(
        execution=SimpleNamespace(poll_interval_seconds=2.0)
    )
    worker, store, service = _runtime(
        tmp_path,
        settings=initial,
        settings_path=settings_path,
        settings_loader=lambda _path: loaded,
    )
    worker.start()
    try:
        lease = _grant_submit(worker, store)
        assert (
            store.current_new_risk_lease().lease_id
            == lease.lease_id
        )
        command = _enqueue_execution(
            store,
            action=WorkerCommandAction.SUBMIT,
            lease_id=lease.lease_id,
        )
        settings_path.write_text("new-settings", encoding="utf-8")
        assert worker.run_once() is None
        result = store.get_command(command.id)
        assert result.status is WorkerCommandStatus.FAILED
        assert result.failure_code == "settings_changed"
        assert store.current_new_risk_lease() is None
    finally:
        worker.close()

    assert service.reloads == [(loaded, False)]


def test_close_revokes_worker_owned_new_risk_lease(tmp_path):
    worker, store, _service = _runtime(tmp_path)
    worker.start()
    lease = _grant_submit(worker, store)
    command = _enqueue_execution(
        store,
        action=WorkerCommandAction.SUBMIT,
        lease_id=lease.lease_id,
    )

    worker.close()

    assert store.current_new_risk_lease() is None
    stopped = store.get_command(command.id)
    assert stopped.status is WorkerCommandStatus.FAILED
    assert stopped.failure_code == "worker_stopped"
