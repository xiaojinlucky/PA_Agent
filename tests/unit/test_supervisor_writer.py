from __future__ import annotations

import json

import pytest

from pa_agent.agents.supervisor import SupervisorAgent
from pa_agent.agents.supervisor_models import (
    SupervisorDecision,
    SupervisorInputSnapshot,
    snapshot_digest,
)
from pa_agent.records.supervisor_writer import (
    SupervisorPersistenceError,
    SupervisorWriter,
)
from tests.unit.test_supervisor_agent import _snapshot


def _record(snapshot):
    return SupervisorAgent._record(
        snapshot,
        SupervisorDecision(action="block_entry", reason="测试拒绝"),
        profile_id="primary-profile",
        model_id="primary-model",
        fallback_level="primary",
    )


def test_save_durable_round_trips_and_is_idempotent(tmp_path):
    writer = SupervisorWriter(tmp_path)
    record = _record(_snapshot())

    first_path = writer.save_durable(record)
    second_path = writer.save_durable(record)

    assert first_path == second_path
    assert json.loads(first_path.read_text(encoding="utf-8"))["action"] == "block_entry"
    assert writer.load_for(record.input_snapshot) == record


def test_same_key_cannot_be_overwritten_by_a_different_conclusion(tmp_path):
    writer = SupervisorWriter(tmp_path)
    record = _record(_snapshot())
    writer.save_durable(record)
    conflicting = record.model_copy(
        update={"action": "allow_entry", "reason": "冲突"}
    )

    with pytest.raises(SupervisorPersistenceError, match="不同监督结论"):
        writer.save_durable(conflicting)


def test_corrupt_existing_record_fails_loudly(tmp_path):
    writer = SupervisorWriter(tmp_path)
    snapshot = _snapshot()
    path = writer._path(snapshot)
    path.write_text('{"action":"allow_entry"}', encoding="utf-8")

    with pytest.raises(SupervisorPersistenceError):
        writer.load_for_key(
            campaign_id=snapshot.campaign_id,
            bar_ms=snapshot.closed_bar_ts_open_ms,
            analysis_digest=snapshot.analysis_digest,
        )


def test_inner_snapshot_identity_must_match_outer_record(tmp_path):
    writer = SupervisorWriter(tmp_path)
    record = _record(_snapshot())
    path = writer._path(record.input_snapshot)
    inner = record.input_snapshot.model_copy(update={"campaign_id": "other"})
    payload = record.model_dump(mode="json")
    payload["input_snapshot"] = inner.model_dump(mode="json")
    payload["input_snapshot_digest"] = snapshot_digest(inner)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SupervisorPersistenceError, match="record_id 与输入快照"):
        writer.load_for_key(
            campaign_id=record.campaign_id,
            bar_ms=record.closed_bar_ts_open_ms,
            analysis_digest=record.analysis_digest,
        )


def test_snapshot_is_frozen_and_extra_fields_are_rejected():
    snapshot = _snapshot()
    with pytest.raises((TypeError, ValueError)):
        snapshot.active_execution_count = 1
    with pytest.raises(ValueError):
        SupervisorInputSnapshot.model_validate(
            {**snapshot.model_dump(), "unexpected": True}
        )
