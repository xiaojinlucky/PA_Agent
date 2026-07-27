"""PendingWriter — persists AnalysisRecord and FollowupTurn to disk.

File naming convention:
    {YYYY-MM-DD_HH-mm-ss}_{symbol}_{timeframe}.json
    {YYYY-MM-DD_HH-mm-ss}_{symbol}_{timeframe}.followups.jsonl

Disk failures are logged and emitted to the event bus but never propagated.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pa_agent.records.schema import (
    AnalysisRecord,
    ConversationCheckpoint,
    FollowupTurn,
    validate_sidecar_record_id,
)
from pa_agent.util.mask_secret import mask_secret


def _default_logger() -> logging.Logger:
    return logging.getLogger(__name__)


def _ms_to_local_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to local datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()


def _build_basename(record: AnalysisRecord) -> str:
    """Build the filename stem (without extension) for a record."""
    timestamp_ms = record.meta.timestamp_local_ms
    dt = _ms_to_local_datetime(timestamp_ms)
    ts_str = (
        f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}"
        f"-{timestamp_ms % 1000:03d}"
    )
    symbol = record.meta.symbol
    timeframe = record.meta.timeframe
    campaign_suffix = (
        f"_{record.meta.campaign_id}"
        if record.meta.campaign_id is not None
        else ""
    )
    return f"{ts_str}_{symbol}_{timeframe}{campaign_suffix}"


def _build_legacy_basename(record: AnalysisRecord) -> str:
    """返回 WO-F 前秒级、无 Campaign 后缀的历史文件名。

    历史实现误把分钟写成月份（``%m``）。这里必须保留该格式，才能继续
    找到已经落盘的旧主记录和自由追问 sidecar；新记录只使用
    ``_build_basename`` 的正确分钟格式。
    """
    dt = _ms_to_local_datetime(record.meta.timestamp_local_ms)
    return (
        f"{dt.strftime('%Y-%m-%d_%H-%m-%S')}_"
        f"{record.meta.symbol}_{record.meta.timeframe}"
    )


class PendingWriter:
    """Writes analysis records and followup turns to the pending directory."""

    def __init__(
        self,
        pending_dir: Optional[Path] = None,
        event_bus=None,
        logger: Optional[logging.Logger] = None,
        api_key: str = "",
    ) -> None:
        if pending_dir is None:
            from pa_agent.config.paths import RECORDS_PENDING_DIR
            pending_dir = RECORDS_PENDING_DIR

        self._pending_dir = pending_dir
        self._event_bus = event_bus
        self._logger = logger or _default_logger()
        self._api_key = api_key

        try:
            self._pending_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._logger.error(
                "PendingWriter: failed to create pending directory %s: %s",
                self._pending_dir,
                exc,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_full(self, record: AnalysisRecord) -> Path:
        """Serialize and save a complete analysis record.

        Returns the path written to, or a best-effort path on failure.
        """
        basename = _build_basename(record)
        path = self._pending_dir / f"{basename}.json"
        data = record.model_dump()
        data = self._sanitize(data, self._api_key)
        self._write_json(path, data)
        try:
            from pa_agent.records.analysis_history import invalidate_latest_record_cache

            invalidate_latest_record_cache()
        except Exception:  # noqa: BLE001
            pass
        return path

    def full_path(self, record: AnalysisRecord) -> Path:
        """Return the canonical full-record path without writing it."""
        return self._pending_dir / f"{_build_basename(record)}.json"

    def record_id(self, record: AnalysisRecord) -> str:
        """解析主记录及自由追问 sidecar 共用的文件名 stem。

        新记录使用毫秒级 canonical 名；打开旧秒级记录时，只要旧主文件或
        sidecar 仍存在，就继续沿用旧名，避免历史追问断成新的孤立会话。
        """
        canonical = _build_basename(record)
        if (self._pending_dir / f"{canonical}.json").is_file():
            return canonical
        legacy = _build_legacy_basename(record)
        legacy_paths = (
            self._pending_dir / f"{legacy}.json",
            self._pending_dir / f"{legacy}.followups.jsonl",
            self._pending_dir / f"{legacy}.conversation.json",
        )
        if any(path.is_file() for path in legacy_paths):
            return legacy
        return canonical

    def save_full_durable(self, record: AnalysisRecord) -> Path:
        """Atomically write, fsync and re-read a complete record.

        Unlike :meth:`save_full`, this method propagates any persistence error.
        Trading execution may only consume records written through this path.
        """
        path = self.full_path(record)
        data = self._sanitize(record.model_dump(), self._api_key)
        self._write_json_durable(path, data)
        try:
            persisted = AnalysisRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise OSError(f"durable analysis record verification failed: {path}") from exc
        if (
            persisted.meta.timestamp_local_ms != record.meta.timestamp_local_ms
            or persisted.meta.symbol != record.meta.symbol
            or persisted.meta.timeframe != record.meta.timeframe
            or persisted.stage2_decision != record.stage2_decision
        ):
            raise OSError(f"durable analysis record content mismatch: {path}")
        try:
            from pa_agent.records.analysis_history import invalidate_latest_record_cache

            invalidate_latest_record_cache()
        except Exception:  # noqa: BLE001
            pass
        return path

    def save_partial(self, record: AnalysisRecord, reason: str) -> Path:
        """Serialize and save a partial analysis record with a reason field.

        The ``_partial_reason`` key is injected into the serialized dict
        (it is not part of the Pydantic model). When ``record.exception`` is
        set, ``partial_reason`` is also copied into that dict for easier
        filtering without reading ``_partial_reason``.

        Returns the path written to, or a best-effort path on failure.
        """
        basename = _build_basename(record)
        path = self._pending_dir / f"{basename}.json"
        data = record.model_dump()
        data["_partial_reason"] = reason
        if isinstance(data.get("exception"), dict):
            data["exception"] = {**data["exception"], "partial_reason": reason}
        data = self._sanitize(data, self._api_key)
        self._write_json(path, data)
        try:
            from pa_agent.records.analysis_history import invalidate_latest_record_cache

            invalidate_latest_record_cache()
        except Exception:  # noqa: BLE001
            pass
        return path

    def save_partial_durable(
        self,
        record: AnalysisRecord,
        reason: str,
    ) -> Path:
        """原子持久化并回读验证一份必须可恢复的失败记录。

        普通 UI 取消等历史路径仍可使用 ``save_partial`` 的尽力而为语义；
        会影响 Campaign 幂等推进的声明校验失败必须走本方法。
        """
        path = self.full_path(record)
        data = record.model_dump()
        data["_partial_reason"] = reason
        if isinstance(data.get("exception"), dict):
            data["exception"] = {
                **data["exception"],
                "partial_reason": reason,
            }
        data = self._sanitize(data, self._api_key)
        self._write_json_durable(path, data)
        try:
            persisted_raw = json.loads(path.read_text(encoding="utf-8"))
            if persisted_raw.pop("_partial_reason", None) != reason:
                raise ValueError("partial reason mismatch")
            persisted = AnalysisRecord.model_validate(persisted_raw)
        except Exception as exc:
            raise OSError(
                f"durable partial analysis record verification failed: {path}"
            ) from exc
        expected_exception = record.exception
        if isinstance(expected_exception, dict):
            expected_exception = {
                **expected_exception,
                "partial_reason": reason,
            }
        if (
            persisted.meta.timestamp_local_ms != record.meta.timestamp_local_ms
            or persisted.meta.symbol != record.meta.symbol
            or persisted.meta.timeframe != record.meta.timeframe
            or persisted.exception != expected_exception
        ):
            raise OSError(
                f"durable partial analysis record content mismatch: {path}"
            )
        try:
            from pa_agent.records.analysis_history import (
                invalidate_latest_record_cache,
            )

            invalidate_latest_record_cache()
        except Exception:  # noqa: BLE001
            pass
        return path

    def append_followup(self, record_id: str, turn: FollowupTurn) -> None:
        """Append a single followup turn to the JSONL sidecar file.

        ``record_id`` is the basename (without extension) of the record file,
        e.g. ``"2026-05-18_14-00-13_XAUUSD_1h"``.
        """
        path = self._pending_dir / f"{record_id}.followups.jsonl"
        line = json.dumps(turn.model_dump(), ensure_ascii=False)
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            self._handle_disk_error(exc, path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize(data: dict, api_key: str) -> dict:
        """Recursively replace any occurrence of *api_key* in string values.

        If *api_key* is empty, returns *data* unchanged.
        Handles nested dicts, lists, and plain string values at any depth.
        """
        if not api_key:
            return data

        masked = mask_secret(api_key)

        def _walk(node):
            if isinstance(node, str):
                return node.replace(api_key, masked)
            if isinstance(node, dict):
                return {k: _walk(v) for k, v in node.items()}
            if isinstance(node, list):
                return [_walk(item) for item in node]
            return node

        return _walk(data)

    def _write_json(self, path: Path, data: dict) -> None:
        """Write *data* as pretty-printed JSON to *path*, handling errors."""
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self._handle_disk_error(exc, path)

    def load_conversation_checkpoint(
        self,
        record_id: str,
    ) -> ConversationCheckpoint | None:
        """读取可恢复线程；文件缺失或损坏时明确从新线程开始。"""
        try:
            path = self._conversation_checkpoint_path(record_id)
        except ValueError:
            self._logger.warning("PendingWriter: invalid conversation record id")
            return None
        if not path.exists():
            return None
        try:
            checkpoint = ConversationCheckpoint.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            self._logger.warning(
                "PendingWriter: invalid conversation checkpoint %s: %s",
                path,
                type(exc).__name__,
            )
            return None
        if checkpoint.record_id != record_id:
            self._logger.warning(
                "PendingWriter: conversation checkpoint record mismatch: %s",
                path,
            )
            return None
        return checkpoint

    def save_conversation_checkpoint(
        self,
        checkpoint: ConversationCheckpoint,
    ) -> Path:
        """原子保存非敏感线程定位信息；失败时向调用方暴露错误。"""
        path = self._conversation_checkpoint_path(checkpoint.record_id)
        self._write_json_durable(path, checkpoint.model_dump())
        return path

    def _conversation_checkpoint_path(self, record_id: str) -> Path:
        """Return a checkpoint path that is guaranteed to stay in pending."""

        safe_record_id = validate_sidecar_record_id(record_id)
        pending_root = self._pending_dir.resolve(strict=False)
        path = (
            self._pending_dir / f"{safe_record_id}.conversation.json"
        ).resolve(strict=False)
        if path.parent != pending_root:
            raise ValueError("conversation checkpoint path escapes pending directory")
        return path

    def _write_json_durable(self, path: Path, data: dict) -> None:
        """Write JSON through a same-directory temporary file and atomically replace."""
        text = json.dumps(data, ensure_ascii=False, indent=2)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except Exception as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._handle_disk_error(
                exc if isinstance(exc, OSError) else OSError(str(exc)),
                path,
            )
            raise

    def _handle_disk_error(self, exc: OSError, path: Path) -> None:
        """Log the error and optionally emit to the event bus."""
        self._logger.error(
            "PendingWriter: disk error writing %s: %s", path, exc
        )
        if self._event_bus is not None:
            try:
                self._event_bus.emit("disk_error", {"path": str(path), "error": str(exc)})
            except Exception as bus_exc:  # noqa: BLE001
                self._logger.error(
                    "PendingWriter: event_bus emit failed: %s", bus_exc
                )
