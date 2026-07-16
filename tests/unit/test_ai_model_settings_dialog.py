"""AI 模型档案对话框的关键安全与切换门禁测试。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QDialog, QLineEdit

from pa_agent.ai.provider_probe import ProbeStatus, ProviderProbeResult
from pa_agent.app_context import AppContext
from pa_agent.config.settings import AIProviderSettings, Settings
from pa_agent.gui.ai_model_settings_dialog import AIModelSettingsDialog


def _probe_result(
    *,
    adapter_id: str,
    connection: ProbeStatus = ProbeStatus.PASSED,
    parameters: ProbeStatus = ProbeStatus.PASSED,
    reasoning: bool | None = False,
    response: bool | None = True,
    challenge: bool | None = True,
    message: str = "",
) -> ProviderProbeResult:
    return ProviderProbeResult(
        adapter_id=adapter_id,
        tested_at="2026-07-16T00:00:00+00:00",
        connection_auth=connection,
        parameter_acceptance=parameters,
        reasoning_observed=reasoning,
        response_observed=response,
        challenge_matched=challenge,
        message=message,
    )


def _verified_settings() -> Settings:
    settings = Settings()
    settings.save_ai_profile(
        "default",
        "默认",
        AIProviderSettings(
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key="key-default",
            adapter_id="deepseek",
        ),
        replace=True,
    )
    settings.mark_ai_profile_verification(
        "default",
        passed=True,
        tested_at="2026-07-16T00:00:00+00:00",
        adapter_id="deepseek",
        checks={
            "connection_auth": True,
            "parameter_acceptance": True,
            "response_observed": True,
            "challenge_matched": True,
        },
    )
    settings.save_ai_profile(
        "research",
        "研究模型",
        AIProviderSettings(
            model="gpt-5",
            base_url="https://api.openai.com/v1",
            api_key="key-research",
            adapter_id="openai",
            reasoning_effort="medium",
            context_window=400_000,
        ),
    )
    return settings


def test_api_key_is_masked_by_default_and_remasks_on_focus_loss(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)

    assert dialog._api_key_edit.echoMode() is QLineEdit.EchoMode.Password


def test_switching_profiles_always_remasks_api_key(qtbot) -> None:
    dialog = AIModelSettingsDialog(_verified_settings())
    qtbot.addWidget(dialog)
    dialog._show_key_btn.setChecked(True)

    dialog._load_profile("research")

    assert dialog._show_key_btn.isChecked() is False
    assert dialog._api_key_edit.echoMode() is QLineEdit.EchoMode.Password


def test_context_window_is_visible_and_saved_per_profile(qtbot) -> None:
    dialog = AIModelSettingsDialog(_verified_settings())
    qtbot.addWidget(dialog)

    dialog._load_profile("research")
    assert dialog._context_window_spin.value() == 400_000

    dialog._context_window_spin.setValue(500_000)
    provider = dialog._store_current_in_working()

    assert provider.context_window == 500_000
    assert dialog._working.ai_profiles["default"].provider.context_window == 2_000_000
    assert dialog._working.ai_profiles["research"].provider.context_window == 500_000


def test_unsupported_saved_effort_falls_back_to_high_not_low(qtbot) -> None:
    dialog = AIModelSettingsDialog(_verified_settings())
    qtbot.addWidget(dialog)
    dialog._adapter_combo.setCurrentIndex(dialog._adapter_combo.findData("openai"))
    dialog._model_edit.setText("gpt-5")

    dialog._refresh_capability_controls(preferred_effort="max")

    assert dialog._effort_combo.currentData() == "high"
    dialog._show_key_btn.setChecked(True)
    assert dialog._api_key_edit.echoMode() is QLineEdit.EchoMode.Normal

    dialog._api_key_edit.editingFinished.emit()
    assert dialog._api_key_edit.echoMode() is QLineEdit.EchoMode.Password


def test_adapter_capability_forces_minimax_m2_thinking(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)

    dialog._adapter_combo.setCurrentIndex(
        dialog._adapter_combo.findData("minimax_m2")
    )

    assert dialog._thinking_check.isChecked() is True
    assert dialog._thinking_check.isEnabled() is False
    assert dialog._effort_combo.isEnabled() is False
    assert "固定开启" in dialog._thinking_value.text()


def test_successful_inactive_probe_is_saved_and_becomes_activatable(
    qtbot,
    tmp_path,
) -> None:
    path = tmp_path / "settings.json"
    settings = _verified_settings()
    dialog = AIModelSettingsDialog(settings, settings_path=path)
    qtbot.addWidget(dialog)
    dialog._populate_profile_list(select_id="research")
    dialog._probe_profile_id = "research"

    dialog._on_probe_finished(_probe_result(adapter_id="openai", reasoning=True))

    profile = settings.ai_profiles["research"]
    assert profile.verification.is_current_for(profile.provider)
    assert profile.verification.observations["reasoning_observed"] is True
    assert path.exists()
    assert dialog._activate_btn.isEnabled() is True

    dialog._request_activation()
    assert dialog.activation_requested_id == "research"
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_failed_active_probe_does_not_overwrite_running_profile(qtbot, tmp_path) -> None:
    settings = _verified_settings()
    original_model = settings.provider.model
    dialog = AIModelSettingsDialog(
        settings,
        settings_path=tmp_path / "settings.json",
    )
    qtbot.addWidget(dialog)
    dialog._model_edit.setText("deepseek-new-model")
    dialog._dirty = True
    dialog._store_current_in_working()
    dialog._probe_profile_id = "default"

    dialog._on_probe_finished(
        _probe_result(
            adapter_id="deepseek",
            connection=ProbeStatus.FAILED,
            parameters=ProbeStatus.UNKNOWN,
            reasoning=None,
            message="认证失败。",
        )
    )

    assert settings.provider.model == original_model
    assert settings.ai_profiles["default"].provider.model == original_model
    assert dialog._working.ai_profiles["default"].provider.model == original_model
    assert dialog._model_edit.text() == "deepseek-new-model"
    assert dialog._dirty is True


def test_successful_active_probe_defers_formal_settings_until_runtime_commit(
    qtbot,
    tmp_path,
) -> None:
    settings = _verified_settings()
    original_model = settings.provider.model
    dialog = AIModelSettingsDialog(
        settings,
        settings_path=tmp_path / "settings.json",
    )
    qtbot.addWidget(dialog)
    dialog._model_edit.setText("deepseek-new-model")
    dialog._dirty = True
    dialog._store_current_in_working()
    dialog._probe_profile_id = "default"

    dialog._on_probe_finished(_probe_result(adapter_id="deepseek"))

    assert settings.provider.model == original_model
    assert settings.ai_profiles["default"].provider.model == original_model
    assert dialog.runtime_candidate is not None
    assert dialog.runtime_candidate.provider.model == "deepseek-new-model"
    assert dialog.activation_requested_id == "default"
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_runtime_activation_rebuilds_client_and_resets_model_session(
    qtbot,
) -> None:
    settings = _verified_settings()
    settings.mark_ai_profile_verification(
        "research",
        passed=True,
        tested_at="2026-07-16T00:00:00+00:00",
        adapter_id="openai",
        checks={
            "connection_auth": True,
            "parameter_acceptance": True,
            "response_observed": True,
            "challenge_matched": True,
        },
    )
    source = MagicMock()
    source._connected = True
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["1m", "5m", "15m", "1h"]
    old_client = MagicMock()
    ctx = AppContext(
        settings=settings,
        data_source=source,
        client=old_client,
        pending_writer=MagicMock(_api_key="key-default"),
        ledger=MagicMock(),
    )

    from pa_agent.gui.main_window import MainWindow

    window = MainWindow(ctx)
    qtbot.addWidget(window)
    new_client = MagicMock()
    window._free_chat_session = object()
    window._stream_panel._session = object()
    window._stream_panel.set_input_enabled(True)

    with (
        patch("pa_agent.ai.client_factory.create_ai_client", return_value=new_client),
        patch("pa_agent.config.settings.save_settings"),
        patch("pa_agent.util.logging.update_api_key"),
    ):
        activated = window._activate_ai_profile_runtime("research")

    assert activated is True
    assert settings.active_ai_profile_id == "research"
    assert ctx.client is new_client
    assert window._free_chat_session is None
    assert window._stream_panel._session is None
    assert window._stream_panel._input_edit.isEnabled() is False
    assert ctx.pending_writer._api_key == "key-research"


def test_runtime_activation_is_blocked_while_analysis_runs(qtbot) -> None:
    settings = _verified_settings()
    source = MagicMock()
    source._connected = True
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]
    window_ctx = AppContext(settings=settings, data_source=source)

    from pa_agent.gui.main_window import MainWindow

    window = MainWindow(window_ctx)
    qtbot.addWidget(window)
    window._analysis_in_progress = True

    with patch("pa_agent.gui.main_window.QMessageBox.information") as info:
        activated = window._activate_ai_profile_runtime("default")

    assert activated is False
    info.assert_called_once()


def test_runtime_activation_save_failure_keeps_old_client_and_settings(qtbot) -> None:
    settings = _verified_settings()
    settings.mark_ai_profile_verification(
        "research",
        passed=True,
        tested_at="2026-07-16T00:00:00+00:00",
        adapter_id="openai",
        checks={
            "connection_auth": True,
            "parameter_acceptance": True,
            "response_observed": True,
            "challenge_matched": True,
        },
    )
    source = MagicMock()
    source._connected = True
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]
    old_client = MagicMock()
    ctx = AppContext(settings=settings, data_source=source, client=old_client)

    from pa_agent.gui.main_window import MainWindow

    window = MainWindow(ctx)
    qtbot.addWidget(window)
    with (
        patch("pa_agent.ai.client_factory.create_ai_client", return_value=MagicMock()),
        patch("pa_agent.config.settings.save_settings", side_effect=OSError("disk full")),
        patch("pa_agent.gui.main_window.QMessageBox.warning") as warning,
    ):
        activated = window._activate_ai_profile_runtime("research")

    assert activated is False
    assert settings.active_ai_profile_id == "default"
    assert ctx.client is old_client
    warning.assert_called_once()


def test_model_switch_gate_includes_snapshot_and_chat_workers(qtbot) -> None:
    settings = _verified_settings()
    source = MagicMock()
    source._connected = True
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]

    from pa_agent.gui.main_window import MainWindow

    window = MainWindow(AppContext(settings=settings, data_source=source))
    qtbot.addWidget(window)
    snapshot_worker = MagicMock()
    snapshot_worker.isRunning.return_value = True
    window._snapshot_fetch_worker = snapshot_worker

    assert window._ai_request_in_progress() is True

    snapshot_worker.isRunning.return_value = False
    chat_worker = MagicMock()
    chat_worker.isRunning.return_value = True
    window._stream_panel._sending = False
    window._stream_panel._worker = chat_worker

    assert window._ai_request_in_progress() is True
