"""OKX V5 公共 K 线数据源，不读取账户或交易凭据。"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pa_agent.data.base import DataSource, DataSourceTransientError, KlineBar
from pa_agent.execution.credentials import OkxCredentials
from pa_agent.execution.errors import BrokerApiError, BrokerTransportError
from pa_agent.execution.okx_client import OkxRestClient

_TIMEFRAME_TO_OKX_BAR: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "1d": "1Dutc",
}
_PRESET_INSTRUMENTS: tuple[str, ...] = (
    "XAU-USDT-SWAP",
    "XAUT-USDT",
    "BTC-USDT-SWAP",
    "BTC-USDT",
    "ETH-USDT-SWAP",
    "ETH-USDT",
)
_INSTRUMENT_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+){1,2}$")
_MAX_CANDLES = 300

# RefreshLoop 还会额外请求 50 根指标预热和 5 根缓冲。
OKX_MAX_ANALYSIS_BARS = _MAX_CANDLES - 55


def normalize_okx_instrument(symbol: str) -> str:
    """规范并验证 PA 当前支持的 OKX 现货或永续合约代码。"""
    normalized = str(symbol or "").strip().upper()
    if not _INSTRUMENT_RE.fullmatch(normalized):
        raise ValueError(
            "OKX 品种格式无效；现货示例 XAUT-USDT，永续示例 XAU-USDT-SWAP"
        )
    parts = normalized.split("-")
    if len(parts) == 2:
        return normalized
    if len(parts) == 3 and parts[-1] == "SWAP":
        return normalized
    raise ValueError("PA 当前 OKX 行情只支持现货和永续合约")


def okx_instrument_type(symbol: str) -> str:
    normalized = normalize_okx_instrument(symbol)
    return "SWAP" if normalized.endswith("-SWAP") else "SPOT"


class OkxSource(DataSource):
    """从 OKX 公共接口读取任意可交易现货或永续合约的 K 线。"""

    def __init__(self, client: OkxRestClient | None = None) -> None:
        self._client = client or OkxRestClient(
            OkxCredentials(api_key="", secret_key="", passphrase=""),
            base_url="https://www.okx.com",
            simulated=False,
        )
        self._connected = False
        self._symbol = ""
        self._timeframe = ""

    def connect(self) -> None:
        # 公共行情无需登录；具体品种在 subscribe 时向 OKX 在线验证。
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._symbol = ""
        self._timeframe = ""

    def list_symbols(self) -> list[str]:
        return list(_PRESET_INSTRUMENTS)

    def supported_timeframes(self) -> list[str]:
        return list(_TIMEFRAME_TO_OKX_BAR)

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if not self._connected:
            raise DataSourceTransientError("OKX 公共行情源尚未连接")
        if timeframe not in _TIMEFRAME_TO_OKX_BAR:
            raise ValueError(
                f"OKX 不支持周期 {timeframe!r}；可用周期：{list(_TIMEFRAME_TO_OKX_BAR)}"
            )
        normalized = normalize_okx_instrument(symbol)
        instrument_type = okx_instrument_type(normalized)
        try:
            rows = self._client.public_instruments(
                instrument_type,
                instrument=normalized,
            )
        except (BrokerApiError, BrokerTransportError) as exc:
            raise DataSourceTransientError(f"OKX 无法验证品种：{exc}") from exc
        instrument = next(
            (
                row
                for row in rows
                if str(row.get("instId") or "").strip().upper() == normalized
            ),
            None,
        )
        if instrument is None:
            raise ValueError(f"OKX 未找到品种 {normalized}")
        state = str(instrument.get("state") or "").strip().lower()
        if state != "live":
            raise ValueError(f"OKX 品种 {normalized} 当前状态不是 live（可正常交易）")

        # 在线验证全部通过后再切换，失败不会破坏旧订阅。
        self._symbol = normalized
        self._timeframe = timeframe

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""

    def is_symbol_available(self, symbol: str) -> bool:
        try:
            normalize_okx_instrument(symbol)
        except ValueError:
            return False
        return True

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("OKX 公共行情源尚未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("OKX 尚未订阅品种和周期")
        if n < 1:
            return []
        if n > _MAX_CANDLES:
            raise DataSourceTransientError(
                "OKX 最近 K 线接口单次最多返回 300 根；"
                "PA_Agent 的分析 K 线数量请设为不超过 245"
            )
        try:
            rows = self._client.candles(
                instrument=self._symbol,
                bar=_TIMEFRAME_TO_OKX_BAR[self._timeframe],
                limit=n,
            )
        except (BrokerApiError, BrokerTransportError) as exc:
            raise DataSourceTransientError(f"OKX K 线暂时不可用：{exc}") from exc
        if not rows:
            raise DataSourceTransientError(
                f"OKX 未返回 K 线：{self._symbol} {self._timeframe}"
            )

        bars: list[KlineBar] = []
        previous_ts: int | None = None
        closed_seq = 0
        forming_count = 0
        for row in rows[:n]:
            try:
                timestamp = int(row[0])
                open_price = Decimal(row[1])
                high_price = Decimal(row[2])
                low_price = Decimal(row[3])
                close_price = Decimal(row[4])
                volume = Decimal(row[5])
                amount = Decimal(row[7])
                confirm = row[8]
            except (IndexError, InvalidOperation, TypeError, ValueError) as exc:
                raise DataSourceTransientError("OKX K 线字段无法解析") from exc
            numeric_values = (
                open_price,
                high_price,
                low_price,
                close_price,
                volume,
                amount,
            )
            if not all(value.is_finite() for value in numeric_values):
                raise DataSourceTransientError("OKX K 线包含非有限数值")
            if min(open_price, high_price, low_price, close_price) <= 0:
                raise DataSourceTransientError("OKX K 线价格必须为正数")
            if volume < 0 or amount < 0:
                raise DataSourceTransientError("OKX K 线成交量不能为负数")
            if high_price < max(open_price, close_price):
                raise DataSourceTransientError("OKX K 线最高价低于开盘价或收盘价")
            if low_price > min(open_price, close_price):
                raise DataSourceTransientError("OKX K 线最低价高于开盘价或收盘价")
            if previous_ts is not None and timestamp >= previous_ts:
                raise DataSourceTransientError("OKX K 线时间必须严格从新到旧")
            previous_ts = timestamp
            if confirm not in {"0", "1"}:
                raise DataSourceTransientError("OKX K 线收盘标记无效")
            closed = confirm == "1"
            if closed:
                closed_seq += 1
                seq = closed_seq
            else:
                forming_count += 1
                if forming_count > 1:
                    raise DataSourceTransientError("OKX K 线包含多根未收盘数据")
                seq = 0
            bars.append(
                KlineBar(
                    seq=seq,
                    ts_open=float(timestamp),
                    open=float(open_price),
                    high=float(high_price),
                    low=float(low_price),
                    close=float(close_price),
                    volume=float(volume),
                    amount=float(amount),
                    closed=closed,
                )
            )
        return bars
