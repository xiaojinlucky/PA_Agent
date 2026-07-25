from __future__ import annotations

from decimal import Decimal

import pytest

from pa_agent.risk.sizing import (
    RiskCalculationFailure,
    calculate_fixed_quantity_risk,
    calculate_risk_size,
)


def _calculate(**overrides):
    values = {
        "account_equity": "10000",
        "risk_capital_cap": "5000",
        "risk_percent": "0.10",
        "entry_price": "4000",
        "stop_loss_price": "3990",
        "side": "long",
        "ct_val": "0.001",
        "ct_mult": "1",
        "lot_sz": "1",
        "min_sz": "1",
        "max_sz": "100000",
        "fee_rate": "0.0005",
        "slippage_rate": "0.001",
    }
    values.update(overrides)
    return calculate_risk_size(**values)


def test_long_and_short_use_the_same_absolute_stop_loss_distance():
    long = _calculate()
    short = _calculate(
        entry_price="4000",
        stop_loss_price="4010",
        side="short",
    )

    assert long.target_contract_size > 0
    assert short.target_contract_size > 0
    assert long.stop_distance_usdt == Decimal("10")
    assert short.stop_distance_usdt == Decimal("10")
    assert long.price_loss_per_contract_usdt == short.price_loss_per_contract_usdt


def test_fee_and_slippage_are_included_in_each_contract_worst_case_loss():
    without_costs = _calculate(fee_rate="0", slippage_rate="0")
    with_costs = _calculate()

    assert with_costs.worst_case_loss_per_contract_usdt > (
        without_costs.worst_case_loss_per_contract_usdt
    )
    assert with_costs.target_contract_size < without_costs.target_contract_size
    assert with_costs.fee_per_contract_usdt > 0
    assert with_costs.slippage_per_contract_usdt > 0


def test_broker_maximum_insufficient_for_risk_target_blocks_new_risk():
    with pytest.raises(RiskCalculationFailure) as exc:
        _calculate(max_sz="10")

    assert exc.value.code == "max_size_exceeded"
    assert exc.value.required_size > exc.value.maximum_size
    assert exc.value.maximum_size == Decimal("10")


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"stop_loss_price": None}, "missing_input"),
        ({"entry_price": "4000", "stop_loss_price": "4000"}, "invalid_stop"),
        ({"ct_val": None}, "missing_input"),
        ({"risk_capital_cap": "0"}, "invalid_input"),
        ({"max_sz": "0"}, "invalid_input"),
        ({"min_sz": "11", "max_sz": "10"}, "max_size_below_minimum"),
        (
            {
                "account_equity": "0.01",
                "entry_price": "4000",
                "stop_loss_price": "3990",
            },
            "below_minimum",
        ),
    ],
)
def test_invalid_or_insufficient_inputs_fail_closed(overrides, code):
    with pytest.raises(RiskCalculationFailure) as exc:
        _calculate(**overrides)

    assert exc.value.code == code


def test_same_inputs_are_bitwise_deterministic():
    first = _calculate()
    second = _calculate()

    assert first == second


def test_equity_growth_above_fixed_cap_does_not_increase_risk():
    before = _calculate(
        account_equity="4893.97315732",
        risk_capital_cap="4000",
        entry_price="4055.8",
        stop_loss_price="4076.4",
        side="short",
        max_sz="42625",
    )
    after = _calculate(
        account_equity="8899.30154170003",
        risk_capital_cap="4000",
        entry_price="4055.8",
        stop_loss_price="4076.4",
        side="short",
        max_sz="42625",
    )

    assert before.effective_risk_capital_usdt == Decimal("4000")
    assert after.effective_risk_capital_usdt == Decimal("4000")
    assert before.risk_budget_usdt == Decimal("400.00")
    assert after.risk_budget_usdt == Decimal("400.00")
    assert after.target_contract_size == before.target_contract_size


def test_equity_below_fixed_cap_reduces_risk():
    full = _calculate(
        account_equity="5000",
        risk_capital_cap="5000",
    )
    reduced = _calculate(
        account_equity="3000",
        risk_capital_cap="5000",
    )

    assert full.effective_risk_capital_usdt == Decimal("5000")
    assert reduced.effective_risk_capital_usdt == Decimal("3000")
    assert full.risk_budget_usdt == Decimal("500.00")
    assert reduced.risk_budget_usdt == Decimal("300.00")
    assert reduced.target_contract_size < full.target_contract_size


def test_fixed_quantity_returns_derived_risk_without_changing_quantity():
    result = calculate_fixed_quantity_risk(
        account_equity="10000",
        risk_capital_cap="5000",
        quantity="120",
        entry_price="4000",
        stop_loss_price="3990",
        side="long",
        ct_val="0.001",
        ct_mult="1",
        lot_sz="1",
        min_sz="1",
        max_sz="100000",
        fee_rate="0.0005",
        slippage_rate="0.001",
    )

    assert result.target_contract_size == Decimal("120")
    assert result.risk_used_usdt == (
        result.worst_case_loss_per_contract_usdt * Decimal("120")
    )
    assert result.risk_percent == (
        result.risk_used_usdt / Decimal("5000")
    )


@pytest.mark.parametrize(
    ("quantity", "max_sz", "code"),
    [
        ("0.5", "100000", "below_minimum"),
        ("1.5", "100000", "invalid_lot"),
        ("120", "100", "max_size_exceeded"),
        ("500000", "500000", "risk_exceeds_capital"),
    ],
)
def test_fixed_quantity_invalid_or_over_risk_never_gets_silently_reduced(
    quantity,
    max_sz,
    code,
):
    with pytest.raises(RiskCalculationFailure) as exc:
        calculate_fixed_quantity_risk(
            account_equity="10000",
            risk_capital_cap="5000",
            quantity=quantity,
            entry_price="4000",
            stop_loss_price="3990",
            side="long",
            ct_val="0.001",
            ct_mult="1",
            lot_sz="1",
            min_sz="1",
            max_sz=max_sz,
            fee_rate="0.0005",
            slippage_rate="0.001",
        )

    assert exc.value.code == code
