from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pa_agent.data.market_workspace import (
    AnalysisCapabilityState,
    AnalysisGateReason,
    AnalysisResultState,
    AnalysisResultView,
    EvidenceState,
    KlineEvidenceView,
    MarketDataBundle,
    QuoteFailureKind,
    QuoteFreshness,
    QuoteFreshnessReason,
    QuoteSnapshot,
    RequestFamily,
    RequestToken,
    SelectionGenerationGate,
    SelectionIdentity,
    WatchlistGenerationGate,
    WatchlistQuoteSet,
    WatchlistRequestToken,
    evaluate_quote_freshness,
    quote_failure_view,
)


def _identity(
    *,
    generation: int = 1,
    market: str = "US",
    source: str = "longbridge",
    symbol: str = "AAPL.US",
) -> SelectionIdentity:
    return SelectionIdentity(
        selection_generation=generation,
        market=market,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        symbol=symbol,
        display_timeframe="10m",
    )


def _snapshot(
    *,
    generation: int = 1,
    request_sequence: int = 1,
    quote_ts: int = 100_000,
    received_at: int = 100_500,
) -> QuoteSnapshot:
    return QuoteSnapshot.from_prices(
        selection_generation=generation,
        request_sequence=request_sequence,
        symbol="AAPL.US",
        market="US",
        source="longbridge",
        name="Apple",
        currency="usd",
        last="102.00",
        prev_close="100",
        price_tick="0.01",
        quote_ts_utc_ms=quote_ts,
        received_at_utc_ms=received_at,
    )


def _quote_view(
    *,
    snapshot: QuoteSnapshot | None = None,
    now_ms: int = 100_500,
) -> object:
    return evaluate_quote_freshness(
        snapshot or _snapshot(),
        identity=_identity(),
        request_sequence=1,
        now_utc_ms=now_ms,
        transport_budget_ms=1_500,
    )


def _kline_evidence(
    timeframe: str,
    *,
    analysis_as_of_utc_ms: int = 100_500,
    now_utc_ms: int = 100_500,
    price_tick: str | None = "0.01",
    closed_bar_count: int = 100,
    failure_reason: str | None = None,
) -> KlineEvidenceView:
    return KlineEvidenceView(
        schema_version=1,
        selection_generation=1,
        request_sequence=2,
        symbol="AAPL.US",
        market="US",
        source="longbridge",
        timeframe=timeframe,
        bar_count=101,
        closed_bar_count=closed_bar_count,
        required_closed_bars=100,
        latest_closed_ts_utc_ms=100_000,
        received_at_utc_ms=100_500,
        analysis_as_of_utc_ms=analysis_as_of_utc_ms,
        now_utc_ms=now_utc_ms,
        max_age_ms=1_000,
        price_tick=price_tick,
        failure_reason=failure_reason,
    )


def _market_bundle(
    *,
    quote_snapshot: QuoteSnapshot | None = None,
    ten_minute: KlineEvidenceView | None = None,
    one_hour: KlineEvidenceView | None = None,
    four_hour: KlineEvidenceView | None = None,
) -> MarketDataBundle:
    return MarketDataBundle(
        schema_version=1,
        token=RequestToken(_identity(), RequestFamily.KLINE, 2),
        analysis_as_of_utc_ms=100_500,
        quote=_quote_view(snapshot=quote_snapshot),
        ten_minute=ten_minute or _kline_evidence("10m"),
        one_hour=one_hour,
        four_hour=four_hour,
    )


def test_quote_snapshot_normalises_decimal_values_and_computes_change() -> None:
    snapshot = _snapshot()

    assert snapshot.last == "102"
    assert snapshot.prev_close == "100"
    assert snapshot.change == "2"
    assert snapshot.change_pct == "2"
    assert snapshot.price_tick == "0.01"
    assert snapshot.currency == "USD"


def test_quote_snapshot_allows_missing_provider_declared_price_tick() -> None:
    snapshot = replace(_snapshot(), price_tick=None)

    assert snapshot.price_tick is None


@pytest.mark.parametrize(
    ("market", "source"),
    [
        ("US", "okx"),
        ("HK", "okx"),
        ("CN", "okx"),
        ("Crypto", "longbridge"),
    ],
)
def test_selection_identity_rejects_wrong_market_source_route(
    market: str,
    source: str,
) -> None:
    with pytest.raises(ValueError, match="必须使用"):
        _identity(market=market, source=source)


@pytest.mark.parametrize(
    ("quote_ts", "received_at"),
    [
        (105_501, 100_500),
        (-1, 100_500),
        (100_000, -1),
    ],
)
def test_quote_snapshot_rejects_invalid_clock_evidence(
    quote_ts: int,
    received_at: int,
) -> None:
    with pytest.raises(ValueError):
        _snapshot(quote_ts=quote_ts, received_at=received_at)


def test_realtime_snapshot_rejects_declared_delay() -> None:
    with pytest.raises(ValueError, match="实时行情"):
        replace(_snapshot(), expected_delay_ms=15_000)


def test_quote_snapshot_rejects_unknown_quote_mode() -> None:
    with pytest.raises(ValueError, match="quote_mode 只支持"):
        replace(
            _snapshot(),
            quote_mode="unknown",  # type: ignore[arg-type]
            expected_delay_ms=60_000,
        )


@pytest.mark.parametrize(
    ("now_ms", "expected", "reason"),
    [
        (100_499, QuoteFreshness.UNAVAILABLE, QuoteFreshnessReason.CLOCK_INVALID),
        (100_500, QuoteFreshness.FRESH, QuoteFreshnessReason.OK),
        (102_000, QuoteFreshness.FRESH, QuoteFreshnessReason.OK),
        (102_001, QuoteFreshness.STALE, QuoteFreshnessReason.AGE_EXCEEDED),
    ],
)
def test_quote_freshness_received_age_boundaries(
    now_ms: int,
    expected: QuoteFreshness,
    reason: QuoteFreshnessReason,
) -> None:
    view = evaluate_quote_freshness(
        _snapshot(quote_ts=100_500),
        identity=_identity(),
        request_sequence=1,
        now_utc_ms=now_ms,
        transport_budget_ms=1_500,
    )

    assert view.freshness is expected
    assert view.reason is reason


def test_quote_freshness_accepts_source_clock_exactly_five_seconds_ahead() -> None:
    snapshot = _snapshot(quote_ts=105_500, received_at=100_500)
    view = evaluate_quote_freshness(
        snapshot,
        identity=_identity(),
        request_sequence=1,
        now_utc_ms=100_500,
        transport_budget_ms=1_500,
    )

    assert view.freshness is QuoteFreshness.FRESH


def test_quote_snapshot_rejects_source_clock_more_than_five_seconds_ahead() -> None:
    with pytest.raises(ValueError, match="快于本机"):
        _snapshot(quote_ts=105_501, received_at=100_500)


def test_old_request_sequence_is_rejected_without_preserving_value() -> None:
    view = evaluate_quote_freshness(
        _snapshot(request_sequence=1),
        identity=_identity(),
        request_sequence=2,
        now_utc_ms=100_500,
        transport_budget_ms=1_500,
    )

    assert view.snapshot is None
    assert view.reason is QuoteFreshnessReason.REQUEST_SUPERSEDED


def test_session_paused_preserves_same_identity_snapshot() -> None:
    view = evaluate_quote_freshness(
        _snapshot(),
        identity=_identity(),
        request_sequence=1,
        now_utc_ms=500_000,
        transport_budget_ms=1_500,
        session_paused=True,
    )

    assert view.snapshot is not None
    assert view.freshness is QuoteFreshness.SESSION_PAUSED


def test_auth_failure_clears_quote_but_transport_failure_preserves_it() -> None:
    snapshot = _snapshot()
    auth = quote_failure_view(
        identity=_identity(),
        request_sequence=1,
        failure=QuoteFailureKind.AUTH_FAILED,
        previous_snapshot=snapshot,
    )
    transport = quote_failure_view(
        identity=_identity(),
        request_sequence=1,
        failure=QuoteFailureKind.TRANSPORT_FAILED,
        previous_snapshot=snapshot,
    )

    assert auth.snapshot is None
    assert auth.freshness is QuoteFreshness.UNAVAILABLE
    assert transport.snapshot is snapshot
    assert transport.freshness is QuoteFreshness.STALE


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (
            QuoteFailureKind.PERMISSION_DENIED,
            QuoteFreshnessReason.PERMISSION_DENIED,
        ),
        (
            QuoteFailureKind.SYMBOL_UNSUPPORTED,
            QuoteFreshnessReason.SYMBOL_UNSUPPORTED,
        ),
        (
            QuoteFailureKind.INVALID_RESPONSE,
            QuoteFreshnessReason.INVALID_RESPONSE,
        ),
    ],
)
def test_hard_quote_failures_clear_old_value_with_exact_reason(
    failure: QuoteFailureKind,
    reason: QuoteFreshnessReason,
) -> None:
    view = quote_failure_view(
        identity=_identity(),
        request_sequence=1,
        failure=failure,
        previous_snapshot=_snapshot(),
    )

    assert view.snapshot is None
    assert view.reason is reason


def test_watchlist_quote_set_rejects_duplicate_rows_and_more_than_100() -> None:
    snapshot = _snapshot()
    token = WatchlistRequestToken(
        selection_generation=1,
        market="US",
        source="longbridge",
        symbols=("AAPL.US",),
        watchlist_change_sequence=1,
        watchlist_refresh_sequence=1,
    )
    with pytest.raises(ValueError, match="完整返回"):
        WatchlistQuoteSet(token, ())
    with pytest.raises(ValueError, match="完整返回"):
        WatchlistQuoteSet(token, (snapshot, snapshot))

    with pytest.raises(ValueError, match="1 到 100"):
        WatchlistRequestToken(
            selection_generation=1,
            market="US",
            source="longbridge",
            symbols=tuple(f"S{index}.US" for index in range(101)),
            watchlist_change_sequence=1,
            watchlist_refresh_sequence=1,
        )


def test_watchlist_gate_accepts_only_latest_complete_batch() -> None:
    gate = WatchlistGenerationGate()
    identity = _identity()
    first = gate.issue(identity, ("AAPL.US", "MSFT.US"))
    second = gate.issue(identity, ("AAPL.US", "MSFT.US"))

    assert gate.accepts(first, current_identity=identity) is False
    assert gate.accepts(second, current_identity=identity) is True
    newer_identity = replace(identity, selection_generation=2)
    assert gate.accepts(second, current_identity=newer_identity) is False


def test_watchlist_token_and_selection_identity_canonicalize_symbols() -> None:
    identity = _identity(symbol="aapl.us")
    token = WatchlistRequestToken(
        selection_generation=1,
        market="Crypto",
        source="okx",
        symbols=("btc-usdt", "xau-usdt-swap"),
        watchlist_change_sequence=1,
        watchlist_refresh_sequence=1,
    )

    assert identity.symbol == "AAPL.US"
    assert token.symbols == ("BTC-USDT", "XAU-USDT-SWAP")


def test_generation_gate_rejects_superseded_success_and_failure_equally() -> None:
    gate = SelectionGenerationGate()
    old_identity = gate.stage(
        market="US",
        source="longbridge",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    old_success = gate.issue(old_identity, RequestFamily.QUOTE)
    old_failure = gate.issue(old_identity, RequestFamily.KLINE)

    new_identity = gate.stage(
        market="Crypto",
        source="okx",
        symbol="XAU-USDT-SWAP",
        display_timeframe="10m",
    )

    assert gate.accepts(old_success) is False
    assert gate.accepts(old_failure) is False
    new_quote = gate.issue(new_identity, RequestFamily.QUOTE)
    assert gate.accepts(new_quote) is True


def test_generation_gate_only_accepts_latest_sequence_and_commits_atomically() -> None:
    gate = SelectionGenerationGate()
    identity = gate.stage(
        market="US",
        source="longbridge",
        symbol="AAPL.US",
        display_timeframe="1h",
    )
    first = gate.issue(identity, RequestFamily.QUOTE)
    second = gate.issue(identity, RequestFamily.QUOTE)

    assert gate.accepts(first) is False
    assert gate.accepts(second) is True
    gate.commit(identity)
    assert gate.committed is identity
    assert gate.staged is None
    assert gate.accepts(second) is True


def test_generation_gate_failed_stage_keeps_previous_committed_selection() -> None:
    gate = SelectionGenerationGate()
    committed = gate.stage(
        market="US",
        source="longbridge",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    gate.commit(committed)
    failed = gate.stage(
        market="Crypto",
        source="okx",
        symbol="XAU-USDT-SWAP",
        display_timeframe="10m",
    )
    gate.abort(failed)

    assert gate.committed is committed
    assert gate.staged is None


def test_generation_gate_abort_never_reuses_committed_request_sequence() -> None:
    gate = SelectionGenerationGate()
    committed = gate.stage(
        market="US",
        source="longbridge",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    gate.commit(committed)
    old = gate.issue(committed, RequestFamily.QUOTE)
    failed = gate.stage(
        market="Crypto",
        source="okx",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    gate.abort(failed)

    assert gate.accepts(old) is False
    current = gate.issue(committed, RequestFamily.QUOTE)
    assert current.request_sequence > old.request_sequence
    assert gate.accepts(current) is True


def test_kline_evidence_ready_requires_enough_closed_bars_and_timestamp() -> None:
    view = KlineEvidenceView(
        schema_version=1,
        selection_generation=1,
        request_sequence=2,
        symbol="AAPL.US",
        market="US",
        source="longbridge",
        timeframe="1h",
        bar_count=101,
        closed_bar_count=100,
        required_closed_bars=100,
        latest_closed_ts_utc_ms=100_000,
        received_at_utc_ms=100_500,
        analysis_as_of_utc_ms=100_500,
        now_utc_ms=100_500,
        max_age_ms=1_000,
        price_tick=None,
    )

    assert view.state is EvidenceState.READY
    assert replace(view, closed_bar_count=99).state is EvidenceState.INSUFFICIENT
    assert replace(view, now_utc_ms=101_001).state is EvidenceState.STALE
    with pytest.raises(ValueError, match="快于接收时间"):
        replace(view, latest_closed_ts_utc_ms=105_501)
    with pytest.raises(ValueError, match="快于当前时间"):
        replace(
            view,
            received_at_utc_ms=105_500,
            latest_closed_ts_utc_ms=105_501,
        )


def test_market_bundle_allows_missing_or_stale_higher_timeframes() -> None:
    stale_4h = _kline_evidence("4h", now_utc_ms=101_001)

    bundle = _market_bundle(
        one_hour=None,
        four_hour=stale_4h,
    )

    assert bundle.analysis_state is AnalysisCapabilityState.READY
    assert bundle.analysis_reason is AnalysisGateReason.OK
    assert bundle.ready_higher_timeframes == ()


def test_market_bundle_accepts_only_ready_higher_timeframe_context() -> None:
    bundle = _market_bundle(
        one_hour=_kline_evidence("1h"),
        four_hour=_kline_evidence("4h", failure_reason="permission_denied"),
    )

    assert bundle.analysis_state is AnalysisCapabilityState.READY
    assert bundle.ready_higher_timeframes == ("1h",)


def test_market_bundle_without_authoritative_tick_is_display_only() -> None:
    bundle = _market_bundle(
        quote_snapshot=replace(_snapshot(), price_tick=None),
        ten_minute=_kline_evidence("10m", price_tick=None),
    )

    assert bundle.analysis_state is AnalysisCapabilityState.DISPLAY_ONLY
    assert bundle.analysis_reason is AnalysisGateReason.PRICE_TICK_UNAVAILABLE
    assert bundle.analysis_allowed is False


def test_market_bundle_blocks_stale_quote_but_not_optional_context() -> None:
    stale_quote = evaluate_quote_freshness(
        _snapshot(),
        identity=_identity(),
        request_sequence=1,
        now_utc_ms=102_001,
        transport_budget_ms=1_500,
    )

    bundle = MarketDataBundle(
        schema_version=1,
        token=RequestToken(_identity(), RequestFamily.KLINE, 2),
        analysis_as_of_utc_ms=100_500,
        quote=stale_quote,
        ten_minute=_kline_evidence("10m"),
        one_hour=None,
        four_hour=None,
    )

    assert bundle.analysis_state is AnalysisCapabilityState.BLOCKED
    assert bundle.analysis_reason is AnalysisGateReason.QUOTE_NOT_READY


def test_market_bundle_blocks_insufficient_ten_minute_evidence() -> None:
    bundle = _market_bundle(
        ten_minute=_kline_evidence("10m", closed_bar_count=99),
    )

    assert bundle.analysis_state is AnalysisCapabilityState.BLOCKED
    assert bundle.analysis_reason is AnalysisGateReason.TEN_MINUTE_NOT_READY


def test_market_bundle_rejects_mixed_analysis_as_of() -> None:
    one_hour = _kline_evidence(
        "1h",
        analysis_as_of_utc_ms=100_499,
    )

    with pytest.raises(ValueError, match="analysis_as_of"):
        _market_bundle(one_hour=one_hour)


def test_market_bundle_rejects_wrong_kline_identity_or_sequence() -> None:
    wrong_sequence = replace(_kline_evidence("10m"), request_sequence=3)

    with pytest.raises(ValueError, match="request_sequence"):
        _market_bundle(ten_minute=wrong_sequence)


def test_kline_evidence_rejects_bar_after_analysis_cutoff() -> None:
    with pytest.raises(ValueError, match="分析截止时间"):
        replace(
            _kline_evidence("10m"),
            latest_closed_ts_utc_ms=105_501,
            received_at_utc_ms=105_501,
            now_utc_ms=105_501,
        )


def test_analysis_result_view_is_read_only_projection_bound_to_request() -> None:
    identity = _identity()
    gate = SelectionGenerationGate()
    identity = gate.stage(
        market="US",
        source="longbridge",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    gate.commit(identity)
    token = gate.issue(identity, RequestFamily.ANALYSIS)
    record = SimpleNamespace(
        meta=SimpleNamespace(
            symbol="AAPL.US",
            timeframe="10m",
            data_source="longbridge",
        ),
        stage1_diagnosis={
            "cycle_position": "broad_channel",
            "direction": "bullish",
        },
        stage2_decision={
            "decision": {
                "order_type": "限价单",
                "order_direction": "做多",
                "diagnosis_confidence": 78,
                "trade_confidence": 63,
                "entry_price": "100.00",
                "stop_loss_price": "98",
                "take_profit_price": "104",
                "take_profit_price_2": "106",
                "reasoning": "测试理由",
            },
            "diagnosis_summary": {
                "cycle_position": "broad_channel",
                "direction": "bullish",
            },
            "terminal": {"outcome": "trade"},
        },
        exception=None,
    )

    view = AnalysisResultView.from_record(record, token=token, gate=gate)

    assert view.state is AnalysisResultState.SUCCEEDED
    assert view.selection_generation == 1
    assert view.request_sequence == token.request_sequence
    assert view.analysis_timeframe == "10m"
    assert view.entry_price == "100"
    assert view.stop_loss == "98"
    assert view.take_profit == "104"
    assert view.take_profit_2 == "106"
    assert view.terminal_outcome == "trade"


def test_analysis_result_view_rejects_wrong_symbol_or_display_request() -> None:
    token = RequestToken(_identity(), RequestFamily.QUOTE, 1)
    gate = SelectionGenerationGate()
    record = SimpleNamespace(
        meta=SimpleNamespace(symbol="MSFT.US", timeframe="10m"),
        stage1_diagnosis={},
        stage2_decision={},
        exception={"category": "provider", "stage": "stage1"},
    )

    with pytest.raises(ValueError, match="analysis 请求"):
        AnalysisResultView.from_record(record, token=token, gate=gate)


def test_analysis_result_view_rejects_superseded_token_and_wrong_source() -> None:
    gate = SelectionGenerationGate()
    identity = gate.stage(
        market="US",
        source="longbridge",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    gate.commit(identity)
    old = gate.issue(identity, RequestFamily.ANALYSIS)
    latest = gate.issue(identity, RequestFamily.ANALYSIS)
    record = SimpleNamespace(
        meta=SimpleNamespace(
            symbol="AAPL.US",
            timeframe="10m",
            data_source="longbridge",
        ),
        stage1_diagnosis={},
        stage2_decision={
            "decision": {"order_type": "不下单"},
            "terminal": {"outcome": "wait"},
        },
        exception=None,
    )

    with pytest.raises(ValueError, match="取代"):
        AnalysisResultView.from_record(record, token=old, gate=gate)
    record.meta.data_source = "okx"
    with pytest.raises(ValueError, match="数据源"):
        AnalysisResultView.from_record(record, token=latest, gate=gate)
