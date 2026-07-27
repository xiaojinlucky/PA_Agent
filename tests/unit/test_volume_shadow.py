"""成交量影子摘要测试。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.data.volume_shadow import (
    VolumeShadowScore,
    format_volume_shadow_score,
    record_volume_shadow,
    score_volume_shadow_files,
    summarize_volume,
)

_SCORE_NOW_MS = 1_800_000_000_000
_SCORE_BASE_MS = 1_749_130_200_000


def _frame(
    volumes: list[object],
    *,
    symbol: str = "AAPL.US",
    timeframe: str = "10m",
    forming_volume: object | None = None,
) -> KlineFrame:
    bars: list[KlineBar] = []
    if forming_volume is not None:
        bars.append(
            KlineBar(
                seq=0,
                ts_open=1_700_000_600_000.0,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=cast(float, forming_volume),
                closed=False,
            )
        )
    for index, volume in enumerate(volumes):
        bars.append(
            KlineBar(
                seq=index + 1,
                ts_open=float(1_700_000_000_000 - index * 600_000),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=cast(float, volume),
                closed=True,
            )
        )
    count = len(bars)
    return KlineFrame(
        symbol=symbol,
        timeframe=timeframe,
        bars=tuple(bars),
        indicators=IndicatorBundle(
            ema20=tuple(float("nan") for _ in range(count)),
            atr14=tuple(float("nan") for _ in range(count)),
        ),
        snapshot_ts_local_ms=1_700_000_700_000,
    )


def _shadow_record(
    timestamp: int,
    *,
    state: str,
    symbol: str = "AAPL.US",
    timeframe: str = "10m",
) -> dict[str, object]:
    latest_by_state = {
        "expanding": 150.0,
        "contracting": 50.0,
        "normal": 100.0,
    }
    relative_by_state = {
        "expanding": 1.5,
        "contracting": 0.5,
        "normal": 1.0,
    }
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "bar_open_utc_ms": timestamp,
        "latest_volume": latest_by_state[state],
        "baseline_volume": 100.0,
        "relative_volume": relative_by_state[state],
        "state": state,
    }


def _write_shadow(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )


def _write_score_csv(
    path: Path,
    timestamps: list[int],
    *,
    row_overrides: dict[int, dict[str, object]] | None = None,
    include_scope: bool = True,
    include_closed: bool = True,
    symbol: str = "AAPL.US",
    timeframe: str = "10m",
) -> None:
    extra_columns = []
    if include_scope:
        extra_columns.extend(["symbol", "timeframe"])
    if include_closed:
        extra_columns.append("closed")
    columns = [
        "ts_open",
        "open",
        "high",
        "low",
        "close",
        "volume",
        *extra_columns,
    ]
    lines = [",".join(columns)]
    for index, timestamp in enumerate(timestamps):
        row: dict[str, object] = {
            "ts_open": timestamp,
            "open": 100.0,
            "high": 101.0 + index,
            "low": 99.0 - index,
            "close": 100.0,
            "volume": 100.0,
            "symbol": symbol,
            "timeframe": timeframe,
            "closed": "true",
        }
        if row_overrides and index in row_overrides:
            row.update(row_overrides[index])
        lines.append(",".join(str(row[column]) for column in columns))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("latest", "expected_state", "expected_relative"),
    [
        (150.0, "expanding", 1.5),
        (70.0, "contracting", 0.7),
        (100.0, "normal", 1.0),
    ],
)
def test_summarize_volume_classifies_state(
    latest: float,
    expected_state: str,
    expected_relative: float,
) -> None:
    summary = summarize_volume(_frame([latest, *([100.0] * 20)]))

    assert summary is not None
    assert summary == {
        "symbol": "AAPL.US",
        "timeframe": "10m",
        "bar_open_utc_ms": 1_700_000_000_000,
        "latest_volume": latest,
        "baseline_volume": 100.0,
        "relative_volume": expected_relative,
        "state": expected_state,
    }


def test_summarize_volume_uses_latest_closed_bar_and_rounds_ratio() -> None:
    summary = summarize_volume(
        _frame([123.456, *([100.0] * 20)], forming_volume=10_000.0)
    )

    assert summary is not None
    assert summary["latest_volume"] == 123.456
    assert summary["relative_volume"] == 1.2346
    assert summary["bar_open_utc_ms"] == 1_700_000_000_000


def test_summarize_volume_classifies_from_persisted_rounded_ratio() -> None:
    summary = summarize_volume(_frame([149.999, *([100.0] * 20)]))

    assert summary is not None
    assert summary["relative_volume"] == 1.5
    assert summary["state"] == "expanding"


@pytest.mark.parametrize(
    "invalid_volume",
    [None, float("nan"), float("inf"), -1.0],
)
def test_summarize_volume_rejects_invalid_participating_volume(
    invalid_volume: object,
) -> None:
    assert (
        summarize_volume(_frame([100.0, invalid_volume, *([100.0] * 19)]))
        is None
    )


def test_summarize_volume_rejects_insufficient_closed_bars() -> None:
    assert summarize_volume(_frame([100.0] * 5)) is None


def test_summarize_volume_rejects_zero_baseline() -> None:
    assert summarize_volume(_frame([100.0, *([0.0] * 5)])) is None


def test_summarize_volume_accepts_zero_latest_volume() -> None:
    summary = summarize_volume(_frame([0.0, *([100.0] * 5)]))

    assert summary is not None
    assert summary["relative_volume"] == 0.0
    assert summary["state"] == "contracting"


def test_summarize_volume_rejects_non_finite_ratio() -> None:
    assert summarize_volume(_frame([1e308, *([5e-324] * 5)])) is None


def test_summarize_volume_rejects_median_overflow() -> None:
    assert summarize_volume(_frame([1.0, *([1e308] * 6)])) is None


def test_summarize_volume_ignores_values_older_than_baseline_window() -> None:
    summary = summarize_volume(_frame([100.0, *([100.0] * 20), float("nan")]))

    assert summary is not None
    assert summary["baseline_volume"] == 100.0


def test_record_volume_shadow_appends_readable_json_lines(tmp_path) -> None:
    frame = _frame([150.0, *([100.0] * 20)])

    first_path = record_volume_shadow(frame, out_dir=tmp_path)
    second_path = record_volume_shadow(frame, out_dir=tmp_path)

    assert first_path == tmp_path / "AAPL.US_10m.jsonl"
    assert second_path == first_path
    lines = first_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["state"] == "expanding"
    assert json.loads(lines[1]) == json.loads(lines[0])


def test_record_volume_shadow_does_not_create_output_for_invalid_frame(
    tmp_path,
) -> None:
    out_dir = tmp_path / "volume_shadow"

    assert record_volume_shadow(_frame([100.0] * 5), out_dir=out_dir) is None
    assert not out_dir.exists()


def test_record_volume_shadow_encodes_unsafe_filename_characters(tmp_path) -> None:
    path = record_volume_shadow(
        _frame(
            [150.0, *([100.0] * 20)],
            symbol="../BINANCE:BTC/USDT",
        ),
        out_dir=tmp_path,
    )

    assert path is not None
    assert path.parent == tmp_path
    assert path.name == "..%2FBINANCE%3ABTC%2FUSDT_10m.jsonl"


def test_record_volume_shadow_rejects_symlink_output_file(
    tmp_path,
    monkeypatch,
) -> None:
    out_dir = tmp_path / "volume_shadow"
    out_dir.mkdir()
    target = (out_dir / "AAPL.US_10m.jsonl").resolve()
    original_is_symlink = Path.is_symlink

    def _is_symlink(path: Path) -> bool:
        return path == target or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", _is_symlink)

    with pytest.raises(ValueError, match="不能是符号链接"):
        record_volume_shadow(
            _frame([150.0, *([100.0] * 20)]),
            out_dir=out_dir,
        )
    assert not target.exists()


def test_format_volume_shadow_score_withholds_small_sample_conclusion() -> None:
    score = VolumeShadowScore(
        symbol="AAPL.US",
        timeframe="10m",
        lookahead=3,
        input_records=9,
        duplicate_records=0,
        normal_records=0,
        unmatched_records=0,
        insufficient_future_records=0,
        expanding_ranges_pct=(1.0, 2.0, 3.0, 4.0, 5.0),
        contracting_ranges_pct=(1.0, 2.0, 3.0, 4.0),
    )

    lines = format_volume_shadow_score(score)

    assert lines[-1] == "样本不足，暂不给结论"
    assert not any(line.startswith("描述性结果") for line in lines)


def test_format_volume_shadow_score_keeps_conclusion_descriptive() -> None:
    score = VolumeShadowScore(
        symbol="AAPL.US",
        timeframe="10m",
        lookahead=3,
        input_records=10,
        duplicate_records=0,
        normal_records=0,
        unmatched_records=0,
        insufficient_future_records=0,
        expanding_ranges_pct=(2.0, 2.0, 2.0, 2.0, 2.0),
        contracting_ranges_pct=(1.0, 1.0, 1.0, 1.0, 1.0),
    )

    lines = format_volume_shadow_score(score)

    assert lines[-1] == (
        "描述性结果：放量组平均波动更大；尚未做统计显著性检验"
    )


def test_score_volume_shadow_uses_strict_future_bars_and_audits_counts(
    tmp_path,
) -> None:
    timestamps = [_SCORE_BASE_MS + index * 600_000 for index in range(15)]
    records = [
        _shadow_record(
            timestamp,
            state="expanding" if index % 2 == 0 else "contracting",
        )
        for index, timestamp in enumerate(timestamps[:10])
    ]
    records.append(dict(records[0]))
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(shadow_path, records)
    _write_score_csv(
        csv_path,
        timestamps,
        row_overrides={
            0: {"high": 150.0, "low": 50.0},
            1: {"high": 102.0, "low": 98.0},
            2: {"high": 102.0, "low": 98.0},
            3: {"high": 102.0, "low": 98.0},
        },
    )

    score = score_volume_shadow_files(
        shadow_path,
        csv_path,
        symbol="AAPL.US",
        timeframe="10m",
        lookahead=3,
        now_utc_ms=_SCORE_NOW_MS,
    )

    assert score.input_records == 11
    assert score.duplicate_records == 1
    assert score.unmatched_records == 0
    assert score.insufficient_future_records == 0
    assert score.scored_records == 10
    assert score.enough_samples
    assert len(score.expanding_ranges_pct) == 5
    assert len(score.contracting_ranges_pct) == 5
    assert score.expanding_ranges_pct[0] == 4.0


def test_score_volume_shadow_reports_unmatched_and_insufficient_future(
    tmp_path,
) -> None:
    timestamps = [_SCORE_BASE_MS + index * 600_000 for index in range(4)]
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [
            _shadow_record(_SCORE_BASE_MS - 600_000, state="expanding"),
            _shadow_record(timestamps[-1], state="contracting"),
            _shadow_record(timestamps[0], state="normal"),
        ],
    )
    _write_score_csv(csv_path, timestamps)

    with pytest.raises(ValueError, match="未匹配=1，后续不足=1"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            lookahead=3,
            now_utc_ms=_SCORE_NOW_MS,
        )


@pytest.mark.parametrize(
    "row_override",
    [
        {"high": 90.0, "low": 110.0},
        {"open": float("nan")},
        {"high": float("inf")},
        {"open": 0.0},
        {"low": 0.0},
        {"open": 102.0},
        {"close": 102.0},
        {"close": -100.0},
        {"volume": -1.0},
        {"closed": "false"},
        {"symbol": "BTC-USDT"},
        {"timeframe": "1m"},
    ],
)
def test_score_volume_shadow_rejects_invalid_csv_contract(
    tmp_path,
    row_override: dict[str, object],
) -> None:
    timestamp = _SCORE_BASE_MS
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [_shadow_record(timestamp, state="expanding")],
    )
    _write_score_csv(
        csv_path,
        [timestamp],
        row_overrides={0: row_override},
    )

    with pytest.raises(ValueError):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_requires_csv_scope_columns(tmp_path) -> None:
    timestamp = _SCORE_BASE_MS
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [_shadow_record(timestamp, state="expanding")],
    )
    _write_score_csv(
        csv_path,
        [timestamp],
        include_scope=False,
    )

    with pytest.raises(ValueError, match="CSV 缺少列"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_rejects_rows_tighter_than_declared_timeframe(
    tmp_path,
) -> None:
    timestamps = [_SCORE_BASE_MS, _SCORE_BASE_MS + 60_000]
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [_shadow_record(timestamps[0], state="expanding")],
    )
    _write_score_csv(csv_path, timestamps)

    with pytest.raises(ValueError, match="间隔小于指定周期"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_rejects_continuous_market_gap(tmp_path) -> None:
    timestamps = [_SCORE_BASE_MS, _SCORE_BASE_MS + 1_200_000]
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [
            _shadow_record(
                timestamps[0],
                state="expanding",
                symbol="BTC-USDT",
            )
        ],
    )
    _write_score_csv(
        csv_path,
        timestamps,
        symbol="BTC-USDT",
    )

    with pytest.raises(ValueError, match="连续市场 CSV"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="BTC-USDT",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_rejects_missing_stock_bar(tmp_path) -> None:
    timestamps = [_SCORE_BASE_MS, _SCORE_BASE_MS + 1_200_000]
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [_shadow_record(timestamps[0], state="expanding")],
    )
    _write_score_csv(csv_path, timestamps)

    with pytest.raises(ValueError, match="CSV 缺少应存在的 K 线"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_allows_stock_cross_session_gap(tmp_path) -> None:
    timestamps = [1_749_153_000_000, 1_749_216_600_000]
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [_shadow_record(timestamps[0], state="expanding")],
    )
    _write_score_csv(csv_path, timestamps)

    score = score_volume_shadow_files(
        shadow_path,
        csv_path,
        symbol="AAPL.US",
        timeframe="10m",
        lookahead=1,
        now_utc_ms=_SCORE_NOW_MS,
    )

    assert score.scored_records == 1


def test_score_volume_shadow_rejects_forming_bar_without_closed_column(
    tmp_path,
) -> None:
    timestamp = _SCORE_NOW_MS - 300_000
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [_shadow_record(timestamp, state="expanding")],
    )
    _write_score_csv(
        csv_path,
        [timestamp],
        include_closed=False,
    )

    with pytest.raises(ValueError, match="尚未收盘"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_rejects_zero_duration_timeframe(tmp_path) -> None:
    timestamp = _SCORE_BASE_MS
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [
            _shadow_record(
                timestamp,
                state="expanding",
                timeframe="0m",
            )
        ],
    )
    _write_score_csv(
        csv_path,
        [timestamp],
        timeframe="0m",
    )

    with pytest.raises(ValueError, match="不支持的周期"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="0m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_rejects_scope_mismatch(tmp_path) -> None:
    timestamp = _SCORE_BASE_MS
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [_shadow_record(timestamp, state="expanding")],
    )
    _write_score_csv(csv_path, [timestamp])

    with pytest.raises(ValueError, match="指定标的或周期不一致"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="MSFT.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_rejects_json_scope_mismatch_independently(
    tmp_path,
) -> None:
    timestamp = _SCORE_BASE_MS
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [
            _shadow_record(
                timestamp,
                state="expanding",
                symbol="BTC-USDT",
                timeframe="1m",
            )
        ],
    )
    _write_score_csv(csv_path, [timestamp])

    with pytest.raises(ValueError, match="指定标的或周期不一致"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_counts_unmatched_normal_record(tmp_path) -> None:
    timestamp = _SCORE_BASE_MS
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [_shadow_record(timestamp - 600_000, state="normal")],
    )
    _write_score_csv(csv_path, [timestamp])

    with pytest.raises(ValueError, match="未匹配=1"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


@pytest.mark.parametrize(
    "record_update",
    [
        {"relative_volume": 1.4},
        {"state": "contracting"},
        {"baseline_volume": 0.0},
        {"latest_volume": float("nan")},
    ],
)
def test_score_volume_shadow_rejects_inconsistent_shadow_record(
    tmp_path,
    record_update: dict[str, object],
) -> None:
    timestamp = _SCORE_BASE_MS
    record = _shadow_record(timestamp, state="expanding")
    record.update(record_update)
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(shadow_path, [record])
    _write_score_csv(csv_path, [timestamp])

    with pytest.raises(ValueError):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_rejects_conflicting_duplicate_record(
    tmp_path,
) -> None:
    timestamp = _SCORE_BASE_MS
    first = _shadow_record(timestamp, state="expanding")
    conflicting = dict(first, latest_volume=160.0, relative_volume=1.6)
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(shadow_path, [first, conflicting])
    _write_score_csv(csv_path, [timestamp])

    with pytest.raises(ValueError, match="冲突影子记录"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )


def test_score_volume_shadow_rejects_duplicate_csv_timestamp(tmp_path) -> None:
    timestamp = _SCORE_BASE_MS
    shadow_path = tmp_path / "shadow.jsonl"
    csv_path = tmp_path / "bars.csv"
    _write_shadow(
        shadow_path,
        [_shadow_record(timestamp, state="expanding")],
    )
    _write_score_csv(csv_path, [timestamp, timestamp])

    with pytest.raises(ValueError, match="重复 ts_open"):
        score_volume_shadow_files(
            shadow_path,
            csv_path,
            symbol="AAPL.US",
            timeframe="10m",
            now_utc_ms=_SCORE_NOW_MS,
        )
