"""交易监督智能体的严格输入、输出和耐久记录模型。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SupervisorAction = Literal["allow_entry", "block_entry"]
SupervisorFallbackLevel = Literal["primary", "backup", "deterministic"]


class _FrozenDict(dict):
    """JSON 可序列化、递归值由外层转换的只读字典。"""

    def _blocked(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("监督输入快照不可变")

    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked
    setdefault = _blocked
    update = _blocked
    __ior__ = _blocked


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        frozen = _FrozenDict()
        for key, item in value.items():
            dict.__setitem__(frozen, key, _freeze(item))
        return frozen
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


class SupervisorDecision(BaseModel):
    """监督模型唯一可以返回的决策。"""

    model_config = ConfigDict(extra="forbid")

    action: SupervisorAction
    reason: str = Field(min_length=1, max_length=2_000)


class SupervisorInputSnapshot(BaseModel):
    """监督调用的不可变、脱敏输入快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    campaign_id: str = Field(min_length=1, max_length=128)
    analysis_digest: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=128)
    timeframe: str = Field(min_length=1, max_length=32)
    closed_bar_ts_open_ms: int = Field(ge=0)
    closed_bar: dict[str, Any]
    stage1_diagnosis: dict[str, Any]
    stage2_decision: dict[str, Any]
    active_execution_count: int = Field(ge=0)
    account_equity_usdt: str = Field(min_length=1, max_length=64)
    max_buy: str = Field(min_length=1, max_length=64)
    max_sell: str = Field(min_length=1, max_length=64)
    technical_plan_quantity: str = Field(min_length=1, max_length=64)

    @field_validator(
        "closed_bar",
        "stage1_diagnosis",
        "stage2_decision",
        mode="after",
    )
    @classmethod
    def _deep_freeze_mapping(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _freeze(value)


class SupervisorDecisionRecord(BaseModel):
    """与一根 K 线和一份 PA 分析一一对应的监督结论。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    record_id: str = Field(min_length=1, max_length=256)
    campaign_id: str = Field(min_length=1, max_length=128)
    analysis_digest: str = Field(min_length=1, max_length=128)
    closed_bar_ts_open_ms: int = Field(ge=0)
    input_snapshot_digest: str = Field(min_length=1, max_length=128)
    input_snapshot: SupervisorInputSnapshot
    action: SupervisorAction
    reason: str = Field(min_length=1, max_length=2_000)
    profile_id: str = Field(max_length=128)
    model_id: str = Field(max_length=256)
    fallback_level: SupervisorFallbackLevel
    created_at: str = Field(min_length=1, max_length=128)


def snapshot_digest(snapshot: SupervisorInputSnapshot) -> str:
    """Return the stable digest sent to both primary and backup models."""

    encoded = json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
