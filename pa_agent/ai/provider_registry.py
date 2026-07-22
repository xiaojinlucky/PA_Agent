"""PA_Agent 支持的大模型供应商模板与运行时凭据解析。"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from pa_agent.execution.credentials import merged_environment

if TYPE_CHECKING:
    from pa_agent.config.settings import AIProviderSettings

AuthKind = Literal["api_key", "codex_subscription"]


@dataclass(frozen=True)
class ProviderPreset:
    adapter_id: str
    label: str
    auth_kind: AuthKind
    default_model: str
    default_base_url: str
    api_key_env_names: tuple[str, ...] = ()
    model_env_names: tuple[str, ...] = ()
    base_url_env_names: tuple[str, ...] = ()


_PRESETS = {
    "codex_subscription": ProviderPreset(
        adapter_id="codex_subscription",
        label="Codex 订阅（ChatGPT 登录）",
        auth_kind="codex_subscription",
        default_model="auto",
        default_base_url="",
    ),
    "deepseek": ProviderPreset(
        adapter_id="deepseek",
        label="DeepSeek API",
        auth_kind="api_key",
        default_model="deepseek-v4-flash",
        default_base_url="https://api.deepseek.com",
        api_key_env_names=("DEEPSEEK_API_KEY",),
        model_env_names=("DEEPSEEK_MODEL",),
        base_url_env_names=("DEEPSEEK_BASE_URL",),
    ),
    "kimi": ProviderPreset(
        adapter_id="kimi",
        label="Kimi API",
        auth_kind="api_key",
        default_model="kimi-k2.6",
        default_base_url="https://api.moonshot.cn/v1",
        api_key_env_names=("KIMI_API_KEY", "MOONSHOT_API_KEY"),
        model_env_names=("KIMI_MODEL", "MOONSHOT_MODEL"),
        base_url_env_names=("KIMI_BASE_URL", "MOONSHOT_BASE_URL"),
    ),
    "mimo": ProviderPreset(
        adapter_id="mimo",
        label="小米 MiMo API",
        auth_kind="api_key",
        default_model="mimo-v2.5-pro",
        default_base_url="https://api.xiaomimimo.com/v1",
        api_key_env_names=("MIMO_API_KEY",),
        model_env_names=("MIMO_MODEL",),
        base_url_env_names=("MIMO_BASE_URL",),
    ),
}

PROVIDER_PRESETS = MappingProxyType(_PRESETS)


def get_provider_preset(adapter_id: str) -> ProviderPreset | None:
    return PROVIDER_PRESETS.get(str(adapter_id or "").strip().lower())


def _first_configured(values: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = str(values.get(name, "") or "").strip()
        if value:
            return value
    return ""


def preset_runtime_defaults(adapter_id: str) -> tuple[str, str]:
    """返回供应商模板的模型和 Base URL；共享 env 可覆盖内置默认值。"""
    preset = get_provider_preset(adapter_id)
    if preset is None:
        return "", ""
    values = merged_environment()
    model = _first_configured(values, preset.model_env_names) or preset.default_model
    base_url = (
        _first_configured(values, preset.base_url_env_names)
        or preset.default_base_url
    )
    return model, base_url


def resolve_provider_runtime_settings(
    settings: AIProviderSettings,
) -> AIProviderSettings:
    """在内存副本中补齐 env 凭据，不把共享 env 的密钥写回设置文件。"""
    from pa_agent.ai.provider_capabilities import resolve_provider_capability

    resolved = settings.model_copy(deep=True)
    capability = resolve_provider_capability(resolved)
    preset = get_provider_preset(capability.adapter_id)
    if preset is None:
        return resolved

    if preset.auth_kind == "api_key" and not resolved.api_key.strip():
        resolved.api_key = _first_configured(
            merged_environment(),
            preset.api_key_env_names,
        )
    return resolved


def validate_provider_usage(settings: AIProviderSettings) -> None:
    """阻止把受限套餐密钥用于 PA_Agent 的非编程自动化分析。"""
    from pa_agent.ai.provider_capabilities import resolve_provider_capability

    capability = resolve_provider_capability(settings)
    if (
        capability.adapter_id == "mimo"
        and settings.api_key.strip().startswith("tp-")
    ):
        raise ValueError(
            "小米 MiMo Token Plan 密钥仅允许用于编程工具，不能用于 "
            "PA_Agent 自动化分析；请使用按量 API 的 sk- 密钥。"
        )


def provider_auth_configured(settings: AIProviderSettings) -> bool:
    """判断当前供应商是否具备可用的认证入口。"""
    from pa_agent.ai.provider_capabilities import resolve_provider_capability

    capability = resolve_provider_capability(settings)
    preset = get_provider_preset(capability.adapter_id)
    if preset is not None and preset.auth_kind == "codex_subscription":
        from pa_agent.ai.codex_subscription_client import codex_login_status

        return codex_login_status().logged_in
    return bool(resolve_provider_runtime_settings(settings).api_key.strip())
