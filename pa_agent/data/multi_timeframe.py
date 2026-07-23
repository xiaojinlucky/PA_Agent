"""多周期背景标签实验。

这里只生成薄的、可审计的高周期背景，不替主周期做交易闸门。
主周期仍负责当前 PA 决策；高周期只回答“主周期可能嵌在哪个更大结构里”。
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from pa_agent.data.base import KlineFrame
from pa_agent.data.datetime_ts import format_epoch_for_display

_HIGHER_TIMEFRAME_MAP: dict[str, tuple[str, ...]] = {
    "1m": ("5m", "15m"),
    "3m": ("15m", "1h"),
    "5m": ("15m", "1h"),
    "15m": ("1h", "4h"),
    "30m": ("2h", "4h"),
    "1h": ("4h", "1d"),
    "2h": ("4h", "1d"),
    "4h": ("1d", "1w"),
    "1d": ("1w",),
}


def higher_timeframes_for(main_timeframe: str) -> tuple[str, ...]:
    """Return the first two useful higher frames for a main timeframe."""
    key = str(main_timeframe or "").strip().lower()
    return _HIGHER_TIMEFRAME_MAP.get(key, ())


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _direction_label(frame: KlineFrame) -> str:
    if not frame.bars:
        return "无数据"
    close = _finite(frame.bars[0].close)
    ema = _finite(frame.indicators.ema20[0]) if frame.indicators.ema20 else None
    if close is None or ema is None or ema <= 0:
        return "无法判断"
    old = (
        _finite(frame.indicators.ema20[4])
        if len(frame.indicators.ema20) > 4
        else None
    )
    if old is None:
        slope = 0
    elif ema > old:
        slope = 1
    elif ema < old:
        slope = -1
    else:
        slope = 0
    side = 1 if close > ema else -1 if close < ema else 0
    if side > 0 and slope >= 0:
        return "偏多"
    if side < 0 and slope <= 0:
        return "偏空"
    return "混合/过渡"


def _render_frame_line(frame: KlineFrame, *, role: str) -> str:
    if not frame.bars:
        return f"- {role} {frame.timeframe}: 无已收盘数据"
    bar = frame.bars[0]
    close = _finite(bar.close)
    ema = _finite(frame.indicators.ema20[0]) if frame.indicators.ema20 else None
    atr = _finite(frame.indicators.atr14[0]) if frame.indicators.atr14 else None
    ts = format_epoch_for_display(bar.ts_open, short=True)
    close_side = "上方" if close is not None and ema is not None and close > ema else (
        "下方" if close is not None and ema is not None and close < ema else "附近"
    )
    return (
        f"- {role} {frame.timeframe}: 标签={_direction_label(frame)}；"
        f"最新已收盘={ts}；收盘={close if close is not None else '—'}；"
        f"EMA20={ema if ema is not None else '—'}（收盘在其{close_side}）；"
        f"ATR14={atr if atr is not None else '—'}"
    )


def render_higher_timeframe_context(
    main_frame: KlineFrame,
    higher_frames: Mapping[str, KlineFrame],
) -> str:
    """Render compact context for prompts and durable records."""
    lines = [
        f"主周期={main_frame.timeframe}（当前交易决策周期）",
        "高周期只提供背景标签，不直接否决主周期方向、不自动改仓位：",
    ]
    for timeframe in higher_timeframes_for(main_frame.timeframe):
        frame = higher_frames.get(timeframe)
        if frame is None:
            lines.append(f"- 背景 {timeframe}: 本轮未获得数据")
        else:
            lines.append(_render_frame_line(frame, role="背景"))
    if len(lines) == 2:
        lines.append("- 无配置的高周期背景")
    lines.append(
        "解释顺序：先读主周期的趋势/区间/位置，再判断主周期是高周期趋势延续、"
        "回撤还是高周期区间中的运动；若不一致，写入风险说明，不把标签变成硬闸门。"
    )
    return "\n".join(lines)
