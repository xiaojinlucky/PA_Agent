from __future__ import annotations

import json

import pytest

from pa_agent.records.pending_writer import PendingWriter
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
