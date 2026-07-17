from __future__ import annotations

from decimal import Decimal

import pytest

from pa_agent.execution.errors import BrokerTransportError, PreflightError
from pa_agent.execution.longbridge_adapter import LongbridgeAdapter
from pa_agent.execution.models import (
    ExecutionPlan,
    ExecutionRecord,
    ExecutionState,
    utc_now_iso,
)


class FakeLongbridgeSession:
    def __init__(self, *, maximum="10"):
        self.maximum = maximum
        self.calls = []
        self.orders = {}
        self.positions_rows = []
        self.price = Decimal("100")
        self.counter = 0
        self.preflight_error = None
        self.submit_error = None
        self.cancel_error = None
        self.executions_by_order = {}

    def static_info(self, symbol):
        self.calls.append(("static_info", symbol))
        if self.preflight_error:
            raise self.preflight_error
        return {
            "symbol": symbol,
            "lot_size": "1",
            "currency": "USD",
            "name": "Test",
        }

    def positions(self, symbol=None):
        self.calls.append(("positions", symbol))
        return list(self.positions_rows)

    def estimate_max_quantity(self, **kwargs):
        self.calls.append(("estimate", kwargs))
        return {"cash_max_qty": self.maximum, "margin_max_qty": self.maximum}

    def submit_order(self, body):
        self.calls.append(("submit_order", body))
        if self.submit_error:
            raise self.submit_error
        self.counter += 1
        order_id = f"order-{self.counter}"
        self.orders[order_id] = {
            "order_id": order_id,
            "state": "pending",
            "quantity": body["submitted_quantity"],
            "filled_quantity": "0",
            "average_fill_price": "",
            "remark": body["remark"],
        }
        return order_id

    def order(self, order_id):
        self.calls.append(("order", order_id))
        return dict(self.orders[order_id])

    def cancel_order(self, order_id):
        self.calls.append(("cancel_order", order_id))
        if self.cancel_error:
            raise self.cancel_error

    def executions(self, *, symbol, order_id, start_at):
        self.calls.append(("executions", symbol, order_id, start_at))
        return list(self.executions_by_order.get(order_id, []))

    def find_today_order_by_remark(self, *, symbol, remark):
        return self.find_order_by_remark(symbol=symbol, remark=remark)

    def find_order_by_remark(self, *, symbol, remark, start_at=None):
        self.calls.append(("find_order_by_remark", symbol, remark, start_at))
        for order in self.orders.values():
            if order.get("remark") == remark:
                return dict(order)
        return None

    def current_price(self, symbol):
        self.calls.append(("current_price", symbol))
        return self.price

    def account_balances(self):
        return [
            {
                "currency": "USD",
                "total_cash": "5000",
                "remaining_finance_amount": "10000",
                "net_assets": "12000",
                "buy_power": "15000",
                "risk_level": "1",
                "margin_call": "0",
                "max_finance_amount": "20000",
            }
        ]

    def profit_summary(self):
        return {
            "currency": "USD",
            "current_total_asset": "12000",
            "sum_profit": "2000",
            "sum_profit_rate": "0.2",
        }


def _plan(*, account="intraday", fallback=True) -> ExecutionPlan:
    return ExecutionPlan(
        id="d4986e25-d6cb-42cb-b691-b33ffddfb52a",
        analysis_digest="digest",
        analysis_record_path="record.json",
        broker="longbridge",
        environment="live",
        product="securities",
        requested_account=account,
        allow_account_fallback=fallback,
        source_symbol="GLD",
        instrument="GLD.US",
        direction="long",
        entry_type="limit",
        quantity=Decimal("2"),
        entry_price=Decimal("100"),
        take_profit_1=Decimal("110"),
        take_profit_2=Decimal("120"),
        stop_loss=Decimal("95"),
        trade_confidence=90,
        created_at=utc_now_iso(),
        config_fingerprint="config",
    )


def _adapter(intraday, comprehensive):
    sessions = {"intraday": intraday, "comprehensive": comprehensive}
    return LongbridgeAdapter(lambda profile: sessions[profile]), sessions


def test_intraday_capacity_shortage_falls_back_before_submit():
    adapter, _ = _adapter(
        FakeLongbridgeSession(maximum="0"),
        FakeLongbridgeSession(maximum="10"),
    )

    result = adapter.preflight(_plan())

    assert result.selected_account == "comprehensive"
    assert result.warnings and "回退综合账户" in result.warnings[0]


def test_paper_capacity_shortage_never_falls_back_to_live_account():
    paper = FakeLongbridgeSession(maximum="0")
    comprehensive = FakeLongbridgeSession(maximum="10")
    sessions = {"paper": paper, "comprehensive": comprehensive}
    adapter = LongbridgeAdapter(lambda profile: sessions[profile])
    plan = _plan(account="paper", fallback=True).model_copy(
        update={"environment": "demo"}
    )

    with pytest.raises(PreflightError, match="paper 可交易数量"):
        adapter.preflight(plan)

    assert not comprehensive.calls


def test_paper_route_rejects_outside_regular_trading_hours():
    paper = FakeLongbridgeSession(maximum="10")
    adapter = LongbridgeAdapter(
        lambda _profile: paper,
        allow_outside_rth=True,
    )
    plan = _plan(account="paper", fallback=False).model_copy(
        update={"environment": "demo"}
    )

    with pytest.raises(PreflightError, match="模拟账户不支持"):
        adapter.preflight(plan)

    assert not paper.calls


def test_network_or_auth_error_never_falls_back():
    intraday = FakeLongbridgeSession()
    intraday.preflight_error = BrokerTransportError(
        "network",
        write_may_have_reached=False,
    )
    comprehensive = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, comprehensive)

    with pytest.raises(BrokerTransportError):
        adapter.preflight(_plan())

    assert not comprehensive.calls


def test_existing_intraday_position_blocks_instead_of_falling_back():
    intraday = FakeLongbridgeSession()
    intraday.positions_rows = [{"symbol": "GLD.US", "quantity": "1"}]
    comprehensive = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, comprehensive)

    with pytest.raises(PreflightError, match="已有"):
        adapter.preflight(_plan())

    assert not comprehensive.calls


def test_invalid_symbol_is_reported_as_preflight_error():
    adapter, _ = _adapter(FakeLongbridgeSession(), FakeLongbridgeSession())

    with pytest.raises(PreflightError, match="品种格式无效"):
        adapter.preflight(_plan().model_copy(update={"instrument": "GLD"}))


def test_submit_uses_selected_account_and_idempotent_request_id():
    intraday = FakeLongbridgeSession()
    comprehensive = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, comprehensive)
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.SUBMITTING,
        preflight=preflight,
        remaining_quantity=plan.quantity,
    )

    submitted = adapter.submit_entry(record)

    body = next(call[1] for call in intraday.calls if call[0] == "submit_order")
    assert submitted.selected_account == "intraday"
    assert body["client_request_id"] == submitted.client_order_id
    assert body["order_type"] == "LO"
    assert not [call for call in comprehensive.calls if call[0] == "submit_order"]


def test_unknown_entry_recovers_by_persisted_remark_without_resubmit():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    prepared = adapter.prepare_submit(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.READY,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    remark = prepared.broker_state["entry_remark"]
    intraday.orders["recovered-entry"] = {
        "order_id": "recovered-entry",
        "state": "pending",
        "quantity": "2",
        "filled_quantity": "0",
        "average_fill_price": "",
        "remark": remark,
    }
    unknown = prepared.model_copy(
        update={
            "state": ExecutionState.UNKNOWN,
            "needs_attention": True,
        }
    )

    recovered = adapter.reconcile(unknown, allow_writes=False)

    assert recovered.state == ExecutionState.ENTRY_PENDING
    assert recovered.broker_order_id == "recovered-entry"
    assert not [call for call in intraday.calls if call[0] == "submit_order"]


def test_partial_fill_cancels_remainder_then_protects_actual_fill():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {
            "state": "partially_filled",
            "filled_quantity": "1",
            "average_fill_price": "100",
        }
    )

    cancel_intent = adapter.reconcile(record, allow_writes=True)
    partial = adapter.reconcile(cancel_intent, allow_writes=True)
    intraday.orders[record.broker_order_id]["state"] = "canceled"
    protecting = adapter.reconcile(partial, allow_writes=True)
    opened = adapter.reconcile(protecting, allow_writes=True)

    assert partial.broker_state["entry_cancel_requested"] is True
    assert protecting.state == ExecutionState.PROTECTING
    assert protecting.remaining_quantity == Decimal("1")
    stop_body = [call[1] for call in intraday.calls if call[0] == "submit_order"][-1]
    assert stop_body["order_type"] == "MIT"
    assert stop_body["submitted_quantity"] == "1"
    assert opened.state == ExecutionState.OPEN


def test_filled_without_reported_quantity_uses_execution_details():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {
            "state": "filled",
            "filled_quantity": "0",
            "average_fill_price": "",
        }
    )
    intraday.executions_by_order[record.broker_order_id] = [
        {"quantity": "1", "price": "99"},
        {"quantity": "1", "price": "101"},
    ]

    protecting = adapter.reconcile(record, allow_writes=True)

    assert protecting.state == ExecutionState.PROTECTING
    assert protecting.filled_quantity == Decimal("2")
    assert protecting.average_fill_price == Decimal("100")
    assert len(
        [call for call in intraday.calls if call[0] == "submit_order"]
    ) == 1


def test_filled_without_any_confirmed_quantity_never_builds_stop():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {
            "state": "filled",
            "filled_quantity": "0",
            "average_fill_price": "",
        }
    )

    unresolved = adapter.reconcile(record, allow_writes=True)
    unresolved_again = adapter.reconcile(unresolved, allow_writes=False)

    assert unresolved.state == ExecutionState.ENTRY_PENDING
    assert unresolved.filled_quantity == Decimal("0")
    assert unresolved.broker_state["write_unknown"] == "entry_fill_quantity"
    assert unresolved_again.needs_attention is True
    assert len(
        [call for call in intraday.calls if call[0] == "submit_order"]
    ) == 1


def test_entry_timeout_requests_cancel_instead_of_leaving_order_open():
    intraday = FakeLongbridgeSession()
    sessions = {"intraday": intraday, "comprehensive": FakeLongbridgeSession()}
    adapter = LongbridgeAdapter(
        lambda profile: sessions[profile],
        entry_timeout_seconds=10,
    )
    plan = _plan()
    preflight = adapter.preflight(plan)
    prepared = adapter.prepare_submit(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.READY,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    prepared.broker_state["entry_submitted_at"] = "2000-01-01T00:00:00+00:00"
    submitted = adapter.submit_entry(prepared)

    intent = adapter.reconcile(submitted, allow_writes=True)
    updated = adapter.reconcile(intent, allow_writes=True)

    assert intent.broker_state["entry_cancel_intent"] is True
    assert updated.broker_state["entry_cancel_requested"] is True
    assert any(call[0] == "cancel_order" for call in intraday.calls)


def test_take_profit_cancels_stop_exits_tranche_and_rebuilds_stop():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {"state": "filled", "filled_quantity": "2", "average_fill_price": "100"}
    )
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    stop_id = record.broker_state["stop_order"]["order_id"]
    intraday.price = Decimal("111")

    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    intraday.orders[stop_id]["state"] = "canceled"
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    exit_id = record.broker_state["partial_exit"]["order_id"]
    intraday.orders[exit_id].update(
        {"state": "filled", "filled_quantity": "1", "average_fill_price": "111"}
    )
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)

    assert record.state == ExecutionState.OPEN
    assert record.remaining_quantity == Decimal("1")
    assert record.realized_pnl == Decimal("11")
    assert record.broker_state["take_profit_completed"] == [1]
    assert record.broker_state["stop_order"]["quantity"] == "1"
    assert record.broker_state["stop_order"]["order_id"]


def test_stop_submit_unknown_never_blindly_retries_and_can_be_reconciled():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {"state": "filled", "filled_quantity": "2", "average_fill_price": "100"}
    )
    protecting = adapter.reconcile(record, allow_writes=True)
    intraday.submit_error = BrokerTransportError(
        "timeout",
        write_may_have_reached=True,
    )

    unknown = adapter.reconcile(protecting, allow_writes=True)
    still_unknown = adapter.reconcile(unknown, allow_writes=False)

    submit_calls = [call for call in intraday.calls if call[0] == "submit_order"]
    assert len(submit_calls) == 2
    assert unknown.broker_state["stop_order"]["state"] == "unknown"
    assert unknown.broker_state["write_unknown"] == "stop"
    assert still_unknown.needs_attention is True

    stop = unknown.broker_state["stop_order"]
    intraday.orders["recovered-stop"] = {
        "order_id": "recovered-stop",
        "state": "pending",
        "quantity": stop["quantity"],
        "filled_quantity": "0",
        "average_fill_price": "",
        "remark": stop["remark"],
    }
    recovered = adapter.reconcile(still_unknown, allow_writes=False)

    assert recovered.state == ExecutionState.OPEN
    assert recovered.broker_state["stop_order"]["order_id"] == "recovered-stop"
    assert "write_unknown" not in recovered.broker_state
    assert len([call for call in intraday.calls if call[0] == "submit_order"]) == 2


def test_stop_cancel_is_sent_once_while_broker_status_is_pending():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {"state": "filled", "filled_quantity": "2", "average_fill_price": "100"}
    )
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    stop_id = record.broker_state["stop_order"]["order_id"]
    requested = adapter.request_exit(record, reason="manual")

    cancel_sent = adapter.reconcile(requested, allow_writes=True)
    waiting_1 = adapter.reconcile(cancel_sent, allow_writes=True)
    waiting_2 = adapter.reconcile(waiting_1, allow_writes=True)
    intraday.orders[stop_id]["state"] = "canceled"
    progressed = adapter.reconcile(waiting_2, allow_writes=True)

    assert len(
        [call for call in intraday.calls if call[0] == "cancel_order"]
    ) == 1
    assert waiting_2.broker_state["partial_exit"]["cancel_status"] == "submitted"
    assert progressed.broker_state["partial_exit"]["phase"] == "submit_exit"


def test_unknown_stop_cancel_is_resolved_while_disarmed_without_retry():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {"state": "filled", "filled_quantity": "2", "average_fill_price": "100"}
    )
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    stop_id = record.broker_state["stop_order"]["order_id"]
    requested = adapter.request_exit(record, reason="manual")
    intraday.cancel_error = BrokerTransportError(
        "timeout",
        write_may_have_reached=True,
    )
    unknown = adapter.reconcile(requested, allow_writes=True)
    intraday.cancel_error = None
    intraday.orders[stop_id]["state"] = "canceled"

    recovered = adapter.reconcile(unknown, allow_writes=False)

    assert unknown.broker_state["write_unknown"] == "cancel_stop"
    assert recovered.broker_state["partial_exit"]["phase"] == "submit_exit"
    assert "write_unknown" not in recovered.broker_state
    assert len(
        [call for call in intraday.calls if call[0] == "cancel_order"]
    ) == 1


def test_protecting_without_intent_persists_stop_metadata_before_write():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.PROTECTING,
        selected_account="intraday",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
    )

    initialised = adapter.reconcile(record, allow_writes=True)

    assert initialised.broker_state["stop_order"]["state"] == "planned"
    assert not [call for call in intraday.calls if call[0] == "submit_order"]


def test_active_exit_unknown_never_retries_and_recovers_by_remark():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {"state": "filled", "filled_quantity": "2", "average_fill_price": "100"}
    )
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    stop_id = record.broker_state["stop_order"]["order_id"]
    record = adapter.request_exit(record, reason="manual")
    record = adapter.reconcile(record, allow_writes=True)
    intraday.orders[stop_id]["state"] = "canceled"
    submit_phase = adapter.reconcile(record, allow_writes=True)
    intraday.submit_error = BrokerTransportError(
        "timeout",
        write_may_have_reached=True,
    )

    unknown = adapter.reconcile(submit_phase, allow_writes=True)
    still_unknown = adapter.reconcile(unknown, allow_writes=True)

    action = unknown.broker_state["partial_exit"]
    assert action["phase"] == "exit_unknown"
    assert unknown.broker_state["write_unknown"] == "exit"
    assert len([call for call in intraday.calls if call[0] == "submit_order"]) == 3
    assert still_unknown.needs_attention is True

    intraday.orders["recovered-exit"] = {
        "order_id": "recovered-exit",
        "state": "pending",
        "quantity": action["quantity"],
        "filled_quantity": "0",
        "average_fill_price": "",
        "remark": action["remark"],
    }
    recovered = adapter.reconcile(still_unknown, allow_writes=True)
    assert recovered.broker_state["partial_exit"]["phase"] == "wait_exit"
    assert "write_unknown" not in recovered.broker_state

    intraday.orders["recovered-exit"].update(
        {"state": "filled", "filled_quantity": "2", "average_fill_price": "110"}
    )
    closed = adapter.reconcile(recovered, allow_writes=True)
    assert closed.state == ExecutionState.CLOSED
    assert closed.realized_pnl == Decimal("20")
    assert len([call for call in intraday.calls if call[0] == "submit_order"]) == 3


def test_partially_filled_canceled_exit_rebuilds_stop_for_exact_remainder():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {"state": "filled", "filled_quantity": "2", "average_fill_price": "100"}
    )
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    stop_id = record.broker_state["stop_order"]["order_id"]
    record = adapter.request_exit(record, reason="manual")
    record = adapter.reconcile(record, allow_writes=True)
    intraday.orders[stop_id]["state"] = "canceled"
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    exit_id = record.broker_state["partial_exit"]["order_id"]
    intraday.orders[exit_id].update(
        {"state": "canceled", "filled_quantity": "1", "average_fill_price": "110"}
    )

    recovering = adapter.reconcile(record, allow_writes=True)

    assert recovering.state == ExecutionState.PROTECTING
    assert recovering.remaining_quantity == Decimal("1")
    assert recovering.realized_pnl == Decimal("10")
    assert recovering.broker_state["stop_order"]["quantity"] == "1"
    assert recovering.broker_state["stop_order"]["state"] == "planned"


def test_partially_filled_stop_blocks_take_profit_then_rebuilds_for_remainder():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {"state": "filled", "filled_quantity": "2", "average_fill_price": "100"}
    )
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    stop_id = record.broker_state["stop_order"]["order_id"]
    intraday.price = Decimal("120")
    intraday.orders[stop_id].update(
        {
            "state": "partially_filled",
            "filled_quantity": "1",
            "average_fill_price": "95",
        }
    )

    waiting = adapter.reconcile(record, allow_writes=True)
    intraday.orders[stop_id]["state"] = "canceled"
    recovering = adapter.reconcile(waiting, allow_writes=True)

    assert waiting.state == ExecutionState.OPEN
    assert waiting.needs_attention is True
    assert not waiting.broker_state.get("partial_exit")
    assert recovering.state == ExecutionState.PROTECTING
    assert recovering.remaining_quantity == Decimal("1")
    assert recovering.realized_pnl == Decimal("-5")


def test_stop_cancel_race_reduces_followup_exit_to_actual_remainder():
    intraday = FakeLongbridgeSession()
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = adapter.submit_entry(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.SUBMITTING,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    intraday.orders[record.broker_order_id].update(
        {"state": "filled", "filled_quantity": "2", "average_fill_price": "100"}
    )
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    stop_id = record.broker_state["stop_order"]["order_id"]
    record = adapter.request_exit(record, reason="manual")
    intraday.orders[stop_id].update(
        {
            "state": "canceled",
            "filled_quantity": "1",
            "average_fill_price": "95",
        }
    )

    submit_phase = adapter.reconcile(record, allow_writes=True)
    pending = adapter.reconcile(submit_phase, allow_writes=True)

    body = [call[1] for call in intraday.calls if call[0] == "submit_order"][-1]
    assert submit_phase.remaining_quantity == Decimal("1")
    assert submit_phase.broker_state["partial_exit"]["quantity"] == "1"
    assert body["submitted_quantity"] == "1"
    assert pending.broker_state["partial_exit"]["phase"] == "wait_exit"


def test_account_snapshot_maps_funds_positions_and_profit():
    intraday = FakeLongbridgeSession()
    intraday.positions_rows = [
        {
            "symbol": "GLD.US",
            "quantity": "2",
            "available_quantity": "2",
            "cost_price": "100",
            "currency": "USD",
            "market": "US",
            "account_channel": "lb",
            "name": "GLD",
        }
    ]
    adapter, _ = _adapter(intraday, FakeLongbridgeSession())

    snapshot = adapter.account_snapshot(_plan())

    assert snapshot.equity == Decimal("12000")
    assert snapshot.buying_power == Decimal("15000")
    assert snapshot.total_pnl == Decimal("2000")
    assert snapshot.realized_pnl is None
    assert snapshot.positions[0].instrument == "GLD.US"
