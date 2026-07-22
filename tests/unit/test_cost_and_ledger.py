"""Tests for SessionTokenLedger token thresholds (pricing removed)."""
from __future__ import annotations

from types import SimpleNamespace

from pa_agent.ai.deepseek_client import AIUsage
from pa_agent.ai.session_ledger import SessionTokenLedger
from pa_agent.gui.main_window import _analysis_token_breakdown


def _usage(prompt: int, completion: int, cached: int = 0) -> AIUsage:
    return AIUsage(
        prompt_tokens=prompt,
        cached_prompt_tokens=cached,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )


def test_ledger_accumulates_tokens():
    ledger = SessionTokenLedger(context_window=1_000_000)
    ledger.add(_usage(100, 50))
    ledger.add(_usage(200, 80))
    assert ledger.total_input == 300
    assert ledger.total_output == 130
    assert ledger.context_used == 280


def test_ledger_breakdown_keys():
    ledger = SessionTokenLedger(context_window=10_000)
    ledger.add(_usage(1000, 500))
    bd = ledger.breakdown()
    assert "total_input" in bd
    assert "context_used" in bd
    assert "total_cny" not in bd


def test_yellow_threshold_at_80_pct():
    ledger = SessionTokenLedger(context_window=1000, warn_pct=80.0)
    events: list[tuple[str, dict]] = []
    ledger.threshold_crossed.connect(lambda level, data: events.append((level, data)))
    ledger.add(_usage(800, 0))
    assert len(events) == 1
    assert events[0][0] == "yellow"


def test_red_threshold_at_95_pct():
    ledger = SessionTokenLedger(context_window=1000, warn_pct=80.0)
    events: list[tuple[str, dict]] = []
    ledger.threshold_crossed.connect(lambda level, data: events.append((level, data)))
    ledger.add(_usage(950, 0))
    assert any(e[0] == "red" for e in events)


def test_reset_clears_counters():
    ledger = SessionTokenLedger(context_window=1000)
    ledger.add(_usage(500, 100))
    ledger.reset()
    assert ledger.total_input == 0
    assert ledger.total_output == 0
    assert ledger.context_used == 0


def test_compacted_context_rearms_threshold_warning():
    ledger = SessionTokenLedger(context_window=1000, warn_pct=80.0)
    events: list[tuple[str, dict]] = []
    ledger.threshold_crossed.connect(lambda level, data: events.append((level, data)))

    ledger.add(_usage(810, 0))
    ledger.add(_usage(200, 0))
    ledger.add(_usage(820, 0))

    assert [event[0] for event in events] == ["yellow", "yellow"]


def test_unknown_context_tracks_tokens_without_fake_percentage():
    ledger = SessionTokenLedger(context_window=None)
    events: list[tuple[str, dict]] = []
    ledger.threshold_crossed.connect(lambda level, data: events.append((level, data)))

    ledger.add(_usage(900_000, 100_000))
    breakdown = ledger.breakdown()

    assert breakdown["context_used"] == 1_000_000
    assert breakdown["context_window"] is None
    assert breakdown["context_pct"] is None
    assert events == []


def test_analysis_then_followup_keeps_current_and_cumulative_usage_separate():
    record = SimpleNamespace(
        stage1_response={
            "usage": {
                "prompt_tokens": 100,
                "cached_prompt_tokens": 10,
                "completion_tokens": 10,
            }
        },
        stage2_response={
            "usage": {
                "prompt_tokens": 200,
                "cached_prompt_tokens": 20,
                "completion_tokens": 20,
            }
        },
        usage_total={
            "prompt_tokens": 300,
            "cached_prompt_tokens": 30,
            "completion_tokens": 30,
            "total_tokens": 330,
        },
    )
    initial = _analysis_token_breakdown(record, 1_000)
    ledger = SessionTokenLedger(context_window=1_000)
    ledger.seed(
        total_input=initial["total_input"],
        total_cached_input=initial["total_cached_input"],
        total_output=initial["total_output"],
        current_input=initial["current_input"],
        current_cached_input=initial["current_cached_input"],
        current_output=initial["current_output"],
    )

    assert ledger.context_used == 220
    assert ledger.total_input + ledger.total_output == 330

    ledger.add(_usage(50, 5))
    after_followup = ledger.breakdown()

    assert after_followup["context_used"] == 55
    assert after_followup["total_input"] + after_followup["total_output"] == 385
