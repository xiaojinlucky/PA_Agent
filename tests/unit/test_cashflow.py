from __future__ import annotations

from decimal import Decimal

import pytest

from pa_agent.risk.cashflow import (
    CashflowReconciliationFailure,
    classify_okx_external_cashflows,
    reconcile_equity_cashflows,
)


def _bill(
    bill_id: str,
    *,
    bill_type: str,
    subtype: str,
    change: str,
    currency: str = "USDT",
    timestamp: str = "1784850000000",
) -> dict[str, str]:
    return {
        "billId": bill_id,
        "type": bill_type,
        "subType": subtype,
        "balChg": change,
        "ccy": currency,
        "ts": timestamp,
    }


def test_transfer_in_adjusts_high_water_without_creating_trading_profit():
    events = classify_okx_external_cashflows(
        [_bill("11", bill_type="1", subtype="11", change="10000")]
    )

    result = reconcile_equity_cashflows(
        equity_basis="account_total_equity_usd",
        previous_equity_usd="7000",
        current_equity_usd="17000",
        previous_adjusted_high_water_usd="7000",
        external_cashflows=events,
    )

    assert result.net_external_cashflow_usd == Decimal("10000")
    assert result.non_cashflow_equity_change_usd == Decimal("0")
    assert result.adjusted_high_water_usd == Decimal("17000")
    assert result.drawdown_fraction == Decimal("0")


def test_transfer_out_does_not_create_false_drawdown():
    events = classify_okx_external_cashflows(
        [_bill("12", bill_type="1", subtype="12", change="-5000")]
    )

    result = reconcile_equity_cashflows(
        equity_basis="account_total_equity_usd",
        previous_equity_usd="12000",
        current_equity_usd="7000",
        previous_adjusted_high_water_usd="12000",
        external_cashflows=events,
    )

    assert result.net_external_cashflow_usd == Decimal("-5000")
    assert result.non_cashflow_equity_change_usd == Decimal("0")
    assert result.adjusted_high_water_usd == Decimal("7000")
    assert result.drawdown_fraction == Decimal("0")


def test_trading_loss_reaches_fifty_percent_drawdown_without_cashflow():
    result = reconcile_equity_cashflows(
        equity_basis="account_total_equity_usd",
        previous_equity_usd="10000",
        current_equity_usd="5000",
        previous_adjusted_high_water_usd="10000",
        external_cashflows=(),
    )

    assert result.net_external_cashflow_usd == Decimal("0")
    assert result.non_cashflow_equity_change_usd == Decimal("-5000")
    assert result.adjusted_high_water_usd == Decimal("10000")
    assert result.drawdown_fraction == Decimal("0.5")


def test_trade_and_fee_bills_are_not_external_cashflows():
    rows = [
        _bill("trade", bill_type="2", subtype="1", change="4000"),
        _bill("fee", bill_type="2", subtype="2", change="-1"),
    ]

    assert classify_okx_external_cashflows(rows) == ()


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (
            _bill("unknown", bill_type="1", subtype="99", change="1"),
            "unknown_transfer_subtype",
        ),
        (
            _bill(
                "btc",
                bill_type="1",
                subtype="11",
                change="1",
                currency="BTC",
            ),
            "unsupported_transfer_currency",
        ),
        (
            _bill("wrong-sign", bill_type="1", subtype="12", change="1"),
            "invalid_transfer_sign",
        ),
    ],
)
def test_untrusted_transfer_classification_fails_closed(row, code):
    with pytest.raises(CashflowReconciliationFailure) as exc:
        classify_okx_external_cashflows([row])

    assert exc.value.code == code


def test_single_currency_usdt_balance_is_rejected_as_drawdown_basis():
    with pytest.raises(CashflowReconciliationFailure) as exc:
        reconcile_equity_cashflows(
            equity_basis="usdt_currency_equity",
            previous_equity_usd="4893",
            current_equity_usd="8899",
            previous_adjusted_high_water_usd="4893",
            external_cashflows=(),
        )

    assert exc.value.code == "invalid_equity_basis"


def test_previous_high_water_below_previous_equity_fails_closed():
    with pytest.raises(CashflowReconciliationFailure) as exc:
        reconcile_equity_cashflows(
            equity_basis="account_total_equity_usd",
            previous_equity_usd="10000",
            current_equity_usd="10000",
            previous_adjusted_high_water_usd="9000",
            external_cashflows=(),
        )

    assert exc.value.code == "invalid_high_water"
