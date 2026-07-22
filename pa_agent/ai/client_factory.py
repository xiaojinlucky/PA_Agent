"""Construct the correct AI client for the configured provider route."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pa_agent.ai.provider_capabilities import resolve_provider_capability
from pa_agent.ai.provider_registry import (
    resolve_provider_runtime_settings,
    validate_provider_usage,
)
from pa_agent.config.settings import AIProviderSettings


def _safe_base_url_for_log(base_url: str) -> str:
    """只保留 URL 的协议、主机、端口和路径，绝不记录 userinfo/query。"""
    value = str(base_url or "").strip()
    if not value:
        return "(empty)"
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if not parsed.scheme or not host:
            return "(configured URL)"
        safe_host = f"[{host}]" if ":" in host else host
        try:
            port = parsed.port
        except ValueError:
            return "(configured URL)"
        netloc = f"{safe_host}:{port}" if port is not None else safe_host
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except (TypeError, ValueError):
        return "(configured URL)"


def create_ai_client(
    settings: AIProviderSettings,
    logger_: logging.Logger | None = None,
) -> Any:
    """Construct the client declared by the provider's resolved adapter."""
    log = logger_ or logging.getLogger(__name__)
    resolved = resolve_provider_runtime_settings(settings)
    validate_provider_usage(resolved)
    capability = resolve_provider_capability(resolved)
    if capability.client_kind == "codex_cli":
        from pa_agent.ai.codex_subscription_client import CodexSubscriptionClient

        log.info("AI client route: Codex subscription (model=%s)", resolved.model)
        return CodexSubscriptionClient(settings=resolved, logger_=log)
    if capability.client_kind == "cursor_sdk":
        from pa_agent.ai.cursor_sdk_client import CursorSdkClient

        log.info("AI client route: Cursor SDK (model=%s)", resolved.model)
        return CursorSdkClient(settings=resolved, logger_=log)

    from pa_agent.ai.deepseek_client import DeepSeekClient

    log.info(
        "AI client route: OpenAI-compatible (model=%s base_url=%s)",
        resolved.model,
        _safe_base_url_for_log(resolved.base_url),
    )
    return DeepSeekClient(settings=resolved, logger_=log)
