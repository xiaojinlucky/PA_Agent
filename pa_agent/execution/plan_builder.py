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
from pa_agent.records.schema import AnalysisRecord


def _positive_decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PlanBlocked("invalid_number", f"{field_name} 不是有效数字") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise PlanBlocked("invalid_number", f"{field_name} 必须是有限正数")
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
            "environment": "live",
            "okx_margin_mode": "",
            "longbridge_outside_rth": bool(route.allow_outside_rth),
            "min_trade_confidence": int(execution.min_trade_confidence),
            "entry_timeout_seconds": int(execution.entry_timeout_seconds),
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
    entry_type = entry_type_map.get(order_type_raw)
    if entry_type is None:
        raise PlanBlocked("unsupported_order_type", f"不支持的 PA 入场类型：{order_type_raw}")

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
        allow_fallback = bool(
            requested_account == "intraday" and route.allow_comprehensive_fallback
        )
        environment = "live"
        okx_api_base_url = ""
        okx_margin_mode = ""
        longbridge_allow_outside_rth = bool(route.allow_outside_rth)
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
    quantity = _positive_decimal(route.quantity, "quantity")

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
    )
