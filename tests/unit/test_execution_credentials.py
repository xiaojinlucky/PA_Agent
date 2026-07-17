from __future__ import annotations

import base64
import json

import pytest

from pa_agent.execution.credentials import (
    hard_live_gate_enabled,
    load_longbridge_account_credentials,
    load_okx_credentials,
    okx_live_gate_enabled,
    paper_trading_gate_enabled,
)
from pa_agent.execution.errors import CredentialError


def _clear(monkeypatch):
    for key in (
        "LONGBRIDGE_PAPER_APP_KEY",
        "LONGBRIDGE_PAPER_APP_SECRET",
        "LONGBRIDGE_PAPER_ACCESS_TOKEN",
        "LONGBRIDGE_PAPER_ACCOUNT_ID",
        "LONGBRIDGE_COMPREHENSIVE_APP_KEY",
        "LONGBRIDGE_COMPREHENSIVE_APP_SECRET",
        "LONGBRIDGE_COMPREHENSIVE_ACCESS_TOKEN",
        "LONGBRIDGE_COMPREHENSIVE_ACCOUNT_ID",
        "LONGBRIDGE_INTRADAY_APP_KEY",
        "LONGBRIDGE_INTRADAY_APP_SECRET",
        "LONGBRIDGE_INTRADAY_ACCESS_TOKEN",
        "LONGBRIDGE_INTRADAY_ACCOUNT_ID",
        "LONGBRIDGE_APP_KEY",
        "LONGBRIDGE_APP_SECRET",
        "LONGBRIDGE_ACCESS_TOKEN",
        "OKX_API_KEY",
        "OKX_SECRET_KEY",
        "OKX_API_SECRET",
        "OKX_PASSPHRASE",
        "PA_AGENT_LIVE_TRADING_ENABLED",
        "PA_AGENT_PAPER_TRADING_ENABLED",
        "OKX_LIVE_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)


def _legacy_token(
    account_class: str,
    account_id: str,
    nonce: str = "",
) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"ac": account_class, "aaid": int(account_id), "nonce": nonce}
        ).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_longbridge_profiles_never_mix_credentials(tmp_path, monkeypatch):
    _clear(monkeypatch)
    paper_token = _legacy_token("lb_papertrading", "101")
    comprehensive_token = _legacy_token("lb", "202")
    intraday_token = _legacy_token("lb", "303")
    env_file = tmp_path / "env"
    env_file.write_text(
        "\n".join(
            [
                "LONGBRIDGE_PAPER_APP_KEY=pk",
                "LONGBRIDGE_PAPER_APP_SECRET=ps",
                f"LONGBRIDGE_PAPER_ACCESS_TOKEN={paper_token}",
                "LONGBRIDGE_PAPER_ACCOUNT_ID=101",
                "LONGBRIDGE_COMPREHENSIVE_APP_KEY=ck",
                "LONGBRIDGE_COMPREHENSIVE_APP_SECRET=cs",
                f"LONGBRIDGE_COMPREHENSIVE_ACCESS_TOKEN={comprehensive_token}",
                "LONGBRIDGE_COMPREHENSIVE_ACCOUNT_ID=202",
                "LONGBRIDGE_INTRADAY_APP_KEY=ik",
                "LONGBRIDGE_INTRADAY_APP_SECRET=is",
                f"LONGBRIDGE_INTRADAY_ACCESS_TOKEN={intraday_token}",
                "LONGBRIDGE_INTRADAY_ACCOUNT_ID=303",
            ]
        ),
        encoding="utf-8",
    )

    comprehensive = load_longbridge_account_credentials(
        "comprehensive", env_file=env_file
    )
    intraday = load_longbridge_account_credentials("intraday", env_file=env_file)
    paper = load_longbridge_account_credentials("paper", env_file=env_file)

    assert paper.access_token == paper_token
    assert comprehensive.access_token == comprehensive_token
    assert intraday.access_token == intraday_token
    assert len(
        {
            paper.account_identity,
            comprehensive.account_identity,
            intraday.account_identity,
        }
    ) == 3


def test_longbridge_token_rotation_keeps_same_concrete_account_identity(
    tmp_path,
    monkeypatch,
):
    _clear(monkeypatch)
    env_file = tmp_path / "env"
    first_token = _legacy_token("lb", "202", "old")
    env_file.write_text(
        "LONGBRIDGE_COMPREHENSIVE_APP_KEY=key\n"
        "LONGBRIDGE_COMPREHENSIVE_APP_SECRET=secret\n"
        f"LONGBRIDGE_COMPREHENSIVE_ACCESS_TOKEN={first_token}\n"
        "LONGBRIDGE_COMPREHENSIVE_ACCOUNT_ID=202\n",
        encoding="utf-8",
    )
    first = load_longbridge_account_credentials(
        "comprehensive",
        env_file=env_file,
    )
    second_token = _legacy_token("lb", "202", "new")
    env_file.write_text(
        "LONGBRIDGE_COMPREHENSIVE_APP_KEY=new-key\n"
        "LONGBRIDGE_COMPREHENSIVE_APP_SECRET=new-secret\n"
        f"LONGBRIDGE_COMPREHENSIVE_ACCESS_TOKEN={second_token}\n"
        "LONGBRIDGE_COMPREHENSIVE_ACCOUNT_ID=202\n",
        encoding="utf-8",
    )
    second = load_longbridge_account_credentials(
        "comprehensive",
        env_file=env_file,
    )

    assert first.access_token != second.access_token
    assert first.account_identity == second.account_identity


@pytest.mark.parametrize(
    ("profile", "account_class", "token_account_id", "expected_account_id", "error"),
    [
        ("paper", "lb", "101", "101", "账户类型不匹配"),
        ("comprehensive", "lb_papertrading", "202", "202", "账户类型不匹配"),
        ("intraday", "lb", "999", "303", "绑定账户 ID 不匹配"),
    ],
)
def test_longbridge_profile_rejects_wrong_token_identity(
    tmp_path,
    monkeypatch,
    profile,
    account_class,
    token_account_id,
    expected_account_id,
    error,
):
    _clear(monkeypatch)
    prefix = {
        "paper": "LONGBRIDGE_PAPER",
        "comprehensive": "LONGBRIDGE_COMPREHENSIVE",
        "intraday": "LONGBRIDGE_INTRADAY",
    }[profile]
    env_file = tmp_path / "env"
    env_file.write_text(
        f"{prefix}_APP_KEY=key\n"
        f"{prefix}_APP_SECRET=secret\n"
        f"{prefix}_ACCESS_TOKEN={_legacy_token(account_class, token_account_id)}\n"
        f"{prefix}_ACCOUNT_ID={expected_account_id}\n",
        encoding="utf-8",
    )

    with pytest.raises(CredentialError, match=error):
        load_longbridge_account_credentials(profile, env_file=env_file)


def test_longbridge_profile_rejects_unverifiable_token(tmp_path, monkeypatch):
    _clear(monkeypatch)
    env_file = tmp_path / "env"
    env_file.write_text(
        "LONGBRIDGE_PAPER_APP_KEY=key\n"
        "LONGBRIDGE_PAPER_APP_SECRET=secret\n"
        "LONGBRIDGE_PAPER_ACCESS_TOKEN=opaque\n"
        "LONGBRIDGE_PAPER_ACCOUNT_ID=101\n",
        encoding="utf-8",
    )

    with pytest.raises(CredentialError, match="无法解析账户身份"):
        load_longbridge_account_credentials("paper", env_file=env_file)


def test_longbridge_profile_requires_bound_account_id(tmp_path, monkeypatch):
    _clear(monkeypatch)
    env_file = tmp_path / "env"
    env_file.write_text(
        "LONGBRIDGE_PAPER_APP_KEY=key\n"
        "LONGBRIDGE_PAPER_APP_SECRET=secret\n"
        f"LONGBRIDGE_PAPER_ACCESS_TOKEN={_legacy_token('lb_papertrading', '101')}\n",
        encoding="utf-8",
    )

    with pytest.raises(CredentialError, match="LONGBRIDGE_PAPER_ACCOUNT_ID"):
        load_longbridge_account_credentials("paper", env_file=env_file)


def test_partial_profile_is_rejected_instead_of_cross_filled(tmp_path, monkeypatch):
    _clear(monkeypatch)
    env_file = tmp_path / "env"
    env_file.write_text(
        "\n".join(
            [
                "LONGBRIDGE_INTRADAY_APP_KEY=ik",
                "LONGBRIDGE_INTRADAY_APP_SECRET=is",
                "LONGBRIDGE_ACCESS_TOKEN=legacy-token",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(CredentialError, match="LONGBRIDGE_INTRADAY_ACCESS_TOKEN"):
        load_longbridge_account_credentials("intraday", env_file=env_file)


def test_okx_passphrase_is_mandatory(tmp_path, monkeypatch):
    _clear(monkeypatch)
    env_file = tmp_path / "env"
    env_file.write_text(
        "OKX_API_KEY=key\nOKX_SECRET_KEY=secret\nOKX_PASSPHRASE=\n",
        encoding="utf-8",
    )

    with pytest.raises(CredentialError, match="OKX_PASSPHRASE"):
        load_okx_credentials(env_file=env_file)


def test_hard_live_gate_is_false_unless_explicitly_true(tmp_path, monkeypatch):
    _clear(monkeypatch)
    env_file = tmp_path / "env"
    env_file.write_text("PA_AGENT_LIVE_TRADING_ENABLED=false\n", encoding="utf-8")
    assert hard_live_gate_enabled(env_file=env_file) is False
    env_file.write_text("PA_AGENT_LIVE_TRADING_ENABLED=true\n", encoding="utf-8")
    assert hard_live_gate_enabled(env_file=env_file) is True


def test_paper_gate_is_independent_from_live_gate(tmp_path, monkeypatch):
    _clear(monkeypatch)
    env_file = tmp_path / "env"
    env_file.write_text(
        "PA_AGENT_LIVE_TRADING_ENABLED=false\n"
        "PA_AGENT_PAPER_TRADING_ENABLED=true\n",
        encoding="utf-8",
    )

    assert hard_live_gate_enabled(env_file=env_file) is False
    assert paper_trading_gate_enabled(env_file=env_file) is True


def test_okx_live_gate_is_independent_and_explicit(tmp_path, monkeypatch):
    _clear(monkeypatch)
    env_file = tmp_path / "env"
    env_file.write_text(
        "PA_AGENT_LIVE_TRADING_ENABLED=true\nOKX_LIVE_ENABLED=false\n",
        encoding="utf-8",
    )
    assert okx_live_gate_enabled(env_file=env_file) is False
    env_file.write_text(
        "PA_AGENT_LIVE_TRADING_ENABLED=true\nOKX_LIVE_ENABLED=true\n",
        encoding="utf-8",
    )
    assert okx_live_gate_enabled(env_file=env_file) is True
