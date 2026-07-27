"""Price helpers.

``infer_price_tick_from_frame`` is only a display/feature heuristic. Safety
validation must use the exchange-declared tick on ``KlineFrame.price_tick``.
"""
from __future__ import annotations

import math
import re
from typing import Any


def infer_price_tick_from_frame(kline_frame: Any) -> float | None:
    """Prefer a declared tick, else guess for non-safety display/features only."""
    try:
        declared = float(getattr(kline_frame, "price_tick", None))
    except (TypeError, ValueError):
        declared = 0.0
    if math.isfinite(declared) and declared > 0:
        return declared

    bars = getattr(kline_frame, "bars", None) if kline_frame is not None else None
    if not bars:
        return None

    max_decimals = 0
    for bar in bars:
        for attr in ("open", "high", "low", "close"):
            try:
                value = float(getattr(bar, attr))
            except (TypeError, ValueError):
                continue
            text = f"{value:.12f}".rstrip("0")
            if "." in text:
                max_decimals = max(max_decimals, len(text.split(".")[1]))

    if max_decimals <= 0:
        return 1.0
    return 10 ** (-min(max_decimals, 6))


def parse_k_seq(value: object) -> int | None:
    if value is None:
        return None
    m = re.search(r"K\s*(\d+)", str(value), flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


def format_breakout_tick_hint(kline_frame: Any) -> str:
    """One-line Stage-2 hint using only the exchange-declared tick."""
    from pa_agent.ai.claim_validation import declared_price_tick

    tick = declared_price_tick(kline_frame)
    if tick is None:
        return (
            "**真实品种 tick 当前不可用**：禁止猜测最小跳动，也禁止输出可执行"
            "价位；本轮必须 `order_type=不下单`。"
        )
    tick_s = f"{tick:g}"
    return (
        f"**突破单定价（交易所声明的真实最小跳动 = {tick_s}）**：做多时 "
        f"`entry_price` 必须 **严格大于** `entry_basis_bar` 的 high，"
        f"推荐 `entry_price = 该 K 线 high + {tick_s}`（禁止等于 high）；"
        f"做空时 `entry_price` 必须 **严格低于** low，推荐 `low - {tick_s}`。"
        f"`entry_rule` 必须写明：`K{{n}} low/high = {{实际价格}}，entry = {{实际价格}} ± {tick_s}`，"
        f"勿重复 order_type/方向长句。所有价位必须是 {tick_s} 的整数倍；"
        f"**程序不会替你改价，错误会直接拒绝整轮。**"
    )
