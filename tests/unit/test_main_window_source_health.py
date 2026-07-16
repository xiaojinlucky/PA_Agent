"""主窗口数据源状态提示测试。"""
from __future__ import annotations

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
