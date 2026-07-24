from __future__ import annotations

import multiprocessing
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker_protocol import (
    WorkerCommand,
    WorkerCommandAction,
    WorkerCommandResolutionEvidence,
    WorkerCommandStatus,
    WorkerState,
)
from pa_agent.execution.worker_store import WorkerStore

_PROCESS_TIMEOUT_SECONDS = 60


class _MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


def _claim_in_process(
    path: str,
    worker_id: str,
    start_event,
    ready_queue,
    result_queue,
) -> None:
    try:
        store = WorkerStore(path)
        ready_queue.put(worker_id)
        if not start_event.wait(_PROCESS_TIMEOUT_SECONDS):
            raise TimeoutError("claim start timeout")
        command = store.claim_next(worker_id=worker_id)
        result_queue.put(
            (
                worker_id,
                command.id if command is not None else None,
                "",
            )
        )
    except BaseException as exc:
        result_queue.put((worker_id, None, repr(exc)))


def _grant_submit_lease(store: WorkerStore):
    lease = store.grant_new_risk_lease(
        worker_id="worker-one",
        config_fingerprint="route-fingerprint",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        ttl_seconds=60,
    )
    assert lease is not None
    return lease


def _resolution_evidence(
    command: WorkerCommand,
) -> WorkerCommandResolutionEvidence:
    return WorkerCommandResolutionEvidence(
        execution_id=command.execution_id,
        command_action=command.action.value,
        command_failure_code=command.failure_code,
        broker=command.broker,
        environment=command.environment,
        account=command.account,
        instrument="XAU-USDT-SWAP",
        execution_state="canceled",
        broker_order_id_present=False,
        client_order_id_present=False,
        filled_quantity=Decimal("0"),
        event_kinds=("plan_created", "ready_expired"),
        active_execution_count=0,
        new_risk_lease_present=False,
        broker_position_count=0,
        broker_pending_order_count=0,
        broker_pending_algo_order_count=0,
        broker_account_identity_digest="e" * 64,
        observed_at=datetime(2026, 7, 24, 4, 0, tzinfo=UTC),
    )


def test_worker_schema_is_independent_and_protocol_rejects_extra_fields(tmp_path):
    path = tmp_path / "execution.sqlite3"
    ExecutionStore(path)
    WorkerStore(path)

    with sqlite3.connect(path) as connection:
        execution_version = connection.execute(
            """
            SELECT value FROM execution_meta WHERE key='schema_version'
            """
        ).fetchone()
        worker_version = connection.execute(
            """
            SELECT value FROM worker_meta WHERE key='worker_schema_version'
            """
        ).fetchone()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        synchronous = connection.execute("PRAGMA synchronous").fetchone()

    assert execution_version == ("2",)
    assert worker_version == ("2",)
    assert journal_mode == ("wal",)
    assert synchronous == (2,)
    with pytest.raises(ValidationError, match="Extra inputs"):
        WorkerCommand(
            id="command",
            action="submit",
            execution_id="execution",
            requester="gui",
            broker="okx",
            environment="demo",
            account="paper",
            new_risk_lease_id="lease",
            created_at=datetime.now(UTC),
            api_secret="must-not-be-stored",
        )


def test_enqueue_deduplicates_active_but_failed_command_can_retry(tmp_path):
    store = WorkerStore(tmp_path / "worker.sqlite3")
    lease = _grant_submit_lease(store)

    first, first_created = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-one",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
    )
    duplicate, duplicate_created = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-one",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
    )
    claimed = store.claim_next(worker_id="worker-one")
    assert claimed is not None
    failed = store.finish_command(
        claimed.id,
        worker_id="worker-one",
        status=WorkerCommandStatus.FAILED,
        failure_code="broker_rejected",
    )
    retried, retried_created = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-one",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
    )

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert failed.status is WorkerCommandStatus.FAILED
    assert retried_created is True
    assert retried.id != first.id


def test_claim_next_is_atomic_across_processes(tmp_path):
    path = tmp_path / "worker.sqlite3"
    seed = WorkerStore(path)
    command, _ = seed.enqueue(
        action=WorkerCommandAction.RECONCILE,
        requester="system",
        broker="okx",
        environment="demo",
        account="paper-account",
    )
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    ready_queue = context.Queue()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_claim_in_process,
            args=(
                str(path),
                worker_id,
                start_event,
                ready_queue,
                result_queue,
            ),
        )
        for worker_id in ("worker-one", "worker-two")
    ]
    for process in processes:
        process.start()
    try:
        assert {
            ready_queue.get(timeout=_PROCESS_TIMEOUT_SECONDS)
            for _ in processes
        } == {"worker-one", "worker-two"}
        start_event.set()
        results = [
            result_queue.get(timeout=_PROCESS_TIMEOUT_SECONDS)
            for _ in processes
        ]
    finally:
        for process in processes:
            process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert all(process.exitcode == 0 for process in processes)
    assert not [error for _, _, error in results if error]
    assert [command_id for _, command_id, _ in results].count(command.id) == 1
    assert [command_id for _, command_id, _ in results].count(None) == 1
    stored = seed.get_command(command.id)
    assert stored is not None
    assert stored.status is WorkerCommandStatus.RUNNING


def test_recover_inflight_marks_uncertain_and_never_replays(tmp_path):
    store = WorkerStore(tmp_path / "worker.sqlite3")
    first, _ = store.enqueue(
        action=WorkerCommandAction.RECONCILE,
        requester="system",
        broker="okx",
        environment="demo",
        account="paper-account",
        command_id="first",
    )
    second, _ = store.enqueue(
        action=WorkerCommandAction.REFRESH_ACCOUNT,
        requester="system",
        broker="okx",
        environment="demo",
        account="paper-account",
        command_id="second",
    )
    assert store.claim_next(worker_id="crashed-worker").id == first.id

    assert store.recover_inflight() == 1
    recovered = store.get_command(first.id)
    assert recovered is not None
    assert recovered.status is WorkerCommandStatus.UNCERTAIN
    next_command = store.claim_next(worker_id="replacement-worker")

    assert next_command is not None
    assert next_command.id == second.id
    assert store.claim_next(worker_id="replacement-worker") is None


def test_recover_inflight_marks_read_only_refresh_retryable(tmp_path):
    store = WorkerStore(tmp_path / "worker.sqlite3")
    command, _ = store.enqueue(
        action=WorkerCommandAction.REFRESH_ACCOUNT,
        requester="system",
        broker="okx",
        environment="demo",
        account="paper-account",
    )
    assert store.claim_next(worker_id="crashed-worker").id == command.id

    assert store.recover_inflight() == 1
    recovered = store.get_command(command.id)
    replacement, created = store.enqueue(
        action=WorkerCommandAction.REFRESH_ACCOUNT,
        requester="system",
        broker="okx",
        environment="demo",
        account="paper-account",
    )

    assert recovered.status is WorkerCommandStatus.FAILED
    assert recovered.failure_code == "worker_restarted_read_retryable"
    assert created is True
    assert replacement.id != command.id


def test_uncertain_command_blocks_same_action_until_explicit_resolution(tmp_path):
    store = WorkerStore(tmp_path / "worker.sqlite3")
    command, _ = store.enqueue(
        action=WorkerCommandAction.RECONCILE,
        requester="system",
        broker="okx",
        environment="demo",
        account="paper-account",
    )
    assert store.claim_next(worker_id="worker-one").id == command.id
    store.recover_inflight()

    with pytest.raises(RuntimeError, match="uncertain"):
        store.enqueue(
            action=WorkerCommandAction.RECONCILE,
            requester="system",
            broker="okx",
            environment="demo",
            account="paper-account",
        )


def test_uncertain_write_blocks_new_risk_until_durable_resolution(tmp_path):
    store = WorkerStore(tmp_path / "worker.sqlite3")
    lease = _grant_submit_lease(store)
    command, _ = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-schema-failed",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
    )
    assert store.claim_next(worker_id="worker-one").id == command.id
    store.recover_inflight(failure_code="ValidationError")
    assert store.revoke_new_risk_lease(lease.lease_id) is True

    assert store.list_unresolved_write_commands(
        broker="okx",
        environment="demo",
        account="paper-account",
    ) == [store.get_command(command.id)]
    assert (
        store.grant_new_risk_lease(
            worker_id="worker-one",
            config_fingerprint="route-fingerprint",
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="paper-account",
            ttl_seconds=60,
        )
        is None
    )

    resolution = store.resolve_uncertain_command(
        command.id,
        resolution_code="confirmed_not_written_schema_validation",
        evidence=_resolution_evidence(store.get_command(command.id)),
        resolved_by="operator-audit",
    )

    assert resolution.command_id == command.id
    assert store.get_command(command.id).status is WorkerCommandStatus.UNCERTAIN
    assert store.get_command_resolution(command.id) == resolution
    assert store.list_unresolved_write_commands(
        broker="okx",
        environment="demo",
        account="paper-account",
    ) == []
    assert (
        store.grant_new_risk_lease(
            worker_id="worker-one",
            config_fingerprint="route-fingerprint",
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="paper-account",
            ttl_seconds=60,
        )
        is not None
    )


def test_uncertain_resolution_is_idempotent_but_cannot_be_rewritten(tmp_path):
    store = WorkerStore(tmp_path / "worker.sqlite3")
    lease = _grant_submit_lease(store)
    command, _ = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-idempotent",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
    )
    assert store.claim_next(worker_id="worker-one").id == command.id
    store.recover_inflight(failure_code="ValidationError")
    recovered = store.get_command(command.id)
    kwargs = {
        "resolution_code": "confirmed_read_only",
        "evidence": _resolution_evidence(recovered),
        "resolved_by": "operator-audit",
    }

    first = store.resolve_uncertain_command(command.id, **kwargs)
    second = store.resolve_uncertain_command(command.id, **kwargs)

    assert second == first
    with pytest.raises(RuntimeError, match="不同"):
        store.resolve_uncertain_command(
            command.id,
            resolution_code="different_resolution",
            evidence=_resolution_evidence(recovered),
            resolved_by="operator-audit",
        )


def test_worker_schema_v1_upgrades_resolution_table_without_rewriting_commands(
    tmp_path,
):
    path = tmp_path / "worker.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE worker_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
            ("worker_schema_version", "1"),
        )

    store = WorkerStore(path)

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM worker_meta WHERE key='worker_schema_version'"
        ).fetchone()
        resolution_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='worker_command_resolutions'
            """
        ).fetchone()

    assert version == ("2",)
    assert resolution_table == ("worker_command_resolutions",)
    assert store.list_commands() == []


def test_worker_schema_v1_migration_preserves_existing_uncertain_write(
    tmp_path,
):
    path = tmp_path / "worker.sqlite3"
    with sqlite3.connect(path) as connection:
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
            INSERT INTO worker_commands(
                id, scope_key, action, execution_id, requester,
                broker, environment, account, new_risk_lease_id,
                reason_code, status, worker_id, created_at, started_at,
                finished_at, result_code, failure_code
            ) VALUES (
                'old-command', 'execution:old-execution', 'submit',
                'old-execution', 'campaign', 'okx', 'demo', 'okx',
                'old-lease', '', 'uncertain', 'old-worker',
                '2026-07-24T00:00:00+00:00',
                '2026-07-24T00:00:01+00:00',
                '2026-07-24T00:00:02+00:00',
                '', 'ValidationError'
            );
            """
        )

    store = WorkerStore(path)
    command = store.get_command("old-command")

    assert command is not None
    assert command.status is WorkerCommandStatus.UNCERTAIN
    assert command.parameters is None
    assert command.result is None
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(worker_commands)"
            ).fetchall()
        }
    assert {"parameters_json", "result_json"} <= columns


def test_new_risk_lease_route_expiry_renew_and_revoke(tmp_path):
    clock = _MutableClock(datetime(2026, 7, 20, 1, 0, tzinfo=UTC))
    store = WorkerStore(tmp_path / "worker.sqlite3", clock=clock)
    lease = _grant_submit_lease(store)

    assert store.is_new_risk_authorized(
        lease.lease_id,
        worker_id="worker-one",
        config_fingerprint="route-fingerprint",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
    )
    assert not store.is_new_risk_authorized(
        lease.lease_id,
        worker_id="worker-one",
        config_fingerprint="route-fingerprint",
        requester="gui-session",
        broker="okx",
        environment="live",
        account="paper-account",
    )
    assert not store.is_new_risk_authorized(
        lease.lease_id,
        worker_id="replacement-worker",
        config_fingerprint="route-fingerprint",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
    )
    assert not store.is_new_risk_authorized(
        lease.lease_id,
        worker_id="worker-one",
        config_fingerprint="changed-route",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
    )
    assert (
        store.grant_new_risk_lease(
            worker_id="worker-one",
            config_fingerprint="other-fingerprint",
            requester="other",
            broker="longbridge",
            environment="demo",
            account="paper",
            ttl_seconds=60,
        )
        is None
    )
    clock.advance(seconds=30)
    renewed = store.renew_new_risk_lease(
        lease.lease_id,
        worker_id="worker-one",
        config_fingerprint="route-fingerprint",
        requester="gui-session",
        ttl_seconds=90,
    )
    assert renewed is not None
    clock.advance(seconds=61)
    assert store.is_new_risk_authorized(
        lease.lease_id,
        worker_id="worker-one",
        config_fingerprint="route-fingerprint",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
    )

    pending, _ = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-revoked",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
    )
    assert store.revoke_new_risk_lease(lease.lease_id) is True
    revoked = store.get_command(pending.id)
    assert revoked is not None
    assert revoked.status is WorkerCommandStatus.FAILED
    assert revoked.failure_code == "new_risk_revoked"
    assert store.revoke_new_risk_lease(lease.lease_id) is False
    assert store.current_new_risk_lease() is None

    with pytest.raises(PermissionError, match="NEW_RISK"):
        store.enqueue(
            action=WorkerCommandAction.SUBMIT,
            execution_id="execution-no-lease",
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="paper-account",
            new_risk_lease_id=lease.lease_id,
        )


def test_expired_lease_fails_pending_submit_before_claim(tmp_path):
    clock = _MutableClock(datetime(2026, 7, 20, 1, 0, tzinfo=UTC))
    store = WorkerStore(tmp_path / "worker.sqlite3", clock=clock)
    lease = _grant_submit_lease(store)
    submit, _ = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-expired",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
    )
    refresh, _ = store.enqueue(
        action=WorkerCommandAction.REFRESH_ACCOUNT,
        requester="system",
        broker="okx",
        environment="demo",
        account="paper-account",
    )
    clock.advance(seconds=61)

    claimed = store.claim_next(worker_id="worker")

    expired = store.get_command(submit.id)
    assert expired is not None
    assert expired.status is WorkerCommandStatus.FAILED
    assert expired.failure_code == "new_risk_expired"
    assert claimed is not None
    assert claimed.id == refresh.id


def test_heartbeat_preserves_start_tracks_reconcile_and_detects_stale(tmp_path):
    clock = _MutableClock(datetime(2026, 7, 20, 1, 0, tzinfo=UTC))
    store = WorkerStore(tmp_path / "worker.sqlite3", clock=clock)
    starting = store.record_heartbeat(
        worker_id="worker-one",
        pid=1234,
        state=WorkerState.STARTING,
    )
    clock.advance(seconds=10)
    reconcile_at = clock.now
    running = store.record_heartbeat(
        worker_id="worker-one",
        pid=1234,
        state=WorkerState.RUNNING,
        last_successful_reconcile_at=reconcile_at,
    )

    assert running.started_at == starting.started_at
    assert running.last_seen_at == clock.now
    assert running.last_successful_reconcile_at == reconcile_at
    assert store.latest_heartbeat() == running
    assert store.is_heartbeat_stale(
        "worker-one",
        stale_after_seconds=30,
    ) is False
    clock.advance(seconds=30)
    assert store.is_heartbeat_stale(
        "worker-one",
        stale_after_seconds=30,
    ) is True
    assert store.is_reconcile_stale(
        "worker-one",
        stale_after_seconds=30,
    ) is True
    store.record_heartbeat(
        worker_id="worker-one",
        pid=1234,
        state=WorkerState.RUNNING,
    )
    assert store.is_heartbeat_stale(
        "worker-one",
        stale_after_seconds=30,
    ) is False
    assert store.is_reconcile_stale(
        "worker-one",
        stale_after_seconds=30,
    ) is True
    assert store.is_heartbeat_stale(
        "missing-worker",
        stale_after_seconds=30,
    ) is True
