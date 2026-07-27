"""高周期背景的关键位置与 20GB 标记测试。"""
from __future__ import annotations

import math

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.data.multi_timeframe import (
    _gap_bar_streak,
    _nearest_levels,
    render_higher_timeframe_context,
)


def _bar(seq: int, *, high: float, low: float, close: float) -> KlineBar:
    return KlineBar(
        seq=seq,
        ts_open=float(1_700_000_000_000 - seq * 3_600_000),
        open=(high + low) / 2,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        closed=True,
    )


def _frame(bars: tuple[KlineBar, ...], ema: tuple[float, ...]) -> KlineFrame:
    return KlineFrame(
        symbol="AAPL.US",
        timeframe="1h",
        bars=bars,
        indicators=IndicatorBundle(
            ema20=ema, atr14=tuple(1.0 for _ in range(len(bars)))
        ),
        snapshot_ts_local_ms=1_700_000_000_000,
    )


def _flat_bars(count: int, *, high: float, low: float, close: float):
    return tuple(_bar(i + 1, high=high, low=low, close=close) for i in range(count))


def test_nearest_levels_picks_closest_swing_on_each_side():
    # K1 收盘 100；K3 是 swing high 110，K5 是更远的 swing high 120（应取 110）
    # K7 是 swing low 90，K9 是更远的 swing low 80（应取 90）
    bars = (
        _bar(1, high=101, low=99, close=100),
        _bar(2, high=105, low=100, close=103),
        _bar(3, high=110, low=104, close=108),   # swing high
        _bar(4, high=106, low=101, close=104),
        _bar(5, high=120, low=105, close=118),   # 更远的 swing high
        _bar(6, high=104, low=95, close=98),
        _bar(7, high=96, low=90, close=93),      # swing low
        _bar(8, high=99, low=94, close=97),
        _bar(9, high=95, low=80, close=85),      # 更远的 swing low
        _bar(10, high=100, low=92, close=96),
    )
    resistance, support = _nearest_levels(_frame(bars, tuple(100.0 for _ in bars)))
    assert resistance is not None and math.isclose(resistance[0], 110)
    assert resistance[1] == 3
    assert support is not None and math.isclose(support[0], 90)
    assert support[1] == 7


def test_nearest_levels_returns_none_without_swings():
    """没有真实 swing 极点时不编造价位。"""
    bars = _flat_bars(6, high=101, low=99, close=100)
    resistance, support = _nearest_levels(_frame(bars, tuple(100.0 for _ in bars)))
    assert resistance is None
    assert support is None


def test_gap_bar_streak_counts_until_ema_touched():
    # 前 5 根完全在 EMA 上方，第 6 根触及 EMA → streak = 5
    bars = _flat_bars(10, high=120, low=110, close=115)
    ema = tuple([100.0] * 5 + [115.0] + [100.0] * 4)
    assert _gap_bar_streak(_frame(bars, ema)) == 5


def test_gap_bar_streak_stops_on_missing_ema():
    """缺 EMA 立即停止计数，不猜测。"""
    bars = _flat_bars(6, high=120, low=110, close=115)
    ema = (100.0, 100.0, float("nan"), 100.0, 100.0, 100.0)
    assert _gap_bar_streak(_frame(bars, ema)) == 2


def test_context_marks_20gb_when_streak_reaches_threshold():
    bars = _flat_bars(25, high=120, low=110, close=115)
    frame = _frame(bars, tuple(100.0 for _ in bars))
    text = render_higher_timeframe_context(frame, {"4h": frame})
    assert "20GB=连续" in text
    assert "趋势极强" in text


def test_context_omits_20gb_below_threshold():
    bars = _flat_bars(25, high=120, low=110, close=115)
    # 第 10 根触及 EMA → streak = 9，低于阈值 20
    ema = tuple([100.0] * 9 + [115.0] + [100.0] * 15)
    frame = _frame(bars, ema)
    text = render_higher_timeframe_context(frame, {"4h": frame})
    assert "20GB" not in text


def test_context_reports_levels_and_stays_descriptive():
    bars = (
        _bar(1, high=101, low=99, close=100),
        _bar(2, high=105, low=100, close=103),
        _bar(3, high=110, low=104, close=108),
        _bar(4, high=106, low=101, close=104),
        _bar(5, high=103, low=95, close=98),
        _bar(6, high=96, low=90, close=93),
        _bar(7, high=99, low=94, close=97),
    )
    frame = _frame(bars, tuple(100.0 for _ in bars))
    text = render_higher_timeframe_context(frame, {"4h": frame})
    assert "关键位置=" in text
    assert "上方最近阻力" in text
    # 高周期只描述不裁决：既有免责语必须仍在
    assert "不直接否决主周期方向" in text


def test_context_handles_insufficient_higher_frame_data():
    bars = _flat_bars(2, high=101, low=99, close=100)
    frame = _frame(bars, (100.0, 100.0))
    text = render_higher_timeframe_context(frame, {"4h": frame})
    assert "关键位置=窗口内无明确 swing 极点" in text
