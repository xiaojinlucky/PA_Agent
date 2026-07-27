"""Pydantic v2 data models for PA Agent records persistence.

Defines the canonical schema for analysis records, followup turns,
alarm payloads, validation errors, and experience entries.
"""

from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_sidecar_record_id(value: object) -> str:
    """Validate a record basename before it is joined to the pending directory."""

    record_id = str(value or "").strip()
    if (
        not record_id
        or record_id in {".", ".."}
        or "/" in record_id
        or "\\" in record_id
        or any(ord(char) < 32 or ord(char) == 127 for char in record_id)
        or len(record_id) > 240
    ):
        raise ValueError("invalid analysis record id")
    return record_id


class RecordMeta(BaseModel):
    """Metadata captured at the moment of analysis submission."""

    model_config = ConfigDict(extra="forbid")

    timestamp_local_iso: str  # Local time ISO string, used for filename
    timestamp_local_ms: int   # Local time in milliseconds
    symbol: str
    timeframe: str
    # 价格来源必须与执行路由一起耐久化，不能只靠分析结束后的当前 GUI 设置猜测。
    data_source: str = "unknown"
    # 非秘密行情来源说明；10m 聚合不能被误称为 OKX 原生周期。
    market_data_provenance: str = "unknown"
    bar_count: int
    ai_provider: dict         # Sanitized provider config snapshot (no plaintext API key)
    decision_stance: str = "conservative"  # conservative | balanced | aggressive | extreme_aggressive
    # 只有受控 Campaign 分析填写；交互式 GUI 保持 None。用于恢复时证明
    # pending 记录确实属于当前 Campaign，不能只按标的/周期猜所有权。
    campaign_id: str | None = None

    @field_validator("campaign_id")
    @classmethod
    def _validate_campaign_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(UUID(str(value)))


class AnalysisRecord(BaseModel):
    """Full record of a two-stage AI analysis run."""

    model_config = ConfigDict(extra="forbid")

    meta: RecordMeta
    kline_data: list[dict]              # Same data as sent to AI
    htf_text: str
    # 最新已收盘主周期 K 线对应的 ATR14 快照；执行滑点由它乘以用户倍数得到。
    analysis_atr14: float | None = None
    # 分析时行情源声明的真实最小价格跳动；用于证明价位精度校验没有
    # 从 K 线小数位猜测。
    analysis_price_tick: str | None = None
    stage1_messages: list[dict]
    stage1_response: Optional[dict]     # Raw response (includes reasoning_content)
    stage1_diagnosis: Optional[dict]
    stage2_messages: list[dict]
    stage2_response: Optional[dict]
    stage2_decision: Optional[dict]
    strategy_files_used: list[str]
    experience_loaded: list[dict]
    exception: Optional[dict]           # If error occurred: category + debug info
    usage_total: dict                   # Cumulative usage for audit


class FollowupTurn(BaseModel):
    """A single turn in the post-analysis free-chat session."""

    model_config = ConfigDict(extra="forbid")

    turn: int
    ts_ms: int
    user: str
    ai_content: str
    ai_reasoning: Optional[str]
    usage: dict
    cancelled: bool = False


class ConversationCheckpoint(BaseModel):
    """可恢复的官方模型线程定位信息；不含登录令牌或 API Key。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    record_id: str
    provider_adapter: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    thread_id: str
    last_turn: int = Field(default=0, ge=0)
    updated_at_ms: int = Field(ge=0)

    @field_validator("record_id", mode="before")
    @classmethod
    def _validate_record_id(cls, value: object) -> str:
        return validate_sidecar_record_id(value)

    @field_validator("thread_id", mode="before")
    @classmethod
    def _validate_thread_id(cls, value: object) -> str:
        try:
            return str(UUID(str(value or "").strip()))
        except (ValueError, AttributeError) as exc:
            raise ValueError("invalid Codex thread id") from exc


class AlarmPayload(BaseModel):
    """Payload emitted when a JSON validation alarm is triggered (R8.6)."""

    model_config = ConfigDict(extra="forbid")

    category: str                       # 'a'..'e'
    stage: str                          # '阶段一-诊断' or '阶段二-决策'
    timestamp_local_iso: str
    raw_text: str
    parse_position: Optional[str]
    missing_fields: list[str]
    invalid_fields: list[str]
    consecutive_count: int
    history_excerpt: list[dict]


class ValidationError(BaseModel):
    """Structured validation error produced by JsonValidator.

    Note: this is a Pydantic model, not the built-in exception class.
    """

    model_config = ConfigDict(extra="forbid")

    category: str                       # 'a', 'b', 'c', or 'd'
    missing_fields: list[str] = []
    invalid_fields: list[str] = []
    raw_text: str
    parse_position: Optional[str] = None
    allowed_values: dict = {}


class ExperienceEntry(BaseModel):
    """A single entry loaded from the experience library."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    case_type: str                      # 'success' or 'failure'
    cycle_position: str
    timestamp_ms: int
    content: dict                       # Parsed JSON content of the experience file
