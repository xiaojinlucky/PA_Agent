"""Validate that a leverage command is exactly the action supervision allowed."""
from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pa_agent.agents.supervisor_models import (
    SupervisorDecisionRecord,
    snapshot_digest,
)
from pa_agent.execution.worker_protocol import (
    SetLeverageParameters,
    leverage_intent_snapshot,
)
from pa_agent.records.schema import AnalysisRecord


class LeverageAuthorizationError(ValueError):
    """Durable analysis or supervisor evidence does not authorize the command."""


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LeverageAuthorizationError(
            f"监督快照 {field} 不是有效数字"
        ) from exc
    if not parsed.is_finite():
        raise LeverageAuthorizationError(
            f"监督快照 {field} 不是有限数字"
        )
    return parsed


def validate_leverage_authorization(
    parameters: SetLeverageParameters,
) -> None:
    """Verify immutable analysis bytes and the durable allow-entry decision."""
    analysis_path = Path(parameters.analysis_record_path)
    try:
        analysis_bytes = analysis_path.read_bytes()
        record = AnalysisRecord.model_validate_json(analysis_bytes)
    except (OSError, ValueError, TypeError) as exc:
        raise LeverageAuthorizationError(
            "杠杆命令引用的耐久分析记录无法读取"
        ) from exc
    if hashlib.sha256(analysis_bytes).hexdigest() != parameters.analysis_digest:
        raise LeverageAuthorizationError("杠杆命令与耐久分析摘要不一致")
    expected_intent = leverage_intent_snapshot(parameters)
    stage2_response = (
        record.stage2_response
        if isinstance(record.stage2_response, dict)
        else {}
    )
    if stage2_response.get("leverage_intent") != expected_intent:
        raise LeverageAuthorizationError("耐久分析没有授权这组杠杆参数")

    decision = (
        record.stage2_decision.get("decision")
        if isinstance(record.stage2_decision, dict)
        else None
    )
    if not isinstance(decision, dict):
        raise LeverageAuthorizationError("耐久分析缺少可执行三价")
    direction = {
        "做多": "long",
        "做空": "short",
    }.get(str(decision.get("order_direction") or "").strip())
    if direction != parameters.direction:
        raise LeverageAuthorizationError("杠杆方向与 PA 决策不一致")
    signal_entry_price = _decimal(decision.get("entry_price"), "entry_price")
    if signal_entry_price <= 0:
        raise LeverageAuthorizationError("PA 决策入场价必须为正数")
    risk_sizing = (
        stage2_response.get("risk_sizing")
        if isinstance(stage2_response.get("risk_sizing"), dict)
        else None
    )
    if risk_sizing is None:
        raise LeverageAuthorizationError("耐久分析缺少最终有效限价定仓快照")
    if (
        _decimal(
            risk_sizing.get("reference_price_usdt"),
            "risk_sizing.reference_price_usdt",
        )
        != parameters.entry_price
    ):
        raise LeverageAuthorizationError("杠杆容量参考价与最终有效限价不一致")

    supervisor_path = Path(parameters.supervisor_record_path)
    try:
        supervisor_bytes = supervisor_path.read_bytes()
        supervisor = SupervisorDecisionRecord.model_validate_json(
            supervisor_bytes
        )
    except (OSError, ValueError, TypeError) as exc:
        raise LeverageAuthorizationError(
            "杠杆命令引用的监督记录无法读取"
        ) from exc
    if (
        hashlib.sha256(supervisor_bytes).hexdigest()
        != parameters.supervisor_record_digest
    ):
        raise LeverageAuthorizationError("监督记录摘要不一致")
    if (
        supervisor.record_id != parameters.supervisor_record_id
        or supervisor.action != "allow_entry"
        or supervisor.analysis_digest != parameters.analysis_digest
        or supervisor.input_snapshot.analysis_digest
        != parameters.analysis_digest
        or supervisor.input_snapshot.model_dump(mode="json").get(
            "leverage_intent"
        )
        != expected_intent
        or supervisor.input_snapshot_digest
        != snapshot_digest(supervisor.input_snapshot)
    ):
        raise LeverageAuthorizationError("监督记录没有授权这组杠杆参数")
    if (
        _decimal(
            supervisor.input_snapshot.technical_plan_quantity,
            "technical_plan_quantity",
        )
        != parameters.required_quantity
    ):
        raise LeverageAuthorizationError("监督数量与杠杆风险数量不一致")
    capacity = _decimal(
        (
            supervisor.input_snapshot.max_buy
            if parameters.direction == "long"
            else supervisor.input_snapshot.max_sell
        ),
        "direction_capacity",
    )
    if capacity != parameters.target_capacity:
        raise LeverageAuthorizationError("监督容量与杠杆目标容量不一致")
