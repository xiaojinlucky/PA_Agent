"""Pydantic settings models for PA Agent."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pa_agent.ai.provider_capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    REQUIRED_PROVIDER_VERIFICATION_CHECKS,
    resolve_provider_capability,
)

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]
DataSourceKind = Literal[
    "mt5", "tradingview", "longbridge", "akshare", "eastmoney", "tushare"
]
NormalizationMode = Literal["strict", "lenient"]


class AIProviderSettings(BaseModel):
    """AI provider connection and behaviour settings."""
    model_config = ConfigDict(extra="ignore")

    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    api_key_encrypted: str = ""
    #: Explicit request adapter. ``auto`` keeps legacy endpoint/model inference.
    adapter_id: str = "auto"
    thinking: bool = True
    reasoning_effort: Literal[
        "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "high"
    context_window: int = Field(default=2_000_000, ge=1_024, le=100_000_000)

    @field_validator("adapter_id", mode="before")
    @classmethod
    def _normalise_adapter_id(cls, value: object) -> str:
        return str(value or "auto").strip().lower() or "auto"


def provider_config_fingerprint(provider: AIProviderSettings) -> str:
    """Return a stable verification fingerprint without exposing credential text."""
    payload = {
        "model": provider.model,
        "base_url": provider.base_url.rstrip("/"),
        "api_key": provider.api_key,
        "api_key_encrypted": provider.api_key_encrypted,
        "adapter_id": provider.adapter_id,
        "thinking": provider.thinking,
        "reasoning_effort": provider.reasoning_effort,
        "context_window": provider.context_window,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AIProviderVerification(BaseModel):
    """Persisted result of an explicit provider capability probe."""
    model_config = ConfigDict(extra="ignore")

    status: Literal["untested", "passed", "failed"] = "untested"
    tested_at: str = ""
    adapter_id: str = ""
    capability_schema_version: int = CAPABILITY_SCHEMA_VERSION
    config_fingerprint: str = ""
    checks: dict[str, bool] = Field(default_factory=dict)
    observations: dict[str, bool] = Field(default_factory=dict)
    error: str = ""

    def is_current_for(self, provider: AIProviderSettings) -> bool:
        return (
            self.status == "passed"
            and bool(self.tested_at)
            and all(
                self.checks.get(name) is True
                for name in REQUIRED_PROVIDER_VERIFICATION_CHECKS
            )
            and self.capability_schema_version == CAPABILITY_SCHEMA_VERSION
            and self.adapter_id == resolve_provider_capability(provider).adapter_id
            and bool(self.config_fingerprint)
            and self.config_fingerprint == provider_config_fingerprint(provider)
        )

    def invalidate(self) -> None:
        self.status = "untested"
        self.tested_at = ""
        self.adapter_id = ""
        self.capability_schema_version = CAPABILITY_SCHEMA_VERSION
        self.config_fingerprint = ""
        self.checks = {}
        self.observations = {}
        self.error = ""


class AIProviderProfile(BaseModel):
    """Named AI connection profile with independent verification state."""
    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str
    provider: AIProviderSettings = Field(default_factory=AIProviderSettings)
    verification: AIProviderVerification = Field(default_factory=AIProviderVerification)

    @field_validator("id", "display_name", mode="before")
    @classmethod
    def _strip_required_text(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("profile id and display name must not be blank")
        return text

    def invalidate_stale_verification(self) -> bool:
        verification = self.verification
        if verification.status == "untested":
            return False
        if verification.capability_schema_version != CAPABILITY_SCHEMA_VERSION:
            verification.invalidate()
            return True
        if verification.config_fingerprint == provider_config_fingerprint(self.provider):
            return False
        verification.invalidate()
        return True


class PromptSettings(BaseModel):
    """Prompt assembly tuning (accuracy-oriented defaults)."""
    model_config = ConfigDict(extra="ignore")

    #: When True, Stage 2 loads every strategy .txt (legacy/test behaviour).
    stage2_load_full_strategy_library: bool = False
    experience_max_entries: int = Field(default=0, ge=0, le=10)
    experience_max_chars_per_entry: int = Field(default=400, ge=100, le=4000)
    #: Inject pattern判定表 + 速查 brief into Stage 1 user prompt (reduces missed tags).
    stage1_inject_pattern_briefs: bool = True


class ValidationSettings(BaseModel):
    """Post-LLM validation behaviour."""
    model_config = ConfigDict(extra="ignore")

    normalization_mode: NormalizationMode = "lenient"
    #: Stage-1 cross-field checks (gate trace, bar_by_bar, pattern tags). Off by default.
    stage1_coherence_checks: bool = False
    #: Stage-2 trace / diagnosis cross-checks (not order safety). Off by default.
    stage2_coherence_checks: bool = False
    trace_semantic_checks: bool = False
    strict_bar_by_bar_features: bool = False
    #: Allow Stage 1 truncated JSON tail repair before failing syntax validation.
    disable_truncation_repair: bool = False
    #: Re-call API with structured feedback when validation fails (format errors).
    retry_enabled: bool = True
    retry_max: int = Field(default=3, ge=0, le=5)
    #: Max retries for category=c semantic errors (subset only).
    retry_max_semantic: int = Field(default=1, ge=0, le=3)
    retry_stage2: bool = True


class GeneralSettings(BaseModel):
    """UI and data-feed general settings."""
    model_config = ConfigDict(extra="ignore")

    analysis_bar_count: int = Field(default=100, ge=2, le=5000)
    refresh_interval_ms: int = 1000
    context_warning_threshold_pct: float = 80.0
    last_data_source: DataSourceKind = "mt5"
    #: A-share K-line adjust for East Money / Baostock (qfq=前复权)
    kline_adjust: Literal["qfq", "hfq", "none"] = "qfq"
    #: TradingView 交易所；空字符串 =（自动）依次探测预设列表
    last_tradingview_exchange: str = ""
    last_symbol: str = "XAUUSDm"
    #: 各数据源最后使用的品种，避免切源时把不兼容代码带到新数据源。
    last_symbols_by_source: dict[str, str] = Field(default_factory=dict)
    last_timeframe: str = "15m"
    decision_flow_auto_play: bool = True
    decision_flow_play_seconds: int = 50
    #: 阶段二给出限价/突破/市价单时：警报音、弹窗，并自动切到「决策」页（跳过决策树可视化演示）
    alert_on_order_opportunity: bool = True
    incremental_max_new_bars: int = Field(default=10, ge=0, le=500)
    #: 阶段二交易倾向：balanced=默认；conservative/aggressive 逐级调整下单意愿
    decision_stance: DecisionStance = "balanced"
    #: 决策树可视化：在「整图适配」基础上的缩放百分比（100=与适配一致；可任意放大，仅下限 10%）
    decision_flow_default_zoom_pct: int = Field(default=600, ge=10)
    #: 「实时」页思考过程/撰写回答框与追问输入框的等宽字体字号（pt）
    stream_pane_font_pt: int = Field(default=11, ge=8, le=28)
    #: K 线图上 #序号 标签的字号（pt）
    chart_seq_label_font_pt: int = Field(default=11, ge=6, le=24)
    #: 两阶段分析结束后是否自动恢复 K 线图表实时刷新
    auto_resume_chart_after_analysis: bool = False
    #: 持续跟踪分析：有新K线收盘时自动触发新一轮分析
    keep_analysis: bool = False
    #: 重试后取消持续跟踪分析：校验失败触发重试后自动关闭 keep_analysis
    cancel_keep_analysis_on_retry: bool = False
    #: 交易决策置信度门槛：仅当 trade_confidence >= 此值时，才视为有下单机会（弹窗警报并提供决策详情）
    decision_confidence_threshold: int = Field(default=40, ge=0, le=100)
    #: 开启下根K线预期功能；关闭时不向模型请求该预测，节省 token
    enable_next_bar_prediction: bool = False
    #: 同一结构位 entry 相差≤3跳时，禁止反向新方案的冷却 K 线根数（已收盘）
    structure_flip_cooldown_bars: int = Field(default=3, ge=1, le=50)

    @field_validator("last_data_source", mode="before")
    @classmethod
    def _coerce_legacy_data_source(cls, v: object) -> object:
        if v == "yfinance":
            return "mt5"
        if v in ("adata", "a_share"):
            return "akshare"
        if v == "eastmoney":
            return "eastmoney"
        if v == "tushare":
            return "tushare"
        return v

    @field_validator("decision_flow_default_zoom_pct", mode="before")
    @classmethod
    def _coerce_zoom_pct(cls, v: object) -> object:
        if v is None:
            return 50
        return v


_FEISHU_CONFIG_KEYS = (
    "enabled",
    "webhook_url",
    "secret",
    "app_id",
    "app_secret",
    "notify_on_order_only",
)


class FeishuSettings(BaseModel):
    """Feishu bot notification settings (persisted in settings.json)."""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    webhook_url: str = ""
    secret: str = ""
    app_id: str = ""
    app_secret: str = ""
    #: True = only push when there is an order opportunity.
    notify_on_order_only: bool = True


class TushareSettings(BaseModel):
    """Tushare Pro data source settings (persisted in ignored settings.json)."""
    model_config = ConfigDict(extra="ignore")

    token: str = ""


class PushPlusSettings(BaseModel):
    """PushPlus notification settings (settings.json only; no GUI)."""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    token: str = ""


class Settings(BaseModel):
    """Root settings object persisted to config/settings.json."""
    model_config = ConfigDict(extra="ignore")

    #: Legacy-compatible mirror of the active profile. Profiles remain canonical.
    provider: AIProviderSettings = Field(default_factory=AIProviderSettings)
    active_ai_profile_id: str = "default"
    ai_profiles: dict[str, AIProviderProfile] = Field(default_factory=dict)
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    prompt: PromptSettings = Field(default_factory=PromptSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    pushplus: PushPlusSettings = Field(default_factory=PushPlusSettings)
    tushare: TushareSettings = Field(default_factory=TushareSettings)

    @model_validator(mode="after")
    def _normalise_ai_profiles(self) -> "Settings":
        if not self.ai_profiles:
            profile = AIProviderProfile(
                id="default",
                display_name="默认配置",
                provider=self.provider.model_copy(deep=True),
            )
            self.ai_profiles = {profile.id: profile}
            self.active_ai_profile_id = profile.id
            return self

        normalised: dict[str, AIProviderProfile] = {}
        for key, profile in self.ai_profiles.items():
            profile_id = str(key or profile.id).strip()
            if not profile_id:
                raise ValueError("AI profile id must not be blank")
            profile.id = profile_id
            if profile_id in normalised:
                raise ValueError(f"duplicate AI profile id: {profile_id}")
            profile.invalidate_stale_verification()
            normalised[profile_id] = profile
        self.ai_profiles = normalised

        active_id = str(self.active_ai_profile_id or "").strip()
        if active_id not in self.ai_profiles:
            verified_ids = sorted(
                profile_id
                for profile_id, profile in self.ai_profiles.items()
                if profile.verification.is_current_for(profile.provider)
            )
            active_id = verified_ids[0] if verified_ids else sorted(self.ai_profiles)[0]
        self.active_ai_profile_id = active_id
        self.provider = self.ai_profiles[active_id].provider.model_copy(deep=True)
        return self

    def sync_active_ai_profile(self) -> AIProviderProfile:
        """Copy the legacy provider mirror into the canonical active profile."""
        profile = self.ai_profiles.get(self.active_ai_profile_id)
        if profile is None:
            raise KeyError(f"unknown active AI profile: {self.active_ai_profile_id}")
        provider = self.provider.model_copy(deep=True)
        profile.provider = provider
        profile.invalidate_stale_verification()
        return profile

    def save_ai_profile(
        self,
        profile_id: str,
        display_name: str,
        provider: AIProviderSettings,
        *,
        replace: bool = False,
    ) -> AIProviderProfile:
        """Add a named, initially untested profile without activating it."""
        profile_id = str(profile_id or "").strip()
        display_name = str(display_name or "").strip()
        if not profile_id or not display_name:
            raise ValueError("profile id and display name must not be blank")
        if profile_id in self.ai_profiles and not replace:
            raise ValueError(f"AI profile already exists: {profile_id}")
        profile = AIProviderProfile(
            id=profile_id,
            display_name=display_name,
            provider=provider.model_copy(deep=True),
        )
        self.ai_profiles[profile_id] = profile
        if profile_id == self.active_ai_profile_id:
            self.provider = profile.provider.model_copy(deep=True)
        return profile

    def mark_ai_profile_verification(
        self,
        profile_id: str,
        *,
        passed: bool,
        tested_at: str,
        adapter_id: str,
        checks: dict[str, bool] | None = None,
        observations: dict[str, bool] | None = None,
        error: str = "",
    ) -> AIProviderVerification:
        """Store a probe result for the profile's exact current configuration."""
        if profile_id == self.active_ai_profile_id:
            self.sync_active_ai_profile()
        profile = self.ai_profiles.get(profile_id)
        if profile is None:
            raise KeyError(f"unknown AI profile: {profile_id}")
        resolved_adapter_id = resolve_provider_capability(profile.provider).adapter_id
        adapter_id = str(adapter_id or "").strip()
        if adapter_id != resolved_adapter_id:
            raise ValueError(
                f"verification adapter mismatch: expected {resolved_adapter_id}, got {adapter_id}"
            )
        tested_at = str(tested_at or "").strip()
        if not tested_at:
            raise ValueError("verification tested_at must not be blank")
        check_results = dict(checks or {})
        if passed and not all(
            check_results.get(name) is True
            for name in REQUIRED_PROVIDER_VERIFICATION_CHECKS
        ):
            raise ValueError(
                "passed verification requires all mandatory provider checks"
            )
        safe_error = str(error or "")
        for secret in (profile.provider.api_key, profile.provider.api_key_encrypted):
            if secret:
                safe_error = safe_error.replace(secret, "***")
        verification = AIProviderVerification(
            status="passed" if passed else "failed",
            tested_at=tested_at,
            adapter_id=adapter_id,
            capability_schema_version=CAPABILITY_SCHEMA_VERSION,
            config_fingerprint=provider_config_fingerprint(profile.provider),
            checks=check_results,
            observations=dict(observations or {}),
            error="" if passed else safe_error[:500],
        )
        profile.verification = verification
        return verification

    def activate_ai_profile(
        self,
        profile_id: str,
        *,
        require_verified: bool = True,
    ) -> AIProviderSettings:
        """Activate a profile atomically; verified profiles are required by default."""
        profile = self.ai_profiles.get(profile_id)
        if profile is None:
            raise KeyError(f"unknown AI profile: {profile_id}")

        self.sync_active_ai_profile()
        profile.invalidate_stale_verification()
        resolved_adapter_id = resolve_provider_capability(profile.provider).adapter_id
        verification_current = (
            profile.verification.is_current_for(profile.provider)
            and profile.verification.adapter_id == resolved_adapter_id
        )
        if require_verified and not verification_current:
            raise ValueError(
                f"AI profile is not verified for its current configuration: {profile_id}"
            )

        self.active_ai_profile_id = profile_id
        self.provider = profile.provider.model_copy(deep=True)
        return self.provider


def provider_api_key_configured(settings: Settings | None) -> bool:
    """Return True when a non-empty API key is loaded in memory."""
    if settings is None:
        return False
    return bool((settings.provider.api_key or "").strip())


# ── Persistence ───────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)


def _migrate_legacy_feishu_json(raw: dict, settings_path: Path) -> bool:
    """Merge legacy config/feishu.json into settings.feishu when needed."""
    legacy_path = settings_path.parent / "feishu.json"
    if not legacy_path.exists():
        return False

    feishu = raw.setdefault("feishu", {})
    if (feishu.get("webhook_url") or "").strip():
        return False

    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("legacy feishu.json unreadable (%s); skipping migration", exc)
        return False

    migrated = False
    for key in _FEISHU_CONFIG_KEYS:
        if key not in legacy:
            continue
        value = legacy.get(key)
        if value in (None, ""):
            continue
        if feishu.get(key) in (None, ""):
            feishu[key] = value
            migrated = True
    if migrated:
        logger.info("Migrated Feishu config from %s into settings.json", legacy_path)
    return migrated


def load_settings(path: Path | None = None) -> "Settings":
    """Load settings from *path* (default: SETTINGS_JSON_PATH).

    Returns default Settings and writes them to disk if the file is absent.
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH

    if not path.exists():
        defaults = Settings()
        save_settings(defaults, path)
        return defaults

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("settings.json unreadable (%s); using defaults", exc)
        return Settings()

    # Migrate legacy field names
    profiles_raw = raw.get("ai_profiles")
    active_profile_raw = str(raw.get("active_ai_profile_id") or "").strip()
    migrated_ai_profiles = not isinstance(profiles_raw, dict) or not profiles_raw
    repaired_active_profile = (
        isinstance(profiles_raw, dict)
        and bool(profiles_raw)
        and active_profile_raw not in profiles_raw
    )
    general = raw.get("general", {})
    if "cost_warning_threshold_pct" in general and "context_warning_threshold_pct" not in general:
        general["context_warning_threshold_pct"] = general.pop("cost_warning_threshold_pct")
    general.pop("last_htf_text", None)
    from pa_agent.data.market_defaults import migrate_general_gold_defaults

    migrate_general_gold_defaults(general)
    if "default_bar_count" in general and "analysis_bar_count" not in general:
        general["analysis_bar_count"] = general.pop("default_bar_count")
    raw["general"] = general
    provider = raw.get("provider", {})
    provider.pop("pricing", None)
    raw["provider"] = provider

    # Migrate legacy encrypted key: drop it, api_key already in provider dict
    raw.setdefault("provider", {}).setdefault("api_key", "")

    migrated_feishu = _migrate_legacy_feishu_json(raw, path)
    settings = Settings.model_validate(raw)
    dirty = migrated_feishu or migrated_ai_profiles or repaired_active_profile
    if settings.pushplus.enabled and not settings.pushplus.token.strip():
        if not (os.environ.get("PUSHPLUS_TOKEN") or "").strip():
            settings.pushplus.enabled = False
            logger.info(
                "PushPlus enabled but token empty — auto-disabled "
                "(Feishu notifications unaffected)"
            )
            dirty = True
    if dirty:
        save_settings(settings, path)
    return settings


def save_settings(settings: "Settings", path: Path | None = None) -> None:
    """Persist settings to *path* (default: SETTINGS_JSON_PATH)."""
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    settings.sync_active_ai_profile()
    data = settings.model_dump()

    payload = json.dumps(data, ensure_ascii=False, indent=2)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
