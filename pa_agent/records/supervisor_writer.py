"""交易监督结论的原子耐久写入。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from pa_agent.agents.supervisor_models import (
    SupervisorDecisionRecord,
    SupervisorInputSnapshot,
    snapshot_digest,
)


class SupervisorPersistenceError(RuntimeError):
    """监督结论存在损坏、错配或无法耐久写入。"""


class SupervisorWriter:
    """按 Campaign + K 线 + PA 分析摘要保存唯一监督结论。"""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def record_id(snapshot: SupervisorInputSnapshot) -> str:
        return (
            f"{snapshot.campaign_id}:{snapshot.closed_bar_ts_open_ms}:"
            f"{snapshot.analysis_digest}"
        )

    def _path(self, snapshot: SupervisorInputSnapshot) -> Path:
        return self._path_for_key(
            snapshot.campaign_id,
            snapshot.closed_bar_ts_open_ms,
            snapshot.analysis_digest,
        )

    def _path_for_key(
        self,
        campaign_id: str,
        bar_ms: int,
        analysis_digest: str,
    ) -> Path:
        key = f"{campaign_id}:{int(bar_ms)}:{analysis_digest}".encode()
        filename = hashlib.sha256(key).hexdigest() + ".json"
        return self.directory / filename

    def path_for_key(
        self,
        *,
        campaign_id: str,
        bar_ms: int,
        analysis_digest: str,
    ) -> Path:
        """Return the canonical durable path for an existing decision."""
        return self._path_for_key(campaign_id, bar_ms, analysis_digest)

    def load_for(
        self,
        snapshot: SupervisorInputSnapshot,
    ) -> SupervisorDecisionRecord | None:
        record = self.load_for_key(
            campaign_id=snapshot.campaign_id,
            bar_ms=snapshot.closed_bar_ts_open_ms,
            analysis_digest=snapshot.analysis_digest,
        )
        if record is None:
            return None
        self._validate_matches(record, snapshot)
        return record

    def load_for_key(
        self,
        *,
        campaign_id: str,
        bar_ms: int,
        analysis_digest: str,
    ) -> SupervisorDecisionRecord | None:
        path = self._path_for_key(campaign_id, bar_ms, analysis_digest)
        if not path.exists():
            return None
        try:
            record = SupervisorDecisionRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError) as exc:
            raise SupervisorPersistenceError(
                f"监督结论无法读取或校验: {path}"
            ) from exc
        expected_id = f"{campaign_id}:{int(bar_ms)}:{analysis_digest}"
        if record.record_id != expected_id:
            raise SupervisorPersistenceError("监督结论 record_id 错配")
        if record.record_id != self.record_id(record.input_snapshot):
            raise SupervisorPersistenceError("监督结论 record_id 与输入快照错配")
        if record.campaign_id != str(campaign_id):
            raise SupervisorPersistenceError("监督结论 campaign_id 错配")
        if record.analysis_digest != str(analysis_digest):
            raise SupervisorPersistenceError("监督结论 analysis_digest 错配")
        if record.closed_bar_ts_open_ms != int(bar_ms):
            raise SupervisorPersistenceError("监督结论 K 线时间错配")
        if record.input_snapshot.campaign_id != record.campaign_id:
            raise SupervisorPersistenceError("监督结论内外 campaign_id 错配")
        if record.input_snapshot.analysis_digest != record.analysis_digest:
            raise SupervisorPersistenceError("监督结论内外 analysis_digest 错配")
        if (
            record.input_snapshot.closed_bar_ts_open_ms
            != record.closed_bar_ts_open_ms
        ):
            raise SupervisorPersistenceError("监督结论内外 K 线时间错配")
        if record.input_snapshot_digest != snapshot_digest(record.input_snapshot):
            raise SupervisorPersistenceError("监督结论输入摘要错配")
        return record

    def save_durable(self, record: SupervisorDecisionRecord) -> Path:
        expected_id = self.record_id(record.input_snapshot)
        if record.record_id != expected_id:
            raise SupervisorPersistenceError("监督记录 ID 与输入快照不一致")
        if record.analysis_digest != record.input_snapshot.analysis_digest:
            raise SupervisorPersistenceError("监督记录 analysis_digest 与输入快照不一致")
        expected_snapshot_digest = snapshot_digest(record.input_snapshot)
        if record.input_snapshot_digest != expected_snapshot_digest:
            raise SupervisorPersistenceError("监督记录输入摘要与输入快照不一致")
        path = self._path(record.input_snapshot)

        existing = self.load_for(record.input_snapshot)
        if existing is not None:
            if existing.model_dump(mode="json") != record.model_dump(mode="json"):
                raise SupervisorPersistenceError(
                    "同一根 K 线已经存在不同监督结论，禁止覆盖"
                ) from None
            return path

        payload = json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.directory,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # 同目录硬链接发布不会覆盖已经存在的结论；并发写入同一键时，
                # 只有一个完整临时文件能成为正式文件，另一方必须读取并比较。
                os.link(temp_path, path)
            except FileExistsError:
                existing = self.load_for(record.input_snapshot)
                if existing is not None and (
                    existing.model_dump(mode="json")
                    == record.model_dump(mode="json")
                ):
                    return path
                raise SupervisorPersistenceError(
                    "同一根 K 线已经存在不同监督结论，禁止覆盖"
                ) from None
        except Exception as exc:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)
            if isinstance(exc, SupervisorPersistenceError):
                raise
            raise SupervisorPersistenceError(
                f"监督结论无法耐久写入: {path}"
            ) from exc
        finally:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)

        try:
            persisted = SupervisorDecisionRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError) as exc:
            raise SupervisorPersistenceError(
                f"监督结论写入后无法重新校验: {path}"
            ) from exc
        if persisted.model_dump(mode="json") != record.model_dump(mode="json"):
            raise SupervisorPersistenceError("监督结论写入后内容不一致")
        return path

    def _validate_matches(
        self,
        record: SupervisorDecisionRecord,
        snapshot: SupervisorInputSnapshot,
    ) -> None:
        if record.record_id != self.record_id(snapshot):
            raise SupervisorPersistenceError("监督结论 record_id 错配")
        if record.campaign_id != snapshot.campaign_id:
            raise SupervisorPersistenceError("监督结论 campaign_id 错配")
        if record.analysis_digest != snapshot.analysis_digest:
            raise SupervisorPersistenceError("监督结论 analysis_digest 错配")
        if record.closed_bar_ts_open_ms != snapshot.closed_bar_ts_open_ms:
            raise SupervisorPersistenceError("监督结论 K 线时间错配")
        if record.input_snapshot_digest != snapshot_digest(snapshot):
            raise SupervisorPersistenceError("监督结论输入摘要错配")
        if record.input_snapshot.model_dump(mode="json") != snapshot.model_dump(mode="json"):
            raise SupervisorPersistenceError("监督结论输入快照内容错配")
