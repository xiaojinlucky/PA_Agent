"""OKX spot/perpetual lifecycle adapter."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pa_agent.execution.errors import (
    BrokerApiError,
    BrokerRejected,
    BrokerTransportError,
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
from pa_agent.execution.okx_client import OkxRestClient


def _decimal(value: object, *, default: Decimal | None = None) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return parsed if parsed.is_finite() else default


def _positive_decimal(value: object, label: str) -> Decimal:
    parsed = _decimal(value)
    if parsed is None or parsed <= 0:
        raise PreflightError(f"OKX {label} 无效")
    return parsed


def _is_multiple(value: Decimal, step: Decimal) -> bool:
    if step <= 0:
        return False
    return value % step == 0


def _client_id(execution_id: str, action: str, index: int = 0) -> str:
    compact = "".join(ch for ch in execution_id.lower() if ch.isalnum())
    action_part = "".join(ch for ch in action.lower() if ch.isalnum())[:5]
    return f"pa{compact[:20]}{action_part}{index:02d}"[:32]


def _entry_side(plan: ExecutionPlan) -> str:
    return "buy" if plan.direction == "long" else "sell"


def _exit_side(plan: ExecutionPlan) -> str:
    return "sell" if plan.direction == "long" else "buy"


def _okx_order_state(raw: dict[str, Any]) -> str:
    state = str(raw.get("state") or "").strip().lower()
    if state == "live":
        return "pending"
    if state == "partially_filled":
        return "partially_filled"
    if state == "filled":
        return "filled"
    if state in {"canceled", "mmp_canceled"}:
        return "canceled"
    return "unknown"


class OkxAdapter:
    """One-write-per-reconcile adapter so every broker ID can be persisted."""

    def __init__(
        self,
        client: OkxRestClient,
        *,
        margin_mode: str = "cross",
        entry_timeout_seconds: int = 120,
    ) -> None:
        self._client = client
        self._margin_mode = margin_mode
        self._entry_timeout_seconds = int(entry_timeout_seconds)

    @staticmethod
    def _trade_mode(plan: ExecutionPlan) -> str:
        return "cash" if plan.product == "spot" else ""

    def preflight(self, plan: ExecutionPlan) -> PreflightResult:
        self._client.sync_server_time()
        inst_type = "SPOT" if plan.product == "spot" else "SWAP"
        trade_mode = "cash" if plan.product == "spot" else self._margin_mode
        instruments = self._client.instruments(inst_type)
        instrument = next(
            (item for item in instruments if str(item.get("instId")) == plan.instrument),
            None,
        )
        if instrument is None:
            raise PreflightError(
                f"OKX 当前账户没有可用的 {inst_type} 品种 {plan.instrument}"
            )
        if str(instrument.get("state") or "") != "live":
            raise PreflightError(
                f"OKX 品种 {plan.instrument} 当前状态为 "
                f"{instrument.get('state') or 'unknown'}，不能交易"
            )
        actual_type = str(instrument.get("instType") or inst_type).upper()
        if actual_type != inst_type:
            raise PreflightError(
                f"OKX 品种类型为 {actual_type}，与配置的 {inst_type} 不一致"
            )

        tick = _positive_decimal(instrument.get("tickSz"), "tickSz")
        lot = _positive_decimal(instrument.get("lotSz"), "lotSz")
        minimum = _positive_decimal(instrument.get("minSz"), "minSz")
        if plan.quantity < minimum or not _is_multiple(plan.quantity, lot):
            raise PreflightError(
                f"OKX 数量 {plan.quantity} 必须不小于 {minimum} 且为 {lot} 的整数倍"
            )
        for label, price in (
            ("入场价", plan.entry_price),
            ("止盈1", plan.take_profit_1),
            ("止盈2", plan.take_profit_2),
            ("止损价", plan.stop_loss),
        ):
            if not _is_multiple(price, tick):
                raise PreflightError(
                    f"OKX {label} {price} 不是价格跳动 {tick} 的整数倍"
                )

        account_config = self._client.account_config()
        position_mode = str(account_config.get("posMode") or "net_mode")
        if plan.product == "swap" and position_mode not in {
            "net_mode",
            "long_short_mode",
        }:
            raise PreflightError(f"不支持的 OKX 持仓模式：{position_mode}")

        price_for_max = (
            str(plan.entry_price) if plan.entry_type in {"limit", "breakout"} else None
        )
        maximum = self._client.max_order_size(
            instrument=plan.instrument,
            trade_mode=trade_mode,
            price=price_for_max,
        )
        max_field = "maxBuy" if plan.direction == "long" else "maxSell"
        max_quantity = _decimal(maximum.get(max_field), default=Decimal("0"))
        if max_quantity is None or plan.quantity > max_quantity:
            raise PreflightError(
                f"OKX 可交易数量 {max_quantity or 0} 小于计划数量 {plan.quantity}"
            )

        if plan.product == "swap":
            existing = [
                item
                for item in self._client.positions(instrument=plan.instrument)
                if (_decimal(item.get("pos"), default=Decimal("0")) or Decimal("0"))
                != 0
            ]
            if existing:
                raise PreflightError(
                    f"OKX {plan.instrument} 已有持仓；PA 当前不与既有仓位合并"
                )
            leverage_rows = self._client.leverage_info(
                instrument=plan.instrument,
                margin_mode=trade_mode,
            )
            leverage = next(
                (
                    str(item.get("lever") or "")
                    for item in leverage_rows
                    if str(item.get("instId") or plan.instrument) == plan.instrument
                ),
                "",
            )
        else:
            leverage = ""

        warnings: list[str] = []
        if plan.take_profit_1 == plan.take_profit_2:
            warnings.append("两档止盈相同，将只建立一档保护")
        return PreflightResult(
            selected_account="okx",
            quantity=plan.quantity,
            entry_price=plan.entry_price,
            take_profit_1=plan.take_profit_1,
            take_profit_2=plan.take_profit_2,
            stop_loss=plan.stop_loss,
            price_tick=tick,
            quantity_step=lot,
            minimum_quantity=minimum,
            warnings=warnings,
            broker_metadata={
                "inst_type": inst_type,
                "trade_mode": trade_mode,
                "position_mode": position_mode,
                "base_currency": str(instrument.get("baseCcy") or ""),
                "quote_currency": str(instrument.get("quoteCcy") or ""),
                "settle_currency": str(instrument.get("settleCcy") or ""),
                "contract_value": str(instrument.get("ctVal") or ""),
                "contract_type": str(instrument.get("ctType") or ""),
                "current_leverage": leverage,
            },
        )

    @staticmethod
    def _position_side(plan: ExecutionPlan, preflight: PreflightResult) -> str:
        mode = str(preflight.broker_metadata.get("position_mode") or "net_mode")
        if mode == "long_short_mode":
            return "long" if plan.direction == "long" else "short"
        return "net"

    def submit_entry(self, record: ExecutionRecord) -> ExecutionRecord:
        preflight = record.preflight
        if preflight is None:
            raise PreflightError("OKX 提交前缺少预检结果")
        plan = record.plan
        client_id = record.client_order_id or _client_id(record.id, "entry")
        side = _entry_side(plan)
        trade_mode = str(preflight.broker_metadata["trade_mode"])
        common: dict[str, Any] = {
            "instId": plan.instrument,
            "tdMode": trade_mode,
            "side": side,
            "sz": str(preflight.quantity),
        }
        if plan.product == "spot":
            common["tgtCcy"] = "base_ccy"
        else:
            common["posSide"] = self._position_side(plan, preflight)

        try:
            if plan.entry_type == "breakout":
                response = self._client.place_algo_order(
                    {
                        **common,
                        "algoClOrdId": client_id,
                        "ordType": "trigger",
                        "triggerPx": str(preflight.entry_price),
                        "orderPx": "-1",
                        "triggerPxType": "last",
                    }
                )
                broker_order_id = str(response.get("algoId") or "")
                entry_kind = "algo"
            else:
                body = {
                    **common,
                    "clOrdId": client_id,
                    "ordType": "market" if plan.entry_type == "market" else "limit",
                }
                if plan.entry_type == "limit":
                    body["px"] = str(preflight.entry_price)
                response = self._client.place_order(body)
                broker_order_id = str(response.get("ordId") or "")
                entry_kind = "regular"
        except BrokerApiError as exc:
            raise BrokerRejected(str(exc)) from exc
        except BrokerTransportError as exc:
            if exc.write_may_have_reached:
                raise SubmissionUnknown(str(exc)) from exc
            raise

        if not broker_order_id:
            raise SubmissionUnknown("OKX 接受响应缺少订单 ID")
        broker_state = dict(record.broker_state)
        broker_state["entry_kind"] = entry_kind
        return record.model_copy(
            update={
                "state": ExecutionState.ENTRY_PENDING,
                "selected_account": "okx",
                "client_order_id": client_id,
                "broker_order_id": broker_order_id,
                "broker_state": broker_state,
                "state_reason": "OKX 入场请求已受理，等待成交",
                "last_error": "",
                "needs_attention": False,
            }
        )

    def prepare_submit(self, record: ExecutionRecord) -> ExecutionRecord:
        """Persist the deterministic client ID before the external write."""
        if record.preflight is None:
            raise PreflightError("OKX 提交前缺少预检结果")
        client_id = _client_id(record.id, "entry")
        broker_state = {
            **record.broker_state,
            "entry_kind": (
                "algo" if record.plan.entry_type == "breakout" else "regular"
            ),
            "entry_submitted_at": utc_now_iso(),
            "entry_cancel_requested": False,
            "protection_targets": [],
            "exit_order": {},
        }
        return record.model_copy(
            update={
                "state": ExecutionState.SUBMITTING,
                "selected_account": "okx",
                "client_order_id": client_id,
                "broker_state": broker_state,
                "state_reason": "准备提交 OKX 入场订单",
            }
        )

    def _regular_entry_order(self, record: ExecutionRecord) -> dict[str, Any]:
        return self._client.get_order(
            instrument=record.plan.instrument,
            order_id=record.broker_order_id,
            client_order_id=record.client_order_id,
        )

    def _algo_entry_order(self, record: ExecutionRecord) -> dict[str, Any]:
        stored_child_id = str(
            record.broker_state.get("entry_child_order_id") or ""
        )
        if stored_child_id:
            child = self._client.get_order(
                instrument=record.plan.instrument,
                order_id=stored_child_id,
            )
            if child:
                child = dict(child)
                child["_pa_child_order_id"] = stored_child_id
                return child
        algo = self._client.get_algo_order(
            algo_id=record.broker_order_id,
            client_algo_id=record.client_order_id,
        )
        child_ids = algo.get("ordIdList") or []
        if isinstance(child_ids, str):
            child_ids = [child_ids] if child_ids else []
        latest_child = str(
            (child_ids[-1] if child_ids else "")
            or algo.get("ordId")
            or ""
        )
        if latest_child:
            child = self._client.get_order(
                instrument=record.plan.instrument,
                order_id=latest_child,
            )
            if child:
                child = dict(child)
                child["_pa_child_order_id"] = latest_child
                return child
        algo_state = str(algo.get("state") or "").lower()
        if algo_state in {"canceled", "order_failed", "failed"}:
            return {
                "state": "canceled" if algo_state == "canceled" else "rejected",
                "accFillSz": "0",
                "avgPx": "",
                "sMsg": str(algo.get("failReason") or algo.get("sMsg") or ""),
            }
        return {
            "state": "live",
            "accFillSz": "0",
            "avgPx": "",
        }

    @staticmethod
    def _submitted_timed_out(record: ExecutionRecord) -> bool:
        submitted = str(record.broker_state.get("entry_submitted_at") or "")
        if not submitted:
            return False
        try:
            submitted_at = datetime.fromisoformat(submitted).astimezone(UTC)
        except ValueError:
            return False
        timeout = int(record.broker_state.get("entry_timeout_seconds") or 0)
        if timeout <= 0:
            return False
        return (datetime.now(UTC) - submitted_at).total_seconds() >= timeout

    def _cancel_entry_write(self, record: ExecutionRecord) -> ExecutionRecord:
        kind = str(record.broker_state.get("entry_kind") or "regular")
        child_order_id = str(
            record.broker_state.get("entry_child_order_id") or ""
        )
        try:
            if kind == "algo" and child_order_id:
                self._client.cancel_order(
                    instrument=record.plan.instrument,
                    order_id=child_order_id,
                )
            elif kind == "algo":
                self._client.cancel_algo_orders(
                    [{"algoId": record.broker_order_id, "instId": record.plan.instrument}]
                )
            else:
                self._client.cancel_order(
                    instrument=record.plan.instrument,
                    order_id=record.broker_order_id,
                    client_order_id=record.client_order_id,
                )
        except BrokerApiError as exc:
            raise BrokerRejected(str(exc)) from exc
        except BrokerTransportError as exc:
            state = dict(record.broker_state)
            state["entry_cancel_requested"] = True
            state["entry_cancel_status"] = "unknown"
            state["entry_cancel_target"] = (
                "child_order" if child_order_id else kind
            )
            state["write_unknown"] = "cancel_entry"
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "OKX 撤销入场状态不明，保持只读对账",
                }
            )
        state = dict(record.broker_state)
        state["entry_cancel_requested"] = True
        state["entry_cancel_status"] = "submitted"
        state["entry_cancel_target"] = (
            "child_order" if child_order_id else kind
        )
        return record.model_copy(
            update={
                "broker_state": state,
                "state_reason": "已请求撤销 OKX 未成交入场数量",
            }
        )

    def _confirmed_entry_quantity(
        self,
        record: ExecutionRecord,
        *,
        reported_quantity: Decimal,
        reported_average: Decimal | None,
    ) -> tuple[Decimal, Decimal, Decimal | None, dict[str, str]]:
        """Return gross fill and safely sellable/protectable quantity."""
        gross = reported_quantity
        average = reported_average
        fill_rows: list[dict[str, Any]] = []
        needs_fills = gross <= 0 or record.plan.product == "spot"
        if needs_fills:
            order_id = str(
                record.broker_state.get("entry_child_order_id")
                or (
                    record.broker_order_id
                    if str(record.broker_state.get("entry_kind")) != "algo"
                    else ""
                )
            )
            if not order_id:
                raise ReconciliationError(
                    "OKX 已成交入场缺少可查询的普通订单号"
                )
            fill_rows = self._client.fills(
                instrument=record.plan.instrument,
                order_id=order_id,
            )
            confirmed = sum(
                (
                    _decimal(item.get("fillSz"), default=Decimal("0"))
                    or Decimal("0")
                )
                for item in fill_rows
            )
            if confirmed <= 0:
                raise ReconciliationError(
                    "OKX 显示已成交但成交明细尚未返回可靠数量"
                )
            if gross > 0 and confirmed != gross:
                raise ReconciliationError(
                    "OKX 订单成交量与成交明细暂不一致，禁止建立保护"
                )
            gross = confirmed
            if average is None:
                weighted = sum(
                    (
                        (_decimal(item.get("fillSz"), default=Decimal("0")) or Decimal("0"))
                        * (_decimal(item.get("fillPx"), default=Decimal("0")) or Decimal("0"))
                    )
                    for item in fill_rows
                )
                if weighted > 0:
                    average = weighted / gross
        if gross > record.plan.quantity:
            raise ReconciliationError(
                "OKX 实际成交数量超过本次计划量"
            )

        metadata: dict[str, str] = {}
        managed = gross
        if record.plan.product == "spot":
            preflight = record.preflight
            if preflight is None:
                raise ReconciliationError("OKX 现货保护缺少预检")
            base_currency = str(
                preflight.broker_metadata.get("base_currency") or ""
            ).upper()
            if not base_currency:
                raise ReconciliationError("OKX 现货品种缺少基础币信息")
            base_fee = Decimal("0")
            for item in fill_rows:
                fee = _decimal(item.get("fee"), default=Decimal("0")) or Decimal("0")
                fee_currency = str(item.get("feeCcy") or "").upper()
                if fee != 0 and not fee_currency:
                    raise ReconciliationError(
                        "OKX 现货成交费用缺少币种，禁止推算保护数量"
                    )
                if fee_currency == base_currency and fee < 0:
                    base_fee += fee
            net = gross + base_fee
            step = preflight.quantity_step or Decimal("1")
            minimum = preflight.minimum_quantity or step
            managed = (net // step) * step
            dust = net - managed
            metadata = {
                "spot_gross_filled_quantity": str(gross),
                "spot_base_fee": str(base_fee),
                "spot_net_filled_quantity": str(net),
                "spot_dust_quantity": str(dust),
            }
            if net <= 0 or managed < minimum:
                raise ReconciliationError(
                    "OKX 现货扣除基础币手续费后的可卖数量低于最小交易量，"
                    "无法建立不会超量的保护单"
                )
        return gross, managed, average, metadata

    @staticmethod
    def _entry_quantity_unconfirmed(
        record: ExecutionRecord,
        message: str,
    ) -> ExecutionRecord:
        state = dict(record.broker_state)
        state["entry_fill_quantity_unknown"] = True
        state["write_unknown"] = "entry_fill_quantity"
        return record.model_copy(
            update={
                "broker_state": state,
                "needs_attention": True,
                "last_error": message,
                "state_reason": (
                    "OKX 入场已成交但实际可保护数量未确认，保持只读对账"
                ),
            }
        )

    @staticmethod
    def _split_targets(
        quantity: Decimal,
        *,
        step: Decimal,
        minimum: Decimal,
        tp1: Decimal,
        tp2: Decimal,
    ) -> list[dict[str, Any]]:
        if tp1 == tp2:
            return [{"index": 1, "quantity": str(quantity), "take_profit": str(tp1)}]
        total_steps = int(quantity / step)
        first = Decimal(total_steps // 2) * step
        second = quantity - first
        if first < minimum or second < minimum:
            return [{"index": 1, "quantity": str(quantity), "take_profit": str(tp1)}]
        return [
            {"index": 1, "quantity": str(first), "take_profit": str(tp1)},
            {"index": 2, "quantity": str(second), "take_profit": str(tp2)},
        ]

    def _initialise_protection_targets(self, record: ExecutionRecord) -> ExecutionRecord:
        preflight = record.preflight
        if preflight is None:
            raise ReconciliationError("OKX 保护阶段缺少预检")
        state = dict(record.broker_state)
        if state.get("protection_targets"):
            return record
        step = preflight.quantity_step or Decimal("1")
        minimum = preflight.minimum_quantity or step
        targets = self._split_targets(
            record.remaining_quantity,
            step=step,
            minimum=minimum,
            tp1=preflight.take_profit_1,
            tp2=preflight.take_profit_2,
        )
        for target in targets:
            index = int(target["index"])
            target.update(
                {
                    "client_algo_id": _client_id(record.id, "protect", index),
                    "algo_id": "",
                    "state": "planned",
                    "retry": 0,
                    "exit_order_ids": [],
                    "filled_quantity": "0",
                    "average_fill_price": "",
                }
            )
        state["protection_targets"] = targets
        state["protection_base_quantity"] = str(record.remaining_quantity)
        state["realized_before_protection"] = (
            str(record.realized_pnl)
            if record.realized_pnl is not None
            else ""
        )
        return record.model_copy(
            update={
                "state": ExecutionState.PROTECTING,
                "broker_state": state,
                "state_reason": "准备建立 OKX 原生止盈止损",
            }
        )

    def _place_one_protection(self, record: ExecutionRecord) -> ExecutionRecord:
        preflight = record.preflight
        if preflight is None:
            raise ReconciliationError("OKX 保护阶段缺少预检")
        state = dict(record.broker_state)
        targets = [dict(item) for item in state.get("protection_targets") or []]
        pending = next((item for item in targets if not item.get("algo_id")), None)
        if pending is None:
            return record.model_copy(
                update={
                    "state": ExecutionState.OPEN,
                    "state_reason": "OKX 仓位已成交且原生保护已建立",
                    "needs_attention": False,
                }
            )
        if pending.get("state") == "unknown":
            try:
                found = self._client.get_algo_order(
                    client_algo_id=str(pending["client_algo_id"])
                )
            except (BrokerApiError, BrokerTransportError) as exc:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": "OKX 保护单提交状态未知，正在只读对账",
                    }
                )
            algo_id = str(found.get("algoId") or "")
            if not algo_id:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": (
                            "OKX 保护单提交状态未知；未查到原订单，禁止自动重发"
                        ),
                    }
                )
            for target in targets:
                if target["client_algo_id"] == pending["client_algo_id"]:
                    target["algo_id"] = algo_id
                    target["state"] = str(found.get("state") or "live")
                    break
            state["protection_targets"] = targets
            state.pop("write_unknown", None)
            all_placed = all(item.get("algo_id") for item in targets)
            return record.model_copy(
                update={
                    "state": (
                        ExecutionState.OPEN if all_placed else ExecutionState.PROTECTING
                    ),
                    "broker_state": state,
                    "needs_attention": False,
                    "last_error": "",
                    "state_reason": "已按客户算法订单号恢复 OKX 保护单",
                }
            )
        plan = record.plan
        body: dict[str, Any] = {
            "instId": plan.instrument,
            "tdMode": str(preflight.broker_metadata["trade_mode"]),
            "side": _exit_side(plan),
            "ordType": "oco",
            "sz": str(pending["quantity"]),
            "algoClOrdId": str(pending["client_algo_id"]),
            "tpTriggerPx": str(pending["take_profit"]),
            "tpOrdPx": "-1",
            "slTriggerPx": str(preflight.stop_loss),
            "slOrdPx": "-1",
            "tpTriggerPxType": "last" if plan.product == "spot" else "mark",
            "slTriggerPxType": "last" if plan.product == "spot" else "mark",
        }
        if plan.product == "swap":
            pos_side = self._position_side(plan, preflight)
            body["posSide"] = pos_side
            if pos_side == "net":
                body["reduceOnly"] = True
        try:
            response = self._client.place_algo_order(body)
        except BrokerApiError as exc:
            raise BrokerRejected(f"OKX 保护单被拒绝：{exc}") from exc
        except BrokerTransportError as exc:
            for target in targets:
                if target["client_algo_id"] == pending["client_algo_id"]:
                    target["state"] = "unknown"
                    break
            state["protection_targets"] = targets
            state["write_unknown"] = "protection"
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "OKX 保护单提交状态未知，已停写并禁止自动重发",
                }
            )
        algo_id = str(response.get("algoId") or "")
        if not algo_id:
            for target in targets:
                if target["client_algo_id"] == pending["client_algo_id"]:
                    target["state"] = "unknown"
                    break
            state["protection_targets"] = targets
            state["write_unknown"] = "protection"
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": True,
                    "last_error": "OKX 保护单响应缺少 algoId",
                    "state_reason": "OKX 保护单提交状态未知，已停写并禁止自动重发",
                }
            )
        for target in targets:
            if target["client_algo_id"] == pending["client_algo_id"]:
                target["algo_id"] = algo_id
                target["state"] = "live"
                break
        state["protection_targets"] = targets
        all_placed = all(item.get("algo_id") for item in targets)
        return record.model_copy(
            update={
                "state": ExecutionState.OPEN if all_placed else ExecutionState.PROTECTING,
                "broker_state": state,
                "state_reason": (
                    "OKX 仓位已成交且原生保护已建立"
                    if all_placed
                    else "正在逐笔建立 OKX 原生保护"
                ),
                "needs_attention": False,
            }
        )

    def _entry_reconcile(
        self,
        record: ExecutionRecord,
        *,
        allow_writes: bool,
    ) -> ExecutionRecord:
        try:
            if str(record.broker_state.get("entry_kind")) == "algo":
                raw = self._algo_entry_order(record)
            else:
                raw = self._regular_entry_order(record)
        except BrokerApiError as exc:
            raise ReconciliationError(f"OKX 入场订单查询失败：{exc}") from exc
        except BrokerTransportError as exc:
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "OKX 入场订单暂时无法查询",
                }
            )

        child_order_id = str(raw.get("_pa_child_order_id") or "")
        if (
            child_order_id
            and child_order_id
            != str(record.broker_state.get("entry_child_order_id") or "")
        ):
            broker_state = dict(record.broker_state)
            broker_state["entry_child_order_id"] = child_order_id
            return record.model_copy(
                update={
                    "broker_state": broker_state,
                    "state_reason": (
                        "已持久化 OKX 突破单触发后的普通子订单号"
                    ),
                }
            )

        state = _okx_order_state(raw)
        if str(raw.get("state") or "").lower() == "rejected":
            state = "rejected"
        filled = _decimal(raw.get("accFillSz"), default=Decimal("0")) or Decimal("0")
        avg = _decimal(raw.get("avgPx"))
        settled_state = dict(record.broker_state)
        if state in {"rejected", "canceled", "filled"}:
            if settled_state.get("write_unknown") == "cancel_entry":
                settled_state.pop("write_unknown", None)
            settled_state.pop("entry_cancel_status", None)
        if state == "rejected":
            return record.model_copy(
                update={
                    "state": ExecutionState.REJECTED,
                    "filled_quantity": filled,
                    "remaining_quantity": max(record.plan.quantity - filled, Decimal("0")),
                    "average_fill_price": avg,
                    "state_reason": "OKX 入场订单被拒绝",
                    "last_error": str(raw.get("sMsg") or raw.get("msg") or ""),
                    "broker_state": settled_state,
                }
            )
        if state == "canceled":
            if filled <= 0:
                return record.model_copy(
                    update={
                        "state": ExecutionState.CANCELED,
                        "filled_quantity": Decimal("0"),
                        "remaining_quantity": Decimal("0"),
                        "state_reason": "OKX 入场订单已撤销且未成交",
                        "broker_state": settled_state,
                    }
                )
            try:
                gross, managed, avg, metadata = self._confirmed_entry_quantity(
                    record,
                    reported_quantity=filled,
                    reported_average=avg,
                )
            except (
                BrokerApiError,
                BrokerTransportError,
                ReconciliationError,
            ) as exc:
                return self._entry_quantity_unconfirmed(record, str(exc))
            broker_state = dict(settled_state)
            broker_state.update(metadata)
            broker_state.pop("entry_fill_quantity_unknown", None)
            if broker_state.get("write_unknown") == "entry_fill_quantity":
                broker_state.pop("write_unknown", None)
            updated = record.model_copy(
                update={
                    "filled_quantity": gross,
                    "remaining_quantity": managed,
                    "average_fill_price": avg,
                    "realized_pnl": record.realized_pnl or Decimal("0"),
                    "broker_state": broker_state,
                }
            )
            return self._initialise_protection_targets(updated)
        if state == "filled":
            try:
                gross, managed, avg, metadata = self._confirmed_entry_quantity(
                    record,
                    reported_quantity=filled,
                    reported_average=avg,
                )
            except (
                BrokerApiError,
                BrokerTransportError,
                ReconciliationError,
            ) as exc:
                return self._entry_quantity_unconfirmed(record, str(exc))
            broker_state = dict(settled_state)
            broker_state.update(metadata)
            broker_state.pop("entry_fill_quantity_unknown", None)
            if broker_state.get("write_unknown") == "entry_fill_quantity":
                broker_state.pop("write_unknown", None)
            updated = record.model_copy(
                update={
                    "filled_quantity": gross,
                    "remaining_quantity": managed,
                    "average_fill_price": avg,
                    "realized_pnl": record.realized_pnl or Decimal("0"),
                    "broker_state": broker_state,
                }
            )
            return self._initialise_protection_targets(updated)
        if state == "partially_filled":
            updated = record.model_copy(
                update={
                    "state": ExecutionState.PARTIALLY_FILLED,
                    "filled_quantity": filled,
                    "remaining_quantity": filled,
                    "average_fill_price": avg,
                    "state_reason": "OKX 入场部分成交，准备撤销剩余数量",
                }
            )
            if not bool(updated.broker_state.get("entry_cancel_requested")):
                if not bool(updated.broker_state.get("entry_cancel_intent")):
                    broker_state = dict(updated.broker_state)
                    broker_state["entry_cancel_intent"] = True
                    return updated.model_copy(
                        update={
                            "broker_state": broker_state,
                            "needs_attention": not allow_writes,
                            "state_reason": "已持久化 OKX 部分成交撤余单意图",
                        }
                    )
                if not allow_writes:
                    return updated.model_copy(
                        update={
                            "needs_attention": True,
                            "state_reason": "OKX 入场部分成交；启用本次会话后才能撤余单并保护",
                        }
                    )
                return self._cancel_entry_write(updated)
            return updated

        timed_out = self._entry_age_seconds(record) >= self._entry_timeout_seconds
        if timed_out and not bool(record.broker_state.get("entry_cancel_requested")):
            if not bool(record.broker_state.get("entry_cancel_intent")):
                state_data = dict(record.broker_state)
                state_data["entry_cancel_intent"] = True
                return record.model_copy(
                    update={
                        "broker_state": state_data,
                        "needs_attention": not allow_writes,
                        "state_reason": "已持久化 OKX 超时撤单意图",
                    }
                )
            if allow_writes:
                return self._cancel_entry_write(record)
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "state_reason": "OKX 入场等待超时；启用本次会话后才能撤单",
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
                    "OKX 撤销入场状态不明，持续只读查询"
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

    def _swap_order_realized_pnl(
        self,
        *,
        instrument: str,
        order_id: str,
    ) -> Decimal | None:
        rows = self._client.fills(
            instrument_type="SWAP",
            instrument=instrument,
            order_id=order_id,
        )
        if not rows:
            return None
        total = Decimal("0")
        for row in rows:
            pnl = _decimal(row.get("fillPnl"))
            if pnl is None:
                return None
            total += pnl
        return total

    def _protection_status(
        self,
        target: dict[str, Any],
        record: ExecutionRecord,
    ) -> dict[str, Any]:
        instrument = record.plan.instrument
        algo_id = str(target.get("algo_id") or "")
        if not algo_id:
            return target
        algo = self._client.get_algo_order(algo_id=algo_id)
        state = str(algo.get("state") or "").lower()
        target["state"] = state or "unknown"
        child_ids = algo.get("ordIdList") or []
        if isinstance(child_ids, str):
            child_ids = [child_ids] if child_ids else []
        single = str(algo.get("ordId") or "")
        if single and single not in child_ids:
            child_ids.append(single)
        total = Decimal("0")
        notional = Decimal("0")
        realized = Decimal("0")
        realized_known = True
        child_states: list[str] = []
        unique_child_ids = list(dict.fromkeys(str(item) for item in child_ids if item))
        for order_id in unique_child_ids:
            order = self._client.get_order(instrument=instrument, order_id=str(order_id))
            child_states.append(_okx_order_state(order))
            qty = _decimal(order.get("accFillSz"), default=Decimal("0")) or Decimal("0")
            price = _decimal(order.get("avgPx"), default=Decimal("0")) or Decimal("0")
            total += qty
            notional += qty * price
            if record.plan.product == "swap" and qty > 0:
                fill_pnl = self._swap_order_realized_pnl(
                    instrument=instrument,
                    order_id=str(order_id),
                )
                if fill_pnl is None:
                    realized_known = False
                else:
                    realized += fill_pnl
        target["exit_order_ids"] = unique_child_ids
        target["filled_quantity"] = str(total)
        target["average_fill_price"] = str(notional / total) if total > 0 else ""
        if record.plan.product == "swap":
            target["realized_pnl"] = str(realized) if realized_known else ""
            target["realized_pnl_known"] = realized_known
        target["child_states"] = child_states
        target["child_active"] = any(
            state in {"pending", "partially_filled"} for state in child_states
        )
        return target

    def _queue_protection_replacements(
        self,
        record: ExecutionRecord,
        targets: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        preflight = record.preflight
        if preflight is None:
            return targets, False, True
        minimum = preflight.minimum_quantity or preflight.quantity_step or Decimal("1")
        additions: list[dict[str, Any]] = []
        unprotected = False
        for target in targets:
            quantity = (
                _decimal(target.get("quantity"), default=Decimal("0"))
                or Decimal("0")
            )
            filled = (
                _decimal(target.get("filled_quantity"), default=Decimal("0"))
                or Decimal("0")
            )
            child_states = target.get("child_states") or []
            terminal_without_full_fill = target.get("state") in {
                "canceled",
                "order_failed",
            } or (
                target.get("state") == "effective"
                and bool(child_states)
                and not bool(target.get("child_active"))
            )
            if (
                not terminal_without_full_fill
                or filled >= quantity
                or target.get("replacement_created")
            ):
                continue
            remainder = max(quantity - filled, Decimal("0"))
            target["replacement_created"] = True
            retry = int(target.get("retry") or 0) + 1
            if remainder < minimum or retry > 3:
                unprotected = remainder > 0
                continue
            index = int(target.get("index") or 1)
            additions.append(
                {
                    "index": index,
                    "quantity": str(remainder),
                    "take_profit": str(target["take_profit"]),
                    "client_algo_id": _client_id(
                        record.id,
                        "protect",
                        index * 10 + retry,
                    ),
                    "algo_id": "",
                    "state": "planned",
                    "retry": retry,
                    "exit_order_ids": [],
                    "filled_quantity": "0",
                    "average_fill_price": "",
                }
            )
        targets.extend(additions)
        return targets, bool(additions), unprotected

    def _open_reconcile(
        self,
        record: ExecutionRecord,
        *,
        allow_writes: bool,
    ) -> ExecutionRecord:
        state = dict(record.broker_state)
        targets = [dict(item) for item in state.get("protection_targets") or []]
        try:
            targets = [
                self._protection_status(target, record) for target in targets
            ]
        except (BrokerApiError, BrokerTransportError) as exc:
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "OKX 保护单状态暂时无法查询",
                }
            )
        state["protection_targets"] = targets
        exited = sum(
            (
                _decimal(target.get("filled_quantity"), default=Decimal("0"))
                or Decimal("0")
            )
            for target in targets
        )
        base_quantity = (
            _decimal(
                state.get("protection_base_quantity"),
                default=record.remaining_quantity,
            )
            or record.remaining_quantity
        )
        remaining = max(base_quantity - exited, Decimal("0"))
        realized = self._realized_with_baseline(record, targets, state)
        if remaining <= 0:
            return record.model_copy(
                update={
                    "state": ExecutionState.CLOSED,
                    "remaining_quantity": Decimal("0"),
                    "realized_pnl": realized,
                    "unrealized_pnl": Decimal("0"),
                    "broker_state": state,
                    "state_reason": "OKX 仓位已通过保护或退出订单关闭",
                    "needs_attention": realized is None,
                    "last_error": "",
                }
            )
        targets, replacement_added, unprotected = self._queue_protection_replacements(
            record,
            targets,
        )
        if replacement_added:
            state["protection_targets"] = targets
            return record.model_copy(
                update={
                    "state": ExecutionState.PROTECTING,
                    "remaining_quantity": remaining,
                    "realized_pnl": realized,
                    "broker_state": state,
                    "needs_attention": not allow_writes,
                    "state_reason": (
                        "OKX 保护单未覆盖全部剩余仓位，准备按差额重建"
                    ),
                }
            )
        unrealized, currency = self._execution_unrealized(record, remaining)
        canceled_unfilled = [
            item
            for item in targets
            if item.get("state") in {"canceled", "order_failed"}
            and (_decimal(item.get("filled_quantity"), default=Decimal("0")) or 0) == 0
        ]
        attention = (
            unprotected
            or any(
                not item.get("replacement_created") for item in canceled_unfilled
            )
            or realized is None
        )
        return record.model_copy(
            update={
                "state": ExecutionState.OPEN,
                "remaining_quantity": remaining,
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "pnl_currency": currency,
                "broker_state": state,
                "needs_attention": attention,
                "state_reason": (
                    "OKX 保护单失效或成交盈亏尚未完成对账"
                    if attention
                    else "OKX 仓位与保护单监控中"
                ),
            }
        )

    @staticmethod
    def _realized_from_targets(
        record: ExecutionRecord,
        targets: list[dict[str, Any]],
    ) -> Decimal | None:
        if record.plan.product == "swap":
            total = Decimal("0")
            for target in targets:
                quantity = (
                    _decimal(target.get("filled_quantity"), default=Decimal("0"))
                    or Decimal("0")
                )
                if quantity <= 0:
                    continue
                value = _decimal(target.get("realized_pnl"))
                if value is None or not bool(target.get("realized_pnl_known")):
                    return None
                total += value
            return total
        entry = record.average_fill_price or record.plan.entry_price
        pnl = Decimal("0")
        for target in targets:
            qty = _decimal(target.get("filled_quantity"), default=Decimal("0")) or Decimal("0")
            price = _decimal(target.get("average_fill_price"))
            if qty <= 0 or price is None:
                continue
            pnl += (price - entry) * qty if record.plan.direction == "long" else (entry - price) * qty
        return pnl

    @classmethod
    def _realized_with_baseline(
        cls,
        record: ExecutionRecord,
        targets: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> Decimal | None:
        if "realized_before_protection" in state:
            baseline = _decimal(state.get("realized_before_protection"))
        else:
            baseline = record.realized_pnl or Decimal("0")
        current = cls._realized_from_targets(record, targets)
        if baseline is None or current is None:
            return None
        return baseline + current

    def _execution_unrealized(
        self,
        record: ExecutionRecord,
        remaining: Decimal,
    ) -> tuple[Decimal | None, str]:
        preflight = record.preflight
        if preflight is None:
            return None, ""
        if record.plan.product == "swap":
            try:
                positions = self._client.positions(instrument=record.plan.instrument)
            except (BrokerApiError, BrokerTransportError):
                return record.unrealized_pnl, record.pnl_currency
            pos_side = self._position_side(record.plan, preflight)
            for item in positions:
                if str(item.get("instId")) != record.plan.instrument:
                    continue
                item_side = str(item.get("posSide") or "net")
                if pos_side != "net" and item_side != pos_side:
                    continue
                return (
                    _decimal(item.get("upl")),
                    str(item.get("ccy") or preflight.broker_metadata.get("settle_currency") or ""),
                )
            return Decimal("0"), str(preflight.broker_metadata.get("settle_currency") or "")
        try:
            ticker = self._client.ticker(record.plan.instrument)
        except (BrokerApiError, BrokerTransportError):
            return record.unrealized_pnl, record.pnl_currency
        last = _decimal(ticker.get("last"))
        entry = record.average_fill_price
        if last is None or entry is None:
            return None, str(preflight.broker_metadata.get("quote_currency") or "")
        pnl = (last - entry) * remaining
        return pnl, str(preflight.broker_metadata.get("quote_currency") or "")

    def _cancel_one_protection(self, record: ExecutionRecord) -> ExecutionRecord:
        state = dict(record.broker_state)
        targets = [dict(item) for item in state.get("protection_targets") or []]
        try:
            targets = [
                self._protection_status(item, record)
                for item in targets
            ]
        except (BrokerApiError, BrokerTransportError) as exc:
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "正在确认 OKX 保护单撤销状态",
                }
            )
        exited = sum(
            (
                _decimal(item.get("filled_quantity"), default=Decimal("0"))
                or Decimal("0")
            )
            for item in targets
        )
        base_quantity = (
            _decimal(
                state.get("protection_base_quantity"),
                default=record.remaining_quantity,
            )
            or record.remaining_quantity
        )
        remaining = max(base_quantity - exited, Decimal("0"))
        realized = self._realized_with_baseline(record, targets, state)
        for item in targets:
            if (
                item.get("cancel_status") == "unknown"
                and item.get("state") in {"canceled", "effective", "order_failed"}
            ):
                item["cancel_status"] = "confirmed"
        if not any(item.get("cancel_status") == "unknown" for item in targets):
            state.pop("write_unknown", None)
        state["protection_targets"] = targets
        if remaining <= 0:
            return record.model_copy(
                update={
                    "state": ExecutionState.CLOSED,
                    "remaining_quantity": Decimal("0"),
                    "realized_pnl": realized,
                    "unrealized_pnl": Decimal("0"),
                    "broker_state": state,
                    "state_reason": "OKX 保护单在主动离场前已关闭仓位",
                    "needs_attention": realized is None,
                }
            )
        if any(bool(item.get("child_active")) for item in targets):
            return record.model_copy(
                update={
                    "remaining_quantity": remaining,
                    "realized_pnl": realized,
                    "broker_state": state,
                    "state_reason": "等待已触发的 OKX 保护子订单完成",
                }
            )
        if any(
            item.get("cancel_requested")
            and item.get("cancel_status") in {"submitted", "unknown"}
            and item.get("state") not in {"canceled", "effective", "order_failed"}
            for item in targets
        ):
            unknown = any(
                item.get("cancel_status") == "unknown"
                for item in targets
                if item.get("cancel_requested")
            )
            return record.model_copy(
                update={
                    "remaining_quantity": remaining,
                    "realized_pnl": realized,
                    "broker_state": state,
                    "needs_attention": unknown,
                    "state_reason": (
                        "OKX 撤销保护单状态未知，保持停写并只读确认"
                        if unknown
                        else "等待 OKX 确认保护单已撤销"
                    ),
                }
            )
        cancel_intent = next(
            (
                item
                for item in targets
                if item.get("algo_id")
                and item.get("cancel_requested")
                and item.get("cancel_status") == "intent"
            ),
            None,
        )
        if cancel_intent is not None:
            try:
                self._client.cancel_algo_orders(
                    [
                        {
                            "algoId": str(cancel_intent["algo_id"]),
                            "instId": record.plan.instrument,
                        }
                    ]
                )
            except BrokerApiError as exc:
                raise BrokerRejected(f"OKX 撤销保护单失败：{exc}") from exc
            except BrokerTransportError as exc:
                cancel_intent["cancel_status"] = "unknown"
                state["protection_targets"] = targets
                state["write_unknown"] = "cancel_protection"
                return record.model_copy(
                    update={
                        "broker_state": state,
                        "realized_pnl": realized,
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": "OKX 撤销保护单状态未知，已停写",
                    }
                )
            cancel_intent["cancel_status"] = "submitted"
            state["protection_targets"] = targets
            return record.model_copy(
                update={
                    "broker_state": state,
                    "realized_pnl": realized,
                    "state_reason": "正在撤销 OKX 保护单后主动离场",
                }
            )
        target = next(
            (
                item
                for item in targets
                if item.get("algo_id")
                and item.get("state") not in {"canceled", "effective", "order_failed"}
                and not item.get("cancel_requested")
            ),
            None,
        )
        if target is None:
            state["exit_phase"] = "submit_exit"
            return record.model_copy(
                update={
                    "remaining_quantity": remaining,
                    "realized_pnl": realized,
                    "broker_state": state,
                }
            )
        target["cancel_requested"] = True
        target["cancel_status"] = "intent"
        state["protection_targets"] = targets
        return record.model_copy(
            update={
                "broker_state": state,
                "realized_pnl": realized,
                "state_reason": "已持久化 OKX 保护单撤销意图",
            }
        )

    def _submit_exit(self, record: ExecutionRecord) -> ExecutionRecord:
        preflight = record.preflight
        if preflight is None:
            raise ReconciliationError("OKX 主动离场缺少预检")
        state = dict(record.broker_state)
        exit_order = dict(state.get("exit_order") or {})
        client_id = str(exit_order.get("client_order_id") or "")
        if not client_id:
            raise ReconciliationError("OKX 主动离场客户订单号尚未持久化")
        body: dict[str, Any] = {
            "instId": record.plan.instrument,
            "tdMode": str(preflight.broker_metadata["trade_mode"]),
            "clOrdId": client_id,
            "side": _exit_side(record.plan),
            "ordType": "market",
            "sz": str(exit_order.get("quantity") or record.remaining_quantity),
        }
        if record.plan.product == "spot":
            body["tgtCcy"] = "base_ccy"
        else:
            pos_side = self._position_side(record.plan, preflight)
            body["posSide"] = pos_side
            if pos_side == "net":
                body["reduceOnly"] = True
        try:
            response = self._client.place_order(body)
        except BrokerApiError as exc:
            raise BrokerRejected(f"OKX 主动离场被拒绝：{exc}") from exc
        except BrokerTransportError as exc:
            state["exit_phase"] = "exit_unknown"
            state["write_unknown"] = "exit"
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "OKX 主动离场提交状态未知，已停写并禁止自动重发",
                }
            )
        order_id = str(response.get("ordId") or "")
        if not order_id:
            state["exit_phase"] = "exit_unknown"
            state["write_unknown"] = "exit"
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": True,
                    "last_error": "OKX 主动离场响应缺少 ordId",
                    "state_reason": "OKX 主动离场提交状态未知，已停写并禁止自动重发",
                }
            )
        state["exit_phase"] = "wait_exit"
        exit_order["order_id"] = order_id
        state["exit_order"] = exit_order
        state.pop("write_unknown", None)
        return record.model_copy(
            update={
                "broker_state": state,
                "state_reason": "OKX 主动离场订单已受理",
            }
        )

    def _exit_reconcile(
        self,
        record: ExecutionRecord,
        *,
        allow_writes: bool,
    ) -> ExecutionRecord:
        state = dict(record.broker_state)
        phase = str(state.get("exit_phase") or "cancel_protection")
        if phase == "cancel_protection":
            targets = state.get("protection_targets") or []
            if any(
                item.get("cancel_status") in {"submitted", "unknown"}
                for item in targets
            ):
                return self._cancel_one_protection(record)
            if not allow_writes:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": "主动离场待执行；需启用本次会话",
                    }
                )
            return self._cancel_one_protection(record)
        if phase == "submit_exit":
            state["exit_phase"] = "submit_exit_ready"
            state["exit_order"] = {
                "order_id": "",
                "client_order_id": _client_id(record.id, "exit"),
                "quantity": str(record.remaining_quantity),
                "realized_before_exit": (
                    str(record.realized_pnl)
                    if record.realized_pnl is not None
                    else ""
                ),
            }
            return record.model_copy(
                update={
                    "broker_state": state,
                    "state_reason": "已持久化 OKX 主动离场订单意图",
                }
            )
        if phase == "submit_exit_ready":
            if not allow_writes:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": "保护单已撤，需启用本次会话完成主动离场",
                    }
                )
            return self._submit_exit(record)
        if phase == "exit_unknown":
            exit_order = dict(state.get("exit_order") or {})
            client_id = str(exit_order.get("client_order_id") or "")
            try:
                raw = self._client.get_order(
                    instrument=record.plan.instrument,
                    client_order_id=client_id,
                )
            except (BrokerApiError, BrokerTransportError) as exc:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": "OKX 主动离场提交状态未知，正在只读对账",
                    }
                )
            order_id = str(raw.get("ordId") or "")
            if not order_id:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": (
                            "OKX 主动离场提交状态未知；未查到原订单，禁止自动重发"
                        ),
                    }
                )
            exit_order["order_id"] = order_id
            state["exit_order"] = exit_order
            state["exit_phase"] = "wait_exit"
            state.pop("write_unknown", None)
            return record.model_copy(
                update={
                    "broker_state": state,
                    "needs_attention": False,
                    "last_error": "",
                    "state_reason": "已按客户订单号恢复 OKX 主动离场订单",
                }
            )

        exit_order = dict(state.get("exit_order") or {})
        order_id = str(exit_order.get("order_id") or "")
        client_id = str(exit_order.get("client_order_id") or "")
        try:
            raw = self._client.get_order(
                instrument=record.plan.instrument,
                order_id=order_id,
                client_order_id=client_id,
            )
        except (BrokerApiError, BrokerTransportError) as exc:
            return record.model_copy(
                update={
                    "needs_attention": True,
                    "last_error": str(exc),
                    "state_reason": "OKX 主动离场订单暂时无法查询",
                }
            )
        status = _okx_order_state(raw)
        qty = _decimal(raw.get("accFillSz"), default=Decimal("0")) or Decimal("0")
        price = _decimal(raw.get("avgPx"))
        original_quantity = (
            _decimal(exit_order.get("quantity"), default=record.remaining_quantity)
            or record.remaining_quantity
        )
        remaining = max(original_quantity - qty, Decimal("0"))
        before = _decimal(exit_order.get("realized_before_exit"))
        if record.plan.product == "swap" and qty > 0:
            exit_pnl = self._swap_order_realized_pnl(
                instrument=record.plan.instrument,
                order_id=order_id,
            )
        elif qty > 0 and price is not None:
            entry = record.average_fill_price or record.plan.entry_price
            exit_pnl = (
                (price - entry) * qty
                if record.plan.direction == "long"
                else (entry - price) * qty
            )
        else:
            exit_pnl = Decimal("0")
        realized = before + exit_pnl if before is not None and exit_pnl is not None else None
        exit_order["filled_quantity"] = str(qty)
        exit_order["average_fill_price"] = str(price) if price is not None else ""
        exit_order["realized_pnl"] = str(exit_pnl) if exit_pnl is not None else ""
        state["exit_order"] = exit_order
        if status == "filled":
            if remaining > 0:
                state["protection_targets"] = []
                state["exit_phase"] = ""
                state["exit_order"] = {}
                return record.model_copy(
                    update={
                        "state": ExecutionState.PROTECTING,
                        "remaining_quantity": remaining,
                        "realized_pnl": realized,
                        "broker_state": state,
                        "state_reason": (
                            "OKX 主动离场终态数量不足，按实际剩余仓位恢复保护"
                        ),
                        "needs_attention": True,
                    }
                )
            return record.model_copy(
                update={
                    "state": ExecutionState.CLOSED,
                    "remaining_quantity": remaining,
                    "realized_pnl": realized,
                    "unrealized_pnl": Decimal("0") if remaining <= 0 else record.unrealized_pnl,
                    "broker_state": state,
                    "state_reason": "OKX 主动离场已成交",
                    "needs_attention": realized is None,
                    "last_error": "",
                }
            )
        if status == "canceled":
            if remaining <= 0:
                return record.model_copy(
                    update={
                        "state": ExecutionState.CLOSED,
                        "remaining_quantity": Decimal("0"),
                        "realized_pnl": realized,
                        "unrealized_pnl": Decimal("0"),
                        "broker_state": state,
                        "needs_attention": realized is None,
                        "state_reason": "OKX 主动离场已成交后撤余单",
                    }
                )
            state["protection_targets"] = []
            state["exit_phase"] = ""
            state["exit_order"] = {}
            return record.model_copy(
                update={
                    "state": ExecutionState.PROTECTING,
                    "remaining_quantity": remaining,
                    "realized_pnl": realized,
                    "broker_state": state,
                    "needs_attention": True,
                    "state_reason": "OKX 主动离场订单被撤，按实际剩余仓位恢复保护",
                }
            )
        return record.model_copy(
            update={
                "state": ExecutionState.EXIT_PENDING,
                "remaining_quantity": remaining,
                "realized_pnl": realized,
                "broker_state": state,
                "needs_attention": realized is None and qty > 0,
                "state_reason": "等待 OKX 主动离场成交",
            }
        )

    def reconcile(
        self,
        record: ExecutionRecord,
        *,
        allow_writes: bool,
    ) -> ExecutionRecord:
        if record.state == ExecutionState.UNKNOWN:
            # Querying by deterministic client ID is safe and resolves most write timeouts.
            try:
                if str(record.broker_state.get("entry_kind")) == "algo":
                    raw = self._client.get_algo_order(
                        client_algo_id=record.client_order_id
                    )
                else:
                    raw = self._client.get_order(
                        instrument=record.plan.instrument,
                        client_order_id=record.client_order_id,
                    )
            except (BrokerApiError, BrokerTransportError):
                return record
            if not raw:
                return record
            resolved_id = str(raw.get("ordId") or raw.get("algoId") or record.broker_order_id)
            record = record.model_copy(
                update={
                    "state": ExecutionState.ENTRY_PENDING,
                    "broker_order_id": resolved_id,
                    "state_reason": "已按 OKX 客户订单号对账",
                    "needs_attention": False,
                    "last_error": "",
                }
            )
        if record.state in {
            ExecutionState.ENTRY_PENDING,
            ExecutionState.PARTIALLY_FILLED,
        }:
            return self._entry_reconcile(record, allow_writes=allow_writes)
        if record.state == ExecutionState.PROTECTING:
            if not record.broker_state.get("protection_targets"):
                initialised = self._initialise_protection_targets(record)
                if allow_writes:
                    return initialised
                return initialised.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": "OKX 仓位已成交；需启用本次会话建立保护",
                    }
                )
            pending = next(
                (
                    item
                    for item in record.broker_state.get("protection_targets") or []
                    if not item.get("algo_id")
                ),
                None,
            )
            if pending is None or pending.get("state") == "unknown":
                return self._place_one_protection(record)
            if not allow_writes:
                return record.model_copy(
                    update={
                        "needs_attention": True,
                        "state_reason": "OKX 仓位已成交；需启用本次会话建立保护",
                    }
                )
            return self._place_one_protection(record)
        if record.state == ExecutionState.OPEN:
            return self._open_reconcile(record, allow_writes=allow_writes)
        if record.state == ExecutionState.EXIT_PENDING:
            return self._exit_reconcile(record, allow_writes=allow_writes)
        return record

    def request_exit(
        self,
        record: ExecutionRecord,
        *,
        reason: str,
    ) -> ExecutionRecord:
        if record.state not in {ExecutionState.OPEN, ExecutionState.PROTECTING}:
            raise PreflightError("只有持仓中的 OKX 执行可以主动离场")
        targets = record.broker_state.get("protection_targets") or []
        if any(
            not item.get("algo_id") and item.get("state") == "unknown"
            for item in targets
        ):
            raise PreflightError("OKX 保护单提交状态未知，需先完成对账")
        state = dict(record.broker_state)
        state["exit_phase"] = "cancel_protection"
        state["exit_reason"] = str(reason or "manual")
        return record.model_copy(
            update={
                "state": ExecutionState.EXIT_PENDING,
                "broker_state": state,
                "state_reason": "准备撤销 OKX 保护单并主动离场",
                "needs_attention": False,
            }
        )

    def cancel_entry(self, record: ExecutionRecord) -> ExecutionRecord:
        if record.state not in {
            ExecutionState.ENTRY_PENDING,
            ExecutionState.PARTIALLY_FILLED,
        }:
            raise PreflightError("当前 OKX 执行没有可撤销的入场订单")
        if record.broker_state.get("entry_fill_quantity_unknown"):
            raise PreflightError(
                "OKX 入场已显示成交但数量仍在只读对账，禁止发送撤单"
            )
        if bool(record.broker_state.get("entry_cancel_requested")):
            return record
        return self._cancel_entry_write(record)

    def account_snapshot(
        self,
        plan: ExecutionPlan,
        *,
        account_profile: str | None = None,
    ) -> AccountSnapshot:
        del account_profile
        balances = self._client.balance()
        position_rows = self._client.positions()
        balance = balances[0] if balances else {}
        details = balance.get("details") or []
        base_currency = "USD"
        if plan.product == "spot" and "-" in plan.instrument:
            base_currency = plan.instrument.rsplit("-", 1)[-1]
        elif position_rows:
            base_currency = str(position_rows[0].get("ccy") or "USD")
        detail = next(
            (item for item in details if str(item.get("ccy")) == base_currency),
            details[0] if details else {},
        )
        positions: list[PositionSnapshot] = []
        for item in position_rows:
            qty = _decimal(item.get("pos"), default=Decimal("0")) or Decimal("0")
            if qty == 0:
                continue
            pos_side = str(item.get("posSide") or "net")
            if pos_side == "net":
                direction = "long" if qty > 0 else "short"
            else:
                direction = "long" if pos_side == "long" else "short"
            unrealized = _decimal(item.get("upl"))
            realized = _decimal(item.get("realizedPnl"))
            positions.append(
                PositionSnapshot(
                    instrument=str(item.get("instId") or ""),
                    direction=direction,
                    quantity=abs(qty),
                    available_quantity=None,
                    average_price=_decimal(item.get("avgPx")),
                    mark_price=_decimal(item.get("markPx")),
                    unrealized_pnl=unrealized,
                    realized_pnl=realized,
                    currency=str(item.get("ccy") or ""),
                    leverage=_decimal(item.get("lever")),
                    raw={
                        "margin_mode": str(item.get("mgnMode") or ""),
                        "position_side": pos_side,
                        "liquidation_price": str(item.get("liqPx") or ""),
                    },
                )
            )
        if plan.product == "spot":
            for item in details:
                quantity = _decimal(
                    item.get("cashBal"),
                    default=Decimal("0"),
                ) or Decimal("0")
                if quantity == 0:
                    continue
                currency = str(item.get("ccy") or "")
                positions.append(
                    PositionSnapshot(
                        instrument=currency,
                        direction="long" if quantity > 0 else "short",
                        quantity=abs(quantity),
                        available_quantity=_decimal(item.get("availBal")),
                        average_price=None,
                        mark_price=None,
                        unrealized_pnl=None,
                        realized_pnl=None,
                        currency=currency,
                        leverage=None,
                        raw={
                            "kind": "spot_balance",
                            "equity_usd": str(item.get("eqUsd") or ""),
                            "frozen_balance": str(item.get("frozenBal") or ""),
                        },
                    )
                )
        account_upl = _decimal(detail.get("upl"))
        return AccountSnapshot(
            broker="okx",
            account_profile="okx-demo" if self._client.simulated else "okx-live",
            base_currency=base_currency,
            equity=_decimal(detail.get("eq")),
            cash=_decimal(detail.get("cashBal")),
            available=_decimal(detail.get("availBal")),
            buying_power=_decimal(detail.get("availEq")),
            total_pnl=None,
            unrealized_pnl=account_upl,
            realized_pnl=None,
            positions=positions,
            raw_summary={
                "adjusted_equity": str(balance.get("adjEq") or ""),
                "margin_ratio": str(balance.get("mgnRatio") or ""),
                "notional_usd": str(balance.get("notionalUsd") or ""),
            },
        )
