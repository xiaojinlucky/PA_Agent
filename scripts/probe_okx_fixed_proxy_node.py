"""在独立端口测试一个 v2rayN 节点，不切换主 v2rayN。"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from pa_agent.execution.credentials import load_okx_credentials
from pa_agent.execution.errors import BrokerApiError
from pa_agent.execution.okx_client import OkxRestClient, UrlLibTransport

_ACTIVE_PROXY_PORT = 10981


def _enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_attempts(value: str) -> int:
    attempts = int(value)
    if not 1 <= attempts <= 10:
        raise argparse.ArgumentTypeError("attempts 必须在 1 到 10 之间")
    return attempts


def _probe_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("probe-port 必须是有效端口")
    if port == _ACTIVE_PROXY_PORT:
        raise argparse.ArgumentTypeError(
            "probe-port 不得占用正式固定代理端口 10981"
        )
    return port


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp_path.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_bytes(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _assert_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"独立测试端口 {port} 已被占用，禁止误测其他进程"
            ) from exc


def _listener_pids(port: int) -> set[int]:
    command = (
        "$rows = Get-NetTCPConnection -State Listen "
        f"-LocalAddress 127.0.0.1 -LocalPort {port} "
        "-ErrorAction SilentlyContinue; "
        "$rows | ForEach-Object { $_.OwningProcess }"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise RuntimeError("无法核验固定代理监听进程")
    return {
        int(line.strip())
        for line in completed.stdout.splitlines()
        if line.strip().isdigit()
    }


def _process_path(process_id: int) -> Path:
    command = (
        f"$process = Get-Process -Id {process_id} "
        "-ErrorAction Stop; $process.Path"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    path = completed.stdout.strip()
    if completed.returncode != 0 or not path:
        raise RuntimeError(f"无法核验监听进程 {process_id} 的程序路径")
    return Path(path).resolve()


def _profile(database_path: Path, profile_id: str) -> dict[str, Any]:
    connection = sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            select p.*, coalesce(e.Delay, -1) as Delay,
                   coalesce(s.Remarks, '') as SubRemarks
              from ProfileItem p
              left join ProfileExItem e on e.IndexId = p.IndexId
              left join SubItem s on s.Id = p.Subid
             where p.IndexId = ?
            """,
            (profile_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError("节点 ID 不存在")
    result = dict(row)
    if int(result["ConfigType"]) not in {1, 6, 11}:
        raise ValueError(
            "当前探测器只接受 VMess、Hysteria2 或 AnyTLS 节点"
        )
    if int(result["Delay"]) == -1:
        raise ValueError("节点延迟为 -1，按用户规则禁止测试或切换")
    return result


def _candidate_config(
    template: dict[str, Any],
    profile: dict[str, Any],
    *,
    listen_port: int,
) -> dict[str, Any]:
    config = copy.deepcopy(template)
    if len(config.get("inbounds") or []) != 1:
        raise ValueError("sing-box 模板不是单入口配置")
    inbound = config["inbounds"][0]
    inbound.update(
        {
            "type": "mixed",
            "tag": "pa-agent-okx-probe",
            "listen": "127.0.0.1",
            "listen_port": listen_port,
        }
    )
    proxy = next(
        (
            outbound
            for outbound in config.get("outbounds") or []
            if outbound.get("tag") == "proxy"
        ),
        None,
    )
    if proxy is None:
        raise ValueError("sing-box 模板缺少 proxy 出口")
    proxy.clear()
    proxy.update(
        {
            "server": str(profile["Address"]),
            "server_port": int(profile["Port"]),
            "tag": "proxy",
        }
    )
    config_type = int(profile["ConfigType"])
    if config_type == 11:
        proxy.update(
            {
                "type": "anytls",
                "password": str(profile["Password"]),
                "tls": {
                    "enabled": True,
                    "server_name": str(
                        profile.get("Sni") or profile["Address"]
                    ),
                    "insecure": _enabled(profile.get("AllowInsecure")),
                },
            }
        )
    elif config_type == 6:
        proxy.update(
            {
                "type": "hysteria2",
                "password": str(
                    profile.get("Password") or profile["Id"]
                ),
                "tls": {
                    "enabled": True,
                    "server_name": str(
                        profile.get("Sni") or profile["Address"]
                    ),
                    "insecure": _enabled(profile.get("AllowInsecure")),
                },
            }
        )
    else:
        protocol_extra = json.loads(profile.get("ProtoExtra") or "{}")
        transport_extra = json.loads(
            profile.get("TransportExtra") or "{}"
        )
        proxy.update(
            {
                "type": "vmess",
                "uuid": str(profile["Id"]),
                "security": str(
                    profile.get("Security")
                    or protocol_extra.get("VmessSecurity")
                    or "auto"
                ),
                "alter_id": int(protocol_extra.get("AlterId") or 0),
            }
        )
        network = str(profile.get("Network") or "raw")
        if network == "ws":
            transport: dict[str, Any] = {
                "type": "ws",
                "path": str(
                    profile.get("Path")
                    or transport_extra.get("Path")
                    or "/"
                ),
            }
            request_host = str(profile.get("RequestHost") or "").strip()
            if request_host:
                transport["headers"] = {"Host": [request_host]}
            proxy["transport"] = transport
        elif network not in {"", "raw", "tcp"}:
            raise ValueError(f"尚未核验的 VMess 传输：{network}")
        if str(profile.get("StreamSecurity") or "") == "tls":
            proxy["tls"] = {
                "enabled": True,
                "server_name": str(
                    profile.get("Sni") or profile["Address"]
                ),
                "insecure": _enabled(profile.get("AllowInsecure")),
            }
    experimental = config.get("experimental")
    if isinstance(experimental, dict):
        experimental.pop("cache_file", None)
        experimental.pop("clash_api", None)
        if not experimental:
            config.pop("experimental", None)
    return config


def _wait_for_listener(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"sing-box 提前退出，退出码 {process.returncode}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                if process.pid not in _listener_pids(port):
                    raise RuntimeError(
                        "测试端口监听者不是本次 sing-box 进程"
                    )
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("独立测试端口未在 12 秒内监听")


def _probe(
    *,
    core_path: Path,
    config_path: Path,
    proxy_port: int,
    attempts: int,
) -> dict[str, Any]:
    _assert_port_available(proxy_port)
    completed = subprocess.run(
        [str(core_path), "check", "-c", str(config_path)],
        cwd=config_path.parent,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise RuntimeError("sing-box 配置检查失败")
    process = subprocess.Popen(
        [str(core_path), "run", "-c", str(config_path)],
        cwd=config_path.parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        _wait_for_listener(proxy_port, process)
        proxy_url = f"http://127.0.0.1:{proxy_port}"
        transport = UrlLibTransport(proxy_url=proxy_url)
        egress_ip = ""
        try:
            ip_response = transport.request(
                "GET",
                "https://api.ipify.org?format=json",
                headers={"Accept": "application/json"},
                body=None,
                timeout=10,
            )
            egress_ip = str(
                json.loads(ip_response.body.decode("utf-8"))["ip"]
            )
        except Exception:  # noqa: BLE001
            pass
        client = OkxRestClient(
            load_okx_credentials("demo"),
            simulated=True,
            transport=transport,
            timeout=10,
        )
        public_successes = 0
        private_successes = 0
        public_latencies: list[int] = []
        balance_rows = 0
        api_codes: list[str] = []
        errors: list[str] = []
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                client.sync_server_time()
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)
                continue
            public_successes += 1
            public_latencies.append(
                round((time.monotonic() - started) * 1000)
            )
            try:
                balance_rows = len(client.balance())
            except BrokerApiError as exc:
                api_codes.append(exc.code)
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)
            else:
                private_successes += 1
            if attempt + 1 < attempts:
                time.sleep(1)
        return {
            "public_ok": public_successes == attempts,
            "private_read_ok": private_successes == attempts,
            "egress_ip": egress_ip,
            "attempts": attempts,
            "public_successes": public_successes,
            "private_read_successes": private_successes,
            "okx_latency_ms": (
                round(sum(public_latencies) / len(public_latencies))
                if public_latencies
                else None
            ),
            "balance_rows": balance_rows,
            "api_codes": sorted(set(api_codes)),
            "errors": errors,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "public_ok": False,
            "private_read_ok": False,
            "attempts": attempts,
            "public_successes": 0,
            "private_read_successes": 0,
            "error": type(exc).__name__,
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _probe_existing_proxy(
    *,
    proxy_port: int,
    attempts: int,
) -> dict[str, Any]:
    transport = UrlLibTransport(
        proxy_url=f"http://127.0.0.1:{proxy_port}"
    )
    client = OkxRestClient(
        load_okx_credentials("demo"),
        simulated=True,
        transport=transport,
        timeout=10,
    )
    successes = 0
    errors: list[str] = []
    for attempt in range(attempts):
        try:
            client.sync_server_time()
            client.balance()
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)
        else:
            successes += 1
        if attempt + 1 < attempts:
            time.sleep(1)
    return {
        "private_read_ok": successes == attempts,
        "private_read_successes": successes,
        "errors": errors,
    }


def _wait_for_reloaded_proxy(
    *,
    runtime_core_path: Path,
    previous_process_id: int,
    timeout: float = 20,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process_ids = _listener_pids(_ACTIVE_PROXY_PORT)
        if len(process_ids) == 1:
            process_id = next(iter(process_ids))
            if (
                process_id != previous_process_id
                and _process_path(process_id) == runtime_core_path
            ):
                return process_id
        time.sleep(0.25)
    raise TimeoutError("固定代理未在限定时间内加载新配置")


def _activate(
    *,
    runtime_directory: Path,
    active_config: dict[str, Any],
    metadata: dict[str, Any],
    attempts: int,
) -> dict[str, Any]:
    active_path = runtime_directory / "config.json"
    runtime_core_path = (runtime_directory / "sing-box.exe").resolve()
    process_ids = _listener_pids(_ACTIVE_PROXY_PORT)
    if len(process_ids) != 1:
        raise RuntimeError("正式固定代理必须恰好有一个监听进程")
    previous_process_id = next(iter(process_ids))
    if _process_path(previous_process_id) != runtime_core_path:
        raise RuntimeError("10981 监听者不是 PA Agent 固定代理，拒绝覆盖")
    previous_config = active_path.read_bytes()
    metadata_path = runtime_directory / "metadata.json"
    previous_metadata = (
        metadata_path.read_bytes()
        if metadata_path.is_file()
        else None
    )
    _atomic_write_json(active_path, active_config)
    try:
        os.kill(previous_process_id, signal.SIGTERM)
        reloaded_process_id = _wait_for_reloaded_proxy(
            runtime_core_path=runtime_core_path,
            previous_process_id=previous_process_id,
        )
        verification = _probe_existing_proxy(
            proxy_port=_ACTIVE_PROXY_PORT,
            attempts=attempts,
        )
        if not verification["private_read_ok"]:
            raise RuntimeError("新固定代理的私有只读复核失败")
        _atomic_write_json(metadata_path, metadata)
    except Exception:
        _atomic_write_bytes(active_path, previous_config)
        if previous_metadata is not None:
            _atomic_write_bytes(metadata_path, previous_metadata)
        else:
            metadata_path.unlink(missing_ok=True)
        current_process_ids = _listener_pids(_ACTIVE_PROXY_PORT)
        for process_id in current_process_ids:
            if _process_path(process_id) == runtime_core_path:
                os.kill(process_id, signal.SIGTERM)
        _wait_for_reloaded_proxy(
            runtime_core_path=runtime_core_path,
            previous_process_id=(
                next(iter(current_process_ids))
                if current_process_ids
                else previous_process_id
            ),
        )
        raise
    return {
        **verification,
        "proxy_process_id": reloaded_process_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2rayn-root", type=Path, required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--probe-port", type=_probe_port, default=10982)
    parser.add_argument("--attempts", type=_positive_attempts, default=3)
    parser.add_argument("--runtime-directory", type=Path, required=True)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()

    root = args.v2rayn_root.resolve()
    runtime_directory = args.runtime_directory.resolve()
    runtime_directory.mkdir(parents=True, exist_ok=True)
    profile = _profile(
        root / "guiConfigs" / "guiNDB.db",
        args.profile_id,
    )
    template_path = root / "binConfigs" / "config.json"
    template = json.loads(template_path.read_text(encoding="utf-8-sig"))
    candidate = _candidate_config(
        template,
        profile,
        listen_port=args.probe_port,
    )
    probe_path = runtime_directory / "probe-config.json"
    _atomic_write_json(probe_path, candidate)
    try:
        result = _probe(
            core_path=root / "bin" / "sing_box" / "sing-box.exe",
            config_path=probe_path,
            proxy_port=args.probe_port,
            attempts=args.attempts,
        )
    finally:
        probe_path.unlink(missing_ok=True)
    summary = {
        "profile_id": args.profile_id,
        "node_label": (
            f"{profile['SubRemarks']} / {profile['Remarks']}"
        ),
        "recorded_delay_ms": int(profile["Delay"]),
        **result,
        "activated": False,
    }
    if args.activate:
        if not result.get("private_read_ok"):
            raise RuntimeError("私有只读接口未成功，禁止激活该节点")
        active_config = copy.deepcopy(candidate)
        active_config["inbounds"][0].update(
            {
                "tag": "pa-agent-okx",
                "listen_port": 10981,
            }
        )
        metadata = {
            "schema_version": 1,
            "node_label": summary["node_label"],
            "profile_id": args.profile_id,
            "listen_host": "127.0.0.1",
            "listen_port": 10981,
            "source_config_sha256": hashlib.sha256(
                template_path.read_bytes()
            ).hexdigest(),
            "generated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z",
                time.localtime(),
            ),
        }
        activation = _activate(
            runtime_directory=runtime_directory,
            active_config=active_config,
            metadata=metadata,
            attempts=args.attempts,
        )
        summary["activation_verification"] = activation
        summary["activated"] = True
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
