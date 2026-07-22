"""Unit tests for explicit provider inference adapters."""

from __future__ import annotations

import pytest

from pa_agent.ai.provider_capabilities import (
    REQUIRED_PROVIDER_VERIFICATION_CHECKS,
    fixed_model_context_window,
    get_provider_capability,
    infer_provider_adapter_id,
    normalise_reasoning_effort,
    resolve_provider_capability,
)
from pa_agent.config.settings import AIProviderSettings


@pytest.mark.parametrize(
    ("base_url", "model", "expected"),
    [
        ("https://api.deepseek.com", "deepseek-v4-pro", "deepseek"),
        ("https://api.openai.com/v1", "gpt-5.6", "openai"),
        ("https://proxy.example/v1", "claude-opus-4-8", "anthropic_adaptive"),
        ("https://proxy.example/v1", "claude-opus-4-5", "anthropic_budget"),
        ("https://api.minimax.io/v1", "MiniMax-M3", "minimax_m3"),
        ("https://api.minimax.io/v1", "MiniMax-M2.7", "minimax_m2"),
        ("https://api.moonshot.cn/v1", "kimi-k2.6", "kimi"),
        ("https://api.xiaomimimo.com/v1", "mimo-v2.5-pro", "mimo"),
        ("", "openclaw_cs/main", "cursor_agent"),
    ],
)
def test_legacy_provider_inference(base_url: str, model: str, expected: str) -> None:
    assert infer_provider_adapter_id(base_url, model) == expected


def test_explicit_adapter_overrides_legacy_inference() -> None:
    settings = AIProviderSettings(
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        adapter_id="openai",
    )
    assert resolve_provider_capability(settings).adapter_id == "openai"


def test_unknown_explicit_adapter_fails_early() -> None:
    settings = AIProviderSettings(adapter_id="not-registered")
    with pytest.raises(ValueError, match="unknown AI provider adapter"):
        resolve_provider_capability(settings)


def test_official_capability_semantics() -> None:
    deepseek = get_provider_capability("deepseek")
    assert deepseek.supported_efforts == ("high", "max")
    assert deepseek.max_tokens_parameter == "max_tokens"

    openai = get_provider_capability("openai")
    assert openai.thinking_transport == "none"
    assert openai.max_tokens_parameter == "max_tokens"

    anthropic = get_provider_capability("anthropic_adaptive")
    assert anthropic.thinking_transport == "anthropic_adaptive"
    assert anthropic.supported_efforts == ("low", "medium", "high", "max")

    minimax_m2 = get_provider_capability("minimax_m2")
    assert minimax_m2.supports_thinking_off is False
    assert minimax_m2.supported_efforts == ()

    mimo = get_provider_capability("mimo")
    assert mimo.thinking_transport == "mimo_toggle"
    assert mimo.supported_efforts == ()

    kimi = get_provider_capability("kimi")
    assert kimi.thinking_transport == "kimi_toggle"
    assert kimi.max_tokens_parameter == "max_tokens"

    codex = get_provider_capability("codex_subscription")
    assert codex.client_kind == "codex_cli"
    assert codex.supported_efforts == (
        "low",
        "medium",
        "high",
        "xhigh",
    )

    codex_56 = resolve_provider_capability(
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="gpt-5.6-sol",
        )
    )
    assert codex_56.supported_efforts == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    )
    codex_luna = resolve_provider_capability(
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="gpt-5.6-luna",
        )
    )
    assert codex_luna.supported_efforts == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )

    cursor = get_provider_capability("cursor_agent")
    assert cursor.client_kind == "cursor_sdk"
    assert cursor.supports_thinking_on is False


def test_codex_context_window_requires_an_exact_known_model() -> None:
    assert (
        fixed_model_context_window(
            AIProviderSettings(
                adapter_id="codex_subscription",
                model="gpt-5.6-sol",
            )
        )
        == 272_000
    )
    assert (
        fixed_model_context_window(
            AIProviderSettings(
                adapter_id="codex_subscription",
                model="gpt-5.5",
            )
        )
        == 272_000
    )
    assert (
        fixed_model_context_window(
            AIProviderSettings(
                adapter_id="codex_subscription",
                model="auto",
            )
        )
        is None
    )


@pytest.mark.parametrize(
    ("model", "supports_off", "efforts"),
    [
        ("gpt-4.1", True, ()),
        ("gpt-5", False, ("minimal", "low", "medium", "high")),
        ("gpt-5.1", True, ("low", "medium", "high")),
        ("gpt-5.4", True, ("low", "medium", "high", "xhigh")),
        ("gpt-5.6", True, ("low", "medium", "high", "xhigh", "max")),
        ("o3-mini", False, ("low", "medium", "high")),
    ],
)
def test_openai_model_specific_reasoning_matrix(
    model: str,
    supports_off: bool,
    efforts: tuple[str, ...],
) -> None:
    capability = resolve_provider_capability(
        AIProviderSettings(
            adapter_id="openai",
            base_url="https://api.openai.com/v1",
            model=model,
        )
    )

    assert capability.supports_thinking_off is supports_off
    assert capability.supported_efforts == efforts
    assert capability.thinking_transport == (
        "none" if not efforts else "reasoning_effort"
    )


def test_generic_chat_defaults_to_no_thinking_parameters() -> None:
    plain = get_provider_capability("generic_openai_compatible")
    reasoning = get_provider_capability("generic_reasoning_compatible")

    assert plain.thinking_transport == "none"
    assert plain.supported_efforts == ()
    assert reasoning.thinking_transport == "reasoning_effort"


def test_kimi_model_specific_thinking_matrix() -> None:
    k3 = resolve_provider_capability(
        AIProviderSettings(adapter_id="kimi", model="kimi-k3")
    )
    assert k3.supports_thinking_off is False
    assert k3.thinking_transport == "reasoning_effort"
    assert k3.supported_efforts == ("max",)

    k26 = resolve_provider_capability(
        AIProviderSettings(adapter_id="kimi", model="kimi-k2.6")
    )
    assert k26.supports_thinking_off is True
    assert k26.thinking_transport == "kimi_toggle"

    k27 = resolve_provider_capability(
        AIProviderSettings(adapter_id="kimi", model="kimi-k2.7-code")
    )
    assert k27.supports_thinking_off is False
    assert k27.thinking_transport == "kimi_preserved"


@pytest.mark.parametrize(
    "model",
    [
        "gpt-5-pro",
        "gpt-5-codex",
        "gpt-5.2-pro",
        "gpt-5.4-pro",
        "gpt-5.5-pro",
        "o1-pro",
        "o1-pro-2025-03-19",
        "o3-pro",
        "o3-pro-2025-06-10",
    ],
)
def test_responses_only_openai_pro_models_fail_before_probe(model: str) -> None:
    with pytest.raises(ValueError, match="Responses API"):
        resolve_provider_capability(
            AIProviderSettings(
                adapter_id="openai",
                base_url="https://api.openai.com/v1",
                model=model,
            )
        )


def test_verification_requires_random_challenge_match() -> None:
    assert REQUIRED_PROVIDER_VERIFICATION_CHECKS == (
        "connection_auth",
        "parameter_acceptance",
        "response_observed",
        "challenge_matched",
    )


def test_deepseek_effort_is_normalised_to_documented_values() -> None:
    capability = get_provider_capability("deepseek")
    assert normalise_reasoning_effort(capability, "low") == "high"
    assert normalise_reasoning_effort(capability, "medium") == "high"
    assert normalise_reasoning_effort(capability, "high") == "high"
    assert normalise_reasoning_effort(capability, "max") == "max"
