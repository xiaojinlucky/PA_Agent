from __future__ import annotations

import pytest

from pa_agent.data.base import DataSourceTransientError, KlineBar
from pa_agent.data.market_workspace import (
    WatchlistRequestToken,
)
from pa_agent.data.okx_source import (
    OKX_MAX_ANALYSIS_BARS,
    OkxSource,
    aggregate_okx_five_minute_rows,
    normalize_okx_instrument,
    okx_instrument_type,
)
from pa_agent.data.okx_public_client import OkxPublicTransportError


def test_okx_closed_bar_end_uses_declared_fixed_interval() -> None:
    bar = KlineBar(
        seq=1,
        ts_open=1_700_000_000_000,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
        closed=True,
    )

    assert OkxSource.closed_bar_end_utc_ms(bar, "10m") == (
        1_700_000_600_000
    )


class _FakeOkxClient:
    def __init__(self) -> None:
        self.instrument_rows = [
            {
                "instId": "XAU-USDT-SWAP",
                "state": "live",
                "tickSz": "0.1",
            },
            {"instId": "XAUT-USDT", "state": "live", "tickSz": "0.01"},
        ]
        self.candle_rows = [
            ["3000", "100", "102", "99", "101", "10", "0", "1000", "0"],
            ["2000", "98", "101", "97", "100", "9", "0", "900", "1"],
            ["1000", "97", "99", "96", "98", "8", "0", "800", "1"],
        ]
        self.public_calls: list[tuple[str, str | None]] = []
        self.candle_calls: list[tuple[str, str, int, str | None]] = []
        self.ticker_calls: list[str] = []
        self.ticker_rows: list[dict[str, str]] = []

    def public_instruments(
        self,
        inst_type: str,
        *,
        instrument: str | None = None,
    ) -> list[dict[str, str]]:
        self.public_calls.append((inst_type, instrument))
        return [
            row for row in self.instrument_rows
            if instrument is None or row["instId"] == instrument
        ]

    def candles(
        self,
        *,
        instrument: str,
        bar: str,
        limit: int,
        after: str | None = None,
    ) -> list[list[str]]:
        self.candle_calls.append((instrument, bar, limit, after))
        rows = self.candle_rows
        if after is not None:
            rows = [row for row in rows if int(row[0]) < int(after)]
        return rows[:limit]

    def tickers(self, inst_type: str) -> list[dict[str, str]]:
        self.ticker_calls.append(inst_type)
        return list(self.ticker_rows)


def test_okx_symbol_supports_spot_and_swap_without_product_whitelist():
    assert normalize_okx_instrument("btc-usdt") == "BTC-USDT"
    assert normalize_okx_instrument("eth-usdt-swap") == "ETH-USDT-SWAP"
    assert okx_instrument_type("BTC-USDT") == "SPOT"
    assert okx_instrument_type("ETH-USDT-SWAP") == "SWAP"


@pytest.mark.parametrize("symbol", ["", "XAUUSD", "BTC-USDT-250101", "A-B-C-D"])
def test_okx_symbol_rejects_unsupported_format(symbol):
    with pytest.raises(ValueError):
        normalize_okx_instrument(symbol)


def test_okx_source_validates_instrument_then_returns_newest_first_bars():
    client = _FakeOkxClient()
    source = OkxSource(client=client)
    source.connect()
    source.subscribe("xau-usdt-swap", "30m")

    bars = source.latest_snapshot(3)

    assert client.public_calls == [("SWAP", "XAU-USDT-SWAP")]
    assert source.price_tick() == "0.1"
    assert client.candle_calls == [("XAU-USDT-SWAP", "30m", 3, None)]
    assert [bar.seq for bar in bars] == [0, 1, 2]
    assert [bar.closed for bar in bars] == [False, True, True]
    assert [bar.ts_open for bar in bars] == [3000.0, 2000.0, 1000.0]
    assert all(bar.price_tick == "0.1" for bar in bars)


def test_okx_source_reads_higher_timeframe_without_changing_main_subscription():
    client = _FakeOkxClient()
    source = OkxSource(client=client)
    source.connect()
    source.subscribe("XAU-USDT-SWAP", "15m")

    bars = source.latest_snapshot_for_timeframe("1h", 3)
    source.latest_snapshot(2)

    assert len(bars) == 3
    assert source._timeframe == "15m"
    assert client.candle_calls == [
        ("XAU-USDT-SWAP", "1H", 3, None),
        ("XAU-USDT-SWAP", "15m", 2, None),
    ]


def test_okx_source_failed_switch_preserves_previous_subscription():
    client = _FakeOkxClient()
    source = OkxSource(client=client)
    source.connect()
    source.subscribe("XAU-USDT-SWAP", "30m")

    with pytest.raises(ValueError, match="未找到"):
        source.subscribe("BTC-USDT", "30m")

    assert source._symbol == "XAU-USDT-SWAP"
    assert source._timeframe == "30m"
    assert source.price_tick() == "0.1"


def test_okx_source_translates_transport_errors_without_exposing_credentials():
    class _FailingClient(_FakeOkxClient):
        def public_instruments(self, inst_type, *, instrument=None):
            raise OkxPublicTransportError("temporary")

    source = OkxSource(client=_FailingClient())
    source.connect()
    with pytest.raises(DataSourceTransientError, match="无法验证品种"):
        source.subscribe("XAUT-USDT", "15m")


def test_okx_analysis_limit_reserves_indicator_warmup():
    assert OKX_MAX_ANALYSIS_BARS == 245


def test_okx_batch_quotes_group_by_instrument_type_without_per_symbol_calls():
    client = _FakeOkxClient()
    client.instrument_rows.extend(
        [
            {"instId": "BTC-USDT", "state": "live", "tickSz": "0.01"},
            {"instId": "ETH-USDT", "state": "live", "tickSz": "0.01"},
        ]
    )
    client.ticker_rows = [
        {"instId": "BTC-USDT", "last": "100", "ts": "1700000000000"},
        {"instId": "ETH-USDT", "last": "200", "ts": "1700000000001"},
    ]
    token = WatchlistRequestToken(
        selection_generation=1,
        market="Crypto",
        source="okx",
        symbols=("BTC-USDT", "ETH-USDT"),
        watchlist_change_sequence=1,
        watchlist_refresh_sequence=7,
    )
    source = OkxSource(client=client)
    source.connect()

    snapshots = source.batch_quote_snapshots(
        token,
        received_at_utc_ms=1_700_000_000_100,
    )

    assert client.ticker_calls == ["SPOT"]
    assert client.public_calls == [("SPOT", None)]
    assert [snapshot.symbol for snapshot in snapshots] == [
        "BTC-USDT",
        "ETH-USDT",
    ]
    assert [snapshot.request_sequence for snapshot in snapshots] == [7, 7]
    assert all(snapshot.prev_close is None for snapshot in snapshots)
    assert all(snapshot.change is None for snapshot in snapshots)
    assert all(snapshot.currency == "USDT" for snapshot in snapshots)


def test_okx_batch_quotes_accepts_lowercase_symbols_without_dropping_rows():
    client = _FakeOkxClient()
    client.instrument_rows.append(
        {"instId": "BTC-USDT", "state": "live", "tickSz": "0.01"}
    )
    client.ticker_rows = [
        {"instId": "BTC-USDT", "last": "100", "ts": "1700000000000"}
    ]
    token = WatchlistRequestToken(
        selection_generation=1,
        market="Crypto",
        source="okx",
        symbols=("btc-usdt",),
        watchlist_change_sequence=1,
        watchlist_refresh_sequence=1,
    )
    source = OkxSource(client=client)
    source.connect()

    snapshots = source.batch_quote_snapshots(
        token,
        received_at_utc_ms=1_700_000_000_100,
    )

    assert token.symbols == ("BTC-USDT",)
    assert [snapshot.symbol for snapshot in snapshots] == ["BTC-USDT"]


def test_okx_batch_quotes_rejects_wrong_market_route():
    token = WatchlistRequestToken(
        selection_generation=1,
        market="US",
        source="longbridge",
        symbols=("AAPL.US",),
        watchlist_change_sequence=1,
        watchlist_refresh_sequence=1,
    )
    source = OkxSource(client=_FakeOkxClient())
    source.connect()

    with pytest.raises(ValueError, match="Crypto/okx"):
        source.batch_quote_snapshots(
            token,
            received_at_utc_ms=1_700_000_000_100,
        )


def test_okx_batch_quotes_preserves_input_order_across_spot_and_swap():
    client = _FakeOkxClient()
    client.instrument_rows.extend(
        [{"instId": "BTC-USDT", "state": "live", "tickSz": "0.01"}]
    )
    client.ticker_rows = [
        {
            "instId": "XAU-USDT-SWAP",
            "last": "4000",
            "ts": "1700000000000",
        },
        {"instId": "BTC-USDT", "last": "100", "ts": "1700000000001"},
    ]
    token = WatchlistRequestToken(
        selection_generation=2,
        market="Crypto",
        source="okx",
        symbols=("XAU-USDT-SWAP", "BTC-USDT"),
        watchlist_change_sequence=3,
        watchlist_refresh_sequence=4,
    )
    source = OkxSource(client=client)
    source.connect()

    snapshots = source.batch_quote_snapshots(
        token,
        received_at_utc_ms=1_700_000_000_100,
    )

    assert client.ticker_calls == ["SWAP", "SPOT"]
    assert [snapshot.symbol for snapshot in snapshots] == list(token.symbols)


def test_okx_batch_quotes_fails_whole_batch_when_one_symbol_is_missing():
    client = _FakeOkxClient()
    client.instrument_rows.append(
        {"instId": "BTC-USDT", "state": "live", "tickSz": "0.01"}
    )
    client.ticker_rows = []
    token = WatchlistRequestToken(
        selection_generation=1,
        market="Crypto",
        source="okx",
        symbols=("BTC-USDT",),
        watchlist_change_sequence=1,
        watchlist_refresh_sequence=1,
    )
    source = OkxSource(client=client)
    source.connect()

    with pytest.raises(DataSourceTransientError, match="缺少 BTC-USDT"):
        source.batch_quote_snapshots(
            token,
            received_at_utc_ms=1_700_000_000_100,
        )


def _five_minute_row(
    timestamp: int,
    open_price: str,
    high: str,
    low: str,
    close: str,
    volume: str,
    amount: str,
    confirm: str = "1",
) -> list[str]:
    return [
        str(timestamp), open_price, high, low, close,
        volume, "0", amount, confirm,
    ]


def test_okx_ten_minute_uses_two_closed_five_minute_bars() -> None:
    rows = [
        _five_minute_row(1_200_000, "14", "15", "13", "14", "1", "10", "0"),
        _five_minute_row(900_000, "12", "15", "11", "14", "3", "30"),
        _five_minute_row(600_000, "10", "13", "9", "12", "2", "20"),
        _five_minute_row(300_000, "9", "11", "8", "10", "5", "50"),
        _five_minute_row(0, "8", "10", "7", "9", "4", "40"),
    ]

    aggregated = aggregate_okx_five_minute_rows(rows, limit=2)

    assert aggregated == [
        ["600000", "10", "15", "9", "14", "5", "0", "50", "1"],
        ["0", "8", "11", "7", "10", "9", "0", "90", "1"],
    ]


def test_okx_ten_minute_paginates_to_cover_analysis_warmup() -> None:
    client = _FakeOkxClient()
    client.candle_rows = [
        _five_minute_row(
            index * 300_000,
            "100",
            "101",
            "99",
            "100",
            "1",
            "100",
        )
        for index in range(601, -1, -1)
    ]
    source = OkxSource(client=client)
    source.connect()
    source.subscribe("XAU-USDT-SWAP", "10m")

    bars = source.latest_snapshot(300)

    assert len(bars) == 300
    assert len(client.candle_calls) == 3
    assert [call[2] for call in client.candle_calls] == [300, 300, 2]
    assert client.candle_calls[0][3] is None
    assert int(client.candle_calls[1][3] or "0") < int(
        client.candle_rows[0][0]
    )
    assert all(bar.closed for bar in bars)


def test_okx_pagination_rejects_conflicting_overlap_and_no_progress() -> None:
    class _ConflictClient(_FakeOkxClient):
        def candles(self, *, instrument, bar, limit, after=None):
            if after is None:
                return [
                    _five_minute_row(600_000, "1", "2", "1", "2", "1", "1"),
                    _five_minute_row(300_000, "1", "2", "1", "2", "1", "1"),
                ]
            return [
                _five_minute_row(300_000, "9", "9", "9", "9", "1", "1"),
                _five_minute_row(0, "1", "2", "1", "2", "1", "1"),
            ]

    source = OkxSource(client=_ConflictClient())
    source.connect()
    source.subscribe("XAU-USDT-SWAP", "10m")

    with pytest.raises(DataSourceTransientError, match="内容冲突"):
        source._fetch_candle_rows(timeframe="10m", required_rows=3)

    class _NoProgressClient(_ConflictClient):
        def candles(self, *, instrument, bar, limit, after=None):
            return [
                _five_minute_row(600_000, "1", "2", "1", "2", "1", "1"),
                _five_minute_row(300_000, "1", "2", "1", "2", "1", "1"),
            ]

    source = OkxSource(client=_NoProgressClient())
    source.connect()
    source.subscribe("XAU-USDT-SWAP", "10m")
    with pytest.raises(DataSourceTransientError, match="游标没有"):
        source._fetch_candle_rows(timeframe="10m", required_rows=3)


def test_okx_pagination_rejects_early_empty_incomplete_history() -> None:
    class _ShortClient(_FakeOkxClient):
        def candles(self, *, instrument, bar, limit, after=None):
            if after is not None:
                return []
            return [
                _five_minute_row(300_000, "1", "2", "1", "2", "1", "1")
            ]

    source = OkxSource(client=_ShortClient())
    source.connect()
    source.subscribe("XAU-USDT-SWAP", "10m")

    with pytest.raises(DataSourceTransientError, match="分页不足"):
        source._fetch_candle_rows(timeframe="10m", required_rows=3)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                _five_minute_row(900_000, "1", "2", "1", "2", "1", "1"),
                _five_minute_row(300_000, "1", "2", "1", "2", "1", "1"),
            ],
            "缺根",
        ),
        (
            [
                _five_minute_row(600_001, "1", "2", "1", "2", "1", "1"),
                _five_minute_row(300_000, "1", "2", "1", "2", "1", "1"),
            ],
            "UTC 5 分钟边界",
        ),
        (
            [
                _five_minute_row(600_000, "1", "2", "1", "2", "1", "1"),
                _five_minute_row(900_000, "1", "2", "1", "2", "1", "1"),
            ],
            "严格从新到旧",
        ),
        (
            [
                _five_minute_row(900_000, "1", "2", "1", "2", "1", "1", "0"),
                _five_minute_row(600_000, "1", "2", "1", "2", "1", "1"),
            ],
            "没有两根连续",
        ),
    ],
)
def test_okx_ten_minute_rejects_invalid_five_minute_inputs(
    rows: list[list[str]],
    message: str,
) -> None:
    with pytest.raises(DataSourceTransientError, match=message):
        aggregate_okx_five_minute_rows(rows, limit=1)
