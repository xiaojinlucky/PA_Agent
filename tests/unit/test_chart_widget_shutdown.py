"""图表窗口关闭时必须先停止刷新，再交给 Qt 销毁。"""
from __future__ import annotations

from unittest.mock import MagicMock

from pa_agent.app_context import AppContext
from pa_agent.config.settings import Settings
from pa_agent.data.market_workspace_controller import MarketWorkspaceController
from pa_agent.gui.chart_widget import ChartWidget
from pa_agent.gui.main_window import _LIVE_MAIN_WINDOWS, MainWindow


def test_chart_close_stops_timer_and_is_idempotent(qtbot) -> None:
    chart = ChartWidget()
    qtbot.addWidget(chart)
    chart.show()

    assert chart._timer.isActive() is True
    assert chart.close() is True
    assert chart._timer.isActive() is False
    assert chart.close() is True


def test_main_window_closes_legacy_and_market_charts_before_teardown(
    qtbot,
) -> None:
    settings = Settings()
    source = MagicMock()
    source._connected = False
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["10m", "1h", "4h"]
    controller = MarketWorkspaceController(settings)
    runtime = MagicMock()
    window = MainWindow(
        AppContext(
            settings=settings,
            data_source=source,
            market_workspace_controller=controller,
            market_workspace_runtime=runtime,
        )
    )
    qtbot.addWidget(window)
    window._market_workspace_bridge.close()
    window._startup_ai_auth_check_done = True
    window._startup_tv_connectivity_check_done = True
    window.show()
    legacy_chart = window._chart_widget
    market_chart = window._market_workspace._chart

    assert legacy_chart._timer.isActive() is True
    assert market_chart._timer.isActive() is True
    assert window in _LIVE_MAIN_WINDOWS
    assert window.close() is True
    assert legacy_chart._timer.isActive() is False
    assert market_chart._timer.isActive() is False
    assert window not in _LIVE_MAIN_WINDOWS


def test_workspace_destruction_closes_charts_without_close_event(qtbot) -> None:
    from PyQt6 import sip
    from PyQt6.QtWidgets import QApplication

    settings = Settings()
    source = MagicMock()
    source._connected = False
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["10m", "1h", "4h"]
    controller = MarketWorkspaceController(settings)
    window = MainWindow(
        AppContext(
            settings=settings,
            data_source=source,
            market_workspace_controller=controller,
            market_workspace_runtime=MagicMock(),
        )
    )
    window._market_workspace_bridge.close()
    window._startup_ai_auth_check_done = True
    window._startup_tv_connectivity_check_done = True
    legacy_chart = window._chart_widget
    market_chart = window._market_workspace._chart
    legacy_timer = legacy_chart._timer
    market_timer = market_chart._timer
    window.show()
    qtbot.wait(1)
    legacy_chart.viewport().update()
    market_chart.viewport().update()

    sip.delete(window._central)
    QApplication.processEvents()

    assert legacy_chart._close_started is True
    assert market_chart._close_started is True
    assert sip.isdeleted(legacy_timer) or legacy_timer.isActive() is False
    assert sip.isdeleted(market_timer) or market_timer.isActive() is False
    sip.delete(window)
    assert window not in _LIVE_MAIN_WINDOWS
