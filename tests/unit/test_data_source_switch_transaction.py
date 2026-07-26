"""数据源切换失败时不能破坏当前可用连接。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.config.settings import Settings
from pa_agent.gui.main_window import MainWindow


class _Combo:
    def __init__(self, value: str) -> None:
        self._value = value

    def currentText(self) -> str:
        return self._value

    def blockSignals(self, _blocked: bool) -> None:
        return None

    def setCurrentText(self, value: str) -> None:
        self._value = value


class _OldSource:
    def __init__(self) -> None:
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


class _FailingSource:
    def __init__(self) -> None:
        self.disconnected = False

    def connect(self) -> None:
        raise RuntimeError("connect failed")

    def unsubscribe(self) -> None:
        return None

    def disconnect(self) -> None:
        self.disconnected = True


class _WorkingSource:
    def __init__(self) -> None:
        self._connected = False
        self.disconnected = False
        self.subscribed: tuple[str, str] | None = None

    def connect(self) -> None:
        self._connected = True

    def supported_timeframes(self) -> list[str]:
        return ["5m", "15m"]

    def subscribe(self, symbol: str, timeframe: str) -> None:
        self.subscribed = (symbol, timeframe)

    def unsubscribe(self) -> None:
        return None

    def disconnect(self) -> None:
        self._connected = False
        self.disconnected = True


class _SwitchHarness:
    _switch_data_source = MainWindow._switch_data_source

    def __init__(self, old_source: _OldSource) -> None:
        self._ctx = SimpleNamespace(data_source=old_source, settings=Settings())
        self._active_data_source_kind = "mt5"
        self._symbol_combo = _Combo("XAUUSD")
        self._tf_combo = _Combo("15m")
        self._switching = False
        self._analysis_in_progress = False
        self._last_frame_ready_bars = ["old-bars"]
        self._chart_refresh_paused = False
        self.update_calls = 0
        self.refresh_running = True
        self.fail_population_once = False
        self.status_messages: list[str] = []
        self._status_bar = SimpleNamespace(showMessage=self.status_messages.append)

    def _current_data_source_kind(self) -> str:
        return self._active_data_source_kind

    def _disconnect_data_source(self, data_source: object) -> None:
        disconnect = getattr(data_source, "disconnect", None)
        if callable(disconnect):
            disconnect()

    def _analysis_bar_count(self) -> int:
        return self._ctx.settings.general.analysis_bar_count

    def _ui_is_alive(self) -> bool:
        return False

    def _cancel_analysis_worker(self) -> None:
        return None

    def _stop_refresh_loop(self) -> None:
        self.refresh_running = False

    def _start_refresh_loop(self) -> None:
        self.refresh_running = True

    def _sync_tv_exchange_visibility(self) -> None:
        return None

    def _update_data_source_health_label(self) -> None:
        return None

    def _populate_symbol_combo_for_source(self) -> None:
        if self.fail_population_once:
            self.fail_population_once = False
            raise RuntimeError("population failed")

    def _populate_timeframe_combo_for_source(self) -> None:
        return None

    def _set_chart_refresh_paused(self, paused: bool) -> None:
        self._chart_refresh_paused = paused

    def _disable_chat_input(self) -> None:
        return None

    def _update_symbol_data_alert(self) -> None:
        return None

    def _refresh_chart_once(self) -> None:
        return None

    def _update_submit_button_state(self) -> None:
        self.update_calls += 1


def test_failed_new_source_keeps_old_source_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_source = _OldSource()
    failing_source = _FailingSource()
    harness = _SwitchHarness(old_source)
    monkeypatch.setattr(
        "pa_agent.data.factory.create_data_source", lambda _kind: failing_source
    )

    with pytest.raises(RuntimeError, match="connect failed"):
        harness._switch_data_source("longbridge")

    assert harness._ctx.data_source is old_source
    assert harness._active_data_source_kind == "mt5"
    assert old_source.disconnected is False
    assert failing_source.disconnected is True
    assert harness._switching is False
    assert harness.update_calls == 1


def test_post_commit_ui_failure_rolls_back_source_and_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_source = _OldSource()
    old_source._connected = True
    new_source = _WorkingSource()
    harness = _SwitchHarness(old_source)
    harness.fail_population_once = True
    monkeypatch.setattr(
        "pa_agent.data.factory.create_data_source", lambda _kind: new_source
    )
    monkeypatch.setattr("pa_agent.config.settings.save_settings", lambda _settings: None)

    with pytest.raises(RuntimeError, match="population failed"):
        harness._switch_data_source("longbridge")

    assert harness._ctx.data_source is old_source
    assert harness._active_data_source_kind == "mt5"
    assert old_source.disconnected is False
    assert new_source.disconnected is True
    assert harness.refresh_running is True
    assert harness._symbol_combo.currentText() == "XAUUSD"
    assert harness._switching is False


def test_longbridge_limit_is_rejected_before_switch_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_source = _OldSource()
    harness = _SwitchHarness(old_source)
    harness._ctx.settings.general.analysis_bar_count = 3001
    created = False

    def _create(_kind: str) -> _WorkingSource:
        nonlocal created
        created = True
        return _WorkingSource()

    monkeypatch.setattr("pa_agent.data.factory.create_data_source", _create)

    with pytest.raises(ValueError, match="最多支持 3000 根"):
        harness._switch_data_source("longbridge")

    assert created is False
    assert harness._ctx.data_source is old_source
    assert harness._switching is False


def test_successful_switch_reports_settings_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_source = _OldSource()
    new_source = _WorkingSource()
    harness = _SwitchHarness(old_source)
    monkeypatch.setattr(
        "pa_agent.data.factory.create_data_source", lambda _kind: new_source
    )

    def _fail_save(_settings: Settings) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pa_agent.config.settings.save_settings", _fail_save)

    harness._switch_data_source("longbridge")

    assert harness._ctx.data_source is new_source
    assert old_source.disconnected is True
    assert "设置未保存" in harness.status_messages[-1]


class _FetchHarness:
    _on_fetch_data_clicked = MainWindow._on_fetch_data_clicked

    def __init__(self) -> None:
        self._ctx = SimpleNamespace(data_source=SimpleNamespace(_connected=False))
        self.switched: list[str] = []
        self.messages: list[str] = []
        self._status_bar = SimpleNamespace(showMessage=self.messages.append)

    def _current_data_source_kind(self) -> str:
        return "longbridge"

    def _switch_data_source(self, kind: str) -> None:
        self.switched.append(kind)


def test_fetch_button_reconnects_disconnected_longbridge() -> None:
    harness = _FetchHarness()

    harness._on_fetch_data_clicked()

    assert harness.switched == ["longbridge"]
