"""市场规则块路由测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from pa_agent.ai.market_rules import (
    market_for_symbol,
    market_rules_block,
    session_context_line,
)

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


def _utc_ms(text: str) -> int:
    from datetime import UTC, datetime

    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    ("symbol", "moment", "expected_label"),
    (
        # 2025-06-04 周三美股：09:45 / 13:00 / 15:45 ET
        ("AAPL.US", "2025-06-04T13:45:00", "开盘时段"),
        ("AAPL.US", "2025-06-04T17:00:00", "午盘"),
        ("AAPL.US", "2025-06-04T19:45:00", "尾盘"),
        ("AAPL.US", "2025-06-07T15:00:00", "闭市"),
        # 2025-06-05 周四 A 股：09:35 / 12:00 / 14:40 北京时间
        ("600519.SH", "2025-06-05T01:35:00", "开盘时段"),
        ("600519.SH", "2025-06-05T04:00:00", "午休"),
        ("600519.SH", "2025-06-05T06:40:00", "尾盘"),
        ("000001.SZ", "2025-06-05T01:35:00", "开盘时段"),
        ("700.HK", "2025-06-05T02:00:00", "开盘时段"),
    ),
)
def test_session_context_line_labels(symbol, moment, expected_label):
    line = session_context_line(symbol, _utc_ms(moment))
    assert line is not None
    assert expected_label in line
    assert "仅作背景" in line


def test_session_context_line_marks_half_day():
    # 2024-12-24 港股平安夜半日市：11:30 香港时间已进入尾盘
    line = session_context_line("700.HK", _utc_ms("2024-12-24T03:30:00"))
    assert line is not None
    assert "半日市" in line
    assert "尾盘" in line


@pytest.mark.parametrize(
    "symbol",
    ("XAU-USDT-SWAP", "BTC.HAS", "XAUUSD", ""),
)
def test_session_context_line_skips_non_stock_symbols(symbol):
    """加密 24 小时连续交易、未归类符号一律不加时段标签。"""
    assert session_context_line(symbol, _utc_ms("2025-06-04T13:45:00")) is None


@pytest.mark.parametrize("bad_ts", (None, "", "not-a-number", 0, -1))
def test_session_context_line_rejects_invalid_timestamp(bad_ts):
    assert session_context_line("AAPL.US", bad_ts) is None
