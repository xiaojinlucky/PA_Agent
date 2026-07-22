"""Headless execution worker for durable broker commands."""
from __future__ import annotations

import inspect
import logging
import os
import re
import signal
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from pa_agent.config.settings import load_settings
from pa_agent.execution.errors import (
    BrokerRejected,
    CredentialError,
    LiveTradingDisabled,
    PreflightError,
)
from pa_agent.execution.models import ExecutionState
from pa_agent.execution.worker_protocol import (
    WorkerCommand,
    WorkerCommandAction,
    WorkerCommandStatus,
    WorkerState,
)
from pa_agent.execution.worker_store import WorkerStore

_WRITE_ACTIONS = frozenset(
    {
        WorkerCommandAction.SUBMIT,
        WorkerCommandAction.CANCEL_ENTRY,
        WorkerCommandAction.REQUEST_EXIT,
    }
)
_EXECUTION_ACTIONS = _WRITE_ACTIONS
_DEFINITELY_NOT_WRITTEN = (
    BrokerRejected,
    LiveTradingDisabled,
    PreflightError,
    CredentialError,
)
_HEARTBEAT_STALE_SECONDS = 10
_RECONCILE_STALE_SECONDS = 30
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|secret|token|passphrase|password)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9._+/=-]{24,}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WorkerAlreadyRunning(RuntimeError):
    """Another process owns this worker's lock file."""


class _CommandRejected(RuntimeError):
    """A deterministic command validation failure before any broker write."""


class _CommandUncertain(RuntimeError):
    """A broker write may have happened and must never be replayed."""


class _ReconciliationNeedsAttention(RuntimeError):
    """Reconciliation completed but an active execution is unsafe."""


class WorkerNewRiskAuthority:
    """Bind one claimed submit command to the final broker-write check."""

    def __init__(
        self,
        store: WorkerStore,
        worker_id: str,
        *,
        heartbeat_stale_seconds: int = _HEARTBEAT_STALE_SECONDS,
        reconcile_stale_seconds: int = _RECONCILE_STALE_SECONDS,
    ) -> None:
        self._store = store
        self._worker_id = worker_id
        self._heartbeat_stale_seconds = int(heartbeat_stale_seconds)
        self._reconcile_stale_seconds = int(reconcile_stale_seconds)
        self._local = threading.local()

    def bind(self, command: WorkerCommand) -> None:
        self._local.command = command

    def clear(self) -> None:
        try:
            del self._local.command
        except AttributeError:
            pass

    def is_authorized(self, plan, effective_account: str) -> bool:
        command = getattr(self._local, "command", None)
        if (
            command is None
            or command.action is not WorkerCommandAction.SUBMIT
            or command.execution_id != plan.id
        ):
            return False
        heartbeat = self._store.get_heartbeat(self._worker_id)
        if (
            heartbeat is None
            or heartbeat.state is not WorkerState.RUNNING
            or heartbeat.last_successful_reconcile_at is None
            or self._store.is_heartbeat_stale(
                self._worker_id,
                stale_after_seconds=self._heartbeat_stale_seconds,
            )
            or self._store.is_reconcile_stale(
                self._worker_id,
                stale_after_seconds=self._reconcile_stale_seconds,
            )
        ):
            return False
        lease_valid = self._store.is_new_risk_authorized(
            command.new_risk_lease_id,
            worker_id=self._worker_id,
            config_fingerprint=plan.config_fingerprint,
            requester=command.requester,
            broker=plan.broker,
            environment=plan.environment,
            account=command.account,
        )
        if not lease_valid:
            return False
        actual_account = str(effective_account or "").strip()
        if actual_account == command.account:
            return True
        return bool(
            plan.broker == "longbridge"
            and command.account == "intraday"
            and actual_account == "comprehensive"
            and bool(plan.allow_account_fallback)
        )


def _masked_exception(exc: BaseException) -> str:
    """Return an exception description that cannot expose common credentials."""
    if isinstance(
        exc,
        (
            _CommandRejected,
            _CommandUncertain,
            _ReconciliationNeedsAttention,
        ),
    ):
        return f"{type(exc).__name__}: {exc}"
    text = str(exc)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _LONG_TOKEN.sub("<redacted>", text)
    return f"{type(exc).__name__}: {text}"


class ExecutionWorker:
    """Run execution commands independently from the GUI process.

    ``start`` only acquires ownership and performs startup recovery. The caller
    can then use ``run_once`` in tests or ``run_forever`` in a dedicated
    process. No GUI lifecycle signal is consulted, so closing the GUI cannot
    stop this worker.
    """

    def __init__(
        self,
        *,
        store: WorkerStore,
        service: Any,
        settings: Any | None = None,
        settings_path: Path | None = None,
        settings_loader: Callable[[Path], Any] = load_settings,
        lock_path: Path | None = None,
        worker_id: str | None = None,
        poll_interval_seconds: float | None = None,
        heartbeat_interval_seconds: float = 1.0,
        clock: Callable[[], datetime] = _utc_now,
        logger: logging.Logger | None = None,
        new_risk_authority: WorkerNewRiskAuthority | None = None,
    ) -> None:
        self.store = store
        self.service = service
        self.settings = settings
        self.settings_path = (
            Path(settings_path) if settings_path is not None else None
        )
        self._settings_loader = settings_loader
        self.worker_id = (worker_id or str(uuid.uuid4())).strip()
        if not self.worker_id:
            raise ValueError("worker_id 不能为空")
        self._new_risk_authority = (
            new_risk_authority
            or WorkerNewRiskAuthority(
                self.store,
                self.worker_id,
                reconcile_stale_seconds=max(
                    _RECONCILE_STALE_SECONDS,
                    int(self._configured_poll_interval(settings) * 3),
                ),
            )
        )
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._poll_interval_seconds = (
            float(poll_interval_seconds)
            if poll_interval_seconds is not None
            else self._configured_poll_interval(settings)
        )
        if self._poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于 0")
        self._heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        if self._heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds 必须大于 0")
        self._lock_path = (
            Path(lock_path)
            if lock_path is not None
            else Path(f"{self.store.path}.worker.lock")
        )
        self._file_lock = FileLock(str(self._lock_path))
        self._started = False
        self._stop_event = threading.Event()
        self._heartbeat_stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_lock = threading.RLock()
        self._heartbeat_state = WorkerState.STARTING
        self._heartbeat_error_code = ""
        self._last_successful_reconcile_at: datetime | None = None
        self._settings_reload_error_code = ""
        self._settings_signature = self._read_settings_signature()

    @staticmethod
    def _configured_poll_interval(settings: Any | None) -> float:
        try:
            return float(settings.execution.poll_interval_seconds)
        except (AttributeError, TypeError, ValueError):
            return 1.0

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("worker clock 必须返回带时区时间")
        return value.astimezone(UTC)

    @staticmethod
    def _supported_kwargs(callable_object: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
        """Pass compatibility fields only when the installed store supports them."""
        parameters = inspect.signature(callable_object).parameters
        if any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return kwargs
        return {key: value for key, value in kwargs.items() if key in parameters}

    def _record_heartbeat(
        self,
        state: WorkerState,
        *,
        last_error_code: str = "",
    ) -> None:
        kwargs = self._supported_kwargs(
            self.store.record_heartbeat,
            worker_id=self.worker_id,
            pid=os.getpid(),
            state=state,
            last_reconcile_at=self._last_successful_reconcile_at,
            last_successful_reconcile_at=self._last_successful_reconcile_at,
            last_error_code=last_error_code,
        )
        self.store.record_heartbeat(**kwargs)

    def _set_heartbeat(
        self,
        state: WorkerState,
        *,
        last_error_code: str = "",
    ) -> None:
        if (
            state is WorkerState.RUNNING
            and self._settings_reload_error_code
        ):
            state = WorkerState.NEEDS_ATTENTION
            last_error_code = self._settings_reload_error_code
        with self._heartbeat_lock:
            self._heartbeat_state = state
            self._heartbeat_error_code = last_error_code
            self._record_heartbeat(
                state,
                last_error_code=last_error_code,
            )
        if state is WorkerState.NEEDS_ATTENTION:
            try:
                self._revoke_lease(
                    require_owned=True,
                    reason_code="worker_needs_attention",
                )
            except Exception as exc:  # noqa: BLE001
                self._log_failure("lease_revoke", exc)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop_event.wait(
            self._heartbeat_interval_seconds
        ):
            try:
                with self._heartbeat_lock:
                    self._record_heartbeat(
                        self._heartbeat_state,
                        last_error_code=self._heartbeat_error_code,
                    )
            except Exception as exc:  # noqa: BLE001
                self._log_failure("heartbeat", exc)

    def _start_heartbeat_thread(self) -> None:
        self._heartbeat_stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"execution-heartbeat-{self.worker_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat_thread(self) -> None:
        self._heartbeat_stop_event.set()
        thread = self._heartbeat_thread
        self._heartbeat_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self._heartbeat_interval_seconds * 2))

    def _read_settings_signature(self) -> tuple[int, int] | None:
        if self.settings_path is None:
            return None
        try:
            stat = self.settings_path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _reload_settings_if_changed(self) -> bool:
        if self.settings_path is None:
            return False
        signature = self._read_settings_signature()
        if signature == self._settings_signature:
            return False
        loaded = self._settings_loader(self.settings_path)
        self.service.reload_settings(loaded, revoke_new_risk=False)
        self.settings = loaded
        self._poll_interval_seconds = self._configured_poll_interval(loaded)
        self._settings_signature = signature
        self._settings_reload_error_code = ""
        return True

    def _current_lease(self) -> Any | None:
        getter = getattr(self.store, "current_new_risk_lease", None)
        return getter() if getter is not None else None

    def _revoke_lease(
        self,
        *,
        require_owned: bool,
        reason_code: str,
    ) -> bool:
        lease = self._current_lease()
        if lease is None:
            return False
        owner = str(getattr(lease, "worker_id", "") or "").strip()
        if require_owned and owner and owner != self.worker_id:
            return False
        kwargs = self._supported_kwargs(
            self.store.revoke_new_risk_lease,
            failure_code=reason_code,
            reason_code=reason_code,
            worker_id=self.worker_id,
        )
        return bool(
            self.store.revoke_new_risk_lease(
                lease.lease_id,
                **kwargs,
            )
        )

    def start(self) -> None:
        """Acquire the singleton lock and perform fail-closed recovery."""
        if self._started:
            return
        try:
            self._file_lock.acquire(timeout=0)
        except Timeout as exc:
            raise WorkerAlreadyRunning(
                f"执行 Worker 已在运行：{self._lock_path}"
            ) from exc
        try:
            self._stop_event.clear()
            self._set_heartbeat(WorkerState.STARTING)
            self.store.recover_inflight(failure_code="worker_restarted")
            self._revoke_lease(
                require_owned=False,
                reason_code="worker_restarted",
            )
            disarm = getattr(self.service, "disarm", None)
            if disarm is not None:
                disarm(revoke_external=False)
            self._started = True
            self._start_heartbeat_thread()
            try:
                self._run_reconcile()
            except Exception as exc:  # noqa: BLE001
                self._log_failure("startup_reconcile", exc)
                self._set_heartbeat(
                    WorkerState.NEEDS_ATTENTION,
                    last_error_code=type(exc).__name__[:128],
                )
            else:
                self._set_heartbeat(WorkerState.RUNNING)
        except Exception:
            self._started = False
            self._stop_heartbeat_thread()
            self._file_lock.release()
            raise

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("ExecutionWorker 尚未启动")

    def _load_execution(self, command: WorkerCommand) -> Any:
        record = self.service.store.get(command.execution_id)
        if record is None:
            raise _CommandRejected("execution_record_missing")
        trusted_account = (
            str(record.selected_account).strip()
            or str(record.plan.requested_account).strip()
        )
        trusted_route = (
            str(record.plan.broker).strip().lower(),
            str(record.plan.environment).strip().lower(),
            trusted_account,
        )
        command_route = (
            str(command.broker).strip().lower(),
            str(command.environment).strip().lower(),
            str(command.account).strip(),
        )
        if command_route != trusted_route:
            raise _CommandRejected("execution_route_mismatch")
        return record

    def _require_submit_lease(
        self,
        command: WorkerCommand,
        record: Any,
    ) -> None:
        kwargs = self._supported_kwargs(
            self.store.is_new_risk_authorized,
            broker=command.broker,
            environment=command.environment,
            account=command.account,
            worker_id=self.worker_id,
            config_fingerprint=record.plan.config_fingerprint,
            requester=command.requester,
        )
        authorized = self.store.is_new_risk_authorized(
            command.new_risk_lease_id,
            **kwargs,
        )
        if not authorized:
            raise _CommandRejected("new_risk_not_authorized")

    def _dispatch(self, command: WorkerCommand) -> str:
        record = None
        if command.action in _EXECUTION_ACTIONS:
            record = self._load_execution(command)
        if command.action is WorkerCommandAction.SUBMIT:
            self._require_submit_lease(command, record)
            result = self.service.submit(command.execution_id)
            self._validate_write_result(command.action, result)
        elif command.action is WorkerCommandAction.CANCEL_ENTRY:
            result = self.service.cancel_entry(command.execution_id)
            self._validate_write_result(command.action, result)
        elif command.action is WorkerCommandAction.REQUEST_EXIT:
            result = self.service.request_exit(
                command.execution_id,
                reason=command.reason_code,
            )
            self._validate_write_result(command.action, result)
        elif command.action is WorkerCommandAction.REFRESH_ACCOUNT:
            if command.execution_id:
                self._load_execution(command)
                self.service.refresh_account(command.execution_id)
            else:
                execution = self.settings.execution
                daily_broker = str(execution.selected_broker)
                if daily_broker == "longbridge":
                    daily_account = str(
                        execution.longbridge.preferred_account
                    )
                    daily_environment = (
                        "demo" if daily_account == "paper" else "live"
                    )
                else:
                    daily_account = "okx"
                    daily_environment = (
                        "demo"
                        if bool(execution.okx.simulated)
                        else "live"
                    )
                command_route = (
                    command.broker,
                    command.environment,
                    command.account,
                )
                daily_route = (
                    daily_broker,
                    daily_environment,
                    daily_account,
                )
                if command_route == daily_route:
                    self.service.refresh_account()
                elif command_route == ("okx", "demo", "okx"):
                    self.service.refresh_account_route(
                        broker=command.broker,
                        environment=command.environment,
                        account=command.account,
                    )
                else:
                    raise _CommandRejected(
                        "account_refresh_route_mismatch"
                    )
        elif command.action is WorkerCommandAction.RECONCILE:
            self._run_reconcile()
        else:  # pragma: no cover - enum validation normally makes this unreachable.
            raise _CommandRejected("unsupported_action")
        return f"{command.action.value}_completed"

    @staticmethod
    def _validate_write_result(
        action: WorkerCommandAction,
        result: Any,
    ) -> None:
        """Translate durable execution outcomes into command outcomes."""
        state = getattr(result, "state", None)
        if state is None:
            return
        broker_state = getattr(result, "broker_state", {})
        broker_state = broker_state if isinstance(broker_state, dict) else {}
        if (
            state is ExecutionState.UNKNOWN
            or bool(broker_state.get("write_unknown"))
        ):
            raise _CommandUncertain(f"{action.value}_result_uncertain")
        if bool(getattr(result, "needs_attention", False)):
            raise _ReconciliationNeedsAttention(
                f"{action.value}_result_needs_attention"
            )
        if action is WorkerCommandAction.SUBMIT and state in {
            ExecutionState.READY,
            ExecutionState.BLOCKED,
            ExecutionState.CANCELED,
            ExecutionState.REJECTED,
            ExecutionState.ERROR,
        }:
            raise _CommandRejected(f"submit_state_{state.value}")
        if action is WorkerCommandAction.CANCEL_ENTRY:
            return
        if action is WorkerCommandAction.REQUEST_EXIT and state not in {
            ExecutionState.EXIT_PENDING,
            ExecutionState.CLOSED,
        }:
            raise _CommandRejected(f"request_exit_state_{state.value}")

    def _run_reconcile(self) -> None:
        self._set_heartbeat(WorkerState.RECONCILING)
        monitor_once = getattr(self.service, "monitor_once", None)
        if callable(monitor_once):
            monitor_once()
        else:
            self.service.reconcile_once()
        active = self.service.store.list_active()
        if any(
            bool(getattr(record, "needs_attention", False))
            or getattr(record, "state", None)
            in {ExecutionState.UNKNOWN, ExecutionState.ERROR}
            for record in active
        ):
            raise _ReconciliationNeedsAttention(
                "active_execution_needs_attention"
            )
        with self._heartbeat_lock:
            self._last_successful_reconcile_at = self._now()

    def _log_failure(self, command_id: str, exc: BaseException) -> None:
        self._logger.error(
            "Execution worker command %s failed: %s",
            command_id,
            _masked_exception(exc),
        )

    @staticmethod
    def _failure_status(
        action: WorkerCommandAction,
        exc: BaseException,
    ) -> WorkerCommandStatus:
        if isinstance(exc, _CommandUncertain):
            return WorkerCommandStatus.UNCERTAIN
        if isinstance(
            exc,
            (
                _CommandRejected,
                _ReconciliationNeedsAttention,
                *_DEFINITELY_NOT_WRITTEN,
            ),
        ):
            return WorkerCommandStatus.FAILED
        if action in _WRITE_ACTIONS:
            return WorkerCommandStatus.UNCERTAIN
        return WorkerCommandStatus.FAILED

    def run_once(self) -> WorkerCommand | None:
        """Reload settings, claim at most one command, and persist its result."""
        self._require_started()
        reload_error: Exception | None = None
        try:
            self._reload_settings_if_changed()
        except Exception as exc:  # noqa: BLE001
            reload_error = exc
            self._settings_reload_error_code = type(exc).__name__[:128]
            self._log_failure("settings_reload", exc)
            self._set_heartbeat(
                WorkerState.NEEDS_ATTENTION,
                last_error_code=self._settings_reload_error_code,
            )
        command = self.store.claim_next(worker_id=self.worker_id)
        if command is None:
            return None
        self._new_risk_authority.bind(command)
        try:
            if (
                reload_error is not None
                and command.action is WorkerCommandAction.SUBMIT
            ):
                raise _CommandRejected("settings_reload_failed")
            result_code = self._dispatch(command)
        except Exception as exc:  # noqa: BLE001
            self._log_failure(command.id, exc)
            status = self._failure_status(command.action, exc)
            failure_code = (
                str(exc)
                if isinstance(
                    exc,
                    (
                        _CommandRejected,
                        _CommandUncertain,
                        _ReconciliationNeedsAttention,
                    ),
                )
                else type(exc).__name__
            )[:128]
            finished = self.store.finish_command(
                command.id,
                worker_id=self.worker_id,
                status=status,
                failure_code=failure_code,
            )
            self._set_heartbeat(
                (
                    WorkerState.NEEDS_ATTENTION
                    if (
                        reload_error is not None
                        or
                        status is WorkerCommandStatus.UNCERTAIN
                        or isinstance(
                            exc,
                            _ReconciliationNeedsAttention,
                        )
                    )
                    else WorkerState.RUNNING
                ),
                last_error_code=failure_code,
            )
            return finished
        finally:
            self._new_risk_authority.clear()
        finished = self.store.finish_command(
            command.id,
            worker_id=self.worker_id,
            status=WorkerCommandStatus.SUCCEEDED,
            result_code=result_code,
        )
        self._set_heartbeat(
            (
                WorkerState.NEEDS_ATTENTION
                if reload_error is not None
                else WorkerState.RUNNING
            ),
            last_error_code=(
                type(reload_error).__name__[:128]
                if reload_error is not None
                else ""
            ),
        )
        return finished

    def run_forever(self) -> None:
        """Run until explicitly stopped by the worker process owner."""
        self.start()
        try:
            while not self._stop_event.is_set():
                command = self.run_once()
                if command is not None:
                    continue
                try:
                    self._run_reconcile()
                except Exception as exc:  # noqa: BLE001
                    self._log_failure("periodic_reconcile", exc)
                    self._set_heartbeat(
                        WorkerState.NEEDS_ATTENTION,
                        last_error_code=type(exc).__name__[:128],
                    )
                else:
                    self._set_heartbeat(WorkerState.RUNNING)
                self._stop_event.wait(self._poll_interval_seconds)
        finally:
            self.close()

    def close(self) -> None:
        """Stop this worker, revoke its risk authority, and release the lock."""
        self._stop_event.set()
        if not self._started:
            return
        try:
            self._set_heartbeat(WorkerState.STOPPING)
            self._stop_heartbeat_thread()
            disarm = getattr(self.service, "disarm", None)
            if disarm is not None:
                disarm(revoke_external=False)
            self._revoke_lease(
                require_owned=True,
                reason_code="worker_stopped",
            )
        finally:
            self._stop_heartbeat_thread()
            self._started = False
            self._file_lock.release()

    def request_stop(self) -> None:
        """Ask the owner loop to stop without releasing the singleton lock."""
        self._stop_event.set()

    stop = request_stop


def _build_default_worker() -> ExecutionWorker:
    from pa_agent.config.paths import (
        EXECUTION_CONTROL_DB_PATH,
        EXECUTION_DB_PATH,
        EXECUTION_WORKER_LOCK_PATH,
        SETTINGS_JSON_PATH,
    )
    from pa_agent.execution.service import ExecutionService
    from pa_agent.execution.store import ExecutionStore
    from pa_agent.util.logging import configure_logging

    configure_logging()
    settings = load_settings(SETTINGS_JSON_PATH)
    worker_store = WorkerStore(EXECUTION_CONTROL_DB_PATH)
    worker_id = str(uuid.uuid4())
    try:
        execution_store = ExecutionStore(
            EXECUTION_DB_PATH,
            schema_mode="require_current",
        )
    except RuntimeError as exc:
        error_code = (
            "execution_schema_migration_required"
            if "显式迁移" in str(exc)
            else "execution_schema_invalid"
        )
        worker_store.record_heartbeat(
            worker_id=worker_id,
            pid=os.getpid(),
            state=WorkerState.NEEDS_ATTENTION,
            last_error_code=error_code,
        )
        logging.getLogger("pa_agent.execution.worker").error(
            "Execution worker startup blocked: %s",
            error_code,
        )
        raise
    authority = WorkerNewRiskAuthority(
        worker_store,
        worker_id,
        reconcile_stale_seconds=max(
            _RECONCILE_STALE_SECONDS,
            int(
                ExecutionWorker._configured_poll_interval(settings) * 3
            ),
        ),
    )
    service = ExecutionService(
        settings=settings,
        pending_writer=None,
        store=execution_store,
        new_risk_authorizer=authority.is_authorized,
        new_risk_revoker=lambda: worker_store.revoke_current_new_risk_lease(
            failure_code="service_disarmed",
        ),
        logger=logging.getLogger("pa_agent.execution.worker"),
    )
    return ExecutionWorker(
        store=worker_store,
        service=service,
        settings=settings,
        settings_path=SETTINGS_JSON_PATH,
        lock_path=EXECUTION_WORKER_LOCK_PATH,
        worker_id=worker_id,
        new_risk_authority=authority,
        logger=logging.getLogger("pa_agent.execution.worker"),
    )


def main() -> int:
    try:
        worker = _build_default_worker()
    except RuntimeError:
        return 3

    def _stop(_signum, _frame) -> None:
        worker.request_stop()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            signal.signal(signal_value, _stop)
    try:
        worker.run_forever()
    except WorkerAlreadyRunning:
        logging.getLogger(__name__).info("Execution worker already running")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
