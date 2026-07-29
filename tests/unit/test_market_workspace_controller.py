from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pa_agent.config.settings import Settings
from pa_agent.data.market_workspace import (
    KlineEvidenceView,
    MarketDataBundle,
    QuoteFailureKind,
    QuoteSnapshot,
    RequestFamily,
    WatchlistQuoteSet,
    evaluate_quote_freshness,
)
from pa_agent.data.market_workspace_controller import (
    AnalysisState,
    MarketWorkspaceController,
    SelectionState,
    SettingsSaveFailureKind,
    SettingsSaveState,
    SourceAuthState,
)


def _controller(
    settings: Settings | None = None,
    *,
    clock_utc_ms: Callable[[], int] | None = None,
) -> MarketWorkspaceController:
    clock = clock_utc_ms or (lambda: 100_500)
    return MarketWorkspaceController(
        settings or Settings(),
        clock_utc_ms=clock,
    )


def _bundle(
    request: object,
    *,
    as_of_utc_ms: int | None = None,
    last: str = "100",
    price_tick: str | None = "0.01",
    required_closed_bars: int | None = None,
    quote_age_ms: int = 500,
    kline_age_ms: int = 500,
    session_paused: bool = False,
) -> MarketDataBundle:
    identity = request.identity
    quote_token = request.quote_token
    kline_token = request.kline_token
    request_as_of = request.analysis_as_of_utc_ms
    if request_as_of is None:
        raise ValueError("测试请求必须先由控制器冻结 analysis_as_of")
    effective_as_of = (
        request_as_of if as_of_utc_ms is None else as_of_utc_ms
    )
    required = (
        request.required_closed_bars
        if required_closed_bars is None
        else required_closed_bars
    )
    snapshot = QuoteSnapshot.from_prices(
        selection_generation=identity.selection_generation,
        request_sequence=quote_token.request_sequence,
        symbol=identity.symbol,
        market=identity.market,
        source=identity.source,
        name=identity.symbol,
        currency="USD",
        last=last,
        prev_close="99",
        price_tick=price_tick,
        quote_ts_utc_ms=effective_as_of - quote_age_ms,
        received_at_utc_ms=effective_as_of,
    )
    quote = evaluate_quote_freshness(
        snapshot,
        identity=identity,
        request_sequence=quote_token.request_sequence,
        now_utc_ms=effective_as_of,
        transport_budget_ms=request.quote_transport_budget_ms,
        session_paused=session_paused,
    )

    def evidence(timeframe: str, max_age_ms: int) -> KlineEvidenceView:
        return KlineEvidenceView(
            schema_version=1,
            selection_generation=identity.selection_generation,
            request_sequence=kline_token.request_sequence,
            symbol=identity.symbol,
            market=identity.market,
            source=identity.source,
            timeframe=timeframe,
            bar_count=max(100, required),
            closed_bar_count=max(100, required),
            required_closed_bars=required,
            latest_closed_ts_utc_ms=effective_as_of - kline_age_ms,
            received_at_utc_ms=effective_as_of,
            analysis_as_of_utc_ms=effective_as_of,
            now_utc_ms=effective_as_of,
            max_age_ms=max_age_ms,
            price_tick=price_tick,
            session_paused=session_paused,
        )

    return MarketDataBundle(
        schema_version=1,
        token=kline_token,
        analysis_as_of_utc_ms=effective_as_of,
        quote=quote,
        ten_minute=evidence("10m", request.ten_minute_max_age_ms),
        one_hour=evidence("1h", request.one_hour_max_age_ms),
        four_hour=evidence("4h", request.four_hour_max_age_ms),
    )


def _saved_settings(save_request: object, *, revision: int) -> Settings:
    saved = Settings()
    saved.market_workspace = save_request.workspace.model_copy(deep=True)
    saved.revision = revision
    return saved


def _record(request: object, *, failed: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        meta=SimpleNamespace(
            symbol=request.token.identity.symbol,
            timeframe="10m",
            data_source=request.token.identity.source,
        ),
        stage1_diagnosis={"cycle_position": "trend", "direction": "up"},
        stage2_decision={
            "diagnosis_summary": {
                "cycle_position": "trend",
                "direction": "up",
            },
            "decision": {
                "order_type": "wait",
                "order_direction": "none",
                "diagnosis_confidence": 80,
                "trade_confidence": 20,
                "reasoning": "只读分析",
            },
            "terminal": {"outcome": "wait"},
        },
        exception=(
            {"type": "ProviderTimeout", "stage": "stage1"} if failed else None
        ),
    )


def _watchlist_result(request: object) -> WatchlistQuoteSet:
    token = request.token
    snapshots = tuple(
        QuoteSnapshot.from_prices(
            selection_generation=token.selection_generation,
            request_sequence=token.watchlist_refresh_sequence,
            symbol=symbol,
            market=token.market,
            source=token.source,
            name=symbol,
            currency="USD",
            last=str(100 + index),
            prev_close="99",
            price_tick="0.01",
            quote_ts_utc_ms=100_000,
            received_at_utc_ms=100_500,
        )
        for index, symbol in enumerate(token.symbols)
    )
    return WatchlistQuoteSet(token=token, snapshots=snapshots)


def _commit(
    controller: MarketWorkspaceController,
    market: str,
    symbol: str,
    *,
    last: str = "100",
    price_tick: str | None = "0.01",
) -> tuple[object, object]:
    request = controller.begin_selection(
        market=market,
        symbol=symbol,
        display_timeframe="10m",
    )
    request = controller.freeze_analysis_as_of(request)
    result = controller.complete_market_data(
        request,
        _bundle(
            request,
            last=last,
            price_tick=price_tick,
        ),
    )
    assert result.accepted is True
    return request, result.save_request


def test_reverse_selection_callbacks_only_commit_latest_generation() -> None:
    controller = _controller()
    old = controller.begin_selection(
        market="US",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    old = controller.freeze_analysis_as_of(old)
    current = controller.begin_selection(
        market="HK",
        symbol="700.HK",
        display_timeframe="1h",
    )
    current = controller.freeze_analysis_as_of(current)

    accepted = controller.complete_market_data(current, _bundle(current))
    ignored = controller.complete_market_data(old, _bundle(old))

    assert accepted.accepted is True
    assert ignored.accepted is False
    assert controller.view.committed_identity == current.identity
    assert controller.view.bundle == _bundle(current)


def test_multiple_refreshes_reject_older_success_and_failure() -> None:
    controller = _controller()
    _commit(controller, "Crypto", "BTC-USDT")
    first = controller.refresh_current()
    first = controller.freeze_analysis_as_of(first)
    second = controller.refresh_current()
    second = controller.freeze_analysis_as_of(second)

    assert controller.complete_market_data(
        first,
        _bundle(first, last="101"),
    ).accepted is False
    assert (
        controller.fail_market_data(first, QuoteFailureKind.TRANSPORT_FAILED)
        is False
    )
    assert controller.complete_market_data(
        second,
        _bundle(second, last="102"),
    ).accepted is True
    assert controller.view.bundle is not None
    assert controller.view.bundle.analysis_as_of_utc_ms == 100_500


def test_request_as_of_is_frozen_by_controller_clock_and_not_external_bundle() -> None:
    now = [100_500]
    controller = MarketWorkspaceController(
        Settings(),
        clock_utc_ms=lambda: now[0],
    )
    request = controller.begin_selection(
        market="US",
        symbol="AAPL.US",
        display_timeframe="10m",
    )

    bound = controller.freeze_analysis_as_of(request)

    assert bound.analysis_as_of_utc_ms == 100_500
    with pytest.raises(ValueError, match="analysis_as_of"):
        controller.complete_market_data(
            bound,
            _bundle(bound, as_of_utc_ms=100_499),
        )


def test_controller_clock_rechecks_bundle_at_completion_and_analysis_time() -> None:
    now = [100_500]
    controller = MarketWorkspaceController(
        Settings(),
        clock_utc_ms=lambda: now[0],
        quote_transport_budget_ms=1_500,
        ten_minute_max_age_ms=1_000,
    )
    request = controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    bound = controller.freeze_analysis_as_of(request)
    bundle = _bundle(bound)
    now[0] = 100_600
    assert controller.complete_market_data(bound, bundle).accepted

    now[0] = 102_001
    with pytest.raises(ValueError, match="不可分析"):
        controller.begin_analysis()
    assert controller.view.bundle_current is False


def test_market_request_has_one_terminal_outcome() -> None:
    controller = _controller()
    request = controller.begin_selection(
        market="US",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    request = controller.freeze_analysis_as_of(request)
    bundle = _bundle(request)

    assert controller.complete_market_data(request, bundle).accepted
    assert controller.complete_market_data(request, bundle).accepted is False
    assert (
        controller.fail_market_data(request, QuoteFailureKind.AUTH_FAILED)
        is False
    )
    assert controller.view.longbridge_auth is SourceAuthState.VALID


def test_refresh_inflight_blocks_old_bundle_analysis() -> None:
    controller = _controller()
    _commit(controller, "Crypto", "BTC-USDT")

    controller.refresh_current()

    assert controller.view.bundle is not None
    assert controller.view.bundle_current is False
    with pytest.raises(ValueError, match="不可分析"):
        controller.begin_analysis()


def test_current_transport_failure_keeps_display_but_blocks_analysis_until_refresh() -> None:
    controller = _controller()
    _commit(controller, "Crypto", "BTC-USDT")
    failed_refresh = controller.refresh_current()

    assert controller.fail_market_data(
        failed_refresh,
        QuoteFailureKind.TRANSPORT_FAILED,
    )
    assert controller.view.bundle is not None
    assert controller.view.bundle_current is False
    with pytest.raises(ValueError, match="不可分析"):
        controller.begin_analysis()

    recovered = controller.refresh_current()
    recovered = controller.freeze_analysis_as_of(recovered)
    assert controller.complete_market_data(recovered, _bundle(recovered)).accepted
    assert controller.view.bundle_current is True
    assert controller.begin_analysis().bundle == controller.view.bundle


def test_rapid_four_market_switch_keeps_only_crypto() -> None:
    controller = _controller()
    requests = []
    for market, symbol in (
        ("US", "AAPL.US"),
        ("HK", "700.HK"),
        ("CN", "600519.SH"),
        ("Crypto", "XAU-USDT-SWAP"),
    ):
        request = controller.begin_selection(
            market=market,
            symbol=symbol,
            display_timeframe="10m",
        )
        requests.append(
            controller.freeze_analysis_as_of(request)
        )

    for request in reversed(requests):
        controller.complete_market_data(request, _bundle(request))

    assert controller.view.committed_identity == requests[-1].identity
    assert controller.view.bundle is not None
    assert controller.view.bundle.token.identity.market == "Crypto"


def test_wrong_bundle_source_or_analysis_as_of_is_rejected() -> None:
    controller = _controller()
    expected = controller.begin_selection(
        market="US",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    expected = controller.freeze_analysis_as_of(expected)
    other_controller = _controller()
    other = other_controller.begin_selection(
        market="HK",
        symbol="700.HK",
        display_timeframe="10m",
    )
    other = other_controller.freeze_analysis_as_of(other)
    other_bundle = _bundle(other)

    with pytest.raises(ValueError, match="请求"):
        controller.complete_market_data(expected, other_bundle)

    another = controller.begin_selection(
        market="US",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    another = controller.freeze_analysis_as_of(another)
    with pytest.raises(ValueError, match="analysis_as_of"):
        controller.complete_market_data(
            another,
            _bundle(another, as_of_utc_ms=100_499),
        )


def test_stale_malformed_success_is_discarded_before_projection() -> None:
    controller = _controller()
    old = controller.begin_selection(
        market="US",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    old = controller.freeze_analysis_as_of(old)
    current = controller.begin_selection(
        market="HK",
        symbol="700.HK",
        display_timeframe="10m",
    )
    current = controller.freeze_analysis_as_of(current)

    result = controller.complete_market_data(old, _bundle(current))

    assert result.accepted is False
    assert controller.view.staged_identity == current.identity


def test_settings_saves_are_serial_and_old_completion_never_marks_new_state_saved() -> None:
    controller = _controller()
    _, first_save = _commit(controller, "HK", "700.HK")
    assert first_save is not None
    _, queued_save = _commit(controller, "CN", "600519.SH")
    assert queued_save is None
    assert controller.view.settings_save_state is SettingsSaveState.SAVING

    second_save = controller.complete_settings_save(
        first_save,
        _saved_settings(first_save, revision=1),
    )
    assert second_save is not None
    assert second_save.workspace.selected_market == "CN"
    assert controller.view.settings_save_state is SettingsSaveState.SAVING

    assert (
        controller.complete_settings_save(
            first_save,
            _saved_settings(first_save, revision=2),
        )
        is None
    )
    assert controller.view.settings_save_state is SettingsSaveState.SAVING

    assert (
        controller.complete_settings_save(
            second_save,
            _saved_settings(second_save, revision=2),
        )
        is None
    )
    assert controller.view.settings_save_state is SettingsSaveState.SAVED
    assert controller.settings_snapshot.market_workspace.selected_market == "CN"


def test_settings_failure_does_not_roll_back_validated_page_and_can_retry() -> None:
    controller = _controller()
    _, save_request = _commit(controller, "CN", "600519.SH")
    assert save_request is not None

    assert controller.fail_settings_save(
        save_request,
        SettingsSaveFailureKind.WRITE_FAILED,
    )
    assert controller.view.selection_state is SelectionState.COMMITTED
    assert controller.view.committed_identity is not None
    assert controller.view.committed_identity.market == "CN"
    assert controller.view.settings_save_state is SettingsSaveState.FAILED

    retry = controller.retry_settings_save()
    assert retry.workspace.selected_market == "CN"
    controller.complete_settings_save(
        retry,
        _saved_settings(retry, revision=1),
    )
    assert controller.view.settings_save_state is SettingsSaveState.SAVED


def test_invalid_settings_save_result_is_terminal_and_retryable() -> None:
    controller = _controller()
    _, save_request = _commit(controller, "HK", "700.HK")
    assert save_request is not None
    invalid = _saved_settings(
        save_request,
        revision=save_request.baseline.revision,
    )

    with pytest.raises(ValueError, match="revision"):
        controller.complete_settings_save(save_request, invalid)

    assert controller.view.settings_save_state is SettingsSaveState.FAILED
    assert (
        controller.view.settings_save_failure
        is SettingsSaveFailureKind.WRITE_FAILED
    )
    retry = controller.retry_settings_save()
    assert retry.workspace == save_request.workspace
    assert (
        controller.complete_settings_save(
            save_request,
            _saved_settings(save_request, revision=1),
        )
        is None
    )


def test_incomplete_settings_save_result_is_terminal_and_retryable() -> None:
    controller = _controller()
    _, save_request = _commit(controller, "HK", "700.HK")
    assert save_request is not None

    with pytest.raises(ValueError, match="完整 Settings"):
        controller.complete_settings_save(
            save_request,
            SimpleNamespace(revision=save_request.baseline.revision + 1),
        )

    assert controller.view.settings_save_state is SettingsSaveState.FAILED
    assert (
        controller.view.settings_save_failure
        is SettingsSaveFailureKind.WRITE_FAILED
    )
    retry = controller.retry_settings_save()
    assert retry.workspace == save_request.workspace


def test_revision_conflict_cannot_be_rebased_by_ordinary_retry() -> None:
    controller = _controller()
    _, save_request = _commit(controller, "HK", "700.HK")
    assert save_request is not None
    assert controller.fail_settings_save(
        save_request,
        SettingsSaveFailureKind.REVISION_CONFLICT,
    )
    latest = Settings()
    latest.revision = 1
    latest.market_workspace = latest.market_workspace.model_copy(
        update={"selected_market": "CN"},
        deep=True,
    )

    with pytest.raises(ValueError, match="冲突"):
        controller.retry_settings_save(latest)
    assert controller.view.settings_save_state is SettingsSaveState.CONFLICT


def test_workspace_save_request_repr_never_contains_provider_secret() -> None:
    settings = Settings()
    settings.provider.api_key = "TEST_SECRET_MUST_NOT_APPEAR"
    controller = MarketWorkspaceController(settings)
    _, save_request = _commit(controller, "HK", "700.HK")

    assert save_request is not None
    assert "TEST_SECRET_MUST_NOT_APPEAR" not in repr(save_request)


def test_watchlist_controller_rejects_old_refresh_and_old_generation() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    save_request = controller.set_watchlist(("MSFT.US", "AAPL.US"))
    assert save_request is not None
    first = controller.begin_watchlist_refresh()
    second = controller.begin_watchlist_refresh()
    assert first is not None
    assert second is not None

    assert controller.complete_watchlist(first, _watchlist_result(first)) is False
    assert controller.complete_watchlist(second, _watchlist_result(second)) is True
    assert controller.view.watchlist == _watchlist_result(second)

    controller.begin_selection(
        market="HK",
        symbol="700.HK",
        display_timeframe="10m",
    )
    assert controller.complete_watchlist(second, _watchlist_result(second)) is False


def test_watchlist_request_has_one_terminal_outcome() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    request = controller.begin_watchlist_refresh()
    assert request is not None
    result = _watchlist_result(request)

    assert controller.complete_watchlist(request, result)
    assert controller.complete_watchlist(request, result) is False


def test_stale_malformed_watchlist_success_is_ignored_before_projection() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    older = controller.begin_watchlist_refresh()
    newer = controller.begin_watchlist_refresh()
    assert older is not None
    assert newer is not None

    assert (
        controller.complete_watchlist(
            older,
            _watchlist_result(newer),
        )
        is False
    )


def test_current_malformed_watchlist_success_is_terminal_and_clears_data() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    initial = controller.begin_watchlist_refresh()
    assert initial is not None
    assert controller.complete_watchlist(initial, _watchlist_result(initial))
    current = controller.begin_watchlist_refresh()
    assert current is not None
    malformed = SimpleNamespace(
        token=replace(
            current.token,
            watchlist_refresh_sequence=(
                current.token.watchlist_refresh_sequence + 100
            ),
        )
    )

    with pytest.raises(ValueError, match="token"):
        controller.complete_watchlist(
            current,
            _watchlist_result(malformed),
        )

    assert controller.view.watchlist is None
    assert (
        controller.view.last_market_failure
        is QuoteFailureKind.INVALID_RESPONSE
    )
    assert (
        controller.complete_watchlist(
            current,
            _watchlist_result(current),
        )
        is False
    )


def test_incomplete_current_watchlist_result_cannot_recover_authentication() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    older = controller.begin_watchlist_refresh()
    current = controller.begin_watchlist_refresh()
    assert older is not None
    assert current is not None
    assert controller.fail_watchlist(
        older,
        QuoteFailureKind.AUTH_FAILED,
    )

    with pytest.raises(ValueError, match="WatchlistQuoteSet"):
        controller.complete_watchlist(
            current,
            SimpleNamespace(token=current.token),
        )

    assert controller.view.longbridge_auth is SourceAuthState.INVALID
    assert controller.view.watchlist is None
    assert controller.view.bundle is None
    assert controller.view.bundle_current is False


def test_watchlist_auth_failure_updates_shared_source_and_success_recovers() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    failed = controller.begin_watchlist_refresh()
    assert failed is not None

    assert controller.fail_watchlist(
        failed,
        QuoteFailureKind.AUTH_FAILED,
    )
    assert controller.view.longbridge_auth is SourceAuthState.INVALID
    assert controller.view.bundle is None

    recovered = controller.begin_watchlist_refresh()
    assert recovered is not None
    assert controller.complete_watchlist(
        recovered,
        _watchlist_result(recovered),
    )
    assert controller.view.longbridge_auth is SourceAuthState.VALID


def test_newer_watchlist_success_recovers_after_older_auth_failure() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    older = controller.begin_watchlist_refresh()
    newer = controller.begin_watchlist_refresh()
    assert older is not None
    assert newer is not None

    assert controller.fail_watchlist(
        older,
        QuoteFailureKind.AUTH_FAILED,
    )
    assert controller.view.longbridge_auth is SourceAuthState.INVALID

    controller.complete_watchlist(newer, _watchlist_result(newer))

    assert controller.view.longbridge_auth is SourceAuthState.VALID


def test_clearing_watchlist_invalidates_inflight_refresh() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    old = controller.begin_watchlist_refresh()
    assert old is not None

    controller.set_watchlist(())

    assert controller.complete_watchlist(old, _watchlist_result(old)) is False
    assert controller.view.watchlist is None
    assert controller.begin_watchlist_refresh() is None


def test_longbridge_auth_failure_clears_only_longbridge_and_later_success_recovers() -> None:
    controller = _controller()
    _, crypto_save = _commit(controller, "Crypto", "BTC-USDT")
    assert crypto_save is not None
    longbridge = controller.begin_selection(
        market="US",
        symbol="AAPL.US",
        display_timeframe="10m",
    )

    assert controller.fail_market_data(
        longbridge,
        QuoteFailureKind.AUTH_FAILED,
    )
    assert controller.view.committed_identity is not None
    assert controller.view.committed_identity.market == "Crypto"
    assert controller.view.bundle is not None
    assert controller.view.longbridge_auth is SourceAuthState.INVALID
    assert controller.view.okx_auth is SourceAuthState.VALID

    recovered = controller.begin_selection(
        market="HK",
        symbol="700.HK",
        display_timeframe="10m",
    )
    recovered = controller.freeze_analysis_as_of(recovered)
    assert controller.complete_market_data(recovered, _bundle(recovered)).accepted
    assert controller.view.longbridge_auth is SourceAuthState.VALID
    assert controller.view.committed_identity == recovered.identity

    assert (
        controller.fail_market_data(longbridge, QuoteFailureKind.AUTH_FAILED)
        is False
    )
    assert controller.view.longbridge_auth is SourceAuthState.VALID


def test_auth_failure_on_current_longbridge_selection_removes_market_data() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    refresh = controller.refresh_current()

    assert controller.fail_market_data(refresh, QuoteFailureKind.AUTH_FAILED)
    assert controller.view.selection_state is SelectionState.AUTH_INVALID
    assert controller.view.bundle is None
    assert controller.view.longbridge_auth is SourceAuthState.INVALID


def test_newer_watchlist_success_recovers_after_older_market_auth_failure() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    market_request = controller.refresh_current()
    watchlist_request = controller.begin_watchlist_refresh()
    assert watchlist_request is not None

    controller.fail_market_data(
        market_request,
        QuoteFailureKind.AUTH_FAILED,
    )

    assert controller.complete_watchlist(
        watchlist_request,
        _watchlist_result(watchlist_request),
    )
    assert controller.view.longbridge_auth is SourceAuthState.VALID
    assert controller.view.watchlist is not None
    assert controller.view.bundle is None


def test_latest_longbridge_auth_failure_is_kept_during_crypto_switch() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    longbridge_refresh = controller.refresh_current()
    crypto = controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    crypto = controller.freeze_analysis_as_of(crypto)

    assert controller.fail_market_data(
        longbridge_refresh,
        QuoteFailureKind.AUTH_FAILED,
    )
    assert controller.view.selection_state is SelectionState.STAGING
    assert controller.view.staged_identity == crypto.identity
    assert controller.view.bundle is None
    assert controller.view.longbridge_auth is SourceAuthState.INVALID

    assert controller.complete_market_data(crypto, _bundle(crypto)).accepted
    assert controller.view.committed_identity == crypto.identity
    assert controller.view.bundle is not None
    assert controller.view.okx_auth is SourceAuthState.VALID


def test_failed_crypto_switch_cannot_restore_invalid_longbridge_page() -> None:
    controller = _controller()
    longbridge, _ = _commit(controller, "US", "AAPL.US")
    longbridge_refresh = controller.refresh_current()
    crypto = controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    controller.fail_market_data(
        longbridge_refresh,
        QuoteFailureKind.AUTH_FAILED,
    )

    assert controller.fail_market_data(
        crypto,
        QuoteFailureKind.TRANSPORT_FAILED,
    )
    assert controller.view.committed_identity == longbridge.identity
    assert controller.view.bundle is None
    assert controller.view.selection_state is SelectionState.AUTH_INVALID


def test_analysis_switch_freezes_input_and_late_result_goes_to_history_only() -> None:
    controller = _controller()
    _commit(controller, "Crypto", "BTC-USDT")
    analysis = controller.begin_analysis()
    assert analysis.bundle.analysis_as_of_utc_ms == 100_500
    assert controller.view.analysis_state is AnalysisState.RUNNING

    next_selection = controller.begin_selection(
        market="US",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    next_selection = controller.freeze_analysis_as_of(next_selection)
    controller.complete_market_data(
        next_selection,
        _bundle(next_selection, price_tick=None),
    )
    result = controller.complete_analysis(analysis, _record(analysis))

    assert result is not None
    assert controller.view.analysis_result is None
    assert controller.view.analysis_state is AnalysisState.IDLE
    assert controller.view.analysis_history == (result,)


def test_running_analysis_cannot_start_twice_and_projection_error_is_terminal() -> None:
    controller = _controller()
    _commit(controller, "Crypto", "BTC-USDT")
    analysis = controller.begin_analysis()

    with pytest.raises(ValueError, match="进行中"):
        controller.begin_analysis()
    with pytest.raises(ValueError, match="meta"):
        controller.complete_analysis(analysis, SimpleNamespace())

    assert controller.view.analysis_state is AnalysisState.FAILED
    assert controller.view.analysis_failure is not None


def test_worker_analysis_failure_has_stable_terminal_state() -> None:
    controller = _controller()
    _commit(controller, "Crypto", "BTC-USDT")
    analysis = controller.begin_analysis()

    assert controller.fail_analysis(analysis, "worker_failed")
    assert controller.view.analysis_state is AnalysisState.FAILED
    assert controller.view.analysis_failure == "worker_failed"
    assert controller.fail_analysis(analysis, "worker_failed") is False


def test_unknown_analysis_failure_is_terminal_before_error_is_raised() -> None:
    controller = _controller()
    _commit(controller, "Crypto", "BTC-USDT")
    analysis = controller.begin_analysis()

    with pytest.raises(ValueError, match="不支持"):
        controller.fail_analysis(analysis, "unexpected_failure")

    assert controller.view.analysis_state is AnalysisState.FAILED
    assert controller.view.analysis_failure == "invalid_result"
    assert controller.fail_analysis(analysis, "worker_failed") is False


def test_running_analysis_refresh_success_resets_new_as_of_to_idle() -> None:
    now = [100_500]
    controller = _controller(clock_utc_ms=lambda: now[0])
    _commit(controller, "Crypto", "BTC-USDT")
    analysis = controller.begin_analysis()
    now[0] = 101_500
    refresh = controller.refresh_current()
    refresh = controller.freeze_analysis_as_of(refresh)
    controller.complete_market_data(
        refresh,
        _bundle(refresh),
    )

    result = controller.complete_analysis(analysis, _record(analysis))

    assert controller.view.analysis_state is AnalysisState.IDLE
    assert controller.view.analysis_result is None
    assert controller.view.analysis_history == (result,)


def test_display_only_bundle_never_issues_analysis_request() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US", price_tick=None)

    with pytest.raises(ValueError, match="不可分析"):
        controller.begin_analysis()


def test_failed_analysis_uses_exception_type_when_category_is_absent() -> None:
    controller = _controller()
    _commit(controller, "Crypto", "BTC-USDT")
    analysis = controller.begin_analysis()

    result = controller.complete_analysis(
        analysis,
        _record(analysis, failed=True),
    )

    assert result is not None
    assert result.error_category == "ProviderTimeout"
    assert controller.view.analysis_state is AnalysisState.FAILED


def test_refresh_invalidates_completed_analysis_bound_to_old_as_of() -> None:
    controller = _controller()
    _commit(controller, "Crypto", "BTC-USDT")
    analysis = controller.begin_analysis()
    controller.complete_analysis(analysis, _record(analysis))
    assert controller.view.analysis_state is AnalysisState.SUCCEEDED

    controller.refresh_current()

    assert controller.view.analysis_result is None
    assert controller.view.analysis_state is AnalysisState.IDLE


def test_controller_source_has_no_qt_or_execution_dependency() -> None:
    source_path = (
        Path(__file__).parents[2]
        / "pa_agent"
        / "data"
        / "market_workspace_controller.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any(name == "PyQt6" or name.startswith("PyQt6.") for name in imported)
    assert not any(
        name == "pa_agent.execution" or name.startswith("pa_agent.execution.")
        for name in imported
    )


def test_controller_issues_only_read_side_request_families() -> None:
    controller = _controller()
    request = controller.begin_initial_load()

    assert request.connect_token.family is RequestFamily.CONNECT
    assert request.static_info_token.family is RequestFamily.STATIC_INFO
    assert request.quote_token.family is RequestFamily.QUOTE
    assert request.kline_token.family is RequestFamily.KLINE


@pytest.mark.parametrize(
    ("refresh_interval_ms", "expected_grace_ms"),
    [(1_000, 15_000), (6_000, 18_000)],
)
def test_default_quote_and_ten_minute_budgets_use_frozen_grace_contract(
    refresh_interval_ms: int,
    expected_grace_ms: int,
) -> None:
    settings = Settings()
    settings.general.refresh_interval_ms = refresh_interval_ms
    controller = _controller(settings)

    request = controller.begin_initial_load()

    assert request.quote_transport_budget_ms == expected_grace_ms
    assert (
        request.ten_minute_max_age_ms
        == 10 * 60 * 1_000 + expected_grace_ms
    )


def test_crypto_cannot_use_session_paused_to_accept_stale_evidence() -> None:
    now = 2_000_000
    controller = _controller(clock_utc_ms=lambda: now)
    request = controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    request = controller.freeze_analysis_as_of(request)

    with pytest.raises(ValueError):
        controller.complete_market_data(
            request,
            _bundle(
                request,
                quote_age_ms=30 * 60 * 1_000,
                kline_age_ms=30 * 60 * 1_000,
                session_paused=True,
            ),
        )


def test_default_crypto_ten_minute_budget_rejects_sixteen_minute_old_bar() -> None:
    now = 2_000_000
    controller = _controller(clock_utc_ms=lambda: now)
    request = controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    request = controller.freeze_analysis_as_of(request)

    with pytest.raises(ValueError, match="过期"):
        controller.complete_market_data(
            request,
            _bundle(
                request,
                kline_age_ms=16 * 60 * 1_000,
            ),
        )


def test_controller_binds_required_bars_to_settings_and_rejects_zero_bypass() -> None:
    zero_controller = _controller()
    zero_request = zero_controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    zero_request = zero_controller.freeze_analysis_as_of(zero_request)
    with pytest.raises(ValueError, match="至少"):
        replace(
            _bundle(zero_request).ten_minute,
            bar_count=0,
            closed_bar_count=0,
            required_closed_bars=0,
        )

    settings = Settings()
    settings.general.analysis_bar_count = 120
    controller = _controller(settings)
    request = controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    request = controller.freeze_analysis_as_of(request)
    with pytest.raises(ValueError, match="required_closed_bars"):
        controller.complete_market_data(
            request,
            _bundle(request, required_closed_bars=100),
        )


def test_failed_switch_identity_remains_in_controller_view() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    failed = controller.begin_selection(
        market="HK",
        symbol="700.HK",
        display_timeframe="1h",
    )

    assert controller.fail_market_data(
        failed,
        QuoteFailureKind.TRANSPORT_FAILED,
    )
    assert controller.view.committed_identity is not None
    assert controller.view.committed_identity.market == "US"
    assert controller.view.failed_identity == failed.identity


def test_selection_rejects_symbol_from_another_market() -> None:
    controller = _controller()

    with pytest.raises(ValueError, match="US"):
        controller.begin_selection(
            market="US",
            symbol="700.HK",
            display_timeframe="10m",
        )


def test_more_than_thirty_two_requests_do_not_hide_old_auth_failure() -> None:
    controller = _controller()
    _commit(controller, "US", "AAPL.US")
    requests = [controller.begin_watchlist_refresh() for _ in range(33)]
    assert all(request is not None for request in requests)
    oldest = requests[0]
    assert oldest is not None

    assert controller.fail_watchlist(
        oldest,
        QuoteFailureKind.AUTH_FAILED,
    )
    assert controller.view.longbridge_auth is SourceAuthState.INVALID
    assert controller.view.bundle is None
    assert controller.view.bundle_current is False


def test_more_than_thirty_two_late_analysis_results_remain_auditable() -> None:
    controller = _controller()
    _commit(controller, "Crypto", "BTC-USDT")
    requests = []
    for _ in range(33):
        requests.append(controller.begin_analysis())
        refresh = controller.refresh_current()
        refresh = controller.freeze_analysis_as_of(refresh)
        assert controller.complete_market_data(
            refresh,
            _bundle(refresh),
        ).accepted

    oldest = requests[0]
    result = controller.complete_analysis(oldest, _record(oldest))
    assert result in controller.view.analysis_history
