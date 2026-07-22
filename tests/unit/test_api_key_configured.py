"""Tests for API key presence helper."""
from __future__ import annotations

from unittest.mock import patch

from pa_agent.config.settings import Settings, provider_api_key_configured


def test_provider_api_key_configured_empty() -> None:
    s = Settings()
    s.provider.api_key = ""
    with patch("pa_agent.ai.provider_registry.merged_environment", return_value={}):
        assert not provider_api_key_configured(s)
    assert not provider_api_key_configured(None)


def test_provider_api_key_configured_whitespace() -> None:
    s = Settings()
    s.provider.api_key = "   "
    with patch("pa_agent.ai.provider_registry.merged_environment", return_value={}):
        assert not provider_api_key_configured(s)


def test_provider_api_key_configured_present() -> None:
    s = Settings()
    s.provider.api_key = "sk-test"
    assert provider_api_key_configured(s)


def test_provider_api_key_configured_from_shared_env() -> None:
    s = Settings()
    s.provider.adapter_id = "deepseek"
    s.provider.api_key = ""
    with patch(
        "pa_agent.ai.provider_registry.merged_environment",
        return_value={"DEEPSEEK_API_KEY": "env-secret"},
    ):
        assert provider_api_key_configured(s)


def test_provider_api_key_configured_from_codex_login() -> None:
    s = Settings()
    s.provider.adapter_id = "codex_subscription"
    s.provider.model = "auto"
    with patch(
        "pa_agent.ai.codex_subscription_client.codex_login_status"
    ) as status:
        status.return_value.logged_in = True
        assert provider_api_key_configured(s)


def test_verified_active_codex_profile_does_not_override_logged_out_state() -> None:
    s = Settings()
    provider = s.provider.model_copy(
        update={
            "adapter_id": "codex_subscription",
            "model": "auto",
            "base_url": "",
            "api_key": "",
        }
    )
    s.save_ai_profile("default", "Codex 订阅", provider, replace=True)
    s.mark_ai_profile_verification(
        "default",
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
    with patch(
        "pa_agent.ai.codex_subscription_client.codex_login_status"
    ) as status:
        status.return_value.logged_in = False
        assert not provider_api_key_configured(s)
