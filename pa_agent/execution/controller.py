"""GUI-side execution controller.

This module can create durable plans and enqueue bounded commands, but it never
constructs a broker adapter or sends a broker request.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pa_agent.config.paths import (
    EXECUTION_CONTROL_DB_PATH,
    EXECUTION_DB_PATH,
    PROJECT_ROOT,
)
from pa_agent.execution.credentials import (
    hard_live_gate_enabled,
    okx_live_gate_enabled,
    paper_trading_gate_enabled,
)
from pa_agent.execution.errors import LiveTradingDisabled, PreflightError
from pa_agent.execution.models import ExecutionRecord, ExecutionState
from pa_agent.execution.plan_builder import (
    build_execution_plan,
    execution_route_fingerprint,
)
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker_protocol import (
    WorkerCommand,
    WorkerCommandAction,
    WorkerCommandStatus,
    WorkerState,
)
from pa_agent.execution.worker_store import WorkerStore

_ARM_CONFIRMATION = "启用实盘交易"
_PAPER_ARM_CONFIRMATION = "启用模拟交易"
_LEASE_TTL_SECONDS = 30
_LEASE_RENEW_INTERVAL_SECONDS = 10
_HEARTBEAT_STALE_SECONDS = 10
_MIN_RECONCILE_STALE_SECONDS = 30
_WINDOWS_SERVICE_NAME = "PAAgentExecutionWorker"


class ExecutionController:
    """Create plans, issue short authority leases and enqueue worker commands."""

    def __init__(
        self,
        *,
        settings,
        pending_writer,
        event_bus=None,
        store: ExecutionStore | None = None,
        worker_store: WorkerStore | None = None,
        worker_launcher: Callable[[], None] | None = None,
        gate_checker: Callable[[], bool] | None = None,
        paper_gate_checker: Callable[[], bool] | None = None,
        okx_live_gate_checker: Callable[[], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._pending_writer = pending_writer
        self._event_bus = event_bus
        self._store = store or ExecutionStore(
            EXECUTION_DB_PATH,
            schema_mode="defer",
        )
        self._worker_store = worker_store or WorkerStore(
            EXECUTION_CONTROL_DB_PATH
        )
        self._worker_launcher = worker_launcher or self._spawn_worker
        self._gate_checker = gate_checker or hard_live_gate_enabled
        self._paper_gate_checker = (
            paper_gate_checker or paper_trading_gate_enabled
        )
        self._okx_live_gate_checker = (
            okx_live_gate_checker or okx_live_gate_enabled
        )
        self._logger = logger or logging.getLogger(__name__)
        self._requester_id = f"gui-{uuid.uuid4()}"
        self._lease_id = ""
        self._lease_worker_id = ""
        self._lease_fingerprint = ""
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._seen_revisions: dict[str, int] = {}
        self._seen_snapshots: dict[tuple[str, str], str] = {}
        self._last_worker_warning = ""
        self._lock = threading.RLock()

    @property
    def execution_store(self) -> ExecutionStore:
        """只读暴露现有执行账本，供工作台读取层使用。"""
        return self._store

    @property
    def worker_store(self) -> WorkerStore:
        """只读暴露现有 Worker 控制库，供工作台读取层使用。"""
        return self._worker_store

    def _selected_route_identity(self) -> tuple[str, str, str]:
        execution = self._settings.execution
        broker = str(execution.selected_broker)
        if broker == "longbridge":
            account = str(execution.longbridge.preferred_account)
            environment = "demo" if account == "paper" else "live"
            return broker, environment, account
        environment = "demo" if bool(execution.okx.simulated) else "live"
        return broker, environment, "okx"

    def _selected_fingerprint(self) -> str:
        broker, _environment, _account = self._selected_route_identity()
        return execution_route_fingerprint(self._settings, broker)

    def arm_confirmation_text(self) -> str:
        _broker, environment, _account = self._selected_route_identity()
        return (
            _PAPER_ARM_CONFIRMATION
            if environment == "demo"
            else _ARM_CONFIRMATION
        )

    def _require_environment_gate(self, broker: str, environment: str) -> None:
        if environment == "demo":
            if not self._paper_gate_checker():
                raise LiveTradingDisabled(
                    "共享 env 中 PA_AGENT_PAPER_TRADING_ENABLED 不是 true"
                )
            return
        if not self._gate_checker():
            raise LiveTradingDisabled(
                "共享 env 中 PA_AGENT_LIVE_TRADING_ENABLED 不是 true"
            )
        if broker == "okx" and not self._okx_live_gate_checker():
            raise LiveTradingDisabled(
                "共享 env 中 OKX_LIVE_ENABLED 不是 true"
            )

    def _current_worker_id(self) -> str:
        heartbeat = self._worker_store.latest_heartbeat()
        if heartbeat is None:
            raise LiveTradingDisabled("交易后台尚未启动")
        if self._worker_store.is_heartbeat_stale(
            heartbeat.worker_id,
            stale_after_seconds=_HEARTBEAT_STALE_SECONDS,
        ):
            raise LiveTradingDisabled("交易后台心跳已停止")
        if heartbeat.state not in {
            WorkerState.RUNNING,
            WorkerState.RECONCILING,
        }:
            raise LiveTradingDisabled(
                f"交易后台当前状态为 {heartbeat.state.value}，尚不能新增风险"
            )
        if heartbeat.last_successful_reconcile_at is None:
            raise LiveTradingDisabled("交易后台尚未完成首次券商对账")
        if self._worker_store.is_reconcile_stale(
            heartbeat.worker_id,
            stale_after_seconds=self._reconcile_stale_seconds(),
        ):
            raise LiveTradingDisabled("交易后台券商对账已陈旧，禁止新增风险")
        return heartbeat.worker_id

    def _reconcile_stale_seconds(self) -> int:
        return max(
            _MIN_RECONCILE_STALE_SECONDS,
            int(
                float(
                    getattr(
                        self._settings.execution,
                        "poll_interval_seconds",
                        1,
                    )
                )
                * 3
            ),
        )

    @property
    def is_armed(self) -> bool:
        with self._lock:
            if not self._lease_id:
                return False
            try:
                broker, environment, account = self._selected_route_identity()
                fingerprint = self._selected_fingerprint()
                worker_id = self._current_worker_id()
                self._require_environment_gate(broker, environment)
            except Exception:  # noqa: BLE001
                return False
            if (
                worker_id != self._lease_worker_id
                or fingerprint != self._lease_fingerprint
            ):
                return False
            return self._worker_store.is_new_risk_authorized(
                self._lease_id,
                worker_id=worker_id,
                config_fingerprint=fingerprint,
                requester=self._requester_id,
                broker=broker,
                environment=environment,
                account=account,
            )

    def arm(self, confirmation: str) -> None:
        with self._lock:
            expected = self.arm_confirmation_text()
            if str(confirmation).strip() != expected:
                raise LiveTradingDisabled("交易启用确认文字不匹配")
            if not bool(self._settings.execution.enabled):
                raise LiveTradingDisabled("执行模块尚未在 PA 配置中启用")
            broker, environment, account = self._selected_route_identity()
            self._require_environment_gate(broker, environment)
            worker_id = self._current_worker_id()
            fingerprint = self._selected_fingerprint()
            lease = self._worker_store.grant_new_risk_lease(
                worker_id=worker_id,
                config_fingerprint=fingerprint,
                requester=self._requester_id,
                broker=broker,
                environment=environment,
                account=account,
                ttl_seconds=_LEASE_TTL_SECONDS,
            )
            if lease is None:
                raise LiveTradingDisabled(
                    "已有其他 PA 会话持有新增风险授权，请先停用或等待其短租约到期"
                )
            self._lease_id = lease.lease_id
            self._lease_worker_id = lease.worker_id
            self._lease_fingerprint = lease.config_fingerprint
        self._emit_armed(True)

    def disarm(self) -> None:
        with self._lock:
            lease_id = self._lease_id
            self._lease_id = ""
            self._lease_worker_id = ""
            self._lease_fingerprint = ""
            if lease_id:
                self._worker_store.revoke_new_risk_lease(lease_id)
        self._emit_armed(False)

    def reload_settings(self, settings=None) -> None:
        self.disarm()
        if settings is not None:
            self._settings = settings

    def prepare_analysis(
        self,
        record,
        *,
        is_demo_replay: bool = False,
    ) -> ExecutionRecord:
        path = Path(self._pending_writer.full_path(record))
        plan = build_execution_plan(
            record,
            self._settings,
            record_path=path,
            is_demo_replay=is_demo_replay,
        )
        execution, created = self._store.create(plan)
        self._emit_record(execution)
        codex_live_requires_review = False
        if plan.environment == "live":
            from pa_agent.ai.provider_capabilities import (
                resolve_provider_capability,
            )

            codex_live_requires_review = (
                resolve_provider_capability(
                    self._settings.provider
                ).client_kind
                == "codex_cli"
            )
            if created and codex_live_requires_review:
                self._store.append_event(
                    execution.id,
                    "human_review_required",
                    {
                        "reason": "codex_subscription_live_trade",
                        "auto_execute_blocked": True,
                    },
                )
        if (
            created
            and bool(self._settings.execution.auto_execute)
            and self.is_armed
            and not codex_live_requires_review
        ):
            self.submit(execution.id)
        return execution

    @staticmethod
    def _record_route(record: ExecutionRecord) -> tuple[str, str, str]:
        return (
            record.plan.broker,
            record.plan.environment,
            record.selected_account or record.plan.requested_account,
        )

    def _execution_command(
        self,
        action: WorkerCommandAction,
        execution_id: str,
        *,
        reason_code: str = "",
    ) -> WorkerCommand:
        record = self._store.get(execution_id)
        if record is None:
            raise KeyError(f"未知 execution id: {execution_id}")
        broker, environment, account = self._record_route(record)
        lease_id = ""
        if action is WorkerCommandAction.SUBMIT:
            if not self.is_armed:
                raise LiveTradingDisabled(
                    "新增风险授权租约不存在、已过期或与执行账户不一致"
                )
            if record.plan.config_fingerprint != self._lease_fingerprint:
                raise LiveTradingDisabled(
                    "待执行计划来自旧交易配置，必须按当前配置重新生成分析计划"
                )
            if self._record_route(record) != self._selected_route_identity():
                raise LiveTradingDisabled(
                    "待执行计划的券商、环境或账户与当前新增风险授权不一致"
                )
            lease_id = self._lease_id
        command, _created = self._worker_store.enqueue(
            action=action,
            execution_id=record.id,
            requester=self._requester_id,
            broker=broker,
            environment=environment,
            account=account,
            new_risk_lease_id=lease_id,
            reason_code=reason_code,
        )
        return command

    def submit(self, execution_id: str) -> WorkerCommand:
        return self._execution_command(
            WorkerCommandAction.SUBMIT,
            execution_id,
        )

    def cancel_entry(self, execution_id: str) -> WorkerCommand:
        return self._execution_command(
            WorkerCommandAction.CANCEL_ENTRY,
            execution_id,
        )

    def request_exit(
        self,
        execution_id: str,
        *,
        reason: str = "主动离场",
    ) -> WorkerCommand:
        reason_code = "manual" if reason == "主动离场" else "requested"
        return self._execution_command(
            WorkerCommandAction.REQUEST_EXIT,
            execution_id,
            reason_code=reason_code,
        )

    def refresh_account(
        self,
        execution_id: str | None = None,
    ) -> WorkerCommand:
        if execution_id:
            record = self._store.get(execution_id)
            if record is None:
                raise KeyError(f"未知 execution id: {execution_id}")
            broker, environment, account = self._record_route(record)
        else:
            broker, environment, account = self._selected_route_identity()
        command, _created = self._worker_store.enqueue(
            action=WorkerCommandAction.REFRESH_ACCOUNT,
            execution_id=execution_id or "",
            requester=self._requester_id,
            broker=broker,
            environment=environment,
            account=account,
        )
        return command

    def latest_execution(self) -> ExecutionRecord | None:
        rows = self._store.list_recent(limit=1)
        return rows[0] if rows else None

    def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        """Return one durable execution without exposing the writable store."""
        return self._store.get(execution_id)

    def list_recent(self, *, limit: int = 30) -> list[ExecutionRecord]:
        return self._store.list_recent(limit=limit)

    def list_active(self) -> list[ExecutionRecord]:
        return self._store.list_active()

    def events(self, execution_id: str):
        return self._store.events(execution_id)

    def latest_account_snapshot(self, broker: str, account_profile: str):
        return self._store.latest_account_snapshot(broker, account_profile)

    def get_command(self, command_id: str) -> WorkerCommand | None:
        return self._worker_store.get_command(command_id)

    def reconcile(self) -> WorkerCommand:
        broker, environment, account = self._selected_route_identity()
        command, _created = self._worker_store.enqueue(
            action=WorkerCommandAction.RECONCILE,
            requester=self._requester_id,
            broker=broker,
            environment=environment,
            account=account,
        )
        return command

    def wait_for_worker(self, *, timeout: float = 10.0):
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            heartbeat = self._worker_store.latest_heartbeat()
            if (
                heartbeat is not None
                and heartbeat.state is WorkerState.NEEDS_ATTENTION
            ):
                raise LiveTradingDisabled(
                    "交易后台需要人工处理："
                    f"{heartbeat.last_error_code or 'unknown'}"
                )
            if (
                heartbeat is not None
                and heartbeat.state
                in {WorkerState.RUNNING, WorkerState.RECONCILING}
                and not self._worker_store.is_heartbeat_stale(
                    heartbeat.worker_id,
                    stale_after_seconds=_HEARTBEAT_STALE_SECONDS,
                )
                and heartbeat.last_successful_reconcile_at is not None
                and not self._worker_store.is_reconcile_stale(
                    heartbeat.worker_id,
                    stale_after_seconds=self._reconcile_stale_seconds(),
                )
            ):
                return heartbeat
            time.sleep(0.05)
        raise LiveTradingDisabled("交易后台未在限定时间内进入可用状态")

    def wait_for_command(
        self,
        command_id: str,
        *,
        timeout: float = 30.0,
    ) -> WorkerCommand:
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            command = self._worker_store.get_command(command_id)
            if command is None:
                raise KeyError(f"未知 worker command id: {command_id}")
            if command.status in {
                WorkerCommandStatus.SUCCEEDED,
                WorkerCommandStatus.FAILED,
                WorkerCommandStatus.UNCERTAIN,
            }:
                return command
            time.sleep(0.05)
        raise TimeoutError(f"交易命令等待超时: {command_id}")

    def wait_for_reconcile(
        self,
        *,
        after: datetime | None = None,
        timeout: float = 30.0,
    ) -> datetime:
        """Wait for the worker's own global reconciliation; enqueue no command."""
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            heartbeat = self._worker_store.latest_heartbeat()
            if (
                heartbeat is not None
                and heartbeat.state is WorkerState.NEEDS_ATTENTION
            ):
                raise LiveTradingDisabled(
                    "交易后台需要人工处理："
                    f"{heartbeat.last_error_code or 'unknown'}"
                )
            if (
                heartbeat is not None
                and heartbeat.last_successful_reconcile_at is not None
                and (
                    after is None
                    or heartbeat.last_successful_reconcile_at > after
                )
                and not self._worker_store.is_heartbeat_stale(
                    heartbeat.worker_id,
                    stale_after_seconds=_HEARTBEAT_STALE_SECONDS,
                )
            ):
                return heartbeat.last_successful_reconcile_at
            time.sleep(0.05)
        raise TimeoutError("等待交易后台完成下一轮券商对账超时")

    def latest_successful_reconcile_at(self) -> datetime | None:
        heartbeat = self._worker_store.latest_heartbeat()
        return (
            heartbeat.last_successful_reconcile_at
            if heartbeat is not None
            else None
        )

    def worker_health_snapshot(self) -> dict[str, object]:
        """Return process and reconciliation health as separate read-only facts."""
        heartbeat = self._worker_store.latest_heartbeat()
        if heartbeat is None:
            return {
                "available": False,
                "process_healthy": False,
                "reconcile_healthy": False,
                "state": "missing",
                "last_seen_at": None,
                "last_successful_reconcile_at": None,
                "last_error_code": "",
            }
        process_healthy = not self._worker_store.is_heartbeat_stale(
            heartbeat.worker_id,
            stale_after_seconds=_HEARTBEAT_STALE_SECONDS,
        )
        reconcile_healthy = (
            heartbeat.last_successful_reconcile_at is not None
            and not self._worker_store.is_reconcile_stale(
                heartbeat.worker_id,
                stale_after_seconds=self._reconcile_stale_seconds(),
            )
        )
        return {
            "available": True,
            "process_healthy": process_healthy,
            "reconcile_healthy": reconcile_healthy,
            "state": heartbeat.state.value,
            "last_seen_at": heartbeat.last_seen_at,
            "last_successful_reconcile_at": (
                heartbeat.last_successful_reconcile_at
            ),
            "last_error_code": heartbeat.last_error_code,
        }

    def expire_unsubmitted(
        self,
        execution_id: str,
        *,
        reason: str,
    ) -> ExecutionRecord:
        """Locally cancel a READY plan that has never reached a broker."""
        current = self._store.get(execution_id)
        if current is None:
            raise KeyError(f"未知 execution id: {execution_id}")
        if current.state is not ExecutionState.READY:
            raise PreflightError(
                f"当前状态 {current.state.value} 不能按未提交计划作废"
            )
        if (
            current.preflight is not None
            or current.client_order_id
            or current.broker_order_id
            or current.filled_quantity != 0
        ):
            raise PreflightError(
                "执行记录已有预检、订单号或成交，禁止按未提交计划作废"
            )
        expired = current.model_copy(
            update={
                "state": ExecutionState.CANCELED,
                "state_reason": str(reason),
                "last_error": "",
                "needs_attention": False,
            }
        )
        saved = self._store.save(
            expired,
            event_kind="ready_expired",
            event_payload={"reason": str(reason)},
        )
        self._emit_record(saved)
        return saved

    def start_monitoring(self) -> None:
        """Start the worker if needed and a read-only GUI status poller."""
        self._worker_launcher()
        with self._lock:
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return
            self._stop_event.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                name="pa-execution-controller-poller",
                daemon=True,
            )
            self._poll_thread.start()

    def stop_monitoring(self, timeout: float = 5.0) -> None:
        """Stop only the GUI poller; the headless worker keeps managing risk."""
        self.disarm()
        self._stop_event.set()
        thread = self._poll_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, min(float(timeout), 5.0)))
        if thread is None or not thread.is_alive():
            self._poll_thread = None

    def _renew_lease(self) -> None:
        with self._lock:
            if not self._lease_id:
                return
            try:
                broker, environment, _account = self._selected_route_identity()
                self._require_environment_gate(broker, environment)
                worker_id = self._current_worker_id()
                fingerprint = self._selected_fingerprint()
                if (
                    worker_id != self._lease_worker_id
                    or fingerprint != self._lease_fingerprint
                ):
                    raise LiveTradingDisabled(
                        "交易后台或当前执行配置已变化"
                    )
            except Exception:  # noqa: BLE001
                lease_id = self._lease_id
                self._lease_id = ""
                self._lease_worker_id = ""
                self._lease_fingerprint = ""
                self._worker_store.revoke_new_risk_lease(
                    lease_id,
                    failure_code="lease_health_check_failed",
                )
                self._emit_armed(False)
                return
            renewed = self._worker_store.renew_new_risk_lease(
                self._lease_id,
                worker_id=self._lease_worker_id,
                config_fingerprint=self._lease_fingerprint,
                requester=self._requester_id,
                ttl_seconds=_LEASE_TTL_SECONDS,
            )
            if renewed is None:
                self._lease_id = ""
                self._lease_worker_id = ""
                self._lease_fingerprint = ""
                self._emit_armed(False)

    def _poll_loop(self) -> None:
        next_renewal = time.monotonic() + _LEASE_RENEW_INTERVAL_SECONDS
        while not self._stop_event.wait(1.0):
            try:
                if time.monotonic() >= next_renewal:
                    self._renew_lease()
                    next_renewal = (
                        time.monotonic() + _LEASE_RENEW_INTERVAL_SECONDS
                    )
                self._poll_worker_health()
                self._poll_records()
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("交易状态轮询失败：%s", exc)

    def _poll_worker_health(self) -> None:
        heartbeat = self._worker_store.latest_heartbeat()
        warning = ""
        if heartbeat is None:
            warning = "交易后台尚未产生心跳"
        elif self._worker_store.is_heartbeat_stale(
            heartbeat.worker_id,
            stale_after_seconds=_HEARTBEAT_STALE_SECONDS,
        ):
            warning = "交易后台心跳已停止"
        elif heartbeat.state is WorkerState.NEEDS_ATTENTION:
            warning = (
                "交易后台需要人工处理："
                f"{heartbeat.last_error_code or 'unknown'}"
            )
        elif self._worker_store.is_reconcile_stale(
            heartbeat.worker_id,
            stale_after_seconds=self._reconcile_stale_seconds(),
        ):
            warning = "交易后台进程仍在运行，但券商对账已长时间没有成功"
        if warning and warning != self._last_worker_warning:
            self._emit_error(warning)
        self._last_worker_warning = warning

    def _poll_records(self) -> None:
        for record in self._store.list_recent(limit=50):
            if self._seen_revisions.get(record.id) != record.revision:
                self._seen_revisions[record.id] = record.revision
                self._emit_record(record)
            broker, profile = self._snapshot_route(record)
            snapshot = self._store.latest_account_snapshot(broker, profile)
            if snapshot is None:
                continue
            key = (broker, profile)
            if self._seen_snapshots.get(key) == snapshot.captured_at:
                continue
            self._seen_snapshots[key] = snapshot.captured_at
            self._emit_account(snapshot)
        broker, environment, account = self._selected_route_identity()
        profile = (
            ("okx-demo" if environment == "demo" else "okx-live")
            if broker == "okx"
            else account
        )
        snapshot = self._store.latest_account_snapshot(broker, profile)
        if snapshot is not None:
            key = (broker, profile)
            if self._seen_snapshots.get(key) != snapshot.captured_at:
                self._seen_snapshots[key] = snapshot.captured_at
                self._emit_account(snapshot)

    @staticmethod
    def _snapshot_route(record: ExecutionRecord) -> tuple[str, str]:
        if record.plan.broker == "okx":
            return "okx", (
                "okx-demo"
                if record.plan.environment == "demo"
                else "okx-live"
            )
        return (
            "longbridge",
            record.selected_account or record.plan.requested_account,
        )

    def _emit_armed(self, armed: bool) -> None:
        bus = self._event_bus
        if bus is not None and hasattr(bus, "emit_execution_armed"):
            bus.emit_execution_armed(bool(armed))

    def _emit_record(self, record: ExecutionRecord) -> None:
        bus = self._event_bus
        if bus is not None and hasattr(bus, "emit_execution_update"):
            bus.emit_execution_update(record)

    def _emit_account(self, snapshot) -> None:
        bus = self._event_bus
        if bus is not None and hasattr(bus, "emit_account_update"):
            bus.emit_account_update(snapshot)

    def _emit_error(self, message: str) -> None:
        bus = self._event_bus
        if bus is not None and hasattr(bus, "emit_execution_error"):
            bus.emit_execution_error(message)

    def _spawn_worker(self) -> None:
        heartbeat = self._worker_store.latest_heartbeat()
        if (
            heartbeat is not None
            and not self._worker_store.is_heartbeat_stale(
                heartbeat.worker_id,
                stale_after_seconds=_HEARTBEAT_STALE_SECONDS,
            )
        ):
            return
        if self._request_installed_windows_service_start():
            return
        command = [
            sys.executable,
            "-m",
            "pa_agent.execution.worker",
        ]
        kwargs: dict = {
            "cwd": str(PROJECT_ROOT),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
        subprocess.Popen(command, **kwargs)

    def _request_installed_windows_service_start(self) -> bool:
        """Ask an installed WinSW service to start; never bypass it."""
        if not self._is_windows_service_platform():
            return False
        service_control = self._windows_service_control_path()
        if not service_control.is_file():
            self._logger.error("Windows 服务控制程序 sc.exe 不可用")
            self._emit_error(
                "无法确认交易后台 Windows 服务是否安装; "
                "已停止备用 Worker, 交易保持停用"
            )
            return True
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            query = subprocess.run(
                [
                    str(service_control),
                    "query",
                    _WINDOWS_SERVICE_NAME,
                ],
                capture_output=True,
                check=False,
                timeout=10,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._logger.error("无法查询交易后台 Windows 服务: %s", exc)
            self._emit_error(
                "无法确认交易后台 Windows 服务状态; 已停止备用 Worker, 交易保持停用"
            )
            return True
        query_output = bytes(query.stdout or b"") + bytes(query.stderr or b"")
        if query.returncode != 0:
            if b"1060" in query_output:
                return False
            self._logger.error(
                "查询交易后台 Windows 服务失败, 退出码 %s",
                query.returncode,
            )
            self._emit_error(
                "交易后台 Windows 服务状态未知; 已停止备用 Worker, 交易保持停用"
            )
            return True
        try:
            started = subprocess.run(
                [
                    str(service_control),
                    "start",
                    _WINDOWS_SERVICE_NAME,
                ],
                capture_output=True,
                check=False,
                timeout=10,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._logger.error("无法启动交易后台 Windows 服务: %s", exc)
            self._emit_error(
                "无法启动交易后台 Windows 服务; 交易保持停用"
            )
            return True
        start_output = bytes(started.stdout or b"") + bytes(
            started.stderr or b""
        )
        if started.returncode != 0 and b"1056" not in start_output:
            self._logger.error(
                "启动交易后台 Windows 服务失败, 退出码 %s",
                started.returncode,
            )
            self._emit_error(
                "交易后台 Windows 服务启动失败; 交易保持停用"
            )
        return True

    @staticmethod
    def _is_windows_service_platform() -> bool:
        return os.name == "nt"

    @staticmethod
    def _windows_service_control_path() -> Path:
        return (
            Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
            / "System32"
            / "sc.exe"
        )
