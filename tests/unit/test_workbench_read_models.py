"""工作台只读读取层测试。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from pa_agent.config.settings import Settings
from pa_agent.execution.models import AccountSnapshot, ExecutionState
from pa_agent.execution.worker_protocol import WorkerHeartbeat, WorkerState
from pa_agent.gui.read_models import FactCertainty, WorkbenchReadModel


class _ExecutionStore:
    def __init__(self, *, active=(), recent=(), account=None) -> None:
        self.active = list(active)
        self.recent = list(recent)
        self.account = account
        self.account_route: tuple[str, str] | None = None

    def list_active(self):
        return self.active

    def list_recent(self, limit: int = 50):
        assert limit == 1
        return self.recent

    def latest_account_snapshot(self, broker: str, account_profile: str):
        self.account_route = (broker, account_profile)
        return self.account


class _WorkerStore:
    def __init__(self, heartbeat: WorkerHeartbeat | None) -> None:
        self.heartbeat = heartbeat
        self.heartbeat_stale = False
        self.reconcile_stale = False

    def latest_heartbeat(self):
        return self.heartbeat

    def is_heartbeat_stale(self, worker_id: str, *, stale_after_seconds: int):
        assert worker_id == self.heartbeat.worker_id
        assert stale_after_seconds == 10
        return self.heartbeat_stale

    def is_reconcile_stale(self, worker_id: str, *, stale_after_seconds: int):
        assert worker_id == self.heartbeat.worker_id
        assert stale_after_seconds == 30
        return self.reconcile_stale


def _settings() -> Settings:
    settings = Settings()
    settings.general.last_data_source = "okx"
    settings.general.last_symbol = "XAU-USDT-SWAP"
    settings.general.last_timeframe = "15m"
    settings.execution.selected_broker = "okx"
    return settings


def _source(*, connected: bool, symbol: str, timeframe: str):
    return SimpleNamespace(
        _connected=connected,
        _symbol=symbol,
        _timeframe=timeframe,
    )


def _model(*, source, execution_store, worker_store):
    return WorkbenchReadModel(
        settings=_settings(),
        data_source=source,
        execution_store=execution_store,
        worker_store=worker_store,
        clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )


def test_capture_separates_confirmed_subscription_from_plan_and_local_lifecycle():
    now = datetime(2026, 7, 23, 11, 59, tzinfo=UTC)
    heartbeat = WorkerHeartbeat(
        worker_id="worker-1",
        pid=123,
        started_at=now - timedelta(minutes=1),
        last_seen_at=now,
        last_successful_reconcile_at=now,
        state=WorkerState.RUNNING,
    )
    latest = SimpleNamespace(id="exec-1", state=ExecutionState.OPEN)
    execution_store = _ExecutionStore(
        active=[latest],
        recent=[latest],
        account=AccountSnapshot(broker="okx", account_profile="okx"),
    )
    worker_store = _WorkerStore(heartbeat)

    snapshot = _model(
        source=_source(
            connected=True,
            symbol="XAU-USDT-SWAP",
            timeframe="15m",
        ),
        execution_store=execution_store,
        worker_store=worker_store,
    ).capture()

    assert snapshot.source_kind.certainty is FactCertainty.PLAN
    assert snapshot.connection == snapshot.connection.__class__(
        value="已连接",
        certainty=FactCertainty.CONFIRMED,
        source="data source connection marker",
        observed_at="2026-07-23T12:00:00+00:00",
    )
    assert snapshot.symbol.certainty is FactCertainty.CONFIRMED
    assert snapshot.timeframe.certainty is FactCertainty.CONFIRMED
    assert snapshot.worker_state.certainty is FactCertainty.CONFIRMED
    assert snapshot.heartbeat.value == "新鲜"
    assert snapshot.reconcile.value == "新鲜"
    assert snapshot.account.certainty is FactCertainty.CONFIRMED
    assert snapshot.active_execution_count.value == "1"
    assert snapshot.latest_execution_state.value == "open"
    assert snapshot.latest_execution_state.certainty is FactCertainty.PLAN
    assert execution_store.account_route == ("okx", "okx")


def test_capture_keeps_missing_worker_and_account_as_unknown():
    execution_store = _ExecutionStore()
    worker_store = _WorkerStore(None)

    snapshot = _model(
        source=_source(connected=False, symbol="", timeframe=""),
        execution_store=execution_store,
        worker_store=worker_store,
    ).capture()

    assert snapshot.connection.value == "未连接"
    assert snapshot.connection.certainty is FactCertainty.CONFIRMED
    assert snapshot.symbol.certainty is FactCertainty.PLAN
    assert snapshot.timeframe.certainty is FactCertainty.PLAN
    assert snapshot.worker_state.certainty is FactCertainty.UNKNOWN
    assert snapshot.heartbeat.certainty is FactCertainty.UNKNOWN
    assert snapshot.reconcile.certainty is FactCertainty.UNKNOWN
    assert snapshot.account.certainty is FactCertainty.UNKNOWN
    assert snapshot.latest_execution_state.value == "无"
    assert snapshot.active_executions == ()


def test_capture_rejects_naive_clock():
    model = WorkbenchReadModel(
        settings=_settings(),
        data_source=_source(connected=False, symbol="", timeframe=""),
        execution_store=_ExecutionStore(),
        worker_store=_WorkerStore(None),
        clock=lambda: datetime(2026, 7, 23, 12, 0),
    )

    with pytest.raises(RuntimeError, match="带时区"):
        model.capture()
