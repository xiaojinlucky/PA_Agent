from __future__ import annotations

import multiprocessing
import sqlite3
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


def _claim_route_in_process(
    path: str,
    execution_id: str,
    start_event,
    ready_queue,
    result_queue,
) -> None:
    try:
        store = ExecutionStore(path)
        record = store.get(execution_id)
        if record is None:
            raise RuntimeError("missing execution")
        ready_queue.put(execution_id)
        if not start_event.wait(10):
            raise TimeoutError("claim start timeout")
        conflict = store.acquire_route_claim(
            record,
            account_identity="same-okx-account",
        )
        result_queue.put(
            (
                execution_id,
                conflict.id if conflict is not None else None,
                "",
            )
        )
    except BaseException as exc:
        result_queue.put((execution_id, None, repr(exc)))


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


def test_route_claim_is_atomic_across_independent_processes(tmp_path):
    path = tmp_path / "execution.sqlite3"
    seed = ExecutionStore(path)
    first, _ = seed.create(_plan("claim-one"))
    second, _ = seed.create(_plan("claim-two"))
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    ready_queue = context.Queue()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_claim_route_in_process,
            args=(
                str(path),
                execution_id,
                start_event,
                ready_queue,
                result_queue,
            ),
        )
        for execution_id in (first.id, second.id)
    ]
    for process in processes:
        process.start()
    try:
        assert {ready_queue.get(timeout=10) for _ in range(2)} == {
            first.id,
            second.id,
        }
        start_event.set()
        results = [result_queue.get(timeout=10) for _ in range(2)]
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert all(process.exitcode == 0 for process in processes)
    assert not [error for _, _, error in results if error]
    compact_results = [
        (execution_id, conflict)
        for execution_id, conflict, _error in results
    ]
    winners = [
        execution_id
        for execution_id, conflict in compact_results
        if conflict is None
    ]
    losers = [
        (execution_id, conflict)
        for execution_id, conflict in compact_results
        if conflict is not None
    ]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0][1] == winners[0]
    owner_record = seed.get(winners[0])
    assert owner_record is not None
    owner_record = owner_record.model_copy(
        update={"account_identity": "same-okx-account"}
    )
    assert seed.route_claim_owner(owner_record) == winners[0]


def test_schema_v1_is_migrated_but_unknown_future_version_is_not_overwritten(
    tmp_path,
):
    legacy_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            "CREATE TABLE execution_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO execution_meta(key, value) VALUES ('schema_version', '1')"
        )

    ExecutionStore(legacy_path)

    with sqlite3.connect(legacy_path) as connection:
        version = connection.execute(
            "SELECT value FROM execution_meta WHERE key='schema_version'"
        ).fetchone()
        route_table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='execution_route_claims'"
        ).fetchone()
    assert version == ("2",)
    assert route_table == ("execution_route_claims",)

    future_path = tmp_path / "future.sqlite3"
    with sqlite3.connect(future_path) as connection:
        connection.execute(
            "CREATE TABLE execution_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO execution_meta(key, value) VALUES ('schema_version', '3')"
        )
    with pytest.raises(RuntimeError, match="不支持"):
        ExecutionStore(future_path)
    with sqlite3.connect(future_path) as connection:
        future_version = connection.execute(
            "SELECT value FROM execution_meta WHERE key='schema_version'"
        ).fetchone()
    assert future_version == ("3",)
