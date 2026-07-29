"""多市场看盘页的纯 Python 状态控制器。

后台线程和界面只负责执行请求。选择身份、截止时间、请求终态、设置保存、
行情可用性和分析状态都由本控制器统一裁决。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from pa_agent.config.settings import (
    MarketWorkspacePersistenceBaseline,
    MarketWorkspaceSettings,
    Settings,
)
from pa_agent.data.market_workspace import (
    AnalysisResultState,
    AnalysisResultView,
    EvidenceState,
    MarketCode,
    MarketDataBundle,
    MarketWorkspaceRenderPayload,
    QuoteFailureKind,
    QuoteFreshness,
    RequestFamily,
    RequestToken,
    SelectionGenerationGate,
    SelectionIdentity,
    WatchlistGenerationGate,
    WatchlistQuoteSet,
    WatchlistRequestToken,
    evaluate_quote_freshness,
    quote_source_for_market,
)


class SelectionState(StrEnum):
    UNINITIALIZED = "uninitialized"
    STAGING = "staging"
    COMMITTED = "committed"
    SWITCH_FAILED = "switch_failed"
    AUTH_INVALID = "auth_invalid"


class SettingsSaveState(StrEnum):
    SAVED = "saved"
    SAVING = "saving"
    FAILED = "failed"
    CONFLICT = "conflict"


class SettingsSaveFailureKind(StrEnum):
    WRITE_FAILED = "write_failed"
    REVISION_CONFLICT = "revision_conflict"


class AnalysisState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class AnalysisFailureKind(StrEnum):
    INVALID_RESULT = "invalid_result"
    WORKER_FAILED = "worker_failed"


class AnalysisFailureStage(StrEnum):
    SERVICE_INITIALIZATION = "service_initialization"
    INPUT_FREEZE = "input_freeze"
    MARKET_DIAGNOSIS = "market_diagnosis"
    DECISION_GENERATION = "decision_generation"
    RESULT_VALIDATION = "result_validation"


class SourceAuthState(StrEnum):
    UNKNOWN = "unknown"
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class MarketDataRequest:
    identity: SelectionIdentity
    connect_token: RequestToken
    static_info_token: RequestToken
    quote_token: RequestToken
    kline_token: RequestToken
    source_request_sequence: int
    required_closed_bars: int
    quote_transport_budget_ms: int
    ten_minute_max_age_ms: int
    one_hour_max_age_ms: int
    four_hour_max_age_ms: int
    analysis_as_of_utc_ms: int | None = None

    def __post_init__(self) -> None:
        tokens = (
            (self.connect_token, RequestFamily.CONNECT),
            (self.static_info_token, RequestFamily.STATIC_INFO),
            (self.quote_token, RequestFamily.QUOTE),
            (self.kline_token, RequestFamily.KLINE),
        )
        for token, family in tokens:
            if token.identity != self.identity:
                raise ValueError(f"{family.value} token 与选择 identity 不一致")
            if token.family is not family:
                raise ValueError(f"token 必须属于 {family.value} 请求")
        if self.source_request_sequence < 1:
            raise ValueError("source_request_sequence 必须大于等于 1")
        if self.required_closed_bars < 2:
            raise ValueError("required_closed_bars 至少为 2")
        if min(
            self.quote_transport_budget_ms,
            self.ten_minute_max_age_ms,
            self.one_hour_max_age_ms,
            self.four_hour_max_age_ms,
        ) <= 0:
            raise ValueError("行情与 K 线时间预算必须为正数")
        if (
            self.analysis_as_of_utc_ms is not None
            and self.analysis_as_of_utc_ms < 0
        ):
            raise ValueError("analysis_as_of_utc_ms 不能为负数")


@dataclass(frozen=True, slots=True)
class WatchlistDataRequest:
    identity: SelectionIdentity
    token: WatchlistRequestToken
    source_request_sequence: int

    def __post_init__(self) -> None:
        if (
            self.token.selection_generation
            != self.identity.selection_generation
            or self.token.market != self.identity.market
            or self.token.source != self.identity.source
        ):
            raise ValueError("自选请求与页面 identity 不一致")
        if self.source_request_sequence < 1:
            raise ValueError("source_request_sequence 必须大于等于 1")


@dataclass(frozen=True, slots=True)
class WorkspaceSaveRequest:
    token: RequestToken
    baseline: MarketWorkspacePersistenceBaseline
    workspace: MarketWorkspaceSettings

    def __post_init__(self) -> None:
        if self.token.family is not RequestFamily.SETTINGS:
            raise ValueError("设置保存 token 必须属于 SETTINGS 请求")


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    token: RequestToken
    bundle: MarketDataBundle
    render_payload: MarketWorkspaceRenderPayload

    def __post_init__(self) -> None:
        if self.token.family is not RequestFamily.ANALYSIS:
            raise ValueError("分析 token 必须属于 ANALYSIS 请求")
        if self.bundle.token.identity != self.token.identity:
            raise ValueError("分析请求与冻结数据包 identity 不一致")
        if self.render_payload.token.identity != self.token.identity:
            raise ValueError("分析请求与冻结图表载荷 identity 不一致")
        if (
            self.render_payload.analysis_as_of_utc_ms
            != self.bundle.analysis_as_of_utc_ms
        ):
            raise ValueError("分析请求的数据包与图表载荷 as_of 不一致")


@dataclass(frozen=True, slots=True)
class MarketDataApplyResult:
    accepted: bool
    save_request: WorkspaceSaveRequest | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceViewState:
    selection_state: SelectionState
    committed_identity: SelectionIdentity | None
    staged_identity: SelectionIdentity | None
    failed_identity: SelectionIdentity | None
    bundle: MarketDataBundle | None
    render_payload: MarketWorkspaceRenderPayload | None
    bundle_current: bool
    watchlist: WatchlistQuoteSet | None
    last_market_failure: QuoteFailureKind | None
    longbridge_auth: SourceAuthState
    okx_auth: SourceAuthState
    settings_save_state: SettingsSaveState
    settings_save_failure: SettingsSaveFailureKind | None
    analysis_state: AnalysisState
    active_analysis_token: RequestToken | None
    analysis_failure: AnalysisFailureKind | None
    analysis_failure_stage: AnalysisFailureStage | None
    analysis_result: AnalysisResultView | None
    analysis_history: tuple[AnalysisResultView, ...]


class MarketWorkspaceController:
    """多市场页所有异步身份与页面状态的唯一所有者。"""

    def __init__(
        self,
        settings: Settings,
        *,
        clock_utc_ms: Callable[[], int] | None = None,
        quote_transport_budget_ms: int | None = None,
        ten_minute_max_age_ms: int | None = None,
        one_hour_max_age_ms: int = 2 * 60 * 60 * 1_000,
        four_hour_max_age_ms: int = 8 * 60 * 60 * 1_000,
    ) -> None:
        self._settings_snapshot = settings.model_copy(deep=True)
        self._workspace = settings.market_workspace.model_copy(deep=True)
        self._clock_utc_ms = clock_utc_ms or (
            lambda: time.time_ns() // 1_000_000
        )
        refresh_interval_ms = int(settings.general.refresh_interval_ms)
        kline_grace_ms = max(15_000, 3 * refresh_interval_ms)
        self._quote_transport_budget_ms = int(
            quote_transport_budget_ms
            if quote_transport_budget_ms is not None
            else kline_grace_ms
        )
        self._ten_minute_max_age_ms = int(
            ten_minute_max_age_ms
            if ten_minute_max_age_ms is not None
            else 10 * 60 * 1_000 + kline_grace_ms
        )
        self._one_hour_max_age_ms = int(one_hour_max_age_ms)
        self._four_hour_max_age_ms = int(four_hour_max_age_ms)
        if min(
            self._quote_transport_budget_ms,
            self._ten_minute_max_age_ms,
            self._one_hour_max_age_ms,
            self._four_hour_max_age_ms,
        ) <= 0:
            raise ValueError("行情与 K 线时间预算必须为正数")

        self._gate = SelectionGenerationGate()
        self._watchlist_gate = WatchlistGenerationGate()
        self._selection_state = SelectionState.UNINITIALIZED
        self._failed_identity: SelectionIdentity | None = None
        self._bundle: MarketDataBundle | None = None
        self._render_payload: MarketWorkspaceRenderPayload | None = None
        self._bundle_contract: MarketDataRequest | None = None
        self._bundle_current = False
        self._watchlist: WatchlistQuoteSet | None = None
        self._last_market_failure: QuoteFailureKind | None = None
        self._auth: dict[str, SourceAuthState] = {
            "longbridge": SourceAuthState.UNKNOWN,
            "okx": SourceAuthState.UNKNOWN,
        }
        self._source_request_sequences = {"longbridge": 0, "okx": 0}
        self._source_auth_terminal_sequences = {
            "longbridge": 0,
            "okx": 0,
        }
        self._issued_market: dict[RequestToken, MarketDataRequest] = {}
        self._issued_watchlist: dict[
            WatchlistRequestToken,
            WatchlistDataRequest,
        ] = {}

        self._settings_save_state = SettingsSaveState.SAVED
        self._settings_save_failure: SettingsSaveFailureKind | None = None
        self._save_inflight: WorkspaceSaveRequest | None = None

        self._analysis_state = AnalysisState.IDLE
        self._analysis_failure: AnalysisFailureKind | None = None
        self._analysis_failure_stage: AnalysisFailureStage | None = None
        self._analysis_result: AnalysisResultView | None = None
        self._analysis_history: list[AnalysisResultView] = []
        self._active_analysis: AnalysisRequest | None = None
        self._issued_analysis: dict[RequestToken, AnalysisRequest] = {}

    @property
    def view(self) -> WorkspaceViewState:
        return WorkspaceViewState(
            selection_state=self._selection_state,
            committed_identity=self._gate.committed,
            staged_identity=self._gate.staged,
            failed_identity=self._failed_identity,
            bundle=self._bundle,
            render_payload=self._render_payload,
            bundle_current=self._bundle_current,
            watchlist=self._watchlist,
            last_market_failure=self._last_market_failure,
            longbridge_auth=self._auth["longbridge"],
            okx_auth=self._auth["okx"],
            settings_save_state=self._settings_save_state,
            settings_save_failure=self._settings_save_failure,
            analysis_state=self._analysis_state,
            active_analysis_token=(
                self._active_analysis.token
                if self._active_analysis is not None
                else None
            ),
            analysis_failure=self._analysis_failure,
            analysis_failure_stage=self._analysis_failure_stage,
            analysis_result=self._analysis_result,
            analysis_history=tuple(self._analysis_history),
        )

    @property
    def settings_snapshot(self) -> Settings:
        return self._settings_snapshot.model_copy(deep=True)

    @property
    def workspace_settings(self) -> MarketWorkspaceSettings:
        return self._workspace.model_copy(deep=True)

    def _now_utc_ms(self) -> int:
        now = int(self._clock_utc_ms())
        if now < 0:
            raise ValueError("控制器时钟不能返回负数")
        return now

    def _next_source_sequence(self, source: str) -> int:
        self._source_request_sequences[source] += 1
        return self._source_request_sequences[source]

    def begin_initial_load(self) -> MarketDataRequest:
        market = self._workspace.selected_market
        return self.begin_selection(
            market=market,
            symbol=self._workspace.last_symbols_by_market[market],
            display_timeframe=self._workspace.display_timeframes_by_market[
                market
            ],
        )

    def begin_selection(
        self,
        *,
        market: MarketCode,
        symbol: str,
        display_timeframe: str,
    ) -> MarketDataRequest:
        self._invalidate_analysis_for_data_change()
        self._watchlist_gate.invalidate()
        identity = self._gate.stage(
            market=market,
            source=quote_source_for_market(market),
            symbol=symbol,
            display_timeframe=display_timeframe,
        )
        self._selection_state = SelectionState.STAGING
        self._failed_identity = None
        self._last_market_failure = None
        return self._issue_market_data_request(identity)

    def refresh_current(self) -> MarketDataRequest:
        if self._gate.staged is not None:
            raise ValueError("切换尚未完成，不能刷新旧选择")
        identity = self._gate.committed
        if identity is None:
            raise ValueError("页面尚无已提交选择")
        self._invalidate_analysis_for_data_change()
        self._bundle_current = False
        self._failed_identity = None
        self._last_market_failure = None
        return self._issue_market_data_request(identity)

    def _issue_market_data_request(
        self,
        identity: SelectionIdentity,
    ) -> MarketDataRequest:
        request = MarketDataRequest(
            identity=identity,
            connect_token=self._gate.issue(
                identity,
                RequestFamily.CONNECT,
            ),
            static_info_token=self._gate.issue(
                identity,
                RequestFamily.STATIC_INFO,
            ),
            quote_token=self._gate.issue(
                identity,
                RequestFamily.QUOTE,
            ),
            kline_token=self._gate.issue(
                identity,
                RequestFamily.KLINE,
            ),
            source_request_sequence=self._next_source_sequence(
                identity.source
            ),
            required_closed_bars=int(
                self._settings_snapshot.general.analysis_bar_count
            ),
            quote_transport_budget_ms=self._quote_transport_budget_ms,
            ten_minute_max_age_ms=self._ten_minute_max_age_ms,
            one_hour_max_age_ms=self._one_hour_max_age_ms,
            four_hour_max_age_ms=self._four_hour_max_age_ms,
        )
        self._issued_market[request.kline_token] = request
        return request

    def freeze_analysis_as_of(
        self,
        request: MarketDataRequest,
    ) -> MarketDataRequest:
        issued = self._issued_market.get(request.kline_token)
        if issued is None:
            raise ValueError("行情请求已经终结或不是本控制器发出")
        if issued != request:
            raise ValueError("行情请求快照已被修改")
        if request.analysis_as_of_utc_ms is not None:
            raise ValueError("analysis_as_of 已经冻结")
        if not self._accepts_market_tokens(request):
            self._issued_market.pop(request.kline_token, None)
            raise ValueError("行情请求已被新的 generation 或 sequence 取代")
        bound = replace(
            request,
            analysis_as_of_utc_ms=self._now_utc_ms(),
        )
        self._issued_market[bound.kline_token] = bound
        return bound

    def complete_market_data(
        self,
        request: MarketDataRequest,
        bundle: MarketDataBundle,
        render_payload: MarketWorkspaceRenderPayload | None = None,
    ) -> MarketDataApplyResult:
        issued = self._issued_market.get(request.kline_token)
        if issued is None:
            return MarketDataApplyResult(False)
        if issued != request:
            raise ValueError("行情请求快照不是控制器当前冻结版本")
        if request.analysis_as_of_utc_ms is None:
            raise ValueError("行情请求尚未冻结 analysis_as_of")
        if not self._accepts_market_tokens(request):
            self._issued_market.pop(request.kline_token, None)
            return MarketDataApplyResult(False)

        try:
            self._validate_bundle_for_request(request, bundle)
            if render_payload is not None:
                self._validate_render_payload_for_request(
                    request,
                    bundle,
                    render_payload,
                )
            self._assert_bundle_fresh(request, bundle)
        except ValueError:
            self._issued_market.pop(request.kline_token, None)
            self._apply_market_failure(
                request.identity,
                QuoteFailureKind.INVALID_RESPONSE,
            )
            raise

        self._issued_market.pop(request.kline_token, None)
        self._record_source_success(
            request.identity.source,
            request.source_request_sequence,
        )
        was_staged = self._gate.staged == request.identity
        if was_staged:
            self._gate.commit(request.identity)
        elif self._gate.committed != request.identity:
            return MarketDataApplyResult(False)

        self._bundle = bundle
        self._render_payload = render_payload
        self._bundle_contract = request
        self._bundle_current = True
        self._watchlist = None if was_staged else self._watchlist
        self._selection_state = SelectionState.COMMITTED
        self._failed_identity = None
        self._last_market_failure = None
        self._analysis_state = AnalysisState.IDLE
        self._analysis_failure = None
        self._analysis_failure_stage = None
        self._analysis_result = None

        save_request = (
            self._record_committed_selection(request.identity)
            if was_staged
            else None
        )
        return MarketDataApplyResult(True, save_request)

    @staticmethod
    def _validate_render_payload_for_request(
        request: MarketDataRequest,
        bundle: MarketDataBundle,
        render_payload: MarketWorkspaceRenderPayload,
    ) -> None:
        if render_payload.token != request.kline_token:
            raise ValueError("图表载荷请求 token 与控制器请求不一致")
        if (
            render_payload.analysis_as_of_utc_ms
            != request.analysis_as_of_utc_ms
            or render_payload.analysis_as_of_utc_ms
            != bundle.analysis_as_of_utc_ms
        ):
            raise ValueError("图表载荷 analysis_as_of 与控制器请求不一致")
        analysis_frame = render_payload.analysis_frame("10m")
        if analysis_frame is None:
            raise ValueError("图表载荷缺少冻结的 10m 分析帧")
        if len(analysis_frame.bars) < request.required_closed_bars:
            raise ValueError("冻结的 10m 分析帧根数不足")
        if any(not bar.closed for bar in analysis_frame.bars):
            raise ValueError("冻结的 10m 分析帧包含未收盘 K 线")
        if bundle.analysis_allowed:
            ticks = {
                value
                for value in (
                    bundle.ten_minute.price_tick,
                    analysis_frame.price_tick,
                )
                if value is not None
            }
            if len(ticks) != 1:
                raise ValueError("冻结分析帧与分析能力门的 price_tick 不一致")

    def _validate_bundle_for_request(
        self,
        request: MarketDataRequest,
        bundle: MarketDataBundle,
    ) -> None:
        if bundle.token != request.kline_token:
            raise ValueError("数据包请求 token 与控制器请求不一致")
        if (
            bundle.quote.generation
            != request.identity.selection_generation
            or bundle.quote.request_sequence
            != request.quote_token.request_sequence
        ):
            raise ValueError("报价请求 token 与控制器请求不一致")
        if bundle.analysis_as_of_utc_ms != request.analysis_as_of_utc_ms:
            raise ValueError("数据包 analysis_as_of 与控制器请求不一致")

        expected = (
            (bundle.ten_minute, "10m", request.ten_minute_max_age_ms),
            (bundle.one_hour, "1h", request.one_hour_max_age_ms),
            (bundle.four_hour, "4h", request.four_hour_max_age_ms),
        )
        for evidence, timeframe, max_age_ms in expected:
            if evidence is None:
                continue
            if evidence.timeframe != timeframe:
                raise ValueError(f"K 线周期应为 {timeframe}")
            if (
                evidence.required_closed_bars
                != request.required_closed_bars
            ):
                raise ValueError(
                    "K 线 required_closed_bars 与控制器请求不一致"
                )
            if evidence.max_age_ms != max_age_ms:
                raise ValueError("K 线 max_age_ms 与控制器请求不一致")
        if request.identity.market == "Crypto" and (
            bundle.quote.freshness is QuoteFreshness.SESSION_PAUSED
            or any(
                evidence is not None and evidence.session_paused
                for evidence in (
                    bundle.ten_minute,
                    bundle.one_hour,
                    bundle.four_hour,
                )
            )
        ):
            raise ValueError("Crypto 是连续交易市场，不能标记为 session_paused")

    def _assert_bundle_fresh(
        self,
        request: MarketDataRequest,
        bundle: MarketDataBundle,
    ) -> None:
        now_utc_ms = self._now_utc_ms()
        if (
            request.analysis_as_of_utc_ms is None
            or request.analysis_as_of_utc_ms > now_utc_ms + 5_000
        ):
            raise ValueError("analysis_as_of 快于控制器当前时间")
        quote = evaluate_quote_freshness(
            bundle.quote.snapshot,
            identity=request.identity,
            request_sequence=request.quote_token.request_sequence,
            now_utc_ms=now_utc_ms,
            transport_budget_ms=request.quote_transport_budget_ms,
            session_paused=(
                bundle.quote.freshness is QuoteFreshness.SESSION_PAUSED
            ),
        )
        if quote.freshness not in {
            QuoteFreshness.FRESH,
            QuoteFreshness.SESSION_PAUSED,
        }:
            raise ValueError("报价相对控制器当前时间已经过期")

        ten_minute = bundle.ten_minute
        if ten_minute.state is EvidenceState.STALE:
            raise ValueError("10m K 线证据已经过期")
        if ten_minute.state is not EvidenceState.READY:
            raise ValueError("10m K 线证据未就绪")
        latest = ten_minute.latest_closed_ts_utc_ms
        if latest is None:
            raise ValueError("10m K 线缺少最新收盘时间")
        if latest > now_utc_ms + 5_000:
            raise ValueError("10m K 线快于控制器当前时间")
        if (
            not ten_minute.session_paused
            and now_utc_ms - latest > request.ten_minute_max_age_ms
        ):
            raise ValueError("10m K 线相对控制器当前时间已经过期")

    def _accepts_market_tokens(self, request: MarketDataRequest) -> bool:
        return all(
            self._gate.accepts(token)
            for token in (
                request.connect_token,
                request.static_info_token,
                request.quote_token,
                request.kline_token,
            )
        )

    def fail_market_data(
        self,
        request: MarketDataRequest,
        failure: QuoteFailureKind,
    ) -> bool:
        issued = self._issued_market.get(request.kline_token)
        if issued is None:
            return False
        if issued != request:
            raise ValueError("行情失败回调的请求快照已被修改")
        self._issued_market.pop(request.kline_token, None)

        if failure is QuoteFailureKind.AUTH_FAILED:
            return self._record_source_auth_failure(
                source=request.identity.source,
                source_sequence=request.source_request_sequence,
                failed_identity=request.identity,
            )
        if not self._accepts_market_tokens(request):
            return False
        self._apply_market_failure(request.identity, failure)
        return True

    def _apply_market_failure(
        self,
        identity: SelectionIdentity,
        failure: QuoteFailureKind,
    ) -> None:
        self._failed_identity = identity
        self._last_market_failure = failure
        if self._gate.staged == identity:
            self._gate.abort(identity)
            committed = self._gate.committed
            if (
                committed is not None
                and self._auth[committed.source]
                is SourceAuthState.INVALID
            ):
                self._selection_state = SelectionState.AUTH_INVALID
            else:
                self._selection_state = SelectionState.SWITCH_FAILED
            return
        if self._gate.committed != identity:
            return

        self._bundle_current = False
        if failure in {
            QuoteFailureKind.AUTH_FAILED,
            QuoteFailureKind.PERMISSION_DENIED,
            QuoteFailureKind.SYMBOL_UNSUPPORTED,
            QuoteFailureKind.INVALID_RESPONSE,
        }:
            self._bundle = None
            self._render_payload = None
            self._bundle_contract = None
            self._watchlist = None
            self._watchlist_gate.invalidate()
        self._selection_state = (
            SelectionState.AUTH_INVALID
            if failure is QuoteFailureKind.AUTH_FAILED
            else SelectionState.COMMITTED
        )

    def _record_source_success(
        self,
        source: str,
        source_sequence: int,
    ) -> None:
        if (
            source_sequence
            < self._source_auth_terminal_sequences[source]
        ):
            return
        self._source_auth_terminal_sequences[source] = source_sequence
        self._auth[source] = SourceAuthState.VALID
        committed = self._gate.committed
        if (
            committed is not None
            and committed.source == source
            and self._selection_state is SelectionState.AUTH_INVALID
        ):
            self._selection_state = SelectionState.COMMITTED

    def _record_source_auth_failure(
        self,
        *,
        source: str,
        source_sequence: int,
        failed_identity: SelectionIdentity,
    ) -> bool:
        if (
            source_sequence
            < self._source_auth_terminal_sequences[source]
        ):
            return False
        self._source_auth_terminal_sequences[source] = source_sequence
        self._auth[source] = SourceAuthState.INVALID

        for token, pending in tuple(self._issued_market.items()):
            if (
                pending.identity.source == source
                and pending.source_request_sequence <= source_sequence
            ):
                self._issued_market.pop(token, None)
        for token, pending in tuple(self._issued_watchlist.items()):
            if (
                pending.identity.source == source
                and pending.source_request_sequence <= source_sequence
            ):
                self._issued_watchlist.pop(token, None)

        staged = self._gate.staged
        committed = self._gate.committed
        staged_affected = staged is not None and staged.source == source
        committed_affected = (
            committed is not None and committed.source == source
        )
        newer_staged_request = any(
            pending.identity == staged
            and pending.source_request_sequence > source_sequence
            for pending in self._issued_market.values()
        )
        newer_watchlist_request = any(
            pending.identity.source == source
            and pending.source_request_sequence > source_sequence
            for pending in self._issued_watchlist.values()
        )
        if staged_affected and not newer_staged_request and staged is not None:
            self._failed_identity = staged
            self._gate.abort(staged)
        elif committed_affected:
            self._failed_identity = failed_identity

        if staged_affected or committed_affected:
            self._last_market_failure = QuoteFailureKind.AUTH_FAILED
        if committed_affected:
            self._bundle = None
            self._render_payload = None
            self._bundle_contract = None
            self._bundle_current = False
            self._watchlist = None
            if not newer_watchlist_request:
                self._watchlist_gate.invalidate()
            self._cancel_active_analysis()

        if self._gate.staged is not None:
            self._selection_state = SelectionState.STAGING
        elif committed_affected or self._gate.committed is None:
            self._selection_state = SelectionState.AUTH_INVALID
        else:
            self._selection_state = SelectionState.COMMITTED
        return True

    def begin_watchlist_refresh(self) -> WatchlistDataRequest | None:
        if self._gate.staged is not None:
            raise ValueError("切换尚未完成，不能刷新旧自选")
        identity = self._gate.committed
        if identity is None:
            raise ValueError("页面尚无已提交选择")
        symbols = tuple(self._workspace.watchlists_by_market[identity.market])
        if not symbols:
            self._watchlist = None
            return None
        token = self._watchlist_gate.issue(identity, symbols)
        request = WatchlistDataRequest(
            identity=identity,
            token=token,
            source_request_sequence=self._next_source_sequence(
                identity.source
            ),
        )
        self._issued_watchlist[token] = request
        return request

    def complete_watchlist(
        self,
        request: WatchlistDataRequest,
        result: WatchlistQuoteSet,
    ) -> bool:
        issued = self._issued_watchlist.get(request.token)
        if issued is None:
            return False
        if issued != request:
            raise ValueError("自选请求快照已被修改")
        self._issued_watchlist.pop(request.token, None)
        identity = self._gate.committed
        if identity is None or not self._watchlist_gate.accepts(
            request.token,
            current_identity=identity,
        ):
            return False
        if not isinstance(result, WatchlistQuoteSet):
            self._failed_identity = request.identity
            self._last_market_failure = QuoteFailureKind.INVALID_RESPONSE
            self._watchlist = None
            self._watchlist_gate.invalidate()
            raise ValueError("自选结果必须是完整 WatchlistQuoteSet")
        if result.token != request.token:
            self._failed_identity = request.identity
            self._last_market_failure = QuoteFailureKind.INVALID_RESPONSE
            self._watchlist = None
            self._watchlist_gate.invalidate()
            raise ValueError("自选结果 token 与请求不一致")
        self._record_source_success(
            request.identity.source,
            request.source_request_sequence,
        )
        self._watchlist = result
        return True

    def fail_watchlist(
        self,
        request: WatchlistDataRequest,
        failure: QuoteFailureKind,
    ) -> bool:
        issued = self._issued_watchlist.get(request.token)
        if issued is None:
            return False
        if issued != request:
            raise ValueError("自选失败回调的请求快照已被修改")
        self._issued_watchlist.pop(request.token, None)
        if failure is QuoteFailureKind.AUTH_FAILED:
            return self._record_source_auth_failure(
                source=request.identity.source,
                source_sequence=request.source_request_sequence,
                failed_identity=request.identity,
            )
        identity = self._gate.committed
        if identity is None or not self._watchlist_gate.accepts(
            request.token,
            current_identity=identity,
        ):
            return False
        self._failed_identity = request.identity
        self._last_market_failure = failure
        if failure is not QuoteFailureKind.TRANSPORT_FAILED:
            self._watchlist = None
            self._watchlist_gate.invalidate()
        return True

    def set_watchlist(
        self,
        symbols: tuple[str, ...] | list[str],
    ) -> WorkspaceSaveRequest | None:
        if self._gate.staged is not None:
            raise ValueError("切换尚未完成，不能修改旧市场自选")
        identity = self._gate.committed
        if identity is None:
            raise ValueError("页面尚无已提交选择")
        payload = self._workspace.model_dump(mode="python")
        payload["watchlists_by_market"][identity.market] = list(symbols)
        self._workspace = MarketWorkspaceSettings.model_validate(payload)
        self._watchlist_gate.invalidate(
            tuple(self._workspace.watchlists_by_market[identity.market])
        )
        self._watchlist = None
        return self._queue_settings_save()

    def _record_committed_selection(
        self,
        identity: SelectionIdentity,
    ) -> WorkspaceSaveRequest | None:
        payload = self._workspace.model_dump(mode="python")
        payload["selected_market"] = identity.market
        payload["last_symbols_by_market"][identity.market] = identity.symbol
        payload["display_timeframes_by_market"][
            identity.market
        ] = identity.display_timeframe
        updated = MarketWorkspaceSettings.model_validate(payload)
        if updated == self._workspace:
            return None
        self._workspace = updated
        return self._queue_settings_save()

    def _queue_settings_save(self) -> WorkspaceSaveRequest | None:
        self._settings_save_state = SettingsSaveState.SAVING
        self._settings_save_failure = None
        if self._save_inflight is not None:
            return None
        return self._start_settings_save()

    def _start_settings_save(self) -> WorkspaceSaveRequest:
        identity = self._gate.committed
        if identity is None:
            raise ValueError("页面尚无已提交选择，不能保存多市场设置")
        request = WorkspaceSaveRequest(
            token=self._gate.issue(identity, RequestFamily.SETTINGS),
            baseline=MarketWorkspacePersistenceBaseline.from_settings(
                self._settings_snapshot
            ),
            workspace=self._workspace.model_copy(deep=True),
        )
        self._save_inflight = request
        self._settings_save_state = SettingsSaveState.SAVING
        self._settings_save_failure = None
        return request

    def complete_settings_save(
        self,
        request: WorkspaceSaveRequest,
        saved: Settings,
    ) -> WorkspaceSaveRequest | None:
        if request != self._save_inflight:
            return None
        if not isinstance(saved, Settings):
            self.fail_settings_save(
                request,
                SettingsSaveFailureKind.WRITE_FAILED,
            )
            raise ValueError("设置保存结果必须是完整 Settings")
        if saved.revision <= request.baseline.revision:
            self.fail_settings_save(
                request,
                SettingsSaveFailureKind.WRITE_FAILED,
            )
            raise ValueError("设置保存结果 revision 未前进")
        if saved.market_workspace != request.workspace:
            self.fail_settings_save(
                request,
                SettingsSaveFailureKind.WRITE_FAILED,
            )
            raise ValueError("设置保存结果与请求快照不一致")

        self._settings_snapshot = saved.model_copy(deep=True)
        self._save_inflight = None
        if self._workspace != request.workspace:
            return self._start_settings_save()
        self._settings_save_state = SettingsSaveState.SAVED
        self._settings_save_failure = None
        return None

    def fail_settings_save(
        self,
        request: WorkspaceSaveRequest,
        failure: SettingsSaveFailureKind,
    ) -> bool:
        if request != self._save_inflight:
            return False
        self._save_inflight = None
        self._settings_save_failure = failure
        self._settings_save_state = (
            SettingsSaveState.CONFLICT
            if failure is SettingsSaveFailureKind.REVISION_CONFLICT
            else SettingsSaveState.FAILED
        )
        return True

    def retry_settings_save(
        self,
        latest_settings: Settings | None = None,
    ) -> WorkspaceSaveRequest:
        if self._save_inflight is not None:
            raise ValueError("已有设置保存正在进行")
        if self._settings_save_state is SettingsSaveState.CONFLICT:
            raise ValueError("设置冲突不能普通重试，必须明确采用磁盘或覆盖")
        if latest_settings is not None:
            raise ValueError("普通重试不能替换设置保存基线")
        if self._settings_save_state is not SettingsSaveState.FAILED:
            raise ValueError("当前没有可重试的设置写入失败")
        return self._start_settings_save()

    def begin_analysis(self) -> AnalysisRequest:
        if self._active_analysis is not None:
            raise ValueError("已有分析正在进行中")
        if self._gate.staged is not None:
            raise ValueError("切换尚未完成，当前页面不可分析")
        identity = self._gate.committed
        bundle = self._bundle
        render_payload = self._render_payload
        market_request = self._bundle_contract
        if (
            identity is None
            or bundle is None
            or render_payload is None
            or market_request is None
            or not self._bundle_current
            or not bundle.analysis_allowed
        ):
            raise ValueError("当前数据仅可展示或证据不足，不可分析")
        try:
            self._assert_bundle_fresh(market_request, bundle)
        except ValueError as exc:
            self._bundle_current = False
            raise ValueError("当前行情或 10m K 线已经过期，不可分析") from exc

        token = self._gate.issue(identity, RequestFamily.ANALYSIS)
        request = AnalysisRequest(
            token=token,
            bundle=bundle,
            render_payload=render_payload,
        )
        self._issued_analysis[token] = request
        self._active_analysis = request
        self._analysis_state = AnalysisState.RUNNING
        self._analysis_failure = None
        self._analysis_failure_stage = None
        self._analysis_result = None
        return request

    def complete_analysis(
        self,
        request: AnalysisRequest,
        record: object,
    ) -> AnalysisResultView:
        issued = self._issued_analysis.get(request.token)
        if issued is None:
            raise ValueError("分析请求已经终结或不是本控制器发出")
        if issued != request:
            raise ValueError("分析请求快照已被修改")
        try:
            result = AnalysisResultView.from_bound_record(
                record,
                token=request.token,
            )
        except ValueError:
            self._issued_analysis.pop(request.token, None)
            if self._active_analysis == request:
                self._active_analysis = None
                self._analysis_state = AnalysisState.FAILED
                self._analysis_failure = AnalysisFailureKind.INVALID_RESULT
                self._analysis_failure_stage = (
                    AnalysisFailureStage.RESULT_VALIDATION
                )
                self._analysis_result = None
            raise

        self._issued_analysis.pop(request.token, None)
        self._analysis_history.append(result)
        is_current = (
            self._active_analysis == request
            and self._gate.accepts(request.token)
            and self._gate.committed == request.token.identity
            and self._gate.staged is None
            and self._bundle == request.bundle
            and self._bundle_current
        )
        if is_current:
            self._active_analysis = None
            self._analysis_result = result
            self._analysis_failure = None
            self._analysis_failure_stage = None
            self._analysis_state = (
                AnalysisState.SUCCEEDED
                if result.state is AnalysisResultState.SUCCEEDED
                else AnalysisState.FAILED
            )
        elif self._active_analysis == request:
            self._active_analysis = None
        return result

    def fail_analysis(
        self,
        request: AnalysisRequest,
        failure: AnalysisFailureKind | str,
        *,
        stage: AnalysisFailureStage | str | None = None,
    ) -> bool:
        issued = self._issued_analysis.get(request.token)
        if issued is None:
            return False
        if issued != request:
            raise ValueError("分析失败回调的请求快照已被修改")
        try:
            failure_kind = AnalysisFailureKind(str(failure))
        except ValueError as exc:
            self._issued_analysis.pop(request.token, None)
            if self._active_analysis == request:
                self._active_analysis = None
                self._analysis_state = AnalysisState.FAILED
                self._analysis_failure = AnalysisFailureKind.INVALID_RESULT
                self._analysis_failure_stage = (
                    AnalysisFailureStage.RESULT_VALIDATION
                )
                self._analysis_result = None
            raise ValueError("不支持的分析失败类别") from exc
        if stage is None:
            failure_stage = (
                AnalysisFailureStage.RESULT_VALIDATION
                if failure_kind is AnalysisFailureKind.INVALID_RESULT
                else AnalysisFailureStage.MARKET_DIAGNOSIS
            )
        else:
            try:
                failure_stage = AnalysisFailureStage(str(stage))
            except ValueError as exc:
                self._issued_analysis.pop(request.token, None)
                if self._active_analysis == request:
                    self._active_analysis = None
                    self._analysis_state = AnalysisState.FAILED
                    self._analysis_failure = AnalysisFailureKind.INVALID_RESULT
                    self._analysis_failure_stage = (
                        AnalysisFailureStage.RESULT_VALIDATION
                    )
                    self._analysis_result = None
                raise ValueError("不支持的分析失败阶段") from exc
        self._issued_analysis.pop(request.token, None)
        if self._active_analysis != request:
            return False
        self._active_analysis = None
        self._analysis_state = AnalysisState.FAILED
        self._analysis_failure = failure_kind
        self._analysis_failure_stage = failure_stage
        self._analysis_result = None
        return True

    def _cancel_active_analysis(self) -> None:
        if self._active_analysis is not None:
            self._active_analysis = None
            self._analysis_state = AnalysisState.CANCELED
            self._analysis_failure = None
            self._analysis_failure_stage = None

    def _invalidate_analysis_for_data_change(self) -> None:
        had_active_request = self._active_analysis is not None
        self._cancel_active_analysis()
        if not had_active_request:
            self._analysis_state = AnalysisState.IDLE
            self._analysis_failure = None
            self._analysis_failure_stage = None
        self._analysis_result = None
