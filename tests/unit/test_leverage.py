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
        "maximum_leverage_cap": "50",
    }
    kwargs.update(overrides)
    return build_minimum_leverage_parameters(**kwargs)


def test_planner_selects_first_sufficient_point_on_verified_policy_grid():
    client = _CapacityClient()

    parameters = _build(client)

    assert parameters.target_leverage == Decimal("25")
    assert parameters.required_quantity == Decimal("201")
    assert parameters.current_capacity == Decimal("200")
    assert parameters.target_capacity == Decimal("250")
    assert parameters.maximum_leverage == Decimal("50")
    assert parameters.maximum_capacity == Decimal("500")
    assert parameters.planning_method == "bounded_sequential_policy_grid_v2"
    assert parameters.policy_grid_step == Decimal("5")
    assert [
        (point.leverage, point.capacity)
        for point in parameters.verified_grid
    ] == [
        (Decimal(value), Decimal(value * 10))
        for value in range(20, 51, 5)
    ]
    assert len(parameters.leverage_intent_digest) == 64
    assert client.quoted == [
        Decimal(value) for value in range(20, 51, 5)
    ]


def test_planner_includes_exact_fractional_official_maximum_as_tail_point():
    client = _CapacityClient(max_leverage=Decimal("21.5"))

    parameters = _build(
        client,
        current_leverage="20.25",
        required_quantity="214",
    )

    assert parameters.target_leverage == Decimal("21.5")
    assert client.quoted == [
        Decimal("20.25"),
        Decimal("21.5"),
    ]


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
    assert client.quoted == [Decimal("20"), Decimal("25")]


def test_planner_refuses_exchange_reported_pending_orders():
    client = _CapacityClient(pending_orders=True)

    with pytest.raises(
        LeveragePlanningFailure,
        match="存在挂单",
    ) as caught:
        _build(client)

    assert caught.value.code == "pending_orders"


def test_planner_uses_first_sufficient_verified_point_when_capacity_is_non_monotonic():
    client = _CapacityClient(
        max_leverage=Decimal("30"),
        capacity_override={
            Decimal("20"): Decimal("200"),
            Decimal("25"): Decimal("260"),
            Decimal("30"): Decimal("250"),
        },
    )

    parameters = _build(client, required_quantity="201")

    assert parameters.target_leverage == Decimal("25")
    assert parameters.target_capacity == Decimal("260")
    assert client.quoted == [
        Decimal("20"),
        Decimal("25"),
        Decimal("30"),
    ]


def test_planner_never_reads_or_selects_above_user_maximum_leverage():
    client = _CapacityClient(max_leverage=Decimal("50"))

    parameters = _build(
        client,
        required_quantity="249",
        maximum_leverage_cap="25",
    )

    assert parameters.target_leverage == Decimal("25")
    assert parameters.maximum_leverage == Decimal("25")
    assert parameters.exchange_maximum_leverage == Decimal("50")
    assert parameters.user_maximum_leverage == Decimal("25")
    assert client.quoted == [Decimal("20"), Decimal("25")]


def test_planner_blocks_when_user_maximum_leverage_is_insufficient():
    client = _CapacityClient(max_leverage=Decimal("50"))

    with pytest.raises(
        LeveragePlanningFailure,
        match="用户设置的最大杠杆",
    ) as caught:
        _build(
            client,
            required_quantity="251",
            maximum_leverage_cap="25",
        )

    assert caught.value.code == "user_max_leverage_capacity_insufficient"
    assert client.quoted == [Decimal("20"), Decimal("25")]


def test_planner_accepts_exact_capacity_read_budget_boundary():
    client = _CapacityClient(
        capacity_per_leverage=Decimal("1"),
        max_leverage=Decimal("75"),
    )

    parameters = _build(
        client,
        current_leverage="1",
        required_quantity="75",
        maximum_leverage_cap="75",
    )

    assert parameters.target_leverage == Decimal("75")
    assert len(client.quoted) == 16
    assert client.quoted[0] == Decimal("1")
    assert client.quoted[-1] == Decimal("75")


def test_planner_blocks_before_exceeding_capacity_read_budget():
    client = _CapacityClient(
        capacity_per_leverage=Decimal("1"),
        max_leverage=Decimal("80"),
    )

    with pytest.raises(
        LeveragePlanningFailure,
        match="只读容量探测上限",
    ) as caught:
        _build(
            client,
            current_leverage="1",
            required_quantity="80",
            maximum_leverage_cap="80",
        )

    assert caught.value.code == "capacity_grid_exceeds_read_budget"
    assert client.quoted == [Decimal("1")]
