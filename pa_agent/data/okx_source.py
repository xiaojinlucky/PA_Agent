"""OKX V5 公共 K 线数据源，不读取账户或交易凭据。"""
from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from pa_agent.data.base import DataSource, DataSourceTransientError, KlineBar
from pa_agent.data.bar_close_wait import timeframe_to_seconds
from pa_agent.data.market_workspace import QuoteSnapshot, WatchlistRequestToken
from pa_agent.data.okx_public_client import (
    OkxPublicClient,
    OkxPublicError,
)

_TIMEFRAME_TO_OKX_BAR: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    # OKX 没有原生 10m；读取真实 5m 后在本地按 UTC 边界两两聚合。
    "10m": "5m",
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
_MAX_CANDLE_PAGES = 3
_MAX_TEN_MINUTE_BARS = 300
_TEN_MINUTE_MS = 10 * 60 * 1000
_FIVE_MINUTE_MS = 5 * 60 * 1000

# RefreshLoop 还会额外请求 50 根指标预热和 5 根缓冲。
OKX_MAX_ANALYSIS_BARS = _MAX_CANDLES - 55


def aggregate_okx_five_minute_rows(
    rows: list[list[str]],
    *,
    limit: int,
) -> list[list[str]]:
    """把 OKX 新到旧的真实 5m 行按 UTC 边界聚合成 10m 行。"""
    groups: dict[int, list[list[str]]] = {}
    previous_timestamp: int | None = None
    for row in rows:
        try:
            if len(row) < 9 or row[8] not in {"0", "1"}:
                raise ValueError("missing or invalid confirm")
            timestamp = int(row[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise DataSourceTransientError("OKX 5m K 线时间或收盘标记无法解析") from exc
        if timestamp % _FIVE_MINUTE_MS != 0:
            raise DataSourceTransientError("OKX 5m K 线没有对齐 UTC 5 分钟边界")
        if previous_timestamp is not None:
            if timestamp >= previous_timestamp:
                raise DataSourceTransientError("OKX 5m K 线必须严格从新到旧")
            if previous_timestamp - timestamp != _FIVE_MINUTE_MS:
                raise DataSourceTransientError("OKX 5m K 线缺根，不能聚合 10m")
        previous_timestamp = timestamp
        bucket = timestamp - timestamp % _TEN_MINUTE_MS
        groups.setdefault(bucket, []).append(row)

    aggregated: list[list[str]] = []
    newest_bucket = max(groups, default=None)
    oldest_bucket = min(groups, default=None)
    for bucket in sorted(groups, reverse=True):
        parts = sorted(groups[bucket], key=lambda item: int(item[0]))
        exact_pair = (
            len(parts) == 2
            and int(parts[0][0]) == bucket
            and int(parts[1][0]) == bucket + _FIVE_MINUTE_MS
        )
        if not exact_pair:
            if bucket == newest_bucket:
                if len(parts) != 1 or parts[0][8] != "0":
                    raise DataSourceTransientError(
                        "OKX 最新 10m 桶缺根或状态异常，不能聚合"
                    )
                continue
            if bucket == oldest_bucket:
                continue
            raise DataSourceTransientError("OKX 5m K 线跨越 10m 边界，不能聚合")
        try:
            open_price = Decimal(parts[0][1])
            high_price = max(Decimal(part[2]) for part in parts)
            low_price = min(Decimal(part[3]) for part in parts)
            close_price = Decimal(parts[-1][4])
            volume = sum((Decimal(part[5]) for part in parts), Decimal("0"))
            volume_ccy = sum((Decimal(part[6]) for part in parts), Decimal("0"))
            amount = sum((Decimal(part[7]) for part in parts), Decimal("0"))
        except (IndexError, InvalidOperation, TypeError, ValueError) as exc:
            raise DataSourceTransientError("OKX 5m K 线字段无法聚合") from exc
        if not all(part[8] == "1" for part in parts):
            if bucket == newest_bucket:
                continue
            raise DataSourceTransientError("OKX 5m K 线尚未全部收盘，不能聚合 10m")
        aggregated.append(
            [
                str(bucket),
                str(open_price),
                str(high_price),
                str(low_price),
                str(close_price),
                str(volume),
                str(volume_ccy),
                str(amount),
                "1",
            ]
        )
        if len(aggregated) >= limit:
            break
    if not aggregated:
        raise DataSourceTransientError("OKX 没有两根连续且均已收盘的 5m K 线可聚合为 10m")
    return aggregated


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

    def __init__(self, client: Any | None = None) -> None:
        self._client = client or OkxPublicClient()
        self._connected = False
        self._symbol = ""
        self._timeframe = ""
        self._price_tick: str | None = None

    def connect(self) -> None:
        # 公共行情无需登录；具体品种在 subscribe 时向 OKX 在线验证。
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._symbol = ""
        self._timeframe = ""
        self._price_tick = None

    def list_symbols(self) -> list[str]:
        return list(_PRESET_INSTRUMENTS)

    def supported_timeframes(self) -> list[str]:
        return list(_TIMEFRAME_TO_OKX_BAR)

    @staticmethod
    def closed_bar_end_utc_ms(
        bar: KlineBar,
        timeframe: str,
    ) -> int:
        """OKX UTC 固定周期 K 线的真实结束时刻。"""

        if not bar.closed:
            raise ValueError("只能查询已收盘 K 线的结束时间")
        duration_s = timeframe_to_seconds(timeframe)
        if duration_s is None:
            raise ValueError(f"OKX 无法确定 {timeframe} 的 K 线结束时间")
        return int(bar.ts_open) + duration_s * 1_000

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
        except OkxPublicError as exc:
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
        try:
            price_tick = Decimal(str(instrument.get("tickSz") or ""))
        except InvalidOperation as exc:
            raise ValueError(f"OKX 品种 {normalized} 的 tickSz 无效") from exc
        if not price_tick.is_finite() or price_tick <= 0:
            raise ValueError(f"OKX 品种 {normalized} 的 tickSz 无效")

        # 在线验证全部通过后再切换，失败不会破坏旧订阅。
        self._symbol = normalized
        self._timeframe = timeframe
        self._price_tick = format(price_tick, "f")

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        self._price_tick = None

    def price_tick(self) -> str | None:
        """返回 OKX 公共品种元数据声明的真实最小跳动。"""
        return self._price_tick

    def is_symbol_available(self, symbol: str) -> bool:
        try:
            normalize_okx_instrument(symbol)
        except ValueError:
            return False
        return True

    def batch_quote_snapshots(
        self,
        token: WatchlistRequestToken,
        *,
        received_at_utc_ms: int | None = None,
    ) -> tuple[QuoteSnapshot, ...]:
        """按产品类型批量读取报价；不改变当前主图订阅。"""
        if not self._connected:
            raise DataSourceTransientError("OKX 公共行情源尚未连接")
        if token.market != "Crypto" or token.source != "okx":
            raise ValueError("OKX 批量报价只接受 Crypto/okx 批次")
        requested_by_type: dict[str, dict[str, str]] = {}
        request_order = list(token.symbols)
        for symbol_value in token.symbols:
            symbol = normalize_okx_instrument(symbol_value)
            bucket = requested_by_type.setdefault(okx_instrument_type(symbol), {})
            if symbol in bucket:
                raise ValueError(f"批量报价包含重复品种：{symbol}")
            bucket[symbol] = symbol

        snapshots_by_symbol: dict[str, QuoteSnapshot] = {}
        try:
            for instrument_type, requested in requested_by_type.items():
                ticker_rows = self._client.tickers(instrument_type)
                instrument_rows = self._client.public_instruments(instrument_type)
                response_received_at_utc_ms = (
                    int(received_at_utc_ms)
                    if received_at_utc_ms is not None
                    else int(time.time() * 1000)
                )
                tick_by_symbol = {
                    str(row.get("instId") or "").strip().upper(): row.get("tickSz")
                    for row in instrument_rows
                    if str(row.get("state") or "").strip().lower() == "live"
                }
                ticker_by_symbol = {
                    str(row.get("instId") or "").strip().upper(): row
                    for row in ticker_rows
                }
                for symbol in requested:
                    row = ticker_by_symbol.get(symbol)
                    price_tick = tick_by_symbol.get(symbol)
                    if row is None or price_tick in {None, ""}:
                        raise DataSourceTransientError(
                            f"OKX 批量报价缺少 {symbol} 的报价或真实 tickSz"
                        )
                    parts = symbol.split("-")
                    quote_currency = parts[-2] if parts[-1] == "SWAP" else parts[-1]
                    snapshots_by_symbol[symbol] = QuoteSnapshot.from_prices(
                        selection_generation=token.selection_generation,
                        request_sequence=token.watchlist_refresh_sequence,
                        symbol=symbol,
                        market="Crypto",
                        source="okx",
                        name=symbol,
                        currency=quote_currency,
                        last=row.get("last"),
                        # OKX tickers 没有“上一交易日收盘价”字段；
                        # 禁止用 24 小时开盘价伪装。
                        prev_close=None,
                        price_tick=price_tick,
                        quote_ts_utc_ms=int(str(row.get("ts") or "")),
                        received_at_utc_ms=response_received_at_utc_ms,
                    )
        except OkxPublicError as exc:
            raise DataSourceTransientError(f"OKX 批量报价暂时不可用：{exc}") from exc
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DataSourceTransientError("OKX 批量报价字段无法解析") from exc
        return tuple(
            snapshots_by_symbol[symbol]
            for symbol in request_order
        )

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("OKX 公共行情源尚未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("OKX 尚未订阅品种和周期")
        return self._latest_snapshot_for_timeframe(self._timeframe, n)

    def latest_snapshot_for_timeframe(
        self,
        timeframe: str,
        n: int,
        *,
        analysis_as_of_utc_ms: int | None = None,
    ) -> list[KlineBar]:
        """读取同一已订阅 OKX 品种的另一个周期，不改变主图订阅。"""
        if not self._connected:
            raise DataSourceTransientError("OKX 公共行情源尚未连接")
        if not self._symbol:
            raise DataSourceTransientError("OKX 尚未订阅品种")
        if timeframe not in _TIMEFRAME_TO_OKX_BAR:
            raise ValueError(
                f"OKX 不支持周期 {timeframe!r}；可用周期：{list(_TIMEFRAME_TO_OKX_BAR)}"
            )
        return self._latest_snapshot_for_timeframe(
            timeframe,
            n,
            analysis_as_of_utc_ms=analysis_as_of_utc_ms,
        )

    def _latest_snapshot_for_timeframe(
        self,
        timeframe: str,
        n: int,
        *,
        analysis_as_of_utc_ms: int | None = None,
    ) -> list[KlineBar]:
        if n < 1:
            return []
        max_requested = (
            _MAX_TEN_MINUTE_BARS if timeframe == "10m" else _MAX_CANDLES
        )
        if n > max_requested:
            raise DataSourceTransientError(
                f"OKX {timeframe} 单次最多返回 {max_requested} 根；"
                "10m 由真实 5m 分页后两两聚合"
            )
        raw_limit = n * 2 + 2 if timeframe == "10m" else n
        try:
            rows = self._fetch_candle_rows(
                timeframe=timeframe,
                required_rows=raw_limit,
            )
        except OkxPublicError as exc:
            raise DataSourceTransientError(f"OKX K 线暂时不可用：{exc}") from exc
        if not rows:
            raise DataSourceTransientError(
                f"OKX 未返回 K 线：{self._symbol} {timeframe}"
            )
        if timeframe == "10m":
            rows = aggregate_okx_five_minute_rows(rows, limit=n)
        if analysis_as_of_utc_ms is not None:
            if analysis_as_of_utc_ms < 0:
                raise ValueError("analysis_as_of_utc_ms 不能为负数")
            interval_ms = timeframe_to_seconds(timeframe) * 1_000
            frozen_rows: list[list[str]] = []
            for row in rows:
                try:
                    timestamp = int(row[0])
                except (IndexError, TypeError, ValueError) as exc:
                    raise DataSourceTransientError(
                        "OKX K 线统一截止时间字段无法解析"
                    ) from exc
                if timestamp > analysis_as_of_utc_ms:
                    continue
                frozen = list(row)
                frozen[8] = (
                    "1"
                    if timestamp + interval_ms
                    <= analysis_as_of_utc_ms
                    else "0"
                )
                frozen_rows.append(frozen)
            rows = frozen_rows
            if not rows:
                raise DataSourceTransientError(
                    f"OKX {self._symbol} {timeframe} 在统一分析截止时间前没有 K 线"
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
                    price_tick=self._price_tick,
                )
            )
        return bars

    def _fetch_candle_rows(
        self,
        *,
        timeframe: str,
        required_rows: int,
    ) -> list[list[str]]:
        """分页读取并按时间去重；冲突重复行和游标不前进都会失败关闭。"""
        rows_by_timestamp: dict[int, list[str]] = {}
        after: str | None = None
        oldest_timestamp: int | None = None
        max_pages = _MAX_CANDLE_PAGES if timeframe == "10m" else 1
        for _ in range(max_pages):
            remaining = required_rows - len(rows_by_timestamp)
            if remaining <= 0:
                break
            page = self._client.candles(
                instrument=self._symbol,
                bar=_TIMEFRAME_TO_OKX_BAR[timeframe],
                limit=min(_MAX_CANDLES, remaining),
                after=after,
            )
            if not page:
                break
            page_oldest: int | None = None
            for row in page:
                try:
                    if len(row) < 9:
                        raise ValueError("missing fields")
                    timestamp = int(row[0])
                except (IndexError, TypeError, ValueError) as exc:
                    raise DataSourceTransientError(
                        "OKX K 线分页结果的时间字段无法解析"
                    ) from exc
                existing = rows_by_timestamp.get(timestamp)
                if existing is not None and existing != row:
                    raise DataSourceTransientError(
                        "OKX K 线分页返回同时间戳但内容冲突的数据"
                    )
                rows_by_timestamp[timestamp] = row
                page_oldest = (
                    timestamp
                    if page_oldest is None
                    else min(page_oldest, timestamp)
                )
            if page_oldest is None:
                break
            if oldest_timestamp is not None and page_oldest >= oldest_timestamp:
                raise DataSourceTransientError("OKX K 线分页游标没有向更早时间推进")
            oldest_timestamp = page_oldest
            after = str(page_oldest)
        if len(rows_by_timestamp) < required_rows:
            raise DataSourceTransientError(
                f"OKX K 线分页不足：需要 {required_rows} 行，"
                f"实际 {len(rows_by_timestamp)} 行"
            )
        return [
            rows_by_timestamp[timestamp]
            for timestamp in sorted(rows_by_timestamp, reverse=True)
        ]
