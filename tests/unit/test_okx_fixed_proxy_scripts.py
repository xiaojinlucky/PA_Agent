from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from pa_agent.execution.credentials import OkxCredentials
from pa_agent.execution.okx_client import HttpResponse, OkxRestClient
from scripts import probe_okx_fixed_proxy_node as probe_module


@pytest.mark.parametrize("value", ("0", "-1", "11"))
def test_probe_attempts_reject_zero_negative_or_unbounded_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        probe_module._positive_attempts(value)


def test_probe_port_rejects_active_proxy_port():
    with pytest.raises(argparse.ArgumentTypeError, match="10981"):
        probe_module._probe_port("10981")


def test_anytls_candidate_keeps_one_warm_session_for_natural_worker_scans():
    candidate = probe_module._candidate_config(
        {
            "inbounds": [{}],
            "outbounds": [{"tag": "proxy"}, {"type": "direct"}],
        },
        {
            "ConfigType": 11,
            "Address": "node.example.test",
            "Port": 443,
            "Password": "test-password",
            "Sni": "edge.example.test",
            "AllowInsecure": "false",
        },
        listen_port=10982,
    )

    proxy = candidate["outbounds"][0]
    assert proxy["idle_session_check_interval"] == "30s"
    assert proxy["idle_session_timeout"] == "5m"
    assert proxy["min_idle_session"] == 1
    assert proxy["tcp_keep_alive"] == "30s"
    assert proxy["tcp_keep_alive_interval"] == "30s"


@pytest.mark.parametrize("value", ("44", "601", "0", "-1"))
def test_idle_seconds_must_exceed_anytls_idle_check_window(value):
    with pytest.raises(argparse.ArgumentTypeError):
        probe_module._idle_seconds(value)


@pytest.mark.parametrize("value", ("-1", "11"))
def test_idle_cycles_reject_out_of_range_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        probe_module._idle_cycles(value)


def test_endurance_verification_idles_before_every_cycle_read(
    monkeypatch,
):
    sleeps: list[int] = []
    monkeypatch.setattr(
        probe_module.time,
        "sleep",
        lambda seconds: sleeps.append(seconds),
    )
    reads: list[str] = []

    class _Client:
        def sync_server_time(self):
            reads.append("time")

    monkeypatch.setattr(
        probe_module,
        "_read_full_risk_route",
        lambda _client: reads.append("full_route"),
    )

    result = probe_module._endurance_verification(
        _Client(),
        idle_cycles=2,
        idle_seconds=75,
        verify_risk_bills=True,
    )

    assert sleeps == [75, 75]
    assert reads == ["time", "full_route", "time", "full_route"]
    assert result["endurance_ok"] is True
    assert result["endurance_successes"] == 2
    assert result["endurance_errors"] == []


def test_endurance_verification_fails_closed_on_idle_eof(
    monkeypatch,
):
    monkeypatch.setattr(
        probe_module.time,
        "sleep",
        lambda _seconds: None,
    )
    cycle_results = iter((None, OSError("eof after idle")))

    class _Client:
        def sync_server_time(self):
            outcome = next(cycle_results)
            if outcome is not None:
                raise outcome

    monkeypatch.setattr(
        probe_module,
        "_read_full_risk_route",
        lambda _client: None,
    )

    result = probe_module._endurance_verification(
        _Client(),
        idle_cycles=2,
        idle_seconds=90,
        verify_risk_bills=True,
    )

    assert result["endurance_ok"] is False
    assert result["endurance_successes"] == 1
    assert result["endurance_errors"] == ["cycle2:OSError"]


def test_probe_skips_endurance_when_tight_rounds_already_failed(
    monkeypatch,
):
    monkeypatch.setattr(
        probe_module.time,
        "sleep",
        lambda _seconds: None,
    )
    endurance_calls: list[dict] = []
    monkeypatch.setattr(
        probe_module,
        "_endurance_verification",
        lambda *args, **kwargs: endurance_calls.append(kwargs),
    )
    transport_calls: list[str] = []

    class _FailingClient:
        def sync_server_time(self):
            transport_calls.append("time")
            raise OSError("tight round failure")

    monkeypatch.setattr(
        probe_module,
        "OkxRestClient",
        lambda *args, **kwargs: _FailingClient(),
    )
    monkeypatch.setattr(
        probe_module,
        "UrlLibTransport",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        probe_module,
        "load_okx_credentials",
        lambda _environment: object(),
    )

    result = probe_module._probe_existing_proxy(
        proxy_port=10982,
        attempts=2,
        verify_risk_bills=True,
        idle_cycles=2,
        idle_seconds=75,
    )

    assert endurance_calls == []
    assert result["private_read_ok"] is False
    assert result["endurance_ok"] is False
    assert result["endurance_errors"] == ["skipped_tight_rounds_failed"]


def test_activate_rolls_back_when_idle_endurance_fails(
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
    monkeypatch.setattr(
        probe_module.os,
        "kill",
        lambda _process_id, _sig: None,
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
            "risk_bills_ok": True,
            "endurance_ok": False,
            "endurance_errors": ["cycle1:BrokerTransportError"],
        },
    )

    with pytest.raises(
        RuntimeError,
        match="空闲耐久复核失败",
    ):
        probe_module._activate(
            runtime_directory=tmp_path,
            active_config={"new": True},
            metadata={"node_label": "new"},
            attempts=3,
            idle_cycles=2,
            idle_seconds=75,
        )

    assert json.loads(active_path.read_text(encoding="utf-8")) == {
        "old": True
    }
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == {
        "node_label": "old"
    }


def test_main_limits_post_switch_verify_to_one_idle_cycle(
    tmp_path,
    monkeypatch,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "config.json").write_text("{}", encoding="utf-8")
    activation_calls: list[dict] = []
    monkeypatch.setattr(
        probe_module,
        "_profile",
        lambda *_args: {
            "SubRemarks": "provider",
            "Remarks": "node",
            "Delay": 1,
        },
    )
    monkeypatch.setattr(
        probe_module,
        "_candidate_config",
        lambda *_args, **_kwargs: {"inbounds": [{}]},
    )
    monkeypatch.setattr(
        probe_module,
        "_atomic_write_json",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        probe_module,
        "_probe",
        lambda **_kwargs: {
            "private_read_ok": True,
            "risk_bills_ok": True,
            "endurance_ok": True,
        },
    )

    def _record_activation(**kwargs):
        activation_calls.append(kwargs)
        return {"private_read_ok": True, "proxy_process_id": 1}

    monkeypatch.setattr(probe_module, "_activate", _record_activation)
    monkeypatch.setattr(
        "sys.argv",
        [
            "probe",
            "--v2rayn-root",
            str(tmp_path / "v2rayn"),
            "--profile-id",
            "profile",
            "--runtime-directory",
            str(runtime),
            "--idle-cycles",
            "3",
            "--idle-seconds",
            "90",
            "--activate",
        ],
    )

    probe_module.main()

    assert activation_calls[0]["idle_cycles"] == 1
    assert activation_calls[0]["idle_seconds"] == 90


def test_main_requires_explicit_singbox_template_when_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        probe_module,
        "_profile",
        lambda *_args: {
            "SubRemarks": "provider",
            "Remarks": "node",
            "Delay": 1,
        },
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "probe",
            "--v2rayn-root",
            str(tmp_path / "v2rayn"),
            "--profile-id",
            "profile",
            "--runtime-directory",
            str(tmp_path / "runtime"),
        ],
    )

    with pytest.raises(RuntimeError, match="template-config"):
        probe_module.main()


def test_main_rejects_activation_without_idle_cycles(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "sys.argv",
        [
            "probe",
            "--v2rayn-root",
            str(tmp_path),
            "--profile-id",
            "profile",
            "--runtime-directory",
            str(tmp_path / "runtime"),
            "--idle-cycles",
            "0",
            "--activate",
        ],
    )

    with pytest.raises(SystemExit):
        probe_module.main()


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
    probe_calls: list[dict] = []

    def _pass_full_probe(**kwargs):
        probe_calls.append(kwargs)
        return {
            "private_read_ok": True,
            "private_read_successes": 3,
            "risk_bills_ok": True,
            "risk_bill_successes": 3,
            "bill_rows": 1010,
            "endurance_ok": True,
            "endurance_successes": 2,
            "errors": [],
        }

    monkeypatch.setattr(
        probe_module,
        "_probe_existing_proxy",
        _pass_full_probe,
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
        idle_cycles=2,
        idle_seconds=75,
    )

    assert json.loads(active_path.read_text(encoding="utf-8")) == {
        "new": True
    }
    assert json.loads(
        (tmp_path / "metadata.json").read_text(encoding="utf-8")
    )["node_label"] == "tested"
    assert killed == [(101, probe_module.signal.SIGTERM)]
    assert probe_calls == [
        {
            "proxy_port": 10981,
            "attempts": 3,
            "verify_risk_bills": True,
            "idle_cycles": 2,
            "idle_seconds": 75,
        }
    ]
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
            "risk_bills_ok": True,
            "risk_bill_successes": 3,
            "bill_rows": 1010,
            "endurance_ok": True,
            "endurance_successes": 2,
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


def test_activate_rolls_back_when_full_risk_probe_fails(
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
            "private_read_ok": False,
            "private_read_successes": 2,
            "risk_bills_ok": False,
            "risk_bill_successes": 2,
            "bill_rows": 1010,
            "errors": ["TimeoutError"],
        },
    )

    with pytest.raises(
        RuntimeError,
        match="空闲耐久复核失败",
    ):
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


def test_activate_stops_candidate_when_rollback_write_fails(
    tmp_path,
    monkeypatch,
):
    runtime_core = tmp_path / "sing-box.exe"
    runtime_core.write_bytes(b"exe")
    active_path = tmp_path / "config.json"
    active_path.write_text('{"old": true}', encoding="utf-8")
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text('{"node_label": "old"}', encoding="utf-8")
    listeners = iter(({101}, {202}))
    monkeypatch.setattr(
        probe_module,
        "_listener_pids",
        lambda _port: next(listeners),
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
    monkeypatch.setattr(
        probe_module,
        "_wait_for_reloaded_proxy",
        lambda **_kwargs: 202,
    )
    monkeypatch.setattr(
        probe_module,
        "_probe_existing_proxy",
        lambda **_kwargs: {
            "private_read_ok": False,
            "risk_bills_ok": False,
        },
    )
    real_atomic_write_bytes = probe_module._atomic_write_bytes
    active_writes = 0

    def _fail_rollback(path, content):
        nonlocal active_writes
        if path == active_path:
            active_writes += 1
            if active_writes == 2:
                raise OSError("rollback write failed")
        real_atomic_write_bytes(path, content)

    monkeypatch.setattr(
        probe_module,
        "_atomic_write_bytes",
        _fail_rollback,
    )
    fail_closed_calls: list[dict] = []
    monkeypatch.setattr(
        probe_module,
        "_stop_runtime_proxy_fail_closed",
        lambda **kwargs: fail_closed_calls.append(kwargs),
    )

    with pytest.raises(
        RuntimeError,
        match="旧配置恢复失败；10981 已下线",
    ):
        probe_module._activate(
            runtime_directory=tmp_path,
            active_config={"new": True},
            metadata={"node_label": "new"},
            attempts=3,
        )

    assert killed == [101, 202]
    assert fail_closed_calls[0]["known_process_ids"] == {202}
    assert json.loads(active_path.read_text(encoding="utf-8")) == {
        "new": True
    }


def test_activate_downs_foreign_10981_takeover_during_rollback(
    tmp_path,
    monkeypatch,
):
    runtime_core = (tmp_path / "sing-box.exe").resolve()
    runtime_core.write_bytes(b"exe")
    foreign_core = (tmp_path / "foreign-proxy.exe").resolve()
    foreign_core.write_bytes(b"foreign")
    active_path = tmp_path / "config.json"
    active_path.write_text('{"old": true}', encoding="utf-8")
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text('{"node_label": "old"}', encoding="utf-8")
    listeners = iter(({101}, {909}, {909}, set()))
    monkeypatch.setattr(
        probe_module,
        "_listener_pids",
        lambda _port: next(listeners),
    )
    monkeypatch.setattr(
        probe_module,
        "_process_path",
        lambda process_id: (
            foreign_core
            if process_id == 909
            else runtime_core
        ),
    )
    killed: list[int] = []
    monkeypatch.setattr(
        probe_module.os,
        "kill",
        lambda process_id, _sig: killed.append(process_id),
    )
    reload_attempts = 0

    def _reload_or_detect_takeover(**_kwargs):
        nonlocal reload_attempts
        reload_attempts += 1
        if reload_attempts == 1:
            return 202
        raise RuntimeError("10981 已被外部进程占用")

    monkeypatch.setattr(
        probe_module,
        "_wait_for_reloaded_proxy",
        _reload_or_detect_takeover,
    )
    monkeypatch.setattr(
        probe_module,
        "_probe_existing_proxy",
        lambda **_kwargs: {
            "private_read_ok": False,
            "risk_bills_ok": False,
            "endurance_ok": False,
        },
    )
    monkeypatch.setattr(
        probe_module,
        "_runtime_supervisor_pids",
        lambda _runtime_directory: set(),
    )

    with pytest.raises(
        RuntimeError,
        match="旧代理未能重新启动；10981 已下线",
    ):
        probe_module._activate(
            runtime_directory=tmp_path,
            active_config={"new": True},
            metadata={"node_label": "new"},
            attempts=3,
        )

    assert active_path.read_text(encoding="utf-8") == '{"old": true}'
    assert metadata_path.read_text(encoding="utf-8") == (
        '{"node_label": "old"}'
    )
    assert {101, 202, 909}.issubset(killed)


def test_fail_closed_shutdown_stops_supervisor_and_runtime_proxy(
    tmp_path,
    monkeypatch,
):
    runtime_core = (tmp_path / "sing-box.exe").resolve()
    listeners = iter(({202}, set()))
    monkeypatch.setattr(
        probe_module,
        "_runtime_supervisor_pids",
        lambda _runtime_directory: {303},
    )
    monkeypatch.setattr(
        probe_module,
        "_listener_pids",
        lambda _port: next(listeners),
    )
    monkeypatch.setattr(
        probe_module,
        "_process_path",
        lambda _process_id: runtime_core,
    )
    killed: list[int] = []
    monkeypatch.setattr(
        probe_module.os,
        "kill",
        lambda process_id, _sig: killed.append(process_id),
    )

    probe_module._stop_runtime_proxy_fail_closed(
        runtime_directory=tmp_path,
        runtime_core_path=runtime_core,
        known_process_ids={202},
    )

    assert killed == [303, 202]


class _ReadProbeClient:
    def __init__(self, *, fail_bills_on: int | None = None) -> None:
        self.fail_bills_on = fail_bills_on
        self.bill_calls = 0

    def sync_server_time(self) -> None:
        return None

    def balance(self) -> list[dict]:
        return [{}]

    def positions(self) -> list[dict]:
        return []

    def instruments(self, instrument_type: str) -> list[dict]:
        assert instrument_type == "SWAP"
        return [{"instId": "XAU-USDT-SWAP"}]

    def account_config(self) -> dict:
        return {
            "uid": "uid",
            "mainUid": "main-uid",
            "type": "2",
        }

    def account_bills(self) -> list[dict]:
        self.bill_calls += 1
        if self.bill_calls == self.fail_bills_on:
            raise TimeoutError("page timeout")
        return [{}] * 1010


def test_existing_proxy_full_risk_probe_reads_every_bill_page_attempt(
    monkeypatch,
):
    client = _ReadProbeClient()
    monkeypatch.setattr(
        probe_module,
        "UrlLibTransport",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        probe_module,
        "OkxRestClient",
        lambda *_args, **_kwargs: client,
    )
    monkeypatch.setattr(
        probe_module,
        "load_okx_credentials",
        lambda _environment: object(),
    )
    monkeypatch.setattr(probe_module.time, "sleep", lambda _seconds: None)

    result = probe_module._probe_existing_proxy(
        proxy_port=10981,
        attempts=3,
        verify_risk_bills=True,
    )

    assert result["private_read_ok"] is True
    assert result["risk_bills_ok"] is True
    assert result["risk_bill_successes"] == 3
    assert result["bill_rows"] == 1010
    assert result["position_rows"] == 0
    assert result["instrument_rows"] == 1
    assert result["account_config_ok"] is True
    assert client.bill_calls == 3


def test_existing_proxy_full_risk_probe_fails_on_any_bill_timeout(
    monkeypatch,
):
    client = _ReadProbeClient(fail_bills_on=2)
    monkeypatch.setattr(
        probe_module,
        "UrlLibTransport",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        probe_module,
        "OkxRestClient",
        lambda *_args, **_kwargs: client,
    )
    monkeypatch.setattr(
        probe_module,
        "load_okx_credentials",
        lambda _environment: object(),
    )
    monkeypatch.setattr(probe_module.time, "sleep", lambda _seconds: None)

    result = probe_module._probe_existing_proxy(
        proxy_port=10981,
        attempts=3,
        verify_risk_bills=True,
    )

    assert result["private_read_ok"] is False
    assert result["risk_bills_ok"] is False
    assert result["risk_bill_successes"] == 2
    assert result["errors"] == ["TimeoutError"]


def test_full_risk_probe_rejects_incomplete_account_identity():
    client = _ReadProbeClient()
    client.account_config = lambda: {
        "uid": "uid",
        "mainUid": "",
        "type": "2",
    }

    with pytest.raises(
        RuntimeError,
        match="账户身份字段不完整",
    ):
        probe_module._read_full_risk_route(client)


class _SequenceTransport:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.urls: list[str] = []

    def request(self, _method, url, **_kwargs):
        self.urls.append(url)
        payload = self.payloads.pop(0)
        return HttpResponse(
            200,
            json.dumps(payload).encode("utf-8"),
        )


def test_full_risk_route_uses_real_paginated_bill_reader():
    first_bill_page = [
        {"billId": str(2000 - index), "ts": str(1000 - index)}
        for index in range(100)
    ]
    transport = _SequenceTransport(
        [
            {"code": "0", "data": [{"details": []}]},
            {"code": "0", "data": []},
            {
                "code": "0",
                "data": [{"instId": "XAU-USDT-SWAP"}],
            },
            {"code": "0", "data": [{"ts": "1700000000000"}]},
            {
                "code": "0",
                "data": [
                    {"uid": "uid", "mainUid": "main", "type": "2"}
                ],
            },
            {"code": "0", "data": first_bill_page},
            {
                "code": "0",
                "data": [{"billId": "1900", "ts": "899"}],
            },
        ]
    )
    client = OkxRestClient(
        OkxCredentials("key", "secret", "passphrase"),
        simulated=True,
        transport=transport,
        now_ms=lambda: 1_700_000_000_000,
    )

    evidence = probe_module._read_full_risk_route(client)

    assert evidence["bill_rows"] == 101
    assert "after=1901" in transport.urls[-1]


def test_main_forces_full_risk_probe_before_activation(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "v2rayn"
    root.mkdir(parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "config.json").write_text("{}", encoding="utf-8")
    probe_calls: list[dict] = []
    activation_calls: list[dict] = []
    monkeypatch.setattr(
        probe_module,
        "_profile",
        lambda *_args: {
            "SubRemarks": "provider",
            "Remarks": "node",
            "Delay": 1,
        },
    )
    monkeypatch.setattr(
        probe_module,
        "_candidate_config",
        lambda *_args, **_kwargs: {"inbounds": [{}]},
    )
    monkeypatch.setattr(
        probe_module,
        "_atomic_write_json",
        lambda *_args, **_kwargs: None,
    )

    def _failed_full_probe(**kwargs):
        probe_calls.append(kwargs)
        return {
            "private_read_ok": False,
            "risk_bills_ok": False,
        }

    monkeypatch.setattr(probe_module, "_probe", _failed_full_probe)
    monkeypatch.setattr(
        probe_module,
        "_activate",
        lambda **kwargs: activation_calls.append(kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "probe",
            "--v2rayn-root",
            str(root),
            "--profile-id",
            "profile",
            "--runtime-directory",
            str(runtime),
            "--activate",
        ],
    )

    with pytest.raises(
        RuntimeError,
        match="禁止激活该节点",
    ):
        probe_module.main()

    assert probe_calls[0]["verify_risk_bills"] is True
    assert activation_calls == []


def test_provision_rechecks_acl_before_any_runtime_execution():
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "provision_okx_fixed_proxy.ps1"
    ).read_text(encoding="utf-8-sig")

    main = script[script.index('if (-not $RuntimeDirectory) {') :]
    initial_protection = main.index("Protect-RuntimeDirectory")
    first_copy = main.index("Copy-Item")
    final_protection = main.rindex("Protect-RuntimeDirectory")
    final_verification = main.rindex("Assert-RuntimeDirectorySecurity")
    executable_check = main.index("& $runtimeCorePath check")

    assert initial_protection < first_copy
    assert first_copy < final_protection
    assert final_protection < final_verification < executable_check
    assert "/C" not in script
    assert "AccessControlType]::Deny" in script
    assert "FileAttributes]::ReparsePoint" in script
