"""入场和主动离场的下单方式及价格调整规则。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

EntryOrderMode = Literal["signal", "limit", "limit_with_slippage", "market"]
ExitOrderMode = Literal["limit", "limit_with_slippage", "market"]

ENTRY_ORDER_MODES = frozenset(
    {"signal", "limit", "limit_with_slippage", "market"}
)
EXIT_ORDER_MODES = frozenset({"limit", "limit_with_slippage", "market"})


def normalise_entry_order_mode(value: object) -> EntryOrderMode:
    mode = str(value or "signal").strip().lower()
    if mode not in ENTRY_ORDER_MODES:
        raise ValueError(f"未知入场下单方式：{mode}")
    return mode  # type: ignore[return-value]


def normalise_exit_order_mode(value: object) -> ExitOrderMode:
    mode = str(value or "market").strip().lower()
    if mode not in EXIT_ORDER_MODES:
        raise ValueError(f"未知主动离场下单方式：{mode}")
    return mode  # type: ignore[return-value]


def effective_entry_type(
    signal_entry_type: Literal["limit", "market", "breakout"],
    mode: EntryOrderMode,
) -> Literal["limit", "market", "breakout"]:
    """把用户选择转换成实际入口；signal 保留 PA 原始类型。"""
    if mode == "signal":
        return signal_entry_type
    if mode in {"limit", "limit_with_slippage"}:
        return "limit"
    return "market"


def _positive_atr(value: object) -> Decimal:
    try:
        atr = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("ATR 必须是数字") from exc
    if not atr.is_finite() or atr <= 0:
        raise ValueError("ATR 必须是有限正数")
    return atr


def _nonnegative_atr_multiple(value: object) -> Decimal:
    try:
        multiple = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("ATR 滑点倍数必须是数字") from exc
    if not multiple.is_finite() or multiple < 0:
        raise ValueError("ATR 滑点倍数必须是有限非负数")
    return multiple


def _apply_atr_slippage(
    price: Decimal,
    direction: str,
    atr: object,
    multiple: object,
    *,
    entry: bool,
) -> Decimal:
    """按当前 ATR 快照移动限价；不负责券商最小价位对齐。

    入场价向成交侧移动：多头上移、空头下移；主动离场价反向移动。
    """
    if direction not in {"long", "short"}:
        raise ValueError(f"未知交易方向：{direction}")
    amount = _positive_atr(atr) * _nonnegative_atr_multiple(multiple)
    sign = 1 if direction == "long" else -1
    if not entry:
        sign *= -1
    adjusted = price + (Decimal(sign) * amount)
    if not adjusted.is_finite() or adjusted <= 0:
        raise ValueError("ATR 滑点调整后的价格必须为正数")
    return adjusted


def apply_entry_atr_slippage(
    price: Decimal,
    direction: str,
    atr: object,
    multiple: object,
) -> Decimal:
    return _apply_atr_slippage(price, direction, atr, multiple, entry=True)


def apply_exit_atr_slippage(
    price: Decimal,
    direction: str,
    atr: object,
    multiple: object,
) -> Decimal:
    return _apply_atr_slippage(price, direction, atr, multiple, entry=False)
