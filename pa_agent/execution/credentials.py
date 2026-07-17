"""Load broker credentials from process environment or Quant's shared env file."""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pa_agent.execution.errors import CredentialError

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class LongbridgeAccountCredentials:
    app_key: str
    app_secret: str
    access_token: str


@dataclass(frozen=True)
class OkxCredentials:
    api_key: str
    secret_key: str
    passphrase: str


def shared_env_path() -> Path:
    from pa_agent.config.paths import PROJECT_ROOT

    return PROJECT_ROOT.parent / "env"


def read_env_file(path: Path | None = None) -> dict[str, str]:
    target = path or shared_env_path()
    if not target.is_file():
        return {}
    try:
        lines = target.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise CredentialError(f"无法读取共享环境文件：{target}") from exc

    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _ENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def merged_environment(path: Path | None = None) -> dict[str, str]:
    values = read_env_file(path)
    for key, value in os.environ.items():
        if value:
            values[key] = value
    return values


def _complete_triplet(
    values: Mapping[str, str],
    *,
    prefix: str,
) -> LongbridgeAccountCredentials | None:
    names = (
        f"{prefix}_APP_KEY",
        f"{prefix}_APP_SECRET",
        f"{prefix}_ACCESS_TOKEN",
    )
    configured = [str(values.get(name, "") or "").strip() for name in names]
    present = [bool(value) for value in configured]
    if all(present):
        return LongbridgeAccountCredentials(*configured)
    if any(present):
        missing = [name for name, exists in zip(names, present, strict=True) if not exists]
        raise CredentialError(f"{prefix}_* 凭据不完整，缺少：{', '.join(missing)}")
    return None


def _longbridge_token_identity(access_token: str) -> tuple[str, str]:
    """Read signed legacy-token identity claims so profile mistakes fail closed."""
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            raise ValueError("unexpected token shape")
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        if not isinstance(claims, dict):
            raise ValueError("unexpected token claims")
        account_class = str(claims.get("ac", "") or "").strip()
        account_id = str(claims.get("aaid", "") or "").strip()
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise CredentialError(
            "Longbridge Legacy Access Token 无法解析账户身份，禁止创建交易会话"
        ) from exc
    if not account_class or not account_id:
        raise CredentialError(
            "Longbridge Legacy Access Token 缺少账户身份，禁止创建交易会话"
        )
    return account_class, account_id


def _validate_longbridge_profile_identity(
    values: Mapping[str, str],
    *,
    profile: str,
    credentials: LongbridgeAccountCredentials,
) -> None:
    account_id_key = {
        "paper": "LONGBRIDGE_PAPER_ACCOUNT_ID",
        "comprehensive": "LONGBRIDGE_COMPREHENSIVE_ACCOUNT_ID",
        "intraday": "LONGBRIDGE_INTRADAY_ACCOUNT_ID",
    }[profile]
    expected_account_id = str(values.get(account_id_key, "") or "").strip()
    if not expected_account_id:
        raise CredentialError(
            f"{profile} 未配置 {account_id_key}，禁止创建交易会话"
        )

    account_class, token_account_id = _longbridge_token_identity(
        credentials.access_token
    )
    expected_class = "lb_papertrading" if profile == "paper" else "lb"
    if account_class != expected_class:
        raise CredentialError(
            f"{profile} Access Token 的账户类型不匹配，禁止创建交易会话"
        )
    if token_account_id != expected_account_id:
        raise CredentialError(
            f"{profile} Access Token 与绑定账户 ID 不匹配，禁止创建交易会话"
        )


def load_longbridge_account_credentials(
    profile: str,
    *,
    env_file: Path | None = None,
) -> LongbridgeAccountCredentials:
    values = merged_environment(env_file)
    if profile == "paper":
        prefixes = ("LONGBRIDGE_PAPER",)
    elif profile == "comprehensive":
        prefixes = ("LONGBRIDGE_COMPREHENSIVE", "LONGBRIDGE")
    elif profile == "intraday":
        prefixes = ("LONGBRIDGE_INTRADAY",)
    else:
        raise CredentialError(f"未知 Longbridge 账户配置：{profile}")

    for prefix in prefixes:
        credentials = _complete_triplet(values, prefix=prefix)
        if credentials is not None:
            _validate_longbridge_profile_identity(
                values,
                profile=profile,
                credentials=credentials,
            )
            return credentials
    expected = " 或 ".join(f"{prefix}_*" for prefix in prefixes)
    raise CredentialError(f"未找到 {profile} 账户的完整 Longbridge 凭据（需要 {expected}）")


def load_okx_credentials(*, env_file: Path | None = None) -> OkxCredentials:
    values = merged_environment(env_file)
    api_key = str(values.get("OKX_API_KEY", "") or "").strip()
    secret_key = str(
        values.get("OKX_SECRET_KEY", "")
        or values.get("OKX_API_SECRET", "")
        or ""
    ).strip()
    passphrase = str(values.get("OKX_PASSPHRASE", "") or "").strip()
    present = (bool(api_key), bool(secret_key), bool(passphrase))
    if not all(present):
        labels = ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")
        missing = [
            label for label, configured in zip(labels, present, strict=True) if not configured
        ]
        raise CredentialError(f"OKX 凭据不完整，缺少：{', '.join(missing)}")
    return OkxCredentials(api_key, secret_key, passphrase)


def hard_live_gate_enabled(*, env_file: Path | None = None) -> bool:
    values = merged_environment(env_file)
    return str(values.get("PA_AGENT_LIVE_TRADING_ENABLED", "")).strip().lower() in _TRUE_VALUES


def paper_trading_gate_enabled(*, env_file: Path | None = None) -> bool:
    """Require an explicit switch for simulated broker writes."""
    values = merged_environment(env_file)
    return str(values.get("PA_AGENT_PAPER_TRADING_ENABLED", "")).strip().lower() in _TRUE_VALUES


def okx_live_gate_enabled(*, env_file: Path | None = None) -> bool:
    """Require a separate explicit acknowledgement for OKX live writes."""
    values = merged_environment(env_file)
    return str(values.get("OKX_LIVE_ENABLED", "")).strip().lower() in _TRUE_VALUES
