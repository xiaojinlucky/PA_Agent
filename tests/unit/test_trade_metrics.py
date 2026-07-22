"""Unit tests for trade_metrics helpers."""
from __future__ import annotations

from pa_agent.util.trade_metrics import (
    compute_risk_reward,
    format_estimated_win_rate,
    format_estimated_win_rate_reasoning,
    is_long_direction,
    max_risk_reward_ratio,
    min_risk_reward_ratio,
    validate_order_trade_metrics,
)


def test_is_long_direction():
    assert is_long_direction("做多") is True
    assert is_long_direction("做空") is False


def test_compute_risk_reward_short():
    rr = compute_risk_reward(4541, 4510, 4553, "做空")
    assert rr is not None
    assert rr["risk"] == 12
    assert rr["reward"] == 31


def test_rr_bounds_all_stances_share_one_floor() -> None:
    for stance in ("conservative", "balanced", "aggressive", "extreme_aggressive", None):
        assert min_risk_reward_ratio(stance) == 1.0
    assert max_risk_reward_ratio() is None


def test_high_rr_validation_preserves_structural_stop() -> None:
    decision = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.0,
        "take_profit_price": 110.0,
        "take_profit_price_2": 115.0,
        "stop_loss_price": 99.0,
        "estimated_win_rate": 45,
    }
    original_stop = decision["stop_loss_price"]
    assert not validate_order_trade_metrics(decision)
    assert decision["stop_loss_price"] == original_stop
    rr = compute_risk_reward(
        decision["entry_price"],
        decision["take_profit_price"],
        decision["stop_loss_price"],
        decision["order_direction"],
    )
    assert rr is not None
    assert rr["ratio"] == 10.0


def test_format_estimated_win_rate_from_model_field():
    decision = {
        "estimated_win_rate": 47,
        "estimated_win_rate_reasoning": "宽通道顺势，方程用 47%",
    }
    assert format_estimated_win_rate(decision) == "47%"
    assert "47" in format_estimated_win_rate_reasoning(decision)
