"""工作台只读读取层。

这里不发网络请求、不写数据库、不创建交易计划。它只把已有的行情对象、
Worker 心跳、执行账本和账户快照组合成 UI 可以直接显示的快照，并明确每个
字段究竟是已观察事实，还是配置/执行计划。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pa_agent.execution.models import AccountSnapshot, ExecutionRecord
from pa_agent.execution.worker_protocol import WorkerHeartbeat

_HEARTBEAT_STALE_SECONDS = 10
_RECONCILE_STALE_SECONDS = 30


class FactCertainty(StrEnum):
    """工作台向用户展示的事实来源层级。"""

    CONFIRMED = "confirmed"
    PLAN = "plan"
    PLANNING = "planning"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReadFact:
    """一个带来源、采集时间和语义层级的只读字段。"""

    value: str
    certainty: FactCertainty
    source: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class WorkbenchReadSnapshot:
    """一次工作台读取结果；对象均来自已有只读存储。"""

    captured_at: str
    source_kind: ReadFact
    connection: ReadFact
    symbol: ReadFact
    timeframe: ReadFact
    worker_state: ReadFact
    heartbeat: ReadFact
    reconcile: ReadFact
    account: ReadFact
    active_execution_count: ReadFact
    latest_execution_state: ReadFact
    active_executions: tuple[ExecutionRecord, ...]
    latest_execution: ExecutionRecord | None
    account_snapshot: AccountSnapshot | None


class WorkbenchReadModel:
    """把真实本地状态组成只读工作台快照。"""

    def __init__(
        self,
        *,
        settings: Any,
        data_source: Any,
        execution_store: Any,
        worker_store: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._data_source = data_source
        self._execution_store = execution_store
        self._worker_store = worker_store
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_context(cls, context: Any) -> WorkbenchReadModel:
        """从 AppContext 取出已有存储；不会新建另一套账本。"""
        execution_service = context.execution_service
        return cls(
            settings=context.settings,
            data_source=context.data_source,
            execution_store=execution_service.execution_store,
            worker_store=execution_service.worker_store,
        )

    def set_data_source(self, data_source: Any) -> None:
        """在主窗口完成数据源事务切换后更新读取对象。"""
        self._data_source = data_source

    def capture(self) -> WorkbenchReadSnapshot:
        """读取一次完整快照；任何真实读取错误都直接暴露。"""
        captured_at = self._now_iso()
        source_kind = self._source_kind_fact(captured_at)
        connection = self._connection_fact(captured_at)
        symbol = self._subscription_fact("_symbol", "last_symbol", captured_at)
        timeframe = self._subscription_fact(
            "_timeframe", "last_timeframe", captured_at
        )

        heartbeat = self._worker_store.latest_heartbeat()
        worker_state, heartbeat_fact, reconcile = self._worker_facts(
            heartbeat,
            captured_at,
        )

        broker, account_profile = self._selected_account_route()
        account_snapshot = None
        if broker and account_profile:
            account_snapshot = self._execution_store.latest_account_snapshot(
                broker,
                account_profile,
            )
        account = self._account_fact(
            broker,
            account_profile,
            account_snapshot,
            captured_at,
        )

        active_executions = tuple(self._execution_store.list_active())
        recent = tuple(self._execution_store.list_recent(limit=1))
        latest_execution = recent[0] if recent else None
        latest_execution_state = self._latest_execution_fact(
            latest_execution,
            captured_at,
        )
        active_execution_count = ReadFact(
            value=str(len(active_executions)),
            certainty=FactCertainty.CONFIRMED,
            source="records/execution.sqlite3",
            observed_at=captured_at,
        )

        return WorkbenchReadSnapshot(
            captured_at=captured_at,
            source_kind=source_kind,
            connection=connection,
            symbol=symbol,
            timeframe=timeframe,
            worker_state=worker_state,
            heartbeat=heartbeat_fact,
            reconcile=reconcile,
            account=account,
            active_execution_count=active_execution_count,
            latest_execution_state=latest_execution_state,
            active_executions=active_executions,
            latest_execution=latest_execution,
            account_snapshot=account_snapshot,
        )

    def _now_iso(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("工作台读取时钟必须返回带时区时间")
        return value.astimezone(UTC).isoformat(timespec="seconds")

    def _source_kind_fact(self, observed_at: str) -> ReadFact:
        general = getattr(self._settings, "general", None)
        value = str(getattr(general, "last_data_source", "") or "").strip()
        if not value:
            return ReadFact(
                value="未知",
                certainty=FactCertainty.UNKNOWN,
                source="settings.json",
                observed_at=observed_at,
            )
        return ReadFact(
            value=value,
            certainty=FactCertainty.PLAN,
            source="settings.json / last_data_source",
            observed_at=observed_at,
        )

    def _connection_fact(self, observed_at: str) -> ReadFact:
        marker = getattr(self._data_source, "_connected", None)
        if not isinstance(marker, bool):
            return ReadFact(
                value="未知",
                certainty=FactCertainty.UNKNOWN,
                source="data source connection marker",
                observed_at=observed_at,
            )
        return ReadFact(
            value="已连接" if marker else "未连接",
            certainty=FactCertainty.CONFIRMED,
            source="data source connection marker",
            observed_at=observed_at,
        )

    def _subscription_fact(
        self,
        actual_attr: str,
        configured_attr: str,
        observed_at: str,
    ) -> ReadFact:
        actual = str(getattr(self._data_source, actual_attr, "") or "").strip()
        if actual:
            return ReadFact(
                value=actual,
                certainty=FactCertainty.CONFIRMED,
                source=f"data source subscription / {actual_attr}",
                observed_at=observed_at,
            )
        general = getattr(self._settings, "general", None)
        configured = str(getattr(general, configured_attr, "") or "").strip()
        if configured:
            return ReadFact(
                value=configured,
                certainty=FactCertainty.PLAN,
                source=f"settings.json / {configured_attr}",
                observed_at=observed_at,
            )
        return ReadFact(
            value="未知",
            certainty=FactCertainty.UNKNOWN,
            source=f"data source subscription / {actual_attr}",
            observed_at=observed_at,
        )

    def _worker_facts(
        self,
        heartbeat: WorkerHeartbeat | None,
        observed_at: str,
    ) -> tuple[ReadFact, ReadFact, ReadFact]:
        if heartbeat is None:
            unknown = ReadFact(
                value="未观察到心跳",
                certainty=FactCertainty.UNKNOWN,
                source="execution-control.sqlite3 / worker_heartbeats",
                observed_at=observed_at,
            )
            return unknown, unknown, unknown

        heartbeat_stale = self._worker_store.is_heartbeat_stale(
            heartbeat.worker_id,
            stale_after_seconds=_HEARTBEAT_STALE_SECONDS,
        )
        worker_state = ReadFact(
            value=(
                f"{heartbeat.state.value}（心跳陈旧）"
                if heartbeat_stale
                else heartbeat.state.value
            ),
            certainty=FactCertainty.CONFIRMED,
            source="execution-control.sqlite3 / worker_heartbeats",
            observed_at=observed_at,
        )
        heartbeat_fact = ReadFact(
            value="陈旧" if heartbeat_stale else "新鲜",
            certainty=FactCertainty.CONFIRMED,
            source="execution-control.sqlite3 / last_seen_at",
            observed_at=observed_at,
        )

        if heartbeat.last_successful_reconcile_at is None:
            reconcile = ReadFact(
                value="未完成首次对账",
                certainty=FactCertainty.UNKNOWN,
                source="execution-control.sqlite3 / last_successful_reconcile_at",
                observed_at=observed_at,
            )
        else:
            reconcile_stale = self._worker_store.is_reconcile_stale(
                heartbeat.worker_id,
                stale_after_seconds=_RECONCILE_STALE_SECONDS,
            )
            reconcile = ReadFact(
                value="陈旧" if reconcile_stale else "新鲜",
                certainty=FactCertainty.CONFIRMED,
                source="execution-control.sqlite3 / last_successful_reconcile_at",
                observed_at=observed_at,
            )
        return worker_state, heartbeat_fact, reconcile

    def _selected_account_route(self) -> tuple[str, str]:
        execution = getattr(self._settings, "execution", None)
        broker = str(getattr(execution, "selected_broker", "") or "").strip()
        if broker == "longbridge":
            account = str(
                getattr(
                    getattr(execution, "longbridge", None),
                    "preferred_account",
                    "",
                )
                or ""
            ).strip()
            return broker, account
        if broker == "okx":
            return broker, "okx"
        return "", ""

    @staticmethod
    def _account_fact(
        broker: str,
        account_profile: str,
        snapshot: AccountSnapshot | None,
        observed_at: str,
    ) -> ReadFact:
        route = f"{broker}/{account_profile}" if broker and account_profile else "未知账户"
        if snapshot is None:
            return ReadFact(
                value=f"未读取到账户快照（{route}）",
                certainty=FactCertainty.UNKNOWN,
                source="records/execution.sqlite3 / account_snapshots",
                observed_at=observed_at,
            )
        return ReadFact(
            value=f"已读取账户快照（{route}）",
            certainty=FactCertainty.CONFIRMED,
            source="records/execution.sqlite3 / account_snapshots",
            observed_at=observed_at,
        )

    @staticmethod
    def _latest_execution_fact(
        latest_execution: ExecutionRecord | None,
        observed_at: str,
    ) -> ReadFact:
        if latest_execution is None:
            return ReadFact(
                value="无",
                certainty=FactCertainty.CONFIRMED,
                source="records/execution.sqlite3",
                observed_at=observed_at,
            )
        return ReadFact(
            value=latest_execution.state.value,
            certainty=FactCertainty.PLAN,
            source=(
                "records/execution.sqlite3 / local lifecycle "
                f"{latest_execution.id}"
            ),
            observed_at=observed_at,
        )
