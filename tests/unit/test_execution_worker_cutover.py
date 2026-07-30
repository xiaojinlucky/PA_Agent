from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from filelock import FileLock

import pa_agent.execution.database_fence as database_fence_module
import pa_agent.execution.worker_cutover as worker_cutover_module
from pa_agent.execution.database_fence import (
    DatabaseFenceError,
    DatabaseWriteFence,
)
from pa_agent.execution.models import (
    ExecutionPlan,
    ExecutionState,
    utc_now_iso,
)
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker import ExecutionWorker
from pa_agent.execution.worker_cutover import (
    SAFE_CUTOVER_CONFIRMATION,
    SAFE_CUTOVER_RECOVERY_CONFIRMATION,
    CutoverError,
    CutoverPaths,
    audit_safe_v4_to_v5_cutover,
    perform_safe_v4_to_v5_cutover,
    recover_safe_v4_to_v5_cutover,
    verify_cutover_archive,
)
from pa_agent.execution.worker_protocol import (
    WorkerCommandResolutionEvidence,
    WorkerState,
)
from pa_agent.execution.worker_store import WorkerStore
from pa_agent.risk.runtime import RiskRuntimeState

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_CUTOVER_RUNTIME_SOURCES = (
    "pa_agent/execution/worker_cutover.py",
    "pa_agent/execution/database_fence.py",
    "pa_agent/execution/store.py",
    "pa_agent/execution/worker_store.py",
    "pa_agent/main.py",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checkpoint(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert result is not None
        assert result[0] == 0
    finally:
        connection.close()


def _make_archive_writable(path: Path) -> None:
    if not path.exists():
        return
    if os.name == "nt":
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32con.TOKEN_QUERY,
        )
        current_user = win32security.GetTokenInformation(
            token,
            win32security.TokenUser,
        )[0]
        system = win32security.CreateWellKnownSid(
            win32security.WinLocalSystemSid,
            None,
        )
        administrators = win32security.CreateWellKnownSid(
            win32security.WinBuiltinAdministratorsSid,
            None,
        )
        for candidate in (path, *sorted(path.rglob("*"))):
            dacl = win32security.ACL()
            inheritance = (
                win32con.OBJECT_INHERIT_ACE
                | win32con.CONTAINER_INHERIT_ACE
                if candidate.is_dir()
                else 0
            )
            for sid in (current_user, system, administrators):
                dacl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION,
                    inheritance,
                    win32con.GENERIC_ALL,
                    sid,
                )
            win32security.SetNamedSecurityInfo(
                str(candidate),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
    for candidate in sorted(path.rglob("*"), reverse=True):
        if candidate.is_file():
            candidate.chmod(stat.S_IREAD | stat.S_IWRITE)


def _make_archive_readonly(path: Path) -> None:
    worker_cutover_module._seal_archive(path)


def _refresh_prepared_target_manifest(
    archive_directory: Path,
    prepared_database: Path,
) -> None:
    manifest_path = archive_directory / "prepared-target.json"
    manifest_path.chmod(stat.S_IREAD | stat.S_IWRITE)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["prepared_target_sha256"] = _sha256(prepared_database)
    payload["prepared_target_size"] = prepared_database.stat().st_size
    manifest_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _plan(execution_id: str, index: int) -> ExecutionPlan:
    return ExecutionPlan(
        id=execution_id,
        analysis_digest=f"{index:064x}",
        analysis_record_path="records/pending/test.json",
        broker="okx",
        environment="demo",
        product="swap",
        requested_account="paper",
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


def _committed_fixture_sources(project_root: Path) -> str:
    for relative_path in _CUTOVER_RUNTIME_SOURCES:
        source = _SOURCE_ROOT / relative_path
        target = project_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    subprocess.run(
        ["git", "init", str(project_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(project_root), "config", "core.autocrlf", "false"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(project_root), "add", "--", "pa_agent"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "-c",
            "user.name=PA Agent Test",
            "-c",
            "user.email=pa-agent-test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _install_fixture_protocol(
    control_db: Path,
    *,
    deployed_sha: str,
) -> None:
    fence = DatabaseWriteFence(control_db)
    automatic = fence.state()
    installed_at = database_fence_module._now_iso()
    installed = database_fence_module.DatabaseFenceState(
        generation=automatic.generation,
        active=False,
        operation_id="",
        updated_at=installed_at,
        protocol_sha=deployed_sha,
        protocol_installed_at=installed_at,
        protocol_instance_id=automatic.protocol_instance_id,
    )
    database_fence_module._write_state(fence.state_path, installed)
    database_fence_module._write_database_protocol_receipt(
        control_db,
        database_fence_module.DatabaseFenceProtocolReceipt(
            protocol_sha=installed.protocol_sha,
            protocol_installed_at=installed.protocol_installed_at,
            protocol_instance_id=installed.protocol_instance_id,
            minimum_generation=installed.generation,
        ),
    )
    assert fence.state() == installed


def _fixture_paths(tmp_path: Path) -> CutoverPaths:
    project_root = tmp_path / "project"
    records = project_root / "records"
    records.mkdir(parents=True)
    deployed_sha = _committed_fixture_sources(project_root)
    paths = CutoverPaths(
        project_root=project_root,
        control_db=records / "execution_control.sqlite3",
        execution_db=records / "execution.sqlite3",
        gui_lock=records / "execution_gui_writer.lock",
        worker_lock=records / "execution_worker.lock",
        campaign_lock=records / "okx_demo_campaign.lock",
        archive_root=project_root / "scratch" / "worker-control-archives",
        deployed_sha=deployed_sha,
        fence_protocol_sha=deployed_sha,
    )

    store = WorkerStore(paths.control_db, worker_lock_path=paths.worker_lock)
    now = datetime(2026, 7, 30, 2, 0, tzinfo=UTC)
    route_key = "okx:demo:paper"
    state = RiskRuntimeState(
        route_key=route_key,
        broker="okx",
        environment="demo",
        account="paper",
        account_identity="a" * 64,
        last_external_cashflow_bill_id="bill-external",
        last_account_bill_id="bill-account",
        last_account_bill_timestamp_ms=1_753_840_800_000,
        last_bill_scan_at=now,
        adjusted_high_water_usd=Decimal("1000"),
        last_total_equity_usd=Decimal("900"),
        drawdown_usd=Decimal("100"),
        drawdown_fraction=Decimal("0.1"),
        kill_active=True,
        kill_reason="risk_runtime_BrokerTransportError",
        kill_activated_at=now,
        updated_at=now,
    )
    baseline = {
        "kind": "v4_cutover_baseline",
        "route_key": route_key,
        "backfilled": True,
        "established_at": now.isoformat(),
    }
    evidence = {
        "kind": "risk_runtime_test_evidence",
        "route_key": route_key,
        "observed_at": now.isoformat(),
    }
    store.save_risk_runtime_state(
        state,
        baseline=baseline,
        evidence=evidence,
    )
    _install_fixture_protocol(
        paths.control_db,
        deployed_sha=deployed_sha,
    )

    execution_store = ExecutionStore(paths.execution_db)
    for index, execution_id in enumerate(
        (
            "execution-one",
            "execution-two",
            "execution-three",
            "execution-four",
            "execution-five",
            "execution-six",
            "execution-resolved",
        ),
        start=1,
    ):
        record, created = execution_store.create(_plan(execution_id, index))
        assert created is True
        execution_store.save(
            record.model_copy(update={"state": ExecutionState.CANCELED}),
            event_kind="ready_expired",
        )
    _checkpoint(paths.execution_db)

    with sqlite3.connect(paths.control_db) as connection:
        connection.executescript("""
            DROP INDEX idx_worker_commands_one_new_risk_per_lease;
            DROP TABLE worker_new_risk_lease;
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
            UPDATE worker_meta
            SET value='4'
            WHERE key='worker_schema_version';
            """)
        succeeded_commands = tuple(
            (
                f"command-{name}",
                f"execution:execution-{name}",
                "submit",
                f"execution-{name}",
                "campaign",
                "okx",
                "demo",
                "paper",
                (
                    "reused-lease-a"
                    if index <= 4
                    else "reused-lease-b"
                ),
                "",
                "null",
                "succeeded",
                "old-worker",
                f"2026-07-30T02:0{index - 1}:00+00:00",
                f"2026-07-30T02:0{index - 1}:01+00:00",
                f"2026-07-30T02:0{index - 1}:02+00:00",
                "submit_completed",
                "",
                "null",
            )
            for index, name in enumerate(
                ("one", "two", "three", "four", "five", "six"),
                start=1,
            )
        )
        command_rows = (
            *succeeded_commands,
            (
                "command-resolved",
                "execution:execution-resolved",
                "submit",
                "execution-resolved",
                "campaign",
                "okx",
                "demo",
                "paper",
                "resolved-lease",
                "",
                "null",
                "uncertain",
                "old-worker",
                "2026-07-30T02:06:00+00:00",
                "2026-07-30T02:06:01+00:00",
                "2026-07-30T02:06:02+00:00",
                "",
                "ValidationError",
                "null",
            ),
        )
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
            command_rows,
        )
        resolution_evidence = WorkerCommandResolutionEvidence(
            execution_id="execution-resolved",
            command_action="submit",
            command_failure_code="ValidationError",
            broker="okx",
            environment="demo",
            account="paper",
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
            broker_account_identity_digest="a" * 64,
            observed_at=datetime(2026, 7, 30, 2, 7, tzinfo=UTC),
        )
        resolution_payload = json.dumps(
            resolution_evidence.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO worker_command_resolutions(
                command_id, resolution_code, evidence_json,
                evidence_digest, resolved_by, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "command-resolved",
                "confirmed_not_written_schema_validation",
                resolution_payload,
                hashlib.sha256(resolution_payload.encode()).hexdigest(),
                "operator",
                "2026-07-30T02:07:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO worker_heartbeats(
                worker_id, pid, started_at, last_seen_at,
                last_successful_reconcile_at, state, last_error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-worker",
                100,
                "2026-07-30T01:00:00+00:00",
                "2026-07-30T02:04:00+00:00",
                "2026-07-30T02:04:00+00:00",
                "stopping",
                "",
            ),
        )
    connection.close()
    _checkpoint(paths.control_db)
    audit = audit_safe_v4_to_v5_cutover(paths)
    return replace(
        paths,
        expected_plan_sha256=audit.plan_sha256,
    )


def _mutate_control(paths: CutoverPaths, sql: str) -> None:
    connection = sqlite3.connect(paths.control_db)
    try:
        connection.execute(sql)
        connection.commit()
    finally:
        connection.close()
    _checkpoint(paths.control_db)


def _schema_version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM worker_meta WHERE key='worker_schema_version'"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def _active_cutover_id(paths: CutoverPaths) -> str:
    for name in (
        "execution_worker_cutover.intent.json",
        "execution_worker_cutover.completed.json",
    ):
        candidate = paths.control_db.parent / name
        if candidate.is_file():
            payload = json.loads(
                candidate.read_text(encoding="utf-8")
            )
            return str(payload["cutover_id"])
    raise AssertionError("切换意图或完成记录均不存在")


def _path_payload(paths: CutoverPaths) -> dict[str, str]:
    return {
        field_name: str(getattr(paths, field_name))
        for field_name in (
            "project_root",
            "control_db",
            "execution_db",
            "gui_lock",
            "worker_lock",
            "campaign_lock",
            "archive_root",
            "deployed_sha",
            "fence_protocol_sha",
            "expected_plan_sha256",
        )
    }


def _paths_from_payload(payload: dict[str, str]) -> CutoverPaths:
    return CutoverPaths(
        project_root=Path(payload["project_root"]),
        control_db=Path(payload["control_db"]),
        execution_db=Path(payload["execution_db"]),
        gui_lock=Path(payload["gui_lock"]),
        worker_lock=Path(payload["worker_lock"]),
        campaign_lock=Path(payload["campaign_lock"]),
        archive_root=Path(payload["archive_root"]),
        deployed_sha=payload["deployed_sha"],
        fence_protocol_sha=payload["fence_protocol_sha"],
        expected_plan_sha256=payload["expected_plan_sha256"],
    )


def _crash_cutover_process(
    payload: dict[str, str],
    phase: str,
) -> None:
    paths = _paths_from_payload(payload)

    def crash(current_phase: str, _context: object) -> None:
        if current_phase == phase:
            os._exit(91)

    perform_safe_v4_to_v5_cutover(
        paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
        _phase_hook=crash,
    )


def _attempt_late_database_writes(
    payload: dict[str, str],
    result_queue,
) -> None:
    paths = _paths_from_payload(payload)
    results: list[bool] = []
    try:
        WorkerStore(
            paths.control_db,
            worker_lock_path=paths.worker_lock,
        ).record_heartbeat(
            worker_id="late-worker",
            pid=321,
            state=WorkerState.STARTING,
        )
    except DatabaseFenceError:
        results.append(True)
    else:
        results.append(False)
    try:
        ExecutionStore(
            paths.execution_db,
            schema_mode="require_current",
        ).append_event(
            "execution-one",
            "late_write",
        )
    except DatabaseFenceError:
        results.append(True)
    else:
        results.append(False)
    result_queue.put(tuple(results))


class _StartupReadOnlyService:
    def __init__(self, store: ExecutionStore) -> None:
        self.store = store
        self.calls: list[tuple[object, ...]] = []

    def disarm(self, *, revoke_external: bool = True) -> None:
        self.calls.append(("disarm", revoke_external))

    def reconcile_once(self) -> list[object]:
        self.calls.append(("reconcile",))
        return []


def test_safe_cutover_archives_exact_v4_and_builds_empty_v5_queue(tmp_path):
    paths = _fixture_paths(tmp_path)
    connection = sqlite3.connect(paths.control_db)
    try:
        source_risk_rows = tuple(
            connection.execute("SELECT * FROM risk_runtime_state ORDER BY route_key")
        )
        source_risk_metadata = tuple(connection.execute("""
                SELECT key, value FROM worker_meta
                WHERE key LIKE 'risk_runtime_baseline:%'
                   OR key LIKE 'risk_runtime_evidence:%'
                ORDER BY key
                """))
    finally:
        connection.close()
    _checkpoint(paths.control_db)
    source_bytes = paths.control_db.read_bytes()
    source_hash = _sha256(paths.control_db)

    result = perform_safe_v4_to_v5_cutover(
        paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
    )
    try:
        archived_db = result.archive_directory / "source" / paths.control_db.name
        assert archived_db.read_bytes() == source_bytes
        assert result.source_database_sha256 == source_hash
        with pytest.raises(PermissionError), archived_db.open("ab"):
            pass
        assert verify_cutover_archive(result.archive_directory).valid is True
        assert archived_db.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        archived_connection = sqlite3.connect(
            f"{archived_db.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            assert archived_connection.execute(
                "SELECT value FROM worker_meta " "WHERE key='worker_schema_version'"
            ).fetchone() == ("4",)
            assert archived_connection.execute(
                "SELECT COUNT(*) FROM worker_commands"
            ).fetchone() == (7,)
            assert archived_connection.execute(
                "SELECT COUNT(*) FROM worker_command_resolutions"
            ).fetchone() == (1,)
        finally:
            archived_connection.close()

        assert _schema_version(paths.control_db) == 5
        with sqlite3.connect(paths.control_db) as connection:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "worker_commands",
                    "worker_command_resolutions",
                    "worker_new_risk_lease",
                    "worker_heartbeats",
                )
            }
            risk_count = connection.execute("SELECT COUNT(*) FROM risk_runtime_state").fetchone()[0]
            metadata_count = connection.execute("""
                SELECT COUNT(*) FROM worker_meta
                WHERE key LIKE 'risk_runtime_baseline:%'
                   OR key LIKE 'risk_runtime_evidence:%'
                """).fetchone()[0]
            receipt_count = connection.execute("""
                SELECT COUNT(*) FROM worker_meta
                WHERE key LIKE 'worker_safe_cutover:%'
                """).fetchone()[0]
            target_risk_rows = tuple(
                connection.execute("SELECT * FROM risk_runtime_state ORDER BY route_key")
            )
            target_risk_metadata = tuple(connection.execute("""
                    SELECT key, value FROM worker_meta
                    WHERE key LIKE 'risk_runtime_baseline:%'
                       OR key LIKE 'risk_runtime_evidence:%'
                    ORDER BY key
                    """))
        assert counts == {
            "worker_commands": 0,
            "worker_command_resolutions": 0,
            "worker_new_risk_lease": 0,
            "worker_heartbeats": 0,
        }
        assert risk_count == 1
        assert metadata_count == 2
        assert receipt_count == 1
        assert target_risk_rows == source_risk_rows
        assert target_risk_metadata == source_risk_metadata
        startup_store = WorkerStore(
            paths.control_db,
            worker_lock_path=paths.worker_lock,
        )
        with FileLock(str(paths.worker_lock)) as worker_lock:
            startup_store.migrate_to_current(worker_lock=worker_lock)
            assert startup_store.backfill_risk_runtime_baselines(worker_lock=worker_lock) == 0
        state = startup_store.get_risk_runtime_state("okx:demo:paper")
        assert state is not None
        assert state.kill_active is True
        assert state.kill_reason == "risk_runtime_BrokerTransportError"
    finally:
        _make_archive_writable(result.archive_directory)


def test_safe_cutover_audit_is_read_only_and_reports_required_exception(tmp_path):
    paths = _fixture_paths(tmp_path)
    before = paths.control_db.read_bytes()

    audit = audit_safe_v4_to_v5_cutover(paths)

    assert audit.source_schema_version == 4
    assert audit.risk_state_count == 1
    assert audit.history_command_count == 7
    assert audit.history_resolution_count == 1
    assert audit.duplicate_lease_group_count == 2
    assert audit.risk_stop_active is True
    assert paths.control_db.read_bytes() == before
    assert _schema_version(paths.control_db) == 4


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            "DELETE FROM worker_commands WHERE id='command-one'",
            "已授权的 7 条",
        ),
        (
            "UPDATE worker_commands "
            "SET new_risk_lease_id='single-use-lease' "
            "WHERE id='command-six'",
            "已授权的 2 组",
        ),
    ),
)
def test_safe_cutover_rejects_history_outside_exact_authorization(
    tmp_path,
    mutation,
    message,
):
    paths = _fixture_paths(tmp_path)
    _mutate_control(paths, mutation)

    with pytest.raises(CutoverError, match=message):
        audit_safe_v4_to_v5_cutover(paths)

    assert _schema_version(paths.control_db) == 4
    assert DatabaseWriteFence(paths.control_db).state().active is False


def test_safe_cutover_rejects_source_changed_after_confirmed_plan(tmp_path):
    paths = _fixture_paths(tmp_path)
    _mutate_control(
        paths,
        "UPDATE worker_heartbeats "
        "SET last_seen_at='2026-07-30T02:09:00+00:00' "
        "WHERE worker_id='old-worker'",
    )

    with pytest.raises(CutoverError, match="已确认切换计划"):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
        )

    assert _schema_version(paths.control_db) == 4
    assert DatabaseWriteFence(paths.control_db).state().active is False


def test_safe_cutover_atomic_promotion_blocks_writer_after_intent(
    tmp_path,
):
    paths = _fixture_paths(tmp_path)
    blocked_errors: list[str] = []
    before_count = len(
        ExecutionStore(
            paths.execution_db,
            schema_mode="require_current",
        ).events("execution-one")
    )

    def attempt_late_event(phase: str, _context: object) -> None:
        if phase != "intent_prepared":
            return
        with pytest.raises(DatabaseFenceError) as captured:
            ExecutionStore(
                paths.execution_db,
                schema_mode="require_current",
            ).append_event("execution-one", "late_committed_wal")
        blocked_errors.append(str(captured.value))

    result = perform_safe_v4_to_v5_cutover(
        paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
        _phase_hook=attempt_late_event,
    )

    assert blocked_errors
    assert DatabaseWriteFence(paths.control_db).state().active is False
    after_events = ExecutionStore(
        paths.execution_db,
        schema_mode="require_current",
    ).events("execution-one")
    assert len(after_events) == before_count
    _make_archive_writable(result.archive_directory)


def test_safe_cutover_fence_rejects_late_control_and_ledger_writes(
    tmp_path,
):
    paths = _fixture_paths(tmp_path)
    execution_store = ExecutionStore(
        paths.execution_db,
        schema_mode="require_current",
    )
    event_count = len(execution_store.events("execution-one"))

    def attempt_late_writes(phase: str, _context: object) -> None:
        if phase != "archive_published":
            return
        process_context = multiprocessing.get_context("spawn")
        result_queue = process_context.Queue()
        process = process_context.Process(
            target=_attempt_late_database_writes,
            args=(_path_payload(paths), result_queue),
        )
        process.start()
        try:
            assert result_queue.get(timeout=60) == (True, True)
        finally:
            process.join(timeout=60)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            result_queue.close()
            result_queue.join_thread()
        assert process.exitcode == 0

    result = perform_safe_v4_to_v5_cutover(
        paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
        _phase_hook=attempt_late_writes,
    )
    try:
        with sqlite3.connect(paths.control_db) as connection:
            assert connection.execute("SELECT COUNT(*) FROM worker_heartbeats").fetchone() == (0,)
        connection.close()
        assert len(execution_store.events("execution-one")) == event_count
        assert not [
            event for event in execution_store.events("execution-one") if event.kind == "late_write"
        ]
    finally:
        _make_archive_writable(result.archive_directory)


def test_safe_cutover_v5_starts_real_worker_without_broker_write(
    tmp_path,
):
    paths = _fixture_paths(tmp_path)
    result = perform_safe_v4_to_v5_cutover(
        paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
    )
    worker_store = WorkerStore(
        paths.control_db,
        worker_lock_path=paths.worker_lock,
    )
    execution_store = ExecutionStore(
        paths.execution_db,
        schema_mode="require_current",
    )
    service = _StartupReadOnlyService(execution_store)
    worker = ExecutionWorker(
        store=worker_store,
        service=service,
        lock_path=paths.worker_lock,
        worker_id="cutover-startup-worker",
        heartbeat_interval_seconds=60,
        clock=lambda: datetime(2026, 7, 30, 3, 0, tzinfo=UTC),
    )
    try:
        worker.start()
        heartbeat = worker_store.get_heartbeat("cutover-startup-worker")
        risk_state = worker_store.get_risk_runtime_state("okx:demo:paper")
        assert heartbeat is not None
        assert heartbeat.state is WorkerState.RUNNING
        assert worker_store.list_commands() == []
        assert worker_store.current_new_risk_lease() is None
        assert risk_state is not None
        assert risk_state.kill_active is True
        assert risk_state.kill_reason == "risk_runtime_BrokerTransportError"
        assert service.calls == [
            ("disarm", False),
            ("reconcile",),
        ]
    finally:
        worker.close()
        _make_archive_writable(result.archive_directory)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            "UPDATE worker_commands SET status='pending' " "WHERE id='command-one'",
            "待执行",
        ),
        (
            "DELETE FROM worker_command_resolutions",
            "未解决",
        ),
        (
            """
            INSERT INTO worker_new_risk_lease(
                slot, lease_id, worker_id, config_fingerprint, requester,
                broker, environment, account, granted_at, expires_at
            ) VALUES (
                'NEW_RISK', 'active-lease', 'worker', 'fingerprint',
                'campaign', 'okx', 'demo', 'paper',
                '2026-07-30T02:00:00+00:00',
                '2030-07-30T02:00:00+00:00'
            )
            """,
            "租约",
        ),
        (
            "UPDATE risk_runtime_state SET kill_active=0",
            "风险停止",
        ),
        (
            "INSERT INTO worker_meta(key, value) VALUES " "('unknown_runtime_state', '{}')",
            "未知元数据",
        ),
    ),
)
def test_safe_cutover_rejects_unsafe_control_state_without_rewriting_source(
    tmp_path,
    mutation,
    message,
):
    paths = _fixture_paths(tmp_path)
    _mutate_control(paths, mutation)
    source_hash = _sha256(paths.control_db)

    with pytest.raises(CutoverError, match=message):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
        )

    assert _sha256(paths.control_db) == source_hash
    assert _schema_version(paths.control_db) == 4


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing_field", "正式数据合同"),
        ("command_mismatch", "不一致"),
        ("resolution_code", "不一致"),
    ),
)
def test_safe_cutover_strictly_validates_resolved_uncertain_evidence(
    tmp_path,
    mutation,
    message,
):
    paths = _fixture_paths(tmp_path)
    connection = sqlite3.connect(paths.control_db)
    try:
        row = connection.execute(
            "SELECT evidence_json FROM worker_command_resolutions "
            "WHERE command_id='command-resolved'"
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row[0]))
        if mutation == "missing_field":
            payload.pop("broker_position_count")
        elif mutation == "command_mismatch":
            payload["account"] = "another-account"
        else:
            connection.execute(
                "UPDATE worker_command_resolutions "
                "SET resolution_code='confirmed_read_only' "
                "WHERE command_id='command-resolved'"
            )
        if mutation != "resolution_code":
            evidence_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                UPDATE worker_command_resolutions
                SET evidence_json=?, evidence_digest=?
                WHERE command_id='command-resolved'
                """,
                (
                    evidence_json,
                    hashlib.sha256(evidence_json.encode()).hexdigest(),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    _checkpoint(paths.control_db)
    source_hash = _sha256(paths.control_db)

    with pytest.raises(CutoverError, match=message):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
        )

    assert _sha256(paths.control_db) == source_hash
    assert _schema_version(paths.control_db) == 4
    assert DatabaseWriteFence(paths.control_db).state().active is False


def test_safe_cutover_rejects_active_execution_without_rewriting_source(tmp_path):
    paths = _fixture_paths(tmp_path)
    with sqlite3.connect(paths.execution_db) as connection:
        connection.execute("UPDATE executions SET state='open' WHERE id='execution-one'")
    connection.close()
    _checkpoint(paths.execution_db)
    source_hash = _sha256(paths.control_db)

    with pytest.raises(CutoverError, match="execution"):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
        )

    assert _sha256(paths.control_db) == source_hash
    assert _schema_version(paths.control_db) == 4


def test_safe_cutover_exception_before_swap_keeps_v4_and_requires_recovery(
    tmp_path,
):
    paths = _fixture_paths(tmp_path)
    source_bytes = paths.control_db.read_bytes()

    def crash(current_phase: str, _context: object) -> None:
        if current_phase == "target_prepared":
            raise RuntimeError("crash:target_prepared")

    with pytest.raises(CutoverError, match="维护栅栏保持开启"):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
            _phase_hook=crash,
        )

    assert paths.control_db.read_bytes() == source_bytes
    assert _schema_version(paths.control_db) == 4
    assert DatabaseWriteFence(paths.control_db).state().active is True
    cutover_id = _active_cutover_id(paths)
    recovery = recover_safe_v4_to_v5_cutover(
        paths,
        cutover_id=cutover_id,
        confirmation=SAFE_CUTOVER_RECOVERY_CONFIRMATION,
    )
    assert recovery.active_schema_version == 5
    assert recovery.fence_cleared is True
    assert DatabaseWriteFence(paths.control_db).state().active is False
    if recovery.archive_directory is not None:
        _make_archive_writable(recovery.archive_directory)


def test_safe_cutover_exception_after_swap_keeps_v5_and_active_fence(
    tmp_path,
):
    paths = _fixture_paths(tmp_path)

    def crash(current_phase: str, _context: object) -> None:
        if current_phase == "swapped":
            raise RuntimeError("crash:swapped")

    with pytest.raises(CutoverError, match="维护栅栏保持开启"):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
            _phase_hook=crash,
        )

    assert _schema_version(paths.control_db) == 5
    fence = DatabaseWriteFence(paths.control_db)
    assert fence.state().active is True
    with (
        pytest.raises(
            DatabaseFenceError,
            match="正在维护",
        ),
        fence.write(),
    ):
        pass
    recovery = recover_safe_v4_to_v5_cutover(
        paths,
        cutover_id=_active_cutover_id(paths),
        confirmation=SAFE_CUTOVER_RECOVERY_CONFIRMATION,
    )
    assert recovery.active_schema_version == 5
    assert recovery.fence_cleared is True
    assert DatabaseWriteFence(paths.control_db).state().active is False
    for archive in paths.archive_root.glob("*"):
        _make_archive_writable(archive)


@pytest.mark.parametrize(
    "tamper_sql",
    (
        """
        CREATE TRIGGER replace_existing_command
        BEFORE INSERT ON worker_commands
        BEGIN
            DELETE FROM worker_commands;
        END;
        """,
        """
        INSERT INTO worker_meta(key, value)
        VALUES ('unrelated_metadata', 'changed');
        """,
    ),
)
def test_completed_v5_verifier_rejects_change_after_last_hash(
    tmp_path,
    tamper_sql,
):
    paths = _fixture_paths(tmp_path)

    def add_trigger(phase: str, _context: object) -> None:
        if phase != "post_swap_validated":
            return
        connection = sqlite3.connect(paths.control_db)
        try:
            connection.executescript(tamper_sql)
            connection.commit()
        finally:
            connection.close()
        _checkpoint(paths.control_db)

    with pytest.raises(CutoverError, match="维护栅栏保持开启"):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
            _phase_hook=add_trigger,
        )

    assert _schema_version(paths.control_db) == 5
    assert DatabaseWriteFence(paths.control_db).state().active is True
    with pytest.raises(CutoverError, match="完整结构或内容"):
        recover_safe_v4_to_v5_cutover(
            paths,
            cutover_id=_active_cutover_id(paths),
            confirmation=SAFE_CUTOVER_RECOVERY_CONFIRMATION,
        )
    assert DatabaseWriteFence(paths.control_db).state().active is True
    for archive in paths.archive_root.glob("*"):
        _make_archive_writable(archive)


def test_safe_cutover_recovers_intent_created_before_maintenance(tmp_path):
    paths = _fixture_paths(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_cutover_process,
        args=(_path_payload(paths), "intent_prepared"),
    )
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)

    assert process.exitcode == 91
    assert _schema_version(paths.control_db) == 4
    assert DatabaseWriteFence(paths.control_db).state().active is False
    recovery = recover_safe_v4_to_v5_cutover(
        paths,
        cutover_id=_active_cutover_id(paths),
        confirmation=SAFE_CUTOVER_RECOVERY_CONFIRMATION,
    )
    assert recovery.active_schema_version == 5
    assert DatabaseWriteFence(paths.control_db).state().active is False
    if recovery.archive_directory is not None:
        _make_archive_writable(recovery.archive_directory)


def test_safe_cutover_reuses_empty_target_after_preintent_process_crash(
    tmp_path,
):
    paths = _fixture_paths(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_cutover_process,
        args=(_path_payload(paths), "target_schema_created"),
    )
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)

    assert process.exitcode == 91
    assert _schema_version(paths.control_db) == 4
    assert DatabaseWriteFence(paths.control_db).state().active is False
    assert not (
        paths.control_db.parent
        / "execution_worker_cutover.intent.json"
    ).exists()
    target_path = paths.control_db.with_name(
        f".{paths.control_db.name}.v5-"
        f"{paths.expected_plan_sha256[:32]}.tmp"
    )
    assert target_path.is_file()
    assert _schema_version(target_path) == 5

    result = perform_safe_v4_to_v5_cutover(
        paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
    )

    assert _schema_version(paths.control_db) == 5
    assert DatabaseWriteFence(paths.control_db).state().active is False
    _make_archive_writable(result.archive_directory)


@pytest.mark.parametrize(
    "tamper_sql",
    (
        """
        CREATE TRIGGER replace_existing_command
        BEFORE INSERT ON worker_commands
        BEGIN
            DELETE FROM worker_commands;
        END;
        """,
        """
        DROP TABLE worker_new_risk_lease;
        CREATE TABLE worker_new_risk_lease (
            slot TEXT PRIMARY KEY CHECK(slot='new_risk'),
            lease_id TEXT NOT NULL UNIQUE,
            worker_id TEXT NOT NULL,
            config_fingerprint TEXT NOT NULL,
            requester TEXT NOT NULL,
            command_id TEXT NOT NULL DEFAULT '',
            broker TEXT NOT NULL,
            environment TEXT NOT NULL,
            account TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        """,
    ),
)
def test_safe_cutover_rejects_noncanonical_preintent_target(
    tmp_path,
    tamper_sql,
):
    paths = _fixture_paths(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_cutover_process,
        args=(_path_payload(paths), "target_schema_created"),
    )
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    assert process.exitcode == 91

    target_path = paths.control_db.with_name(
        f".{paths.control_db.name}.v5-"
        f"{paths.expected_plan_sha256[:32]}.tmp"
    )
    connection = sqlite3.connect(target_path)
    try:
        connection.executescript(tamper_sql)
        connection.commit()
    finally:
        connection.close()
    _checkpoint(target_path)

    with pytest.raises(CutoverError, match="正式 WorkerStore schema v5"):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
        )

    assert _schema_version(paths.control_db) == 4
    assert DatabaseWriteFence(paths.control_db).state().active is False
    assert target_path.is_file()


@pytest.mark.parametrize(
    "leftover_name",
    (
        ".source-manifest.json.writing",
        ".prepared-target.json.writing",
    ),
)
def test_safe_cutover_recovery_reuses_fixed_json_writing_file(
    tmp_path,
    leftover_name,
):
    paths = _fixture_paths(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_cutover_process,
        args=(_path_payload(paths), "databases_checkpointed"),
    )
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    assert process.exitcode == 91

    cutover_id = _active_cutover_id(paths)
    archive_temp = paths.archive_root / f".preparing-{cutover_id}"
    archive_temp.mkdir(parents=True)
    leftover = archive_temp / leftover_name
    leftover.write_text("partial-json", encoding="utf-8")

    recovery = recover_safe_v4_to_v5_cutover(
        paths,
        cutover_id=cutover_id,
        confirmation=SAFE_CUTOVER_RECOVERY_CONFIRMATION,
    )

    assert recovery.active_schema_version == 5
    assert DatabaseWriteFence(paths.control_db).state().active is False
    assert not leftover.exists()
    if recovery.archive_directory is not None:
        _make_archive_writable(recovery.archive_directory)


def test_safe_cutover_recovers_finish_after_protocol_floor_commit(tmp_path):
    paths = _fixture_paths(tmp_path)
    result = perform_safe_v4_to_v5_cutover(
        paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
    )
    completed_path = (
        paths.control_db.parent
        / "execution_worker_cutover.completed.json"
    )
    intent_path = (
        paths.control_db.parent
        / "execution_worker_cutover.intent.json"
    )
    fence = DatabaseWriteFence(paths.control_db)
    completed = fence.state()
    database_fence_module._write_state(
        fence.state_path,
        database_fence_module.DatabaseFenceState(
            generation=completed.generation,
            active=True,
            operation_id=result.cutover_id,
            updated_at=database_fence_module._now_iso(),
            protocol_sha=completed.protocol_sha,
            protocol_installed_at=completed.protocol_installed_at,
            protocol_instance_id=completed.protocol_instance_id,
        ),
    )
    assert fence.state().active is True

    recovery = recover_safe_v4_to_v5_cutover(
        paths,
        cutover_id=result.cutover_id,
        confirmation=SAFE_CUTOVER_RECOVERY_CONFIRMATION,
    )

    assert recovery.active_schema_version == 5
    assert fence.state().active is False
    assert completed_path.is_file()
    assert not intent_path.exists()
    _make_archive_writable(result.archive_directory)


def test_interrupted_cutover_fence_blocks_real_worker_start(tmp_path):
    paths = _fixture_paths(tmp_path)

    def crash(current_phase: str, _context: object) -> None:
        if current_phase == "swapped":
            raise RuntimeError("crash:swapped")

    with pytest.raises(CutoverError, match="维护栅栏保持开启"):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
            _phase_hook=crash,
        )

    worker_store = WorkerStore(
        paths.control_db,
        worker_lock_path=paths.worker_lock,
    )
    execution_store = ExecutionStore(
        paths.execution_db,
        schema_mode="require_current",
    )
    service = _StartupReadOnlyService(execution_store)
    worker = ExecutionWorker(
        store=worker_store,
        service=service,
        lock_path=paths.worker_lock,
        worker_id="blocked-startup-worker",
        heartbeat_interval_seconds=60,
    )
    with pytest.raises(DatabaseFenceError, match="正在维护"):
        worker.start()
    assert service.calls == []
    for archive in paths.archive_root.glob("*"):
        _make_archive_writable(archive)


@pytest.mark.parametrize(
    ("phase", "expected_schema"),
    (
        ("databases_checkpointed", 4),
        ("target_prepared", 4),
        ("archive_sealed", 4),
        ("archive_published", 4),
        ("before_swap", 4),
        ("swapped", 5),
        ("completion_recorded", 5),
    ),
)
def test_safe_cutover_hard_crash_is_fail_closed_across_processes(
    tmp_path,
    phase,
    expected_schema,
):
    paths = _fixture_paths(tmp_path)
    source_bytes = paths.control_db.read_bytes()
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_cutover_process,
        args=(_path_payload(paths), phase),
    )

    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)

    assert process.exitcode == 91
    assert _schema_version(paths.control_db) == expected_schema
    if expected_schema == 4:
        assert paths.control_db.read_bytes() == source_bytes
    fence = DatabaseWriteFence(paths.control_db)
    assert fence.state().active is True
    with pytest.raises(DatabaseFenceError), fence.write():
        pass
    recovery = recover_safe_v4_to_v5_cutover(
        paths,
        cutover_id=_active_cutover_id(paths),
        confirmation=SAFE_CUTOVER_RECOVERY_CONFIRMATION,
    )
    assert recovery.active_schema_version == 5
    assert recovery.fence_cleared is True
    assert DatabaseWriteFence(paths.control_db).state().active is False
    for archive in paths.archive_root.glob("*"):
        _make_archive_writable(archive)


def test_safe_cutover_detects_sealed_archive_tamper_before_swap(tmp_path):
    paths = _fixture_paths(tmp_path)
    source_bytes = paths.control_db.read_bytes()

    def tamper(phase: str, context: object) -> None:
        if phase != "archive_sealed":
            return
        archive_directory = context.archive_directory
        archived_db = archive_directory / "source" / paths.control_db.name
        _make_archive_writable(archive_directory)
        with archived_db.open("ab") as handle:
            handle.write(b"tampered")

    with pytest.raises(CutoverError, match="维护栅栏保持开启"):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
            _phase_hook=tamper,
        )

    assert paths.control_db.read_bytes() == source_bytes
    assert _schema_version(paths.control_db) == 4
    assert DatabaseWriteFence(paths.control_db).state().active is True
    with pytest.raises(CutoverError, match="档案"):
        recover_safe_v4_to_v5_cutover(
            paths,
            cutover_id=_active_cutover_id(paths),
            confirmation=SAFE_CUTOVER_RECOVERY_CONFIRMATION,
        )
    assert DatabaseWriteFence(paths.control_db).state().active is True
    for archive in paths.archive_root.glob("*"):
        _make_archive_writable(archive)


def test_safe_cutover_rejects_formal_archive_from_another_intent(
    tmp_path,
):
    source_paths = _fixture_paths(tmp_path / "source")
    source_result = perform_safe_v4_to_v5_cutover(
        source_paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
    )
    target_paths = _fixture_paths(tmp_path / "target")
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_cutover_process,
        args=(_path_payload(target_paths), "databases_checkpointed"),
    )
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    assert process.exitcode == 91

    target_cutover_id = _active_cutover_id(target_paths)
    wrong_archive = (
        target_paths.archive_root / f"v4-{target_cutover_id}"
    )
    wrong_archive.parent.mkdir(parents=True)
    shutil.copytree(source_result.archive_directory, wrong_archive)
    _make_archive_writable(wrong_archive)
    _make_archive_readonly(wrong_archive)

    try:
        with pytest.raises(CutoverError, match="当前切换意图"):
            recover_safe_v4_to_v5_cutover(
                target_paths,
                cutover_id=target_cutover_id,
                confirmation=SAFE_CUTOVER_RECOVERY_CONFIRMATION,
            )
        assert DatabaseWriteFence(
            target_paths.control_db
        ).state().active is True
    finally:
        _make_archive_writable(source_result.archive_directory)
        _make_archive_writable(wrong_archive)


def test_archive_verifier_rejects_forged_prepared_target_hash(tmp_path):
    paths = _fixture_paths(tmp_path)
    result = perform_safe_v4_to_v5_cutover(
        paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
    )
    prepared_manifest = result.archive_directory / "prepared-target.json"
    try:
        _make_archive_writable(result.archive_directory)
        payload = json.loads(prepared_manifest.read_text(encoding="utf-8"))
        payload["prepared_target_sha256"] = "0" * 64
        prepared_manifest.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        _make_archive_readonly(result.archive_directory)

        with pytest.raises(CutoverError, match="准备目标"):
            verify_cutover_archive(result.archive_directory)
    finally:
        _make_archive_writable(result.archive_directory)


def test_archive_verifier_rejects_tampered_prepared_v5_database(tmp_path):
    paths = _fixture_paths(tmp_path)
    result = perform_safe_v4_to_v5_cutover(
        paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
    )
    prepared_database = result.archive_directory / "prepared" / "execution_control.v5.sqlite3"
    try:
        _make_archive_writable(result.archive_directory)
        with prepared_database.open("ab") as handle:
            handle.write(b"tampered")
        _make_archive_readonly(result.archive_directory)

        with pytest.raises(CutoverError, match="摘要"):
            verify_cutover_archive(result.archive_directory)
    finally:
        _make_archive_writable(result.archive_directory)


@pytest.mark.parametrize(
    ("tamper_kind", "message"),
    (
        ("non_unique_index", "一次性授权"),
        ("extra_receipt", "切换回执数量"),
    ),
)
def test_archive_verifier_rejects_forged_v5_safety_contract(
    tmp_path,
    tamper_kind,
    message,
):
    paths = _fixture_paths(tmp_path)
    result = perform_safe_v4_to_v5_cutover(
        paths,
        confirmation=SAFE_CUTOVER_CONFIRMATION,
    )
    prepared_database = (
        result.archive_directory
        / "prepared"
        / "execution_control.v5.sqlite3"
    )
    try:
        _make_archive_writable(result.archive_directory)
        connection = sqlite3.connect(prepared_database)
        try:
            if tamper_kind == "non_unique_index":
                connection.executescript("""
                    DROP INDEX idx_worker_commands_one_new_risk_per_lease;
                    CREATE INDEX idx_worker_commands_one_new_risk_per_lease
                    ON worker_commands(new_risk_lease_id)
                    WHERE new_risk_lease_id<>''
                      AND action IN ('submit', 'set_leverage');
                    """)
            else:
                connection.execute(
                    "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
                    ("worker_safe_cutover:extra", "{}"),
                )
            connection.commit()
        finally:
            connection.close()
        _checkpoint(prepared_database)
        _refresh_prepared_target_manifest(
            result.archive_directory,
            prepared_database,
        )
        _make_archive_readonly(result.archive_directory)

        with pytest.raises(CutoverError, match=message):
            verify_cutover_archive(result.archive_directory)
    finally:
        _make_archive_writable(result.archive_directory)


def test_safe_cutover_requires_exact_confirmation_official_paths_and_free_lock(
    tmp_path,
):
    paths = _fixture_paths(tmp_path)
    source_hash = _sha256(paths.control_db)

    with pytest.raises(CutoverError, match="确认文本"):
        perform_safe_v4_to_v5_cutover(paths, confirmation="yes")

    wrong_paths = CutoverPaths(
        project_root=paths.project_root,
        control_db=paths.control_db,
        execution_db=paths.execution_db,
        gui_lock=paths.gui_lock,
        worker_lock=paths.worker_lock,
        campaign_lock=paths.campaign_lock,
        archive_root=tmp_path / "outside",
        deployed_sha=paths.deployed_sha,
        fence_protocol_sha=paths.fence_protocol_sha,
        expected_plan_sha256=paths.expected_plan_sha256,
    )
    with pytest.raises(CutoverError, match="scratch"):
        perform_safe_v4_to_v5_cutover(
            wrong_paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
        )

    with (
        FileLock(str(paths.worker_lock)),
        pytest.raises(CutoverError, match="单例锁"),
    ):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
        )

    from pa_agent.okx_demo_campaign import CampaignProcessLock

    with (
        CampaignProcessLock(paths.campaign_lock),
        pytest.raises(CutoverError, match="Campaign"),
    ):
        perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
        )

    assert _sha256(paths.control_db) == source_hash
    assert _schema_version(paths.control_db) == 4


def test_corrupt_database_fence_fails_closed_for_both_stores(tmp_path):
    paths = _fixture_paths(tmp_path)
    worker_store = WorkerStore(
        paths.control_db,
        worker_lock_path=paths.worker_lock,
    )
    execution_store = ExecutionStore(
        paths.execution_db,
        schema_mode="require_current",
    )
    fence = DatabaseWriteFence(paths.control_db)
    fence.state_path.write_text('{"version":1}\n', encoding="utf-8")

    with pytest.raises(DatabaseFenceError, match="合同无效"):
        worker_store.record_heartbeat(
            worker_id="blocked-worker",
            pid=1,
            state=WorkerState.STARTING,
        )
    with pytest.raises(DatabaseFenceError, match="合同无效"):
        execution_store.append_event(
            "execution-one",
            "blocked-write",
        )


def test_safe_cutover_rejects_nonempty_wal_and_non_v4_source(tmp_path):
    paths = _fixture_paths(tmp_path)
    wal_path = Path(f"{paths.control_db}-wal")
    wal_path.write_bytes(b"not-empty")
    source_hash = _sha256(paths.control_db)

    with pytest.raises(CutoverError, match="WAL"):
        audit_safe_v4_to_v5_cutover(paths)
    assert _sha256(paths.control_db) == source_hash

    other_paths = _fixture_paths(tmp_path / "other")
    _mutate_control(
        other_paths,
        "UPDATE worker_meta SET value='5' " "WHERE key='worker_schema_version'",
    )
    with pytest.raises(CutoverError, match="schema v4"):
        perform_safe_v4_to_v5_cutover(
            other_paths,
            confirmation=SAFE_CUTOVER_CONFIRMATION,
        )
