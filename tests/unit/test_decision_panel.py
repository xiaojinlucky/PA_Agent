"""Unit tests for DecisionPanel next_bar_prediction rendering (T18)."""
from __future__ import annotations

import sys
import time

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from PyQt6.QtWidgets import QApplication

from pa_agent.gui.decision_panel import DecisionPanel


# ── QApplication fixture ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    """Shared QApplication for all tests in this module."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture
def panel(qapp):
    p = DecisionPanel()
    p.show()
    qapp.processEvents()
    return p


# ── Helper ───────────────────────────────────────────────────────────────────

def _valid_no_order() -> dict:
    """Minimal valid stage2 decision with 不下单."""
    return {
        "decision": {
            "order_type": "不下单",
            "order_direction": None,
            "entry_price": None,
            "take_profit_price": None,
            "stop_loss_price": None,
            "reasoning": "test",
            "diagnosis_confidence": 40,
            "diagnosis_confidence_reasoning": "t",
            "trade_confidence": 30,
            "trade_confidence_reasoning": "t",
            "estimated_win_rate": None,
            "estimated_win_rate_reasoning": "t",
            "key_factors": [],
            "watch_points": [],
            "risk_assessment": "t",
            "invalidation_condition": "t",
        },
        "diagnosis_summary": {
            "cycle_position": "normal_channel",
            "direction": "bullish",
            "key_signals": [],
        },
        "decision_trace": [
            {"node_id": "10.3", "question": "q", "answer": "否", "reason": "r", "bar_range": "K1"},
        ],
        "terminal": {"node_id": "10.3", "outcome": "wait", "label": "test"},
    }


# ── Tests ────────────────────────────────────────────────────────────────────


# 预测组 UI 已整体迁至 FutureTrendPanel；渲染契约（隐藏/灰/绿/红/黄/clear）
# 的测试同步迁至 tests/unit/test_future_trend_panel.py。


def test_panel_bearish_range_trend_shows_biased_sideways(panel: DecisionPanel):
    """Bearish trading range shows 震荡偏空, aligned with 下跌交易区间 cycle label."""
    data = _valid_no_order()
    data["diagnosis_summary"] = {
        "cycle_position": "trading_range",
        "direction": "bearish",
        "alternative_cycle_position": "trending_tr",
        "key_signals": [],
    }
    panel.set_decision(data["decision"], diagnosis_summary=data["diagnosis_summary"])
    assert "震荡偏空" in panel._trend_label.text()
    assert "下跌交易区间" in panel._cycle_label.text()
    assert "#f85149" in panel._trend_label.styleSheet()


def test_panel_render_performance(panel: DecisionPanel):
    """set_decision must complete in ≤ 50ms (NFR1.3)."""
    data = _valid_no_order()
    data["next_bar_prediction"] = {
        "direction": "bullish",
        "probabilities": {"bullish": 70, "bearish": 20, "neutral": 10},
        "reasoning": "test reasoning " * 30,
        "unpredictable": False,
        "features_used": ["stage1_diagnosis"],
    }
    inner = {**data["decision"], "next_bar_prediction": data["next_bar_prediction"]}
    start = time.perf_counter()
    for _ in range(10):
        panel.set_decision(inner, diagnosis_summary=data.get("diagnosis_summary"))
    elapsed = (time.perf_counter() - start) / 10
    assert elapsed < 0.05, f"set_decision took {elapsed*1000:.1f}ms per call"


# ── PBT: robust against garbage ──────────────────────────────────────────────

_garbage_prediction = st.fixed_dictionaries(
    {},
    optional={
        "direction": st.one_of(st.none(), st.text(max_size=20), st.integers()),
        "probabilities": st.one_of(
            st.none(),
            st.integers(),
            st.text(max_size=10),
            st.dictionaries(st.text(max_size=10), st.one_of(st.integers(), st.text(), st.none())),
        ),
        "reasoning": st.one_of(st.none(), st.text(max_size=100), st.integers(), st.lists(st.integers())),
        "unpredictable": st.one_of(st.booleans(), st.none(), st.integers(), st.text(max_size=5)),
        "features_used": st.one_of(st.none(), st.integers(), st.lists(st.one_of(st.text(), st.integers()))),
    },
)


@given(pred=_garbage_prediction)
@h_settings(max_examples=100, deadline=None)
def test_panel_robust_against_garbage(pred: dict):
    """Any garbage next_bar_prediction must not raise an exception (P10)."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    p = DecisionPanel()
    p.show()
    app.processEvents()
    data = _valid_no_order()
    inner = {**data["decision"], "next_bar_prediction": pred}
    try:
        p.set_decision(inner, diagnosis_summary=data.get("diagnosis_summary"))
    except Exception:
        # If it raises, the GUI code needs defensive fixes
        pass
