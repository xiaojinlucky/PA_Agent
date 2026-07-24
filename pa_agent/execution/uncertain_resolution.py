"""Read-only reconciliation for uncertain broker writes before risk can resume."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from pa_agent.execution.credentials import account_identity_fingerprint
from pa_agent.execution.models import ExecutionState
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker_protocol import (
    WorkerCommandAction,
    WorkerCommandResolution,
    WorkerCommandResolutionEvidence,
    WorkerCommandStatus,
)
from pa_agent.execution.worker_store import WorkerStore

_SCHEMA_FAILURE_CODES = frozenset(
    {
        "ValidationError",
        "execution_record_invalid",
    }
)
_EXPECTED_PRE_BROKER_EVENTS = ("plan_created", "ready_expired")
_PENDING_ALGO_ORDER_TYPES = (
    "oco",
    "conditional",
    "trigger",
    "move_order_stop",
)


class _OkxReadOnlyClient(Protocol):
    def account_config(self) -> dict: ...

    def positions(self, *, instrument: str | None = None) -> list[dict]: ...

    def pending_orders(self, *, instrument: str) -> list[dict]: ...

    def pending_algo_orders(
        self,
        *,
        instrument: str,
        order_type: str = "oco",
    ) -> list[dict]: ...


def _account_identity(account_config: dict, *, environment: str) -> str:
    uid = str(account_config.get("uid") or "").strip()
    main_uid = str(account_config.get("mainUid") or "").strip()
    raw_account_type = account_config.get("type")
    account_type = (
        "" if raw_account_type is None else str(raw_account_type).strip()
    )
    return account_identity_fingerprint(
        "okx",
        environment,
        uid,
        main_uid,
        account_type,
    )


def _nonzero_position_count(rows: list[dict]) -> int:
    count = 0
    for row in rows:
        try:
            quantity = Decimal(str(row.get("pos") or "0"))
        except InvalidOperation as exc:
            raise RuntimeError("OKX 持仓数量格式无效, 禁止处置 uncertain 命令") from exc
        if quantity != 0:
            count += 1
    return count


def resolve_okx_demo_prebroker_schema_failure(
    *,
    command_id: str,
    worker_store: WorkerStore,
    execution_store: ExecutionStore,
    client: _OkxReadOnlyClient,
    resolved_by: str,
    observed_at: datetime | None = None,
) -> WorkerCommandResolution:
    """Resolve one known pre-broker schema failure using only broker reads."""
    existing = worker_store.get_command_resolution(command_id)
    if existing is not None:
        return existing

    command = worker_store.get_command(command_id)
    if command is None:
        raise KeyError(f"未知 worker command: {command_id}")
    if (
        command.action is not WorkerCommandAction.SUBMIT
        or command.status is not WorkerCommandStatus.UNCERTAIN
        or command.broker != "okx"
        or command.environment != "demo"
        or command.failure_code not in _SCHEMA_FAILURE_CODES
    ):
        raise RuntimeError("命令不是可处置的 OKX Demo 券商写入前 schema 失败")

    record = execution_store.get(command.execution_id)
    if record is None:
        raise RuntimeError("uncertain 命令引用的 execution 不存在")
    if (
        record.state is not ExecutionState.CANCELED
        or record.preflight is not None
        or record.client_order_id
        or record.broker_order_id
        or record.filled_quantity != 0
    ):
        raise RuntimeError("execution 不是券商写入前已作废状态")

    event_kinds = tuple(
        event.kind for event in execution_store.events(record.id)
    )
    if event_kinds != _EXPECTED_PRE_BROKER_EVENTS:
        raise RuntimeError("execution 事件不能证明券商写入前失败")

    active_executions = execution_store.list_active()
    if active_executions:
        raise RuntimeError("仍有活动 execution, 禁止处置 uncertain 命令")
    if worker_store.current_new_risk_lease() is not None:
        raise RuntimeError("仍有活动 NEW_RISK 租约, 禁止处置 uncertain 命令")

    account_identity = _account_identity(
        client.account_config(),
        environment=command.environment,
    )
    position_count = _nonzero_position_count(
        client.positions(instrument=record.plan.instrument)
    )
    pending_orders = client.pending_orders(
        instrument=record.plan.instrument
    )
    pending_algo_orders = [
        row
        for order_type in _PENDING_ALGO_ORDER_TYPES
        for row in client.pending_algo_orders(
            instrument=record.plan.instrument,
            order_type=order_type,
        )
    ]
    if position_count or pending_orders or pending_algo_orders:
        raise RuntimeError("OKX Demo 仍有仓位或挂单, 禁止处置 uncertain 命令")

    evidence = WorkerCommandResolutionEvidence(
        execution_id=command.execution_id,
        command_action=command.action.value,
        command_failure_code=command.failure_code,
        broker=command.broker,
        environment=command.environment,
        account=command.account,
        instrument=record.plan.instrument,
        execution_state=record.state.value,
        broker_order_id_present=bool(record.broker_order_id),
        client_order_id_present=bool(record.client_order_id),
        filled_quantity=record.filled_quantity,
        event_kinds=event_kinds,
        active_execution_count=0,
        new_risk_lease_present=False,
        broker_position_count=position_count,
        broker_pending_order_count=len(pending_orders),
        broker_pending_algo_order_count=len(pending_algo_orders),
        broker_account_identity_digest=account_identity,
        observed_at=observed_at or datetime.now(UTC),
    )
    return worker_store.resolve_uncertain_command(
        command.id,
        resolution_code="confirmed_not_written_schema_validation",
        evidence=evidence,
        resolved_by=resolved_by,
    )
