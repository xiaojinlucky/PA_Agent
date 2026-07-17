from __future__ import annotations

from decimal import Decimal

import pytest

from pa_agent.execution.models import (
    ExecutionPlan,
    ExecutionState,
    utc_now_iso,
)
from pa_agent.execution.store import ExecutionStore


def _plan(identifier: str = "one") -> ExecutionPlan:
    return ExecutionPlan(
        id=f"plan-{identifier}",
        analysis_digest=f"digest-{identifier}",
        analysis_record_path="records/pending/test.json",
        broker="okx",
        environment="demo",
        product="swap",
        requested_account="okx",
        source_symbol="XAUUSD",
        instrument="XAU-USDT-SWAP",
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


def test_duplicate_analysis_returns_existing_execution(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")

    first, created_first = store.create(_plan())
    second, created_second = store.create(_plan())

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert len(store.list_recent()) == 1


def test_transition_and_event_are_atomic_and_revision_checked(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    record, _ = store.create(_plan())
    submitting = record.model_copy(update={"state": ExecutionState.SUBMITTING})

    saved = store.save(submitting, event_kind="submit_started")

    assert saved.revision == 1
    assert store.get(saved.id).state == ExecutionState.SUBMITTING
    assert [event.kind for event in store.events(saved.id)] == [
        "plan_created",
        "submit_started",
    ]
    with pytest.raises(RuntimeError, match="revision conflict"):
        store.save(submitting, event_kind="stale_write")


def test_list_active_excludes_ready_and_terminal_states(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    ready, _ = store.create(_plan("ready"))
    active, _ = store.create(_plan("active"))
    active = store.save(
        active.model_copy(update={"state": ExecutionState.OPEN}),
        event_kind="opened",
    )
    closed, _ = store.create(_plan("closed"))
    store.save(
        closed.model_copy(update={"state": ExecutionState.CLOSED}),
        event_kind="closed",
    )

    assert [item.id for item in store.list_active()] == [active.id]
    assert ready.id not in {item.id for item in store.list_active()}
