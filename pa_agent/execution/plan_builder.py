"""Build a deterministic execution plan from a durable AnalysisRecord."""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pa_agent.execution.errors import PlanBlocked
from pa_agent.execution.models import ExecutionPlan, utc_now_iso
from pa_agent.execution.order_modes import (
    effective_entry_type,
    normalise_entry_order_mode,
    normalise_exit_order_mode,
)
from pa_agent.records.schema import AnalysisRecord


def _positive_decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PlanBlocked("invalid_number", f"{field_name} 不是有效数字") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise PlanBlocked("invalid_number", f"{field_name} 必须是有限正数")
    return parsed


def _nonnegative_decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PlanBlocked("invalid_number", f"{field_name} 不是有效数字") from exc
    if not parsed.is_finite() or parsed < 0:
        raise PlanBlocked("invalid_number", f"{field_name} 必须是有限非负数")
    return parsed


def _verify_durable_record(record: AnalysisRecord, record_path: Path) -> str:
    from pa_agent.config.paths import RECORDS_PENDING_DIR

    try:
        resolved = record_path.resolve(strict=True)
        records_root = RECORDS_PENDING_DIR.resolve(strict=True)
    except OSError as exc:
        raise PlanBlocked("record_not_durable", "分析记录尚未可靠写盘") from exc
    if not resolved.is_relative_to(records_root) or not resolved.is_file():
        raise PlanBlocked("record_not_durable", "分析记录不在受信任的 records/pending 目录")
    try:
        raw_bytes = resolved.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8"))
        persisted = AnalysisRecord.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PlanBlocked("record_not_durable", "分析记录无法重新读取并通过模型校验") from exc
    if "_partial_reason" in raw or persisted.exception is not None:
        raise PlanBlocked("analysis_failed", "异常或部分分析记录不能进入实盘")
    if (
        persisted.meta.timestamp_local_ms != record.meta.timestamp_local_ms
        or persisted.meta.symbol != record.meta.symbol
        or persisted.meta.timeframe != record.meta.timeframe
        or persisted.stage2_decision != record.stage2_decision
    ):
        raise PlanBlocked("record_mismatch", "内存分析结果与落盘记录不一致")
    return hashlib.sha256(raw_bytes).hexdigest()


def _config_fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execution_route_fingerprint(settings, broker: str | None = None) -> str:
    """Fingerprint the exact current non-secret route used for one plan."""
    execution = getattr(settings, "execution", None)
    if execution is None:
        raise PlanBlocked("execution_disabled", "实盘执行配置尚未启用")
    selected = str(execution.selected_broker)
    target = broker or selected
    if target != selected:
        raise PlanBlocked("route_changed", "当前选择的券商与待执行计划不一致")
    if target == "longbridge":
        route = execution.longbridge
        requested_account = route.preferred_account
        is_paper = requested_account == "paper"
        payload = {
            "broker": target,
            "product": "securities",
            "requested_account": requested_account,
            "allow_fallback": bool(
                requested_account == "intraday"
                and route.allow_comprehensive_fallback
            ),
            "source_symbol": str(route.source_symbol or "").strip().upper(),
            "instrument": str(route.instrument or "").strip().upper(),
            "quantity": str(_positive_decimal(route.quantity, "quantity")),
            "environment": "demo" if is_paper else "live",
            "okx_margin_mode": "",
            "longbridge_outside_rth": bool(route.allow_outside_rth) and not is_paper,
            "min_trade_confidence": int(execution.min_trade_confidence),
            "entry_timeout_seconds": int(execution.entry_timeout_seconds),
            "entry_order_mode": str(getattr(execution, "entry_order_mode", "signal")),
            "exit_order_mode": str(getattr(execution, "exit_order_mode", "market")),
            "entry_slippage_atr_multiple": str(
                getattr(execution, "entry_slippage_atr_multiple", Decimal("0.50"))
            ),
            "exit_slippage_atr_multiple": str(
                getattr(execution, "exit_slippage_atr_multiple", Decimal("0.50"))
            ),
            "risk_capital_cap_usdt": "",
            "risk_percent": "",
            "sizing_mode": "",
            "maximum_leverage": "",
        }
    elif target == "okx":
        route = execution.okx
        payload = {
            "broker": target,
            "product": route.product,
            "requested_account": "okx",
            "allow_fallback": False,
            "source_symbol": str(route.source_symbol or "").strip().upper(),
            "instrument": str(route.instrument or "").strip().upper(),
            "quantity": str(_positive_decimal(route.quantity, "quantity")),
            "environment": "demo" if route.simulated else "live",
            "okx_margin_mode": route.margin_mode,
            "okx_api_base_url": route.api_base_url,
            "longbridge_outside_rth": False,
            "min_trade_confidence": int(execution.min_trade_confidence),
            "entry_timeout_seconds": int(execution.entry_timeout_seconds),
            "entry_order_mode": str(getattr(execution, "entry_order_mode", "signal")),
            "exit_order_mode": str(getattr(execution, "exit_order_mode", "market")),
            "entry_slippage_atr_multiple": str(
                getattr(execution, "entry_slippage_atr_multiple", Decimal("0.50"))
            ),
            "exit_slippage_atr_multiple": str(
                getattr(execution, "exit_slippage_atr_multiple", Decimal("0.50"))
            ),
            "risk_capital_cap_usdt": str(route.risk_capital_cap_usdt),
            "risk_percent": str(route.risk_percent),
            "sizing_mode": str(
                getattr(route, "sizing_mode", "risk_budget")
            ),
            "maximum_leverage": str(route.maximum_leverage),
        }
    else:
        raise PlanBlocked("unknown_broker", f"未知执行券商：{target}")
    return _config_fingerprint(payload)


def build_execution_plan(
    record: AnalysisRecord,
    settings,
    *,
    record_path: Path,
    is_demo_replay: bool = False,
) -> ExecutionPlan:
    """Return a safe plan or raise :class:`PlanBlocked` with a stable reason."""
    if is_demo_replay:
        raise PlanBlocked("demo_replay", "演示或历史回放不能触发实盘")
    execution = getattr(settings, "execution", None)
    if execution is None or not bool(execution.enabled):
        raise PlanBlocked("execution_disabled", "实盘执行配置尚未启用")
    if record.exception is not None:
        raise PlanBlocked("analysis_failed", "失败的分析不能触发实盘")

    stage2 = record.stage2_decision
    if not isinstance(stage2, dict) or stage2.get("gate_shortcircuited"):
        raise PlanBlocked("analysis_not_tradable", "阶段二未形成可交易决策")
    decision = stage2.get("decision")
    if not isinstance(decision, dict):
        raise PlanBlocked("analysis_not_tradable", "阶段二缺少交易决策")

    order_type_raw = str(decision.get("order_type") or "").strip()
    if order_type_raw == "不下单":
        raise PlanBlocked("no_order", "PA 决策为不下单")
    entry_type_map = {"限价单": "limit", "市价单": "market", "突破单": "breakout"}
    signal_entry_type = entry_type_map.get(order_type_raw)
    if signal_entry_type is None:
        raise PlanBlocked("unsupported_order_type", f"不支持的 PA 入场类型：{order_type_raw}")
    try:
        entry_order_mode = normalise_entry_order_mode(
            getattr(execution, "entry_order_mode", "signal")
        )
        exit_order_mode = normalise_exit_order_mode(
            getattr(execution, "exit_order_mode", "market")
        )
    except ValueError as exc:
        raise PlanBlocked("invalid_execution_mode", str(exc)) from exc
    entry_type = effective_entry_type(signal_entry_type, entry_order_mode)
    entry_slippage_atr_multiple = _nonnegative_decimal(
        getattr(execution, "entry_slippage_atr_multiple", Decimal("0.50")),
        "入场 ATR 滑点倍数",
    )
    exit_slippage_atr_multiple = _nonnegative_decimal(
        getattr(execution, "exit_slippage_atr_multiple", Decimal("0.50")),
        "离场 ATR 滑点倍数",
    )
    if entry_slippage_atr_multiple > 5 or exit_slippage_atr_multiple > 5:
        raise PlanBlocked("invalid_slippage", "ATR 滑点倍数必须在 0 到 5 之间")

    entry_atr: Decimal | None = None
    if record.analysis_atr14 is not None:
        entry_atr = _positive_decimal(record.analysis_atr14, "analysis_atr14")
    if (
        entry_order_mode == "limit_with_slippage"
        or exit_order_mode == "limit_with_slippage"
    ) and entry_atr is None:
        raise PlanBlocked(
            "missing_atr_for_slippage",
            "选择 ATR 滑点时，分析记录必须包含最新已收盘主周期 ATR14",
        )

    direction_raw = str(decision.get("order_direction") or "").strip()
    direction_map = {"做多": "long", "做空": "short"}
    direction = direction_map.get(direction_raw)
    if direction is None:
        raise PlanBlocked("invalid_direction", "PA 决策缺少有效的做多/做空方向")

    confidence_raw = decision.get("trade_confidence")
    if isinstance(confidence_raw, bool):
        raise PlanBlocked("invalid_confidence", "交易置信度无效")
    try:
        confidence = int(confidence_raw)
    except (TypeError, ValueError) as exc:
        raise PlanBlocked("invalid_confidence", "交易置信度无效") from exc
    if not math.isfinite(float(confidence)) or not 0 <= confidence <= 100:
        raise PlanBlocked("invalid_confidence", "交易置信度必须在 0 到 100 之间")
    if confidence < int(execution.min_trade_confidence):
        raise PlanBlocked(
            "confidence_below_execution_gate",
            f"交易置信度 {confidence} 低于实盘门槛 {execution.min_trade_confidence}",
        )

    entry = _positive_decimal(decision.get("entry_price"), "entry_price")
    tp1 = _positive_decimal(decision.get("take_profit_price"), "take_profit_price")
    tp2 = _positive_decimal(decision.get("take_profit_price_2"), "take_profit_price_2")
    stop = _positive_decimal(decision.get("stop_loss_price"), "stop_loss_price")
    if direction == "long" and not (stop < entry < tp1 <= tp2):
        raise PlanBlocked("invalid_price_order", "做多价格必须满足 止损 < 入场 < 止盈1 <= 止盈2")
    if direction == "short" and not (stop > entry > tp1 >= tp2):
        raise PlanBlocked("invalid_price_order", "做空价格必须满足 止损 > 入场 > 止盈1 >= 止盈2")

    broker = execution.selected_broker
    if broker == "longbridge":
        route = execution.longbridge
        product = "securities"
        requested_account = route.preferred_account
        is_paper = requested_account == "paper"
        allow_fallback = bool(
            requested_account == "intraday" and route.allow_comprehensive_fallback
        )
        environment = "demo" if is_paper else "live"
        okx_api_base_url = ""
        okx_margin_mode = ""
        longbridge_allow_outside_rth = bool(route.allow_outside_rth) and not is_paper
    elif broker == "okx":
        route = execution.okx
        product = route.product
        requested_account = "okx"
        allow_fallback = False
        environment = "demo" if route.simulated else "live"
        okx_api_base_url = route.api_base_url
        okx_margin_mode = route.margin_mode
        longbridge_allow_outside_rth = False
        if product == "spot" and direction == "short":
            raise PlanBlocked("spot_short_not_supported", "OKX 现货不能新开空仓，请选择永续")
        analysis_data_source = str(
            getattr(record.meta, "data_source", "unknown") or "unknown"
        ).strip().lower()
        if analysis_data_source != "okx":
            raise PlanBlocked(
                "price_source_mismatch",
                "OKX 执行只接受同一 OKX 行情源生成的价格；"
                f"当前分析来源为 {analysis_data_source or 'unknown'}",
            )
    else:
        raise PlanBlocked("unknown_broker", f"未知执行券商：{broker}")

    source_symbol = str(route.source_symbol or "").strip().upper()
    analysis_symbol = str(record.meta.symbol or "").strip().upper()
    if not source_symbol:
        raise PlanBlocked("route_incomplete", "执行配置缺少 PA 来源品种")
    if source_symbol != analysis_symbol:
        raise PlanBlocked(
            "source_symbol_mismatch",
            f"当前分析品种 {analysis_symbol} 与执行映射 {source_symbol} 不一致",
        )
    instrument = str(route.instrument or "").strip().upper()
    if not instrument:
        raise PlanBlocked("route_incomplete", "执行配置缺少券商品种")
    if source_symbol != instrument:
        raise PlanBlocked(
            "price_basis_mismatch",
            "PA 的入场、止盈、止损价格只能直接用于同一个券商品种；"
            f"当前分析 {source_symbol}，下单 {instrument}",
        )
    quantity = _positive_decimal(route.quantity, "quantity")
    risk_snapshot = (
        record.stage2_response.get("risk_sizing")
        if isinstance(record.stage2_response, dict)
        and isinstance(record.stage2_response.get("risk_sizing"), dict)
        else {}
    )

    def _risk_decimal(field: str) -> Decimal | None:
        value = risk_snapshot.get(field)
        if value in {None, ""}:
            return None
        return _positive_decimal(value, f"risk_sizing.{field}")

    sizing_mode = str(risk_snapshot.get("sizing_mode") or "risk_budget")
    if sizing_mode not in {"risk_budget", "fixed_quantity"}:
        raise PlanBlocked(
            "risk_sizing_mode_invalid",
            f"风险定仓模式无效：{sizing_mode}",
        )
    fixed_quantity = (
        _risk_decimal("target_quantity")
        if sizing_mode == "fixed_quantity"
        else None
    )

    digest = _verify_durable_record(record, record_path)
    plan_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"pa-agent:{digest}"))
    return ExecutionPlan(
        id=plan_id,
        analysis_digest=digest,
        analysis_record_path=str(record_path.resolve()),
        broker=broker,
        environment=environment,
        product=product,
        requested_account=requested_account,
        allow_account_fallback=allow_fallback,
        source_symbol=source_symbol,
        instrument=instrument,
        direction=direction,
        entry_type=entry_type,
        quantity=quantity,
        entry_price=entry,
        take_profit_1=tp1,
        take_profit_2=tp2,
        stop_loss=stop,
        trade_confidence=confidence,
        created_at=utc_now_iso(),
        config_fingerprint=execution_route_fingerprint(settings, broker),
        okx_api_base_url=okx_api_base_url,
        okx_margin_mode=okx_margin_mode,
        longbridge_allow_outside_rth=longbridge_allow_outside_rth,
        entry_timeout_seconds=int(execution.entry_timeout_seconds),
        entry_order_mode=entry_order_mode,
        exit_order_mode=exit_order_mode,
        entry_atr=entry_atr,
        entry_slippage_atr_multiple=entry_slippage_atr_multiple,
        exit_slippage_atr_multiple=exit_slippage_atr_multiple,
        authorized_sizing_mode=sizing_mode,
        authorized_fixed_quantity=fixed_quantity,
        risk_equity_basis=str(risk_snapshot.get("equity_basis") or ""),
        authorized_account_total_equity_usd=_risk_decimal(
            "account_total_equity_usd"
        ),
        authorized_account_equity_usdt=_risk_decimal("equity_usdt"),
        authorized_risk_capital_cap_usdt=_risk_decimal(
            "risk_capital_cap_usdt"
        ),
        authorized_effective_risk_capital_usdt=_risk_decimal(
            "effective_risk_capital_usdt"
        ),
        authorized_risk_percent=_risk_decimal("risk_percent"),
        authorized_risk_budget_usdt=_risk_decimal("risk_budget_usdt"),
        authorized_risk_used_usdt=_risk_decimal("risk_used_usdt"),
        authorized_contract_notional_usdt=_risk_decimal(
            "contract_notional_usdt"
        ),
        authorized_worst_case_loss_per_contract_usdt=_risk_decimal(
            "worst_case_loss_per_contract_usdt"
        ),
    )
