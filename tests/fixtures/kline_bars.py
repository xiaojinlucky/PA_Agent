"""Synthetic newest-first K-line lists for tests."""
from __future__ import annotations

from pa_agent.data.base import KlineBar


def make_newest_first_bars(
    n: int,
    *,
    base_ts: float = 1_700_000_000.0,
    step_sec: float = 900.0,
    with_forming: bool = True,
    trend_step: float = 0.0,
) -> list[KlineBar]:
    """Build newest-first bars; positive ``trend_step`` creates a rising market."""
    bars: list[KlineBar] = []
    if with_forming:
        forming_shift = 2.0 * trend_step
        bars.append(
            KlineBar(
                seq=1,
                ts_open=base_ts,
                open=2000.0 + forming_shift,
                high=2010.0 + forming_shift,
                low=1990.0 + forming_shift,
                close=2005.0 + forming_shift,
                volume=100.0,
                closed=False,
            )
        )
    start = 2 if with_forming else 1
    for seq in range(start, start + n):
        age = seq - start
        price_shift = (1.0 - age) * trend_step
        bars.append(
            KlineBar(
                seq=seq,
                ts_open=base_ts - (seq - 1) * step_sec,
                open=2000.0 + price_shift,
                high=2010.0 + price_shift,
                low=1990.0 + price_shift,
                close=2005.0 + price_shift,
                volume=100.0,
                closed=True,
            )
        )
    return bars
