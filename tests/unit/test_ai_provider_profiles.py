"""Unit tests for multi-profile AI settings persistence and activation."""

from __future__ import annotations

import json

import pytest

from pa_agent.config.settings import (
    AIProviderSettings,
    Settings,
    load_settings,
    save_settings,
)


def _passed_checks() -> dict[str, bool]:
    return {
        "connection_auth": True,
        "parameter_acceptance": True,
        "response_observed": True,
        "challenge_matched": True,
    }


def test_legacy_single_provider_is_migrated_without_value_changes(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "provider": {
                    "model": "legacy-model",
                    "base_url": "https://legacy.example/v1",
                    "api_key": "dummy",
                    "thinking": False,
                    "reasoning_effort": "low",
                    "context_window": 123_456,
                }
            }
        ),
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.active_ai_profile_id == "default"
    profile = settings.ai_profiles["default"]
    assert profile.verification.status == "untested"
    assert profile.provider.model == "legacy-model"
    assert profile.provider.base_url == "https://legacy.example/v1"
    assert settings.provider == profile.provider

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["active_ai_profile_id"] == "default"
    assert persisted["ai_profiles"]["default"]["provider"]["model"] == "legacy-model"
    assert persisted["provider"]["model"] == "legacy-model"


def test_multiple_verified_profiles_round_trip_and_activate(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    settings.save_ai_profile(
        "research",
        "研究模型",
        AIProviderSettings(
            model="gpt-5.6",
            base_url="https://api.openai.com/v1",
            api_key="test-key-2",
            adapter_id="openai",
            reasoning_effort="medium",
        ),
    )
    verification = settings.mark_ai_profile_verification(
        "research",
        passed=True,
        tested_at="2026-07-15T10:00:00+08:00",
        adapter_id="openai",
        checks=_passed_checks(),
    )
    assert verification.status == "passed"

    activated = settings.activate_ai_profile("research")
    assert activated.model == "gpt-5.6"
    save_settings(settings, path)

    loaded = load_settings(path)
    assert loaded.active_ai_profile_id == "research"
    assert set(loaded.ai_profiles) == {"default", "research"}
    assert loaded.provider.model == "gpt-5.6"
    assert loaded.ai_profiles["research"].verification.is_current_for(
        loaded.ai_profiles["research"].provider
    )


def test_activation_rejects_untested_profile_by_default() -> None:
    settings = Settings()
    settings.save_ai_profile("draft", "草稿", AIProviderSettings(model="draft-model"))

    with pytest.raises(ValueError, match="not verified"):
        settings.activate_ai_profile("draft")

    assert settings.active_ai_profile_id == "default"
    assert settings.provider.model == "deepseek-v4-flash"


def test_activation_can_explicitly_allow_legacy_unverified_profile() -> None:
    settings = Settings()
    settings.save_ai_profile("legacy", "历史配置", AIProviderSettings(model="legacy-model"))

    activated = settings.activate_ai_profile("legacy", require_verified=False)

    assert activated.model == "legacy-model"
    assert settings.active_ai_profile_id == "legacy"


def test_provider_change_invalidates_verification_on_save(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    settings.mark_ai_profile_verification(
        "default",
        passed=True,
        tested_at="2026-07-15T10:00:00+08:00",
        adapter_id="deepseek",
        checks=_passed_checks(),
    )
    assert settings.ai_profiles["default"].verification.status == "passed"

    settings.provider.model = "deepseek-v4-pro"
    save_settings(settings, path)

    loaded = load_settings(path)
    verification = loaded.ai_profiles["default"].verification
    assert verification.status == "untested"
    assert verification.config_fingerprint == ""
    assert loaded.provider.model == "deepseek-v4-pro"


def test_unknown_profile_activation_fails_without_changing_active_profile() -> None:
    settings = Settings()
    with pytest.raises(KeyError, match="unknown AI profile"):
        settings.activate_ai_profile("missing")
    assert settings.active_ai_profile_id == "default"


def test_verification_adapter_must_match_resolved_profile_adapter() -> None:
    settings = Settings()
    with pytest.raises(ValueError, match="verification adapter mismatch"):
        settings.mark_ai_profile_verification(
            "default",
            passed=True,
            tested_at="2026-07-15T10:00:00+08:00",
            adapter_id="openai",
        )


def test_old_capability_schema_invalidates_passed_verification() -> None:
    settings = Settings()
    settings.mark_ai_profile_verification(
        "default",
        passed=True,
        tested_at="2026-07-15T10:00:00+08:00",
        adapter_id="deepseek",
        checks=_passed_checks(),
    )
    raw = settings.model_dump()
    raw["ai_profiles"]["default"]["verification"]["capability_schema_version"] = 0

    loaded = Settings.model_validate(raw)

    assert loaded.ai_profiles["default"].verification.status == "untested"


def test_replacing_active_profile_keeps_legacy_provider_mirror_in_sync(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()

    settings.save_ai_profile(
        "default",
        "新默认配置",
        AIProviderSettings(model="replacement-model", adapter_id="openai"),
        replace=True,
    )
    save_settings(settings, path)

    loaded = load_settings(path)
    assert loaded.provider.model == "replacement-model"
    assert loaded.ai_profiles["default"].provider.model == "replacement-model"


def test_verification_error_redacts_profile_secrets() -> None:
    settings = Settings()
    settings.provider.api_key = "sk-test-secret"
    settings.provider.api_key_encrypted = "encrypted-test-secret"

    verification = settings.mark_ai_profile_verification(
        "default",
        passed=False,
        tested_at="2026-07-15T10:00:00+08:00",
        adapter_id="deepseek",
        error="failed sk-test-secret and encrypted-test-secret",
    )

    assert verification.error == "failed *** and ***"
    assert "secret" not in verification.error


def test_passed_verification_requires_successful_checks() -> None:
    settings = Settings()

    with pytest.raises(ValueError, match="mandatory provider checks"):
        settings.mark_ai_profile_verification(
            "default",
            passed=True,
            tested_at="2026-07-15T10:00:00+08:00",
            adapter_id="deepseek",
            checks={"stream": False},
        )


def test_atomic_save_failure_preserves_existing_settings_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "settings.json"
    original = Settings()
    original.provider.model = "original-model"
    save_settings(original, path)
    original_payload = path.read_text(encoding="utf-8")
    changed = original.model_copy(deep=True)
    changed.provider.model = "changed-model"

    def _fail_replace(_source, _target) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("pa_agent.config.settings.os.replace", _fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_settings(changed, path)

    assert path.read_text(encoding="utf-8") == original_payload
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []
