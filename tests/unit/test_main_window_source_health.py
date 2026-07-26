"""主窗口数据源状态提示测试。"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from pa_agent.app_context import AppContext
from pa_agent.config.settings import Settings
from pa_agent.gui.main_window import MainWindow


def test_longbridge_health_label_shows_expiry_without_credentials(qtbot) -> None:
    settings = Settings()
    settings.general.last_data_source = "longbridge"
    settings.general.last_symbols_by_source["longbridge"] = "AAPL.US"
    source = MagicMock()
    source._connected = True
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = [
        "1m", "2m", "3m", "5m", "10m", "15m", "20m", "30m", "45m",
        "1h", "2h", "3h", "4h", "1d", "1w",
    ]
    expires_at = datetime.now(UTC) + timedelta(days=30)
    source.token_expiry = SimpleNamespace(
        status="expiring",  # UI 必须根据当前日期重新分类，不能信任缓存状态。
        expires_at_utc=expires_at,
    )

    window = MainWindow(AppContext(settings=settings, data_source=source))
    qtbot.addWidget(window)

    text = window._data_source_health_label.text()
    assert window._data_source_health_label.isHidden() is False
    assert window._data_source_health_label.objectName() == "sourceHealthOk"
    assert "行情只读" in text
    assert expires_at.strftime("%Y-%m-%d UTC") in text
    assert "APP_KEY" not in text
    assert "ACCESS_TOKEN" not in text
    assert window._data_source_health_timer.isActive() is True
    assert window._data_source_health_timer.interval() == 60_000
    assert [window._tf_combo.itemText(i) for i in range(window._tf_combo.count())] == [
        "1m", "2m", "3m", "5m", "10m", "15m", "20m", "30m", "45m",
        "1h", "2h", "3h", "4h", "1d", "1w",
    ]


def test_longbridge_health_label_distinguishes_disconnected_source(qtbot) -> None:
    settings = Settings()
    settings.general.last_data_source = "longbridge"
    source = MagicMock()
    source._connected = False
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]
    source.token_expiry = SimpleNamespace(
        status="valid",
        expires_at_utc=datetime.now(UTC) + timedelta(days=30),
    )

    window = MainWindow(AppContext(settings=settings, data_source=source))
    qtbot.addWidget(window)

    assert window._data_source_health_label.objectName() == "sourceHealthError"
    assert "未连接" in window._data_source_health_label.text()


def test_execution_status_explicitly_blocks_mt5_prices_for_okx_demo(qtbot) -> None:
    settings = Settings()
    settings.general.last_data_source = "mt5"
    settings.general.last_symbol = "XAUUSD"
    settings.execution.enabled = True
    settings.execution.selected_broker = "okx"
    settings.execution.okx.source_symbol = "XAU-USDT-SWAP"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    settings.execution.okx.simulated = True
    source = MagicMock()
    source._connected = False
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]

    window = MainWindow(AppContext(settings=settings, data_source=source))
    qtbot.addWidget(window)

    text = window._execution_status_label.text()
    assert "MT5/XAUUSD" in text
    assert "OKX 模拟 XAU-USDT-SWAP" in text
    assert "行情/执行不一致，已阻断" in text
    assert "入场 跟随信号 / 离场 市价" in text


def test_execution_status_shows_okx_demo_order_modes_and_atr(qtbot) -> None:
    settings = Settings()
    settings.general.last_data_source = "okx"
    settings.general.last_symbol = "XAU-USDT-SWAP"
    settings.execution.enabled = True
    settings.execution.selected_broker = "okx"
    settings.execution.entry_order_mode = "limit_with_slippage"
    settings.execution.exit_order_mode = "limit_with_slippage"
    settings.execution.okx.source_symbol = "XAU-USDT-SWAP"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    settings.execution.okx.simulated = True
    source = MagicMock()
    source._connected = False
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m", "1h", "4h"]

    window = MainWindow(AppContext(settings=settings, data_source=source))
    qtbot.addWidget(window)

    assert "路由配置一致" in window._execution_status_label.text()
    assert "入场 限价+ATR / 离场 限价+ATR" in window._execution_status_label.text()
    assert (
        "GUI 配置：资金上限 0 USDT / 单笔风险 10.00% / 杠杆上限 20×"
        in (
        window._execution_status_label.text()
        )
    )
    assert "ATR 倍数 0.50" in window._execution_status_label.toolTip()


def test_live_trading_is_built_as_a_top_level_workspace() -> None:
    source = inspect.getsource(MainWindow._setup_ui)

    assert 'self._central = QTabWidget()' in source
    assert 'self._central.addTab(self._analysis_workbench, "分析工作台")' in source
    assert '"实盘交易",' in source
    assert "TradingDialog" not in inspect.getsource(
        MainWindow._open_trading_dialog
    )


def test_trading_workspace_hides_redundant_status_bar(qtbot) -> None:
    settings = Settings()
    source = MagicMock()
    source._connected = False
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]
    window = MainWindow(AppContext(settings=settings, data_source=source))
    qtbot.addWidget(window)

    assert window._status_bar.isHidden() is False
    window._central.setCurrentIndex(window._trading_tab_index)
    assert window._status_bar.isHidden() is True
    window._central.setCurrentIndex(0)
    assert window._status_bar.isHidden() is False


def test_read_model_capture_failure_does_not_abort_main_window(qtbot) -> None:
    settings = Settings()
    source = MagicMock()
    source._connected = False
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]
    read_model = MagicMock()
    read_model.capture.side_effect = RuntimeError("broken snapshot")

    window = MainWindow(
        AppContext(
            settings=settings,
            data_source=source,
            workbench_read_model=read_model,
        )
    )
    qtbot.addWidget(window)

    assert window._workbench_read_model_status_label.text() == "只读：读取失败"
    assert window._campaign_status_label.text() == "10 分钟 OKX 模拟盘：读取失败"
    assert "RuntimeError" in window._workbench_read_model_status_label.toolTip()
