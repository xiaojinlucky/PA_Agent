from __future__ import annotations

from decimal import Decimal

import pytest

from pa_agent.risk.leverage import (
    LeveragePlanningFailure,
    build_minimum_leverage_parameters,
)


class _CapacityClient:
    def __init__(
        self,
        *,
        capacity_per_leverage: Decimal = Decimal("10"),
        max_leverage: Decimal = Decimal("50"),
        pending_orders: bool = False,
        capacity_override: dict[Decimal, Decimal] | None = None,
    ) -> None:
        self.capacity_per_leverage = capacity_per_leverage
        self.max_leverage = max_leverage
        self.pending_orders = pending_orders
        self.capacity_override = capacity_override or {}
        self.quoted: list[Decimal] = []

    def leverage_adjustment_info(self, **kwargs) -> dict:
        return {
            "estMaxAmt": str(
                Decimal(str(kwargs["leverage"]))
                * self.capacity_per_leverage
            ),
            "existOrd": self.pending_orders,
            "maxLever": str(self.max_leverage),
            "minLever": "0.01",
        }

    def max_order_size(self, **kwargs) -> dict:
        leverage = Decimal(str(kwargs["leverage"]))
        self.quoted.append(leverage)
        capacity = self.capacity_override.get(
            leverage,
            leverage * self.capacity_per_leverage,
        )
        return {
            "maxBuy": str(capacity),
            "maxSell": str(capacity),
        }


def _build(client, **overrides):
    kwargs = {
        "client": client,
        "analysis_digest": "a" * 64,
        "config_fingerprint": "config",
        "instrument": "XAU-USDT-SWAP",
        "direction": "long",
        "current_leverage": "20",
        "required_quantity": "201",
        "entry_price": "4000",
        "expected_account_identity": "b" * 64,
        "okx_api_base_url": "https://www.okx.com",
    }
    kwargs.update(overrides)
    return build_minimum_leverage_parameters(**kwargs)


def test_planner_selects_lowest_official_point_zero_one_leverage():
    client = _CapacityClient()

    parameters = _build(client)

    assert parameters.target_leverage == Decimal("20.10")
    assert parameters.required_quantity == Decimal("201")
    assert Decimal("20.09") in client.quoted
    assert Decimal("20.10") in client.quoted


def test_planner_returns_none_when_current_capacity_is_already_sufficient():
    client = _CapacityClient()

    parameters = _build(client, required_quantity="200")

    assert parameters is None
    assert client.quoted == [Decimal("20")]


def test_planner_fails_when_official_maximum_leverage_is_insufficient():
    client = _CapacityClient(
        capacity_per_leverage=Decimal("1"),
        max_leverage=Decimal("25"),
    )

    with pytest.raises(
        LeveragePlanningFailure,
        match="最大允许杠杆",
    ) as caught:
        _build(client, required_quantity="30")

    assert caught.value.code == "max_leverage_capacity_insufficient"


def test_planner_refuses_exchange_reported_pending_orders():
    client = _CapacityClient(pending_orders=True)

    with pytest.raises(
        LeveragePlanningFailure,
        match="存在挂单",
    ) as caught:
        _build(client)

    assert caught.value.code == "pending_orders"


def test_planner_refuses_non_monotonic_broker_capacity_curve():
    client = _CapacityClient(
        max_leverage=Decimal("50"),
        capacity_override={
            Decimal("20"): Decimal("120000"),
            Decimal("50.00"): Decimal("28000"),
        },
    )

    with pytest.raises(
        LeveragePlanningFailure,
        match="不是单调增加",
    ) as caught:
        _build(client, required_quantity="130000")

    assert caught.value.code == "non_monotonic_capacity"
