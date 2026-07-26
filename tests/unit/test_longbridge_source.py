"""Longbridge 只读行情数据源测试。"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import types
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pa_agent.data.base import DataSourceTransientError
from pa_agent.data.longbridge_source import (
    LONGBRIDGE_TOKEN_EXPIRING_THRESHOLD,
    LongbridgeSource,
    _timestamp_in_market_timezone,
    classify_longbridge_token_expiry,
    inspect_longbridge_token_expiry,
    load_longbridge_credentials,
    normalize_longbridge_symbol,
)

_CREDENTIAL_KEYS = (
    "LONGBRIDGE_APP_KEY",
    "LONGBRIDGE_APP_SECRET",
    "LONGBRIDGE_ACCESS_TOKEN",
    "LONGPORT_APP_KEY",
    "LONGPORT_APP_SECRET",
    "LONGPORT_ACCESS_TOKEN",
)


def _clear_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_env(
    path: Path,
    *,
    prefix: str = "LONGBRIDGE",
    access_token: str = "test-token",
) -> None:
    path.write_text(
        f"{prefix}_APP_KEY=test-key\n"
        f"{prefix}_APP_SECRET='test-secret'\n"
        f'{prefix}_ACCESS_TOKEN="{access_token}"\n',
        encoding="utf-8",
    )


def _jwt_with_payload(payload: dict[str, object]) -> str:
    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.test-signature"


def _jwt_with_exp(expires_at: datetime) -> str:
    return _jwt_with_payload({"exp": expires_at.timestamp()})


def _fake_sdk(rows: list[SimpleNamespace] | None = None) -> tuple[object, dict[str, object]]:
    calls: dict[str, object] = {}

    class Config:
        @staticmethod
        def from_apikey(
            app_key: str,
            app_secret: str,
            access_token: str,
            **kwargs: object,
        ) -> object:
            calls["credentials"] = (app_key, app_secret, access_token)
            calls["config_kwargs"] = kwargs
            return object()

    class QuoteContext:
        def __init__(self, config: object) -> None:
            calls["quote_context"] = config

        def static_info(self, symbols: list[str]) -> list[SimpleNamespace]:
            calls["static_info"] = symbols
            return [SimpleNamespace(symbol=symbols[0])]

        def trading_session(self) -> list[SimpleNamespace]:
            calls["trading_session"] = True
            sessions = {
                "US": [(time(9, 30), time(16, 0))],
                "HK": [(time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))],
                "CN": [(time(9, 30), time(11, 30)), (time(13, 0), time(14, 57))],
            }
            return [
                SimpleNamespace(
                    market=market,
                    trade_sessions=[
                        SimpleNamespace(
                            begin_time=begin_time,
                            end_time=end_time,
                            trade_session="intraday",
                        )
                        for begin_time, end_time in market_sessions
                    ],
                )
                for market, market_sessions in sessions.items()
            ]

        def trading_days(
            self, market: object, begin: date, end: date
        ) -> SimpleNamespace:
            calls["trading_days"] = (market, begin, end)
            days: list[date] = []
            current = begin
            while current <= end:
                if current.weekday() < 5:
                    days.append(current)
                current += timedelta(days=1)
            return SimpleNamespace(trading_days=days, half_trading_days=[])

        def candlesticks(self, *args: object) -> list[SimpleNamespace]:
            calls["candlesticks"] = args
            return list(rows or [])

    sdk = SimpleNamespace(
        Config=Config,
        QuoteContext=QuoteContext,
        Period=SimpleNamespace(
            Min_5="period-5m",
            Min_60="period-1h",
            Min_120="period-2h",
            Min_180="period-3h",
            Min_240="period-4h",
            Day="period-day",
            Week="period-week",
        ),
        AdjustType=SimpleNamespace(NoAdjust="no-adjust"),
        TradeSessions=SimpleNamespace(Intraday="intraday"),
        TradeSession=SimpleNamespace(Intraday="intraday"),
        Market=SimpleNamespace(US="US", HK="HK", CN="CN"),
    )
    return sdk, calls


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, sdk: object
) -> None:
    package = types.ModuleType("longbridge")
    package.openapi = sdk  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "longbridge", package)


def test_load_credentials_from_shared_env_without_mutating_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)

    credentials = load_longbridge_credentials(env_file)

    assert credentials.app_key == "test-key"
    assert credentials.app_secret == "test-secret"
    assert credentials.access_token == "test-token"
    assert "LONGBRIDGE_APP_KEY" not in os.environ


def test_incomplete_process_credentials_do_not_mix_with_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    monkeypatch.setenv("LONGBRIDGE_APP_KEY", "partial")
    env_file = tmp_path / "env"
    _write_env(env_file)

    with pytest.raises(DataSourceTransientError, match="凭据不完整"):
        load_longbridge_credentials(env_file)


def test_longport_legacy_names_are_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file, prefix="LONGPORT")

    credentials = load_longbridge_credentials(env_file)

    assert credentials.app_key == "test-key"


@pytest.mark.parametrize(
    ("expires_delta", "expected_status"),
    [
        (LONGBRIDGE_TOKEN_EXPIRING_THRESHOLD + timedelta(seconds=1), "valid"),
        (LONGBRIDGE_TOKEN_EXPIRING_THRESHOLD, "expiring"),
        (timedelta(seconds=1), "expiring"),
        (timedelta(0), "expired"),
        (timedelta(seconds=-1), "expired"),
    ],
)
def test_token_expiry_status_boundaries(expires_delta: timedelta, expected_status: str) -> None:
    now = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
    expires_at = now + expires_delta

    result = inspect_longbridge_token_expiry(
        _jwt_with_exp(expires_at),
        now=now,
    )

    assert result.status == expected_status
    assert result.expires_at_utc == expires_at
    assert result.expires_at_utc.tzinfo is UTC


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        "header.invalid-base64.signature",
        _jwt_with_payload({}),
        _jwt_with_payload({"exp": True}),
        _jwt_with_payload({"exp": "not-a-timestamp"}),
    ],
)
def test_token_expiry_unknown_for_unusable_exp(token: str) -> None:
    result = inspect_longbridge_token_expiry(token)

    assert result.status == "unknown"
    assert result.expires_at_utc is None


def test_parsed_token_expiry_can_be_reclassified_without_decoding_again() -> None:
    expires_at = datetime(2026, 7, 22, tzinfo=UTC)

    result = classify_longbridge_token_expiry(
        expires_at,
        now=datetime(2026, 7, 16, tzinfo=UTC),
    )

    assert result.status == "expiring"
    assert result.expires_at_utc == expires_at


def test_expired_token_fails_before_sdk_connection_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    token = _jwt_with_exp(datetime(2020, 1, 1, tzinfo=UTC))
    env_file = tmp_path / "env"
    _write_env(env_file, access_token=token)
    sdk, calls = _fake_sdk()
    _install_fake_sdk(monkeypatch, sdk)
    source = LongbridgeSource(env_file=env_file)

    with pytest.raises(DataSourceTransientError, match="Access Token 已过期") as exc_info:
        source.connect()

    assert source.token_expiry.status == "expired"
    assert source.token_expiry.expires_at_utc == datetime(2020, 1, 1, tzinfo=UTC)
    assert calls == {}
    assert token not in str(exc_info.value)


def test_expiring_token_warns_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _clear_credentials(monkeypatch)
    token = _jwt_with_exp(datetime.now(UTC) + timedelta(days=1))
    env_file = tmp_path / "env"
    _write_env(env_file, access_token=token)
    sdk, _ = _fake_sdk()
    _install_fake_sdk(monkeypatch, sdk)
    source = LongbridgeSource(env_file=env_file)

    with caplog.at_level(logging.WARNING):
        source.connect()

    assert source.token_expiry.status == "expiring"
    assert "Access Token 即将到期" in caplog.text
    assert token not in caplog.text


def test_future_exp_does_not_replace_read_only_server_authentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    token = _jwt_with_exp(datetime.now(UTC) + timedelta(days=30))
    env_file = tmp_path / "env"
    _write_env(env_file, access_token=token)
    sdk, _ = _fake_sdk()

    class RevokedQuoteContext(sdk.QuoteContext):  # type: ignore[misc, valid-type]
        def trading_session(self) -> list[SimpleNamespace]:
            raise RuntimeError("server rejected credential")

    sdk.QuoteContext = RevokedQuoteContext
    _install_fake_sdk(monkeypatch, sdk)
    source = LongbridgeSource(env_file=env_file)

    with pytest.raises(DataSourceTransientError, match="认证或网络异常") as exc_info:
        source.connect()

    assert source.token_expiry.status == "valid"
    assert token not in str(exc_info.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("aapl.us", "AAPL.US"), ("700.hk", "700.HK"), ("000001.sz", "000001.SZ")],
)
def test_symbol_normalization(raw: str, expected: str) -> None:
    assert normalize_longbridge_symbol(raw) == expected


@pytest.mark.parametrize("raw", ["XAUUSD", "AAPL", ".US", ""])
def test_symbol_requires_market_suffix(raw: str) -> None:
    with pytest.raises(ValueError, match=r"ticker\.region"):
        normalize_longbridge_symbol(raw)


def test_naive_sdk_timestamp_is_interpreted_as_host_wall_clock() -> None:
    timestamp = datetime(2026, 7, 16, 1, 0)
    market_timezone = ZoneInfo("America/New_York")

    converted = _timestamp_in_market_timezone(timestamp, market_timezone)

    assert converted == timestamp.astimezone(market_timezone)


def test_connect_uses_quote_context_only_and_disables_package_print(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    sdk, calls = _fake_sdk()
    _install_fake_sdk(monkeypatch, sdk)

    source = LongbridgeSource(env_file=env_file)
    source.connect()

    assert source._connected is True
    assert calls["credentials"] == ("test-key", "test-secret", "test-token")
    assert calls["config_kwargs"] == {"enable_print_quote_packages": False}
    assert "quote_context" in calls
    assert not hasattr(sdk, "TradeContext")
    assert source._regular_session_closes["CN"] == time(15, 0)


def test_subscribe_rejects_well_formed_but_unknown_symbol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    sdk, _ = _fake_sdk()

    class EmptyStaticInfo(sdk.QuoteContext):  # type: ignore[misc, valid-type]
        def static_info(self, symbols: list[str]) -> list[SimpleNamespace]:
            return []

    sdk.QuoteContext = EmptyStaticInfo
    _install_fake_sdk(monkeypatch, sdk)
    source = LongbridgeSource(env_file=env_file)
    source.connect()

    with pytest.raises(DataSourceTransientError, match="未找到品种"):
        source.subscribe("NOTAREALSYMBOL12345.US", "5m")

    assert source._symbol == ""


def test_failed_symbol_validation_preserves_previous_subscription(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    sdk, _ = _fake_sdk()

    class OneKnownSymbol(sdk.QuoteContext):  # type: ignore[misc, valid-type]
        def static_info(self, symbols: list[str]) -> list[SimpleNamespace]:
            return [SimpleNamespace(symbol=symbols[0])] if symbols == ["AAPL.US"] else []

    sdk.QuoteContext = OneKnownSymbol
    _install_fake_sdk(monkeypatch, sdk)
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("AAPL.US", "5m")

    with pytest.raises(DataSourceTransientError, match="未找到品种"):
        source.subscribe("NOTAREALSYMBOL12345.US", "15m")

    assert (source._symbol, source._timeframe) == ("AAPL.US", "5m")


def test_snapshot_sorts_and_converts_decimal_turnover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    older = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
    newest = datetime(2026, 7, 15, 1, 5, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            timestamp=older,
            open=Decimal("100.0"),
            high=Decimal("102.0"),
            low=Decimal("99.0"),
            close=Decimal("101.0"),
            volume=10,
            turnover=Decimal("1005.0"),
        ),
        SimpleNamespace(
            timestamp=newest,
            open=Decimal("101.0"),
            high=Decimal("103.0"),
            low=Decimal("100.0"),
            close=Decimal("102.0"),
            volume=20,
            turnover=Decimal("2040.0"),
        ),
    ]
    sdk, calls = _fake_sdk(rows)
    _install_fake_sdk(monkeypatch, sdk)
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda timezone: datetime.fromtimestamp(newest.timestamp() + 60, tz=timezone),
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("aapl.us", "5m")

    bars = source.latest_snapshot(2)

    assert [bar.ts_open for bar in bars] == [
        newest.timestamp() * 1000,
        older.timestamp() * 1000,
    ]
    assert bars[0].seq == 0
    assert bars[0].closed is False
    assert bars[0].amount == 2040.0
    assert bars[1].seq == 1
    assert bars[1].closed is True
    assert calls["candlesticks"] == (
        "AAPL.US",
        "period-5m",
        2,
        "no-adjust",
        "intraday",
    )


def test_snapshot_normalizes_naive_sdk_timestamp_before_epoch_conversion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    naive_local = datetime(2026, 7, 16, 1, 0)
    row = _market_bar(naive_local)
    sdk, _ = _fake_sdk([row])
    _install_fake_sdk(monkeypatch, sdk)
    expected_market_time = datetime(2026, 7, 15, 13, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._timestamp_in_market_timezone",
        lambda _timestamp, _timezone: expected_market_time,
    )
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda timezone: datetime(2026, 7, 15, 13, 1, tzinfo=timezone),
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("AAPL.US", "5m")

    bar = source.latest_snapshot(1)[0]

    assert bar.ts_open == expected_market_time.timestamp() * 1000


def test_snapshot_marks_stale_head_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    timestamp = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
    row = SimpleNamespace(
        timestamp=timestamp,
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("1"),
        close=Decimal("2"),
        volume=1,
        turnover=Decimal("2"),
    )
    sdk, _ = _fake_sdk([row])
    _install_fake_sdk(monkeypatch, sdk)
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda timezone: datetime.fromtimestamp(timestamp.timestamp() + 600, tz=timezone),
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("AAPL.US", "5m")

    bars = source.latest_snapshot(1)

    assert bars[0].seq == 1
    assert bars[0].closed is True


def _market_bar(timestamp: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        timestamp=timestamp,
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("1"),
        close=Decimal("2"),
        volume=1,
        turnover=Decimal("2"),
    )


@pytest.mark.parametrize(
    ("symbol", "bar_open_utc", "market_now", "expected_closed"),
    [
        ("600519.SH", datetime(2026, 7, 14, 16, tzinfo=UTC), (14, 59), False),
        ("600519.SH", datetime(2026, 7, 14, 16, tzinfo=UTC), (15, 1), True),
        ("700.HK", datetime(2026, 7, 14, 16, tzinfo=UTC), (15, 59), False),
        ("700.HK", datetime(2026, 7, 14, 16, tzinfo=UTC), (16, 1), True),
        ("AAPL.US", datetime(2026, 7, 15, 4, tzinfo=UTC), (15, 59), False),
        ("AAPL.US", datetime(2026, 7, 15, 4, tzinfo=UTC), (16, 1), True),
    ],
)
def test_daily_bar_uses_market_session_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symbol: str,
    bar_open_utc: datetime,
    market_now: tuple[int, int],
    expected_closed: bool,
) -> None:
    """覆盖中、美、港市场盘中与常规收盘后。"""
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    sdk, _ = _fake_sdk([_market_bar(bar_open_utc)])
    _install_fake_sdk(monkeypatch, sdk)
    hour, minute = market_now
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda timezone: datetime(2026, 7, 15, hour, minute, tzinfo=timezone),
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe(symbol, "1d")

    bar = source.latest_snapshot(1)[0]

    assert bar.closed is expected_closed
    assert bar.seq == (1 if expected_closed else 0)


@pytest.mark.parametrize("timeframe", ["1h", "2h", "3h", "4h"])
def test_intraday_head_bar_is_capped_at_regular_market_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    timeframe: str,
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    sdk, _ = _fake_sdk()
    _install_fake_sdk(monkeypatch, sdk)
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda timezone: datetime(2026, 7, 15, 16, 1, tzinfo=timezone),
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("AAPL.US", "5m")
    source._timeframe = timeframe

    is_forming = source._head_bar_is_forming(
        datetime(2026, 7, 15, 15, 0, tzinfo=ZoneInfo("America/New_York")),
        int(timeframe[:-1]) * 60 * 60,
        timeframe,
    )

    assert is_forming is False


@pytest.mark.parametrize(
    ("symbol", "timeframe", "bar_open", "market_now"),
    [
        ("700.HK", "1h", time(11, 30), time(12, 1)),
        ("700.HK", "2h", time(11, 30), time(12, 1)),
        ("600519.SH", "3h", time(9, 30), time(11, 31)),
        ("600519.SH", "3h", time(13, 0), time(15, 1)),
        ("600519.SH", "3h", time(15, 0), time(15, 0)),
    ],
)
def test_intraday_head_bar_is_capped_at_each_session_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symbol: str,
    timeframe: str,
    bar_open: time,
    market_now: time,
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    sdk, _ = _fake_sdk()
    _install_fake_sdk(monkeypatch, sdk)
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe(symbol, "5m")
    source._timeframe = timeframe
    timezone = source._market_timezone
    assert timezone is not None
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda _timezone: datetime.combine(date(2026, 7, 15), market_now, timezone),
    )

    is_forming = source._head_bar_is_forming(
        datetime.combine(date(2026, 7, 15), bar_open, timezone),
        int(timeframe[:-1]) * 60 * 60,
        timeframe,
    )

    assert is_forming is False


@pytest.mark.parametrize(
    ("market_now", "expected_closed"),
    [
        (datetime(2026, 7, 17, 15, 59), False),
        (datetime(2026, 7, 17, 16, 1), True),
        (datetime(2026, 7, 18, 10, 0), True),
    ],
)
def test_weekly_bar_uses_last_market_trading_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    market_now: datetime,
    expected_closed: bool,
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    monday_open_utc = datetime(2026, 7, 12, 16, tzinfo=UTC)
    sdk, calls = _fake_sdk([_market_bar(monday_open_utc)])
    _install_fake_sdk(monkeypatch, sdk)
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda timezone: market_now.replace(tzinfo=timezone),
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("700.HK", "1w")

    bar = source.latest_snapshot(1)[0]

    assert bar.closed is expected_closed
    assert bar.seq == (1 if expected_closed else 0)
    assert calls["trading_days"] == (
        "HK",
        date(2026, 7, 13),
        date(2026, 7, 19),
    )


def test_empty_snapshot_and_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    sdk, _ = _fake_sdk([])
    _install_fake_sdk(monkeypatch, sdk)
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("AAPL.US", "5m")

    with pytest.raises(DataSourceTransientError, match="未返回 K 线"):
        source.latest_snapshot(1)

    source.disconnect()
    assert source._connected is False
    assert source._quote_context is None


def _closed_us_5m_rows(*minute_offsets: int) -> list[SimpleNamespace]:
    """2025-06-04（周三）美股盘中 13:30 UTC 起的 5m K 线，按分钟偏移生成。"""
    base = datetime(2025, 6, 4, 13, 30, tzinfo=UTC)
    return [
        SimpleNamespace(
            timestamp=base + timedelta(minutes=offset),
            open=Decimal("100.0"),
            high=Decimal("101.0"),
            low=Decimal("99.0"),
            close=Decimal("100.5"),
            volume=10,
            turnover=Decimal("1005.0"),
        )
        for offset in minute_offsets
    ]


def test_paged_history_merges_older_rows_without_duplicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    recent = _closed_us_5m_rows(10, 15)
    older = _closed_us_5m_rows(0, 5)
    sdk, calls = _fake_sdk(recent)

    class PagedContext(sdk.QuoteContext):  # type: ignore[misc, valid-type]
        def history_candlesticks_by_offset(
            self, *args: object
        ) -> list[SimpleNamespace]:
            calls["history_by_offset"] = args
            return list(older) + [recent[0]]

    sdk.QuoteContext = PagedContext
    _install_fake_sdk(monkeypatch, sdk)
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._MAX_CANDLESTICKS", 2
    )
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda timezone: datetime(2025, 6, 4, 23, 0, tzinfo=timezone),
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("AAPL.US", "5m")

    bars = source.latest_snapshot(4)

    assert len(bars) == 4
    assert [bar.ts_open for bar in bars] == sorted(
        (row.timestamp.timestamp() * 1000 for row in recent + older),
        reverse=True,
    )
    history_args = calls["history_by_offset"]
    assert history_args[3] is False
    assert history_args[5] == recent[0].timestamp


def test_gap_inside_trading_session_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pa_agent.data.base import DataSourceError

    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    # 缺 13:35（美东 09:35，交易时段内）：属于 #890 类缺根，必须拒绝。
    sdk, _ = _fake_sdk(_closed_us_5m_rows(0, 10))
    _install_fake_sdk(monkeypatch, sdk)
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda timezone: datetime(2025, 6, 4, 23, 0, tzinfo=timezone),
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("AAPL.US", "5m")

    with pytest.raises(DataSourceError, match="交易时段内缺根"):
        source.latest_snapshot(2)


def test_cn_lunch_break_gap_is_legitimate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    # 2025-06-05（周四）上交所：11:25 是上午最后一根 5m，13:00 是下午第一根。
    morning_last = datetime(2025, 6, 5, 3, 25, tzinfo=UTC)
    afternoon_first = datetime(2025, 6, 5, 5, 0, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            timestamp=timestamp,
            open=Decimal("100.0"),
            high=Decimal("101.0"),
            low=Decimal("99.0"),
            close=Decimal("100.5"),
            volume=10,
            turnover=Decimal("1005.0"),
        )
        for timestamp in (morning_last, afternoon_first)
    ]
    sdk, _ = _fake_sdk(rows)
    _install_fake_sdk(monkeypatch, sdk)
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda timezone: datetime(2025, 6, 5, 23, 0, tzinfo=timezone),
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("600519.SH", "5m")

    bars = source.latest_snapshot(2)

    assert len(bars) == 2
    assert all(bar.closed for bar in bars)


def test_latest_snapshot_for_timeframe_uses_requested_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    hourly = datetime(2025, 6, 4, 14, 30, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            timestamp=hourly,
            open=Decimal("100.0"),
            high=Decimal("101.0"),
            low=Decimal("99.0"),
            close=Decimal("100.5"),
            volume=10,
            turnover=Decimal("1005.0"),
        )
    ]
    sdk, calls = _fake_sdk(rows)
    _install_fake_sdk(monkeypatch, sdk)
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._now_in_market_timezone",
        lambda timezone: datetime(2025, 6, 4, 23, 0, tzinfo=timezone),
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("AAPL.US", "5m")

    bars = source.latest_snapshot_for_timeframe("1h", 1)

    assert len(bars) == 1
    assert calls["candlesticks"][1] == "period-1h"
    # 订阅的主周期保持不变
    assert source._timeframe == "5m"


def test_rate_limiter_delays_over_limit_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pa_agent.data import longbridge_source as module

    limiter = module._QuoteRateLimiter(max_calls_per_second=2)
    clock = {"now": 100.0}
    sleeps: list[float] = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])

    def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(module.time, "sleep", _fake_sleep)

    limiter.acquire()
    limiter.acquire()
    limiter.acquire()

    assert sleeps
    assert abs(sum(sleeps) - 1.0) < 0.05


def test_sdk_call_timeout_guard_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import time as real_time

    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    sdk, _ = _fake_sdk([])
    _install_fake_sdk(monkeypatch, sdk)
    monkeypatch.setattr(
        "pa_agent.data.longbridge_source._SDK_CALL_TIMEOUT_SECONDS", 0.05
    )
    source = LongbridgeSource(env_file=env_file)
    source.connect()

    with pytest.raises(DataSourceTransientError, match="SDK 卡顿保护"):
        source._call_quote_sdk("测试", real_time.sleep, 0.5)


def test_snapshot_rejects_more_than_paged_hard_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_credentials(monkeypatch)
    env_file = tmp_path / "env"
    _write_env(env_file)
    sdk, _ = _fake_sdk([])
    _install_fake_sdk(monkeypatch, sdk)
    source = LongbridgeSource(env_file=env_file)
    source.connect()
    source.subscribe("AAPL.US", "5m")

    with pytest.raises(DataSourceTransientError, match="单次快照上限 5000"):
        source.latest_snapshot(5001)
