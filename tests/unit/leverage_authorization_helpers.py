from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pa_agent.agents.supervisor_models import (
    SupervisorDecisionRecord,
    SupervisorInputSnapshot,
    snapshot_digest,
)
from pa_agent.execution.worker_protocol import (
    SetLeverageParameters,
    leverage_intent_snapshot,
)
from pa_agent.records.schema import AnalysisRecord


def authorized_leverage_parameters(
    *,
    analysis_path: Path,
    record: AnalysisRecord,
    config_fingerprint: str,
    expected_account_identity: str,
    current_capacity: Decimal = Decimal("10"),
    target_capacity: Decimal = Decimal("30"),
    required_quantity: Decimal = Decimal("20"),
    effective_entry_price: Decimal | None = None,
    risk_capital_cap_usdt: Decimal = Decimal("2"),
    risk_percent: Decimal = Decimal("0.10"),
    sizing_mode: str = "risk_budget",
) -> tuple[SetLeverageParameters, AnalysisRecord]:
    decision = record.stage2_decision["decision"]
    direction = (
        "long"
        if decision["order_direction"] == "做多"
        else "short"
    )
    draft = SetLeverageParameters(
        analysis_digest="a" * 64,
        analysis_record_path=str(analysis_path.resolve()),
        config_fingerprint=config_fingerprint,
        instrument="XAU-USDT-SWAP",
        direction=direction,
        margin_mode="cross",
        position_mode="net_mode",
        current_leverage=Decimal("5"),
        target_leverage=Decimal("10"),
        current_capacity=current_capacity,
        target_capacity=target_capacity,
        maximum_leverage=Decimal("10"),
        maximum_capacity=target_capacity,
        planning_method="bounded_sequential_policy_grid_v1",
        policy_grid_step=Decimal("5"),
        verified_grid=(
            {"leverage": "5", "capacity": str(current_capacity)},
            {"leverage": "10", "capacity": str(target_capacity)},
        ),
        required_quantity=required_quantity,
        entry_price=(
            effective_entry_price
            if effective_entry_price is not None
            else Decimal(str(decision["entry_price"]))
        ),
        expected_account_identity=expected_account_identity,
        okx_api_base_url="https://www.okx.com",
    )
    response = (
        dict(record.stage2_response)
        if isinstance(record.stage2_response, dict)
        else {}
    )
    response["risk_sizing"] = {
        "sizing_mode": sizing_mode,
        "equity_basis": "fixed_cap_or_usdt_equity_whichever_lower",
        "account_total_equity_usd": str(risk_capital_cap_usdt),
        "equity_usdt": str(risk_capital_cap_usdt),
        "risk_capital_cap_usdt": str(risk_capital_cap_usdt),
        "effective_risk_capital_usdt": str(risk_capital_cap_usdt),
        "risk_percent": str(risk_percent),
        "risk_budget_usdt": str(risk_capital_cap_usdt * risk_percent),
        "reference_price_usdt": str(draft.entry_price),
        "target_quantity": str(required_quantity),
    }
    response["leverage_intent"] = leverage_intent_snapshot(draft)
    record = record.model_copy(update={"stage2_response": response})
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        record.model_dump_json(indent=2),
        encoding="utf-8",
    )
    analysis_digest = hashlib.sha256(analysis_path.read_bytes()).hexdigest()
    parameters = SetLeverageParameters.model_validate(
        {
            **draft.model_dump(mode="python"),
            "analysis_digest": analysis_digest,
        }
    )

    snapshot = SupervisorInputSnapshot(
        campaign_id="campaign-test",
        analysis_digest=analysis_digest,
        symbol=record.meta.symbol,
        timeframe=record.meta.timeframe,
        closed_bar_ts_open_ms=1_784_300_400_000,
        closed_bar={
            "ts_open": 1_784_300_400_000,
            "closed": True,
            "close": decision["entry_price"],
        },
        stage1_diagnosis=dict(record.stage1_diagnosis or {}),
        stage2_decision=dict(record.stage2_decision or {}),
        leverage_intent=leverage_intent_snapshot(parameters),
        active_execution_count=0,
        account_equity_usdt="5000",
        max_buy=str(target_capacity),
        max_sell=str(target_capacity),
        technical_plan_quantity=str(required_quantity),
    )
    supervisor = SupervisorDecisionRecord(
        record_id=(
            f"{snapshot.campaign_id}:"
            f"{snapshot.closed_bar_ts_open_ms}:"
            f"{analysis_digest}"
        ),
        campaign_id=snapshot.campaign_id,
        analysis_digest=analysis_digest,
        closed_bar_ts_open_ms=snapshot.closed_bar_ts_open_ms,
        input_snapshot_digest=snapshot_digest(snapshot),
        input_snapshot=snapshot,
        action="allow_entry",
        reason="测试监督放行",
        profile_id="test-profile",
        model_id="test-model",
        fallback_level="primary",
        created_at=datetime.now(UTC).isoformat(),
    )
    supervisor_path = analysis_path.with_name("supervisor.json")
    supervisor_path.write_text(
        supervisor.model_dump_json(indent=2),
        encoding="utf-8",
    )
    final = SetLeverageParameters.model_validate(
        {
            **parameters.model_dump(mode="python"),
            "supervisor_record_id": supervisor.record_id,
            "supervisor_record_path": str(supervisor_path.resolve()),
            "supervisor_record_digest": hashlib.sha256(
                supervisor_path.read_bytes()
            ).hexdigest(),
        }
    )
    return final, record
