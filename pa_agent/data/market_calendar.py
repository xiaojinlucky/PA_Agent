"""多市场交易日历与会话状态（exchange_calendars 薄封装）。

语义与 Quant/shared/market_contracts 的日历合同保持一致：
US→XNYS、HK→XHKG、SH/SZ→XSHG，加密市场 24/7。
本模块无状态、只读，供 K 线缺口校验、收盘等待和市场时钟共用。
PA_Agent 仓库自包含：不 import 仓库外的共享包，依赖 exchange_calendars。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

_CALENDAR_NAMES: dict[str, str] = {
    "US": "XNYS",
    "HK": "XHKG",
    "SH": "XSHG",
    "SZ": "XSHG",
    "CN": "XSHG",
}


class SessionPhase(StrEnum):
    OPEN = "open"
    BREAK = "break"
    CLOSED = "closed"


@dataclass(frozen=True)
class MarketSessionState:
    """某一时刻的市场会话状态；时间为 UTC 毫秒。"""

    market: str
    phase: SessionPhase
    is_half_day: bool
    as_of_utc_ms: int
    next_change_utc_ms: int | None


class MarketCalendarError(RuntimeError):
    """日历不可用或市场后缀不受支持；调用方不得静默降级。"""


def supported_market(market: str) -> bool:
    return str(market or "").strip().upper() in _CALENDAR_NAMES


@lru_cache(maxsize=8)
def _load_calendar(calendar_name: str):
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise MarketCalendarError(
            "exchange_calendars 未安装，无法判定市场交易时段"
        ) from exc
    return xcals.get_calendar(calendar_name)


def _calendar_for_market(market: str):
    key = str(market or "").strip().upper()
    if key not in _CALENDAR_NAMES:
        raise MarketCalendarError(f"不支持的市场后缀：{market!r}")
    return _load_calendar(_CALENDAR_NAMES[key])


def _timestamp(utc_ms: int):
    import pandas as pd

    return pd.Timestamp(int(utc_ms), unit="ms", tz="UTC")


def _to_utc_ms(timestamp) -> int:
    return int(timestamp.value // 1_000_000)


def session_state(market: str, at_utc_ms: int) -> MarketSessionState:
    """返回市场在指定 UTC 毫秒时刻的会话状态。"""
    import pandas as pd

    calendar = _calendar_for_market(market)
    minute = _timestamp(at_utc_ms)
    open_strict = bool(calendar.is_open_on_minute(minute, ignore_breaks=False))
    open_ignoring_breaks = bool(
        calendar.is_open_on_minute(minute, ignore_breaks=True)
    )

    if not open_ignoring_breaks:
        next_open = calendar.next_open(minute)
        return MarketSessionState(
            market=market,
            phase=SessionPhase.CLOSED,
            is_half_day=False,
            as_of_utc_ms=at_utc_ms,
            next_change_utc_ms=_to_utc_ms(next_open),
        )

    session = calendar.minute_to_session(minute)
    is_half_day = bool(session in calendar.early_closes)
    break_start = calendar.session_break_start(session)
    break_end = calendar.session_break_end(session)
    close = calendar.session_close(session)

    if open_strict:
        if break_start is not pd.NaT and minute < break_start:
            next_change = break_start
        else:
            next_change = close
        return MarketSessionState(
            market=market,
            phase=SessionPhase.OPEN,
            is_half_day=is_half_day,
            as_of_utc_ms=at_utc_ms,
            next_change_utc_ms=_to_utc_ms(next_change),
        )

    return MarketSessionState(
        market=market,
        phase=SessionPhase.BREAK,
        is_half_day=is_half_day,
        as_of_utc_ms=at_utc_ms,
        next_change_utc_ms=_to_utc_ms(break_end),
    )


def is_trading_minute(market: str, at_utc_ms: int) -> bool:
    """指定 UTC 毫秒时刻是否处于连续交易时段（午休不算）。"""
    return session_state(market, at_utc_ms).phase is SessionPhase.OPEN


#: 开盘时段与尾盘各自的窗口长度（分钟）。日内结构在这两段与中段差异显著。
_EDGE_WINDOW_MINUTES = 60


@dataclass(frozen=True)
class IntradayPhase:
    """K 线所处的日内阶段，仅作背景描述。"""

    label: str
    is_half_day: bool
    minutes_from_open: int | None
    minutes_to_close: int | None


def intraday_phase(market: str, at_utc_ms: int) -> IntradayPhase:
    """把会话状态细分为开盘时段/午盘/尾盘/午休/闭市。

    半日市的收盘时间本就更早，尾盘窗口随之自动前移，不需要特判。
    连续交易时段短于两个窗口之和时（如半日市），优先判开盘时段，
    避免同一根 K 线既算开盘又算尾盘。
    """
    state = session_state(market, at_utc_ms)
    if state.phase is SessionPhase.CLOSED:
        return IntradayPhase("闭市", False, None, None)
    if state.phase is SessionPhase.BREAK:
        return IntradayPhase("午休", state.is_half_day, None, None)

    calendar = _calendar_for_market(market)
    minute = _timestamp(at_utc_ms)
    session = calendar.minute_to_session(minute)
    open_ms = _to_utc_ms(calendar.session_open(session))
    close_ms = _to_utc_ms(calendar.session_close(session))
    from_open = max(0, (at_utc_ms - open_ms) // 60000)
    to_close = max(0, (close_ms - at_utc_ms) // 60000)

    if from_open < _EDGE_WINDOW_MINUTES:
        label = "开盘时段"
    elif to_close <= _EDGE_WINDOW_MINUTES:
        label = "尾盘"
    else:
        label = "午盘"
    return IntradayPhase(label, state.is_half_day, int(from_open), int(to_close))
