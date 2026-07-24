"""Tests for stage prompt .txt file list helpers."""
from __future__ import annotations

from pathlib import Path

from pa_agent.ai.prompt_assembler import (
    COMMON_SYSTEM_STAGE1_TXT_FILES,
    COMMON_SYSTEM_STAGE2_TXT_FILES,
    STAGE1_TASK_PROMPT_TXT_FILES,
    STAGE2_BASE_PROMPT_TXT_FILES,
    STAGE2_FULL_STRATEGY_PROMPT_TXT_FILES,
    stage1_prompt_txt_files,
    stage2_prompt_txt_files,
    stage2_user_task_txt_files,
)


PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompt_engineering"


def test_stage1_txt_files() -> None:
    files = stage1_prompt_txt_files()
    assert files == [*COMMON_SYSTEM_STAGE1_TXT_FILES, *STAGE1_TASK_PROMPT_TXT_FILES]
    # Stage 1 now uses the full binary tree (same as Stage 2) for prefix caching
    assert "二元决策.txt" in files
    assert "二元决策_闸门.txt" not in files
    assert "文件13-窄通道与宽通道策略.txt" not in files


def test_stage2_routed_only_bullish() -> None:
    routed = ["震荡区间交易策略.txt", "上涨通道分析识别.txt"]
    files = stage2_user_task_txt_files(routed, direction="bullish")
    assert "上涨通道分析识别.txt" in files
    assert "下跌通道分析识别.txt" not in files
    assert "下跌通道交易策略.txt" not in files
    assert "文件17-止损和止盈与仓位管理.txt" in files
    for name in STAGE2_FULL_STRATEGY_PROMPT_TXT_FILES:
        if name.startswith("下跌") or name.startswith("极速下跌"):
            assert name not in files


def test_stage2_full_library_flag() -> None:
    routed = ["震荡区间交易策略.txt"]
    files = stage2_user_task_txt_files(
        routed,
        direction="bullish",
        load_full_strategy_library=True,
    )
    for name in STAGE2_FULL_STRATEGY_PROMPT_TXT_FILES:
        assert name in files


def test_stage2_txt_files_order() -> None:
    routed = ["震荡区间交易策略.txt", "震荡区间分析识别.txt"]
    files = stage2_prompt_txt_files(routed, direction="neutral")
    expected_user = stage2_user_task_txt_files(routed, direction="neutral")
    assert files == [*COMMON_SYSTEM_STAGE2_TXT_FILES, *expected_user]
    assert files[:2] == list(COMMON_SYSTEM_STAGE2_TXT_FILES)
    assert files[-4:] == list(STAGE2_BASE_PROMPT_TXT_FILES)
    assert routed[0] in files


def test_external_mtf_prompt_contract_has_no_single_period_contradiction() -> None:
    """The runtime-injected HTF context must not be disabled by stale prompt text."""
    binary = (PROMPT_DIR / "二元决策.txt").read_text(encoding="utf-8")
    diagnosis = (PROMPT_DIR / "市场诊断框架.txt").read_text(encoding="utf-8")

    assert "外部多周期背景（程序提供时才启用）" in binary
    assert "主周期是唯一交易主线" in binary
    assert "不能要求多周期共识" in binary
    assert "不额外切换到更小周期" in binary

    assert "若程序额外提供 `1h/4h` 等外部高周期薄标签" in diagnosis
    assert "不要假设存在任何外部周期图表" not in diagnosis
    assert "不引用任何外部周期图表" not in diagnosis
    assert "若三窗口方向一致，诊断置信度最高" not in diagnosis
    second_entry = (PROMPT_DIR / "文件15-二次入场机会.txt").read_text(encoding="utf-8")
    assert "仅当共振方向与 direction 一致时可评估三价" not in second_entry
    assert "不构成额外硬闸" in second_entry
    spike = (PROMPT_DIR / "极速上涨分析识别.txt").read_text(encoding="utf-8")
    assert "应在多个观察窗口上都能被识别" not in spike
    assert "长程结构窗口级别确认 = 高可靠性" not in spike
    assert "不要求多窗口共识" in spike
    falling_spike = (PROMPT_DIR / "极速下跌分析识别.txt").read_text(encoding="utf-8")
    assert "长程结构窗口级别确认 = 高可靠性" not in falling_spike
    assert "不要求多窗口共识" in falling_spike
    assert "三个窗口方向一致 = 信号可靠性提高" not in diagnosis
