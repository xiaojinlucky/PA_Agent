from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest
from filelock import FileLock

import pa_agent.execution.database_fence as database_fence_module
from pa_agent.execution.database_fence import (
    DatabaseFenceError,
    DatabaseWriteFence,
    GuiDatabaseWriterProcessLock,
    _verify_deployed_repository,
    install_official_database_fence_protocol,
)
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker_protocol import WorkerState
from pa_agent.execution.worker_store import WorkerStore

_DEPLOYED_SHA = "a" * 40


def _stores(tmp_path: Path) -> tuple[Path, WorkerStore, ExecutionStore]:
    control_path = tmp_path / "execution_control.sqlite3"
    worker_store = WorkerStore(control_path)
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    return control_path, worker_store, execution_store


def _committed_protocol_repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    source_root = Path(__file__).resolve().parents[2]
    for relative_path in (
        "pa_agent/execution/database_fence.py",
        "pa_agent/execution/store.py",
        "pa_agent/execution/worker_store.py",
        "pa_agent/main.py",
    ):
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source_root / relative_path).read_bytes())
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "add", "--", "pa_agent"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=PA Agent Test",
            "-c",
            "user.email=pa-agent-test@example.invalid",
            "commit",
            "-m",
            "test protocol",
        ],
        check=True,
        capture_output=True,
    )
    deployed_sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    return root, deployed_sha


def test_first_store_write_bootstraps_non_cutover_protocol(tmp_path):
    control_path, worker_store, _execution_store = _stores(tmp_path)

    state = DatabaseWriteFence(control_path).state()

    assert state.protocol_sha == "auto-bootstrap"
    assert state.generation == 0
    assert state.active is False
    worker_store.record_heartbeat(
        worker_id="bootstrap-worker",
        pid=1,
        state=WorkerState.STARTING,
    )


def test_missing_state_with_initialization_marker_fails_closed(tmp_path):
    control_path, worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    assert fence.marker_path.exists()
    fence.state_path.unlink()

    with pytest.raises(DatabaseFenceError, match="初始化标记仍存在"):
        worker_store.record_heartbeat(
            worker_id="blocked-worker",
            pid=1,
            state=WorkerState.STARTING,
        )


def test_protocol_install_is_explicit_idempotent_and_sha_bound(tmp_path):
    control_path, _worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)

    installed = fence.install_protocol(deployed_sha=_DEPLOYED_SHA)

    assert installed.protocol_sha == _DEPLOYED_SHA
    assert fence.install_protocol(deployed_sha=_DEPLOYED_SHA) == installed
    with pytest.raises(DatabaseFenceError, match="另一个提交"):
        fence.install_protocol(deployed_sha="b" * 40)
    with pytest.raises(DatabaseFenceError, match="不匹配"):
        fence.require_protocol(deployed_sha="b" * 40)


def test_protocol_install_recovers_marker_only_interruption(tmp_path):
    control_path, _worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    marker_text = fence.marker_path.read_text(encoding="utf-8")
    fence.state_path.unlink()

    installed = fence.install_protocol(deployed_sha=_DEPLOYED_SHA)

    assert installed.protocol_sha == _DEPLOYED_SHA
    assert installed.active is False
    assert fence.marker_path.read_text(encoding="utf-8") == marker_text
    assert fence.require_protocol(deployed_sha=_DEPLOYED_SHA) == installed


def test_formal_protocol_cannot_bootstrap_after_state_and_marker_are_lost(tmp_path):
    control_path, worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    fence.install_protocol(deployed_sha=_DEPLOYED_SHA)
    fence.state_path.unlink()
    fence.marker_path.unlink()

    with pytest.raises(DatabaseFenceError, match="正式协议"):
        worker_store.record_heartbeat(
            worker_id="lost-formal-protocol-worker",
            pid=1,
            state=WorkerState.STARTING,
        )


def test_formal_protocol_receipt_cannot_be_removed_from_control_database(tmp_path):
    control_path, worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    fence.install_protocol(deployed_sha=_DEPLOYED_SHA)
    with sqlite3.connect(control_path) as connection:
        connection.execute("DELETE FROM worker_meta WHERE key='worker_database_fence_protocol'")
        connection.commit()

    with pytest.raises(DatabaseFenceError, match="协议回执不一致"):
        worker_store.record_heartbeat(
            worker_id="missing-protocol-receipt-worker",
            pid=1,
            state=WorkerState.STARTING,
        )


def test_interrupted_maintenance_blocks_both_store_families(tmp_path):
    control_path, worker_store, execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    fence.install_protocol(deployed_sha=_DEPLOYED_SHA)
    maintenance = fence.begin_maintenance(
        operation_id="interrupted-operation",
        deployed_sha=_DEPLOYED_SHA,
    )
    maintenance.release()

    with pytest.raises(DatabaseFenceError, match="正在维护"):
        worker_store.record_heartbeat(
            worker_id="blocked-worker",
            pid=1,
            state=WorkerState.STARTING,
        )
    with pytest.raises(DatabaseFenceError, match="正在维护"):
        execution_store.append_event(
            "missing-execution",
            "blocked-write",
        )


def test_gui_writer_process_lock_is_exclusive(tmp_path):
    lock_path = tmp_path / "execution_gui_writer.lock"

    with (
        GuiDatabaseWriterProcessLock(lock_path),
        pytest.raises(DatabaseFenceError, match="GUI 写入锁已被占用"),
        GuiDatabaseWriterProcessLock(lock_path),
    ):
        pass


def test_gui_entrypoint_holds_writer_lock_around_app_context_bootstrap():
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "pa_agent/main.py").read_text(encoding="utf-8"))
    main_node = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    gui_lock = next(
        node
        for node in ast.walk(main_node)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "GuiDatabaseWriterProcessLock"
            for item in node.items
        )
    )

    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "bootstrap"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "AppContext"
        for node in ast.walk(gui_lock)
    )


def test_deployed_repository_verification_binds_real_head_and_clean_sources(tmp_path):
    root, deployed_sha = _committed_protocol_repository(tmp_path)

    _verify_deployed_repository(project_root=root, deployed_sha=deployed_sha)
    with pytest.raises(DatabaseFenceError, match="HEAD 不一致"):
        _verify_deployed_repository(project_root=root, deployed_sha="b" * 40)

    (root / "pa_agent/main.py").write_text("# dirty\n", encoding="utf-8")
    with pytest.raises(DatabaseFenceError, match="未提交"):
        _verify_deployed_repository(project_root=root, deployed_sha=deployed_sha)


@pytest.mark.parametrize(
    ("index_flag", "relative_path"),
    (
        ("--assume-unchanged", "pa_agent/execution/store.py"),
        ("--skip-worktree", "pa_agent/execution/worker_store.py"),
    ),
)
def test_deployed_repository_rejects_changes_hidden_by_index_flags(
    tmp_path,
    index_flag,
    relative_path,
):
    root, deployed_sha = _committed_protocol_repository(tmp_path)
    subprocess.run(
        ["git", "-C", str(root), "update-index", index_flag, relative_path],
        check=True,
        capture_output=True,
    )
    (root / relative_path).write_text("# guard removed\n", encoding="utf-8")
    hidden_status = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    assert hidden_status == ""

    with pytest.raises(DatabaseFenceError, match="协议源码与部署 SHA 不一致"):
        _verify_deployed_repository(project_root=root, deployed_sha=deployed_sha)


def test_official_protocol_install_uses_gui_lifecycle_lock(tmp_path):
    root, deployed_sha = _committed_protocol_repository(tmp_path)
    control_path = root / "records" / "execution_control.sqlite3"
    WorkerStore(control_path)
    fence = DatabaseWriteFence(control_path)
    gui_lock_path = root / "records" / "execution_gui_writer.lock"

    with (
        GuiDatabaseWriterProcessLock(gui_lock_path),
        pytest.raises(DatabaseFenceError, match="GUI 写入锁已被占用"),
    ):
        install_official_database_fence_protocol(
            project_root=root,
            deployed_sha=deployed_sha,
        )

    assert fence.state().protocol_sha == "auto-bootstrap"


def test_wal_only_v5_receipt_blocks_fence_generation_downgrade(tmp_path):
    control_path, worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    fence.install_protocol(deployed_sha=_DEPLOYED_SHA)
    maintenance = fence.begin_maintenance(
        operation_id="wal-only-cutover",
        deployed_sha=_DEPLOYED_SHA,
    )
    writer = sqlite3.connect(control_path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
            (
                "worker_safe_cutover:wal-only-cutover",
                json.dumps({"fence_generation": maintenance.state.generation}),
            ),
        )
        writer.commit()
        assert (tmp_path / "execution_control.sqlite3-wal").stat().st_size > 0
        maintenance.finish()
        state_payload = json.loads(fence.state_path.read_text(encoding="utf-8"))
        state_payload["generation"] = 0
        fence.state_path.write_text(
            json.dumps(state_payload) + "\n",
            encoding="utf-8",
        )

        with pytest.raises(DatabaseFenceError, match="代际低于"):
            worker_store.record_heartbeat(
                worker_id="wal-downgrade-worker",
                pid=1,
                state=WorkerState.STARTING,
            )
    finally:
        maintenance.release()
        writer.close()


@pytest.mark.parametrize("downgrade_state", (False, True))
def test_completed_v5_fails_closed_when_cutover_receipt_is_deleted(
    tmp_path,
    downgrade_state,
):
    control_path, worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    fence.install_protocol(deployed_sha=_DEPLOYED_SHA)
    maintenance = fence.begin_maintenance(
        operation_id="deleted-cutover-receipt",
        deployed_sha=_DEPLOYED_SHA,
    )
    try:
        with sqlite3.connect(control_path) as connection:
            connection.execute(
                "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
                (
                    "worker_safe_cutover:deleted-cutover-receipt",
                    json.dumps({"fence_generation": maintenance.state.generation}),
                ),
            )
            connection.commit()
        maintenance.finish()
    finally:
        maintenance.release()

    with sqlite3.connect(control_path) as connection:
        connection.execute("DELETE FROM worker_meta WHERE key LIKE 'worker_safe_cutover:%'")
        connection.commit()
    if downgrade_state:
        state_payload = json.loads(fence.state_path.read_text(encoding="utf-8"))
        state_payload["generation"] = 0
        fence.state_path.write_text(
            json.dumps(state_payload) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(DatabaseFenceError, match="安全切换回执"):
        worker_store.record_heartbeat(
            worker_id="missing-cutover-receipt-worker",
            pid=1,
            state=WorkerState.STARTING,
        )


@pytest.mark.parametrize(
    "receipt_key",
    (
        "Worker_safe_cutover:case-variant-cutover",
        "workerXsafeYcutover:wildcard-variant-cutover",
    ),
)
def test_cutover_finish_rejects_noncanonical_receipt_key(
    tmp_path,
    receipt_key,
):
    control_path, _worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    fence.install_protocol(deployed_sha=_DEPLOYED_SHA)
    maintenance = fence.begin_maintenance(
        operation_id="noncanonical-cutover",
        deployed_sha=_DEPLOYED_SHA,
    )
    try:
        with sqlite3.connect(control_path) as connection:
            connection.execute(
                "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
                (
                    receipt_key,
                    json.dumps({"fence_generation": maintenance.state.generation}),
                ),
            )
            connection.commit()

        with pytest.raises(DatabaseFenceError, match="安全切换回执"):
            maintenance.finish()
        assert fence.state().active is True
    finally:
        maintenance.release()


def test_resume_after_protocol_floor_committed_before_state_completion(tmp_path):
    control_path, worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    fence.install_protocol(deployed_sha=_DEPLOYED_SHA)
    operation_id = "protocol-floor-crash"
    maintenance = fence.begin_maintenance(
        operation_id=operation_id,
        deployed_sha=_DEPLOYED_SHA,
    )
    generation = maintenance.state.generation
    with sqlite3.connect(control_path) as connection:
        connection.execute(
            "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
            (
                f"worker_safe_cutover:{operation_id}",
                json.dumps({"fence_generation": generation}),
            ),
        )
        protocol_payload = json.loads(
            connection.execute(
                "SELECT value FROM worker_meta " "WHERE key='worker_database_fence_protocol'"
            ).fetchone()[0]
        )
        protocol_payload["minimum_generation"] = generation
        connection.execute(
            "UPDATE worker_meta SET value=? " "WHERE key='worker_database_fence_protocol'",
            (json.dumps(protocol_payload),),
        )
        connection.commit()
    maintenance.release()

    assert fence.state().active is True
    with pytest.raises(DatabaseFenceError, match="正在维护"):
        worker_store.record_heartbeat(
            worker_id="blocked-during-resume",
            pid=1,
            state=WorkerState.STARTING,
        )

    resumed = fence.resume_maintenance(
        operation_id=operation_id,
        deployed_sha=_DEPLOYED_SHA,
    )
    try:
        resumed.finish()
    finally:
        resumed.release()

    assert fence.state().active is False
    heartbeat = worker_store.record_heartbeat(
        worker_id="after-safe-resume",
        pid=2,
        state=WorkerState.STARTING,
    )
    assert heartbeat.worker_id == "after-safe-resume"


def test_writer_started_before_completed_maintenance_is_rejected(
    tmp_path,
    monkeypatch,
):
    control_path, worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    fence.install_protocol(deployed_sha=_DEPLOYED_SHA)
    original_read_state = database_fence_module._read_state
    stale_state_read = Event()
    maintenance_finished = Event()
    writer_name = "stale-fence-writer"

    def pause_after_real_state_read(path):
        state = original_read_state(path)
        if current_thread().name == writer_name and not stale_state_read.is_set():
            stale_state_read.set()
            if not maintenance_finished.wait(timeout=10):
                raise AssertionError("维护完成前等待超时")
        return state

    monkeypatch.setattr(
        database_fence_module,
        "_read_state",
        pause_after_real_state_read,
    )
    errors: list[Exception] = []

    def write_heartbeat() -> None:
        try:
            worker_store.record_heartbeat(
                worker_id="stale-writer",
                pid=1,
                state=WorkerState.STARTING,
            )
        except Exception as exc:
            errors.append(exc)

    writer = Thread(target=write_heartbeat, name=writer_name, daemon=True)
    writer.start()
    try:
        assert stale_state_read.wait(timeout=5)
        maintenance = fence.begin_maintenance(
            operation_id="complete-before-stale-writer",
            deployed_sha=_DEPLOYED_SHA,
        )
        try:
            with sqlite3.connect(control_path) as connection:
                connection.execute(
                    "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
                    (
                        "worker_safe_cutover:complete-before-stale-writer",
                        json.dumps({"fence_generation": maintenance.state.generation}),
                    ),
                )
                connection.commit()
            maintenance.finish()
        finally:
            maintenance.release()
    finally:
        maintenance_finished.set()
        writer.join(timeout=10)

    assert writer.is_alive() is False
    assert len(errors) == 1
    assert isinstance(errors[0], DatabaseFenceError)
    assert "维护代际已变化" in str(errors[0])
    assert worker_store.get_heartbeat("stale-writer") is None


def test_lock_timeout_revalidates_downgraded_completed_state(tmp_path):
    control_path, worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    fence.install_protocol(deployed_sha=_DEPLOYED_SHA)
    maintenance = fence.begin_maintenance(
        operation_id="timeout-downgrade",
        deployed_sha=_DEPLOYED_SHA,
    )
    try:
        with sqlite3.connect(control_path) as connection:
            connection.execute(
                "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
                (
                    "worker_safe_cutover:timeout-downgrade",
                    json.dumps({"fence_generation": maintenance.state.generation}),
                ),
            )
            connection.commit()
        maintenance.finish()
    finally:
        maintenance.release()

    state_payload = json.loads(fence.state_path.read_text(encoding="utf-8"))
    state_payload["generation"] = 0
    fence.state_path.write_text(
        json.dumps(state_payload) + "\n",
        encoding="utf-8",
    )
    lock = FileLock(str(fence.lock_path))
    lock.acquire(timeout=0)
    try:
        with pytest.raises(DatabaseFenceError, match="代际低于"):
            worker_store.record_heartbeat(
                worker_id="timeout-downgrade-writer",
                pid=1,
                state=WorkerState.STARTING,
            )
    finally:
        lock.release()

    assert worker_store.get_heartbeat("timeout-downgrade-writer") is None


def test_v5_receipt_prevents_missing_or_downgraded_fence_state(tmp_path):
    control_path, worker_store, _execution_store = _stores(tmp_path)
    fence = DatabaseWriteFence(control_path)
    fence.install_protocol(deployed_sha=_DEPLOYED_SHA)
    maintenance = fence.begin_maintenance(
        operation_id="cutover-operation",
        deployed_sha=_DEPLOYED_SHA,
    )
    writer = sqlite3.connect(control_path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
            (
                "worker_safe_cutover:cutover-operation",
                json.dumps(
                    {
                        "fence_generation": maintenance.state.generation,
                    }
                ),
            ),
        )
        writer.commit()
        assert (tmp_path / "execution_control.sqlite3-wal").stat().st_size > 0
        maintenance.finish()
    finally:
        maintenance.release()

    installed_state = fence.state()
    marker_text = fence.marker_path.read_text(encoding="utf-8")
    fence.state_path.unlink()
    fence.marker_path.unlink()
    with pytest.raises(DatabaseFenceError, match="栅栏文件缺失"):
        worker_store.record_heartbeat(
            worker_id="missing-fence-worker",
            pid=1,
            state=WorkerState.STARTING,
        )

    now = "2026-07-30T12:00:00+00:00"
    fence.state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "generation": 0,
                "active": False,
                "operation_id": "",
                "updated_at": now,
                "protocol_sha": _DEPLOYED_SHA,
                "protocol_installed_at": installed_state.protocol_installed_at,
                "protocol_instance_id": installed_state.protocol_instance_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    fence.marker_path.write_text(marker_text, encoding="utf-8")
    with pytest.raises(DatabaseFenceError, match="代际低于"):
        worker_store.record_heartbeat(
            worker_id="downgraded-fence-worker",
            pid=1,
            state=WorkerState.STARTING,
        )
    writer.close()


@pytest.mark.parametrize(
    ("relative_path", "class_name", "method_names"),
    (
        (
            "pa_agent/execution/worker_store.py",
            "WorkerStore",
            (
                "_initialise",
                "save_risk_runtime_state",
                "backfill_risk_runtime_baselines",
                "enqueue",
                "claim_next",
                "finish_command",
                "recover_inflight",
                "resolve_uncertain_command",
                "grant_new_risk_lease",
                "renew_new_risk_lease",
                "revoke_new_risk_lease",
                "record_heartbeat",
            ),
        ),
        (
            "pa_agent/execution/store.py",
            "ExecutionStore",
            (
                "_initialise",
                "create",
                "save",
                "append_event",
                "acquire_route_claim",
                "save_account_snapshot",
            ),
        ),
    ),
)
def test_every_store_write_entrypoint_uses_database_fence(
    relative_path,
    class_name,
    method_names,
):
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    for method_name in method_names:
        method = methods[method_name]
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "database_write_guard"
            for node in ast.walk(method)
        ), f"{class_name}.{method_name} 缺少数据库维护栅栏"
