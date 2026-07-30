"""生产执行数据库共享的跨进程写入栅栏。

旧数据库首次遇到新版 Store 时只会建立 ``auto-bootstrap`` 标记。正式
v4→v5 切换必须先在所有旧写进程退出后，把该标记升级为一个已提交的
完整 Git SHA。普通写入、维护切换和崩溃恢复随后都核对同一份持久状态。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout

_FENCE_VERSION = 2
_MARKER_VERSION = 1
_LOCK_NAME = "execution_databases.fence.lock"
_STATE_NAME = "execution_databases.fence.json"
_MARKER_NAME = "execution_databases.fence.initialized.json"
_RECOVERY_NAME = "execution_databases.fence.recovery.json"
_GUI_LOCK_NAME = "execution_gui_writer.lock"
_PROTOCOL_META_KEY = "worker_database_fence_protocol"
_AUTO_PROTOCOL = "auto-bootstrap"
_WRITE_TIMEOUT_SECONDS = 5.0
_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_HEX_32_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_UTC_TIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$"
)
_RECOVERY_SOURCE_KINDS = frozenset(
    {
        "legacy-bootstrap-v2",
        "current-bootstrap-v2",
        "formal-without-receipt-v2",
    }
)
_LOADED_SOURCE_SHA256 = hashlib.sha256(
    Path(__file__).read_bytes().replace(b"\r\n", b"\n")
).hexdigest()
_PROTOCOL_SOURCE_PATHS = (
    "pa_agent/execution/database_fence.py",
    "pa_agent/execution/store.py",
    "pa_agent/execution/worker_store.py",
    "pa_agent/main.py",
)


class DatabaseFenceError(RuntimeError):
    """数据库写入被维护状态、旧进程协议或损坏栅栏阻断。"""


@dataclass(frozen=True, slots=True)
class DatabaseFenceState:
    generation: int
    active: bool
    operation_id: str
    updated_at: str
    protocol_sha: str
    protocol_installed_at: str
    protocol_instance_id: str


@dataclass(frozen=True, slots=True)
class DatabaseFenceProtocolReceipt:
    protocol_sha: str
    protocol_installed_at: str
    protocol_instance_id: str
    minimum_generation: int


@dataclass(frozen=True, slots=True)
class DatabaseFenceMarker:
    instance_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class DatabaseFenceRecoveryIntent:
    deployed_sha: str
    prepared_at: str
    protocol_installed_at: str
    marker_created_at: str
    recovery_id: str
    protocol_instance_id: str
    source_state_sha256: str
    source_kind: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _paths(database_path: Path) -> tuple[Path, Path, Path]:
    directory = Path(database_path).resolve(strict=False).parent
    return (
        directory / _LOCK_NAME,
        directory / _STATE_NAME,
        directory / _MARKER_NAME,
    )


def _is_full_git_sha(value: str) -> bool:
    return _GIT_SHA_PATTERN.fullmatch(value) is not None


def _parse_aware_time(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise DatabaseFenceError(f"执行数据库维护栅栏 {field_name} 缺失")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DatabaseFenceError(f"执行数据库维护栅栏 {field_name} 无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DatabaseFenceError(f"执行数据库维护栅栏 {field_name} 缺少时区")
    return value


def _parse_canonical_utc_time(value: object, *, field_name: str) -> str:
    parsed = _parse_aware_time(value, field_name=field_name)
    parsed_datetime = datetime.fromisoformat(parsed)
    if (
        _CANONICAL_UTC_TIME_PATTERN.fullmatch(parsed) is None
        or parsed_datetime.year < 2000
        or parsed_datetime.year > 2100
    ):
        raise DatabaseFenceError(f"执行数据库维护栅栏 {field_name} 不是标准 UTC 时间")
    return parsed


def _read_state(path: Path) -> DatabaseFenceState | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise DatabaseFenceError("执行数据库维护栅栏损坏，禁止写入") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "generation",
        "active",
        "operation_id",
        "updated_at",
        "protocol_sha",
        "protocol_installed_at",
        "protocol_instance_id",
    }:
        raise DatabaseFenceError("执行数据库维护栅栏合同无效，禁止写入")
    generation = payload.get("generation")
    active = payload.get("active")
    operation_id = payload.get("operation_id")
    protocol_sha = payload.get("protocol_sha")
    protocol_instance_id = payload.get("protocol_instance_id")
    if (
        payload.get("version") != _FENCE_VERSION
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or not isinstance(active, bool)
        or not isinstance(operation_id, str)
        or not isinstance(protocol_sha, str)
        or not isinstance(protocol_instance_id, str)
        or len(protocol_instance_id) != 32
        or any(character not in "0123456789abcdef" for character in protocol_instance_id)
        or (protocol_sha not in {_AUTO_PROTOCOL} and not _is_full_git_sha(protocol_sha))
        or (active and (generation < 1 or not operation_id.strip()))
        or (not active and operation_id)
    ):
        raise DatabaseFenceError("执行数据库维护栅栏字段无效，禁止写入")
    return DatabaseFenceState(
        generation=generation,
        active=active,
        operation_id=operation_id,
        updated_at=_parse_aware_time(
            payload.get("updated_at"),
            field_name="更新时间",
        ),
        protocol_sha=protocol_sha,
        protocol_installed_at=_parse_aware_time(
            payload.get("protocol_installed_at"),
            field_name="协议安装时间",
        ),
        protocol_instance_id=protocol_instance_id,
    )


def _is_strict_legacy_bootstrap_state(path: Path) -> bool:
    """只识别正式协议部署前曾写出的无实例 bootstrap 合同。"""

    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "generation",
        "active",
        "operation_id",
        "updated_at",
        "protocol_sha",
        "protocol_installed_at",
    }:
        return False
    version = payload.get("version")
    generation = payload.get("generation")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != 2
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation != 0
        or payload.get("active") is not False
        or payload.get("operation_id") != ""
        or payload.get("protocol_sha") != _AUTO_PROTOCOL
        or payload.get("updated_at") != payload.get("protocol_installed_at")
    ):
        return False
    try:
        _parse_canonical_utc_time(
            payload.get("updated_at"),
            field_name="旧版更新时间",
        )
        _parse_canonical_utc_time(
            payload.get("protocol_installed_at"),
            field_name="旧版协议安装时间",
        )
    except DatabaseFenceError:
        return False
    return True


def _write_state(path: Path, state: DatabaseFenceState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = {
        "version": _FENCE_VERSION,
        "generation": state.generation,
        "active": state.active,
        "operation_id": state.operation_id,
        "updated_at": state.updated_at,
        "protocol_sha": state.protocol_sha,
        "protocol_installed_at": state.protocol_installed_at,
        "protocol_instance_id": state.protocol_instance_id,
    }
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
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_marker(path: Path) -> DatabaseFenceMarker | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise DatabaseFenceError("执行数据库栅栏初始化标记损坏") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "instance_id",
        "created_at",
    }:
        raise DatabaseFenceError("执行数据库栅栏初始化标记合同无效")
    instance_id = payload.get("instance_id")
    if (
        payload.get("version") != _MARKER_VERSION
        or not isinstance(instance_id, str)
        or len(instance_id) != 32
        or any(character not in "0123456789abcdef" for character in instance_id)
    ):
        raise DatabaseFenceError("执行数据库栅栏初始化标记字段无效")
    return DatabaseFenceMarker(
        instance_id=instance_id,
        created_at=_parse_aware_time(
            payload.get("created_at"),
            field_name="初始化标记时间",
        ),
    )


def _write_marker(path: Path, *, instance_id: str, created_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (
        json.dumps(
            {
                "version": _MARKER_VERSION,
                "instance_id": instance_id,
                "created_at": created_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise DatabaseFenceError("执行数据库栅栏初始化标记已经存在")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_recovery_intent(path: Path) -> DatabaseFenceRecoveryIntent | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise DatabaseFenceError("执行数据库栅栏恢复意图损坏，禁止写入") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "phase",
        "deployed_sha",
        "prepared_at",
        "protocol_installed_at",
        "marker_created_at",
        "recovery_id",
        "protocol_instance_id",
        "source_state_sha256",
        "source_kind",
    }:
        raise DatabaseFenceError("执行数据库栅栏恢复意图合同无效，禁止写入")
    deployed_sha = payload.get("deployed_sha")
    recovery_id = payload.get("recovery_id")
    protocol_instance_id = payload.get("protocol_instance_id")
    source_state_sha256 = payload.get("source_state_sha256")
    source_kind = payload.get("source_kind")
    if (
        isinstance(payload.get("version"), bool)
        or payload.get("version") != 1
        or payload.get("phase") != "prepared"
        or not isinstance(deployed_sha, str)
        or not _is_full_git_sha(deployed_sha)
        or not isinstance(recovery_id, str)
        or _HEX_32_PATTERN.fullmatch(recovery_id) is None
        or not isinstance(protocol_instance_id, str)
        or _HEX_32_PATTERN.fullmatch(protocol_instance_id) is None
        or not isinstance(source_state_sha256, str)
        or _HEX_64_PATTERN.fullmatch(source_state_sha256) is None
        or not isinstance(source_kind, str)
        or source_kind not in _RECOVERY_SOURCE_KINDS
    ):
        raise DatabaseFenceError("执行数据库栅栏恢复意图字段无效，禁止写入")
    return DatabaseFenceRecoveryIntent(
        deployed_sha=deployed_sha,
        prepared_at=_parse_canonical_utc_time(
            payload.get("prepared_at"),
            field_name="恢复准备时间",
        ),
        protocol_installed_at=_parse_canonical_utc_time(
            payload.get("protocol_installed_at"),
            field_name="恢复协议安装时间",
        ),
        marker_created_at=_parse_canonical_utc_time(
            payload.get("marker_created_at"),
            field_name="恢复标记创建时间",
        ),
        recovery_id=recovery_id,
        protocol_instance_id=protocol_instance_id,
        source_state_sha256=source_state_sha256,
        source_kind=source_kind,
    )


def _write_recovery_intent(
    path: Path,
    intent: DatabaseFenceRecoveryIntent,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (
        json.dumps(
            {
                "version": 1,
                "phase": "prepared",
                "deployed_sha": intent.deployed_sha,
                "prepared_at": intent.prepared_at,
                "protocol_installed_at": intent.protocol_installed_at,
                "marker_created_at": intent.marker_created_at,
                "recovery_id": intent.recovery_id,
                "protocol_instance_id": intent.protocol_instance_id,
                "source_state_sha256": intent.source_state_sha256,
                "source_kind": intent.source_kind,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise DatabaseFenceError("执行数据库栅栏恢复意图已经存在")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _state_file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise DatabaseFenceError("无法读取执行数据库栅栏状态") from exc


def _expected_recovery_state(
    intent: DatabaseFenceRecoveryIntent,
) -> DatabaseFenceState:
    return DatabaseFenceState(
        generation=0,
        active=False,
        operation_id="",
        updated_at=intent.protocol_installed_at,
        protocol_sha=intent.deployed_sha,
        protocol_installed_at=intent.protocol_installed_at,
        protocol_instance_id=intent.protocol_instance_id,
    )


def _protocol_database_path(database_path: Path) -> Path:
    directory = Path(database_path).resolve(strict=False).parent
    official_control = directory / "execution_control.sqlite3"
    return (
        official_control if official_control.exists() else Path(database_path).resolve(strict=False)
    )


def _worker_meta_exists(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_meta'"
        ).fetchone()
        is not None
    )


def _read_worker_schema_version_from_connection(
    connection: sqlite3.Connection,
) -> int | None:
    if not _worker_meta_exists(connection):
        return None
    row = connection.execute(
        "SELECT value FROM worker_meta WHERE key='worker_schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        version = int(str(row[0]))
    except (TypeError, ValueError) as exc:
        raise DatabaseFenceError("控制库 Worker schema 版本无效") from exc
    if str(version) != str(row[0]) or version < 1:
        raise DatabaseFenceError("控制库 Worker schema 版本无效")
    return version


def _parse_protocol_receipt(value: object) -> DatabaseFenceProtocolReceipt:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise DatabaseFenceError("控制库中的正式栅栏协议回执损坏") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "protocol_sha",
        "protocol_installed_at",
        "protocol_instance_id",
        "minimum_generation",
    }:
        raise DatabaseFenceError("控制库中的正式栅栏协议回执合同无效")
    protocol_sha = payload.get("protocol_sha")
    protocol_instance_id = payload.get("protocol_instance_id")
    minimum_generation = payload.get("minimum_generation")
    if (
        payload.get("version") != 2
        or not isinstance(protocol_sha, str)
        or not _is_full_git_sha(protocol_sha)
        or not isinstance(protocol_instance_id, str)
        or len(protocol_instance_id) != 32
        or any(character not in "0123456789abcdef" for character in protocol_instance_id)
        or isinstance(minimum_generation, bool)
        or not isinstance(minimum_generation, int)
        or minimum_generation < 0
    ):
        raise DatabaseFenceError("控制库中的正式栅栏协议回执字段无效")
    return DatabaseFenceProtocolReceipt(
        protocol_sha=protocol_sha,
        protocol_installed_at=_parse_aware_time(
            payload.get("protocol_installed_at"),
            field_name="协议回执安装时间",
        ),
        protocol_instance_id=protocol_instance_id,
        minimum_generation=minimum_generation,
    )


def _read_database_fence_metadata_from_connection(
    connection: sqlite3.Connection,
) -> tuple[DatabaseFenceProtocolReceipt | None, int | None]:
    if not _worker_meta_exists(connection):
        return None, None
    rows = connection.execute(
        "SELECT key, value FROM worker_meta "
        "WHERE key=? OR key GLOB 'worker_safe_cutover:*'",
        (_PROTOCOL_META_KEY,),
    ).fetchall()
    protocol_rows = [row for row in rows if str(row[0]) == _PROTOCOL_META_KEY]
    cutover_rows = [row for row in rows if str(row[0]).startswith("worker_safe_cutover:")]
    if len(protocol_rows) > 1:
        raise DatabaseFenceError("控制库中的正式栅栏协议回执数量不正确")
    protocol_receipt = None if not protocol_rows else _parse_protocol_receipt(protocol_rows[0][1])
    if not cutover_rows:
        return protocol_receipt, None
    if len(cutover_rows) != 1:
        raise DatabaseFenceError("活动控制库的安全切换回执数量不正确")
    try:
        cutover_receipt = json.loads(str(cutover_rows[0][1]))
    except (TypeError, ValueError) as exc:
        raise DatabaseFenceError("活动控制库的安全切换回执无效") from exc
    generation = (
        cutover_receipt.get("fence_generation") if isinstance(cutover_receipt, dict) else None
    )
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise DatabaseFenceError("活动控制库没有绑定有效栅栏代际")
    return protocol_receipt, generation


def _read_database_fence_metadata(
    database_path: Path,
) -> tuple[DatabaseFenceProtocolReceipt | None, int | None]:
    candidate = _protocol_database_path(database_path)
    if not candidate.is_file() or candidate.stat().st_size == 0:
        return None, None
    uri = f"{candidate.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            return _read_database_fence_metadata_from_connection(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise DatabaseFenceError("无法核对控制库中的执行数据库栅栏元数据") from exc


def _protocol_receipt_payload(receipt: DatabaseFenceProtocolReceipt) -> str:
    return json.dumps(
        {
            "version": 2,
            "protocol_sha": receipt.protocol_sha,
            "protocol_installed_at": receipt.protocol_installed_at,
            "protocol_instance_id": receipt.protocol_instance_id,
            "minimum_generation": receipt.minimum_generation,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _upsert_database_protocol_receipt(
    connection: sqlite3.Connection,
    receipt: DatabaseFenceProtocolReceipt,
) -> None:
    if not _worker_meta_exists(connection):
        raise DatabaseFenceError("正式控制数据库缺少 worker_meta，无法安装栅栏协议回执")
    payload = _protocol_receipt_payload(receipt)
    existing = connection.execute(
        "SELECT value FROM worker_meta WHERE key=?",
        (_PROTOCOL_META_KEY,),
    ).fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
            (_PROTOCOL_META_KEY, payload),
        )
        return
    existing_receipt = _parse_protocol_receipt(existing[0])
    if (
        existing_receipt.protocol_sha != receipt.protocol_sha
        or existing_receipt.protocol_installed_at != receipt.protocol_installed_at
        or existing_receipt.protocol_instance_id != receipt.protocol_instance_id
    ):
        raise DatabaseFenceError("控制库已经绑定另一个正式栅栏协议")
    if receipt.minimum_generation < existing_receipt.minimum_generation:
        raise DatabaseFenceError("禁止降低正式栅栏协议的最低代际")
    if receipt.minimum_generation > existing_receipt.minimum_generation:
        connection.execute(
            "UPDATE worker_meta SET value=? WHERE key=?",
            (payload, _PROTOCOL_META_KEY),
        )


def _write_database_protocol_receipt(
    database_path: Path,
    receipt: DatabaseFenceProtocolReceipt,
) -> None:
    candidate = _protocol_database_path(database_path)
    if not candidate.is_file():
        raise DatabaseFenceError("正式控制数据库不存在，无法安装栅栏协议回执")
    connection = sqlite3.connect(candidate, timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            _upsert_database_protocol_receipt(connection, receipt)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise DatabaseFenceError("正式栅栏协议回执写入控制数据库失败") from exc
    finally:
        connection.close()


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
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _verify_deployed_repository(*, project_root: Path, deployed_sha: str) -> None:
    clean_sha = deployed_sha.strip().lower()
    if not _is_full_git_sha(clean_sha):
        raise ValueError("栅栏部署 SHA 必须是 40 位小写 Git SHA")
    top_level = _run_git(project_root, ["rev-parse", "--show-toplevel"])
    if top_level.returncode != 0:
        raise DatabaseFenceError("项目根不是可核对的 Git 工作区")
    if Path(top_level.stdout.strip()).resolve(strict=True) != project_root:
        raise DatabaseFenceError("栅栏安装目录不是 Git 项目根")
    head = _run_git(project_root, ["rev-parse", "HEAD"])
    if head.returncode != 0 or head.stdout.strip().lower() != clean_sha:
        raise DatabaseFenceError("栅栏部署 SHA 与项目当前 HEAD 不一致")
    status = _run_git(
        project_root,
        ["status", "--porcelain=v1", "--untracked-files=no"],
    )
    if status.returncode != 0 or status.stdout.strip():
        raise DatabaseFenceError("项目存在未提交的受跟踪文件改动")
    for relative_path in _PROTOCOL_SOURCE_PATHS:
        committed = _run_git(
            project_root,
            ["show", f"{clean_sha}:{relative_path}"],
            text=False,
        )
        if committed.returncode != 0:
            raise DatabaseFenceError(f"栅栏提交缺少协议源码：{relative_path}")
        working_path = project_root / relative_path
        try:
            working_source = working_path.read_bytes()
        except OSError as exc:
            raise DatabaseFenceError(f"无法读取工作树协议源码：{relative_path}") from exc
        committed_digest = hashlib.sha256(committed.stdout.replace(b"\r\n", b"\n")).digest()
        working_digest = hashlib.sha256(working_source.replace(b"\r\n", b"\n")).digest()
        if committed_digest != working_digest:
            raise DatabaseFenceError(f"工作树协议源码与部署 SHA 不一致：{relative_path}")
        if (
            relative_path == "pa_agent/execution/database_fence.py"
            and committed_digest.hex() != _LOADED_SOURCE_SHA256
        ):
            raise DatabaseFenceError("当前进程加载的栅栏源码不属于部署 SHA")


def active_windows_image_pids(image_name: str) -> tuple[int, ...]:
    """只读列出指定 Windows 映像名；查询失败时直接阻断维护。"""

    if os.name != "nt":
        raise DatabaseFenceError("写入进程检查只支持 Windows")
    completed = subprocess.run(
        [
            "tasklist.exe",
            "/FI",
            f"IMAGENAME eq {image_name}",
            "/FO",
            "CSV",
            "/NH",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise DatabaseFenceError("无法核对旧版桌面写入进程")
    pids: list[int] = []
    for row in csv.reader(io.StringIO(completed.stdout)):
        if len(row) < 2 or row[0].strip().casefold() != image_name.casefold():
            continue
        try:
            pid = int(row[1].replace(",", "").strip())
        except ValueError as exc:
            raise DatabaseFenceError("桌面写入进程编号无法解析") from exc
        if pid > 0:
            pids.append(pid)
    return tuple(sorted(set(pids)))


class GuiDatabaseWriterProcessLock:
    """让 GUI 整个生命周期与正式栅栏安装互斥。"""

    def __init__(self, lock_path: Path | None = None) -> None:
        if lock_path is None:
            lock_path = Path(__file__).resolve().parents[2] / "records" / _GUI_LOCK_NAME
        self._lock = FileLock(str(Path(lock_path).resolve(strict=False)))
        self._acquired = False

    def __enter__(self) -> GuiDatabaseWriterProcessLock:
        Path(self._lock.lock_file).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=0)
        except Timeout as exc:
            raise DatabaseFenceError("GUI 写入锁已被占用，禁止启动") from exc
        self._acquired = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        if self._acquired:
            self._lock.release()
            self._acquired = False


def install_official_database_fence_protocol(
    *,
    project_root: Path,
    deployed_sha: str,
) -> DatabaseFenceState:
    """在 Worker、Campaign 和旧桌面均退出后安装正式协议标记。"""

    root = Path(project_root).resolve(strict=True)
    records = root / "records"
    control_path = records / "execution_control.sqlite3"
    gui_lock_path = records / _GUI_LOCK_NAME
    worker_lock_path = records / "execution_worker.lock"
    campaign_lock_path = records / "okx_demo_campaign.lock"
    if not control_path.is_file():
        raise DatabaseFenceError("正式控制数据库不存在")
    _verify_deployed_repository(project_root=root, deployed_sha=deployed_sha)
    clean_sha = deployed_sha.strip().lower()

    gui_lock = FileLock(str(gui_lock_path))
    worker_lock = FileLock(str(worker_lock_path))
    try:
        gui_lock.acquire(timeout=0)
    except Timeout as exc:
        raise DatabaseFenceError("GUI 写入锁已被占用") from exc
    try:
        if active_windows_image_pids("pa-agent.exe"):
            raise DatabaseFenceError("旧版 PA_Agent 桌面进程仍在运行")
        try:
            worker_lock.acquire(timeout=0)
        except Timeout as exc:
            raise DatabaseFenceError("Worker 单例锁已被占用") from exc
        try:
            from pa_agent.okx_demo_campaign import (
                CampaignError,
                CampaignProcessLock,
            )

            try:
                with CampaignProcessLock(campaign_lock_path):
                    if active_windows_image_pids("pa-agent.exe"):
                        raise DatabaseFenceError("协议安装期间出现 PA_Agent 桌面进程")
                    _verify_deployed_repository(
                        project_root=root,
                        deployed_sha=deployed_sha,
                    )
                    fence = DatabaseWriteFence(control_path)
                    global_lock = FileLock(str(fence.lock_path))
                    try:
                        global_lock.acquire(timeout=0)
                    except Timeout as exc:
                        raise DatabaseFenceError(
                            "执行数据库仍有写入者，禁止安装栅栏协议"
                        ) from exc
                    try:
                        candidate = _protocol_database_path(control_path)
                        connection = sqlite3.connect(
                            candidate,
                            timeout=5.0,
                            isolation_level=None,
                        )
                        intent: DatabaseFenceRecoveryIntent | None = None
                        expected_receipt: DatabaseFenceProtocolReceipt | None = None
                        already_installed = False
                        try:
                            connection.execute("PRAGMA busy_timeout = 5000")
                            connection.execute("PRAGMA synchronous = FULL")
                            connection.execute("BEGIN IMMEDIATE")
                            try:
                                schema_version = _read_worker_schema_version_from_connection(
                                    connection
                                )
                                protocol_receipt, required_generation = (
                                    _read_database_fence_metadata_from_connection(connection)
                                )
                                marker = _read_marker(fence.marker_path)
                                intent_path = (
                                    fence.state_path.parent / _RECOVERY_NAME
                                )
                                intent = _read_recovery_intent(intent_path)
                                strict_legacy = False
                                try:
                                    state = _read_state(fence.state_path)
                                except DatabaseFenceError:
                                    strict_legacy = _is_strict_legacy_bootstrap_state(
                                        fence.state_path
                                    )
                                    if not strict_legacy:
                                        raise
                                    state = None

                                if intent is None and state is not None:
                                    installed_receipt = DatabaseFenceProtocolReceipt(
                                        protocol_sha=state.protocol_sha,
                                        protocol_installed_at=state.protocol_installed_at,
                                        protocol_instance_id=state.protocol_instance_id,
                                        minimum_generation=state.generation,
                                    )
                                    if (
                                        state.protocol_sha == clean_sha
                                        and not state.active
                                        and marker is not None
                                        and marker.instance_id
                                        == state.protocol_instance_id
                                        and protocol_receipt == installed_receipt
                                        and schema_version in {4, 5}
                                        and (
                                            required_generation is None
                                            or required_generation == state.generation
                                        )
                                    ):
                                        already_installed = True

                                if not already_installed:
                                    if schema_version != 4:
                                        raise DatabaseFenceError(
                                            "正式栅栏恢复只允许在 Worker schema v4 上执行"
                                        )
                                    if required_generation is not None:
                                        raise DatabaseFenceError(
                                            "活动 v5 已存在，禁止恢复栅栏协议"
                                        )

                                    source_digest = _state_file_sha256(fence.state_path)
                                    if intent is None:
                                        if protocol_receipt is not None:
                                            raise DatabaseFenceError(
                                                "控制库正式回执与待恢复状态冲突"
                                            )
                                        prepared_at = _now_iso()
                                        if strict_legacy:
                                            if marker is not None:
                                                raise DatabaseFenceError(
                                                    "旧版自动栅栏存在无恢复意图的初始化标记"
                                                )
                                            source_kind = "legacy-bootstrap-v2"
                                            instance_id = uuid.uuid4().hex
                                            marker_created_at = prepared_at
                                            protocol_installed_at = prepared_at
                                        elif state is not None and (
                                            state.protocol_sha == _AUTO_PROTOCOL
                                        ):
                                            if (
                                                state.generation != 0
                                                or state.active
                                                or state.operation_id
                                                or marker is None
                                                or marker.instance_id
                                                != state.protocol_instance_id
                                            ):
                                                raise DatabaseFenceError(
                                                    "当前自动栅栏状态不满足正式安装条件"
                                                )
                                            _parse_canonical_utc_time(
                                                state.updated_at,
                                                field_name="自动栅栏更新时间",
                                            )
                                            _parse_canonical_utc_time(
                                                state.protocol_installed_at,
                                                field_name="自动栅栏安装时间",
                                            )
                                            marker_created_at = (
                                                _parse_canonical_utc_time(
                                                    marker.created_at,
                                                    field_name="自动栅栏标记时间",
                                                )
                                            )
                                            if not (
                                                state.updated_at
                                                == state.protocol_installed_at
                                                == marker_created_at
                                            ):
                                                raise DatabaseFenceError(
                                                    "当前自动栅栏时间合同不一致"
                                                )
                                            source_kind = "current-bootstrap-v2"
                                            instance_id = state.protocol_instance_id
                                            protocol_installed_at = prepared_at
                                        elif state is not None and (
                                            state.protocol_sha == clean_sha
                                        ):
                                            if (
                                                state.generation != 0
                                                or state.active
                                                or state.operation_id
                                                or marker is None
                                                or marker.instance_id
                                                != state.protocol_instance_id
                                            ):
                                                raise DatabaseFenceError(
                                                    "缺少回执的正式栅栏状态不满足恢复条件"
                                                )
                                            _parse_canonical_utc_time(
                                                state.updated_at,
                                                field_name="正式栅栏更新时间",
                                            )
                                            protocol_installed_at = (
                                                _parse_canonical_utc_time(
                                                    state.protocol_installed_at,
                                                    field_name="正式栅栏安装时间",
                                                )
                                            )
                                            marker_created_at = (
                                                _parse_canonical_utc_time(
                                                    marker.created_at,
                                                    field_name="正式栅栏标记时间",
                                                )
                                            )
                                            if not (
                                                state.updated_at
                                                == protocol_installed_at
                                                == marker_created_at
                                            ):
                                                raise DatabaseFenceError(
                                                    "缺少回执的正式栅栏时间合同不一致"
                                                )
                                            source_kind = "formal-without-receipt-v2"
                                            instance_id = state.protocol_instance_id
                                        else:
                                            raise DatabaseFenceError(
                                                "执行数据库栅栏状态不属于可恢复合同"
                                            )
                                        intent = DatabaseFenceRecoveryIntent(
                                            deployed_sha=clean_sha,
                                            prepared_at=prepared_at,
                                            protocol_installed_at=protocol_installed_at,
                                            marker_created_at=marker_created_at,
                                            recovery_id=uuid.uuid4().hex,
                                            protocol_instance_id=instance_id,
                                            source_state_sha256=source_digest,
                                            source_kind=source_kind,
                                        )
                                        _write_recovery_intent(intent_path, intent)
                                        if _read_recovery_intent(intent_path) != intent:
                                            raise DatabaseFenceError(
                                                "新建的栅栏恢复意图未能原样持久化"
                                            )
                                    elif intent.deployed_sha != clean_sha:
                                        raise DatabaseFenceError(
                                            "栅栏恢复意图绑定了另一个部署 SHA"
                                        )

                                    expected_state = _expected_recovery_state(intent)
                                    state_is_source = (
                                        source_digest
                                        == intent.source_state_sha256
                                    )
                                    state_is_target = state == expected_state
                                    if not state_is_source and not state_is_target:
                                        raise DatabaseFenceError(
                                            "栅栏状态不属于恢复意图绑定的源或目标"
                                        )
                                    if state_is_source:
                                        if (
                                            intent.source_kind
                                            == "legacy-bootstrap-v2"
                                            and not strict_legacy
                                        ):
                                            raise DatabaseFenceError(
                                                "恢复意图绑定的旧版源状态不再存在"
                                            )
                                        if (
                                            intent.source_kind
                                            == "current-bootstrap-v2"
                                            and (
                                                state is None
                                                or state.protocol_sha
                                                != _AUTO_PROTOCOL
                                                or state.generation != 0
                                                or state.active
                                                or state.operation_id
                                                or marker is None
                                                or marker.instance_id
                                                != intent.protocol_instance_id
                                                or marker.created_at
                                                != intent.marker_created_at
                                            )
                                        ):
                                            raise DatabaseFenceError(
                                                "恢复意图绑定的当前自动栅栏源状态无效"
                                            )
                                        if (
                                            intent.source_kind
                                            == "formal-without-receipt-v2"
                                            and state != expected_state
                                        ):
                                            raise DatabaseFenceError(
                                                "恢复意图绑定的正式源状态无效"
                                            )

                                    if marker is None:
                                        if not (
                                            intent.source_kind
                                            == "legacy-bootstrap-v2"
                                            and state_is_source
                                            and strict_legacy
                                        ):
                                            raise DatabaseFenceError(
                                                "栅栏恢复缺少受意图绑定的初始化标记"
                                            )
                                        _write_marker(
                                            fence.marker_path,
                                            instance_id=intent.protocol_instance_id,
                                            created_at=intent.marker_created_at,
                                        )
                                        marker = _read_marker(fence.marker_path)
                                    if (
                                        marker is None
                                        or marker.instance_id
                                        != intent.protocol_instance_id
                                        or marker.created_at
                                        != intent.marker_created_at
                                    ):
                                        raise DatabaseFenceError(
                                            "初始化标记不属于当前栅栏恢复意图"
                                        )
                                    _parse_canonical_utc_time(
                                        marker.created_at,
                                        field_name="恢复初始化标记时间",
                                    )

                                    if state != expected_state:
                                        _write_state(fence.state_path, expected_state)

                                    expected_receipt = DatabaseFenceProtocolReceipt(
                                        protocol_sha=intent.deployed_sha,
                                        protocol_installed_at=(
                                            intent.protocol_installed_at
                                        ),
                                        protocol_instance_id=(
                                            intent.protocol_instance_id
                                        ),
                                        minimum_generation=0,
                                    )
                                    if (
                                        protocol_receipt is not None
                                        and protocol_receipt != expected_receipt
                                    ):
                                        raise DatabaseFenceError(
                                            "控制库回执不属于当前栅栏恢复意图"
                                        )
                                    _upsert_database_protocol_receipt(
                                        connection,
                                        expected_receipt,
                                    )

                                    if active_windows_image_pids("pa-agent.exe"):
                                        raise DatabaseFenceError(
                                            "协议提交前出现 PA_Agent 桌面进程"
                                        )
                                    _verify_deployed_repository(
                                        project_root=root,
                                        deployed_sha=clean_sha,
                                    )
                                    if (
                                        _read_worker_schema_version_from_connection(
                                            connection
                                        )
                                        != 4
                                    ):
                                        raise DatabaseFenceError(
                                            "协议提交前 Worker schema 已改变"
                                        )
                                    final_receipt, final_generation = (
                                        _read_database_fence_metadata_from_connection(
                                            connection
                                        )
                                    )
                                    if (
                                        final_receipt != expected_receipt
                                        or final_generation is not None
                                    ):
                                        raise DatabaseFenceError(
                                            "协议提交前控制库合同已改变"
                                        )
                                connection.execute("COMMIT")
                                if not already_installed:
                                    if intent is None or expected_receipt is None:
                                        raise DatabaseFenceError(
                                            "正式栅栏恢复提交缺少内部合同"
                                        )
                                    connection.execute("BEGIN IMMEDIATE")
                                    try:
                                        persisted_intent = _read_recovery_intent(
                                            intent_path
                                        )
                                        if persisted_intent != intent:
                                            raise DatabaseFenceError(
                                                "正式栅栏恢复意图在提交后发生变化"
                                            )
                                        if (
                                            _read_worker_schema_version_from_connection(
                                                connection
                                            )
                                            != 4
                                        ):
                                            raise DatabaseFenceError(
                                                "协议收口前 Worker schema 已改变"
                                            )
                                        final_receipt, final_generation = (
                                            _read_database_fence_metadata_from_connection(
                                                connection
                                            )
                                        )
                                        if (
                                            final_receipt != expected_receipt
                                            or final_generation is not None
                                        ):
                                            raise DatabaseFenceError(
                                                "协议收口前控制库合同已改变"
                                            )
                                        if active_windows_image_pids("pa-agent.exe"):
                                            raise DatabaseFenceError(
                                                "协议收口前出现 PA_Agent 桌面进程"
                                            )
                                        _verify_deployed_repository(
                                            project_root=root,
                                            deployed_sha=clean_sha,
                                        )
                                        try:
                                            intent_path.unlink()
                                        except OSError as exc:
                                            raise DatabaseFenceError(
                                                "正式栅栏已经提交，但恢复意图未能清除"
                                            ) from exc
                                        connection.execute("COMMIT")
                                    except Exception:
                                        if connection.in_transaction:
                                            connection.execute("ROLLBACK")
                                        raise
                            except Exception:
                                if connection.in_transaction:
                                    connection.execute("ROLLBACK")
                                raise
                        except sqlite3.Error as exc:
                            raise DatabaseFenceError(
                                "正式栅栏协议安装的控制库事务失败"
                            ) from exc
                        finally:
                            connection.close()

                        if already_installed:
                            return fence._validated_state()
                        if intent is None:
                            raise DatabaseFenceError("正式栅栏恢复意图意外缺失")
                        return fence._validated_state()
                    finally:
                        global_lock.release()
            except CampaignError as exc:
                raise DatabaseFenceError("Campaign 单例锁已被占用") from exc
        finally:
            worker_lock.release()
    finally:
        gui_lock.release()


class DatabaseMaintenanceLease:
    """持有全局写锁；只有显式完成后才解除耐久维护状态。"""

    def __init__(
        self,
        *,
        database_path: Path,
        lock: FileLock,
        state_path: Path,
        state: DatabaseFenceState,
    ) -> None:
        self._database_path = database_path
        self._lock = lock
        self._state_path = state_path
        self.state = state
        self._finished = False
        self._released = False

    def finish(self) -> None:
        if self._finished:
            return
        current = _read_state(self._state_path)
        if current != self.state:
            raise DatabaseFenceError("执行数据库维护栅栏在切换期间被改写")
        protocol_receipt, required_generation = _read_database_fence_metadata(self._database_path)
        if required_generation is None:
            raise DatabaseFenceError("完成数据库维护缺少精确的安全切换回执")
        if required_generation != self.state.generation:
            raise DatabaseFenceError("安全切换回执代际与当前维护代际不一致")
        if (
            protocol_receipt is None
            or protocol_receipt.protocol_sha != self.state.protocol_sha
            or protocol_receipt.protocol_installed_at != self.state.protocol_installed_at
            or protocol_receipt.protocol_instance_id != self.state.protocol_instance_id
        ):
            raise DatabaseFenceError("正式栅栏状态与控制库协议回执不一致")
        _write_database_protocol_receipt(
            self._database_path,
            DatabaseFenceProtocolReceipt(
                protocol_sha=protocol_receipt.protocol_sha,
                protocol_installed_at=protocol_receipt.protocol_installed_at,
                protocol_instance_id=protocol_receipt.protocol_instance_id,
                minimum_generation=self.state.generation,
            ),
        )
        _write_state(
            self._state_path,
            DatabaseFenceState(
                generation=self.state.generation,
                active=False,
                operation_id="",
                updated_at=_now_iso(),
                protocol_sha=self.state.protocol_sha,
                protocol_installed_at=self.state.protocol_installed_at,
                protocol_instance_id=self.state.protocol_instance_id,
            ),
        )
        self._finished = True

    def release(self) -> None:
        if self._released:
            return
        self._lock.release()
        self._released = True

    def __enter__(self) -> DatabaseMaintenanceLease:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.release()


class DatabaseWriteFence:
    """协调控制库和执行账本的所有生产写入与维护切换。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path).resolve(strict=False)
        self.lock_path, self.state_path, self.marker_path = _paths(self.database_path)
        self.recovery_path = self.state_path.parent / _RECOVERY_NAME

    def _validated_state(self) -> DatabaseFenceState:
        if _read_recovery_intent(self.recovery_path) is not None:
            raise DatabaseFenceError("执行数据库栅栏恢复尚未完成，禁止写入")
        state = _read_state(self.state_path)
        marker = _read_marker(self.marker_path)
        protocol_receipt, required_generation = _read_database_fence_metadata(self.database_path)
        if state is None:
            if required_generation is not None:
                raise DatabaseFenceError("活动 v5 的维护栅栏文件缺失，禁止重新初始化")
            if protocol_receipt is not None:
                raise DatabaseFenceError("正式协议仍在控制库中，但维护栅栏状态缺失")
            if marker is not None:
                raise DatabaseFenceError("执行数据库维护栅栏状态缺失但初始化标记仍存在")
            raise DatabaseFenceError("执行数据库写入栅栏协议尚未安装")
        if marker is None:
            raise DatabaseFenceError("执行数据库栅栏初始化标记缺失")
        if marker.instance_id != state.protocol_instance_id:
            raise DatabaseFenceError("执行数据库栅栏初始化标记与状态不一致")
        if state.protocol_sha == _AUTO_PROTOCOL:
            if protocol_receipt is not None:
                raise DatabaseFenceError("自动栅栏状态与正式协议回执冲突")
        else:
            if (
                protocol_receipt is None
                or protocol_receipt.protocol_sha != state.protocol_sha
                or protocol_receipt.protocol_installed_at != state.protocol_installed_at
                or protocol_receipt.protocol_instance_id != state.protocol_instance_id
            ):
                raise DatabaseFenceError("正式栅栏状态与控制库协议回执不一致")
            if state.generation < protocol_receipt.minimum_generation:
                raise DatabaseFenceError("执行数据库维护栅栏代际低于正式协议与安全切换回执")
        if required_generation is not None and (
            state.generation < required_generation or not _is_full_git_sha(state.protocol_sha)
        ):
            raise DatabaseFenceError("执行数据库维护栅栏代际低于活动 v5 回执")
        if (
            not state.active
            and state.generation >= 1
            and (
                protocol_receipt is None
                or protocol_receipt.minimum_generation != state.generation
                or required_generation is None
                or required_generation != state.generation
            )
        ):
            raise DatabaseFenceError("已完成 v5 的安全切换回执缺失或代际不一致")
        return state

    def _bootstrap_automatic_protocol(self) -> DatabaseFenceState:
        if _read_recovery_intent(self.recovery_path) is not None:
            raise DatabaseFenceError("执行数据库栅栏恢复尚未完成，禁止自动初始化")
        existing = _read_state(self.state_path)
        if existing is not None:
            return self._validated_state()
        marker = _read_marker(self.marker_path)
        protocol_receipt, required_generation = _read_database_fence_metadata(self.database_path)
        if required_generation is not None:
            raise DatabaseFenceError("活动 v5 的维护栅栏文件缺失，禁止重新初始化")
        if protocol_receipt is not None:
            raise DatabaseFenceError("正式协议仍在控制库中，禁止自动重建栅栏")
        if marker is not None:
            raise DatabaseFenceError("执行数据库维护栅栏状态缺失但初始化标记仍存在")
        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=_WRITE_TIMEOUT_SECONDS)
        except Timeout as exc:
            raise DatabaseFenceError("执行数据库写入栅栏获取超时") from exc
        try:
            if _read_recovery_intent(self.recovery_path) is not None:
                raise DatabaseFenceError("执行数据库栅栏恢复尚未完成，禁止自动初始化")
            existing = _read_state(self.state_path)
            if existing is not None:
                return self._validated_state()
            marker = _read_marker(self.marker_path)
            protocol_receipt, required_generation = _read_database_fence_metadata(
                self.database_path
            )
            if required_generation is not None:
                raise DatabaseFenceError("活动 v5 的维护栅栏文件缺失，禁止重新初始化")
            if protocol_receipt is not None:
                raise DatabaseFenceError("正式协议仍在控制库中，禁止自动重建栅栏")
            if marker is not None:
                raise DatabaseFenceError("执行数据库维护栅栏状态缺失但初始化标记仍存在")
            now = _now_iso()
            instance_id = uuid.uuid4().hex
            _write_marker(
                self.marker_path,
                instance_id=instance_id,
                created_at=now,
            )
            state = DatabaseFenceState(
                generation=0,
                active=False,
                operation_id="",
                updated_at=now,
                protocol_sha=_AUTO_PROTOCOL,
                protocol_installed_at=now,
                protocol_instance_id=instance_id,
            )
            _write_state(self.state_path, state)
            return state
        finally:
            lock.release()

    def state(self) -> DatabaseFenceState:
        return self._validated_state()

    def install_protocol(self, *, deployed_sha: str) -> DatabaseFenceState:
        """禁止绕过进程锁、源码核验和恢复合同直接安装正式协议。"""

        clean_sha = deployed_sha.strip().lower()
        if not _is_full_git_sha(clean_sha):
            raise ValueError("栅栏部署 SHA 必须是 40 位小写 Git SHA")
        raise DatabaseFenceError(
            "正式栅栏协议只能通过带四重锁与 Git 核验的正式安装入口安装"
        )

    def require_protocol(self, *, deployed_sha: str) -> DatabaseFenceState:
        clean_sha = deployed_sha.strip().lower()
        if not _is_full_git_sha(clean_sha):
            raise ValueError("栅栏部署 SHA 必须是 40 位小写 Git SHA")
        state = self._validated_state()
        if state.protocol_sha != clean_sha:
            raise DatabaseFenceError("执行数据库栅栏部署 SHA 不匹配")
        return state

    @contextmanager
    def write(self) -> Iterator[None]:
        before = _read_state(self.state_path)
        if before is None:
            before = self._bootstrap_automatic_protocol()
        if before.active:
            raise DatabaseFenceError("执行数据库正在维护，禁止写入")
        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=_WRITE_TIMEOUT_SECONDS)
        except Timeout as exc:
            self._validated_state()
            raise DatabaseFenceError("执行数据库写入栅栏获取超时") from exc
        try:
            after = self._validated_state()
            if after.active or after != before:
                raise DatabaseFenceError("执行数据库维护代际已变化，拒绝维护前开始的写入")
            yield
        finally:
            lock.release()

    @contextmanager
    def exclusive_check(self, *, deployed_sha: str) -> Iterator[None]:
        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise DatabaseFenceError("执行数据库仍有写入者，禁止核验") from exc
        try:
            state = self.require_protocol(deployed_sha=deployed_sha)
            if state.active:
                raise DatabaseFenceError("执行数据库存在未完成维护，禁止核验")
            yield
        finally:
            lock.release()

    def begin_maintenance(
        self,
        *,
        operation_id: str,
        deployed_sha: str,
    ) -> DatabaseMaintenanceLease:
        clean_operation_id = operation_id.strip()
        if not clean_operation_id or len(clean_operation_id) > 128:
            raise ValueError("维护操作编号无效")
        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise DatabaseFenceError("执行数据库仍有写入者，禁止切换") from exc
        try:
            current = self.require_protocol(deployed_sha=deployed_sha)
            if current.active:
                raise DatabaseFenceError("执行数据库存在未完成维护，必须先人工复核")
            active = DatabaseFenceState(
                generation=current.generation + 1,
                active=True,
                operation_id=clean_operation_id,
                updated_at=_now_iso(),
                protocol_sha=current.protocol_sha,
                protocol_installed_at=current.protocol_installed_at,
                protocol_instance_id=current.protocol_instance_id,
            )
            _write_state(self.state_path, active)
            return DatabaseMaintenanceLease(
                database_path=self.database_path,
                lock=lock,
                state_path=self.state_path,
                state=active,
            )
        except Exception:
            lock.release()
            raise

    def resume_maintenance(
        self,
        *,
        operation_id: str,
        deployed_sha: str,
    ) -> DatabaseMaintenanceLease:
        """在新进程中重新取得同一崩溃维护操作的锁。"""

        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise DatabaseFenceError("执行数据库仍有写入者，禁止恢复") from exc
        try:
            current = self.require_protocol(deployed_sha=deployed_sha)
            if not current.active or current.operation_id != operation_id.strip():
                raise DatabaseFenceError("没有匹配的未完成数据库维护操作")
            return DatabaseMaintenanceLease(
                database_path=self.database_path,
                lock=lock,
                state_path=self.state_path,
                state=current,
            )
        except Exception:
            lock.release()
            raise


@contextmanager
def database_write_guard(database_path: Path) -> Iterator[None]:
    """让一次生产数据库写入遵守共享维护代际。"""

    with DatabaseWriteFence(database_path).write():
        yield


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="安装 PA_Agent 执行数据库写入栅栏协议",
    )
    parser.add_argument("install", choices=("install",))
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--deployed-sha", required=True)
    args = parser.parse_args(argv)
    try:
        state = install_official_database_fence_protocol(
            project_root=args.project_root,
            deployed_sha=args.deployed_sha,
        )
    except (DatabaseFenceError, OSError, ValueError) as exc:
        parser.exit(2, f"database fence install blocked: {exc}\n")
    print(
        json.dumps(
            {
                "installed": True,
                "generation": state.generation,
                "active": state.active,
                "protocol_sha": state.protocol_sha,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
