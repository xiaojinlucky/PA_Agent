"""Canonical models shared by execution services and broker adapters."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pa_agent.execution.order_modes import EntryOrderMode, ExitOrderMode


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ExecutionState(StrEnum):
    READY = "ready"
    SUBMITTING = "submitting"
    ENTRY_PENDING = "entry_pending"
    PARTIALLY_FILLED = "partially_filled"
    PROTECTING = "protecting"
    OPEN = "open"
    EXIT_PENDING = "exit_pending"
    CLOSED = "closed"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    ERROR = "error"


ACTIVE_EXECUTION_STATES = frozenset(
    {
        ExecutionState.SUBMITTING,
        ExecutionState.ENTRY_PENDING,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.PROTECTING,
        ExecutionState.OPEN,
        ExecutionState.EXIT_PENDING,
        ExecutionState.UNKNOWN,
    }
)


class ExecutionPlan(BaseModel):
    """Deterministic, user-configured order plan derived from a durable analysis."""

    model_config = ConfigDict(extra="forbid")

    id: str
    analysis_digest: str
    analysis_record_path: str
    broker: Literal["longbridge", "okx"]
    environment: Literal["live", "demo"]
    product: Literal["securities", "spot", "swap"]
    requested_account: Literal["paper", "comprehensive", "intraday", "okx"]
    allow_account_fallback: bool = False
    source_symbol: str
    instrument: str
    direction: Literal["long", "short"]
    entry_type: Literal["limit", "market", "breakout"]
    quantity: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    take_profit_1: Decimal = Field(gt=0)
    take_profit_2: Decimal = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    trade_confidence: int = Field(ge=0, le=100)
    created_at: str
    config_fingerprint: str
    okx_api_base_url: str = ""
    okx_margin_mode: Literal["", "cross", "isolated"] = ""
    longbridge_allow_outside_rth: bool = False
    entry_timeout_seconds: int = Field(default=120, ge=10, le=86_400)
    # signal 只用于兼容历史计划；界面提供的实际选择是其余三种方式。
    entry_order_mode: EntryOrderMode = "signal"
    exit_order_mode: ExitOrderMode = "market"
    # limit_with_slippage 使用分析节点捕获的 ATR 快照，不使用固定基点。
    entry_atr: Decimal | None = Field(default=None, gt=0)
    entry_slippage_atr_multiple: Decimal = Field(
        default=Decimal("0.50"), ge=0, le=5
    )
    exit_slippage_atr_multiple: Decimal = Field(
        default=Decimal("0.50"), ge=0, le=5
    )


class PreflightResult(BaseModel):
    """Broker-verified order parameters; no write has occurred yet."""

    model_config = ConfigDict(extra="forbid")

    selected_account: str
    account_identity: str = ""
    quantity: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    take_profit_1: Decimal = Field(gt=0)
    take_profit_2: Decimal = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    price_tick: Decimal | None = None
    quantity_step: Decimal | None = None
    minimum_quantity: Decimal | None = None
    warnings: list[str] = Field(default_factory=list)
    broker_metadata: dict = Field(default_factory=dict)


class OrderSnapshot(BaseModel):
    """Normalized broker order status."""

    model_config = ConfigDict(extra="forbid")

    broker_order_id: str
    client_order_id: str = ""
    state: Literal[
        "pending",
        "partially_filled",
        "filled",
        "canceled",
        "rejected",
        "unknown",
    ]
    submitted_quantity: Decimal = Decimal("0")
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    message: str = ""
    raw: dict = Field(default_factory=dict)


class PositionSnapshot(BaseModel):
    """Normalized broker/account position."""

    model_config = ConfigDict(extra="forbid")

    instrument: str
    direction: Literal["long", "short", "flat"]
    quantity: Decimal
    available_quantity: Decimal | None = None
    average_price: Decimal | None = None
    mark_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    currency: str = ""
    leverage: Decimal | None = None
    raw: dict = Field(default_factory=dict)


class AccountSnapshot(BaseModel):
    """Funds, positions and P/L shown back in PA."""

    model_config = ConfigDict(extra="forbid")

    broker: Literal["longbridge", "okx"]
    account_profile: str
    captured_at: str = Field(default_factory=utc_now_iso)
    base_currency: str = ""
    equity: Decimal | None = None
    cash: Decimal | None = None
    available: Decimal | None = None
    buying_power: Decimal | None = None
    total_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    positions: list[PositionSnapshot] = Field(default_factory=list)
    raw_summary: dict = Field(default_factory=dict)


class ExecutionRecord(BaseModel):
    """Durable lifecycle state stored in SQLite after every transition."""

    model_config = ConfigDict(extra="forbid")

    id: str
    plan: ExecutionPlan
    state: ExecutionState = ExecutionState.READY
    selected_account: str = ""
    account_identity: str = ""
    preflight: PreflightResult | None = None
    client_order_id: str = ""
    broker_order_id: str = ""
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    pnl_currency: str = ""
    broker_state: dict = Field(default_factory=dict)
    state_reason: str = ""
    last_error: str = ""
    needs_attention: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    revision: int = 0


class ExecutionEvent(BaseModel):
    """Append-only audit event."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    kind: str
    created_at: str = Field(default_factory=utc_now_iso)
    payload: dict = Field(default_factory=dict)
