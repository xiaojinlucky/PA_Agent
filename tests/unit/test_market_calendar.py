"""多市场交易日历会话状态测试（真实 exchange_calendars 数据）。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from pa_agent.data.market_calendar import (
    MarketCalendarError,
    SessionPhase,
    intraday_phase,
    is_trading_minute,
    session_close_utc_ms,
    session_state,
    supported_market,
)


def _utc_ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    ("market", "moment", "expected_phase"),
    (
        # 2025-06-05 周四：上交所 10:00 北京时间开市中
        ("SH", "2025-06-05T02:00:00", SessionPhase.OPEN),
        ("SZ", "2025-06-05T02:00:00", SessionPhase.OPEN),
        # 12:00 北京时间午休
        ("SH", "2025-06-05T04:00:00", SessionPhase.BREAK),
        # 16:00 北京时间已收盘
        ("CN", "2025-06-05T08:00:00", SessionPhase.CLOSED),
        # 2025-06-04 周三：纽约 11:00 ET 开市中
        ("US", "2025-06-04T15:00:00", SessionPhase.OPEN),
        # 周六闭市
        ("US", "2025-06-07T15:00:00", SessionPhase.CLOSED),
        # 港股 10:30 香港时间开市中；12:30 午休
        ("HK", "2025-06-05T02:30:00", SessionPhase.OPEN),
        ("HK", "2025-06-05T04:30:00", SessionPhase.BREAK),
    ),
)
def test_session_phases(market, moment, expected_phase):
    state = session_state(market, _utc_ms(moment))
    assert state.phase is expected_phase


def test_closed_state_reports_future_next_open():
    at = _utc_ms("2025-06-07T15:00:00")
    state = session_state("US", at)
    assert state.phase is SessionPhase.CLOSED
    assert state.next_change_utc_ms is not None
    assert state.next_change_utc_ms > at


def test_hk_christmas_eve_flagged_half_day():
    state = session_state("HK", _utc_ms("2024-12-24T02:30:00"))
    assert state.phase is SessionPhase.OPEN
    assert state.is_half_day is True


def test_hk_half_day_flag_survives_after_early_close():
    at = _utc_ms("2024-12-24T04:30:00")

    state = session_state("HK", at)
    phase = intraday_phase("HK", at)

    assert state.phase is SessionPhase.CLOSED
    assert state.is_half_day is True
    assert phase.label == "闭市"
    assert phase.is_half_day is True


@pytest.mark.parametrize(
    ("market", "session_date", "expected_close"),
    (
        ("HK", date(2025, 12, 24), "2025-12-24T04:00:00"),
        ("US", date(2025, 11, 28), "2025-11-28T18:00:00"),
        ("CN", date(2025, 6, 5), "2025-06-05T07:00:00"),
    ),
)
def test_session_close_uses_actual_regular_or_half_day_boundary(
    market: str,
    session_date: date,
    expected_close: str,
) -> None:
    assert session_close_utc_ms(market, session_date) == _utc_ms(expected_close)


def test_is_trading_minute_excludes_break():
    assert is_trading_minute("SH", _utc_ms("2025-06-05T02:00:00")) is True
    assert is_trading_minute("SH", _utc_ms("2025-06-05T04:00:00")) is False


def test_unsupported_market_fails_loudly():
    assert supported_market("US") is True
    assert supported_market("MOON") is False
    with pytest.raises(MarketCalendarError, match="不支持的市场"):
        session_state("MOON", _utc_ms("2025-06-05T02:00:00"))
