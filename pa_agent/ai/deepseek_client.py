"""DeepSeek AI client (OpenAI-compatible API)."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pa_agent.util.threading import CancelToken

from pa_agent.ai.mimo_compat import (
    ReasoningCache,
    mimo_max_output_tokens,
    patch_messages_for_mimo,
    response_message_dict,
    store_reasoning_from_response,
)
from pa_agent.ai.provider_capabilities import (
    normalise_reasoning_effort,
    resolve_provider_capability,
)
from pa_agent.config.settings import AIProviderSettings

try:
    from openai import OpenAI as _OpenAI  # type: ignore[import]
except ImportError as _exc:
    _OpenAI = None  # type: ignore[assignment,misc]
    _OPENAI_IMPORT_ERROR = _exc
else:
    _OPENAI_IMPORT_ERROR = None

logger = logging.getLogger(__name__)

_MIMO_REASONING_CACHE = ReasoningCache()


def clear_provider_runtime_caches() -> None:
    """清空不得跨 AI 档案复用的进程内缓存。"""
    _MIMO_REASONING_CACHE.clear()


@dataclass
class AIUsage:
    """Token usage from a single API call."""
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of prompt tokens served from KV cache (0.0–1.0).

        DeepSeek 硬盘缓存命中率。值越高，费用越低。
        0.0 = 无缓存命中；1.0 = 全部命中缓存。
        """
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_prompt_tokens / self.prompt_tokens

    @property
    def cache_miss_tokens(self) -> int:
        """Prompt tokens that were NOT served from cache (billed at full rate)."""
        return max(0, self.prompt_tokens - self.cached_prompt_tokens)


@dataclass
class AIReply:
    """Structured response from a single AI API call."""
    content: str
    reasoning_content: str
    raw: dict[str, Any]          # full raw response dict for debug tab
    usage: AIUsage
    request_id: str
    latency_ms: float


class CancelledError(Exception):
    """Raised when a cancel_token is set before or during an API call."""


def _is_deepseek_native(base_url: str) -> bool:
    return "deepseek.com" in (base_url or "").lower()


def _is_deepseek_model(model: str) -> bool:
    """True for DeepSeek model ids; excludes QClaw ``openclaw`` and WorkBuddy ``openclaw_wb`` Agent aliases."""
    m = (model or "").lower()
    if m in ("openclaw", "openclaw_wb", "openclaw_cs"):
        return False
    if m.startswith("openclaw/") or m.startswith("openclaw_wb/") or m.startswith("openclaw_cs/"):
        return False
    return "deepseek" in m


def _is_qclaw_openclaw_agent(settings: AIProviderSettings) -> bool:
    """True when requests go through QClaw's public-gateway OpenClaw Agent."""
    from pa_agent.ai.cursor_connector import is_openclaw_cs_model
    from pa_agent.ai.qclaw_connector import detect_qclaw, is_openclaw_model

    if not detect_qclaw():
        return False
    model = settings.model or ""
    return is_openclaw_model(model) or is_openclaw_cs_model(model)


def _openclaw_agent_request_extra(settings: AIProviderSettings) -> dict[str, Any]:
    """Ask QClaw/WorkBuddy Agent to answer in-chat only (no exec/write tool loop)."""
    if _is_qclaw_openclaw_agent(settings) or _is_workbuddy_agent(settings):
        return {"tool_choice": "none"}
    return {}


def _is_workbuddy_agent(settings: AIProviderSettings) -> bool:
    """True when requests go through WorkBuddy's model route."""
    from pa_agent.ai.workbuddy_connector import is_workbuddy_route

    return is_workbuddy_route(settings)


def _is_openclaw_agent_model(model: str) -> bool:
    """True for QClaw/WorkBuddy/Cursor OpenClaw Agent model aliases."""
    m = (model or "").lower()
    return (
        m in ("openclaw", "openclaw_wb", "openclaw_cs")
        or m.startswith("openclaw/")
        or m.startswith("openclaw_wb/")
        or m.startswith("openclaw_cs/")
    )


def supports_kv_prefix_chain(settings: AIProviderSettings | None) -> bool:
    """Whether Stage 2 may chain after Stage 1 messages for DeepSeek KV prefix cache.

    OpenClaw Agent routes misread ``system + stage1_user + stage2_user`` as a
    finished chat and reply with prose menus; those providers stay standalone.
    """
    if settings is None:
        return True
    if _is_qclaw_openclaw_agent(settings) or _is_workbuddy_agent(settings):
        return False
    if _is_openclaw_agent_model(settings.model):
        return False
    return resolve_provider_capability(settings).adapter_id == "deepseek"


def _extract_cached_prompt_tokens(usage: Any) -> int:
    """Read KV-cache hit count from provider usage (DeepSeek or OpenAI-compat)."""
    if usage is None:
        return 0
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is not None:
        return int(hit or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0)
        if cached:
            return int(cached)
    return 0


def _effective_api_model(settings: AIProviderSettings) -> str:
    """Model id sent to the upstream API (resolve provider aliases)."""
    if _is_workbuddy_agent(settings):
        from pa_agent.ai.workbuddy_connector import resolve_workbuddy_api_model

        return resolve_workbuddy_api_model(settings.model)
    return settings.model


def _workbuddy_agent_request_extra(settings: AIProviderSettings) -> dict[str, Any]:
    """Add WorkBuddy-specific request parameters.

    Returns empty dict if not using WorkBuddy agent route.
    WorkBuddy uses the same tool_choice: none strategy as QClaw.
    """
    return _openclaw_agent_request_extra(settings)


def _is_packyapi(base_url: str) -> bool:
    return "packyapi.com" in (base_url or "").lower()


# Packy claude-officially returns 400 if max_tokens exceeds model output cap.
_PACKY_CLAUDE_MAX_OUTPUT_TOKENS = 128_000
# DeepSeek API: max_tokens must be in [1, 393216].
_DEEPSEEK_MAX_OUTPUT_TOKENS = 393_216


# Sent to OpenAI-compatible gateways; upstream may clamp below these values.
_MINIMAX_M2_MAX_OUTPUT_TOKENS = 128_000
_OPENAI_GPT5_MAX_OUTPUT_TOKENS = 128_000
_ANTHROPIC_CURRENT_MAX_OUTPUT_TOKENS = 128_000
_ANTHROPIC_HAIKU_45_MAX_OUTPUT_TOKENS = 64_000

# Application policy for legacy Claude models using manual thinking budgets.
# Anthropic requires budget_tokens < max_tokens and notes diminishing use above 32k.
_EFFORT_TO_THINKING_BUDGET = {
    "low": 4_096,
    "medium": 16_384,
    "high": 32_768,
}


def _effort_budget_tokens(effort: str | None, *, max_output: int) -> int:
    """Thinking budget; must stay below max_output (Anthropic/Packy rule)."""
    if max_output <= 1_024:
        raise ValueError("max_output must be greater than 1024 for Anthropic thinking")
    response_reserve = max(256, min(4_096, max_output // 4))
    upper = max_output - response_reserve
    if upper < 1_024:
        raise ValueError("max_output leaves too little room for Anthropic response text")
    key = str(effort or "high").strip().lower()
    if key == "max":
        return upper
    target = _EFFORT_TO_THINKING_BUDGET.get(key, _EFFORT_TO_THINKING_BUDGET["high"])
    return min(target, upper)


def _thinking_enabled(extra_body: dict[str, Any], effort: str | None) -> bool:
    if extra_body:
        return extra_body.get("thinking", {}).get("type") in ("enabled", "adaptive")
    return effort is not None and effort != "none"


def _is_stream_options_rejection(exc: BaseException) -> bool:
    """Retry only when the provider explicitly rejects ``stream_options``."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if not isinstance(exc, TypeError) and status not in (400, 422):
        return False
    text = str(exc).lower()
    return "stream_options" in text or "include_usage" in text


def _packy_anthropic_messages_api(settings: AIProviderSettings) -> bool:
    """Packy claude-officially uses Anthropic Messages API (no role=system in messages)."""
    return _is_packyapi(settings.base_url) and "claude" in (settings.model or "").lower()


def _is_mimo(settings: AIProviderSettings) -> bool:
    return resolve_provider_capability(settings).adapter_id == "mimo"


def _prepare_chat_messages(
    settings: AIProviderSettings,
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Hoist system turns to top-level ``system`` for Anthropic-native Packy routes."""
    if not _packy_anthropic_messages_api(settings):
        return messages, None
    system_parts: list[str] = []
    api_messages: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            text = msg.get("content", "")
            if isinstance(text, str) and text.strip():
                system_parts.append(text)
            continue
        api_messages.append(msg)
    system_param = "\n\n".join(system_parts) if system_parts else None
    return api_messages, system_param


def _prepare_api_messages(
    settings: AIProviderSettings,
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Normalize messages for the active provider before API submission."""
    api_messages, system_param = _prepare_chat_messages(settings, messages)
    if _is_mimo(settings):
        api_messages = patch_messages_for_mimo(
            api_messages,
            model=settings.model,
            reasoning_cache=_MIMO_REASONING_CACHE,
        )
    return api_messages, system_param


def _provider_max_output_tokens(settings: AIProviderSettings) -> int | None:
    """Per-gateway completion cap (max_tokens); avoids 400 from provider limits."""
    model = (settings.model or "").lower()
    capability = resolve_provider_capability(settings)
    if _is_packyapi(settings.base_url) and "claude" in model:
        return _PACKY_CLAUDE_MAX_OUTPUT_TOKENS
    if capability.adapter_id == "deepseek":
        return _DEEPSEEK_MAX_OUTPUT_TOKENS
    if capability.adapter_id == "mimo":
        return mimo_max_output_tokens(settings.model)
    if capability.adapter_id == "minimax_m2":
        return _MINIMAX_M2_MAX_OUTPUT_TOKENS
    if capability.adapter_id == "openai" and model.startswith("gpt-5"):
        return _OPENAI_GPT5_MAX_OUTPUT_TOKENS
    if capability.adapter_id in {
        "anthropic_adaptive",
        "anthropic_adaptive_always",
    }:
        return _ANTHROPIC_CURRENT_MAX_OUTPUT_TOKENS
    if capability.adapter_id == "anthropic_budget":
        if "haiku-4-5" in model:
            return _ANTHROPIC_HAIKU_45_MAX_OUTPUT_TOKENS
        return _ANTHROPIC_CURRENT_MAX_OUTPUT_TOKENS
    return None


def _completion_token_parameter(settings: AIProviderSettings) -> str:
    capability = resolve_provider_capability(settings)
    return capability.max_tokens_parameter or "max_tokens"


def _completion_max_tokens(
    settings: AIProviderSettings,
    *,
    extra_body: dict[str, Any],
    effort: str | None,
) -> int | None:
    """Total completion budget (thinking + content) for OpenAI-compatible APIs."""
    del effort, extra_body
    return _provider_max_output_tokens(settings)


def _bounded_output_tokens(
    default_max: int | None,
    requested_max: int | None,
) -> int | None:
    """Apply an optional caller budget without exceeding provider limits."""
    if requested_max is None:
        return default_max
    value = int(requested_max)
    if value <= 0:
        raise ValueError("max_output_tokens must be greater than zero")
    return min(default_max, value) if default_max is not None else value


def _resolve_thinking_params(
    settings: AIProviderSettings,
    *,
    thinking: bool | None,
    reasoning_effort: str | None,
    max_output_tokens: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Return (extra_body, reasoning_effort) for chat.completions.create."""
    _thinking = thinking if thinking is not None else settings.thinking
    requested_effort = (
        reasoning_effort if reasoning_effort is not None else settings.reasoning_effort
    )
    capability = resolve_provider_capability(settings)
    _effort = normalise_reasoning_effort(capability, requested_effort)
    transport = capability.thinking_transport

    if transport == "deepseek_toggle":
        return (
            {"thinking": {"type": "enabled" if _thinking else "disabled"}},
            _effort if _thinking else None,
        )

    if transport == "reasoning_effort":
        effective_thinking = _thinking or not capability.supports_thinking_off
        if effective_thinking:
            return {}, _effort
        if capability.adapter_id == "openai" and capability.supports_thinking_off:
            return {}, "none"
        return {}, None

    if transport == "anthropic_adaptive":
        if not _thinking and capability.supports_thinking_off:
            return {"thinking": {"type": "disabled"}}, None
        extra_body: dict[str, Any] = {"thinking": {"type": "adaptive"}}
        if _effort is not None:
            extra_body["output_config"] = {"effort": _effort}
        return extra_body, None

    if transport == "minimax_adaptive":
        effective_thinking = _thinking or not capability.supports_thinking_off
        return {
            "thinking": {"type": "adaptive" if effective_thinking else "disabled"},
            "reasoning_split": True,
        }, None

    if transport == "mimo_toggle":
        return {
            "thinking": {"type": "enabled" if _thinking else "disabled"}
        }, None

    if transport == "kimi_toggle":
        effective_thinking = _thinking or not capability.supports_thinking_off
        thinking_body: dict[str, str] = {
            "type": "enabled" if effective_thinking else "disabled"
        }
        model = str(settings.model or "").strip().lower()
        if model.startswith("kimi-k2.6") and effective_thinking:
            thinking_body["keep"] = "all"
        return {"thinking": thinking_body}, None

    if transport == "kimi_preserved":
        # K2.7 Code 固定开启保留式思考，官方明确要求不要传 thinking 参数。
        return {}, None

    if not _thinking or transport == "none":
        return {}, None

    max_out = _bounded_output_tokens(
        _completion_max_tokens(settings, extra_body={}, effort=_effort),
        max_output_tokens,
    )

    if transport == "anthropic_budget":
        if max_out is None:
            raise ValueError("Anthropic manual thinking requires a known output limit")
        budget = _effort_budget_tokens(_effort, max_output=max_out)
        extra_body = {
            "thinking": {"type": "enabled", "budget_tokens": budget},
        }
        return extra_body, None

    return {}, None


class DeepSeekClient:
    """Thin wrapper around the OpenAI-compatible DeepSeek API."""

    def __init__(self, settings: AIProviderSettings, logger_: logging.Logger | None = None) -> None:
        self._settings = settings
        self._log = logger_ or logger

    def update_provider(self, settings: AIProviderSettings) -> None:
        """Replace in-memory provider settings (e.g. after QClaw auto-fallback)."""
        self._settings = settings

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        context_window: int | None = None,
        cancel_token: "CancelToken | None" = None,
        timeout_s: float = 600.0,
        max_output_tokens: int | None = None,
    ) -> AIReply:
        """Send *messages* to the DeepSeek API and return a structured reply.

        Raises CancelledError if cancel_token is set before the call.
        Never sends temperature/top_p/presence_penalty/frequency_penalty.
        """
        # Check cancellation before making the network call
        if cancel_token is not None and cancel_token.is_set():
            raise CancelledError("Request cancelled before API call")

        extra_body, _effort = _resolve_thinking_params(
            self._settings,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
        )
        extra_body = {**extra_body, **_openclaw_agent_request_extra(self._settings)}
        api_messages, system_param = _prepare_api_messages(self._settings, messages)
        if system_param:
            extra_body = {**extra_body, "system": system_param}
        capability = resolve_provider_capability(self._settings)
        _thinking_on = (
            _thinking_enabled(extra_body, _effort)
            or not capability.supports_thinking_off
        )
        _max_tokens = _bounded_output_tokens(
            _completion_max_tokens(
                self._settings,
                extra_body=extra_body,
                effort=_effort,
            ),
            max_output_tokens,
        )
        _token_parameter = _completion_token_parameter(self._settings)

        self._log.debug(
            "DeepSeekClient.chat: model=%s thinking=%s effort=%s max_tokens=%s "
            "system_hoisted=%s msgs=%d",
            self._settings.model,
            _thinking_on,
            _effort,
            _max_tokens,
            bool(system_param),
            len(api_messages),
        )

        if _OpenAI is None:
            raise RuntimeError("openai package is not installed") from _OPENAI_IMPORT_ERROR

        client = _OpenAI(
            base_url=self._settings.base_url,
            api_key=self._settings.api_key,
            max_retries=0,
        )

        t0 = time.monotonic()
        create_kwargs: dict[str, Any] = {
            "model": _effective_api_model(self._settings),
            "messages": api_messages,
            "timeout": timeout_s,
        }
        if _max_tokens is not None:
            create_kwargs[_token_parameter] = _max_tokens
        if extra_body:
            create_kwargs["extra_body"] = extra_body
        if _effort is not None:
            create_kwargs["reasoning_effort"] = _effort
        # Kimi K2.5/K2.6 的温度由官方按 Thinking 模式固定；显式传 0 会被拒绝。
        # 其他现有适配器在关闭 Thinking 时继续使用 0，以保持 JSON 指令稳定性。
        if not _thinking_on and capability.adapter_id != "kimi":
            create_kwargs["temperature"] = 0
        try:
            response = client.chat.completions.create(
                **create_kwargs,
                # IMPORTANT: do NOT add temperature, top_p, presence_penalty,
                # frequency_penalty — they are incompatible with thinking mode.
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self._log.error("DeepSeekClient API error after %.0f ms: %s", latency_ms, exc)
            raise

        latency_ms = (time.monotonic() - t0) * 1000

        msg = response.choices[0].message
        content = msg.content or ""
        reasoning_content = getattr(msg, "reasoning_content", None) or ""
        # MiniMax with reasoning_split=True may also use reasoning_details
        if not reasoning_content:
            details = getattr(msg, "reasoning_details", None)
            if details:
                parts = []
                for detail in details:
                    t = detail.get("text") if isinstance(detail, dict) else getattr(detail, "text", None)
                    if t:
                        parts.append(t)
                reasoning_content = "".join(parts)

        if _is_mimo(self._settings):
            store_reasoning_from_response(
                api_messages,
                response_message_dict(content, reasoning_content, msg),
                _MIMO_REASONING_CACHE,
            )

        # Build usage
        u = response.usage
        usage = AIUsage(
            prompt_tokens=getattr(u, "prompt_tokens", 0),
            cached_prompt_tokens=_extract_cached_prompt_tokens(u),
            completion_tokens=getattr(u, "completion_tokens", 0),
            total_tokens=getattr(u, "total_tokens", 0),
        )

        request_id = getattr(response, "id", "") or ""

        # Build raw dict for debug tab — mask API key if it somehow appears
        raw: dict[str, Any] = {
            "id": request_id,
            "model": getattr(response, "model", ""),
            "content": content,
            "reasoning_content": reasoning_content,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "cached_prompt_tokens": usage.cached_prompt_tokens,
                "cache_miss_tokens": usage.cache_miss_tokens,
                "cache_hit_rate_pct": round(usage.cache_hit_rate * 100, 1),
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
            "latency_ms": latency_ms,
        }

        self._log.debug(
            "DeepSeekClient.chat done: latency=%.0f ms tokens=%d/%d",
            latency_ms, usage.prompt_tokens, usage.completion_tokens,
        )

        # Log KV-cache hit rate so operators can monitor savings.
        # DeepSeek硬盘缓存：prompt_cache_hit_tokens 是命中缓存的 token 数。
        if usage.prompt_tokens > 0:
            hit_rate = usage.cached_prompt_tokens / usage.prompt_tokens * 100
            self._log.info(
                "KV-cache: hit=%d miss=%d total_prompt=%d hit_rate=%.1f%%",
                usage.cached_prompt_tokens,
                usage.prompt_tokens - usage.cached_prompt_tokens,
                usage.prompt_tokens,
                hit_rate,
            )

        return AIReply(
            content=content,
            reasoning_content=reasoning_content,
            raw=raw,
            usage=usage,
            request_id=request_id,
            latency_ms=latency_ms,
        )

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        on_reasoning_token: Callable[[str], None] | None = None,
        on_content_token: Callable[[str], None] | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        cancel_token: "CancelToken | None" = None,
        timeout_s: float = 600.0,
        max_output_tokens: int | None = None,
    ) -> AIReply:
        """Stream *messages* to the DeepSeek API, calling callbacks per token.

        Follows the official DeepSeek streaming example exactly:
        - reasoning_content tokens arrive first (thinking phase)
        - content tokens arrive after (answer phase)
        - delta.reasoning_content is None (not empty string) when absent

        Parameters
        ----------
        on_reasoning_token:
            Called with each reasoning/thinking token chunk as it arrives.
        on_content_token:
            Called with each content token chunk as it arrives.

        Returns the same AIReply as chat() once the stream is complete.
        Raises CancelledError if cancel_token is set before or during the call.
        """
        if cancel_token is not None and cancel_token.is_set():
            raise CancelledError("Request cancelled before API call")

        from pa_agent.ai.cursor_connector import is_openclaw_cs_model

        if is_openclaw_cs_model(self._settings.model):
            raise RuntimeError(
                "模型 openclaw_cs 必须使用 Cursor SDK 路由，但当前仍在使用 DeepSeekClient。"
                "请在「AI 模型」设置中重新保存，或重启应用后再分析。"
            )

        extra_body, _effort = _resolve_thinking_params(
            self._settings,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
        )
        extra_body = {**extra_body, **_openclaw_agent_request_extra(self._settings)}
        api_messages, system_param = _prepare_api_messages(self._settings, messages)
        if system_param:
            extra_body = {**extra_body, "system": system_param}
        capability = resolve_provider_capability(self._settings)
        _thinking_on = (
            _thinking_enabled(extra_body, _effort)
            or not capability.supports_thinking_off
        )
        _max_tokens = _bounded_output_tokens(
            _completion_max_tokens(
                self._settings,
                extra_body=extra_body,
                effort=_effort,
            ),
            max_output_tokens,
        )
        _token_parameter = _completion_token_parameter(self._settings)

        self._log.info(
            "DeepSeekClient.stream_chat: model=%s thinking=%s reasoning_effort=%s "
            "max_tokens=%s system_hoisted=%s msgs=%d",
            self._settings.model,
            _thinking_on,
            _effort,
            _max_tokens,
            bool(system_param),
            len(api_messages),
        )

        if _OpenAI is None:
            raise RuntimeError("openai package is not installed") from _OPENAI_IMPORT_ERROR

        client = _OpenAI(
            base_url=self._settings.base_url,
            api_key=self._settings.api_key,
            max_retries=0,
        )

        t0 = time.monotonic()
        reasoning_content = ""
        content = ""
        request_id = ""
        model_name = ""
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        cached_tokens = 0

        try:
            # Build kwargs with stream_options to get usage in the final chunk.
            # Some providers may not support it; if the create() call itself
            # rejects stream_options we retry without it.
            deadline = t0 + float(timeout_s)
            stream_kwargs: dict[str, Any] = {
                "model": _effective_api_model(self._settings),
                "messages": api_messages,
                "timeout": timeout_s,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if _max_tokens is not None:
                stream_kwargs[_token_parameter] = _max_tokens
            if extra_body:
                stream_kwargs["extra_body"] = extra_body
            if _effort is not None:
                stream_kwargs["reasoning_effort"] = _effort

            try:
                stream = client.chat.completions.create(**stream_kwargs)
            except Exception as exc:
                if not _is_stream_options_rejection(exc):
                    raise
                self._log.debug("stream_options not supported; retrying without it")
                stream_kwargs.pop("stream_options", None)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("AI request timed out") from None
                stream_kwargs["timeout"] = remaining
                stream = client.chat.completions.create(**stream_kwargs)

            for chunk in stream:
                if time.monotonic() >= deadline:
                    raise TimeoutError("AI request timed out")
                # Check cancellation on each chunk
                if cancel_token is not None and cancel_token.is_set():
                    raise CancelledError("Request cancelled during streaming")

                # Extract usage from the final chunk (stream_options)
                if hasattr(chunk, "usage") and chunk.usage is not None:
                    u = chunk.usage
                    prompt_tokens = getattr(u, "prompt_tokens", 0) or prompt_tokens
                    completion_tokens = getattr(u, "completion_tokens", 0) or completion_tokens
                    total_tokens = getattr(u, "total_tokens", 0) or total_tokens
                    cached_tokens = _extract_cached_prompt_tokens(u) or cached_tokens

                if not getattr(chunk, "choices", None):
                    continue

                request_id = request_id or (getattr(chunk, "id", "") or "")
                model_name = model_name or (getattr(chunk, "model", "") or "")

                choice0 = chunk.choices[0]
                delta = getattr(choice0, "delta", None)
                if delta is None:
                    continue

                # Official pattern: reasoning_content is None when absent, not ""
                # reasoning_content arrives first (thinking phase), then content
                # MiniMax with reasoning_split=True uses delta.reasoning_details[].text
                # instead of delta.reasoning_content.
                r = getattr(delta, "reasoning_content", None)
                if not r:
                    # MiniMax streaming: reasoning_details is a list of dicts
                    details = getattr(delta, "reasoning_details", None)
                    if details:
                        for detail in details:
                            t = detail.get("text") if isinstance(detail, dict) else getattr(detail, "text", None)
                            if t:
                                r = (r or "") + t
                if r:
                    reasoning_content += r
                    if on_reasoning_token is not None:
                        on_reasoning_token(r)
                delta_content = getattr(delta, "content", None)
                if delta_content:
                    content += delta_content
                    if on_content_token is not None:
                        on_content_token(delta_content)

        except CancelledError:
            raise
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self._log.error("DeepSeekClient stream error after %.0f ms: %s", latency_ms, exc)
            raise

        latency_ms = (time.monotonic() - t0) * 1000

        usage = AIUsage(
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        raw: dict[str, Any] = {
            "id": request_id,
            "model": model_name,
            "content": content,
            "reasoning_content": reasoning_content,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "cached_prompt_tokens": usage.cached_prompt_tokens,
                "cache_miss_tokens": usage.cache_miss_tokens,
                "cache_hit_rate_pct": round(usage.cache_hit_rate * 100, 1),
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
            "latency_ms": latency_ms,
        }

        self._log.info(
            "DeepSeekClient.stream_chat done: latency=%.0f ms "
            "reasoning_chars=%d content_chars=%d deepseek_thinking=%s effort=%s",
            latency_ms,
            len(reasoning_content),
            len(content),
            _thinking_on,
            _effort,
        )

        # Log KV-cache hit rate for stream calls as well.
        if usage.prompt_tokens > 0:
            hit_rate = usage.cached_prompt_tokens / usage.prompt_tokens * 100
            self._log.info(
                "KV-cache: hit=%d miss=%d total_prompt=%d hit_rate=%.1f%%",
                usage.cached_prompt_tokens,
                usage.prompt_tokens - usage.cached_prompt_tokens,
                usage.prompt_tokens,
                hit_rate,
            )
        if not content.strip():
            self._log.warning(
                "API returned empty content (model=%s base_url=%s). "
                "Check 原始 tab Raw Response; for KKAI/Claude ensure model ID and token group match.",
                self._settings.model,
                self._settings.base_url,
            )
        if _thinking_on and len(reasoning_content) < 80:
            adapter_id = resolve_provider_capability(self._settings).adapter_id
            self._log.warning(
                "Thinking enabled but reasoning_content is very short (%d chars). "
                "Check adapter, model ID, token group, and provider response fields "
                "(adapter=%s reasoning_effort=%s).",
                len(reasoning_content),
                adapter_id,
                _effort,
            )

        if _is_mimo(self._settings):
            store_reasoning_from_response(
                api_messages,
                {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning_content,
                },
                _MIMO_REASONING_CACHE,
            )

        return AIReply(
            content=content,
            reasoning_content=reasoning_content,
            raw=raw,
            usage=usage,
            request_id=request_id,
            latency_ms=latency_ms,
        )
