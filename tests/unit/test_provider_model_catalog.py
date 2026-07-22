"""供应商模型目录与模型 ID 规范化测试。"""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from pa_agent.ai.provider_model_catalog import (
    ModelCatalogEntry,
    ModelCatalogError,
    builtin_provider_model_catalog,
    canonicalize_model_id,
    fetch_provider_model_catalog,
    merge_model_catalogs,
)
from pa_agent.config.settings import AIProviderSettings


def _entry(model_id: str) -> ModelCatalogEntry:
    return ModelCatalogEntry(model_id=model_id, display_name=model_id)


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("gpt-5.6-sol", "gpt-5.6-sol"),
        ("GPT-5.6-SOL", "gpt-5.6-sol"),
        ("gpt-5.6 sol", "gpt-5.6-sol"),
        ("gpt 5.6 sol", "gpt-5.6-sol"),
    ),
)
def test_codex_model_aliases_are_canonicalized(raw: str, expected: str) -> None:
    entries = (_entry("gpt-5.6-sol"), _entry("gpt-5.6-terra"))

    assert canonicalize_model_id(raw, entries, strict=True) == expected


def test_unknown_model_is_rejected_when_catalog_is_authoritative() -> None:
    with pytest.raises(ModelCatalogError, match="不在当前目录"):
        canonicalize_model_id(
            "gpt-99",
            (_entry("gpt-5.6-sol"),),
            strict=True,
        )


def test_builtin_catalogs_cover_prioritized_providers() -> None:
    codex = builtin_provider_model_catalog(
        AIProviderSettings(adapter_id="codex_subscription", model="auto")
    )
    deepseek = builtin_provider_model_catalog(
        AIProviderSettings(adapter_id="deepseek", model="deepseek-v4-flash")
    )
    kimi = builtin_provider_model_catalog(
        AIProviderSettings(adapter_id="kimi", model="kimi-k2.6")
    )

    assert {entry.model_id for entry in codex} >= {
        "auto",
        "gpt-5.6-sol",
        "gpt-5.5",
    }
    assert {entry.model_id for entry in deepseek} >= {
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    }
    assert {entry.model_id for entry in kimi} >= {
        "kimi-k3",
        "kimi-k2.6",
        "moonshot-v1-128k",
    }


def test_live_catalog_is_authoritative_and_keeps_matching_builtin_capabilities() -> None:
    base = (
        ModelCatalogEntry(
            model_id="kimi-k2.6",
            display_name="Kimi K2.6",
            context_window=262_144,
            supports_thinking_on=True,
            supports_thinking_off=True,
        ),
        ModelCatalogEntry(
            model_id="kimi-old",
            display_name="Kimi Old",
        ),
    )
    live = (
        ModelCatalogEntry(
            model_id="kimi-k2.6",
            display_name="kimi-k2.6",
        ),
        ModelCatalogEntry(
            model_id="kimi-new",
            display_name="kimi-new",
        ),
    )

    merged = merge_model_catalogs(base, live)

    known = next(entry for entry in merged if entry.model_id == "kimi-k2.6")
    assert known.display_name == "Kimi K2.6"
    assert known.context_window == 262_144
    assert known.supports_thinking_off is True
    assert any(entry.model_id == "kimi-new" for entry in merged)
    assert {entry.model_id for entry in merged} == {"kimi-k2.6", "kimi-new"}


def test_empty_live_catalog_does_not_reintroduce_builtin_models() -> None:
    assert merge_model_catalogs((_entry("deepseek-chat"),), ()) == ()


def test_codex_catalog_uses_official_cli_metadata() -> None:
    payload = {
        "models": [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6 Sol",
                "context_window": 372000,
                "default_reasoning_level": "high",
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "high"},
                    {"effort": "ultra"},
                ],
                "additional_speed_tiers": ["fast"],
                "visibility": "list",
            }
        ]
    }
    completed = MagicMock(
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )
    provider = AIProviderSettings(
        model="gpt-5.6 sol",
        adapter_id="codex_subscription",
    )

    with (
        patch(
            "pa_agent.ai.provider_model_catalog._codex_executable",
            return_value="codex.exe",
        ),
        patch(
            "pa_agent.ai.provider_model_catalog.subprocess.run",
            return_value=completed,
        ) as run,
    ):
        entries = fetch_provider_model_catalog(provider)

    assert run.call_args.args[0] == ["codex.exe", "debug", "models"]
    selected = next(item for item in entries if item.model_id == "gpt-5.6-sol")
    assert selected.context_window == 372000
    assert selected.supported_efforts == ("low", "high", "ultra")
    assert selected.default_effort == "high"
    assert selected.service_tiers == ("fast",)
    assert selected.speed_mode == "service_tier"


def test_kimi_catalog_reads_context_reasoning_and_model_speed_variant() -> None:
    payload = {
        "data": [
            {
                "id": "kimi-k2.6",
                "context_length": 262144,
                "supports_reasoning": True,
            },
            {
                "id": "kimi-k2-turbo-preview",
                "context_length": 131072,
                "supports_reasoning": False,
            },
        ]
    }
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    provider = AIProviderSettings(
        model="kimi-k2.6",
        base_url="https://api.moonshot.cn/v1",
        api_key="secret-for-test",
        adapter_id="kimi",
    )

    with patch(
        "pa_agent.ai.provider_model_catalog.urllib.request.urlopen",
        return_value=response,
    ):
        entries = fetch_provider_model_catalog(provider)

    standard = next(item for item in entries if item.model_id == "kimi-k2.6")
    turbo = next(
        item for item in entries if item.model_id == "kimi-k2-turbo-preview"
    )
    assert standard.context_window == 262144
    assert standard.supports_thinking_on is True
    assert standard.supports_thinking_off is True
    assert standard.speed_mode == "model_variant"
    assert turbo.supports_thinking_on is False
    assert turbo.supports_thinking_off is True
    assert "高速" in turbo.speed_description


def test_catalog_auth_failure_never_exposes_api_key() -> None:
    secret = "never-show-this-secret"
    provider = AIProviderSettings(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key=secret,
        adapter_id="deepseek",
    )
    http_error = urllib.error.HTTPError(
        url="https://api.deepseek.com/models",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=None,
    )

    with (
        patch(
            "pa_agent.ai.provider_model_catalog.urllib.request.urlopen",
            side_effect=http_error,
        ),
        pytest.raises(ModelCatalogError) as caught,
    ):
        fetch_provider_model_catalog(provider)

    assert "认证失败" in str(caught.value)
    assert secret not in str(caught.value)
