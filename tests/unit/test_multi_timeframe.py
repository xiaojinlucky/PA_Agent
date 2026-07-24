from __future__ import annotations

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.data.multi_timeframe import (
    higher_timeframes_for,
    render_higher_timeframe_context,
)


def _frame(
    timeframe: str,
    close: float,
    ema: float,
    old_ema: float,
    *,
    count: int = 5,
) -> KlineFrame:
    bars = tuple(
        KlineBar(
            seq=index + 1,
            ts_open=1_784_300_400_000 - index * 900_000,
            open=close - 1,
            high=close + 2,
            low=close - 2,
            close=close,
            volume=1,
            closed=True,
        )
        for index in range(count)
    )
    return KlineFrame(
        symbol="XAU-USDT-SWAP",
        timeframe=timeframe,
        bars=bars,
        indicators=IndicatorBundle(
            ema20=tuple([ema] * 4 + [old_ema] + [ema] * max(0, count - 5)),
            atr14=tuple([3.5] * count),
        ),
        snapshot_ts_local_ms=1_784_300_400_000,
    )


def test_15m_uses_one_hour_and_four_hour_background() -> None:
    assert higher_timeframes_for("15m") == ("1h", "4h")


def test_10m_uses_real_okx_one_hour_and_four_hour_background() -> None:
    assert higher_timeframes_for("10m") == ("1h", "4h")


def test_context_is_thin_and_does_not_turn_high_timeframe_into_veto() -> None:
    text = render_higher_timeframe_context(
        _frame("15m", 100, 99, 98),
        {
            "1h": _frame("1h", 110, 111, 112),
            "4h": _frame("4h", 120, 119, 118),
        },
    )

    assert "主周期=15m" in text
    assert "背景 1h" in text
    assert "背景 4h" in text
    assert "不直接否决主周期方向" in text
    assert "结构=" in text
    assert "位置=" in text
    assert "不要求多周期共识" in text
    assert "K线数据" not in text


def test_context_adds_coarse_structure_and_recent_position() -> None:
    text = render_higher_timeframe_context(
        _frame("10m", 100, 99, 98),
        {"1h": _frame("1h", 110, 111, 112, count=20)},
    )

    assert "结构=区间/整理倾向" in text
    assert "位置=区间中部" in text
