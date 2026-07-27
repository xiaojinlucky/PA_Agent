from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from pa_agent.data.mt5 import MT5Source


def _rates() -> np.ndarray:
    dtype = [
        ("time", "i8"),
        ("open", "f8"),
        ("high", "f8"),
        ("low", "f8"),
        ("close", "f8"),
        ("tick_volume", "f8"),
        ("spread", "i8"),
        ("real_volume", "f8"),
    ]
    return np.array(
        [
            (1, 100, 101, 99, 100, 1, 0, 1),
            (2, 100, 102, 99, 101, 2, 0, 2),
            (3, 101, 103, 100, 102, 3, 0, 3),
        ],
        dtype=dtype,
    )


def test_mt5_higher_timeframe_read_keeps_main_subscription() -> None:
    source = MT5Source()
    source._connected = True
    source._symbol = "XAUUSD"
    source._timeframe = "15m"

    with (
        patch("MetaTrader5.TIMEFRAME_H1", 60),
        patch("MetaTrader5.symbol_select"),
        patch(
            "MetaTrader5.symbol_info",
            return_value=SimpleNamespace(trade_tick_size=0.01),
        ),
        patch("MetaTrader5.copy_rates_from_pos", return_value=_rates()) as fetch,
    ):
        bars = source.latest_snapshot_for_timeframe("1h", 2)

    assert len(bars) == 2
    assert all(bar.price_tick == "0.01" for bar in bars)
    assert source._timeframe == "15m"
    fetch.assert_called_once_with("XAUUSD", 60, 0, 3)
