"""Independent SQLite control plane for the headless execution worker."""
from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pa_agent.execution.worker_protocol import (
    NewRiskLease,
    WorkerCommand,
    WorkerCommandAction,
    WorkerCommandStatus,
    WorkerHeartbeat,
    WorkerState,
)

_WORKER_SCHEMA_VERSION = 1
_SAFE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]*$")
_TERMINAL_COMMAND_STATES = frozenset(
    {
        WorkerCommandStatus.SUCCEEDED,
        WorkerCommandStatus.FAILED,
        WorkerCommandStatus.UNCERTAIN,
    }
)


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


class WorkerStore:
    """Durable worker queue, heartbeat and NEW_RISK authority.

    The database path is deliberately mandatory. Callers must explicitly pass
    an isolated or configured execution database and cannot accidentally open
    the production ledger through an implicit default.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        self._clock = clock or _system_utc_now
        self._lock = threading.RLock()
        self._initialise()

    @property
    def path(self) -> Path:
        return self._path

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("worker clock 必须返回带时区时间")
        return value.astimezone(UTC)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value).astimezone(UTC) if value else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _validated_safe_code(value: str, *, field_name: str) -> str:
        normalized = value.strip()
        if (
            len(normalized) > 128
            or _SAFE_CODE_PATTERN.fullmatch(normalized) is None
        ):
            raise ValueError(
                f"{field_name} 只能保存不含密钥和原始异常的简短代码"
            )
        return normalized

    def _initialise(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS worker_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                version = connection.execute(
                    """
                    SELECT value FROM worker_meta
                    WHERE key='worker_schema_version'
                    """
                ).fetchone()
                if version is None:
                    connection.execute(
                        """
                        INSERT INTO worker_meta(key, value)
                        VALUES ('worker_schema_version', ?)
                        """,
                        (str(_WORKER_SCHEMA_VERSION),),
                    )
                else:
                    try:
                        parsed_version = int(version["value"])
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError("worker schema 版本无效") from exc
                    if parsed_version != _WORKER_SCHEMA_VERSION:
                        raise RuntimeError("不支持的 worker schema 版本")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS worker_commands (
                        id TEXT PRIMARY KEY,
                        scope_key TEXT NOT NULL,
                        action TEXT NOT NULL,
                        execution_id TEXT NOT NULL,
                        requester TEXT NOT NULL,
                        broker TEXT NOT NULL,
                        environment TEXT NOT NULL,
                        account TEXT NOT NULL,
                        new_risk_lease_id TEXT NOT NULL,
                        reason_code TEXT NOT NULL,
                        status TEXT NOT NULL,
                        worker_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        result_code TEXT NOT NULL,
                        failure_code TEXT NOT NULL,
                        CHECK(action IN (
                            'submit', 'cancel_entry', 'request_exit',
                            'refresh_account', 'reconcile'
                        )),
                        CHECK(status IN (
                            'pending', 'running', 'succeeded',
                            'failed', 'uncertain'
                        ))
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_worker_commands_one_active
                    ON worker_commands(scope_key, action)
                    WHERE status IN ('pending', 'running')
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_worker_commands_claim
                    ON worker_commands(status, created_at, id)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS worker_new_risk_lease (
                        slot TEXT PRIMARY KEY CHECK(slot='NEW_RISK'),
                        lease_id TEXT NOT NULL UNIQUE,
                        worker_id TEXT NOT NULL,
                        config_fingerprint TEXT NOT NULL,
                        requester TEXT NOT NULL,
                        broker TEXT NOT NULL,
                        environment TEXT NOT NULL,
                        account TEXT NOT NULL,
                        granted_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS worker_heartbeats (
                        worker_id TEXT PRIMARY KEY,
                        pid INTEGER NOT NULL,
                        started_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        last_successful_reconcile_at TEXT,
                        state TEXT NOT NULL,
                        last_error_code TEXT NOT NULL,
                        CHECK(state IN (
                            'starting', 'reconciling', 'running',
                            'needs_attention', 'stopping'
                        ))
                    )
                    """
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _scope_key(
        *,
        execution_id: str,
        broker: str,
        environment: str,
        account: str,
    ) -> str:
        if execution_id:
            return f"execution:{execution_id}"
        return "\x1f".join(
            (
                "route",
                broker.strip().lower(),
                environment.strip().lower(),
                account.strip(),
            )
        )

    @staticmethod
    def _row_to_command(row: sqlite3.Row | None) -> WorkerCommand | None:
        if row is None:
            return None
        return WorkerCommand(
            id=row["id"],
            action=row["action"],
            execution_id=row["execution_id"],
            requester=row["requester"],
            broker=row["broker"],
            environment=row["environment"],
            account=row["account"],
            new_risk_lease_id=row["new_risk_lease_id"],
            reason_code=row["reason_code"],
            status=row["status"],
            worker_id=row["worker_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=WorkerStore._parse_time(row["started_at"]),
            finished_at=WorkerStore._parse_time(row["finished_at"]),
            result_code=row["result_code"],
            failure_code=row["failure_code"],
        )

    @staticmethod
    def _row_to_lease(row: sqlite3.Row | None) -> NewRiskLease | None:
        if row is None:
            return None
        return NewRiskLease(
            lease_id=row["lease_id"],
            worker_id=row["worker_id"],
            config_fingerprint=row["config_fingerprint"],
            requester=row["requester"],
            broker=row["broker"],
            environment=row["environment"],
            account=row["account"],
            granted_at=datetime.fromisoformat(row["granted_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    @staticmethod
    def _row_to_heartbeat(row: sqlite3.Row | None) -> WorkerHeartbeat | None:
        if row is None:
            return None
        return WorkerHeartbeat(
            worker_id=row["worker_id"],
            pid=row["pid"],
            started_at=datetime.fromisoformat(row["started_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            last_successful_reconcile_at=WorkerStore._parse_time(
                row["last_successful_reconcile_at"]
            ),
            state=row["state"],
            last_error_code=row["last_error_code"],
        )

    def enqueue(
        self,
        *,
        action: WorkerCommandAction,
        execution_id: str = "",
        requester: str,
        broker: str,
        environment: str,
        account: str,
        new_risk_lease_id: str = "",
        reason_code: str = "",
        command_id: str | None = None,
    ) -> tuple[WorkerCommand, bool]:
        """Enqueue one command or return its existing pending/running twin."""
        now = self._now()
        candidate = WorkerCommand(
            id=command_id or str(uuid.uuid4()),
            action=action,
            execution_id=execution_id,
            requester=requester,
            broker=broker,
            environment=environment,
            account=account,
            new_risk_lease_id=new_risk_lease_id,
            reason_code=reason_code,
            created_at=now,
        )
        scope_key = self._scope_key(
            execution_id=candidate.execution_id,
            broker=candidate.broker,
            environment=candidate.environment,
            account=candidate.account,
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM worker_commands
                    WHERE scope_key=? AND action=?
                      AND status IN ('pending', 'running', 'uncertain')
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """,
                    (scope_key, candidate.action.value),
                ).fetchone()
                if existing is not None:
                    if existing["status"] == WorkerCommandStatus.UNCERTAIN.value:
                        raise RuntimeError(
                            "同一 execution 和动作存在 uncertain 命令，"
                            "完成只读对账和人工处置前禁止新建"
                        )
                    connection.execute("COMMIT")
                    existing_command = self._row_to_command(existing)
                    assert existing_command is not None
                    return existing_command, False
                if candidate.action is WorkerCommandAction.SUBMIT:
                    lease = connection.execute(
                        """
                        SELECT * FROM worker_new_risk_lease
                        WHERE slot='NEW_RISK' AND lease_id=?
                          AND requester=?
                          AND broker=? AND environment=? AND account=?
                          AND expires_at>?
                        """,
                        (
                            candidate.new_risk_lease_id,
                            candidate.requester,
                            candidate.broker,
                            candidate.environment,
                            candidate.account,
                            self._iso(now),
                        ),
                    ).fetchone()
                    if lease is None:
                        raise PermissionError(
                            "submit 命令缺少当前路由的有效 NEW_RISK 租约"
                        )
                connection.execute(
                    """
                    INSERT INTO worker_commands(
                        id, scope_key, action, execution_id, requester,
                        broker, environment, account, new_risk_lease_id,
                        reason_code,
                        status, worker_id, created_at, started_at, finished_at,
                        result_code, failure_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, NULL, NULL, '', '')
                    """,
                    (
                        candidate.id,
                        scope_key,
                        candidate.action.value,
                        candidate.execution_id,
                        candidate.requester,
                        candidate.broker,
                        candidate.environment,
                        candidate.account,
                        candidate.new_risk_lease_id,
                        candidate.reason_code,
                        candidate.status.value,
                        self._iso(candidate.created_at),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return candidate, True

    def _fail_unauthorized_submits(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
        worker_id: str,
    ) -> None:
        connection.execute(
            """
            UPDATE worker_commands
            SET status='failed', finished_at=?, failure_code='new_risk_expired'
            WHERE status='pending' AND action='submit'
              AND NOT EXISTS (
                  SELECT 1 FROM worker_new_risk_lease lease
                  WHERE lease.slot='NEW_RISK'
                    AND lease.lease_id=worker_commands.new_risk_lease_id
                    AND lease.worker_id=?
                    AND lease.requester=worker_commands.requester
                    AND lease.broker=worker_commands.broker
                    AND lease.environment=worker_commands.environment
                    AND lease.account=worker_commands.account
                    AND lease.expires_at>?
              )
            """,
            (self._iso(now), worker_id, self._iso(now)),
        )

    def claim_next(self, *, worker_id: str) -> WorkerCommand | None:
        """Atomically claim the oldest pending command."""
        clean_worker_id = worker_id.strip()
        if not clean_worker_id:
            raise ValueError("worker_id 不能为空")
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._fail_unauthorized_submits(
                    connection,
                    now=now,
                    worker_id=clean_worker_id,
                )
                row = connection.execute(
                    """
                    SELECT * FROM worker_commands
                    WHERE status='pending'
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                cursor = connection.execute(
                    """
                    UPDATE worker_commands
                    SET status='running', worker_id=?, started_at=?
                    WHERE id=? AND status='pending'
                    """,
                    (clean_worker_id, self._iso(now), row["id"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("worker command claim 并发冲突")
                claimed = connection.execute(
                    "SELECT * FROM worker_commands WHERE id=?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._row_to_command(claimed)

    def finish_command(
        self,
        command_id: str,
        *,
        worker_id: str,
        status: WorkerCommandStatus,
        result_code: str = "",
        failure_code: str = "",
    ) -> WorkerCommand:
        """Finish an owned running command with a durable terminal state."""
        if status not in _TERMINAL_COMMAND_STATES:
            raise ValueError("命令只能结束为 succeeded、failed 或 uncertain")
        result_code = self._validated_safe_code(
            result_code,
            field_name="result_code",
        )
        failure_code = self._validated_safe_code(
            failure_code,
            field_name="failure_code",
        )
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE worker_commands
                    SET status=?, finished_at=?, result_code=?, failure_code=?
                    WHERE id=? AND status='running' AND worker_id=?
                    """,
                    (
                        status.value,
                        self._iso(now),
                        result_code,
                        failure_code,
                        command_id,
                        worker_id.strip(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("命令不存在、未运行或不属于当前 worker")
                row = connection.execute(
                    "SELECT * FROM worker_commands WHERE id=?",
                    (command_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        command = self._row_to_command(row)
        assert command is not None
        return command

    def recover_inflight(
        self,
        *,
        failure_code: str = "worker_restarted",
    ) -> int:
        """Recover inherited commands without replaying possible broker writes."""
        failure_code = self._validated_safe_code(
            failure_code,
            field_name="failure_code",
        )
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                retryable = connection.execute(
                    """
                    UPDATE worker_commands
                    SET status='failed', finished_at=?,
                        failure_code='worker_restarted_read_retryable'
                    WHERE status='running' AND action='refresh_account'
                    """,
                    (self._iso(now),),
                )
                cursor = connection.execute(
                    """
                    UPDATE worker_commands
                    SET status='uncertain', finished_at=?, failure_code=?
                    WHERE status='running'
                    """,
                    (self._iso(now), failure_code),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return int(retryable.rowcount) + int(cursor.rowcount)

    def get_command(self, command_id: str) -> WorkerCommand | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worker_commands WHERE id=?",
                (command_id,),
            ).fetchone()
        return self._row_to_command(row)

    def list_commands(self) -> list[WorkerCommand]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM worker_commands
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        return [
            command
            for row in rows
            if (command := self._row_to_command(row)) is not None
        ]

    @staticmethod
    def _validate_ttl(ttl_seconds: int) -> int:
        ttl = int(ttl_seconds)
        if not 5 <= ttl <= 300:
            raise ValueError("NEW_RISK 租约时长必须在 5 到 300 秒之间")
        return ttl

    def grant_new_risk_lease(
        self,
        *,
        worker_id: str,
        config_fingerprint: str,
        requester: str,
        broker: str,
        environment: str,
        account: str,
        ttl_seconds: int,
    ) -> NewRiskLease | None:
        """Grant the single NEW_RISK slot unless a live lease already owns it."""
        now = self._now()
        lease = NewRiskLease(
            lease_id=str(uuid.uuid4()),
            worker_id=worker_id,
            config_fingerprint=config_fingerprint,
            requester=requester,
            broker=broker,
            environment=environment,
            account=account,
            granted_at=now,
            expires_at=now + timedelta(seconds=self._validate_ttl(ttl_seconds)),
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    """
                    SELECT * FROM worker_new_risk_lease
                    WHERE slot='NEW_RISK'
                    """
                ).fetchone()
                if (
                    current is not None
                    and current["expires_at"] > self._iso(now)
                ):
                    connection.execute("COMMIT")
                    return None
                connection.execute(
                    "DELETE FROM worker_new_risk_lease WHERE slot='NEW_RISK'"
                )
                connection.execute(
                    """
                    INSERT INTO worker_new_risk_lease(
                        slot, lease_id, worker_id, config_fingerprint,
                        requester, broker, environment,
                        account, granted_at, expires_at
                    ) VALUES ('NEW_RISK', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease.lease_id,
                        lease.worker_id,
                        lease.config_fingerprint,
                        lease.requester,
                        lease.broker,
                        lease.environment,
                        lease.account,
                        self._iso(lease.granted_at),
                        self._iso(lease.expires_at),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return lease

    def renew_new_risk_lease(
        self,
        lease_id: str,
        *,
        worker_id: str,
        config_fingerprint: str,
        requester: str,
        ttl_seconds: int,
    ) -> NewRiskLease | None:
        now = self._now()
        expires_at = now + timedelta(
            seconds=self._validate_ttl(ttl_seconds)
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE worker_new_risk_lease
                    SET expires_at=?
                    WHERE slot='NEW_RISK' AND lease_id=?
                      AND worker_id=? AND config_fingerprint=?
                      AND requester=? AND expires_at>?
                    """,
                    (
                        self._iso(expires_at),
                        lease_id.strip(),
                        worker_id.strip(),
                        config_fingerprint.strip(),
                        requester.strip(),
                        self._iso(now),
                    ),
                )
                if cursor.rowcount != 1:
                    connection.execute("COMMIT")
                    return None
                row = connection.execute(
                    """
                    SELECT * FROM worker_new_risk_lease
                    WHERE slot='NEW_RISK'
                    """
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._row_to_lease(row)

    def current_new_risk_lease(self) -> NewRiskLease | None:
        """Return the current non-expired lease, without adopting its authority."""
        now = self._now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM worker_new_risk_lease
                WHERE slot='NEW_RISK' AND expires_at>?
                """,
                (self._iso(now),),
            ).fetchone()
        return self._row_to_lease(row)

    def revoke_current_new_risk_lease(
        self,
        *,
        failure_code: str = "new_risk_revoked",
    ) -> bool:
        lease = self.current_new_risk_lease()
        if lease is None:
            return False
        return self.revoke_new_risk_lease(
            lease.lease_id,
            failure_code=failure_code,
        )

    def revoke_new_risk_lease(
        self,
        lease_id: str,
        *,
        failure_code: str = "new_risk_revoked",
    ) -> bool:
        """Revoke authority and fail its not-yet-claimed submit commands."""
        failure_code = self._validated_safe_code(
            failure_code,
            field_name="failure_code",
        )
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                lease = connection.execute(
                    """
                    SELECT * FROM worker_new_risk_lease
                    WHERE slot='NEW_RISK' AND lease_id=?
                    """,
                    (lease_id.strip(),),
                ).fetchone()
                if lease is None:
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    """
                    UPDATE worker_commands
                    SET status='failed', finished_at=?, failure_code=?
                    WHERE status='pending' AND action='submit'
                      AND new_risk_lease_id=?
                      AND broker=? AND environment=? AND account=?
                    """,
                    (
                        self._iso(now),
                        failure_code,
                        lease["lease_id"],
                        lease["broker"],
                        lease["environment"],
                        lease["account"],
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM worker_new_risk_lease
                    WHERE slot='NEW_RISK' AND lease_id=?
                    """,
                    (lease_id.strip(),),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def is_new_risk_authorized(
        self,
        lease_id: str,
        *,
        worker_id: str,
        config_fingerprint: str,
        requester: str,
        broker: str,
        environment: str,
        account: str,
    ) -> bool:
        now = self._now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM worker_new_risk_lease
                WHERE slot='NEW_RISK' AND lease_id=?
                  AND worker_id=? AND config_fingerprint=?
                  AND requester=?
                  AND broker=? AND environment=? AND account=?
                  AND expires_at>?
                """,
                (
                    lease_id.strip(),
                    worker_id.strip(),
                    config_fingerprint.strip(),
                    requester.strip(),
                    broker.strip().lower(),
                    environment.strip().lower(),
                    account.strip(),
                    self._iso(now),
                ),
            ).fetchone()
        return row is not None

    def record_heartbeat(
        self,
        *,
        worker_id: str,
        pid: int,
        state: WorkerState,
        last_successful_reconcile_at: datetime | None = None,
        last_error_code: str = "",
    ) -> WorkerHeartbeat:
        """Upsert one worker heartbeat while preserving its start time."""
        now = self._now()
        clean_worker_id = worker_id.strip()
        if not clean_worker_id:
            raise ValueError("worker_id 不能为空")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM worker_heartbeats WHERE worker_id=?
                    """,
                    (clean_worker_id,),
                ).fetchone()
                started_at = (
                    datetime.fromisoformat(existing["started_at"])
                    if existing is not None
                    else now
                )
                previous_reconcile = (
                    self._parse_time(
                        existing["last_successful_reconcile_at"]
                    )
                    if existing is not None
                    else None
                )
                heartbeat = WorkerHeartbeat(
                    worker_id=clean_worker_id,
                    pid=pid,
                    started_at=started_at,
                    last_seen_at=now,
                    last_successful_reconcile_at=(
                        last_successful_reconcile_at
                        if last_successful_reconcile_at is not None
                        else previous_reconcile
                    ),
                    state=state,
                    last_error_code=last_error_code,
                )
                connection.execute(
                    """
                    INSERT INTO worker_heartbeats(
                        worker_id, pid, started_at, last_seen_at,
                        last_successful_reconcile_at, state, last_error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        pid=excluded.pid,
                        last_seen_at=excluded.last_seen_at,
                        last_successful_reconcile_at=
                            excluded.last_successful_reconcile_at,
                        state=excluded.state,
                        last_error_code=excluded.last_error_code
                    """,
                    (
                        heartbeat.worker_id,
                        heartbeat.pid,
                        self._iso(heartbeat.started_at),
                        self._iso(heartbeat.last_seen_at),
                        (
                            self._iso(
                                heartbeat.last_successful_reconcile_at
                            )
                            if heartbeat.last_successful_reconcile_at
                            is not None
                            else None
                        ),
                        heartbeat.state.value,
                        heartbeat.last_error_code,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return heartbeat

    def get_heartbeat(self, worker_id: str) -> WorkerHeartbeat | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worker_heartbeats WHERE worker_id=?",
                (worker_id.strip(),),
            ).fetchone()
        return self._row_to_heartbeat(row)

    def latest_heartbeat(self) -> WorkerHeartbeat | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM worker_heartbeats
                ORDER BY last_seen_at DESC, worker_id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._row_to_heartbeat(row)

    def is_heartbeat_stale(
        self,
        worker_id: str,
        *,
        stale_after_seconds: int,
    ) -> bool:
        if stale_after_seconds < 1:
            raise ValueError("heartbeat 陈旧阈值必须至少为 1 秒")
        heartbeat = self.get_heartbeat(worker_id)
        if heartbeat is None:
            return True
        cutoff = self._now() - timedelta(seconds=stale_after_seconds)
        return heartbeat.last_seen_at <= cutoff

    def is_reconcile_stale(
        self,
        worker_id: str,
        *,
        stale_after_seconds: int,
    ) -> bool:
        """Distinguish a live process from stalled broker reconciliation."""
        if stale_after_seconds < 1:
            raise ValueError("对账陈旧阈值必须至少为 1 秒")
        heartbeat = self.get_heartbeat(worker_id)
        if heartbeat is None:
            return True
        reference = (
            heartbeat.last_successful_reconcile_at
            or heartbeat.started_at
        )
        cutoff = self._now() - timedelta(seconds=stale_after_seconds)
        return reference <= cutoff
