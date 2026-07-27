"""Tests for display-only tick inference and hard-reject breakout semantics."""
from __future__ import annotations

from pa_agent.ai.json_validator import JsonValidator
from pa_agent.ai.prompt_assembler import _STAGE2_OUTPUT_CONTRACT
from pa_agent.ai.stage2_normalizer import normalize_stage2
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.util.price_tick import (
    format_breakout_tick_hint,
    infer_price_tick_from_frame,
)



def _frame(high: float = 104.0) -> KlineFrame:
    return KlineFrame(
        symbol="XAUUSD",
        timeframe="5m",
        bars=(
            KlineBar(
                seq=1,
                ts_open=1.0,
                open=100.0,
                high=high,
                low=99.0,
                close=103.0,
                volume=1,
                closed=True,
            ),
        ),
        indicators=IndicatorBundle(ema20=(100.0,), atr14=(2.0,)),
        snapshot_ts_local_ms=1,
        price_tick="0.001",
    )


def test_infer_tick_from_three_decimal_prices() -> None:
    frame = _frame(high=4556.595)
    assert infer_price_tick_from_frame(frame) == 0.001


def test_stage2_normalizer_does_not_rewrite_breakout_entry() -> None:
    frame = _frame(high=4556.595)
    obj = normalize_stage2(
        {
            "decision": {
                "order_type": "突破单",
                "order_direction": "做多",
                "entry_basis_bar": "K1",
                "entry_basis_extreme": "high",
                "entry_price": 4556.595,
                "take_profit_price": 4560.0,
                "take_profit_price_2": 4562.0,
                "stop_loss_price": 4554.0,
                "estimated_win_rate": 55,
            },
        },
        kline_frame=frame,
    )
    assert obj["decision"]["entry_price"] == 4556.595
    msgs = JsonValidator._check_breakout_price_extreme(obj, frame)
    assert msgs


def test_stage2_normalizer_does_not_rewrite_breakout_extreme() -> None:
    obj = normalize_stage2(
        {
            "decision": {
                "order_type": "突破单",
                "order_direction": "做空",
                "entry_basis_extreme": "high",
                "entry_basis_bar": "K1",
                "entry_price": 99.0,
                "take_profit_price": 95.0,
                "take_profit_price_2": 90.0,
                "stop_loss_price": 102.0,
                "estimated_win_rate": 60,
            },
        },
        decision_stance="extreme_aggressive",
    )
    assert obj["decision"]["entry_basis_extreme"] == "high"
    assert JsonValidator._check_breakout_order_basis(obj) is not None


def test_breakout_hint_uses_declared_tick_and_promises_no_rewrite() -> None:
    hint = format_breakout_tick_hint(_frame())
    assert "交易所声明" in hint
    assert "0.001" in hint
    assert "不会替你改价" in hint


def test_static_stage2_contract_does_not_promise_tick_guess_or_rewrite() -> None:
    assert "不会校正任何价位" in _STAGE2_OUTPUT_CONTRACT
    assert "禁止从 K 线小数位猜测" in _STAGE2_OUTPUT_CONTRACT
    assert "程序会按 K 线表小数位推断最小跳动" not in _STAGE2_OUTPUT_CONTRACT
