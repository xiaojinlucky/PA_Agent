"""Construct :class:`DataSource` implementations by kind id."""
from __future__ import annotations

from typing import Literal

from pa_agent.data.base import DataSource
from pa_agent.data.market_defaults import (
    A_SHARE_DEFAULT_SYMBOL,
    GOLD_MT5_SYMBOL,
    GOLD_TV_SYMBOL,
    LONGBRIDGE_DEFAULT_SYMBOL,
    OKX_DEFAULT_SYMBOL,
)

DataSourceKind = Literal[
    "mt5",
    "tradingview",
    "longbridge",
    "okx",
    "akshare",
    "eastmoney",
    "tushare",
    "yfinance",
]

# UI-visible sources only — ``eastmoney`` is config/programmatic, not listed here.
DATA_SOURCE_CHOICES: tuple[tuple[DataSourceKind, str], ...] = (
    ("mt5", "MT5"),
    ("tradingview", "TradingView"),
    ("longbridge", "Longbridge"),
    ("okx", "OKX 行情"),
)

_HIDDEN_KINDS: frozenset[DataSourceKind] = frozenset(
    {"akshare", "eastmoney", "tushare", "yfinance"}
)

_DEFAULT_SYMBOLS: dict[DataSourceKind, str] = {
    "mt5": GOLD_MT5_SYMBOL,
    "tradingview": GOLD_TV_SYMBOL,
    "longbridge": LONGBRIDGE_DEFAULT_SYMBOL,
    "okx": OKX_DEFAULT_SYMBOL,
    "akshare": A_SHARE_DEFAULT_SYMBOL,
    "eastmoney": A_SHARE_DEFAULT_SYMBOL,
    "tushare": A_SHARE_DEFAULT_SYMBOL,
    "yfinance": "GC=F",
}


def default_tradingview_exchange() -> str:
    """Empty string = UI «（自动）» — probe all TV preset venues."""
    return ""


def normalize_data_source_kind(kind: str | None) -> DataSourceKind:
    """Return a supported data-source kind, defaulting to MT5."""
    supported = {k for k, _ in DATA_SOURCE_CHOICES} | _HIDDEN_KINDS
    if kind in supported:
        return kind  # type: ignore[return-value]
    return "mt5"


def data_source_label(kind: str | None) -> str:
    """Human-readable label for *kind*."""
    normalized = normalize_data_source_kind(kind)
    for key, label in DATA_SOURCE_CHOICES:
        if key == normalized:
            return label
    if normalized == "eastmoney":
        return "东方财富"
    if normalized == "tushare":
        return "Tushare(A股)"
    if normalized == "longbridge":
        return "Longbridge"
    if normalized == "akshare":
        return "AkShare"
    if normalized == "yfinance":
        return "YFinance"
    return "MT5"


def default_symbol_for_kind(kind: str | None) -> str:
    return _DEFAULT_SYMBOLS[normalize_data_source_kind(kind)]


def max_analysis_bars_for_kind(kind: str | None) -> int:
    """返回数据源单次刷新可支持的最大分析窗口。"""
    if normalize_data_source_kind(kind) == "longbridge":
        from pa_agent.data.longbridge_source import LONGBRIDGE_MAX_ANALYSIS_BARS

        return LONGBRIDGE_MAX_ANALYSIS_BARS
    if normalize_data_source_kind(kind) == "okx":
        from pa_agent.data.okx_source import OKX_MAX_ANALYSIS_BARS

        return OKX_MAX_ANALYSIS_BARS
    return 5_000


def create_data_source(kind: str | None) -> DataSource:
    """Instantiate a fresh data source for *kind* (not connected)."""
    normalized = normalize_data_source_kind(kind)
    if normalized == "tradingview":
        from pa_agent.data.tradingview import TradingViewSource

        return TradingViewSource()
    if normalized == "longbridge":
        from pa_agent.data.longbridge_source import LongbridgeSource

        return LongbridgeSource()
    if normalized == "okx":
        from pa_agent.data.okx_source import OkxSource

        return OkxSource()
    if normalized == "eastmoney":
        from pa_agent.data.eastmoney_source import EastMoneySource

        return EastMoneySource()
    if normalized == "tushare":
        from pa_agent.config.paths import SETTINGS_JSON_PATH
        from pa_agent.config.settings import load_settings
        from pa_agent.data.tushare_source import TushareSource

        return TushareSource(settings=load_settings(SETTINGS_JSON_PATH))
    if normalized == "akshare":
        from pa_agent.data.akshare_source import AkShareSource

        return AkShareSource()
    if normalized == "yfinance":
        from pa_agent.data.yfinance_source import YFinanceSource

        return YFinanceSource()
    from pa_agent.data.mt5 import MT5Source

    return MT5Source()
