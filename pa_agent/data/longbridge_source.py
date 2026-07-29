"""Longbridge OpenAPI 只读行情数据源。"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from decimal import InvalidOperation
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pa_agent.data.bar_close_wait import timeframe_to_seconds
from pa_agent.data.base import (
    DataSource,
    DataSourceAuthenticationError,
    DataSourceError,
    DataSourcePermissionError,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)
from pa_agent.data.datetime_ts import datetime_to_ts_ms
from pa_agent.data.market_calendar import MarketCalendarError, is_trading_minute
from pa_agent.data.market_workspace import (
    QuoteMode,
    QuoteSnapshot,
    WatchlistRequestToken,
)

logger = logging.getLogger(__name__)

_TF_MAP: dict[str, str] = {
    "1m": "Min_1",
    "2m": "Min_2",
    "3m": "Min_3",
    "5m": "Min_5",
    "10m": "Min_10",
    "15m": "Min_15",
    "20m": "Min_20",
    "30m": "Min_30",
    "45m": "Min_45",
    "1h": "Min_60",
    "2h": "Min_120",
    "3h": "Min_180",
    "4h": "Min_240",
    "1d": "Day",
    "1w": "Week",
}

_PRESET_SYMBOLS: tuple[str, ...] = (
    "AAPL.US",
    "GLD.US",
    "SPY.US",
    "QQQ.US",
    "TSLA.US",
    "700.HK",
    "9988.HK",
    "600519.SH",
    "000001.SZ",
)

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._!-]*\.[A-Za-z]{2,4}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_CANDLESTICKS = 1000
_MAX_TOTAL_CANDLESTICKS = 5000
_QUOTE_RATE_LIMIT_PER_SECOND = 10
_SDK_CALL_TIMEOUT_SECONDS = 15.0
_SUPPORTED_MARKET_SUFFIXES = frozenset({"US", "HK", "SH", "SZ"})
_MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "CN": ZoneInfo("Asia/Shanghai"),
}
# RefreshLoop 会在分析窗口外额外请求 50 根指标预热和 5 根缓冲；
# 超过 1000 根的部分由 history_candlesticks_by_offset 分页补齐。
LONGBRIDGE_MAX_ANALYSIS_BARS = 3000
LONGBRIDGE_TOKEN_EXPIRING_THRESHOLD = timedelta(days=7)
_QUOTE_PROFILE_ENV = "PA_AGENT_LONGBRIDGE_QUOTE_PROFILE"
_QUOTE_PROFILE_PREFIXES: dict[str, tuple[str, ...]] = {
    "default": ("LONGBRIDGE", "LONGPORT"),
    "comprehensive": ("LONGBRIDGE_COMPREHENSIVE",),
    "intraday": ("LONGBRIDGE_INTRADAY",),
}


class _QuoteRateLimiter:
    """长桥行情 SDK 频控：官方限制每秒最多 10 次调用。"""

    def __init__(
        self, max_calls_per_second: int = _QUOTE_RATE_LIMIT_PER_SECOND
    ) -> None:
        self._max_calls = max_calls_per_second
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 1.0:
                    self._calls.popleft()
                if len(self._calls) < self._max_calls:
                    self._calls.append(now)
                    return
                wait_seconds = 1.0 - (now - self._calls[0])
            time.sleep(max(wait_seconds, 0.01))

LongbridgeTokenStatus = Literal["valid", "expiring", "expired", "unknown"]


@dataclass(frozen=True)
class LongbridgeCredentials:
    """Longbridge Legacy API Key 三件套，仅在连接阶段短暂使用。"""

    app_key: str
    app_secret: str
    access_token: str


@dataclass(frozen=True)
class LongbridgeTokenExpiry:
    """Legacy Access Token 的本地到期预检结果。"""

    status: LongbridgeTokenStatus
    expires_at_utc: datetime | None


def inspect_longbridge_token_expiry(
    access_token: str,
    *,
    now: datetime | None = None,
) -> LongbridgeTokenExpiry:
    """从 JWT ``exp`` 读取到期时间, 不验签, 结果仅用于连接前预检。"""
    parts = str(access_token or "").split(".")
    if len(parts) != 3:
        return LongbridgeTokenExpiry("unknown", None)

    try:
        encoded_payload = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(encoded_payload.encode("ascii")).decode("utf-8")
        )
        exp = payload.get("exp") if isinstance(payload, dict) else None
        if (
            isinstance(exp, bool)
            or not isinstance(exp, (int, float))
            or not math.isfinite(float(exp))
        ):
            return LongbridgeTokenExpiry("unknown", None)
        expires_at_utc = datetime.fromtimestamp(float(exp), tz=UTC)
    except (ValueError, TypeError, OSError, OverflowError, UnicodeError, json.JSONDecodeError):
        return LongbridgeTokenExpiry("unknown", None)

    return classify_longbridge_token_expiry(expires_at_utc, now=now)


def classify_longbridge_token_expiry(
    expires_at_utc: datetime | None,
    *,
    now: datetime | None = None,
) -> LongbridgeTokenExpiry:
    """按当前时刻重新分类已解析的 Token 到期时间。"""
    if expires_at_utc is None:
        return LongbridgeTokenExpiry("unknown", None)
    expires_at_utc = (
        expires_at_utc.replace(tzinfo=UTC)
        if expires_at_utc.tzinfo is None
        else expires_at_utc.astimezone(UTC)
    )
    current = now or datetime.now(UTC)
    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)

    if expires_at_utc <= current:
        status: LongbridgeTokenStatus = "expired"
    elif expires_at_utc <= current + LONGBRIDGE_TOKEN_EXPIRING_THRESHOLD:
        status = "expiring"
    else:
        status = "valid"
    return LongbridgeTokenExpiry(status, expires_at_utc)


def _read_env_file(path: Path) -> dict[str, str]:
    """读取简单 ``KEY=VALUE`` 文件，不修改进程环境。"""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise DataSourceTransientError(f"无法读取 Longbridge 凭据文件：{path}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _ENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _credentials_from_mapping(
    values: Mapping[str, str],
    *,
    prefixes: tuple[str, ...] = _QUOTE_PROFILE_PREFIXES["default"],
) -> LongbridgeCredentials | None:
    """只接受同一命名空间中完整的三项凭据，禁止跨组拼接。"""
    for prefix in prefixes:
        app_key = str(values.get(f"{prefix}_APP_KEY", "") or "").strip()
        app_secret = str(values.get(f"{prefix}_APP_SECRET", "") or "").strip()
        access_token = str(values.get(f"{prefix}_ACCESS_TOKEN", "") or "").strip()
        present = (bool(app_key), bool(app_secret), bool(access_token))
        if all(present):
            return LongbridgeCredentials(app_key, app_secret, access_token)
        if any(present):
            missing = [
                name
                for name, configured in zip(
                    ("APP_KEY", "APP_SECRET", "ACCESS_TOKEN"), present, strict=True
                )
                if not configured
            ]
            raise DataSourceTransientError(
                f"{prefix}_* 凭据不完整，缺少：{', '.join(missing)}"
            )
    return None


def _selected_quote_profile(
    process_values: Mapping[str, str],
    file_values: Mapping[str, str],
) -> str:
    """读取非机密的行情凭据档案选择；进程环境优先于共享文件。"""
    raw = str(
        process_values.get(_QUOTE_PROFILE_ENV)
        or file_values.get(_QUOTE_PROFILE_ENV)
        or "default"
    ).strip()
    profile = raw.lower()
    if profile not in _QUOTE_PROFILE_PREFIXES:
        raise DataSourceTransientError(
            "Longbridge 行情凭据档案无效；只允许 default、"
            "comprehensive 或 intraday"
        )
    return profile


def load_longbridge_credentials(env_file: Path | None = None) -> LongbridgeCredentials:
    """按显式行情档案先读进程环境，再读 Quant 根目录共享 ``env``。"""
    from pa_agent.execution.credentials import shared_env_path

    shared_env = env_file or shared_env_path()
    file_values = _read_env_file(shared_env)
    profile = _selected_quote_profile(os.environ, file_values)
    prefixes = _QUOTE_PROFILE_PREFIXES[profile]
    for values in (os.environ, file_values):
        credentials = _credentials_from_mapping(values, prefixes=prefixes)
        if credentials is not None:
            return credentials
    expected = " 或 ".join(f"{prefix}_*" for prefix in prefixes)
    raise DataSourceTransientError(
        f"未找到完整的 Longbridge 行情凭据档案 {profile}；"
        f"请在 Quant\\env 配置同组 {expected}"
    )


def normalize_longbridge_symbol(symbol: str) -> str:
    """校验并规范 Longbridge ``ticker.region`` 代码。"""
    value = (symbol or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(value):
        raise ValueError(
            "Longbridge 品种代码必须为 ticker.region，例如 AAPL.US、700.HK、"
            "600519.SH 或 000001.SZ"
        )
    suffix = value.rsplit(".", 1)[-1]
    if suffix not in _SUPPORTED_MARKET_SUFFIXES:
        raise ValueError("Longbridge 当前仅支持 US、HK、SH、SZ 市场后缀")
    return value


def _now_in_market_timezone(timezone: ZoneInfo) -> datetime:
    """可测试的市场本地当前时间入口。"""
    return datetime.now(timezone)


def _timestamp_in_market_timezone(timestamp: datetime, timezone: ZoneInfo) -> datetime:
    """把 SDK 时间戳转换为交易所本地时间。"""
    if timestamp.tzinfo is None:
        # Longbridge SDK 的 naive datetime 表示宿主机本地墙钟时间；
        # astimezone 会先按宿主机时区解释，再转换到交易所时区。
        return timestamp.astimezone(timezone)
    return timestamp.astimezone(timezone)


def _sdk_market_name(sdk: Any, market: Any) -> str | None:
    for name in _MARKET_TIMEZONES:
        if market == getattr(sdk.Market, name):
            return name
    return None


class LongbridgeSource(DataSource):
    """通过官方 Longbridge SDK 的 ``QuoteContext`` 拉取只读 K 线。"""

    def __init__(self, *, env_file: Path | None = None) -> None:
        self._env_file = env_file
        self._quote_context: Any = None
        self._sdk: Any = None
        self._connected = False
        self._symbol = ""
        self._timeframe = ""
        self._snapshot_lock = threading.Lock()
        self._regular_session_closes: dict[str, datetime_time] = {}
        self._regular_session_windows: dict[
            str, tuple[tuple[datetime_time, datetime_time], ...]
        ] = {}
        self._market: Any = None
        self._market_name = ""
        self._market_timezone: ZoneInfo | None = None
        self._weekly_last_trading_day_cache: dict[tuple[str, date], date] = {}
        self._token_expiry = LongbridgeTokenExpiry("unknown", None)
        self._rate_limiter = _QuoteRateLimiter()
        self._sdk_executor: ThreadPoolExecutor | None = None

    def _call_quote_sdk(self, label: str, fn: Any, *args: Any) -> Any:
        """频控 + 超时保护后执行一次行情 SDK 调用。

        长桥 SDK 存在偶发同步调用卡顿（上游 issue #380）；
        超时后不重试、明确失败，由上层决定下一步。
        """
        executor = self._sdk_executor
        if executor is None:
            raise DataSourceTransientError("Longbridge 未连接")
        self._rate_limiter.acquire()
        future = executor.submit(fn, *args)
        try:
            return future.result(timeout=_SDK_CALL_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            future.cancel()
            raise DataSourceTransientError(
                f"Longbridge {label}超过 {_SDK_CALL_TIMEOUT_SECONDS:.0f} 秒未响应"
                "（SDK 卡顿保护，已放弃本次调用）"
            ) from exc

    @property
    def token_expiry(self) -> LongbridgeTokenExpiry:
        """返回当前凭据的本地到期预检结果, 不暴露 Token。"""
        return classify_longbridge_token_expiry(self._token_expiry.expires_at_utc)

    def connect(self) -> None:
        credentials = load_longbridge_credentials(self._env_file)
        token_expiry = inspect_longbridge_token_expiry(credentials.access_token)
        self._token_expiry = token_expiry
        if token_expiry.status == "expired":
            self._connected = False
            expires_at = token_expiry.expires_at_utc
            expires_text = (
                expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                if expires_at is not None
                else "未知时间"
            )
            raise DataSourceAuthenticationError(
                f"Longbridge Legacy Access Token 已过期 ({expires_text}), 请更换凭据"
            )
        if token_expiry.status == "expiring":
            expires_at = token_expiry.expires_at_utc
            logger.warning(
                "Longbridge Legacy Access Token 即将到期 (%s), 请及时更换凭据",
                (
                    expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                    if expires_at is not None
                    else "未知时间"
                ),
            )
        try:
            from longbridge import openapi as sdk
        except ImportError as exc:
            raise DataSourceTransientError(
                "未安装 Longbridge SDK，请执行：pip install longbridge"
            ) from exc

        if self._sdk_executor is not None:
            self._sdk_executor.shutdown(wait=False, cancel_futures=True)
        self._sdk_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="lb-quote"
        )
        try:
            config = sdk.Config.from_apikey(
                credentials.app_key,
                credentials.app_secret,
                credentials.access_token,
                enable_print_quote_packages=False,
            )
            quote_context = self._call_quote_sdk("连接", sdk.QuoteContext, config)
            session_closes: dict[str, datetime_time] = {}
            session_windows: dict[
                str, tuple[tuple[datetime_time, datetime_time], ...]
            ] = {}
            for market_session in self._call_quote_sdk(
                "交易时段读取", quote_context.trading_session
            ):
                intraday_sessions = [
                    session
                    for session in market_session.trade_sessions
                    if session.trade_session == sdk.TradeSession.Intraday
                ]
                market_name = _sdk_market_name(sdk, market_session.market)
                if market_name is not None and intraday_sessions:
                    windows = [
                        (session.begin_time, session.end_time)
                        for session in intraday_sessions
                    ]
                    if market_name == "CN":
                        last_index = max(
                            range(len(windows)), key=lambda index: windows[index][1]
                        )
                        begin_time, end_time = windows[last_index]
                        windows[last_index] = (
                            begin_time,
                            max(end_time, datetime_time(15, 0)),
                        )
                    session_windows[market_name] = tuple(windows)
                    session_closes[market_name] = max(end for _, end in windows)
            # SDK 的 CN Intraday 尾点可能是最后一根分钟线的起始时间（如 14:57），
            # 日线/周线是否收盘必须按沪深常规收盘 15:00 判断。
        except DataSourceTransientError:
            self._connected = False
            raise
        except Exception as exc:
            self._connected = False
            error_type = (
                DataSourceAuthenticationError
                if "401004" in str(exc)
                else DataSourceTransientError
            )
            raise error_type(
                f"Longbridge 连接失败（认证或网络异常，{type(exc).__name__}）"
            ) from exc

        self._sdk = sdk
        self._quote_context = quote_context
        self._regular_session_closes = session_closes
        self._regular_session_windows = session_windows
        self._connected = True
        logger.info("LongbridgeSource connected (quote-only)")

    def disconnect(self) -> None:
        with self._snapshot_lock:
            self._quote_context = None
            self._sdk = None
            self._connected = False
            self._regular_session_closes = {}
            self._regular_session_windows = {}
            self._market = None
            self._market_name = ""
            self._market_timezone = None
            self._weekly_last_trading_day_cache = {}
            if self._sdk_executor is not None:
                self._sdk_executor.shutdown(wait=False, cancel_futures=True)
                self._sdk_executor = None
        logger.info("LongbridgeSource disconnected")

    def list_symbols(self) -> list[str]:
        return list(_PRESET_SYMBOLS)

    def supported_timeframes(self) -> list[str]:
        return list(_TF_MAP)

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _TF_MAP:
            raise ValueError(
                f"Longbridge 不支持周期 {timeframe!r}；可用周期：{list(_TF_MAP)}"
            )
        normalized = normalize_longbridge_symbol(symbol)
        if not self._connected or self._quote_context is None:
            raise DataSourceTransientError("Longbridge 未连接")
        try:
            with self._snapshot_lock:
                static_rows = self._call_quote_sdk(
                    "品种校验", self._quote_context.static_info, [normalized]
                )
        except DataSourceTransientError:
            raise
        except Exception as exc:
            raise DataSourceTransientError(
                f"Longbridge 无法验证品种（网络或行情权限异常，{type(exc).__name__}）"
            ) from exc
        if not static_rows:
            raise DataSourceTransientError(
                f"Longbridge 未找到品种 {normalized}，请检查代码或行情权限"
            )
        suffix = normalized.rsplit(".", 1)[-1]
        market_name = "CN" if suffix in {"SH", "SZ"} else suffix
        market = getattr(self._sdk.Market, market_name)
        market_timezone = _MARKET_TIMEZONES[market_name]
        if market_name not in self._regular_session_closes:
            raise DataSourceTransientError(
                f"Longbridge 未返回 {market_name} 市场常规交易时段"
            )
        self._symbol = normalized
        self._timeframe = timeframe
        self._market = market
        self._market_name = market_name
        self._market_timezone = market_timezone
        logger.info("LongbridgeSource subscribed: %s %s", self._symbol, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        self._market = None
        self._market_name = ""
        self._market_timezone = None
        logger.info("LongbridgeSource unsubscribed")

    def is_symbol_available(self, symbol: str) -> bool:
        try:
            normalize_longbridge_symbol(symbol)
        except ValueError:
            return False
        return True

    def batch_quote_snapshots(
        self,
        token: WatchlistRequestToken,
        *,
        received_at_utc_ms: int,
        quote_mode: QuoteMode | None = None,
        expected_delay_ms: int = 0,
    ) -> tuple[QuoteSnapshot, ...]:
        """一次 SDK 调用读取最多 100 个股票报价，不改变主图订阅。"""
        if not self._connected or self._quote_context is None:
            raise DataSourceTransientError("Longbridge 未连接")
        if quote_mode is None:
            raise DataSourcePermissionError(
                "Longbridge 行情套餐未证明实时或延迟模式，拒绝标记报价"
            )
        if quote_mode not in {"realtime", "delayed"}:
            raise ValueError("Longbridge quote_mode 只支持 realtime 或 delayed")
        if quote_mode == "realtime" and expected_delay_ms != 0:
            raise ValueError("Longbridge 实时报价的 expected_delay_ms 必须为 0")
        if quote_mode == "delayed" and expected_delay_ms <= 0:
            raise ValueError("Longbridge 延迟报价必须声明正数 expected_delay_ms")
        if token.source != "longbridge" or token.market == "Crypto":
            raise ValueError("Longbridge 批量报价只接受 US/HK/CN 批次")

        request_by_symbol: dict[str, str] = {}
        for symbol_value in token.symbols:
            symbol = normalize_longbridge_symbol(symbol_value)
            suffix = symbol.rsplit(".", 1)[-1]
            actual_market = "CN" if suffix in {"SH", "SZ"} else suffix
            if actual_market != token.market:
                raise ValueError(f"{symbol} 与所选市场 {token.market} 不一致")
            if symbol in request_by_symbol:
                raise ValueError(f"批量报价包含重复品种：{symbol}")
            request_by_symbol[symbol] = symbol

        symbols = list(request_by_symbol)
        try:
            with self._snapshot_lock:
                static_rows = self._call_quote_sdk(
                    "静态资料批量读取",
                    self._quote_context.static_info,
                    symbols,
                )
                quote_rows = self._call_quote_sdk(
                    "批量报价读取",
                    self._quote_context.quote,
                    symbols,
                )
        except DataSourceTransientError:
            raise
        except Exception as exc:
            if "401004" in str(exc):
                raise DataSourceAuthenticationError(
                    "Longbridge 批量报价认证失败，请更换 Legacy Access Token"
                ) from exc
            raise DataSourceTransientError(
                f"Longbridge 批量报价失败（{type(exc).__name__}）"
            ) from exc

        static_by_symbol = {
            str(getattr(row, "symbol", "") or "").strip().upper(): row
            for row in (static_rows or [])
        }
        quote_by_symbol = {
            str(getattr(row, "symbol", "") or "").strip().upper(): row
            for row in (quote_rows or [])
        }
        snapshots: list[QuoteSnapshot] = []
        for symbol in request_by_symbol:
            static = static_by_symbol.get(symbol)
            quote = quote_by_symbol.get(symbol)
            if static is None or quote is None:
                raise DataSourceTransientError(
                    f"Longbridge 批量报价缺少 {symbol} 的静态资料或报价"
                )
            market = token.market
            name_fields = (
                ("name_cn", "name_en", "name_hk")
                if market == "CN"
                else ("name_hk", "name_cn", "name_en")
                if market == "HK"
                else ("name_en", "name_cn", "name_hk")
            )
            name = next(
                (
                    str(getattr(static, field, "") or "").strip()
                    for field in name_fields
                    if str(getattr(static, field, "") or "").strip()
                ),
                symbol,
            )
            timestamp = getattr(quote, "timestamp", None)
            if not isinstance(timestamp, datetime):
                raise DataSourceTransientError(
                    f"Longbridge {symbol} 报价缺少有效时间"
                )
            try:
                snapshots.append(
                    QuoteSnapshot.from_prices(
                        selection_generation=token.selection_generation,
                        request_sequence=token.watchlist_refresh_sequence,
                        symbol=symbol,
                        market=market,
                        source="longbridge",
                        name=name,
                        currency=getattr(static, "currency", None),
                        last=getattr(quote, "last_done", None),
                        prev_close=getattr(quote, "prev_close", None),
                        # Longbridge quote/static_info 不声明真实最小跳动；
                        # 报价可以展示，但不得据此生成可执行价格。
                        price_tick=None,
                        quote_ts_utc_ms=datetime_to_ts_ms(timestamp),
                        received_at_utc_ms=received_at_utc_ms,
                        quote_mode=quote_mode,
                        expected_delay_ms=expected_delay_ms,
                    )
                )
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise DataSourceTransientError(
                    f"Longbridge {symbol} 报价字段无法解析"
                ) from exc
        return tuple(snapshots)

    def _last_trading_day_of_week(self, week_start: date) -> date:
        if self._market is None or self._quote_context is None:
            raise DataSourceTransientError("Longbridge 未订阅市场")
        cache_key = (self._market_name, week_start)
        cached = self._weekly_last_trading_day_cache.get(cache_key)
        if cached is not None:
            return cached
        week_end = week_start + timedelta(days=6)
        try:
            with self._snapshot_lock:
                calendar = self._call_quote_sdk(
                    "交易日历读取",
                    self._quote_context.trading_days,
                    self._market,
                    week_start,
                    week_end,
                )
        except DataSourceTransientError:
            raise
        except Exception as exc:
            raise DataSourceTransientError(
                f"Longbridge 交易日历读取失败（{type(exc).__name__}）"
            ) from exc
        trading_days = list(calendar.trading_days) + list(calendar.half_trading_days)
        if not trading_days:
            raise DataSourceTransientError("Longbridge 未返回本周交易日历")
        last_day = max(trading_days)
        self._weekly_last_trading_day_cache[cache_key] = last_day
        return last_day

    def _head_bar_is_forming(
        self,
        timestamp: datetime,
        duration_s: int | None,
        timeframe: str,
    ) -> bool:
        if self._market is None or self._market_timezone is None:
            raise DataSourceTransientError("Longbridge 未订阅市场")
        market_open = _timestamp_in_market_timezone(timestamp, self._market_timezone)
        market_now = _now_in_market_timezone(self._market_timezone)
        session_close = self._regular_session_closes[self._market_name]

        if timeframe == "1d":
            close_at = datetime.combine(
                market_open.date(), session_close, self._market_timezone
            )
            return market_now < close_at
        if timeframe == "1w":
            week_start = market_open.date() - timedelta(days=market_open.weekday())
            last_trading_day = self._last_trading_day_of_week(week_start)
            close_at = datetime.combine(
                last_trading_day, session_close, self._market_timezone
            )
            return market_now < close_at
        if duration_s is None:
            return False
        close_at = market_open + timedelta(seconds=duration_s)
        market_close_at = self._session_close_for_bar(market_open)
        if market_close_at is not None:
            close_at = min(close_at, market_close_at)
        return market_now < close_at

    def _session_close_for_bar(self, market_open: datetime) -> datetime | None:
        if self._market_timezone is None:
            raise DataSourceTransientError("Longbridge 未订阅市场")
        windows = self._regular_session_windows.get(self._market_name, ())
        open_time = market_open.timetz().replace(tzinfo=None)
        for begin_time, end_time in windows:
            if begin_time <= open_time <= end_time:
                return datetime.combine(
                    market_open.date(), end_time, self._market_timezone
                )
        return None

    def _timestamp_to_market_epoch_ms(self, timestamp: datetime) -> int:
        if self._market_timezone is None:
            raise DataSourceTransientError("Longbridge 未订阅市场")
        localized = _timestamp_in_market_timezone(timestamp, self._market_timezone)
        return datetime_to_ts_ms(localized)

    def _fetch_rows(self, timeframe: str, n: int) -> list[Any]:
        """最近 1000 根用 candlesticks；更早历史用 by_offset 分页回溯。"""
        period = getattr(self._sdk.Period, _TF_MAP[timeframe])
        try:
            rows = list(
                self._call_quote_sdk(
                    "K 线拉取",
                    self._quote_context.candlesticks,
                    self._symbol,
                    period,
                    int(min(n, _MAX_CANDLESTICKS)),
                    self._sdk.AdjustType.NoAdjust,
                    self._sdk.TradeSessions.Intraday,
                )
                or []
            )
            seen_timestamps = {row.timestamp for row in rows}
            while rows and len(rows) < n:
                oldest = min(rows, key=lambda row: row.timestamp)
                older_rows = self._call_quote_sdk(
                    "历史 K 线分页",
                    self._quote_context.history_candlesticks_by_offset,
                    self._symbol,
                    period,
                    self._sdk.AdjustType.NoAdjust,
                    False,
                    int(min(n - len(rows) + 1, _MAX_CANDLESTICKS)),
                    oldest.timestamp,
                )
                fresh = [
                    row
                    for row in (older_rows or [])
                    if row.timestamp not in seen_timestamps
                ]
                if not fresh:
                    break
                rows.extend(fresh)
                seen_timestamps.update(row.timestamp for row in fresh)
        except DataSourceTransientError:
            raise
        except Exception as exc:
            raise DataSourceTransientError(
                f"Longbridge K 线拉取失败（{type(exc).__name__}）"
            ) from exc
        return rows

    def _assert_no_intraday_gaps(
        self, closed_ts_ascending_ms: list[int], timeframe: str
    ) -> None:
        """校验交易时段内不缺根（上游已知 5m 类缺根 issue #890）。

        相邻已收盘 K 线之间若存在"应有开盘时刻落在连续交易时段内却没有
        对应 K 线"，判定为数据缺口并拒绝分析；午休、收盘、周末与节假日
        的间隔按交易日历判定为合法。只校验 1 小时及以下周期——更高周期
        的棒边界与固定步长网格不对齐，无法用算术枚举。
        """
        duration_s = timeframe_to_seconds(timeframe)
        if duration_s is None or duration_s > 3600:
            return
        step_ms = duration_s * 1000
        for previous_ts, next_ts in zip(
            closed_ts_ascending_ms, closed_ts_ascending_ms[1:], strict=False
        ):
            for slot_ms in range(previous_ts + step_ms, next_ts, step_ms):
                try:
                    slot_is_trading = is_trading_minute(self._market_name, slot_ms)
                except MarketCalendarError as exc:
                    raise DataSourceTransientError(
                        f"Longbridge 缺口校验无法取得交易日历（{exc}）"
                    ) from exc
                if slot_is_trading:
                    missing_at = datetime.fromtimestamp(
                        slot_ms / 1000, tz=UTC
                    ).isoformat()
                    raise DataSourceError(
                        f"Longbridge {self._symbol} {timeframe} 在交易时段内"
                        f"缺根（{missing_at} 应有 K 线未返回，参考上游 issue "
                        "#890），拒绝用不完整数据分析"
                    )

    def _snapshot_for(self, timeframe: str, n: int) -> list[KlineBar]:
        if not self._connected or self._quote_context is None or self._sdk is None:
            raise DataSourceTransientError("Longbridge 未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("Longbridge 未订阅品种/周期")
        if timeframe not in _TF_MAP:
            raise ValueError(
                f"Longbridge 不支持周期 {timeframe!r}；可用周期：{list(_TF_MAP)}"
            )
        if n < 1:
            return []
        if n > _MAX_TOTAL_CANDLESTICKS:
            raise DataSourceTransientError(
                f"Longbridge 单次快照上限 {_MAX_TOTAL_CANDLESTICKS} 根"
                f"（含分页），请求 {n} 根被拒绝"
            )

        with self._snapshot_lock:
            rows = self._fetch_rows(timeframe, n)

        if not rows:
            raise DataSourceTransientError(
                f"Longbridge 未返回 K 线：{self._symbol} {timeframe}"
            )

        ordered = sorted(
            rows,
            key=lambda row: self._timestamp_to_market_epoch_ms(row.timestamp),
            reverse=True,
        )
        duration_s = timeframe_to_seconds(timeframe)
        head_is_forming = self._head_bar_is_forming(
            ordered[0].timestamp, duration_s, timeframe
        )
        bars: list[KlineBar] = []
        closed_seq = 1
        for i, row in enumerate(ordered[:n]):
            ts_ms = self._timestamp_to_market_epoch_ms(row.timestamp)
            still_forming = bool(i == 0 and head_is_forming)
            seq = 0 if still_forming else closed_seq
            if not still_forming:
                closed_seq += 1
            bars.append(
                normalize_kline_bar(
                    KlineBar(
                        seq=seq,
                        ts_open=float(ts_ms),
                        open=float(row.open),
                        high=float(row.high),
                        low=float(row.low),
                        close=float(row.close),
                        volume=float(row.volume),
                        amount=float(row.turnover),
                        closed=not still_forming,
                    )
                )
            )
        closed_ts_ascending = sorted(
            int(bar.ts_open) for bar in bars if bar.closed
        )
        self._assert_no_intraday_gaps(closed_ts_ascending, timeframe)
        return bars

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        return self._snapshot_for(self._timeframe, n)

    def latest_snapshot_for_timeframe(
        self, timeframe: str, n: int
    ) -> list[KlineBar]:
        """按指定周期返回最近 K 线；供 1h/4h 高周期薄背景使用。"""
        return self._snapshot_for(timeframe, n)
