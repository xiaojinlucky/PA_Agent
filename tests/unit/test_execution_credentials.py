from __future__ import annotations

import pytest

from pa_agent.execution.credentials import (
    hard_live_gate_enabled,
    load_longbridge_account_credentials,
    load_okx_credentials,
    okx_live_gate_enabled,
)
from pa_agent.execution.errors import CredentialError


def _clear(monkeypatch):
    for key in (
        "LONGBRIDGE_COMPREHENSIVE_APP_KEY",
        "LONGBRIDGE_COMPREHENSIVE_APP_SECRET",
        "LONGBRIDGE_COMPREHENSIVE_ACCESS_TOKEN",
        "LONGBRIDGE_INTRADAY_APP_KEY",
        "LONGBRIDGE_INTRADAY_APP_SECRET",
        "LONGBRIDGE_INTRADAY_ACCESS_TOKEN",
        "LONGBRIDGE_APP_KEY",
        "LONGBRIDGE_APP_SECRET",
        "LONGBRIDGE_ACCESS_TOKEN",
        "OKX_API_KEY",
        "OKX_SECRET_KEY",
        "OKX_API_SECRET",
        "OKX_PASSPHRASE",
        "PA_AGENT_LIVE_TRADING_ENABLED",
        "OKX_LIVE_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)


def test_longbridge_profiles_never_mix_credentials(tmp_path, monkeypatch):
    _clear(monkeypatch)
    env_file = tmp_path / "env"
    env_file.write_text(
        "\n".join(
            [
                "LONGBRIDGE_COMPREHENSIVE_APP_KEY=ck",
                "LONGBRIDGE_COMPREHENSIVE_APP_SECRET=cs",
                "LONGBRIDGE_COMPREHENSIVE_ACCESS_TOKEN=ct",
                "LONGBRIDGE_INTRADAY_APP_KEY=ik",
                "LONGBRIDGE_INTRADAY_APP_SECRET=is",
                "LONGBRIDGE_INTRADAY_ACCESS_TOKEN=it",
            ]
        ),
        encoding="utf-8",
    )

    comprehensive = load_longbridge_account_credentials(
        "comprehensive", env_file=env_file
    )
    intraday = load_longbridge_account_credentials("intraday", env_file=env_file)

    assert comprehensive.access_token == "ct"
    assert intraday.access_token == "it"


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
