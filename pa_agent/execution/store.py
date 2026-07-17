"""SQLite-backed execution ledger with append-only transition events."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from pa_agent.execution.models import (
    ACTIVE_EXECUTION_STATES,
    AccountSnapshot,
    ExecutionEvent,
    ExecutionPlan,
    ExecutionRecord,
    ExecutionState,
    utc_now_iso,
)

_SCHEMA_VERSION = 2
_CLAIM_RELEASABLE_STATES = frozenset(
    {
        ExecutionState.CLOSED,
        ExecutionState.BLOCKED,
        ExecutionState.CANCELED,
        ExecutionState.REJECTED,
        ExecutionState.ERROR,
    }
)


class ExecutionStore:
    """Persist every execution transition before or after broker side effects."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from pa_agent.config.paths import EXECUTION_DB_PATH

            path = EXECUTION_DB_PATH
        self._path = Path(path)
        self._lock = threading.RLock()
        self._initialise()

    @property
    def path(self) -> Path:
        return self._path

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

    def _initialise(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            existing_version = connection.execute(
                "SELECT value FROM execution_meta WHERE key='schema_version'"
            ).fetchone()
            if existing_version is not None:
                try:
                    parsed_version = int(existing_version["value"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "execution.sqlite3 schema 版本无效"
                    ) from exc
                if parsed_version not in {1, _SCHEMA_VERSION}:
                    raise RuntimeError(
                        "不支持的 execution.sqlite3 schema 版本"
                    )
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY,
                    analysis_digest TEXT NOT NULL UNIQUE,
                    broker TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_executions_active
                    ON executions(state, updated_at);
                CREATE TABLE IF NOT EXISTS execution_route_claims (
                    route_key TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL UNIQUE,
                    broker TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    account_identity TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    FOREIGN KEY(execution_id) REFERENCES executions(id)
                );
                CREATE TABLE IF NOT EXISTS execution_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(execution_id) REFERENCES executions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_events_execution
                    ON execution_events(execution_id, event_id);
                CREATE TABLE IF NOT EXISTS account_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    broker TEXT NOT NULL,
                    account_profile TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_account_snapshots_latest
                    ON account_snapshots(broker, account_profile, snapshot_id);
                INSERT INTO execution_meta(key, value)
                VALUES ('schema_version', '2')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value;
                COMMIT;
                """
            )
            version = connection.execute(
                "SELECT value FROM execution_meta WHERE key='schema_version'"
            ).fetchone()
            if version is None or int(version["value"]) != _SCHEMA_VERSION:
                raise RuntimeError("不支持的 execution.sqlite3 schema 版本")

    @staticmethod
    def _record_json(record: ExecutionRecord) -> str:
        return record.model_dump_json()

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> ExecutionRecord | None:
        if row is None:
            return None
        return ExecutionRecord.model_validate_json(row["payload_json"])

    def create(self, plan: ExecutionPlan) -> tuple[ExecutionRecord, bool]:
        """Create one READY execution, returning the existing one on duplicate analysis."""
        now = utc_now_iso()
        record = ExecutionRecord(
            id=plan.id,
            plan=plan,
            state=ExecutionState.READY,
            remaining_quantity=plan.quantity,
            created_at=now,
            updated_at=now,
        )
        event = ExecutionEvent(
            execution_id=record.id,
            kind="plan_created",
            payload={"state": record.state.value},
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT payload_json FROM executions WHERE analysis_digest=?",
                    (plan.analysis_digest,),
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return ExecutionRecord.model_validate_json(existing["payload_json"]), False
                connection.execute(
                    """
                    INSERT INTO executions(
                        id, analysis_digest, broker, instrument, state,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        plan.analysis_digest,
                        plan.broker,
                        plan.instrument,
                        record.state.value,
                        self._record_json(record),
                        record.created_at,
                        record.updated_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO execution_events(execution_id, kind, created_at, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        event.execution_id,
                        event.kind,
                        event.created_at,
                        json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return record, True

    def save(
        self,
        record: ExecutionRecord,
        *,
        event_kind: str,
        event_payload: dict | None = None,
    ) -> ExecutionRecord:
        """Atomically update the record and append its audit event."""
        current = self.get(record.id)
        if current is None:
            raise KeyError(f"未知 execution id: {record.id}")
        if record.revision != current.revision:
            raise RuntimeError(
                f"execution {record.id} revision conflict: "
                f"expected {current.revision}, got {record.revision}"
            )
        updated = record.model_copy(
            update={
                "revision": current.revision + 1,
                "updated_at": utc_now_iso(),
            }
        )
        event = ExecutionEvent(
            execution_id=updated.id,
            kind=event_kind,
            payload=event_payload or {},
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE executions
                    SET state=?, payload_json=?, updated_at=?
                    WHERE id=? AND payload_json=?
                    """,
                    (
                        updated.state.value,
                        self._record_json(updated),
                        updated.updated_at,
                        updated.id,
                        self._record_json(current),
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"execution {record.id} concurrent update")
                connection.execute(
                    """
                    INSERT INTO execution_events(execution_id, kind, created_at, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        event.execution_id,
                        event.kind,
                        event.created_at,
                        json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return updated

    def append_event(self, execution_id: str, kind: str, payload: dict | None = None) -> None:
        event = ExecutionEvent(
            execution_id=execution_id,
            kind=kind,
            payload=payload or {},
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_events(execution_id, kind, created_at, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.execution_id,
                    event.kind,
                    event.created_at,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                ),
            )

    def get(self, execution_id: str) -> ExecutionRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM executions WHERE id=?",
                (execution_id,),
            ).fetchone()
        return self._row_to_record(row)

    def get_by_analysis_digest(self, digest: str) -> ExecutionRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM executions WHERE analysis_digest=?",
                (digest,),
            ).fetchone()
        return self._row_to_record(row)

    def list_active(self) -> list[ExecutionRecord]:
        state_values = tuple(state.value for state in ACTIVE_EXECUTION_STATES)
        placeholders = ",".join("?" for _ in state_values)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM executions
                WHERE state IN ({placeholders})
                ORDER BY updated_at ASC
                """,
                state_values,
            ).fetchall()
        return [ExecutionRecord.model_validate_json(row["payload_json"]) for row in rows]

    def list_recent(self, limit: int = 50) -> list[ExecutionRecord]:
        safe_limit = max(1, min(int(limit), 500))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM executions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [ExecutionRecord.model_validate_json(row["payload_json"]) for row in rows]

    @staticmethod
    def _route_key(
        *,
        broker: str,
        environment: str,
        account_identity: str,
        instrument: str,
    ) -> str:
        payload = "\x1f".join(
            (
                broker.strip().lower(),
                environment.strip().lower(),
                account_identity.strip(),
                instrument.strip().upper(),
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def acquire_route_claim(
        self,
        record: ExecutionRecord,
        *,
        account_identity: str,
    ) -> ExecutionRecord | None:
        """Atomically claim one concrete broker/account/instrument route.

        Returns the conflicting owner, or ``None`` when this execution owns the
        route. Terminal owners are replaced in the same SQLite transaction.
        """
        identity = account_identity.strip()
        if not identity:
            raise RuntimeError("实际账户身份为空，禁止占用活动路由")
        route_key = self._route_key(
            broker=record.plan.broker,
            environment=record.plan.environment,
            account_identity=identity,
            instrument=record.plan.instrument,
        )
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_by_execution = connection.execute(
                    """
                    SELECT route_key
                    FROM execution_route_claims
                    WHERE execution_id=?
                    """,
                    (record.id,),
                ).fetchone()
                if (
                    existing_by_execution is not None
                    and existing_by_execution["route_key"] != route_key
                ):
                    raise RuntimeError(
                        "同一 execution 已绑定其他实际账户路由，禁止改绑"
                    )
                claim = connection.execute(
                    """
                    SELECT execution_id
                    FROM execution_route_claims
                    WHERE route_key=?
                    """,
                    (route_key,),
                ).fetchone()
                if claim is None:
                    connection.execute(
                        """
                        INSERT INTO execution_route_claims(
                            route_key, execution_id, broker, environment,
                            account_identity, instrument, claimed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            route_key,
                            record.id,
                            record.plan.broker,
                            record.plan.environment,
                            identity,
                            record.plan.instrument,
                            now,
                        ),
                    )
                    connection.execute("COMMIT")
                    return None
                owner_id = str(claim["execution_id"])
                if owner_id == record.id:
                    connection.execute("COMMIT")
                    return None
                owner_row = connection.execute(
                    "SELECT payload_json FROM executions WHERE id=?",
                    (owner_id,),
                ).fetchone()
                if owner_row is None:
                    raise RuntimeError(
                        "活动路由占用指向不存在的 execution，禁止自动接管"
                    )
                owner = ExecutionRecord.model_validate_json(
                    owner_row["payload_json"]
                )
                if owner.state not in _CLAIM_RELEASABLE_STATES:
                    connection.execute("COMMIT")
                    return owner
                connection.execute(
                    """
                    UPDATE execution_route_claims
                    SET execution_id=?, broker=?, environment=?,
                        account_identity=?, instrument=?, claimed_at=?
                    WHERE route_key=? AND execution_id=?
                    """,
                    (
                        record.id,
                        record.plan.broker,
                        record.plan.environment,
                        identity,
                        record.plan.instrument,
                        now,
                        route_key,
                        owner_id,
                    ),
                )
                connection.execute("COMMIT")
                return None
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def route_claim_owner(self, record: ExecutionRecord) -> str | None:
        identity = record.account_identity.strip()
        if not identity:
            return None
        route_key = self._route_key(
            broker=record.plan.broker,
            environment=record.plan.environment,
            account_identity=identity,
            instrument=record.plan.instrument,
        )
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT execution_id
                FROM execution_route_claims
                WHERE route_key=?
                """,
                (route_key,),
            ).fetchone()
        return str(row["execution_id"]) if row is not None else None

    def events(self, execution_id: str) -> list[ExecutionEvent]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT kind, created_at, payload_json
                FROM execution_events
                WHERE execution_id=?
                ORDER BY event_id ASC
                """,
                (execution_id,),
            ).fetchall()
        return [
            ExecutionEvent(
                execution_id=execution_id,
                kind=row["kind"],
                created_at=row["created_at"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def save_account_snapshot(self, snapshot: AccountSnapshot) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO account_snapshots(
                    broker, account_profile, captured_at, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.broker,
                    snapshot.account_profile,
                    snapshot.captured_at,
                    snapshot.model_dump_json(),
                ),
            )

    def latest_account_snapshot(
        self,
        broker: str,
        account_profile: str,
    ) -> AccountSnapshot | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM account_snapshots
                WHERE broker=? AND account_profile=?
                ORDER BY snapshot_id DESC
                LIMIT 1
                """,
                (broker, account_profile),
            ).fetchone()
        if row is None:
            return None
        return AccountSnapshot.model_validate_json(row["payload_json"])
