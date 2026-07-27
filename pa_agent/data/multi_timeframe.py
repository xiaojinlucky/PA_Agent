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
    # OKX 无原生 40m/160m；10m 主周期使用可直接验证的 1h/4h 薄背景。
    "10m": ("1h", "4h"),
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


def _recent_position_label(frame: KlineFrame, *, window: int = 20) -> str:
    """Return the latest close's coarse position in the recent HTF range."""
    bars = tuple(frame.bars[:window])
    if len(bars) < 5:
        return "数据不足"
    highs: list[float] = []
    lows: list[float] = []
    close = _finite(bars[0].close)
    for bar in bars:
        high = _finite(bar.high)
        low = _finite(bar.low)
        if high is None or low is None:
            continue
        highs.append(max(high, low))
        lows.append(min(high, low))
    if close is None or not highs or not lows:
        return "无法判断"
    upper = max(highs)
    lower = min(lows)
    width = upper - lower
    if width <= 0:
        return "无法判断"
    ratio = (close - lower) / width
    if ratio >= 0.8:
        return "靠近近期高位"
    if ratio <= 0.2:
        return "靠近近期低位"
    return "区间中部"


def _structure_label(frame: KlineFrame, *, window: int = 20) -> str:
    """Return a deliberately coarse HTF structure label for model context only."""
    bars = tuple(frame.bars[:window])
    if len(bars) < 8:
        return "数据不足"
    closes = [_finite(bar.close) for bar in bars]
    if any(value is None for value in closes):
        return "无法判断"
    atr = _finite(frame.indicators.atr14[0]) if frame.indicators.atr14 else None
    net_move = abs(float(closes[0]) - float(closes[-1]))
    net_move_atr = net_move / atr if atr is not None and atr > 0 else None

    overlaps: list[float] = []
    for current, previous in zip(bars, bars[1:], strict=False):
        current_high = _finite(current.high)
        current_low = _finite(current.low)
        previous_high = _finite(previous.high)
        previous_low = _finite(previous.low)
        if None in (current_high, current_low, previous_high, previous_low):
            continue
        high = max(current_high, previous_high)
        low = min(current_low, previous_low)
        union = high - low
        if union <= 0:
            continue
        overlap = max(
            0.0,
            min(current_high, previous_high) - max(current_low, previous_low),
        )
        overlaps.append(overlap / union)
    if net_move_atr is None or len(overlaps) < 3:
        return "过渡/不清晰"
    mean_overlap = sum(overlaps) / len(overlaps)
    if mean_overlap >= 0.55 and net_move_atr < 1.5:
        return "区间/整理倾向"
    if mean_overlap <= 0.35 and net_move_atr >= 1.5:
        return "趋势/通道倾向"
    return "过渡/混合"


#: 连续多少根未触及 EMA20 即视为趋势极强（原始资料的 20GB）。
_GAP_BAR_THRESHOLD = 20


def _gap_bar_streak(frame: KlineFrame) -> int:
    """从最新已收盘 K 线往回数，连续多少根完全没触及 EMA20。

    触及 = 该根的 [low, high] 区间覆盖到当根 EMA20。缺 EMA 或价格
    非有限值时立即停止计数，不猜测。
    """
    ema_series = frame.indicators.ema20 if frame.indicators else ()
    streak = 0
    for index, bar in enumerate(frame.bars):
        if index >= len(ema_series):
            break
        ema = _finite(ema_series[index])
        high = _finite(bar.high)
        low = _finite(bar.low)
        if ema is None or high is None or low is None:
            break
        if min(low, high) <= ema <= max(low, high):
            break
        streak += 1
    return streak


def _nearest_levels(
    frame: KlineFrame, *, window: int = 40
) -> tuple[tuple[float, int] | None, tuple[float, int] | None]:
    """返回最新收盘价上方最近阻力与下方最近支撑（价格，来自第几根）。

    只用真实 swing 极点：某根的高点高于左右相邻两根即为 swing high，
    低点低于左右相邻两根即为 swing low。找不到就返回 None，不编造价位。
    """
    bars = tuple(frame.bars[:window])
    if len(bars) < 3:
        return None, None
    close = _finite(bars[0].close)
    if close is None:
        return None, None

    resistance: tuple[float, int] | None = None
    support: tuple[float, int] | None = None
    for i in range(1, len(bars) - 1):
        bar, newer, older = bars[i], bars[i - 1], bars[i + 1]
        high = _finite(bar.high)
        low = _finite(bar.low)
        newer_high, newer_low = _finite(newer.high), _finite(newer.low)
        older_high, older_low = _finite(older.high), _finite(older.low)
        if None in (high, low, newer_high, newer_low, older_high, older_low):
            continue
        seq = int(getattr(bar, "seq", i + 1) or i + 1)
        if high > newer_high and high > older_high and high > close:
            if resistance is None or high < resistance[0]:
                resistance = (high, seq)
        if low < newer_low and low < older_low and low < close:
            if support is None or low > support[0]:
                support = (low, seq)
    return resistance, support


def _levels_label(frame: KlineFrame) -> str:
    resistance, support = _nearest_levels(frame)
    parts: list[str] = []
    if resistance is not None:
        parts.append(f"上方最近阻力 {resistance[0]:.6g}（K{resistance[1]}）")
    if support is not None:
        parts.append(f"下方最近支撑 {support[0]:.6g}（K{support[1]}）")
    return "；".join(parts) if parts else "窗口内无明确 swing 极点"


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
    structure = _structure_label(frame)
    position = _recent_position_label(frame)
    line = (
        f"- {role} {frame.timeframe}: 标签={_direction_label(frame)}；"
        f"最新已收盘={ts}；收盘={close if close is not None else '—'}；"
        f"EMA20={ema if ema is not None else '—'}（收盘在其{close_side}）；"
        f"ATR14={atr if atr is not None else '—'}；"
        f"结构={structure}；位置={position}；"
        f"关键位置={_levels_label(frame)}"
    )
    streak = _gap_bar_streak(frame)
    if streak >= _GAP_BAR_THRESHOLD:
        line += f"；20GB=连续 {streak} 根未触及 EMA20（趋势极强，回撤到 EMA 前不宜逆势）"
    return line


def render_higher_timeframe_context(
    main_frame: KlineFrame,
    higher_frames: Mapping[str, KlineFrame],
) -> str:
    """Render compact context for prompts and durable records."""
    lines = [
        f"主周期={main_frame.timeframe}（当前交易决策周期）",
        "高周期只提供方向、结构、位置和波动背景，不直接否决主周期方向、不自动改置信度、仓位或价格：",
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
        "回撤还是高周期区间中的运动；若关系不清就如实写不确定。若不一致，写入风险说明，"
        "不要求多周期共识，不把标签变成硬闸门，也不切换低周期拼接触发理由。"
    )
    return "\n".join(lines)
