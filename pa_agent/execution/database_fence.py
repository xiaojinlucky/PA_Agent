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
_GUI_LOCK_NAME = "execution_gui_writer.lock"
_PROTOCOL_META_KEY = "worker_database_fence_protocol"
_AUTO_PROTOCOL = "auto-bootstrap"
_WRITE_TIMEOUT_SECONDS = 5.0
_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
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


def _read_marker(path: Path) -> str | None:
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
    _parse_aware_time(payload.get("created_at"), field_name="初始化标记时间")
    return instance_id


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


def _protocol_database_path(database_path: Path) -> Path:
    directory = Path(database_path).resolve(strict=False).parent
    official_control = directory / "execution_control.sqlite3"
    return (
        official_control if official_control.exists() else Path(database_path).resolve(strict=False)
    )


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
    }:
        raise DatabaseFenceError("控制库中的正式栅栏协议回执合同无效")
    protocol_sha = payload.get("protocol_sha")
    protocol_instance_id = payload.get("protocol_instance_id")
    if (
        payload.get("version") != 1
        or not isinstance(protocol_sha, str)
        or not _is_full_git_sha(protocol_sha)
        or not isinstance(protocol_instance_id, str)
        or len(protocol_instance_id) != 32
        or any(character not in "0123456789abcdef" for character in protocol_instance_id)
    ):
        raise DatabaseFenceError("控制库中的正式栅栏协议回执字段无效")
    return DatabaseFenceProtocolReceipt(
        protocol_sha=protocol_sha,
        protocol_installed_at=_parse_aware_time(
            payload.get("protocol_installed_at"),
            field_name="协议回执安装时间",
        ),
        protocol_instance_id=protocol_instance_id,
    )


def _read_database_protocol_receipt(
    database_path: Path,
) -> DatabaseFenceProtocolReceipt | None:
    candidate = _protocol_database_path(database_path)
    if not candidate.is_file() or candidate.stat().st_size == 0:
        return None
    uri = f"{candidate.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            meta_exists = connection.execute(
                "SELECT 1 FROM sqlite_master " "WHERE type='table' AND name='worker_meta'"
            ).fetchone()
            if meta_exists is None:
                return None
            row = connection.execute(
                "SELECT value FROM worker_meta WHERE key=?",
                (_PROTOCOL_META_KEY,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise DatabaseFenceError("无法核对控制库中的正式栅栏协议回执") from exc
    return None if row is None else _parse_protocol_receipt(row[0])


def _write_database_protocol_receipt(
    database_path: Path,
    receipt: DatabaseFenceProtocolReceipt,
) -> None:
    candidate = _protocol_database_path(database_path)
    if not candidate.is_file():
        raise DatabaseFenceError("正式控制数据库不存在，无法安装栅栏协议回执")
    payload = json.dumps(
        {
            "version": 1,
            "protocol_sha": receipt.protocol_sha,
            "protocol_installed_at": receipt.protocol_installed_at,
            "protocol_instance_id": receipt.protocol_instance_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection = sqlite3.connect(candidate, timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            meta_exists = connection.execute(
                "SELECT 1 FROM sqlite_master " "WHERE type='table' AND name='worker_meta'"
            ).fetchone()
            if meta_exists is None:
                raise DatabaseFenceError("正式控制数据库缺少 worker_meta，无法安装栅栏协议回执")
            existing = connection.execute(
                "SELECT value FROM worker_meta WHERE key=?",
                (_PROTOCOL_META_KEY,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO worker_meta(key, value) VALUES (?, ?)",
                    (_PROTOCOL_META_KEY, payload),
                )
            elif _parse_protocol_receipt(existing[0]) != receipt:
                raise DatabaseFenceError("控制库已经绑定另一个正式栅栏协议")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise DatabaseFenceError("正式栅栏协议回执写入控制数据库失败") from exc
    finally:
        connection.close()


def _safe_cutover_generation(database_path: Path) -> int | None:
    """读取活动控制库回执绑定的最低栅栏代际。"""

    directory = Path(database_path).resolve(strict=False).parent
    official_control = directory / "execution_control.sqlite3"
    candidate = (
        official_control if official_control.exists() else Path(database_path).resolve(strict=False)
    )
    if not candidate.is_file() or candidate.stat().st_size == 0:
        return None
    uri = f"{candidate.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            meta_exists = connection.execute(
                "SELECT 1 FROM sqlite_master " "WHERE type='table' AND name='worker_meta'"
            ).fetchone()
            if meta_exists is None:
                return None
            rows = connection.execute(
                "SELECT key, value FROM worker_meta " "WHERE key LIKE 'worker_safe_cutover:%'"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise DatabaseFenceError("无法核对执行数据库与维护栅栏的代际绑定") from exc
    if not rows:
        return None
    if len(rows) != 1:
        raise DatabaseFenceError("活动控制库的安全切换回执数量不正确")
    try:
        receipt = json.loads(str(rows[0][1]))
    except (TypeError, ValueError) as exc:
        raise DatabaseFenceError("活动控制库的安全切换回执无效") from exc
    generation = receipt.get("fence_generation") if isinstance(receipt, dict) else None
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise DatabaseFenceError("活动控制库没有绑定有效栅栏代际")
    return generation


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
                    return DatabaseWriteFence(control_path).install_protocol(
                        deployed_sha=deployed_sha
                    )
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
        required_generation = _safe_cutover_generation(self._database_path)
        if required_generation is not None and self.state.generation < required_generation:
            raise DatabaseFenceError("执行数据库维护栅栏代际低于活动 v5 回执")
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

    def _validated_state(self) -> DatabaseFenceState:
        state = _read_state(self.state_path)
        marker_instance_id = _read_marker(self.marker_path)
        protocol_receipt = _read_database_protocol_receipt(self.database_path)
        required_generation = _safe_cutover_generation(self.database_path)
        if state is None:
            if required_generation is not None:
                raise DatabaseFenceError("活动 v5 的维护栅栏文件缺失，禁止重新初始化")
            if protocol_receipt is not None:
                raise DatabaseFenceError("正式协议仍在控制库中，但维护栅栏状态缺失")
            if marker_instance_id is not None:
                raise DatabaseFenceError("执行数据库维护栅栏状态缺失但初始化标记仍存在")
            raise DatabaseFenceError("执行数据库写入栅栏协议尚未安装")
        if marker_instance_id is None:
            raise DatabaseFenceError("执行数据库栅栏初始化标记缺失")
        if marker_instance_id != state.protocol_instance_id:
            raise DatabaseFenceError("执行数据库栅栏初始化标记与状态不一致")
        if state.protocol_sha == _AUTO_PROTOCOL:
            if protocol_receipt is not None:
                raise DatabaseFenceError("自动栅栏状态与正式协议回执冲突")
        elif protocol_receipt != DatabaseFenceProtocolReceipt(
            protocol_sha=state.protocol_sha,
            protocol_installed_at=state.protocol_installed_at,
            protocol_instance_id=state.protocol_instance_id,
        ):
            raise DatabaseFenceError("正式栅栏状态与控制库协议回执不一致")
        if required_generation is not None and (
            state.generation < required_generation or not _is_full_git_sha(state.protocol_sha)
        ):
            raise DatabaseFenceError("执行数据库维护栅栏代际低于活动 v5 回执")
        return state

    def _bootstrap_automatic_protocol(self) -> DatabaseFenceState:
        existing = _read_state(self.state_path)
        if existing is not None:
            return self._validated_state()
        marker_instance_id = _read_marker(self.marker_path)
        protocol_receipt = _read_database_protocol_receipt(self.database_path)
        required_generation = _safe_cutover_generation(self.database_path)
        if required_generation is not None:
            raise DatabaseFenceError("活动 v5 的维护栅栏文件缺失，禁止重新初始化")
        if protocol_receipt is not None:
            raise DatabaseFenceError("正式协议仍在控制库中，禁止自动重建栅栏")
        if marker_instance_id is not None:
            raise DatabaseFenceError("执行数据库维护栅栏状态缺失但初始化标记仍存在")
        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=_WRITE_TIMEOUT_SECONDS)
        except Timeout as exc:
            raise DatabaseFenceError("执行数据库写入栅栏获取超时") from exc
        try:
            existing = _read_state(self.state_path)
            if existing is not None:
                return self._validated_state()
            marker_instance_id = _read_marker(self.marker_path)
            protocol_receipt = _read_database_protocol_receipt(self.database_path)
            if protocol_receipt is not None:
                raise DatabaseFenceError("正式协议仍在控制库中，禁止自动重建栅栏")
            if marker_instance_id is not None:
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
        """把自动标记升级为已提交 SHA；不同 SHA 不可静默覆盖。"""

        clean_sha = deployed_sha.strip().lower()
        if not _is_full_git_sha(clean_sha):
            raise ValueError("栅栏部署 SHA 必须是 40 位小写 Git SHA")
        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise DatabaseFenceError("执行数据库仍有写入者，禁止安装栅栏协议") from exc
        try:
            state = _read_state(self.state_path)
            marker_instance_id = _read_marker(self.marker_path)
            protocol_receipt = _read_database_protocol_receipt(self.database_path)
            required_generation = _safe_cutover_generation(self.database_path)
            if state is None:
                if required_generation is not None or protocol_receipt is not None:
                    raise DatabaseFenceError("执行数据库维护栅栏状态异常缺失，禁止安装协议")
                now = _now_iso()
                instance_id = marker_instance_id or uuid.uuid4().hex
                if marker_instance_id is None:
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
                    protocol_sha=clean_sha,
                    protocol_installed_at=now,
                    protocol_instance_id=instance_id,
                )
                _write_state(self.state_path, state)
                _write_database_protocol_receipt(
                    self.database_path,
                    DatabaseFenceProtocolReceipt(
                        protocol_sha=state.protocol_sha,
                        protocol_installed_at=state.protocol_installed_at,
                        protocol_instance_id=state.protocol_instance_id,
                    ),
                )
                return self._validated_state()
            if marker_instance_id is None:
                raise DatabaseFenceError("执行数据库栅栏初始化标记缺失")
            if marker_instance_id != state.protocol_instance_id:
                raise DatabaseFenceError("执行数据库栅栏初始化标记与状态不一致")
            if state.active:
                raise DatabaseFenceError("执行数据库仍处于维护状态")
            if required_generation is not None:
                raise DatabaseFenceError("活动 v5 已存在，禁止改写栅栏部署 SHA")
            if state.protocol_sha == clean_sha:
                expected_receipt = DatabaseFenceProtocolReceipt(
                    protocol_sha=state.protocol_sha,
                    protocol_installed_at=state.protocol_installed_at,
                    protocol_instance_id=state.protocol_instance_id,
                )
                if protocol_receipt is None:
                    _write_database_protocol_receipt(
                        self.database_path,
                        expected_receipt,
                    )
                elif protocol_receipt != expected_receipt:
                    raise DatabaseFenceError("控制库已经绑定另一个正式栅栏协议")
                return self._validated_state()
            if state.protocol_sha != _AUTO_PROTOCOL:
                raise DatabaseFenceError("栅栏协议已绑定另一个提交 SHA")
            if protocol_receipt is not None:
                raise DatabaseFenceError("自动栅栏状态与正式协议回执冲突")
            installed_at = _now_iso()
            upgraded = DatabaseFenceState(
                generation=state.generation,
                active=False,
                operation_id="",
                updated_at=installed_at,
                protocol_sha=clean_sha,
                protocol_installed_at=installed_at,
                protocol_instance_id=state.protocol_instance_id,
            )
            _write_state(self.state_path, upgraded)
            _write_database_protocol_receipt(
                self.database_path,
                DatabaseFenceProtocolReceipt(
                    protocol_sha=upgraded.protocol_sha,
                    protocol_installed_at=upgraded.protocol_installed_at,
                    protocol_instance_id=upgraded.protocol_instance_id,
                ),
            )
            return self._validated_state()
        finally:
            lock.release()

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
        try:
            before = self._validated_state()
        except DatabaseFenceError as exc:
            if "协议尚未安装" not in str(exc):
                raise
            before = self._bootstrap_automatic_protocol()
        if before.active:
            raise DatabaseFenceError("执行数据库正在维护，禁止写入")
        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=_WRITE_TIMEOUT_SECONDS)
        except Timeout as exc:
            raise DatabaseFenceError("执行数据库写入栅栏获取超时") from exc
        try:
            after = self._validated_state()
            if after.active or after.generation != before.generation:
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
