from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.gui.chart_widget import ChartWidget, MarketTimeAxisItem


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _frame() -> KlineFrame:
    bars = (
        KlineBar(
            seq=1,
            ts_open=1_700_000_000_000,
            open=100,
            high=103,
            low=99,
            close=102,
            volume=1,
        ),
        KlineBar(
            seq=2,
            ts_open=1_699_999_400_000,
            open=102,
            high=103,
            low=98,
            close=99,
            volume=1,
        ),
    )
    return KlineFrame(
        symbol="AAPL.US",
        timeframe="10m",
        bars=bars,
        indicators=IndicatorBundle(
            ema20=(101.0, 100.0),
            atr14=(1.0, 1.0),
        ),
        snapshot_ts_local_ms=1_700_000_000_000,
    )


def test_market_mode_uses_prd_chart_contract(qapp) -> None:
    chart = ChartWidget(market_read_only=True)
    chart.set_frame_now(_frame(), fit_view=True)

    plot = chart.getPlotItem()
    assert plot.getAxis("right").isVisible()
    assert not plot.getAxis("left").isVisible()
    assert isinstance(plot.getAxis("bottom"), MarketTimeAxisItem)
    assert chart._show_ema is False
    assert chart._show_sequence_labels is False
    assert chart._up_color == "#E5484D"
    assert chart._down_color == "#2EBD85"
    assert chart._ema_line is None
    assert chart._seq_labels == []
