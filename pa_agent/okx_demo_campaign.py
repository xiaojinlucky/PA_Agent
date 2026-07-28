"""OKX Demo 黄金 10 分钟自动交易运行器。

该入口故意固定交易范围，运行时不采纳日常交易路由中的品种和券商字段：

- OKX XAU-USDT-SWAP / 10m（由真实已收盘 5m 两两聚合）
- PA 极度激进
- 执行置信度门槛 20
- OKX Demo XAU-USDT-SWAP / cross / 固定风险资金上限与实时权益较小值的动态张数
- 运行窗口 24 小时
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Decimal,
    InvalidOperation,
)
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pa_agent.agents.supervisor import resolve_verified_profile
from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.json_validator import JsonValidator
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.router import route_strategy_files
from pa_agent.config.paths import (
    EXPERIENCE_DIR,
    PROMPT_DIR,
    RECORDS_PENDING_DIR,
    SETTINGS_JSON_PATH,
)
from pa_agent.config.settings import load_settings
from pa_agent.data.base import DataSourceTransientError, KlineBar
from pa_agent.data.datetime_ts import ts_open_to_ms
from pa_agent.data.multi_timeframe import (
    higher_timeframes_for,
    render_higher_timeframe_context,
)
from pa_agent.data.okx_source import aggregate_okx_five_minute_rows
from pa_agent.data.snapshot import build_analysis_frame
from pa_agent.execution.controller import ExecutionController
from pa_agent.execution.credentials import (
    account_identity_fingerprint,
    load_okx_credentials,
)
from pa_agent.execution.errors import (
    BrokerApiError,
    BrokerTransportError,
    LiveTradingDisabled,
    NewRiskLeaseUnavailable,
    PlanBlocked,
)
from pa_agent.execution.models import ACTIVE_EXECUTION_STATES, ExecutionState
from pa_agent.execution.okx_client import OkxRestClient
from pa_agent.execution.order_modes import apply_entry_atr_slippage
from pa_agent.execution.plan_builder import execution_route_fingerprint
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker_protocol import (
    SetLeverageParameters,
    WorkerCommandStatus,
    leverage_intent_snapshot,
)
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.records.analysis_history import (
    list_record_paths,
    load_record,
)
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.records.schema import AnalysisRecord, RecordMeta
from pa_agent.risk.leverage import (
    LeveragePlanningFailure,
    build_minimum_leverage_parameters,
)
from pa_agent.risk.runtime import (
    RECOVERABLE_TRANSIENT_RISK_STOP_REASONS,
)
from pa_agent.risk.sizing import (
    RiskCalculationFailure,
    calculate_fixed_quantity_risk,
    calculate_risk_size,
)
from pa_agent.util.logging import configure_logging
from pa_agent.util.threading import CancelToken

CAMPAIGN_INSTRUMENT = "XAU-USDT-SWAP"
CAMPAIGN_SYMBOL = CAMPAIGN_INSTRUMENT
CAMPAIGN_TIMEFRAME = "10m"
CAMPAIGN_PRODUCT = "swap"
CAMPAIGN_MARGIN_MODE = "cross"
# PA Demo 是 USDT 结算合约；定仓资本取用户固定上限与最新 USDT eq 较小值。
# totalEq 只用于资金流、高水位和 50% 回撤。
CAMPAIGN_RISK_EQUITY_BASIS = "fixed_cap_or_usdt_equity_whichever_lower"
# 仅保留为旧测试/默认配置的 10% 示例；生产定仓读取用户 risk_percent。
CAMPAIGN_EQUITY_FRACTION = Decimal("0.10")
CAMPAIGN_FEE_RATE = Decimal("0.0005")
CAMPAIGN_SLIPPAGE_RATE = Decimal("0.0010")
CAMPAIGN_DEFAULT_SIZING_MODE = "risk_budget"
CAMPAIGN_BOOTSTRAP_QUANTITY = "1"
CAMPAIGN_OKX_API_BASE_URL = "https://www.okx.com"
CAMPAIGN_STANCE = "extreme_aggressive"
CAMPAIGN_MIN_CONFIDENCE = 20
CAMPAIGN_ENTRY_ORDER_MODE = "limit_with_slippage"
CAMPAIGN_EXIT_ORDER_MODE = "limit_with_slippage"
# 允许滑点不是固定基点，而是分析时主周期 ATR14 的倍数。0.50 ATR
# 故意设得足够大，先让 Demo 真实检验“更容易成交但价格更差”的边界。
CAMPAIGN_ENTRY_SLIPPAGE_ATR_MULTIPLE = Decimal("0.50")
CAMPAIGN_EXIT_SLIPPAGE_ATR_MULTIPLE = Decimal("0.50")
CAMPAIGN_HIGHER_TIMEFRAMES = higher_timeframes_for(CAMPAIGN_TIMEFRAME)
_CAMPAIGN_TIMEFRAME_TO_OKX_BAR = {
    # OKX 没有原生 10m，主周期使用真实 5m 两两聚合。
    "10m": "5m",
    "1h": "1H",
    "4h": "4H",
}
# 本轮只用于 Demo 10 分钟闭环验收。它只改变模型在本运行器中的下单方式，
# 不写入日常设置，也不改变 GUI / 普通分析的提示词。
CAMPAIGN_EXECUTION_STYLE = "limit_with_slippage_when_valid"
CAMPAIGN_FAST_EXECUTION_GUIDANCE = """
## 本轮专用：10 分钟 Demo 快速执行模式

这是一次仅限 OKX Demo 的闭环验收：目标是尽快验证「分析 → 入场 → 保护 → 离场」。
它不是日常策略默认规则，也不会改变其他调用。

- 若 §9、§10.3、§14 允许交易，输出基于最新已收盘 K1 的有效即时三价。
  执行层会按 0.50 ATR 使用 limit_with_slippage，
  不得把滑点后的委托价写回 PA 信号价。
- 三价必须满足方向顺序、最小价格跳动和 RR / 交易者方程；止损和止盈不能沿用远离 K1 的旧计划。
- 本运行器只有「有效的即时方案」或「不下单」两种选择，不得用等待未来回撤或突破冒充当前有效信号。
- 不要把“还可以等更好价格”“尚未完美确认”本身当作不下单理由。
  只有无法构造合法方向、止损、TP1、TP2 的即时三价时才输出不下单。
  不得伪造价格、取消止损或放宽 §14。
""".strip()
# 10 分钟循环的限价加滑点模式保留 270 秒入场窗口。
CAMPAIGN_ENTRY_TIMEOUT_SECONDS = 270
CAMPAIGN_DURATION = timedelta(hours=24)
CAMPAIGN_POLL_SECONDS = 30.0
CAMPAIGN_CLOSEOUT_SECONDS = 15 * 60
CAMPAIGN_STATE_PATH = RECORDS_PENDING_DIR.parent / "okx_demo_campaign.json"
CAMPAIGN_LOCK_PATH = RECORDS_PENDING_DIR.parent / "okx_demo_campaign.lock"
CAMPAIGN_HISTORY_DIR = RECORDS_PENDING_DIR.parent / "okx_demo_campaign_history"
CAMPAIGN_RECONCILE_TIMEOUT_RESULT = "blocked:reconcile:timeout"
CAMPAIGN_RECONCILE_WORKER_ATTENTION_RESULT = (
    "blocked:reconcile:worker_needs_attention"
)
CAMPAIGN_SAFE_TERMINAL_STATES = frozenset(
    {
        ExecutionState.CLOSED,
        ExecutionState.BLOCKED,
        ExecutionState.CANCELED,
        ExecutionState.REJECTED,
    }
)

# 这不是策略信号，也不是日常运行器的一部分。它只用来把 Demo 的
# 「市价入场 -> 成交回读 -> 原生保护 -> 受控离场」完整走一遍。
CANARY_TIMEFRAME = "demo_canary"
CANARY_ORIGIN = "okx_demo_lifecycle_canary"
CONTROLLED_DEMO_S_ORIGIN = "controlled_reproducible_demo_s"
CANARY_DIRECTION = "long"
CANARY_TIMEOUT_SECONDS = 120.0
CANARY_CLEANUP_TIMEOUT_SECONDS = CAMPAIGN_ENTRY_TIMEOUT_SECONDS + 60.0
CANARY_PRICE_BUFFER_RATIO = Decimal("0.005")
CANARY_MIN_BUFFER_TICKS = Decimal("50")

logger = logging.getLogger("pa_agent.okx_demo_campaign")


class CampaignError(RuntimeError):
    """实验配置或运行状态不满足硬约束。"""


class CampaignRiskBlocked(CampaignError):
    """当前 PA 信号无法通过确定性风险定仓。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        required_size: Decimal | None = None,
        maximum_size: Decimal | None = None,
    ) -> None:
        self.code = str(code)
        self.required_size = required_size
        self.maximum_size = maximum_size
        super().__init__(message)


class CampaignState(BaseModel):
    """不含密钥的可恢复实验状态。"""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    campaign_id: str
    config_fingerprint: str
    frozen_risk_capital_cap_usdt: Decimal | None = Field(
        default=None,
        ge=0,
    )
    frozen_risk_percent: Decimal | None = Field(default=None, gt=0, le=1)
    frozen_maximum_leverage: Decimal | None = Field(default=None, ge=1)
    frozen_sizing_mode: Literal["risk_budget", "fixed_quantity"] | None = None
    frozen_fixed_quantity: Decimal | None = Field(default=None, gt=0)
    started_at: str
    expires_at: str
    status: Literal[
        "active",
        "stopping",
        "completed",
        "needs_attention",
    ] = "active"
    inflight_bar_ms: int | None = None
    risk_recovery_bar_ms: int | None = None
    risk_recovery_command_id: str = ""
    last_completed_bar_ms: int | None = None
    analyses_completed: int = Field(default=0, ge=0)
    analyses_failed: int = Field(default=0, ge=0)
    executions_prepared: int = Field(default=0, ge=0)
    execution_ids: list[str] = Field(default_factory=list)
    supervisor_record_ids: list[str] = Field(default_factory=list)
    last_execution_id: str = ""
    last_supervisor_action: str = ""
    last_plan_result: str = ""
    last_error: str = ""
    updated_at: str

    @property
    def expires_at_utc(self) -> datetime:
        return _parse_utc(self.expires_at, "expires_at")

    @property
    def started_at_utc(self) -> datetime:
        return _parse_utc(self.started_at, "started_at")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_utc(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CampaignError(f"实验状态 {field_name} 不是有效时间") from exc
    if parsed.tzinfo is None:
        raise CampaignError(f"实验状态 {field_name} 缺少时区")
    return parsed.astimezone(UTC)


def _campaign_config_payload(settings: Any | None = None) -> dict[str, Any]:
    active_settings = settings or load_settings(SETTINGS_JSON_PATH)
    risk_route = active_settings.execution.okx
    return {
        "symbol": CAMPAIGN_SYMBOL,
        "timeframe": CAMPAIGN_TIMEFRAME,
        "market_data_source": "okx_public_5m_utc_pair_aggregation",
        "instrument": CAMPAIGN_INSTRUMENT,
        "product": CAMPAIGN_PRODUCT,
        "margin_mode": CAMPAIGN_MARGIN_MODE,
        "sizing": {
            "mode": str(
                getattr(
                    risk_route,
                    "sizing_mode",
                    CAMPAIGN_DEFAULT_SIZING_MODE,
                )
            ),
            "equity_basis": CAMPAIGN_RISK_EQUITY_BASIS,
            "fixed_quantity": (
                str(risk_route.quantity)
                if str(
                    getattr(
                        risk_route,
                        "sizing_mode",
                        CAMPAIGN_DEFAULT_SIZING_MODE,
                    )
                )
                == "fixed_quantity"
                else None
            ),
            "risk_capital_cap_usdt": str(
                risk_route.risk_capital_cap_usdt
            ),
            "risk_percent": str(risk_route.risk_percent),
            "maximum_leverage": str(risk_route.maximum_leverage),
            "fee_rate": str(CAMPAIGN_FEE_RATE),
            "slippage_rate": str(CAMPAIGN_SLIPPAGE_RATE),
            "price_source": (
                "pa_stage2_entry_price_plus_0.50_atr_effective_limit"
            ),
            "stop_source": "pa_stage2_stop_loss_price",
            "contract_value_source": "okx_swap_ctVal_x_ctMult",
        },
        "api_base_url": CAMPAIGN_OKX_API_BASE_URL,
        "decision_stance": CAMPAIGN_STANCE,
        "execution_style": CAMPAIGN_EXECUTION_STYLE,
        "min_trade_confidence": CAMPAIGN_MIN_CONFIDENCE,
        "entry_order_mode": CAMPAIGN_ENTRY_ORDER_MODE,
        "exit_order_mode": CAMPAIGN_EXIT_ORDER_MODE,
        "entry_slippage_atr_multiple": str(CAMPAIGN_ENTRY_SLIPPAGE_ATR_MULTIPLE),
        "exit_slippage_atr_multiple": str(CAMPAIGN_EXIT_SLIPPAGE_ATR_MULTIPLE),
        "higher_timeframes": list(CAMPAIGN_HIGHER_TIMEFRAMES),
        "entry_timeout_seconds": CAMPAIGN_ENTRY_TIMEOUT_SECONDS,
        "environment": "demo",
        "duration_seconds": int(CAMPAIGN_DURATION.total_seconds()),
    }


def campaign_config_fingerprint(settings: Any | None = None) -> str:
    encoded = json.dumps(
        _campaign_config_payload(settings),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _campaign_frozen_risk_values(
    settings: Any,
) -> dict[str, Any]:
    route = settings.execution.okx
    sizing_mode = str(
        getattr(route, "sizing_mode", CAMPAIGN_DEFAULT_SIZING_MODE)
    )
    fixed_quantity = (
        _positive_decimal(route.quantity)
        if sizing_mode == "fixed_quantity"
        else None
    )
    if sizing_mode == "fixed_quantity" and fixed_quantity <= 0:
        raise CampaignError("固定张数模式必须设置正数合约张数")
    return {
        "frozen_risk_capital_cap_usdt": Decimal(
            str(route.risk_capital_cap_usdt)
        ),
        "frozen_risk_percent": Decimal(str(route.risk_percent)),
        "frozen_maximum_leverage": Decimal(str(route.maximum_leverage)),
        "frozen_sizing_mode": sizing_mode,
        "frozen_fixed_quantity": fixed_quantity,
    }


def build_campaign_settings(
    base_settings,
    *,
    quantity: Decimal | str = CAMPAIGN_BOOTSTRAP_QUANTITY,
    entry_order_mode: str | None = None,
    exit_order_mode: str | None = None,
    entry_slippage_atr_multiple: Decimal | float | str | None = None,
    exit_slippage_atr_multiple: Decimal | float | str | None = None,
):
    """复制设置并只在内存中应用实验路由。"""
    settings = copy.deepcopy(base_settings)
    # 10 分钟循环优先保证一根 K 线内完成判断，不沿用日常高推理强度。
    settings.provider.reasoning_effort = "medium"
    settings.general.decision_stance = CAMPAIGN_STANCE
    settings.general.last_data_source = "okx"
    settings.general.last_symbol = CAMPAIGN_SYMBOL
    settings.general.last_timeframe = CAMPAIGN_TIMEFRAME
    settings.execution.enabled = True
    # 由实验进程在 execution id 耐久落盘后显式提交, 避免券商写入先于归属记录。
    settings.execution.auto_execute = False
    settings.execution.selected_broker = "okx"
    settings.execution.min_trade_confidence = CAMPAIGN_MIN_CONFIDENCE
    settings.execution.entry_timeout_seconds = CAMPAIGN_ENTRY_TIMEOUT_SECONDS
    settings.execution.entry_order_mode = entry_order_mode or CAMPAIGN_ENTRY_ORDER_MODE
    settings.execution.exit_order_mode = exit_order_mode or CAMPAIGN_EXIT_ORDER_MODE
    settings.execution.entry_slippage_atr_multiple = (
        CAMPAIGN_ENTRY_SLIPPAGE_ATR_MULTIPLE
        if entry_slippage_atr_multiple is None
        else Decimal(str(entry_slippage_atr_multiple))
    )
    settings.execution.exit_slippage_atr_multiple = (
        CAMPAIGN_EXIT_SLIPPAGE_ATR_MULTIPLE
        if exit_slippage_atr_multiple is None
        else Decimal(str(exit_slippage_atr_multiple))
    )
    settings.execution.okx.source_symbol = CAMPAIGN_SYMBOL
    settings.execution.okx.instrument = CAMPAIGN_INSTRUMENT
    resolved_quantity = _positive_decimal(quantity)
    if resolved_quantity <= 0:
        raise CampaignError("OKX Demo 动态仓位未得到有效合约数")
    settings.execution.okx.quantity = str(resolved_quantity)
    settings.execution.okx.product = CAMPAIGN_PRODUCT
    settings.execution.okx.margin_mode = CAMPAIGN_MARGIN_MODE
    settings.execution.okx.simulated = True
    settings.execution.okx.api_base_url = CAMPAIGN_OKX_API_BASE_URL
    validate_campaign_settings(settings)
    return settings


def validate_campaign_settings(settings) -> None:
    """每次启用写会话前重检不可变实验边界。"""
    route = settings.execution.okx
    checks = {
        "execution.enabled": bool(settings.execution.enabled),
        "execution.auto_execute=false": not bool(settings.execution.auto_execute),
        "execution.selected_broker": settings.execution.selected_broker == "okx",
        "execution.min_trade_confidence": (
            int(settings.execution.min_trade_confidence) == CAMPAIGN_MIN_CONFIDENCE
        ),
        "execution.entry_timeout_seconds": (
            int(settings.execution.entry_timeout_seconds)
            == CAMPAIGN_ENTRY_TIMEOUT_SECONDS
        ),
        "execution.entry_order_mode": settings.execution.entry_order_mode
        in {"signal", "limit", "limit_with_slippage", "market"},
        "execution.exit_order_mode": settings.execution.exit_order_mode
        in {"limit", "limit_with_slippage", "market"},
        "execution.entry_slippage_atr_multiple": 0
        <= Decimal(str(settings.execution.entry_slippage_atr_multiple))
        <= 5,
        "execution.exit_slippage_atr_multiple": 0
        <= Decimal(str(settings.execution.exit_slippage_atr_multiple))
        <= 5,
        "general.decision_stance": settings.general.decision_stance == CAMPAIGN_STANCE,
        "general.last_symbol": settings.general.last_symbol == CAMPAIGN_SYMBOL,
        "general.last_timeframe": settings.general.last_timeframe == CAMPAIGN_TIMEFRAME,
        "okx.source_symbol": route.source_symbol == CAMPAIGN_SYMBOL,
        "okx.instrument": route.instrument == CAMPAIGN_INSTRUMENT,
        "okx.quantity": _positive_decimal(route.quantity) > 0,
        "okx.product": route.product == CAMPAIGN_PRODUCT,
        "okx.margin_mode": route.margin_mode == CAMPAIGN_MARGIN_MODE,
        "okx.simulated": bool(route.simulated),
        "okx.api_base_url": route.api_base_url == CAMPAIGN_OKX_API_BASE_URL,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise CampaignError(
            "OKX Demo 实验配置越界, 禁止继续: " + ", ".join(failed)
        )


def _campaign_execution_bar_ms(execution: Any) -> int:
    """从耐久分析记录核验一个 Campaign execution 所属的 K 线。"""

    path = Path(execution.plan.analysis_record_path)
    try:
        path.resolve().relative_to(RECORDS_PENDING_DIR.resolve())
    except (OSError, ValueError) as exc:
        raise CampaignError(
            f"execution {execution.id} 的分析记录不在实验记录目录"
        ) from exc
    record = load_record(path)
    if record is None or not record.kline_data:
        raise CampaignError(
            f"execution {execution.id} 的分析记录不存在或不可读取"
        )
    if (
        record.meta.symbol != CAMPAIGN_SYMBOL
        or record.meta.timeframe != CAMPAIGN_TIMEFRAME
        or record.meta.decision_stance != CAMPAIGN_STANCE
    ):
        raise CampaignError(
            f"execution {execution.id} 的分析记录不属于本实验"
        )
    try:
        return int(ts_open_to_ms(record.kline_data[0]["ts_open"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise CampaignError(
            f"execution {execution.id} 的分析记录缺少有效 K 线时间"
        ) from exc


class CampaignStateStore:
    """同目录原子替换实验状态。"""

    def __init__(self, path: Path = CAMPAIGN_STATE_PATH) -> None:
        self.path = Path(path)

    def load(self) -> CampaignState | None:
        if not self.path.is_file():
            return None
        try:
            return CampaignState.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise CampaignError(f"无法读取实验状态: {self.path}") from exc

    def create_or_resume(
        self,
        *,
        now: datetime | None = None,
        settings: Any | None = None,
    ) -> CampaignState:
        current = (now or _utc_now()).astimezone(UTC)
        existing = self.load()
        active_settings = settings or load_settings(SETTINGS_JSON_PATH)
        fingerprint = campaign_config_fingerprint(active_settings)
        frozen_risk = _campaign_frozen_risk_values(active_settings)
        if existing is not None:
            if existing.config_fingerprint != fingerprint:
                raise CampaignError("现有实验状态与当前固定配置不一致, 禁止覆盖")
            if existing.status == "completed":
                raise CampaignError("该 24 小时实验已经完成, 禁止自动重新计时")
            if existing.status in {"stopping", "needs_attention"}:
                raise CampaignError(
                    f"现有实验状态为 {existing.status}，禁止自动恢复；"
                    "请先完成人工核对，再使用显式 restart"
                )
            existing_frozen = {
                field: getattr(existing, field)
                for field in frozen_risk
            }
            if (
                any(value is not None for value in existing_frozen.values())
                and existing_frozen != frozen_risk
            ):
                raise CampaignError("现有实验状态的冻结风险参数与指纹不一致")
            return existing.model_copy(
                update={
                    "status": "active",
                    "updated_at": current.isoformat(),
                    **frozen_risk,
                }
            )
        return CampaignState(
            campaign_id=str(uuid.uuid4()),
            config_fingerprint=fingerprint,
            **frozen_risk,
            started_at=current.isoformat(),
            expires_at=(current + CAMPAIGN_DURATION).isoformat(),
            updated_at=current.isoformat(),
        )

    def restart(
        self,
        *,
        reason: str,
        now: datetime | None = None,
        execution_lookup: Callable[[str], Any] | None = None,
        settings: Any | None = None,
    ) -> CampaignState:
        """保留旧状态快照后，以明确新配置开启新的 Demo Campaign。"""
        current = (now or _utc_now()).astimezone(UTC)
        existing = self.load()
        restart_last_completed_bar_ms = (
            existing.last_completed_bar_ms
            if existing is not None
            else None
        )
        if existing is not None:
            owned_executions: dict[str, Any] = {}
            if existing.execution_ids:
                if execution_lookup is None:
                    raise CampaignError(
                        "旧 Campaign 仍有自己创建的 execution，禁止直接切换配置"
                    )
                allowed_terminal_states = {
                    ExecutionState.CLOSED,
                    ExecutionState.BLOCKED,
                    ExecutionState.CANCELED,
                    ExecutionState.REJECTED,
                }
                unresolved = []
                for execution_id in existing.execution_ids:
                    execution = execution_lookup(execution_id)
                    if execution is not None:
                        owned_executions[execution_id] = execution
                    if (
                        execution is None
                        or execution.state not in allowed_terminal_states
                        or bool(
                            getattr(execution, "needs_attention", False)
                        )
                    ):
                        unresolved.append(execution_id)
                if unresolved:
                    raise CampaignError(
                        "旧 Campaign 仍有未确认终态的 execution，禁止直接切换配置"
                    )
            if (
                existing.inflight_bar_ms is not None
                and existing.last_execution_id in owned_executions
            ):
                execution_bar_ms = _campaign_execution_bar_ms(
                    owned_executions[existing.last_execution_id]
                )
                if execution_bar_ms == existing.inflight_bar_ms:
                    restart_last_completed_bar_ms = max(
                        value
                        for value in (
                            restart_last_completed_bar_ms,
                            execution_bar_ms,
                        )
                        if value is not None
                    )
            CAMPAIGN_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            archive = CAMPAIGN_HISTORY_DIR / (
                f"{existing.campaign_id}-{current.strftime('%Y%m%dT%H%M%SZ')}.json"
            )
            if archive.exists():
                raise CampaignError("旧 Campaign 状态归档已存在，禁止覆盖")
            archive_payload = {
                "superseded_at": current.isoformat(),
                "reason": reason,
                "state": existing.model_dump(mode="json"),
            }
            self._write_json(archive, archive_payload)
        active_settings = settings or load_settings(SETTINGS_JSON_PATH)
        return CampaignState(
            campaign_id=str(uuid.uuid4()),
            config_fingerprint=campaign_config_fingerprint(active_settings),
            **_campaign_frozen_risk_values(active_settings),
            started_at=current.isoformat(),
            expires_at=(current + CAMPAIGN_DURATION).isoformat(),
            last_completed_bar_ms=restart_last_completed_bar_ms,
            updated_at=current.isoformat(),
        )

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(3):
                try:
                    os.replace(temp_path, path)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        except OSError as exc:
            raise CampaignError(f"无法耐久保存实验状态: {path}") from exc

    def save(self, state: CampaignState) -> None:
        updated = state.model_copy(update={"updated_at": _utc_now().isoformat()})
        self._write_json(self.path, updated.model_dump(mode="json"))


class CampaignProcessLock:
    """持有操作系统文件锁, 进程退出后自动释放。"""

    def __init__(self, path: Path = CAMPAIGN_LOCK_PATH) -> None:
        self.path = Path(path)
        self._handle: Any = None

    def __enter__(self) -> CampaignProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._handle.close()
            self._handle = None
            raise CampaignError("已有 OKX Demo 24 小时实验进程在运行") from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _positive_decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return parsed if parsed.is_finite() and parsed > 0 else Decimal("0")


@dataclass(frozen=True)
class CampaignSizing:
    """一次 Demo 新开仓所需的不可变风险数量快照。"""

    sizing_mode: Literal["risk_budget", "fixed_quantity"]
    quantity: Decimal
    account_total_equity_usd: Decimal
    equity_usdt: Decimal
    risk_capital_cap_usdt: Decimal
    effective_risk_capital_usdt: Decimal
    risk_percent: Decimal
    risk_budget_usdt: Decimal
    risk_used_usdt: Decimal
    reference_price_usdt: Decimal
    contract_notional_usdt: Decimal
    stop_distance_usdt: Decimal
    worst_case_loss_per_contract_usdt: Decimal
    fee_per_contract_usdt: Decimal
    slippage_per_contract_usdt: Decimal
    fee_rate: Decimal
    slippage_rate: Decimal
    minimum_quantity: Decimal
    quantity_step: Decimal
    max_buy: Decimal
    max_sell: Decimal
    equity_basis: str = CAMPAIGN_RISK_EQUITY_BASIS


@dataclass(frozen=True)
class CampaignLeverageCandidate:
    """A read-only capacity plan plus sizing at its target leverage."""

    parameters: SetLeverageParameters
    sizing: CampaignSizing


def _campaign_sizing_snapshot(sizing: CampaignSizing) -> dict[str, str]:
    """把真实定仓输入和结果写成可长期复核的非秘密快照。"""
    return {
        "sizing_mode": sizing.sizing_mode,
        "equity_basis": sizing.equity_basis,
        "account_total_equity_usd": str(
            sizing.account_total_equity_usd
        ),
        "equity_usdt": str(sizing.equity_usdt),
        "risk_capital_cap_usdt": str(sizing.risk_capital_cap_usdt),
        "effective_risk_capital_usdt": str(
            sizing.effective_risk_capital_usdt
        ),
        "risk_percent": str(sizing.risk_percent),
        "risk_budget_usdt": str(sizing.risk_budget_usdt),
        "risk_used_usdt": str(sizing.risk_used_usdt),
        "reference_price_usdt": str(sizing.reference_price_usdt),
        "stop_distance_usdt": str(sizing.stop_distance_usdt),
        "contract_notional_usdt": str(sizing.contract_notional_usdt),
        "worst_case_loss_per_contract_usdt": str(
            sizing.worst_case_loss_per_contract_usdt
        ),
        "fee_per_contract_usdt": str(sizing.fee_per_contract_usdt),
        "slippage_per_contract_usdt": str(sizing.slippage_per_contract_usdt),
        "fee_rate": str(sizing.fee_rate),
        "slippage_rate": str(sizing.slippage_rate),
        "minimum_quantity": str(sizing.minimum_quantity),
        "quantity_step": str(sizing.quantity_step),
        "max_buy": str(sizing.max_buy),
        "max_sell": str(sizing.max_sell),
        "target_quantity": str(sizing.quantity),
    }


def _attach_campaign_sizing(
    record: AnalysisRecord,
    sizing: CampaignSizing,
) -> None:
    """把本次定仓快照并入策略记录，不改模型判断或委托价格。"""
    response = (
        dict(record.stage2_response)
        if isinstance(record.stage2_response, dict)
        else {}
    )
    response["risk_sizing"] = _campaign_sizing_snapshot(sizing)
    record.stage2_response = response


def _attach_campaign_leverage_intent(
    record: AnalysisRecord,
    parameters: SetLeverageParameters,
) -> None:
    """Put the exact proposed leverage action inside the supervised record."""
    response = (
        dict(record.stage2_response)
        if isinstance(record.stage2_response, dict)
        else {}
    )
    response["leverage_intent"] = leverage_intent_snapshot(parameters)
    record.stage2_response = response


def _rebuild_leverage_parameters(
    parameters: SetLeverageParameters,
    **updates: object,
) -> SetLeverageParameters:
    payload = parameters.model_dump(mode="python")
    payload.update(updates)
    if any(
        field in updates
        for field in (
            "analysis_record_path",
            "config_fingerprint",
            "instrument",
            "direction",
            "margin_mode",
            "position_mode",
            "current_leverage",
            "target_leverage",
            "current_capacity",
            "target_capacity",
            "maximum_leverage",
            "maximum_capacity",
            "planning_method",
            "policy_grid_step",
            "verified_grid",
            "required_quantity",
            "entry_price",
            "expected_account_identity",
            "okx_api_base_url",
        )
    ):
        payload["leverage_intent_digest"] = ""
    return SetLeverageParameters.model_validate(payload)


def _campaign_instrument(client: OkxRestClient) -> dict[str, Any]:
    instrument = next(
        (
            row
            for row in client.instruments("SWAP")
            if str(row.get("instId") or "") == CAMPAIGN_INSTRUMENT
        ),
        None,
    )
    if instrument is None:
        raise CampaignError(
            f"OKX Demo 当前账户不支持 {CAMPAIGN_INSTRUMENT}"
        )
    if str(instrument.get("state") or "") != "live":
        raise CampaignError(
            f"{CAMPAIGN_INSTRUMENT} 当前状态不是 live"
        )
    return dict(instrument)


def _demo_usdt_equity(balance_rows: list[dict[str, Any]]) -> Decimal:
    equities: list[Decimal] = []
    for account in balance_rows:
        for detail in account.get("details") or []:
            if str(detail.get("ccy") or "").upper() != "USDT":
                continue
            equity = _positive_decimal(detail.get("eq"))
            if equity <= 0:
                raise CampaignError("OKX Demo USDT 总权益无效")
            equities.append(equity)
    if not equities:
        raise CampaignError("OKX Demo 账户未返回 USDT 总权益")
    return sum(equities, Decimal("0"))


def _demo_total_equity(balance_rows: list[dict[str, Any]]) -> Decimal:
    totals = [
        _positive_decimal(account.get("totalEq"))
        for account in balance_rows
        if _positive_decimal(account.get("totalEq")) > 0
    ]
    if len(totals) != 1:
        raise CampaignError("OKX Demo 账户未返回唯一有效的 totalEq")
    return totals[0]


def _campaign_risk_policy(
    *,
    risk_capital_cap_usdt: object | None = None,
    risk_percent: object | None = None,
    maximum_leverage: object | None = None,
) -> tuple[object, object, object]:
    """返回冻结的用户风险配置；缺项只从正式设置读取，不使用动态余额兜底。"""

    if (
        risk_capital_cap_usdt is None
        or risk_percent is None
        or maximum_leverage is None
    ):
        route = load_settings(SETTINGS_JSON_PATH).execution.okx
        risk_capital_cap_usdt = (
            route.risk_capital_cap_usdt
            if risk_capital_cap_usdt is None
            else risk_capital_cap_usdt
        )
        risk_percent = (
            route.risk_percent if risk_percent is None else risk_percent
        )
        maximum_leverage = (
            route.maximum_leverage
            if maximum_leverage is None
            else maximum_leverage
        )
    return risk_capital_cap_usdt, risk_percent, maximum_leverage


def _campaign_sizing_mode_policy(
    *,
    sizing_mode: object | None = None,
    fixed_quantity: object | None = None,
) -> tuple[Literal["risk_budget", "fixed_quantity"], object | None]:
    """读取本轮冻结的定仓模式；固定张数缺失时明确失败。"""

    if sizing_mode is None:
        route = load_settings(SETTINGS_JSON_PATH).execution.okx
        sizing_mode = getattr(
            route,
            "sizing_mode",
            CAMPAIGN_DEFAULT_SIZING_MODE,
        )
        if fixed_quantity is None:
            fixed_quantity = route.quantity
    mode = str(sizing_mode or "").strip()
    if mode not in {"risk_budget", "fixed_quantity"}:
        raise CampaignError(f"不支持的 Campaign 定仓模式：{mode or '空'}")
    if mode == "fixed_quantity" and _positive_decimal(fixed_quantity) <= 0:
        raise CampaignError("固定张数模式必须设置正数合约张数")
    return mode, fixed_quantity  # type: ignore[return-value]


def resolve_campaign_sizing(
    client: OkxRestClient | None = None,
    *,
    entry_price: object | None = None,
    stop_loss_price: object | None = None,
    side: object | None = None,
    risk_capital_cap_usdt: object | None = None,
    risk_percent: object | None = None,
    leverage: object | None = None,
    sizing_mode: object | None = None,
    fixed_quantity: object | None = None,
) -> CampaignSizing:
    """用 PA 的入场/止损和 OKX 实时规格计算 Demo 首仓张数。"""
    risk_capital_cap_usdt, risk_percent, _maximum_leverage = (
        _campaign_risk_policy(
            risk_capital_cap_usdt=risk_capital_cap_usdt,
            risk_percent=risk_percent,
        )
    )
    resolved_sizing_mode, fixed_quantity = _campaign_sizing_mode_policy(
        sizing_mode=sizing_mode,
        fixed_quantity=fixed_quantity,
    )
    active_client = client or OkxRestClient(
        load_okx_credentials("demo"),
        base_url=CAMPAIGN_OKX_API_BASE_URL,
        simulated=True,
    )
    account_config = active_client.account_config()
    if str(account_config.get("posMode") or "") != "net_mode":
        raise CampaignError("OKX Demo 首发风险定仓只允许 net_mode 净持仓")
    instrument = _campaign_instrument(active_client)
    minimum = _positive_decimal(instrument.get("minSz"))
    lot = _positive_decimal(instrument.get("lotSz"))
    contract_value = _positive_decimal(instrument.get("ctVal"))
    contract_multiplier = _positive_decimal(instrument.get("ctMult"))
    if min(minimum, lot, contract_value, contract_multiplier) <= 0:
        raise CampaignError("OKX 黄金永续缺少有效的合约规格")

    balance_rows = active_client.balance()
    account_total_equity_usd = _demo_total_equity(balance_rows)
    equity_usdt = _demo_usdt_equity(balance_rows)
    maximum = active_client.max_order_size(
        instrument=CAMPAIGN_INSTRUMENT,
        trade_mode=CAMPAIGN_MARGIN_MODE,
        price=(
            str(_positive_decimal(entry_price))
            if _positive_decimal(entry_price) > 0
            else None
        ),
        leverage=(
            str(_positive_decimal(leverage))
            if _positive_decimal(leverage) > 0
            else None
        ),
    )
    max_buy = _positive_decimal(maximum.get("maxBuy"))
    max_sell = _positive_decimal(maximum.get("maxSell"))
    if max_buy <= 0 or max_sell <= 0:
        raise CampaignError("OKX Demo 当前最大可开张数无效")
    if not isinstance(side, str) or side not in {"long", "short"}:
        raise CampaignError("PA 方向不是可计算风险的 long/short")
    selected_max = max_buy if side == "long" else max_sell
    try:
        common_inputs = {
            "account_equity": equity_usdt,
            "risk_capital_cap": risk_capital_cap_usdt,
            "entry_price": entry_price,
            "stop_loss_price": stop_loss_price,
            "side": side,
            "ct_val": contract_value,
            "ct_mult": contract_multiplier,
            "lot_sz": lot,
            "min_sz": minimum,
            "max_sz": selected_max,
            "fee_rate": CAMPAIGN_FEE_RATE,
            "slippage_rate": CAMPAIGN_SLIPPAGE_RATE,
        }
        if resolved_sizing_mode == "fixed_quantity":
            result = calculate_fixed_quantity_risk(
                **common_inputs,
                quantity=fixed_quantity,
            )
        else:
            result = calculate_risk_size(
                **common_inputs,
                risk_percent=risk_percent,
            )
    except RiskCalculationFailure as exc:
        raise CampaignRiskBlocked(
            exc.code,
            f"风险定仓失败[{exc.code}]: {exc}",
            required_size=exc.required_size,
            maximum_size=exc.maximum_size,
        ) from exc
    return CampaignSizing(
        sizing_mode=resolved_sizing_mode,
        quantity=result.target_contract_size,
        account_total_equity_usd=account_total_equity_usd,
        equity_usdt=equity_usdt,
        risk_capital_cap_usdt=result.risk_capital_cap_usdt,
        effective_risk_capital_usdt=result.effective_risk_capital_usdt,
        risk_percent=result.risk_percent,
        risk_budget_usdt=result.risk_budget_usdt,
        risk_used_usdt=result.risk_used_usdt,
        reference_price_usdt=_positive_decimal(entry_price),
        contract_notional_usdt=result.contract_notional_usdt,
        stop_distance_usdt=result.stop_distance_usdt,
        worst_case_loss_per_contract_usdt=result.worst_case_loss_per_contract_usdt,
        fee_per_contract_usdt=result.fee_per_contract_usdt,
        slippage_per_contract_usdt=result.slippage_per_contract_usdt,
        fee_rate=CAMPAIGN_FEE_RATE,
        slippage_rate=CAMPAIGN_SLIPPAGE_RATE,
        minimum_quantity=result.minimum_size,
        quantity_step=result.lot_size,
        max_buy=max_buy,
        max_sell=max_sell,
        equity_basis=CAMPAIGN_RISK_EQUITY_BASIS,
    )


def _record_risk_inputs(record: AnalysisRecord) -> tuple[Decimal, Decimal, str]:
    """从已持久化的 PA 决策取风险引擎唯一需要的三项输入。"""
    stage2 = record.stage2_decision
    decision = stage2.get("decision") if isinstance(stage2, dict) else None
    if not isinstance(decision, dict):
        raise CampaignError("PA 阶段二缺少风险定仓所需的决策")
    direction = {"做多": "long", "做空": "short"}.get(
        str(decision.get("order_direction") or "").strip()
    )
    if direction is None:
        raise CampaignError("PA 决策缺少有效方向，禁止风险定仓")
    return (
        _positive_decimal(decision.get("entry_price")),
        _positive_decimal(decision.get("stop_loss_price")),
        direction,
    )


def _record_effective_entry_price(
    record: AnalysisRecord,
    client: OkxRestClient,
    entry_price: Decimal,
    side: str,
) -> Decimal:
    """按 Campaign 实际委托模式计算风险定仓使用的最终入场限价。"""
    if CAMPAIGN_ENTRY_ORDER_MODE != "limit_with_slippage":
        return entry_price
    try:
        shifted = apply_entry_atr_slippage(
            entry_price,
            side,
            record.analysis_atr14,
            CAMPAIGN_ENTRY_SLIPPAGE_ATR_MULTIPLE,
        )
    except ValueError as exc:
        raise CampaignError(f"PA 记录 ATR 滑点无效：{exc}") from exc
    tick = _positive_decimal(_campaign_instrument(client).get("tickSz"))
    return _align_to_tick(
        shifted,
        tick,
        rounding=ROUND_CEILING if side == "long" else ROUND_FLOOR,
    )


def resolve_record_campaign_sizing(
    record: AnalysisRecord,
    client: OkxRestClient | None = None,
    *,
    risk_capital_cap_usdt: object | None = None,
    risk_percent: object | None = None,
    sizing_mode: object | None = None,
    fixed_quantity: object | None = None,
) -> CampaignSizing:
    """把一份 PA 记录转换成唯一的 Demo 风险数量。"""
    entry_price, stop_loss_price, side = _record_risk_inputs(record)
    active_client = client or OkxRestClient(
        load_okx_credentials("demo"),
        base_url=CAMPAIGN_OKX_API_BASE_URL,
        simulated=True,
    )
    effective_entry_price = _record_effective_entry_price(
        record,
        active_client,
        entry_price,
        side,
    )
    return resolve_campaign_sizing(
        active_client,
        entry_price=effective_entry_price,
        stop_loss_price=stop_loss_price,
        side=side,
        risk_capital_cap_usdt=risk_capital_cap_usdt,
        risk_percent=risk_percent,
        sizing_mode=sizing_mode,
        fixed_quantity=fixed_quantity,
    )


def resolve_record_campaign_leverage(
    record: AnalysisRecord,
    analysis_digest: str,
    *,
    risk_capital_cap_usdt: object | None = None,
    risk_percent: object | None = None,
    maximum_leverage: object | None = None,
    sizing_mode: object | None = None,
    fixed_quantity: object | None = None,
) -> CampaignLeverageCandidate:
    """Build a Demo leverage candidate only after current capacity blocks."""
    (
        risk_capital_cap_usdt,
        risk_percent,
        maximum_leverage,
    ) = _campaign_risk_policy(
        risk_capital_cap_usdt=risk_capital_cap_usdt,
        risk_percent=risk_percent,
        maximum_leverage=maximum_leverage,
    )
    entry_price, stop_loss_price, side = _record_risk_inputs(record)
    client = OkxRestClient(
        load_okx_credentials("demo"),
        base_url=CAMPAIGN_OKX_API_BASE_URL,
        simulated=True,
    )
    effective_entry_price = _record_effective_entry_price(
        record,
        client,
        entry_price,
        side,
    )
    try:
        resolve_campaign_sizing(
            client,
            entry_price=effective_entry_price,
            stop_loss_price=stop_loss_price,
            side=side,
            risk_capital_cap_usdt=risk_capital_cap_usdt,
            risk_percent=risk_percent,
            sizing_mode=sizing_mode,
            fixed_quantity=fixed_quantity,
        )
    except CampaignRiskBlocked as exc:
        if exc.code != "max_size_exceeded" or exc.required_size is None:
            raise
        required_quantity = exc.required_size
    else:
        raise LeveragePlanningFailure(
            "current_capacity_sufficient",
            "当前杠杆容量已经足够，无需创建杠杆命令",
        )

    account_config = client.account_config()
    raw_account_type = account_config.get("type")
    account_identity = account_identity_fingerprint(
        "okx",
        "demo",
        str(account_config.get("uid") or ""),
        str(account_config.get("mainUid") or ""),
        "" if raw_account_type is None else str(raw_account_type),
    )
    leverage_rows = client.leverage_info(
        instrument=CAMPAIGN_INSTRUMENT,
        margin_mode=CAMPAIGN_MARGIN_MODE,
    )
    current_leverage = next(
        (
            _positive_decimal(row.get("lever"))
            for row in leverage_rows
            if str(row.get("instId") or "") == CAMPAIGN_INSTRUMENT
            and str(row.get("mgnMode") or "") == CAMPAIGN_MARGIN_MODE
            and str(row.get("posSide") or "net") == "net"
        ),
        Decimal("0"),
    )
    if current_leverage <= 0:
        raise LeveragePlanningFailure(
            "missing_current_leverage",
            "OKX 未返回当前全仓净持仓杠杆",
        )
    parameters = build_minimum_leverage_parameters(
        client=client,
        analysis_digest=analysis_digest,
        config_fingerprint="pending_campaign_sizing",
        instrument=CAMPAIGN_INSTRUMENT,
        direction=side,
        current_leverage=current_leverage,
        required_quantity=required_quantity,
        entry_price=effective_entry_price,
        expected_account_identity=account_identity,
        okx_api_base_url=CAMPAIGN_OKX_API_BASE_URL,
        maximum_leverage_cap=maximum_leverage,
    )
    if parameters is None:
        raise LeveragePlanningFailure(
            "current_capacity_sufficient",
            "容量刷新后已足够，无需创建杠杆命令",
        )
    sizing = resolve_campaign_sizing(
        client,
        entry_price=effective_entry_price,
        stop_loss_price=stop_loss_price,
        side=side,
        risk_capital_cap_usdt=risk_capital_cap_usdt,
        risk_percent=risk_percent,
        leverage=parameters.target_leverage,
        sizing_mode=sizing_mode,
        fixed_quantity=fixed_quantity,
    )
    if sizing.quantity != required_quantity:
        raise LeveragePlanningFailure(
            "risk_quantity_changed",
            "候选杠杆改变了风险公式目标数量",
        )
    return CampaignLeverageCandidate(
        parameters=parameters,
        sizing=sizing,
    )


def _align_to_tick(
    value: Decimal,
    tick: Decimal,
    *,
    rounding: str,
) -> Decimal:
    if tick <= 0:
        raise CampaignError("OKX 最小价格跳动无效")
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _canary_price_triplet(
    runtime: CampaignRuntime,
) -> tuple[Decimal, Decimal, Decimal, Decimal, KlineBar, float | None]:
    """只读取行情和合约规格，构造短时间内不易触发保护的 Demo 三价。"""
    bars = runtime.source.latest_snapshot(3)
    latest_closed = next((bar for bar in bars if bar.closed), None)
    if latest_closed is None:
        raise CampaignError("Demo 生命周期验收缺少已收盘 10m 聚合 K 线")

    client = OkxRestClient(
        load_okx_credentials("demo"),
        base_url=CAMPAIGN_OKX_API_BASE_URL,
        simulated=True,
    )
    instrument = next(
        (
            row
            for row in client.instruments("SWAP")
            if str(row.get("instId") or "") == CAMPAIGN_INSTRUMENT
        ),
        None,
    )
    if instrument is None:
        raise CampaignError(
            f"OKX Demo 当前账户不支持 {CAMPAIGN_INSTRUMENT}"
        )
    tick = _positive_decimal(instrument.get("tickSz"))
    if tick <= 0:
        raise CampaignError("OKX 黄金永续缺少有效 tickSz")

    reference = Decimal(str(latest_closed.close))
    entry = _align_to_tick(reference, tick, rounding=ROUND_HALF_UP)
    buffer = max(
        entry * CANARY_PRICE_BUFFER_RATIO,
        tick * CANARY_MIN_BUFFER_TICKS,
    )
    buffer = _align_to_tick(buffer, tick, rounding=ROUND_CEILING)
    if buffer <= 0:
        raise CampaignError("Demo 生命周期验收保护距离无效")

    # 验收期间会在确认 OCO 已建立后立即主动离场，因此这里用较宽的
    # 0.5% 保护距离避免把「保护触发」误当作「受控离场」。
    if CANARY_DIRECTION == "long":
        stop = entry - buffer
        tp1 = entry + buffer
        tp2 = entry + buffer * Decimal("2")
    else:
        stop = entry + buffer
        tp1 = entry - buffer
        tp2 = entry - buffer * Decimal("2")
    if min(entry, stop, tp1, tp2) <= 0:
        raise CampaignError("Demo 生命周期验收三价必须为正数")
    analysis_atr14: float | None = None
    execution_settings = getattr(
        getattr(runtime, "settings", None), "execution", None
    )
    if str(getattr(execution_settings, "entry_order_mode", "")) == "market":
        maximum_market_quantity = _positive_decimal(instrument.get("maxMktSz"))
        if maximum_market_quantity <= 0:
            raise CampaignError("Demo 市价入场缺少有效的 OKX maxMktSz")
        for _ in range(20):
            sizing = resolve_campaign_sizing(
                client,
                entry_price=entry,
                stop_loss_price=stop,
                side=CANARY_DIRECTION,
                risk_capital_cap_usdt=(
                    runtime.settings.execution.okx.risk_capital_cap_usdt
                ),
                risk_percent=runtime.settings.execution.okx.risk_percent,
            )
            if sizing.quantity <= maximum_market_quantity:
                break
            buffer = _align_to_tick(
                buffer + max(buffer / Decimal("2"), tick),
                tick,
                rounding=ROUND_CEILING,
            )
            if CANARY_DIRECTION == "long":
                stop = entry - buffer
                tp1 = entry + buffer
                tp2 = entry + buffer * Decimal("2")
            else:
                stop = entry + buffer
                tp1 = entry - buffer
                tp2 = entry - buffer * Decimal("2")
            if min(entry, stop, tp1, tp2) <= 0:
                raise CampaignError("Demo 市价入场风险距离导致三价非正数")
        else:
            raise CampaignError(
                "Demo 市价入场无法在保持10%风险公式时满足 OKX maxMktSz"
            )
    selected_modes = {
        str(getattr(execution_settings, "entry_order_mode", "")),
        str(getattr(execution_settings, "exit_order_mode", "")),
    }
    if "limit_with_slippage" in selected_modes:
        atr_bars = runtime.source.latest_snapshot(100)
        atr_frame = build_analysis_frame(
            atr_bars,
            min(50, max(20, len(atr_bars))),
            CAMPAIGN_SYMBOL,
            CAMPAIGN_TIMEFRAME,
        )
        if atr_frame is None or not atr_frame.indicators.atr14:
            raise CampaignError("Demo ATR 滑点验收缺少足够的主周期 ATR14 数据")
        value = atr_frame.indicators.atr14[0]
        if value is None or not float(value) > 0:
            raise CampaignError("Demo ATR 滑点验收的 ATR14 无效")
        analysis_atr14 = float(value)
    return entry, tp1, tp2, stop, latest_closed, analysis_atr14


def build_demo_canary_record(
    *,
    entry: Decimal,
    tp1: Decimal,
    tp2: Decimal,
    stop: Decimal,
    bar: KlineBar,
    entry_order_mode: str = CAMPAIGN_ENTRY_ORDER_MODE,
    analysis_atr14: float | None = None,
    now: datetime | None = None,
) -> AnalysisRecord:
    """生成可审计、明确非策略信号的耐久执行授权记录。"""
    current = (now or datetime.now().astimezone()).astimezone()
    timestamp_ms = int(current.timestamp() * 1000)
    direction_text = "做多" if CANARY_DIRECTION == "long" else "做空"
    canary_order_type = (
        "限价单"
        if entry_order_mode in {"limit", "limit_with_slippage"}
        else "市价单"
    )
    decision = {
        "order_direction": direction_text,
        "order_type": canary_order_type,
        "entry_price": str(entry),
        "take_profit_price": str(tp1),
        "take_profit_price_2": str(tp2),
        "stop_loss_price": str(stop),
        "trade_confidence": 100,
        "reason": "Demo 生命周期验收；不是 PA 策略信号",
    }
    return AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso=current.isoformat(),
            timestamp_local_ms=timestamp_ms,
            symbol=CAMPAIGN_SYMBOL,
            # 避免被 10m 策略运行器当成上一根真实策略分析记录。
            timeframe=CANARY_TIMEFRAME,
            data_source="okx",
            market_data_provenance="okx_public_ticker_controlled_canary",
            bar_count=1,
            ai_provider={"adapter": CANARY_ORIGIN, "model": "none"},
            decision_stance=CANARY_ORIGIN,
        ),
        kline_data=[
            {
                "seq": bar.seq,
                "ts_open": bar.ts_open,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "amount": bar.amount,
                "closed": bar.closed,
            }
        ],
        htf_text="Demo 生命周期验收，不生成策略观点。",
        analysis_atr14=analysis_atr14,
        stage1_messages=[
            {"role": "system", "content": CANARY_ORIGIN},
        ],
        stage1_response={"origin": CANARY_ORIGIN},
        stage1_diagnosis={"origin": CANARY_ORIGIN, "gate_result": "canary"},
        stage2_messages=[
            {"role": "system", "content": CANARY_ORIGIN},
        ],
        stage2_response={"origin": CANARY_ORIGIN},
        stage2_decision={"origin": CANARY_ORIGIN, "decision": decision},
        strategy_files_used=[CANARY_ORIGIN],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )


def build_controlled_demo_s_record(
    base_record: AnalysisRecord,
    *,
    client: OkxRestClient,
    risk_capital_cap_usdt: object | None = None,
    risk_percent: object | None = None,
    now: datetime | None = None,
) -> tuple[AnalysisRecord, CampaignSizing]:
    """基于真实 10m 快照构造可复现的 Demo-S 三价，不伪装成自然信号。"""
    if (
        base_record.meta.symbol != CAMPAIGN_SYMBOL
        or base_record.meta.timeframe != CAMPAIGN_TIMEFRAME
        or base_record.meta.data_source != "okx"
        or base_record.meta.market_data_provenance
        != "okx_5m_utc_pair_aggregation"
    ):
        raise CampaignError("Demo-S 基础记录不是受审计的 OKX 5m→10m 聚合记录")
    if not base_record.kline_data:
        raise CampaignError("Demo-S 基础记录缺少真实 10m K 线")
    atr = _positive_decimal(base_record.analysis_atr14)
    if atr <= 0:
        raise CampaignError("Demo-S 基础记录缺少有效的真实 10m ATR14")

    instrument = _campaign_instrument(client)
    tick = _positive_decimal(instrument.get("tickSz"))
    ticker = client.ticker(CAMPAIGN_INSTRUMENT)
    reference_price = _positive_decimal(ticker.get("last"))
    latest = base_record.kline_data[0]
    stage1_direction = str(
        base_record.stage1_diagnosis.get("direction") or ""
    ).strip().lower()
    direction = {
        "bullish": "long",
        "long": "long",
        "bearish": "short",
        "short": "short",
    }.get(stage1_direction)
    if direction is None:
        direction = "long" if float(latest["close"]) >= float(latest["open"]) else "short"
    executable_reference = _positive_decimal(
        (
            ticker.get("askPx")
            if direction == "long"
            else ticker.get("bidPx")
        )
        or ticker.get("last")
    )
    direction_text = "做多" if direction == "long" else "做空"
    level_field = "support_levels" if direction == "long" else "resistance_levels"
    levels = base_record.stage1_diagnosis.get(level_field)
    selected_level = (
        _positive_decimal(levels[0])
        if isinstance(levels, list) and levels
        else reference_price
    )
    selected_level_source = level_field
    entry = _align_to_tick(selected_level, tick, rounding=ROUND_HALF_UP)
    shifted_entry = _align_to_tick(
        entry + atr * CAMPAIGN_ENTRY_SLIPPAGE_ATR_MULTIPLE
        if direction == "long"
        else entry - atr * CAMPAIGN_ENTRY_SLIPPAGE_ATR_MULTIPLE,
        tick,
        rounding=ROUND_CEILING if direction == "long" else ROUND_FLOOR,
    )
    is_executable = (
        shifted_entry >= executable_reference
        if direction == "long"
        else shifted_entry <= executable_reference
    )
    if not is_executable:
        selected_level_source = (
            "okx_live_ask_effective_limit"
            if direction == "long"
            else "okx_live_bid_effective_limit"
        )
        entry = _align_to_tick(
            executable_reference
            - atr * CAMPAIGN_ENTRY_SLIPPAGE_ATR_MULTIPLE
            if direction == "long"
            else executable_reference
            + atr * CAMPAIGN_ENTRY_SLIPPAGE_ATR_MULTIPLE,
            tick,
            rounding=ROUND_FLOOR if direction == "long" else ROUND_CEILING,
        )
        shifted_entry = _align_to_tick(
            entry + atr * CAMPAIGN_ENTRY_SLIPPAGE_ATR_MULTIPLE
            if direction == "long"
            else entry - atr * CAMPAIGN_ENTRY_SLIPPAGE_ATR_MULTIPLE,
            tick,
            rounding=ROUND_CEILING if direction == "long" else ROUND_FLOOR,
        )
        if (
            direction == "long"
            and shifted_entry < executable_reference
        ) or (
            direction == "short"
            and shifted_entry > executable_reference
        ):
            raise CampaignError("Demo-S 无法生成可成交的 0.50 ATR 限价")

    distance = _align_to_tick(
        max(atr * Decimal("2"), tick),
        tick,
        rounding=ROUND_CEILING,
    )
    sizing: CampaignSizing | None = None
    stop = Decimal("0")
    for _ in range(20):
        stop = (
            _align_to_tick(entry - distance, tick, rounding=ROUND_FLOOR)
            if direction == "long"
            else _align_to_tick(entry + distance, tick, rounding=ROUND_CEILING)
        )
        if stop <= 0:
            raise CampaignError("Demo-S 风险距离导致止损价非正数")
        try:
            sizing = resolve_campaign_sizing(
                client,
                entry_price=shifted_entry,
                stop_loss_price=stop,
                side=direction,
                risk_capital_cap_usdt=risk_capital_cap_usdt,
                risk_percent=risk_percent,
            )
            break
        except CampaignRiskBlocked as exc:
            if exc.code != "max_size_exceeded":
                raise
            distance = _align_to_tick(
                distance + atr,
                tick,
                rounding=ROUND_CEILING,
            )
    if sizing is None:
        raise CampaignError("Demo-S 无法在保持 10% 风险公式时满足真实最大可开张数")

    if direction == "long":
        tp1 = _align_to_tick(entry + distance * 2, tick, rounding=ROUND_CEILING)
        tp2 = _align_to_tick(entry + distance * 3, tick, rounding=ROUND_CEILING)
    else:
        tp1 = _align_to_tick(entry - distance * 2, tick, rounding=ROUND_FLOOR)
        tp2 = _align_to_tick(entry - distance * 3, tick, rounding=ROUND_FLOOR)
    if min(entry, stop, tp1, tp2) <= 0:
        raise CampaignError("Demo-S 三价必须全部为正数")

    current = (now or datetime.now().astimezone()).astimezone()
    timestamp_ms = int(current.timestamp() * 1000)
    decision = {
        "order_direction": direction_text,
        "order_type": "限价单",
        "entry_price": str(entry),
        "take_profit_price": str(tp1),
        "take_profit_price_2": str(tp2),
        "stop_loss_price": str(stop),
        "trade_confidence": 20,
        "reason": (
            "WO-EXEC-03 Demo-S controlled reproducible input；"
            f"方向继承真实阶段一 {stage1_direction or direction}，"
            f"入场参考 {selected_level_source}；"
            "基于真实 OKX 5m→10m 已收盘快照与 ATR14"
        ),
    }
    controlled_stage1 = {
        "input_mode": "controlled_reproducible",
        "gate_result": "proceed",
        "direction": "bullish" if direction == "long" else "bearish",
        "diagnosis_confidence": 20,
        "entry_setup": (
            f"WO-EXEC-03 受控 Demo-S：{direction_text}，"
            f"signal_entry={entry}，effective_limit={shifted_entry}，"
            f"stop={stop}，TP1={tp1}，TP2={tp2}"
        ),
        "risk_warning": (
            "仅限 OKX Demo；真实 10m ATR14="
            f"{atr}；风险数量={sizing.quantity}；"
            f"maxBuy={sizing.max_buy}；maxSell={sizing.max_sell}"
        ),
        "controlled_basis": {
            "closed_bar_ts_open": latest["ts_open"],
            "market_data_provenance": (
                "okx_public_5m_utc_pair_aggregation_controlled_reproducible"
            ),
            "analysis_atr14": str(atr),
            "reference_price": str(reference_price),
            "executable_reference_price": str(executable_reference),
            "effective_limit_price": str(shifted_entry),
            "entry_level_source": selected_level_source,
        },
        "trend_context": {
            "primary_direction": "bullish" if direction == "long" else "bearish",
            "trading_direction": "bullish" if direction == "long" else "bearish",
            "conflict": False,
        },
        "bar_analysis": {
            "last_closed_bar": "K1",
            "entry_setup_type": "controlled_reproducible",
            "follow_through": "controlled_test",
        },
    }
    record = base_record.model_copy(deep=True)
    record.meta = record.meta.model_copy(
        update={
            "timestamp_local_iso": current.isoformat(),
            "timestamp_local_ms": timestamp_ms,
            "market_data_provenance": (
                "okx_public_5m_utc_pair_aggregation_controlled_reproducible"
            ),
            "ai_provider": {
                **record.meta.ai_provider,
                "controlled_input": CONTROLLED_DEMO_S_ORIGIN,
            },
        }
    )
    record.stage1_response = {
        "origin": CONTROLLED_DEMO_S_ORIGIN,
        "base_stage1_response": base_record.stage1_response,
        "base_stage1_diagnosis": base_record.stage1_diagnosis,
    }
    record.stage1_diagnosis = controlled_stage1
    record.stage2_response = {
        "origin": CONTROLLED_DEMO_S_ORIGIN,
        "base_stage2_response": base_record.stage2_response,
        "base_stage2_decision": base_record.stage2_decision,
    }
    record.stage2_decision = {
        "origin": CONTROLLED_DEMO_S_ORIGIN,
        "decision": decision,
    }
    _attach_campaign_sizing(record, sizing)
    record.strategy_files_used = list(
        dict.fromkeys(
            [*record.strategy_files_used, CONTROLLED_DEMO_S_ORIGIN]
        )
    )
    return record, sizing


def find_latest_natural_campaign_record(
    campaign_id: str,
) -> AnalysisRecord | None:
    """只返回当前 Campaign 自己的真实自然分析，跳过受控或外部记录。"""
    for path in list_record_paths(RECORDS_PENDING_DIR):
        record = load_record(path)
        if record is None or record.exception is not None:
            continue
        if (
            record.meta.symbol == CAMPAIGN_SYMBOL
            and record.meta.timeframe == CAMPAIGN_TIMEFRAME
            and record.meta.campaign_id == campaign_id
            and record.meta.data_source == "okx"
            and record.meta.market_data_provenance
            == "okx_5m_utc_pair_aggregation"
            and bool(record.stage1_diagnosis)
            and bool(record.stage2_decision)
            and bool(record.kline_data)
        ):
            return record
    return None


def _wait_for_execution_state(
    service: ExecutionController,
    execution_id: str,
    *,
    accepted: set[ExecutionState],
    timeout: float = CANARY_TIMEOUT_SECONDS,
):
    """等待 Worker 的只读对账把执行推进到预期状态。"""
    deadline = time.monotonic() + max(1.0, timeout)
    after = service.latest_successful_reconcile_at()
    while time.monotonic() < deadline:
        execution = service.get_execution(execution_id)
        if execution is None:
            raise CampaignError("Demo 生命周期验收 execution 在账本中消失")
        if execution.state in accepted:
            return execution
        if bool(getattr(execution, "needs_attention", False)) or execution.state in {
            ExecutionState.BLOCKED,
            ExecutionState.CANCELED,
            ExecutionState.REJECTED,
            ExecutionState.UNKNOWN,
            ExecutionState.ERROR,
        }:
            raise CampaignError(
                "Demo 生命周期验收未达到预期状态: "
                f"{execution.state.value} ({execution.state_reason or execution.last_error})"
            )
        remaining = max(0.1, deadline - time.monotonic())
        try:
            after = service.wait_for_reconcile(
                after=after,
                timeout=min(10.0, remaining),
            )
        except TimeoutError:
            continue
        except LiveTradingDisabled:
            # Worker 会在单次对账异常时短暂进入 needs_attention，并在下一次
            # 成功对账后自行恢复。执行记录仍是普通活动态时继续等到账本终态；
            # UNKNOWN/ERROR/needs_attention 等真实风险状态已在上方硬阻断。
            time.sleep(min(0.2, remaining))
            continue
    raise CampaignError("Demo 生命周期验收等待 Worker 对账超时")


def _assert_canary_protection(execution) -> None:
    targets = execution.broker_state.get("protection_targets")
    if not isinstance(targets, list) or not targets:
        raise CampaignError("Demo 生命周期验收未发现原生保护单")
    if not all(str(target.get("algo_id") or "") for target in targets):
        raise CampaignError("Demo 生命周期验收保护单缺少 OKX algoId")


def _attempt_canary_cleanup(
    service: ExecutionController,
    execution_id: str,
    *,
    timeout: float = CANARY_CLEANUP_TIMEOUT_SECONDS,
) -> object:
    """持续跟随撤单竞态；一旦出现成交仓位，只发一次主动减险。"""
    deadline = time.monotonic() + max(0.0, timeout)
    cancel_requested = False
    exit_requested = False
    safe_terminal_states = {
        ExecutionState.CLOSED,
        ExecutionState.CANCELED,
        ExecutionState.BLOCKED,
        ExecutionState.REJECTED,
    }
    while time.monotonic() < deadline:
        execution = service.get_execution(execution_id)
        if execution is None:
            raise CampaignError("Demo 生命周期验收异常收口时执行记录丢失")
        if execution.state in safe_terminal_states:
            return execution
        if execution.state in {ExecutionState.UNKNOWN, ExecutionState.ERROR}:
            raise CampaignError(
                "Demo 生命周期验收异常收口进入不安全状态: "
                f"{execution.state.value}"
            )
        if execution.state is ExecutionState.READY:
            return service.expire_unsubmitted(
                execution_id,
                reason="Demo 生命周期验收异常发生在提交前",
            )
        command = None
        if (
            execution.state is ExecutionState.ENTRY_PENDING
            and not cancel_requested
        ):
            cancel_requested = True
            command = service.cancel_entry(execution_id)
        elif (
            execution.state
            in {
                ExecutionState.PARTIALLY_FILLED,
                ExecutionState.PROTECTING,
                ExecutionState.OPEN,
            }
            and not exit_requested
        ):
            exit_requested = True
            command = service.request_exit(
                execution_id,
                reason="Demo 生命周期验收异常收口",
            )
        if command is not None:
            try:
                service.wait_for_command(command.id, timeout=30.0)
            except (LiveTradingDisabled, TimeoutError):
                pass
            continue
        previous = service.latest_successful_reconcile_at()
        try:
            service.wait_for_reconcile(
                after=previous,
                timeout=min(5.0, max(0.1, deadline - time.monotonic())),
            )
        except (LiveTradingDisabled, TimeoutError):
            time.sleep(0.2)
    execution = service.get_execution(execution_id)
    if execution is None:
        raise CampaignError("Demo 生命周期验收异常收口超时且执行记录丢失")
    if execution.state not in safe_terminal_states:
        raise CampaignError(
            "Demo 生命周期验收异常收口未达到安全终态: "
            f"{execution.state.value}"
        )
    return execution


def run_demo_lifecycle_canary(
    *,
    entry_order_mode: str | None = None,
    exit_order_mode: str | None = None,
    entry_slippage_atr_multiple: Decimal | float | str | None = None,
    exit_slippage_atr_multiple: Decimal | float | str | None = None,
) -> dict[str, str]:
    """通过现有 Controller/Worker 完成一次明确标记的 Demo 闭环验收。"""
    okx_demo_private_preflight()
    runtime: CampaignRuntime | None = None
    execution_id = ""
    try:
        runtime = build_runtime(
            entry_order_mode=entry_order_mode,
            exit_order_mode=exit_order_mode,
            entry_slippage_atr_multiple=entry_slippage_atr_multiple,
            exit_slippage_atr_multiple=exit_slippage_atr_multiple,
        )
        validate_campaign_settings(runtime.settings)
        service = runtime.execution_service
        service.start_monitoring()
        service.wait_for_worker(timeout=10.0)
        if service.list_active():
            raise CampaignError("存在活动执行，Demo 生命周期验收不能与其并行")
        service.arm(service.arm_confirmation_text())

        entry, tp1, tp2, stop, bar, analysis_atr14 = _canary_price_triplet(runtime)
        record = build_demo_canary_record(
            entry=entry,
            tp1=tp1,
            tp2=tp2,
            stop=stop,
            bar=bar,
            entry_order_mode=runtime.settings.execution.entry_order_mode,
            analysis_atr14=analysis_atr14,
        )
        sizing = runtime.sizing_resolver(record)
        _attach_campaign_sizing(record, sizing)
        runtime.writer.save_full_durable(record)
        _apply_campaign_sizing(runtime, sizing)
        execution = service.prepare_analysis(record)
        execution_id = execution.id
        try:
            _require_fresh_campaign_sizing(runtime, record, sizing)
        except CampaignRiskBlocked as exc:
            service.expire_unsubmitted(
                execution.id,
                reason="USDT 风险快照变化，旧计划禁止提交",
            )
            raise CampaignError(str(exc)) from exc
        command = service.submit(execution.id)
        result = service.wait_for_command(command.id, timeout=30.0)
        if result.status is not WorkerCommandStatus.SUCCEEDED:
            raise CampaignError(
                "Demo 生命周期验收入场命令未成功: "
                f"{result.failure_code or result.status.value}"
            )

        opened = _wait_for_execution_state(
            service,
            execution.id,
            accepted={ExecutionState.OPEN},
        )
        _assert_canary_protection(opened)
        exit_command = service.request_exit(
            execution.id,
            reason="Demo 生命周期验收受控离场",
        )
        exit_result = service.wait_for_command(exit_command.id, timeout=30.0)
        if exit_result.status is not WorkerCommandStatus.SUCCEEDED:
            raise CampaignError(
                "Demo 生命周期验收离场命令未成功: "
                f"{exit_result.failure_code or exit_result.status.value}"
            )
        closed = _wait_for_execution_state(
            service,
            execution.id,
            accepted={ExecutionState.CLOSED},
        )
        return {
            "execution_id": closed.id,
            "state": closed.state.value,
            "origin": CANARY_ORIGIN,
        }
    except Exception as exc:
        cleanup_error: Exception | None = None
        if runtime is not None and execution_id:
            try:
                _attempt_canary_cleanup(runtime.execution_service, execution_id)
            except Exception as cleanup_exc:
                cleanup_error = cleanup_exc
                logger.exception("Demo 生命周期验收异常收口未达到安全终态")
        if cleanup_error is not None:
            raise CampaignError(
                f"Demo 生命周期验收失败: {exc}; "
                f"异常收口失败: {cleanup_error}"
            ) from cleanup_error
        raise
    finally:
        if runtime is not None:
            runtime.execution_service.stop_monitoring()
            runtime.source.disconnect()


def run_controlled_demo_s() -> dict[str, str]:
    """用真实 10m 记录和确定性生产执行链完成 Demo-S。"""
    preflight = okx_demo_private_preflight()
    state = CampaignStateStore().load()
    if state is None or state.status != "active":
        raise CampaignError("Demo-S 要求 10m Campaign 正在运行")
    base_record = find_latest_natural_campaign_record(state.campaign_id)
    if base_record is None:
        raise CampaignError("Demo-S 尚无真实 10m PA 记录")
    bar_ms = int(ts_open_to_ms(base_record.kline_data[0]["ts_open"]))
    if state.last_completed_bar_ms != bar_ms:
        raise CampaignError("Demo-S 只能使用 Campaign 最新完成的真实 10m 记录")

    runtime: CampaignRuntime | None = None
    execution_id = ""
    try:
        runtime = build_runtime(
            entry_order_mode="limit_with_slippage",
            exit_order_mode="limit_with_slippage",
            entry_slippage_atr_multiple=Decimal("0.50"),
            exit_slippage_atr_multiple=Decimal("0.50"),
        )
        service = runtime.execution_service
        service.start_monitoring()
        service.wait_for_worker(timeout=10.0)
        if service.list_active():
            raise CampaignError("存在活动执行，Demo-S 禁止新增风险")
        service.arm(service.arm_confirmation_text())

        client = OkxRestClient(
            load_okx_credentials("demo"),
            base_url=CAMPAIGN_OKX_API_BASE_URL,
            simulated=True,
        )
        record, sizing = build_controlled_demo_s_record(
            base_record,
            client=client,
            risk_capital_cap_usdt=(
                runtime.settings.execution.okx.risk_capital_cap_usdt
            ),
            risk_percent=runtime.settings.execution.okx.risk_percent,
        )
        runtime.writer.save_full_durable(record)
        _apply_campaign_sizing(runtime, sizing)

        execution = service.prepare_analysis(record)
        execution_id = execution.id
        state = state.model_copy(
            update={
                "execution_ids": list(
                    dict.fromkeys([*state.execution_ids, execution.id])
                ),
                "last_execution_id": execution.id,
                "executions_prepared": state.executions_prepared + 1,
                "last_plan_result": f"execution:{execution.state.value}",
                "last_error": "",
                "updated_at": _utc_now().isoformat(),
            }
        )
        CampaignStateStore().save(state)
        try:
            _require_fresh_campaign_sizing(runtime, record, sizing)
        except CampaignRiskBlocked as exc:
            service.expire_unsubmitted(
                execution.id,
                reason="USDT 风险快照变化，旧计划禁止提交",
            )
            raise CampaignError(str(exc)) from exc
        command = service.submit(execution.id)
        result = service.wait_for_command(command.id, timeout=30.0)
        if result.status is not WorkerCommandStatus.SUCCEEDED:
            raise CampaignError(
                f"Demo-S 入场命令失败: {result.failure_code or result.status.value}"
            )
        opened = _wait_for_execution_state(
            service,
            execution.id,
            accepted={ExecutionState.OPEN},
            timeout=float(runtime.settings.execution.entry_timeout_seconds) + 60,
        )
        _assert_canary_protection(opened)
        exit_command = service.request_exit(
            execution.id,
            reason="WO-EXEC-03 Demo-S 受控主动离场",
        )
        exit_result = service.wait_for_command(exit_command.id, timeout=30.0)
        if exit_result.status is not WorkerCommandStatus.SUCCEEDED:
            raise CampaignError(
                f"Demo-S 离场命令失败: {exit_result.failure_code or exit_result.status.value}"
            )
        closed = _wait_for_execution_state(
            service,
            execution.id,
            accepted={ExecutionState.CLOSED},
            timeout=float(runtime.settings.execution.entry_timeout_seconds) + 60,
        )
        state = state.model_copy(
            update={
                "last_plan_result": f"execution:{closed.state.value}",
                "last_error": "",
                "updated_at": _utc_now().isoformat(),
            }
        )
        CampaignStateStore().save(state)
        return {
            "execution_id": closed.id,
            "state": closed.state.value,
            "origin": CONTROLLED_DEMO_S_ORIGIN,
            "provenance": record.meta.market_data_provenance,
            "authorization_mode": "deterministic_script",
            "risk_sized_quantity": str(sizing.quantity),
            "quantity": str(closed.plan.quantity),
            "equity_usdt": str(sizing.equity_usdt),
            "max_buy": str(sizing.max_buy),
            "max_sell": str(sizing.max_sell),
            "entry_price": str(record.stage2_decision["decision"]["entry_price"]),
            "stop_price": str(record.stage2_decision["decision"]["stop_loss_price"]),
            "atr14": str(record.analysis_atr14),
            "preflight_simulated": str(preflight["simulated"]).lower(),
        }
    except Exception as exc:
        cleanup_error: Exception | None = None
        if runtime is not None and execution_id:
            try:
                latest_execution = _attempt_canary_cleanup(
                    runtime.execution_service,
                    execution_id,
                )
            except Exception as cleanup_exc:
                cleanup_error = cleanup_exc
                logger.exception("Demo-S 异常收口未达到安全终态")
                latest_execution = runtime.execution_service.get_execution(
                    execution_id
                )
            latest_state = CampaignStateStore().load() or state
            if latest_execution is not None and latest_state is not None:
                CampaignStateStore().save(
                    latest_state.model_copy(
                        update={
                            "last_plan_result": (
                                f"execution:{latest_execution.state.value}"
                            ),
                            "last_error": f"Demo-S: {exc}",
                            "updated_at": _utc_now().isoformat(),
                        }
                    )
                )
        if cleanup_error is not None:
            raise CampaignError(
                f"Demo-S 失败: {exc}; 异常收口失败: {cleanup_error}"
            ) from cleanup_error
        raise
    finally:
        if runtime is not None:
            runtime.execution_service.stop_monitoring()
            runtime.source.disconnect()


def okx_demo_private_preflight(
    settings: Any | None = None,
) -> dict[str, Any]:
    """用模拟标头完成账户、规格、容量和行情只读验证。

    这里没有 PA 的入场和止损，所以不计算可下单数量；缺少止损时风险引擎
    必须阻断，而不是偷偷退回旧固定数量。
    """
    settings = settings or load_settings(SETTINGS_JSON_PATH)
    risk_route = settings.execution.okx
    client = OkxRestClient(
        load_okx_credentials("demo"),
        base_url=CAMPAIGN_OKX_API_BASE_URL,
        simulated=True,
    )
    clock_offset_ms = client.sync_server_time()
    account_config = client.account_config()
    if not all(
        str(account_config.get(field) or "").strip()
        for field in ("uid", "mainUid", "type")
    ):
        raise CampaignError("OKX Demo account/config 缺少账户身份字段")
    if str(account_config.get("posMode") or "") != "net_mode":
        raise CampaignError("OKX Demo 首发风险定仓只允许 net_mode 净持仓")

    instrument = _campaign_instrument(client)
    minimum = _positive_decimal(instrument.get("minSz"))
    lot = _positive_decimal(instrument.get("lotSz"))
    contract_value = _positive_decimal(instrument.get("ctVal"))
    contract_multiplier = _positive_decimal(instrument.get("ctMult"))
    if min(minimum, lot, contract_value, contract_multiplier) <= 0:
        raise CampaignError("OKX 黄金永续缺少有效的合约规格")
    equity_usdt = _demo_usdt_equity(client.balance())
    effective_risk_capital_usdt = min(
        equity_usdt,
        risk_route.risk_capital_cap_usdt,
    )
    ticker = client.ticker(CAMPAIGN_INSTRUMENT)
    reference_price = _positive_decimal(ticker.get("last"))
    if reference_price <= 0:
        raise CampaignError("OKX 黄金永续当前市价无效")
    maximum = client.max_order_size(
        instrument=CAMPAIGN_INSTRUMENT,
        trade_mode=CAMPAIGN_MARGIN_MODE,
    )
    max_buy = _positive_decimal(maximum.get("maxBuy"))
    max_sell = _positive_decimal(maximum.get("maxSell"))
    if max_buy <= 0 or max_sell <= 0:
        raise CampaignError("OKX Demo 当前最大可开张数无效")

    balances = client.balance()
    positions = client.positions(instrument=CAMPAIGN_INSTRUMENT)
    leverage = client.leverage_info(
        instrument=CAMPAIGN_INSTRUMENT,
        margin_mode=CAMPAIGN_MARGIN_MODE,
    )
    market_rows_5m = client.candles(
        instrument=CAMPAIGN_INSTRUMENT,
        bar="5m",
        limit=4,
    )
    market_rows = aggregate_okx_five_minute_rows(market_rows_5m, limit=1)
    return {
        "simulated": client.simulated,
        "account_identity_present": True,
        "instrument": CAMPAIGN_INSTRUMENT,
        "instrument_state": "live",
        "sizing_mode": risk_route.sizing_mode,
        "fixed_quantity": (
            str(risk_route.quantity)
            if risk_route.sizing_mode == "fixed_quantity"
            else ""
        ),
        "equity_basis": CAMPAIGN_RISK_EQUITY_BASIS,
        "equity_usdt": str(equity_usdt),
        "risk_capital_cap_usdt": str(risk_route.risk_capital_cap_usdt),
        "effective_risk_capital_usdt": str(
            effective_risk_capital_usdt
        ),
        "risk_percent": str(risk_route.risk_percent),
        "risk_budget_usdt": (
            str(effective_risk_capital_usdt * risk_route.risk_percent)
            if risk_route.sizing_mode == "risk_budget"
            else "requires_entry_and_stop"
        ),
        "maximum_leverage": str(risk_route.maximum_leverage),
        "fee_rate": str(CAMPAIGN_FEE_RATE),
        "slippage_rate": str(CAMPAIGN_SLIPPAGE_RATE),
        "reference_price_usdt": str(reference_price),
        "contract_notional_usdt": str(
            contract_value * contract_multiplier * reference_price
        ),
        "minimum_quantity": str(minimum),
        "quantity_step": str(lot),
        "max_buy": str(max_buy),
        "max_sell": str(max_sell),
        "risk_quantity": "requires_entry_and_stop",
        "balance_rows": len(balances),
        "existing_position_rows": len(positions),
        "leverage_rows": len(leverage),
        "market_candle_rows": len(market_rows),
        "clock_offset_ms": clock_offset_ms,
    }


class OkxCampaignSource:
    """只读取本实验执行产品自己的 OKX 公共 K 线。"""

    def __init__(self, client: OkxRestClient | None = None) -> None:
        self._client = client or OkxRestClient(
            load_okx_credentials("demo"),
            base_url=CAMPAIGN_OKX_API_BASE_URL,
            simulated=True,
        )
        self._subscribed = False
        self._price_tick: str | None = None

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        self._subscribed = False
        self._price_tick = None

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if symbol != CAMPAIGN_INSTRUMENT or timeframe != CAMPAIGN_TIMEFRAME:
            raise CampaignError("OKX Demo 快速运行只允许自身执行产品的 10m K 线")
        self._subscribed = True

    def price_tick(self) -> str:
        """读取并缓存 OKX 公共品种元数据中的真实 tickSz。"""
        if not self._subscribed:
            raise CampaignError("OKX Demo 行情源尚未订阅")
        if self._price_tick is not None:
            return self._price_tick
        try:
            rows = self._client.public_instruments(
                "SWAP",
                instrument=CAMPAIGN_INSTRUMENT,
            )
        except (BrokerApiError, BrokerTransportError) as exc:
            raise DataSourceTransientError(
                f"OKX 品种 tickSz 暂时不可用: {exc}"
            ) from exc
        instrument = next(
            (
                row
                for row in rows
                if str(row.get("instId") or "").strip()
                == CAMPAIGN_INSTRUMENT
            ),
            None,
        )
        if instrument is None:
            raise CampaignError(
                f"OKX 公共品种元数据缺少 {CAMPAIGN_INSTRUMENT}"
            )
        if str(instrument.get("state") or "").strip().lower() != "live":
            raise CampaignError(
                f"{CAMPAIGN_INSTRUMENT} 公共品种状态不是 live"
            )
        tick = _positive_decimal(instrument.get("tickSz"))
        if tick <= 0:
            raise CampaignError("OKX 黄金永续缺少有效 tickSz")
        self._price_tick = format(tick, "f")
        return self._price_tick

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        return self.latest_snapshot_for_timeframe(CAMPAIGN_TIMEFRAME, n)

    def latest_snapshot_for_timeframe(
        self,
        timeframe: str,
        n: int,
    ) -> list[KlineBar]:
        """Read another OKX public timeframe for background-only context."""
        if not self._subscribed:
            raise CampaignError("OKX Demo 行情源尚未订阅")
        if timeframe not in {CAMPAIGN_TIMEFRAME, *CAMPAIGN_HIGHER_TIMEFRAMES}:
            raise CampaignError(f"OKX Demo 不允许读取实验外周期：{timeframe}")
        try:
            if timeframe == CAMPAIGN_TIMEFRAME and n > 150:
                raise DataSourceTransientError(
                    "OKX 10m 由真实 5m 两两聚合，单次最多请求 150 根 10m"
                )
            raw_limit = (
                min(300, n * 2 + 2)
                if timeframe == CAMPAIGN_TIMEFRAME
                else n
            )
            rows = self._client.candles(
                instrument=CAMPAIGN_INSTRUMENT,
                bar=_CAMPAIGN_TIMEFRAME_TO_OKX_BAR[timeframe],
                limit=raw_limit,
            )
        except (BrokerApiError, BrokerTransportError) as exc:
            raise DataSourceTransientError(f"OKX K 线暂时不可用: {exc}") from exc
        if timeframe == CAMPAIGN_TIMEFRAME:
            rows = aggregate_okx_five_minute_rows(rows, limit=n)

        bars: list[KlineBar] = []
        previous_ts: int | None = None
        closed_seq = 0
        forming_count = 0
        for row in rows:
            try:
                timestamp = int(row[0])
                open_price = Decimal(row[1])
                high_price = Decimal(row[2])
                low_price = Decimal(row[3])
                close_price = Decimal(row[4])
                volume = Decimal(row[5])
                amount = Decimal(row[7])
                confirm = row[8]
            except (IndexError, InvalidOperation, TypeError, ValueError) as exc:
                raise CampaignError(f"OKX {timeframe} K 线字段无法解析") from exc
            numeric_values = (
                open_price,
                high_price,
                low_price,
                close_price,
                volume,
                amount,
            )
            if not all(value.is_finite() for value in numeric_values):
                raise CampaignError(f"OKX {timeframe} K 线包含非有限数值")
            if min(open_price, high_price, low_price, close_price) <= 0:
                raise CampaignError(f"OKX {timeframe} K 线价格必须为正数")
            if volume < 0 or amount < 0:
                raise CampaignError(f"OKX {timeframe} K 线成交量不能为负数")
            if high_price < max(open_price, close_price):
                raise CampaignError(f"OKX {timeframe} K 线最高价低于开盘价或收盘价")
            if low_price > min(open_price, close_price):
                raise CampaignError(f"OKX {timeframe} K 线最低价高于开盘价或收盘价")
            if previous_ts is not None and timestamp >= previous_ts:
                raise CampaignError(f"OKX {timeframe} K 线时间必须严格从新到旧")
            previous_ts = timestamp
            if confirm not in {"0", "1"}:
                raise CampaignError(f"OKX {timeframe} K 线收盘标记无效")
            closed = confirm == "1"
            if closed:
                closed_seq += 1
                seq = closed_seq
            else:
                forming_count += 1
                if forming_count > 1:
                    raise CampaignError(f"OKX {timeframe} K 线包含多根未收盘数据")
                seq = 0
            bars.append(
                KlineBar(
                    seq=seq,
                    ts_open=timestamp,
                    open=float(open_price),
                    high=float(high_price),
                    low=float(low_price),
                    close=float(close_price),
                    volume=float(volume),
                    amount=float(amount),
                    closed=closed,
                )
            )
        return bars


@dataclass
class CampaignRuntime:
    settings: Any
    source: OkxCampaignSource
    writer: PendingWriter
    orchestrator: TwoStageOrchestrator
    execution_service: ExecutionController
    # 普通交易不依赖逐笔 AI 审批；外部只读监控 Agent 负责异常反馈。
    # 保留可选字段仅兼容历史测试构造，不参与生产交易路径。
    supervisor: Any | None = None
    sizing_resolver: Callable[[AnalysisRecord], CampaignSizing] = (
        resolve_record_campaign_sizing
    )
    leverage_resolver: Callable[
        [AnalysisRecord, str],
        CampaignLeverageCandidate,
    ] = resolve_record_campaign_leverage


def _apply_campaign_sizing(
    runtime: CampaignRuntime,
    sizing: CampaignSizing,
) -> None:
    """把本次风险定仓写入计划配置，并让新增风险租约同步到新指纹。"""
    service = runtime.execution_service
    was_armed = service.is_armed
    runtime.settings.execution.okx.quantity = str(sizing.quantity)
    validate_campaign_settings(runtime.settings)
    if was_armed:
        service.disarm()
        service.arm(service.arm_confirmation_text())


_CAMPAIGN_SIZING_FINGERPRINT_FIELDS = (
    "equity_basis",
    "risk_capital_cap_usdt",
    "effective_risk_capital_usdt",
    "risk_percent",
    "risk_budget_usdt",
    "risk_used_usdt",
    "quantity",
    "reference_price_usdt",
    "stop_distance_usdt",
    "worst_case_loss_per_contract_usdt",
    "fee_per_contract_usdt",
    "slippage_per_contract_usdt",
)


def _campaign_sizing_fingerprint(sizing: CampaignSizing) -> tuple[str, ...]:
    """返回必须与已生成计划保持一致的风险定仓字段。"""
    return tuple(
        str(getattr(sizing, field, ""))
        for field in _CAMPAIGN_SIZING_FINGERPRINT_FIELDS
    )


def _require_fresh_campaign_sizing(
    runtime: CampaignRuntime,
    record: AnalysisRecord,
    initial: CampaignSizing,
) -> CampaignSizing:
    """提交前重算风险；仅真正改变授权风险数量的输入才让计划失效。"""
    current = runtime.sizing_resolver(record)
    if _campaign_sizing_fingerprint(current) != _campaign_sizing_fingerprint(initial):
        raise CampaignRiskBlocked(
            "stale_risk_sizing",
            "USDT 风险基数或定仓输入在计划生成后发生变化，旧计划禁止提交",
        )
    return current


def build_runtime(
    *,
    base_settings: Any | None = None,
    resolved_quantity: Decimal | str | None = None,
    entry_order_mode: str | None = None,
    exit_order_mode: str | None = None,
    entry_slippage_atr_multiple: Decimal | float | str | None = None,
    exit_slippage_atr_multiple: Decimal | float | str | None = None,
) -> CampaignRuntime:
    base_settings = base_settings or load_settings(SETTINGS_JSON_PATH)
    # 分析结果尚未产生时没有 entry/stop，不能预先猜数量；只用合法的内存
    # 启动值，真正创建计划前必须由风险引擎覆盖。
    sizing_quantity = resolved_quantity or CAMPAIGN_BOOTSTRAP_QUANTITY
    settings = build_campaign_settings(
        base_settings,
        quantity=sizing_quantity,
        entry_order_mode=entry_order_mode,
        exit_order_mode=exit_order_mode,
        entry_slippage_atr_multiple=entry_slippage_atr_multiple,
        exit_slippage_atr_multiple=exit_slippage_atr_multiple,
    )
    pa_primary_id, pa_primary_profile = resolve_verified_profile(
        settings,
        settings.ai_roles.pa_primary_profile_id,
        "PA 主模型",
    )
    pa_backup_id = str(settings.ai_roles.pa_backup_profile_id or "").strip()
    if pa_backup_id:
        if pa_backup_id == pa_primary_id:
            raise CampaignError("PA 主模型和备用模型不能绑定同一档案")
        resolve_verified_profile(settings, pa_backup_id, "PA 备用模型")
    settings.provider = pa_primary_profile.provider.model_copy(deep=True)
    settings.provider.reasoning_effort = "medium"
    configure_logging(api_key=settings.provider.api_key)

    exp_reader = ExperienceReader(experience_dir=EXPERIENCE_DIR, logger=logger)
    writer = PendingWriter(
        pending_dir=RECORDS_PENDING_DIR,
        logger=logger,
        api_key=settings.provider.api_key,
    )
    orchestrator = TwoStageOrchestrator(
        client=create_ai_client(settings.provider, logger_=logger),
        assembler=PromptAssembler(
            prompt_dir=PROMPT_DIR,
            experience_reader=exp_reader,
            prompt_settings=settings.prompt,
            stage2_execution_guidance=CAMPAIGN_FAST_EXECUTION_GUIDANCE,
        ),
        router=route_strategy_files,
        validator=JsonValidator(settings),
        pending_writer=writer,
        exp_reader=exp_reader,
        settings=settings,
    )
    service = ExecutionController(
        settings=settings,
        pending_writer=writer,
        logger=logger,
    )
    source = OkxCampaignSource()
    source.connect()
    source.subscribe(CAMPAIGN_SYMBOL, CAMPAIGN_TIMEFRAME)
    risk_route = settings.execution.okx

    def _sizing_resolver(record: AnalysisRecord) -> CampaignSizing:
        return resolve_record_campaign_sizing(
            record,
            risk_capital_cap_usdt=risk_route.risk_capital_cap_usdt,
            risk_percent=risk_route.risk_percent,
            sizing_mode=risk_route.sizing_mode,
            fixed_quantity=risk_route.quantity,
        )

    def _leverage_resolver(
        record: AnalysisRecord,
        analysis_digest: str,
    ) -> CampaignLeverageCandidate:
        return resolve_record_campaign_leverage(
            record,
            analysis_digest,
            risk_capital_cap_usdt=risk_route.risk_capital_cap_usdt,
            risk_percent=risk_route.risk_percent,
            maximum_leverage=risk_route.maximum_leverage,
            sizing_mode=risk_route.sizing_mode,
            fixed_quantity=risk_route.quantity,
        )

    return CampaignRuntime(
        settings=settings,
        source=source,
        writer=writer,
        orchestrator=orchestrator,
        execution_service=service,
        sizing_resolver=_sizing_resolver,
        leverage_resolver=_leverage_resolver,
    )


class OkxDemoCampaign:
    """运行分析、执行、监控和到期收口。"""

    def __init__(
        self,
        runtime: CampaignRuntime,
        state_store: CampaignStateStore,
        state: CampaignState,
        *,
        poll_seconds: float = CAMPAIGN_POLL_SECONDS,
        closeout_seconds: float = CAMPAIGN_CLOSEOUT_SECONDS,
    ) -> None:
        expected_fingerprint = campaign_config_fingerprint(runtime.settings)
        expected_frozen = _campaign_frozen_risk_values(runtime.settings)
        actual_frozen = {
            field: getattr(state, field)
            for field in expected_frozen
        }
        if (
            state.config_fingerprint != expected_fingerprint
            or actual_frozen != expected_frozen
        ):
            raise CampaignError(
                "Campaign 运行时设置与状态文件冻结参数不一致，禁止启动"
            )
        self.runtime = runtime
        self.state_store = state_store
        self.state = state
        self.poll_seconds = float(poll_seconds)
        self.closeout_seconds = float(closeout_seconds)

    def _save_state(self, **updates: Any) -> None:
        self.state = self.state.model_copy(update=updates)
        self.state_store.save(self.state)

    def _recover_transient_risk_stop_for_bar(self, bar_ms: int) -> bool:
        """每根新 K 线最多复核一次明确允许的临时只读故障。"""

        service = self.runtime.execution_service
        attempted_bar = self.state.risk_recovery_bar_ms
        if attempted_bar is not None and attempted_bar != bar_ms:
            if (
                self.state.last_completed_bar_ms is not None
                and attempted_bar <= self.state.last_completed_bar_ms
            ):
                self._save_state(
                    risk_recovery_bar_ms=None,
                    risk_recovery_command_id="",
                )
                attempted_bar = None
            else:
                raise CampaignError(
                    "上一根 K 线的临时风险恢复结果尚未确认，禁止进入下一根"
                )

        if attempted_bar == bar_ms:
            command_id = self.state.risk_recovery_command_id
            if not command_id:
                # 可能崩溃在命令入队后、命令 ID 落盘前。此时宁可跳过本根，
                # 也不能猜测命令未入队后再创建第二条恢复命令。
                self._save_state(
                    inflight_bar_ms=None,
                    risk_recovery_bar_ms=None,
                    risk_recovery_command_id="",
                    last_completed_bar_ms=bar_ms,
                    last_plan_result="blocked:risk:recovery_command_unconfirmed",
                    last_error="临时风险恢复命令是否入队无法确认，本轮不下单",
                )
                return False
            try:
                result = service.wait_for_command(command_id, timeout=30.0)
            except (LiveTradingDisabled, TimeoutError) as exc:
                raise DataSourceTransientError(
                    "临时风险恢复仍在等待同一条命令结果"
                ) from exc
            return self._finish_transient_risk_recovery(bar_ms, result)

        risk_state = service.worker_store.get_risk_runtime_state(
            "okx:demo:okx"
        )
        if (
            risk_state is None
            or not risk_state.kill_active
            or risk_state.kill_reason
            not in RECOVERABLE_TRANSIENT_RISK_STOP_REASONS
        ):
            return True

        # 先落“本根已尝试”标记，再创建命令。若进程在两次落盘之间崩溃，
        # 重启后会跳过本根，不会重复创建恢复命令。
        self._save_state(
            risk_recovery_bar_ms=bar_ms,
            risk_recovery_command_id="",
        )
        command = service.recover_transient_risk_stop()
        self._save_state(risk_recovery_command_id=command.id)
        try:
            result = service.wait_for_command(command.id, timeout=30.0)
        except (LiveTradingDisabled, TimeoutError) as exc:
            raise DataSourceTransientError(
                "临时风险恢复仍在等待同一条命令结果"
            ) from exc
        return self._finish_transient_risk_recovery(bar_ms, result)

    def _finish_transient_risk_recovery(
        self,
        bar_ms: int,
        result,
    ) -> bool:
        """收口一条已耐久记录的临时风险恢复命令。"""

        if result.status is WorkerCommandStatus.SUCCEEDED:
            self._save_state(
                risk_recovery_bar_ms=None,
                risk_recovery_command_id="",
            )
            logger.info(
                "临时风险读取故障已由新鲜完整证据恢复: command=%s",
                result.id,
            )
            return True

        failure = result.failure_code or result.status.value
        self._save_state(
            inflight_bar_ms=None,
            risk_recovery_bar_ms=None,
            risk_recovery_command_id="",
            last_completed_bar_ms=bar_ms,
            last_plan_result="blocked:risk:transient_read_unavailable",
            last_error="风险账户读取尚未恢复，本轮不下单",
        )
        logger.warning(
            "临时风险读取故障复核未通过，本根 K 线不分析、不下单: %s",
            failure,
        )
        return False

    def _recover_record_for_bar(
        self,
        bar_ms: int,
    ) -> AnalysisRecord | None:
        """只恢复当前 Campaign 自己为指定 K 线写下的耐久记录。"""
        started_ms = int(self.state.started_at_utc.timestamp() * 1000)
        owned_candidate: AnalysisRecord | None = None
        ownerless_candidate_seen = False
        for path in list_record_paths(RECORDS_PENDING_DIR):
            record = load_record(path)
            if record is None:
                continue
            if (
                record.meta.symbol != CAMPAIGN_SYMBOL
                or record.meta.timeframe != CAMPAIGN_TIMEFRAME
                or record.meta.decision_stance != CAMPAIGN_STANCE
                or record.meta.data_source != "okx"
                or record.meta.market_data_provenance
                != "okx_5m_utc_pair_aggregation"
                or record.meta.timestamp_local_ms < started_ms
                or not record.kline_data
            ):
                continue
            exception = record.exception
            is_claim = (
                isinstance(exception, dict)
                and str(exception.get("type") or "") == "claim_validation"
            )
            is_success = (
                exception is None
                and bool(record.stage1_diagnosis)
                and bool(record.stage2_decision)
            )
            if not is_claim and not is_success:
                continue
            try:
                record_bar_ms = int(
                    ts_open_to_ms(record.kline_data[0]["ts_open"])
                )
            except (KeyError, TypeError, ValueError):
                continue
            if record_bar_ms != bar_ms:
                continue

            if record.meta.campaign_id is None:
                ownerless_candidate_seen = True
                continue
            if record.meta.campaign_id != self.state.campaign_id:
                continue
            if owned_candidate is None:
                owned_candidate = record
        if owned_candidate is not None:
            return owned_candidate
        if ownerless_candidate_seen:
            raise CampaignError(
                "发现与 inflight K 线匹配但缺少 campaign_id 的旧耐久记录；"
                "无法区分旧 Campaign 与交互式分析，已失败关闭，禁止重调模型"
            )
        return None

    def _handle_claim_validation_record(
        self,
        record: AnalysisRecord,
        bar_ms: int,
    ) -> bool:
        """把声明校验失败耐久收口为本根 blocked，并允许下一根继续。"""
        exception = record.exception
        if not isinstance(exception, dict):
            return False
        if str(exception.get("type") or "") != "claim_validation":
            return False

        from pa_agent.ai.claim_validation import (
            extract_claim_validation_code,
        )

        code = str(exception.get("code") or "").strip()
        encoded_code = extract_claim_validation_code(
            exception.get("invalid_fields") or []
        )
        if not code or code != encoded_code:
            raise CampaignError("声明校验失败记录缺少一致的稳定错误码")

        self._validate_record_context(record, bar_ms)
        message = str(exception.get("message") or exception)
        self._save_state(
            inflight_bar_ms=None,
            last_completed_bar_ms=bar_ms,
            analyses_failed=self.state.analyses_failed + 1,
            last_plan_result=f"blocked:claim_validation:{code}",
            last_error=message,
        )
        logger.warning(
            "PA 声明校验阻断本根 K 线，零执行写入；下一根继续: code=%s",
            code,
        )
        return True

    def _execution_bar_ms(self, execution) -> int:
        return _campaign_execution_bar_ms(execution)

    def _owned_ready_executions(self):
        ready = []
        for execution_id in self.state.execution_ids:
            execution = self.runtime.execution_service.get_execution(
                execution_id
            )
            if execution is None:
                raise CampaignError(
                    f"实验 execution {execution_id} 不存在于执行账本"
                )
            if execution.state == ExecutionState.READY:
                ready.append(execution)
        return ready

    def _recover_owned_ready_for_bar(self, bar_ms: int) -> bool:
        ready_with_bars = [
            (execution, self._execution_bar_ms(execution))
            for execution in self._owned_ready_executions()
        ]
        future = [
            execution
            for execution, execution_bar_ms in ready_with_bars
            if execution_bar_ms > bar_ms
        ]
        if future:
            raise CampaignError(
                f"发现 {len(future)} 条来自未来 K 线的 READY 计划"
            )

        last_completed = self.state.last_completed_bar_ms
        stale = [
            (execution, execution_bar_ms)
            for execution, execution_bar_ms in ready_with_bars
            if execution_bar_ms < bar_ms
            or (
                last_completed is not None
                and execution_bar_ms <= last_completed
            )
        ]
        current = [
            execution
            for execution, execution_bar_ms in ready_with_bars
            if execution_bar_ms == bar_ms
            and (
                last_completed is None
                or execution_bar_ms > last_completed
            )
        ]
        if len(current) > 1:
            raise CampaignError("同一根 K 线存在多条 READY 计划，禁止重复提交")

        for execution, execution_bar_ms in stale:
            self.runtime.execution_service.expire_unsubmitted(
                execution.id,
                reason="新的已收盘 K 线已出现，未提交计划已过期",
            )
            logger.warning(
                "已作废过期且未提交的计划: id=%s bar_ms=%s",
                execution.id,
                execution_bar_ms,
            )
        if stale:
            completed_candidates = [
                value
                for value in (
                    self.state.last_completed_bar_ms,
                    *(item[1] for item in stale),
                )
                if value is not None
            ]
            self._save_state(
                last_completed_bar_ms=max(completed_candidates),
                last_plan_result="execution:canceled",
                last_error="",
            )

        if not current:
            return False
        try:
            self._ensure_demo_write_session()
            command = self.runtime.execution_service.submit(current[0].id)
        except Exception as exc:
            self._disarm_new_risk(
                action="恢复提交命令创建",
                primary_error=exc,
            )
            raise
        self._wait_for_worker_command(
            command.id,
            action="恢复提交",
            release_new_risk=True,
        )
        execution = self.runtime.execution_service.get_execution(current[0].id)
        if execution is None:
            raise CampaignError("恢复提交后执行记录消失")
        logger.info(
            "恢复同一根 K 线的 READY 计划: id=%s state=%s",
            execution.id,
            execution.state.value,
        )
        self._save_state(
            inflight_bar_ms=None,
            last_completed_bar_ms=bar_ms,
            last_execution_id=execution.id,
            last_plan_result=f"execution:{execution.state.value}",
            last_error="",
        )
        return True

    def _recover_owned_execution_for_bar(self, bar_ms: int) -> bool:
        """崩溃发生在提交后时，复用已有 execution，绝不重建第二笔。"""
        for execution_id in self.state.execution_ids:
            execution = self.runtime.execution_service.get_execution(
                execution_id
            )
            if execution is None:
                raise CampaignError(
                    f"实验 execution {execution_id} 不存在于执行账本"
                )
            if getattr(execution, "state", None) is ExecutionState.READY:
                continue
            if getattr(execution, "plan", None) is None:
                # 纯单元测试替身没有真实 ExecutionPlan；不能从替身猜 K 线归属。
                continue
            execution_bar_ms = self._execution_bar_ms(execution)
            if execution_bar_ms != bar_ms:
                continue
            state = execution.state
            if (
                state in {ExecutionState.UNKNOWN, ExecutionState.ERROR}
                or bool(getattr(execution, "needs_attention", False))
            ):
                raise CampaignError(
                    f"execution {execution.id} 需要人工核对，禁止自动进入下一根"
                )
            safe_terminal_states = {
                ExecutionState.BLOCKED,
                ExecutionState.CANCELED,
                ExecutionState.REJECTED,
                ExecutionState.CLOSED,
            }
            if (
                state not in ACTIVE_EXECUTION_STATES
                and state not in safe_terminal_states
            ):
                raise CampaignError(
                    f"execution {execution.id} 状态 {state.value} 不允许自动恢复"
                )
            self._save_state(
                inflight_bar_ms=None,
                last_completed_bar_ms=bar_ms,
                last_execution_id=execution.id,
                last_plan_result=f"execution:{state.value}",
                last_error="",
            )
            logger.info(
                "恢复同一根 K 线已有 execution: id=%s state=%s",
                execution.id,
                state.value,
            )
            return True
        return False

    def _expire_owned_ready(self, *, reason: str) -> None:
        for execution in self._owned_ready_executions():
            self.runtime.execution_service.expire_unsubmitted(
                execution.id,
                reason=reason,
            )
            logger.info(
                "已作废未提交计划: id=%s reason=%s",
                execution.id,
                reason,
            )

    def _refresh_order_quantity(self, record: AnalysisRecord) -> CampaignSizing:
        """在计划耐久化前，用此刻风险快照刷新本次实际合约数。"""
        sizing = self.runtime.sizing_resolver(record)
        _apply_campaign_sizing(self.runtime, sizing)
        logger.info(
            "Demo 风险定仓已刷新: equity_basis=%s equity_usdt=%s "
            "risk_budget_usdt=%s entry=%s stop_distance=%s quantity=%s "
            "risk_used=%s",
            sizing.equity_basis,
            sizing.equity_usdt,
            sizing.risk_budget_usdt,
            sizing.reference_price_usdt,
            sizing.stop_distance_usdt,
            sizing.quantity,
            sizing.risk_used_usdt,
        )
        return sizing

    @staticmethod
    def _record_order_direction(record: Any) -> str | None:
        if getattr(record, "exception", None) is not None:
            return None
        stage2 = getattr(record, "stage2_decision", None)
        if not isinstance(stage2, dict) or stage2.get("gate_shortcircuited"):
            return None
        decision = stage2.get("decision")
        if not isinstance(decision, dict):
            return None
        if str(decision.get("order_type") or "").strip() in {
            "",
            "不下单",
        }:
            return None
        direction = {
            "做多": "long",
            "做空": "short",
        }.get(str(decision.get("order_direction") or "").strip())
        if direction is None:
            raise CampaignError("PA 可执行决策缺少明确的做多/做空方向")
        return direction

    @classmethod
    def _is_new_risk_candidate(cls, record: Any) -> bool:
        return cls._record_order_direction(record) is not None

    def _apply_existing_position_script(
        self,
        record: AnalysisRecord,
        bar_ms: int,
        completed_count: int,
    ) -> bool:
        """同向持有；反向先清掉旧执行，再允许走完整的新风险链。"""
        active = self._owned_active_executions()
        if not active:
            return False
        if len(active) != 1:
            raise CampaignError(
                f"同一 Campaign 出现 {len(active)} 条活动执行，禁止脚本自动处置"
            )

        execution = active[0]
        current_direction = str(
            getattr(getattr(execution, "plan", None), "direction", "")
        ).strip()
        if current_direction not in {"long", "short"}:
            raise CampaignError("活动执行缺少可核验的持仓方向")
        proposed_direction = self._record_order_direction(record)

        if (
            execution.state is ExecutionState.UNKNOWN
            or bool(getattr(execution, "needs_attention", False))
        ):
            raise CampaignError(
                "活动执行状态不明或需要人工关注，禁止脚本自动处置"
            )
        if (
            proposed_direction is None
            or proposed_direction == current_direction
            or execution.state
            in {
                ExecutionState.SUBMITTING,
                ExecutionState.EXIT_PENDING,
            }
        ):
            self._save_state(
                inflight_bar_ms=None,
                last_completed_bar_ms=bar_ms,
                analyses_completed=completed_count,
                last_execution_id=execution.id,
                last_plan_result=f"script:hold:{execution.state.value}",
                last_error="",
            )
            return True

        service = self.runtime.execution_service
        timeout = float(self.runtime.settings.execution.entry_timeout_seconds) + 60
        if execution.state in {
            ExecutionState.ENTRY_PENDING,
            ExecutionState.PARTIALLY_FILLED,
        }:
            cancel = service.cancel_entry(execution.id)
            cancel_result = self._wait_for_worker_command(
                cancel.id,
                action="反向前撤销旧入场",
                allow_failed=True,
            )
            if cancel_result.status is WorkerCommandStatus.FAILED:
                raced = service.get_execution(execution.id)
                if raced is None:
                    raise CampaignError("撤单竞态后旧 execution 消失")
                if raced.state not in {
                    ExecutionState.CANCELED,
                    ExecutionState.CLOSED,
                    ExecutionState.PROTECTING,
                    ExecutionState.OPEN,
                }:
                    raise CampaignError(
                        "反向前撤单失败且旧执行未进入可安全续接状态："
                        f"{raced.state.value}"
                    )
                execution = raced
            else:
                execution = _wait_for_execution_state(
                    service,
                    execution.id,
                    accepted={
                        ExecutionState.CANCELED,
                        ExecutionState.CLOSED,
                        ExecutionState.PROTECTING,
                        ExecutionState.OPEN,
                    },
                    timeout=timeout,
                )
            if execution.state in {
                ExecutionState.CANCELED,
                ExecutionState.CLOSED,
            }:
                return False

        if execution.state in {
            ExecutionState.PROTECTING,
            ExecutionState.OPEN,
        }:
            exit_command = service.request_exit(
                execution.id,
                reason="PA 已收盘 K 线出现反向可执行信号",
            )
            self._wait_for_worker_command(
                exit_command.id,
                action="反向前主动离场",
            )
            _wait_for_execution_state(
                service,
                execution.id,
                accepted={ExecutionState.CLOSED},
                timeout=timeout,
            )
            return False

        raise CampaignError(
            f"活动执行状态 {execution.state.value} 不允许脚本自动反向"
        )

    def _analysis_digest(self, record: AnalysisRecord) -> str:
        """Use the exact durable analysis bytes consumed by plan_builder."""
        full_path = getattr(self.runtime.writer, "full_path", None)
        if callable(full_path):
            path = Path(full_path(record))
            if path.is_file():
                return hashlib.sha256(path.read_bytes()).hexdigest()
        if hasattr(record, "model_dump"):
            payload = json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return hashlib.sha256(payload).hexdigest()
        raise CampaignError("无法为确定性执行脚本建立 PA 分析摘要")

    def _validate_record_context(
        self,
        record: AnalysisRecord,
        bar_ms: int,
    ) -> None:
        if (
            record.meta.symbol != CAMPAIGN_SYMBOL
            or record.meta.timeframe != CAMPAIGN_TIMEFRAME
            or record.meta.campaign_id != self.state.campaign_id
            or record.meta.data_source != "okx"
            or record.meta.market_data_provenance
            != "okx_5m_utc_pair_aggregation"
        ):
            raise CampaignError("PA 耐久记录不属于当前 OKX 10m Campaign")
        if not record.kline_data or not isinstance(
            record.kline_data[0],
            dict,
        ):
            raise CampaignError("PA 耐久记录缺少主周期已收盘 K 线")
        closed_bar = record.kline_data[0]
        try:
            record_bar_ms = int(ts_open_to_ms(closed_bar["ts_open"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CampaignError("PA 耐久记录主周期 K 线时间无效") from exc
        if record_bar_ms != int(bar_ms) or closed_bar.get("closed") is not True:
            raise CampaignError("PA 耐久记录与当前已收盘 10m K 线不一致")

    def _consume_record(self, record, bar_ms: int, *, reused: bool) -> None:
        self._validate_record_context(record, bar_ms)
        completed_count = self.state.analyses_completed + (0 if reused else 1)
        if _utc_now() >= self.state.expires_at_utc:
            self._save_state(
                inflight_bar_ms=None,
                last_completed_bar_ms=bar_ms,
                analyses_completed=completed_count,
                last_plan_result="blocked:campaign_expired",
                last_error="",
            )
            logger.info("PA 分析返回时实验已到期, 不创建执行计划")
            return

        sizing: CampaignSizing | None = None
        leverage_parameters: SetLeverageParameters | None = None
        if self._apply_existing_position_script(
            record,
            bar_ms,
            completed_count,
        ):
            return
        if self._is_new_risk_candidate(record):
            try:
                sizing = self._refresh_order_quantity(record)
            except CampaignRiskBlocked as exc:
                if (
                    exc.code == "max_size_exceeded"
                    and exc.required_size is not None
                ):
                    try:
                        leverage_candidate = (
                            self.runtime.leverage_resolver(
                                record,
                                self._analysis_digest(record),
                            )
                        )
                    except (
                        CampaignRiskBlocked,
                        LeveragePlanningFailure,
                    ) as leverage_exc:
                        code = getattr(
                            leverage_exc,
                            "code",
                            "leverage_unavailable",
                        )
                        self._save_state(
                            inflight_bar_ms=None,
                            last_completed_bar_ms=bar_ms,
                            analyses_completed=completed_count,
                            last_plan_result=(
                                f"blocked:risk:leverage:{code}"
                            ),
                            last_error=str(leverage_exc),
                        )
                        logger.info(
                            "本轮动态杠杆容量仍不足，继续等待下一根 K 线: %s",
                            leverage_exc,
                        )
                        return
                    sizing = leverage_candidate.sizing
                    _apply_campaign_sizing(self.runtime, sizing)
                    leverage_parameters = _rebuild_leverage_parameters(
                        leverage_candidate.parameters,
                        analysis_record_path=str(
                            Path(
                                self.runtime.writer.full_path(record)
                            ).resolve()
                        ),
                        config_fingerprint=execution_route_fingerprint(
                            self.runtime.settings,
                            "okx",
                        ),
                    )
                else:
                    self._save_state(
                        inflight_bar_ms=None,
                        last_completed_bar_ms=bar_ms,
                        analyses_completed=completed_count,
                        last_plan_result=f"blocked:risk:{exc.code}",
                        last_error=str(exc),
                    )
                    logger.info(
                        "本轮风险定仓阻断，继续等待下一根已收盘 K 线: %s",
                        exc,
                    )
                    return
            _attach_campaign_sizing(record, sizing)
            if leverage_parameters is not None:
                _attach_campaign_leverage_intent(
                    record,
                    leverage_parameters,
                )
            self.runtime.writer.save_full_durable(record)
            if leverage_parameters is not None:
                leverage_parameters = _rebuild_leverage_parameters(
                    leverage_parameters,
                    analysis_digest=self._analysis_digest(record),
                )
            if leverage_parameters is not None:
                try:
                    self._ensure_demo_write_session()
                    leverage_command = (
                        self.runtime.execution_service.set_leverage(
                            leverage_parameters
                        )
                    )
                except Exception as exc:
                    self._disarm_new_risk(
                        action="动态杠杆命令创建",
                        primary_error=exc,
                    )
                    raise
                leverage_result = self._wait_for_worker_command(
                    leverage_command.id,
                    action="动态杠杆",
                    allow_failed=True,
                    release_new_risk=True,
                )
                if leverage_result.status is not WorkerCommandStatus.SUCCEEDED:
                    self._save_state(
                        inflight_bar_ms=None,
                        last_completed_bar_ms=bar_ms,
                        analyses_completed=completed_count,
                        last_plan_result=(
                            "blocked:risk:leverage:"
                            f"{leverage_result.failure_code or 'failed'}"
                        ),
                        last_error=(
                            leverage_result.failure_code
                            or leverage_result.status.value
                        ),
                    )
                    return
                try:
                    refreshed_sizing = self.runtime.sizing_resolver(record)
                except CampaignRiskBlocked as exc:
                    self._save_state(
                        inflight_bar_ms=None,
                        last_completed_bar_ms=bar_ms,
                        analyses_completed=completed_count,
                        last_plan_result=(
                            f"blocked:risk:after_leverage:{exc.code}"
                        ),
                        last_error=str(exc),
                    )
                    logger.info(
                        "杠杆确认后风险定仓仍被阻断，继续下一根 K 线: %s",
                        exc,
                    )
                    return
                if (
                    _campaign_sizing_fingerprint(refreshed_sizing)
                    != _campaign_sizing_fingerprint(sizing)
                ):
                    self._save_state(
                        inflight_bar_ms=None,
                        last_completed_bar_ms=bar_ms,
                        analyses_completed=completed_count,
                        last_plan_result=(
                            "blocked:risk:stale_after_leverage"
                        ),
                        last_error=(
                            "杠杆确认后权益或风险输入变化，"
                            "本根 K 线不创建执行计划"
                        ),
                    )
                    return
                sizing = refreshed_sizing
                _attach_campaign_sizing(record, sizing)
                self.runtime.writer.save_full_durable(record)
                if (
                    self._analysis_digest(record)
                    != leverage_parameters.analysis_digest
                ):
                    raise CampaignError(
                        "杠杆执行后的耐久分析摘要发生变化"
                    )

        try:
            execution = self.runtime.execution_service.prepare_analysis(record)
        except PlanBlocked as exc:
            result = f"blocked:{exc.code}"
            logger.info("本轮未创建执行计划: %s", exc)
            self._save_state(
                inflight_bar_ms=None,
                last_completed_bar_ms=bar_ms,
                analyses_completed=completed_count,
                last_plan_result=result,
                last_error="",
            )
            return

        owned_ids = list(dict.fromkeys([*self.state.execution_ids, execution.id]))
        prepared_count = self.state.executions_prepared + (
            0 if execution.id in self.state.execution_ids else 1
        )
        self._save_state(
            execution_ids=owned_ids,
            executions_prepared=prepared_count,
            analyses_completed=completed_count,
            last_execution_id=execution.id,
            last_plan_result=f"execution:{execution.state.value}",
            last_error="",
        )
        if (
            execution.state == ExecutionState.READY
            and _utc_now() < self.state.expires_at_utc
        ):
            try:
                self._ensure_demo_write_session()
                if sizing is not None:
                    _require_fresh_campaign_sizing(
                        self.runtime,
                        record,
                        sizing,
                    )
                command = self.runtime.execution_service.submit(execution.id)
            except CampaignRiskBlocked as exc:
                try:
                    self.runtime.execution_service.expire_unsubmitted(
                        execution.id,
                        reason="USDT 风险快照变化，旧计划禁止提交",
                    )
                    self._save_state(
                        inflight_bar_ms=None,
                        last_completed_bar_ms=bar_ms,
                        analyses_completed=completed_count,
                        last_plan_result=f"blocked:risk:{exc.code}",
                        last_error=str(exc),
                    )
                    logger.info("提交前风险快照已失效：%s", exc)
                except Exception as cleanup_exc:
                    self._disarm_new_risk(
                        action="提交前风险复核",
                        primary_error=cleanup_exc,
                    )
                    raise
                self._disarm_new_risk(action="提交前风险复核")
                return
            except Exception as exc:
                self._disarm_new_risk(
                    action="提交命令创建",
                    primary_error=exc,
                )
                raise
            submit_result = self._wait_for_worker_command(
                command.id,
                action="提交入场",
                allow_failed=True,
                release_new_risk=True,
            )
            refreshed = self.runtime.execution_service.get_execution(
                execution.id
            )
            if refreshed is None:
                raise CampaignError("提交后执行记录消失")
            execution = refreshed
            if submit_result.status is WorkerCommandStatus.FAILED:
                if execution.state is not ExecutionState.BLOCKED:
                    raise CampaignError(
                        "提交命令确定失败，但执行记录没有进入安全阻断终态"
                    )
                reason = str(
                    getattr(execution, "state_reason", "")
                    or getattr(execution, "last_error", "")
                    or "提交前风险闸门阻断"
                )
                self._save_state(
                    inflight_bar_ms=None,
                    last_completed_bar_ms=bar_ms,
                    last_plan_result=(
                        "blocked:submit:"
                        f"{submit_result.failure_code or 'blocked'}"
                    ),
                    last_error=reason,
                )
                logger.info(
                    "提交前确定性阻断，未触达券商写入，本轮结束: %s",
                    reason,
                )
                return
        logger.info(
            "执行记录已准备: id=%s state=%s",
            execution.id,
            execution.state.value,
        )
        self._save_state(
            inflight_bar_ms=None,
            last_completed_bar_ms=bar_ms,
            last_plan_result=f"execution:{execution.state.value}",
            last_error="",
        )

    def process_latest_closed_bar(self) -> bool:
        validate_campaign_settings(self.runtime.settings)
        fetch_count = int(self.runtime.settings.general.analysis_bar_count) + 50
        price_tick = self.runtime.source.price_tick()
        bars = self.runtime.source.latest_snapshot(fetch_count)
        frame = build_analysis_frame(
            bars,
            int(self.runtime.settings.general.analysis_bar_count),
            CAMPAIGN_SYMBOL,
            CAMPAIGN_TIMEFRAME,
            price_tick=price_tick,
        )
        if frame is None:
            raise DataSourceTransientError("XAU-USDT-SWAP 10m 已收盘聚合 K 线不足")
        bar_ms = int(ts_open_to_ms(frame.bars[0].ts_open))
        if self._recover_owned_ready_for_bar(bar_ms):
            return True
        if self._recover_owned_execution_for_bar(bar_ms):
            return True
        if (
            self.state.last_completed_bar_ms is not None
            and bar_ms <= self.state.last_completed_bar_ms
        ):
            return False

        inflight_bar_ms = self.state.inflight_bar_ms
        if inflight_bar_ms is not None and inflight_bar_ms < bar_ms:
            recovered_stale = self._recover_record_for_bar(inflight_bar_ms)
            if recovered_stale is not None:
                logger.info(
                    "先收口上一根 K 线的耐久分析记录，再处理最新 K 线"
                )
                if not self._handle_claim_validation_record(
                    recovered_stale,
                    inflight_bar_ms,
                ):
                    self._validate_record_context(
                        recovered_stale,
                        inflight_bar_ms,
                    )
                    # 耐久记录可能写在上次计数保存之前或之后；没有逐根计数
                    # 凭据时绝不猜测加一，与同根 reused 恢复保持 at-most-once。
                    self._save_state(
                        inflight_bar_ms=None,
                        last_completed_bar_ms=inflight_bar_ms,
                        last_plan_result="blocked:stale_recovered_analysis",
                        last_error=(
                            "恢复到的耐久分析对应旧 K 线；"
                            "信号已过期，未创建执行计划"
                        ),
                    )
                    logger.warning(
                        "恢复到旧 K 线的成功分析，信号已过期，零执行写入"
                    )
                return True

        if not self._recover_transient_risk_stop_for_bar(bar_ms):
            return True

        if self.state.inflight_bar_ms == bar_ms:
            recovered = self._recover_record_for_bar(bar_ms)
            if recovered is not None:
                logger.info("恢复同一根 K 线的耐久分析记录, 不重复调用模型")
                if self._handle_claim_validation_record(recovered, bar_ms):
                    return True
                self._consume_record(recovered, bar_ms, reused=True)
                return True

        higher_frames = {}
        higher_reader = getattr(
            self.runtime.source,
            "latest_snapshot_for_timeframe",
            None,
        )
        if callable(higher_reader):
            higher_count = min(
                50,
                max(20, int(self.runtime.settings.general.analysis_bar_count)),
            )
            higher_fetch_count = min(245, higher_count + 50)
            for higher_timeframe in higher_timeframes_for(CAMPAIGN_TIMEFRAME):
                higher_bars = higher_reader(higher_timeframe, higher_fetch_count)
                higher_frame = build_analysis_frame(
                    higher_bars,
                    higher_count,
                    CAMPAIGN_SYMBOL,
                    higher_timeframe,
                    price_tick=price_tick,
                )
                if higher_frame is None:
                    raise DataSourceTransientError(
                        f"XAU-USDT-SWAP {higher_timeframe} 已收盘 K 线不足，不能生成多周期背景"
                    )
                higher_frames[higher_timeframe] = higher_frame
        else:
            logger.warning(
                "当前行情源没有多周期读取接口；本轮只保留主周期分析，未伪造高周期背景"
            )
        higher_timeframe_text = (
            render_higher_timeframe_context(frame, higher_frames)
            if callable(higher_reader)
            else ""
        )

        self._save_state(inflight_bar_ms=bar_ms, last_error="")
        events: list[str] = []
        submit_kwargs = (
            {"higher_timeframe_text": higher_timeframe_text}
            if higher_timeframe_text
            else {}
        )
        record = self.runtime.orchestrator.submit(
            frame,
            CancelToken(),
            lambda event: events.append(event.name),
            campaign_id=self.state.campaign_id,
            **submit_kwargs,
        )
        if record.exception is not None:
            if self._handle_claim_validation_record(record, bar_ms):
                return True
            exception_type = str(record.exception.get("type") or "unknown")
            message = str(record.exception.get("message") or record.exception)
            self._save_state(
                inflight_bar_ms=None,
                last_completed_bar_ms=bar_ms,
                analyses_failed=self.state.analyses_failed + 1,
                last_plan_result=f"failed:{exception_type}",
                last_error=message,
            )
            if exception_type == "network_error":
                logger.warning(
                    "PA 分析因临时模型/网络错误跳过本根 K 线: %s",
                    message,
                )
                return True
            raise CampaignError(
                "PA 分析未形成可执行的完整记录: "
                + exception_type
            )
        logger.info("PA 分析完成: events=%s", ",".join(events))
        self._consume_record(record, bar_ms, reused=False)
        return True

    def _ensure_demo_write_session(self) -> None:
        """网络恢复后先重做 Demo 只读检查，再恢复本实验会话写门。"""
        validate_campaign_settings(self.runtime.settings)
        self.runtime.execution_service.start_monitoring()
        self.runtime.execution_service.wait_for_worker(timeout=10.0)
        if self.runtime.execution_service.is_armed:
            return
        try:
            okx_demo_private_preflight()
        except BrokerTransportError as exc:
            raise DataSourceTransientError(
                "OKX Demo 私有只读检查暂时不可用"
            ) from exc
        except BrokerApiError as exc:
            raise CampaignError(
                f"OKX Demo 私有只读检查未通过：{type(exc).__name__}"
            ) from exc
        except CampaignError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CampaignError(
                f"OKX Demo 私有只读检查未通过：{type(exc).__name__}"
            ) from exc
        if not self.runtime.execution_service.is_armed:
            try:
                self.runtime.execution_service.arm(
                    self.runtime.execution_service.arm_confirmation_text()
                )
            except NewRiskLeaseUnavailable as exc:
                raise DataSourceTransientError(
                    "OKX Demo 新增风险租约暂时被其他会话占用"
                ) from exc
        if not self.runtime.execution_service.is_armed:
            raise DataSourceTransientError(
                "OKX Demo 新增风险短租约未能生效"
            )

    def _wait_for_worker_command(
        self,
        command_id: str,
        *,
        action: str,
        allow_failed: bool = False,
        release_new_risk: bool = False,
    ):
        result = self.runtime.execution_service.wait_for_command(
            command_id,
            timeout=30.0,
        )
        terminal_statuses = {
            WorkerCommandStatus.SUCCEEDED,
            WorkerCommandStatus.FAILED,
            WorkerCommandStatus.UNCERTAIN,
        }
        if result.status not in terminal_statuses:
            raise CampaignError(
                f"{action}命令尚未进入耐久终态：{result.status.value}"
            )
        if release_new_risk:
            self._disarm_new_risk(
                action=action,
                terminal_result=result,
            )
        if result.status is WorkerCommandStatus.SUCCEEDED:
            return result
        if result.status is WorkerCommandStatus.UNCERTAIN:
            raise CampaignError(
                f"{action}结果不明，禁止自动重试；请先完成券商只读对账"
            )
        if allow_failed:
            return result
        raise CampaignError(
            f"{action}失败：{result.failure_code or result.status.value}"
        )

    def _disarm_new_risk(
        self,
        *,
        action: str,
        terminal_result=None,
        primary_error: Exception | None = None,
    ) -> None:
        """释放新增风险租约；失败时保留已经确认的业务事实。"""

        try:
            self.runtime.execution_service.disarm()
        except Exception as disarm_error:
            facts = []
            if terminal_result is not None:
                facts.append(f"命令状态={terminal_result.status.value}")
                if terminal_result.failure_code:
                    facts.append(
                        f"失败代码={terminal_result.failure_code}"
                    )
            if primary_error is not None:
                facts.append(f"原始异常={type(primary_error).__name__}")
            facts.append(f"释放异常={type(disarm_error).__name__}")
            error = CampaignError(
                f"{action}的新增风险租约释放失败（{'；'.join(facts)}）"
            )
            if primary_error is not None:
                error.add_note(
                    "租约释放异常："
                    f"{type(disarm_error).__name__}: {disarm_error}"
                )
                raise error from primary_error
            raise error from disarm_error

    def run(self) -> bool:
        validate_campaign_settings(self.runtime.settings)
        self._save_state(status="active", last_error="")
        logger.info(
            "OKX Demo 实验开始: campaign_id=%s expires_at=%s",
            self.state.campaign_id,
            self.state.expires_at,
        )

        while _utc_now() < self.state.expires_at_utc:
            try:
                self._monitor_owned_executions()
                # PA 分析不依赖新增风险租约；只有真正提交前才争用 NEW_RISK。
                self.process_latest_closed_bar()
                self._monitor_owned_executions()
            except DataSourceTransientError as exc:
                logger.warning("行情暂时不可用: %s", exc)
                if not self.state.last_error.startswith(
                    "blocked:reconcile:"
                ):
                    self._save_state(last_error=str(exc))
            remaining = (
                self.state.expires_at_utc - _utc_now()
            ).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(self.poll_seconds, remaining))
        return self.close_out()

    def _monitor_owned_executions(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        owned_ids = list(self.state.execution_ids)
        if not owned_ids:
            return

        def load_owned_executions():
            executions = []
            for execution_id in owned_ids:
                execution = self.runtime.execution_service.get_execution(
                    execution_id
                )
                if execution is None:
                    message = (
                        f"实验 execution {execution_id} 不存在于执行账本"
                    )
                    self._save_state(
                        last_plan_result="blocked:execution:missing",
                        last_error=message,
                    )
                    raise CampaignError(message)
                state = getattr(execution, "state", None)
                state_value = str(getattr(state, "value", state) or "missing")
                if state in {ExecutionState.UNKNOWN, ExecutionState.ERROR}:
                    message = (
                        f"execution {execution_id} 状态 {state_value}，"
                        "需要人工核对"
                    )
                    self._save_state(
                        last_plan_result=f"execution:{state_value}",
                        last_error=message,
                    )
                    raise CampaignError(message)
                if not isinstance(state, ExecutionState):
                    message = (
                        f"execution {execution_id} 状态 {state_value} "
                        "不在已核验状态集合"
                    )
                    self._save_state(
                        last_plan_result="blocked:execution:invalid_state",
                        last_error=message,
                    )
                    raise CampaignError(message)
                if (
                    state not in CAMPAIGN_SAFE_TERMINAL_STATES
                    and bool(getattr(execution, "needs_attention", False))
                ):
                    message = (
                        f"execution {execution_id} 需要人工核对，"
                        "禁止自动进入下一根"
                    )
                    self._save_state(
                        last_plan_result=(
                            "blocked:execution:needs_attention"
                        ),
                        last_error=message,
                    )
                    raise CampaignError(message)
                executions.append(execution)
            return executions

        owned_executions = load_owned_executions()
        if any(
            execution.state in ACTIVE_EXECUTION_STATES
            for execution in owned_executions
        ):
            after = (
                self.runtime.execution_service.latest_successful_reconcile_at()
            )
            poll_interval = float(
                self.runtime.settings.execution.poll_interval_seconds
            )
            reconcile_timeout = max(30.0, poll_interval * 3 + 5)
            if timeout_seconds is not None:
                reconcile_timeout = min(
                    reconcile_timeout,
                    max(0.1, float(timeout_seconds)),
                )
            try:
                self.runtime.execution_service.wait_for_reconcile(
                    after=after,
                    timeout=reconcile_timeout,
                )
            except (LiveTradingDisabled, TimeoutError) as exc:
                # 超时边界上 Worker 可能已经把 execution 推进到安全终态；
                # 先重读耐久账本，只有仍为普通活动态时才暂缓本轮 K 线。
                owned_executions = load_owned_executions()
                if any(
                    execution.state in ACTIVE_EXECUTION_STATES
                    for execution in owned_executions
                ):
                    message = (
                        str(exc).strip()
                        or "等待交易后台完成下一轮券商对账超时"
                    )
                    transient_result = (
                        CAMPAIGN_RECONCILE_TIMEOUT_RESULT
                        if isinstance(exc, TimeoutError)
                        else CAMPAIGN_RECONCILE_WORKER_ATTENTION_RESULT
                    )
                    updates = {
                        "last_error": f"{transient_result}: {message}",
                    }
                    if (
                        not self.state.last_plan_result
                        or self.state.last_plan_result.startswith(
                            "execution:"
                        )
                        or self.state.last_plan_result.startswith(
                            "blocked:reconcile:"
                        )
                    ):
                        updates["last_plan_result"] = transient_result
                    self._save_state(**updates)
                    raise DataSourceTransientError(message) from exc
            else:
                owned_executions = load_owned_executions()

        last_execution_id = self.state.last_execution_id
        if not last_execution_id:
            return
        execution = next(
            (
                item
                for item in owned_executions
                if item.id == last_execution_id
            ),
            None,
        )
        if execution is None:
            raise CampaignError(
                f"实验 execution {last_execution_id} 不存在于执行账本"
            )
        actual_result = f"execution:{execution.state.value}"
        updates = {}
        if (
            (
                self.state.last_plan_result.startswith("execution:")
                and self.state.last_plan_result != actual_result
            )
            or self.state.last_plan_result.startswith("blocked:reconcile:")
        ):
            updates["last_plan_result"] = actual_result
        if (
            updates
            or self.state.last_error.startswith("blocked:reconcile:")
        ):
            updates["last_error"] = ""
        if updates:
            self._save_state(**updates)

    def _owned_active_executions(self):
        owned_ids = set(self.state.execution_ids)
        return [
            record
            for record in self.runtime.execution_service.list_active()
            if record.id in owned_ids
            and record.state in ACTIVE_EXECUTION_STATES
        ]

    def close_out(self) -> bool:
        try:
            self.runtime.execution_service.disarm()
            self._save_state(status="stopping")
            self._expire_owned_ready(
                reason="24 小时 OKX Demo 实验到期，未提交计划作废"
            )
            cleanup_deadline = time.monotonic() + self.closeout_seconds
            poll_interval = float(
                self.runtime.settings.execution.poll_interval_seconds
            )
            completed_closeout_actions: set[tuple[str, str]] = set()
            while time.monotonic() < cleanup_deadline:
                try:
                    self._monitor_owned_executions(
                        timeout_seconds=cleanup_deadline - time.monotonic(),
                    )
                except DataSourceTransientError as exc:
                    logger.warning("收口等待交易后台对账暂时失败: %s", exc)
                    remaining = max(
                        0.0, cleanup_deadline - time.monotonic()
                    )
                    if remaining > 0:
                        time.sleep(min(poll_interval, remaining))
                    continue
                except CampaignError as exc:
                    self._save_state(
                        status="needs_attention",
                        last_error=str(exc),
                    )
                    logger.error("收口发现执行状态需要人工核对: %s", exc)
                    return False
                active = self._owned_active_executions()
                if not active:
                    if self.state.execution_ids:
                        command = (
                            self.runtime.execution_service.refresh_account(
                                self.state.execution_ids[-1]
                            )
                        )
                    else:
                        command = (
                            self.runtime.execution_service.refresh_account()
                        )
                    self._wait_for_worker_command(
                        command.id,
                        action="最终账户快照",
                    )
                    self._save_state(status="completed", last_error="")
                    logger.info("OKX Demo 实验已完成, 活动执行为 0")
                    return True
                for execution in active:
                    if execution.state in {
                        ExecutionState.ENTRY_PENDING,
                        ExecutionState.PARTIALLY_FILLED,
                    }:
                        action_key = (execution.id, "cancel_entry")
                        if action_key in completed_closeout_actions:
                            continue
                        command = self.runtime.execution_service.cancel_entry(
                            execution.id
                        )
                        self._wait_for_worker_command(
                            command.id,
                            action=f"收口撤销入场 {execution.id}",
                        )
                        completed_closeout_actions.add(action_key)
                    elif execution.state in {
                        ExecutionState.PROTECTING,
                        ExecutionState.OPEN,
                    }:
                        action_key = (execution.id, "request_exit")
                        if action_key in completed_closeout_actions:
                            continue
                        command = self.runtime.execution_service.request_exit(
                            execution.id,
                            reason="24 小时 OKX Demo 实验到期",
                        )
                        self._wait_for_worker_command(
                            command.id,
                            action=f"收口离场 {execution.id}",
                        )
                        completed_closeout_actions.add(action_key)
                time.sleep(poll_interval)

            remaining = self._owned_active_executions()
            self._save_state(
                status="needs_attention",
                last_error=f"到期收口后仍有 {len(remaining)} 条活动执行",
            )
            logger.error(
                "实验到期收口未完成, 仍有 %d 条活动执行", len(remaining)
            )
            return False
        except BaseException as exc:
            error = str(exc).strip() or type(exc).__name__
            try:
                self._save_state(
                    status="needs_attention",
                    last_error=error,
                )
            except BaseException as state_exc:
                logger.exception(
                    "实验收口异常，且 needs_attention 状态写入失败: %s",
                    state_exc,
                )
            logger.error("实验收口异常，已转人工处理: %s", error)
            raise

    def stop(self) -> None:
        self.runtime.execution_service.stop_monitoring()
        self.runtime.source.disconnect()


def _safe_status(state_store: CampaignStateStore) -> dict[str, Any]:
    state = state_store.load()
    if state is None:
        return {"exists": False}
    payload = state.model_dump(mode="json")
    payload["exists"] = True
    payload["config"] = {
        "sizing_mode": payload["frozen_sizing_mode"],
        "fixed_quantity": payload["frozen_fixed_quantity"],
        "risk_capital_cap_usdt": payload["frozen_risk_capital_cap_usdt"],
        "risk_percent": payload["frozen_risk_percent"],
        "maximum_leverage": payload["frozen_maximum_leverage"],
    }
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PA_Agent OKX Demo XAU-USDT-SWAP 10m 快速运行"
    )
    parser.add_argument(
        "command",
        choices=("preflight", "run", "restart", "status", "canary", "demo-s"),
        help=(
            "preflight=私有只读预检; run=10m 策略自动模拟运行; "
            "restart=归档空闲旧 Campaign 后按新固定配置启动; "
            "demo-s=真实 10m 记录的受控可复现监督闭环; "
            "canary=明确标记的 Demo 成交-保护-离场验收; status=读取状态"
        ),
    )
    parser.add_argument(
        "--entry-mode",
        choices=("signal", "limit", "limit_with_slippage", "market"),
        default=None,
        help="仅 canary 使用；覆盖本次 Demo 验收入场方式",
    )
    parser.add_argument(
        "--exit-mode",
        choices=("limit", "limit_with_slippage", "market"),
        default=None,
        help="仅 canary 使用；覆盖本次 Demo 验收主动离场方式",
    )
    parser.add_argument(
        "--entry-slippage-atr",
        type=Decimal,
        default=None,
        help="仅 canary 使用；入场限价滑点的 ATR14 倍数",
    )
    parser.add_argument(
        "--exit-slippage-atr",
        type=Decimal,
        default=None,
        help="仅 canary 使用；主动离场限价滑点的 ATR14 倍数",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "status":
        print(json.dumps(_safe_status(CampaignStateStore()), ensure_ascii=False, indent=2))
        return 0

    base_settings = load_settings(SETTINGS_JSON_PATH)
    configure_logging(api_key=base_settings.provider.api_key)
    try:
        preflight = okx_demo_private_preflight(base_settings)
    except Exception as exc:
        logger.error("OKX Demo 私有只读预检失败: %s", exc)
        return 2
    logger.info("OKX Demo 私有只读预检通过: %s", preflight)
    if args.command == "preflight":
        return 0

    if args.command == "demo-s":
        try:
            with CampaignProcessLock():
                result = run_controlled_demo_s()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except KeyboardInterrupt:
            logger.warning("Demo-S 被人工中断；Worker 将继续只读对账和减险")
            return 130
        except Exception as exc:
            logger.exception("Demo-S 失败: %s", exc)
            return 3

    if args.command == "canary":
        runtime_lock: CampaignProcessLock | None = None
        try:
            # 与 24 小时策略运行器共用同一把锁，避免两个模块同时拿新增风险租约。
            runtime_lock = CampaignProcessLock()
            runtime_lock.__enter__()
            result = run_demo_lifecycle_canary(
                entry_order_mode=args.entry_mode,
                exit_order_mode=args.exit_mode,
                entry_slippage_atr_multiple=args.entry_slippage_atr,
                exit_slippage_atr_multiple=args.exit_slippage_atr,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except KeyboardInterrupt:
            logger.warning("Demo 生命周期验收被人工中断；Worker 将继续只读对账和减险")
            return 130
        except Exception as exc:
            logger.exception("Demo 生命周期验收失败: %s", exc)
            return 3
        finally:
            if runtime_lock is not None:
                runtime_lock.__exit__(None, None, None)

    state_store = CampaignStateStore()
    execution_store = ExecutionStore(schema_mode="require_current")
    runtime: CampaignRuntime | None = None
    campaign: OkxDemoCampaign | None = None
    try:
        with CampaignProcessLock():
            state = (
                state_store.restart(
                    reason="用户授权更新 10m OKX Demo 运行器配置",
                    execution_lookup=execution_store.get,
                    settings=base_settings,
                )
                if args.command == "restart"
                else state_store.create_or_resume(settings=base_settings)
            )
            state_store.save(state)
            runtime = build_runtime(base_settings=base_settings)
            campaign = OkxDemoCampaign(runtime, state_store, state)
            try:
                completed = campaign.run()
            except KeyboardInterrupt:
                if campaign.state.status != "active":
                    logger.error(
                        "人工中断发生在收口已开始之后，禁止再次发送收口命令"
                    )
                    raise
                logger.warning("收到人工停止请求, 开始安全收口")
                completed = campaign.close_out()
            return 0 if completed else 3
    except Exception as exc:
        logger.exception("OKX Demo 实验发生阻塞故障: %s", exc)
        state = state_store.load()
        if state is not None and state.status == "active":
            state_store.save(
                state.model_copy(
                    update={"status": "needs_attention", "last_error": str(exc)}
                )
            )
        return 2
    finally:
        if campaign is not None:
            campaign.stop()
        elif runtime is not None:
            runtime.execution_service.stop_monitoring()
            runtime.source.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
