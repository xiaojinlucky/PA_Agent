"""Unit tests for settings load/save round-trip (task 2.4)."""

from __future__ import annotations

import json
from decimal import Decimal
from threading import Event, Thread
from unittest.mock import patch

import pytest

import pa_agent.config.settings as settings_module
from pa_agent.config.settings import (
    AIProviderSettings,
    MarketWorkspacePersistenceBaseline,
    MarketWorkspaceSettings,
    Settings,
    SettingsConflictError,
    load_settings,
    save_ai_profile_activation,
    save_market_workspace_settings,
    save_settings,
)


def test_defaults(tmp_path):
    """load_settings on a missing file returns defaults and creates the file."""
    p = tmp_path / "settings.json"
    s = load_settings(p)
    assert s.provider.model == "deepseek-v4-flash"
    assert s.provider.base_url == "https://api.deepseek.com"
    assert s.provider.thinking is True
    assert s.provider.reasoning_effort == "high"
    assert s.provider.context_window == 1_000_000
    assert s.general.analysis_bar_count == 100
    assert s.general.last_symbol == "XAUUSDm"
    assert s.general.last_timeframe == "15m"
    assert s.general.decision_stance == "balanced"
    assert s.general.decision_flow_auto_play is True
    assert s.general.auto_resume_chart_after_analysis is False
    assert p.exists(), "defaults should be written to disk"


def test_round_trip(tmp_path):
    """save → load preserves all fields."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.provider.api_key = "sk-test-1234"
    original.general.last_symbol = "BTCUSDT"
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.provider.api_key == "sk-test-1234"
    # Crypto symbols migrate to gold defaults on load
    assert loaded.general.last_symbol == "XAUUSDm"
    assert loaded.provider.model == original.provider.model


def test_load_persists_missing_order_mode_and_atr_fields(tmp_path):
    p = tmp_path / "settings.json"
    raw = Settings().model_dump(mode="json")
    for field in (
        "entry_order_mode",
        "exit_order_mode",
        "entry_slippage_atr_multiple",
        "exit_slippage_atr_multiple",
    ):
        raw["execution"].pop(field)
    p.write_text(json.dumps(raw), encoding="utf-8")

    loaded = load_settings(p)
    persisted = json.loads(p.read_text(encoding="utf-8"))

    assert loaded.execution.entry_order_mode == "signal"
    assert loaded.execution.exit_order_mode == "market"
    assert loaded.execution.entry_slippage_atr_multiple == 0.5
    assert loaded.execution.exit_slippage_atr_multiple == 0.5
    assert persisted["execution"]["entry_order_mode"] == "signal"
    assert persisted["execution"]["exit_order_mode"] == "market"
    assert persisted["execution"]["entry_slippage_atr_multiple"] == "0.50"
    assert persisted["execution"]["exit_slippage_atr_multiple"] == "0.50"


def test_longbridge_source_and_per_source_symbols_round_trip(tmp_path):
    p = tmp_path / "settings.json"
    original = Settings()
    original.general.last_data_source = "longbridge"
    original.general.last_symbol = "AAPL.US"
    original.general.last_symbols_by_source = {
        "mt5": "XAUUSD",
        "longbridge": "AAPL.US",
    }

    save_settings(original, p)
    loaded = load_settings(p)

    assert loaded.general.last_data_source == "longbridge"
    assert loaded.general.last_symbol == "AAPL.US"
    assert loaded.general.last_symbols_by_source["mt5"] == "XAUUSD"
    assert loaded.general.last_symbols_by_source["longbridge"] == "AAPL.US"


def test_market_workspace_settings_round_trip_is_independent_from_legacy_page(
    tmp_path,
) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    settings.market_workspace = MarketWorkspaceSettings(
        selected_market="HK",
        last_symbols_by_market={
            "US": "MSFT.US",
            "HK": "700.HK",
            "CN": "600519.SH",
            "Crypto": "BTC-USDT",
        },
        display_timeframes_by_market={
            "US": "1h",
            "HK": "10m",
            "CN": "4h",
            "Crypto": "10m",
        },
        watchlists_by_market={
            "US": ["MSFT.US", "AAPL.US"],
            "HK": ["700.HK"],
            "CN": [],
            "Crypto": ["BTC-USDT"],
        },
    )
    settings.general.last_data_source = "mt5"
    settings.general.last_symbol = "XAUUSDm"

    save_settings(settings, path)
    loaded = load_settings(path)

    assert loaded.market_workspace == settings.market_workspace
    assert loaded.general.last_data_source == "mt5"
    assert loaded.general.last_symbol == "XAUUSDm"


def test_market_workspace_settings_reject_unknown_market_duplicate_or_bad_timeframe() -> None:
    with pytest.raises(ValueError):
        MarketWorkspaceSettings(
            watchlists_by_market={
                "US": ["AAPL.US", "aapl.us"],
                "HK": ["700.HK"],
                "CN": ["600519.SH"],
                "Crypto": ["BTC-USDT"],
            }
        )
    with pytest.raises(ValueError):
        MarketWorkspaceSettings(
            display_timeframes_by_market={
                "US": "5m",
                "HK": "10m",
                "CN": "10m",
                "Crypto": "10m",
            }
        )
    with pytest.raises(ValueError):
        MarketWorkspaceSettings(
            last_symbols_by_market={
                "US": "AAPL.US",
                "HK": "700.HK",
                "CN": "600519.SH",
                "Crypto": "BTC-USDT",
                "OTHER": "UNKNOWN",
            }
        )
    with pytest.raises(ValueError, match="US"):
        MarketWorkspaceSettings(
            last_symbols_by_market={
                "US": "700.HK",
                "HK": "700.HK",
                "CN": "600519.SH",
                "Crypto": "BTC-USDT",
            }
        )


def test_market_workspace_save_merges_unrelated_concurrent_change(tmp_path) -> None:
    path = tmp_path / "settings.json"
    original = Settings()
    save_settings(original, path)
    baseline = load_settings(path)

    concurrent = load_settings(path)
    concurrent.general.last_timeframe = "1m"
    save_settings(concurrent, path)

    requested = baseline.market_workspace.model_copy(
        update={
            "selected_market": "HK",
            "last_symbols_by_market": {
                **baseline.market_workspace.last_symbols_by_market,
                "HK": "9988.HK",
            },
        },
        deep=True,
    )
    saved = save_market_workspace_settings(
        MarketWorkspacePersistenceBaseline.from_settings(baseline),
        requested,
        path,
    )
    reloaded = load_settings(path)

    assert saved.market_workspace.selected_market == "HK"
    assert reloaded.market_workspace.last_symbols_by_market["HK"] == "9988.HK"
    assert reloaded.general.last_timeframe == "1m"


def test_market_workspace_save_fails_closed_on_concurrent_workspace_change(
    tmp_path,
) -> None:
    path = tmp_path / "settings.json"
    original = Settings()
    save_settings(original, path)
    stale = load_settings(path)

    current = load_settings(path)
    current.market_workspace = current.market_workspace.model_copy(
        update={"selected_market": "CN"},
        deep=True,
    )
    save_settings(current, path)

    requested = stale.market_workspace.model_copy(
        update={"selected_market": "HK"},
        deep=True,
    )
    with pytest.raises(SettingsConflictError, match="多市场"):
        save_market_workspace_settings(
            MarketWorkspacePersistenceBaseline.from_settings(stale),
            requested,
            path,
        )

    assert load_settings(path).market_workspace.selected_market == "CN"


def test_market_workspace_save_rejects_regressed_disk_revision(tmp_path) -> None:
    path = tmp_path / "settings.json"
    save_settings(Settings(), path)
    baseline = load_settings(path)
    regressed = baseline.model_copy(deep=True)
    regressed.revision = baseline.revision - 1
    path.write_text(
        json.dumps(regressed.model_dump(mode="json")),
        encoding="utf-8",
    )

    with pytest.raises(SettingsConflictError, match="revision"):
        save_market_workspace_settings(
            MarketWorkspacePersistenceBaseline.from_settings(baseline),
            baseline.market_workspace,
            path,
        )


def test_active_source_symbol_map_drives_startup_symbol(tmp_path):
    p = tmp_path / "settings.json"
    raw = Settings().model_dump(mode="json")
    raw["general"]["last_data_source"] = "longbridge"
    raw["general"]["last_symbol"] = "XAUUSD"
    raw["general"]["last_symbols_by_source"] = {"longbridge": "gld.us"}
    p.write_text(json.dumps(raw), encoding="utf-8")

    loaded = load_settings(p)

    assert loaded.general.last_symbol == "GLD.US"
    assert loaded.general.last_symbols_by_source["longbridge"] == "GLD.US"


def test_api_key_present_on_disk(tmp_path):
    """The saved JSON contains the plaintext API key."""
    p = tmp_path / "settings.json"
    s = Settings()
    s.provider.api_key = "sk-super-secret-key"
    save_settings(s, p)
    raw = p.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["provider"]["api_key"] == "sk-super-secret-key"


def test_corrupt_json_returns_defaults(tmp_path):
    """Corrupt settings.json falls back to defaults without raising."""
    p = tmp_path / "settings.json"
    p.write_text("{not valid json", encoding="utf-8")
    s = load_settings(p)
    assert s.provider.model == "deepseek-v4-flash"


def test_missing_api_key_leaves_api_key_blank(tmp_path):
    """If api_key is absent, api_key stays empty string."""
    p = tmp_path / "settings.json"
    data = Settings().model_dump(mode="json")
    data["provider"].pop("api_key", None)
    data["provider"].pop("api_key_encrypted", None)
    p.write_text(json.dumps(data), encoding="utf-8")
    s = load_settings(p)
    assert s.provider.api_key == ""


def test_feishu_round_trip(tmp_path):
    """save → load preserves feishu settings."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.feishu.webhook_url = "https://example.com/hook"
    original.feishu.secret = "sec"
    original.feishu.app_id = "cli_test"
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.feishu.webhook_url == "https://example.com/hook"
    assert loaded.feishu.secret == "sec"
    assert loaded.feishu.app_id == "cli_test"


def test_pushplus_round_trip(tmp_path):
    """save → load preserves pushplus settings."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.pushplus.token = "pp-test-token"
    original.pushplus.enabled = False
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.pushplus.token == "pp-test-token"
    assert loaded.pushplus.enabled is False


def test_tushare_round_trip(tmp_path):
    """save → load preserves tushare token."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.tushare.token = "ts-test-token"
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.tushare.token == "ts-test-token"


def test_execution_routes_round_trip_without_credentials(tmp_path):
    p = tmp_path / "settings.json"
    original = Settings()
    original.execution.enabled = True
    original.execution.selected_broker = "okx"
    original.execution.okx.source_symbol = "BTCUSD"
    original.execution.okx.instrument = "BTC-USDT-SWAP"
    original.execution.okx.sizing_mode = "fixed_quantity"
    original.execution.okx.quantity = "0.25"
    original.execution.okx.product = "swap"
    original.execution.okx.margin_mode = "isolated"
    original.execution.okx.risk_capital_cap_usdt = "5000"
    original.execution.okx.risk_percent = "0.08"
    original.execution.okx.maximum_leverage = "25"

    save_settings(original, p)
    loaded = load_settings(p)
    raw = json.loads(p.read_text(encoding="utf-8"))

    assert loaded.execution.enabled is True
    assert loaded.execution.selected_broker == "okx"
    assert loaded.execution.okx.instrument == "BTC-USDT-SWAP"
    assert loaded.execution.okx.sizing_mode == "fixed_quantity"
    assert loaded.execution.okx.margin_mode == "isolated"
    assert loaded.execution.okx.risk_capital_cap_usdt == 5000
    assert loaded.execution.okx.risk_percent == Decimal("0.08")
    assert loaded.execution.okx.maximum_leverage == 25
    assert "api_key" not in raw["execution"]["okx"]
    assert "passphrase" not in raw["execution"]["okx"]


def test_longbridge_paper_profile_round_trip(tmp_path):
    p = tmp_path / "settings.json"
    original = Settings()
    original.execution.longbridge.preferred_account = "paper"

    save_settings(original, p)
    loaded = load_settings(p)

    assert loaded.execution.longbridge.preferred_account == "paper"


def test_pushplus_auto_disabled_when_enabled_without_token(tmp_path):
    """load_settings disables pushplus when enabled but token empty."""
    p = tmp_path / "settings.json"
    p.write_text(
        '{"pushplus": {"enabled": true, "token": ""}}',
        encoding="utf-8",
    )
    with patch.dict("os.environ", {}, clear=True):
        loaded = load_settings(p)
    assert loaded.pushplus.enabled is False
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["pushplus"]["enabled"] is False


def test_migrate_legacy_feishu_json(tmp_path):
    """Legacy config/feishu.json is merged into settings.json on load."""
    p = tmp_path / "settings.json"
    legacy = tmp_path / "feishu.json"
    save_settings(Settings(), p)
    legacy.write_text(
        json.dumps(
            {
                "enabled": True,
                "webhook_url": "https://example.com/legacy-hook",
                "secret": "legacy-secret",
                "app_id": "cli_legacy",
                "app_secret": "legacy-app-secret",
                "notify_on_order_only": True,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings(p)
    assert loaded.feishu.webhook_url == "https://example.com/legacy-hook"
    assert loaded.feishu.secret == "legacy-secret"
    assert loaded.feishu.app_id == "cli_legacy"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["feishu"]["webhook_url"] == "https://example.com/legacy-hook"


def test_stale_settings_snapshot_cannot_delete_a_new_ai_profile(tmp_path):
    p = tmp_path / "settings.json"
    original = Settings()
    save_settings(original, p)
    stale = load_settings(p)
    current = load_settings(p)
    current.save_ai_profile(
        "kimi",
        "Kimi API",
        AIProviderSettings(
            model="kimi-k2.6",
            base_url="https://api.moonshot.cn/v1",
            adapter_id="kimi",
        ),
    )
    save_settings(current, p)

    stale.general.last_data_source = "okx"
    with pytest.raises(SettingsConflictError):
        save_settings(stale, p)

    reloaded = load_settings(p)
    assert "kimi" in reloaded.ai_profiles
    assert reloaded.general.last_data_source != "okx"


def test_ai_activation_merge_holds_lock_through_atomic_write(tmp_path):
    """合并读取后，第二个设置写入不能再插入激活写入之前。"""

    path = tmp_path / "settings.json"
    baseline = Settings()
    baseline.save_ai_profile(
        "default",
        "Codex 订阅",
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="gpt-5.6-sol",
        ),
        replace=True,
    )
    baseline.mark_ai_profile_verification(
        "default",
        passed=True,
        tested_at="2026-07-20T00:00:00+00:00",
        adapter_id="codex_subscription",
        checks={
            "connection_auth": True,
            "parameter_acceptance": True,
            "response_observed": True,
            "challenge_matched": True,
        },
    )
    save_settings(baseline, path)

    candidate = baseline.model_copy(deep=True)
    candidate.save_ai_profile(
        "default",
        "Codex 订阅",
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="gpt-5.6-terra",
        ),
        replace=True,
    )
    candidate.provider = candidate.ai_profiles["default"].provider.model_copy(
        deep=True
    )
    candidate.mark_ai_profile_verification(
        "default",
        passed=True,
        tested_at="2026-07-20T01:00:00+00:00",
        adapter_id="codex_subscription",
        checks={
            "connection_auth": True,
            "parameter_acceptance": True,
            "response_observed": True,
            "challenge_matched": True,
        },
    )

    first_concurrent = load_settings(path)
    first_concurrent.general.last_timeframe = "1m"
    save_settings(first_concurrent, path)

    second_concurrent = load_settings(path)
    second_concurrent.general.last_data_source = "okx"
    writer_started = Event()
    writer_finished = Event()
    writer_errors: list[Exception] = []

    def _second_writer() -> None:
        writer_started.set()
        try:
            save_settings(second_concurrent, path)
        except Exception as exc:  # noqa: BLE001
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    real_read = settings_module._read_settings_snapshot
    writer: Thread | None = None

    def _read_while_spawning_writer(target: object) -> Settings:
        nonlocal writer
        snapshot = real_read(target)  # type: ignore[arg-type]
        writer = Thread(target=_second_writer)
        writer.start()
        assert writer_started.wait(timeout=2)
        assert writer_finished.wait(timeout=0.1) is False
        return snapshot

    with patch(
        "pa_agent.config.settings._read_settings_snapshot",
        side_effect=_read_while_spawning_writer,
    ):
        merged = save_ai_profile_activation(baseline, candidate, path)

    assert writer is not None
    writer.join(timeout=2)
    assert writer.is_alive() is False
    assert len(writer_errors) == 1
    assert isinstance(writer_errors[0], SettingsConflictError)

    reloaded = load_settings(path)
    assert merged.provider.model == "gpt-5.6-terra"
    assert reloaded.provider.model == "gpt-5.6-terra"
    assert reloaded.general.last_timeframe == "1m"
    assert reloaded.general.last_data_source != "okx"


def test_failed_atomic_replace_does_not_mutate_caller_settings(tmp_path):
    p = tmp_path / "settings.json"
    settings = Settings()
    before = settings.model_copy(deep=True)

    with (
        patch(
            "pa_agent.config.settings.os.replace",
            side_effect=OSError("simulated disk failure"),
        ),
        pytest.raises(OSError, match="simulated disk failure"),
    ):
        save_settings(settings, p)

    assert settings == before
    assert settings.revision == 0
    assert not p.exists()


def test_codex_subscription_discards_unused_api_fields() -> None:
    provider = AIProviderSettings(
        model="gpt-5.6-sol",
        base_url="https://api.deepseek.com",
        api_key="must-not-survive",
        api_key_encrypted="must-not-survive-either",
        adapter_id="codex_subscription",
    )

    assert provider.base_url == ""
    assert provider.api_key == ""
    assert provider.api_key_encrypted == ""


def test_authoritative_catalog_context_survives_profile_round_trip(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    settings.save_ai_profile(
        "kimi-live",
        "Kimi 实时目录",
        AIProviderSettings(
            model="kimi-k2.6",
            base_url="https://api.moonshot.cn/v1",
            adapter_id="kimi",
            context_window=262_144,
            context_window_source="catalog",
        ),
    )

    save_settings(settings, path)
    loaded = load_settings(path)

    provider = loaded.ai_profiles["kimi-live"].provider
    assert provider.context_window == 262_144
    assert provider.context_window_source == "catalog"


def test_unknown_legacy_context_uses_current_builtin_fallback() -> None:
    settings = Settings(
        provider=AIProviderSettings(
            model="gpt-5.6-sol",
            adapter_id="codex_subscription",
            context_window=372_000,
        )
    )

    assert settings.provider.context_window == 272_000
    assert settings.provider.context_window_source == "builtin"


def test_unknown_model_context_round_trips_as_unknown(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    settings.save_ai_profile(
        "kimi-preview",
        "Kimi Preview",
        AIProviderSettings(
            model="kimi-account-preview",
            base_url="https://api.moonshot.cn/v1",
            adapter_id="kimi",
            context_window=None,
        ),
    )

    save_settings(settings, path)
    loaded = load_settings(path)

    assert loaded.ai_profiles["kimi-preview"].provider.context_window is None
