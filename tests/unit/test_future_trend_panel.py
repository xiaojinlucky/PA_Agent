"""FutureTrendPanel 下一根K线预期渲染契约。

语义自旧 DecisionPanel 预测组测试迁移（该组 UI 已整体迁至本面板）：
无预测隐藏 / 不可预测灰 / 看涨绿 / 看跌红 / 中性黄 / clear 隐藏并清空。
"""
from __future__ import annotations

import sys

import pytest
from PyQt6.QtWidgets import QApplication

from pa_agent.gui.future_trend_panel import FutureTrendPanel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture
def panel(qapp):
    p = FutureTrendPanel()
    p.show()
    qapp.processEvents()
    return p


def _decision_with_bar_prediction(prediction: dict | None) -> dict:
    decision: dict = {"order_type": "不下单"}
    if prediction is not None:
        decision["next_bar_prediction"] = prediction
    return decision


def test_no_prediction_hides_bar_group(panel: FutureTrendPanel):
    panel.set_prediction(_decision_with_bar_prediction(None))
    assert not panel._bar_group.isVisible()


def test_unpredictable_renders_gray(panel: FutureTrendPanel):
    panel.set_prediction(
        _decision_with_bar_prediction(
            {
                "direction": None,
                "probabilities": None,
                "reasoning": "数据不足，无法预测方向",
                "unpredictable": True,
                "features_used": ["stage1_diagnosis"],
            }
        )
    )
    assert panel._bar_group.isVisible()
    assert "不可预测" in panel._bar_direction_label.text()
    assert "#8b949e" in panel._bar_direction_label.styleSheet()


def test_bullish_renders_green(panel: FutureTrendPanel):
    panel.set_prediction(
        _decision_with_bar_prediction(
            {
                "direction": "bullish",
                "probabilities": {"bullish": 70, "bearish": 20, "neutral": 10},
                "reasoning": "多头趋势明确，结构支持阳线",
                "unpredictable": False,
                "features_used": ["stage1_diagnosis"],
            }
        )
    )
    assert panel._bar_group.isVisible()
    line = panel._bar_direction_label.text()
    assert "阳线的概率为70%" in line
    assert "阴线的概率为20%" in line
    assert "中性的概率为10%" in line
    assert "#3fb950" in panel._bar_direction_label.styleSheet()


def test_bearish_renders_red(panel: FutureTrendPanel):
    panel.set_prediction(
        _decision_with_bar_prediction(
            {
                "direction": "bearish",
                "probabilities": {"bullish": 15, "bearish": 65, "neutral": 20},
                "reasoning": "空头趋势持续，阴线概率最高",
                "unpredictable": False,
                "features_used": ["stage1_diagnosis"],
            }
        )
    )
    assert "阴线的概率为65%" in panel._bar_direction_label.text()
    assert "#f85149" in panel._bar_direction_label.styleSheet()


def test_neutral_renders_yellow(panel: FutureTrendPanel):
    panel.set_prediction(
        _decision_with_bar_prediction(
            {
                "direction": "neutral",
                "probabilities": {"bullish": 20, "bearish": 25, "neutral": 55},
                "reasoning": "震荡区间，方向不明，中性概率最高",
                "unpredictable": False,
                "features_used": ["stage1_diagnosis"],
            }
        )
    )
    assert "中性的概率为55%" in panel._bar_direction_label.text()
    assert "#e6b800" in panel._bar_direction_label.styleSheet()


def test_clear_hides_group_and_empties_reasoning(panel: FutureTrendPanel):
    panel.set_prediction(
        _decision_with_bar_prediction(
            {
                "direction": "bullish",
                "probabilities": {"bullish": 70, "bearish": 20, "neutral": 10},
                "reasoning": "test",
                "unpredictable": False,
                "features_used": ["stage1_diagnosis"],
            }
        )
    )
    assert panel._bar_group.isVisible()

    panel.clear()
    assert not panel._bar_group.isVisible()
    assert panel._bar_reasoning_edit.toPlainText() == ""
