"""Unit tests for PendingWriter._sanitize and api_key integration."""
from __future__ import annotations

import json
from datetime import datetime

from pa_agent.orchestrator.free_chat import _derive_record_id
from pa_agent.records.pending_writer import (
    PendingWriter,
    _build_basename,
    _build_legacy_basename,
)
from pa_agent.util.mask_secret import mask_secret

# ---------------------------------------------------------------------------
# _sanitize static method
# ---------------------------------------------------------------------------

class TestSanitize:
    def test_empty_api_key_is_noop(self):
        data = {"key": "some-secret-value", "nested": {"x": "some-secret-value"}}
        result = PendingWriter._sanitize(data, "")
        assert result == data

    def test_replaces_exact_match_in_string(self):
        api_key = "sk-abc123"
        data = {"field": api_key}
        result = PendingWriter._sanitize(data, api_key)
        assert result["field"] == mask_secret(api_key)

    def test_replaces_substring_within_string(self):
        api_key = "sk-abc123"
        data = {"field": f"Authorization: Bearer {api_key} extra"}
        result = PendingWriter._sanitize(data, api_key)
        assert api_key not in result["field"]
        assert mask_secret(api_key) in result["field"]

    def test_replaces_in_nested_dict(self):
        api_key = "sk-abc123"
        data = {"outer": {"inner": {"deep": api_key}}}
        result = PendingWriter._sanitize(data, api_key)
        assert result["outer"]["inner"]["deep"] == mask_secret(api_key)

    def test_replaces_in_list(self):
        api_key = "sk-abc123"
        data = {"items": [api_key, "safe", f"prefix-{api_key}"]}
        result = PendingWriter._sanitize(data, api_key)
        assert result["items"][0] == mask_secret(api_key)
        assert result["items"][1] == "safe"
        assert api_key not in result["items"][2]

    def test_replaces_in_nested_list_of_dicts(self):
        api_key = "sk-abc123"
        data = {"turns": [{"role": "user", "content": f"key={api_key}"}]}
        result = PendingWriter._sanitize(data, api_key)
        assert api_key not in result["turns"][0]["content"]
        assert mask_secret(api_key) in result["turns"][0]["content"]

    def test_non_string_values_are_unchanged(self):
        api_key = "sk-abc123"
        data = {"count": 42, "flag": True, "nothing": None}
        result = PendingWriter._sanitize(data, api_key)
        assert result == data

    def test_multiple_occurrences_in_one_string(self):
        api_key = "sk-abc123"
        data = {"field": f"{api_key} and {api_key}"}
        result = PendingWriter._sanitize(data, api_key)
        assert api_key not in result["field"]
        masked = mask_secret(api_key)
        assert result["field"] == f"{masked} and {masked}"


# ---------------------------------------------------------------------------
# Constructor api_key parameter
# ---------------------------------------------------------------------------

class TestConstructorApiKey:
    def test_default_api_key_is_empty(self, tmp_path):
        writer = PendingWriter(pending_dir=tmp_path)
        assert writer._api_key == ""

    def test_api_key_stored(self, tmp_path):
        writer = PendingWriter(pending_dir=tmp_path, api_key="sk-test")
        assert writer._api_key == "sk-test"


# ---------------------------------------------------------------------------
# save_full / save_partial sanitize before writing
# ---------------------------------------------------------------------------

def _make_record(api_key_in_content: str):
    """Build a minimal AnalysisRecord with the api_key embedded in a text field."""
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    meta = RecordMeta(
        timestamp_local_iso="2026-05-18T14:00:00+08:00",
        timestamp_local_ms=1_700_000_000_000,
        symbol="XAUUSD",
        timeframe="1h",
        bar_count=100,
        ai_provider={"model": "deepseek", "note": api_key_in_content},
    )
    return AnalysisRecord(
        meta=meta,
        kline_data=[],
        htf_text=f"htf context key={api_key_in_content}",
        stage1_messages=[{"role": "user", "content": api_key_in_content}],
        stage1_response=None,
        stage1_diagnosis=None,
        stage2_messages=[],
        stage2_response=None,
        stage2_decision=None,
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={"prompt_tokens": 0, "completion_tokens": 0},
    )


class TestSaveFullSanitizes:
    def test_api_key_not_in_written_file(self, tmp_path):
        api_key = "sk-supersecret"
        record = _make_record(api_key)
        writer = PendingWriter(pending_dir=tmp_path, api_key=api_key)
        path = writer.save_full(record)
        content = path.read_text(encoding="utf-8")
        assert api_key not in content
        assert mask_secret(api_key) in content

    def test_no_api_key_writes_plaintext(self, tmp_path):
        api_key = "sk-supersecret"
        record = _make_record(api_key)
        writer = PendingWriter(pending_dir=tmp_path)  # no api_key
        path = writer.save_full(record)
        content = path.read_text(encoding="utf-8")
        assert api_key in content


class TestSavePartialSanitizes:
    def test_api_key_not_in_written_file(self, tmp_path):
        api_key = "sk-supersecret"
        record = _make_record(api_key)
        writer = PendingWriter(pending_dir=tmp_path, api_key=api_key)
        path = writer.save_partial(record, reason="timeout")
        content = path.read_text(encoding="utf-8")
        assert api_key not in content
        assert mask_secret(api_key) in content

    def test_partial_reason_preserved(self, tmp_path):
        api_key = "sk-supersecret"
        record = _make_record(api_key)
        writer = PendingWriter(pending_dir=tmp_path, api_key=api_key)
        path = writer.save_partial(record, reason="timeout")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["_partial_reason"] == "timeout"

    def test_exception_partial_reason_mirrored(self, tmp_path):
        record = _make_record("sk-test")
        record = record.model_copy(
            update={
                "exception": {
                    "type": "validation_error",
                    "stage": "stage2",
                    "category": "c",
                    "message": "bad field",
                }
            }
        )
        writer = PendingWriter(pending_dir=tmp_path)
        path = writer.save_partial(record, reason="stage2_c")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["_partial_reason"] == "stage2_c"
        assert data["exception"]["partial_reason"] == "stage2_c"

    def test_claim_failure_partial_is_durable_and_reloadable(self, tmp_path):
        from pa_agent.records.analysis_history import load_record

        record = _make_record("sk-test").model_copy(
            update={
                "exception": {
                    "type": "claim_validation",
                    "stage": "stage2",
                    "category": "c",
                    "code": "price_out_of_range",
                    "message": "bad price",
                    "invalid_fields": [
                        "claim_validation:price_out_of_range:"
                        "decision.entry_price:bad price"
                    ],
                }
            }
        )
        writer = PendingWriter(pending_dir=tmp_path)
        path = writer.save_partial_durable(
            record,
            reason="stage2_claim_validation_price_out_of_range",
        )

        reloaded = load_record(path)
        assert reloaded is not None
        assert reloaded.exception is not None
        assert reloaded.exception["code"] == "price_out_of_range"
        assert (
            reloaded.exception["partial_reason"]
            == "stage2_claim_validation_price_out_of_range"
        )

    def test_generic_partial_remains_unavailable_as_history_baseline(
        self,
        tmp_path,
    ):
        from pa_agent.records.analysis_history import load_record

        record = _make_record("sk-test")
        writer = PendingWriter(pending_dir=tmp_path)
        path = writer.save_partial(record, reason="timeout")

        assert load_record(path) is None

    def test_campaign_id_separates_same_second_record_names(
        self,
        tmp_path,
    ):
        record = _make_record("sk-test")
        campaign_record = record.model_copy(
            update={
                "meta": record.meta.model_copy(
                    update={
                        "campaign_id": (
                            "11111111-1111-4111-8111-111111111111"
                        )
                    }
                )
            }
        )
        writer = PendingWriter(pending_dir=tmp_path)

        assert writer.full_path(record) != writer.full_path(campaign_record)

    def test_millisecond_separates_same_second_record_names(self, tmp_path):
        record = _make_record("sk-test")
        next_millisecond = record.model_copy(
            update={
                "meta": record.meta.model_copy(
                    update={
                        "timestamp_local_ms": (
                            record.meta.timestamp_local_ms + 1
                        )
                    }
                )
            }
        )
        writer = PendingWriter(pending_dir=tmp_path)

        assert writer.full_path(record) != writer.full_path(next_millisecond)

    def test_record_names_use_minutes_instead_of_month(
        self,
        monkeypatch,
    ):
        record = _make_record("sk-test")
        monkeypatch.setattr(
            "pa_agent.records.pending_writer._ms_to_local_datetime",
            lambda _: datetime(2026, 7, 27, 19, 43, 26),
        )

        assert _build_basename(record) == (
            "2026-07-27_19-43-26-000_XAUUSD_1h"
        )

    def test_free_chat_sidecar_uses_exact_record_basename(self, tmp_path):
        record = _make_record("sk-test")
        record = record.model_copy(
            update={
                "meta": record.meta.model_copy(
                    update={
                        "campaign_id": (
                            "11111111-1111-4111-8111-111111111111"
                        )
                    }
                )
            }
        )
        writer = PendingWriter(pending_dir=tmp_path)

        assert _derive_record_id(record) == writer.full_path(record).stem

    def test_old_buggy_month_record_keeps_legacy_sidecar_id(
        self,
        tmp_path,
        monkeypatch,
    ):
        record = _make_record("sk-test")
        record = record.model_copy(
            update={
                "meta": record.meta.model_copy(
                    update={
                        "timestamp_local_ms": (
                            record.meta.timestamp_local_ms + 123
                        )
                    }
                )
            }
        )
        monkeypatch.setattr(
            "pa_agent.records.pending_writer._ms_to_local_datetime",
            lambda _: datetime(2026, 7, 27, 19, 43, 26),
        )
        writer = PendingWriter(pending_dir=tmp_path)
        legacy_id = "2026-07-27_19-07-26_XAUUSD_1h"
        (tmp_path / f"{legacy_id}.json").write_text("{}", encoding="utf-8")

        assert _build_legacy_basename(record) == legacy_id
        assert writer.record_id(record) == legacy_id
