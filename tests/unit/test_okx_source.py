from __future__ import annotations

import pytest

from pa_agent.data.base import DataSourceTransientError
from pa_agent.data.okx_source import (
    OKX_MAX_ANALYSIS_BARS,
    OkxSource,
    aggregate_okx_five_minute_rows,
    normalize_okx_instrument,
    okx_instrument_type,
)
from pa_agent.execution.errors import BrokerTransportError


class _FakeOkxClient:
    def __init__(self) -> None:
        self.instrument_rows = [
            {"instId": "XAU-USDT-SWAP", "state": "live"},
            {"instId": "XAUT-USDT", "state": "live"},
        ]
        self.candle_rows = [
            ["3000", "100", "102", "99", "101", "10", "0", "1000", "0"],
            ["2000", "98", "101", "97", "100", "9", "0", "900", "1"],
            ["1000", "97", "99", "96", "98", "8", "0", "800", "1"],
        ]
        self.public_calls: list[tuple[str, str | None]] = []
        self.candle_calls: list[tuple[str, str, int]] = []

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
    ) -> list[list[str]]:
        self.candle_calls.append((instrument, bar, limit))
        return self.candle_rows[:limit]


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
    assert client.candle_calls == [("XAU-USDT-SWAP", "30m", 3)]
    assert [bar.seq for bar in bars] == [0, 1, 2]
    assert [bar.closed for bar in bars] == [False, True, True]
    assert [bar.ts_open for bar in bars] == [3000.0, 2000.0, 1000.0]


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
        ("XAU-USDT-SWAP", "1H", 3),
        ("XAU-USDT-SWAP", "15m", 2),
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


def test_okx_source_translates_transport_errors_without_exposing_credentials():
    class _FailingClient(_FakeOkxClient):
        def public_instruments(self, inst_type, *, instrument=None):
            raise BrokerTransportError(
                "temporary",
                write_may_have_reached=False,
            )

    source = OkxSource(client=_FailingClient())
    source.connect()
    with pytest.raises(DataSourceTransientError, match="无法验证品种"):
        source.subscribe("XAUT-USDT", "15m")


def test_okx_analysis_limit_reserves_indicator_warmup():
    assert OKX_MAX_ANALYSIS_BARS == 245


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
