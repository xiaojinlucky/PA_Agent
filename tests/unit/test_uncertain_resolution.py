from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pa_agent.execution.models import (
    ExecutionPlan,
    ExecutionState,
    utc_now_iso,
)
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.uncertain_resolution import (
    resolve_okx_demo_prebroker_schema_failure,
)
from pa_agent.execution.worker_protocol import (
    WorkerCommandAction,
    WorkerCommandStatus,
)
from pa_agent.execution.worker_store import WorkerStore


class _ReadOnlyOkxClient:
    def __init__(self) -> None:
        self.position_rows: list[dict] = []
        self.order_rows: list[dict] = []
        self.algo_rows: dict[str, list[dict]] = {}

    def account_config(self) -> dict:
        return {
            "uid": "1001",
            "mainUid": "1001",
            "type": "0",
            "posMode": "net_mode",
        }

    def positions(self, *, instrument=None) -> list[dict]:
        del instrument
        return list(self.position_rows)

    def pending_orders(self, *, instrument: str) -> list[dict]:
        del instrument
        return list(self.order_rows)

    def pending_algo_orders(
        self,
        *,
        instrument: str,
        order_type: str = "oco",
    ) -> list[dict]:
        del instrument
        return list(self.algo_rows.get(order_type, []))


def _plan(identifier: str = "schema-failure") -> ExecutionPlan:
    return ExecutionPlan(
        id=f"execution-{identifier}",
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
        entry_price=Decimal("4000"),
        take_profit_1=Decimal("4010"),
        take_profit_2=Decimal("4020"),
        stop_loss=Decimal("3990"),
        trade_confidence=20,
        created_at=utc_now_iso(),
        config_fingerprint="config",
    )


def _stores_with_uncertain_submit(tmp_path, *, add_submit_intent=False):
    execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
    record, _ = execution_store.create(_plan())
    if add_submit_intent:
        record = execution_store.save(
            record,
            event_kind="submit_intent",
        )
    record = execution_store.save(
        record.model_copy(
            update={
                "state": ExecutionState.CANCELED,
                "state_reason": "execution_record_invalid",
            }
        ),
        event_kind="ready_expired",
    )

    worker_store = WorkerStore(tmp_path / "worker.sqlite3")
    lease = worker_store.grant_new_risk_lease(
        worker_id="worker-one",
        config_fingerprint="config",
        requester="campaign",
        broker="okx",
        environment="demo",
        account="okx",
        ttl_seconds=60,
    )
    assert lease is not None
    command, _ = worker_store.enqueue(
        action=WorkerCommandAction.SUBMIT,
        execution_id=record.id,
        requester="campaign",
        broker="okx",
        environment="demo",
        account="okx",
        new_risk_lease_id=lease.lease_id,
    )
    assert worker_store.claim_next(worker_id="worker-one").id == command.id
    worker_store.recover_inflight(failure_code="ValidationError")
    assert worker_store.revoke_new_risk_lease(lease.lease_id) is True
    return execution_store, worker_store, command


def test_read_only_resolution_keeps_uncertain_history_and_unblocks_route(
    tmp_path,
):
    execution_store, worker_store, command = (
        _stores_with_uncertain_submit(tmp_path)
    )
    client = _ReadOnlyOkxClient()

    resolution = resolve_okx_demo_prebroker_schema_failure(
        command_id=command.id,
        worker_store=worker_store,
        execution_store=execution_store,
        client=client,
        resolved_by="operator-audit",
        observed_at=datetime(2026, 7, 24, 4, 30, tzinfo=UTC),
    )

    assert worker_store.get_command(command.id).status is WorkerCommandStatus.UNCERTAIN
    assert resolution.resolution_code == (
        "confirmed_not_written_schema_validation"
    )
    assert resolution.evidence.event_kinds == (
        "plan_created",
        "ready_expired",
    )
    assert resolution.evidence.broker_position_count == 0
    assert resolution.evidence.broker_pending_order_count == 0
    assert resolution.evidence.broker_pending_algo_order_count == 0
    assert resolution.evidence_digest
    assert worker_store.list_unresolved_write_commands(
        broker="okx",
        environment="demo",
        account="okx",
    ) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("position_rows", [{"pos": "1"}]),
        ("order_rows", [{"ordId": "ordinary-order"}]),
        ("algo_rows", {"trigger": [{"algoId": "algo-order"}]}),
    ],
)
def test_resolution_refuses_any_broker_exposure_or_pending_order(
    tmp_path,
    field,
    value,
):
    execution_store, worker_store, command = (
        _stores_with_uncertain_submit(tmp_path)
    )
    client = _ReadOnlyOkxClient()
    setattr(client, field, value)

    with pytest.raises(RuntimeError, match="仓位或挂单"):
        resolve_okx_demo_prebroker_schema_failure(
            command_id=command.id,
            worker_store=worker_store,
            execution_store=execution_store,
            client=client,
            resolved_by="operator-audit",
        )

    assert worker_store.get_command_resolution(command.id) is None


def test_resolution_refuses_event_history_that_reached_submit_intent(
    tmp_path,
):
    execution_store, worker_store, command = _stores_with_uncertain_submit(
        tmp_path,
        add_submit_intent=True,
    )

    with pytest.raises(RuntimeError, match="不能证明"):
        resolve_okx_demo_prebroker_schema_failure(
            command_id=command.id,
            worker_store=worker_store,
            execution_store=execution_store,
            client=_ReadOnlyOkxClient(),
            resolved_by="operator-audit",
        )

    assert worker_store.get_command_resolution(command.id) is None
