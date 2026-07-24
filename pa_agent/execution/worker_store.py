"""Independent SQLite control plane for the headless execution worker."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pa_agent.execution.worker_protocol import (
    NewRiskLease,
    SetLeverageParameters,
    SetLeverageResolutionEvidence,
    SetLeverageResult,
    WorkerCommand,
    WorkerCommandAction,
    WorkerCommandResolution,
    WorkerCommandResolutionEvidence,
    WorkerCommandStatus,
    WorkerHeartbeat,
    WorkerState,
)
from pa_agent.risk.runtime import RiskRuntimeState

_WORKER_SCHEMA_VERSION = 4
_WRITE_ACTION_VALUES = (
    WorkerCommandAction.SUBMIT.value,
    WorkerCommandAction.SET_LEVERAGE.value,
    WorkerCommandAction.CANCEL_ENTRY.value,
    WorkerCommandAction.REQUEST_EXIT.value,
)
_SAFE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]*$")
_TERMINAL_COMMAND_STATES = frozenset(
    {
        WorkerCommandStatus.SUCCEEDED,
        WorkerCommandStatus.FAILED,
        WorkerCommandStatus.UNCERTAIN,
    }
)
_CREATE_WORKER_COMMANDS_SQL = """
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
        parameters_json TEXT NOT NULL,
        status TEXT NOT NULL,
        worker_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        result_code TEXT NOT NULL,
        failure_code TEXT NOT NULL,
        result_json TEXT NOT NULL,
        CHECK(action IN (
            'submit', 'set_leverage', 'cancel_entry', 'request_exit',
            'refresh_account', 'reconcile', 'clear_drawdown_stop'
        )),
        CHECK(status IN (
            'pending', 'running', 'succeeded',
            'failed', 'uncertain'
        ))
    )
"""


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
        self._schema_version = 0
        self._initialise(allow_migration=False)

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

    @property
    def schema_version(self) -> int:
        return self._schema_version

    def migrate_to_current(self, *, worker_lock: object) -> None:
        """Upgrade durable state only while the caller owns the Worker lock."""
        if not bool(getattr(worker_lock, "is_locked", False)):
            raise RuntimeError("Worker 单例锁未持有, 禁止迁移 worker schema")
        self._initialise(allow_migration=True)

    def _initialise(self, *, allow_migration: bool) -> None:
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
                    existing_worker_state = connection.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type='table'
                          AND name LIKE 'worker_%'
                          AND name<>'worker_meta'
                        LIMIT 1
                        """
                    ).fetchone()
                    if existing_worker_state is not None:
                        raise RuntimeError(
                            "worker schema 缺少版本号, 禁止猜测或自动改写"
                        )
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
                    if parsed_version not in {1, 2, 3, _WORKER_SCHEMA_VERSION}:
                        raise RuntimeError("不支持的 worker schema 版本")
                    if (
                        parsed_version != _WORKER_SCHEMA_VERSION
                        and not allow_migration
                    ):
                        self._schema_version = parsed_version
                        connection.execute("COMMIT")
                        return
                has_previous_resolutions = False
                if version is not None and parsed_version == 2:
                    has_previous_resolutions = (
                        connection.execute(
                            """
                            SELECT 1 FROM sqlite_master
                            WHERE type='table'
                              AND name='worker_command_resolutions'
                            """
                        ).fetchone()
                        is not None
                    )
                    if has_previous_resolutions:
                        connection.execute(
                            "ALTER TABLE worker_command_resolutions "
                            "RENAME TO worker_command_resolutions_previous"
                        )
                connection.execute(_CREATE_WORKER_COMMANDS_SQL)
                if version is not None and parsed_version in {1, 2}:
                    connection.execute(
                        "DROP INDEX IF EXISTS idx_worker_commands_one_active"
                    )
                    connection.execute(
                        "DROP INDEX IF EXISTS idx_worker_commands_claim"
                    )
                    connection.execute(
                        "ALTER TABLE worker_commands "
                        "RENAME TO worker_commands_previous"
                    )
                    connection.execute(_CREATE_WORKER_COMMANDS_SQL)
                    if parsed_version == 1:
                        connection.execute(
                            """
                            INSERT INTO worker_commands(
                                id, scope_key, action, execution_id, requester,
                                broker, environment, account, new_risk_lease_id,
                                reason_code, parameters_json, status, worker_id,
                                created_at, started_at, finished_at, result_code,
                                failure_code, result_json
                            )
                            SELECT
                                id, scope_key, action, execution_id, requester,
                                broker, environment, account, new_risk_lease_id,
                                reason_code, 'null', status, worker_id,
                                created_at, started_at, finished_at, result_code,
                                failure_code, 'null'
                            FROM worker_commands_previous
                            """
                        )
                    else:
                        connection.execute(
                            """
                            INSERT INTO worker_commands(
                                id, scope_key, action, execution_id, requester,
                                broker, environment, account, new_risk_lease_id,
                                reason_code, parameters_json, status, worker_id,
                                created_at, started_at, finished_at, result_code,
                                failure_code, result_json
                            )
                            SELECT
                                id, scope_key, action, execution_id, requester,
                                broker, environment, account, new_risk_lease_id,
                                reason_code, parameters_json, status, worker_id,
                                created_at, started_at, finished_at, result_code,
                                failure_code, result_json
                            FROM worker_commands_previous
                            """
                        )
                    if parsed_version == 1:
                        connection.execute("DROP TABLE worker_commands_previous")
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
                    CREATE TABLE IF NOT EXISTS worker_command_resolutions (
                        command_id TEXT PRIMARY KEY,
                        resolution_code TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        evidence_digest TEXT NOT NULL,
                        resolved_by TEXT NOT NULL,
                        resolved_at TEXT NOT NULL,
                        FOREIGN KEY(command_id)
                            REFERENCES worker_commands(id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                if has_previous_resolutions:
                    connection.execute(
                        """
                        INSERT INTO worker_command_resolutions(
                            command_id, resolution_code, evidence_json,
                            evidence_digest, resolved_by, resolved_at
                        )
                        SELECT
                            command_id, resolution_code, evidence_json,
                            evidence_digest, resolved_by, resolved_at
                        FROM worker_command_resolutions_previous
                        """
                    )
                    connection.execute(
                        "DROP TABLE worker_command_resolutions_previous"
                    )
                    connection.execute("DROP TABLE worker_commands_previous")
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
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS risk_runtime_state (
                        route_key TEXT PRIMARY KEY,
                        broker TEXT NOT NULL,
                        environment TEXT NOT NULL,
                        account TEXT NOT NULL,
                        account_identity TEXT NOT NULL,
                        last_external_cashflow_bill_id TEXT NOT NULL,
                        last_account_bill_id TEXT NOT NULL,
                        last_account_bill_timestamp_ms INTEGER,
                        last_bill_scan_at TEXT,
                        adjusted_high_water_usd TEXT,
                        last_total_equity_usd TEXT,
                        drawdown_usd TEXT,
                        drawdown_fraction TEXT,
                        kill_active INTEGER NOT NULL CHECK(kill_active IN (0, 1)),
                        kill_reason TEXT NOT NULL,
                        kill_activated_at TEXT,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                if version is not None and parsed_version == 3:
                    connection.execute(
                        "ALTER TABLE risk_runtime_state "
                        "ADD COLUMN last_account_bill_id "
                        "TEXT NOT NULL DEFAULT ''"
                    )
                    connection.execute(
                        "ALTER TABLE risk_runtime_state "
                        "ADD COLUMN last_account_bill_timestamp_ms INTEGER"
                    )
                    connection.execute(
                        "ALTER TABLE risk_runtime_state "
                        "ADD COLUMN last_bill_scan_at TEXT"
                    )
                if version is not None and parsed_version in {1, 2, 3}:
                    connection.execute(
                        """
                        UPDATE worker_meta
                        SET value=?
                        WHERE key='worker_schema_version'
                        """,
                        (str(_WORKER_SCHEMA_VERSION),),
                    )
                self._schema_version = _WORKER_SCHEMA_VERSION
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
        columns = set(row.keys())
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
            parameters=SetLeverageParameters.model_validate_json(
                row["parameters_json"]
            )
            if (
                "parameters_json" in columns
                and row["parameters_json"] != "null"
            )
            else None,
            status=row["status"],
            worker_id=row["worker_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=WorkerStore._parse_time(row["started_at"]),
            finished_at=WorkerStore._parse_time(row["finished_at"]),
            result_code=row["result_code"],
            failure_code=row["failure_code"],
            result=SetLeverageResult.model_validate_json(
                row["result_json"]
            )
            if "result_json" in columns and row["result_json"] != "null"
            else None,
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

    @staticmethod
    def _row_to_risk_runtime_state(
        row: sqlite3.Row | None,
    ) -> RiskRuntimeState | None:
        if row is None:
            return None

        def parse_decimal(value: object, field_name: str) -> Decimal | None:
            if value is None or value == "":
                return None
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"risk runtime {field_name} 不是有效数字"
                ) from exc
            if not parsed.is_finite():
                raise RuntimeError(f"risk runtime {field_name} 不是有限数字")
            return parsed

        updated_at = WorkerStore._parse_time(row["updated_at"])
        if updated_at is None:
            raise RuntimeError("risk runtime updated_at 缺失")
        return RiskRuntimeState(
            route_key=row["route_key"],
            broker=row["broker"],
            environment=row["environment"],
            account=row["account"],
            account_identity=row["account_identity"],
            last_external_cashflow_bill_id=row[
                "last_external_cashflow_bill_id"
            ],
            last_account_bill_id=row["last_account_bill_id"],
            last_account_bill_timestamp_ms=(
                int(row["last_account_bill_timestamp_ms"])
                if row["last_account_bill_timestamp_ms"] is not None
                else None
            ),
            last_bill_scan_at=WorkerStore._parse_time(
                row["last_bill_scan_at"]
            ),
            adjusted_high_water_usd=parse_decimal(
                row["adjusted_high_water_usd"],
                "adjusted_high_water_usd",
            ),
            last_total_equity_usd=parse_decimal(
                row["last_total_equity_usd"],
                "last_total_equity_usd",
            ),
            drawdown_usd=parse_decimal(row["drawdown_usd"], "drawdown_usd"),
            drawdown_fraction=parse_decimal(
                row["drawdown_fraction"],
                "drawdown_fraction",
            ),
            kill_active=bool(row["kill_active"]),
            kill_reason=row["kill_reason"],
            kill_activated_at=WorkerStore._parse_time(
                row["kill_activated_at"]
            ),
            updated_at=updated_at,
        )

    def get_risk_runtime_state(
        self,
        route_key: str,
    ) -> RiskRuntimeState | None:
        if self._schema_version != _WORKER_SCHEMA_VERSION:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM risk_runtime_state WHERE route_key=?",
                (route_key.strip(),),
            ).fetchone()
        return self._row_to_risk_runtime_state(row)

    def save_risk_runtime_state(
        self,
        state: RiskRuntimeState,
    ) -> RiskRuntimeState:
        if self._schema_version != _WORKER_SCHEMA_VERSION:
            raise RuntimeError("worker schema 尚未迁移，不能保存 risk runtime")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO risk_runtime_state(
                        route_key, broker, environment, account,
                        account_identity, last_external_cashflow_bill_id,
                        last_account_bill_id,
                        last_account_bill_timestamp_ms, last_bill_scan_at,
                        adjusted_high_water_usd, last_total_equity_usd,
                        drawdown_usd, drawdown_fraction, kill_active,
                        kill_reason, kill_activated_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(route_key) DO UPDATE SET
                        broker=excluded.broker,
                        environment=excluded.environment,
                        account=excluded.account,
                        account_identity=excluded.account_identity,
                        last_external_cashflow_bill_id=(
                            excluded.last_external_cashflow_bill_id
                        ),
                        last_account_bill_id=excluded.last_account_bill_id,
                        last_account_bill_timestamp_ms=(
                            excluded.last_account_bill_timestamp_ms
                        ),
                        last_bill_scan_at=excluded.last_bill_scan_at,
                        adjusted_high_water_usd=(
                            excluded.adjusted_high_water_usd
                        ),
                        last_total_equity_usd=excluded.last_total_equity_usd,
                        drawdown_usd=excluded.drawdown_usd,
                        drawdown_fraction=excluded.drawdown_fraction,
                        kill_active=excluded.kill_active,
                        kill_reason=excluded.kill_reason,
                        kill_activated_at=excluded.kill_activated_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        state.route_key,
                        state.broker,
                        state.environment,
                        state.account,
                        state.account_identity,
                        state.last_external_cashflow_bill_id,
                        state.last_account_bill_id,
                        state.last_account_bill_timestamp_ms,
                        (
                            self._iso(state.last_bill_scan_at)
                            if state.last_bill_scan_at
                            else None
                        ),
                        (
                            str(state.adjusted_high_water_usd)
                            if state.adjusted_high_water_usd is not None
                            else None
                        ),
                        (
                            str(state.last_total_equity_usd)
                            if state.last_total_equity_usd is not None
                            else None
                        ),
                        (
                            str(state.drawdown_usd)
                            if state.drawdown_usd is not None
                            else None
                        ),
                        (
                            str(state.drawdown_fraction)
                            if state.drawdown_fraction is not None
                            else None
                        ),
                        int(state.kill_active),
                        state.kill_reason,
                        (
                            self._iso(state.kill_activated_at)
                            if state.kill_activated_at is not None
                            else None
                        ),
                        self._iso(state.updated_at),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return state

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
        parameters: SetLeverageParameters | None = None,
        command_id: str | None = None,
    ) -> tuple[WorkerCommand, bool]:
        """Enqueue one command or return its existing pending/running twin."""
        if self._schema_version != _WORKER_SCHEMA_VERSION:
            raise RuntimeError(
                "worker schema 尚未由持锁 Worker 迁移, 禁止创建命令"
            )
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
            parameters=parameters,
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
                    SELECT command.*
                    FROM worker_commands command
                    LEFT JOIN worker_command_resolutions resolution
                      ON resolution.command_id=command.id
                    WHERE command.scope_key=? AND command.action=?
                      AND (
                        command.status IN ('pending', 'running')
                        OR (
                          command.status='uncertain'
                          AND resolution.command_id IS NULL
                        )
                      )
                    ORDER BY command.created_at ASC, command.id ASC
                    LIMIT 1
                    """,
                    (scope_key, candidate.action.value),
                ).fetchone()
                if existing is not None:
                    if existing["status"] == WorkerCommandStatus.UNCERTAIN.value:
                        raise RuntimeError(
                            "同一 execution 和动作存在 uncertain 命令, "
                            "完成只读对账和人工处置前禁止新建"
                        )
                    existing_command = self._row_to_command(existing)
                    assert existing_command is not None
                    if (
                        candidate.action is WorkerCommandAction.SET_LEVERAGE
                        and existing_command.parameters != candidate.parameters
                    ):
                        raise RuntimeError(
                            "同一路由已有不同参数的 set_leverage 命令, "
                            "禁止静默复用"
                        )
                    connection.execute("COMMIT")
                    return existing_command, False
                if candidate.action in {
                    WorkerCommandAction.SUBMIT,
                    WorkerCommandAction.SET_LEVERAGE,
                }:
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
                            "新增风险命令缺少当前路由的有效 NEW_RISK 租约"
                        )
                connection.execute(
                    """
                    INSERT INTO worker_commands(
                        id, scope_key, action, execution_id, requester,
                        broker, environment, account, new_risk_lease_id,
                        reason_code,
                        parameters_json,
                        status, worker_id, created_at, started_at, finished_at,
                        result_code, failure_code, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, NULL, NULL, '', '', 'null')
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
                        candidate.parameters.model_dump_json()
                        if candidate.parameters is not None
                        else "null",
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
            WHERE status='pending' AND action IN ('submit', 'set_leverage')
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
        result: SetLeverageResult | None = None,
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
        if result is not None and status is not WorkerCommandStatus.SUCCEEDED:
            raise ValueError("只有成功命令可以保存结构化结果")
        result_json = (
            result.model_dump_json() if result is not None else "null"
        )
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE worker_commands
                    SET status=?, finished_at=?, result_code=?, failure_code=?,
                        result_json=?
                    WHERE id=? AND status='running' AND worker_id=?
                    """,
                    (
                        status.value,
                        self._iso(now),
                        result_code,
                        failure_code,
                        result_json,
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
    def _resolution_evidence_json(
        evidence: (
            WorkerCommandResolutionEvidence
            | SetLeverageResolutionEvidence
        ),
    ) -> str:
        return json.dumps(
            evidence.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def _row_to_resolution(
        cls,
        row: sqlite3.Row | None,
    ) -> WorkerCommandResolution | None:
        if row is None:
            return None
        evidence_json = str(row["evidence_json"])
        evidence_digest = hashlib.sha256(
            evidence_json.encode("utf-8")
        ).hexdigest()
        if evidence_digest != row["evidence_digest"]:
            raise RuntimeError("uncertain 命令处置证据摘要不匹配")
        return WorkerCommandResolution.model_validate(
            {
                "command_id": row["command_id"],
                "resolution_code": row["resolution_code"],
                "evidence": json.loads(evidence_json),
                "evidence_digest": evidence_digest,
                "resolved_by": row["resolved_by"],
                "resolved_at": datetime.fromisoformat(
                    row["resolved_at"]
                ),
            }
        )

    def resolve_uncertain_command(
        self,
        command_id: str,
        *,
        resolution_code: str,
        evidence: (
            WorkerCommandResolutionEvidence
            | SetLeverageResolutionEvidence
        ),
        resolved_by: str,
    ) -> WorkerCommandResolution:
        """Attach immutable evidence to one uncertain command without rewriting it."""
        if self._schema_version != _WORKER_SCHEMA_VERSION:
            raise RuntimeError("worker schema 尚未迁移, 不能保存处置证据")
        now = self._now()
        evidence_json = self._resolution_evidence_json(evidence)
        evidence_digest = hashlib.sha256(
            evidence_json.encode("utf-8")
        ).hexdigest()
        candidate = WorkerCommandResolution(
            command_id=command_id,
            resolution_code=resolution_code,
            evidence=evidence,
            evidence_digest=evidence_digest,
            resolved_by=resolved_by,
            resolved_at=now,
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                command = connection.execute(
                    "SELECT * FROM worker_commands WHERE id=?",
                    (candidate.command_id,),
                ).fetchone()
                if command is None:
                    raise KeyError(f"未知 worker command: {candidate.command_id}")
                if command["status"] != WorkerCommandStatus.UNCERTAIN.value:
                    raise RuntimeError("只有 uncertain 命令可以追加处置证据")
                if command["action"] not in _WRITE_ACTION_VALUES:
                    raise RuntimeError("只有券商写命令可以追加处置证据")
                expected_evidence = [
                    ("command_action", command["action"]),
                    ("command_failure_code", command["failure_code"]),
                    ("broker", command["broker"]),
                    ("environment", command["environment"]),
                    ("account", command["account"]),
                ]
                if command["action"] == WorkerCommandAction.SET_LEVERAGE.value:
                    parameters = SetLeverageParameters.model_validate_json(
                        command["parameters_json"]
                    )
                    expected_evidence.append(
                        ("analysis_digest", parameters.analysis_digest)
                    )
                else:
                    expected_evidence.append(
                        ("execution_id", command["execution_id"])
                    )
                for field_name, expected in expected_evidence:
                    actual = getattr(candidate.evidence, field_name)
                    actual_value = (
                        actual.value
                        if isinstance(actual, WorkerCommandAction)
                        else actual
                    )
                    if actual_value != expected:
                        raise RuntimeError(
                            f"处置证据字段与命令不一致: {field_name}"
                        )
                existing = connection.execute(
                    """
                    SELECT * FROM worker_command_resolutions
                    WHERE command_id=?
                    """,
                    (candidate.command_id,),
                ).fetchone()
                if existing is not None:
                    resolution = self._row_to_resolution(existing)
                    assert resolution is not None
                    if (
                        resolution.resolution_code
                        != candidate.resolution_code
                        or resolution.evidence != candidate.evidence
                        or resolution.evidence_digest
                        != candidate.evidence_digest
                        or resolution.resolved_by
                        != candidate.resolved_by
                    ):
                        raise RuntimeError("uncertain 命令已有不同的耐久处置证据")
                    connection.execute("COMMIT")
                    return resolution
                connection.execute(
                    """
                    INSERT INTO worker_command_resolutions(
                        command_id, resolution_code, evidence_json,
                        evidence_digest,
                        resolved_by, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.command_id,
                        candidate.resolution_code,
                        evidence_json,
                        candidate.evidence_digest,
                        candidate.resolved_by,
                        self._iso(candidate.resolved_at),
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM worker_command_resolutions
                    WHERE command_id=?
                    """,
                    (candidate.command_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        resolution = self._row_to_resolution(row)
        assert resolution is not None
        return resolution

    def get_command_resolution(
        self,
        command_id: str,
    ) -> WorkerCommandResolution | None:
        if self._schema_version != _WORKER_SCHEMA_VERSION:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM worker_command_resolutions
                WHERE command_id=?
                """,
                (command_id.strip(),),
            ).fetchone()
        return self._row_to_resolution(row)

    def list_unresolved_write_commands(
        self,
        *,
        broker: str,
        environment: str,
        account: str,
    ) -> list[WorkerCommand]:
        """Return writes that still make the route unsafe for new exposure."""
        placeholders = ",".join("?" for _ in _WRITE_ACTION_VALUES)
        with self._lock, self._connect() as connection:
            parameters = (
                broker.strip().lower(),
                environment.strip().lower(),
                account.strip(),
                *_WRITE_ACTION_VALUES,
            )
            if self._schema_version == _WORKER_SCHEMA_VERSION:
                rows = connection.execute(
                    f"""
                    SELECT command.*
                    FROM worker_commands command
                    LEFT JOIN worker_command_resolutions resolution
                      ON resolution.command_id=command.id
                    WHERE command.broker=?
                      AND command.environment=?
                      AND command.account=?
                      AND command.action IN ({placeholders})
                      AND (
                        command.status IN ('pending', 'running')
                        OR (
                          command.status='uncertain'
                          AND resolution.command_id IS NULL
                        )
                      )
                    ORDER BY command.created_at ASC, command.id ASC
                    """,
                    parameters,
                ).fetchall()
            else:
                rows = connection.execute(
                    f"""
                    SELECT command.*
                    FROM worker_commands command
                    WHERE command.broker=?
                      AND command.environment=?
                      AND command.account=?
                      AND command.action IN ({placeholders})
                      AND command.status IN ('pending', 'running', 'uncertain')
                    ORDER BY command.created_at ASC, command.id ASC
                    """,
                    parameters,
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
        if self._schema_version != _WORKER_SCHEMA_VERSION:
            return None
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
                unresolved = connection.execute(
                    f"""
                    SELECT command.id
                    FROM worker_commands command
                    LEFT JOIN worker_command_resolutions resolution
                      ON resolution.command_id=command.id
                    WHERE command.broker=?
                      AND command.environment=?
                      AND command.account=?
                      AND command.action IN ({
                          ",".join("?" for _ in _WRITE_ACTION_VALUES)
                      })
                      AND (
                        command.status IN ('pending', 'running')
                        OR (
                          command.status='uncertain'
                          AND resolution.command_id IS NULL
                        )
                      )
                    LIMIT 1
                    """,
                    (
                        lease.broker,
                        lease.environment,
                        lease.account,
                        *_WRITE_ACTION_VALUES,
                    ),
                ).fetchone()
                if unresolved is not None:
                    connection.execute("COMMIT")
                    return None
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
                    WHERE status='pending'
                      AND action IN ('submit', 'set_leverage')
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
