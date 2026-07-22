"""供应商模型目录。

API 供应商使用各自已认证的 ``GET /models``；Codex 订阅只调用本机官方
Codex CLI 的内置目录。返回值不包含凭据，也不记录供应商的原始错误正文。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from pa_agent.ai.codex_subscription_client import (
    _codex_executable,
    _sanitized_codex_environment,
)
from pa_agent.ai.provider_capabilities import (
    fixed_model_context_window,
    resolve_provider_capability,
)
from pa_agent.ai.provider_registry import resolve_provider_runtime_settings
from pa_agent.config.settings import AIProviderSettings

SpeedMode = Literal["service_tier", "model_variant", "fixed", "unknown"]
_MAX_CATALOG_BYTES = 2 * 1024 * 1024
_MODEL_ALIAS_TOKEN = re.compile(r"[^a-z0-9]+")


class ModelCatalogError(RuntimeError):
    """可安全展示给用户的模型目录错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ModelCatalogEntry:
    """一个模型及其可可靠确认的能力。"""

    model_id: str
    display_name: str
    context_window: int | None = None
    supports_thinking_on: bool | None = None
    supports_thinking_off: bool | None = None
    supported_efforts: tuple[str, ...] = ()
    default_effort: str = ""
    service_tiers: tuple[str, ...] = ()
    speed_mode: SpeedMode = "unknown"
    speed_description: str = ""


_BUILTIN_CATALOGS: dict[str, tuple[ModelCatalogEntry, ...]] = {
    "codex_subscription": (
        ModelCatalogEntry(
            model_id="auto",
            display_name="由 Codex 自动选择",
            supports_thinking_on=True,
            supports_thinking_off=False,
            supported_efforts=("low", "medium", "high", "xhigh"),
            default_effort="high",
            speed_description="模型和速度由当前 Codex 订阅能力决定。",
        ),
        ModelCatalogEntry(
            model_id="gpt-5.6-sol",
            display_name="GPT-5.6 Sol",
            context_window=272_000,
            supports_thinking_on=True,
            supports_thinking_off=False,
            supported_efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
            default_effort="low",
            service_tiers=("fast",),
            speed_mode="service_tier",
            speed_description="可选择标准或 Fast 服务线路。",
        ),
        ModelCatalogEntry(
            model_id="gpt-5.6-terra",
            display_name="GPT-5.6 Terra",
            context_window=272_000,
            supports_thinking_on=True,
            supports_thinking_off=False,
            supported_efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
            default_effort="medium",
            service_tiers=("fast",),
            speed_mode="service_tier",
            speed_description="可选择标准或 Fast 服务线路。",
        ),
        ModelCatalogEntry(
            model_id="gpt-5.6-luna",
            display_name="GPT-5.6 Luna",
            context_window=272_000,
            supports_thinking_on=True,
            supports_thinking_off=False,
            supported_efforts=("low", "medium", "high", "xhigh", "max"),
            default_effort="medium",
            service_tiers=("fast",),
            speed_mode="service_tier",
            speed_description="可选择标准或 Fast 服务线路。",
        ),
        ModelCatalogEntry(
            model_id="gpt-5.5",
            display_name="GPT-5.5",
            context_window=272_000,
            supports_thinking_on=True,
            supports_thinking_off=False,
            supported_efforts=("low", "medium", "high", "xhigh"),
            default_effort="medium",
            service_tiers=("fast",),
            speed_mode="service_tier",
            speed_description="可选择标准或 Fast 服务线路。",
        ),
        ModelCatalogEntry(
            model_id="gpt-5.4",
            display_name="GPT-5.4",
            context_window=272_000,
            supports_thinking_on=True,
            supports_thinking_off=False,
            supported_efforts=("low", "medium", "high", "xhigh"),
            default_effort="medium",
            service_tiers=("fast",),
            speed_mode="service_tier",
            speed_description="可选择标准或 Fast 服务线路。",
        ),
        ModelCatalogEntry(
            model_id="gpt-5.4-mini",
            display_name="GPT-5.4 Mini",
            context_window=272_000,
            supports_thinking_on=True,
            supports_thinking_off=False,
            supported_efforts=("low", "medium", "high", "xhigh"),
            default_effort="medium",
            speed_mode="fixed",
            speed_description="该模型没有可切换的服务速度档位。",
        ),
        ModelCatalogEntry(
            model_id="gpt-5.2",
            display_name="GPT-5.2",
            context_window=272_000,
            supports_thinking_on=True,
            supports_thinking_off=False,
            supported_efforts=("low", "medium", "high", "xhigh"),
            default_effort="medium",
            speed_mode="fixed",
            speed_description="该模型没有可切换的服务速度档位。",
        ),
    ),
    "deepseek": (
        ModelCatalogEntry(
            model_id="deepseek-v4-pro",
            display_name="DeepSeek V4 Pro",
            context_window=1_000_000,
            supports_thinking_on=True,
            supports_thinking_off=True,
            speed_mode="fixed",
            speed_description="DeepSeek 当前没有公布可独立切换的速度档位。",
        ),
        ModelCatalogEntry(
            model_id="deepseek-v4-flash",
            display_name="DeepSeek V4 Flash",
            context_window=1_000_000,
            supports_thinking_on=True,
            supports_thinking_off=True,
            speed_mode="fixed",
            speed_description="DeepSeek 当前没有公布可独立切换的速度档位。",
        ),
        ModelCatalogEntry(
            model_id="deepseek-chat",
            display_name="DeepSeek Chat（旧版）",
            context_window=1_000_000,
            speed_mode="fixed",
            speed_description="DeepSeek 当前没有公布可独立切换的速度档位。",
        ),
        ModelCatalogEntry(
            model_id="deepseek-reasoner",
            display_name="DeepSeek Reasoner（旧版）",
            context_window=1_000_000,
            speed_mode="fixed",
            speed_description="DeepSeek 当前没有公布可独立切换的速度档位。",
        ),
    ),
    "kimi": (
        ModelCatalogEntry(
            model_id="kimi-k3",
            display_name="Kimi K3",
            context_window=1_048_576,
            supports_thinking_on=True,
            supports_thinking_off=False,
            supported_efforts=("max",),
            default_effort="max",
        ),
        ModelCatalogEntry(
            model_id="kimi-k2.6",
            display_name="Kimi K2.6",
            context_window=262_144,
            supports_thinking_on=True,
            supports_thinking_off=True,
            speed_mode="model_variant",
            speed_description="Kimi 的高速能力通过具体模型 ID 选择。",
        ),
        ModelCatalogEntry(
            model_id="kimi-k2.7-code",
            display_name="Kimi K2.7 Code",
            context_window=262_144,
            supports_thinking_on=True,
            supports_thinking_off=False,
            speed_mode="model_variant",
            speed_description="Kimi 的高速能力通过具体模型 ID 选择。",
        ),
        ModelCatalogEntry(
            model_id="kimi-k2.5",
            display_name="Kimi K2.5",
            context_window=262_144,
            supports_thinking_on=True,
            supports_thinking_off=True,
            speed_mode="model_variant",
            speed_description="Kimi 的高速能力通过具体模型 ID 选择。",
        ),
        ModelCatalogEntry(
            model_id="moonshot-v1-128k",
            display_name="Moonshot V1 128K",
            context_window=131_072,
            supports_thinking_on=False,
            supports_thinking_off=True,
            speed_mode="fixed",
        ),
        ModelCatalogEntry(
            model_id="moonshot-v1-32k",
            display_name="Moonshot V1 32K",
            context_window=32_768,
            supports_thinking_on=False,
            supports_thinking_off=True,
            speed_mode="fixed",
        ),
        ModelCatalogEntry(
            model_id="moonshot-v1-8k",
            display_name="Moonshot V1 8K",
            context_window=8_192,
            supports_thinking_on=False,
            supports_thinking_off=True,
            speed_mode="fixed",
        ),
    ),
}


def builtin_provider_model_catalog(
    provider: AIProviderSettings,
) -> tuple[ModelCatalogEntry, ...]:
    """返回无需联网即可选择的基础目录；动态结果仍是账号可见性的真值。"""

    capability = resolve_provider_capability(provider)
    return _BUILTIN_CATALOGS.get(capability.adapter_id, ())


def merge_model_catalogs(
    base_entries: tuple[ModelCatalogEntry, ...],
    live_entries: tuple[ModelCatalogEntry, ...],
) -> tuple[ModelCatalogEntry, ...]:
    """以账号实时目录为可选真值，并用基础目录补齐同 ID 的能力元数据。"""

    base_by_id = {
        entry.model_id.casefold(): entry
        for entry in base_entries
    }
    merged: dict[str, ModelCatalogEntry] = {}
    order: list[str] = []
    for live in live_entries:
        key = live.model_id.casefold()
        if key in merged:
            continue
        base = base_by_id.get(key)
        if base is None:
            merged[key] = live
            order.append(key)
            continue
        order.append(key)
        merged[key] = ModelCatalogEntry(
            model_id=live.model_id,
            display_name=(
                base.display_name
                if live.display_name == live.model_id and base.display_name
                else live.display_name
            ),
            context_window=live.context_window or base.context_window,
            supports_thinking_on=(
                live.supports_thinking_on
                if live.supports_thinking_on is not None
                else base.supports_thinking_on
            ),
            supports_thinking_off=(
                live.supports_thinking_off
                if live.supports_thinking_off is not None
                else base.supports_thinking_off
            ),
            supported_efforts=live.supported_efforts or base.supported_efforts,
            default_effort=live.default_effort or base.default_effort,
            service_tiers=live.service_tiers or base.service_tiers,
            speed_mode=(
                live.speed_mode
                if live.speed_mode != "unknown"
                else base.speed_mode
            ),
            speed_description=(
                live.speed_description or base.speed_description
            ),
        )
    return tuple(merged[key] for key in order)


def _safe_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _alias_token(value: str) -> str:
    return _MODEL_ALIAS_TOKEN.sub("", str(value or "").strip().casefold())


def canonicalize_model_id(
    raw_model_id: str,
    entries: tuple[ModelCatalogEntry, ...],
    *,
    strict: bool,
) -> str:
    """把唯一可判定的空格/连字符别名改为官方模型 ID。"""

    value = str(raw_model_id or "").strip()
    if not value:
        raise ModelCatalogError("model_blank", "请先选择或填写模型 ID。")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ModelCatalogError("model_invalid", "模型 ID 含有不可见控制字符。")

    exact = {
        entry.model_id.casefold(): entry.model_id
        for entry in entries
    }
    if value.casefold() in exact:
        return exact[value.casefold()]

    token = _alias_token(value)
    aliases = {
        entry.model_id
        for entry in entries
        if _alias_token(entry.model_id) == token
    }
    if len(aliases) == 1:
        return next(iter(aliases))
    if strict:
        raise ModelCatalogError(
            "model_unavailable",
            "该模型不在当前目录中，请点击“刷新模型”后选择；"
            "只有供应商明确提供但目录尚未收录时，才开启手动填写。",
        )
    if any(char.isspace() for char in value):
        raise ModelCatalogError(
            "model_invalid",
            "模型 ID 中不能包含空格；请从已拉取的模型列表中选择。",
        )
    return value


def _codex_entries(timeout_s: float) -> tuple[ModelCatalogEntry, ...]:
    executable = _codex_executable()
    if not executable:
        raise ModelCatalogError("codex_missing", "未安装官方 Codex CLI。")
    try:
        completed = subprocess.run(
            [executable, "debug", "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=_sanitized_codex_environment(),
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise ModelCatalogError("timeout", "读取 Codex 模型列表超时。") from exc
    except OSError as exc:
        raise ModelCatalogError("codex_unavailable", "无法启动官方 Codex CLI。") from exc
    if completed.returncode != 0:
        raise ModelCatalogError("catalog_failed", "Codex CLI 未返回可用模型列表。")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ModelCatalogError("catalog_invalid", "Codex 模型列表格式无效。") from exc

    entries: list[ModelCatalogEntry] = [
        ModelCatalogEntry(
            model_id="auto",
            display_name="由 Codex 自动选择",
            supports_thinking_on=True,
            supports_thinking_off=False,
            supported_efforts=("low", "medium", "high", "xhigh"),
            speed_mode="unknown",
            speed_description="自动模式的模型与速度由 Codex 当前订阅能力决定。",
        )
    ]
    for item in payload.get("models", []):
        if not isinstance(item, dict) or item.get("visibility") not in (None, "list"):
            continue
        model_id = str(item.get("slug") or "").strip()
        if not model_id:
            continue
        efforts = tuple(
            str(level.get("effort") or "").strip()
            for level in item.get("supported_reasoning_levels", [])
            if isinstance(level, dict) and str(level.get("effort") or "").strip()
        )
        speed_tiers = tuple(
            str(tier or "").strip()
            for tier in item.get("additional_speed_tiers", [])
            if str(tier or "").strip()
        )
        entries.append(
            ModelCatalogEntry(
                model_id=model_id,
                display_name=str(item.get("display_name") or model_id).strip(),
                context_window=_safe_positive_int(item.get("context_window")),
                supports_thinking_on=bool(efforts),
                supports_thinking_off=False,
                supported_efforts=efforts,
                default_effort=str(item.get("default_reasoning_level") or "").strip(),
                service_tiers=speed_tiers,
                speed_mode="service_tier" if speed_tiers else "fixed",
                speed_description=(
                    "可选择标准或 Fast 服务线路；Fast 会提高速度，也会增加订阅用量。"
                    if speed_tiers
                    else "该模型未公布可切换的服务速度档位。"
                ),
            )
        )
    if not entries:
        raise ModelCatalogError("catalog_empty", "Codex 当前没有返回可选择的模型。")
    return tuple(entries)


def _api_catalog_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ModelCatalogError("base_url_invalid", "Base URL 不是有效的 HTTP 地址。")
    return f"{base}/models"


def _api_entries(
    provider: AIProviderSettings,
    *,
    timeout_s: float,
) -> tuple[ModelCatalogEntry, ...]:
    runtime = resolve_provider_runtime_settings(provider)
    if not runtime.api_key.strip():
        raise ModelCatalogError("auth_missing", "请先配置该供应商的 API Key。")
    capability = resolve_provider_capability(runtime)
    request = urllib.request.Request(
        _api_catalog_url(runtime.base_url),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {runtime.api_key}",
            "User-Agent": "PA-Agent/0.1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read(_MAX_CATALOG_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            message = "模型列表认证失败，请检查 API Key 与账号权限。"
            code = "authentication_failed"
        elif exc.code == 429:
            message = "模型列表请求受到限流，请稍后重试。"
            code = "rate_limited"
        else:
            message = f"供应商模型列表请求失败（HTTP {exc.code}）。"
            code = "http_error"
        raise ModelCatalogError(code, message) from exc
    except TimeoutError as exc:
        raise ModelCatalogError("timeout", "读取供应商模型列表超时。") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise ModelCatalogError("connection_failed", "无法连接供应商的模型列表接口。") from exc
    if len(raw) > _MAX_CATALOG_BYTES:
        raise ModelCatalogError("catalog_too_large", "供应商模型列表异常过大，已停止读取。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelCatalogError("catalog_invalid", "供应商模型列表格式无效。") from exc

    data = payload.get("data")
    if not isinstance(data, list):
        raise ModelCatalogError("catalog_invalid", "供应商模型列表缺少 data 数组。")
    by_id: dict[str, ModelCatalogEntry] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        context_window = _safe_positive_int(item.get("context_length"))
        if context_window is None:
            probe = runtime.model_copy(update={"model": model_id})
            context_window = fixed_model_context_window(probe)

        supports_reasoning = item.get("supports_reasoning")
        thinking_on: bool | None = (
            supports_reasoning if isinstance(supports_reasoning, bool) else None
        )
        thinking_off: bool | None = None
        speed_mode: SpeedMode = "unknown"
        speed_description = "供应商没有公布可独立切换的速度参数。"
        lower = model_id.casefold()
        if capability.adapter_id == "kimi":
            if lower.startswith(("kimi-k2.5", "kimi-k2.6")):
                thinking_on, thinking_off = True, True
            elif lower.startswith(("kimi-k2-thinking", "kimi-k2.7-code", "kimi-k3")):
                thinking_on, thinking_off = True, False
            elif lower.startswith(("kimi-k2-", "moonshot-v1-")):
                thinking_on, thinking_off = False, True
            speed_mode = "model_variant"
            speed_description = (
                "这是高速模型版本；Kimi 的速度通过模型 ID 选择。"
                if "turbo" in lower
                else "Kimi 的高速能力通过带 turbo 的模型 ID 选择。"
            )
        elif capability.adapter_id == "deepseek":
            speed_mode = "fixed"
            speed_description = "DeepSeek 当前没有公布可独立切换的速度档位。"
        by_id[model_id] = ModelCatalogEntry(
            model_id=model_id,
            display_name=model_id,
            context_window=context_window,
            supports_thinking_on=thinking_on,
            supports_thinking_off=thinking_off,
            supported_efforts=(),
            speed_mode=speed_mode,
            speed_description=speed_description,
        )
    if not by_id:
        raise ModelCatalogError("catalog_empty", "当前账号没有返回可选择的模型。")
    return tuple(sorted(by_id.values(), key=lambda entry: entry.model_id.casefold()))


def fetch_provider_model_catalog(
    provider: AIProviderSettings,
    *,
    timeout_s: float = 10.0,
) -> tuple[ModelCatalogEntry, ...]:
    """读取当前档案可用模型；所有异常均为不含凭据的 ``ModelCatalogError``。"""

    capability = resolve_provider_capability(provider)
    if capability.client_kind == "codex_cli":
        return _codex_entries(timeout_s)
    if capability.adapter_id not in {"deepseek", "kimi"}:
        raise ModelCatalogError(
            "catalog_unsupported",
            "当前适配器尚未提供可靠的自动模型列表，请使用供应商公布的精确模型 ID。",
        )
    return _api_entries(provider, timeout_s=timeout_s)
