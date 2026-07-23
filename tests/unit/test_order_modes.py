from decimal import Decimal

import pytest

from pa_agent.execution.order_modes import (
    apply_entry_atr_slippage,
    apply_exit_atr_slippage,
    effective_entry_type,
)


@pytest.mark.parametrize(
    ("signal_type", "mode", "expected"),
    [
        ("limit", "signal", "limit"),
        ("breakout", "limit", "limit"),
        ("limit", "limit_with_slippage", "limit"),
        ("limit", "market", "market"),
    ],
)
def test_effective_entry_type_applies_explicit_mode(signal_type, mode, expected):
    assert effective_entry_type(signal_type, mode) == expected


def test_entry_slippage_moves_limit_towards_fill():
    assert apply_entry_atr_slippage(Decimal("100"), "long", 2, "0.5") == Decimal("101.0")
    assert apply_entry_atr_slippage(Decimal("100"), "short", 2, "0.5") == Decimal("99.0")


def test_exit_slippage_moves_limit_towards_fill():
    assert apply_exit_atr_slippage(Decimal("100"), "long", 2, "0.5") == Decimal("99.0")
    assert apply_exit_atr_slippage(Decimal("100"), "short", 2, "0.5") == Decimal("101.0")


def test_slippage_cannot_create_non_positive_price():
    with pytest.raises(ValueError, match="必须为正数"):
        apply_entry_atr_slippage(Decimal("1"), "short", 10, "0.2")
