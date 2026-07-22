"""测试专用设置对象, 禁止依赖用户真实 AI 认证状态。"""
from __future__ import annotations

from pathlib import Path

from pa_agent.ai.provider_capabilities import resolve_provider_capability
from pa_agent.config.settings import Settings, load_settings


def make_verified_test_settings(path: Path) -> Settings:
    """返回只在临时目录生效、且已通过测试认证门的设置。"""
    settings = load_settings(path)
    settings.general.alert_on_order_opportunity = False
    settings.provider.api_key = "test-api-key"
    settings.sync_active_ai_profile()
    adapter_id = resolve_provider_capability(settings.provider).adapter_id
    settings.mark_ai_profile_verification(
        settings.active_ai_profile_id,
        passed=True,
        tested_at="2026-07-20T00:00:00+00:00",
        adapter_id=adapter_id,
        checks={
            "connection_auth": True,
            "parameter_acceptance": True,
            "response_observed": True,
            "challenge_matched": True,
        },
    )
    return settings
