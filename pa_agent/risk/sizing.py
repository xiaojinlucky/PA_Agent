"""与券商品种无关的确定性止损风险定仓。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Literal


class RiskCalculationFailure(ValueError):
    """输入或约束不满足时，风险计算明确失败。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


@dataclass(frozen=True)
class RiskSizingResult:
    """一份新开仓风险快照。"""

    target_contract_size: Decimal
    risk_budget_usdt: Decimal
    risk_used_usdt: Decimal
    stop_distance_usdt: Decimal
    contract_notional_usdt: Decimal
    price_loss_per_contract_usdt: Decimal
    fee_per_contract_usdt: Decimal
    slippage_per_contract_usdt: Decimal
    worst_case_loss_per_contract_usdt: Decimal
    lot_size: Decimal
    minimum_size: Decimal
    maximum_size: Decimal


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise RiskCalculationFailure(
            "missing_input", f"{field_name} 缺失或不是数字"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RiskCalculationFailure(
            "invalid_input", f"{field_name} 不是有效数字"
        ) from exc
    if not parsed.is_finite():
        raise RiskCalculationFailure(
            "invalid_input", f"{field_name} 必须是有限数字"
        )
    return parsed


def _positive(value: object, field_name: str) -> Decimal:
    parsed = _decimal(value, field_name)
    if parsed <= 0:
        raise RiskCalculationFailure(
            "invalid_input", f"{field_name} 必须是正数"
        )
    return parsed


def _non_negative(value: object, field_name: str) -> Decimal:
    parsed = _decimal(value, field_name)
    if parsed < 0:
        raise RiskCalculationFailure(
            "invalid_input", f"{field_name} 不能为负数"
        )
    return parsed


def _floor_to_lot(value: Decimal, lot_size: Decimal) -> Decimal:
    return (value / lot_size).to_integral_value(rounding=ROUND_DOWN) * lot_size


def calculate_risk_size(
    *,
    account_equity: object,
    risk_percent: object,
    entry_price: object,
    stop_loss_price: object,
    side: Literal["long", "short"],
    ct_val: object,
    ct_mult: object,
    lot_sz: object,
    min_sz: object,
    max_sz: object,
    fee_rate: object,
    slippage_rate: object,
) -> RiskSizingResult:
    """按最坏止损损失计算合法合约数。

    `ctVal * ctMult` 是一张合约对应的基础标的数量。费用和滑点按入场、
    止损两次名义金额分别计入，因此同一组输入永远得到同一结果。
    """

    equity = _positive(account_equity, "account_equity")
    risk_fraction = _positive(risk_percent, "risk_percent")
    if risk_fraction > 1:
        raise RiskCalculationFailure(
            "invalid_input", "risk_percent 不能大于 1"
        )
    entry = _positive(entry_price, "entry_price")
    stop = _positive(stop_loss_price, "stop_loss_price")
    if not isinstance(side, str) or side not in {"long", "short"}:
        raise RiskCalculationFailure("invalid_side", "side 必须是 long 或 short")
    if side == "long" and not stop < entry:
        raise RiskCalculationFailure(
            "invalid_stop", "做多止损必须低于入场价"
        )
    if side == "short" and not stop > entry:
        raise RiskCalculationFailure(
            "invalid_stop", "做空止损必须高于入场价"
        )

    contract_value = _positive(ct_val, "ctVal") * _positive(ct_mult, "ctMult")
    lot_size = _positive(lot_sz, "lotSz")
    minimum_size = _positive(min_sz, "minSz")
    maximum_size = _positive(max_sz, "max_order_size")
    fee_fraction = _non_negative(fee_rate, "fee_rate")
    slippage_fraction = _non_negative(slippage_rate, "slippage_rate")
    if fee_fraction >= 1 or slippage_fraction >= 1:
        raise RiskCalculationFailure(
            "invalid_input", "fee_rate 和 slippage_rate 必须小于 1"
        )

    stop_distance = abs(entry - stop)
    price_loss = stop_distance * contract_value
    round_trip_notional = (entry + stop) * contract_value
    fee = round_trip_notional * fee_fraction
    slippage = round_trip_notional * slippage_fraction
    worst_case_loss = price_loss + fee + slippage
    if worst_case_loss <= 0:
        raise RiskCalculationFailure(
            "invalid_loss", "单张最坏止损损失必须是正数"
        )

    risk_budget = equity * risk_fraction
    risk_limited_size = _floor_to_lot(risk_budget / worst_case_loss, lot_size)
    broker_limited_size = _floor_to_lot(maximum_size, lot_size)
    if broker_limited_size < minimum_size:
        raise RiskCalculationFailure(
            "max_size_below_minimum",
            "OKX 最大可开数量低于最小下单数量",
        )
    if risk_limited_size < minimum_size:
        raise RiskCalculationFailure(
            "below_minimum",
            "按止损风险计算出的数量低于最小下单数量",
        )
    if risk_limited_size > broker_limited_size:
        raise RiskCalculationFailure(
            "max_size_exceeded",
            "按止损风险计算出的数量超过 OKX 当前最大可开数量",
        )
    target_size = risk_limited_size

    return RiskSizingResult(
        target_contract_size=target_size,
        risk_budget_usdt=risk_budget,
        risk_used_usdt=target_size * worst_case_loss,
        stop_distance_usdt=stop_distance,
        contract_notional_usdt=entry * contract_value,
        price_loss_per_contract_usdt=price_loss,
        fee_per_contract_usdt=fee,
        slippage_per_contract_usdt=slippage,
        worst_case_loss_per_contract_usdt=worst_case_loss,
        lot_size=lot_size,
        minimum_size=minimum_size,
        maximum_size=maximum_size,
    )
