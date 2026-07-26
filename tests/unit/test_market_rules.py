"""市场规则块路由测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from pa_agent.ai.market_rules import market_for_symbol, market_rules_block

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompt_engineering"


@pytest.mark.parametrize(
    ("symbol", "expected"),
    (
        ("600519.SH", "CN"),
        ("000001.SZ", "CN"),
        ("700.HK", "HK"),
        ("AAPL.US", "US"),
        ("BTC.HAS", "CRYPTO"),
        ("XAU-USDT-SWAP", "CRYPTO"),
        ("BTC-USDT", "CRYPTO"),
        ("ETH-USD-SWAP", "CRYPTO"),
        ("XAUUSD", None),
        ("", None),
        ("AAPL", None),
        ("600519.XX", None),
    ),
)
def test_market_for_symbol(symbol, expected):
    assert market_for_symbol(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "must_contain"),
    (
        ("600519.SH", "T+1"),
        ("700.HK", "半日市"),
        ("AAPL.US", "T+0"),
        ("XAU-USDT-SWAP", "资金费率"),
    ),
)
def test_rules_block_content_routing(symbol, must_contain):
    block = market_rules_block(symbol, prompt_dir=_PROMPT_DIR)
    assert block is not None
    assert "版本生效日期" in block
    assert must_contain in block


def test_unmapped_symbol_returns_none_without_guessing():
    assert market_rules_block("XAUUSD", prompt_dir=_PROMPT_DIR) is None


def test_missing_rule_file_crashes_loudly(tmp_path):
    with pytest.raises(OSError):
        market_rules_block("AAPL.US", prompt_dir=tmp_path)
