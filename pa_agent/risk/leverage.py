"""Read-only OKX leverage planning from broker-reported capacity."""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Literal, Protocol

from pa_agent.execution.worker_protocol import SetLeverageParameters

_LEVERAGE_STEP = Decimal("0.01")


class LeveragePlanningFailure(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _CapacityClient(Protocol):
    def leverage_adjustment_info(self, **kwargs) -> dict: ...

    def max_order_size(self, **kwargs) -> dict: ...


def _positive(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LeveragePlanningFailure(
            "invalid_broker_value",
            f"{field} 不是有效数字",
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise LeveragePlanningFailure(
            "invalid_broker_value",
            f"{field} 必须是正数",
        )
    return parsed


def _capacity(
    client: _CapacityClient,
    *,
    instrument: str,
    direction: Literal["long", "short"],
    entry_price: Decimal,
    leverage: Decimal,
) -> Decimal:
    row = client.max_order_size(
        instrument=instrument,
        trade_mode="cross",
        price=str(entry_price),
        leverage=str(leverage),
    )
    field = "maxBuy" if direction == "long" else "maxSell"
    return _positive(row.get(field), field)


def build_minimum_leverage_parameters(
    *,
    client: _CapacityClient,
    analysis_digest: str,
    config_fingerprint: str,
    instrument: str,
    direction: Literal["long", "short"],
    current_leverage: object,
    required_quantity: object,
    entry_price: object,
    expected_account_identity: str,
    okx_api_base_url: str,
) -> SetLeverageParameters | None:
    """Return the lowest 0.01x leverage whose official capacity is sufficient."""
    current = _positive(current_leverage, "current_leverage")
    required = _positive(required_quantity, "required_quantity")
    price = _positive(entry_price, "entry_price")
    adjustment = client.leverage_adjustment_info(
        instrument_type="SWAP",
        margin_mode="cross",
        leverage=str(current),
        instrument=instrument,
        position_side="net",
    )
    if adjustment.get("existOrd") is not False:
        raise LeveragePlanningFailure(
            "pending_orders",
            "OKX 杠杆估算显示存在挂单",
        )
    maximum = _positive(adjustment.get("maxLever"), "maxLever")
    minimum = _positive(adjustment.get("minLever"), "minLever")
    if current < minimum or current > maximum:
        raise LeveragePlanningFailure(
            "current_leverage_out_of_range",
            "OKX 当前杠杆不在官方允许范围内",
        )

    cache: dict[Decimal, Decimal] = {}

    def quote(leverage: Decimal) -> Decimal:
        if leverage not in cache:
            cache[leverage] = _capacity(
                client,
                instrument=instrument,
                direction=direction,
                entry_price=price,
                leverage=leverage,
            )
        return cache[leverage]

    current_capacity = quote(current)
    if current_capacity >= required:
        return None
    current_units = int(
        (current / _LEVERAGE_STEP).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    maximum_units = int(
        (maximum / _LEVERAGE_STEP).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    maximum_capacity = quote(
        Decimal(maximum_units) * _LEVERAGE_STEP
    )
    if maximum_capacity < current_capacity:
        raise LeveragePlanningFailure(
            "non_monotonic_capacity",
            "OKX 候选杠杆容量不是单调增加, 禁止猜测最低杠杆",
        )
    if maximum_capacity < required:
        raise LeveragePlanningFailure(
            "max_leverage_capacity_insufficient",
            "OKX 最大允许杠杆仍不足以容纳风险目标张数",
        )

    low = current_units
    high = maximum_units
    while high - low > 1:
        middle = (low + high) // 2
        candidate = Decimal(middle) * _LEVERAGE_STEP
        if quote(candidate) >= required:
            high = middle
        else:
            low = middle
    target = Decimal(high) * _LEVERAGE_STEP
    previous = target - _LEVERAGE_STEP
    if previous > current and quote(previous) >= required:
        raise LeveragePlanningFailure(
            "non_monotonic_capacity",
            "OKX 杠杆容量不是可验证的单调结果",
        )
    target = target.quantize(_LEVERAGE_STEP, rounding=ROUND_CEILING)
    return SetLeverageParameters(
        analysis_digest=analysis_digest,
        config_fingerprint=config_fingerprint,
        instrument=instrument,
        direction=direction,
        margin_mode="cross",
        position_mode="net_mode",
        current_leverage=current,
        target_leverage=target,
        required_quantity=required,
        entry_price=price,
        expected_account_identity=expected_account_identity,
        okx_api_base_url=okx_api_base_url,
    )
