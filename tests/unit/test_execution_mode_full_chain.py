from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from pa_agent.config.settings import Settings
from pa_agent.execution.controller import ExecutionController
from pa_agent.execution.models import ExecutionState
from pa_agent.execution.okx_adapter import OkxAdapter
from pa_agent.execution.service import ExecutionService
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker import ExecutionWorker, WorkerNewRiskAuthority
from pa_agent.execution.worker_protocol import WorkerCommandStatus
from pa_agent.execution.worker_store import WorkerStore
from pa_agent.records.schema import AnalysisRecord
from pa_agent.risk.sizing import calculate_risk_size
from tests.unit.test_execution_controller import _PendingWriter
from tests.unit.test_execution_plan_builder import _record
from tests.unit.test_okx_adapter import FakeOkxClient


@dataclass(frozen=True)
class _ChainResult:
    entry_body: dict
    exit_body: dict
    protection_bodies: tuple[dict, ...]
    plan_entry_price: Decimal
    plan_quantity: Decimal
    preflight_entry_price: Decimal
    final_state: ExecutionState


def _settings(
    *,
    entry_mode: str,
    exit_mode: str,
    atr: Decimal,
    quantity: Decimal,
) -> Settings:
    settings = Settings()
    settings.execution.enabled = True
    settings.execution.auto_execute = False
    settings.execution.selected_broker = "okx"
    settings.execution.min_trade_confidence = 20
    settings.execution.entry_order_mode = entry_mode
    settings.execution.exit_order_mode = exit_mode
    settings.execution.entry_slippage_atr_multiple = Decimal("0.50")
    settings.execution.exit_slippage_atr_multiple = Decimal("0.50")
    settings.execution.okx.simulated = True
    settings.execution.okx.source_symbol = "XAU-USDT-SWAP"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    settings.execution.okx.product = "swap"
    settings.execution.okx.quantity = str(quantity)
    return settings


def _analysis(*, atr: Decimal) -> AnalysisRecord:
    record = _record()
    return record.model_copy(
        update={
            "analysis_atr14": float(atr),
            "meta": record.meta.model_copy(
                update={
                    "timeframe": "10m",
                    "data_source": "okx",
                }
            ),
        }
    )


def _run_worker_command(worker: ExecutionWorker, command) -> None:
    finished = worker.run_once()
    assert finished is not None
    assert finished.id == command.id
    assert finished.status is WorkerCommandStatus.SUCCEEDED


def _reconcile(
    controller: ExecutionController,
    worker: ExecutionWorker,
) -> None:
    _run_worker_command(worker, controller.reconcile())


def _run_full_chain(
    root: Path,
    monkeypatch,
    *,
    entry_mode: str,
    exit_mode: str,
    atr: Decimal = Decimal("2"),
    quantity: Decimal = Decimal("2"),
) -> _ChainResult:
    pending = root / "pending"
    pending.mkdir(parents=True)
    analysis = _analysis(atr=atr)
    record_path = pending / "record.json"
    record_path.write_text(
        analysis.model_dump_json(indent=2),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pa_agent.config.paths.RECORDS_PENDING_DIR",
        pending,
    )

    controller_settings = _settings(
        entry_mode=entry_mode,
        exit_mode=exit_mode,
        atr=atr,
        quantity=quantity,
    )
    worker_settings = Settings()
    worker_settings.execution.enabled = False
    worker_settings.execution.selected_broker = "longbridge"
    execution_store = ExecutionStore(root / "execution.sqlite3")
    worker_store = WorkerStore(root / "control.sqlite3")
    worker_id = f"worker-{root.name}"
    authority = WorkerNewRiskAuthority(worker_store, worker_id)
    client = FakeOkxClient()
    client.simulated = True
    adapter = OkxAdapter(client)
    service = ExecutionService(
        settings=worker_settings,
        pending_writer=None,
        store=execution_store,
        adapter_factories={"okx": lambda _plan: adapter},
        gate_checker=lambda: False,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: False,
        new_risk_authorizer=authority.is_authorized,
        new_risk_revoker=lambda: worker_store.revoke_current_new_risk_lease(
            failure_code="service_disarmed",
        ),
    )
    worker = ExecutionWorker(
        store=worker_store,
        service=service,
        settings=worker_settings,
        lock_path=root / "worker.lock",
        worker_id=worker_id,
        new_risk_authority=authority,
    )
    controller = ExecutionController(
        settings=controller_settings,
        pending_writer=_PendingWriter(record_path),
        store=execution_store,
        worker_store=worker_store,
        worker_launcher=lambda: None,
        gate_checker=lambda: False,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: False,
    )

    worker.start()
    try:
        controller.arm("启用模拟交易")
        prepared = controller.prepare_analysis(analysis)
        assert prepared.plan.entry_order_mode == entry_mode
        assert prepared.plan.exit_order_mode == exit_mode
        _run_worker_command(worker, controller.submit(prepared.id))

        submitted = controller.get_execution(prepared.id)
        assert submitted is not None
        assert submitted.state is ExecutionState.ENTRY_PENDING
        assert submitted.broker_order_id
        client.orders[submitted.broker_order_id].update(
            {
                "state": "filled",
                "accFillSz": str(quantity),
                "avgPx": "100",
            }
        )
        client.fills_by_order[submitted.broker_order_id] = [
            {
                "fillSz": str(quantity),
                "fillPx": "100",
                "fee": "0",
                "feeCcy": "USDT",
            }
        ]

        for _ in range(8):
            _reconcile(controller, worker)
            opened = controller.get_execution(prepared.id)
            assert opened is not None
            if opened.state is ExecutionState.OPEN:
                break
        else:
            raise AssertionError("入场成交后未能经生产对账链进入 OPEN")

        protection_bodies = tuple(
            call[1] for call in client.calls if call[0] == "place_algo_order"
        )
        assert len(protection_bodies) == 2

        _run_worker_command(
            worker,
            controller.request_exit(prepared.id, reason="主动离场"),
        )
        marked_canceled: set[str] = set()
        filled_exit_id = ""
        for _ in range(16):
            _reconcile(controller, worker)
            for call in client.calls:
                if call[0] != "cancel_algo_orders":
                    continue
                payload = call[1]
                for item in payload:
                    algo_id = str(item["algoId"])
                    if algo_id in client.algo_orders:
                        client.algo_orders[algo_id]["state"] = "canceled"
                        marked_canceled.add(algo_id)
            current = controller.get_execution(prepared.id)
            assert current is not None
            exit_order = dict(current.broker_state.get("exit_order") or {})
            exit_order_id = str(exit_order.get("order_id") or "")
            if exit_order_id and exit_order_id not in marked_canceled:
                client.orders[exit_order_id].update(
                    {
                        "state": "filled",
                        "accFillSz": str(quantity),
                        "avgPx": "105",
                    }
                )
                client.fills_by_order[exit_order_id] = [{"fillPnl": "0"}]
                filled_exit_id = exit_order_id
            if current.state is ExecutionState.CLOSED:
                break
        else:
            raise AssertionError("主动离场后未能经生产对账链进入 CLOSED")

        assert marked_canceled == set(client.algo_orders)
        assert filled_exit_id
        order_bodies = [
            call[1] for call in client.calls if call[0] == "place_order"
        ]
        assert len(order_bodies) == 2
        final = controller.get_execution(prepared.id)
        assert final is not None
        assert final.remaining_quantity == Decimal("0")
        assert final.preflight is not None
        return _ChainResult(
            entry_body=order_bodies[0],
            exit_body=order_bodies[1],
            protection_bodies=protection_bodies,
            plan_entry_price=final.plan.entry_price,
            plan_quantity=final.plan.quantity,
            preflight_entry_price=final.preflight.entry_price,
            final_state=final.state,
        )
    finally:
        worker.close()


@pytest.mark.parametrize(
    ("entry_mode", "expected_type", "expected_price"),
    [
        ("limit", "limit", Decimal("100")),
        ("limit_with_slippage", "limit", Decimal("101")),
        ("market", "market", None),
    ],
)
@pytest.mark.parametrize(
    ("exit_mode", "expected_exit_type", "expected_exit_price"),
    [
        ("limit", "limit", Decimal("105")),
        ("limit_with_slippage", "limit", Decimal("104")),
        ("market", "market", None),
    ],
)
def test_all_nine_order_mode_combinations_use_the_production_chain(
    tmp_path,
    monkeypatch,
    entry_mode,
    expected_type,
    expected_price,
    exit_mode,
    expected_exit_type,
    expected_exit_price,
):
    result = _run_full_chain(
        tmp_path,
        monkeypatch,
        entry_mode=entry_mode,
        exit_mode=exit_mode,
    )

    assert result.final_state is ExecutionState.CLOSED
    assert result.entry_body["ordType"] == expected_type
    assert result.exit_body["ordType"] == expected_exit_type
    if expected_price is None:
        assert "px" not in result.entry_body
    else:
        assert Decimal(result.entry_body["px"]) == expected_price
    if expected_exit_price is None:
        assert "px" not in result.exit_body
    else:
        assert Decimal(result.exit_body["px"]) == expected_exit_price

    assert {
        Decimal(body["tpTriggerPx"]) for body in result.protection_bodies
    } == {Decimal("110"), Decimal("120")}
    assert all(
        Decimal(body["slTriggerPx"]) == Decimal("95")
        for body in result.protection_bodies
    )
    assert all(body["ordType"] == "oco" for body in result.protection_bodies)


def test_atr_two_to_four_doubles_entry_and_exit_slippage_without_moving_protection(
    tmp_path,
    monkeypatch,
):
    atr_two = _run_full_chain(
        tmp_path / "atr-two",
        monkeypatch,
        entry_mode="limit_with_slippage",
        exit_mode="limit_with_slippage",
        atr=Decimal("2"),
    )
    atr_four = _run_full_chain(
        tmp_path / "atr-four",
        monkeypatch,
        entry_mode="limit_with_slippage",
        exit_mode="limit_with_slippage",
        atr=Decimal("4"),
    )

    entry_gap_two = Decimal(atr_two.entry_body["px"]) - Decimal("100")
    entry_gap_four = Decimal(atr_four.entry_body["px"]) - Decimal("100")
    exit_gap_two = Decimal("105") - Decimal(atr_two.exit_body["px"])
    exit_gap_four = Decimal("105") - Decimal(atr_four.exit_body["px"])
    assert entry_gap_four == entry_gap_two * 2
    assert exit_gap_four == exit_gap_two * 2
    assert atr_two.plan_entry_price == atr_four.plan_entry_price == Decimal("100")
    for result in (atr_two, atr_four):
        assert {
            Decimal(body["tpTriggerPx"]) for body in result.protection_bodies
        } == {Decimal("110"), Decimal("120")}
        assert all(
            Decimal(body["slTriggerPx"]) == Decimal("95")
            for body in result.protection_bodies
        )


def test_risk_sizing_slippage_changes_quantity_but_not_submitted_price(
    tmp_path,
    monkeypatch,
):
    common = {
        "account_equity": "2",
        "risk_percent": "0.10",
        "entry_price": "100",
        "stop_loss_price": "95",
        "side": "long",
        "ct_val": "0.01",
        "ct_mult": "1",
        "lot_sz": "1",
        "min_sz": "1",
        "max_sz": "10",
        "fee_rate": "0.0005",
    }
    no_risk_slippage = calculate_risk_size(
        **common,
        slippage_rate="0",
    )
    conservative_risk_slippage = calculate_risk_size(
        **common,
        slippage_rate="0.01",
    )
    assert (
        conservative_risk_slippage.target_contract_size
        < no_risk_slippage.target_contract_size
    )

    no_slippage_chain = _run_full_chain(
        tmp_path / "sizing-no-slippage",
        monkeypatch,
        entry_mode="limit_with_slippage",
        exit_mode="limit_with_slippage",
        quantity=no_risk_slippage.target_contract_size,
    )
    conservative_chain = _run_full_chain(
        tmp_path / "sizing-conservative",
        monkeypatch,
        entry_mode="limit_with_slippage",
        exit_mode="limit_with_slippage",
        quantity=conservative_risk_slippage.target_contract_size,
    )

    assert (
        conservative_chain.plan_quantity
        < no_slippage_chain.plan_quantity
    )
    assert conservative_chain.entry_body["px"] == no_slippage_chain.entry_body["px"]
    assert conservative_chain.exit_body["px"] == no_slippage_chain.exit_body["px"]
    assert (
        conservative_chain.preflight_entry_price
        == no_slippage_chain.preflight_entry_price
    )
