"""成交量影子摘要。

本模块只计算和记录可审计的成交量事实，不把成交量接入任何模型提示词。
"""
from __future__ import annotations

import csv
import json
import math
import os
import time
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from statistics import fmean, median
from urllib.parse import quote

from filelock import FileLock, Timeout

from pa_agent.config.paths import PROJECT_ROOT
from pa_agent.data.bar_close_wait import timeframe_to_seconds
from pa_agent.data.base import KlineFrame
from pa_agent.data.datetime_ts import ts_open_to_ms
from pa_agent.data.market_calendar import MarketCalendarError, is_trading_minute

_BASELINE_WINDOW = 20
_MIN_CLOSED_BARS = 6
_EXPANDING_THRESHOLD = 1.5
_CONTRACTING_THRESHOLD = 0.7
_MIN_SCORE_SAMPLES = 10
_OUTPUT_DIR_ENV = "PA_AGENT_VOLUME_SHADOW_DIR"
_OUTPUT_LOCK_TIMEOUT_SECONDS = 2.0
_REQUIRED_CSV_COLUMNS = {
    "symbol",
    "timeframe",
    "ts_open",
    "open",
    "high",
    "low",
    "close",
    "volume",
}


def _finite_non_negative(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def summarize_volume(frame: KlineFrame) -> dict[str, object] | None:
    """汇总最新已收盘 K 线及其最近成交量背景。

    形成中的 K 线不会参与计算。若实际参与计算的成交量不可信、已收盘
    K 线不足六根、基准中位数为零，返回 ``None``，不猜测或填零。
    """
    bars = tuple(bar for bar in getattr(frame, "bars", ()) if bar.closed)
    if len(bars) < _MIN_CLOSED_BARS:
        return None

    latest = bars[0]
    baseline_bars = bars[1 : _BASELINE_WINDOW + 1]
    latest_volume = _finite_non_negative(latest.volume)
    baseline_values = [_finite_non_negative(bar.volume) for bar in baseline_bars]
    if latest_volume is None or any(value is None for value in baseline_values):
        return None

    baseline_volume = float(median(baseline_values))
    if not math.isfinite(baseline_volume) or baseline_volume == 0:
        return None
    try:
        raw_relative_volume = latest_volume / baseline_volume
    except (OverflowError, ZeroDivisionError):
        return None
    if not math.isfinite(raw_relative_volume):
        return None
    relative_volume = round(raw_relative_volume, 4)

    if relative_volume >= _EXPANDING_THRESHOLD:
        state = "expanding"
    elif relative_volume <= _CONTRACTING_THRESHOLD:
        state = "contracting"
    else:
        state = "normal"

    try:
        bar_open_utc_ms = int(ts_open_to_ms(latest.ts_open))
    except (TypeError, ValueError, OverflowError):
        return None
    if bar_open_utc_ms <= 0:
        return None

    return {
        "symbol": str(frame.symbol),
        "timeframe": str(frame.timeframe),
        "bar_open_utc_ms": bar_open_utc_ms,
        "latest_volume": latest_volume,
        "baseline_volume": baseline_volume,
        "relative_volume": relative_volume,
        "state": state,
    }


def _truncate_incomplete_jsonl_tail(handle: int) -> None:
    """丢弃上次进程中断留下的半行，保留最后一条完整 JSONL 记录。"""
    size = os.fstat(handle).st_size
    if size == 0:
        return
    os.lseek(handle, -1, os.SEEK_END)
    if os.read(handle, 1) == b"\n":
        return

    cursor = size
    complete_size = 0
    while cursor > 0:
        block_size = min(8192, cursor)
        cursor -= block_size
        os.lseek(handle, cursor, os.SEEK_SET)
        block = os.read(handle, block_size)
        newline_at = block.rfind(b"\n")
        if newline_at >= 0:
            complete_size = cursor + newline_at + 1
            break
    os.ftruncate(handle, complete_size)
    os.fsync(handle)


def _append_jsonl_line(path: Path, line: str) -> None:
    """跨线程、跨进程串行追加一条完整 JSONL，并在普通写错时回滚半行。"""
    lock = FileLock(f"{path}.lock", timeout=_OUTPUT_LOCK_TIMEOUT_SECONDS)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        with lock:
            handle = os.open(path, flags, 0o600)
            try:
                _truncate_incomplete_jsonl_tail(handle)
                original_size = os.fstat(handle).st_size
                payload = memoryview(f"{line}\n".encode())
                try:
                    while payload:
                        written = os.write(handle, payload)
                        if written <= 0:
                            raise OSError("成交量影子 JSONL 未写入任何字节")
                        payload = payload[written:]
                    os.fsync(handle)
                except Exception:
                    os.ftruncate(handle, original_size)
                    os.fsync(handle)
                    raise
            finally:
                os.close(handle)
    except Timeout as exc:
        raise OSError("成交量影子 JSONL 写锁超时") from exc


def record_volume_shadow(
    frame: KlineFrame,
    *,
    out_dir: str | Path | None = None,
) -> Path | None:
    """把一条有效摘要追加为 UTF-8 JSONL；无有效摘要时不创建文件。

    生产默认写入 ``scratch/volume_shadow``。测试或离线工具可通过显式
    ``out_dir`` 或 ``PA_AGENT_VOLUME_SHADOW_DIR`` 把影子记录隔离到临时目录。
    """
    summary = summarize_volume(frame)
    if summary is None:
        return None

    configured_dir = out_dir
    if configured_dir is None:
        configured_dir = os.environ.get(_OUTPUT_DIR_ENV)
    directory = Path(
        configured_dir
        if configured_dir is not None
        else PROJECT_ROOT / "scratch" / "volume_shadow"
    ).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    symbol = quote(str(summary["symbol"]), safe="._-")
    timeframe = quote(str(summary["timeframe"]), safe="._-")
    path = directory / f"{symbol}_{timeframe}.jsonl"
    if path.is_symlink():
        raise ValueError("成交量影子输出文件不能是符号链接")
    line = json.dumps(
        summary,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
    )
    _append_jsonl_line(path, line)
    return path


@dataclass(frozen=True)
class VolumeShadowScore:
    """一次离线影子评分的完整审计结果。"""

    symbol: str
    timeframe: str
    lookahead: int
    input_records: int
    duplicate_records: int
    normal_records: int
    unmatched_records: int
    insufficient_future_records: int
    expanding_ranges_pct: tuple[float, ...]
    contracting_ranges_pct: tuple[float, ...]

    @property
    def scored_records(self) -> int:
        return len(self.expanding_ranges_pct) + len(self.contracting_ranges_pct)

    @property
    def enough_samples(self) -> bool:
        return (
            self.scored_records >= _MIN_SCORE_SAMPLES
            and bool(self.expanding_ranges_pct)
            and bool(self.contracting_ranges_pct)
        )

    def mean_range_pct(self, state: str) -> float:
        if state == "expanding":
            values = self.expanding_ranges_pct
        elif state == "contracting":
            values = self.contracting_ranges_pct
        else:
            raise ValueError(f"不支持的成交量状态：{state}")
        return fmean(values) if values else 0.0


def format_volume_shadow_score(score: VolumeShadowScore) -> tuple[str, ...]:
    """把描述性评分渲染为稳定、可测试的命令行文本。"""
    if score.unmatched_records:
        raise ValueError("存在未匹配影子记录，不能输出评分结论")

    lines = [
        (
            "记录审计: "
            f"输入={score.input_records} "
            f"重复={score.duplicate_records} "
            f"正常量={score.normal_records} "
            f"未匹配={score.unmatched_records} "
            f"后续不足={score.insufficient_future_records} "
            f"已评分={score.scored_records}"
        ),
        (
            f"放量组: 样本数={len(score.expanding_ranges_pct)} "
            f"平均后续{score.lookahead}根相对振幅="
            f"{score.mean_range_pct('expanding'):.4f}%"
        ),
        (
            f"缩量组: 样本数={len(score.contracting_ranges_pct)} "
            f"平均后续{score.lookahead}根相对振幅="
            f"{score.mean_range_pct('contracting'):.4f}%"
        ),
    ]
    if not score.enough_samples:
        lines.append("样本不足，暂不给结论")
        return tuple(lines)

    expanding_mean = score.mean_range_pct("expanding")
    contracting_mean = score.mean_range_pct("contracting")
    larger_group = "放量组" if expanding_mean > contracting_mean else "缩量组"
    if expanding_mean == contracting_mean:
        larger_group = "两组相同"
    lines.append(
        f"描述性结果：{larger_group}平均波动更大；尚未做统计显著性检验"
    )
    return tuple(lines)


@dataclass(frozen=True)
class _CsvBar:
    ts_open_ms: int
    high: float
    low: float
    close: float


def _stock_market_from_symbol(symbol: str) -> str | None:
    suffix = symbol.rsplit(".", 1)[-1].upper()
    return suffix if suffix in {"US", "HK", "SH", "SZ"} else None


def _require_trading_minute(market: str, timestamp: int) -> bool:
    try:
        return is_trading_minute(market, timestamp)
    except (MarketCalendarError, ValueError) as exc:
        raise ValueError(
            f"无法校验 {market} 市场 K 线时间：{timestamp}"
        ) from exc


def _validate_score_bar_spacing(
    bars: list[_CsvBar],
    *,
    symbol: str,
    duration_ms: int,
) -> None:
    market = _stock_market_from_symbol(symbol)
    if market is not None:
        for bar in bars:
            if not _require_trading_minute(market, bar.ts_open_ms):
                raise ValueError(
                    f"CSV 的 K 线不在 {market} 连续交易时段：{bar.ts_open_ms}"
                )

    for previous, current in pairwise(bars):
        delta = current.ts_open_ms - previous.ts_open_ms
        if delta < duration_ms:
            raise ValueError("CSV 相邻 K 线间隔小于指定周期")
        if delta == duration_ms:
            continue
        if market is None:
            raise ValueError("连续市场 CSV 存在缺失或错误周期 K 线")
        for candidate in range(
            previous.ts_open_ms + duration_ms,
            current.ts_open_ms,
            duration_ms,
        ):
            if _require_trading_minute(market, candidate):
                raise ValueError(f"CSV 缺少应存在的 K 线：{candidate}")


def _csv_number(value: object, *, field: str, line_number: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"CSV 第 {line_number} 行的 {field} 不是有效数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"CSV 第 {line_number} 行的 {field} 不是有限数字")
    return number


def _csv_timestamp_ms(value: object, *, line_number: int) -> int:
    timestamp = _csv_number(value, field="ts_open", line_number=line_number)
    if timestamp <= 0:
        raise ValueError(f"CSV 第 {line_number} 行的 ts_open 必须大于零")
    if timestamp < 10_000_000_000:
        timestamp *= 1000
    integer_timestamp = int(timestamp)
    if timestamp != integer_timestamp:
        raise ValueError(f"CSV 第 {line_number} 行的 ts_open 不能含小数毫秒")
    return integer_timestamp


def _json_number(
    value: dict[str, object],
    *,
    field: str,
    line_number: int,
) -> float:
    raw = value.get(field)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"JSONL 第 {line_number} 行的 {field} 必须是数字")
    number = float(raw)
    if not math.isfinite(number):
        raise ValueError(f"JSONL 第 {line_number} 行的 {field} 不是有限数字")
    return number


def _expected_state(relative_volume: float) -> str:
    if relative_volume >= _EXPANDING_THRESHOLD:
        return "expanding"
    if relative_volume <= _CONTRACTING_THRESHOLD:
        return "contracting"
    return "normal"


def _load_shadow_records(
    path: Path,
    *,
    symbol: str,
    timeframe: str,
) -> tuple[list[dict[str, object]], int, int]:
    records_by_timestamp: dict[int, dict[str, object]] = {}
    input_records = 0
    duplicate_records = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            input_records += 1
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"JSONL 第 {line_number} 行不是有效 JSON"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL 第 {line_number} 行必须是 JSON 对象")
            if value.get("symbol") != symbol or value.get("timeframe") != timeframe:
                raise ValueError(
                    f"JSONL 第 {line_number} 行与指定标的或周期不一致"
                )

            timestamp = value.get("bar_open_utc_ms")
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or timestamp <= 0
            ):
                raise ValueError(
                    f"JSONL 第 {line_number} 行的 bar_open_utc_ms 必须是正整数"
                )
            latest = _json_number(
                value,
                field="latest_volume",
                line_number=line_number,
            )
            baseline = _json_number(
                value,
                field="baseline_volume",
                line_number=line_number,
            )
            relative = _json_number(
                value,
                field="relative_volume",
                line_number=line_number,
            )
            if latest < 0 or baseline <= 0 or relative < 0:
                raise ValueError(f"JSONL 第 {line_number} 行的成交量字段越界")
            expected_relative = round(latest / baseline, 4)
            if not math.isfinite(expected_relative) or relative != expected_relative:
                raise ValueError(
                    f"JSONL 第 {line_number} 行的 relative_volume 与原始值不一致"
                )
            state = value.get("state")
            if state != _expected_state(relative):
                raise ValueError(
                    f"JSONL 第 {line_number} 行的 state 与 relative_volume 不一致"
                )

            canonical = {
                "symbol": symbol,
                "timeframe": timeframe,
                "bar_open_utc_ms": timestamp,
                "latest_volume": latest,
                "baseline_volume": baseline,
                "relative_volume": relative,
                "state": state,
            }
            previous = records_by_timestamp.get(timestamp)
            if previous is not None:
                if previous != canonical:
                    raise ValueError(f"同一 K 线存在冲突影子记录：{timestamp}")
                duplicate_records += 1
                continue
            records_by_timestamp[timestamp] = canonical
    return list(records_by_timestamp.values()), input_records, duplicate_records


def _load_score_bars(
    path: Path,
    *,
    symbol: str,
    timeframe: str,
    now_utc_ms: int,
) -> list[_CsvBar]:
    duration_seconds = timeframe_to_seconds(timeframe)
    if duration_seconds is None or duration_seconds <= 0:
        raise ValueError(f"不支持的周期：{timeframe}")
    duration_ms = duration_seconds * 1000

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or ())
        missing = _REQUIRED_CSV_COLUMNS.difference(fieldnames)
        if missing:
            raise ValueError(f"CSV 缺少列：{', '.join(sorted(missing))}")

        bars: list[_CsvBar] = []
        seen_timestamps: set[int] = set()
        for line_number, row in enumerate(reader, start=2):
            if row.get("symbol") != symbol:
                raise ValueError(f"CSV 第 {line_number} 行的 symbol 与指定标的不一致")
            if row.get("timeframe") != timeframe:
                raise ValueError(f"CSV 第 {line_number} 行的 timeframe 与指定周期不一致")
            if "closed" in fieldnames:
                closed = str(row.get("closed") or "").strip().lower()
                if closed not in {"1", "true"}:
                    raise ValueError(f"CSV 第 {line_number} 行不是已收盘 K 线")

            timestamp = _csv_timestamp_ms(
                row.get("ts_open"),
                line_number=line_number,
            )
            if timestamp in seen_timestamps:
                raise ValueError(f"CSV 出现重复 ts_open：{timestamp}")
            if timestamp + duration_ms > now_utc_ms:
                raise ValueError(f"CSV 第 {line_number} 行尚未收盘")
            seen_timestamps.add(timestamp)

            open_price = _csv_number(
                row.get("open"),
                field="open",
                line_number=line_number,
            )
            high = _csv_number(
                row.get("high"),
                field="high",
                line_number=line_number,
            )
            low = _csv_number(
                row.get("low"),
                field="low",
                line_number=line_number,
            )
            close = _csv_number(
                row.get("close"),
                field="close",
                line_number=line_number,
            )
            volume = _csv_number(
                row.get("volume"),
                field="volume",
                line_number=line_number,
            )
            if min(open_price, high, low, close) <= 0:
                raise ValueError(f"CSV 第 {line_number} 行的 OHLC 必须大于零")
            if volume < 0:
                raise ValueError(f"CSV 第 {line_number} 行的 volume 不能为负数")
            if high < low:
                raise ValueError(f"CSV 第 {line_number} 行的 high 不能小于 low")
            if not low <= open_price <= high or not low <= close <= high:
                raise ValueError(f"CSV 第 {line_number} 行的 open/close 超出 high-low")

            bars.append(
                _CsvBar(
                    ts_open_ms=timestamp,
                    high=high,
                    low=low,
                    close=close,
                )
            )
    bars.sort(key=lambda bar: bar.ts_open_ms)
    _validate_score_bar_spacing(
        bars,
        symbol=symbol,
        duration_ms=duration_ms,
    )
    return bars


def score_volume_shadow_files(
    shadow_jsonl: str | Path,
    kline_csv: str | Path,
    *,
    symbol: str,
    timeframe: str,
    lookahead: int = 3,
    now_utc_ms: int | None = None,
) -> VolumeShadowScore:
    """用同标的、同周期的本地已收盘 K 线做描述性后验评分。"""
    symbol = symbol.strip()
    timeframe = timeframe.strip()
    if not symbol or not timeframe:
        raise ValueError("symbol 和 timeframe 不能为空")
    if lookahead <= 0:
        raise ValueError("lookahead 必须是正整数")
    evaluation_time = (
        int(time.time() * 1000) if now_utc_ms is None else int(now_utc_ms)
    )
    if evaluation_time <= 0:
        raise ValueError("now_utc_ms 必须是正整数")

    records, input_records, duplicate_records = _load_shadow_records(
        Path(shadow_jsonl),
        symbol=symbol,
        timeframe=timeframe,
    )
    bars = _load_score_bars(
        Path(kline_csv),
        symbol=symbol,
        timeframe=timeframe,
        now_utc_ms=evaluation_time,
    )
    index_by_timestamp = {
        bar.ts_open_ms: index for index, bar in enumerate(bars)
    }
    grouped: dict[str, list[float]] = {
        "expanding": [],
        "contracting": [],
    }
    normal_records = 0
    unmatched_records = 0
    insufficient_future_records = 0
    for record in records:
        state = str(record["state"])
        index = index_by_timestamp.get(int(record["bar_open_utc_ms"]))
        if index is None:
            unmatched_records += 1
            continue
        if state == "normal":
            normal_records += 1
            continue
        future = bars[index + 1 : index + 1 + lookahead]
        if len(future) != lookahead:
            insufficient_future_records += 1
            continue
        reference_close = bars[index].close
        future_range_pct = (
            max(bar.high for bar in future) - min(bar.low for bar in future)
        ) / reference_close * 100
        if not math.isfinite(future_range_pct) or future_range_pct < 0:
            raise ValueError("派生的后续相对振幅无效")
        grouped[state].append(future_range_pct)

    score = VolumeShadowScore(
        symbol=symbol,
        timeframe=timeframe,
        lookahead=lookahead,
        input_records=input_records,
        duplicate_records=duplicate_records,
        normal_records=normal_records,
        unmatched_records=unmatched_records,
        insufficient_future_records=insufficient_future_records,
        expanding_ranges_pct=tuple(grouped["expanding"]),
        contracting_ranges_pct=tuple(grouped["contracting"]),
    )
    if score.unmatched_records:
        raise ValueError(
            "记录审计失败："
            f"输入={score.input_records}，"
            f"重复={score.duplicate_records}，"
            f"正常量={score.normal_records}，"
            f"未匹配={score.unmatched_records}，"
            f"后续不足={score.insufficient_future_records}；"
            "影子记录在 CSV 中没有同时间戳 K 线"
        )
    return score
