"""WO-F 声明校验层的价格、K 线引用与真实 tick 测试。"""
from __future__ import annotations

import copy
import json
from dataclasses import replace

from pa_agent.ai.claim_validation import (
    extract_claim_validation_code,
    validate_claims,
)
from pa_agent.ai.json_validator import ValidationError
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from tests.fixtures.validators import schema_test_validator
from tests.integration.conftest import (
    VALID_STAGE1,
    VALID_STAGE2,
)
from tests.integration.conftest import (
    make_frame as make_integration_frame,
)


def _frame(*, price_tick: str | None = "0.1") -> KlineFrame:
    bars = (
        KlineBar(
            seq=1,
            ts_open=3.0,
            open=102.0,
            high=104.0,
            low=101.0,
            close=103.0,
            volume=1.0,
            closed=True,
        ),
        KlineBar(
            seq=2,
            ts_open=2.0,
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=1.0,
            closed=True,
        ),
        KlineBar(
            seq=3,
            ts_open=1.0,
            open=100.0,
            high=101.0,
            low=98.0,
            close=100.0,
            volume=1.0,
            closed=True,
        ),
    )
    return KlineFrame(
        symbol="XAU-USDT-SWAP",
        timeframe="10m",
        bars=bars,
        indicators=IndicatorBundle(
            ema20=(101.0, 100.0, 99.0),
            atr14=(2.0, 2.0, 2.0),
        ),
        snapshot_ts_local_ms=1,
        price_tick=price_tick,
    )


def _stage2(**overrides: object) -> dict:
    decision = {
        "order_type": "突破单",
        "order_direction": "做多",
        "entry_price": 104.1,
        "stop_loss_price": 100.0,
        "take_profit_price": 105.0,
        "take_profit_price_2": 106.0,
        "entry_basis_bar": "K1",
    }
    decision.update(overrides)
    return {
        "decision": decision,
        "decision_trace": [{"bar_range": "K3-K1"}],
    }


def test_legal_breakout_inside_one_atr_tolerance_passes() -> None:
    assert validate_claims("stage2", _stage2(), _frame()) == []


def test_each_order_price_is_checked_against_ohlc_atr_envelope() -> None:
    for field in (
        "entry_price",
        "stop_loss_price",
        "take_profit_price",
        "take_profit_price_2",
    ):
        issues = validate_claims(
            "stage2",
            _stage2(**{field: 106.1}),
            _frame(),
        )
        assert any(
            issue.code == "price_out_of_range"
            and issue.path == f"decision.{field}"
            for issue in issues
        )


def test_flat_stage2_prices_cannot_be_cleared_before_raw_claim_check() -> None:
    payload = copy.deepcopy(VALID_STAGE2)
    flat_decision = payload.pop("decision")
    payload.update(flat_decision)
    payload["entry_price"] = 9999.0
    payload["stop_loss_price"] = 9998.0
    payload["take_profit_price"] = 10000.0
    payload["take_profit_price_2"] = 10001.0
    payload["terminal"]["outcome"] = "reject"

    result = schema_test_validator().validate(
        "stage2",
        json.dumps(payload),
        kline_frame=make_integration_frame(),
        stage1_json=VALID_STAGE1,
    )

    assert isinstance(result, ValidationError)
    assert extract_claim_validation_code(result.invalid_fields) == (
        "price_out_of_range"
    )
    assert any(
        field.startswith(
            "claim_validation:price_out_of_range:entry_price:"
        )
        for field in result.invalid_fields
    )


def test_duplicate_root_price_cannot_hide_behind_no_order_marker() -> None:
    payload = copy.deepcopy(VALID_STAGE2)
    payload.update(
        {
            "order_type": "不下单",
            "entry_price": 9999.0,
            "stop_loss_price": 9998.0,
            "take_profit_price": 10000.0,
            "take_profit_price_2": 10001.0,
        }
    )

    result = schema_test_validator().validate(
        "stage2",
        json.dumps(payload),
        kline_frame=make_integration_frame(),
        stage1_json=VALID_STAGE1,
    )

    assert isinstance(result, ValidationError)
    assert any(
        field.startswith(
            "claim_validation:price_out_of_range:entry_price:"
        )
        for field in result.invalid_fields
    )


def test_extra_stage2_wrapper_prices_are_still_claims() -> None:
    payload = copy.deepcopy(VALID_STAGE2)
    payload["trade_plan"] = {
        "entry_price": 9999.0,
        "stop_loss_price": 9998.0,
    }

    result = schema_test_validator().validate(
        "stage2",
        json.dumps(payload),
        kline_frame=make_integration_frame(),
        stage1_json=VALID_STAGE1,
    )

    assert isinstance(result, ValidationError)
    assert any(
        "trade_plan.entry_price" in field
        for field in result.invalid_fields
    )


def test_scalar_wait_with_flat_prices_reaches_no_order_invariant() -> None:
    payload = copy.deepcopy(VALID_STAGE2)
    flat_decision = payload.pop("decision")
    payload.update(flat_decision)
    payload["decision"] = "wait"
    payload["order_type"] = "不下单"
    payload["terminal"]["outcome"] = "wait"

    result = schema_test_validator().validate(
        "stage2",
        json.dumps(payload),
        kline_frame=make_integration_frame(),
        stage1_json=VALID_STAGE1,
    )

    assert isinstance(result, ValidationError)
    assert "decision.entry_price" in result.invalid_fields
    assert "decision.stop_loss_price" in result.invalid_fields


def test_support_and_resistance_ranges_are_checked() -> None:
    valid = {
        "support_levels": ["98.0", "99.0-100.0"],
        "resistance_levels": ["104.0", "105.0-106.0"],
    }
    assert validate_claims("stage1", valid, _frame()) == []

    invalid = {**valid, "resistance_levels": ["106.1"]}
    issues = validate_claims("stage1", invalid, _frame())
    assert any(
        issue.code == "price_out_of_range"
        and issue.path == "resistance_levels[0]"
        for issue in issues
    )


def test_support_and_resistance_containers_must_be_lists() -> None:
    for obj, path in (
        ({"support_levels": "99.0"}, "support_levels"),
        (
            {"stage1_diagnosis": {"resistance_levels": {"price": 104.0}}},
            "stage1_diagnosis.resistance_levels",
        ),
    ):
        issues = validate_claims("stage1", obj, _frame())
        assert any(
            issue.code == "price_not_numeric" and issue.path == path
            for issue in issues
        )


def test_bar_range_and_new_closed_bars_cannot_exceed_frame() -> None:
    obj = {
        "gate_trace": [{"bar_range": "K4-K1"}],
        "incremental_delta": {"new_closed_bars": ["K1", "K4"]},
    }
    issues = validate_claims("stage1", obj, _frame())
    assert {
        issue.path
        for issue in issues
        if issue.code == "bar_reference_out_of_range"
    } == {
        "gate_trace[0].bar_range",
        "incremental_delta.new_closed_bars[1]",
    }


def test_price_precision_uses_declared_tick_only() -> None:
    issues = validate_claims(
        "stage2",
        _stage2(entry_price=104.05),
        _frame(price_tick="0.1"),
    )
    assert "price_tick_misaligned" in [issue.code for issue in issues]

    missing = validate_claims(
        "stage2",
        _stage2(),
        _frame(price_tick=None),
    )
    assert "price_tick_unavailable" in [issue.code for issue in missing]


def test_stable_code_round_trip_from_invalid_field() -> None:
    issue = validate_claims(
        "stage2",
        _stage2(entry_price=104.05),
        _frame(),
    )[0]
    assert extract_claim_validation_code([issue.as_invalid_field()]) == issue.code


def test_raw_bar_reference_cannot_be_hidden_by_incremental_normalizer() -> None:
    payload = copy.deepcopy(VALID_STAGE1)
    payload["incremental_delta"] = {
        # 长度错误会被既有归一化器重建；原始 K999 仍必须被声明层看见。
        "new_closed_bars": ["K999", "K998"],
        "changed_fields": [],
        "summary": "test",
    }
    result = schema_test_validator().validate(
        "stage1",
        json.dumps(payload),
        kline_frame=_frame(),
        incremental_new_bar_count=1,
    )
    assert isinstance(result, ValidationError)
    assert any(
        field.startswith(
            "claim_validation:bar_reference_out_of_range:"
        )
        for field in result.invalid_fields
    )


def test_non_skipped_bar_range_must_reference_real_kline() -> None:
    for raw in (None, "", "全局", "不适用", "由你填写"):
        issues = validate_claims(
            "stage1",
            {
                "gate_trace": [
                    {
                        "answer": "是",
                        "skipped": False,
                        "bar_range": raw,
                    }
                ]
            },
            _frame(),
        )
        assert any(
            issue.code == "bar_reference_invalid"
            and issue.path == "gate_trace[0].bar_range"
            for issue in issues
        )

    assert (
        validate_claims(
            "stage1",
            {
                "gate_trace": [
                    {
                        "answer": "不适用",
                        "skipped": True,
                        "bar_range": "不适用",
                    }
                ]
            },
            _frame(),
        )
        == []
    )


def test_missing_bar_range_cannot_be_filled_by_normalizer() -> None:
    payload = copy.deepcopy(VALID_STAGE1)
    del payload["gate_trace"][0]["bar_range"]

    result = schema_test_validator().validate(
        "stage1",
        json.dumps(payload),
        kline_frame=_frame(),
    )

    assert isinstance(result, ValidationError)
    assert any(
        field.startswith(
            "claim_validation:bar_reference_invalid:"
            "gate_trace[0].bar_range:"
        )
        for field in result.invalid_fields
    )


def test_numeric_bar_bounds_must_be_actual_integers() -> None:
    issues = validate_claims(
        "stage1",
        {
            "gate_trace": [
                {
                    "bar_range": "K3-K1",
                    "bar_from": 2.5,
                    "bar_to": "1",
                }
            ],
            "incremental_delta": {"new_closed_bars": "K1"},
        },
        _frame(),
    )

    assert {
        issue.path
        for issue in issues
        if issue.code == "bar_reference_invalid"
    } == {
        "gate_trace[0].bar_from",
        "gate_trace[0].bar_to",
        "incremental_delta.new_closed_bars",
    }


def test_invalid_bounds_cannot_crash_before_missing_range_claim() -> None:
    payload = copy.deepcopy(VALID_STAGE1)
    trace = payload["gate_trace"][0]
    del trace["bar_range"]
    trace["bar_from"] = "x"
    trace["bar_to"] = 1

    result = schema_test_validator().validate(
        "stage1",
        json.dumps(payload),
        kline_frame=_frame(),
    )

    assert isinstance(result, ValidationError)
    assert extract_claim_validation_code(result.invalid_fields) == (
        "bar_reference_invalid"
    )
    assert any(
        "gate_trace[0].bar_range" in field
        for field in result.invalid_fields
    )
    assert any(
        "gate_trace[0].bar_from" in field
        for field in result.invalid_fields
    )


def test_kline_reference_in_reason_cannot_exceed_frame() -> None:
    issues = validate_claims(
        "stage2",
        {
            "decision_trace": [
                {
                    "bar_range": "K3-K1",
                    "reason": "对比 K999 后判断",
                }
            ]
        },
        _frame(),
    )
    assert any(
        issue.code == "bar_reference_out_of_range"
        and issue.path == "decision_trace[0].reason"
        for issue in issues
    )


def test_wrapped_numeric_stage1_price_cannot_be_refreshed_away() -> None:
    issues = validate_claims(
        "stage1",
        {
            "stage1_diagnosis": {
                "support_levels": [9999.0],
                "resistance_levels": [104.0],
            }
        },
        _frame(),
    )
    assert any(
        issue.code == "price_out_of_range"
        and issue.path == "stage1_diagnosis.support_levels[0]"
        for issue in issues
    )


def test_current_k1_atr_must_be_valid_even_if_older_atr_exists() -> None:
    frame = replace(
        _frame(),
        indicators=IndicatorBundle(
            ema20=(101.0, 100.0, 99.0),
            atr14=(float("nan"), 50.0, 50.0),
        ),
    )
    issues = validate_claims("stage2", _stage2(), frame)
    assert issues[0].code == "atr_unavailable"


def test_stable_first_code_does_not_depend_on_json_key_order() -> None:
    price_claim = {"support_levels": ["9999.0"]}
    bar_claim = {"gate_trace": [{"bar_range": "K999"}]}
    first = validate_claims(
        "stage1",
        {**price_claim, **bar_claim},
        _frame(),
    )
    second = validate_claims(
        "stage1",
        {**bar_claim, **price_claim},
        _frame(),
    )
    assert first[0].code == second[0].code == "bar_reference_out_of_range"
