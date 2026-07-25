from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts import probe_okx_fixed_proxy_node as probe_module


@pytest.mark.parametrize("value", ("0", "-1", "11"))
def test_probe_attempts_reject_zero_negative_or_unbounded_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        probe_module._positive_attempts(value)


def test_probe_port_rejects_active_proxy_port():
    with pytest.raises(argparse.ArgumentTypeError, match="10981"):
        probe_module._probe_port("10981")


def test_activate_reloads_and_verifies_the_runtime_proxy(
    tmp_path,
    monkeypatch,
):
    runtime_core = tmp_path / "sing-box.exe"
    runtime_core.write_bytes(b"exe")
    active_path = tmp_path / "config.json"
    active_path.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(
        probe_module,
        "_listener_pids",
        lambda _port: {101},
    )
    monkeypatch.setattr(
        probe_module,
        "_process_path",
        lambda _process_id: runtime_core.resolve(),
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        probe_module.os,
        "kill",
        lambda process_id, sig: killed.append((process_id, sig)),
    )
    monkeypatch.setattr(
        probe_module,
        "_wait_for_reloaded_proxy",
        lambda **_kwargs: 202,
    )
    monkeypatch.setattr(
        probe_module,
        "_probe_existing_proxy",
        lambda **_kwargs: {
            "private_read_ok": True,
            "private_read_successes": 3,
            "errors": [],
        },
    )

    result = probe_module._activate(
        runtime_directory=tmp_path,
        active_config={"new": True},
        metadata={
            "node_label": "tested",
            "listen_host": "127.0.0.1",
            "listen_port": 10981,
        },
        attempts=3,
    )

    assert json.loads(active_path.read_text(encoding="utf-8")) == {
        "new": True
    }
    assert json.loads(
        (tmp_path / "metadata.json").read_text(encoding="utf-8")
    )["node_label"] == "tested"
    assert killed == [(101, probe_module.signal.SIGTERM)]
    assert result["private_read_ok"] is True
    assert result["proxy_process_id"] == 202


def test_atomic_write_never_leaves_partial_temp_file(tmp_path):
    target = tmp_path / "config.json"

    probe_module._atomic_write_json(target, {"ready": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "ready": True
    }
    assert not Path(f"{target}.tmp").exists()


def test_activate_rolls_back_proxy_when_metadata_write_fails(
    tmp_path,
    monkeypatch,
):
    runtime_core = tmp_path / "sing-box.exe"
    runtime_core.write_bytes(b"exe")
    active_path = tmp_path / "config.json"
    active_path.write_text('{"old": true}', encoding="utf-8")
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text('{"node_label": "old"}', encoding="utf-8")
    listener_results = iter(({101}, {202}))
    monkeypatch.setattr(
        probe_module,
        "_listener_pids",
        lambda _port: next(listener_results),
    )
    monkeypatch.setattr(
        probe_module,
        "_process_path",
        lambda _process_id: runtime_core.resolve(),
    )
    killed: list[int] = []
    monkeypatch.setattr(
        probe_module.os,
        "kill",
        lambda process_id, _sig: killed.append(process_id),
    )
    reloaded = iter((202, 303))
    monkeypatch.setattr(
        probe_module,
        "_wait_for_reloaded_proxy",
        lambda **_kwargs: next(reloaded),
    )
    monkeypatch.setattr(
        probe_module,
        "_probe_existing_proxy",
        lambda **_kwargs: {
            "private_read_ok": True,
            "private_read_successes": 3,
            "errors": [],
        },
    )
    real_atomic_write_json = probe_module._atomic_write_json

    def _fail_metadata(path, payload):
        if path == metadata_path:
            raise OSError("metadata write failed")
        real_atomic_write_json(path, payload)

    monkeypatch.setattr(
        probe_module,
        "_atomic_write_json",
        _fail_metadata,
    )

    with pytest.raises(OSError, match="metadata write failed"):
        probe_module._activate(
            runtime_directory=tmp_path,
            active_config={"new": True},
            metadata={"node_label": "new"},
            attempts=3,
        )

    assert json.loads(active_path.read_text(encoding="utf-8")) == {
        "old": True
    }
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == {
        "node_label": "old"
    }
    assert killed == [101, 202]
