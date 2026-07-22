"""AI 模型档案对话框的关键安全与切换门禁测试。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QDialog, QLineEdit, QMessageBox

from pa_agent.ai.provider_model_catalog import ModelCatalogEntry
from pa_agent.ai.provider_probe import ProbeStatus, ProviderProbeResult
from pa_agent.ai.provider_registry import resolve_provider_runtime_settings
from pa_agent.app_context import AppContext
from pa_agent.config.settings import (
    AIProviderSettings,
    Settings,
    load_settings,
    save_settings,
)
from pa_agent.gui.ai_model_settings_dialog import (
    AIModelSettingsDialog,
    _ProviderProbeWorker,
)
from pa_agent.util.threading import CancelToken


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


def test_api_key_is_masked_by_default(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)

    assert dialog._api_key_edit.echoMode() is QLineEdit.EchoMode.Password


def test_primary_provider_adapters_are_available(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)

    for adapter_id in ("codex_subscription", "deepseek", "kimi", "mimo"):
        assert dialog._adapter_combo.findData(adapter_id) >= 0


def test_codex_adapter_uses_cli_without_url_or_api_key(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)

    index = dialog._adapter_combo.findData("codex_subscription")
    with patch(
        "pa_agent.ai.codex_subscription_client.codex_login_status"
    ) as status:
        status.return_value.logged_in = True
        status.return_value.message = "已通过官方 Codex CLI 使用 ChatGPT 订阅登录。"
        dialog._adapter_combo.setCurrentIndex(index)

    assert dialog._model_edit.text() == "auto"
    assert dialog._base_url_edit.text() == ""
    assert dialog._base_url_edit.isEnabled() is False
    assert dialog._api_key_edit.isEnabled() is False
    assert dialog._connection_form.isRowVisible(dialog._base_url_edit) is False
    assert dialog._connection_form.isRowVisible(dialog._api_key_row) is False
    assert dialog._subscription_auth_group.isHidden() is False
    assert "ChatGPT 订阅登录" in dialog._codex_login_status.text()


def test_codex_form_and_login_actions_do_not_require_api_key(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)
    with patch(
        "pa_agent.ai.codex_subscription_client.codex_login_status",
        side_effect=AssertionError(
            "form validation must not synchronously call Codex CLI"
        ),
    ):
        dialog._adapter_combo.setCurrentIndex(
            dialog._adapter_combo.findData("codex_subscription")
        )
        provider = dialog._provider_from_form()

    assert provider.adapter_id == "codex_subscription"
    assert provider.api_key == ""

    with patch(
        "pa_agent.ai.codex_subscription_client.start_codex_login"
    ) as start:
        dialog._start_codex_login(device_auth=False)
        dialog._start_codex_login(device_auth=True)

    assert start.call_args_list[0].kwargs == {"device_auth": False}
    assert start.call_args_list[1].kwargs == {"device_auth": True}


def test_codex_login_check_has_immediate_feedback_and_focuses_test(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)
    dialog._adapter_combo.setCurrentIndex(
        dialog._adapter_combo.findData("codex_subscription")
    )
    worker = MagicMock()
    worker.isRunning.return_value = False

    with patch(
        "pa_agent.gui.ai_model_settings_dialog._CodexLoginStatusWorker",
        return_value=worker,
    ):
        dialog._start_codex_login_status_check()

    assert dialog._codex_refresh_login_btn.text() == "正在检测…"
    assert "正在调用官方 Codex CLI" in dialog._codex_login_status.text()
    assert dialog._test_btn.isEnabled() is False
    worker.start.assert_called_once()

    worker.status = MagicMock(logged_in=True, message="ChatGPT 订阅已登录")
    dialog._on_codex_login_status_finished()

    assert dialog._codex_refresh_login_btn.text() == "已登录"
    assert "ChatGPT 订阅已登录" in dialog._codex_login_status.text()
    assert dialog._test_btn.isEnabled() is True
    assert dialog.focusWidget() is dialog._test_btn


def test_verified_codex_profile_requests_activation_without_api_key(qtbot) -> None:
    settings = Settings()
    settings.save_ai_profile(
        "codex",
        "Codex 订阅",
        AIProviderSettings(
            model="auto",
            base_url="",
            api_key="",
            adapter_id="codex_subscription",
        ),
    )
    settings.mark_ai_profile_verification(
        "codex",
        passed=True,
        tested_at="2026-07-17T00:00:00+00:00",
        adapter_id="codex_subscription",
        checks={
            "connection_auth": True,
            "parameter_acceptance": True,
            "response_observed": True,
            "challenge_matched": True,
        },
    )
    dialog = AIModelSettingsDialog(settings)
    qtbot.addWidget(dialog)
    status = MagicMock(logged_in=True, message="ChatGPT 订阅已登录")
    with patch(
        "pa_agent.ai.codex_subscription_client.codex_login_status",
        return_value=status,
    ):
        dialog._populate_profile_list(select_id="codex")

    dialog._request_activation()

    assert dialog.activation_requested_id == "codex"
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_switching_provider_clears_old_key_and_uses_new_provider_env(qtbot) -> None:
    dialog = AIModelSettingsDialog(_verified_settings())
    qtbot.addWidget(dialog)
    assert dialog._api_key_edit.text() == "key-default"

    with patch(
        "pa_agent.ai.provider_registry.merged_environment",
        return_value={
            "MOONSHOT_API_KEY": "kimi-env-key",
            "MIMO_API_KEY": "mimo-env-key",
        },
    ):
        dialog._adapter_combo.setCurrentIndex(dialog._adapter_combo.findData("kimi"))
        assert dialog._api_key_edit.text() == "kimi-env-key"
        assert dialog._api_key_from_env is True
        assert dialog._preview_provider().api_key == ""
        assert resolve_provider_runtime_settings(
            dialog._preview_provider()
        ).api_key == "kimi-env-key"

        dialog._show_key_btn.setChecked(True)
        assert dialog._api_key_edit.echoMode() is QLineEdit.EchoMode.Normal
        dialog._show_key_btn.setChecked(False)

        dialog._adapter_combo.setCurrentIndex(dialog._adapter_combo.findData("mimo"))
        assert dialog._api_key_edit.text() == "mimo-env-key"
        assert dialog._api_key_from_env is True
        assert dialog._show_key_btn.isChecked() is False
        assert dialog._show_key_btn.text() == "显示"
        assert dialog._api_key_edit.echoMode() is QLineEdit.EchoMode.Password
        assert dialog._preview_provider().api_key == ""
        assert resolve_provider_runtime_settings(
            dialog._preview_provider()
        ).api_key == "mimo-env-key"


def test_switching_profiles_always_remasks_api_key(qtbot) -> None:
    dialog = AIModelSettingsDialog(_verified_settings())
    qtbot.addWidget(dialog)
    dialog._show_key_btn.setChecked(True)

    dialog._load_profile("research")

    assert dialog._show_key_btn.isChecked() is False
    assert dialog._api_key_edit.echoMode() is QLineEdit.EchoMode.Password


def test_context_window_is_model_derived_and_not_user_editable(qtbot) -> None:
    dialog = AIModelSettingsDialog(_verified_settings())
    qtbot.addWidget(dialog)

    dialog._load_profile("research")
    assert dialog._context_window_label.text() == "尚未确认（模型固定）"
    assert "不能手动修改" in dialog._context_window_label.toolTip()

    dialog._adapter_combo.setCurrentIndex(dialog._adapter_combo.findData("kimi"))
    provider = dialog._preview_provider()

    assert provider.model == "kimi-k2.6"
    assert provider.context_window == 262_144
    assert provider.context_window_source == "builtin"
    assert dialog._context_window_label.text() == "262,144 tokens（模型固定）"
    assert dialog._working.ai_profiles["default"].provider.context_window == 1_000_000

    dialog._manual_model_check.setChecked(True)
    dialog._model_edit.setText("kimi-account-preview")
    provider = dialog._preview_provider()

    assert provider.context_window is None
    assert provider.context_window_source == "unknown"
    assert dialog._context_window_label.text() == "尚未确认（模型固定）"


def test_live_catalog_context_survives_profile_save_and_reload(qtbot, tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = _verified_settings()
    dialog = AIModelSettingsDialog(settings, settings_path=path)
    qtbot.addWidget(dialog)
    dialog._adapter_combo.setCurrentIndex(dialog._adapter_combo.findData("kimi"))

    live_entries = (
        ModelCatalogEntry(
            model_id="kimi-k2.6",
            display_name="Kimi K2.6",
            context_window=262_144,
            supports_thinking_on=True,
            supports_thinking_off=True,
        ),
    )
    dialog._apply_model_catalog(live_entries, authoritative=True)
    provider = dialog._provider_from_form()
    dialog._working.save_ai_profile(
        "default",
        "默认",
        provider,
        replace=True,
    )
    from pa_agent.config.settings import load_settings, save_settings

    save_settings(dialog._working, path)
    reloaded = load_settings(path)

    assert reloaded.ai_profiles["default"].provider.context_window == 262_144
    assert (
        reloaded.ai_profiles["default"].provider.context_window_source
        == "catalog"
    )


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
    assert dialog._api_key_edit.echoMode() is QLineEdit.EchoMode.Normal


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


def test_codex_gpt55_probe_save_then_activate_stays_verified(
    qtbot,
    tmp_path,
) -> None:
    settings = _verified_settings()
    settings.save_ai_profile(
        "codex",
        "Codex 订阅",
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="gpt-5.5",
            reasoning_effort="high",
        ),
    )
    dialog = AIModelSettingsDialog(
        settings,
        settings_path=tmp_path / "settings.json",
    )
    qtbot.addWidget(dialog)
    login_status = MagicMock(logged_in=True, message="ChatGPT 订阅已登录")
    with patch(
        "pa_agent.ai.codex_subscription_client.codex_login_status",
        return_value=login_status,
    ):
        dialog._populate_profile_list(select_id="codex")
    dialog._probe_profile_id = "codex"

    dialog._on_probe_finished(
        _probe_result(adapter_id="codex_subscription", reasoning=True)
    )

    profile = settings.ai_profiles["codex"]
    assert profile.verification.is_current_for(profile.provider)
    assert dialog._activate_btn.isEnabled() is True
    dialog._request_activation()
    assert dialog.activation_requested_id == "codex"
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
    dialog._manual_model_check.setChecked(True)
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
    dialog._manual_model_check.setChecked(True)
    dialog._dirty = True
    dialog._store_current_in_working()
    dialog._probe_profile_id = "default"

    dialog._on_probe_finished(_probe_result(adapter_id="deepseek"))

    assert settings.provider.model == original_model
    assert settings.ai_profiles["default"].provider.model == original_model
    assert dialog.runtime_candidate is not None
    assert dialog.runtime_candidate.provider.model == "deepseek-new-model"
    assert dialog.activation_requested_id is None
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "窗口不会自动关闭或重开" in dialog._probe_message.text()

    dialog._request_activation()

    assert dialog.activation_requested_id == "default"
    assert dialog.runtime_refresh_required is True
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_verified_runtime_candidate_requires_confirmation_before_discard(
    qtbot,
    tmp_path,
) -> None:
    settings = _verified_settings()
    dialog = AIModelSettingsDialog(
        settings,
        settings_path=tmp_path / "settings.json",
    )
    qtbot.addWidget(dialog)
    dialog._model_edit.setText("deepseek-new-model")
    dialog._manual_model_check.setChecked(True)
    dialog._dirty = True
    dialog._store_current_in_working()
    dialog._probe_profile_id = "default"
    dialog._on_probe_finished(_probe_result(adapter_id="deepseek"))

    with patch(
        "pa_agent.gui.ai_model_settings_dialog.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    ):
        assert dialog._confirm_discard_dirty() is False
    assert dialog.runtime_candidate is not None

    with patch(
        "pa_agent.gui.ai_model_settings_dialog.QMessageBox.question",
        return_value=QMessageBox.StandardButton.Yes,
    ):
        assert dialog._confirm_discard_dirty() is True


def test_successful_unchanged_codex_probe_saves_without_closing_dialog(
    qtbot,
    tmp_path,
) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    settings.save_ai_profile(
        "default",
        "Codex 订阅",
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="gpt-5.6-sol",
            base_url="",
            api_key="",
        ),
        replace=True,
    )
    dialog = AIModelSettingsDialog(settings, settings_path=path)
    qtbot.addWidget(dialog)
    dialog._probe_profile_id = "default"

    dialog._on_probe_finished(
        _probe_result(adapter_id="codex_subscription", reasoning=True)
    )

    assert settings.ai_profiles["default"].verification.is_current_for(
        settings.ai_profiles["default"].provider
    )
    assert path.exists()
    from pa_agent.config.settings import load_settings

    reloaded = load_settings(path)
    reloaded_profile = reloaded.ai_profiles[reloaded.active_ai_profile_id]
    assert reloaded_profile.verification.is_current_for(reloaded_profile.provider)
    assert reloaded_profile.provider.api_key == ""
    assert dialog.runtime_candidate is None
    assert dialog.activation_requested_id is None
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "不需要 API Key" in dialog._probe_message.text()


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


def test_runtime_activation_of_verified_codex_never_requires_api_key(qtbot) -> None:
    settings = _verified_settings()
    settings.save_ai_profile(
        "codex",
        "Codex 订阅",
        AIProviderSettings(
            model="auto",
            base_url="",
            api_key="",
            adapter_id="codex_subscription",
        ),
    )
    settings.mark_ai_profile_verification(
        "codex",
        passed=True,
        tested_at="2026-07-17T00:00:00+00:00",
        adapter_id="codex_subscription",
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
    ctx = AppContext(
        settings=settings,
        data_source=source,
        client=MagicMock(),
        pending_writer=MagicMock(_api_key="key-default"),
        ledger=MagicMock(),
    )

    from pa_agent.gui.main_window import MainWindow

    window = MainWindow(ctx)
    qtbot.addWidget(window)
    with (
        patch("pa_agent.ai.client_factory.create_ai_client", return_value=MagicMock()),
        patch("pa_agent.config.settings.save_settings"),
        patch("pa_agent.util.logging.update_api_key"),
        patch(
            "pa_agent.ai.codex_subscription_client.codex_login_status"
        ) as status,
    ):
        status.return_value.logged_in = True
        assert window._activate_ai_profile_runtime("codex") is True
        assert window._has_ai_auth_configured() is True

    assert settings.active_ai_profile_id == "codex"
    assert settings.provider.api_key == ""
    assert window._ai_auth_alert_label.isHidden() is True
    assert window._submit_block_reason() is None


def test_startup_unverified_codex_guidance_never_requests_api_key(qtbot) -> None:
    settings = Settings()
    settings.save_ai_profile(
        "default",
        "Codex 订阅",
        AIProviderSettings(
            model="gpt-5.6-sol",
            base_url="",
            api_key="",
            adapter_id="codex_subscription",
        ),
        replace=True,
    )
    source = MagicMock()
    source._connected = True
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]
    ctx = AppContext(
        settings=settings,
        data_source=source,
        client=MagicMock(),
        pending_writer=MagicMock(),
        ledger=MagicMock(),
    )

    from pa_agent.gui.main_window import MainWindow

    with patch(
        "pa_agent.ai.codex_subscription_client.codex_login_status"
    ) as status:
        status.return_value.logged_in = True
        window = MainWindow(ctx)
        qtbot.addWidget(window)
        with (
            patch("pa_agent.gui.main_window.QMessageBox.information") as info,
            patch.object(window, "_open_settings_dialog") as open_settings,
        ):
            window._on_startup_ai_auth_check()
            readiness_error = window._ai_profile_readiness_error()

    assert readiness_error == (
        "当前 Codex 订阅档案尚未通过真实连接测试"
    )
    message = info.call_args.args[2]
    assert "检测现有登录" in message
    assert "不需要 API Key" in message
    open_settings.assert_called_once_with(focus_auth=True)


def test_verified_codex_activation_does_not_block_gui_on_login_status(qtbot) -> None:
    settings = _verified_settings()
    settings.save_ai_profile(
        "codex",
        "Codex 订阅",
        AIProviderSettings(
            model="auto",
            base_url="",
            api_key="",
            adapter_id="codex_subscription",
        ),
    )
    settings.mark_ai_profile_verification(
        "codex",
        passed=True,
        tested_at="2026-07-17T00:00:00+00:00",
        adapter_id="codex_subscription",
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
    with (
        patch(
            "pa_agent.ai.codex_subscription_client.codex_login_status",
            side_effect=AssertionError("activation must not synchronously call Codex CLI"),
        ),
        patch(
            "pa_agent.ai.client_factory.create_ai_client",
            return_value=new_client,
        ),
        patch("pa_agent.config.settings.save_settings"),
        patch("pa_agent.util.logging.update_api_key"),
    ):
        assert window._activate_ai_profile_runtime("codex") is True

    assert settings.active_ai_profile_id == "codex"
    assert ctx.client is new_client


def test_unverified_active_profile_with_api_key_cannot_submit(qtbot) -> None:
    settings = _verified_settings()
    settings.ai_profiles["default"].verification.invalidate()
    source = MagicMock()
    source._connected = True
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]
    ctx = AppContext(settings=settings, data_source=source, client=MagicMock())

    from pa_agent.gui.main_window import MainWindow

    window = MainWindow(ctx)
    qtbot.addWidget(window)

    assert window._has_ai_auth_configured() is True
    assert "尚未通过" in (window._submit_block_reason() or "")


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
        patch(
            "pa_agent.config.settings._write_settings_candidate",
            side_effect=OSError("disk full"),
        ),
        patch("pa_agent.gui.main_window.QMessageBox.warning") as warning,
    ):
        activated = window._activate_ai_profile_runtime("research")

    assert activated is False
    assert settings.active_ai_profile_id == "default"
    assert ctx.client is old_client
    warning.assert_called_once()


def test_codex_terra_activation_merges_unrelated_revision_change(
    qtbot,
    tmp_path,
) -> None:
    """真实测试期间普通设置被保存，不应阻断已验证的 Codex 模型切换。"""

    path = tmp_path / "settings.json"
    settings = Settings()
    settings.save_ai_profile(
        "codex",
        "Codex 订阅",
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        ),
        replace=True,
    )
    settings.mark_ai_profile_verification(
        "codex",
        passed=True,
        tested_at="2026-07-20T00:00:00+00:00",
        adapter_id="codex_subscription",
        checks={
            "connection_auth": True,
            "parameter_acceptance": True,
            "response_observed": True,
            "challenge_matched": True,
        },
    )
    save_settings(settings, path)

    source = MagicMock()
    source._connected = True
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["1m", "15m"]
    old_client = MagicMock()
    ctx = AppContext(
        settings=settings,
        settings_path=path,
        data_source=source,
        client=old_client,
        pending_writer=MagicMock(),
        ledger=MagicMock(),
    )

    from pa_agent.gui.main_window import MainWindow

    window = MainWindow(ctx)
    qtbot.addWidget(window)
    original_general = settings.general

    candidate = settings.model_copy(deep=True)
    candidate.save_ai_profile(
        "codex",
        "Codex 订阅",
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="gpt-5.6-terra",
            reasoning_effort="high",
        ),
        replace=True,
    )
    candidate.provider = candidate.ai_profiles["codex"].provider.model_copy(deep=True)
    candidate.mark_ai_profile_verification(
        "codex",
        passed=True,
        tested_at="2026-07-20T01:00:00+00:00",
        adapter_id="codex_subscription",
        checks={
            "connection_auth": True,
            "parameter_acceptance": True,
            "response_observed": True,
            "challenge_matched": True,
        },
    )

    concurrent = load_settings(path)
    concurrent.general.last_timeframe = "1m"
    save_settings(concurrent, path)

    new_client = MagicMock()
    with (
        patch("pa_agent.ai.client_factory.create_ai_client", return_value=new_client),
        patch("pa_agent.util.logging.update_api_key"),
    ):
        activated = window._activate_ai_profile_runtime(
            "codex",
            candidate_settings=candidate,
        )

    assert activated is True
    assert ctx.client is new_client
    assert settings.provider.model == "gpt-5.6-terra"
    assert settings.general.last_timeframe == "1m"
    assert settings.general is original_general

    reloaded = load_settings(path)
    assert reloaded.active_ai_profile_id == "codex"
    assert reloaded.provider.model == "gpt-5.6-terra"
    assert reloaded.general.last_timeframe == "1m"
    assert reloaded.ai_profiles["codex"].verification.is_current_for(
        reloaded.ai_profiles["codex"].provider
    )


def test_runtime_activation_rejects_concurrent_ai_profile_change(
    qtbot,
    tmp_path,
) -> None:
    """另一窗口也改了 AI 档案时仍必须拒绝覆盖。"""

    path = tmp_path / "settings.json"
    settings = _verified_settings()
    save_settings(settings, path)
    source = MagicMock()
    source._connected = True
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]
    old_client = MagicMock()
    ctx = AppContext(
        settings=settings,
        settings_path=path,
        data_source=source,
        client=old_client,
    )

    from pa_agent.gui.main_window import MainWindow

    window = MainWindow(ctx)
    qtbot.addWidget(window)
    candidate = settings.model_copy(deep=True)

    concurrent = load_settings(path)
    concurrent.ai_profiles["default"].display_name = "另一窗口修改"
    save_settings(concurrent, path)

    with (
        patch("pa_agent.ai.client_factory.create_ai_client", return_value=MagicMock()),
        patch("pa_agent.gui.main_window.QMessageBox.warning") as warning,
    ):
        activated = window._activate_ai_profile_runtime(
            "default",
            candidate_settings=candidate,
        )

    assert activated is False
    assert ctx.client is old_client
    assert load_settings(path).ai_profiles["default"].display_name == "另一窗口修改"
    assert "另一个窗口也修改了 AI 模型档案" in warning.call_args.args[2]
    assert "重新启动 PA_Agent" in warning.call_args.args[2]


def test_failed_runtime_activation_does_not_reopen_settings_dialog(qtbot) -> None:
    settings = _verified_settings()
    source = MagicMock()
    source._connected = True
    source.list_symbols.return_value = []
    source.supported_timeframes.return_value = ["15m"]
    ctx = AppContext(settings=settings, data_source=source, client=MagicMock())

    from pa_agent.gui.main_window import MainWindow

    window = MainWindow(ctx)
    qtbot.addWidget(window)

    class _FakeDialog:
        created = 0

        def __init__(self, *_args, **_kwargs) -> None:
            type(self).created += 1
            self.activation_requested_id = "default"
            self.runtime_candidate = settings.model_copy(deep=True)
            self.runtime_refresh_required = False
            self.persisted_changes = False

        def exec(self) -> None:
            return None

        def focus_auth_field(self) -> None:
            return None

    with (
        patch(
            "pa_agent.gui.ai_model_settings_dialog.AIModelSettingsDialog",
            _FakeDialog,
        ),
        patch.object(window, "_activate_ai_profile_runtime", return_value=False),
    ):
        window._open_ai_model_settings_dialog()

    assert _FakeDialog.created == 1


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


def test_codex_catalog_drives_context_effort_and_speed_controls(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)
    dialog._adapter_combo.setCurrentIndex(
        dialog._adapter_combo.findData("codex_subscription")
    )
    dialog._apply_model_catalog(
        (
            ModelCatalogEntry(
                model_id="gpt-5.6-sol",
                display_name="GPT-5.6 Sol",
                context_window=372000,
                supports_thinking_on=True,
                supports_thinking_off=False,
                supported_efforts=(
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                ),
                default_effort="high",
                service_tiers=("fast",),
                speed_mode="service_tier",
                speed_description="可选择标准或 Fast 服务线路。",
            ),
        )
    )
    dialog._model_edit.setText("gpt-5.6-sol")
    dialog._refresh_capability_controls()

    assert dialog._context_window_label.text() == "372,000 tokens（模型固定）"
    assert dialog._thinking_check.isChecked() is True
    assert dialog._thinking_check.isEnabled() is False
    assert dialog._connection_form.isRowVisible(dialog._thinking_check) is False
    assert dialog._connection_form.isRowVisible(dialog._effort_combo) is True
    assert dialog._effort_combo.findData("ultra") >= 0
    assert dialog._speed_combo.findData("fast") >= 0
    assert dialog._speed_combo.isEnabled() is True
    assert dialog._connection_form.isRowVisible(dialog._speed_combo) is True
    assert dialog._manual_model_check.isChecked() is False
    assert dialog._model_edit.isEditable() is False


def test_kimi_non_reasoning_model_disables_thinking_and_effort(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)
    dialog._adapter_combo.setCurrentIndex(
        dialog._adapter_combo.findData("kimi")
    )
    dialog._apply_model_catalog(
        (
            ModelCatalogEntry(
                model_id="kimi-k2-turbo-preview",
                display_name="kimi-k2-turbo-preview",
                context_window=131072,
                supports_thinking_on=False,
                supports_thinking_off=True,
                speed_mode="model_variant",
                speed_description="这是高速模型版本。",
            ),
        )
    )
    dialog._model_edit.setText("kimi-k2-turbo-preview")
    dialog._refresh_capability_controls()

    assert dialog._thinking_check.isChecked() is False
    assert dialog._thinking_check.isEnabled() is False
    assert dialog._effort_combo.isEnabled() is False
    assert dialog._speed_combo.isEnabled() is False
    assert dialog._connection_form.isRowVisible(dialog._thinking_check) is False
    assert dialog._connection_form.isRowVisible(dialog._effort_combo) is False
    assert dialog._connection_form.isRowVisible(dialog._speed_combo) is False


def test_manual_model_entry_requires_explicit_opt_in(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)
    dialog._adapter_combo.setCurrentIndex(
        dialog._adapter_combo.findData("deepseek")
    )

    assert dialog._model_edit.isEditable() is False
    assert dialog._model_edit.count() >= 2

    dialog._manual_model_check.setChecked(True)
    dialog._model_edit.setText("deepseek-account-preview")

    assert dialog._model_edit.isEditable() is True
    assert dialog._provider_from_form().model == "deepseek-account-preview"


def test_model_refresh_failure_preserves_existing_catalog(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)
    dialog._adapter_combo.setCurrentIndex(
        dialog._adapter_combo.findData("kimi")
    )
    existing_ids = [
        dialog._model_edit.itemText(index)
        for index in range(dialog._model_edit.count())
    ]
    worker = MagicMock()
    worker.request_signature = dialog._catalog_signature(
        dialog._preview_provider()
    )
    worker.error_message = "认证失败"
    worker.entries = ()
    dialog._catalog_worker = worker

    dialog._on_model_catalog_finished()

    assert [
        dialog._model_edit.itemText(index)
        for index in range(dialog._model_edit.count())
    ] == existing_ids
    assert "已保留现有" in dialog._model_catalog_status.text()


def test_live_catalog_metadata_change_keeps_previous_verification(
    qtbot,
) -> None:
    settings = Settings()
    settings.save_ai_profile(
        "default",
        "Kimi API",
        AIProviderSettings(
            model="kimi-k2.6",
            base_url="https://api.moonshot.cn/v1",
            api_key="test-key",
            adapter_id="kimi",
        ),
        replace=True,
    )
    settings.mark_ai_profile_verification(
        "default",
        passed=True,
        tested_at="2026-07-17T00:00:00+00:00",
        adapter_id="kimi",
        checks={
            "connection_auth": True,
            "parameter_acceptance": True,
            "response_observed": True,
            "challenge_matched": True,
        },
    )
    dialog = AIModelSettingsDialog(settings)
    qtbot.addWidget(dialog)
    assert dialog._dirty is False
    assert dialog._activate_btn.isEnabled() is True
    assert dialog._context_window_value == 262_144
    assert dialog._context_window_source == "builtin"

    worker = MagicMock()
    worker.request_signature = dialog._catalog_signature(
        dialog._preview_provider()
    )
    worker.error_message = ""
    worker.entries = (
        ModelCatalogEntry(
            model_id="kimi-k2.6",
            display_name="kimi-k2.6",
            context_window=262_144,
            supports_thinking_on=True,
            supports_thinking_off=True,
        ),
    )
    dialog._catalog_worker = worker

    dialog._on_model_catalog_finished()

    assert dialog._context_window_value == 262_144
    assert dialog._context_window_source == "catalog"
    assert dialog._dirty is False
    assert dialog._activate_btn.isEnabled() is True
    assert dialog._working.ai_profiles["default"].verification.is_current_for(
        dialog._working.ai_profiles["default"].provider
    )


def test_stale_catalog_result_from_old_api_key_is_ignored(qtbot) -> None:
    settings = Settings()
    settings.save_ai_profile(
        "default",
        "Kimi API",
        AIProviderSettings(
            model="kimi-k2.6",
            base_url="https://api.moonshot.cn/v1",
            api_key="old-key",
            adapter_id="kimi",
        ),
        replace=True,
    )
    dialog = AIModelSettingsDialog(settings)
    qtbot.addWidget(dialog)
    previous_ids = tuple(
        dialog._model_edit.itemText(index)
        for index in range(dialog._model_edit.count())
    )
    worker = MagicMock()
    worker.request_signature = dialog._catalog_signature(
        dialog._preview_provider()
    )
    worker.error_message = ""
    worker.entries = (ModelCatalogEntry("new-only", "new-only"),)
    dialog._catalog_worker = worker
    dialog._api_key_edit.setText("new-key")

    dialog._on_model_catalog_finished()

    assert tuple(
        dialog._model_edit.itemText(index)
        for index in range(dialog._model_edit.count())
    ) == previous_ids
    assert "忽略旧模型列表" in dialog._model_catalog_status.text()


def test_codex_alias_is_normalized_before_probe(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)
    dialog._adapter_combo.setCurrentIndex(
        dialog._adapter_combo.findData("codex_subscription")
    )
    dialog._model_edit.setText("gpt 5.6 sol")
    entry = ModelCatalogEntry(
        model_id="gpt-5.6-sol",
        display_name="GPT-5.6 Sol",
        context_window=372000,
        supports_thinking_on=True,
        supports_thinking_off=False,
        supported_efforts=("high",),
    )

    with (
        patch(
            "pa_agent.gui.ai_model_settings_dialog.fetch_provider_model_catalog",
            return_value=(entry,),
        ),
        patch(
            "pa_agent.gui.ai_model_settings_dialog.provider_auth_configured",
            return_value=True,
        ),
    ):
        provider = dialog._provider_from_form()

    assert provider.model == "gpt-5.6-sol"
    assert dialog._model_edit.text() == "gpt-5.6-sol"


def test_probe_worker_contains_unexpected_exception() -> None:
    provider = AIProviderSettings(
        model="kimi-k2.6",
        base_url="https://api.moonshot.cn/v1",
        api_key="test-key",
        adapter_id="kimi",
    )
    worker = _ProviderProbeWorker(provider, CancelToken())

    with patch(
        "pa_agent.gui.ai_model_settings_dialog.probe_ai_provider",
        side_effect=RuntimeError("unexpected"),
    ):
        worker.run()

    assert worker.result is not None
    assert worker.result.error_code == "probe_internal_error"
    assert worker.result.connection_auth is ProbeStatus.UNKNOWN


def test_probe_finished_slot_contains_unexpected_exception(qtbot) -> None:
    dialog = AIModelSettingsDialog(Settings())
    qtbot.addWidget(dialog)
    worker = MagicMock()
    worker.result = _probe_result(adapter_id="deepseek")
    dialog._probe_worker = worker
    dialog._probe_cancel_token = CancelToken()

    with patch.object(
        dialog,
        "_on_probe_finished",
        side_effect=RuntimeError("unexpected"),
    ):
        dialog._on_probe_worker_finished()

    assert dialog._probe_worker is None
    assert dialog._probe_cancel_token is None
    assert "保持运行" in dialog._probe_message.text()
