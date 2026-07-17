from __future__ import annotations

from decimal import Decimal

from pa_agent.config.settings import Settings
from pa_agent.execution.longbridge_adapter import LongbridgeAdapter
from pa_agent.execution.models import ExecutionState
from pa_agent.execution.okx_adapter import OkxAdapter
from pa_agent.execution.service import ExecutionService
from pa_agent.execution.store import ExecutionStore
from tests.unit.test_execution_plan_builder import _persist, _record
from tests.unit.test_execution_service import FakePendingWriter
from tests.unit.test_longbridge_adapter import FakeLongbridgeSession
from tests.unit.test_okx_adapter import FakeOkxClient


def _service(tmp_path, monkeypatch, *, settings, adapter):
    analysis = _record()
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    path = _persist(analysis, tmp_path)
    service = ExecutionService(
        settings=settings,
        pending_writer=FakePendingWriter(path),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        adapter_factories={
            settings.execution.selected_broker: lambda _plan: adapter
        },
        gate_checker=lambda: True,
        okx_live_gate_checker=lambda: True,
    )
    return service, analysis


def _okx_settings() -> Settings:
    settings = Settings()
    settings.execution.enabled = True
    settings.execution.selected_broker = "okx"
    settings.execution.okx.source_symbol = "XAUUSD"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    settings.execution.okx.quantity = "2"
    settings.execution.okx.product = "swap"
    return settings


def _longbridge_settings() -> Settings:
    settings = Settings()
    settings.execution.enabled = True
    settings.execution.selected_broker = "longbridge"
    settings.execution.longbridge.source_symbol = "XAUUSD"
    settings.execution.longbridge.instrument = "GLD.US"
    settings.execution.longbridge.quantity = "2"
    settings.execution.longbridge.preferred_account = "intraday"
    return settings


def test_okx_analysis_to_entry_protection_pnl_and_active_exit(
    tmp_path,
    monkeypatch,
):
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    service, analysis = _service(
        tmp_path,
        monkeypatch,
        settings=_okx_settings(),
        adapter=adapter,
    )
    execution = service.prepare_analysis(analysis)
    service.arm("启用实盘交易")
    execution = service.submit(execution.id)
    client.orders[execution.broker_order_id].update(
        {"state": "filled", "accFillSz": "2", "avgPx": "100"}
    )
    client.fills_by_order[execution.broker_order_id] = [
        {
            "fillSz": "2",
            "fillPx": "100",
            "fee": "0",
            "feeCcy": "USDT",
        }
    ]

    service.reconcile_once()
    service.reconcile_once()
    service.reconcile_once()
    opened = service.store.get(execution.id)

    assert opened is not None
    assert opened.state == ExecutionState.OPEN
    assert len(opened.broker_state["protection_targets"]) == 2

    first_target = opened.broker_state["protection_targets"][0]
    first_algo_id = first_target["algo_id"]
    client.algo_orders[first_algo_id].update(
        {"state": "effective", "ordIdList": ["tp-1"]}
    )
    client.orders["tp-1"] = {
        "ordId": "tp-1",
        "state": "filled",
        "accFillSz": "1",
        "avgPx": "110",
    }
    client.fills_by_order["tp-1"] = [{"fillPnl": "0.10"}]
    service.reconcile_once()
    partially_closed = service.store.get(execution.id)

    assert partially_closed is not None
    assert partially_closed.remaining_quantity == Decimal("1")
    assert partially_closed.realized_pnl == Decimal("0.10")

    service.request_exit(execution.id, reason="manual")
    service.reconcile_once()
    service.reconcile_once()
    current = service.store.get(execution.id)
    assert current is not None
    second_target = current.broker_state["protection_targets"][1]
    client.algo_orders[second_target["algo_id"]]["state"] = "canceled"
    service.reconcile_once()
    service.reconcile_once()
    service.reconcile_once()
    exit_pending = service.store.get(execution.id)

    assert exit_pending is not None
    exit_order_id = exit_pending.broker_state["exit_order"]["order_id"]
    client.orders[exit_order_id].update(
        {"state": "filled", "accFillSz": "1", "avgPx": "120"}
    )
    client.fills_by_order[exit_order_id] = [{"fillPnl": "0.20"}]
    service.reconcile_once()
    closed = service.store.get(execution.id)

    assert closed is not None
    assert closed.state == ExecutionState.CLOSED
    assert closed.remaining_quantity == Decimal("0")
    assert closed.realized_pnl == Decimal("0.30")


def test_okx_spot_analysis_to_oco_monitoring_and_active_exit(
    tmp_path,
    monkeypatch,
):
    settings = _okx_settings()
    settings.execution.okx.product = "spot"
    settings.execution.okx.instrument = "XAUT-USDT"
    client = FakeOkxClient()
    adapter = OkxAdapter(client)
    service, analysis = _service(
        tmp_path,
        monkeypatch,
        settings=settings,
        adapter=adapter,
    )
    execution = service.prepare_analysis(analysis)
    service.arm("启用实盘交易")
    execution = service.submit(execution.id)
    client.orders[execution.broker_order_id].update(
        {"state": "filled", "accFillSz": "2", "avgPx": "100"}
    )
    client.fills_by_order[execution.broker_order_id] = [
        {
            "fillSz": "2",
            "fillPx": "100",
            "fee": "0",
            "feeCcy": "USDT",
        }
    ]
    service.reconcile_once()
    service.reconcile_once()
    service.reconcile_once()
    opened = service.store.get(execution.id)

    assert opened is not None
    assert opened.state == ExecutionState.OPEN
    protection_calls = [
        call[1] for call in client.calls if call[0] == "place_algo_order"
    ]
    assert len(protection_calls) == 2
    assert all(call["ordType"] == "oco" for call in protection_calls)
    assert all("reduceOnly" not in call for call in protection_calls)

    first_target = opened.broker_state["protection_targets"][0]
    client.algo_orders[first_target["algo_id"]].update(
        {"state": "effective", "ordIdList": ["spot-tp-1"]}
    )
    client.orders["spot-tp-1"] = {
        "ordId": "spot-tp-1",
        "state": "filled",
        "accFillSz": "1",
        "avgPx": "110",
    }
    service.reconcile_once()
    partially_closed = service.store.get(execution.id)

    assert partially_closed is not None
    assert partially_closed.remaining_quantity == Decimal("1")
    assert partially_closed.realized_pnl == Decimal("10")

    service.request_exit(execution.id, reason="manual")
    service.reconcile_once()
    service.reconcile_once()
    current = service.store.get(execution.id)
    assert current is not None
    second_target = current.broker_state["protection_targets"][1]
    client.algo_orders[second_target["algo_id"]]["state"] = "canceled"
    service.reconcile_once()
    service.reconcile_once()
    service.reconcile_once()
    exit_pending = service.store.get(execution.id)
    assert exit_pending is not None
    exit_id = exit_pending.broker_state["exit_order"]["order_id"]
    client.orders[exit_id].update(
        {"state": "filled", "accFillSz": "1", "avgPx": "120"}
    )
    service.reconcile_once()
    closed = service.store.get(execution.id)

    exit_body = [call[1] for call in client.calls if call[0] == "place_order"][-1]
    assert exit_body["tgtCcy"] == "base_ccy"
    assert closed is not None
    assert closed.state == ExecutionState.CLOSED
    assert closed.realized_pnl == Decimal("30")


def test_longbridge_analysis_to_fallback_entry_stop_take_profit_and_exit(
    tmp_path,
    monkeypatch,
):
    intraday = FakeLongbridgeSession(maximum="0")
    comprehensive = FakeLongbridgeSession(maximum="10")
    sessions = {"intraday": intraday, "comprehensive": comprehensive}
    adapter = LongbridgeAdapter(lambda profile: sessions[profile])
    service, analysis = _service(
        tmp_path,
        monkeypatch,
        settings=_longbridge_settings(),
        adapter=adapter,
    )
    execution = service.prepare_analysis(analysis)
    service.arm("启用实盘交易")
    execution = service.submit(execution.id)

    assert execution.selected_account == "comprehensive"
    comprehensive.orders[execution.broker_order_id].update(
        {"state": "filled", "filled_quantity": "2", "average_fill_price": "100"}
    )
    service.reconcile_once()
    service.reconcile_once()
    opened = service.store.get(execution.id)

    assert opened is not None
    assert opened.state == ExecutionState.OPEN
    assert opened.broker_state["stop_order"]["order_id"]

    comprehensive.price = Decimal("111")
    service.reconcile_once()
    service.reconcile_once()
    current = service.store.get(execution.id)
    assert current is not None
    old_stop_id = current.broker_state["stop_order"]["order_id"]
    comprehensive.orders[old_stop_id]["state"] = "canceled"
    service.reconcile_once()
    service.reconcile_once()
    current = service.store.get(execution.id)
    assert current is not None
    exit_id = current.broker_state["partial_exit"]["order_id"]
    comprehensive.orders[exit_id].update(
        {"state": "filled", "filled_quantity": "1", "average_fill_price": "111"}
    )
    service.reconcile_once()
    service.reconcile_once()
    partially_closed = service.store.get(execution.id)

    assert partially_closed is not None
    assert partially_closed.state == ExecutionState.OPEN
    assert partially_closed.remaining_quantity == Decimal("1")
    assert partially_closed.realized_pnl == Decimal("11")

    service.request_exit(execution.id, reason="manual")
    service.reconcile_once()
    current = service.store.get(execution.id)
    assert current is not None
    final_stop_id = current.broker_state["stop_order"]["order_id"]
    comprehensive.orders[final_stop_id]["state"] = "canceled"
    service.reconcile_once()
    service.reconcile_once()
    exit_pending = service.store.get(execution.id)
    assert exit_pending is not None
    final_exit_id = exit_pending.broker_state["partial_exit"]["order_id"]
    comprehensive.orders[final_exit_id].update(
        {"state": "filled", "filled_quantity": "1", "average_fill_price": "120"}
    )
    service.reconcile_once()
    closed = service.store.get(execution.id)

    assert closed is not None
    assert closed.state == ExecutionState.CLOSED
    assert closed.remaining_quantity == Decimal("0")
    assert closed.realized_pnl == Decimal("31")
