"""市场规则块与交易所时区在提示词中的注入测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompt_engineering"


def _frame(symbol: str, *, n: int = 3) -> KlineFrame:
    # 2025-06-04 14:30 UTC 起、间隔 10 分钟、newest-first 的已收盘 K 线。
    base_ms = 1_749_047_400_000
    bars = tuple(
        KlineBar(
            seq=i + 1,
            ts_open=float(base_ms - i * 600_000),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=10.0,
            amount=1005.0,
            closed=True,
        )
        for i in range(n)
    )
    indicators = IndicatorBundle(
        ema20=tuple(100.0 for _ in range(n)),
        atr14=tuple(1.0 for _ in range(n)),
    )
    return KlineFrame(
        symbol=symbol,
        timeframe="10m",
        bars=bars,
        indicators=indicators,
        snapshot_ts_local_ms=base_ms,
    )


@pytest.fixture()
def assembler() -> PromptAssembler:
    return PromptAssembler(_PROMPT_DIR)


def test_stage1_injects_us_rules_and_eastern_time(assembler: PromptAssembler):
    prompt = assembler._build_stage1_user_prompt(_frame("AAPL.US"))
    assert "市场制度规则" in prompt
    assert "T+1（2024-05 起）" in prompt
    assert "时间（美东时间）" in prompt
    # 2025-06-04 14:30 UTC == 美东 10:30
    assert "2025-06-04 10:30" in prompt


def test_stage1_injects_cn_rules_and_beijing_time(assembler: PromptAssembler):
    prompt = assembler._build_stage1_user_prompt(_frame("600519.SH"))
    assert "T+1 交易制度" in prompt
    assert "涨跌停" in prompt
    assert "时间（北京时间）" in prompt
    assert "2025-06-04 22:30" in prompt


def test_stage1_crypto_rules_keep_utc(assembler: PromptAssembler):
    prompt = assembler._build_stage1_user_prompt(_frame("XAU-USDT-SWAP"))
    assert "资金费率" in prompt
    assert "时间（UTC）" in prompt
    assert "2025-06-04 14:30" in prompt


def test_stage1_unmapped_symbol_has_no_rules_and_stays_utc(
    assembler: PromptAssembler,
):
    prompt = assembler._build_stage1_user_prompt(_frame("XAUUSD"))
    assert "市场制度规则" not in prompt
    assert "时间（UTC）" in prompt
    assert "2025-06-04 14:30" in prompt


def test_stage2_standalone_injects_rules_once(assembler: PromptAssembler):
    stage1_json = {"direction": "bullish"}
    prompt = assembler._build_stage2_user_prompt(
        frame=_frame("700.HK"),
        stage1_json=stage1_json,
        strategy_files=[],
        experience_entries=[],
    )
    assert prompt.count("市场制度规则块 · 港股") == 1
    assert "时间（香港时间）" in prompt


def test_stage2_prefix_chain_mode_skips_duplicate_rules(
    assembler: PromptAssembler,
):
    stage1_json = {"direction": "bullish"}
    prompt = assembler._build_stage2_user_prompt(
        frame=_frame("700.HK"),
        stage1_json=stage1_json,
        strategy_files=[],
        experience_entries=[],
        omit_kline_block=True,
    )
    assert "市场制度规则块" not in prompt


def test_market_rule_injection_keeps_system_prompt_bytes_identical(
    assembler: PromptAssembler,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        assembler,
        "_record_volume_shadow",
        lambda _frame: None,
    )
    baseline = assembler._build_shared_system_prompt_inner().encode("utf-8")

    stage1_messages = assembler.build_stage1(_frame("AAPL.US"))
    stage2_messages = assembler.build_stage2(
        frame=_frame("700.HK"),
        stage1_json={"direction": "bullish"},
        strategy_files=[],
        experience_entries=[],
    )
    rebuilt = assembler._build_shared_system_prompt_inner().encode("utf-8")

    assert "市场制度规则块 · 美股" in stage1_messages[1]["content"]
    assert "市场制度规则块 · 港股" in stage2_messages[1]["content"]
    assert stage1_messages[0]["content"].encode("utf-8") == baseline
    assert stage2_messages[0]["content"].encode("utf-8") == baseline
    assert rebuilt == baseline
    assert "市场制度规则块".encode() not in baseline


def _frame_at(symbol: str, utc_iso: str, *, n: int = 3):
    """构造最新已收盘 K 线开盘时刻为 utc_iso 的分析帧。"""
    from datetime import UTC, datetime

    base_ms = int(datetime.fromisoformat(utc_iso).replace(tzinfo=UTC).timestamp() * 1000)
    frame = _frame(symbol, n=n)
    bars = tuple(
        KlineBar(
            seq=bar.seq,
            ts_open=float(base_ms - i * 600_000),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            amount=bar.amount,
            closed=bar.closed,
        )
        for i, bar in enumerate(frame.bars)
    )
    return KlineFrame(
        symbol=frame.symbol,
        timeframe=frame.timeframe,
        bars=bars,
        indicators=frame.indicators,
        snapshot_ts_local_ms=frame.snapshot_ts_local_ms,
    )


def test_stage1_injects_session_phase_for_stocks(assembler: PromptAssembler):
    # 2025-06-04 周三 13:45 UTC = 美东 09:45，开盘后 15 分钟
    prompt = assembler._build_stage1_user_prompt(
        _frame_at("AAPL.US", "2025-06-04T13:45:00")
    )
    assert "当前 K 线时段：开盘时段" in prompt
    assert "仅作背景，不构成交易限制" in prompt


def test_stage1_session_phase_tracks_close_of_session(assembler: PromptAssembler):
    # 2025-06-05 周四 06:40 UTC = 北京 14:40，A 股尾盘
    prompt = assembler._build_stage1_user_prompt(
        _frame_at("600519.SH", "2025-06-05T06:40:00")
    )
    assert "当前 K 线时段：尾盘" in prompt


def test_stage1_no_session_phase_for_crypto_or_unmapped(assembler: PromptAssembler):
    crypto = assembler._build_stage1_user_prompt(
        _frame_at("XAU-USDT-SWAP", "2025-06-04T13:45:00")
    )
    assert "当前 K 线时段" not in crypto
    unmapped = assembler._build_stage1_user_prompt(
        _frame_at("XAUUSD", "2025-06-04T13:45:00")
    )
    assert "当前 K 线时段" not in unmapped


def test_system_prompt_never_carries_session_phase(assembler: PromptAssembler):
    """时段随每根 K 线变化，绝不能进 system prompt（会击穿前缀缓存）。"""
    assert "当前 K 线时段" not in assembler._build_shared_system_prompt_inner()
