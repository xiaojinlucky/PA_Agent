"""多市场页专用的双源只读运行层。

本模块只读取公开行情或 Longbridge QuoteContext。它不接收账户、持仓、
订单或 execution service，也不向券商写入任何内容。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from pa_agent.data.base import (
    DataSourceAuthenticationError,
    DataSourceError,
    DataSourcePermissionError,
    DataSourceTransientError,
    KlineBar,
)
from pa_agent.data.market_calendar import (
    MarketCalendarError,
    SessionPhase,
    session_state,
)
from pa_agent.data.market_workspace import (
    KlineEvidenceView,
    MarketClockView,
    MarketDataBundle,
    MarketTimeframePayload,
    MarketWorkspaceRenderPayload,
    QuoteFailureKind,
    WatchlistQuoteSet,
    WatchlistRequestToken,
    evaluate_quote_freshness,
)
from pa_agent.data.market_workspace_controller import (
    MarketDataRequest,
    WatchlistDataRequest,
)
from pa_agent.data.snapshot import (
    INDICATOR_WARMUP_BARS,
    build_analysis_frame,
    build_live_frame,
)

_TIMEFRAME_INTERVAL_MS = {
    "10m": 10 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
}
_MARKET_TIMEZONES = {
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
    "CN": "Asia/Shanghai",
    "Crypto": "UTC",
}


@dataclass(frozen=True, slots=True)
class MarketWorkspaceLoadResult:
    """后台读取完成、等待 Controller 做最终提交的完整结果。"""

    request: MarketDataRequest
    bundle: MarketDataBundle
    render_payload: MarketWorkspaceRenderPayload


class MarketWorkspaceRuntimeError(RuntimeError):
    """不暴露供应商异常文本的稳定行情失败。"""

    def __init__(self, failure: QuoteFailureKind) -> None:
        self.failure = failure
        super().__init__(failure.value)


def _default_sources() -> dict[str, Any]:
    from pa_agent.data.longbridge_source import LongbridgeSource
    from pa_agent.data.okx_source import OkxSource

    return {
        "longbridge": LongbridgeSource(),
        "okx": OkxSource(),
    }


class MarketWorkspaceRuntime:
    """串行保护每个可变行情源，并生成 Controller 可校验的冻结载荷。"""

    def __init__(
        self,
        *,
        sources: Mapping[str, Any] | None = None,
        clock_utc_ms: Callable[[], int] | None = None,
        session_state_loader: Callable[[str, int], Any] | None = None,
    ) -> None:
        self._sources = dict(sources or _default_sources())
        required = {"longbridge", "okx"}
        if sources is None and set(self._sources) != required:
            raise ValueError("默认多市场运行层必须同时提供 longbridge 和 okx")
        if not self._sources:
            raise ValueError("多市场运行层至少需要一个只读行情源")
        self._clock_utc_ms = clock_utc_ms or (
            lambda: time.time_ns() // 1_000_000
        )
        self._session_state_loader = (
            session_state_loader or session_state
        )
        self._locks = {
            name: threading.RLock() for name in self._sources
        }
        self._connected = {name: False for name in self._sources}
        self._closed = False

    def _now_utc_ms(self) -> int:
        value = int(self._clock_utc_ms())
        if value < 0:
            raise ValueError("多市场运行层时钟不能返回负数")
        return value

    def _source(self, name: str) -> Any:
        if self._closed:
            raise RuntimeError("多市场运行层已经关闭")
        source = self._sources.get(name)
        if source is None:
            raise MarketWorkspaceRuntimeError(
                QuoteFailureKind.TRANSPORT_FAILED
            )
        return source

    def _ensure_connected(self, source_name: str, source: Any) -> None:
        if self._connected[source_name]:
            return
        source.connect()
        self._connected[source_name] = True

    @staticmethod
    def _failure_for_exception(exc: BaseException) -> QuoteFailureKind:
        if isinstance(exc, DataSourceAuthenticationError):
            return QuoteFailureKind.AUTH_FAILED
        if isinstance(exc, DataSourcePermissionError):
            return QuoteFailureKind.PERMISSION_DENIED
        if isinstance(exc, ValueError):
            return QuoteFailureKind.SYMBOL_UNSUPPORTED
        if isinstance(exc, DataSourceTransientError):
            return QuoteFailureKind.TRANSPORT_FAILED
        if isinstance(exc, DataSourceError):
            return QuoteFailureKind.INVALID_RESPONSE
        return QuoteFailureKind.INVALID_RESPONSE

    def _raise_runtime_error(
        self,
        source_name: str,
        source: Any,
        exc: BaseException,
    ) -> None:
        failure = self._failure_for_exception(exc)
        if failure in {
            QuoteFailureKind.AUTH_FAILED,
            QuoteFailureKind.PERMISSION_DENIED,
        }:
            self._connected[source_name] = False
            with suppress(Exception):
                source.disconnect()
        raise MarketWorkspaceRuntimeError(failure) from exc

    @staticmethod
    def _main_quote_token(request: MarketDataRequest) -> WatchlistRequestToken:
        identity = request.identity
        return WatchlistRequestToken(
            selection_generation=identity.selection_generation,
            market=identity.market,
            source=identity.source,
            symbols=(identity.symbol,),
            watchlist_change_sequence=identity.selection_generation,
            watchlist_refresh_sequence=request.quote_token.request_sequence,
        )

    def load_market_data(
        self,
        request: MarketDataRequest,
        *,
        freeze_request: Callable[[MarketDataRequest], MarketDataRequest],
    ) -> MarketWorkspaceLoadResult:
        """读取报价后由 Controller 冻结 as_of，再读取同一截止时间的三周期。"""

        source_name = request.identity.source
        source = self._source(source_name)
        lock = self._locks[source_name]
        with lock:
            try:
                self._ensure_connected(source_name, source)
                source.subscribe(
                    request.identity.symbol,
                    request.identity.display_timeframe,
                )
                snapshots = source.batch_quote_snapshots(
                    self._main_quote_token(request)
                )
                if len(snapshots) != 1:
                    raise DataSourceTransientError(
                        "主标的报价必须完整返回一项"
                    )
            except Exception as exc:
                self._raise_runtime_error(source_name, source, exc)

            # 必须在报价真实返回之后冻结。若期间页面已经换代，Controller
            # 会直接拒绝；这个 ValueError 不能被伪装成供应商故障。
            bound_request = freeze_request(request)
            as_of = bound_request.analysis_as_of_utc_ms
            if as_of is None:
                raise ValueError("Controller 未冻结 analysis_as_of")

            market_clock = self._market_clock(
                bound_request.identity.market,
                as_of,
            )
            session_paused = market_clock.phase in {"break", "closed"}
            quote = evaluate_quote_freshness(
                snapshots[0],
                identity=bound_request.identity,
                request_sequence=(
                    bound_request.quote_token.request_sequence
                ),
                now_utc_ms=as_of,
                transport_budget_ms=(
                    bound_request.quote_transport_budget_ms
                ),
                session_paused=session_paused,
            )

            timeframe_results: list[
                tuple[KlineEvidenceView, MarketTimeframePayload]
            ] = []
            for timeframe, max_age_ms in (
                ("10m", bound_request.ten_minute_max_age_ms),
                ("1h", bound_request.one_hour_max_age_ms),
                ("4h", bound_request.four_hour_max_age_ms),
            ):
                try:
                    result = self._read_timeframe(
                        source,
                        request=bound_request,
                        timeframe=timeframe,
                        max_age_ms=max_age_ms,
                        session_paused=session_paused,
                    )
                except Exception as exc:
                    if timeframe == "10m":
                        self._raise_runtime_error(
                            source_name,
                            source,
                            exc,
                        )
                    result = self._unavailable_timeframe(
                        request=bound_request,
                        timeframe=timeframe,
                        max_age_ms=max_age_ms,
                        session_paused=session_paused,
                        failure=self._failure_for_exception(exc),
                    )
                timeframe_results.append(result)

            completed_at = self._now_utc_ms()
            evidence = {
                item.timeframe: item
                for item, _payload in timeframe_results
            }
            render_payload = MarketWorkspaceRenderPayload(
                token=bound_request.kline_token,
                analysis_as_of_utc_ms=as_of,
                timeframes=tuple(
                    payload for _item, payload in timeframe_results
                ),
                market_clock=market_clock,
                loaded_at_utc_ms=completed_at,
            )
            bundle = MarketDataBundle(
                schema_version=1,
                token=bound_request.kline_token,
                analysis_as_of_utc_ms=as_of,
                quote=quote,
                ten_minute=evidence["10m"],
                one_hour=evidence["1h"],
                four_hour=evidence["4h"],
            )
            return MarketWorkspaceLoadResult(
                request=bound_request,
                bundle=bundle,
                render_payload=render_payload,
            )

    def _read_timeframe(
        self,
        source: Any,
        *,
        request: MarketDataRequest,
        timeframe: str,
        max_age_ms: int,
        session_paused: bool,
    ) -> tuple[KlineEvidenceView, MarketTimeframePayload]:
        as_of = request.analysis_as_of_utc_ms
        if as_of is None:
            raise ValueError("K 线读取前必须冻结 analysis_as_of")
        fetch_count = (
            request.required_closed_bars
            + INDICATOR_WARMUP_BARS
            + 1
        )
        bars = source.latest_snapshot_for_timeframe(
            timeframe,
            fetch_count,
            analysis_as_of_utc_ms=as_of,
        )
        received_at = self._now_utc_ms()
        closed = [bar for bar in bars if bar.closed]
        latest_closed = (
            self._latest_closed_at_utc_ms(
                source,
                closed,
                timeframe=timeframe,
            )
            if closed
            else None
        )
        price_tick = self._single_price_tick(bars)
        evidence = KlineEvidenceView(
            schema_version=1,
            selection_generation=request.identity.selection_generation,
            request_sequence=request.kline_token.request_sequence,
            symbol=request.identity.symbol,
            market=request.identity.market,
            source=request.identity.source,
            timeframe=timeframe,
            bar_count=len(bars),
            closed_bar_count=len(closed),
            required_closed_bars=request.required_closed_bars,
            latest_closed_ts_utc_ms=latest_closed,
            received_at_utc_ms=received_at,
            analysis_as_of_utc_ms=as_of,
            now_utc_ms=received_at,
            max_age_ms=max_age_ms,
            price_tick=price_tick,
            session_paused=session_paused,
        )
        display_frame = self._build_display_frame(
            bars,
            request=request,
            timeframe=timeframe,
            price_tick=price_tick,
        )
        analysis_count = (
            request.required_closed_bars
            if timeframe == "10m"
            else min(50, request.required_closed_bars)
        )
        analysis_frame = (
            build_analysis_frame(
                bars,
                analysis_count,
                request.identity.symbol,
                timeframe,
                now_ms=as_of,
                price_tick=price_tick,
            )
            if len(closed) >= analysis_count
            else None
        )
        if timeframe == "10m" and analysis_frame is None:
            raise DataSourceTransientError(
                "10m 已收盘 K 线不足，不能冻结分析输入"
            )
        return evidence, MarketTimeframePayload(
            timeframe=timeframe,
            display=display_frame,
            analysis=analysis_frame,
        )

    @staticmethod
    def _latest_closed_at_utc_ms(
        source: Any,
        closed: list[KlineBar],
        *,
        timeframe: str,
    ) -> int:
        resolver = getattr(source, "closed_bar_end_utc_ms", None)
        if not callable(resolver):
            raise DataSourceTransientError(
                "行情源没有提供已收盘 K 线的真实结束时间"
            )
        latest_bar = max(closed, key=lambda bar: int(bar.ts_open))
        try:
            closed_at = int(resolver(latest_bar, timeframe))
        except (TypeError, ValueError) as exc:
            raise DataSourceTransientError(
                "行情源返回了无效的 K 线结束时间"
            ) from exc
        opened_at = int(latest_bar.ts_open)
        interval_ms = _TIMEFRAME_INTERVAL_MS[timeframe]
        if not opened_at < closed_at <= opened_at + interval_ms:
            raise DataSourceTransientError(
                "行情源 K 线结束时间不在声明周期内"
            )
        return closed_at

    @staticmethod
    def _single_price_tick(bars: list[KlineBar]) -> str | None:
        ticks = {
            str(bar.price_tick)
            for bar in bars
            if getattr(bar, "price_tick", None) is not None
        }
        if len(ticks) > 1:
            raise DataSourceTransientError(
                "同一批 K 线包含不一致的行情源最小跳动"
            )
        return next(iter(ticks), None)

    @staticmethod
    def _build_display_frame(
        bars: list[KlineBar],
        *,
        request: MarketDataRequest,
        timeframe: str,
        price_tick: str | None,
    ):
        closed_count = sum(1 for bar in bars if bar.closed)
        if closed_count < 1:
            return None
        return build_live_frame(
            bars,
            min(request.required_closed_bars, closed_count),
            request.identity.symbol,
            timeframe,
            now_ms=request.analysis_as_of_utc_ms,
            price_tick=price_tick,
        )

    def _unavailable_timeframe(
        self,
        *,
        request: MarketDataRequest,
        timeframe: str,
        max_age_ms: int,
        session_paused: bool,
        failure: QuoteFailureKind,
    ) -> tuple[KlineEvidenceView, MarketTimeframePayload]:
        as_of = request.analysis_as_of_utc_ms
        if as_of is None:
            raise ValueError("K 线失败投影前必须冻结 analysis_as_of")
        now = self._now_utc_ms()
        evidence = KlineEvidenceView(
            schema_version=1,
            selection_generation=request.identity.selection_generation,
            request_sequence=request.kline_token.request_sequence,
            symbol=request.identity.symbol,
            market=request.identity.market,
            source=request.identity.source,
            timeframe=timeframe,
            bar_count=0,
            closed_bar_count=0,
            required_closed_bars=request.required_closed_bars,
            latest_closed_ts_utc_ms=None,
            received_at_utc_ms=now,
            analysis_as_of_utc_ms=as_of,
            now_utc_ms=now,
            max_age_ms=max_age_ms,
            price_tick=None,
            session_paused=session_paused,
            failure_reason=failure.value,
        )
        return evidence, MarketTimeframePayload(
            timeframe=timeframe,
            display=None,
            analysis=None,
        )

    def _market_clock(
        self,
        market: str,
        as_of_utc_ms: int,
    ) -> MarketClockView:
        if market == "Crypto":
            return MarketClockView(
                market="Crypto",
                phase="continuous",
                is_half_day=False,
                as_of_utc_ms=as_of_utc_ms,
                next_change_utc_ms=None,
                timezone_name="UTC",
            )
        try:
            state = self._session_state_loader(market, as_of_utc_ms)
            phase = {
                SessionPhase.OPEN: "open",
                SessionPhase.BREAK: "break",
                SessionPhase.CLOSED: "closed",
            }[state.phase]
            return MarketClockView(
                market=market,  # type: ignore[arg-type]
                phase=phase,  # type: ignore[arg-type]
                is_half_day=state.is_half_day,
                as_of_utc_ms=as_of_utc_ms,
                next_change_utc_ms=state.next_change_utc_ms,
                timezone_name=_MARKET_TIMEZONES[market],
            )
        except (MarketCalendarError, KeyError, RuntimeError, ValueError):
            return MarketClockView(
                market=market,  # type: ignore[arg-type]
                phase="unknown",
                is_half_day=False,
                as_of_utc_ms=as_of_utc_ms,
                next_change_utc_ms=None,
                timezone_name=_MARKET_TIMEZONES.get(market, "UTC"),
            )

    def load_watchlist(
        self,
        request: WatchlistDataRequest,
    ) -> WatchlistQuoteSet:
        """一次批量调用刷新当前市场自选，不改变 Controller 身份。"""

        source_name = request.identity.source
        source = self._source(source_name)
        with self._locks[source_name]:
            try:
                self._ensure_connected(source_name, source)
                snapshots = source.batch_quote_snapshots(request.token)
                return WatchlistQuoteSet(
                    token=request.token,
                    snapshots=tuple(snapshots),
                )
            except Exception as exc:
                self._raise_runtime_error(source_name, source, exc)

    def close(self) -> None:
        """关闭只读行情上下文；重复调用安全。"""

        if self._closed:
            return
        self._closed = True
        for name, source in self._sources.items():
            with self._locks[name]:
                if not self._connected[name]:
                    continue
                try:
                    source.disconnect()
                finally:
                    self._connected[name] = False
