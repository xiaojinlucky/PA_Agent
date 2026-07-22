from __future__ import annotations

from decimal import Decimal

import pytest

from pa_agent.execution.errors import BrokerTransportError, PreflightError
from pa_agent.execution.models import (
    ExecutionPlan,
    ExecutionRecord,
    ExecutionState,
    utc_now_iso,
)
from pa_agent.execution.okx_adapter import OkxAdapter


class FakeOkxClient:
    simulated = False

    def __init__(self):
        self.calls = []
        self.order = {}
        self.algo_orders = {}
        self.order_counter = 0
        self.positions_rows = []
        self.balance_rows = []
        self.place_order_error = None
        self.place_algo_error = None
        self.cancel_algo_error = None
        self.orders = {}
        self.fills_by_order = {}
        self.fills_error = None
        self.algo_absence_confirmed = False
        self.account_config_row = {
            "posMode": "net_mode",
            "uid": "1001",
            "mainUid": "1001",
            "type": "0",
        }

    def sync_server_time(self):
        self.calls.append(("sync_server_time",))
        return 0

    def instruments(self, inst_type):
        self.calls.append(("instruments", inst_type))
        if inst_type == "SPOT":
            return [
                {
                    "instId": "BTC-USDT",
                    "instType": "SPOT",
                    "state": "live",
                    "tickSz": "0.1",
                    "lotSz": "0.001",
                    "minSz": "0.001",
                    "baseCcy": "BTC",
                    "quoteCcy": "USDT",
                },
                {
                    "instId": "XAUT-USDT",
                    "instType": "SPOT",
                    "state": "live",
                    "tickSz": "0.1",
                    "lotSz": "0.001",
                    "minSz": "0.001",
                    "baseCcy": "XAUT",
                    "quoteCcy": "USDT",
                },
            ]
        return [
            {
                "instId": "XAU-USDT-SWAP",
                "instType": "SWAP",
                "state": "live",
                "tickSz": "0.1",
                "lotSz": "1",
                "minSz": "1",
                "settleCcy": "USDT",
                "ctVal": "0.01",
                "ctType": "linear",
            }
        ]

    def account_config(self):
        self.calls.append(("account_config",))
        return dict(self.account_config_row)

    def max_order_size(self, **kwargs):
        self.calls.append(("max_order_size", kwargs))
        return {"maxBuy": "10", "maxSell": "10"}

    def leverage_info(self, **kwargs):
        self.calls.append(("leverage_info", kwargs))
        return [
            {
                "instId": kwargs["instrument"],
                "mgnMode": kwargs["margin_mode"],
                "lever": "5",
            }
        ]

    def positions(self, *, instrument=None):
        self.calls.append(("positions", instrument))
        if instrument:
            return [
                row for row in self.positions_rows if row.get("instId") == instrument
            ]
        return list(self.positions_rows)

    def balance(self):
        return list(self.balance_rows)

    def ticker(self, instrument):
        return {"instId": instrument, "last": "105"}

    def place_order(self, body):
        self.calls.append(("place_order", body))
        if self.place_order_error:
            raise self.place_order_error
        self.order_counter += 1
        order_id = f"order-{self.order_counter}"
        self.orders[order_id] = {
            "ordId": order_id,
            "clOrdId": body.get("clOrdId", ""),
            "state": "live",
            "accFillSz": "0",
            "avgPx": "",
        }
        return {"ordId": order_id, "sCode": "0"}

    def get_order(self, **kwargs):
        self.calls.append(("get_order", kwargs))
        order_id = kwargs.get("order_id") or ""
        client_id = kwargs.get("client_order_id") or ""
        if order_id and order_id in self.orders:
            return dict(self.orders[order_id])
        if client_id:
            found = next(
                (
                    row
                    for row in self.orders.values()
                    if row.get("clOrdId") == client_id
                ),
                None,
            )
            if found:
                return dict(found)
        return dict(self.order)

    def cancel_order(self, **kwargs):
        self.calls.append(("cancel_order", kwargs))
        return {"ordId": kwargs.get("order_id"), "sCode": "0"}

    def place_algo_order(self, body):
        self.calls.append(("place_algo_order", body))
        if self.place_algo_error:
            raise self.place_algo_error
        self.order_counter += 1
        algo_id = f"algo-{self.order_counter}"
        self.algo_orders[algo_id] = {
            "algoId": algo_id,
            "algoClOrdId": body.get("algoClOrdId", ""),
            "state": "live",
        }
        return {"algoId": algo_id, "sCode": "0"}

    def get_algo_order(self, *, algo_id="", client_algo_id=""):
        self.calls.append(("get_algo_order", algo_id, client_algo_id))
        if algo_id and algo_id in self.algo_orders:
            return dict(self.algo_orders[algo_id])
        if client_algo_id:
            found = next(
                (
                    row
                    for row in self.algo_orders.values()
                    if row.get("algoClOrdId") == client_algo_id
                ),
                self.algo_orders.get(client_algo_id),
            )
            if found:
                return dict(found)
        return {"state": "live"}

    def find_algo_order_by_client_id(
        self,
        *,
        client_algo_id,
        order_type,
        instrument,
    ):
        self.calls.append(
            (
                "find_algo_order_by_client_id",
                client_algo_id,
                order_type,
                instrument,
            )
        )
        found = next(
            (
                row
                for row in self.algo_orders.values()
                if row.get("algoClOrdId") == client_algo_id
            ),
            None,
        )
        if found:
            return dict(found)
        if self.algo_absence_confirmed:
            return None
        raise BrokerTransportError(
            "算法单完整查询尚未成功",
            write_may_have_reached=False,
        )

    def cancel_algo_orders(self, orders):
        self.calls.append(("cancel_algo_orders", orders))
        if self.cancel_algo_error:
            raise self.cancel_algo_error
        return [{"algoId": item["algoId"], "sCode": "0"} for item in orders]

    def fills(self, **kwargs):
        self.calls.append(("fills", kwargs))
        if self.fills_error:
            raise self.fills_error
        return list(self.fills_by_order.get(kwargs.get("order_id"), []))


def _plan(*, product="swap", direction="long", instrument=None) -> ExecutionPlan:
    return ExecutionPlan(
        id="7d50ebf2-84bf-4d43-b57f-31d42da969e0",
        analysis_digest="digest",
        analysis_record_path="record.json",
        broker="okx",
        environment="demo",
        product=product,
        requested_account="okx",
        source_symbol="XAUUSD",
        instrument=instrument or ("XAU-USDT-SWAP" if product == "swap" else "XAUT-USDT"),
        direction=direction,
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


def test_preflight_uses_live_instrument_specs_and_account_maximum():
    client = FakeOkxClient()
    adapter = OkxAdapter(client, margin_mode="cross")

    result = adapter.preflight(_plan())

    assert result.quantity_step == Decimal("1")
    assert result.price_tick == Decimal("0.1")
    assert result.broker_metadata["position_mode"] == "net_mode"
    assert result.broker_metadata["current_leverage"] == "5"
    assert ("instruments", "SWAP") in client.calls


def test_preflight_rejects_existing_swap_position_instead_of_merging_it():
    client = FakeOkxClient()
    client.positions_rows = [
        {
            "instId": "XAU-USDT-SWAP",
            "pos": "10",
            "posSide": "net",
        }
    ]
    adapter = OkxAdapter(client, margin_mode="cross")

    with pytest.raises(PreflightError, match="已有持仓"):
        adapter.preflight(_plan())

    assert not [call for call in client.calls if call[0] == "place_order"]


def test_rejected_cancel_terminal_marker_stays_read_only_after_restart():
    client = FakeOkxClient()
    first = OkxAdapter(
        client,
        margin_mode="cross",
        runtime_id="runtime-before-restart",
    )
    plan = _plan()
    preflight = first.preflight(plan)
    prepared = first.prepare_submit(
        ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.READY,
            preflight=preflight,
            remaining_quantity=plan.quantity,
        )
    )
    submitted = first.submit_entry(prepared)
    broker_state = dict(submitted.broker_state)
    broker_state["entry_cancel_status"] = "rejected"
    broker_state["risk_reducing_writes_blocked"] = "cancel_entry_rejected"
    rejected = submitted.model_copy(
        update={
            "broker_state": broker_state,
            "needs_attention": True,
        }
    )
    restarted = OkxAdapter(
        client,
        margin_mode="cross",
        runtime_id="runtime-after-restart",
    )

    reconciled = restarted.reconcile(rejected, allow_writes=False)

    assert reconciled.state is ExecutionState.ENTRY_PENDING
    assert reconciled.needs_attention is False
    assert "write_unknown" not in reconciled.broker_state
    assert not [call for call in client.calls if call[0] == "cancel_order"]


def test_preflight_aligns_only_binary_float_price_artifacts():
    client = FakeOkxClient()
    adapter = OkxAdapter(client, margin_mode="cross")
    plan = _plan().model_copy(
        update={"stop_loss": Decimal("95.00000000000001")}
    )

    result = adapter.preflight(plan)

    assert result.stop_loss == Decimal("95.0")


def test_preflight_still_rejects_material_off_tick_price():
    client = FakeOkxClient()
    adapter = OkxAdapter(client, margin_mode="cross")
    plan = _plan().model_copy(update={"stop_loss": Decimal("95.01")})

    with pytest.raises(PreflightError, match="不是价格跳动"):
        adapter.preflight(plan)


def test_okx_preflight_identity_changes_with_actual_account_config():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()

    first = adapter.preflight(plan)
    client.account_config_row["uid"] = "2002"
    client.account_config_row["mainUid"] = "2002"
    client.account_config_row["type"] = 0
    second = adapter.preflight(plan)

    assert first.account_identity
    assert second.account_identity
    assert first.account_identity != second.account_identity
    assert adapter.account_identity(plan) == second.account_identity


def test_okx_account_identity_syncs_server_time_before_private_config():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)

    identity = adapter.account_identity(_plan())

    assert identity
    assert client.calls[:2] == [
        ("sync_server_time",),
        ("account_config",),
    ]


def test_regular_swap_entry_has_deterministic_client_id_and_position_side():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
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

    body = next(call[1] for call in client.calls if call[0] == "place_order")
    assert body["instId"] == "XAU-USDT-SWAP"
    assert body["posSide"] == "net"
    assert body["clOrdId"] == submitted.client_order_id
    assert len(body["clOrdId"]) <= 32
    assert submitted.state == ExecutionState.ENTRY_PENDING


def test_unknown_entry_recovers_by_persisted_client_id_without_resubmit():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
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
    client.orders["recovered-entry"] = {
        "ordId": "recovered-entry",
        "clOrdId": prepared.client_order_id,
        "state": "live",
        "accFillSz": "0",
        "avgPx": "",
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
    assert not [call for call in client.calls if call[0] == "place_order"]


def test_partial_fill_immediately_requests_cancel_of_remainder():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
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
    client.orders[record.broker_order_id] = {
        "ordId": record.broker_order_id,
        "state": "partially_filled",
        "accFillSz": "1",
        "avgPx": "100",
    }

    intent = adapter.reconcile(record, allow_writes=True)
    assert intent.broker_state["entry_cancel_intent"] is True
    assert not any(call[0] == "cancel_order" for call in client.calls)

    updated = adapter.reconcile(intent, allow_writes=True)

    assert updated.state == ExecutionState.PARTIALLY_FILLED
    assert updated.filled_quantity == Decimal("1")
    assert updated.broker_state["entry_cancel_requested"] is True
    assert any(call[0] == "cancel_order" for call in client.calls)


def test_breakout_partial_fill_persists_child_then_cancels_child_order():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan().model_copy(update={"entry_type": "breakout"})
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
    submitted = adapter.submit_entry(prepared)
    client.algo_orders[submitted.broker_order_id].update(
        {"state": "effective", "ordIdList": ["child-1"]}
    )
    client.orders["child-1"] = {
        "ordId": "child-1",
        "state": "partially_filled",
        "accFillSz": "1",
        "avgPx": "100",
    }

    child_persisted = adapter.reconcile(submitted, allow_writes=True)
    cancel_intent = adapter.reconcile(child_persisted, allow_writes=True)
    canceled = adapter.reconcile(cancel_intent, allow_writes=True)

    assert child_persisted.broker_state["entry_child_order_id"] == "child-1"
    assert cancel_intent.broker_state["entry_cancel_intent"] is True
    assert canceled.broker_state["entry_cancel_target"] == "child_order"
    assert [
        call[1]["order_id"]
        for call in client.calls
        if call[0] == "cancel_order"
    ] == ["child-1"]
    assert not [
        call for call in client.calls if call[0] == "cancel_algo_orders"
    ]


def test_entry_timeout_requests_cancel_instead_of_leaving_order_open():
    client = FakeOkxClient()
    adapter = OkxAdapter(client, entry_timeout_seconds=10)
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
    assert any(call[0] == "cancel_order" for call in client.calls)


def test_filled_entry_builds_two_native_oco_orders_one_at_a_time():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
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
    client.orders[record.broker_order_id] = {
        "ordId": record.broker_order_id,
        "state": "filled",
        "accFillSz": "2",
        "avgPx": "101",
    }

    protecting = adapter.reconcile(record, allow_writes=True)
    first = adapter.reconcile(protecting, allow_writes=True)
    opened = adapter.reconcile(first, allow_writes=True)

    protection_calls = [call for call in client.calls if call[0] == "place_algo_order"]
    assert protecting.state == ExecutionState.PROTECTING
    assert first.state == ExecutionState.PROTECTING
    assert opened.state == ExecutionState.OPEN
    assert len(protection_calls) == 2
    assert {call[1]["tpTriggerPx"] for call in protection_calls} == {"110", "120"}
    assert all(call[1]["slTriggerPx"] == "95" for call in protection_calls)
    assert all(call[1]["reduceOnly"] is True for call in protection_calls)


def test_filled_without_reported_quantity_uses_fills_not_plan_quantity():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
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
    client.orders[record.broker_order_id].update(
        {"state": "filled", "accFillSz": "", "avgPx": ""}
    )
    client.fills_by_order[record.broker_order_id] = [
        {"fillSz": "1", "fillPx": "99"},
        {"fillSz": "1", "fillPx": "101"},
    ]

    protecting = adapter.reconcile(record, allow_writes=True)

    assert protecting.state == ExecutionState.PROTECTING
    assert protecting.filled_quantity == Decimal("2")
    assert protecting.average_fill_price == Decimal("100")
    assert not [
        call for call in client.calls if call[0] == "place_algo_order"
    ]


def test_filled_without_any_confirmed_quantity_stays_read_only():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
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
    client.orders[record.broker_order_id].update(
        {"state": "filled", "accFillSz": "", "avgPx": ""}
    )

    unresolved = adapter.reconcile(record, allow_writes=True)

    assert unresolved.state == ExecutionState.ENTRY_PENDING
    assert unresolved.filled_quantity == Decimal("0")
    assert unresolved.needs_attention is True
    assert unresolved.broker_state["write_unknown"] == "entry_fill_quantity"
    assert not [
        call for call in client.calls if call[0] == "place_algo_order"
    ]


def test_canceled_entry_with_missing_fill_quantity_stays_active_until_confirmed():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
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
    client.orders[record.broker_order_id].update(
        {"state": "canceled", "accFillSz": "", "avgPx": ""}
    )
    client.fills_error = BrokerTransportError(
        "temporary read failure",
        write_may_have_reached=False,
    )

    unresolved = adapter.reconcile(record, allow_writes=True)
    client.fills_error = None
    still_unresolved = adapter.reconcile(unresolved, allow_writes=False)
    client.orders[record.broker_order_id]["accFillSz"] = "0"
    canceled = adapter.reconcile(still_unresolved, allow_writes=False)

    assert unresolved.state == ExecutionState.ENTRY_PENDING
    assert unresolved.broker_state["write_unknown"] == "entry_fill_quantity"
    assert still_unresolved.state == ExecutionState.ENTRY_PENDING
    assert still_unresolved.broker_state["write_unknown"] == "entry_fill_quantity"
    assert canceled.state == ExecutionState.CANCELED
    assert "write_unknown" not in canceled.broker_state


def test_swap_known_fill_quantity_missing_average_still_builds_protection():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
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
    client.orders[record.broker_order_id].update(
        {"state": "filled", "accFillSz": "2", "avgPx": ""}
    )

    protecting = adapter.reconcile(record, allow_writes=True)

    assert protecting.state == ExecutionState.PROTECTING
    assert protecting.filled_quantity == Decimal("2")
    assert protecting.remaining_quantity == Decimal("2")
    assert protecting.average_fill_price is None
    assert not [call for call in client.calls if call[0] == "place_algo_order"]


@pytest.mark.parametrize("product", ["spot", "swap"])
def test_entry_fills_with_partial_missing_prices_keep_average_unknown(product):
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan(product=product)
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
    client.orders[record.broker_order_id].update(
        {"state": "filled", "accFillSz": "2", "avgPx": ""}
    )
    client.fills_by_order[record.broker_order_id] = [
        {
            "tradeId": "entry-1",
            "fillSz": "1",
            "fillPx": "100",
            "fee": "0",
            "feeCcy": "",
        },
        {
            "tradeId": "entry-2",
            "fillSz": "1",
            "fillPx": "",
            "fee": "0",
            "feeCcy": "",
        },
    ]

    protecting = adapter.reconcile(record, allow_writes=True)

    assert protecting.state == ExecutionState.PROTECTING
    assert protecting.filled_quantity == Decimal("2")
    assert protecting.average_fill_price is None


def test_spot_base_fee_is_deducted_and_quantity_is_rounded_down():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan(product="spot")
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
    client.orders[record.broker_order_id].update(
        {"state": "filled", "accFillSz": "2", "avgPx": "100"}
    )
    client.fills_by_order[record.broker_order_id] = [
        {
            "fillSz": "2",
            "fillPx": "100",
            "fee": "-0.0008",
            "feeCcy": "XAUT",
        }
    ]

    protecting = adapter.reconcile(record, allow_writes=True)
    first = adapter.reconcile(protecting, allow_writes=True)
    opened = adapter.reconcile(first, allow_writes=True)
    bodies = [
        call[1] for call in client.calls if call[0] == "place_algo_order"
    ]

    assert protecting.remaining_quantity == Decimal("1.999")
    assert protecting.broker_state["spot_net_filled_quantity"] == "1.9992"
    assert protecting.broker_state["spot_dust_quantity"] == "0.0002"
    assert sum(Decimal(body["sz"]) for body in bodies) == Decimal("1.999")
    assert opened.state == ExecutionState.OPEN


def test_spot_quote_currency_fee_does_not_reduce_base_quantity():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan(product="spot")
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
    client.orders[record.broker_order_id].update(
        {"state": "filled", "accFillSz": "2", "avgPx": "100"}
    )
    client.fills_by_order[record.broker_order_id] = [
        {
            "fillSz": "2",
            "fillPx": "100",
            "fee": "-1",
            "feeCcy": "USDT",
        }
    ]

    protecting = adapter.reconcile(record, allow_writes=True)

    assert protecting.remaining_quantity == Decimal("2")
    assert protecting.broker_state["spot_base_fee"] == "0"


def test_spot_net_quantity_below_minimum_blocks_protection():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan(product="spot").model_copy(
        update={"quantity": Decimal("0.001")}
    )
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
    client.orders[record.broker_order_id].update(
        {"state": "filled", "accFillSz": "0.001", "avgPx": "100"}
    )
    client.fills_by_order[record.broker_order_id] = [
        {
            "fillSz": "0.001",
            "fillPx": "100",
            "fee": "-0.0005",
            "feeCcy": "XAUT",
        }
    ]

    blocked = adapter.reconcile(record, allow_writes=True)

    assert blocked.state == ExecutionState.ENTRY_PENDING
    assert blocked.needs_attention is True
    assert "低于最小交易量" in blocked.last_error
    assert not [
        call for call in client.calls if call[0] == "place_algo_order"
    ]


def test_account_snapshot_maps_funds_position_and_pnl():
    client = FakeOkxClient()
    client.balance_rows = [
        {
            "totalEq": "10000",
            "upl": "12",
            "details": [
                {
                    "ccy": "USDT",
                    "eq": "10000",
                    "cashBal": "9000",
                    "availBal": "8000",
                    "availEq": "8500",
                    "upl": "12",
                }
            ],
        }
    ]
    client.positions_rows = [
        {
            "instId": "XAU-USDT-SWAP",
            "pos": "2",
            "posSide": "net",
            "avgPx": "100",
            "markPx": "106",
            "upl": "12",
            "realizedPnl": "3",
            "ccy": "USDT",
            "lever": "5",
            "mgnMode": "cross",
        }
    ]
    adapter = OkxAdapter(client)

    snapshot = adapter.account_snapshot(_plan())

    assert snapshot.equity == Decimal("10000")
    assert snapshot.available == Decimal("8000")
    assert snapshot.unrealized_pnl == Decimal("12")
    assert snapshot.realized_pnl is None
    assert snapshot.total_pnl is None
    assert snapshot.positions[0].direction == "long"


def test_okx_account_snapshot_uses_settlement_currency_independent_of_row_order():
    client = FakeOkxClient()
    client.balance_rows = [
        {
            "details": [
                {
                    "ccy": "BTC",
                    "eq": "99",
                    "cashBal": "88",
                    "availBal": "77",
                    "availEq": "66",
                },
                {
                    "ccy": "USDT",
                    "eq": "10000",
                    "cashBal": "9000",
                    "availBal": "8000",
                    "availEq": "8500",
                },
            ]
        }
    ]
    adapter = OkxAdapter(client)

    snapshot = adapter.account_snapshot(_plan())

    assert snapshot.base_currency == "USDT"
    assert snapshot.equity == Decimal("10000")
    assert snapshot.cash == Decimal("9000")
    assert snapshot.available == Decimal("8000")
    assert snapshot.buying_power == Decimal("8500")


def test_okx_account_snapshot_does_not_fallback_to_unrelated_currency():
    client = FakeOkxClient()
    client.balance_rows = [
        {
            "details": [
                {
                    "ccy": "BTC",
                    "eq": "99",
                    "cashBal": "88",
                    "availBal": "77",
                    "availEq": "66",
                }
            ]
        }
    ]
    adapter = OkxAdapter(client)

    snapshot = adapter.account_snapshot(_plan())

    assert snapshot.base_currency == "USDT"
    assert snapshot.equity is None
    assert snapshot.cash is None
    assert snapshot.available is None
    assert snapshot.buying_power is None


def test_spot_account_snapshot_maps_nonzero_balances_as_holdings():
    client = FakeOkxClient()
    client.balance_rows = [
        {
            "totalEq": "1200",
            "details": [
                {
                    "ccy": "XAUT",
                    "cashBal": "1.5",
                    "availBal": "0.5",
                    "eqUsd": "1000",
                },
                {
                    "ccy": "USDT",
                    "cashBal": "200",
                    "availBal": "200",
                    "eq": "200",
                },
                {"ccy": "BTC", "cashBal": "0", "availBal": "0"},
            ],
        }
    ]
    adapter = OkxAdapter(client)

    snapshot = adapter.account_snapshot(_plan(product="spot"))

    assert {position.instrument for position in snapshot.positions} == {
        "XAUT",
        "USDT",
    }
    xaut = next(
        position
        for position in snapshot.positions
        if position.instrument == "XAUT"
    )
    assert xaut.quantity == Decimal("1.5")
    assert xaut.available_quantity == Decimal("0.5")
    assert xaut.raw["kind"] == "spot_balance"
    assert snapshot.total_pnl is None
    assert snapshot.realized_pnl is None


def test_generic_spot_instrument_uses_cash_and_base_currency_quantity():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan(product="spot", instrument="BTC-USDT")
    preflight = adapter.preflight(plan)
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.SUBMITTING,
        preflight=preflight,
        remaining_quantity=plan.quantity,
    )

    submitted = adapter.submit_entry(record)

    body = next(call[1] for call in client.calls if call[0] == "place_order")
    assert submitted.state == ExecutionState.ENTRY_PENDING
    assert body["tdMode"] == "cash"
    assert body["tgtCcy"] == "base_ccy"
    assert "posSide" not in body
    assert not [call for call in client.calls if call[0] == "leverage_info"]


def test_protection_submit_unknown_is_never_blindly_retried():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
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
    client.orders[record.broker_order_id] = {
        "ordId": record.broker_order_id,
        "state": "filled",
        "accFillSz": "2",
        "avgPx": "100",
    }
    protecting = adapter.reconcile(record, allow_writes=True)
    client.place_algo_error = BrokerTransportError(
        "timeout",
        write_may_have_reached=True,
    )

    unknown = adapter.reconcile(protecting, allow_writes=True)
    still_unknown = adapter.reconcile(unknown, allow_writes=False)

    assert unknown.broker_state["write_unknown"] == "protection"
    assert unknown.broker_state["protection_targets"][0]["state"] == "unknown"
    assert len([call for call in client.calls if call[0] == "place_algo_order"]) == 1
    assert still_unknown.needs_attention is True

    target = unknown.broker_state["protection_targets"][0]
    client.algo_orders["recovered"] = {
        "algoId": "recovered",
        "algoClOrdId": target["client_algo_id"],
        "state": "live",
    }
    recovered = adapter.reconcile(still_unknown, allow_writes=False)

    assert recovered.broker_state["protection_targets"][0]["algo_id"] == "recovered"
    assert "write_unknown" not in recovered.broker_state
    assert len([call for call in client.calls if call[0] == "place_algo_order"]) == 1


def test_restart_after_protection_intent_never_resubmits_okx_protection():
    client = FakeOkxClient()
    first_adapter = OkxAdapter(client, runtime_id="runtime-one")
    plan = _plan()
    preflight = first_adapter.preflight(plan)
    interrupted = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.PROTECTING,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "protection_targets": [
                {
                    "index": 1,
                    "quantity": "2",
                    "take_profit": "110",
                    "client_algo_id": "client-protection",
                    "algo_id": "",
                    "state": "submitting",
                    "submit_runtime_id": "runtime-one",
                }
            ]
        },
    )
    restarted = OkxAdapter(client, runtime_id="runtime-two")

    unknown = restarted.reconcile(interrupted, allow_writes=True)

    assert unknown.broker_state["protection_targets"][0]["state"] == "unknown"
    assert unknown.broker_state["write_unknown"] == "protection"
    assert not [call for call in client.calls if call[0] == "place_algo_order"]


def test_confirmed_absent_protection_requires_explicit_exit_and_is_not_retried():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.PROTECTING,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "protection_base_quantity": "2",
            "write_unknown": "protection",
            "risk_reducing_writes_blocked": "broker_write_unknown",
            "protection_targets": [
                {
                    "index": 1,
                    "quantity": "2",
                    "take_profit": "110",
                    "client_algo_id": "client-protection",
                    "algo_id": "",
                    "state": "unknown",
                }
            ],
        },
        needs_attention=True,
    )
    client.algo_absence_confirmed = True

    resolved = adapter.reconcile(record, allow_writes=False)
    still_waiting = adapter.reconcile(resolved, allow_writes=True)

    assert resolved.broker_state["protection_targets"][0]["state"] == (
        "confirmed_absent"
    )
    assert "write_unknown" not in resolved.broker_state
    assert "risk_reducing_writes_blocked" not in resolved.broker_state
    assert resolved.needs_attention is True
    assert still_waiting.needs_attention is True
    assert not [call for call in client.calls if call[0] == "place_algo_order"]

    reblocked_state = dict(still_waiting.broker_state)
    reblocked_state["risk_reducing_writes_blocked"] = (
        "identity_or_route_blocked"
    )
    reblocked = still_waiting.model_copy(
        update={"broker_state": reblocked_state}
    )
    cleared = adapter.reconcile(reblocked, allow_writes=False)

    assert "risk_reducing_writes_blocked" not in cleared.broker_state

    requested = adapter.request_exit(
        cleared,
        reason="campaign_expired",
    )

    assert requested.state == ExecutionState.EXIT_PENDING
    assert requested.needs_attention is False
    assert requested.last_error == ""


def test_restart_after_exit_intent_never_resubmits_okx_exit():
    client = FakeOkxClient()
    adapter = OkxAdapter(client, runtime_id="runtime-two")
    plan = _plan()
    preflight = adapter.preflight(plan)
    interrupted = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.EXIT_PENDING,
        selected_account="okx",
        preflight=preflight,
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "exit_phase": "submit_exit_ready",
            "exit_order": {
                "order_id": "",
                "client_order_id": "client-exit",
                "quantity": "2",
                "submit_runtime_id": "runtime-one",
            },
        },
    )

    unknown = adapter.reconcile(interrupted, allow_writes=True)

    assert unknown.broker_state["exit_phase"] == "exit_unknown"
    assert unknown.broker_state["write_unknown"] == "exit"
    assert not [call for call in client.calls if call[0] == "place_order"]


def test_terminal_protection_fill_without_confirmed_quantity_stays_open():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    client.algo_orders["protection"] = {
        "algoId": "protection",
        "state": "effective",
        "ordIdList": ["exit-missing-fill"],
    }
    client.orders["exit-missing-fill"] = {
        "ordId": "exit-missing-fill",
        "state": "filled",
        "accFillSz": "",
        "avgPx": "",
    }
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.OPEN,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "protection_base_quantity": "2",
            "protection_targets": [
                {
                    "index": 1,
                    "client_algo_id": "client-protection",
                    "algo_id": "protection",
                    "state": "live",
                    "quantity": "2",
                    "take_profit": "110",
                }
            ],
        },
    )

    unresolved = adapter.reconcile(record, allow_writes=True)

    assert unresolved.state == ExecutionState.OPEN
    assert unresolved.remaining_quantity == Decimal("2")
    assert unresolved.broker_state["write_unknown"] == "exit_fill_quantity"
    assert unresolved.needs_attention is True


def test_spot_protection_fill_with_partial_missing_prices_never_invents_pnl():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan(product="spot")
    preflight = adapter.preflight(plan)
    client.algo_orders["protection"] = {
        "algoId": "protection",
        "state": "effective",
        "ordIdList": ["exit-mixed-price"],
    }
    client.orders["exit-mixed-price"] = {
        "ordId": "exit-mixed-price",
        "state": "filled",
        "accFillSz": "2",
        "avgPx": "",
    }
    client.fills_by_order["exit-mixed-price"] = [
        {"tradeId": "protect-1", "fillSz": "1", "fillPx": "110"},
        {"tradeId": "protect-2", "fillSz": "1", "fillPx": ""},
    ]
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.OPEN,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "protection_base_quantity": "2",
            "realized_before_protection": "0",
            "protection_targets": [
                {
                    "index": 1,
                    "client_algo_id": "client-protection",
                    "algo_id": "protection",
                    "state": "live",
                    "quantity": "2",
                    "take_profit": "110",
                }
            ],
        },
    )

    closed = adapter.reconcile(record, allow_writes=True)

    assert closed.state == ExecutionState.CLOSED
    assert closed.realized_pnl is None
    assert closed.needs_attention is True
    assert closed.broker_state["protection_targets"][0]["average_fill_price"] == ""


def test_terminal_active_exit_without_confirmed_quantity_never_closes_position():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    client.orders["exit-missing-fill"] = {
        "ordId": "exit-missing-fill",
        "state": "filled",
        "accFillSz": "",
        "avgPx": "",
    }
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.EXIT_PENDING,
        selected_account="okx",
        preflight=preflight,
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "exit_phase": "wait_exit",
            "exit_order": {
                "order_id": "exit-missing-fill",
                "client_order_id": "client-exit",
                "quantity": "2",
            },
        },
    )

    unresolved = adapter.reconcile(record, allow_writes=True)

    assert unresolved.state == ExecutionState.EXIT_PENDING
    assert unresolved.remaining_quantity == Decimal("2")
    assert unresolved.broker_state["write_unknown"] == "exit_fill_quantity"


@pytest.mark.parametrize("status", ["canceled", "rejected"])
def test_terminal_exit_missing_quantity_and_empty_fills_is_not_zero(status):
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    client.orders["exit-missing-fill"] = {
        "ordId": "exit-missing-fill",
        "state": status,
        "accFillSz": "",
        "avgPx": "",
    }
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.EXIT_PENDING,
        selected_account="okx",
        preflight=preflight,
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "exit_phase": "wait_exit",
            "exit_order": {
                "order_id": "exit-missing-fill",
                "client_order_id": "client-exit",
                "quantity": "2",
            },
        },
    )

    unresolved = adapter.reconcile(record, allow_writes=True)

    assert unresolved.state == ExecutionState.EXIT_PENDING
    assert unresolved.remaining_quantity == Decimal("2")
    assert unresolved.broker_state["write_unknown"] == "exit_fill_quantity"


def test_spot_terminal_exit_with_partial_missing_prices_never_invents_pnl():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan(product="spot")
    preflight = adapter.preflight(plan)
    client.orders["exit-mixed-price"] = {
        "ordId": "exit-mixed-price",
        "state": "filled",
        "accFillSz": "2",
        "avgPx": "",
    }
    client.fills_by_order["exit-mixed-price"] = [
        {"tradeId": "exit-1", "fillSz": "1", "fillPx": "110"},
        {"tradeId": "exit-2", "fillSz": "1", "fillPx": ""},
    ]
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.EXIT_PENDING,
        selected_account="okx",
        preflight=preflight,
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "exit_phase": "wait_exit",
            "exit_order": {
                "order_id": "exit-mixed-price",
                "client_order_id": "client-exit",
                "quantity": "2",
            },
        },
    )

    closed = adapter.reconcile(record, allow_writes=True)

    assert closed.state == ExecutionState.CLOSED
    assert closed.realized_pnl is None
    assert closed.needs_attention is True


def test_spot_terminal_exit_deduplicates_exact_trade_ids():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan(product="spot")
    preflight = adapter.preflight(plan)
    client.orders["exit-duplicate-trade"] = {
        "ordId": "exit-duplicate-trade",
        "state": "filled",
        "accFillSz": "1",
        "avgPx": "",
    }
    duplicate = {
        "tradeId": "same-trade",
        "fillSz": "1",
        "fillPx": "110",
    }
    client.fills_by_order["exit-duplicate-trade"] = [
        duplicate,
        dict(duplicate),
    ]
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.EXIT_PENDING,
        selected_account="okx",
        preflight=preflight,
        remaining_quantity=Decimal("1"),
        average_fill_price=Decimal("100"),
        broker_state={
            "exit_phase": "wait_exit",
            "exit_order": {
                "order_id": "exit-duplicate-trade",
                "client_order_id": "client-exit",
                "quantity": "1",
                "realized_before_exit": "0",
            },
        },
    )

    closed = adapter.reconcile(record, allow_writes=True)

    assert closed.state == ExecutionState.CLOSED
    assert closed.realized_pnl == Decimal("10")


def test_spot_terminal_exit_with_known_quantity_missing_price_never_fakes_pnl():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan(product="spot")
    preflight = adapter.preflight(plan)
    client.orders["exit-missing-price"] = {
        "ordId": "exit-missing-price",
        "state": "filled",
        "accFillSz": "2",
        "avgPx": "",
    }
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.EXIT_PENDING,
        selected_account="okx",
        preflight=preflight,
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "exit_phase": "wait_exit",
            "exit_order": {
                "order_id": "exit-missing-price",
                "client_order_id": "client-exit",
                "quantity": "2",
            },
        },
    )

    closed = adapter.reconcile(record, allow_writes=True)

    assert closed.state == ExecutionState.CLOSED
    assert closed.remaining_quantity == Decimal("0")
    assert closed.realized_pnl is None
    assert closed.needs_attention is True


def test_swap_realized_pnl_uses_okx_fill_pnl_not_contract_count_times_price():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    client.algo_orders["algo-1"] = {
        "algoId": "algo-1",
        "state": "effective",
        "ordIdList": ["exit-1"],
    }
    client.orders["exit-1"] = {
        "ordId": "exit-1",
        "state": "filled",
        "accFillSz": "1",
        "avgPx": "110",
    }
    client.fills_by_order["exit-1"] = [{"fillPnl": "0.10"}]
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.OPEN,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "protection_targets": [
                {
                    "client_algo_id": "protect-1",
                    "algo_id": "algo-1",
                    "state": "live",
                    "quantity": "2",
                    "take_profit": "110",
                }
            ]
        },
    )

    updated = adapter.reconcile(record, allow_writes=False)

    assert updated.remaining_quantity == Decimal("1")
    assert updated.realized_pnl == Decimal("0.10")
    assert any(call[0] == "fills" for call in client.calls)


def test_active_exit_persists_intents_then_closes_with_actual_swap_fill_pnl():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    client.algo_orders["protect-1"] = {
        "algoId": "protect-1",
        "algoClOrdId": "protect-client-1",
        "state": "live",
    }
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.OPEN,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        realized_pnl=Decimal("0.05"),
        broker_state={
            "protection_targets": [
                {
                    "client_algo_id": "protect-client-1",
                    "algo_id": "protect-1",
                    "state": "live",
                    "quantity": "2",
                    "take_profit": "110",
                }
            ]
        },
    )

    record = adapter.request_exit(record, reason="manual")
    cancel_intent = adapter.reconcile(record, allow_writes=True)
    cancel_sent = adapter.reconcile(cancel_intent, allow_writes=True)
    client.algo_orders["protect-1"]["state"] = "canceled"
    protection_canceled = adapter.reconcile(cancel_sent, allow_writes=True)
    exit_intent = adapter.reconcile(protection_canceled, allow_writes=True)

    assert exit_intent.broker_state["exit_phase"] == "submit_exit_ready"
    assert exit_intent.broker_state["exit_order"]["client_order_id"]
    assert not [call for call in client.calls if call[0] == "place_order"]

    exit_pending = adapter.reconcile(exit_intent, allow_writes=True)
    exit_order_id = exit_pending.broker_state["exit_order"]["order_id"]
    client.orders[exit_order_id].update(
        {"state": "filled", "accFillSz": "2", "avgPx": "110"}
    )
    client.fills_by_order[exit_order_id] = [{"fillPnl": "0.20"}]
    closed = adapter.reconcile(exit_pending, allow_writes=True)

    assert closed.state == ExecutionState.CLOSED
    assert closed.remaining_quantity == Decimal("0")
    assert closed.realized_pnl == Decimal("0.25")
    assert len([call for call in client.calls if call[0] == "place_order"]) == 1


def test_unknown_protection_cancel_is_resolved_while_disarmed_without_retry():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    client.algo_orders["protect-1"] = {
        "algoId": "protect-1",
        "algoClOrdId": "protect-client-1",
        "state": "live",
    }
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.OPEN,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={
            "protection_base_quantity": "2",
            "protection_targets": [
                {
                    "client_algo_id": "protect-client-1",
                    "algo_id": "protect-1",
                    "state": "live",
                    "quantity": "2",
                    "take_profit": "110",
                }
            ],
        },
    )
    requested = adapter.request_exit(record, reason="manual")
    intent = adapter.reconcile(requested, allow_writes=True)
    client.cancel_algo_error = BrokerTransportError(
        "timeout",
        write_may_have_reached=True,
    )
    unknown = adapter.reconcile(intent, allow_writes=True)
    client.cancel_algo_error = None
    client.algo_orders["protect-1"]["state"] = "canceled"

    recovered = adapter.reconcile(unknown, allow_writes=False)

    assert unknown.broker_state["write_unknown"] == "cancel_protection"
    assert recovered.broker_state["exit_phase"] == "submit_exit"
    assert "write_unknown" not in recovered.broker_state
    assert len(
        [call for call in client.calls if call[0] == "cancel_algo_orders"]
    ) == 1
    assert not [call for call in client.calls if call[0] == "place_order"]


def test_protection_fill_during_active_exit_cancel_is_carried_into_exit_pnl():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    client.algo_orders["protect-race"] = {
        "algoId": "protect-race",
        "state": "effective",
        "ordIdList": ["race-fill"],
    }
    client.orders["race-fill"] = {
        "ordId": "race-fill",
        "state": "filled",
        "accFillSz": "1",
        "avgPx": "110",
    }
    client.fills_by_order["race-fill"] = [{"fillPnl": "0.10"}]
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.OPEN,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        realized_pnl=Decimal("0"),
        broker_state={
            "protection_base_quantity": "2",
            "realized_before_protection": "0",
            "protection_targets": [
                {
                    "index": 1,
                    "client_algo_id": "protect-client-race",
                    "algo_id": "protect-race",
                    "state": "live",
                    "quantity": "1",
                    "take_profit": "110",
                }
            ],
        },
    )

    requested = adapter.request_exit(record, reason="manual")
    submit_phase = adapter.reconcile(requested, allow_writes=True)
    exit_intent = adapter.reconcile(submit_phase, allow_writes=True)

    assert submit_phase.remaining_quantity == Decimal("1")
    assert submit_phase.realized_pnl == Decimal("0.10")
    assert exit_intent.broker_state["exit_order"]["realized_before_exit"] == "0.10"


def test_active_exit_submit_unknown_never_retries_and_recovers_by_client_id():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.OPEN,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        broker_state={"protection_targets": []},
    )

    record = adapter.request_exit(record, reason="manual")
    submit_phase = adapter.reconcile(record, allow_writes=True)
    exit_intent = adapter.reconcile(submit_phase, allow_writes=True)
    client.place_order_error = BrokerTransportError(
        "timeout",
        write_may_have_reached=True,
    )
    unknown = adapter.reconcile(exit_intent, allow_writes=True)
    still_unknown = adapter.reconcile(unknown, allow_writes=True)

    client_id = unknown.broker_state["exit_order"]["client_order_id"]
    assert unknown.broker_state["exit_phase"] == "exit_unknown"
    assert unknown.broker_state["write_unknown"] == "exit"
    assert len([call for call in client.calls if call[0] == "place_order"]) == 1
    assert still_unknown.needs_attention is True

    client.orders["recovered-exit"] = {
        "ordId": "recovered-exit",
        "clOrdId": client_id,
        "state": "live",
        "accFillSz": "0",
        "avgPx": "",
    }
    recovered = adapter.reconcile(still_unknown, allow_writes=True)

    assert recovered.broker_state["exit_phase"] == "wait_exit"
    assert recovered.broker_state["exit_order"]["order_id"] == "recovered-exit"
    assert "write_unknown" not in recovered.broker_state
    assert len([call for call in client.calls if call[0] == "place_order"]) == 1


def test_partially_filled_canceled_exit_restores_protection_for_exact_remainder():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.OPEN,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        realized_pnl=Decimal("0"),
        broker_state={"protection_targets": []},
    )
    record = adapter.request_exit(record, reason="manual")
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    record = adapter.reconcile(record, allow_writes=True)
    exit_order_id = record.broker_state["exit_order"]["order_id"]
    client.orders[exit_order_id].update(
        {"state": "canceled", "accFillSz": "1", "avgPx": "110"}
    )
    client.fills_by_order[exit_order_id] = [{"fillPnl": "0.10"}]

    recovering = adapter.reconcile(record, allow_writes=True)
    protected_intent = adapter.reconcile(recovering, allow_writes=True)

    assert recovering.state == ExecutionState.PROTECTING
    assert recovering.remaining_quantity == Decimal("1")
    assert recovering.realized_pnl == Decimal("0.10")
    assert protected_intent.broker_state["protection_base_quantity"] == "1"
    assert protected_intent.broker_state["realized_before_protection"] == "0.10"
    assert sum(
        Decimal(item["quantity"])
        for item in protected_intent.broker_state["protection_targets"]
    ) == Decimal("1")


def test_canceled_protection_is_rebuilt_only_for_unfilled_remainder():
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    plan = _plan()
    preflight = adapter.preflight(plan)
    client.algo_orders["old-protection"] = {
        "algoId": "old-protection",
        "state": "canceled",
        "ordIdList": ["partial-exit"],
    }
    client.orders["partial-exit"] = {
        "ordId": "partial-exit",
        "state": "canceled",
        "accFillSz": "0.5",
        "avgPx": "110",
    }
    client.fills_by_order["partial-exit"] = [{"fillPnl": "0.05"}]
    record = ExecutionRecord(
        id=plan.id,
        plan=plan,
        state=ExecutionState.OPEN,
        selected_account="okx",
        preflight=preflight,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        average_fill_price=Decimal("100"),
        realized_pnl=Decimal("0"),
        broker_state={
            "protection_base_quantity": "2",
            "realized_before_protection": "0",
            "protection_targets": [
                {
                    "index": 1,
                    "client_algo_id": "old-client",
                    "algo_id": "old-protection",
                    "state": "live",
                    "retry": 0,
                    "quantity": "2",
                    "take_profit": "110",
                }
            ],
        },
    )

    rebuilding = adapter.reconcile(record, allow_writes=True)
    rebuilt = adapter.reconcile(rebuilding, allow_writes=True)

    replacement = rebuilding.broker_state["protection_targets"][-1]
    assert rebuilding.state == ExecutionState.PROTECTING
    assert rebuilding.remaining_quantity == Decimal("1.5")
    assert rebuilding.realized_pnl == Decimal("0.05")
    assert replacement["quantity"] == "1.5"
    assert replacement["retry"] == 1
    assert rebuilt.state == ExecutionState.OPEN
    body = [call[1] for call in client.calls if call[0] == "place_algo_order"][-1]
    assert body["sz"] == "1.5"
