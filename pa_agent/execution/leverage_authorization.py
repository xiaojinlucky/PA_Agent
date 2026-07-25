"""Validate that a leverage command exactly matches durable script evidence."""
from __future__ import annotations

import hashlib
from decimal import ROUND_DOWN, Decimal, InvalidOperation
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
    """Durable analysis or optional legacy evidence does not authorize the command."""


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


def _positive_decimal(value: object, field: str) -> Decimal:
    parsed = _decimal(value, field)
    if parsed <= 0:
        raise LeverageAuthorizationError(f"脚本快照 {field} 必须为正数")
    return parsed


def _has_supervisor_evidence(parameters: SetLeverageParameters) -> bool:
    fields = (
        parameters.supervisor_record_id,
        parameters.supervisor_record_path,
        parameters.supervisor_record_digest,
    )
    if any(fields) and not all(fields):
        raise LeverageAuthorizationError("旧监督证据字段必须同时存在或同时为空")
    return all(fields)


def _validate_script_risk_snapshot(
    parameters: SetLeverageParameters,
    record: AnalysisRecord,
    risk_sizing: dict,
) -> None:
    if parameters.planning_method != "bounded_sequential_policy_grid_v2":
        raise LeverageAuthorizationError("新脚本杠杆授权必须使用策略网格 v2")
    if (
        parameters.exchange_maximum_leverage is None
        or parameters.user_maximum_leverage is None
    ):
        raise LeverageAuthorizationError("新脚本缺少交易所和用户最大杠杆证据")
    if parameters.maximum_leverage != min(
        parameters.exchange_maximum_leverage,
        parameters.user_maximum_leverage,
    ):
        raise LeverageAuthorizationError("新脚本有效最大杠杆不是两项上限的较小值")
    if (
        str(risk_sizing.get("equity_basis") or "")
        != "fixed_cap_or_usdt_equity_whichever_lower"
    ):
        raise LeverageAuthorizationError("脚本风险快照使用了错误的权益口径")
    _positive_decimal(
        risk_sizing.get("account_total_equity_usd"),
        "risk_sizing.account_total_equity_usd",
    )

    capital_cap = _positive_decimal(
        risk_sizing.get("risk_capital_cap_usdt"),
        "risk_sizing.risk_capital_cap_usdt",
    )
    equity = _positive_decimal(
        risk_sizing.get("equity_usdt"),
        "risk_sizing.equity_usdt",
    )
    effective_capital = _positive_decimal(
        risk_sizing.get("effective_risk_capital_usdt"),
        "risk_sizing.effective_risk_capital_usdt",
    )
    risk_percent = _positive_decimal(
        risk_sizing.get("risk_percent"),
        "risk_sizing.risk_percent",
    )
    if risk_percent > 1:
        raise LeverageAuthorizationError("脚本风险比例不能大于 1")
    if effective_capital != min(equity, capital_cap):
        raise LeverageAuthorizationError("脚本有效风险资本与权益/资本上限不一致")
    risk_budget = _positive_decimal(
        risk_sizing.get("risk_budget_usdt"),
        "risk_sizing.risk_budget_usdt",
    )
    if risk_budget != effective_capital * risk_percent:
        raise LeverageAuthorizationError("脚本风险预算与固定资本规则不一致")

    target_quantity = _positive_decimal(
        risk_sizing.get("target_quantity"),
        "risk_sizing.target_quantity",
    )
    if target_quantity != parameters.required_quantity:
        raise LeverageAuthorizationError("脚本风险数量与杠杆所需数量不一致")
    quantity_step = _positive_decimal(
        risk_sizing.get("quantity_step"),
        "risk_sizing.quantity_step",
    )
    minimum_quantity = _positive_decimal(
        risk_sizing.get("minimum_quantity"),
        "risk_sizing.minimum_quantity",
    )
    worst_case_loss = _positive_decimal(
        risk_sizing.get("worst_case_loss_per_contract_usdt"),
        "risk_sizing.worst_case_loss_per_contract_usdt",
    )
    expected_quantity = (
        (risk_budget / worst_case_loss) / quantity_step
    ).to_integral_value(rounding=ROUND_DOWN) * quantity_step
    if expected_quantity < minimum_quantity or expected_quantity != target_quantity:
        raise LeverageAuthorizationError("脚本风险数量不是固定风险公式的唯一结果")
    if (
        _positive_decimal(
            risk_sizing.get("risk_used_usdt"),
            "risk_sizing.risk_used_usdt",
        )
        != target_quantity * worst_case_loss
    ):
        raise LeverageAuthorizationError("脚本实际风险占用与数量不一致")

    decision = record.stage2_decision["decision"]
    entry = parameters.entry_price
    stop = _positive_decimal(
        decision.get("stop_loss_price"),
        "stop_loss_price",
    )
    stop_distance = abs(entry - stop)
    if (
        _positive_decimal(
            risk_sizing.get("stop_distance_usdt"),
            "risk_sizing.stop_distance_usdt",
        )
        != stop_distance
    ):
        raise LeverageAuthorizationError("脚本止损距离与 PA 三价不一致")
    contract_notional = _positive_decimal(
        risk_sizing.get("contract_notional_usdt"),
        "risk_sizing.contract_notional_usdt",
    )
    contract_value = contract_notional / entry
    fee_rate = _decimal(
        risk_sizing.get("fee_rate"),
        "risk_sizing.fee_rate",
    )
    slippage_rate = _decimal(
        risk_sizing.get("slippage_rate"),
        "risk_sizing.slippage_rate",
    )
    if fee_rate < 0 or slippage_rate < 0:
        raise LeverageAuthorizationError("脚本费率和保守滑点率不能为负数")
    round_trip_notional = (entry + stop) * contract_value
    expected_fee = round_trip_notional * fee_rate
    expected_slippage = round_trip_notional * slippage_rate
    if (
        _decimal(
            risk_sizing.get("fee_per_contract_usdt"),
            "risk_sizing.fee_per_contract_usdt",
        )
        != expected_fee
        or _decimal(
            risk_sizing.get("slippage_per_contract_usdt"),
            "risk_sizing.slippage_per_contract_usdt",
        )
        != expected_slippage
        or worst_case_loss
        != stop_distance * contract_value + expected_fee + expected_slippage
    ):
        raise LeverageAuthorizationError("脚本单张最坏损失与三价/费用不一致")

    capacity = _positive_decimal(
        risk_sizing.get(
            "max_buy" if parameters.direction == "long" else "max_sell"
        ),
        "risk_sizing.direction_capacity",
    )
    if capacity != parameters.target_capacity:
        raise LeverageAuthorizationError("脚本容量与目标杠杆容量不一致")


def validate_current_leverage_policy(
    parameters: SetLeverageParameters,
    settings,
) -> None:
    """Bind a leverage command to the current saved OKX risk policy."""
    execution = getattr(settings, "execution", None)
    okx = getattr(execution, "okx", None)
    if (
        execution is None
        or okx is None
        or str(execution.selected_broker) != "okx"
        or not bool(okx.simulated)
    ):
        raise LeverageAuthorizationError("当前配置不是 OKX Demo")
    if (
        parameters.instrument != str(okx.instrument)
        or parameters.margin_mode != str(okx.margin_mode)
        or parameters.okx_api_base_url != str(okx.api_base_url)
    ):
        raise LeverageAuthorizationError("杠杆命令与 Worker 当前 OKX 路由不一致")

    current_cap = _positive_decimal(
        okx.risk_capital_cap_usdt,
        "settings.risk_capital_cap_usdt",
    )
    current_maximum_leverage = _positive_decimal(
        okx.maximum_leverage,
        "settings.maximum_leverage",
    )
    if (
        parameters.target_leverage > current_maximum_leverage
        or parameters.maximum_leverage > current_maximum_leverage
    ):
        raise LeverageAuthorizationError("杠杆命令超过当前用户最大杠杆")
    if (
        parameters.user_maximum_leverage is not None
        and parameters.user_maximum_leverage != current_maximum_leverage
    ):
        raise LeverageAuthorizationError("杠杆命令中的用户上限与当前设置不一致")

    analysis_path = Path(parameters.analysis_record_path)
    try:
        record = AnalysisRecord.model_validate_json(analysis_path.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise LeverageAuthorizationError("杠杆命令引用的风险快照无法读取") from exc
    response = record.stage2_response if isinstance(record.stage2_response, dict) else {}
    risk_sizing = response.get("risk_sizing")
    if not isinstance(risk_sizing, dict):
        raise LeverageAuthorizationError("耐久分析缺少风险定仓快照")
    if (
        _positive_decimal(
            risk_sizing.get("risk_capital_cap_usdt"),
            "risk_sizing.risk_capital_cap_usdt",
        )
        != current_cap
    ):
        raise LeverageAuthorizationError("脚本资金上限与当前设置不一致")
    sizing_mode = str(risk_sizing.get("sizing_mode") or "risk_budget")
    current_sizing_mode = str(getattr(okx, "sizing_mode", "risk_budget"))
    if sizing_mode != current_sizing_mode:
        raise LeverageAuthorizationError("脚本定仓模式与当前设置不一致")
    if sizing_mode == "fixed_quantity":
        current_quantity = _positive_decimal(okx.quantity, "settings.quantity")
        snapshot_quantity = _positive_decimal(
            risk_sizing.get("target_quantity"),
            "risk_sizing.target_quantity",
        )
        if snapshot_quantity != current_quantity:
            raise LeverageAuthorizationError("脚本固定张数与当前设置不一致")
    elif sizing_mode == "risk_budget":
        current_risk_percent = _positive_decimal(
            okx.risk_percent,
            "settings.risk_percent",
        )
        if current_risk_percent > 1:
            raise LeverageAuthorizationError("当前风险比例不能大于 1")
        if (
            _positive_decimal(
                risk_sizing.get("risk_percent"),
                "risk_sizing.risk_percent",
            )
            != current_risk_percent
        ):
            raise LeverageAuthorizationError("脚本风险比例与当前设置不一致")
    else:
        raise LeverageAuthorizationError("脚本定仓模式无效")


def validate_leverage_authorization(
    parameters: SetLeverageParameters,
) -> None:
    """Verify immutable analysis bytes and optional legacy supervisor evidence."""
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

    if not _has_supervisor_evidence(parameters):
        _validate_script_risk_snapshot(parameters, record, risk_sizing)
        return

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
