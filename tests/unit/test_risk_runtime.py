from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from filelock import FileLock

from pa_agent.config.settings import Settings
from pa_agent.execution.models import (
    AccountSnapshot,
    ExecutionPlan,
    ExecutionState,
    PreflightResult,
)
from pa_agent.execution.okx_adapter import OkxAdapter
from pa_agent.execution.service import ExecutionService
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker_protocol import WorkerCommandAction
from pa_agent.execution.worker_store import WorkerStore
from pa_agent.risk.runtime import (
    RiskRuntime,
    RiskRuntimeBlocked,
    route_key,
)
from pa_agent.risk.sizing import RiskSizingResult
from tests.unit.test_execution_plan_builder import _persist, _record
from tests.unit.test_execution_service import FakePendingWriter
from tests.unit.test_okx_adapter import FakeOkxClient


class _MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


def _bill(
    bill_id: str,
    *,
    amount: str,
    subtype: str = "11",
    currency: str = "USDT",
    timestamp: str = "1720000000000",
) -> dict[str, str]:
    return {
        "billId": bill_id,
        "type": "1",
        "subType": subtype,
        "ccy": currency,
        "balChg": amount,
        "ts": timestamp,
    }


def _runtime(
    tmp_path,
    *,
    now: datetime | None = None,
) -> tuple[WorkerStore, RiskRuntime, _MutableClock]:
    clock = _MutableClock(now or datetime(2026, 7, 24, tzinfo=UTC))
    store = WorkerStore(tmp_path / "execution_control.sqlite3", clock=clock)
    return store, RiskRuntime(store, clock=clock), clock


def _refresh(
    runtime: RiskRuntime,
    *,
    equity: str,
    bills: list[dict[str, str]] | None = None,
    identity: str = "a" * 64,
):
    return runtime.refresh(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=identity,
        total_equity_usd=equity,
        bill_rows=bills or [],
    )


def test_external_deposit_lifts_high_water_and_preserves_ten_percent_budget(
    tmp_path,
):
    _store, runtime, clock = _runtime(tmp_path)
    first = _refresh(runtime, equity="4893.97")
    clock.advance(minutes=1)
    second = _refresh(
        runtime,
        equity="8899.30",
        bills=[
            _bill(
                "deposit-1",
                amount="4005.33",
                timestamp=str(int(clock.now.timestamp() * 1000)),
            )
        ],
    )

    assert first.adjusted_high_water_usd == Decimal("4893.97")
    assert second.adjusted_high_water_usd == Decimal("8899.30")
    assert second.drawdown_fraction == Decimal("0")
    assert second.last_external_cashflow_bill_id == "deposit-1"
    assert second.last_total_equity_usd * Decimal("0.10") == Decimal("889.930")


def test_initial_baseline_includes_historical_non_usdt_transfer_once(tmp_path):
    _store, runtime, _clock = _runtime(tmp_path)

    state = _refresh(
        runtime,
        equity="78975.61",
        bills=[
            _bill(
                "historical-eth-transfer",
                amount="1",
                currency="ETH",
            )
        ],
    )

    assert state.kill_active is False
    assert state.adjusted_high_water_usd == Decimal("78975.61")
    assert state.last_total_equity_usd == Decimal("78975.61")
    assert state.last_external_cashflow_bill_id == ""
    assert state.last_account_bill_id == "historical-eth-transfer"
    assert state.last_account_bill_timestamp_ms == 1720000000000
    assert state.last_bill_scan_at is not None


def test_failed_bootstrap_without_trusted_equity_can_recover(tmp_path):
    _store, runtime, clock = _runtime(tmp_path)
    failed = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity="a" * 64,
        reason="risk_runtime_unsupported_transfer_currency",
    )
    assert failed.kill_active is True
    assert failed.last_total_equity_usd is None
    assert failed.adjusted_high_water_usd is None
    assert failed.last_bill_scan_at is None

    clock.advance(minutes=1)
    recovered = _refresh(
        runtime,
        equity="78975.61",
        bills=[
            _bill(
                "historical-btc-transfer",
                amount="1",
                currency="BTC",
            )
        ],
    )

    assert recovered.kill_active is False
    assert recovered.kill_reason == ""
    assert recovered.adjusted_high_water_usd == Decimal("78975.61")
    assert recovered.last_account_bill_id == "historical-btc-transfer"


def test_established_state_with_missing_scan_boundary_does_not_rebaseline(
    tmp_path,
):
    store, runtime, clock = _runtime(tmp_path)
    established = _refresh(runtime, equity="1000")
    store.save_risk_runtime_state(
        replace(
            established,
            last_bill_scan_at=None,
        )
    )

    clock.advance(minutes=1)
    blocked = _refresh(runtime, equity="1100")

    assert blocked.kill_active is True
    assert blocked.kill_reason == "risk_runtime_bill_scan_boundary_missing"
    assert blocked.adjusted_high_water_usd == Decimal("1000")
    assert blocked.last_total_equity_usd == Decimal("1000")


def test_drawdown_stop_persists_across_refresh_and_manual_clear_is_explicit(
    tmp_path,
):
    store, runtime, clock = _runtime(tmp_path)
    _refresh(runtime, equity="1000")
    clock.advance(minutes=1)
    stopped = _refresh(runtime, equity="400")

    assert stopped.kill_active is True
    assert stopped.kill_reason == "drawdown_threshold_exceeded"
    with pytest.raises(RiskRuntimeBlocked):
        runtime.require_new_risk(route_key(broker="okx", environment="demo", account="okx"))

    restarted_store = WorkerStore(store.path, clock=clock)
    restarted_runtime = RiskRuntime(restarted_store, clock=clock)
    recovered = restarted_runtime.get("okx:demo:okx")
    assert recovered is not None
    assert recovered.kill_active is True

    clock.advance(minutes=1)
    still_stopped = _refresh(restarted_runtime, equity="700")
    assert still_stopped.kill_active is True

    cleared = restarted_runtime.clear("okx:demo:okx")
    assert cleared.kill_active is False
    assert cleared.adjusted_high_water_usd == Decimal("700")


def test_missing_bill_scan_boundary_fails_closed_without_destroying_high_water(
    tmp_path,
):
    store, runtime, clock = _runtime(tmp_path)
    _refresh(runtime, equity="1000")
    clock.advance(minutes=1)
    valid = _refresh(
        runtime,
        equity="1010",
        bills=[_bill("deposit-1", amount="10")],
    )
    store.save_risk_runtime_state(
        replace(
            valid,
            last_account_bill_id="missing-boundary",
            last_bill_scan_at=None,
        )
    )
    clock.advance(minutes=1)
    state = _refresh(runtime, equity="900", bills=[])

    assert state.kill_active is True
    assert state.kill_reason == "risk_runtime_bill_scan_boundary_missing"
    assert state.adjusted_high_water_usd == Decimal("1010")


def test_bill_scan_boundary_survives_old_transfer_leaving_seven_day_window(
    tmp_path,
):
    _store, runtime, clock = _runtime(tmp_path)
    old_transfer = _bill("deposit-old", amount="10")
    first = _refresh(runtime, equity="1010", bills=[old_transfer])
    assert first.last_account_bill_id == "deposit-old"

    for _day in range(6):
        clock.advance(days=1)
        healthy = _refresh(runtime, equity="1010", bills=[old_transfer])
        assert healthy.kill_active is False

    clock.advance(days=1)
    rolled_off = _refresh(runtime, equity="1010", bills=[])
    assert rolled_off.kill_active is False
    assert rolled_off.last_account_bill_id == ""

    clock.advance(minutes=1)
    new_timestamp = str(int(clock.now.timestamp() * 1000))
    deposited = _refresh(
        runtime,
        equity="1110",
        bills=[
            _bill(
                "deposit-new",
                amount="100",
                timestamp=new_timestamp,
            )
        ],
    )
    assert deposited.kill_active is False
    assert deposited.adjusted_high_water_usd == Decimal("1110")
    assert deposited.last_external_cashflow_bill_id == "deposit-new"


def test_bill_scan_gap_of_seven_days_fails_closed(tmp_path):
    _store, runtime, clock = _runtime(tmp_path)
    _refresh(
        runtime,
        equity="1010",
        bills=[_bill("deposit-old", amount="10")],
    )
    clock.advance(days=7)

    state = _refresh(runtime, equity="1010", bills=[])

    assert state.kill_active is True
    assert state.kill_reason == "risk_runtime_bill_scan_window_gap"


def test_internal_conversion_is_not_an_external_cashflow(tmp_path):
    _store, runtime, _clock = _runtime(tmp_path)
    state = _refresh(
        runtime,
        equity="1000",
        bills=[
            {
                "billId": "conversion-1",
                "type": "2",
                "subType": "1",
                "ccy": "USDT",
                "balChg": "5000",
                "ts": "1720000000000",
            }
        ],
    )
    assert state.last_external_cashflow_bill_id == ""
    assert state.adjusted_high_water_usd == Decimal("1000")


def test_worker_store_persists_risk_state_and_clear_command_without_execution_db(
    tmp_path,
):
    control_path = tmp_path / "execution_control.sqlite3"
    execution_path = tmp_path / "execution.sqlite3"
    store = WorkerStore(control_path)
    RiskRuntime(store).refresh(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity="a" * 64,
        total_equity_usd="1000",
        bill_rows=[],
    )
    command, created = store.enqueue(
        action=WorkerCommandAction.CLEAR_DRAWDOWN_STOP,
        requester="test",
        broker="okx",
        environment="demo",
        account="okx",
    )
    assert created is True
    assert command.action is WorkerCommandAction.CLEAR_DRAWDOWN_STOP

    ExecutionStore(execution_path)
    with sqlite3.connect(control_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    with sqlite3.connect(execution_path) as connection:
        execution_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "risk_runtime_state" in tables
    assert "risk_runtime_state" not in execution_tables


def test_worker_schema_v2_migrates_risk_runtime_table_under_worker_lock(tmp_path):
    path = tmp_path / "worker.sqlite3"
    WorkerStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO worker_commands(
                id, scope_key, action, execution_id, requester,
                broker, environment, account, new_risk_lease_id,
                reason_code, parameters_json, status, worker_id,
                created_at, started_at, finished_at, result_code,
                failure_code, result_json
            ) VALUES (
                'old-command', 'execution:old', 'submit', 'old', 'test',
                'okx', 'demo', 'okx', 'lease', '', 'null', 'uncertain',
                'worker', '2026-07-24T00:00:00+00:00',
                '2026-07-24T00:00:01+00:00',
                '2026-07-24T00:00:02+00:00', '', 'old_failure', 'null'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO worker_command_resolutions(
                command_id, resolution_code, evidence_json,
                evidence_digest, resolved_by, resolved_at
            ) VALUES ('old-command', 'confirmed', '{}', ?, 'test', ?)
            """,
            ("a" * 64, "2026-07-24T00:00:03+00:00"),
        )
        connection.execute("DROP TABLE risk_runtime_state")
        connection.execute(
            "UPDATE worker_meta SET value='2' "
            "WHERE key='worker_schema_version'"
        )
        connection.commit()

    deferred = WorkerStore(path)
    assert deferred.schema_version == 2
    with FileLock(str(tmp_path / "worker.lock")) as worker_lock:
        deferred.migrate_to_current(worker_lock=worker_lock)

    assert deferred.schema_version == 4
    with sqlite3.connect(path) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='risk_runtime_state'"
        ).fetchone()
        resolution = connection.execute(
            "SELECT command_id FROM worker_command_resolutions"
        ).fetchone()
    assert table == ("risk_runtime_state",)
    assert resolution == ("old-command",)


def test_worker_schema_v3_adds_bill_scan_boundary_under_worker_lock(tmp_path):
    path = tmp_path / "worker-v3.sqlite3"
    WorkerStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "ALTER TABLE risk_runtime_state "
            "DROP COLUMN last_account_bill_id"
        )
        connection.execute(
            "ALTER TABLE risk_runtime_state "
            "DROP COLUMN last_account_bill_timestamp_ms"
        )
        connection.execute(
            "ALTER TABLE risk_runtime_state DROP COLUMN last_bill_scan_at"
        )
        connection.execute(
            "UPDATE worker_meta SET value='3' "
            "WHERE key='worker_schema_version'"
        )
        connection.commit()

    deferred = WorkerStore(path)
    assert deferred.schema_version == 3
    with FileLock(str(tmp_path / "worker-v3.lock")) as worker_lock:
        deferred.migrate_to_current(worker_lock=worker_lock)

    assert deferred.schema_version == 4
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(risk_runtime_state)"
            )
        }
    assert {
        "last_account_bill_id",
        "last_account_bill_timestamp_ms",
        "last_bill_scan_at",
    }.issubset(columns)


class _RuntimeAdapter:
    def __init__(self, equity: str, *, target_size: str = "7") -> None:
        self.equity = Decimal(equity)
        self.target_size = Decimal(target_size)
        self.bills: list[dict[str, str]] = []
        self.calls: list[tuple[str, object]] = []
        self.identity = "b" * 64

    def bind_runtime_id(self, _runtime_id: str) -> None:
        return None

    def bind_write_executor(self, _executor) -> None:
        return None

    def account_identity(self, _plan, *, account_profile=None):
        return self.identity

    def account_bills(self):
        return list(self.bills)

    def account_snapshot(self, _plan, *, account_profile=None, broker_metadata=None):
        return AccountSnapshot(
            broker="okx",
            account_profile=account_profile or "okx-demo",
            equity=self.equity,
            raw_summary={"account_total_equity": str(self.equity)},
        )

    def calculate_risk_size(self, _plan, *, account_equity_usd):
        self.calls.append(("calculate_risk_size", account_equity_usd))
        return RiskSizingResult(
            target_contract_size=self.target_size,
            risk_budget_usdt=Decimal("100"),
            risk_used_usdt=Decimal("70"),
            stop_distance_usdt=Decimal("5"),
            contract_notional_usdt=Decimal("100"),
            price_loss_per_contract_usdt=Decimal("9"),
            fee_per_contract_usdt=Decimal("0.5"),
            slippage_per_contract_usdt=Decimal("0.5"),
            worst_case_loss_per_contract_usdt=Decimal("10"),
            lot_size=Decimal("1"),
            minimum_size=Decimal("1"),
            maximum_size=Decimal("20"),
        )

    def preflight(self, plan):
        self.calls.append(("preflight", plan.quantity))
        return PreflightResult(
            selected_account="okx",
            account_identity=self.identity,
            quantity=plan.quantity,
            entry_price=plan.entry_price,
            take_profit_1=plan.take_profit_1,
            take_profit_2=plan.take_profit_2,
            stop_loss=plan.stop_loss,
        )

    def prepare_submit(self, record):
        return record.model_copy(
            update={
                "state": ExecutionState.SUBMITTING,
                "selected_account": "okx",
                "client_order_id": "client-entry",
            }
        )

    def submit_entry(self, record):
        return record.model_copy(
            update={
                "state": ExecutionState.ENTRY_PENDING,
                "broker_order_id": "broker-entry",
            }
        )


def _service(tmp_path, monkeypatch, adapter, runtime):
    settings = Settings()
    settings.execution.enabled = True
    settings.execution.selected_broker = "okx"
    settings.execution.min_trade_confidence = 20
    settings.execution.okx.simulated = True
    settings.execution.okx.source_symbol = "XAU-USDT-SWAP"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    settings.execution.okx.product = "swap"
    settings.execution.okx.quantity = "7"
    record = _record()
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    path = _persist(record, tmp_path)
    service = ExecutionService(
        settings=settings,
        pending_writer=FakePendingWriter(path),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        adapter_factories={"okx": lambda _plan: adapter},
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: True,
        new_risk_authorizer=lambda _plan, _account: True,
        risk_runtime=runtime,
    )
    return service, record


def test_general_submit_freezes_supervised_quantity_and_uses_usdt_equity(
    tmp_path,
    monkeypatch,
):
    _store, runtime, _clock = _runtime(tmp_path)
    _refresh(runtime, equity="1000", identity="b" * 64)
    adapter = _RuntimeAdapter("1000")
    service, record = _service(tmp_path, monkeypatch, adapter, runtime)
    execution = service.prepare_analysis(record)

    submitted = service.submit(execution.id)

    assert submitted.state is ExecutionState.ENTRY_PENDING
    assert submitted.plan.quantity == Decimal("7")
    assert submitted.remaining_quantity == Decimal("7")
    assert submitted.broker_state["risk_sizing"]["target_contract_size"] == "7"
    assert submitted.broker_state["risk_sizing"]["account_equity_usdt"] == "1000"
    assert ("preflight", Decimal("7")) in adapter.calls


def test_general_submit_blocks_when_fresh_risk_quantity_changed(
    tmp_path,
    monkeypatch,
):
    _store, runtime, _clock = _runtime(tmp_path)
    _refresh(runtime, equity="1000", identity="b" * 64)
    adapter = _RuntimeAdapter("1000", target_size="8")
    service, record = _service(tmp_path, monkeypatch, adapter, runtime)
    execution = service.prepare_analysis(record)

    blocked = service.submit(execution.id)

    assert blocked.state is ExecutionState.BLOCKED
    assert blocked.last_error == "risk_sizing_changed_after_supervision"
    assert not any(name == "preflight" for name, _value in adapter.calls)


def test_general_submit_persists_drawdown_block_before_preflight(
    tmp_path,
    monkeypatch,
):
    _store, runtime, _clock = _runtime(tmp_path)
    _refresh(runtime, equity="1000", identity="b" * 64)
    adapter = _RuntimeAdapter("400")
    service, record = _service(tmp_path, monkeypatch, adapter, runtime)
    execution = service.prepare_analysis(record)

    blocked = service.submit(execution.id)

    assert blocked.state is ExecutionState.BLOCKED
    assert blocked.last_error == "drawdown_threshold_exceeded"
    assert not any(name == "preflight" for name, _value in adapter.calls)
    events = service.store.events(execution.id)
    event = next(
        event for event in events if event.kind == "risk_runtime_blocked"
    )
    assert event.payload == {
        "code": "drawdown_threshold_exceeded",
        "drawdown_fraction": "0.6",
        "adjusted_high_water": "1000",
    }


def test_okx_adapter_risk_sizing_uses_contract_specs_and_current_equity():
    client = FakeOkxClient()
    client.capacity_before = Decimal("1000")
    swap = client.instruments("SWAP")[0]
    client.instruments = lambda inst_type: (
        [
            {
                **swap,
                "ctVal": "0.1",
                "ctMult": "1",
            }
        ]
        if inst_type == "SWAP"
        else []
    )
    adapter = OkxAdapter(client, margin_mode="cross")
    plan = ExecutionPlan(
        id="risk-sizing-plan",
        analysis_digest="a" * 64,
        analysis_record_path="records/pending/risk.json",
        broker="okx",
        environment="demo",
        product="swap",
        requested_account="okx",
        source_symbol="XAU-USDT-SWAP",
        instrument="XAU-USDT-SWAP",
        direction="long",
        entry_type="limit",
        quantity=Decimal("999"),
        entry_price=Decimal("100"),
        take_profit_1=Decimal("110"),
        take_profit_2=Decimal("120"),
        stop_loss=Decimal("95"),
        trade_confidence=20,
        created_at="2026-07-24T00:00:00+00:00",
        config_fingerprint="risk",
        okx_api_base_url="https://www.okx.com",
        okx_margin_mode="cross",
        entry_order_mode="limit",
        exit_order_mode="market",
    )

    lower = adapter.calculate_risk_size(
        plan,
        account_equity_usd="100",
    )
    higher = adapter.calculate_risk_size(
        plan,
        account_equity_usd="200",
    )

    assert lower is not None
    assert higher is not None
    assert lower.target_contract_size > 0
    assert higher.target_contract_size > lower.target_contract_size
    assert higher.maximum_size == Decimal("1000")
