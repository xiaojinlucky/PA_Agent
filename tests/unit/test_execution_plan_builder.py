from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa_agent.config.settings import Settings
from pa_agent.execution.errors import PlanBlocked
from pa_agent.execution.plan_builder import (
    build_execution_plan,
    execution_route_fingerprint,
)
from pa_agent.records.schema import AnalysisRecord, RecordMeta


def _record(*, direction: str = "做多", order_type: str = "限价单") -> AnalysisRecord:
    if direction == "做多":
        entry, tp1, tp2, stop = 100, 110, 120, 95
    else:
        entry, tp1, tp2, stop = 100, 90, 80, 105
    return AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-07-17T00:00:00+08:00",
            timestamp_local_ms=1784217600000,
            symbol="XAUUSD",
            timeframe="15m",
            bar_count=100,
            ai_provider={"model": "test"},
            decision_stance="balanced",
        ),
        kline_data=[],
        htf_text="",
        stage1_messages=[],
        stage1_response={},
        stage1_diagnosis={"gate_result": "proceed"},
        stage2_messages=[],
        stage2_response={},
        stage2_decision={
            "decision": {
                "order_direction": direction,
                "order_type": order_type,
                "entry_price": entry,
                "take_profit_price": tp1,
                "take_profit_price_2": tp2,
                "stop_loss_price": stop,
                "trade_confidence": 88,
            }
        },
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )


def _persist(record: AnalysisRecord, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "record.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path


def _settings() -> Settings:
    settings = Settings()
    settings.execution.enabled = True
    settings.execution.selected_broker = "okx"
    settings.execution.min_trade_confidence = 70
    settings.execution.okx.source_symbol = "XAUUSD"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    settings.execution.okx.quantity = "2"
    settings.execution.okx.product = "swap"
    return settings


def test_plan_uses_only_local_route_fields(tmp_path, monkeypatch):
    record = _record()
    record.stage2_decision["decision"].update(
        {
            "broker": "attacker",
            "instrument": "BTC-USDT",
            "quantity": "999999",
            "leverage": "125",
        }
    )
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    path = _persist(record, tmp_path)

    plan = build_execution_plan(record, _settings(), record_path=path)

    assert plan.broker == "okx"
    assert plan.instrument == "XAU-USDT-SWAP"
    assert str(plan.quantity) == "2"


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda record, settings: setattr(settings.execution, "enabled", False), "execution_disabled"),
        (
            lambda record, settings: record.stage2_decision["decision"].update(
                {"order_type": "不下单"}
            ),
            "no_order",
        ),
        (
            lambda record, settings: record.stage2_decision["decision"].update(
                {"trade_confidence": 69}
            ),
            "confidence_below_execution_gate",
        ),
        (
            lambda record, settings: setattr(
                settings.execution.okx, "source_symbol", "BTCUSD"
            ),
            "source_symbol_mismatch",
        ),
    ],
)
def test_plan_blocks_untrusted_or_ineligible_analysis(
    tmp_path,
    monkeypatch,
    mutator,
    code,
):
    record = _record()
    settings = _settings()
    mutator(record, settings)
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    path = _persist(record, tmp_path)

    with pytest.raises(PlanBlocked) as exc:
        build_execution_plan(record, settings, record_path=path)

    assert exc.value.code == code


def test_plan_blocks_demo_replay_before_any_broker_work(tmp_path, monkeypatch):
    record = _record()
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    path = _persist(record, tmp_path)

    with pytest.raises(PlanBlocked) as exc:
        build_execution_plan(
            record,
            _settings(),
            record_path=path,
            is_demo_replay=True,
        )

    assert exc.value.code == "demo_replay"


def test_plan_requires_file_content_to_match_memory_record(tmp_path, monkeypatch):
    record = _record()
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    path = _persist(record, tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["meta"]["symbol"] = "BTCUSD"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(PlanBlocked) as exc:
        build_execution_plan(record, _settings(), record_path=path)

    assert exc.value.code == "record_mismatch"


def test_okx_spot_cannot_open_short(tmp_path, monkeypatch):
    record = _record(direction="做空")
    settings = _settings()
    settings.execution.okx.product = "spot"
    settings.execution.okx.instrument = "XAUT-USDT"
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    path = _persist(record, tmp_path)

    with pytest.raises(PlanBlocked) as exc:
        build_execution_plan(record, settings, record_path=path)

    assert exc.value.code == "spot_short_not_supported"


def test_okx_route_snapshot_and_fingerprint_include_connection_route(
    tmp_path,
    monkeypatch,
):
    record = _record()
    settings = _settings()
    settings.execution.okx.simulated = True
    settings.execution.okx.api_base_url = "https://demo-a.example"
    settings.execution.okx.margin_mode = "cross"
    settings.execution.entry_timeout_seconds = 45
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    path = _persist(record, tmp_path)

    plan = build_execution_plan(record, settings, record_path=path)
    original_fingerprint = execution_route_fingerprint(settings)
    settings.execution.okx.api_base_url = "https://demo-b.example"

    assert plan.environment == "demo"
    assert plan.okx_api_base_url == "https://demo-a.example"
    assert plan.okx_margin_mode == "cross"
    assert plan.entry_timeout_seconds == 45
    assert execution_route_fingerprint(settings) != original_fingerprint
