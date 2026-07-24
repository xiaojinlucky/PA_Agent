"""账户外部资金流与交易损益的确定性分离。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal


class CashflowReconciliationFailure(ValueError):
    """资金流水无法可信分类或权益口径不满足时明确失败。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


@dataclass(frozen=True)
class ExternalCashflow:
    """一条已确认的 USDT 外部资金变化。"""

    bill_id: str
    kind: Literal["transfer_in", "transfer_out"]
    currency: Literal["USDT"]
    amount_usdt: Decimal
    timestamp_ms: int
    bill_type: str
    bill_subtype: str


@dataclass(frozen=True)
class EquityCashflowReconciliation:
    """一次账户总权益快照之间的资金流校正结果。"""

    equity_basis: Literal["account_total_equity_usd"]
    previous_equity_usd: Decimal
    current_equity_usd: Decimal
    net_external_cashflow_usd: Decimal
    non_cashflow_equity_change_usd: Decimal
    flow_adjusted_prior_high_water_usd: Decimal
    adjusted_high_water_usd: Decimal
    drawdown_usd: Decimal
    drawdown_fraction: Decimal
    last_external_cashflow_bill_id: str


def _finite_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CashflowReconciliationFailure(
            "invalid_input",
            f"{field_name} 缺失或不是数字",
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CashflowReconciliationFailure(
            "invalid_input",
            f"{field_name} 不是有效数字",
        ) from exc
    if not parsed.is_finite():
        raise CashflowReconciliationFailure(
            "invalid_input",
            f"{field_name} 必须是有限数字",
        )
    return parsed


def classify_okx_external_cashflows(
    rows: Iterable[dict[str, object]],
) -> tuple[ExternalCashflow, ...]:
    """从 OKX 交易账户账单中只提取官方定义的转入和转出。

    OKX `type=1` 是 Transfer, `subType=11/12` 分别是转入和转出。
    其他账单属于成交、手续费、资金费等账户活动, 不能当作外部入金。
    非 USDT 划转缺少事件时点的可靠换算价, 本阶段直接失败。
    """

    events: list[ExternalCashflow] = []
    seen_bill_ids: set[str] = set()
    for row in rows:
        bill_id = str(row.get("billId") or "").strip()
        if not bill_id or bill_id in seen_bill_ids:
            raise CashflowReconciliationFailure(
                "invalid_bill_id",
                "OKX 资金账单 ID 缺失或重复",
            )
        seen_bill_ids.add(bill_id)
        bill_type = str(row.get("type") or "").strip()
        bill_subtype = str(row.get("subType") or "").strip()
        if bill_type != "1":
            continue
        if bill_subtype not in {"11", "12"}:
            raise CashflowReconciliationFailure(
                "unknown_transfer_subtype",
                f"OKX 转账账单子类型未识别: {bill_subtype or '<empty>'}",
            )
        currency = str(row.get("ccy") or "").strip().upper()
        if currency != "USDT":
            raise CashflowReconciliationFailure(
                "unsupported_transfer_currency",
                f"暂不支持将 {currency or '<empty>'} 划转换算为 USDT",
            )
        timestamp_text = str(row.get("ts") or "").strip()
        if not timestamp_text.isdigit():
            raise CashflowReconciliationFailure(
                "invalid_timestamp",
                "OKX 转账账单时间无效",
            )
        amount = _finite_decimal(row.get("balChg"), "balChg")
        if bill_subtype == "11" and amount <= 0:
            raise CashflowReconciliationFailure(
                "invalid_transfer_sign",
                "OKX 转入账单金额必须为正数",
            )
        if bill_subtype == "12" and amount >= 0:
            raise CashflowReconciliationFailure(
                "invalid_transfer_sign",
                "OKX 转出账单金额必须为负数",
            )
        events.append(
            ExternalCashflow(
                bill_id=bill_id,
                kind="transfer_in" if bill_subtype == "11" else "transfer_out",
                currency="USDT",
                amount_usdt=amount,
                timestamp_ms=int(timestamp_text),
                bill_type=bill_type,
                bill_subtype=bill_subtype,
            )
        )
    return tuple(
        sorted(events, key=lambda item: (item.timestamp_ms, item.bill_id))
    )


def reconcile_equity_cashflows(
    *,
    equity_basis: str,
    previous_equity_usd: object,
    current_equity_usd: object,
    previous_adjusted_high_water_usd: object,
    external_cashflows: Iterable[ExternalCashflow],
) -> EquityCashflowReconciliation:
    """把外部资金变化从账户总权益变化中剥离, 并调整历史高水位。"""

    if equity_basis != "account_total_equity_usd":
        raise CashflowReconciliationFailure(
            "invalid_equity_basis",
            "资金流校正必须使用账户总权益, 不能使用单一 USDT 余额",
        )
    previous_equity = _finite_decimal(
        previous_equity_usd,
        "previous_equity_usd",
    )
    current_equity = _finite_decimal(
        current_equity_usd,
        "current_equity_usd",
    )
    previous_high_water = _finite_decimal(
        previous_adjusted_high_water_usd,
        "previous_adjusted_high_water_usd",
    )
    if previous_equity < 0 or current_equity < 0 or previous_high_water < 0:
        raise CashflowReconciliationFailure(
            "invalid_input",
            "权益和历史高水位不能为负数",
        )
    if previous_high_water < previous_equity:
        raise CashflowReconciliationFailure(
            "invalid_high_water",
            "历史高水位不能低于上一份账户总权益",
        )

    events = tuple(external_cashflows)
    net_cashflow = sum(
        (event.amount_usdt for event in events),
        start=Decimal("0"),
    )
    flow_adjusted_high_water = previous_high_water + net_cashflow
    if flow_adjusted_high_water < 0:
        raise CashflowReconciliationFailure(
            "cashflow_exceeds_high_water",
            "净转出超过历史高水位, 不能自动校正",
        )
    adjusted_high_water = max(flow_adjusted_high_water, current_equity)
    drawdown = max(adjusted_high_water - current_equity, Decimal("0"))
    drawdown_fraction = (
        drawdown / adjusted_high_water
        if adjusted_high_water > 0
        else Decimal("0")
    )
    return EquityCashflowReconciliation(
        equity_basis="account_total_equity_usd",
        previous_equity_usd=previous_equity,
        current_equity_usd=current_equity,
        net_external_cashflow_usd=net_cashflow,
        non_cashflow_equity_change_usd=(
            current_equity - previous_equity - net_cashflow
        ),
        flow_adjusted_prior_high_water_usd=flow_adjusted_high_water,
        adjusted_high_water_usd=adjusted_high_water,
        drawdown_usd=drawdown,
        drawdown_fraction=drawdown_fraction,
        last_external_cashflow_bill_id=events[-1].bill_id if events else "",
    )
