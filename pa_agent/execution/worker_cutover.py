"""显式封存历史 v4 控制库，并建立空队列的全新 v5 控制库。

这不是普通 schema 迁移的备用分支。普通迁移发现历史租约被多条新增风险
命令使用时仍然必须失败关闭；只有操作员给出精确确认文本后，才允许本模块
在 Worker 单例锁内执行一次可崩溃前向恢复的安全切换。失败后保持维护栅栏；
已经安装的 v5 不自动回滚。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from filelock import FileLock, Timeout
from pydantic import ValidationError as PydanticValidationError

from pa_agent.execution.database_fence import (
    DatabaseFenceError,
    DatabaseFenceProtocolReceipt,
    DatabaseFenceState,
    DatabaseMaintenanceLease,
    DatabaseWriteFence,
    _parse_protocol_receipt,
    _protocol_receipt_payload,
    _write_state,
    active_windows_image_pids,
)
from pa_agent.execution.models import ExecutionRecord, ExecutionState
from pa_agent.execution.worker_protocol import (
    SetLeverageResolutionEvidence,
    WorkerCommand,
    WorkerCommandAction,
    WorkerCommandResolution,
    WorkerCommandResolutionEvidence,
    WorkerCommandStatus,
)
from pa_agent.execution.worker_store import WorkerStore

SAFE_CUTOVER_CONFIRMATION = "ARCHIVE_V4_AND_CREATE_NEW_V5_CONTROL_DB"
SAFE_CUTOVER_RECOVERY_CONFIRMATION = "RECOVER_INTERRUPTED_V4_TO_V5_CUTOVER"

_SOURCE_SCHEMA_VERSION = 4
_TARGET_SCHEMA_VERSION = 5
_ARCHIVE_SCHEMA_VERSION = 2
_INTENT_SCHEMA_VERSION = 1
_AUTHORIZED_HISTORY_COMMAND_COUNT = 7
_AUTHORIZED_DUPLICATE_LEASE_GROUP_COUNT = 2
_CANONICAL_V5_SCHEMA_SHA256 = (
    "8d4eb8bbf6bcd8db12b234a7ef5ac7dc0f32ae815b056fed3bea3120af843edf"
)
_CANONICAL_V5_NEW_RISK_INDEX_SHA256 = (
    "f97575701c0fab4aa660c84713f685b19e6e18cad5430d450546b8c1a743cd1d"
)
_PROTOCOL_META_KEY = "worker_database_fence_protocol"
_INTENT_NAME = "execution_worker_cutover.intent.json"
_COMPLETION_NAME = "execution_worker_cutover.completed.json"
_CUTOVER_SOURCE_PATH = "pa_agent/execution/worker_cutover.py"
_FENCE_PROTOCOL_SOURCE_PATHS = (
    "pa_agent/execution/database_fence.py",
    "pa_agent/execution/store.py",
    "pa_agent/execution/worker_store.py",
    "pa_agent/main.py",
)
_LOADED_CUTOVER_SOURCE_SHA256 = hashlib.sha256(
    Path(__file__).read_bytes().replace(b"\r\n", b"\n")
).hexdigest()
_SAFE_EXECUTION_STATES = frozenset({"closed", "blocked", "canceled", "rejected"})
_SCHEMA_FAILURE_CODES = frozenset({"ValidationError", "execution_record_invalid"})
_TERMINAL_COMMAND_STATES = frozenset({"succeeded", "failed", "uncertain"})
_RISK_COLUMNS = (
    "route_key",
    "broker",
    "environment",
    "account",
    "account_identity",
    "last_external_cashflow_bill_id",
    "last_account_bill_id",
    "last_account_bill_timestamp_ms",
    "last_bill_scan_at",
    "adjusted_high_water_usd",
    "last_total_equity_usd",
    "drawdown_usd",
    "drawdown_fraction",
    "kill_active",
    "kill_reason",
    "kill_activated_at",
    "updated_at",
)
_EXPECTED_CONTROL_TABLES = frozenset(
    {
        "risk_runtime_state",
        "worker_command_resolutions",
        "worker_commands",
        "worker_heartbeats",
        "worker_meta",
        "worker_new_risk_lease",
    }
)


class CutoverError(RuntimeError):
    """安全切换未满足硬门，或中断后必须按耐久意图前向恢复。"""


@dataclass(frozen=True, slots=True)
class CutoverPaths:
    project_root: Path
    control_db: Path
    execution_db: Path
    gui_lock: Path
    worker_lock: Path
    campaign_lock: Path
    archive_root: Path
    deployed_sha: str
    fence_protocol_sha: str
    expected_plan_sha256: str = ""


@dataclass(frozen=True, slots=True)
class ArchiveVerification:
    valid: bool
    cutover_id: str
    source_database_sha256: str
    source_execution_sha256: str
    prepared_target_sha256: str
    source_manifest_sha256: str
    deployed_sha: str
    fence_protocol_sha: str


@dataclass(frozen=True, slots=True)
class CutoverResult:
    cutover_id: str
    archive_directory: Path
    source_database_sha256: str
    prepared_target_sha256: str
    risk_state_count: int
    history_command_count: int


@dataclass(frozen=True, slots=True)
class CutoverAudit:
    source_schema_version: int
    risk_state_count: int
    history_command_count: int
    history_resolution_count: int
    duplicate_lease_group_count: int
    risk_stop_active: bool
    source_database_sha256: str
    source_execution_sha256: str
    plan_sha256: str


@dataclass(frozen=True, slots=True)
class CutoverRecoveryResult:
    cutover_id: str
    active_schema_version: int
    fence_cleared: bool
    archive_directory: Path | None


@dataclass(frozen=True, slots=True)
class CutoverIntent:
    cutover_id: str
    created_at: str
    deployed_sha: str
    fence_protocol_sha: str
    fence_generation: int
    protocol_installed_at: str
    protocol_instance_id: str
    source_database_sha256: str
    source_database_size: int
    source_execution_sha256: str
    source_execution_size: int
    risk_state_count: int
    risk_metadata_count: int
    history_command_count: int
    history_resolution_count: int
    heartbeat_count: int
    duplicate_lease_group_count: int
    risk_state_sha256: str
    risk_metadata_sha256: str
    plan_sha256: str


@dataclass(frozen=True, slots=True)
class CutoverPhaseContext:
    cutover_id: str
    archive_directory: Path
    prepared_target: Path


@dataclass(frozen=True, slots=True)
class _SourceFile:
    path: Path
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    source_files: tuple[_SourceFile, ...]
    risk_rows: tuple[tuple[object, ...], ...]
    risk_metadata: tuple[tuple[str, str], ...]
    route_keys: tuple[str, ...]
    command_count: int
    resolution_count: int
    heartbeat_count: int
    duplicate_lease_group_count: int
    commands: tuple[WorkerCommand, ...]
    resolutions: tuple[WorkerCommandResolution, ...]
    protocol_receipt: DatabaseFenceProtocolReceipt
    protocol_receipt_payload: str

    @property
    def control_database(self) -> _SourceFile:
        for source_file in self.source_files:
            if source_file.relative_path == "source/execution_control.sqlite3":
                return source_file
        raise CutoverError("v4 审计源缺少控制数据库")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_git_sha(value: str, *, field_name: str) -> str:
    clean = value.strip().lower()
    if (
        len(clean) != 40
        or any(character not in "0123456789abcdef" for character in clean)
    ):
        raise CutoverError(f"{field_name}必须是 40 位小写 Git SHA")
    return clean


def _run_git(
    project_root: Path,
    arguments: list[str],
    *,
    text: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(project_root), *arguments],
        check=False,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        creationflags=(
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        ),
    )


def _git_blob(project_root: Path, revision: str, relative_path: str) -> bytes:
    completed = _run_git(
        project_root,
        ["show", f"{revision}:{relative_path}"],
        text=False,
    )
    if completed.returncode != 0:
        raise CutoverError(f"部署提交缺少切换源码：{relative_path}")
    return completed.stdout.replace(b"\r\n", b"\n")


def _verify_cutover_deployment(paths: CutoverPaths) -> None:
    """证明当前切换器属于 HEAD，旧栅栏源码仍逐字属于已安装提交。"""

    root = paths.project_root
    top_level = _run_git(root, ["rev-parse", "--show-toplevel"])
    if top_level.returncode != 0:
        raise CutoverError("项目根不是可核对的 Git 工作区")
    if Path(top_level.stdout.strip()).resolve(strict=True) != root:
        raise CutoverError("切换目录不是 Git 项目根")
    head = _run_git(root, ["rev-parse", "HEAD"])
    if (
        head.returncode != 0
        or head.stdout.strip().lower() != paths.deployed_sha
    ):
        raise CutoverError("切换器部署 SHA 与当前 HEAD 不一致")
    status = _run_git(
        root,
        ["status", "--porcelain=v1", "--untracked-files=no"],
    )
    if status.returncode != 0 or status.stdout.strip():
        raise CutoverError("项目存在未提交的受跟踪文件改动")

    cutover_blob = _git_blob(root, paths.deployed_sha, _CUTOVER_SOURCE_PATH)
    working_cutover = (root / _CUTOVER_SOURCE_PATH).read_bytes().replace(
        b"\r\n",
        b"\n",
    )
    cutover_digest = hashlib.sha256(cutover_blob).hexdigest()
    if (
        working_cutover != cutover_blob
        or cutover_digest != _LOADED_CUTOVER_SOURCE_SHA256
    ):
        raise CutoverError("当前进程加载的切换器不属于部署 SHA")

    for relative_path in _FENCE_PROTOCOL_SOURCE_PATHS:
        installed_blob = _git_blob(
            root,
            paths.fence_protocol_sha,
            relative_path,
        )
        deployed_blob = _git_blob(
            root,
            paths.deployed_sha,
            relative_path,
        )
        working_blob = (root / relative_path).read_bytes().replace(
            b"\r\n",
            b"\n",
        )
        if installed_blob != deployed_blob or deployed_blob != working_blob:
            raise CutoverError(
                f"正式栅栏源码已偏离已安装协议：{relative_path}"
            )


def _write_json_exclusive(path: Path, payload: dict[str, object]) -> None:
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.writing")
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    try:
        if temporary.exists() and (
            not temporary.is_file()
            or temporary.is_symlink()
            or _is_reparse_point(temporary)
        ):
            raise CutoverError("原子 JSON 临时路径不是普通文件")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _intent_payload(intent: CutoverIntent) -> dict[str, object]:
    return {
        "version": _INTENT_SCHEMA_VERSION,
        "kind": "pa_agent_worker_v4_to_v5_cutover_intent",
        "cutover_id": intent.cutover_id,
        "created_at": intent.created_at,
        "deployed_sha": intent.deployed_sha,
        "fence_protocol_sha": intent.fence_protocol_sha,
        "fence_generation": intent.fence_generation,
        "protocol_installed_at": intent.protocol_installed_at,
        "protocol_instance_id": intent.protocol_instance_id,
        "source_database_sha256": intent.source_database_sha256,
        "source_database_size": intent.source_database_size,
        "source_execution_sha256": intent.source_execution_sha256,
        "source_execution_size": intent.source_execution_size,
        "risk_state_count": intent.risk_state_count,
        "risk_metadata_count": intent.risk_metadata_count,
        "history_command_count": intent.history_command_count,
        "history_resolution_count": intent.history_resolution_count,
        "heartbeat_count": intent.heartbeat_count,
        "duplicate_lease_group_count": intent.duplicate_lease_group_count,
        "risk_state_sha256": intent.risk_state_sha256,
        "risk_metadata_sha256": intent.risk_metadata_sha256,
        "plan_sha256": intent.plan_sha256,
    }


def _intent_path(paths: CutoverPaths) -> Path:
    return paths.control_db.parent / _INTENT_NAME


def _completion_path(paths: CutoverPaths) -> Path:
    return paths.control_db.parent / _COMPLETION_NAME


def _parse_cutover_intent(payload: object) -> CutoverIntent:
    expected_fields = {
        "version",
        "kind",
        "cutover_id",
        "created_at",
        "deployed_sha",
        "fence_protocol_sha",
        "fence_generation",
        "protocol_installed_at",
        "protocol_instance_id",
        "source_database_sha256",
        "source_database_size",
        "source_execution_sha256",
        "source_execution_size",
        "risk_state_count",
        "risk_metadata_count",
        "history_command_count",
        "history_resolution_count",
        "heartbeat_count",
        "duplicate_lease_group_count",
        "risk_state_sha256",
        "risk_metadata_sha256",
        "plan_sha256",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_fields
        or payload.get("version") != _INTENT_SCHEMA_VERSION
        or payload.get("kind")
        != "pa_agent_worker_v4_to_v5_cutover_intent"
    ):
        raise CutoverError("切换意图合同无效")
    cutover_id = payload.get("cutover_id")
    protocol_instance_id = payload.get("protocol_instance_id")
    generation = payload.get("fence_generation")
    sizes = (
        payload.get("source_database_size"),
        payload.get("source_execution_size"),
    )
    hashes = (
        payload.get("source_database_sha256"),
        payload.get("source_execution_sha256"),
        payload.get("risk_state_sha256"),
        payload.get("risk_metadata_sha256"),
        payload.get("plan_sha256"),
    )
    counts = (
        payload.get("risk_state_count"),
        payload.get("risk_metadata_count"),
        payload.get("history_command_count"),
        payload.get("history_resolution_count"),
        payload.get("heartbeat_count"),
        payload.get("duplicate_lease_group_count"),
    )
    if (
        not isinstance(cutover_id, str)
        or len(cutover_id) != 32
        or any(character not in "0123456789abcdef" for character in cutover_id)
        or not isinstance(protocol_instance_id, str)
        or len(protocol_instance_id) != 32
        or any(
            character not in "0123456789abcdef"
            for character in protocol_instance_id
        )
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or any(
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            for size in sizes
        )
        or any(
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for count in counts
        )
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in digest
            )
            for digest in hashes
        )
        or payload.get("history_command_count")
        != _AUTHORIZED_HISTORY_COMMAND_COUNT
        or payload.get("duplicate_lease_group_count")
        != _AUTHORIZED_DUPLICATE_LEASE_GROUP_COUNT
    ):
        raise CutoverError("切换意图字段无效")
    for field_name in ("created_at", "protocol_installed_at"):
        try:
            parsed = datetime.fromisoformat(str(payload.get(field_name)))
        except ValueError as exc:
            raise CutoverError("切换意图时间无效") from exc
        if (
            parsed.tzinfo is None
            or parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            raise CutoverError("切换意图时间必须是 UTC")
    return CutoverIntent(
        cutover_id=cutover_id,
        created_at=str(payload["created_at"]),
        deployed_sha=_validated_git_sha(
            str(payload["deployed_sha"]),
            field_name="切换部署 SHA",
        ),
        fence_protocol_sha=_validated_git_sha(
            str(payload["fence_protocol_sha"]),
            field_name="栅栏协议 SHA",
        ),
        fence_generation=generation,
        protocol_installed_at=str(payload["protocol_installed_at"]),
        protocol_instance_id=protocol_instance_id,
        source_database_sha256=str(payload["source_database_sha256"]),
        source_database_size=int(payload["source_database_size"]),
        source_execution_sha256=str(payload["source_execution_sha256"]),
        source_execution_size=int(payload["source_execution_size"]),
        risk_state_count=int(payload["risk_state_count"]),
        risk_metadata_count=int(payload["risk_metadata_count"]),
        history_command_count=int(payload["history_command_count"]),
        history_resolution_count=int(
            payload["history_resolution_count"]
        ),
        heartbeat_count=int(payload["heartbeat_count"]),
        duplicate_lease_group_count=int(
            payload["duplicate_lease_group_count"]
        ),
        risk_state_sha256=str(payload["risk_state_sha256"]),
        risk_metadata_sha256=str(payload["risk_metadata_sha256"]),
        plan_sha256=str(payload["plan_sha256"]),
    )


def _read_cutover_intent(path: Path) -> CutoverIntent:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise CutoverError("切换意图无法读取") from exc
    return _parse_cutover_intent(payload)


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _validated_paths(paths: CutoverPaths) -> CutoverPaths:
    project_root = Path(paths.project_root).resolve(strict=True)
    expected_records = project_root / "records"
    expected_control = expected_records / "execution_control.sqlite3"
    expected_execution = expected_records / "execution.sqlite3"
    expected_gui_lock = expected_records / "execution_gui_writer.lock"
    expected_lock = expected_records / "execution_worker.lock"
    expected_campaign_lock = expected_records / "okx_demo_campaign.lock"
    control_db = Path(paths.control_db).resolve(strict=True)
    execution_db = Path(paths.execution_db).resolve(strict=True)
    gui_lock = Path(paths.gui_lock).resolve(strict=False)
    worker_lock = Path(paths.worker_lock).resolve(strict=False)
    campaign_lock = Path(paths.campaign_lock).resolve(strict=False)
    archive_root = Path(paths.archive_root).resolve(strict=False)
    scratch_root = (project_root / "scratch").resolve(strict=False)

    if control_db != expected_control.resolve(strict=True):
        raise CutoverError("控制数据库不是项目正式 records 路径")
    if execution_db != expected_execution.resolve(strict=True):
        raise CutoverError("执行账本不是项目正式 records 路径")
    if gui_lock != expected_gui_lock.resolve(strict=False):
        raise CutoverError("GUI 写入锁不是项目正式 records 路径")
    if worker_lock != expected_lock.resolve(strict=False):
        raise CutoverError("Worker 单例锁不是项目正式 records 路径")
    if campaign_lock != expected_campaign_lock.resolve(strict=False):
        raise CutoverError("Campaign 单例锁不是项目正式 records 路径")
    if not _is_relative_to(archive_root, scratch_root):
        raise CutoverError("审计档案必须位于项目 scratch 目录")
    if archive_root == scratch_root:
        raise CutoverError("审计档案必须使用 scratch 下的专用子目录")
    checked_paths = {
        project_root,
        expected_records,
        control_db,
        execution_db,
        scratch_root,
        archive_root,
        gui_lock,
        worker_lock,
        campaign_lock,
        _intent_path(paths),
        _completion_path(paths),
    }
    current = archive_root
    while _is_relative_to(current, project_root):
        checked_paths.add(current)
        if current == project_root:
            break
        current = current.parent
    if any(
        candidate.exists() and (candidate.is_symlink() or _is_reparse_point(candidate))
        for candidate in checked_paths
    ):
        raise CutoverError("安全切换路径不能包含符号链接或 Windows 重解析点")
    expected_plan_sha256 = paths.expected_plan_sha256.strip().lower()
    if expected_plan_sha256 and (
        len(expected_plan_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_plan_sha256
        )
    ):
        raise CutoverError("切换计划摘要必须是 64 位小写 SHA-256")
    return CutoverPaths(
        project_root=project_root,
        control_db=control_db,
        execution_db=execution_db,
        gui_lock=gui_lock,
        worker_lock=worker_lock,
        campaign_lock=campaign_lock,
        archive_root=archive_root,
        deployed_sha=_validated_git_sha(
            paths.deployed_sha,
            field_name="切换部署 SHA",
        ),
        fence_protocol_sha=_validated_git_sha(
            paths.fence_protocol_sha,
            field_name="栅栏协议 SHA",
        ),
        expected_plan_sha256=expected_plan_sha256,
    )


def _require_zero_wal(path: Path, *, label: str) -> None:
    wal_path = Path(f"{path}-wal")
    if wal_path.exists() and wal_path.stat().st_size != 0:
        raise CutoverError(f"{label} WAL 不为空，禁止忽略未归并的提交")


def _checkpoint_and_remove_sidecars(path: Path, *, label: str) -> None:
    connection = sqlite3.connect(
        path,
        timeout=5.0,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if (
            result is None
            or len(result) < 3
            or int(result[0]) != 0
            or int(result[1]) != int(result[2])
        ):
            raise CutoverError(f"{label} WAL 无法在独占栅栏内完整归并")
    finally:
        connection.close()
    _require_zero_wal(path, label=label)
    for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        if not sidecar.exists():
            continue
        try:
            sidecar.unlink()
        except OSError as exc:
            raise CutoverError(f"{label}仍被其他进程持有，禁止安全切换") from exc


def _database_checks(connection: sqlite3.Connection, *, label: str) -> None:
    quick = connection.execute("PRAGMA quick_check").fetchone()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if (
        quick is None
        or str(quick[0]).lower() != "ok"
        or integrity is None
        or str(integrity[0]).lower() != "ok"
        or foreign_keys
    ):
        raise CutoverError(f"{label}完整性检查未通过")


def _canonical_v5_schema_sha256(
    connection: sqlite3.Connection,
) -> str:
    rows = [
        [
            str(row["type"]),
            str(row["name"]),
            str(row["tbl_name"]),
            (
                ""
                if row["sql"] is None
                else str(row["sql"]).replace("\r\n", "\n")
            ),
        ]
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
    ]
    payload = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_canonical_v5_schema(
    connection: sqlite3.Connection,
    *,
    label: str,
) -> None:
    if (
        _canonical_v5_schema_sha256(connection)
        != _CANONICAL_V5_SCHEMA_SHA256
    ):
        raise CutoverError(
            f"{label}不是正式 WorkerStore schema v5 结构"
        )


def _parse_aware_datetime(value: object, *, field_name: str) -> None:
    if value in {None, ""}:
        return
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise CutoverError(f"风险状态 {field_name} 时间无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CutoverError(f"风险状态 {field_name} 缺少时区")


def _parse_finite_decimal(value: object, *, field_name: str) -> None:
    if value in {None, ""}:
        return
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CutoverError(f"风险状态 {field_name} 数字无效") from exc
    if not parsed.is_finite():
        raise CutoverError(f"风险状态 {field_name} 不是有限数字")


def _validate_risk_rows(
    rows: tuple[sqlite3.Row, ...],
) -> tuple[tuple[object, ...], tuple[str, ...]]:
    if not rows:
        raise CutoverError("v4 控制库没有可保留的风险状态")
    copied: list[tuple[object, ...]] = []
    route_keys: list[str] = []
    for row in rows:
        route_key = str(row["route_key"]).strip()
        if (
            not route_key
            or not str(row["broker"]).strip()
            or not str(row["environment"]).strip()
            or not str(row["account"]).strip()
            or not str(row["account_identity"]).strip()
        ):
            raise CutoverError("风险状态的路由或账户身份摘要缺失")
        if int(row["kill_active"]) != 1 or not str(row["kill_reason"]).strip():
            raise CutoverError("风险停止未保持激活，禁止安全切换")
        for field_name in (
            "last_bill_scan_at",
            "kill_activated_at",
            "updated_at",
        ):
            _parse_aware_datetime(row[field_name], field_name=field_name)
        for field_name in (
            "adjusted_high_water_usd",
            "last_total_equity_usd",
            "drawdown_usd",
            "drawdown_fraction",
        ):
            _parse_finite_decimal(row[field_name], field_name=field_name)
        route_keys.append(route_key)
        copied.append(tuple(row[column] for column in _RISK_COLUMNS))
    if len(set(route_keys)) != len(route_keys):
        raise CutoverError("风险状态存在重复路由")
    return tuple(copied), tuple(route_keys)


def _validate_risk_metadata(
    rows: tuple[sqlite3.Row, ...],
    *,
    route_keys: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    metadata: list[tuple[str, str]] = []
    baselines: set[str] = set()
    evidence_routes: set[str] = set()
    routes = set(route_keys)
    for row in rows:
        key = str(row["key"])
        value = str(row["value"])
        if key in {"worker_schema_version", _PROTOCOL_META_KEY}:
            continue
        try:
            payload = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise CutoverError("风险元数据不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise CutoverError("风险元数据必须是 JSON 对象")
        if key.startswith("risk_runtime_baseline:"):
            route_key = key.removeprefix("risk_runtime_baseline:")
            if (
                route_key not in routes
                or payload.get("kind") != "v4_cutover_baseline"
                or payload.get("route_key") != route_key
                or not isinstance(payload.get("backfilled"), bool)
            ):
                raise CutoverError("风险基线元数据无效")
            baselines.add(route_key)
        elif key.startswith("risk_runtime_evidence:"):
            matches = [
                route_key
                for route_key in route_keys
                if key.startswith(f"risk_runtime_evidence:{route_key}:")
            ]
            if len(matches) != 1:
                raise CutoverError("风险证据元数据没有唯一匹配路由")
            route_key = matches[0]
            digest = key.removeprefix(f"risk_runtime_evidence:{route_key}:")
            if (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or hashlib.sha256(value.encode("utf-8")).hexdigest() != digest
            ):
                raise CutoverError("风险证据摘要与内容不一致")
            evidence_routes.add(route_key)
        else:
            raise CutoverError("v4 控制库包含未知元数据，禁止静默丢弃")
        metadata.append((key, value))
    if baselines != routes:
        raise CutoverError("每个风险路由都必须有完整 v4 基线")
    if evidence_routes != routes:
        raise CutoverError("每个风险路由都必须有可复核风险证据")
    return tuple(metadata)


def _validated_commands_and_resolutions(
    connection: sqlite3.Connection,
) -> tuple[tuple[WorkerCommand, ...], tuple[WorkerCommandResolution, ...]]:
    command_rows = connection.execute(
        "SELECT * FROM worker_commands ORDER BY created_at, id"
    ).fetchall()
    try:
        commands = tuple(
            command
            for row in command_rows
            if (command := WorkerStore._row_to_command(row)) is not None
        )
    except (PydanticValidationError, TypeError, ValueError) as exc:
        raise CutoverError("历史 Worker 命令不符合正式数据合同") from exc
    if len(commands) != len(command_rows):
        raise CutoverError("历史 Worker 命令无法完整解析")

    resolution_rows = connection.execute(
        "SELECT * FROM worker_command_resolutions ORDER BY command_id"
    ).fetchall()
    try:
        resolutions = tuple(
            resolution
            for row in resolution_rows
            if (resolution := WorkerStore._row_to_resolution(row)) is not None
        )
    except (
        PydanticValidationError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise CutoverError("历史 UNCERTAIN 处置不符合正式数据合同") from exc
    if len(resolutions) != len(resolution_rows):
        raise CutoverError("历史 UNCERTAIN 处置无法完整解析")

    command_by_id = {command.id: command for command in commands}
    if len(command_by_id) != len(commands):
        raise CutoverError("历史 Worker 命令编号不唯一")
    resolution_by_command = {resolution.command_id: resolution for resolution in resolutions}
    if len(resolution_by_command) != len(resolutions):
        raise CutoverError("历史 UNCERTAIN 处置编号不唯一")
    for resolution in resolutions:
        command = command_by_id.get(resolution.command_id)
        if command is None or command.status is not WorkerCommandStatus.UNCERTAIN:
            raise CutoverError("历史处置没有唯一对应的 UNCERTAIN 命令")
    unresolved = [
        command.id
        for command in commands
        if command.status is WorkerCommandStatus.UNCERTAIN
        and command.id not in resolution_by_command
    ]
    if unresolved:
        raise CutoverError("v4 控制库仍有未解决 UNCERTAIN 命令")
    return commands, resolutions


def _read_control_snapshot(
    paths: CutoverPaths,
    *,
    expected_protocol_state: DatabaseFenceState,
) -> tuple[
    tuple[tuple[object, ...], ...],
    tuple[tuple[str, str], ...],
    tuple[str, ...],
    int,
    int,
    int,
    int,
    tuple[WorkerCommand, ...],
    tuple[WorkerCommandResolution, ...],
    DatabaseFenceProtocolReceipt,
    str,
]:
    _require_zero_wal(paths.control_db, label="v4 控制库")
    connection = _open_immutable(paths.control_db)
    try:
        _database_checks(connection, label="v4 控制库")
        tables = frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        )
        if tables != _EXPECTED_CONTROL_TABLES:
            raise CutoverError("v4 控制库表结构不是已审计的完整集合")
        version = connection.execute("""
            SELECT value FROM worker_meta
            WHERE key='worker_schema_version'
            """).fetchone()
        if version is None or str(version["value"]) != str(_SOURCE_SCHEMA_VERSION):
            raise CutoverError("安全切换只接受完整的 schema v4 控制库")

        commands, resolutions = _validated_commands_and_resolutions(connection)
        if len(commands) != _AUTHORIZED_HISTORY_COMMAND_COUNT:
            raise CutoverError(
                "v4 控制库历史命令数不是已授权的 7 条"
            )
        if any(command.status.value not in _TERMINAL_COMMAND_STATES for command in commands):
            raise CutoverError("v4 控制库仍有待执行或正在执行的命令")
        if connection.execute(
            "SELECT 1 FROM worker_commands "
            "WHERE new_risk_lease_id<>'' "
            "AND action NOT IN ('submit', 'set_leverage') LIMIT 1"
        ).fetchone():
            raise CutoverError("历史租约关联了未知新增风险动作")
        lease_count = int(
            connection.execute("SELECT COUNT(*) FROM worker_new_risk_lease").fetchone()[0]
        )
        if lease_count:
            raise CutoverError("v4 控制库仍有 NEW_RISK 租约")
        duplicate_groups = int(connection.execute("""
                SELECT COUNT(*) FROM (
                    SELECT new_risk_lease_id
                    FROM worker_commands
                    WHERE new_risk_lease_id<>''
                      AND action IN ('submit', 'set_leverage')
                    GROUP BY new_risk_lease_id
                    HAVING COUNT(*)>1
                )
                """).fetchone()[0])
        if duplicate_groups != _AUTHORIZED_DUPLICATE_LEASE_GROUP_COUNT:
            raise CutoverError(
                "v4 控制库历史授权复用组数不是已授权的 2 组"
            )
        raw_risk_rows = tuple(
            connection.execute("SELECT * FROM risk_runtime_state ORDER BY route_key").fetchall()
        )
        risk_rows, route_keys = _validate_risk_rows(raw_risk_rows)
        raw_metadata = tuple(
            connection.execute("SELECT key, value FROM worker_meta ORDER BY key").fetchall()
        )
        protocol_rows = [
            row for row in raw_metadata if str(row["key"]) == _PROTOCOL_META_KEY
        ]
        if len(protocol_rows) != 1:
            raise CutoverError("v4 控制库正式栅栏协议回执数量不正确")
        protocol_payload = str(protocol_rows[0]["value"])
        try:
            protocol_receipt = _parse_protocol_receipt(protocol_payload)
        except DatabaseFenceError as exc:
            raise CutoverError(str(exc)) from exc
        if (
            protocol_payload != _protocol_receipt_payload(protocol_receipt)
            or protocol_receipt.protocol_sha
            != expected_protocol_state.protocol_sha
            or protocol_receipt.protocol_installed_at
            != expected_protocol_state.protocol_installed_at
            or protocol_receipt.protocol_instance_id
            != expected_protocol_state.protocol_instance_id
            or protocol_receipt.minimum_generation
            != expected_protocol_state.generation
        ):
            raise CutoverError("v4 控制库正式栅栏协议回执与耐久状态不一致")
        metadata = _validate_risk_metadata(
            raw_metadata,
            route_keys=route_keys,
        )
        resolution_count = len(resolutions)
        heartbeat_count = int(
            connection.execute("SELECT COUNT(*) FROM worker_heartbeats").fetchone()[0]
        )
    finally:
        connection.close()
    return (
        risk_rows,
        metadata,
        route_keys,
        len(commands),
        resolution_count,
        heartbeat_count,
        duplicate_groups,
        commands,
        resolutions,
        protocol_receipt,
        protocol_payload,
    )


def _validate_execution_ledger(
    paths: CutoverPaths,
    *,
    commands: tuple[WorkerCommand, ...],
    resolutions: tuple[WorkerCommandResolution, ...],
    risk_rows: tuple[tuple[object, ...], ...],
) -> None:
    _require_zero_wal(paths.execution_db, label="execution 账本")
    connection = _open_immutable(paths.execution_db)
    try:
        _database_checks(connection, label="execution 账本")
        version = connection.execute(
            "SELECT value FROM execution_meta WHERE key='schema_version'"
        ).fetchone()
        if version is None or str(version["value"]) != "2":
            raise CutoverError("execution 账本 schema 不是已审计的 v2")
        rows = connection.execute("SELECT id, state, payload_json FROM executions").fetchall()
        records: dict[str, ExecutionRecord] = {}
        try:
            for row in rows:
                record = ExecutionRecord.model_validate_json(str(row["payload_json"]))
                if record.id != str(row["id"]) or record.state.value != str(row["state"]):
                    raise CutoverError("execution 主列与耐久记录不一致")
                records[record.id] = record
        except PydanticValidationError as exc:
            raise CutoverError("execution 账本记录不符合正式数据合同") from exc
        if len(records) != len(rows):
            raise CutoverError("execution 账本编号不唯一")
        if any(record.state.value not in _SAFE_EXECUTION_STATES for record in records.values()):
            raise CutoverError("execution 账本仍有非安全终态执行记录")
        command_execution_ids = {
            command.execution_id for command in commands if command.execution_id
        }
        if command_execution_ids - set(records):
            raise CutoverError("历史命令引用的执行记录不完整")

        risk_index = {name: index for index, name in enumerate(_RISK_COLUMNS)}
        identities: dict[tuple[str, str, str], str] = {}
        for risk_row in risk_rows:
            route = (
                str(risk_row[risk_index["broker"]]).strip().lower(),
                str(risk_row[risk_index["environment"]]).strip().lower(),
                str(risk_row[risk_index["account"]]).strip(),
            )
            if route in identities:
                raise CutoverError("风险状态账户路由不唯一")
            identities[route] = str(risk_row[risk_index["account_identity"]]).strip()

        command_by_id = {command.id: command for command in commands}
        for resolution in resolutions:
            command = command_by_id[resolution.command_id]
            route = (
                command.broker,
                command.environment,
                command.account,
            )
            expected_identity = identities.get(route)
            if expected_identity is None:
                raise CutoverError("历史处置没有对应的风险账户身份")
            evidence = resolution.evidence
            if isinstance(evidence, WorkerCommandResolutionEvidence):
                record = records.get(command.execution_id)
                event_rows = connection.execute(
                    """
                    SELECT kind FROM execution_events
                    WHERE execution_id=?
                    ORDER BY event_id
                    """,
                    (command.execution_id,),
                ).fetchall()
                event_kinds = tuple(str(row["kind"]) for row in event_rows)
                if (
                    resolution.resolution_code != "confirmed_not_written_schema_validation"
                    or command.action is not WorkerCommandAction.SUBMIT
                    or command.failure_code not in _SCHEMA_FAILURE_CODES
                    or record is None
                    or record.state is not ExecutionState.CANCELED
                    or record.preflight is not None
                    or bool(record.client_order_id)
                    or bool(record.broker_order_id)
                    or record.filled_quantity != 0
                    or event_kinds != ("plan_created", "ready_expired")
                    or evidence.execution_id != command.execution_id
                    or evidence.command_action != command.action.value
                    or evidence.command_failure_code != command.failure_code
                    or evidence.broker != command.broker
                    or evidence.environment != command.environment
                    or evidence.account != command.account
                    or evidence.instrument != record.plan.instrument
                    or evidence.execution_state != record.state.value
                    or evidence.broker_order_id_present
                    or evidence.client_order_id_present
                    or evidence.filled_quantity != record.filled_quantity
                    or evidence.event_kinds != event_kinds
                    or evidence.active_execution_count != 0
                    or evidence.new_risk_lease_present
                    or evidence.broker_position_count != 0
                    or evidence.broker_pending_order_count != 0
                    or evidence.broker_pending_algo_order_count != 0
                    or evidence.broker_account_identity_digest != expected_identity
                ):
                    raise CutoverError("历史 UNCERTAIN 提交处置与命令或执行账本不一致")
            elif isinstance(evidence, SetLeverageResolutionEvidence):
                parameters = command.parameters
                applied = (
                    resolution.resolution_code == "confirmed_applied_by_leverage_readback"
                    and evidence.confirmed_leverage == evidence.target_leverage
                    and evidence.confirmed_max_size >= evidence.required_quantity
                )
                not_applied = (
                    resolution.resolution_code == "confirmed_not_applied_by_leverage_readback"
                    and parameters is not None
                    and evidence.confirmed_leverage == parameters.current_leverage
                )
                if (
                    command.action is not WorkerCommandAction.SET_LEVERAGE
                    or parameters is None
                    or not (applied or not_applied)
                    or evidence.analysis_digest != parameters.analysis_digest
                    or evidence.command_action != command.action.value
                    or evidence.command_failure_code != command.failure_code
                    or evidence.broker != command.broker
                    or evidence.environment != command.environment
                    or evidence.account != command.account
                    or evidence.instrument != parameters.instrument
                    or evidence.target_leverage != parameters.target_leverage
                    or evidence.required_quantity != parameters.required_quantity
                    or evidence.active_execution_count != 0
                    or evidence.new_risk_lease_present
                    or evidence.broker_position_count != 0
                    or evidence.broker_pending_order_count != 0
                    or evidence.broker_pending_algo_order_count != 0
                    or evidence.broker_account_identity_digest != expected_identity
                ):
                    raise CutoverError("历史 UNCERTAIN 杠杆处置与命令或只读回读不一致")
            else:  # pragma: no cover - Pydantic 联合类型已先失败关闭
                raise CutoverError("历史 UNCERTAIN 处置类型未知")
    finally:
        connection.close()


def _source_file(
    path: Path,
    *,
    relative_path: str,
) -> _SourceFile:
    if not path.is_file():
        raise CutoverError("审计源文件缺失")
    return _SourceFile(
        path=path,
        relative_path=relative_path,
        size=path.stat().st_size,
        sha256=_sha256_file(path),
    )


def _capture_source_snapshot(
    paths: CutoverPaths,
    *,
    expected_protocol_state: DatabaseFenceState,
) -> _SourceSnapshot:
    (
        risk_rows,
        metadata,
        route_keys,
        command_count,
        resolution_count,
        heartbeat_count,
        duplicate_groups,
        commands,
        resolutions,
        protocol_receipt,
        protocol_receipt_payload,
    ) = _read_control_snapshot(
        paths,
        expected_protocol_state=expected_protocol_state,
    )
    _validate_execution_ledger(
        paths,
        commands=commands,
        resolutions=resolutions,
        risk_rows=risk_rows,
    )
    source_files: list[_SourceFile] = [
        _source_file(
            paths.control_db,
            relative_path="source/execution_control.sqlite3",
        ),
        _source_file(
            paths.execution_db,
            relative_path="source/execution.sqlite3",
        ),
    ]
    return _SourceSnapshot(
        source_files=tuple(source_files),
        risk_rows=risk_rows,
        risk_metadata=metadata,
        route_keys=route_keys,
        command_count=command_count,
        resolution_count=resolution_count,
        heartbeat_count=heartbeat_count,
        duplicate_lease_group_count=duplicate_groups,
        commands=commands,
        resolutions=resolutions,
        protocol_receipt=protocol_receipt,
        protocol_receipt_payload=protocol_receipt_payload,
    )


def _execution_source_file(snapshot: _SourceSnapshot) -> _SourceFile:
    for source_file in snapshot.source_files:
        if source_file.relative_path == "source/execution.sqlite3":
            return source_file
    raise CutoverError("v4 审计源缺少 execution 账本")


def _canonical_rows_sha256(rows: tuple[tuple[object, ...], ...]) -> str:
    payload = json.dumps(
        [list(row) for row in rows],
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _risk_metadata_sha256(
    rows: tuple[tuple[str, str], ...],
) -> str:
    payload = json.dumps(
        [list(row) for row in rows],
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cutover_plan_payload(
    paths: CutoverPaths,
    *,
    state: DatabaseFenceState,
    snapshot: _SourceSnapshot,
) -> dict[str, object]:
    execution_source = _execution_source_file(snapshot)
    return {
        "version": 1,
        "kind": "pa_agent_worker_v4_to_v5_cutover_plan",
        "deployed_sha": paths.deployed_sha,
        "fence_protocol_sha": paths.fence_protocol_sha,
        "fence_generation": state.generation + 1,
        "protocol_installed_at": state.protocol_installed_at,
        "protocol_instance_id": state.protocol_instance_id,
        "source_database_sha256": snapshot.control_database.sha256,
        "source_database_size": snapshot.control_database.size,
        "source_execution_sha256": execution_source.sha256,
        "source_execution_size": execution_source.size,
        "risk_state_count": len(snapshot.risk_rows),
        "risk_metadata_count": len(snapshot.risk_metadata),
        "history_command_count": snapshot.command_count,
        "history_resolution_count": snapshot.resolution_count,
        "heartbeat_count": snapshot.heartbeat_count,
        "duplicate_lease_group_count": (
            snapshot.duplicate_lease_group_count
        ),
        "risk_state_sha256": _canonical_rows_sha256(
            snapshot.risk_rows
        ),
        "risk_metadata_sha256": _risk_metadata_sha256(
            snapshot.risk_metadata
        ),
    }


def _cutover_plan_sha256(
    paths: CutoverPaths,
    *,
    state: DatabaseFenceState,
    snapshot: _SourceSnapshot,
) -> str:
    payload = json.dumps(
        _cutover_plan_payload(
            paths,
            state=state,
            snapshot=snapshot,
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_snapshot_against_intent(
    snapshot: _SourceSnapshot,
    intent: CutoverIntent,
) -> None:
    execution_source = _execution_source_file(snapshot)
    if (
        snapshot.control_database.sha256
        != intent.source_database_sha256
        or snapshot.control_database.size
        != intent.source_database_size
        or execution_source.sha256 != intent.source_execution_sha256
        or execution_source.size != intent.source_execution_size
        or len(snapshot.risk_rows) != intent.risk_state_count
        or len(snapshot.risk_metadata) != intent.risk_metadata_count
        or snapshot.command_count != intent.history_command_count
        or snapshot.resolution_count
        != intent.history_resolution_count
        or snapshot.heartbeat_count != intent.heartbeat_count
        or snapshot.duplicate_lease_group_count
        != intent.duplicate_lease_group_count
        or _canonical_rows_sha256(snapshot.risk_rows)
        != intent.risk_state_sha256
        or _risk_metadata_sha256(snapshot.risk_metadata)
        != intent.risk_metadata_sha256
    ):
        raise CutoverError("源数据库事实与已确认切换计划不一致")


def _verify_source_unchanged(snapshot: _SourceSnapshot) -> None:
    for source_file in snapshot.source_files:
        _require_zero_wal(
            source_file.path,
            label=source_file.relative_path,
        )
        if (
            not source_file.path.is_file()
            or source_file.path.stat().st_size != source_file.size
            or _sha256_file(source_file.path) != source_file.sha256
        ):
            raise CutoverError("源数据库在切换准备期间发生变化")


def _copy_file_atomic_exact(source: Path, destination: Path) -> None:
    expected_size = source.stat().st_size
    expected_hash = _sha256_file(source)
    if destination.exists():
        if (
            not destination.is_file()
            or destination.stat().st_size != expected_size
            or _sha256_file(destination) != expected_hash
        ):
            raise CutoverError("切换档案已有文件与耐久意图不一致")
        return
    temporary = destination.with_name(f".{destination.name}.copying")
    try:
        with (
            source.open("rb") as reader,
            temporary.open("wb") as writer,
        ):
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        if (
            temporary.stat().st_size != expected_size
            or _sha256_file(temporary) != expected_hash
        ):
            raise CutoverError("切换档案临时副本哈希不一致")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_source_archive(
    snapshot: _SourceSnapshot,
    *,
    intent_path: Path,
    intent: CutoverIntent,
    archive_directory: Path,
) -> Path:
    archive_source = archive_directory / "source"
    archive_source.mkdir(parents=True, exist_ok=True)
    file_entries: list[dict[str, object]] = []
    for source_file in snapshot.source_files:
        relative = PurePosixPath(source_file.relative_path)
        destination = archive_directory / Path(*relative.parts)
        _copy_file_atomic_exact(source_file.path, destination)
        if (
            destination.stat().st_size != source_file.size
            or _sha256_file(destination) != source_file.sha256
        ):
            raise CutoverError("v4 审计档案复制后哈希不一致")
        file_entries.append(
            {
                "path": source_file.relative_path,
                "size": source_file.size,
                "sha256": source_file.sha256,
            }
        )
    archived_intent = archive_directory / "cutover-intent.json"
    _copy_file_atomic_exact(intent_path, archived_intent)
    intent_sha256 = _sha256_file(archived_intent)
    manifest_path = archive_directory / "source-manifest.json"
    manifest_payload = {
        "archive_schema_version": _ARCHIVE_SCHEMA_VERSION,
        "kind": "pa_agent_worker_v4_audit_archive",
        "cutover_id": intent.cutover_id,
        "created_at": intent.created_at,
        "deployed_sha": intent.deployed_sha,
        "fence_protocol_sha": intent.fence_protocol_sha,
        "protocol_installed_at": intent.protocol_installed_at,
        "protocol_instance_id": intent.protocol_instance_id,
        "source_schema_version": _SOURCE_SCHEMA_VERSION,
        "target_schema_version": _TARGET_SCHEMA_VERSION,
        "fence_generation": intent.fence_generation,
        "plan_sha256": intent.plan_sha256,
        "cutover_intent_sha256": intent_sha256,
        "source_files": file_entries,
        "retained_history": {
            "command_count": snapshot.command_count,
            "resolution_count": snapshot.resolution_count,
            "heartbeat_count": snapshot.heartbeat_count,
            "duplicate_lease_group_count": (
                snapshot.duplicate_lease_group_count
            ),
        },
        "copied_operational_state": {
            "risk_state_count": len(snapshot.risk_rows),
            "risk_metadata_count": len(snapshot.risk_metadata),
        },
        "active_v5_queue_starts_empty": True,
    }
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError) as exc:
            raise CutoverError("已有源档案清单损坏") from exc
        if existing_manifest != manifest_payload:
            raise CutoverError("已有源档案清单与耐久意图不一致")
    else:
        _write_json_atomic(manifest_path, manifest_payload)
    return manifest_path


def _insert_target_state(
    target_path: Path,
    *,
    snapshot: _SourceSnapshot,
    intent: CutoverIntent,
    source_manifest_sha256: str,
) -> None:
    placeholders = ",".join("?" for _ in _RISK_COLUMNS)
    connection = sqlite3.connect(
        target_path,
        timeout=5.0,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            operational_counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in (
                    "worker_commands",
                    "worker_command_resolutions",
                    "worker_new_risk_lease",
                    "worker_heartbeats",
                    "risk_runtime_state",
                )
            }
            existing_cutover_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM worker_meta "
                    "WHERE key GLOB 'worker_safe_cutover:*'"
                ).fetchone()[0]
            )
            if any(operational_counts.values()) or existing_cutover_rows:
                connection.execute("COMMIT")
                return
            if connection.execute(
                "SELECT 1 FROM worker_meta WHERE key=?",
                (_PROTOCOL_META_KEY,),
            ).fetchone() is not None:
                raise CutoverError("全新 v5 控制库意外已有正式协议回执")
            connection.execute(
                "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
                (
                    _PROTOCOL_META_KEY,
                    snapshot.protocol_receipt_payload,
                ),
            )
            connection.executemany(
                f"""
                INSERT INTO risk_runtime_state({",".join(_RISK_COLUMNS)})
                VALUES ({placeholders})
                """,
                snapshot.risk_rows,
            )
            connection.executemany(
                "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
                snapshot.risk_metadata,
            )
            receipt = {
                "kind": "worker_v4_to_v5_safe_cutover",
                "cutover_id": intent.cutover_id,
                "created_at": intent.created_at,
                "deployed_sha": intent.deployed_sha,
                "fence_protocol_sha": intent.fence_protocol_sha,
                "protocol_installed_at": intent.protocol_installed_at,
                "protocol_instance_id": intent.protocol_instance_id,
                "plan_sha256": intent.plan_sha256,
                "source_schema_version": _SOURCE_SCHEMA_VERSION,
                "target_schema_version": _TARGET_SCHEMA_VERSION,
                "source_database_sha256": (snapshot.control_database.sha256),
                "source_manifest_sha256": source_manifest_sha256,
                "fence_generation": intent.fence_generation,
                "copied_risk_state_count": len(snapshot.risk_rows),
                "copied_risk_metadata_count": len(snapshot.risk_metadata),
                "archived_command_count": snapshot.command_count,
                "archived_resolution_count": snapshot.resolution_count,
                "archived_heartbeat_count": snapshot.heartbeat_count,
                "active_queue_started_empty": True,
                "risk_stop_preserved": True,
            }
            connection.execute(
                "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
                (
                    f"worker_safe_cutover:{intent.cutover_id}",
                    json.dumps(
                        receipt,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0:
            raise CutoverError("全新 v5 控制库 WAL 归并失败")
    finally:
        connection.close()
    for sidecar in (Path(f"{target_path}-wal"), Path(f"{target_path}-shm")):
        if sidecar.exists():
            if sidecar.name.endswith("-wal") and sidecar.stat().st_size:
                raise CutoverError("全新 v5 控制库仍有未归并 WAL")
            sidecar.unlink()


def _validate_one_new_risk_index(
    connection: sqlite3.Connection,
    *,
    label: str,
) -> None:
    index_rows = connection.execute(
        "PRAGMA index_list(worker_commands)"
    ).fetchall()
    matches = [
        row
        for row in index_rows
        if str(row["name"])
        == "idx_worker_commands_one_new_risk_per_lease"
    ]
    if (
        len(matches) != 1
        or int(matches[0]["unique"]) != 1
        or int(matches[0]["partial"]) != 1
    ):
        raise CutoverError(f"{label}缺少一次性授权唯一部分索引")
    index_columns = connection.execute(
        "PRAGMA index_info(idx_worker_commands_one_new_risk_per_lease)"
    ).fetchall()
    if (
        len(index_columns) != 1
        or str(index_columns[0]["name"]) != "new_risk_lease_id"
    ):
        raise CutoverError(f"{label}一次性授权索引列不正确")
    index_sql = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type='index'
          AND name='idx_worker_commands_one_new_risk_per_lease'
        """
    ).fetchone()
    exact_sql = (
        ""
        if index_sql is None or index_sql["sql"] is None
        else str(index_sql["sql"]).replace("\r\n", "\n")
    )
    if (
        hashlib.sha256(exact_sql.encode("utf-8")).hexdigest()
        != _CANONICAL_V5_NEW_RISK_INDEX_SHA256
    ):
        raise CutoverError(f"{label}一次性授权索引定义不正确")


def _validate_prepared_target(
    target_path: Path,
    *,
    snapshot: _SourceSnapshot,
    intent: CutoverIntent,
    source_manifest_sha256: str,
) -> None:
    _require_zero_wal(target_path, label="全新 v5 控制库")
    connection = _open_immutable(target_path)
    try:
        _database_checks(connection, label="全新 v5 控制库")
        version = connection.execute(
            "SELECT value FROM worker_meta WHERE key='worker_schema_version'"
        ).fetchone()
        if version is None or str(version["value"]) != str(_TARGET_SCHEMA_VERSION):
            raise CutoverError("全新控制库不是 schema v5")
        for table in (
            "worker_commands",
            "worker_command_resolutions",
            "worker_new_risk_lease",
            "worker_heartbeats",
        ):
            if int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]):
                raise CutoverError("全新 v5 控制库意外带入历史队列")
        risk_count = int(
            connection.execute("SELECT COUNT(*) FROM risk_runtime_state").fetchone()[0]
        )
        if risk_count != len(snapshot.risk_rows):
            raise CutoverError("全新 v5 控制库风险状态数量不一致")
        receipts = connection.execute(
            "SELECT key, value FROM worker_meta "
            "WHERE key GLOB 'worker_safe_cutover:*'"
        ).fetchall()
        if (
            len(receipts) != 1
            or str(receipts[0]["key"])
            != f"worker_safe_cutover:{intent.cutover_id}"
        ):
            raise CutoverError("全新 v5 控制库切换回执数量不正确")
        receipt = receipts[0]
        try:
            receipt_payload = json.loads(str(receipt["value"]))
        except (TypeError, ValueError) as exc:
            raise CutoverError("全新 v5 控制库切换回执无效") from exc
        if (
            not isinstance(receipt_payload, dict)
            or receipt_payload.get("cutover_id") != intent.cutover_id
            or receipt_payload.get("deployed_sha") != intent.deployed_sha
            or receipt_payload.get("fence_protocol_sha")
            != intent.fence_protocol_sha
            or receipt_payload.get("protocol_installed_at")
            != intent.protocol_installed_at
            or receipt_payload.get("protocol_instance_id")
            != intent.protocol_instance_id
            or receipt_payload.get("plan_sha256")
            != intent.plan_sha256
            or receipt_payload.get("source_schema_version") != _SOURCE_SCHEMA_VERSION
            or receipt_payload.get("target_schema_version") != _TARGET_SCHEMA_VERSION
            or receipt_payload.get("source_database_sha256") != snapshot.control_database.sha256
            or receipt_payload.get("source_manifest_sha256") != source_manifest_sha256
            or receipt_payload.get("fence_generation")
            != intent.fence_generation
            or receipt_payload.get("copied_risk_state_count") != len(snapshot.risk_rows)
            or receipt_payload.get("copied_risk_metadata_count") != len(snapshot.risk_metadata)
            or receipt_payload.get("archived_command_count") != snapshot.command_count
            or receipt_payload.get("archived_resolution_count") != snapshot.resolution_count
            or receipt_payload.get("risk_stop_preserved") is not True
            or receipt_payload.get("active_queue_started_empty") is not True
        ):
            raise CutoverError("全新 v5 控制库切换回执未保持安全边界")
        protocol_row = connection.execute(
            "SELECT value FROM worker_meta WHERE key=?",
            (_PROTOCOL_META_KEY,),
        ).fetchone()
        if protocol_row is None:
            raise CutoverError("全新 v5 控制库缺少正式栅栏协议回执")
        try:
            target_protocol = _parse_protocol_receipt(
                str(protocol_row["value"])
            )
        except DatabaseFenceError as exc:
            raise CutoverError(str(exc)) from exc
        if (
            str(protocol_row["value"])
            != snapshot.protocol_receipt_payload
            or target_protocol != snapshot.protocol_receipt
            or target_protocol.protocol_sha
            != intent.fence_protocol_sha
            or target_protocol.protocol_installed_at
            != intent.protocol_installed_at
            or target_protocol.protocol_instance_id
            != intent.protocol_instance_id
            or target_protocol.minimum_generation
            != intent.fence_generation - 1
        ):
            raise CutoverError("全新 v5 控制库正式栅栏协议回执不一致")
        _validate_one_new_risk_index(
            connection,
            label="全新 v5 控制库",
        )
        _validate_canonical_v5_schema(
            connection,
            label="全新 v5 控制库",
        )
        target_risk_rows = tuple(
            connection.execute("SELECT * FROM risk_runtime_state ORDER BY route_key").fetchall()
        )
        validated_risk_rows, target_route_keys = _validate_risk_rows(target_risk_rows)
        if validated_risk_rows != snapshot.risk_rows or target_route_keys != snapshot.route_keys:
            raise CutoverError("全新 v5 控制库没有原样保留风险停止")
        target_risk_metadata = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT key, value FROM worker_meta "
                "WHERE key GLOB 'risk_runtime_baseline:*' "
                "OR key GLOB 'risk_runtime_evidence:*' "
                "ORDER BY key"
            ).fetchall()
        )
        if target_risk_metadata != snapshot.risk_metadata:
            raise CutoverError("全新 v5 控制库没有原样保留风险元数据")
    finally:
        connection.close()


def _copy_prepared_target(
    target_path: Path,
    *,
    archive_directory: Path,
    intent: CutoverIntent,
    source_manifest_sha256: str,
) -> tuple[Path, str]:
    prepared_directory = archive_directory / "prepared"
    prepared_directory.mkdir(exist_ok=True)
    archived_target = prepared_directory / "execution_control.v5.sqlite3"
    _copy_file_atomic_exact(target_path, archived_target)
    target_hash = _sha256_file(target_path)
    if (
        archived_target.stat().st_size != target_path.stat().st_size
        or _sha256_file(archived_target) != target_hash
    ):
        raise CutoverError("准备好的 v5 数据库归档后哈希不一致")
    prepared_manifest = archive_directory / "prepared-target.json"
    prepared_payload = {
        "kind": "pa_agent_worker_v5_prepared_target",
        "cutover_id": intent.cutover_id,
        "created_at": intent.created_at,
        "deployed_sha": intent.deployed_sha,
        "fence_protocol_sha": intent.fence_protocol_sha,
        "protocol_installed_at": intent.protocol_installed_at,
        "protocol_instance_id": intent.protocol_instance_id,
        "plan_sha256": intent.plan_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "fence_generation": intent.fence_generation,
        "path": "prepared/execution_control.v5.sqlite3",
        "prepared_target_sha256": target_hash,
        "prepared_target_size": target_path.stat().st_size,
    }
    if prepared_manifest.exists():
        try:
            existing = json.loads(
                prepared_manifest.read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError) as exc:
            raise CutoverError("已有准备目标清单损坏") from exc
        if existing != prepared_payload:
            raise CutoverError("已有准备目标清单与耐久意图不一致")
    else:
        _write_json_atomic(prepared_manifest, prepared_payload)
    return archived_target, target_hash


def _set_restricted_acl(
    path: Path,
    *,
    current_user_writable: bool = False,
) -> None:
    if os.name != "nt":
        raise CutoverError("v4 审计档案 ACL 只支持 Windows")
    try:
        import ntsecuritycon
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
        access_by_sid = (
            (
                current_user,
                (
                    ntsecuritycon.FILE_ALL_ACCESS
                    if current_user_writable
                    else (
                        ntsecuritycon.FILE_GENERIC_READ
                        | ntsecuritycon.FILE_GENERIC_EXECUTE
                    )
                ),
            ),
            (system, ntsecuritycon.FILE_ALL_ACCESS),
            (administrators, ntsecuritycon.FILE_ALL_ACCESS),
        )
        objects = [path, *sorted(path.rglob("*"))]
        for candidate in objects:
            dacl = win32security.ACL()
            for sid, access_mask in access_by_sid:
                dacl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION,
                    0,
                    access_mask,
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
    except CutoverError:
        raise
    except Exception as exc:
        raise CutoverError("v4 审计档案 ACL 封存失败") from exc


def _seal_archive(path: Path) -> None:
    try:
        _verify_archive_security(path)
    except CutoverError:
        pass
    else:
        return
    _set_restricted_acl(path, current_user_writable=True)
    for candidate in path.rglob("*"):
        if candidate.is_file():
            candidate.chmod(stat.S_IREAD)
    _set_restricted_acl(path)


def _verify_archive_security(path: Path) -> None:
    if os.name != "nt":
        raise CutoverError("v4 审计档案 ACL 只支持 Windows")
    try:
        import ntsecuritycon
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
        expected_masks = {
            win32security.ConvertSidToStringSid(current_user): (
                ntsecuritycon.FILE_GENERIC_READ
                | ntsecuritycon.FILE_GENERIC_EXECUTE
            ),
            win32security.ConvertSidToStringSid(system): (
                ntsecuritycon.FILE_ALL_ACCESS
            ),
            win32security.ConvertSidToStringSid(administrators): (
                ntsecuritycon.FILE_ALL_ACCESS
            ),
        }
        for candidate in (path, *sorted(path.rglob("*"))):
            descriptor = win32security.GetNamedSecurityInfo(
                str(candidate),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
            )
            control, _revision = descriptor.GetSecurityDescriptorControl()
            if not control & win32security.SE_DACL_PROTECTED:
                raise CutoverError("v4 审计档案 ACL 仍继承外部权限")
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None or dacl.GetAceCount() != len(expected_masks):
                raise CutoverError("v4 审计档案 ACL 主体数量不正确")
            actual_masks: dict[str, int] = {}
            for index in range(dacl.GetAceCount()):
                header, mask, sid = dacl.GetAce(index)
                if header[0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
                    raise CutoverError("v4 审计档案 ACL 包含非白名单规则")
                sid_text = win32security.ConvertSidToStringSid(sid)
                if sid_text in actual_masks:
                    raise CutoverError("v4 审计档案 ACL 主体重复")
                actual_masks[sid_text] = int(mask)
            if actual_masks != expected_masks:
                raise CutoverError("v4 审计档案 ACL 权限不在白名单")
            if candidate.is_file() and not (
                candidate.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
            ):
                raise CutoverError("v4 审计档案文件不是只读")
    except CutoverError:
        raise
    except Exception as exc:
        raise CutoverError("v4 审计档案 ACL 无法复核") from exc


def verify_cutover_archive(
    archive_directory: Path,
) -> ArchiveVerification:
    """只读核验源数据库、真实 v5 副本、清单、ACL 和哈希绑定。"""

    archive = Path(archive_directory).resolve(strict=True)
    _verify_archive_security(archive)
    manifest_path = archive / "source-manifest.json"
    target_path = archive / "prepared-target.json"
    intent_path = archive / "cutover-intent.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        prepared = json.loads(target_path.read_text(encoding="utf-8"))
        intent_payload = json.loads(intent_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise CutoverError("v4 审计档案清单无法读取") from exc
    intent = _parse_cutover_intent(intent_payload)
    if (
        not isinstance(manifest, dict)
        or not isinstance(prepared, dict)
        or manifest.get("kind") != "pa_agent_worker_v4_audit_archive"
        or manifest.get("archive_schema_version") != _ARCHIVE_SCHEMA_VERSION
        or manifest.get("source_schema_version") != _SOURCE_SCHEMA_VERSION
        or manifest.get("target_schema_version") != _TARGET_SCHEMA_VERSION
        or prepared.get("kind") != "pa_agent_worker_v5_prepared_target"
        or prepared.get("cutover_id") != manifest.get("cutover_id")
        or manifest.get("cutover_id") != intent.cutover_id
        or manifest.get("deployed_sha") != intent.deployed_sha
        or prepared.get("deployed_sha") != intent.deployed_sha
        or manifest.get("fence_protocol_sha")
        != intent.fence_protocol_sha
        or prepared.get("fence_protocol_sha")
        != intent.fence_protocol_sha
        or manifest.get("protocol_installed_at")
        != intent.protocol_installed_at
        or prepared.get("protocol_installed_at")
        != intent.protocol_installed_at
        or manifest.get("protocol_instance_id")
        != intent.protocol_instance_id
        or prepared.get("protocol_instance_id")
        != intent.protocol_instance_id
        or manifest.get("fence_generation")
        != intent.fence_generation
        or prepared.get("fence_generation")
        != intent.fence_generation
        or manifest.get("plan_sha256") != intent.plan_sha256
        or prepared.get("plan_sha256") != intent.plan_sha256
        or manifest.get("cutover_intent_sha256")
        != _sha256_file(intent_path)
    ):
        raise CutoverError("v4 审计档案清单合同无效")
    manifest_sha256 = _sha256_file(manifest_path)
    if prepared.get("source_manifest_sha256") != manifest_sha256:
        raise CutoverError("v4 审计档案清单摘要不一致")
    source_files = manifest.get("source_files")
    if not isinstance(source_files, list):
        raise CutoverError("v4 审计档案没有源文件清单")
    expected_source_paths = {
        "source/execution_control.sqlite3",
        "source/execution.sqlite3",
    }
    seen_source_paths: set[str] = set()
    control_hash = ""
    execution_hash = ""
    control_size = 0
    execution_size = 0
    for entry in source_files:
        if not isinstance(entry, dict):
            raise CutoverError("v4 审计档案源文件条目无效")
        relative_text = str(entry.get("path") or "")
        if relative_text not in expected_source_paths or relative_text in seen_source_paths:
            raise CutoverError("v4 审计档案源文件集合不正确")
        seen_source_paths.add(relative_text)
        relative = PurePosixPath(relative_text)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise CutoverError("v4 审计档案源文件路径无效")
        source = (archive / Path(*relative.parts)).resolve(strict=True)
        if not _is_relative_to(source, archive):
            raise CutoverError("v4 审计档案源文件越过档案目录")
        expected_size = entry.get("size")
        expected_hash = str(entry.get("sha256") or "")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
            or source.stat().st_size != expected_size
            or _sha256_file(source) != expected_hash
        ):
            raise CutoverError("v4 审计档案源文件哈希不一致")
        if relative_text == "source/execution_control.sqlite3":
            control_hash = expected_hash
            control_size = expected_size
        elif relative_text == "source/execution.sqlite3":
            execution_hash = expected_hash
            execution_size = expected_size
    if seen_source_paths != expected_source_paths:
        raise CutoverError("v4 审计档案源文件集合不完整")
    if (
        control_hash != intent.source_database_sha256
        or control_size != intent.source_database_size
        or execution_hash != intent.source_execution_sha256
        or execution_size != intent.source_execution_size
    ):
        raise CutoverError("v4 审计档案与切换意图的源摘要不一致")

    prepared_path_text = str(prepared.get("path") or "")
    if prepared_path_text != "prepared/execution_control.v5.sqlite3":
        raise CutoverError("v4 审计档案准备目标路径无效")
    prepared_path = (archive / Path(*PurePosixPath(prepared_path_text).parts)).resolve(strict=True)
    if not _is_relative_to(prepared_path, archive):
        raise CutoverError("v4 审计档案准备目标越过档案目录")
    prepared_hash = str(prepared.get("prepared_target_sha256") or "")
    prepared_size = prepared.get("prepared_target_size")
    if (
        not control_hash
        or len(prepared_hash) != 64
        or any(character not in "0123456789abcdef" for character in prepared_hash)
        or isinstance(prepared_size, bool)
        or not isinstance(prepared_size, int)
        or prepared_size < 1
        or prepared_path.stat().st_size != prepared_size
        or _sha256_file(prepared_path) != prepared_hash
    ):
        raise CutoverError("v4 审计档案缺少控制库或准备目标摘要")

    actual_files = {
        candidate.relative_to(archive).as_posix()
        for candidate in archive.rglob("*")
        if candidate.is_file()
    }
    expected_files = expected_source_paths | {
        "prepared/execution_control.v5.sqlite3",
        "cutover-intent.json",
        "source-manifest.json",
        "prepared-target.json",
    }
    if actual_files != expected_files:
        raise CutoverError("v4 审计档案实际文件集合不正确")

    retained = manifest.get("retained_history")
    copied = manifest.get("copied_operational_state")
    if not isinstance(retained, dict) or not isinstance(copied, dict):
        raise CutoverError("v4 审计档案计数合同无效")
    if (
        retained.get("command_count")
        != intent.history_command_count
        or retained.get("resolution_count")
        != intent.history_resolution_count
        or retained.get("heartbeat_count")
        != intent.heartbeat_count
        or retained.get("duplicate_lease_group_count")
        != intent.duplicate_lease_group_count
        or copied.get("risk_state_count")
        != intent.risk_state_count
        or copied.get("risk_metadata_count")
        != intent.risk_metadata_count
    ):
        raise CutoverError("v4 审计档案计数未绑定原始切换计划")
    source_control = archive / "source" / "execution_control.sqlite3"
    source_connection = _open_immutable(source_control)
    try:
        _database_checks(source_connection, label="归档 v4 控制库")
        source_version = source_connection.execute(
            "SELECT value FROM worker_meta WHERE key='worker_schema_version'"
        ).fetchone()
        if source_version is None or str(source_version["value"]) != "4":
            raise CutoverError("归档控制库不是 schema v4")
        source_protocol_row = source_connection.execute(
            "SELECT value FROM worker_meta WHERE key=?",
            (_PROTOCOL_META_KEY,),
        ).fetchone()
        if source_protocol_row is None:
            raise CutoverError("归档 v4 控制库缺少正式栅栏协议回执")
        try:
            source_protocol = _parse_protocol_receipt(
                str(source_protocol_row["value"])
            )
        except DatabaseFenceError as exc:
            raise CutoverError(str(exc)) from exc
        if (
            source_protocol.protocol_sha != intent.fence_protocol_sha
            or source_protocol.protocol_installed_at
            != intent.protocol_installed_at
            or source_protocol.protocol_instance_id
            != intent.protocol_instance_id
            or source_protocol.minimum_generation
            != intent.fence_generation - 1
            or str(source_protocol_row["value"])
            != _protocol_receipt_payload(source_protocol)
        ):
            raise CutoverError("归档 v4 正式栅栏协议回执不一致")
        count_contract = {
            "command_count": "worker_commands",
            "resolution_count": "worker_command_resolutions",
            "heartbeat_count": "worker_heartbeats",
        }
        for field_name, table in count_contract.items():
            value = retained.get(field_name)
            actual = int(source_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if isinstance(value, bool) or not isinstance(value, int) or value != actual:
                raise CutoverError("归档 v4 历史计数不一致")
        archived_commands, archived_resolutions = (
            _validated_commands_and_resolutions(source_connection)
        )
        if any(
            command.status.value not in _TERMINAL_COMMAND_STATES
            for command in archived_commands
        ):
            raise CutoverError("归档 v4 包含非终态 Worker 命令")
        if int(
            source_connection.execute(
                "SELECT COUNT(*) FROM worker_new_risk_lease"
            ).fetchone()[0]
        ):
            raise CutoverError("归档 v4 仍有 NEW_RISK 租约")
        archived_duplicate_groups = int(
            source_connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT new_risk_lease_id
                    FROM worker_commands
                    WHERE new_risk_lease_id<>''
                      AND action IN ('submit', 'set_leverage')
                    GROUP BY new_risk_lease_id
                    HAVING COUNT(*)>1
                )
                """
            ).fetchone()[0]
        )
        if (
            archived_duplicate_groups
            != intent.duplicate_lease_group_count
            or archived_duplicate_groups < 1
        ):
            raise CutoverError("归档 v4 历史授权复用组数不一致")
        raw_archived_risk = tuple(
            source_connection.execute(
                "SELECT * FROM risk_runtime_state ORDER BY route_key"
            ).fetchall()
        )
        archived_risk_rows, archived_route_keys = _validate_risk_rows(
            raw_archived_risk
        )
        raw_archived_metadata = tuple(
            source_connection.execute(
                "SELECT key, value FROM worker_meta ORDER BY key"
            ).fetchall()
        )
        archived_risk_metadata = _validate_risk_metadata(
            raw_archived_metadata,
            route_keys=archived_route_keys,
        )
        if (
            len(archived_risk_rows) != intent.risk_state_count
            or len(archived_risk_metadata)
            != intent.risk_metadata_count
            or _canonical_rows_sha256(archived_risk_rows)
            != intent.risk_state_sha256
            or _risk_metadata_sha256(archived_risk_metadata)
            != intent.risk_metadata_sha256
        ):
            raise CutoverError("归档 v4 风险状态或元数据与计划不一致")
    finally:
        source_connection.close()

    source_execution = archive / "source" / "execution.sqlite3"
    execution_connection = _open_immutable(source_execution)
    try:
        _database_checks(execution_connection, label="归档 execution 账本")
        execution_version = execution_connection.execute(
            "SELECT value FROM execution_meta WHERE key='schema_version'"
        ).fetchone()
        if (
            execution_version is None
            or str(execution_version["value"]) != "2"
            or execution_connection.execute(
                "SELECT 1 FROM executions "
                "WHERE state NOT IN ('closed','blocked','canceled','rejected') "
                "LIMIT 1"
            ).fetchone()
            is not None
        ):
            raise CutoverError("归档 execution 账本不是安全终态 v2")
    finally:
        execution_connection.close()
    archived_paths = CutoverPaths(
        project_root=archive,
        control_db=source_control,
        execution_db=source_execution,
        gui_lock=archive / "unused-gui.lock",
        worker_lock=archive / "unused-worker.lock",
        campaign_lock=archive / "unused-campaign.lock",
        archive_root=archive,
        deployed_sha=intent.deployed_sha,
        fence_protocol_sha=intent.fence_protocol_sha,
        expected_plan_sha256=intent.plan_sha256,
    )
    _validate_execution_ledger(
        archived_paths,
        commands=archived_commands,
        resolutions=archived_resolutions,
        risk_rows=archived_risk_rows,
    )
    archived_snapshot = _SourceSnapshot(
        source_files=(
            _SourceFile(
                path=source_control,
                relative_path="source/execution_control.sqlite3",
                size=control_size,
                sha256=control_hash,
            ),
            _SourceFile(
                path=source_execution,
                relative_path="source/execution.sqlite3",
                size=execution_size,
                sha256=execution_hash,
            ),
        ),
        risk_rows=archived_risk_rows,
        risk_metadata=archived_risk_metadata,
        route_keys=archived_route_keys,
        command_count=len(archived_commands),
        resolution_count=len(archived_resolutions),
        heartbeat_count=int(retained["heartbeat_count"]),
        duplicate_lease_group_count=archived_duplicate_groups,
        commands=archived_commands,
        resolutions=archived_resolutions,
        protocol_receipt=source_protocol,
        protocol_receipt_payload=str(source_protocol_row["value"]),
    )
    previous_fence_state = DatabaseFenceState(
        generation=intent.fence_generation - 1,
        active=False,
        operation_id="",
        updated_at=intent.protocol_installed_at,
        protocol_sha=intent.fence_protocol_sha,
        protocol_installed_at=intent.protocol_installed_at,
        protocol_instance_id=intent.protocol_instance_id,
    )
    if (
        _cutover_plan_sha256(
            archived_paths,
            state=previous_fence_state,
            snapshot=archived_snapshot,
        )
        != intent.plan_sha256
    ):
        raise CutoverError("归档源事实无法重算出原始切换计划")

    target_connection = _open_immutable(prepared_path)
    try:
        _database_checks(target_connection, label="归档 v5 准备目标")
        target_version = target_connection.execute(
            "SELECT value FROM worker_meta WHERE key='worker_schema_version'"
        ).fetchone()
        if target_version is None or str(target_version["value"]) != "5":
            raise CutoverError("归档准备目标不是 schema v5")
        for table in (
            "worker_commands",
            "worker_command_resolutions",
            "worker_new_risk_lease",
            "worker_heartbeats",
        ):
            if int(target_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]):
                raise CutoverError("归档 v5 准备目标队列不是空的")
        if (
            int(target_connection.execute("SELECT COUNT(*) FROM risk_runtime_state").fetchone()[0])
            != copied.get("risk_state_count")
            or target_connection.execute(
                "SELECT 1 FROM risk_runtime_state " "WHERE kill_active<>1 OR kill_reason='' LIMIT 1"
            ).fetchone()
            is not None
        ):
            raise CutoverError("归档 v5 准备目标没有保留风险停止")
        receipts = target_connection.execute(
            "SELECT key, value FROM worker_meta "
            "WHERE key GLOB 'worker_safe_cutover:*'"
        ).fetchall()
        if (
            len(receipts) != 1
            or str(receipts[0]["key"])
            != f"worker_safe_cutover:{intent.cutover_id}"
        ):
            raise CutoverError("归档 v5 准备目标切换回执数量不正确")
        try:
            receipt = json.loads(str(receipts[0]["value"]))
        except (TypeError, ValueError) as exc:
            raise CutoverError("归档 v5 准备目标切换回执无效") from exc
        if (
            not isinstance(receipt, dict)
            or receipt.get("cutover_id") != manifest.get("cutover_id")
            or receipt.get("deployed_sha") != intent.deployed_sha
            or receipt.get("fence_protocol_sha")
            != intent.fence_protocol_sha
            or receipt.get("protocol_installed_at")
            != intent.protocol_installed_at
            or receipt.get("protocol_instance_id")
            != intent.protocol_instance_id
            or receipt.get("plan_sha256") != intent.plan_sha256
            or receipt.get("fence_generation")
            != intent.fence_generation
            or receipt.get("source_database_sha256") != control_hash
            or receipt.get("source_manifest_sha256") != manifest_sha256
            or receipt.get("archived_command_count") != retained.get("command_count")
            or receipt.get("archived_resolution_count") != retained.get("resolution_count")
            or receipt.get("copied_risk_state_count") != copied.get("risk_state_count")
            or receipt.get("active_queue_started_empty") is not True
            or receipt.get("risk_stop_preserved") is not True
        ):
            raise CutoverError("归档 v5 准备目标回执未绑定源档案")
        target_risk_rows = tuple(
            tuple(row)
            for row in target_connection.execute(
                "SELECT * FROM risk_runtime_state ORDER BY route_key"
            ).fetchall()
        )
        target_risk_metadata = tuple(
            tuple(row)
            for row in target_connection.execute(
                "SELECT key, value FROM worker_meta "
                "WHERE key GLOB 'risk_runtime_baseline:*' "
                "OR key GLOB 'risk_runtime_evidence:*' "
                "ORDER BY key"
            ).fetchall()
        )
        if (
            target_risk_rows
            != tuple(tuple(row) for row in archived_risk_rows)
            or target_risk_metadata
            != tuple(tuple(row) for row in archived_risk_metadata)
        ):
            raise CutoverError("归档 v5 没有逐字保留风险状态与元数据")
        _validate_one_new_risk_index(
            target_connection,
            label="归档 v5 准备目标",
        )
        _validate_canonical_v5_schema(
            target_connection,
            label="归档 v5 准备目标",
        )
        target_protocol_row = target_connection.execute(
            "SELECT value FROM worker_meta WHERE key=?",
            (_PROTOCOL_META_KEY,),
        ).fetchone()
        if target_protocol_row is None:
            raise CutoverError("归档 v5 准备目标缺少正式栅栏协议回执")
        try:
            target_protocol = _parse_protocol_receipt(
                str(target_protocol_row["value"])
            )
        except DatabaseFenceError as exc:
            raise CutoverError(str(exc)) from exc
        if (
            target_protocol != source_protocol
            or str(target_protocol_row["value"])
            != _protocol_receipt_payload(target_protocol)
        ):
            raise CutoverError("归档 v5 准备目标协议回执没有原样复制")
    finally:
        target_connection.close()

    return ArchiveVerification(
        valid=True,
        cutover_id=str(manifest["cutover_id"]),
        source_database_sha256=control_hash,
        source_execution_sha256=execution_hash,
        prepared_target_sha256=prepared_hash,
        source_manifest_sha256=manifest_sha256,
        deployed_sha=intent.deployed_sha,
        fence_protocol_sha=intent.fence_protocol_sha,
    )


def _cleanup_target(path: Path) -> None:
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    ):
        if candidate.exists():
            candidate.chmod(stat.S_IREAD | stat.S_IWRITE)
            candidate.unlink()


def _call_phase_hook(
    hook: Callable[[str, CutoverPhaseContext], None] | None,
    phase: str,
    context: CutoverPhaseContext,
) -> None:
    if hook is not None:
        hook(phase, context)


def _cutover_artifact_paths(
    paths: CutoverPaths,
    cutover_id: str,
) -> tuple[Path, Path, Path]:
    return (
        paths.control_db.with_name(
            f".{paths.control_db.name}.v5-{cutover_id}.tmp"
        ),
        paths.archive_root / f".preparing-{cutover_id}",
        paths.archive_root / f"v4-{cutover_id}",
    )


def _control_schema_version(path: Path) -> int:
    _require_zero_wal(path, label="控制库")
    connection = _open_immutable(path)
    try:
        row = connection.execute(
            "SELECT value FROM worker_meta "
            "WHERE key='worker_schema_version'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise CutoverError("控制库 schema 无法读取") from exc
    finally:
        connection.close()
    if row is None:
        raise CutoverError("控制库缺少 schema 版本")
    try:
        return int(str(row["value"]))
    except (TypeError, ValueError) as exc:
        raise CutoverError("控制库 schema 版本无效") from exc


def _intent_matches_paths(
    paths: CutoverPaths,
    intent: CutoverIntent,
) -> None:
    if (
        intent.deployed_sha != paths.deployed_sha
        or intent.fence_protocol_sha != paths.fence_protocol_sha
        or (
            paths.expected_plan_sha256
            and intent.plan_sha256 != paths.expected_plan_sha256
        )
    ):
        raise CutoverError("切换意图与当前部署 SHA 不一致")
    target_path, archive_temp, archive_final = _cutover_artifact_paths(
        paths,
        intent.cutover_id,
    )
    if (
        not _is_relative_to(target_path, paths.control_db.parent)
        or not _is_relative_to(archive_temp, paths.archive_root)
        or not _is_relative_to(archive_final, paths.archive_root)
    ):
        raise CutoverError("切换意图推导出的路径越界")


def _verify_source_matches_intent(
    paths: CutoverPaths,
    intent: CutoverIntent,
) -> None:
    for path, expected_size, expected_hash in (
        (
            paths.control_db,
            intent.source_database_size,
            intent.source_database_sha256,
        ),
        (
            paths.execution_db,
            intent.source_execution_size,
            intent.source_execution_sha256,
        ),
    ):
        _require_zero_wal(path, label=path.name)
        if (
            not path.is_file()
            or path.stat().st_size != expected_size
            or _sha256_file(path) != expected_hash
        ):
            raise CutoverError("活动 v4 与耐久切换意图的源摘要不一致")


@contextmanager
def _lifecycle_locks(
    paths: CutoverPaths,
    *,
    operation: str,
) -> Iterator[None]:
    gui_lock = FileLock(str(paths.gui_lock))
    worker_lock = FileLock(str(paths.worker_lock))
    try:
        gui_lock.acquire(timeout=0)
    except Timeout as exc:
        raise CutoverError(f"GUI 写入锁已被占用，禁止{operation}") from exc
    try:
        if active_windows_image_pids("pa-agent.exe"):
            raise CutoverError(f"PA_Agent 桌面进程仍在运行，禁止{operation}")
        try:
            worker_lock.acquire(timeout=0)
        except Timeout as exc:
            raise CutoverError(
                f"Worker 单例锁已被占用，禁止{operation}"
            ) from exc
        try:
            from pa_agent.okx_demo_campaign import (
                CampaignError,
                CampaignProcessLock,
            )

            try:
                with CampaignProcessLock(paths.campaign_lock):
                    if active_windows_image_pids("pa-agent.exe"):
                        raise CutoverError(
                            f"{operation}期间出现 PA_Agent 桌面进程"
                        )
                    yield
            except CampaignError as exc:
                raise CutoverError(
                    f"Campaign 单例锁已被占用，禁止{operation}"
                ) from exc
        finally:
            worker_lock.release()
    finally:
        gui_lock.release()


def _validate_intent_against_fence(
    intent: CutoverIntent,
    state: DatabaseFenceState,
    *,
    active: bool,
) -> None:
    if (
        state.active is not active
        or state.generation != intent.fence_generation
        or (
            state.operation_id != intent.cutover_id
            if active
            else state.operation_id != ""
        )
        or state.protocol_sha != intent.fence_protocol_sha
        or state.protocol_installed_at != intent.protocol_installed_at
        or state.protocol_instance_id != intent.protocol_instance_id
    ):
        raise CutoverError("耐久切换意图与数据库维护栅栏不一致")


def _completion_record(
    paths: CutoverPaths,
    intent: CutoverIntent,
    *,
    archived_intent: Path,
) -> None:
    intent_path = _intent_path(paths)
    completion_path = _completion_path(paths)
    if completion_path.exists():
        completed = _read_cutover_intent(completion_path)
        if completed != intent:
            raise CutoverError("已有切换完成记录与本次操作不一致")
        if intent_path.exists():
            raise CutoverError("切换意图与完成记录同时存在")
        return
    if intent_path.exists():
        if _read_cutover_intent(intent_path) != intent:
            raise CutoverError("待归档切换意图已变化")
        os.replace(intent_path, completion_path)
    else:
        _copy_file_atomic_exact(archived_intent, completion_path)
    if _read_cutover_intent(completion_path) != intent:
        raise CutoverError("切换完成记录落盘后不一致")


def _validate_empty_v5_target(path: Path) -> None:
    _require_zero_wal(path, label="全新 v5 临时库")
    connection = _open_immutable(path)
    try:
        _database_checks(connection, label="全新 v5 临时库")
        tables = frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        )
        if tables != _EXPECTED_CONTROL_TABLES:
            raise CutoverError("全新 v5 临时库表集合不正确")
        version = connection.execute(
            "SELECT value FROM worker_meta "
            "WHERE key='worker_schema_version'"
        ).fetchone()
        if version is None or str(version["value"]) != "5":
            raise CutoverError("全新 v5 临时库 schema 不是 v5")
        for table in (
            "worker_commands",
            "worker_command_resolutions",
            "worker_new_risk_lease",
            "worker_heartbeats",
            "risk_runtime_state",
        ):
            if int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            ):
                raise CutoverError("全新 v5 临时库不是空库")
        metadata = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT key, value FROM worker_meta ORDER BY key"
            ).fetchall()
        )
        if metadata != (("worker_schema_version", "5"),):
            raise CutoverError("全新 v5 临时库已有运行态元数据")
        _validate_one_new_risk_index(
            connection,
            label="全新 v5 临时库",
        )
        _validate_canonical_v5_schema(
            connection,
            label="全新 v5 临时库",
        )
    finally:
        connection.close()


def _new_cutover_intent(
    paths: CutoverPaths,
    *,
    cutover_id: str,
    created_at: str,
    state: DatabaseFenceState,
    snapshot: _SourceSnapshot,
    plan_sha256: str,
) -> CutoverIntent:
    execution_source = _execution_source_file(snapshot)
    return CutoverIntent(
        cutover_id=cutover_id,
        created_at=created_at,
        deployed_sha=paths.deployed_sha,
        fence_protocol_sha=paths.fence_protocol_sha,
        fence_generation=state.generation + 1,
        protocol_installed_at=state.protocol_installed_at,
        protocol_instance_id=state.protocol_instance_id,
        source_database_sha256=snapshot.control_database.sha256,
        source_database_size=snapshot.control_database.size,
        source_execution_sha256=execution_source.sha256,
        source_execution_size=execution_source.size,
        risk_state_count=len(snapshot.risk_rows),
        risk_metadata_count=len(snapshot.risk_metadata),
        history_command_count=snapshot.command_count,
        history_resolution_count=snapshot.resolution_count,
        heartbeat_count=snapshot.heartbeat_count,
        duplicate_lease_group_count=(
            snapshot.duplicate_lease_group_count
        ),
        risk_state_sha256=_canonical_rows_sha256(snapshot.risk_rows),
        risk_metadata_sha256=_risk_metadata_sha256(
            snapshot.risk_metadata
        ),
        plan_sha256=plan_sha256,
    )


def _atomically_start_cutover_maintenance(
    paths: CutoverPaths,
    *,
    cutover_id: str,
    created_at: str,
    phase_hook: Callable[[str, CutoverPhaseContext], None] | None,
) -> tuple[
    _SourceSnapshot,
    CutoverIntent,
    DatabaseMaintenanceLease,
]:
    """同一 global 锁内完成最终源核验、intent 落盘和 active 提升。"""

    fence = DatabaseWriteFence(paths.control_db)
    lock = FileLock(str(fence.lock_path))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise CutoverError("执行数据库仍有写入者，禁止切换") from exc
    try:
        state = fence.require_protocol(
            deployed_sha=paths.fence_protocol_sha
        )
        if state.active:
            raise CutoverError("执行数据库存在未完成维护，必须先恢复")
        _checkpoint_and_remove_sidecars(
            paths.control_db,
            label="v4 控制库",
        )
        _checkpoint_and_remove_sidecars(
            paths.execution_db,
            label="execution 账本",
        )
        snapshot = _capture_source_snapshot(
            paths,
            expected_protocol_state=state,
        )
        plan_sha256 = _cutover_plan_sha256(
            paths,
            state=state,
            snapshot=snapshot,
        )
        if plan_sha256 != paths.expected_plan_sha256:
            raise CutoverError("当前源数据库与已确认切换计划不一致")
        intent = _new_cutover_intent(
            paths,
            cutover_id=cutover_id,
            created_at=created_at,
            state=state,
            snapshot=snapshot,
            plan_sha256=plan_sha256,
        )
        intent_path = _intent_path(paths)
        if intent_path.exists():
            raise CutoverError("切换意图已经存在，必须使用恢复入口")
        _write_json_atomic(intent_path, _intent_payload(intent))
        target_path, archive_temp, _archive_final = (
            _cutover_artifact_paths(paths, cutover_id)
        )
        _call_phase_hook(
            phase_hook,
            "intent_prepared",
            CutoverPhaseContext(
                cutover_id=cutover_id,
                archive_directory=archive_temp,
                prepared_target=target_path,
            ),
        )
        active_state = DatabaseFenceState(
            generation=state.generation + 1,
            active=True,
            operation_id=cutover_id,
            updated_at=datetime.now(UTC).isoformat(
                timespec="microseconds"
            ),
            protocol_sha=state.protocol_sha,
            protocol_installed_at=state.protocol_installed_at,
            protocol_instance_id=state.protocol_instance_id,
        )
        _write_state(fence.state_path, active_state)
        if fence.state() != active_state:
            raise CutoverError("数据库维护栅栏原子提升后不一致")
        return (
            snapshot,
            intent,
            DatabaseMaintenanceLease(
                database_path=paths.control_db,
                lock=lock,
                state_path=fence.state_path,
                state=active_state,
            ),
        )
    except Exception:
        lock.release()
        raise


def _atomically_resume_premaintenance_intent(
    paths: CutoverPaths,
    *,
    intent: CutoverIntent,
) -> tuple[_SourceSnapshot, DatabaseMaintenanceLease]:
    """恢复 intent 已落盘、active 尚未提升的唯一崩溃窗口。"""

    fence = DatabaseWriteFence(paths.control_db)
    lock = FileLock(str(fence.lock_path))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise CutoverError("执行数据库仍有写入者，禁止恢复") from exc
    try:
        state = fence.require_protocol(
            deployed_sha=paths.fence_protocol_sha
        )
        if (
            state.active
            or state.generation != intent.fence_generation - 1
            or state.operation_id
            or state.protocol_sha != intent.fence_protocol_sha
            or state.protocol_installed_at
            != intent.protocol_installed_at
            or state.protocol_instance_id
            != intent.protocol_instance_id
        ):
            raise CutoverError("维护前栅栏与切换意图不一致")
        _checkpoint_and_remove_sidecars(
            paths.control_db,
            label="v4 控制库",
        )
        _checkpoint_and_remove_sidecars(
            paths.execution_db,
            label="execution 账本",
        )
        _verify_source_matches_intent(paths, intent)
        snapshot = _capture_source_snapshot(
            paths,
            expected_protocol_state=state,
        )
        _validate_snapshot_against_intent(snapshot, intent)
        if (
            _cutover_plan_sha256(
                paths,
                state=state,
                snapshot=snapshot,
            )
            != intent.plan_sha256
        ):
            raise CutoverError("恢复源与原始切换计划不一致")
        active_state = DatabaseFenceState(
            generation=intent.fence_generation,
            active=True,
            operation_id=intent.cutover_id,
            updated_at=datetime.now(UTC).isoformat(
                timespec="microseconds"
            ),
            protocol_sha=state.protocol_sha,
            protocol_installed_at=state.protocol_installed_at,
            protocol_instance_id=state.protocol_instance_id,
        )
        _write_state(fence.state_path, active_state)
        if fence.state() != active_state:
            raise CutoverError("数据库维护栅栏恢复提升后不一致")
        return (
            snapshot,
            DatabaseMaintenanceLease(
                database_path=paths.control_db,
                lock=lock,
                state_path=fence.state_path,
                state=active_state,
            ),
        )
    except Exception:
        lock.release()
        raise


def _validate_archive_binding(
    archive_directory: Path,
    *,
    verification: ArchiveVerification,
    snapshot: _SourceSnapshot,
    intent: CutoverIntent,
) -> None:
    archived_intent = _read_cutover_intent(
        archive_directory / "cutover-intent.json"
    )
    execution_source = _execution_source_file(snapshot)
    if (
        archived_intent != intent
        or verification.cutover_id != intent.cutover_id
        or verification.source_database_sha256
        != snapshot.control_database.sha256
        or verification.source_execution_sha256
        != execution_source.sha256
        or verification.deployed_sha != intent.deployed_sha
        or verification.fence_protocol_sha
        != intent.fence_protocol_sha
    ):
        raise CutoverError("v4 审计档案与当前切换意图或源快照不一致")


def _continue_v4_cutover(
    paths: CutoverPaths,
    *,
    snapshot: _SourceSnapshot,
    intent: CutoverIntent,
    maintenance: DatabaseMaintenanceLease,
    phase_hook: Callable[[str, CutoverPhaseContext], None] | None,
) -> CutoverResult:
    target_path, archive_temp, archive_final = _cutover_artifact_paths(
        paths,
        intent.cutover_id,
    )
    context = CutoverPhaseContext(
        cutover_id=intent.cutover_id,
        archive_directory=archive_temp,
        prepared_target=target_path,
    )
    if not target_path.is_file():
        raise CutoverError("未完成切换缺少维护开始前建立的 v5 临时库")
    _validate_snapshot_against_intent(snapshot, intent)
    _verify_source_matches_intent(paths, intent)
    _call_phase_hook(phase_hook, "databases_checkpointed", context)
    _call_phase_hook(phase_hook, "source_validated", context)

    if archive_final.exists():
        if archive_temp.exists():
            raise CutoverError("正式档案与准备档案同时存在")
        verification = verify_cutover_archive(archive_final)
        _validate_archive_binding(
            archive_final,
            verification=verification,
            snapshot=snapshot,
            intent=intent,
        )
        manifest_hash = verification.source_manifest_sha256
        target_hash = verification.prepared_target_sha256
        _validate_prepared_target(
            target_path,
            snapshot=snapshot,
            intent=intent,
            source_manifest_sha256=manifest_hash,
        )
    else:
        paths.archive_root.mkdir(parents=True, exist_ok=True)
        archive_temp.mkdir(exist_ok=True)
        manifest_path = _copy_source_archive(
            snapshot,
            intent_path=_intent_path(paths),
            intent=intent,
            archive_directory=archive_temp,
        )
        manifest_hash = _sha256_file(manifest_path)
        _verify_source_unchanged(snapshot)
        _insert_target_state(
            target_path,
            snapshot=snapshot,
            intent=intent,
            source_manifest_sha256=manifest_hash,
        )
        _validate_prepared_target(
            target_path,
            snapshot=snapshot,
            intent=intent,
            source_manifest_sha256=manifest_hash,
        )
        _archived_target, target_hash = _copy_prepared_target(
            target_path,
            archive_directory=archive_temp,
            intent=intent,
            source_manifest_sha256=manifest_hash,
        )
        _call_phase_hook(phase_hook, "target_prepared", context)
        _seal_archive(archive_temp)
        verification = verify_cutover_archive(archive_temp)
        _validate_archive_binding(
            archive_temp,
            verification=verification,
            snapshot=snapshot,
            intent=intent,
        )
        if (
            verification.prepared_target_sha256 != target_hash
            or verification.source_manifest_sha256 != manifest_hash
        ):
            raise CutoverError("v4 审计档案与耐久切换意图不一致")
        _call_phase_hook(phase_hook, "archive_sealed", context)
        os.replace(archive_temp, archive_final)
        context = CutoverPhaseContext(
            cutover_id=intent.cutover_id,
            archive_directory=archive_final,
            prepared_target=target_path,
        )

    verification = verify_cutover_archive(archive_final)
    _validate_archive_binding(
        archive_final,
        verification=verification,
        snapshot=snapshot,
        intent=intent,
    )
    if (
        verification.prepared_target_sha256 != target_hash
        or verification.source_manifest_sha256 != manifest_hash
    ):
        raise CutoverError("正式档案的 v5 目标摘要不一致")
    _validate_prepared_target(
        target_path,
        snapshot=snapshot,
        intent=intent,
        source_manifest_sha256=manifest_hash,
    )
    _call_phase_hook(phase_hook, "archive_published", context)
    if active_windows_image_pids("pa-agent.exe"):
        raise CutoverError("交换前出现 PA_Agent 桌面进程")
    _verify_source_unchanged(snapshot)
    if _sha256_file(target_path) != target_hash:
        raise CutoverError("待激活 v5 数据库在交换前发生变化")
    _call_phase_hook(phase_hook, "before_swap", context)

    os.replace(target_path, paths.control_db)
    _call_phase_hook(phase_hook, "swapped", context)
    if _sha256_file(paths.control_db) != target_hash:
        raise CutoverError("原子替换后的 v5 控制库哈希不一致")
    _validate_prepared_target(
        paths.control_db,
        snapshot=snapshot,
        intent=intent,
        source_manifest_sha256=manifest_hash,
    )
    final_verification = verify_cutover_archive(archive_final)
    if final_verification.prepared_target_sha256 != target_hash:
        raise CutoverError("切换后的档案复核不一致")
    _call_phase_hook(phase_hook, "post_swap_validated", context)
    _verify_completed_v5(
        paths,
        intent=intent,
        archive_directory=archive_final,
    )
    if active_windows_image_pids("pa-agent.exe"):
        raise CutoverError("完成维护前出现 PA_Agent 桌面进程")
    result = CutoverResult(
        cutover_id=intent.cutover_id,
        archive_directory=archive_final,
        source_database_sha256=snapshot.control_database.sha256,
        prepared_target_sha256=target_hash,
        risk_state_count=len(snapshot.risk_rows),
        history_command_count=snapshot.command_count,
    )
    _completion_record(
        paths,
        intent,
        archived_intent=archive_final / "cutover-intent.json",
    )
    _call_phase_hook(phase_hook, "completion_recorded", context)
    maintenance.finish()
    return result


def _canonical_database_value(value: object) -> tuple[str, str]:
    if value is None:
        return ("null", "")
    if isinstance(value, bytes):
        return ("blob", value.hex())
    if isinstance(value, bool):
        return ("integer", str(int(value)))
    if isinstance(value, int):
        return ("integer", str(value))
    if isinstance(value, float):
        return ("real", repr(value))
    return ("text", str(value))


def _normalized_v5_logical_snapshot(
    connection: sqlite3.Connection,
    *,
    intent: CutoverIntent,
    label: str,
) -> tuple[
    tuple[tuple[str, str, str, str], ...],
    tuple[
        tuple[
            str,
            tuple[str, ...],
            tuple[tuple[tuple[str, str], ...], ...],
        ],
        ...,
    ],
]:
    schema_rows = tuple(
        (
            str(row["type"]),
            str(row["name"]),
            str(row["tbl_name"]),
            "" if row["sql"] is None else str(row["sql"]),
        )
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql "
            "FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    )
    table_names = tuple(
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' ORDER BY name"
        ).fetchall()
    )
    snapshots: list[
        tuple[
            str,
            tuple[str, ...],
            tuple[tuple[tuple[str, str], ...], ...],
        ]
    ] = []
    protocol_rows = 0
    for table_name in table_names:
        if "\x00" in table_name:
            raise CutoverError(f"{label}包含无效表名")
        quoted_table = '"' + table_name.replace('"', '""') + '"'
        columns = tuple(
            str(row["name"])
            for row in connection.execute(
                f"PRAGMA table_info({quoted_table})"
            ).fetchall()
        )
        raw_rows = connection.execute(
            f"SELECT * FROM {quoted_table}"
        ).fetchall()
        normalized_rows: list[tuple[tuple[str, str], ...]] = []
        key_index = (
            columns.index("key")
            if table_name == "worker_meta" and "key" in columns
            else None
        )
        value_index = (
            columns.index("value")
            if table_name == "worker_meta" and "value" in columns
            else None
        )
        for raw_row in raw_rows:
            values = list(tuple(raw_row))
            if (
                key_index is not None
                and value_index is not None
                and values[key_index] == _PROTOCOL_META_KEY
            ):
                protocol_rows += 1
                try:
                    receipt = _parse_protocol_receipt(
                        str(values[value_index])
                    )
                except DatabaseFenceError as exc:
                    raise CutoverError(str(exc)) from exc
                if (
                    receipt.protocol_sha != intent.fence_protocol_sha
                    or receipt.protocol_installed_at
                    != intent.protocol_installed_at
                    or receipt.protocol_instance_id
                    != intent.protocol_instance_id
                    or receipt.minimum_generation
                    not in {
                        intent.fence_generation - 1,
                        intent.fence_generation,
                    }
                ):
                    raise CutoverError(
                        f"{label}正式栅栏协议回执不一致"
                    )
                values[value_index] = "__verified_protocol_receipt__"
            normalized_rows.append(
                tuple(_canonical_database_value(value) for value in values)
            )
        snapshots.append(
            (
                table_name,
                columns,
                tuple(sorted(normalized_rows)),
            )
        )
    if protocol_rows != 1:
        raise CutoverError(f"{label}正式栅栏协议回执数量不正确")
    return schema_rows, tuple(snapshots)


def _verify_completed_v5(
    paths: CutoverPaths,
    *,
    intent: CutoverIntent,
    archive_directory: Path,
) -> ArchiveVerification:
    verification = verify_cutover_archive(archive_directory)
    if (
        verification.cutover_id != intent.cutover_id
        or verification.deployed_sha != intent.deployed_sha
        or verification.fence_protocol_sha
        != intent.fence_protocol_sha
        or verification.source_database_sha256
        != intent.source_database_sha256
        or verification.source_execution_sha256
        != intent.source_execution_sha256
        or _sha256_file(paths.execution_db)
        != intent.source_execution_sha256
    ):
        raise CutoverError("活动 v5、历史档案与耐久切换意图不一致")
    connection = _open_immutable(paths.control_db)
    archived = _open_immutable(
        archive_directory / "prepared" / "execution_control.v5.sqlite3"
    )
    try:
        _database_checks(connection, label="活动 v5 控制库")
        for table in (
            "worker_commands",
            "worker_command_resolutions",
            "worker_new_risk_lease",
            "worker_heartbeats",
        ):
            if int(connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]):
                raise CutoverError("活动 v5 在恢复完成前出现了新队列状态")
        active_risk = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM risk_runtime_state ORDER BY route_key"
            ).fetchall()
        )
        archived_risk = tuple(
            tuple(row)
            for row in archived.execute(
                "SELECT * FROM risk_runtime_state ORDER BY route_key"
            ).fetchall()
        )
        if active_risk != archived_risk:
            raise CutoverError("活动 v5 风险停止与准备目标不一致")
        active_metadata = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT key, value FROM worker_meta "
                "WHERE key GLOB 'risk_runtime_baseline:*' "
                "OR key GLOB 'risk_runtime_evidence:*' "
                "ORDER BY key"
            ).fetchall()
        )
        archived_metadata = tuple(
            tuple(row)
            for row in archived.execute(
                "SELECT key, value FROM worker_meta "
                "WHERE key GLOB 'risk_runtime_baseline:*' "
                "OR key GLOB 'risk_runtime_evidence:*' "
                "ORDER BY key"
            ).fetchall()
        )
        if active_metadata != archived_metadata:
            raise CutoverError("活动 v5 风险元数据与准备目标不一致")
        active_cutover = tuple(
            tuple(row)
            for row in connection.execute(
            "SELECT key, value FROM worker_meta "
            "WHERE key GLOB 'worker_safe_cutover:*'"
            ).fetchall()
        )
        archived_cutover = tuple(
            tuple(row)
            for row in archived.execute(
            "SELECT key, value FROM worker_meta "
            "WHERE key GLOB 'worker_safe_cutover:*'"
            ).fetchall()
        )
        if active_cutover != archived_cutover:
            raise CutoverError("活动 v5 切换回执与准备目标不一致")
        protocol_row = connection.execute(
            "SELECT value FROM worker_meta WHERE key=?",
            (_PROTOCOL_META_KEY,),
        ).fetchone()
        if protocol_row is None:
            raise CutoverError("活动 v5 缺少正式栅栏协议回执")
        try:
            protocol = _parse_protocol_receipt(str(protocol_row["value"]))
        except DatabaseFenceError as exc:
            raise CutoverError(str(exc)) from exc
        if (
            protocol.protocol_sha != intent.fence_protocol_sha
            or protocol.protocol_installed_at
            != intent.protocol_installed_at
            or protocol.protocol_instance_id
            != intent.protocol_instance_id
            or protocol.minimum_generation
            not in {
                intent.fence_generation - 1,
                intent.fence_generation,
            }
        ):
            raise CutoverError("活动 v5 正式栅栏协议回执不一致")
        active_snapshot = _normalized_v5_logical_snapshot(
            connection,
            intent=intent,
            label="活动 v5 控制库",
        )
        archived_snapshot = _normalized_v5_logical_snapshot(
            archived,
            intent=intent,
            label="归档 v5 准备目标",
        )
        if active_snapshot != archived_snapshot:
            raise CutoverError(
                "活动 v5 与归档准备目标的完整结构或内容不一致"
            )
        if (
            protocol.minimum_generation
            == intent.fence_generation - 1
            and _sha256_file(paths.control_db)
            != verification.prepared_target_sha256
        ):
            raise CutoverError(
                "完成维护前的活动 v5 物理摘要与归档准备目标不一致"
            )
    finally:
        archived.close()
        connection.close()
    return verification


def audit_safe_v4_to_v5_cutover(paths: CutoverPaths) -> CutoverAudit:
    """只读核验例外切换硬门，不创建档案或目标数据库。"""

    validated = _validated_paths(paths)
    _verify_cutover_deployment(validated)
    if _intent_path(validated).exists():
        raise CutoverError("存在未完成切换意图，必须使用恢复入口")
    if _completion_path(validated).exists():
        raise CutoverError("已有切换完成记录，禁止再次切换")
    try:
        with _lifecycle_locks(validated, operation="核验"):
            fence = DatabaseWriteFence(validated.control_db)
            with fence.exclusive_check(
                deployed_sha=validated.fence_protocol_sha
            ):
                state = fence.require_protocol(
                    deployed_sha=validated.fence_protocol_sha
                )
                _require_zero_wal(
                    validated.control_db,
                    label="v4 控制库",
                )
                _require_zero_wal(
                    validated.execution_db,
                    label="execution 账本",
                )
                snapshot = _capture_source_snapshot(
                    validated,
                    expected_protocol_state=state,
                )
                plan_sha256 = _cutover_plan_sha256(
                    validated,
                    state=state,
                    snapshot=snapshot,
                )
    except DatabaseFenceError as exc:
        raise CutoverError(str(exc)) from exc
    return CutoverAudit(
        source_schema_version=_SOURCE_SCHEMA_VERSION,
        risk_state_count=len(snapshot.risk_rows),
        history_command_count=snapshot.command_count,
        history_resolution_count=snapshot.resolution_count,
        duplicate_lease_group_count=snapshot.duplicate_lease_group_count,
        risk_stop_active=True,
        source_database_sha256=snapshot.control_database.sha256,
        source_execution_sha256=(
            _execution_source_file(snapshot).sha256
        ),
        plan_sha256=plan_sha256,
    )


def perform_safe_v4_to_v5_cutover(
    paths: CutoverPaths,
    *,
    confirmation: str,
    clock: Callable[[], datetime] | None = None,
    _phase_hook: Callable[[str, CutoverPhaseContext], None] | None = None,
) -> CutoverResult:
    """执行一次显式、双 SHA 绑定、可崩溃恢复的 v4→v5 切换。"""

    if confirmation != SAFE_CUTOVER_CONFIRMATION:
        raise CutoverError("安全切换确认文本不匹配")
    validated = _validated_paths(paths)
    _verify_cutover_deployment(validated)
    if not validated.expected_plan_sha256:
        raise CutoverError("执行安全切换必须提供只读核验生成的计划摘要")
    now = (clock or (lambda: datetime.now(UTC)))()
    if now.tzinfo is None or now.utcoffset() is None:
        raise CutoverError("安全切换时钟必须带时区")
    created_at = now.astimezone(UTC).isoformat(timespec="microseconds")
    cutover_id = validated.expected_plan_sha256[:32]
    target_path, archive_temp, archive_final = _cutover_artifact_paths(
        validated,
        cutover_id,
    )
    intent_path = _intent_path(validated)
    completion_path = _completion_path(validated)
    maintenance: DatabaseMaintenanceLease | None = None
    intent: CutoverIntent | None = None
    snapshot: _SourceSnapshot | None = None
    created_target = False
    try:
        with _lifecycle_locks(validated, operation="切换"):
            if intent_path.exists():
                raise CutoverError("存在未完成切换意图，必须使用恢复入口")
            if completion_path.exists():
                raise CutoverError("已有切换完成记录，禁止再次切换")
            if archive_temp.exists() or archive_final.exists():
                raise CutoverError("本次安全切换档案路径已经存在")
            if validated.archive_root.exists() and any(
                validated.archive_root.glob(".preparing-*")
            ):
                raise CutoverError("存在其他未完成切换档案，必须先恢复")
            other_targets = tuple(
                candidate
                for candidate in validated.control_db.parent.glob(
                    f".{validated.control_db.name}.v5-*.tmp"
                )
                if candidate != target_path
            )
            if other_targets:
                raise CutoverError("存在其他未完成 v5 临时库，必须先复核")

            if target_path.exists():
                _validate_empty_v5_target(target_path)
            else:
                WorkerStore(
                    target_path,
                    worker_lock_path=validated.worker_lock,
                )
                created_target = True
                _checkpoint_and_remove_sidecars(
                    target_path,
                    label="全新 v5 临时库",
                )
                _validate_empty_v5_target(target_path)
            context = CutoverPhaseContext(
                cutover_id=cutover_id,
                archive_directory=archive_temp,
                prepared_target=target_path,
            )
            _call_phase_hook(
                _phase_hook,
                "target_schema_created",
                context,
            )
            try:
                snapshot, intent, maintenance = (
                    _atomically_start_cutover_maintenance(
                        validated,
                        cutover_id=cutover_id,
                        created_at=created_at,
                        phase_hook=_phase_hook,
                    )
                )
            except DatabaseFenceError as exc:
                raise CutoverError(str(exc)) from exc

            _validate_intent_against_fence(
                intent,
                maintenance.state,
                active=True,
            )
            result = _continue_v4_cutover(
                validated,
                snapshot=snapshot,
                intent=intent,
                maintenance=maintenance,
                phase_hook=_phase_hook,
            )
            return result
    except Exception as exc:
        try:
            active = DatabaseWriteFence(validated.control_db).state().active
        except DatabaseFenceError:
            active = True
        if active:
            raise CutoverError(
                "安全切换已进入耐久维护阶段并中断；维护栅栏保持开启，"
                "必须使用同一切换编号恢复"
            ) from exc
        if intent_path.exists():
            try:
                durable_intent = _read_cutover_intent(intent_path)
            except CutoverError as intent_exc:
                raise CutoverError(
                    "安全切换意图已落盘但无法复核，必须人工恢复"
                ) from intent_exc
            if durable_intent.cutover_id != cutover_id:
                raise CutoverError(
                    "安全切换意图编号与当前计划不一致，必须人工恢复"
                ) from exc
            raise CutoverError(
                "安全切换意图已耐久落盘但维护尚未完成；"
                "必须使用同一切换编号恢复"
            ) from exc
        if created_target:
            try:
                _cleanup_target(target_path)
            except OSError as cleanup_exc:
                raise CutoverError(
                    "安全切换在维护开始前失败，临时库清理失败"
                ) from cleanup_exc
        if isinstance(exc, CutoverError):
            raise
        raise CutoverError("安全切换在维护开始前失败，v4 保持不变") from exc
    finally:
        if maintenance is not None:
            maintenance.release()


def recover_safe_v4_to_v5_cutover(
    paths: CutoverPaths,
    *,
    cutover_id: str,
    confirmation: str,
    _phase_hook: Callable[[str, CutoverPhaseContext], None] | None = None,
) -> CutoverRecoveryResult:
    """只按耐久意图恢复同一切换；不自动回退已经安装的 v5。"""

    if confirmation != SAFE_CUTOVER_RECOVERY_CONFIRMATION:
        raise CutoverError("安全恢复确认文本不匹配")
    if (
        len(cutover_id) != 32
        or any(character not in "0123456789abcdef" for character in cutover_id)
    ):
        raise CutoverError("切换编号无效")
    validated = _validated_paths(paths)
    _verify_cutover_deployment(validated)
    if not validated.expected_plan_sha256:
        raise CutoverError("恢复安全切换必须提供原始计划摘要")
    target_path, archive_temp, archive_final = _cutover_artifact_paths(
        validated,
        cutover_id,
    )
    intent_path = _intent_path(validated)
    completion_path = _completion_path(validated)
    maintenance: DatabaseMaintenanceLease | None = None
    try:
        with _lifecycle_locks(validated, operation="恢复"):
            fence = DatabaseWriteFence(validated.control_db)
            try:
                state = fence.state()
            except DatabaseFenceError as exc:
                raise CutoverError(str(exc)) from exc
            if intent_path.exists():
                intent = _read_cutover_intent(intent_path)
            elif archive_final.exists():
                intent = _read_cutover_intent(
                    archive_final / "cutover-intent.json"
                )
            elif completion_path.exists():
                intent = _read_cutover_intent(completion_path)
            else:
                raise CutoverError("没有可恢复的耐久切换意图")
            if intent.cutover_id != cutover_id:
                raise CutoverError("请求恢复的切换编号与耐久意图不一致")
            _intent_matches_paths(validated, intent)

            if state.active:
                _validate_intent_against_fence(intent, state, active=True)
                try:
                    maintenance = fence.resume_maintenance(
                        operation_id=cutover_id,
                        deployed_sha=validated.fence_protocol_sha,
                    )
                except DatabaseFenceError as exc:
                    raise CutoverError(str(exc)) from exc
                _checkpoint_and_remove_sidecars(
                    validated.control_db,
                    label="活动控制库",
                )
                _checkpoint_and_remove_sidecars(
                    validated.execution_db,
                    label="execution 账本",
                )
                schema = _control_schema_version(validated.control_db)
                if schema == _SOURCE_SCHEMA_VERSION:
                    if not intent_path.exists():
                        raise CutoverError("活动 v4 恢复缺少 records 切换意图")
                    _verify_source_matches_intent(validated, intent)
                    previous_state = DatabaseFenceState(
                        generation=intent.fence_generation - 1,
                        active=False,
                        operation_id="",
                        updated_at=intent.protocol_installed_at,
                        protocol_sha=intent.fence_protocol_sha,
                        protocol_installed_at=intent.protocol_installed_at,
                        protocol_instance_id=intent.protocol_instance_id,
                    )
                    snapshot = _capture_source_snapshot(
                        validated,
                        expected_protocol_state=previous_state,
                    )
                    _validate_snapshot_against_intent(snapshot, intent)
                    if (
                        _cutover_plan_sha256(
                            validated,
                            state=previous_state,
                            snapshot=snapshot,
                        )
                        != intent.plan_sha256
                    ):
                        raise CutoverError("恢复源与原始切换计划不一致")
                    result = _continue_v4_cutover(
                        validated,
                        snapshot=snapshot,
                        intent=intent,
                        maintenance=maintenance,
                        phase_hook=_phase_hook,
                    )
                    return CutoverRecoveryResult(
                        cutover_id=cutover_id,
                        active_schema_version=_TARGET_SCHEMA_VERSION,
                        fence_cleared=True,
                        archive_directory=result.archive_directory,
                    )
                if schema != _TARGET_SCHEMA_VERSION:
                    raise CutoverError("活动控制库不是可恢复的 v4 或 v5")
                if not archive_final.exists() or archive_temp.exists():
                    raise CutoverError("已交换 v5 缺少唯一正式审计档案")
                _verify_completed_v5(
                    validated,
                    intent=intent,
                    archive_directory=archive_final,
                )
                if active_windows_image_pids("pa-agent.exe"):
                    raise CutoverError("恢复完成前出现 PA_Agent 桌面进程")
                recovery_result = CutoverRecoveryResult(
                    cutover_id=cutover_id,
                    active_schema_version=_TARGET_SCHEMA_VERSION,
                    fence_cleared=True,
                    archive_directory=archive_final,
                )
                _completion_record(
                    validated,
                    intent,
                    archived_intent=(
                        archive_final / "cutover-intent.json"
                    ),
                )
                _call_phase_hook(
                    _phase_hook,
                    "completion_recorded",
                    CutoverPhaseContext(
                        cutover_id=cutover_id,
                        archive_directory=archive_final,
                        prepared_target=target_path,
                    ),
                )
                maintenance.finish()
                return recovery_result

            if (
                state.generation == intent.fence_generation - 1
                and not state.operation_id
            ):
                if (
                    state.protocol_sha != intent.fence_protocol_sha
                    or state.protocol_installed_at
                    != intent.protocol_installed_at
                    or state.protocol_instance_id
                    != intent.protocol_instance_id
                ):
                    raise CutoverError("维护前栅栏与切换意图不一致")
                if not intent_path.exists() or not target_path.is_file():
                    raise CutoverError("维护前恢复缺少意图或 v5 临时库")
                if _control_schema_version(
                    validated.control_db
                ) != _SOURCE_SCHEMA_VERSION:
                    raise CutoverError("维护前恢复的活动控制库不是 v4")
                try:
                    snapshot, maintenance = (
                        _atomically_resume_premaintenance_intent(
                            validated,
                            intent=intent,
                        )
                    )
                except DatabaseFenceError as exc:
                    raise CutoverError(str(exc)) from exc
                _validate_intent_against_fence(
                    intent,
                    maintenance.state,
                    active=True,
                )
                result = _continue_v4_cutover(
                    validated,
                    snapshot=snapshot,
                    intent=intent,
                    maintenance=maintenance,
                    phase_hook=_phase_hook,
                )
                return CutoverRecoveryResult(
                    cutover_id=cutover_id,
                    active_schema_version=_TARGET_SCHEMA_VERSION,
                    fence_cleared=True,
                    archive_directory=result.archive_directory,
                )

            _validate_intent_against_fence(intent, state, active=False)
            try:
                with fence.exclusive_check(
                    deployed_sha=validated.fence_protocol_sha
                ):
                    _checkpoint_and_remove_sidecars(
                        validated.control_db,
                        label="已完成 v5 控制库",
                    )
                    _checkpoint_and_remove_sidecars(
                        validated.execution_db,
                        label="execution 账本",
                    )
                    if (
                        _control_schema_version(validated.control_db)
                        != _TARGET_SCHEMA_VERSION
                    ):
                        raise CutoverError(
                            "栅栏未激活且控制库不是已完成 v5"
                        )
                    if not archive_final.exists() or archive_temp.exists():
                        raise CutoverError("已完成 v5 缺少唯一正式审计档案")
                    _verify_completed_v5(
                        validated,
                        intent=intent,
                        archive_directory=archive_final,
                    )
                    _completion_record(
                        validated,
                        intent,
                        archived_intent=(
                            archive_final / "cutover-intent.json"
                        ),
                    )
            except DatabaseFenceError as exc:
                raise CutoverError(str(exc)) from exc
            return CutoverRecoveryResult(
                cutover_id=cutover_id,
                active_schema_version=_TARGET_SCHEMA_VERSION,
                fence_cleared=True,
                archive_directory=archive_final,
            )
    except Exception as exc:
        if isinstance(exc, CutoverError):
            raise
        raise CutoverError("安全切换恢复失败，维护栅栏保持原状") from exc
    finally:
        if maintenance is not None:
            maintenance.release()


def _default_paths(
    *,
    deployed_sha: str,
    fence_protocol_sha: str,
    expected_plan_sha256: str = "",
) -> CutoverPaths:
    from pa_agent.config.paths import (
        EXECUTION_CONTROL_DB_PATH,
        EXECUTION_DB_PATH,
        EXECUTION_WORKER_LOCK_PATH,
        PROJECT_ROOT,
    )

    return CutoverPaths(
        project_root=PROJECT_ROOT,
        control_db=EXECUTION_CONTROL_DB_PATH,
        execution_db=EXECUTION_DB_PATH,
        gui_lock=PROJECT_ROOT / "records" / "execution_gui_writer.lock",
        worker_lock=EXECUTION_WORKER_LOCK_PATH,
        campaign_lock=PROJECT_ROOT / "records" / "okx_demo_campaign.lock",
        archive_root=(PROJECT_ROOT / "scratch" / "production-archives" / "worker-control"),
        deployed_sha=deployed_sha,
        fence_protocol_sha=fence_protocol_sha,
        expected_plan_sha256=expected_plan_sha256,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="封存历史 v4 控制库并建立全新 v5 控制库",
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--check", action="store_true")
    operation.add_argument("--confirm")
    operation.add_argument("--recover")
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--fence-protocol-sha", required=True)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--recovery-confirmation")
    args = parser.parse_args(argv)
    paths = _default_paths(
        deployed_sha=str(args.deployed_sha),
        fence_protocol_sha=str(args.fence_protocol_sha),
        expected_plan_sha256=str(args.plan_sha256 or ""),
    )
    if args.check:
        try:
            audit = audit_safe_v4_to_v5_cutover(paths)
        except CutoverError as exc:
            print(f"worker_control_cutover_blocked: {exc}", file=sys.stderr)
            return 2
        print(
            "worker_control_cutover_ready "
            f"schema={audit.source_schema_version} "
            f"risk_states={audit.risk_state_count} "
            f"archived_commands={audit.history_command_count} "
            f"duplicate_groups={audit.duplicate_lease_group_count} "
            f"plan_sha256={audit.plan_sha256} "
            "risk_stop_active=true"
        )
        return 0
    if args.recover is not None:
        try:
            recovery = recover_safe_v4_to_v5_cutover(
                paths,
                cutover_id=str(args.recover),
                confirmation=str(args.recovery_confirmation),
            )
        except CutoverError as exc:
            print(f"worker_control_cutover_blocked: {exc}", file=sys.stderr)
            return 2
        print(
            "worker_control_cutover_recovered "
            f"schema={recovery.active_schema_version} "
            "risk_stop_preserved=true queue_empty=true"
        )
        return 0
    try:
        result = perform_safe_v4_to_v5_cutover(
            paths,
            confirmation=str(args.confirm),
        )
    except CutoverError as exc:
        print(f"worker_control_cutover_blocked: {exc}", file=sys.stderr)
        return 2
    print(
        "worker_control_cutover_completed "
        f"schema={_TARGET_SCHEMA_VERSION} "
        f"risk_states={result.risk_state_count} "
        "risk_stop_preserved=true queue_empty=true"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
