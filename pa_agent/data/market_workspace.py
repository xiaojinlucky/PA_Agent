"""多市场看盘页使用的纯数据合同与异步结果门禁。

本模块不连接券商、不写设置、不创建分析或交易计划。它只负责：

1. 把报价保存为不可变、可校验的十进制快照；
2. 计算报价新鲜度；
3. 表达最多 100 项本地自选的批量报价结果；
4. 用 generation 和逐请求族序号拒绝迟到的异步结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal

MarketCode = Literal["US", "HK", "CN", "Crypto"]
QuoteSource = Literal["longbridge", "okx"]
QuoteMode = Literal["realtime", "delayed"]

_MARKET_SOURCES: dict[str, str] = {
    "US": "longbridge",
    "HK": "longbridge",
    "CN": "longbridge",
    "Crypto": "okx",
}
_DISPLAY_TIMEFRAMES = frozenset({"10m", "1h", "4h"})


class QuoteFreshness(StrEnum):
    """页面可见的报价新鲜度。"""

    FRESH = "fresh"
    SESSION_PAUSED = "session_paused"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class QuoteFreshnessReason(StrEnum):
    """稳定的新鲜度原因，页面不需要解析供应商异常文本。"""

    OK = "ok"
    SESSION_PAUSED = "session_paused"
    IDENTITY_MISMATCH = "identity_mismatch"
    REQUEST_SUPERSEDED = "request_superseded"
    CLOCK_INVALID = "clock_invalid"
    AGE_EXCEEDED = "age_exceeded"
    AUTH_FAILED = "auth_failed"
    PERMISSION_DENIED = "permission_denied"
    SYMBOL_UNSUPPORTED = "symbol_unsupported"
    INVALID_RESPONSE = "invalid_response"
    TRANSPORT_FAILED = "transport_failed"
    SNAPSHOT_MISSING = "snapshot_missing"


class QuoteFailureKind(StrEnum):
    """行情失败的稳定业务类别。"""

    AUTH_FAILED = "auth_failed"
    PERMISSION_DENIED = "permission_denied"
    SYMBOL_UNSUPPORTED = "symbol_unsupported"
    TRANSPORT_FAILED = "transport_failed"
    INVALID_RESPONSE = "invalid_response"


class RequestFamily(StrEnum):
    """同一页面选择下互不覆盖的异步请求族。"""

    CONNECT = "connect"
    STATIC_INFO = "static_info"
    QUOTE = "quote"
    KLINE = "kline"
    WATCHLIST = "watchlist"
    SETTINGS = "settings"
    ANALYSIS = "analysis"


class EvidenceState(StrEnum):
    """K 线证据当前能否用于页面和分析。"""

    READY = "ready"
    INSUFFICIENT = "insufficient"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class AnalysisResultState(StrEnum):
    """只读分析完成路径的稳定结果状态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AnalysisCapabilityState(StrEnum):
    """当前数据包允许的分析能力。"""

    READY = "ready"
    DISPLAY_ONLY = "display_only"
    BLOCKED = "blocked"


class AnalysisGateReason(StrEnum):
    """分析能力门的稳定原因。"""

    OK = "ok"
    QUOTE_NOT_READY = "quote_not_ready"
    TEN_MINUTE_NOT_READY = "ten_minute_not_ready"
    PRICE_TICK_UNAVAILABLE = "price_tick_unavailable"


def _normalise_decimal(
    value: object,
    *,
    field_name: str,
    allow_none: bool = False,
    positive: bool = False,
) -> str | None:
    if value is None or str(value).strip() == "":
        if allow_none:
            return None
        raise ValueError(f"{field_name} 不能为空")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是十进制数") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} 必须是有限十进制数")
    if positive and parsed <= 0:
        raise ValueError(f"{field_name} 必须大于零")
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _normalise_identity_text(value: object, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} 不能为空")
    return text


@dataclass(frozen=True, slots=True)
class SelectionIdentity:
    """一次已提交或正在暂存的完整页面选择。"""

    selection_generation: int
    market: MarketCode
    source: QuoteSource
    symbol: str
    display_timeframe: str
    analysis_timeframe: str = "10m"

    def __post_init__(self) -> None:
        if self.selection_generation < 1:
            raise ValueError("selection_generation 必须大于等于 1")
        market = _normalise_identity_text(self.market, field_name="market")
        source = _normalise_identity_text(self.source, field_name="source")
        expected_source = _MARKET_SOURCES.get(market)
        if expected_source is None:
            raise ValueError(f"不支持的市场：{market}")
        if source != expected_source:
            raise ValueError(f"{market} 必须使用 {expected_source} 行情源")
        symbol = _normalise_identity_text(
            self.symbol,
            field_name="symbol",
        ).upper()
        display_timeframe = _normalise_identity_text(
            self.display_timeframe,
            field_name="display_timeframe",
        )
        if display_timeframe not in _DISPLAY_TIMEFRAMES:
            raise ValueError("display_timeframe 只支持 10m、1h、4h")
        if self.analysis_timeframe != "10m":
            raise ValueError("首版 analysis_timeframe 固定为 10m")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "display_timeframe", display_timeframe)

    @property
    def key(self) -> tuple[int, str, str, str, str]:
        return (
            self.selection_generation,
            self.market,
            self.source,
            self.symbol,
            self.display_timeframe,
        )


@dataclass(frozen=True, slots=True)
class RequestToken:
    """一个异步请求不可变的身份与序号。"""

    identity: SelectionIdentity
    family: RequestFamily
    request_sequence: int

    def __post_init__(self) -> None:
        if self.request_sequence < 1:
            raise ValueError("request_sequence 必须大于等于 1")


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    """经过身份、数值和时间校验的不可变报价。"""

    schema_version: int
    selection_generation: int
    request_sequence: int
    symbol: str
    market: MarketCode
    source: QuoteSource
    name: str | None
    currency: str | None
    last: str
    prev_close: str | None
    change: str | None
    change_pct: str | None
    # 只有行情源明确提供时才填写；不得从报价小数位或市场默认值猜测。
    price_tick: str | None
    quote_ts_utc_ms: int
    received_at_utc_ms: int
    quote_mode: QuoteMode
    expected_delay_ms: int

    def __post_init__(self) -> None:
        identity = SelectionIdentity(
            selection_generation=self.selection_generation,
            market=self.market,
            source=self.source,
            symbol=self.symbol,
            display_timeframe="10m",
        )
        if self.schema_version != 1:
            raise ValueError("QuoteSnapshot schema_version 当前必须为 1")
        if self.request_sequence < 1:
            raise ValueError("request_sequence 必须大于等于 1")
        if self.quote_ts_utc_ms < 0 or self.received_at_utc_ms < 0:
            raise ValueError("报价时间必须是非负 UTC 毫秒时间戳")
        if self.quote_ts_utc_ms > self.received_at_utc_ms + 5_000:
            raise ValueError("行情源时间快于本机接收时间超过 5 秒")
        if self.expected_delay_ms < 0:
            raise ValueError("expected_delay_ms 不能为负数")
        if self.quote_mode not in {"realtime", "delayed"}:
            raise ValueError("quote_mode 只支持 realtime 或 delayed")
        if self.quote_mode == "realtime" and self.expected_delay_ms != 0:
            raise ValueError("实时行情 expected_delay_ms 必须为 0")
        if self.quote_mode == "delayed" and self.expected_delay_ms <= 0:
            raise ValueError("延迟行情必须声明正数 expected_delay_ms")

        object.__setattr__(self, "symbol", identity.symbol)
        object.__setattr__(self, "market", identity.market)
        object.__setattr__(self, "source", identity.source)
        object.__setattr__(
            self,
            "name",
            str(self.name).strip() if self.name is not None else None,
        )
        object.__setattr__(
            self,
            "currency",
            str(self.currency).strip().upper() if self.currency is not None else None,
        )
        object.__setattr__(
            self,
            "last",
            _normalise_decimal(self.last, field_name="last", positive=True),
        )
        object.__setattr__(
            self,
            "prev_close",
            _normalise_decimal(
                self.prev_close,
                field_name="prev_close",
                allow_none=True,
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "change",
            _normalise_decimal(self.change, field_name="change", allow_none=True),
        )
        object.__setattr__(
            self,
            "change_pct",
            _normalise_decimal(
                self.change_pct,
                field_name="change_pct",
                allow_none=True,
            ),
        )
        object.__setattr__(
            self,
            "price_tick",
            _normalise_decimal(
                self.price_tick,
                field_name="price_tick",
                allow_none=True,
                positive=True,
            ),
        )

    @classmethod
    def from_prices(
        cls,
        *,
        selection_generation: int,
        request_sequence: int,
        symbol: str,
        market: MarketCode,
        source: QuoteSource,
        name: str | None,
        currency: str | None,
        last: object,
        prev_close: object | None,
        price_tick: object | None,
        quote_ts_utc_ms: int,
        received_at_utc_ms: int,
        quote_mode: QuoteMode = "realtime",
        expected_delay_ms: int = 0,
    ) -> QuoteSnapshot:
        """由供应商原始价格构造快照，涨跌只在后端计算一次。"""

        last_decimal = Decimal(_normalise_decimal(last, field_name="last", positive=True) or "0")
        previous_text = _normalise_decimal(
            prev_close,
            field_name="prev_close",
            allow_none=True,
            positive=True,
        )
        change: str | None = None
        change_pct: str | None = None
        if previous_text is not None:
            previous = Decimal(previous_text)
            change_decimal = last_decimal - previous
            change = _normalise_decimal(change_decimal, field_name="change")
            change_pct = _normalise_decimal(
                change_decimal / previous * Decimal("100"),
                field_name="change_pct",
            )
        return cls(
            schema_version=1,
            selection_generation=selection_generation,
            request_sequence=request_sequence,
            symbol=symbol,
            market=market,
            source=source,
            name=name,
            currency=currency,
            last=format(last_decimal, "f"),
            prev_close=previous_text,
            change=change,
            change_pct=change_pct,
            price_tick=price_tick,
            quote_ts_utc_ms=int(quote_ts_utc_ms),
            received_at_utc_ms=int(received_at_utc_ms),
            quote_mode=quote_mode,
            expected_delay_ms=int(expected_delay_ms),
        )

    def matches(
        self,
        identity: SelectionIdentity,
        *,
        request_sequence: int,
    ) -> bool:
        return (
            self.selection_generation == identity.selection_generation
            and self.market == identity.market
            and self.source == identity.source
            and self.symbol == identity.symbol
            and self.request_sequence == request_sequence
        )


@dataclass(frozen=True, slots=True)
class QuoteFreshnessView:
    """页面可以直接渲染的报价状态。"""

    generation: int
    request_sequence: int
    snapshot: QuoteSnapshot | None
    freshness: QuoteFreshness
    reason: QuoteFreshnessReason


def evaluate_quote_freshness(
    snapshot: QuoteSnapshot | None,
    *,
    identity: SelectionIdentity,
    request_sequence: int,
    now_utc_ms: int,
    transport_budget_ms: int,
    session_paused: bool = False,
) -> QuoteFreshnessView:
    """按 PRD05 的时间与身份规则计算报价新鲜度。"""

    if request_sequence < 1:
        raise ValueError("request_sequence 必须大于等于 1")
    if now_utc_ms < 0 or transport_budget_ms < 0:
        raise ValueError("当前时间与传输预算不能为负数")
    if snapshot is None:
        return QuoteFreshnessView(
            identity.selection_generation,
            request_sequence,
            None,
            QuoteFreshness.UNAVAILABLE,
            QuoteFreshnessReason.SNAPSHOT_MISSING,
        )
    if (
        snapshot.selection_generation != identity.selection_generation
        or snapshot.market != identity.market
        or snapshot.source != identity.source
        or snapshot.symbol != identity.symbol
    ):
        return QuoteFreshnessView(
            identity.selection_generation,
            request_sequence,
            None,
            QuoteFreshness.UNAVAILABLE,
            QuoteFreshnessReason.IDENTITY_MISMATCH,
        )
    if snapshot.request_sequence != request_sequence:
        return QuoteFreshnessView(
            identity.selection_generation,
            request_sequence,
            None,
            QuoteFreshness.UNAVAILABLE,
            QuoteFreshnessReason.REQUEST_SUPERSEDED,
        )

    received_age_ms = now_utc_ms - snapshot.received_at_utc_ms
    quote_age_ms = now_utc_ms - snapshot.quote_ts_utc_ms
    if received_age_ms < 0 or quote_age_ms < -5_000:
        return QuoteFreshnessView(
            identity.selection_generation,
            request_sequence,
            None,
            QuoteFreshness.UNAVAILABLE,
            QuoteFreshnessReason.CLOCK_INVALID,
        )
    corrected_quote_age_ms = max(0, quote_age_ms)
    allowed_quote_age_ms = snapshot.expected_delay_ms + transport_budget_ms
    if session_paused:
        return QuoteFreshnessView(
            identity.selection_generation,
            request_sequence,
            snapshot,
            QuoteFreshness.SESSION_PAUSED,
            QuoteFreshnessReason.SESSION_PAUSED,
        )
    if received_age_ms > transport_budget_ms or corrected_quote_age_ms > allowed_quote_age_ms:
        return QuoteFreshnessView(
            identity.selection_generation,
            request_sequence,
            snapshot,
            QuoteFreshness.STALE,
            QuoteFreshnessReason.AGE_EXCEEDED,
        )
    return QuoteFreshnessView(
        identity.selection_generation,
        request_sequence,
        snapshot,
        QuoteFreshness.FRESH,
        QuoteFreshnessReason.OK,
    )


def quote_failure_view(
    *,
    identity: SelectionIdentity,
    request_sequence: int,
    failure: QuoteFailureKind,
    previous_snapshot: QuoteSnapshot | None = None,
) -> QuoteFreshnessView:
    """把稳定失败类别转换为失败关闭的页面状态。"""

    hard_failure_reasons = {
        QuoteFailureKind.AUTH_FAILED: QuoteFreshnessReason.AUTH_FAILED,
        QuoteFailureKind.PERMISSION_DENIED: QuoteFreshnessReason.PERMISSION_DENIED,
        QuoteFailureKind.SYMBOL_UNSUPPORTED: QuoteFreshnessReason.SYMBOL_UNSUPPORTED,
        QuoteFailureKind.INVALID_RESPONSE: QuoteFreshnessReason.INVALID_RESPONSE,
    }
    if failure in hard_failure_reasons:
        return QuoteFreshnessView(
            identity.selection_generation,
            request_sequence,
            None,
            QuoteFreshness.UNAVAILABLE,
            hard_failure_reasons[failure],
        )
    preserved = (
        previous_snapshot
        if previous_snapshot is not None
        and previous_snapshot.matches(identity, request_sequence=request_sequence)
        else None
    )
    return QuoteFreshnessView(
        identity.selection_generation,
        request_sequence,
        preserved,
        QuoteFreshness.STALE if preserved is not None else QuoteFreshness.UNAVAILABLE,
        QuoteFreshnessReason.TRANSPORT_FAILED,
    )


@dataclass(frozen=True, slots=True)
class WatchlistRequestToken:
    """一次完整自选刷新使用的批次级身份。"""

    selection_generation: int
    market: MarketCode
    source: QuoteSource
    symbols: tuple[str, ...]
    watchlist_change_sequence: int
    watchlist_refresh_sequence: int

    def __post_init__(self) -> None:
        if (
            min(
                self.selection_generation,
                self.watchlist_change_sequence,
                self.watchlist_refresh_sequence,
            )
            < 1
        ):
            raise ValueError("自选 generation 和序号必须大于等于 1")
        expected_source = _MARKET_SOURCES.get(str(self.market))
        if expected_source is None or self.source != expected_source:
            raise ValueError("自选市场与数据源路由不一致")
        if not self.symbols or len(self.symbols) > 100:
            raise ValueError("首版自选必须包含 1 到 100 项")
        normalized = tuple(
            _normalise_identity_text(symbol, field_name="symbol").upper() for symbol in self.symbols
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("自选标的不能重复")
        object.__setattr__(self, "symbols", normalized)


@dataclass(frozen=True, slots=True)
class WatchlistQuoteSet:
    """一次有界批量刷新产生的完整、不可缺行的自选报价集合。"""

    token: WatchlistRequestToken
    snapshots: tuple[QuoteSnapshot, ...]

    def __post_init__(self) -> None:
        if len(self.snapshots) != len(self.token.symbols):
            raise ValueError("自选批量报价必须完整返回每一个请求标的")
        by_symbol: dict[str, QuoteSnapshot] = {}
        for snapshot in self.snapshots:
            if snapshot.selection_generation != self.token.selection_generation:
                raise ValueError("自选快照 generation 与批次不一致")
            if snapshot.request_sequence != self.token.watchlist_refresh_sequence:
                raise ValueError("自选快照 request_sequence 与刷新批次不一致")
            if snapshot.market != self.token.market or snapshot.source != self.token.source:
                raise ValueError("自选快照市场或来源与批次不一致")
            if snapshot.symbol in by_symbol:
                raise ValueError(f"自选报价包含重复标的：{snapshot.symbol}")
            by_symbol[snapshot.symbol] = snapshot
        if tuple(by_symbol) != self.token.symbols:
            raise ValueError("自选批量报价顺序或标的集合与请求不一致")


class WatchlistGenerationGate:
    """自选列表独立于当前标的的 change/refresh 批次门禁。"""

    def __init__(self) -> None:
        self._change_sequence = 0
        self._refresh_sequence = 0
        self._symbols: tuple[str, ...] = ()
        self._latest: WatchlistRequestToken | None = None

    def issue(
        self,
        identity: SelectionIdentity,
        symbols: tuple[str, ...],
    ) -> WatchlistRequestToken:
        normalized = tuple(
            _normalise_identity_text(symbol, field_name="symbol").upper() for symbol in symbols
        )
        if normalized != self._symbols:
            self._change_sequence += 1
            self._symbols = normalized
        if self._change_sequence < 1:
            raise ValueError("watchlist_change_sequence 必须大于等于 1")
        self._refresh_sequence += 1
        token = WatchlistRequestToken(
            selection_generation=identity.selection_generation,
            market=identity.market,
            source=identity.source,
            symbols=normalized,
            watchlist_change_sequence=self._change_sequence,
            watchlist_refresh_sequence=self._refresh_sequence,
        )
        self._latest = token
        return token

    def accepts(
        self,
        token: WatchlistRequestToken,
        *,
        current_identity: SelectionIdentity,
    ) -> bool:
        return (
            self._latest == token
            and token.selection_generation == current_identity.selection_generation
            and token.market == current_identity.market
            and token.source == current_identity.source
        )


class SelectionGenerationGate:
    """页面控制器使用的最小 generation/sequence 状态机。"""

    def __init__(self) -> None:
        self._generation = 0
        self._committed: SelectionIdentity | None = None
        self._staged: SelectionIdentity | None = None
        self._sequences: dict[tuple[int, RequestFamily], int] = {}

    @property
    def committed(self) -> SelectionIdentity | None:
        return self._committed

    @property
    def staged(self) -> SelectionIdentity | None:
        return self._staged

    def stage(
        self,
        *,
        market: MarketCode,
        source: QuoteSource,
        symbol: str,
        display_timeframe: str,
    ) -> SelectionIdentity:
        self._generation += 1
        identity = SelectionIdentity(
            selection_generation=self._generation,
            market=market,
            source=source,
            symbol=symbol,
            display_timeframe=display_timeframe,
        )
        self._staged = identity
        return identity

    def issue(
        self,
        identity: SelectionIdentity,
        family: RequestFamily,
    ) -> RequestToken:
        if self._staged != identity and self._committed != identity:
            raise ValueError("只能为当前 staged 或 committed 选择创建请求")
        key = (identity.selection_generation, family)
        sequence = self._sequences.get(key, 0) + 1
        self._sequences[key] = sequence
        return RequestToken(identity, family, sequence)

    def accepts(self, token: RequestToken) -> bool:
        current = self._staged or self._committed
        key = (token.identity.selection_generation, token.family)
        return current == token.identity and self._sequences.get(key, 0) == token.request_sequence

    def commit(self, identity: SelectionIdentity) -> None:
        if self._staged != identity:
            raise ValueError("只能提交当前 staged 选择")
        self._committed = identity
        self._staged = None

    def abort(self, identity: SelectionIdentity) -> None:
        if self._staged != identity:
            raise ValueError("只能放弃当前 staged 选择")
        self._staged = None
        if self._committed is not None:
            generation = self._committed.selection_generation
            for family in RequestFamily:
                key = (generation, family)
                if key in self._sequences:
                    self._sequences[key] += 1


@dataclass(frozen=True, slots=True)
class KlineEvidenceView:
    """由时间与根数证据自行计算状态，不接受调用方直接声明 READY。"""

    schema_version: int
    selection_generation: int
    request_sequence: int
    symbol: str
    market: MarketCode
    source: QuoteSource
    timeframe: str
    bar_count: int
    closed_bar_count: int
    required_closed_bars: int
    latest_closed_ts_utc_ms: int | None
    received_at_utc_ms: int
    analysis_as_of_utc_ms: int
    now_utc_ms: int
    max_age_ms: int
    price_tick: str | None
    session_paused: bool = False
    failure_reason: str | None = None
    state: EvidenceState = field(init=False)

    def __post_init__(self) -> None:
        identity = SelectionIdentity(
            selection_generation=self.selection_generation,
            market=self.market,
            source=self.source,
            symbol=self.symbol,
            display_timeframe=self.timeframe,
        )
        if self.schema_version != 1:
            raise ValueError("KlineEvidenceView schema_version 当前必须为 1")
        if self.request_sequence < 1:
            raise ValueError("request_sequence 必须大于等于 1")
        if min(self.bar_count, self.closed_bar_count, self.required_closed_bars) < 0:
            raise ValueError("K 线数量不能为负数")
        if self.closed_bar_count > self.bar_count:
            raise ValueError("已收盘 K 线数量不能超过总数")
        if self.received_at_utc_ms < 0 or self.analysis_as_of_utc_ms < 0:
            raise ValueError("接收时间与分析截止时间不能为负数")
        if self.now_utc_ms < 0 or self.max_age_ms <= 0:
            raise ValueError("当前时间必须非负，最大年龄必须为正数")
        if self.received_at_utc_ms > self.now_utc_ms + 5_000:
            raise ValueError("K 线接收时间快于当前时间超过 5 秒")
        if self.latest_closed_ts_utc_ms is not None and self.latest_closed_ts_utc_ms < 0:
            raise ValueError("最新收盘时间不能为负数")
        if (
            self.latest_closed_ts_utc_ms is not None
            and self.latest_closed_ts_utc_ms > self.received_at_utc_ms + 5_000
        ):
            raise ValueError("最新已收盘 K 线时间快于接收时间超过 5 秒")
        if (
            self.latest_closed_ts_utc_ms is not None
            and self.latest_closed_ts_utc_ms > self.now_utc_ms + 5_000
        ):
            raise ValueError("最新已收盘 K 线时间快于当前时间超过 5 秒")
        if self.analysis_as_of_utc_ms > self.now_utc_ms + 5_000:
            raise ValueError("分析截止时间快于当前时间超过 5 秒")
        if (
            self.latest_closed_ts_utc_ms is not None
            and self.latest_closed_ts_utc_ms > self.analysis_as_of_utc_ms + 5_000
        ):
            raise ValueError("最新已收盘 K 线快于分析截止时间超过 5 秒")
        object.__setattr__(self, "symbol", identity.symbol)
        object.__setattr__(self, "market", identity.market)
        object.__setattr__(self, "source", identity.source)
        object.__setattr__(
            self,
            "price_tick",
            _normalise_decimal(
                self.price_tick,
                field_name="price_tick",
                allow_none=True,
                positive=True,
            ),
        )
        if self.failure_reason:
            state = EvidenceState.UNAVAILABLE
        elif (
            self.closed_bar_count < self.required_closed_bars
            or self.latest_closed_ts_utc_ms is None
        ):
            state = EvidenceState.INSUFFICIENT
        elif (
            not self.session_paused
            and self.now_utc_ms - self.latest_closed_ts_utc_ms > self.max_age_ms
        ):
            state = EvidenceState.STALE
        else:
            state = EvidenceState.READY
        object.__setattr__(self, "state", state)


@dataclass(frozen=True, slots=True)
class MarketDataBundle:
    """一次冻结的报价和三周期证据；1h/4h 缺失不阻断 10m。"""

    schema_version: int
    token: RequestToken
    analysis_as_of_utc_ms: int
    quote: QuoteFreshnessView
    ten_minute: KlineEvidenceView
    one_hour: KlineEvidenceView | None
    four_hour: KlineEvidenceView | None
    analysis_state: AnalysisCapabilityState = field(init=False)
    analysis_reason: AnalysisGateReason = field(init=False)
    ready_higher_timeframes: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("MarketDataBundle schema_version 当前必须为 1")
        if self.token.family is not RequestFamily.KLINE:
            raise ValueError("MarketDataBundle 只接受 KLINE 请求 token")
        if self.analysis_as_of_utc_ms < 0:
            raise ValueError("analysis_as_of_utc_ms 不能为负数")

        identity = self.token.identity
        if self.quote.generation != identity.selection_generation:
            raise ValueError("报价 generation 与数据包 identity 不一致")
        snapshot = self.quote.snapshot
        if snapshot is not None:
            if not snapshot.matches(
                identity,
                request_sequence=self.quote.request_sequence,
            ):
                raise ValueError("报价 identity 或 request_sequence 不一致")
            if (
                snapshot.quote_ts_utc_ms > self.analysis_as_of_utc_ms + 5_000
                or snapshot.received_at_utc_ms > self.analysis_as_of_utc_ms + 5_000
            ):
                raise ValueError("报价时间快于 analysis_as_of")

        def validate_kline(
            evidence: KlineEvidenceView,
            *,
            expected_timeframe: str,
        ) -> None:
            if evidence.timeframe != expected_timeframe:
                raise ValueError(f"K 线周期应为 {expected_timeframe}，实际为 {evidence.timeframe}")
            if (
                evidence.selection_generation != identity.selection_generation
                or evidence.market != identity.market
                or evidence.source != identity.source
                or evidence.symbol != identity.symbol
            ):
                raise ValueError("K 线 identity 与数据包不一致")
            if evidence.request_sequence != self.token.request_sequence:
                raise ValueError("K 线 request_sequence 与数据包不一致")
            if evidence.analysis_as_of_utc_ms != self.analysis_as_of_utc_ms:
                raise ValueError("K 线 analysis_as_of 与数据包不一致")

        validate_kline(self.ten_minute, expected_timeframe="10m")
        if self.one_hour is not None:
            validate_kline(self.one_hour, expected_timeframe="1h")
        if self.four_hour is not None:
            validate_kline(self.four_hour, expected_timeframe="4h")

        ready_higher = tuple(
            timeframe
            for timeframe, evidence in (
                ("1h", self.one_hour),
                ("4h", self.four_hour),
            )
            if evidence is not None and evidence.state is EvidenceState.READY
        )
        object.__setattr__(
            self,
            "ready_higher_timeframes",
            ready_higher,
        )

        quote_ready = snapshot is not None and self.quote.freshness in {
            QuoteFreshness.FRESH,
            QuoteFreshness.SESSION_PAUSED,
        }
        if not quote_ready:
            state = AnalysisCapabilityState.BLOCKED
            reason = AnalysisGateReason.QUOTE_NOT_READY
        elif self.ten_minute.state is not EvidenceState.READY:
            state = AnalysisCapabilityState.BLOCKED
            reason = AnalysisGateReason.TEN_MINUTE_NOT_READY
        else:
            ticks = {
                tick
                for tick in (
                    snapshot.price_tick,
                    self.ten_minute.price_tick,
                )
                if tick is not None
            }
            if len(ticks) > 1:
                raise ValueError("报价与 10m K 线的 price_tick 不一致")
            if not ticks:
                state = AnalysisCapabilityState.DISPLAY_ONLY
                reason = AnalysisGateReason.PRICE_TICK_UNAVAILABLE
            else:
                state = AnalysisCapabilityState.READY
                reason = AnalysisGateReason.OK
        object.__setattr__(self, "analysis_state", state)
        object.__setattr__(self, "analysis_reason", reason)

    @property
    def analysis_allowed(self) -> bool:
        return self.analysis_state is AnalysisCapabilityState.READY


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_score(value: object, *, field_name: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是 0 到 100 的整数")
    try:
        score = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是 0 到 100 的整数") from exc
    if score < 0 or score > 100:
        raise ValueError(f"{field_name} 必须是 0 到 100 的整数")
    return score


@dataclass(frozen=True, slots=True)
class AnalysisResultView:
    """分析页专用完成结果；构造过程不进入 execution 或交易运行态。"""

    schema_version: int
    selection_generation: int
    request_sequence: int
    symbol: str
    market: MarketCode
    source: QuoteSource
    analysis_timeframe: str
    state: AnalysisResultState
    cycle_position: str | None
    direction: str | None
    diagnosis_confidence: int | None
    order_type: str | None
    order_direction: str | None
    trade_confidence: int | None
    entry_price: str | None
    stop_loss: str | None
    take_profit: str | None
    take_profit_2: str | None
    terminal_outcome: str | None
    reasoning: str | None
    error_category: str | None
    error_stage: str | None

    @classmethod
    def from_record(
        cls,
        record: Any,
        *,
        token: RequestToken,
        gate: SelectionGenerationGate,
    ) -> AnalysisResultView:
        """把已完成记录投影成只读视图，拒绝错标的和迟到请求。"""
        if token.family != RequestFamily.ANALYSIS:
            raise ValueError("AnalysisResultView 只接受 analysis 请求")
        if not gate.accepts(token):
            raise ValueError("分析请求已经被较新的 generation 或 sequence 取代")
        meta = getattr(record, "meta", None)
        if meta is None:
            raise ValueError("分析记录缺少 meta")
        symbol = str(getattr(meta, "symbol", "") or "").strip()
        timeframe = str(getattr(meta, "timeframe", "") or "").strip()
        data_source = str(getattr(meta, "data_source", "") or "").strip().lower()
        if symbol != token.identity.symbol or timeframe != "10m":
            raise ValueError("分析记录与请求的标的或固定 10m 周期不一致")
        if data_source != token.identity.source:
            raise ValueError("分析记录的数据源与请求不一致")

        exception = getattr(record, "exception", None)
        stage1 = getattr(record, "stage1_diagnosis", None) or {}
        stage2 = getattr(record, "stage2_decision", None) or {}
        if not isinstance(stage1, dict) or not isinstance(stage2, dict):
            raise ValueError("分析记录的诊断或决策字段类型无效")
        if exception is not None and not isinstance(exception, dict):
            raise ValueError("分析记录 exception 字段类型无效")
        decision = stage2.get("decision")
        if not isinstance(decision, dict):
            decision = {}
        diagnosis_summary = stage2.get("diagnosis_summary")
        if not isinstance(diagnosis_summary, dict):
            diagnosis_summary = stage1
        terminal = stage2.get("terminal")
        if not isinstance(terminal, dict):
            terminal = {}
        if exception is None:
            if not decision.get("order_type") or terminal.get("outcome") not in {
                "trade",
                "wait",
                "reject",
            }:
                raise ValueError("成功分析记录缺少正式 decision 或 terminal")
            succeeded = True
        else:
            succeeded = False

        return cls(
            schema_version=1,
            selection_generation=token.identity.selection_generation,
            request_sequence=token.request_sequence,
            symbol=symbol,
            market=token.identity.market,
            source=token.identity.source,
            analysis_timeframe="10m",
            state=(AnalysisResultState.SUCCEEDED if succeeded else AnalysisResultState.FAILED),
            cycle_position=_optional_text(diagnosis_summary.get("cycle_position")),
            direction=_optional_text(diagnosis_summary.get("direction")),
            diagnosis_confidence=_optional_score(
                decision.get("diagnosis_confidence"),
                field_name="diagnosis_confidence",
            ),
            order_type=_optional_text(decision.get("order_type")),
            order_direction=_optional_text(decision.get("order_direction")),
            trade_confidence=_optional_score(
                decision.get("trade_confidence"),
                field_name="trade_confidence",
            ),
            entry_price=_normalise_decimal(
                decision.get("entry_price"),
                field_name="entry_price",
                allow_none=True,
                positive=True,
            ),
            stop_loss=_normalise_decimal(
                decision.get("stop_loss_price"),
                field_name="stop_loss",
                allow_none=True,
                positive=True,
            ),
            take_profit=_normalise_decimal(
                decision.get("take_profit_price"),
                field_name="take_profit",
                allow_none=True,
                positive=True,
            ),
            take_profit_2=_normalise_decimal(
                decision.get("take_profit_price_2"),
                field_name="take_profit_2",
                allow_none=True,
                positive=True,
            ),
            terminal_outcome=_optional_text(terminal.get("outcome")),
            reasoning=_optional_text(decision.get("reasoning")),
            error_category=(
                _optional_text(exception.get("category")) if isinstance(exception, dict) else None
            ),
            error_stage=(
                _optional_text(exception.get("stage")) if isinstance(exception, dict) else None
            ),
        )
