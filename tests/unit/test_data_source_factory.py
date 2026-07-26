"""Tests for data source factory and settings."""
from __future__ import annotations

from pa_agent.config.settings import GeneralSettings
from pa_agent.data.eastmoney_source import EastMoneySource
from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    create_data_source,
    default_symbol_for_kind,
    default_tradingview_exchange,
    max_analysis_bars_for_kind,
    normalize_data_source_kind,
)
from pa_agent.data.longbridge_source import LongbridgeSource
from pa_agent.data.mt5 import MT5Source
from pa_agent.data.okx_source import OkxSource
from pa_agent.data.tradingview import TradingViewSource
from pa_agent.data.tushare_source import TushareSource


def test_normalize_data_source_kind_defaults_unknown():
    assert normalize_data_source_kind("invalid") == "mt5"
    assert normalize_data_source_kind(None) == "mt5"


def test_normalize_data_source_kind_hidden_sources():
    assert normalize_data_source_kind("akshare") == "akshare"
    assert normalize_data_source_kind("eastmoney") == "eastmoney"
    assert normalize_data_source_kind("tushare") == "tushare"
    assert normalize_data_source_kind("yfinance") == "yfinance"


def test_eastmoney_not_in_ui_choices():
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "eastmoney" not in ui_kinds
    assert "akshare" not in ui_kinds


def test_tushare_not_in_ui_choices():
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "tushare" not in ui_kinds


def test_longbridge_is_visible_in_ui_choices():
    assert "longbridge" in {k for k, _ in DATA_SOURCE_CHOICES}


def test_okx_is_visible_in_ui_choices():
    assert "okx" in {k for k, _ in DATA_SOURCE_CHOICES}


def test_create_data_source_returns_expected_types():
    assert isinstance(create_data_source("mt5"), MT5Source)
    assert isinstance(create_data_source("tradingview"), TradingViewSource)
    assert isinstance(create_data_source("longbridge"), LongbridgeSource)
    assert isinstance(create_data_source("okx"), OkxSource)
    assert isinstance(create_data_source("eastmoney"), EastMoneySource)
    assert isinstance(create_data_source("tushare"), TushareSource)


def test_default_symbols_per_kind():
    assert default_symbol_for_kind("mt5") == "XAUUSDm"
    assert default_symbol_for_kind("tradingview") == "XAUUSD"
    assert default_symbol_for_kind("longbridge") == "AAPL.US"
    assert default_symbol_for_kind("okx") == "XAU-USDT-SWAP"
    assert default_symbol_for_kind("eastmoney") == "000001"
    assert default_symbol_for_kind("tushare") == "000001"


def test_longbridge_analysis_limit_reserves_refresh_warmup():
    # 长桥历史 K 线已支持 by_offset 分页；分析上限 3000 + 预热 55 根
    # 仍须落在单次快照硬上限 5000 之内。
    assert max_analysis_bars_for_kind("longbridge") == 3000
    assert max_analysis_bars_for_kind("longbridge") + 55 <= 5000
    assert max_analysis_bars_for_kind("okx") == 245
    assert max_analysis_bars_for_kind("mt5") == 5_000


def test_default_tradingview_exchange_is_auto():
    assert default_tradingview_exchange() == ""


def test_general_settings_last_data_source_default():
    g = GeneralSettings()
    assert g.last_data_source == "mt5"
