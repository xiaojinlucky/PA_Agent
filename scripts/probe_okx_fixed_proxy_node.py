"""在独立端口测试一个 v2rayN 节点，不切换主 v2rayN。"""
from __future__ import annotations

import argparse
import base64
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


def _idle_seconds(value: str) -> int:
    seconds = int(value)
    if not 45 <= seconds <= 600:
        raise argparse.ArgumentTypeError(
            "idle-seconds 必须在 45 到 600 之间，"
            "必须超过 AnyTLS 30 秒空闲检查间隔才有耐久意义"
        )
    return seconds


def _idle_cycles(value: str) -> int:
    cycles = int(value)
    if not 0 <= cycles <= 10:
        raise argparse.ArgumentTypeError("idle-cycles 必须在 0 到 10 之间")
    return cycles


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


def _runtime_supervisor_pids(runtime_directory: Path) -> set[int]:
    supervisor_path = (runtime_directory / "run-hidden.ps1").resolve()
    encoded_path = base64.b64encode(
        str(supervisor_path).encode("utf-8")
    ).decode("ascii")
    command = (
        "$target = [Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{encoded_path}')); "
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.ProcessId -ne $PID -and $_.CommandLine -and "
        "$_.CommandLine.IndexOf("
        "$target, [StringComparison]::OrdinalIgnoreCase"
        ") -ge 0 } | ForEach-Object { $_.ProcessId }"
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
        raise RuntimeError("无法核验固定代理守护进程")
    return {
        int(line.strip())
        for line in completed.stdout.splitlines()
        if line.strip().isdigit()
    }


def _stop_runtime_proxy_fail_closed(
    *,
    runtime_directory: Path,
    runtime_core_path: Path,
    known_process_ids: set[int],
) -> None:
    """停止守护与正式代理；旧配置无法恢复时端口必须下线。"""

    errors: list[str] = []
    try:
        supervisor_ids = _runtime_supervisor_pids(runtime_directory)
    except Exception as exc:
        supervisor_ids = set()
        errors.append(f"supervisor_query:{type(exc).__name__}")
    for process_id in supervisor_ids:
        try:
            os.kill(process_id, signal.SIGTERM)
        except OSError as exc:
            errors.append(f"supervisor_stop:{type(exc).__name__}")

    process_ids = set(known_process_ids)
    try:
        process_ids.update(_listener_pids(_ACTIVE_PROXY_PORT))
    except Exception as exc:
        errors.append(f"listener_query:{type(exc).__name__}")
    for process_id in process_ids:
        try:
            is_runtime_core = (
                process_id in known_process_ids
                or _process_path(process_id) == runtime_core_path
            )
            if is_runtime_core:
                os.kill(process_id, signal.SIGTERM)
        except Exception as exc:
            errors.append(f"proxy_stop:{type(exc).__name__}")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            listeners = _listener_pids(_ACTIVE_PROXY_PORT)
        except Exception as exc:
            errors.append(f"offline_verify:{type(exc).__name__}")
            break
        if not listeners:
            return
        time.sleep(0.1)
    raise RuntimeError(
        "旧配置恢复失败后无法证明 10981 已下线"
        + (f"：{','.join(errors)}" if errors else "")
    )


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
                "idle_session_check_interval": "30s",
                "idle_session_timeout": "5m",
                "min_idle_session": 1,
                "tcp_keep_alive": "30s",
                "tcp_keep_alive_interval": "30s",
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


def _read_full_risk_route(client: OkxRestClient) -> dict[str, int | bool]:
    """按生产风险刷新顺序读完所有 OKX 端点。"""

    balance_rows = len(client.balance())
    position_rows = len(client.positions())
    instrument_rows = len(client.instruments("SWAP"))
    client.sync_server_time()
    account_config = client.account_config()
    account_config_ok = all(
        str(account_config.get(field) or "").strip()
        for field in ("uid", "mainUid", "type")
    )
    if not account_config_ok:
        raise RuntimeError("OKX Demo 账户身份字段不完整")
    bill_rows = len(client.account_bills())
    return {
        "balance_rows": balance_rows,
        "position_rows": position_rows,
        "instrument_rows": instrument_rows,
        "account_config_ok": account_config_ok,
        "bill_rows": bill_rows,
    }


def _endurance_verification(
    client: OkxRestClient,
    *,
    idle_cycles: int,
    idle_seconds: int,
    verify_risk_bills: bool,
) -> dict[str, Any]:
    """空闲耐久闸门：每轮先真实空闲，再按生产节奏完整读取。

    紧密连续探测无法暴露 AnyTLS/TLS 会话在空闲后失效的问题；
    这里的空闲时长必须超过 30 秒会话检查间隔，模拟 Worker 60 秒
    自然扫描的真实节奏。
    """

    successes = 0
    errors: list[str] = []
    for cycle in range(idle_cycles):
        time.sleep(idle_seconds)
        try:
            client.sync_server_time()
            if verify_risk_bills:
                _read_full_risk_route(client)
            else:
                client.balance()
        except Exception as exc:
            errors.append(f"cycle{cycle + 1}:{type(exc).__name__}")
        else:
            successes += 1
    return {
        "endurance_cycles": idle_cycles,
        "endurance_idle_seconds": idle_seconds,
        "endurance_successes": successes,
        "endurance_ok": successes == idle_cycles,
        "endurance_errors": errors,
    }


def _probe(
    *,
    core_path: Path,
    config_path: Path,
    proxy_port: int,
    attempts: int,
    verify_risk_bills: bool = False,
    idle_cycles: int = 0,
    idle_seconds: int = 75,
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
        except Exception:
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
        full_risk_evidence: dict[str, int | bool] = {
            "balance_rows": 0,
            "position_rows": 0,
            "instrument_rows": 0,
            "account_config_ok": False,
            "bill_rows": 0,
        }
        risk_bill_successes = 0
        api_codes: list[str] = []
        errors: list[str] = []
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                client.sync_server_time()
            except Exception as exc:
                errors.append(type(exc).__name__)
                continue
            public_successes += 1
            public_latencies.append(
                round((time.monotonic() - started) * 1000)
            )
            try:
                if verify_risk_bills:
                    full_risk_evidence = _read_full_risk_route(client)
                else:
                    full_risk_evidence["balance_rows"] = len(
                        client.balance()
                    )
            except BrokerApiError as exc:
                api_codes.append(exc.code)
            except Exception as exc:
                errors.append(type(exc).__name__)
            else:
                private_successes += 1
                if verify_risk_bills:
                    risk_bill_successes += 1
            if attempt + 1 < attempts:
                time.sleep(1)
        tight_rounds_ok = (
            public_successes == attempts
            and private_successes == attempts
            and (
                not verify_risk_bills
                or risk_bill_successes == attempts
            )
        )
        if idle_cycles > 0 and tight_rounds_ok:
            endurance = _endurance_verification(
                client,
                idle_cycles=idle_cycles,
                idle_seconds=idle_seconds,
                verify_risk_bills=verify_risk_bills,
            )
        else:
            endurance = {
                "endurance_cycles": idle_cycles,
                "endurance_idle_seconds": idle_seconds,
                "endurance_successes": 0,
                "endurance_ok": idle_cycles == 0,
                "endurance_errors": (
                    ["skipped_tight_rounds_failed"]
                    if idle_cycles > 0
                    else []
                ),
            }
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
            **full_risk_evidence,
            "risk_bills_required": verify_risk_bills,
            "risk_bills_ok": (
                not verify_risk_bills
                or risk_bill_successes == attempts
            ),
            "risk_bill_successes": risk_bill_successes,
            **endurance,
            "api_codes": sorted(set(api_codes)),
            "errors": errors,
        }
    except Exception as exc:
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
    verify_risk_bills: bool = False,
    idle_cycles: int = 0,
    idle_seconds: int = 75,
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
    risk_bill_successes = 0
    full_risk_evidence: dict[str, int | bool] = {
        "balance_rows": 0,
        "position_rows": 0,
        "instrument_rows": 0,
        "account_config_ok": False,
        "bill_rows": 0,
    }
    errors: list[str] = []
    for attempt in range(attempts):
        try:
            client.sync_server_time()
            if verify_risk_bills:
                full_risk_evidence = _read_full_risk_route(client)
            else:
                full_risk_evidence["balance_rows"] = len(
                    client.balance()
                )
        except Exception as exc:
            errors.append(type(exc).__name__)
        else:
            successes += 1
            if verify_risk_bills:
                risk_bill_successes += 1
        if attempt + 1 < attempts:
            time.sleep(1)
    tight_rounds_ok = successes == attempts and (
        not verify_risk_bills or risk_bill_successes == attempts
    )
    if idle_cycles > 0 and tight_rounds_ok:
        endurance = _endurance_verification(
            client,
            idle_cycles=idle_cycles,
            idle_seconds=idle_seconds,
            verify_risk_bills=verify_risk_bills,
        )
    else:
        endurance = {
            "endurance_cycles": idle_cycles,
            "endurance_idle_seconds": idle_seconds,
            "endurance_successes": 0,
            "endurance_ok": idle_cycles == 0,
            "endurance_errors": (
                ["skipped_tight_rounds_failed"]
                if idle_cycles > 0
                else []
            ),
        }
    return {
        "private_read_ok": successes == attempts,
        "private_read_successes": successes,
        **full_risk_evidence,
        "risk_bills_required": verify_risk_bills,
        "risk_bills_ok": (
            not verify_risk_bills
            or risk_bill_successes == attempts
        ),
        "risk_bill_successes": risk_bill_successes,
        **endurance,
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
    idle_cycles: int = 0,
    idle_seconds: int = 75,
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
    reloaded_process_id: int | None = None
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
            verify_risk_bills=True,
            idle_cycles=idle_cycles,
            idle_seconds=idle_seconds,
        )
        if (
            not verification["private_read_ok"]
            or not verification["risk_bills_ok"]
            or not verification["endurance_ok"]
        ):
            raise RuntimeError(
                "新固定代理的余额、完整账单分页或空闲耐久复核失败："
                + json.dumps(verification, ensure_ascii=False)
            )
        _atomic_write_json(metadata_path, metadata)
    except Exception:
        rollback_error: Exception | None = None
        current_process_ids: set[int] = set()
        rollback_restored = False
        try:
            _atomic_write_bytes(active_path, previous_config)
            if previous_metadata is not None:
                _atomic_write_bytes(metadata_path, previous_metadata)
            else:
                metadata_path.unlink(missing_ok=True)
            rollback_restored = True
        except Exception as exc:
            rollback_error = exc
        finally:
            known_candidate_ids = (
                {reloaded_process_id}
                if reloaded_process_id is not None
                else set()
            )
            try:
                current_process_ids = _listener_pids(
                    _ACTIVE_PROXY_PORT
                )
            except Exception:
                current_process_ids = set()
            for process_id in known_candidate_ids | current_process_ids:
                try:
                    if (
                        process_id in known_candidate_ids
                        or _process_path(process_id) == runtime_core_path
                    ):
                        os.kill(process_id, signal.SIGTERM)
                except Exception:
                    continue

        if not rollback_restored:
            try:
                _stop_runtime_proxy_fail_closed(
                    runtime_directory=runtime_directory,
                    runtime_core_path=runtime_core_path,
                    known_process_ids=(
                        {reloaded_process_id}
                        if reloaded_process_id is not None
                        else set()
                    ),
                )
            except Exception as shutdown_error:
                raise RuntimeError(
                    "新代理验证失败、旧配置恢复失败，"
                    "且无法证明 10981 已下线"
                ) from shutdown_error
            raise RuntimeError(
                "新代理验证失败，旧配置恢复失败；10981 已下线"
            ) from rollback_error

        try:
            _wait_for_reloaded_proxy(
                runtime_core_path=runtime_core_path,
                previous_process_id=(
                    reloaded_process_id
                    if reloaded_process_id is not None
                    else previous_process_id
                ),
            )
        except Exception as restore_start_error:
            _stop_runtime_proxy_fail_closed(
                runtime_directory=runtime_directory,
                runtime_core_path=runtime_core_path,
                known_process_ids=current_process_ids,
            )
            raise RuntimeError(
                "旧配置已恢复，但旧代理未能重新启动；10981 已下线"
            ) from restore_start_error
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
    parser.add_argument(
        "--verify-risk-bills",
        action="store_true",
        help="额外完整读取 OKX Demo 账单分页",
    )
    parser.add_argument(
        "--idle-seconds",
        type=_idle_seconds,
        default=75,
        help="每个耐久周期的真实空闲秒数，必须超过 AnyTLS 30 秒空闲检查",
    )
    parser.add_argument(
        "--idle-cycles",
        type=_idle_cycles,
        default=2,
        help="空闲耐久周期数；激活时至少 1，0 仅用于快速连通性排查",
    )
    parser.add_argument(
        "--template-config",
        type=Path,
        default=None,
        help=(
            "sing-box 模板配置；默认使用运行目录现有 config.json。"
            "v2rayN 主核心切到 xray 后，其 binConfigs 模板不再是 "
            "sing-box 格式，不能默认依赖"
        ),
    )
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    if args.activate and args.idle_cycles < 1:
        parser.error("激活必须至少通过 1 个空闲耐久周期，不能只靠紧密探测")

    root = args.v2rayn_root.resolve()
    runtime_directory = args.runtime_directory.resolve()
    runtime_directory.mkdir(parents=True, exist_ok=True)
    profile = _profile(
        root / "guiConfigs" / "guiNDB.db",
        args.profile_id,
    )
    template_path = (
        args.template_config.resolve()
        if args.template_config is not None
        else runtime_directory / "config.json"
    )
    if not template_path.is_file():
        raise RuntimeError(
            "sing-box 模板不存在："
            f"{template_path}；请用 --template-config 显式指定一个 "
            "sing-box 格式配置"
        )
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
            verify_risk_bills=(
                args.verify_risk_bills or args.activate
            ),
            idle_cycles=args.idle_cycles,
            idle_seconds=args.idle_seconds,
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
        if (
            not result.get("private_read_ok")
            or not result.get("risk_bills_ok")
            or not result.get("endurance_ok")
        ):
            raise RuntimeError(
                "余额、完整账单分页或空闲耐久未通过，禁止激活该节点"
            )
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
            "template_source": str(template_path),
            "source_config_sha256": hashlib.sha256(
                template_path.read_bytes()
            ).hexdigest(),
            "generated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z",
                time.localtime(),
            ),
        }
        # 候选阶段已通过完整空闲耐久；切换后复核只保留 1 个空闲周期，
        # 缩短探针与 Worker 自然扫描并发打同一 API key 限频的窗口。
        # 切换后的权威耐久判据是 Worker 随后的连续自然扫描。
        activation = _activate(
            runtime_directory=runtime_directory,
            active_config=active_config,
            metadata=metadata,
            attempts=args.attempts,
            idle_cycles=min(args.idle_cycles, 1),
            idle_seconds=args.idle_seconds,
        )
        summary["activation_verification"] = activation
        summary["activated"] = True
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
