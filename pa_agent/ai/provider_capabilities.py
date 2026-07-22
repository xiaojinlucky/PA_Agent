"""Explicit inference adapters for supported AI provider protocols.

The registry describes request semantics only. A profile is considered usable as
"tested" solely through its persisted verification result, never from inference.

Official references (checked 2026-07-15):
- DeepSeek thinking mode: https://api-docs.deepseek.com/guides/thinking_mode/
- OpenAI Chat reasoning_effort: https://developers.openai.com/api/reference/resources/chat
- Anthropic thinking/effort: https://platform.claude.com/docs/en/build-with-claude/effort
- MiniMax OpenAI API: https://platform.minimax.io/docs/api-reference/text-chat-openai
- MiMo OpenAI API: https://mimo.mi.com/docs/api/chat/openai-api
- Kimi Chat API: https://platform.kimi.com/docs/api/chat
- Codex CLI: https://developers.openai.com/codex/noninteractive
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal

ClientKind = Literal["openai_chat", "cursor_sdk", "codex_cli"]
CAPABILITY_SCHEMA_VERSION = 4
REQUIRED_PROVIDER_VERIFICATION_CHECKS = (
    "connection_auth",
    "parameter_acceptance",
    "response_observed",
    "challenge_matched",
)
ThinkingTransport = Literal[
    "none",
    "reasoning_effort",
    "deepseek_toggle",
    "anthropic_adaptive",
    "anthropic_budget",
    "minimax_adaptive",
    "mimo_toggle",
    "kimi_toggle",
    "kimi_preserved",
]


@dataclass(frozen=True)
class ProviderCapability:
    adapter_id: str
    client_kind: ClientKind
    thinking_transport: ThinkingTransport
    supports_thinking_on: bool
    supports_thinking_off: bool
    supported_efforts: tuple[str, ...]
    max_tokens_parameter: str | None
    reasoning_response_fields: tuple[str, ...]


_CAPABILITIES = {
    "deepseek": ProviderCapability(
        adapter_id="deepseek",
        client_kind="openai_chat",
        thinking_transport="deepseek_toggle",
        supports_thinking_on=True,
        supports_thinking_off=True,
        supported_efforts=("high", "max"),
        max_tokens_parameter="max_tokens",
        reasoning_response_fields=("reasoning_content",),
    ),
    "kimi": ProviderCapability(
        adapter_id="kimi",
        client_kind="openai_chat",
        thinking_transport="kimi_toggle",
        supports_thinking_on=True,
        supports_thinking_off=True,
        supported_efforts=(),
        max_tokens_parameter="max_tokens",
        reasoning_response_fields=("reasoning_content",),
    ),
    "openai": ProviderCapability(
        adapter_id="openai",
        client_kind="openai_chat",
        thinking_transport="none",
        supports_thinking_on=False,
        supports_thinking_off=True,
        supported_efforts=(),
        max_tokens_parameter="max_tokens",
        reasoning_response_fields=("reasoning_content",),
    ),
    "anthropic_adaptive": ProviderCapability(
        adapter_id="anthropic_adaptive",
        client_kind="openai_chat",
        thinking_transport="anthropic_adaptive",
        supports_thinking_on=True,
        supports_thinking_off=True,
        supported_efforts=("low", "medium", "high", "max"),
        max_tokens_parameter="max_tokens",
        reasoning_response_fields=("reasoning_content",),
    ),
    "anthropic_adaptive_always": ProviderCapability(
        adapter_id="anthropic_adaptive_always",
        client_kind="openai_chat",
        thinking_transport="anthropic_adaptive",
        supports_thinking_on=True,
        supports_thinking_off=False,
        supported_efforts=("low", "medium", "high", "max"),
        max_tokens_parameter="max_tokens",
        reasoning_response_fields=("reasoning_content",),
    ),
    "anthropic_budget": ProviderCapability(
        adapter_id="anthropic_budget",
        client_kind="openai_chat",
        thinking_transport="anthropic_budget",
        supports_thinking_on=True,
        supports_thinking_off=True,
        supported_efforts=("low", "medium", "high", "max"),
        max_tokens_parameter="max_tokens",
        reasoning_response_fields=("reasoning_content",),
    ),
    "minimax_m3": ProviderCapability(
        adapter_id="minimax_m3",
        client_kind="openai_chat",
        thinking_transport="minimax_adaptive",
        supports_thinking_on=True,
        supports_thinking_off=True,
        supported_efforts=(),
        max_tokens_parameter="max_completion_tokens",
        reasoning_response_fields=("reasoning_content", "reasoning_details"),
    ),
    "minimax_m2": ProviderCapability(
        adapter_id="minimax_m2",
        client_kind="openai_chat",
        thinking_transport="minimax_adaptive",
        supports_thinking_on=True,
        supports_thinking_off=False,
        supported_efforts=(),
        max_tokens_parameter="max_completion_tokens",
        reasoning_response_fields=("reasoning_content", "reasoning_details"),
    ),
    "mimo": ProviderCapability(
        adapter_id="mimo",
        client_kind="openai_chat",
        thinking_transport="mimo_toggle",
        supports_thinking_on=True,
        supports_thinking_off=True,
        supported_efforts=(),
        max_tokens_parameter="max_completion_tokens",
        reasoning_response_fields=("reasoning_content",),
    ),
    "codex_subscription": ProviderCapability(
        adapter_id="codex_subscription",
        client_kind="codex_cli",
        thinking_transport="reasoning_effort",
        supports_thinking_on=True,
        supports_thinking_off=False,
        supported_efforts=("low", "medium", "high", "xhigh"),
        max_tokens_parameter=None,
        reasoning_response_fields=(),
    ),
    "cursor_agent": ProviderCapability(
        adapter_id="cursor_agent",
        client_kind="cursor_sdk",
        thinking_transport="none",
        supports_thinking_on=False,
        supports_thinking_off=False,
        supported_efforts=(),
        max_tokens_parameter=None,
        reasoning_response_fields=(),
    ),
    "generic_openai_compatible": ProviderCapability(
        adapter_id="generic_openai_compatible",
        client_kind="openai_chat",
        thinking_transport="none",
        supports_thinking_on=False,
        supports_thinking_off=True,
        supported_efforts=(),
        max_tokens_parameter="max_tokens",
        reasoning_response_fields=(),
    ),
    "generic_reasoning_compatible": ProviderCapability(
        adapter_id="generic_reasoning_compatible",
        client_kind="openai_chat",
        thinking_transport="reasoning_effort",
        supports_thinking_on=True,
        supports_thinking_off=False,
        supported_efforts=("low", "medium", "high", "max"),
        max_tokens_parameter="max_tokens",
        reasoning_response_fields=("reasoning_content",),
    ),
}

PROVIDER_CAPABILITIES: Mapping[str, ProviderCapability] = MappingProxyType(_CAPABILITIES)


def get_provider_capability(adapter_id: str) -> ProviderCapability:
    """Return a registered adapter and fail early for unknown explicit ids."""
    key = str(adapter_id or "").strip().lower()
    try:
        return PROVIDER_CAPABILITIES[key]
    except KeyError as exc:
        raise ValueError(f"unknown AI provider adapter: {adapter_id}") from exc


def _is_openclaw_alias(model: str) -> bool:
    return model in ("openclaw", "openclaw_wb", "openclaw_cs") or model.startswith(
        ("openclaw/", "openclaw_wb/", "openclaw_cs/")
    )


def infer_provider_adapter_id(base_url: str, model: str) -> str:
    """Infer a migration-compatible adapter suggestion from legacy fields."""
    base = str(base_url or "").strip().lower()
    model_id = str(model or "").strip().lower()

    if model_id in ("openclaw_cs",) or model_id.startswith("openclaw_cs/"):
        return "cursor_agent"
    if not _is_openclaw_alias(model_id) and ("deepseek.com" in base or "deepseek" in model_id):
        return "deepseek"
    if (
        "moonshot.cn" in base
        or "moonshot.ai" in base
        or model_id.startswith(("kimi-", "moonshot-"))
    ):
        return "kimi"
    if "minimax" in base or model_id.startswith("minimax-"):
        return "minimax_m3" if "minimax-m3" in model_id else "minimax_m2"
    if "xiaomimimo.com" in base or ("mimo" in model_id and not _is_openclaw_alias(model_id)):
        return "mimo"
    if "claude" in model_id:
        if any(token in model_id for token in ("fable-5", "mythos")):
            return "anthropic_adaptive_always"
        if any(
            token in model_id
            for token in (
                "claude-opus-4-8",
                "claude-opus-4-7",
                "claude-opus-4-6",
                "claude-sonnet-5",
                "claude-sonnet-4-6",
            )
        ):
            return "anthropic_adaptive"
        return "anthropic_budget"
    if "api.openai.com" in base or model_id.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return "generic_openai_compatible"


def resolve_provider_capability(settings: Any) -> ProviderCapability:
    """Resolve the explicit adapter, using inference only for legacy ``auto``."""
    adapter_id = str(getattr(settings, "adapter_id", "auto") or "auto").lower()
    if adapter_id == "auto":
        adapter_id = infer_provider_adapter_id(
            getattr(settings, "base_url", ""),
            getattr(settings, "model", ""),
        )
    capability = get_provider_capability(adapter_id)
    return _model_specific_capability(
        capability,
        str(getattr(settings, "model", "") or "").strip().lower(),
    )


def fixed_model_context_window(settings: Any) -> int | None:
    """Return a documented model limit when PA has an exact capability entry."""
    capability = resolve_provider_capability(settings)
    model = str(getattr(settings, "model", "") or "").strip().lower()
    if capability.adapter_id == "deepseek":
        if model.startswith("deepseek-v4"):
            return 1_000_000
        if model in {"deepseek-chat", "deepseek-reasoner"}:
            return 1_000_000
    if capability.adapter_id == "kimi":
        if model.startswith("kimi-k3"):
            return 1_048_576
        if model.startswith(("kimi-k2.5", "kimi-k2.6", "kimi-k2.7-code")):
            return 262_144
        if model.startswith(("kimi-k2-0905", "kimi-k2-turbo", "kimi-k2-thinking")):
            return 262_144
        if model.startswith("kimi-k2-0711"):
            return 131_072
        if model.startswith("moonshot-v1-128k"):
            return 131_072
        if model.startswith("moonshot-v1-32k"):
            return 32_768
        if model.startswith("moonshot-v1-8k"):
            return 8_192
    if capability.adapter_id == "codex_subscription":
        if model.startswith("gpt-5.6"):
            return 272_000
        if model.startswith(("gpt-5.5", "gpt-5.4", "gpt-5.2")):
            return 272_000
        if model.startswith("gpt-5.3-codex-spark"):
            return 128_000
        return None
    return None


def _model_specific_capability(
    capability: ProviderCapability,
    model: str,
) -> ProviderCapability:
    """收紧官方已知模型的原生 Thinking/effort 取值。"""
    if capability.adapter_id == "codex_subscription":
        if model.startswith("gpt-5.6-luna"):
            return replace(
                capability,
                supported_efforts=("low", "medium", "high", "xhigh", "max"),
            )
        if model in {"gpt-5.6", "gpt-5.6-sol"} or model.startswith(
            ("gpt-5.6-sol-", "gpt-5.6-terra")
        ):
            return replace(
                capability,
                supported_efforts=(
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                ),
            )
        return capability

    if capability.adapter_id == "kimi":
        if model.startswith("kimi-k3"):
            return replace(
                capability,
                thinking_transport="reasoning_effort",
                supports_thinking_off=False,
                supported_efforts=("max",),
            )
        if model.startswith("kimi-k2.7-code"):
            return replace(
                capability,
                thinking_transport="kimi_preserved",
                supports_thinking_off=False,
            )
        if model.startswith(("kimi-k2-thinking", "kimi-k2.7-code")):
            return replace(
                capability,
                thinking_transport="kimi_preserved",
                supports_thinking_on=True,
                supports_thinking_off=False,
                supported_efforts=(),
            )
        if model.startswith(("kimi-k2-0905", "kimi-k2-0711", "kimi-k2-turbo")):
            return replace(
                capability,
                thinking_transport="none",
                supports_thinking_on=False,
                supports_thinking_off=True,
                supported_efforts=(),
            )
        if model.startswith("moonshot-v1-"):
            return replace(
                capability,
                thinking_transport="none",
                supports_thinking_on=False,
                supports_thinking_off=True,
                supported_efforts=(),
            )

    if capability.adapter_id == "openai":
        responses_only_models = (
            "gpt-5-pro",
            "gpt-5-codex",
            "gpt-5.2-pro",
            "gpt-5.4-pro",
            "gpt-5.5-pro",
            "o1-pro",
            "o3-pro",
        )
        if any(
            model == model_prefix or model.startswith(f"{model_prefix}-")
            for model_prefix in responses_only_models
        ):
            raise ValueError(
                "该 OpenAI 模型仅支持 Responses API，当前 Chat Completions "
                "适配器不能使用"
            )

        def openai_reasoning(
            efforts: tuple[str, ...], *, supports_off: bool
        ) -> ProviderCapability:
            return replace(
                capability,
                thinking_transport="reasoning_effort",
                supports_thinking_on=True,
                supports_thinking_off=supports_off,
                supported_efforts=efforts,
                max_tokens_parameter="max_completion_tokens",
            )

        if model.startswith("gpt-5.6"):
            return openai_reasoning(
                ("low", "medium", "high", "xhigh", "max"),
                supports_off=True,
            )
        if model.startswith(("gpt-5.2", "gpt-5.3", "gpt-5.4", "gpt-5.5")):
            return openai_reasoning(
                ("low", "medium", "high", "xhigh"), supports_off=True
            )
        if model.startswith("gpt-5.1"):
            return openai_reasoning(
                ("low", "medium", "high"), supports_off=True
            )
        if model in {"gpt-5", "gpt-5-mini", "gpt-5-nano"} or any(
            model.startswith(f"{family}-202")
            for family in ("gpt-5", "gpt-5-mini", "gpt-5-nano")
        ):
            return openai_reasoning(
                ("minimal", "low", "medium", "high"), supports_off=False
            )
        if model.startswith(("o1", "o3", "o4", "gpt-oss")):
            return openai_reasoning(
                ("low", "medium", "high"), supports_off=False
            )

    if capability.adapter_id in {
        "anthropic_adaptive",
        "anthropic_adaptive_always",
    }:
        if any(
            token in model
            for token in (
                "fable-5",
                "opus-4-8",
                "opus-4-7",
                "sonnet-5",
            )
        ):
            return replace(
                capability,
                supported_efforts=("low", "medium", "high", "xhigh", "max"),
            )
        if any(token in model for token in ("opus-4-6", "sonnet-4-6")):
            return replace(
                capability,
                supported_efforts=("low", "medium", "high", "max"),
            )
        return replace(
            capability,
            supported_efforts=("low", "medium", "high"),
        )

    return capability


def should_preserve_reasoning_history(settings: Any) -> bool:
    """按供应商原生多轮合同判断是否回传历史 reasoning_content。"""
    capability = resolve_provider_capability(settings)
    if capability.adapter_id == "mimo":
        return True
    if capability.adapter_id != "kimi":
        return False
    model = str(getattr(settings, "model", "") or "").strip().lower()
    if capability.thinking_transport in {"kimi_preserved", "reasoning_effort"}:
        return True
    return model.startswith("kimi-k2.6") and bool(
        getattr(settings, "thinking", False)
    )


def normalise_reasoning_effort(
    capability: ProviderCapability,
    effort: str | None,
) -> str | None:
    """Map the selected level to the adapter/model's documented accepted values."""
    if not capability.supported_efforts:
        return None
    requested = str(effort or "high").strip().lower()
    if capability.adapter_id == "deepseek":
        return "max" if requested == "max" else "high"
    if requested in capability.supported_efforts:
        return requested
    return "high" if "high" in capability.supported_efforts else capability.supported_efforts[0]
