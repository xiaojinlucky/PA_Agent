from __future__ import annotations

import multiprocessing
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from filelock import FileLock
from pydantic import ValidationError

from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker_protocol import (
    SetLeverageParameters,
    SetLeverageResolutionEvidence,
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


def _enqueue_new_risk_in_process(
    path: str,
    execution_id: str,
    lease_id: str,
    command_id: str,
    start_event,
    ready_queue,
    result_queue,
) -> None:
    try:
        store = WorkerStore(Path(path))
        ready_queue.put(execution_id)
        if not start_event.wait(_PROCESS_TIMEOUT_SECONDS):
            raise TimeoutError("enqueue start timeout")
        command, created = store.enqueue(
            action=WorkerCommandAction.SUBMIT,
            execution_id=execution_id,
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="paper-account",
            new_risk_lease_id=lease_id,
            command_id=command_id,
        )
        result_queue.put((execution_id, command.id, created, "", ""))
    except BaseException as exc:
        result_queue.put(
            (execution_id, None, False, type(exc).__name__, str(exc))
        )


def _enqueue_new_risk_then_exit(
    path: str,
    lease_id: str,
    command_id: str,
) -> None:
    store = WorkerStore(Path(path))
    store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-crashed-caller",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease_id,
        command_id=command_id,
    )
    os._exit(0)


def _create_v4_worker_database(
    path: Path,
    *,
    command_count: int,
    command_requester: str = "gui-session",
) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE worker_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO worker_meta(key, value)
            VALUES ('worker_schema_version', '4');
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
                parameters_json TEXT NOT NULL,
                status TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                result_code TEXT NOT NULL,
                failure_code TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE TABLE worker_new_risk_lease (
                slot TEXT PRIMARY KEY CHECK(slot='NEW_RISK'),
                lease_id TEXT NOT NULL UNIQUE,
                worker_id TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                requester TEXT NOT NULL,
                broker TEXT NOT NULL,
                environment TEXT NOT NULL,
                account TEXT NOT NULL,
                granted_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            INSERT INTO worker_new_risk_lease(
                slot, lease_id, worker_id, config_fingerprint, requester,
                broker, environment, account, granted_at, expires_at
            ) VALUES (
                'NEW_RISK', 'legacy-lease', 'worker-one',
                'route-fingerprint', 'gui-session',
                'okx', 'demo', 'paper-account',
                '2026-07-29T00:00:00+00:00',
                '2030-07-29T00:00:00+00:00'
            );
            """
        )
        rows = [
            (
                f"legacy-command-{index}",
                f"execution:legacy-execution-{index}",
                "submit",
                f"legacy-execution-{index}",
                command_requester,
                "okx",
                "demo",
                "paper-account",
                "legacy-lease",
                "",
                "null",
                "failed",
                "worker-one",
                f"2026-07-29T00:00:0{index}+00:00",
                None,
                f"2026-07-29T00:00:1{index}+00:00",
                "",
                "legacy_failure",
                "null",
            )
            for index in range(command_count)
        ]
        connection.executemany(
            """
            INSERT INTO worker_commands(
                id, scope_key, action, execution_id, requester,
                broker, environment, account, new_risk_lease_id,
                reason_code, parameters_json, status, worker_id,
                created_at, started_at, finished_at, result_code,
                failure_code, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return tuple(
            connection.execute(
                "SELECT * FROM worker_commands ORDER BY created_at, id"
            )
        )


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


def _leverage_parameters(
    *,
    digest: str = "a" * 64,
    current: str = "1",
    target: str = "2",
    required: str = "2",
) -> SetLeverageParameters:
    return SetLeverageParameters(
        analysis_digest=digest,
        analysis_record_path="records/pending/analysis.json",
        config_fingerprint="route-fingerprint",
        instrument="XAU-USDT-SWAP",
        direction="long",
        margin_mode="cross",
        position_mode="net_mode",
        current_leverage=current,
        target_leverage=target,
        current_capacity=current,
        target_capacity=required,
        maximum_leverage=target,
        maximum_capacity=required,
        planning_method="bounded_sequential_policy_grid_v1",
        policy_grid_step=str(Decimal(target) - Decimal(current)),
        verified_grid=(
            {"leverage": current, "capacity": current},
            {"leverage": target, "capacity": required},
        ),
        required_quantity=required,
        entry_price="4000",
        expected_account_identity="b" * 64,
        okx_api_base_url="https://www.okx.com",
        supervisor_record_id="supervisor-record",
        supervisor_record_path="records/supervisor/decision.json",
        supervisor_record_digest="d" * 64,
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
    assert worker_version == ("5",)
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


def test_enqueue_deduplicates_active_but_terminal_command_needs_new_lease(
    tmp_path,
):
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
    assert first_created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert failed.status is WorkerCommandStatus.FAILED
    with pytest.raises(PermissionError, match="已经绑定"):
        store.enqueue(
            action=WorkerCommandAction.SUBMIT,
            execution_id="execution-one",
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="paper-account",
            new_risk_lease_id=lease.lease_id,
        )

    assert store.revoke_new_risk_lease(lease.lease_id) is True
    replacement_lease = _grant_submit_lease(store)
    retried, retried_created = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-one",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=replacement_lease.lease_id,
    )
    assert retried_created is True
    assert retried.id != first.id


@pytest.mark.parametrize(
    ("first_reason", "second_reason"),
    [
        ("", "recover_transient_risk_read_failure"),
        ("recover_transient_risk_read_failure", ""),
    ],
)
def test_clear_and_transient_recovery_never_silently_deduplicate(
    tmp_path,
    first_reason,
    second_reason,
):
    store = WorkerStore(tmp_path / "worker.sqlite3")
    store.enqueue(
        action=WorkerCommandAction.CLEAR_DRAWDOWN_STOP,
        requester="controller",
        broker="okx",
        environment="demo",
        account="okx",
        reason_code=first_reason,
    )

    with pytest.raises(
        RuntimeError,
        match="不同类型的风险停止处置命令",
    ):
        store.enqueue(
            action=WorkerCommandAction.CLEAR_DRAWDOWN_STOP,
            requester="controller",
            broker="okx",
            environment="demo",
            account="okx",
            reason_code=second_reason,
        )


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


def test_one_lease_allows_only_one_new_risk_command_across_two_threads(
    tmp_path,
):
    path = tmp_path / "worker.sqlite3"
    seed = WorkerStore(path)
    lease = _grant_submit_lease(seed)
    barrier = threading.Barrier(3)
    results: list[tuple[str, str, str]] = []
    results_lock = threading.Lock()

    def enqueue(execution_id: str) -> None:
        store = WorkerStore(path)
        barrier.wait()
        try:
            command, _created = store.enqueue(
                action=WorkerCommandAction.SUBMIT,
                execution_id=execution_id,
                requester="gui-session",
                broker="okx",
                environment="demo",
                account="paper-account",
                new_risk_lease_id=lease.lease_id,
                command_id=f"command-{execution_id}",
            )
            result = (execution_id, "created", command.id)
        except BaseException as exc:
            result = (execution_id, type(exc).__name__, str(exc))
        with results_lock:
            results.append(result)

    threads = [
        threading.Thread(target=enqueue, args=(execution_id,))
        for execution_id in ("execution-thread-a", "execution-thread-b")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert [kind for _, kind, _ in results].count("created") == 1
    assert [kind for _, kind, _ in results].count("PermissionError") == 1
    commands = [
        command
        for command in seed.list_commands()
        if command.action in {
            WorkerCommandAction.SUBMIT,
            WorkerCommandAction.SET_LEVERAGE,
        }
    ]
    assert len(commands) == 1
    assert seed.current_new_risk_lease().command_id == commands[0].id


def test_one_lease_allows_only_one_new_risk_command_across_processes(
    tmp_path,
):
    path = tmp_path / "worker.sqlite3"
    seed = WorkerStore(path)
    lease = _grant_submit_lease(seed)
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    ready_queue = context.Queue()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_enqueue_new_risk_in_process,
            args=(
                str(path),
                execution_id,
                lease.lease_id,
                f"command-{execution_id}",
                start_event,
                ready_queue,
                result_queue,
            ),
        )
        for execution_id in ("execution-process-a", "execution-process-b")
    ]
    for process in processes:
        process.start()
    try:
        assert {
            ready_queue.get(timeout=_PROCESS_TIMEOUT_SECONDS)
            for _ in processes
        } == {"execution-process-a", "execution-process-b"}
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
    assert [result[2] for result in results].count(True) == 1
    assert [result[3] for result in results].count("PermissionError") == 1
    commands = [
        command
        for command in seed.list_commands()
        if command.action in {
            WorkerCommandAction.SUBMIT,
            WorkerCommandAction.SET_LEVERAGE,
        }
    ]
    assert len(commands) == 1
    assert seed.current_new_risk_lease().command_id == commands[0].id


def test_committed_binding_survives_caller_process_exit(tmp_path):
    path = tmp_path / "worker.sqlite3"
    store = WorkerStore(path)
    lease = _grant_submit_lease(store)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_enqueue_new_risk_then_exit,
        args=(str(path), lease.lease_id, "crash-command"),
    )
    process.start()
    process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)

    assert process.exitcode == 0
    command = store.get_command("crash-command")
    assert command is not None
    assert store.current_new_risk_lease().command_id == command.id
    with pytest.raises(PermissionError, match="已经绑定"):
        store.enqueue(
            action=WorkerCommandAction.SUBMIT,
            execution_id="execution-after-crash",
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="paper-account",
            new_risk_lease_id=lease.lease_id,
        )
    assert len(store.list_commands()) == 1


def test_command_insert_failure_rolls_back_lease_binding(tmp_path):
    path = tmp_path / "worker.sqlite3"
    store = WorkerStore(path)
    store.enqueue(
        action=WorkerCommandAction.RECONCILE,
        requester="system",
        broker="okx",
        environment="demo",
        account="paper-account",
        command_id="duplicate-command-id",
    )
    lease = _grant_submit_lease(store)

    with pytest.raises(sqlite3.IntegrityError):
        store.enqueue(
            action=WorkerCommandAction.SUBMIT,
            execution_id="execution-insert-fails",
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="paper-account",
            new_risk_lease_id=lease.lease_id,
            command_id="duplicate-command-id",
        )

    assert store.current_new_risk_lease().command_id == ""
    command, created = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-after-rollback",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
        command_id="new-risk-after-rollback",
    )
    assert created is True
    assert store.current_new_risk_lease().command_id == command.id


def test_database_unique_index_blocks_duplicate_nonempty_lease_only(tmp_path):
    path = tmp_path / "worker.sqlite3"
    store = WorkerStore(path)
    lease = _grant_submit_lease(store)
    command, _ = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-index-owner",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
    )

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO worker_commands(
                    id, scope_key, action, execution_id, requester,
                    broker, environment, account, new_risk_lease_id,
                    reason_code, parameters_json, status, worker_id,
                    created_at, started_at, finished_at, result_code,
                    failure_code, result_json
                )
                SELECT
                    'forged-second-consumer',
                    'execution:forged-second-consumer',
                    'submit',
                    'forged-second-consumer',
                    requester, broker, environment, account,
                    new_risk_lease_id, reason_code, parameters_json,
                    'failed', worker_id, created_at, started_at, finished_at,
                    result_code, failure_code, result_json
                FROM worker_commands
                WHERE id=?
                """,
                (command.id,),
            )
        connection.rollback()
        for index, action in enumerate(("cancel_entry", "request_exit")):
            connection.execute(
                """
                INSERT INTO worker_commands(
                    id, scope_key, action, execution_id, requester,
                    broker, environment, account, new_risk_lease_id,
                    reason_code, parameters_json, status, worker_id,
                    created_at, started_at, finished_at, result_code,
                    failure_code, result_json
                )
                SELECT
                    ?, ?, ?, ?, requester, broker, environment, account,
                    '', ?, 'null', 'failed', worker_id, created_at,
                    started_at, finished_at, result_code, failure_code, 'null'
                FROM worker_commands
                WHERE id=?
                """,
                (
                    f"de-risk-{index}",
                    f"execution:de-risk-{index}",
                    action,
                    f"de-risk-{index}",
                    "manual" if action == "request_exit" else "",
                    command.id,
                ),
            )

    assert len(store.list_commands()) == 3


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


def test_resolved_uncertain_leverage_allows_new_distinct_command(tmp_path):
    store = WorkerStore(tmp_path / "worker.sqlite3")
    lease = _grant_submit_lease(store)
    parameters = _leverage_parameters()
    command, _ = store.enqueue(
        action=WorkerCommandAction.SET_LEVERAGE,
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
        parameters=parameters,
    )
    assert store.claim_next(worker_id="worker-one").id == command.id
    store.recover_inflight(failure_code="BrokerTransportError")
    store.revoke_new_risk_lease(lease.lease_id)
    store.resolve_uncertain_command(
        command.id,
        resolution_code="confirmed_applied_by_leverage_readback",
        evidence=SetLeverageResolutionEvidence(
            analysis_digest=parameters.analysis_digest,
            command_action="set_leverage",
            command_failure_code="BrokerTransportError",
            broker="okx",
            environment="demo",
            account="paper-account",
            instrument=parameters.instrument,
            target_leverage=parameters.target_leverage,
            confirmed_leverage=parameters.target_leverage,
            required_quantity=parameters.required_quantity,
            confirmed_max_size="10",
            active_execution_count=0,
            new_risk_lease_present=False,
            broker_position_count=0,
            broker_pending_order_count=0,
            broker_pending_algo_order_count=0,
            broker_account_identity_digest="b" * 64,
            observed_at=datetime.now(UTC),
        ),
        resolved_by="worker:replacement",
    )
    replacement_lease = _grant_submit_lease(store)
    replacement_parameters = _leverage_parameters(
        digest="c" * 64,
        current="2",
        target="3",
        required="4",
    )

    replacement, created = store.enqueue(
        action=WorkerCommandAction.SET_LEVERAGE,
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=replacement_lease.lease_id,
        parameters=replacement_parameters,
    )

    assert created is True
    assert replacement.id != command.id
    assert replacement.parameters == replacement_parameters


def test_active_leverage_command_rejects_different_parameters(tmp_path):
    store = WorkerStore(tmp_path / "worker.sqlite3")
    lease = _grant_submit_lease(store)
    first, _ = store.enqueue(
        action=WorkerCommandAction.SET_LEVERAGE,
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
        parameters=_leverage_parameters(),
    )

    with pytest.raises(RuntimeError, match="不同参数"):
        store.enqueue(
            action=WorkerCommandAction.SET_LEVERAGE,
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="paper-account",
            new_risk_lease_id=lease.lease_id,
            parameters=_leverage_parameters(
                digest="c" * 64,
                target="3",
                required="4",
            ),
        )

    assert store.get_command(first.id).parameters == _leverage_parameters()


def test_set_leverage_consumes_lease_and_submit_needs_a_new_lease(tmp_path):
    store = WorkerStore(tmp_path / "worker.sqlite3")
    lease = _grant_submit_lease(store)
    leverage, created = store.enqueue(
        action=WorkerCommandAction.SET_LEVERAGE,
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
        parameters=_leverage_parameters(),
    )

    assert created is True
    assert store.current_new_risk_lease().command_id == leverage.id
    with pytest.raises(PermissionError, match="已经绑定"):
        store.enqueue(
            action=WorkerCommandAction.SUBMIT,
            execution_id="execution-after-leverage",
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="paper-account",
            new_risk_lease_id=lease.lease_id,
        )
    assert len(store.list_commands()) == 1

    assert store.revoke_new_risk_lease(lease.lease_id) is True
    replacement = _grant_submit_lease(store)
    submit, submit_created = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-after-leverage",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=replacement.lease_id,
    )
    assert submit_created is True
    assert submit.id != leverage.id
    assert store.current_new_risk_lease().command_id == submit.id


def test_v4_to_v5_migration_binds_the_only_existing_consumer(tmp_path):
    path = tmp_path / "worker.sqlite3"
    before_rows = _create_v4_worker_database(path, command_count=1)
    store = WorkerStore(path)

    assert store.schema_version == 4
    with FileLock(str(tmp_path / "worker.lock")) as worker_lock:
        store.migrate_to_current(worker_lock=worker_lock)

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM worker_meta "
            "WHERE key='worker_schema_version'"
        ).fetchone()
        after_rows = tuple(
            connection.execute(
                "SELECT * FROM worker_commands ORDER BY created_at, id"
            )
        )
        lease_command_id = connection.execute(
            "SELECT command_id FROM worker_new_risk_lease "
            "WHERE lease_id='legacy-lease'"
        ).fetchone()
        unique_index = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' "
            "AND name='idx_worker_commands_one_new_risk_per_lease'"
        ).fetchone()

    assert store.schema_version == 5
    assert version == ("5",)
    assert after_rows == before_rows
    assert lease_command_id == ("legacy-command-0",)
    assert unique_index is not None
    assert "WHERE new_risk_lease_id<>''" in unique_index[0]


def test_v4_to_v5_duplicate_consumers_fail_closed_without_rewriting_rows(
    tmp_path,
):
    path = tmp_path / "worker.sqlite3"
    before_rows = _create_v4_worker_database(path, command_count=2)
    store = WorkerStore(path)

    with (
        FileLock(str(tmp_path / "worker.lock")) as worker_lock,
        pytest.raises(RuntimeError, match="同一 NEW_RISK 租约"),
    ):
        store.migrate_to_current(worker_lock=worker_lock)

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM worker_meta "
            "WHERE key='worker_schema_version'"
        ).fetchone()
        after_rows = tuple(
            connection.execute(
                "SELECT * FROM worker_commands ORDER BY created_at, id"
            )
        )
        lease_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(worker_new_risk_lease)"
            )
        }

    assert store.schema_version == 4
    assert version == ("4",)
    assert after_rows == before_rows
    assert "command_id" not in lease_columns


def test_v4_to_v5_identity_mismatch_fails_closed_without_rewriting_rows(
    tmp_path,
):
    path = tmp_path / "worker.sqlite3"
    before_rows = _create_v4_worker_database(
        path,
        command_count=1,
        command_requester="different-requester",
    )
    store = WorkerStore(path)

    with (
        FileLock(str(tmp_path / "worker.lock")) as worker_lock,
        pytest.raises(RuntimeError, match="身份不一致"),
    ):
        store.migrate_to_current(worker_lock=worker_lock)

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM worker_meta "
            "WHERE key='worker_schema_version'"
        ).fetchone()
        after_rows = tuple(
            connection.execute(
                "SELECT * FROM worker_commands ORDER BY created_at, id"
            )
        )
        lease_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(worker_new_risk_lease)"
            )
        }
        unique_index = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' "
            "AND name='idx_worker_commands_one_new_risk_per_lease'"
        ).fetchone()

    assert store.schema_version == 4
    assert version == ("4",)
    assert after_rows == before_rows
    assert "command_id" not in lease_columns
    assert unique_index is None


def test_worker_schema_v1_waits_for_explicit_locked_migration(
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
        deferred_version = connection.execute(
            "SELECT value FROM worker_meta WHERE key='worker_schema_version'"
        ).fetchone()
        deferred_resolution_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='worker_command_resolutions'
            """
        ).fetchone()

    assert store.schema_version == 1
    assert deferred_version == ("1",)
    assert deferred_resolution_table is None

    with pytest.raises(RuntimeError, match="单例锁未持有"):
        store.migrate_to_current(
            worker_lock=FileLock(str(tmp_path / "unheld-worker.lock"))
        )
    with FileLock(str(tmp_path / "worker.lock")) as worker_lock:
        store.migrate_to_current(worker_lock=worker_lock)
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

    assert store.schema_version == 5
    assert version == ("5",)
    assert resolution_table == ("worker_command_resolutions",)
    assert store.list_commands() == []


def test_existing_worker_tables_without_version_are_not_adopted(tmp_path):
    path = tmp_path / "worker.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE worker_heartbeats(worker_id TEXT PRIMARY KEY)"
        )

    with pytest.raises(RuntimeError, match="缺少版本号"):
        WorkerStore(path)

    with sqlite3.connect(path) as connection:
        meta_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='worker_meta'
            """
        ).fetchone()
    assert meta_table is None


def test_worker_schema_v1_migration_preserves_all_unresolved_without_claim_or_replay(
    tmp_path,
):
    path = tmp_path / "worker.sqlite3"
    legacy_rows = (
        (
            "old-submit",
            "execution:old-submit-execution",
            "submit",
            "old-submit-execution",
            "campaign",
            "okx",
            "demo",
            "okx",
            "old-submit-lease",
            "",
            "uncertain",
            "old-worker",
            "2026-07-24T00:00:00+00:00",
            "2026-07-24T00:00:01+00:00",
            "2026-07-24T00:00:02+00:00",
            "",
            "BrokerTransportError",
        ),
        (
            "old-cancel",
            "execution:old-cancel-execution",
            "cancel_entry",
            "old-cancel-execution",
            "campaign",
            "okx",
            "demo",
            "okx",
            "",
            "",
            "uncertain",
            "old-worker",
            "2026-07-24T00:00:03+00:00",
            "2026-07-24T00:00:04+00:00",
            "2026-07-24T00:00:05+00:00",
            "",
            "BrokerTransportError",
        ),
        (
            "old-exit",
            "execution:old-exit-execution",
            "request_exit",
            "old-exit-execution",
            "campaign",
            "okx",
            "demo",
            "okx",
            "",
            "risk_exit",
            "uncertain",
            "old-worker",
            "2026-07-24T00:00:06+00:00",
            "2026-07-24T00:00:07+00:00",
            "2026-07-24T00:00:08+00:00",
            "",
            "BrokerTransportError",
        ),
    )
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
            """
        )
        connection.executemany(
            """
            INSERT INTO worker_commands(
                id, scope_key, action, execution_id, requester,
                broker, environment, account, new_risk_lease_id,
                reason_code, status, worker_id, created_at, started_at,
                finished_at, result_code, failure_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            legacy_rows,
        )
        before_rows = tuple(
            connection.execute(
                """
                SELECT
                    id, scope_key, action, execution_id, requester,
                    broker, environment, account, new_risk_lease_id,
                    reason_code, status, worker_id, created_at, started_at,
                    finished_at, result_code, failure_code
                FROM worker_commands
                ORDER BY created_at, id
                """
            )
        )

    store = WorkerStore(path)
    assert store.schema_version == 1
    with FileLock(str(tmp_path / "worker.lock")) as worker_lock:
        store.migrate_to_current(worker_lock=worker_lock)

    with sqlite3.connect(path) as connection:
        after_rows = tuple(
            connection.execute(
                """
                SELECT
                    id, scope_key, action, execution_id, requester,
                    broker, environment, account, new_risk_lease_id,
                    reason_code, status, worker_id, created_at, started_at,
                    finished_at, result_code, failure_code
                FROM worker_commands
                ORDER BY created_at, id
                """
            )
        )
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(worker_commands)"
            ).fetchall()
        }
        version = connection.execute(
            "SELECT value FROM worker_meta "
            "WHERE key='worker_schema_version'"
        ).fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        resolution_count = connection.execute(
            "SELECT COUNT(*) FROM worker_command_resolutions"
        ).fetchone()

    unresolved = store.list_unresolved_write_commands(
        broker="okx",
        environment="demo",
        account="okx",
    )

    assert after_rows == before_rows
    assert [command.id for command in unresolved] == [
        "old-submit",
        "old-cancel",
        "old-exit",
    ]
    assert all(
        command.status is WorkerCommandStatus.UNCERTAIN
        and command.parameters is None
        and command.result is None
        for command in unresolved
    )
    assert {"parameters_json", "result_json"} <= columns
    assert version == ("5",)
    assert integrity == ("ok",)
    assert resolution_count == (0,)


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


def test_bound_lease_authorizes_and_renews_only_its_unique_command(tmp_path):
    clock = _MutableClock(datetime(2026, 7, 20, 1, 0, tzinfo=UTC))
    store = WorkerStore(tmp_path / "worker.sqlite3", clock=clock)
    lease = _grant_submit_lease(store)
    command, _ = store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id="execution-bound",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
        new_risk_lease_id=lease.lease_id,
    )

    common = {
        "worker_id": "worker-one",
        "config_fingerprint": "route-fingerprint",
        "requester": "gui-session",
        "broker": "okx",
        "environment": "demo",
        "account": "paper-account",
    }
    assert not store.is_new_risk_authorized(lease.lease_id, **common)
    assert not store.is_new_risk_authorized(
        lease.lease_id,
        command_id="wrong-command",
        **common,
    )
    assert store.is_new_risk_authorized(
        lease.lease_id,
        command_id=command.id,
        **common,
    )
    assert (
        store.renew_new_risk_lease(
            lease.lease_id,
            worker_id="worker-one",
            config_fingerprint="route-fingerprint",
            requester="gui-session",
            command_id="wrong-command",
            ttl_seconds=90,
        )
        is None
    )
    renewed = store.renew_new_risk_lease(
        lease.lease_id,
        worker_id="worker-one",
        config_fingerprint="route-fingerprint",
        requester="gui-session",
        command_id=command.id,
        ttl_seconds=90,
    )
    assert renewed is not None
    assert renewed.command_id == command.id


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


def test_lease_is_not_authorized_at_the_exact_expiry_boundary(tmp_path):
    clock = _MutableClock(datetime(2026, 7, 20, 1, 0, tzinfo=UTC))
    store = WorkerStore(tmp_path / "worker.sqlite3", clock=clock)
    lease = _grant_submit_lease(store)

    clock.advance(seconds=60)

    assert store.current_new_risk_lease() is None
    assert not store.is_new_risk_authorized(
        lease.lease_id,
        worker_id="worker-one",
        config_fingerprint="route-fingerprint",
        requester="gui-session",
        broker="okx",
        environment="demo",
        account="paper-account",
    )
    with pytest.raises(PermissionError, match="NEW_RISK"):
        store.enqueue(
            action=WorkerCommandAction.SUBMIT,
            execution_id="execution-at-expiry",
            requester="gui-session",
            broker="okx",
            environment="demo",
            account="paper-account",
            new_risk_lease_id=lease.lease_id,
        )


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
