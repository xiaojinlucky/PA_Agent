"""OKX Demo 黄金 15 分钟自动交易运行器。

该入口故意固定交易范围，运行时不采纳日常交易路由中的品种和券商字段：

- OKX XAU-USDT-SWAP / 15m
- PA 激进
- 执行置信度门槛 30
- OKX Demo XAU-USDT-SWAP / cross / 当前 Demo 权益 10% 的动态张数
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
    ROUND_DOWN,
    ROUND_HALF_UP,
    Decimal,
    InvalidOperation,
)
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
from pa_agent.data.snapshot import build_analysis_frame
from pa_agent.execution.controller import ExecutionController
from pa_agent.execution.credentials import load_okx_credentials
from pa_agent.execution.errors import (
    BrokerApiError,
    BrokerTransportError,
    PlanBlocked,
)
from pa_agent.execution.models import ACTIVE_EXECUTION_STATES, ExecutionState
from pa_agent.execution.okx_client import OkxRestClient
from pa_agent.execution.worker_protocol import WorkerCommandStatus
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.records.analysis_history import (
    find_latest_successful_record,
    load_record,
)
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.records.schema import AnalysisRecord, RecordMeta
from pa_agent.util.logging import configure_logging
from pa_agent.util.threading import CancelToken

CAMPAIGN_INSTRUMENT = "XAU-USDT-SWAP"
CAMPAIGN_SYMBOL = CAMPAIGN_INSTRUMENT
CAMPAIGN_TIMEFRAME = "15m"
CAMPAIGN_PRODUCT = "swap"
CAMPAIGN_MARGIN_MODE = "cross"
# 每一笔以 Demo USDT 总权益的 10% 换算合约数。数量在每次准备新计划前
# 重新读取 OKX ticker、合约 ctVal/ctMult 与账户 USDT 权益，不把旧的固定数量
# 当成仓位管理规则。
CAMPAIGN_EQUITY_FRACTION = Decimal("0.10")
CAMPAIGN_SIZING_MODE = "equity_10pct_notional"
CAMPAIGN_BOOTSTRAP_QUANTITY = "1"
CAMPAIGN_OKX_API_BASE_URL = "https://www.okx.com"
CAMPAIGN_STANCE = "aggressive"
CAMPAIGN_MIN_CONFIDENCE = 30
# 本轮只用于 Demo 15 分钟闭环验收。它只改变模型在本运行器中的下单方式，
# 不写入日常设置，也不改变 GUI / 普通分析的提示词。
CAMPAIGN_EXECUTION_STYLE = "market_when_valid"
CAMPAIGN_FAST_EXECUTION_GUIDANCE = """
## 本轮专用：15 分钟 Demo 快速执行模式

这是一次仅限 OKX Demo 的闭环验收：目标是尽快验证「分析 → 入场 → 保护 → 离场」。
它不是日常策略默认规则，也不会改变其他调用。

- 本段是本运行器对通用策略资料的最高优先级覆盖：通用资料中的「§9.0P 计划型限价」和非市价 §11 路径在这里均不可用。仍须保留 §9.0P 审计节点，但若唯一候选是等待未来回撤/反弹的限价或突破，填写「不适用」并说明「本运行器禁止新建挂单」，不得把它写成待触发方案。
- 若 §9、§10.3、§14 允许交易，直接输出 **市价单**；不要因为等待一个更好限价而放弃已经有效的即时方案。并以最新已收盘 K1 的收盘价附近重新构建 entry / stop / TP1 / TP2 三价。
- 改为市价单后，三价必须仍满足方向顺序、最小价格跳动和 RR / 交易者方程；止损和止盈不能沿用一个远离 K1 收盘价的旧限价计划。
- 本运行器内不要新建限价单或突破单：只有「有效的立即市价方案」或「不下单」两种选择。
- 不要把“还可以等更好价格”“尚未完美确认”本身当作不下单理由；只有无法构造合法方向、止损、TP1、TP2 的即时三价时才输出不下单。不得伪造价格、取消止损或放宽 §14。
""".strip()
# 15 分钟循环不再创建新的限价单；270 秒仅用于恢复或收口已有历史限价
# 记录，避免沿用旧 120 秒全局默认值时过早撤单。
CAMPAIGN_ENTRY_TIMEOUT_SECONDS = 270
CAMPAIGN_DURATION = timedelta(hours=24)
CAMPAIGN_POLL_SECONDS = 30.0
CAMPAIGN_CLOSEOUT_SECONDS = 15 * 60
CAMPAIGN_STATE_PATH = RECORDS_PENDING_DIR.parent / "okx_demo_campaign.json"
CAMPAIGN_LOCK_PATH = RECORDS_PENDING_DIR.parent / "okx_demo_campaign.lock"
CAMPAIGN_HISTORY_DIR = RECORDS_PENDING_DIR.parent / "okx_demo_campaign_history"

# 这不是策略信号，也不是日常运行器的一部分。它只用来把 Demo 的
# 「市价入场 -> 成交回读 -> 原生保护 -> 受控离场」完整走一遍。
CANARY_TIMEFRAME = "demo_canary"
CANARY_ORIGIN = "okx_demo_lifecycle_canary"
CANARY_DIRECTION = "long"
CANARY_TIMEOUT_SECONDS = 120.0
CANARY_PRICE_BUFFER_RATIO = Decimal("0.005")
CANARY_MIN_BUFFER_TICKS = Decimal("50")

logger = logging.getLogger("pa_agent.okx_demo_campaign")


class CampaignError(RuntimeError):
    """实验配置或运行状态不满足硬约束。"""


class CampaignState(BaseModel):
    """不含密钥的可恢复实验状态。"""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    campaign_id: str
    config_fingerprint: str
    started_at: str
    expires_at: str
    status: Literal[
        "active",
        "stopping",
        "completed",
        "needs_attention",
    ] = "active"
    inflight_bar_ms: int | None = None
    last_completed_bar_ms: int | None = None
    analyses_completed: int = Field(default=0, ge=0)
    analyses_failed: int = Field(default=0, ge=0)
    executions_prepared: int = Field(default=0, ge=0)
    execution_ids: list[str] = Field(default_factory=list)
    last_execution_id: str = ""
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


def _campaign_config_payload() -> dict[str, Any]:
    return {
        "symbol": CAMPAIGN_SYMBOL,
        "timeframe": CAMPAIGN_TIMEFRAME,
        "market_data_source": "okx_public_candles",
        "instrument": CAMPAIGN_INSTRUMENT,
        "product": CAMPAIGN_PRODUCT,
        "margin_mode": CAMPAIGN_MARGIN_MODE,
        "sizing": {
            "mode": CAMPAIGN_SIZING_MODE,
            "equity_fraction": str(CAMPAIGN_EQUITY_FRACTION),
            "price_source": "okx_public_ticker_last",
            "contract_value_source": "okx_swap_ctVal_x_ctMult",
        },
        "api_base_url": CAMPAIGN_OKX_API_BASE_URL,
        "decision_stance": CAMPAIGN_STANCE,
        "execution_style": CAMPAIGN_EXECUTION_STYLE,
        "min_trade_confidence": CAMPAIGN_MIN_CONFIDENCE,
        "entry_timeout_seconds": CAMPAIGN_ENTRY_TIMEOUT_SECONDS,
        "environment": "demo",
        "duration_seconds": int(CAMPAIGN_DURATION.total_seconds()),
    }


def campaign_config_fingerprint() -> str:
    encoded = json.dumps(
        _campaign_config_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_campaign_settings(
    base_settings,
    *,
    quantity: Decimal | str = CAMPAIGN_BOOTSTRAP_QUANTITY,
):
    """复制设置并只在内存中应用实验路由。"""
    settings = copy.deepcopy(base_settings)
    # 15 分钟循环优先保证一根 K 线内完成判断，不沿用日常高推理强度。
    settings.provider.reasoning_effort = "medium"
    settings.general.decision_stance = CAMPAIGN_STANCE
    settings.general.last_symbol = CAMPAIGN_SYMBOL
    settings.general.last_timeframe = CAMPAIGN_TIMEFRAME
    settings.execution.enabled = True
    # 由实验进程在 execution id 耐久落盘后显式提交, 避免券商写入先于归属记录。
    settings.execution.auto_execute = False
    settings.execution.selected_broker = "okx"
    settings.execution.min_trade_confidence = CAMPAIGN_MIN_CONFIDENCE
    settings.execution.entry_timeout_seconds = CAMPAIGN_ENTRY_TIMEOUT_SECONDS
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

    def create_or_resume(self, *, now: datetime | None = None) -> CampaignState:
        current = (now or _utc_now()).astimezone(UTC)
        existing = self.load()
        fingerprint = campaign_config_fingerprint()
        if existing is not None:
            if existing.config_fingerprint != fingerprint:
                raise CampaignError("现有实验状态与当前固定配置不一致, 禁止覆盖")
            if existing.status == "completed":
                raise CampaignError("该 24 小时实验已经完成, 禁止自动重新计时")
            return existing.model_copy(
                update={"status": "active", "updated_at": current.isoformat()}
            )
        return CampaignState(
            campaign_id=str(uuid.uuid4()),
            config_fingerprint=fingerprint,
            started_at=current.isoformat(),
            expires_at=(current + CAMPAIGN_DURATION).isoformat(),
            updated_at=current.isoformat(),
        )

    def restart(
        self,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> CampaignState:
        """保留旧状态快照后，以明确新配置开启新的 Demo Campaign。"""
        current = (now or _utc_now()).astimezone(UTC)
        existing = self.load()
        if existing is not None:
            if existing.execution_ids:
                raise CampaignError(
                    "旧 Campaign 仍有自己创建的 execution，禁止直接切换配置"
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
                "state": existing.model_dump(),
            }
            self._write_json(archive, archive_payload)
        return CampaignState(
            campaign_id=str(uuid.uuid4()),
            config_fingerprint=campaign_config_fingerprint(),
            started_at=current.isoformat(),
            expires_at=(current + CAMPAIGN_DURATION).isoformat(),
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
            os.replace(temp_path, path)
        except OSError as exc:
            raise CampaignError(f"无法耐久保存实验状态: {path}") from exc

    def save(self, state: CampaignState) -> None:
        updated = state.model_copy(update={"updated_at": _utc_now().isoformat()})
        self._write_json(self.path, updated.model_dump())


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
    """一次 Demo 新开仓所需的不可变数量快照。"""

    quantity: Decimal
    equity_usdt: Decimal
    target_notional_usdt: Decimal
    reference_price_usdt: Decimal
    contract_notional_usdt: Decimal
    minimum_quantity: Decimal
    quantity_step: Decimal
    max_buy: Decimal
    max_sell: Decimal


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


def resolve_campaign_sizing(
    client: OkxRestClient | None = None,
) -> CampaignSizing:
    """把当前 Demo USDT 总权益的 10% 精确换算为永续合约张数。"""
    active_client = client or OkxRestClient(
        load_okx_credentials("demo"),
        base_url=CAMPAIGN_OKX_API_BASE_URL,
        simulated=True,
    )
    instrument = _campaign_instrument(active_client)
    minimum = _positive_decimal(instrument.get("minSz"))
    lot = _positive_decimal(instrument.get("lotSz"))
    contract_value = _positive_decimal(instrument.get("ctVal"))
    contract_multiplier = _positive_decimal(instrument.get("ctMult"))
    if min(minimum, lot, contract_value, contract_multiplier) <= 0:
        raise CampaignError("OKX 黄金永续缺少有效的合约规格")

    equity_usdt = _demo_usdt_equity(active_client.balance())
    ticker = active_client.ticker(CAMPAIGN_INSTRUMENT)
    reference_price = _positive_decimal(ticker.get("last"))
    if reference_price <= 0:
        raise CampaignError("OKX 黄金永续当前市价无效")
    contract_notional = contract_value * contract_multiplier * reference_price
    if contract_notional <= 0:
        raise CampaignError("OKX 黄金永续单张名义价值无效")

    target_notional = equity_usdt * CAMPAIGN_EQUITY_FRACTION
    raw_quantity = target_notional / contract_notional
    quantity = (
        (raw_quantity / lot).to_integral_value(rounding=ROUND_DOWN) * lot
    )
    if quantity < minimum:
        raise CampaignError(
            "OKX Demo 总权益的 10% 小于该永续的最小可交易名义金额"
        )

    maximum = active_client.max_order_size(
        instrument=CAMPAIGN_INSTRUMENT,
        trade_mode=CAMPAIGN_MARGIN_MODE,
    )
    max_buy = _positive_decimal(maximum.get("maxBuy"))
    max_sell = _positive_decimal(maximum.get("maxSell"))
    if quantity > max_buy or quantity > max_sell:
        raise CampaignError(
            "OKX Demo 当前可买/可卖数量不足以覆盖权益 10% 的计划仓位"
        )
    return CampaignSizing(
        quantity=quantity,
        equity_usdt=equity_usdt,
        target_notional_usdt=target_notional,
        reference_price_usdt=reference_price,
        contract_notional_usdt=contract_notional,
        minimum_quantity=minimum,
        quantity_step=lot,
        max_buy=max_buy,
        max_sell=max_sell,
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


def _canary_price_triplet(runtime: CampaignRuntime) -> tuple[Decimal, Decimal, Decimal, Decimal, KlineBar]:
    """只读取行情和合约规格，构造短时间内不易触发保护的 Demo 三价。"""
    bars = runtime.source.latest_snapshot(3)
    latest_closed = next((bar for bar in bars if bar.closed), None)
    if latest_closed is None:
        raise CampaignError("Demo 生命周期验收缺少已收盘 15m K 线")

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
    return entry, tp1, tp2, stop, latest_closed


def build_demo_canary_record(
    *,
    entry: Decimal,
    tp1: Decimal,
    tp2: Decimal,
    stop: Decimal,
    bar: KlineBar,
    now: datetime | None = None,
) -> AnalysisRecord:
    """生成可审计、明确非策略信号的耐久执行授权记录。"""
    current = (now or datetime.now().astimezone()).astimezone()
    timestamp_ms = int(current.timestamp() * 1000)
    direction_text = "做多" if CANARY_DIRECTION == "long" else "做空"
    decision = {
        "order_direction": direction_text,
        "order_type": "市价单",
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
            # 避免被 15m 策略运行器当成上一根真实策略分析记录。
            timeframe=CANARY_TIMEFRAME,
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
        if execution.state in {
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
) -> None:
    """只对已确认有仓位的状态发减险命令，未知状态不盲目重试。"""
    execution = service.get_execution(execution_id)
    if execution is None:
        return
    if execution.state is ExecutionState.ENTRY_PENDING:
        service.cancel_entry(execution_id)
        return
    if execution.state in {
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.PROTECTING,
        ExecutionState.OPEN,
    }:
        service.request_exit(execution_id, reason="Demo 生命周期验收异常收口")


def run_demo_lifecycle_canary() -> dict[str, str]:
    """通过现有 Controller/Worker 完成一次明确标记的 Demo 闭环验收。"""
    preflight = okx_demo_private_preflight()
    runtime: CampaignRuntime | None = None
    execution_id = ""
    try:
        runtime = build_runtime(
            resolved_quantity=preflight["resolved_quantity"],
        )
        validate_campaign_settings(runtime.settings)
        service = runtime.execution_service
        service.start_monitoring()
        service.wait_for_worker(timeout=10.0)
        if service.list_active():
            raise CampaignError("存在活动执行，Demo 生命周期验收不能与其并行")
        service.arm(service.arm_confirmation_text())

        entry, tp1, tp2, stop, bar = _canary_price_triplet(runtime)
        record = build_demo_canary_record(
            entry=entry,
            tp1=tp1,
            tp2=tp2,
            stop=stop,
            bar=bar,
        )
        runtime.writer.save_full_durable(record)
        execution = service.prepare_analysis(record)
        execution_id = execution.id
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
    except Exception:
        if runtime is not None and execution_id:
            try:
                _attempt_canary_cleanup(runtime.execution_service, execution_id)
            except Exception:
                logger.exception("Demo 生命周期验收异常后的减险命令未能入队")
        raise
    finally:
        if runtime is not None:
            runtime.execution_service.stop_monitoring()
            runtime.source.disconnect()


def okx_demo_private_preflight() -> dict[str, Any]:
    """用模拟标头完成私有只读验证, 不返回账户标识或凭据。"""
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

    sizing = resolve_campaign_sizing(client)

    balances = client.balance()
    positions = client.positions(instrument=CAMPAIGN_INSTRUMENT)
    leverage = client.leverage_info(
        instrument=CAMPAIGN_INSTRUMENT,
        margin_mode=CAMPAIGN_MARGIN_MODE,
    )
    market_rows = client.candles(
        instrument=CAMPAIGN_INSTRUMENT,
        bar=CAMPAIGN_TIMEFRAME,
        limit=2,
    )
    if not any(len(row) >= 9 and row[8] == "1" for row in market_rows):
        raise CampaignError("OKX 黄金永续没有可用的已收盘 15m K 线")
    return {
        "simulated": client.simulated,
        "account_identity_present": True,
        "instrument": CAMPAIGN_INSTRUMENT,
        "instrument_state": "live",
        "sizing_mode": CAMPAIGN_SIZING_MODE,
        "equity_usdt": str(sizing.equity_usdt),
        "equity_fraction": str(CAMPAIGN_EQUITY_FRACTION),
        "target_notional_usdt": str(sizing.target_notional_usdt),
        "reference_price_usdt": str(sizing.reference_price_usdt),
        "contract_notional_usdt": str(sizing.contract_notional_usdt),
        "resolved_quantity": str(sizing.quantity),
        "minimum_quantity": str(sizing.minimum_quantity),
        "quantity_step": str(sizing.quantity_step),
        "max_buy_sufficient": sizing.max_buy >= sizing.quantity,
        "max_sell_sufficient": sizing.max_sell >= sizing.quantity,
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

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        self._subscribed = False

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if symbol != CAMPAIGN_INSTRUMENT or timeframe != CAMPAIGN_TIMEFRAME:
            raise CampaignError("OKX Demo 快速运行只允许自身执行产品的 15m K 线")
        self._subscribed = True

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._subscribed:
            raise CampaignError("OKX Demo 行情源尚未订阅")
        try:
            rows = self._client.candles(
                instrument=CAMPAIGN_INSTRUMENT,
                bar=CAMPAIGN_TIMEFRAME,
                limit=n,
            )
        except (BrokerApiError, BrokerTransportError) as exc:
            raise DataSourceTransientError(f"OKX K 线暂时不可用: {exc}") from exc

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
                raise CampaignError("OKX K 线字段无法解析") from exc
            numeric_values = (
                open_price,
                high_price,
                low_price,
                close_price,
                volume,
                amount,
            )
            if not all(value.is_finite() for value in numeric_values):
                raise CampaignError("OKX K 线包含非有限数值")
            if min(open_price, high_price, low_price, close_price) <= 0:
                raise CampaignError("OKX K 线价格必须为正数")
            if volume < 0 or amount < 0:
                raise CampaignError("OKX K 线成交量不能为负数")
            if high_price < max(open_price, close_price):
                raise CampaignError("OKX K 线最高价低于开盘价或收盘价")
            if low_price > min(open_price, close_price):
                raise CampaignError("OKX K 线最低价高于开盘价或收盘价")
            if previous_ts is not None and timestamp >= previous_ts:
                raise CampaignError("OKX K 线时间必须严格从新到旧")
            previous_ts = timestamp
            if confirm not in {"0", "1"}:
                raise CampaignError("OKX K 线收盘标记无效")
            closed = confirm == "1"
            if closed:
                closed_seq += 1
                seq = closed_seq
            else:
                forming_count += 1
                if forming_count > 1:
                    raise CampaignError("OKX K 线包含多根未收盘数据")
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
    sizing_resolver: Callable[[], CampaignSizing] = resolve_campaign_sizing


def build_runtime(
    *,
    resolved_quantity: Decimal | str | None = None,
) -> CampaignRuntime:
    base_settings = load_settings(SETTINGS_JSON_PATH)
    sizing_quantity = (
        resolved_quantity
        if resolved_quantity is not None
        else resolve_campaign_sizing().quantity
    )
    settings = build_campaign_settings(base_settings, quantity=sizing_quantity)
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
    return CampaignRuntime(
        settings=settings,
        source=source,
        writer=writer,
        orchestrator=orchestrator,
        execution_service=service,
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
        self.runtime = runtime
        self.state_store = state_store
        self.state = state
        self.poll_seconds = float(poll_seconds)
        self.closeout_seconds = float(closeout_seconds)

    def _save_state(self, **updates: Any) -> None:
        self.state = self.state.model_copy(update=updates)
        self.state_store.save(self.state)

    def _recover_record_for_bar(self, bar_ms: int):
        record = find_latest_successful_record(
            symbol=CAMPAIGN_SYMBOL,
            timeframe=CAMPAIGN_TIMEFRAME,
        )
        if record is None or record.meta.decision_stance != CAMPAIGN_STANCE:
            return None
        if record.meta.timestamp_local_ms < int(
            self.state.started_at_utc.timestamp() * 1000
        ):
            return None
        if not record.kline_data:
            return None
        try:
            record_bar_ms = int(ts_open_to_ms(record.kline_data[0]["ts_open"]))
        except (KeyError, TypeError, ValueError):
            return None
        return record if record_bar_ms == bar_ms else None

    def _execution_bar_ms(self, execution) -> int:
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
        command = self.runtime.execution_service.submit(current[0].id)
        self._wait_for_worker_command(command.id, action="恢复提交")
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

    def _refresh_order_quantity(self) -> CampaignSizing:
        """在计划耐久化前，用此刻资金和市价刷新本次实际合约数。"""
        sizing = self.runtime.sizing_resolver()
        self.runtime.settings.execution.okx.quantity = str(sizing.quantity)
        validate_campaign_settings(self.runtime.settings)
        logger.info(
            "Demo 动态仓位已刷新: equity_usdt=%s target_notional_usdt=%s "
            "price=%s quantity=%s",
            sizing.equity_usdt,
            sizing.target_notional_usdt,
            sizing.reference_price_usdt,
            sizing.quantity,
        )
        return sizing

    def _consume_record(self, record, bar_ms: int, *, reused: bool) -> None:
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

        try:
            self._refresh_order_quantity()
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
            command = self.runtime.execution_service.submit(execution.id)
            self._wait_for_worker_command(command.id, action="提交入场")
            refreshed = self.runtime.execution_service.get_execution(
                execution.id
            )
            if refreshed is None:
                raise CampaignError("提交后执行记录消失")
            execution = refreshed
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
        bars = self.runtime.source.latest_snapshot(fetch_count)
        frame = build_analysis_frame(
            bars,
            int(self.runtime.settings.general.analysis_bar_count),
            CAMPAIGN_SYMBOL,
            CAMPAIGN_TIMEFRAME,
        )
        if frame is None:
            raise DataSourceTransientError("XAU-USDT-SWAP 15m 已收盘 K 线不足")
        bar_ms = int(ts_open_to_ms(frame.bars[0].ts_open))
        if self._recover_owned_ready_for_bar(bar_ms):
            return True
        if (
            self.state.last_completed_bar_ms is not None
            and bar_ms <= self.state.last_completed_bar_ms
        ):
            return False

        if self.state.inflight_bar_ms == bar_ms:
            recovered = self._recover_record_for_bar(bar_ms)
            if recovered is not None:
                logger.info("恢复同一根 K 线的耐久分析记录, 不重复调用模型")
                self._consume_record(recovered, bar_ms, reused=True)
                return True

        self._save_state(inflight_bar_ms=bar_ms, last_error="")
        events: list[str] = []
        record = self.runtime.orchestrator.submit(
            frame,
            CancelToken(),
            lambda event: events.append(event.name),
        )
        if record.exception is not None:
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
            self.runtime.execution_service.arm(
                self.runtime.execution_service.arm_confirmation_text()
            )
        if not self.runtime.execution_service.is_armed:
            raise DataSourceTransientError(
                "OKX Demo 新增风险短租约未能生效"
            )

    def _wait_for_worker_command(self, command_id: str, *, action: str) -> None:
        result = self.runtime.execution_service.wait_for_command(
            command_id,
            timeout=30.0,
        )
        if result.status is WorkerCommandStatus.SUCCEEDED:
            return
        if result.status is WorkerCommandStatus.UNCERTAIN:
            raise CampaignError(
                f"{action}结果不明，禁止自动重试；请先完成券商只读对账"
            )
        raise CampaignError(
            f"{action}失败：{result.failure_code or result.status.value}"
        )

    def run(self) -> bool:
        validate_campaign_settings(self.runtime.settings)
        # 硬门必须由共享 env 预先开启；实验进程绝不自行提升交易权限。
        self._ensure_demo_write_session()
        self._save_state(status="active", last_error="")
        logger.info(
            "OKX Demo 实验开始: campaign_id=%s expires_at=%s",
            self.state.campaign_id,
            self.state.expires_at,
        )

        while _utc_now() < self.state.expires_at_utc:
            try:
                self._ensure_demo_write_session()
                self._monitor_owned_executions()
                # 对账中的网络/身份异常会按安全规则停写；提交新计划前必须重新只读验证。
                self._ensure_demo_write_session()
                self.process_latest_closed_bar()
                self._monitor_owned_executions()
            except DataSourceTransientError as exc:
                logger.warning("行情暂时不可用: %s", exc)
                self._save_state(last_error=str(exc))
            remaining = (
                self.state.expires_at_utc - _utc_now()
            ).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(self.poll_seconds, remaining))
        return self.close_out()

    def _monitor_owned_executions(self) -> None:
        owned_ids = list(self.state.execution_ids)
        if not owned_ids:
            return
        after = (
            self.runtime.execution_service.latest_successful_reconcile_at()
        )
        poll_interval = float(
            self.runtime.settings.execution.poll_interval_seconds
        )
        self.runtime.execution_service.wait_for_reconcile(
            after=after,
            timeout=max(30.0, poll_interval * 3 + 5),
        )

    def _owned_active_executions(self):
        owned_ids = set(self.state.execution_ids)
        return [
            record
            for record in self.runtime.execution_service.list_active()
            if record.id in owned_ids
            and record.state in ACTIVE_EXECUTION_STATES
        ]

    def close_out(self) -> bool:
        self.runtime.execution_service.disarm()
        self._save_state(status="stopping")
        self._expire_owned_ready(reason="24 小时 OKX Demo 实验到期，未提交计划作废")
        cleanup_deadline = time.monotonic() + self.closeout_seconds
        while time.monotonic() < cleanup_deadline:
            self._monitor_owned_executions()
            active = self._owned_active_executions()
            if not active:
                if self.state.execution_ids:
                    command = self.runtime.execution_service.refresh_account(
                        self.state.execution_ids[-1]
                    )
                else:
                    command = self.runtime.execution_service.refresh_account()
                self._wait_for_worker_command(
                    command.id,
                    action="最终账户快照",
                )
                self._save_state(status="completed", last_error="")
                logger.info("OKX Demo 实验已完成, 活动执行为 0")
                return True
            for execution in active:
                if execution.state == ExecutionState.ENTRY_PENDING:
                    self.runtime.execution_service.cancel_entry(execution.id)
                elif execution.state in {
                    ExecutionState.PARTIALLY_FILLED,
                    ExecutionState.PROTECTING,
                    ExecutionState.OPEN,
                }:
                    self.runtime.execution_service.request_exit(
                        execution.id,
                        reason="24 小时 OKX Demo 实验到期",
                    )
            time.sleep(float(self.runtime.settings.execution.poll_interval_seconds))

        remaining = self._owned_active_executions()
        self._save_state(
            status="needs_attention",
            last_error=f"到期收口后仍有 {len(remaining)} 条活动执行",
        )
        logger.error("实验到期收口未完成, 仍有 %d 条活动执行", len(remaining))
        return False

    def stop(self) -> None:
        self.runtime.execution_service.stop_monitoring()
        self.runtime.source.disconnect()


def _safe_status(state_store: CampaignStateStore) -> dict[str, Any]:
    state = state_store.load()
    if state is None:
        return {"exists": False}
    payload = state.model_dump()
    payload["exists"] = True
    payload["config"] = _campaign_config_payload()
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PA_Agent OKX Demo XAU-USDT-SWAP 15m 快速运行"
    )
    parser.add_argument(
        "command",
        choices=("preflight", "run", "restart", "status", "canary"),
        help=(
            "preflight=私有只读预检; run=15m 策略自动模拟运行; "
            "restart=归档空闲旧 Campaign 后按新固定配置启动; "
            "canary=明确标记的 Demo 成交-保护-离场验收; status=读取状态"
        ),
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
        preflight = okx_demo_private_preflight()
    except Exception as exc:
        logger.error("OKX Demo 私有只读预检失败: %s", exc)
        return 2
    logger.info("OKX Demo 私有只读预检通过: %s", preflight)
    if args.command == "preflight":
        return 0

    if args.command == "canary":
        runtime_lock: CampaignProcessLock | None = None
        try:
            # 与 24 小时策略运行器共用同一把锁，避免两个模块同时拿新增风险租约。
            runtime_lock = CampaignProcessLock()
            runtime_lock.__enter__()
            result = run_demo_lifecycle_canary()
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
    runtime: CampaignRuntime | None = None
    campaign: OkxDemoCampaign | None = None
    try:
        with CampaignProcessLock():
            state = (
                state_store.restart(
                    reason="用户授权更新 15m OKX Demo 运行器配置",
                )
                if args.command == "restart"
                else state_store.create_or_resume()
            )
            state_store.save(state)
            runtime = build_runtime(
                resolved_quantity=preflight["resolved_quantity"],
            )
            campaign = OkxDemoCampaign(runtime, state_store, state)
            completed = campaign.run()
            return 0 if completed else 3
    except KeyboardInterrupt:
        logger.warning("收到人工停止请求, 开始安全收口")
        if campaign is None:
            return 130
        return 0 if campaign.close_out() else 3
    except Exception as exc:
        logger.exception("OKX Demo 实验发生阻塞故障: %s", exc)
        state = state_store.load()
        if state is not None:
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
