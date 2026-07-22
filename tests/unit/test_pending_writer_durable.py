from __future__ import annotations

import json

import pytest

from pa_agent.records.pending_writer import PendingWriter
from pa_agent.records.schema import ConversationCheckpoint
from tests.unit.test_execution_plan_builder import _record


def test_save_full_durable_round_trips_and_exposes_canonical_path(tmp_path):
    writer = PendingWriter(pending_dir=tmp_path)
    record = _record()

    path = writer.save_full_durable(record)

    assert path == writer.full_path(record)
    assert json.loads(path.read_text(encoding="utf-8"))["stage2_decision"] == (
        record.stage2_decision
    )
    assert not list(tmp_path.glob("*.tmp"))


def test_save_full_durable_propagates_replace_failure(tmp_path, monkeypatch):
    writer = PendingWriter(pending_dir=tmp_path)
    record = _record()
    path = writer.full_path(record)
    path.write_text('{"sentinel": true}', encoding="utf-8")

    def _fail_replace(_source, _target):
        raise OSError("disk failure")

    monkeypatch.setattr("pa_agent.records.pending_writer.os.replace", _fail_replace)

    with pytest.raises(OSError, match="disk failure"):
        writer.save_full_durable(record)

    assert path.read_text(encoding="utf-8") == '{"sentinel": true}'


def test_conversation_checkpoint_round_trips_without_credentials(tmp_path):
    writer = PendingWriter(pending_dir=tmp_path)
    checkpoint = ConversationCheckpoint(
        record_id="2026-07-19_XAUUSD_30m",
        provider_adapter="codex_subscription",
        model="gpt-5.6-sol",
        thread_id="019f6506-f487-72a2-92d2-e7eca30a00f2",
        last_turn=2,
        updated_at_ms=123,
    )

    path = writer.save_conversation_checkpoint(checkpoint)
    loaded = writer.load_conversation_checkpoint(checkpoint.record_id)

    assert loaded == checkpoint
    text = path.read_text(encoding="utf-8")
    assert "api_key" not in text.casefold()
    assert "token" not in text.casefold()


def test_invalid_conversation_checkpoint_is_not_resumed(tmp_path):
    writer = PendingWriter(pending_dir=tmp_path)
    path = tmp_path / "record.conversation.json"
    path.write_text('{"thread_id": "broken"}', encoding="utf-8")

    assert writer.load_conversation_checkpoint("record") is None


@pytest.mark.parametrize(
    "record_id",
    ("..\\..\\outside", "../../outside", "..", "bad\x00name"),
)
def test_conversation_checkpoint_rejects_unsafe_record_id(record_id):
    with pytest.raises(ValueError):
        ConversationCheckpoint(
            record_id=record_id,
            provider_adapter="codex_subscription",
            model="gpt-5.6-sol",
            thread_id="019f6506-f487-72a2-92d2-e7eca30a00f2",
            last_turn=0,
            updated_at_ms=0,
        )


@pytest.mark.parametrize(
    ("thread_id", "last_turn", "updated_at_ms"),
    (
        ("--last", 0, 0),
        ("thread-1", 0, 0),
        ("019f6506-f487-72a2-92d2-e7eca30a00f2", -1, 0),
        ("019f6506-f487-72a2-92d2-e7eca30a00f2", 0, -1),
    ),
)
def test_conversation_checkpoint_rejects_unsafe_resume_metadata(
    thread_id,
    last_turn,
    updated_at_ms,
):
    with pytest.raises(ValueError):
        ConversationCheckpoint(
            record_id="2026-07-19_XAUUSD_30m",
            provider_adapter="codex_subscription",
            model="gpt-5.6-sol",
            thread_id=thread_id,
            last_turn=last_turn,
            updated_at_ms=updated_at_ms,
        )


def test_checkpoint_load_rejects_path_traversal_before_filesystem_access(tmp_path):
    writer = PendingWriter(pending_dir=tmp_path / "pending")
    outside = tmp_path / "outside.conversation.json"
    outside.write_text('{"sentinel": true}', encoding="utf-8")

    assert writer.load_conversation_checkpoint("../outside") is None
    assert outside.read_text(encoding="utf-8") == '{"sentinel": true}'
