from __future__ import annotations

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.data.multi_timeframe import (
    higher_timeframes_for,
    render_higher_timeframe_context,
)


def _frame(timeframe: str, close: float, ema: float, old_ema: float) -> KlineFrame:
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
        for index in range(5)
    )
    return KlineFrame(
        symbol="XAU-USDT-SWAP",
        timeframe=timeframe,
        bars=bars,
        indicators=IndicatorBundle(
            ema20=(ema, ema, ema, ema, old_ema),
            atr14=(3.5, 3.5, 3.5, 3.5, 3.5),
        ),
        snapshot_ts_local_ms=1_784_300_400_000,
    )


def test_15m_uses_one_hour_and_four_hour_background() -> None:
    assert higher_timeframes_for("15m") == ("1h", "4h")


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
    assert "K线数据" not in text
