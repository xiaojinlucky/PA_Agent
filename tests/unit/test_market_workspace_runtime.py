from __future__ import annotations

from dataclasses import replace

import pytest

from pa_agent.config.settings import Settings
from pa_agent.data.base import DataSourceTransientError, KlineBar
from pa_agent.data.market_workspace import (
    QuoteFailureKind,
    QuoteSnapshot,
)
from pa_agent.data.market_workspace_controller import MarketWorkspaceController
from pa_agent.data.market_workspace_runtime import (
    MarketWorkspaceRuntime,
    MarketWorkspaceRuntimeError,
)

_AS_OF = 1_700_000_600_000


def _bar(
    *,
    seq: int,
    timestamp: int,
    price_tick: str | None,
) -> KlineBar:
    return KlineBar(
        seq=seq,
        ts_open=float(timestamp),
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=10.0,
        amount=1_000.0,
        closed=True,
        price_tick=price_tick,
    )


class _FakeSource:
    def __init__(
        self,
        *,
        price_tick: str | None,
        unavailable_timeframes: set[str] | None = None,
        now_utc_ms: int = _AS_OF,
        closed_age_ms: int = 0,
    ) -> None:
        self.price_tick = price_tick
        self.unavailable_timeframes = unavailable_timeframes or set()
        self.now_utc_ms = now_utc_ms
        self.closed_age_ms = closed_age_ms
        self.connected = False
        self.symbol = ""
        self.connect_calls = 0
        self.subscribe_calls: list[tuple[str, str]] = []
        self.kline_calls: list[tuple[str, int, int | None]] = []

    def connect(self) -> None:
        self.connected = True
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.connected = False

    def subscribe(self, symbol: str, timeframe: str) -> None:
        assert self.connected
        self.symbol = symbol
        self.subscribe_calls.append((symbol, timeframe))

    def batch_quote_snapshots(
        self,
        token,
        *,
        received_at_utc_ms: int | None = None,
    ) -> tuple[QuoteSnapshot, ...]:
        received = (
            self.now_utc_ms
            if received_at_utc_ms is None
            else received_at_utc_ms
        )
        return tuple(
            QuoteSnapshot.from_prices(
                selection_generation=token.selection_generation,
                request_sequence=token.watchlist_refresh_sequence,
                symbol=symbol,
                market=token.market,
                source=token.source,
                name=symbol,
                currency="USDT" if token.market == "Crypto" else "USD",
                last="101",
                prev_close="100",
                price_tick=self.price_tick,
                quote_ts_utc_ms=received - 100,
                received_at_utc_ms=received,
            )
            for symbol in token.symbols
        )

    def latest_snapshot_for_timeframe(
        self,
        timeframe: str,
        n: int,
        *,
        analysis_as_of_utc_ms: int | None = None,
    ) -> list[KlineBar]:
        self.kline_calls.append((timeframe, n, analysis_as_of_utc_ms))
        if timeframe in self.unavailable_timeframes:
            raise DataSourceTransientError(f"{timeframe} unavailable")
        interval = {
            "10m": 10 * 60_000,
            "1h": 60 * 60_000,
            "4h": 4 * 60 * 60_000,
        }[timeframe]
        return [
            _bar(
                seq=index + 1,
                timestamp=(
                    int(analysis_as_of_utc_ms or self.now_utc_ms)
                    - self.closed_age_ms
                    - interval * (index + 1)
                ),
                price_tick=self.price_tick,
            )
            for index in range(n)
        ]

    @staticmethod
    def closed_bar_end_utc_ms(
        bar: KlineBar,
        timeframe: str,
    ) -> int:
        interval = {
            "10m": 10 * 60_000,
            "1h": 60 * 60_000,
            "4h": 4 * 60 * 60_000,
        }[timeframe]
        return int(bar.ts_open) + interval


def _controller(now_utc_ms: int = _AS_OF) -> MarketWorkspaceController:
    settings = Settings()
    settings.general.analysis_bar_count = 2
    return MarketWorkspaceController(
        settings,
        clock_utc_ms=lambda: now_utc_ms,
    )


def test_runtime_builds_one_controller_bound_crypto_payload() -> None:
    source = _FakeSource(price_tick="0.1")
    controller = _controller()
    request = controller.begin_selection(
        market="Crypto",
        symbol="XAU-USDT-SWAP",
        display_timeframe="1h",
    )
    runtime = MarketWorkspaceRuntime(
        sources={"okx": source},
        clock_utc_ms=lambda: _AS_OF,
    )

    loaded = runtime.load_market_data(
        request,
        freeze_request=controller.freeze_analysis_as_of,
    )

    assert loaded.request.analysis_as_of_utc_ms == _AS_OF
    assert loaded.bundle.analysis_allowed
    assert loaded.render_payload.analysis_frame("10m") is not None
    assert loaded.render_payload.display_frame("1h") is not None
    assert loaded.render_payload.market_clock.phase == "continuous"
    assert source.connect_calls == 1
    assert source.subscribe_calls == [("XAU-USDT-SWAP", "1h")]
    assert all(call[2] == _AS_OF for call in source.kline_calls)

    applied = controller.complete_market_data(
        loaded.request,
        loaded.bundle,
        loaded.render_payload,
    )
    assert applied.accepted
    assert controller.view.render_payload == loaded.render_payload


def test_runtime_keeps_ten_minute_when_higher_timeframes_are_unavailable() -> None:
    source = _FakeSource(
        price_tick="0.1",
        unavailable_timeframes={"1h", "4h"},
    )
    controller = _controller()
    request = controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT-SWAP",
        display_timeframe="10m",
    )
    runtime = MarketWorkspaceRuntime(
        sources={"okx": source},
        clock_utc_ms=lambda: _AS_OF,
    )

    loaded = runtime.load_market_data(
        request,
        freeze_request=controller.freeze_analysis_as_of,
    )

    assert loaded.bundle.ten_minute.state == "ready"
    assert loaded.bundle.one_hour is not None
    assert loaded.bundle.one_hour.state == "unavailable"
    assert loaded.bundle.four_hour is not None
    assert loaded.bundle.four_hour.state == "unavailable"
    assert loaded.bundle.analysis_allowed
    assert loaded.render_payload.analysis_frame("1h") is None
    assert loaded.render_payload.analysis_frame("4h") is None


def test_runtime_fails_closed_when_ten_minute_is_unavailable() -> None:
    source = _FakeSource(
        price_tick="0.1",
        unavailable_timeframes={"10m"},
    )
    controller = _controller()
    request = controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    runtime = MarketWorkspaceRuntime(
        sources={"okx": source},
        clock_utc_ms=lambda: _AS_OF,
    )

    with pytest.raises(MarketWorkspaceRuntimeError) as exc_info:
        runtime.load_market_data(
            request,
            freeze_request=controller.freeze_analysis_as_of,
        )

    assert exc_info.value.failure is QuoteFailureKind.TRANSPORT_FAILED


def test_runtime_rejects_a_request_replaced_before_as_of_freeze() -> None:
    source = _FakeSource(price_tick="0.1")
    controller = _controller()
    older = controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    controller.begin_selection(
        market="Crypto",
        symbol="ETH-USDT",
        display_timeframe="10m",
    )
    runtime = MarketWorkspaceRuntime(
        sources={"okx": source},
        clock_utc_ms=lambda: _AS_OF,
    )

    with pytest.raises(ValueError, match="取代"):
        runtime.load_market_data(
            replace(older),
            freeze_request=controller.freeze_analysis_as_of,
        )


def test_runtime_uses_bar_close_time_during_ten_minute_cycle() -> None:
    now = _AS_OF + 5 * 60_000
    source = _FakeSource(
        price_tick="0.1",
        now_utc_ms=now,
        closed_age_ms=5 * 60_000,
    )
    controller = _controller(now)
    request = controller.begin_selection(
        market="Crypto",
        symbol="BTC-USDT",
        display_timeframe="10m",
    )
    runtime = MarketWorkspaceRuntime(
        sources={"okx": source},
        clock_utc_ms=lambda: now,
    )

    loaded = runtime.load_market_data(
        request,
        freeze_request=controller.freeze_analysis_as_of,
    )

    assert loaded.bundle.ten_minute.latest_closed_ts_utc_ms == (
        now - 5 * 60_000
    )
    assert loaded.bundle.ten_minute.state == "ready"
    assert loaded.bundle.analysis_allowed


def test_runtime_projects_calendar_failure_without_enabling_stock_ai() -> None:
    source = _FakeSource(price_tick=None)
    controller = _controller()
    request = controller.begin_selection(
        market="US",
        symbol="AAPL.US",
        display_timeframe="10m",
    )

    def unavailable_calendar(_market: str, _as_of_utc_ms: int):
        raise RuntimeError("calendar unavailable")

    runtime = MarketWorkspaceRuntime(
        sources={"longbridge": source},
        clock_utc_ms=lambda: _AS_OF,
        session_state_loader=unavailable_calendar,
    )

    loaded = runtime.load_market_data(
        request,
        freeze_request=controller.freeze_analysis_as_of,
    )

    assert loaded.render_payload.market_clock.phase == "unknown"
    assert loaded.bundle.analysis_state == "display_only"
    assert not loaded.bundle.analysis_allowed
