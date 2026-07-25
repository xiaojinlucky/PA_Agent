from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

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
from pa_agent.risk.sizing import RiskCalculationFailure, RiskSizingResult
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
    store, runtime, _clock = _runtime(tmp_path)

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
    baseline = store.get_runtime_metadata(
        "risk_runtime_baseline:okx:demo:okx"
    )
    assert baseline is not None
    assert baseline["kind"] == "v4_cutover_baseline"
    assert baseline["route_key"] == "okx:demo:okx"
    assert baseline["account_identity_digest"] == "a" * 64
    assert baseline["baseline_total_equity_usd"] == "78975.61"
    assert baseline["adjusted_high_water_usd"] == "78975.61"
    assert baseline["last_account_bill_id"] == "historical-eth-transfer"
    assert baseline["last_account_bill_timestamp_ms"] == 1720000000000
    assert baseline["backfilled"] is False


def test_existing_v4_risk_state_backfills_immutable_honest_baseline_under_lock(
    tmp_path,
):
    path = tmp_path / "worker.sqlite3"
    lock_path = tmp_path / "worker.lock"
    clock = _MutableClock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    store = WorkerStore(
        path,
        clock=clock,
        worker_lock_path=lock_path,
    )
    runtime = RiskRuntime(store, clock=clock)
    _refresh(runtime, equity="1000")
    clock.advance(minutes=1)
    stopped = _refresh(runtime, equity="400")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM worker_meta "
            "WHERE key='risk_runtime_baseline:okx:demo:okx'"
        )
        connection.commit()
        state_before = connection.execute(
            "SELECT * FROM risk_runtime_state "
            "WHERE route_key='okx:demo:okx'"
        ).fetchone()

    with pytest.raises(RuntimeError, match="单例锁未持有"):
        store.backfill_risk_runtime_baselines(
            worker_lock=FileLock(str(lock_path))
        )
    assert (
        store.get_runtime_metadata(
            "risk_runtime_baseline:okx:demo:okx"
        )
        is None
    )

    with FileLock(str(lock_path)) as worker_lock:
        assert (
            store.backfill_risk_runtime_baselines(
                worker_lock=worker_lock
            )
            == 1
        )
    with sqlite3.connect(path) as connection:
        state_after = connection.execute(
            "SELECT * FROM risk_runtime_state "
            "WHERE route_key='okx:demo:okx'"
        ).fetchone()
    assert state_after == state_before
    assert store.get_risk_runtime_state(stopped.route_key) == stopped

    baseline = store.get_runtime_metadata(
        "risk_runtime_baseline:okx:demo:okx"
    )
    assert baseline is not None
    assert baseline["kind"] == "v4_cutover_baseline"
    assert baseline["route_key"] == "okx:demo:okx"
    assert baseline["account_identity_digest"] == stopped.account_identity
    assert baseline["baseline_total_equity_usd"] == "400"
    assert baseline["adjusted_high_water_usd"] == "1000"
    assert baseline["last_account_bill_id"] == stopped.last_account_bill_id
    assert baseline["last_bill_scan_at"] == stopped.last_bill_scan_at.isoformat(
        timespec="microseconds"
    )
    assert baseline["established_at"] == "2026-07-24T12:01:00.000000+00:00"
    assert baseline["recorded_at"] == baseline["established_at"]
    assert baseline["backfilled"] is True
    assert baseline["baseline_origin"] == (
        "existing_v4_risk_runtime_state_snapshot"
    )
    assert baseline["source_state_updated_at"] == (
        stopped.updated_at.isoformat(timespec="microseconds")
    )
    assert baseline["original_baseline_established_at"] is None
    assert baseline["historical_maximum_claimed"] is False

    clock.advance(minutes=5)
    with FileLock(str(lock_path)) as worker_lock:
        assert (
            store.backfill_risk_runtime_baselines(
                worker_lock=worker_lock
            )
            == 0
        )
    assert store.get_runtime_metadata(
        "risk_runtime_baseline:okx:demo:okx"
    ) == baseline


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


def test_clear_rejects_non_drawdown_kill_reason(tmp_path):
    store, runtime, _clock = _runtime(tmp_path)
    trusted = _refresh(runtime, equity="1000")
    blocked = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason="risk_runtime_bill_scan_window_gap",
    )

    with pytest.raises(RiskRuntimeBlocked) as exc_info:
        runtime.clear(blocked.route_key)

    assert exc_info.value.code == "risk_clear_reason_not_allowed"
    persisted = store.get_risk_runtime_state(blocked.route_key)
    assert persisted is not None
    assert persisted.kill_active is True
    assert persisted.kill_reason == "risk_runtime_bill_scan_window_gap"
    assert persisted.adjusted_high_water_usd == Decimal("1000")


@pytest.mark.parametrize(
    "reason",
    [
        "risk_runtime_BrokerApiError",
        "risk_runtime_BrokerTransportError",
        "risk_runtime_IncompleteRead",
        "risk_runtime_50004",
    ],
)
def test_manual_clear_rejects_transient_broker_read_failure(
    tmp_path,
    reason,
):
    store, runtime, _clock = _runtime(tmp_path)
    trusted = _refresh(runtime, equity="1000")
    blocked = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason=reason,
    )

    with pytest.raises(RiskRuntimeBlocked) as exc_info:
        runtime.clear(blocked.route_key)

    assert exc_info.value.code == "risk_clear_reason_not_allowed"
    persisted = store.get_risk_runtime_state(blocked.route_key)
    assert persisted is not None
    assert persisted.kill_active is True
    assert persisted.kill_reason == reason
    assert persisted.adjusted_high_water_usd == Decimal("1000")


def test_transient_read_failure_recovery_requires_fresh_read_and_preserves_high_water(
    tmp_path,
):
    store, runtime, clock = _runtime(tmp_path)
    trusted = _refresh(runtime, equity="1000")
    blocked = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason="risk_runtime_BrokerTransportError",
    )

    with pytest.raises(RiskRuntimeBlocked) as exc_info:
        runtime.recover_transient_read_failure(blocked.route_key)
    assert exc_info.value.code == "risk_recovery_evidence_incomplete"

    clock.advance(minutes=1)
    refreshed = _refresh(runtime, equity="900")
    assert refreshed.kill_active is True
    assert refreshed.kill_reason == "risk_runtime_BrokerTransportError"
    assert refreshed.adjusted_high_water_usd == Decimal("1000")
    assert refreshed.last_total_equity_usd == Decimal("900")
    assert refreshed.drawdown_fraction == Decimal("0.1")

    recovered = runtime.recover_transient_read_failure(blocked.route_key)

    assert recovered.kill_active is False
    assert recovered.kill_reason == ""
    assert recovered.adjusted_high_water_usd == Decimal("1000")
    assert recovered.last_total_equity_usd == Decimal("900")
    assert recovered.drawdown_fraction == Decimal("0.1")
    with sqlite3.connect(store.path) as connection:
        evidence_rows = connection.execute(
            """
            SELECT value
            FROM worker_meta
            WHERE key LIKE 'risk_runtime_evidence:okx:demo:okx:%'
            """
        ).fetchall()
    assert len(evidence_rows) == 1
    assert '"kind":"transient_risk_read_recovery"' in evidence_rows[0][0]
    assert (
        '"preserved_adjusted_high_water_usd":"1000"'
        in evidence_rows[0][0]
    )
    assert '"preserved_last_total_equity_usd":"900"' in evidence_rows[0][0]
    assert '"preserved_drawdown_fraction":"0.1"' in evidence_rows[0][0]
    assert '"failure_at":"2026-07-24T00:00:00.000000+00:00"' in (
        evidence_rows[0][0]
    )
    assert '"last_external_cashflow_bill_id":""' in evidence_rows[0][0]
    assert '"last_account_bill_id":""' in evidence_rows[0][0]
    assert '"last_account_bill_timestamp_ms":null' in evidence_rows[0][0]


def test_legacy_incomplete_read_stop_uses_dedicated_recovery_without_reanchor(
    tmp_path,
):
    _store, runtime, clock = _runtime(tmp_path)
    trusted = _refresh(runtime, equity="1000")
    blocked = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason="risk_runtime_IncompleteRead",
    )
    clock.advance(minutes=1)
    refreshed = _refresh(runtime, equity="900")

    recovered = runtime.recover_transient_read_failure(blocked.route_key)

    assert refreshed.adjusted_high_water_usd == Decimal("1000")
    assert recovered.adjusted_high_water_usd == Decimal("1000")
    assert recovered.last_total_equity_usd == Decimal("900")
    assert recovered.drawdown_fraction == Decimal("0.1")
    assert recovered.kill_active is False


def test_transient_read_failure_recovery_rejects_integrity_stop(tmp_path):
    _store, runtime, clock = _runtime(tmp_path)
    trusted = _refresh(runtime, equity="1000")
    blocked = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason="risk_runtime_bill_scan_window_gap",
    )
    clock.advance(minutes=1)
    _refresh(runtime, equity="900")

    with pytest.raises(RiskRuntimeBlocked) as exc_info:
        runtime.recover_transient_read_failure(blocked.route_key)

    assert exc_info.value.code == "risk_recovery_reason_not_allowed"


def test_repeated_transient_failure_invalidates_older_successful_read(tmp_path):
    _store, runtime, clock = _runtime(tmp_path)
    trusted = _refresh(runtime, equity="1000")
    first_failure = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason="risk_runtime_BrokerTransportError",
    )
    clock.advance(minutes=1)
    _refresh(runtime, equity="900")
    clock.advance(minutes=1)
    repeated_failure = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason="risk_runtime_BrokerTransportError",
    )

    assert repeated_failure.kill_activated_at is not None
    assert first_failure.kill_activated_at is not None
    assert repeated_failure.kill_activated_at > first_failure.kill_activated_at
    with pytest.raises(RiskRuntimeBlocked) as exc_info:
        runtime.recover_transient_read_failure(repeated_failure.route_key)

    assert exc_info.value.code == "risk_recovery_evidence_incomplete"


def test_transient_failure_cannot_downgrade_existing_drawdown_stop(tmp_path):
    store, runtime, clock = _runtime(tmp_path)
    trusted = _refresh(runtime, equity="1000")
    clock.advance(minutes=1)
    drawdown_stop = _refresh(runtime, equity="400")
    clock.advance(minutes=1)
    recovered_equity = _refresh(runtime, equity="700")
    clock.advance(minutes=1)

    after_transient_failure = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason="risk_runtime_BrokerTransportError",
    )

    assert recovered_equity.kill_reason == "drawdown_threshold_exceeded"
    assert after_transient_failure.kill_reason == (
        "drawdown_threshold_exceeded"
    )
    assert after_transient_failure.kill_activated_at == (
        drawdown_stop.kill_activated_at
    )
    with pytest.raises(RiskRuntimeBlocked) as exc_info:
        runtime.recover_transient_read_failure(
            after_transient_failure.route_key
        )
    assert exc_info.value.code == "risk_recovery_reason_not_allowed"
    with sqlite3.connect(store.path) as connection:
        evidence_values = [
            row[0]
            for row in connection.execute(
                """
                SELECT value
                FROM worker_meta
                WHERE key LIKE 'risk_runtime_evidence:okx:demo:okx:%'
                """
            ).fetchall()
        ]
    assert any(
        '"kind":"transient_risk_read_failure_while_stopped"' in value
        and '"preserved_kill_reason":"drawdown_threshold_exceeded"' in value
        for value in evidence_values
    )


def test_transient_failure_cannot_downgrade_existing_integrity_stop(tmp_path):
    _store, runtime, clock = _runtime(tmp_path)
    trusted = _refresh(runtime, equity="1000")
    integrity_stop = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason="risk_runtime_account_identity_changed",
    )
    clock.advance(minutes=1)

    after_transient_failure = runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason="risk_runtime_BrokerApiError",
    )

    assert after_transient_failure.kill_reason == (
        "risk_runtime_account_identity_changed"
    )
    assert after_transient_failure.kill_activated_at == (
        integrity_stop.kill_activated_at
    )
    with pytest.raises(RiskRuntimeBlocked) as exc_info:
        runtime.recover_transient_read_failure(
            after_transient_failure.route_key
        )
    assert exc_info.value.code == "risk_recovery_reason_not_allowed"


@pytest.mark.parametrize(
    "reason",
    [
        "risk_runtime_BrokerApiError",
        "risk_runtime_BrokerTransportError",
        "risk_runtime_50004",
    ],
)
def test_transient_failure_cannot_hide_or_clear_current_60_percent_drawdown(
    tmp_path,
    reason,
):
    store, runtime, clock = _runtime(tmp_path)
    trusted = _refresh(runtime, equity="1000")
    runtime.mark_failure(
        broker="okx",
        environment="demo",
        account="okx",
        account_identity=trusted.account_identity,
        reason=reason,
    )
    clock.advance(minutes=1)

    stopped = _refresh(runtime, equity="400")

    assert stopped.kill_active is True
    assert stopped.kill_reason == "drawdown_threshold_exceeded"
    assert stopped.adjusted_high_water_usd == Decimal("1000")
    assert stopped.last_total_equity_usd == Decimal("400")
    assert stopped.drawdown_fraction == Decimal("0.6")

    with pytest.raises(RiskRuntimeBlocked) as exc_info:
        runtime.clear(stopped.route_key)

    assert exc_info.value.code == "drawdown_threshold_exceeded"
    persisted = store.get_risk_runtime_state(stopped.route_key)
    assert persisted is not None
    assert persisted.kill_active is True
    assert persisted.kill_reason == "drawdown_threshold_exceeded"
    assert persisted.adjusted_high_water_usd == Decimal("1000")
    assert persisted.last_total_equity_usd == Decimal("400")
    assert persisted.drawdown_fraction == Decimal("0.6")


def test_identity_mismatch_preserves_original_trusted_identity(tmp_path):
    store, runtime, clock = _runtime(tmp_path)
    original_identity = "a" * 64
    observed_identity = "b" * 64
    trusted = _refresh(
        runtime,
        equity="1000",
        identity=original_identity,
    )
    clock.advance(minutes=1)

    blocked = _refresh(
        runtime,
        equity="1000",
        identity=observed_identity,
    )

    assert blocked.kill_active is True
    assert blocked.kill_reason == "risk_runtime_account_identity_changed"
    assert blocked.account_identity == original_identity
    assert blocked.adjusted_high_water_usd == trusted.adjusted_high_water_usd
    assert blocked.last_account_bill_id == trusted.last_account_bill_id
    assert (
        store.get_risk_runtime_state(blocked.route_key).account_identity
        == original_identity
    )


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
    parameters_json = json.dumps(
        {
            "intent": "preserve",
            "nested": {"quantity": "7", "leverage": "20"},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    result_json = json.dumps(
        {"result": "preserve", "broker_reference": "demo-only"},
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_json = json.dumps(
        {
            "broker": "okx",
            "environment": "demo",
            "observed_at": "2026-07-24T00:00:03+00:00",
            "position_count": 0,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_digest = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO worker_commands(
                id, scope_key, action, execution_id, requester,
                broker, environment, account, new_risk_lease_id,
                reason_code, parameters_json, status, worker_id,
                created_at, started_at, finished_at, result_code,
                failure_code, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    "old-resolved",
                    "execution:old-resolved",
                    "set_leverage",
                    "old-resolved",
                    "test",
                    "okx",
                    "demo",
                    "okx",
                    "lease-resolved",
                    "legacy_reason",
                    parameters_json,
                    "uncertain",
                    "worker-resolved",
                    "2026-07-24T00:00:00+00:00",
                    "2026-07-24T00:00:01+00:00",
                    "2026-07-24T00:00:02+00:00",
                    "legacy_result",
                    "BrokerTransportError",
                    result_json,
                ),
                (
                    "old-unresolved",
                    "execution:old-unresolved",
                    "submit",
                    "old-unresolved",
                    "test",
                    "okx",
                    "demo",
                    "okx",
                    "lease-unresolved",
                    "",
                    "null",
                    "uncertain",
                    "worker-unresolved",
                    "2026-07-24T00:00:04+00:00",
                    "2026-07-24T00:00:05+00:00",
                    "2026-07-24T00:00:06+00:00",
                    "",
                    "BrokerTransportError",
                    "null",
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO worker_command_resolutions(
                command_id, resolution_code, evidence_json,
                evidence_digest, resolved_by, resolved_at
            ) VALUES (
                'old-resolved', 'confirmed_read_only',
                ?, ?, 'migration-test',
                '2026-07-24T00:00:03+00:00'
            )
            """,
            (evidence_json, evidence_digest),
        )
        connection.execute("DROP TABLE risk_runtime_state")
        connection.execute(
            "UPDATE worker_meta SET value='2' "
            "WHERE key='worker_schema_version'"
        )
        connection.commit()
        before_commands = tuple(
            connection.execute(
                """
                SELECT
                    id, scope_key, action, execution_id, requester,
                    broker, environment, account, new_risk_lease_id,
                    reason_code, parameters_json, status, worker_id,
                    created_at, started_at, finished_at, result_code,
                    failure_code, result_json
                FROM worker_commands
                ORDER BY created_at, id
                """
            )
        )
        before_resolutions = tuple(
            connection.execute(
                """
                SELECT
                    command_id, resolution_code, evidence_json,
                    evidence_digest, resolved_by, resolved_at
                FROM worker_command_resolutions
                ORDER BY command_id
                """
            )
        )

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
            """
            SELECT
                command_id, resolution_code, evidence_json,
                evidence_digest, resolved_by, resolved_at
            FROM worker_command_resolutions
            ORDER BY command_id
            """
        ).fetchall()
        after_commands = tuple(
            connection.execute(
                """
                SELECT
                    id, scope_key, action, execution_id, requester,
                    broker, environment, account, new_risk_lease_id,
                    reason_code, parameters_json, status, worker_id,
                    created_at, started_at, finished_at, result_code,
                    failure_code, result_json
                FROM worker_commands
                ORDER BY created_at, id
                """
            )
        )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    assert table == ("risk_runtime_state",)
    assert after_commands == before_commands
    assert tuple(resolution) == before_resolutions
    assert hashlib.sha256(resolution[0][2].encode("utf-8")).hexdigest() == (
        resolution[0][3]
    )
    assert integrity == ("ok",)
    unresolved = deferred.list_unresolved_write_commands(
        broker="okx",
        environment="demo",
        account="okx",
    )
    assert [command.id for command in unresolved] == ["old-unresolved"]


def test_worker_store_v1_open_is_byte_identical_before_worker_lock(tmp_path):
    path = tmp_path / "worker-v1.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute(
            """
            CREATE TABLE worker_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO worker_meta(key, value)
            VALUES ('worker_schema_version', '1')
            """
        )
        connection.execute(
            """
            CREATE TABLE worker_commands (
                id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                action TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                requester TEXT NOT NULL,
                broker TEXT NOT NULL,
                environment TEXT NOT NULL,
                account TEXT NOT NULL,
                new_risk_lease_id TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                status TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                result_code TEXT NOT NULL,
                failure_code TEXT NOT NULL
            )
            """
        )
        connection.commit()

    before_bytes = path.read_bytes()
    before_digest = hashlib.sha256(before_bytes).hexdigest()
    with sqlite3.connect(path) as connection:
        before_schema = tuple(
            connection.execute(
                """
                SELECT type, name, sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            )
        )

    deferred = WorkerStore(path)

    assert deferred.schema_version == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_digest
    assert path.read_bytes() == before_bytes
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()
    with sqlite3.connect(path) as connection:
        after_schema = tuple(
            connection.execute(
                """
                SELECT type, name, sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            )
        )
    assert after_schema == before_schema


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
    def __init__(
        self,
        equity: str,
        *,
        target_size: str = "7",
        risk_used: str = "70",
        contract_notional: str = "100",
        worst_case_loss: str = "10",
    ) -> None:
        self.equity = Decimal(equity)
        self.target_size = Decimal(target_size)
        self.risk_used = Decimal(risk_used)
        self.contract_notional = Decimal(contract_notional)
        self.worst_case_loss = Decimal(worst_case_loss)
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

    def calculate_risk_size(
        self,
        plan,
        *,
        account_equity_usd,
        risk_capital_cap_usdt,
        risk_percent,
    ):
        if Decimal(str(risk_capital_cap_usdt)) <= 0:
            raise RiskCalculationFailure(
                "invalid_input",
                "risk_capital_cap 必须是正数",
            )
        self.calls.append(
            (
                "calculate_risk_size",
                account_equity_usd,
                risk_capital_cap_usdt,
                risk_percent,
            )
        )
        fixed_mode = plan.authorized_sizing_mode == "fixed_quantity"
        risk_percent_value = (
            Decimal("0.07") if fixed_mode else Decimal(str(risk_percent))
        )
        risk_budget = Decimal("70") if fixed_mode else Decimal("100")
        return RiskSizingResult(
            target_contract_size=self.target_size,
            risk_budget_usdt=risk_budget,
            risk_used_usdt=self.risk_used,
            stop_distance_usdt=Decimal("5"),
            contract_notional_usdt=self.contract_notional,
            price_loss_per_contract_usdt=Decimal("9"),
            fee_per_contract_usdt=Decimal("0.5"),
            slippage_per_contract_usdt=Decimal("0.5"),
            worst_case_loss_per_contract_usdt=self.worst_case_loss,
            lot_size=Decimal("1"),
            minimum_size=Decimal("1"),
            maximum_size=Decimal("20"),
            account_equity_usdt=Decimal(str(account_equity_usd)),
            risk_capital_cap_usdt=Decimal(str(risk_capital_cap_usdt)),
            effective_risk_capital_usdt=min(
                Decimal(str(account_equity_usd)),
                Decimal(str(risk_capital_cap_usdt)),
            ),
            risk_percent=risk_percent_value,
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
            broker_metadata={"current_leverage": "20"},
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


def _service(
    tmp_path,
    monkeypatch,
    adapter,
    runtime,
    *,
    sizing_mode="risk_budget",
):
    settings = Settings()
    settings.execution.enabled = True
    settings.execution.selected_broker = "okx"
    settings.execution.min_trade_confidence = 20
    settings.execution.okx.simulated = True
    settings.execution.okx.source_symbol = "XAU-USDT-SWAP"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    settings.execution.okx.product = "swap"
    settings.execution.okx.quantity = "7"
    settings.execution.okx.sizing_mode = sizing_mode
    settings.execution.okx.risk_capital_cap_usdt = "1000"
    settings.execution.okx.risk_percent = "0.10"
    settings.execution.okx.maximum_leverage = "20"
    record = _record()
    response = (
        dict(record.stage2_response)
        if isinstance(record.stage2_response, dict)
        else {}
    )
    response["risk_sizing"] = {
        "sizing_mode": sizing_mode,
        "equity_basis": "fixed_cap_or_usdt_equity_whichever_lower",
        "account_total_equity_usd": "1000",
        "equity_usdt": "1000",
        "risk_capital_cap_usdt": "1000",
        "effective_risk_capital_usdt": "1000",
        "risk_percent": "0.07" if sizing_mode == "fixed_quantity" else "0.10",
        "risk_budget_usdt": "70" if sizing_mode == "fixed_quantity" else "100",
        "risk_used_usdt": "70",
        "contract_notional_usdt": "100",
        "worst_case_loss_per_contract_usdt": "10",
        "target_quantity": "7",
    }
    record = record.model_copy(update={"stage2_response": response})
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
    assert (
        submitted.broker_state["risk_sizing"]["risk_capital_cap_usdt"]
        == "1000"
    )
    assert (
        submitted.broker_state["risk_sizing"]["effective_risk_capital_usdt"]
        == "1000"
    )
    assert submitted.broker_state["risk_sizing"]["risk_percent"] == "0.10"
    sizing_call = next(
        call for call in adapter.calls if call[0] == "calculate_risk_size"
    )
    assert sizing_call[1:] == (
        Decimal("1000"),
        Decimal("1000"),
        Decimal("0.10"),
    )


def test_fixed_quantity_plan_reaches_worker_preflight_without_risk_mode_rewrite(
    tmp_path,
    monkeypatch,
):
    _store, runtime, _clock = _runtime(tmp_path)
    _refresh(runtime, equity="1000", identity="b" * 64)
    adapter = _RuntimeAdapter("1000")
    service, record = _service(
        tmp_path,
        monkeypatch,
        adapter,
        runtime,
        sizing_mode="fixed_quantity",
    )
    execution = service.prepare_analysis(record)

    submitted = service.submit(execution.id)

    assert submitted.state is ExecutionState.ENTRY_PENDING
    assert submitted.plan.authorized_sizing_mode == "fixed_quantity"
    assert submitted.plan.authorized_fixed_quantity == Decimal("7")
    assert submitted.plan.quantity == Decimal("7")
    assert submitted.plan.authorized_risk_percent == Decimal("0.07")
    assert submitted.broker_state["risk_sizing"]["sizing_mode"] == "fixed_quantity"
    assert submitted.broker_state["risk_sizing"]["risk_percent"] == "0.07"
    assert ("preflight", Decimal("7")) in adapter.calls


def test_zero_fixed_risk_cap_blocks_before_broker_submit(
    tmp_path,
    monkeypatch,
):
    _store, runtime, _clock = _runtime(tmp_path)
    _refresh(runtime, equity="1000", identity="b" * 64)
    adapter = _RuntimeAdapter("1000")
    service, record = _service(tmp_path, monkeypatch, adapter, runtime)
    service._settings.execution.okx.risk_capital_cap_usdt = "0"
    execution = service.prepare_analysis(record)

    blocked = service.submit(execution.id)

    assert blocked.state is ExecutionState.BLOCKED
    assert blocked.last_error == "invalid_input"
    assert not any(call[0] == "preflight" for call in adapter.calls)


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
    assert blocked.last_error == "risk_sizing_changed_after_authorization"
    assert not any(call[0] == "preflight" for call in adapter.calls)


def test_general_submit_blocks_when_single_contract_risk_changed_same_quantity(
    tmp_path,
    monkeypatch,
):
    _store, runtime, _clock = _runtime(tmp_path)
    _refresh(runtime, equity="1000", identity="b" * 64)
    adapter = _RuntimeAdapter(
        "1000",
        target_size="7",
        risk_used="77",
        worst_case_loss="11",
    )
    service, record = _service(tmp_path, monkeypatch, adapter, runtime)
    execution = service.prepare_analysis(record)

    blocked = service.submit(execution.id)

    assert blocked.state is ExecutionState.BLOCKED
    assert blocked.last_error == "risk_sizing_changed_after_authorization"
    assert not any(call[0] == "preflight" for call in adapter.calls)


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
    assert not any(call[0] == "preflight" for call in adapter.calls)
    events = service.store.events(execution.id)
    event = next(
        event for event in events if event.kind == "risk_runtime_blocked"
    )
    assert event.payload == {
        "code": "drawdown_threshold_exceeded",
        "drawdown_fraction": "0.6",
        "adjusted_high_water": "1000",
    }


def test_set_leverage_refreshes_risk_runtime_and_blocks_before_post_at_50_percent(
    tmp_path,
    monkeypatch,
):
    _store, runtime, _clock = _runtime(tmp_path)
    _refresh(runtime, equity="1000", identity="b" * 64)

    class _LeverageAdapter:
        def __init__(self):
            self.posts = 0

        def account_snapshot(self, _plan):
            return AccountSnapshot(
                broker="okx",
                account_profile="okx-demo",
                equity=Decimal("400"),
                raw_summary={"account_total_equity": "400"},
            )

        def account_identity(self, _plan):
            return "b" * 64

        def account_bills(self):
            return []

        def bind_write_executor(self, _executor):
            return None

        def set_leverage(
            self,
            _parameters,
            *,
            environment,
            maximum_leverage_cap,
        ):
            del environment, maximum_leverage_cap
            self.posts += 1
            raise AssertionError("回撤闸门关闭后不得发送杠杆 POST")

    adapter = _LeverageAdapter()
    settings = Settings()
    settings.execution.enabled = True
    settings.execution.selected_broker = "okx"
    settings.execution.okx.simulated = True
    settings.execution.okx.product = "swap"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    service = ExecutionService(
        settings=settings,
        pending_writer=FakePendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        leverage_adapter_factory=lambda _command: adapter,
        paper_gate_checker=lambda: True,
        new_risk_authorizer=lambda _plan, _account: True,
        risk_runtime=runtime,
    )
    service.arm(service.arm_confirmation_text())
    monkeypatch.setattr(
        "pa_agent.execution.service.validate_leverage_authorization",
        lambda _parameters: None,
    )
    monkeypatch.setattr(
        "pa_agent.execution.service.validate_current_leverage_policy",
        lambda _parameters, _settings: None,
    )
    parameters = SimpleNamespace(
        analysis_digest="a" * 64,
        config_fingerprint="fixed-cap-test",
        instrument="XAU-USDT-SWAP",
    )
    command = SimpleNamespace(
        action=WorkerCommandAction.SET_LEVERAGE,
        parameters=parameters,
        broker="okx",
        environment="demo",
        account="okx",
    )

    with pytest.raises(RiskRuntimeBlocked) as exc_info:
        service.set_leverage(command)

    assert exc_info.value.code == "drawdown_threshold_exceeded"
    assert adapter.posts == 0


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
        risk_capital_cap_usdt="100",
        risk_percent="0.10",
    )
    higher = adapter.calculate_risk_size(
        plan,
        account_equity_usd="200",
        risk_capital_cap_usdt="200",
        risk_percent="0.10",
    )

    assert lower is not None
    assert higher is not None
    assert lower.target_contract_size > 0
    assert higher.target_contract_size > lower.target_contract_size
    assert higher.maximum_size == Decimal("1000")
