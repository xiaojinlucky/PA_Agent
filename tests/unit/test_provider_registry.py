"""供应商模板和共享 env 解析。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pa_agent.ai.provider_registry import (
    preset_runtime_defaults,
    resolve_provider_runtime_settings,
    validate_provider_usage,
)
from pa_agent.config.settings import AIProviderSettings, provider_config_fingerprint


def test_kimi_preset_uses_shared_env_without_mutating_profile() -> None:
    profile = AIProviderSettings(
        adapter_id="kimi",
        model="kimi-k2.6",
        base_url="https://api.moonshot.cn/v1",
        api_key="",
    )
    values = {
        "MOONSHOT_API_KEY": "moonshot-secret",
        "MOONSHOT_MODEL": "kimi-k3",
        "MOONSHOT_BASE_URL": "https://api.moonshot.ai/v1",
    }
    with patch("pa_agent.ai.provider_registry.merged_environment", return_value=values):
        resolved = resolve_provider_runtime_settings(profile)
        defaults = preset_runtime_defaults("kimi")

    assert resolved.api_key == "moonshot-secret"
    assert profile.api_key == ""
    assert defaults == ("kimi-k3", "https://api.moonshot.ai/v1")


def test_explicit_profile_values_win_over_env_defaults() -> None:
    profile = AIProviderSettings(
        adapter_id="deepseek",
        model="deepseek-v4-pro",
        base_url="https://custom.example/v1",
        api_key="explicit-secret",
    )
    with patch(
        "pa_agent.ai.provider_registry.merged_environment",
        return_value={
            "DEEPSEEK_API_KEY": "env-secret",
            "DEEPSEEK_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        },
    ):
        resolved = resolve_provider_runtime_settings(profile)

    assert resolved.model == "deepseek-v4-pro"
    assert resolved.base_url == "https://custom.example/v1"
    assert resolved.api_key == "explicit-secret"


def test_secret_fingerprint_changes_when_env_key_changes() -> None:
    profile = AIProviderSettings(adapter_id="mimo", api_key="")
    with patch(
        "pa_agent.ai.provider_registry.merged_environment",
        return_value={"MIMO_API_KEY": "first"},
    ):
        first = provider_config_fingerprint(profile)
    with patch(
        "pa_agent.ai.provider_registry.merged_environment",
        return_value={"MIMO_API_KEY": "second"},
    ):
        second = provider_config_fingerprint(profile)

    assert first
    assert second
    assert first != second


def test_context_metadata_does_not_change_verification_fingerprint() -> None:
    first = AIProviderSettings(
        adapter_id="codex_subscription",
        model="gpt-5.6-sol",
        context_window=272_000,
    )
    second = first.model_copy(update={"context_window": 372_000})

    assert provider_config_fingerprint(first) == provider_config_fingerprint(second)


def test_mimo_token_plan_key_is_rejected_for_pa_automation() -> None:
    profile = AIProviderSettings(
        adapter_id="mimo",
        model="mimo-v2.5-pro",
        base_url="https://api.xiaomimimo.com/v1",
        api_key="",
    )
    with patch(
        "pa_agent.ai.provider_registry.merged_environment",
        return_value={"MIMO_API_KEY": "tp-test-secret"},
    ):
        resolved = resolve_provider_runtime_settings(profile)

    with pytest.raises(ValueError, match="Token Plan"):
        validate_provider_usage(resolved)


def test_mimo_payg_key_is_allowed() -> None:
    profile = AIProviderSettings(
        adapter_id="mimo",
        model="mimo-v2.5-pro",
        base_url="https://api.xiaomimimo.com/v1",
        api_key="sk-test-secret",
    )

    resolved = resolve_provider_runtime_settings(profile)

    validate_provider_usage(resolved)
