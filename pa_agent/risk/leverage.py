"""Read-only OKX leverage planning from broker-reported capacity."""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Literal, Protocol

from pa_agent.execution.worker_protocol import (
    LeverageCapacityPoint,
    SetLeverageParameters,
)

# 这是 PA_Agent 主动声明的粗粒度策略网格, 不是 OKX 最小杠杆档位。
# 每个候选值都必须由 OKX max-size 端点逐点验证, 最后另含官方 maxLever。
_POLICY_GRID_STEP = Decimal("5")
_MAX_CAPACITY_READS = 16


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


def _policy_candidate_grid(
    current: Decimal,
    maximum: Decimal,
) -> tuple[Decimal, ...]:
    """构造策略声明的候选网格, 不把它冒充成交易所最小档位。"""
    candidates = [current]
    next_grid_point = (
        current / _POLICY_GRID_STEP
    ).to_integral_value(rounding=ROUND_CEILING) * _POLICY_GRID_STEP
    if next_grid_point <= current:
        next_grid_point += _POLICY_GRID_STEP
    while next_grid_point < maximum:
        candidates.append(next_grid_point)
        next_grid_point += _POLICY_GRID_STEP
    if maximum != current:
        candidates.append(maximum)
    return tuple(candidates)


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
    maximum_leverage_cap: object,
) -> SetLeverageParameters | None:
    """返回有界策略网格中第一个经过逐点验证且容量足够的杠杆。"""
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
    exchange_maximum = _positive(adjustment.get("maxLever"), "maxLever")
    user_maximum = _positive(
        maximum_leverage_cap,
        "maximum_leverage_cap",
    )
    maximum = min(exchange_maximum, user_maximum)
    minimum = _positive(adjustment.get("minLever"), "minLever")
    if current < minimum or current > exchange_maximum:
        raise LeveragePlanningFailure(
            "current_leverage_out_of_range",
            "OKX 当前杠杆不在官方允许范围内",
        )
    if current > user_maximum:
        raise LeveragePlanningFailure(
            "current_leverage_above_user_cap",
            "OKX 当前杠杆已经高于用户设置的最大杠杆，禁止新增风险",
        )

    current_capacity = _capacity(
        client,
        instrument=instrument,
        direction=direction,
        entry_price=price,
        leverage=current,
    )
    if current_capacity >= required:
        return None

    candidates = _policy_candidate_grid(current, maximum)
    if len(candidates) > _MAX_CAPACITY_READS:
        raise LeveragePlanningFailure(
            "capacity_grid_exceeds_read_budget",
            "候选杠杆网格超过只读容量探测上限, 无法在有界请求内证明",
        )

    first_sufficient: Decimal | None = None
    target_capacity: Decimal | None = None
    maximum_capacity = current_capacity
    verified_grid = [
        LeverageCapacityPoint(
            leverage=current,
            capacity=current_capacity,
        )
    ]
    for candidate in candidates[1:]:
        candidate_capacity = _capacity(
            client,
            instrument=instrument,
            direction=direction,
            entry_price=price,
            leverage=candidate,
        )
        verified_grid.append(
            LeverageCapacityPoint(
                leverage=candidate,
                capacity=candidate_capacity,
            )
        )
        if first_sufficient is None and candidate_capacity >= required:
            first_sufficient = candidate
            target_capacity = candidate_capacity
        maximum_capacity = candidate_capacity

    if first_sufficient is None:
        if user_maximum < exchange_maximum:
            raise LeveragePlanningFailure(
                "user_max_leverage_capacity_insufficient",
                "用户设置的最大杠杆仍不足以容纳风险目标张数",
            )
        raise LeveragePlanningFailure(
            "max_leverage_capacity_insufficient",
            "OKX 最大允许杠杆仍不足以容纳风险目标张数",
        )
    if target_capacity is None:
        raise LeveragePlanningFailure(
            "capacity_proof_incomplete",
            "候选网格没有形成可审计的足够容量点",
        )
    return SetLeverageParameters(
        analysis_digest=analysis_digest,
        config_fingerprint=config_fingerprint,
        instrument=instrument,
        direction=direction,
        margin_mode="cross",
        position_mode="net_mode",
        current_leverage=current,
        target_leverage=first_sufficient,
        current_capacity=current_capacity,
        target_capacity=target_capacity,
        maximum_leverage=maximum,
        exchange_maximum_leverage=exchange_maximum,
        user_maximum_leverage=user_maximum,
        maximum_capacity=maximum_capacity,
        planning_method="bounded_sequential_policy_grid_v2",
        policy_grid_step=_POLICY_GRID_STEP,
        verified_grid=tuple(verified_grid),
        required_quantity=required,
        entry_price=price,
        expected_account_identity=expected_account_identity,
        okx_api_base_url=okx_api_base_url,
    )
