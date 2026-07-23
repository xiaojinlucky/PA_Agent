"""Longbridge paper/comprehensive/intraday lifecycle and safe fallback."""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pa_agent.data.longbridge_source import normalize_longbridge_symbol
from pa_agent.execution.errors import (
    BrokerApiError,
    BrokerRejected,
    BrokerTransportError,
    FallbackEligiblePreflightError,
    PreflightError,
    ReconciliationError,
    SubmissionUnknown,
)
from pa_agent.execution.models import (
    AccountSnapshot,
    ExecutionPlan,
    ExecutionRecord,
    ExecutionState,
    PositionSnapshot,
    PreflightResult,
    utc_now_iso,
)
from pa_agent.execution.order_modes import (
    apply_entry_atr_slippage,
    apply_exit_atr_slippage,
)


def _decimal(value: object, default: Decimal | None = None) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _aggregate_executions(
    executions: list[dict[str, Any]],
) -> tuple[Decimal, Decimal | None]:
    """Aggregate exact fills without treating a missing price as zero."""
    anonymous: list[tuple[Decimal, Decimal | None]] = []
    by_trade_id: dict[str, tuple[Decimal, Decimal | None]] = {}
    for item in executions:
        raw_quantity = item.get("quantity")
        quantity = _decimal(raw_quantity)
        if raw_quantity in (None, "") or quantity is None or quantity < 0:
            raise ReconciliationError("Longbridge 成交明细包含无效数量")
        if quantity == 0:
            continue
        price = _decimal(item.get("price"))
        if price is not None and price <= 0:
            price = None
        trade_id = str(item.get("trade_id") or "")
        if not trade_id:
            anonymous.append((quantity, price))
            continue
        existing = by_trade_id.get(trade_id)
        if existing is None:
            by_trade_id[trade_id] = (quantity, price)
            continue
        existing_quantity, existing_price = existing
        if existing_quantity != quantity:
            raise ReconciliationError("Longbridge 重复成交编号的数量不一致")
        if (
            existing_price is not None
            and price is not None
            and existing_price != price
        ):
            raise ReconciliationError("Longbridge 重复成交编号的价格不一致")
        by_trade_id[trade_id] = (
            quantity,
            existing_price if existing_price is not None else price,
        )
    unique = [*by_trade_id.values(), *anonymous]
    total = sum((quantity for quantity, _price in unique), Decimal("0"))
    complete_prices = total > 0 and all(
        price is not None for _quantity, price in unique
    )
    if not complete_prices:
        return total, None
    notional = sum(
        (
            quantity * price
            for quantity, price in unique
            if price is not None
        ),
        Decimal("0"),
    )
    return total, notional / total


def _request_id(execution_id: str, action: str, index: int = 0) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pa-agent:{execution_id}:{action}:{index}"))


def _remark(execution_id: str, action: str, index: int = 0) -> str:
    compact = execution_id.replace("-", "")[:32]
    return f"PA:{compact}:{action}:{index}"[:64]


def _entry_side(plan: ExecutionPlan) -> str:
    return "buy" if plan.direction == "long" else "sell"


def _exit_side(plan: ExecutionPlan) -> str:
    return "sell" if plan.direction == "long" else "buy"


class LongbridgeAdapter:
    def __init__(
        self,
        session_factory: Callable[[str], Any],
        *,
        allow_outside_rth: bool = False,
        entry_timeout_seconds: int = 120,
        runtime_id: str = "",
        write_executor: Callable[[Callable[[], Any]], Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._sessions: dict[str, Any] = {}
        self._allow_outside_rth = bool(allow_outside_rth)
        self._entry_timeout_seconds = int(entry_timeout_seconds)
        self._runtime_id = runtime_id or str(uuid.uuid4())
        self._write_executor = write_executor or (lambda operation: operation())

    def bind_runtime_id(self, runtime_id: str) -> None:
        self._runtime_id = runtime_id

    def bind_write_executor(
        self,
        executor: Callable[[Callable[[], Any]], Any],
    ) -> None:
        self._write_executor = executor

    def _write(self, operation: Callable[[], Any]) -> Any:
        return self._write_executor(operation)

    @staticmethod
    def _available_exit_quantity(
        record: ExecutionRecord,
        session: Any,
    ) -> Decimal:
        """Read the broker position immediately before an exit-side write."""
        rows = session.positions(record.plan.instrument)
        total = Decimal("0")
        matched = False
        expected_symbol = record.plan.instrument.strip().upper()
        for item in rows:
            if str(item.get("symbol") or "").strip().upper() != expected_symbol:
                continue
            quantity = _decimal(item.get("quantity"))
            available = _decimal(item.get("available_quantity"))
            if quantity is None or available is None:
                raise ReconciliationError(
                    "Longbridge 持仓缺少可用数量，禁止发送离场方向订单"
                )
            if available < 0:
                raise ReconciliationError(
                    "Longbridge 持仓可用数量为负数，禁止发送离场方向订单"
                )
            direction_matches = (
                quantity > 0
                if record.plan.direction == "long"
                else quantity < 0
            )
            if not direction_matches:
                continue
            matched = True
            total += min(abs(quantity), available)
        return total if matched else Decimal("0")

    def _session(self, profile: str):
        session = self._sessions.get(profile)
        if session is None:
            session = self._session_factory(profile)
            self._sessions[profile] = session
        return session

    @staticmethod
    def _capacity(value: object, label: str) -> Decimal:
        parsed = _decimal(value)
        if parsed is None or parsed < 0:
            raise PreflightError(f"Longbridge {label} 返回值无效")
        return parsed

    @staticmethod
    def _max_quantity(estimate: dict[str, Any]) -> Decimal:
        cash = LongbridgeAdapter._capacity(
            estimate.get("cash_max_qty"),
            "cash_max_qty",
        )
        margin = LongbridgeAdapter._capacity(
            estimate.get("margin_max_qty"),
            "margin_max_qty",
        )
        return max(cash, margin)

    def account_identity(
        self,
        plan: ExecutionPlan,
        *,
        account_profile: str | None = None,
    ) -> str:
        profile = account_profile or plan.requested_account
        identity = str(
            getattr(self._session(profile), "account_identity", "") or ""
        )
        if not identity:
            raise PreflightError(
                f"Longbridge {profile} 账户缺少可验证的实际账户身份"
            )
        return identity

    def _profile_preflight(
        self,
        plan: ExecutionPlan,
        profile: str,
    ) -> PreflightResult:
        session = self._session(profile)
        static = session.static_info(plan.instrument)
        if not static:
            raise PreflightError(
                f"Longbridge {profile} 账户找不到品种 {plan.instrument}"
            )
        lot_size = _decimal(static.get("lot_size"))
        if lot_size is None or lot_size <= 0:
            raise PreflightError(f"Longbridge {plan.instrument} lot_size 无效")
        if plan.quantity % lot_size != 0:
            raise PreflightError(
                f"Longbridge 数量 {plan.quantity} 必须为每手 {lot_size} 的整数倍"
            )
        existing = [
            item
            for item in session.positions(plan.instrument)
            if (_decimal(item.get("quantity"), Decimal("0")) or Decimal("0")) != 0
        ]
        if existing:
            raise PreflightError(
                f"Longbridge {profile} 账户已有 {plan.instrument} 持仓；"
                "PA 当前不与既有仓位合并"
            )
        effective_entry_price = plan.entry_price
        if plan.entry_order_mode == "limit_with_slippage":
            try:
                effective_entry_price = apply_entry_atr_slippage(
                    plan.entry_price,
                    plan.direction,
                    plan.entry_atr,
                    plan.entry_slippage_atr_multiple,
                )
            except ValueError as exc:
                raise PreflightError(f"Longbridge 入场滑点配置无效：{exc}") from exc
        estimate = session.estimate_max_quantity(
            symbol=plan.instrument,
            side=_entry_side(plan),
            price=effective_entry_price,
        )
        maximum = self._max_quantity(estimate)
        if maximum < plan.quantity:
            raise FallbackEligiblePreflightError(
                f"Longbridge {profile} 可交易数量 {maximum} "
                f"小于计划数量 {plan.quantity}"
            )
        return PreflightResult(
            selected_account=profile,
            account_identity=self.account_identity(
                plan,
                account_profile=profile,
            ),
            quantity=plan.quantity,
            entry_price=effective_entry_price,
            take_profit_1=plan.take_profit_1,
            take_profit_2=plan.take_profit_2,
            stop_loss=plan.stop_loss,
            quantity_step=lot_size,
            minimum_quantity=lot_size,
            broker_metadata={
                "currency": str(static.get("currency") or ""),
                "lot_size": str(lot_size),
                "max_quantity": str(maximum),
                "outside_rth": self._allow_outside_rth,
                "entry_order_mode": plan.entry_order_mode,
                "entry_atr": str(plan.entry_atr) if plan.entry_atr is not None else "",
                "entry_slippage_atr_multiple": str(
                    plan.entry_slippage_atr_multiple
                ),
                "requested_entry_price": str(plan.entry_price),
                "effective_entry_price": str(effective_entry_price),
            },
        )

    def preflight(self, plan: ExecutionPlan) -> PreflightResult:
        try:
            normalize_longbridge_symbol(plan.instrument)
        except ValueError as exc:
            raise PreflightError(f"Longbridge 品种格式无效：{plan.instrument}") from exc
        preferred = plan.requested_account
        if preferred == "paper" and self._allow_outside_rth:
            raise PreflightError("Longbridge 模拟账户不支持美股盘前/盘后交易")
        try:
            return self._profile_preflight(plan, preferred)
        except FallbackEligiblePreflightError as preferred_error:
            if preferred != "intraday" or not plan.allow_account_fallback:
                raise
            # Only deterministic capacity/availability failures reach here.
            # Network/auth errors use Broker* errors and are never caught for fallback.
            try:
                fallback = self._profile_preflight(plan, "comprehensive")
            except PreflightError as fallback_error:
                raise PreflightError(
                    f"日内账户不可执行（{preferred_error}）；综合账户也不可执行"
                    f"（{fallback_error}）"
                ) from fallback_error
            return fallback.model_copy(
                update={
                    "warnings": [
                        f"日内账户预检未通过，提交前回退综合账户：{preferred_error}"
                    ]
                }
            )

    def _entry_body(self, record: ExecutionRecord) -> tuple[dict[str, Any], str, str]:
        preflight = record.preflight
        if preflight is None:
            raise PreflightError("Longbridge 提交前缺少预检结果")
        plan = record.plan
        request_id = _request_id(record.id, "entry")
        remark = _remark(record.id, "entry")
        body: dict[str, Any] = {
            "symbol": plan.instrument,
            "side": "Buy" if plan.direction == "long" else "Sell",
            "submitted_quantity": str(preflight.quantity),
            "time_in_force": "Day",
            "remark": remark,
            "client_request_id": request_id,
        }
        if plan.entry_type == "limit":
            body.update(
                {
                    "order_type": "LO",
                    "submitted_price": str(preflight.entry_price),
                }
            )
        elif plan.entry_type == "market":
            body["order_type"] = "MO"
        else:
            body.update(
                {
                    "order_type": "MIT",
                    "trigger_price": str(preflight.entry_price),
                }
            )
        if self._allow_outside_rth and plan.instrument.endswith(".US"):
            body["outside_rth"] = "ANY_TIME"
        return body, request_id, remark

    def submit_entry(self, record: ExecutionRecord) -> ExecutionRecord:
        if record.preflight is None:
            raise PreflightError("Longbridge 提交前缺少预检结果")
        profile = record.preflight.selected_account
        body, request_id, remark = self._entry_body(record)
        request_id = record.client_order_id or request_id
        remark = str(record.broker_state.get("entry_remark") or remark)
        body["client_request_id"] = request_id
        body["remark"] = remark
        try:
            order_id = self._write(
                lambda: self._session(profile).submit_order(body)
            )
        except BrokerApiError as exc:
            raise BrokerRejected(str(exc)) from exc
        except BrokerTransportError as exc:
            if exc.write_may_have_reached:
                raise SubmissionUnknown(str(exc)) from exc
            raise
        state = dict(record.broker_state)
        return record.model_copy(
            update={
                "state": ExecutionState.ENTRY_PENDING,
                "selected_account": profile,
                "client_order_id": request_id,
                "broker_order_id": order_id,
                "broker_state": state,
                "state_reason": f"Longbridge {profile} 入场请求已受理",
                "needs_attention": False,
                "last_error": "",
            }
        )

    def prepare_submit(self, record: ExecutionRecord) -> ExecutionRecord:
        """Persist account, request ID and remark before calling submit."""
        if record.preflight is None:
            raise PreflightError("Longbridge 提交前缺少预检结果")
        _, request_id, remark = self._entry_body(record)
        state = {
            **record.broker_state,
            "entry_remark": remark,
            "entry_request_id": request_id,
            "entry_cancel_requested": False,
            "entry_submitted_at": utc_now_iso(),
            "stop_order": {},
            "partial_exit": {},
            "take_profit_completed": [],
        }
        return record.model_copy(
            update={
                "state": ExecutionState.SUBMITTING,
                "selected_account": record.preflight.selected_account,
                "client_order_id": request_id,
                "broker_state": state,
                "state_reason": "准备提交 Longbridge 入场订单",
            }
        )

    def _cancel_entry_write(self, record: ExecutionRecord) -> ExecutionRecord:
        try:
            self._write(
                lambda: self._session(record.selected_account).cancel_order(
                    record.broker_order_id
                )
            )
        except BrokerApiError as exc:
            raise BrokerRejected(f"Longbridge 撤销入场失败：{exc}") from exc
        except BrokerTransportError as exc:
            state = dict(record.broker_state)
            state["entry_cancel_requested"] = True
            state["entry_cancel_status"] = "unknown"
            state["write_unknown"] = "cancel_entry"
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "Longbridge 撤销入场状态不明，保持只读对账",
                }
            )
        state = dict(record.broker_state)
        state["entry_cancel_requested"] = True
        state["entry_cancel_status"] = "submitted"
        state.pop("entry_cancel_runtime_id", None)
        return record.model_copy(
            update={
                "broker_state": state,
                "state_reason": "已请求撤销 Longbridge 未成交入场数量",
            }
        )

    def _initialise_stop(self, record: ExecutionRecord) -> ExecutionRecord:
        if record.remaining_quantity <= 0:
            return record.model_copy(
                update={
                    "state": ExecutionState.CLOSED,
                    "state_reason": "Longbridge 仓位数量为零",
                }
            )
        state = dict(record.broker_state)
        state["stop_order"] = {
            "order_id": "",
            "request_id": _request_id(record.id, "stop", record.revision + 1),
            "remark": _remark(record.id, "stop", record.revision + 1),
            "quantity": str(record.remaining_quantity),
            "trigger_price": str(record.preflight.stop_loss if record.preflight else record.plan.stop_loss),
            "state": "submitting",
            "submit_runtime_id": self._runtime_id,
        }
        return record.model_copy(
            update={
                "state": ExecutionState.PROTECTING,
                "broker_state": state,
                "state_reason": "准备建立 Longbridge 券商端止损",
            }
        )

    def _place_stop(self, record: ExecutionRecord) -> ExecutionRecord:
        stop = dict(record.broker_state.get("stop_order") or {})
        if stop.get("order_id"):
            return record.model_copy(
                update={
                    "state": ExecutionState.OPEN,
                    "state_reason": "Longbridge 仓位已成交且止损已建立",
                    "needs_attention": False,
                }
            )
        if stop.get("state") == "unknown":
            try:
                found = self._session(record.selected_account).find_order_by_remark(
                    symbol=record.plan.instrument,
                    remark=str(stop.get("remark") or ""),
                    start_at=self._record_created_at(record),
                )
            except (BrokerApiError, BrokerTransportError) as exc:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": "Longbridge 止损提交状态未知，正在只读对账",
                    }
                )
            if not found:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": (
                            "Longbridge 止损提交状态未知；未查到原订单，禁止自动重发"
                        ),
                    }
                )
            state = dict(record.broker_state)
            recovered_order_id = str(found.get("order_id") or "")
            if not recovered_order_id:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": (
                            "Longbridge 止损对账结果缺少订单号，保持停写"
                        ),
                    }
                )
            stop["order_id"] = recovered_order_id
            stop["state"] = str(found.get("state") or "pending")
            stop.pop("submit_runtime_id", None)
            state["stop_order"] = stop
            state.pop("write_unknown", None)
            return record.model_copy(
                update={
                    "state": ExecutionState.OPEN,
                    "broker_state": state,
                    "needs_attention": False,
                    "last_error": "",
                    "state_reason": "已按备注恢复 Longbridge 止损订单",
                }
            )
        if stop.get("state") == "planned":
            state = dict(record.broker_state)
            stop["state"] = "submitting"
            stop["submit_runtime_id"] = self._runtime_id
            state["stop_order"] = stop
            return record.model_copy(
                update={
                    "broker_state": state,
                    "state_reason": "已持久化 Longbridge 止损提交中状态",
                }
            )
        if (
            stop.get("state") == "submitting"
            and stop.get("submit_runtime_id") != self._runtime_id
        ):
            state = dict(record.broker_state)
            stop["state"] = "unknown"
            state["stop_order"] = stop
            state["write_unknown"] = "stop"
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": True,
                    "state_reason": (
                        "Longbridge 止损提交进程中断，按备注只读对账并禁止重发"
                    ),
                }
            )
        try:
            broker_available = self._available_exit_quantity(
                record,
                self._session(record.selected_account),
            )
        except (
            BrokerApiError,
            BrokerTransportError,
            ReconciliationError,
        ) as exc:
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": (
                        "无法核实 Longbridge 实际可用持仓，禁止提交止损"
                    ),
                }
            )
        desired_quantity = min(
            Decimal(str(stop["quantity"])),
            record.remaining_quantity,
            broker_available,
        )
        if desired_quantity <= 0:
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "state_reason": (
                        "Longbridge 券商端没有可保护的同向可用持仓，"
                        "禁止提交可能反向开仓的止损"
                    ),
                }
            )
        verified_quantity = str(desired_quantity)
        if str(stop.get("quantity") or "") != verified_quantity:
            state = dict(record.broker_state)
            stop["quantity"] = verified_quantity
            stop["position_quantity_mismatch"] = (
                desired_quantity != record.remaining_quantity
            )
            state["stop_order"] = stop
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": (
                        desired_quantity != record.remaining_quantity
                    ),
                    "state_reason": (
                        "已按券商实际可用持仓校准 Longbridge 止损数量，"
                        "等待下一轮再次核实后提交"
                    ),
                }
            )
        body = {
            "symbol": record.plan.instrument,
            "side": "Sell" if record.plan.direction == "long" else "Buy",
            "order_type": "MIT",
            "submitted_quantity": str(stop["quantity"]),
            "trigger_price": str(stop["trigger_price"]),
            "time_in_force": "GTC",
            "remark": str(stop["remark"]),
            "client_request_id": str(stop["request_id"]),
        }
        try:
            order_id = self._write(
                lambda: self._session(record.selected_account).submit_order(body)
            )
        except BrokerApiError as exc:
            raise BrokerRejected(f"Longbridge 止损单被拒绝：{exc}") from exc
        except BrokerTransportError as exc:
            state = dict(record.broker_state)
            stop["state"] = "unknown"
            state["stop_order"] = stop
            state["write_unknown"] = "stop"
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": (
                        "Longbridge 止损提交状态未知，已停写并禁止自动重发"
                    ),
                }
            )
        state = dict(record.broker_state)
        stop["order_id"] = order_id
        stop["state"] = "pending"
        stop.pop("submit_runtime_id", None)
        state["stop_order"] = stop
        quantity_mismatch = bool(stop.get("position_quantity_mismatch"))
        return record.model_copy(
            update={
                "state": ExecutionState.OPEN,
                "broker_state": state,
                "state_reason": (
                    "Longbridge 止损已按券商实际可用持仓建立；"
                    "本地仓位数量与券商不一致，请核对"
                    if quantity_mismatch
                    else "Longbridge 仓位已成交且止损已建立"
                ),
                "needs_attention": quantity_mismatch,
            }
        )

    def _entry_reconcile(
        self,
        record: ExecutionRecord,
        *,
        allow_writes: bool,
    ) -> ExecutionRecord:
        try:
            order = self._session(record.selected_account).order(record.broker_order_id)
        except (BrokerApiError, BrokerTransportError) as exc:
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "Longbridge 入场订单暂时无法查询",
                }
            )
        status = str(order.get("state") or "unknown")
        filled = _decimal(order.get("filled_quantity"), Decimal("0")) or Decimal("0")
        avg = _decimal(order.get("average_fill_price"))
        settled_state = dict(record.broker_state)
        if status in {"rejected", "canceled", "filled"}:
            if settled_state.get("write_unknown") == "cancel_entry":
                settled_state.pop("write_unknown", None)
            settled_state.pop("entry_cancel_status", None)
            try:
                filled, avg = self._confirmed_terminal_fill(
                    record,
                    session=self._session(record.selected_account),
                    order_id=record.broker_order_id,
                    order=order,
                    maximum_quantity=record.plan.quantity,
                )
            except ReconciliationError as exc:
                state_data = dict(settled_state)
                state_data["entry_fill_quantity_unknown"] = True
                state_data["write_unknown"] = "entry_fill_quantity"
                return record.model_copy(
                    update={
                        "broker_state": state_data,
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": (
                            "Longbridge 入场终态成交数量未确认，"
                            "禁止结束监控或建立保护"
                        ),
                    }
                )
            settled_state.pop("entry_fill_quantity_unknown", None)
            if settled_state.get("write_unknown") == "entry_fill_quantity":
                settled_state.pop("write_unknown", None)
            if filled > 0 and avg is None:
                settled_state["entry_average_price_unknown"] = True
            elif avg is not None:
                settled_state.pop("entry_average_price_unknown", None)
        if status == "rejected":
            if filled > 0:
                updated = record.model_copy(
                    update={
                        "filled_quantity": filled,
                        "remaining_quantity": filled,
                        "average_fill_price": avg,
                        "realized_pnl": record.realized_pnl or Decimal("0"),
                        "broker_state": settled_state,
                        "needs_attention": True,
                        "state_reason": (
                            "Longbridge 入场被拒前已有成交，准备保护实际仓位"
                        ),
                    }
                )
                return self._initialise_stop(updated).model_copy(
                    update={"needs_attention": True}
                )
            return record.model_copy(
                update={
                    "state": ExecutionState.REJECTED,
                    "filled_quantity": filled,
                    "remaining_quantity": max(record.plan.quantity - filled, Decimal("0")),
                    "average_fill_price": avg,
                    "last_error": str(order.get("message") or ""),
                    "state_reason": "Longbridge 入场订单被拒绝",
                    "broker_state": settled_state,
                }
            )
        if status == "canceled":
            if filled <= 0:
                return record.model_copy(
                    update={
                        "state": ExecutionState.CANCELED,
                        "remaining_quantity": Decimal("0"),
                        "state_reason": "Longbridge 入场已撤销且未成交",
                        "broker_state": settled_state,
                    }
                )
            updated = record.model_copy(
                update={
                    "filled_quantity": filled,
                    "remaining_quantity": filled,
                    "average_fill_price": avg,
                    "realized_pnl": record.realized_pnl or Decimal("0"),
                    "broker_state": settled_state,
                }
            )
            return self._initialise_stop(updated)
        if status == "filled":
            if filled <= 0:
                try:
                    executions = self._session(
                        record.selected_account
                    ).executions(
                        symbol=record.plan.instrument,
                        order_id=record.broker_order_id,
                        start_at=(
                            self._record_created_at(record)
                            or datetime.now(UTC)
                        ),
                    )
                except (BrokerApiError, BrokerTransportError) as exc:
                    state_data = dict(settled_state)
                    state_data["entry_fill_quantity_unknown"] = True
                    state_data["write_unknown"] = "entry_fill_quantity"
                    return record.model_copy(
                        update={
                            "broker_state": state_data,
                            "needs_attention": True,
                            "last_error": str(exc),
                            "state_reason": (
                                "Longbridge 显示已成交但成交数量缺失，"
                                "正在用成交明细只读确认"
                            ),
                        }
                    )
                actual = sum(
                    (
                        _decimal(item.get("quantity"), Decimal("0"))
                        or Decimal("0")
                    )
                    for item in executions
                )
                if actual <= 0:
                    state_data = dict(settled_state)
                    state_data["entry_fill_quantity_unknown"] = True
                    state_data["write_unknown"] = "entry_fill_quantity"
                    return record.model_copy(
                        update={
                            "broker_state": state_data,
                            "needs_attention": True,
                            "state_reason": (
                                "Longbridge 显示已成交但尚未取得可靠成交数量；"
                                "禁止建立止损"
                            ),
                        }
                    )
                if actual > record.plan.quantity:
                    state_data = dict(settled_state)
                    state_data["entry_fill_quantity_unknown"] = True
                    state_data["write_unknown"] = "entry_fill_quantity"
                    return record.model_copy(
                        update={
                            "broker_state": state_data,
                            "needs_attention": True,
                            "state_reason": (
                                "Longbridge 成交明细数量超过本次计划量，"
                                "禁止自动建立止损"
                            ),
                        }
                    )
                weighted = sum(
                    (
                        (_decimal(item.get("quantity"), Decimal("0")) or Decimal("0"))
                        * (_decimal(item.get("price"), Decimal("0")) or Decimal("0"))
                    )
                    for item in executions
                )
                if weighted > 0:
                    avg = weighted / actual
            else:
                actual = filled
            if actual > record.plan.quantity:
                state_data = dict(settled_state)
                state_data["entry_fill_quantity_unknown"] = True
                state_data["write_unknown"] = "entry_fill_quantity"
                return record.model_copy(
                    update={
                        "broker_state": state_data,
                        "needs_attention": True,
                        "state_reason": (
                            "Longbridge 实际成交数量超过本次计划量，"
                            "禁止自动建立止损"
                        ),
                    }
                )
            state_data = dict(settled_state)
            state_data.pop("entry_fill_quantity_unknown", None)
            if state_data.get("write_unknown") == "entry_fill_quantity":
                state_data.pop("write_unknown", None)
            updated = record.model_copy(
                update={
                    "filled_quantity": actual,
                    "remaining_quantity": actual,
                    "average_fill_price": avg,
                    "realized_pnl": record.realized_pnl or Decimal("0"),
                    "broker_state": state_data,
                }
            )
            return self._initialise_stop(updated)
        if (
            record.broker_state.get("entry_cancel_intent")
            and not record.broker_state.get("entry_cancel_requested")
            and record.broker_state.get("entry_cancel_runtime_id")
            != self._runtime_id
        ):
            state_data = dict(record.broker_state)
            state_data["entry_cancel_status"] = "unknown"
            state_data["write_unknown"] = "cancel_entry"
            return record.model_copy(
                update={
                    "broker_state": state_data,
                    "needs_attention": True,
                    "state_reason": (
                        "Longbridge 撤销入场进程中断，保持停写并只读确认"
                    ),
                }
            )
        if status == "partially_filled":
            updated = record.model_copy(
                update={
                    "state": ExecutionState.PARTIALLY_FILLED,
                    "filled_quantity": filled,
                    "remaining_quantity": filled,
                    "average_fill_price": avg,
                    "state_reason": "Longbridge 入场部分成交，准备撤销剩余数量",
                }
            )
            if not bool(record.broker_state.get("entry_cancel_requested")):
                if not bool(record.broker_state.get("entry_cancel_intent")):
                    state = dict(updated.broker_state)
                    state["entry_cancel_intent"] = True
                    state["entry_cancel_runtime_id"] = self._runtime_id
                    return updated.model_copy(
                        update={
                            "broker_state": state,
                            "needs_attention": not allow_writes,
                            "state_reason": (
                                "已持久化 Longbridge 部分成交撤余单意图"
                            ),
                        }
                    )
                if allow_writes:
                    return self._cancel_entry_write(updated)
                return updated.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": "Longbridge 部分成交；需启用会话撤余单并保护",
                    }
                )
            return updated
        if (
            self._entry_age_seconds(record) >= self._entry_timeout_seconds
            and not bool(record.broker_state.get("entry_cancel_requested"))
        ):
            if not bool(record.broker_state.get("entry_cancel_intent")):
                state = dict(record.broker_state)
                state["entry_cancel_intent"] = True
                state["entry_cancel_runtime_id"] = self._runtime_id
                return record.model_copy(
                    update={
                        "broker_state": state,
                        "needs_attention": not allow_writes,
                        "state_reason": "已持久化 Longbridge 超时撤单意图",
                    }
                )
            if allow_writes:
                return self._cancel_entry_write(record)
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "state_reason": "Longbridge 入场等待超时；需启用会话撤单",
                }
            )
        cancel_unknown = (
            record.broker_state.get("entry_cancel_status") == "unknown"
        )
        return record.model_copy(
            update={
                "state": ExecutionState.ENTRY_PENDING,
                "filled_quantity": filled,
                "average_fill_price": avg,
                "needs_attention": cancel_unknown,
                "last_error": record.last_error if cancel_unknown else "",
                "state_reason": (
                    "Longbridge 撤销入场状态不明，持续只读查询"
                    if cancel_unknown
                    else record.state_reason
                ),
            }
        )

    @staticmethod
    def _entry_age_seconds(record: ExecutionRecord) -> float:
        submitted = str(record.broker_state.get("entry_submitted_at") or "")
        if not submitted:
            return 0
        try:
            submitted_at = datetime.fromisoformat(submitted).astimezone(UTC)
        except ValueError:
            return 0
        return max(0.0, (datetime.now(UTC) - submitted_at).total_seconds())

    @staticmethod
    def _record_created_at(record: ExecutionRecord) -> datetime | None:
        try:
            return datetime.fromisoformat(record.created_at).astimezone(UTC)
        except ValueError:
            return None

    def _confirmed_terminal_fill(
        self,
        record: ExecutionRecord,
        *,
        session: Any,
        order_id: str,
        order: dict[str, Any],
        maximum_quantity: Decimal,
    ) -> tuple[Decimal, Decimal | None]:
        """Confirm terminal exit quantity from the order or exact executions."""
        status = str(order.get("state") or "")
        raw_quantity = order.get("filled_quantity")
        quantity = _decimal(raw_quantity)
        price = _decimal(order.get("average_fill_price"))
        quantity_reported = raw_quantity not in (None, "")
        if quantity_reported and quantity is None:
            raise ReconciliationError("Longbridge 终态成交数量无效")
        quantity_missing = not quantity_reported or (
            status == "filled" and (quantity is None or quantity <= 0)
        )
        price_missing = quantity is not None and quantity > 0 and price is None
        must_query = quantity_missing or price_missing
        if quantity is not None and quantity < 0:
            raise ReconciliationError("Longbridge 终态成交数量为负数")
        if must_query:
            try:
                executions = session.executions(
                    symbol=record.plan.instrument,
                    order_id=order_id,
                    start_at=self._record_created_at(record) or datetime.now(UTC),
                )
            except (BrokerApiError, BrokerTransportError) as exc:
                if quantity_missing:
                    raise ReconciliationError(
                        "Longbridge 终态成交数量缺失且成交明细查询失败"
                    ) from exc
                return quantity or Decimal("0"), price
            confirmed, confirmed_price = _aggregate_executions(executions)
            if quantity_missing:
                if confirmed <= 0:
                    raise ReconciliationError(
                        "Longbridge 终态成交数量缺失且成交明细尚未确认零成交"
                    )
                quantity = confirmed
            elif confirmed > 0 and confirmed != quantity:
                raise ReconciliationError(
                    "Longbridge 订单成交量与成交明细暂不一致"
                )
            if price is None and confirmed > 0:
                price = confirmed_price
        quantity = quantity or Decimal("0")
        if status == "filled" and quantity <= 0:
            raise ReconciliationError(
                "Longbridge 显示已成交但无法确认实际成交数量"
            )
        if quantity > maximum_quantity:
            raise ReconciliationError(
                "Longbridge 终态成交数量超过本次可退出数量"
            )
        return quantity, price

    @staticmethod
    def _exit_quantity_unconfirmed(
        record: ExecutionRecord,
        message: str,
    ) -> ExecutionRecord:
        state = dict(record.broker_state)
        state["write_unknown"] = "exit_fill_quantity"
        return record.model_copy(
            update={
                "broker_state": state,
                "needs_attention": True,
                "last_error": message,
                "state_reason": (
                    "Longbridge 退出订单终态数量未确认，保持停写并只读对账"
                ),
            }
        )

    @staticmethod
    def _take_profit_targets(record: ExecutionRecord) -> list[dict[str, Any]]:
        preflight = record.preflight
        if preflight is None:
            return []
        lot = preflight.quantity_step or Decimal("1")
        lots = int(record.filled_quantity / lot)
        if lots < 2 or preflight.take_profit_1 == preflight.take_profit_2:
            targets = [
                {
                    "index": 1,
                    "price": preflight.take_profit_1,
                    "quantity": record.filled_quantity,
                }
            ]
        else:
            first = Decimal(lots // 2) * lot
            targets = [
                {"index": 1, "price": preflight.take_profit_1, "quantity": first},
                {
                    "index": 2,
                    "price": preflight.take_profit_2,
                    "quantity": record.filled_quantity - first,
                },
            ]
        already_filled = record.broker_state.get("take_profit_filled") or {}
        adjusted: list[dict[str, Any]] = []
        for target in targets:
            filled = (
                _decimal(already_filled.get(str(target["index"])), Decimal("0"))
                or Decimal("0")
            )
            remaining = max(Decimal(target["quantity"]) - filled, Decimal("0"))
            if remaining > 0:
                adjusted.append({**target, "quantity": remaining})
        return adjusted

    @staticmethod
    def _target_reached(record: ExecutionRecord, price: Decimal, target: Decimal) -> bool:
        return price >= target if record.plan.direction == "long" else price <= target

    def _start_partial_exit(
        self,
        record: ExecutionRecord,
        *,
        index: int,
        quantity: Decimal,
        reason: str,
    ) -> ExecutionRecord:
        state = dict(record.broker_state)
        attempts = dict(state.get("exit_attempts") or {})
        attempt_key = str(index)
        attempt = int(attempts.get(attempt_key) or 0) + 1
        attempts[attempt_key] = attempt
        state["exit_attempts"] = attempts
        state["partial_exit"] = {
            "phase": "cancel_stop",
            "index": index,
            "quantity": str(quantity),
            "reason": reason,
            "request_id": _request_id(record.id, f"exit-{index}", attempt),
            "remark": _remark(record.id, f"exit{index}", attempt),
            "order_id": "",
            "cancel_requested": False,
            "cancel_status": "submitting",
            "cancel_runtime_id": self._runtime_id,
        }
        return record.model_copy(
            update={
                "broker_state": state,
                "state_reason": f"Longbridge {reason} 已触发，准备撤止损后减仓",
            }
        )

    def _reconcile_partial_exit(
        self,
        record: ExecutionRecord,
        *,
        allow_writes: bool,
    ) -> ExecutionRecord:
        state = dict(record.broker_state)
        action = dict(state.get("partial_exit") or {})
        stop = dict(state.get("stop_order") or {})
        phase = str(action.get("phase") or "")
        session = self._session(record.selected_account)
        if phase == "exit_unknown":
            try:
                found = session.find_order_by_remark(
                    symbol=record.plan.instrument,
                    remark=str(action.get("remark") or ""),
                    start_at=self._record_created_at(record),
                )
            except (BrokerApiError, BrokerTransportError) as exc:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": "Longbridge 减仓提交状态未知，正在只读对账",
                    }
                )
            if not found:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": (
                            "Longbridge 减仓提交状态未知；未查到原订单，禁止自动重发"
                        ),
                    }
                )
            action["order_id"] = str(found.get("order_id") or "")
            action["phase"] = "wait_exit"
            action["state"] = str(found.get("state") or "pending")
            state["partial_exit"] = action
            state.pop("write_unknown", None)
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": False,
                    "last_error": "",
                    "state_reason": "已按备注恢复 Longbridge 减仓订单",
                }
            )
        if phase == "cancel_stop":
            try:
                stop_status = session.order(str(stop.get("order_id") or ""))
            except (BrokerApiError, BrokerTransportError) as exc:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": "等待确认 Longbridge 旧止损已撤销",
                    }
                )
            stop_state = str(stop_status.get("state") or "")
            if stop_state == "filled":
                try:
                    exit_qty, exit_price = self._confirmed_terminal_fill(
                        record,
                        session=session,
                        order_id=str(stop.get("order_id") or ""),
                        order=stop_status,
                        maximum_quantity=record.remaining_quantity,
                    )
                except ReconciliationError as exc:
                    return self._exit_quantity_unconfirmed(record, str(exc))
                return self._close_from_exit(
                    record,
                    quantity=exit_qty,
                    price=exit_price,
                    reason="止损在撤单竞争中成交",
                )
            if stop_state in {"canceled", "rejected"}:
                try:
                    stop_filled, stop_price = self._confirmed_terminal_fill(
                        record,
                        session=session,
                        order_id=str(stop.get("order_id") or ""),
                        order=stop_status,
                        maximum_quantity=record.remaining_quantity,
                    )
                except ReconciliationError as exc:
                    return self._exit_quantity_unconfirmed(record, str(exc))
                updated = (
                    self._close_from_exit(
                        record,
                        quantity=stop_filled,
                        price=stop_price,
                        reason="止损在撤单竞争中部分成交",
                    )
                    if stop_filled > 0
                    else record
                )
                if updated.remaining_quantity <= 0:
                    return updated
                state = dict(updated.broker_state)
                action = dict(state.get("partial_exit") or action)
                if stop_filled > 0:
                    action["quantity"] = str(updated.remaining_quantity)
                    action["reason"] = "止损撤单竞态后退出剩余仓位"
                action["phase"] = "submit_exit"
                action["cancel_status"] = "confirmed"
                action["submit_runtime_id"] = self._runtime_id
                state["partial_exit"] = action
                stop["state"] = stop_state
                state["stop_order"] = stop
                state.pop("write_unknown", None)
                return updated.model_copy(update={"broker_state": state})
            if action.get("cancel_status") in {"unknown", "submitted"}:
                unknown = action.get("cancel_status") == "unknown"
                return record.model_copy(
                    update={
                        "needs_attention": unknown,
                        "state_reason": (
                            "Longbridge 撤止损状态未知，保持停写并只读确认"
                            if unknown
                            else "等待 Longbridge 确认旧止损已撤销"
                        ),
                    }
                )
            if (
                action.get("cancel_status") == "submitting"
                and action.get("cancel_runtime_id") != self._runtime_id
            ):
                action["cancel_status"] = "unknown"
                state["partial_exit"] = action
                state["write_unknown"] = "cancel_stop"
                return record.model_copy(
                    update={
                        "broker_state": state,
                        "needs_attention": True,
                        "state_reason": (
                            "Longbridge 撤止损进程中断，保持停写并只读确认"
                        ),
                    }
                )
            if not allow_writes:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": "Longbridge 减仓待执行；需启用本次会话",
                    }
                )
            if action.get("cancel_status") != "submitting":
                action["cancel_status"] = "submitting"
                action["cancel_runtime_id"] = self._runtime_id
                state["partial_exit"] = action
                return record.model_copy(
                    update={
                        "broker_state": state,
                        "state_reason": "已持久化 Longbridge 撤止损提交中状态",
                    }
                )
            try:
                self._write(
                    lambda: session.cancel_order(str(stop.get("order_id") or ""))
                )
            except BrokerApiError as exc:
                raise BrokerRejected(f"Longbridge 撤止损失败：{exc}") from exc
            except BrokerTransportError as exc:
                action["cancel_status"] = "unknown"
                state["partial_exit"] = action
                state["write_unknown"] = "cancel_stop"
                return record.model_copy(
                    update={
                        "broker_state": state,
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": "Longbridge 撤止损状态未知，已停写",
                    }
                )
            action["cancel_requested"] = True
            action["cancel_status"] = "submitted"
            action.pop("cancel_runtime_id", None)
            state["partial_exit"] = action
            return record.model_copy(
                update={
                    "broker_state": state,
                    "state_reason": "已请求撤销 Longbridge 旧止损",
                }
            )

        if phase == "submit_exit":
            submit_runtime_id = str(action.get("submit_runtime_id") or "")
            if submit_runtime_id and submit_runtime_id != self._runtime_id:
                action["phase"] = "exit_unknown"
                action["state"] = "unknown"
                state["partial_exit"] = action
                state["write_unknown"] = "exit"
                return record.model_copy(
                    update={
                        "broker_state": state,
                        "needs_attention": True,
                        "state_reason": (
                            "Longbridge 减仓提交进程中断，按备注只读对账并禁止重发"
                        ),
                    }
                )
            if not allow_writes:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": "Longbridge 旧止损已撤；需启用会话立即减仓",
                    }
                )
            if not submit_runtime_id:
                action["submit_runtime_id"] = self._runtime_id
                state["partial_exit"] = action
                return record.model_copy(
                    update={
                        "broker_state": state,
                        "state_reason": "已持久化 Longbridge 减仓提交中状态",
                    }
                )
            try:
                broker_available = self._available_exit_quantity(
                    record,
                    session,
                )
            except (
                BrokerApiError,
                BrokerTransportError,
                ReconciliationError,
            ) as exc:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": (
                            "无法核实 Longbridge 实际可用持仓，禁止提交减仓"
                        ),
                    }
                )
            desired_quantity = min(
                Decimal(str(action["quantity"])),
                record.remaining_quantity,
                broker_available,
            )
            if desired_quantity <= 0:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": (
                            "Longbridge 券商端没有同向可用持仓，"
                            "禁止提交可能反向开仓的减仓单"
                        ),
                    }
                )
            verified_quantity = str(desired_quantity)
            if str(action.get("quantity") or "") != verified_quantity:
                action["quantity"] = verified_quantity
                action["position_quantity_mismatch"] = (
                    desired_quantity != record.remaining_quantity
                )
                state["partial_exit"] = action
                return record.model_copy(
                    update={
                        "broker_state": state,
                        "needs_attention": (
                            desired_quantity != record.remaining_quantity
                        ),
                        "state_reason": (
                            "已按券商实际可用持仓校准 Longbridge 减仓数量，"
                            "等待下一轮再次核实后提交"
                        ),
                    }
                )
            exit_mode = record.plan.exit_order_mode
            body = {
                "symbol": record.plan.instrument,
                "side": "Sell" if record.plan.direction == "long" else "Buy",
                "order_type": "MO" if exit_mode == "market" else "LO",
                "submitted_quantity": str(action["quantity"]),
                "time_in_force": "Day",
                "remark": str(action["remark"]),
                "client_request_id": str(action["request_id"]),
            }
            if exit_mode != "market":
                try:
                    reference_price = session.current_price(record.plan.instrument)
                    if exit_mode == "limit_with_slippage":
                        submitted_price = apply_exit_atr_slippage(
                            reference_price,
                            record.plan.direction,
                            record.plan.entry_atr,
                            record.plan.exit_slippage_atr_multiple,
                        )
                    else:
                        submitted_price = reference_price
                except (BrokerApiError, BrokerTransportError) as exc:
                    return record.model_copy(
                        update={
                            "needs_attention": True,
                            "last_error": str(exc),
                            "state_reason": "Longbridge 主动离场限价缺少最新价，暂不提交",
                        }
                    )
                except ValueError as exc:
                    return record.model_copy(
                        update={
                            "needs_attention": True,
                            "last_error": str(exc),
                            "state_reason": "Longbridge 主动离场限价配置无效，暂不提交",
                        }
                    )
                body["submitted_price"] = str(submitted_price)
                action["reference_price"] = str(reference_price)
                action["submitted_price"] = str(submitted_price)
            action["order_mode"] = exit_mode
            action["atr_reference"] = str(record.plan.entry_atr or "")
            action["slippage_atr_multiple"] = str(
                record.plan.exit_slippage_atr_multiple
            )
            try:
                order_id = self._write(lambda: session.submit_order(body))
            except BrokerApiError as exc:
                raise BrokerRejected(f"Longbridge 减仓被拒绝：{exc}") from exc
            except BrokerTransportError as exc:
                action["phase"] = "exit_unknown"
                action["state"] = "unknown"
                state["partial_exit"] = action
                state["write_unknown"] = "exit"
                return record.model_copy(
                    update={
                        "broker_state": state,
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": (
                            "Longbridge 减仓提交状态未知，已停写并禁止自动重发"
                        ),
                    }
                )
            action["order_id"] = order_id
            action["phase"] = "wait_exit"
            action.pop("submit_runtime_id", None)
            state["partial_exit"] = action
            return record.model_copy(
                update={
                    "broker_state": state,
                    "state_reason": "Longbridge 减仓订单已受理",
                }
            )

        if phase == "wait_exit":
            try:
                exit_order = session.order(str(action.get("order_id") or ""))
            except (BrokerApiError, BrokerTransportError) as exc:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": "Longbridge 减仓订单暂时无法查询",
                    }
                )
            status = str(exit_order.get("state") or "")
            if status == "filled":
                try:
                    qty, price = self._confirmed_terminal_fill(
                        record,
                        session=session,
                        order_id=str(action.get("order_id") or ""),
                        order=exit_order,
                        maximum_quantity=(
                            _decimal(action.get("quantity"))
                            or record.remaining_quantity
                        ),
                    )
                except ReconciliationError as exc:
                    return self._exit_quantity_unconfirmed(record, str(exc))
                updated = self._close_from_exit(
                    record,
                    quantity=qty,
                    price=price,
                    reason=str(action.get("reason") or "减仓"),
                )
                if updated.remaining_quantity > 0:
                    completed = list(updated.broker_state.get("take_profit_completed") or [])
                    if int(action.get("index") or 0) > 0:
                        completed.append(int(action["index"]))
                    next_state = dict(updated.broker_state)
                    next_state["take_profit_completed"] = sorted(set(completed))
                    next_state["partial_exit"] = {}
                    updated = updated.model_copy(update={"broker_state": next_state})
                    return self._initialise_stop(updated)
                return updated
            if status in {"canceled", "rejected"}:
                try:
                    qty, price = self._confirmed_terminal_fill(
                        record,
                        session=session,
                        order_id=str(action.get("order_id") or ""),
                        order=exit_order,
                        maximum_quantity=(
                            _decimal(action.get("quantity"))
                            or record.remaining_quantity
                        ),
                    )
                except ReconciliationError as exc:
                    return self._exit_quantity_unconfirmed(record, str(exc))
                updated = (
                    self._close_from_exit(
                        record,
                        quantity=qty,
                        price=price,
                        reason=str(action.get("reason") or "减仓"),
                    )
                    if qty > 0
                    else record
                )
                if updated.remaining_quantity <= 0:
                    return updated
                next_state = dict(updated.broker_state)
                index = int(action.get("index") or 0)
                if index > 0 and qty > 0:
                    filled_by_target = dict(
                        next_state.get("take_profit_filled") or {}
                    )
                    key = str(index)
                    prior = (
                        _decimal(filled_by_target.get(key), Decimal("0"))
                        or Decimal("0")
                    )
                    filled_by_target[key] = str(prior + qty)
                    next_state["take_profit_filled"] = filled_by_target
                next_state["partial_exit"] = {}
                next_state["stop_order"] = {}
                updated = updated.model_copy(
                    update={
                        "broker_state": next_state,
                        "needs_attention": True,
                        "state_reason": (
                            "Longbridge 减仓订单终止，按实际剩余仓位重建止损"
                        ),
                    }
                )
                return self._initialise_stop(updated).model_copy(
                    update={"needs_attention": True}
                )
        return record

    @staticmethod
    def _close_from_exit(
        record: ExecutionRecord,
        *,
        quantity: Decimal,
        price: Decimal | None,
        reason: str,
    ) -> ExecutionRecord:
        quantity = min(max(quantity, Decimal("0")), record.remaining_quantity)
        remaining = max(record.remaining_quantity - quantity, Decimal("0"))
        entry = record.average_fill_price
        prior = record.realized_pnl or Decimal("0")
        state = dict(record.broker_state)
        prior_unknown = bool(
            state.get("realized_pnl_unknown")
            or state.get("entry_average_price_unknown")
            or entry is None
        )
        if prior_unknown or (quantity > 0 and price is None):
            realized = None
            state["realized_pnl_unknown"] = True
        else:
            trade_pnl = (
                (price - entry) * quantity
                if record.plan.direction == "long"
                else (entry - price) * quantity
            ) if price is not None else Decimal("0")
            realized = prior + trade_pnl
        return record.model_copy(
            update={
                "state": ExecutionState.CLOSED if remaining <= 0 else ExecutionState.OPEN,
                "remaining_quantity": remaining,
                "realized_pnl": realized,
                "unrealized_pnl": Decimal("0") if remaining <= 0 else record.unrealized_pnl,
                "broker_state": state,
                "state_reason": (
                    f"Longbridge 仓位已关闭（{reason}）"
                    if remaining <= 0
                    else f"Longbridge 已完成部分减仓（{reason}）"
                ),
                "needs_attention": realized is None,
                "last_error": "",
            }
        )

    def _open_reconcile(
        self,
        record: ExecutionRecord,
        *,
        allow_writes: bool,
    ) -> ExecutionRecord:
        if record.broker_state.get("partial_exit"):
            return self._reconcile_partial_exit(record, allow_writes=allow_writes)
        stop = dict(record.broker_state.get("stop_order") or {})
        if not stop.get("order_id"):
            return record.model_copy(
                update={
                    "state": ExecutionState.PROTECTING,
                    "needs_attention": not allow_writes,
                    "state_reason": "Longbridge 持仓缺少止损，需要恢复",
                }
            )
        session = self._session(record.selected_account)
        try:
            stop_status = session.order(str(stop["order_id"]))
        except (BrokerApiError, BrokerTransportError) as exc:
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "Longbridge 止损状态暂时无法查询",
                }
            )
        if stop_status.get("state") == "filled":
            try:
                qty, price = self._confirmed_terminal_fill(
                    record,
                    session=session,
                    order_id=str(stop["order_id"]),
                    order=stop_status,
                    maximum_quantity=record.remaining_quantity,
                )
            except ReconciliationError as exc:
                return self._exit_quantity_unconfirmed(record, str(exc))
            return self._close_from_exit(
                record,
                quantity=qty,
                price=price,
                reason="止损",
            )
        if stop_status.get("state") == "partially_filled":
            return record.model_copy(
                update={
                    "state": ExecutionState.OPEN,
                    "needs_attention": True,
                    "state_reason": "Longbridge 止损已触发并部分成交，等待终态",
                }
            )
        if stop_status.get("state") in {"canceled", "rejected"}:
            try:
                qty, price = self._confirmed_terminal_fill(
                    record,
                    session=session,
                    order_id=str(stop["order_id"]),
                    order=stop_status,
                    maximum_quantity=record.remaining_quantity,
                )
            except ReconciliationError as exc:
                return self._exit_quantity_unconfirmed(record, str(exc))
            updated = (
                self._close_from_exit(
                    record,
                    quantity=qty,
                    price=price,
                    reason="止损部分成交后终止",
                )
                if qty > 0
                else record
            )
            if updated.remaining_quantity <= 0:
                return updated
            state = dict(updated.broker_state)
            state["stop_order"] = {}
            return updated.model_copy(
                update={
                    "state": ExecutionState.PROTECTING,
                    "broker_state": state,
                    "needs_attention": not allow_writes,
                    "state_reason": "Longbridge 止损失效，需要重建",
                }
            )
        try:
            current = session.current_price(record.plan.instrument)
        except (BrokerApiError, BrokerTransportError) as exc:
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "Longbridge 最新价暂时无法读取",
                }
            )
        completed = set(record.broker_state.get("take_profit_completed") or [])
        for target in self._take_profit_targets(record):
            index = int(target["index"])
            if index in completed:
                continue
            if self._target_reached(record, current, Decimal(target["price"])):
                return self._start_partial_exit(
                    record,
                    index=index,
                    quantity=min(Decimal(target["quantity"]), record.remaining_quantity),
                    reason=f"止盈{index}",
                )
            break
        entry = record.average_fill_price
        unrealized = None
        if entry is not None:
            unrealized = (
                (current - entry) * record.remaining_quantity
                if record.plan.direction == "long"
                else (entry - current) * record.remaining_quantity
            )
        return record.model_copy(
            update={
                "state": ExecutionState.OPEN,
                "unrealized_pnl": unrealized,
                "pnl_currency": str(
                    record.preflight.broker_metadata.get("currency") if record.preflight else ""
                ),
                "needs_attention": False,
                "last_error": "",
                "state_reason": "Longbridge 仓位、止损和止盈条件监控中",
            }
        )

    def reconcile(
        self,
        record: ExecutionRecord,
        *,
        allow_writes: bool,
    ) -> ExecutionRecord:
        if record.state == ExecutionState.UNKNOWN:
            remark = str(record.broker_state.get("entry_remark") or "")
            if not remark:
                return record
            try:
                found = self._session(record.selected_account).find_order_by_remark(
                    symbol=record.plan.instrument,
                    remark=remark,
                    start_at=self._record_created_at(record),
                )
            except (BrokerApiError, BrokerTransportError):
                return record
            if not found:
                return record
            record = record.model_copy(
                update={
                    "state": ExecutionState.ENTRY_PENDING,
                    "broker_order_id": str(found.get("order_id") or ""),
                    "needs_attention": False,
                    "last_error": "",
                    "state_reason": "已按 Longbridge 备注对账",
                }
            )
        if record.state in {
            ExecutionState.ENTRY_PENDING,
            ExecutionState.PARTIALLY_FILLED,
        }:
            return self._entry_reconcile(record, allow_writes=allow_writes)
        if record.state == ExecutionState.PROTECTING:
            if not record.broker_state.get("stop_order"):
                initialised = self._initialise_stop(record)
                if allow_writes:
                    return initialised
                return initialised.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": "Longbridge 仓位需建立止损；请启用本次会话",
                    }
                )
            stop = dict(record.broker_state.get("stop_order") or {})
            if stop.get("state") == "unknown":
                return self._place_stop(record)
            if not allow_writes:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": "Longbridge 仓位需建立止损；请启用本次会话",
                    }
                )
            return self._place_stop(record)
        if record.state == ExecutionState.OPEN:
            return self._open_reconcile(record, allow_writes=allow_writes)
        if record.state == ExecutionState.EXIT_PENDING:
            return self._reconcile_partial_exit(record, allow_writes=allow_writes)
        return record

    def request_exit(
        self,
        record: ExecutionRecord,
        *,
        reason: str,
    ) -> ExecutionRecord:
        if record.state not in {ExecutionState.OPEN, ExecutionState.PROTECTING}:
            raise PreflightError("只有持仓中的 Longbridge 执行可以主动离场")
        stop = dict(record.broker_state.get("stop_order") or {})
        if not stop.get("order_id") and stop.get("state") == "unknown":
            raise PreflightError("Longbridge 止损提交状态未知，需先完成对账")
        started = self._start_partial_exit(
            record,
            index=0,
            quantity=record.remaining_quantity,
            reason=reason or "主动离场",
        )
        if not stop.get("order_id"):
            state = dict(started.broker_state)
            action = dict(state["partial_exit"])
            action["phase"] = "submit_exit"
            action.pop("cancel_status", None)
            action.pop("cancel_runtime_id", None)
            action["submit_runtime_id"] = self._runtime_id
            state["partial_exit"] = action
            started = started.model_copy(update={"broker_state": state})
        return started.model_copy(update={"state": ExecutionState.EXIT_PENDING})

    def cancel_entry(self, record: ExecutionRecord) -> ExecutionRecord:
        if record.state not in {
            ExecutionState.ENTRY_PENDING,
            ExecutionState.PARTIALLY_FILLED,
        }:
            raise PreflightError("当前 Longbridge 执行没有可撤销的入场订单")
        if record.broker_state.get("entry_fill_quantity_unknown"):
            raise PreflightError(
                "Longbridge 入场已显示成交但数量仍在只读对账，禁止发送撤单"
            )
        if record.broker_state.get("entry_cancel_requested"):
            return record
        return self._cancel_entry_write(record)

    def account_snapshot(
        self,
        plan: ExecutionPlan,
        *,
        account_profile: str | None = None,
        broker_metadata: dict | None = None,
    ) -> AccountSnapshot:
        profile = account_profile or plan.requested_account
        session = self._session(profile)
        balances = session.account_balances()
        positions_raw = session.positions()
        profit = session.profit_summary()
        metadata_currency = str(
            (broker_metadata or {}).get("currency") or ""
        ).upper()
        profit_currency = str(profit.get("currency") or "").upper()
        primary_currency = metadata_currency or profit_currency
        primary = next(
            (
                item
                for item in balances
                if str(item.get("currency") or "").upper() == primary_currency
            ),
            {},
        )
        cash_info = next(
            (
                item
                for balance in balances
                for item in (balance.get("cash_infos") or [])
                if str(item.get("currency") or "").upper() == primary_currency
            ),
            {},
        )
        equity = _decimal(primary.get("net_assets"))
        if equity is None and profit_currency == primary_currency:
            equity = _decimal(profit.get("current_total_asset"))
        positions: list[PositionSnapshot] = []
        for item in positions_raw:
            quantity = _decimal(item.get("quantity"), Decimal("0")) or Decimal("0")
            if quantity == 0:
                continue
            positions.append(
                PositionSnapshot(
                    instrument=str(item.get("symbol") or ""),
                    direction="long" if quantity > 0 else "short",
                    quantity=abs(quantity),
                    available_quantity=_decimal(item.get("available_quantity")),
                    average_price=_decimal(item.get("cost_price")),
                    currency=str(item.get("currency") or ""),
                    raw={
                        "account_channel": str(item.get("account_channel") or ""),
                        "market": str(item.get("market") or ""),
                        "name": str(item.get("name") or ""),
                    },
                )
            )
        return AccountSnapshot(
            broker="longbridge",
            account_profile=profile,
            base_currency=primary_currency,
            equity=equity,
            cash=_decimal(primary.get("total_cash")),
            available=_decimal(cash_info.get("available_cash")),
            buying_power=_decimal(primary.get("buy_power")),
            total_pnl=(
                _decimal(profit.get("sum_profit"))
                if profit_currency == primary_currency
                else None
            ),
            realized_pnl=None,
            unrealized_pnl=None,
            positions=positions,
            raw_summary={
                "risk_level": str(primary.get("risk_level") or ""),
                "margin_call": str(primary.get("margin_call") or ""),
                "max_finance_amount": str(primary.get("max_finance_amount") or ""),
                "remaining_finance_amount": str(
                    primary.get("remaining_finance_amount") or ""
                ),
                "withdraw_cash": str(cash_info.get("withdraw_cash") or ""),
                "frozen_cash": str(cash_info.get("frozen_cash") or ""),
                "settling_cash": str(cash_info.get("settling_cash") or ""),
                "profit_rate": str(profit.get("sum_profit_rate") or ""),
            },
        )
