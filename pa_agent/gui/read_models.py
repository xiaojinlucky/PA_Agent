"""工作台只读读取层。

这里不发网络请求、不写数据库、不创建交易计划。它只把已有的行情对象、
Worker 心跳、执行账本和账户快照组合成 UI 可以直接显示的快照，并明确每个
字段究竟是已观察事实，还是配置/执行计划。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from pa_agent.config.paths import PROJECT_ROOT
from pa_agent.execution.models import AccountSnapshot, ExecutionRecord
from pa_agent.execution.worker_protocol import WorkerHeartbeat, WorkerState

_HEARTBEAT_STALE_SECONDS = 10
_RECONCILE_STALE_SECONDS = 30
_ACCOUNT_SNAPSHOT_STALE_SECONDS = 90
_CAMPAIGN_STALE_SECONDS = 15 * 60
_DEFAULT_CAMPAIGN_STATE_PATH = PROJECT_ROOT / "records" / "okx_demo_campaign.json"


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
    risk_stop: ReadFact
    route_alignment: ReadFact
    risk_gate: ReadFact
    active_execution_count: ReadFact
    latest_execution_state: ReadFact
    campaign_state: ReadFact
    campaign_progress: ReadFact
    campaign_last_result: ReadFact
    campaign_risk_parameters: ReadFact
    campaign_config_alignment: ReadFact
    campaign_execution_ids: tuple[str, ...]
    active_executions: tuple[ExecutionRecord, ...]
    latest_execution: ExecutionRecord | None
    account_snapshot: AccountSnapshot | None
    risk_runtime_state: Any | None


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
        campaign_state_path: Path = _DEFAULT_CAMPAIGN_STATE_PATH,
        account_route: tuple[str, str] | None = None,
        control_route: tuple[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._data_source = data_source
        self._execution_store = execution_store
        self._worker_store = worker_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._campaign_state_path = Path(campaign_state_path)
        self._account_route = (
            self._normalise_account_route(
                account_route,
                field_name="account_route",
            )
            if account_route is not None
            else None
        )
        self._control_route = (
            self._normalise_account_route(
                control_route,
                field_name="control_route",
            )
            if control_route is not None
            else None
        )

    @classmethod
    def from_context(
        cls,
        context: Any,
        *,
        account_route: tuple[str, str] | None = None,
        control_route: tuple[str, str] | None = None,
    ) -> WorkbenchReadModel:
        """从 AppContext 取出已有存储；不会新建另一套账本。"""
        execution_service = context.execution_service
        return cls(
            settings=context.settings,
            data_source=context.data_source,
            execution_store=execution_service.execution_store,
            worker_store=execution_service.worker_store,
            account_route=account_route,
            control_route=control_route,
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

        selected_route = self._selected_account_route()
        account_route = self._account_route or selected_route
        control_route = self._control_route or selected_route
        broker, account_profile = account_route
        route_alignment = self._route_alignment_fact(
            account_route,
            control_route,
            captured_at,
        )
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
        risk_broker, risk_environment, risk_account = (
            self._risk_route_identity(account_route)
        )
        risk_runtime_state = self._risk_runtime_state(
            risk_broker,
            risk_environment,
            risk_account,
        )
        risk_stop = self._risk_stop_fact(
            risk_runtime_state,
            captured_at,
        )
        risk_gate = self._risk_gate_fact(
            risk_runtime_state=risk_runtime_state,
            risk_stop=risk_stop,
            heartbeat=heartbeat,
            heartbeat_fact=heartbeat_fact,
            reconcile=reconcile,
            account=account,
            route_alignment=route_alignment,
            observed_at=captured_at,
        )

        active_executions = tuple(self._execution_store.list_active())
        campaign_execution_ids = self._campaign_execution_ids()
        latest_execution = self._latest_execution(campaign_execution_ids)
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
        (
            campaign_state,
            campaign_progress,
            campaign_last_result,
            campaign_risk_parameters,
            campaign_config_alignment,
        ) = self._campaign_facts(captured_at)
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
            risk_stop=risk_stop,
            route_alignment=route_alignment,
            risk_gate=risk_gate,
            active_execution_count=active_execution_count,
            latest_execution_state=latest_execution_state,
            campaign_state=campaign_state,
            campaign_progress=campaign_progress,
            campaign_last_result=campaign_last_result,
            campaign_risk_parameters=campaign_risk_parameters,
            campaign_config_alignment=campaign_config_alignment,
            campaign_execution_ids=campaign_execution_ids,
            active_executions=active_executions,
            latest_execution=latest_execution,
            account_snapshot=account_snapshot,
            risk_runtime_state=risk_runtime_state,
        )

    def _latest_execution(
        self,
        campaign_execution_ids: tuple[str, ...],
    ) -> ExecutionRecord | None:
        """当前 Campaign 有执行时，禁止被其他测试或旧任务记录覆盖。"""
        if campaign_execution_ids:
            getter = getattr(self._execution_store, "get", None)
            if not callable(getter):
                raise RuntimeError("执行账本缺少按 ID 读取能力")
            for execution_id in reversed(campaign_execution_ids):
                execution = getter(execution_id)
                if execution is not None:
                    return execution
            return None

        recent = tuple(self._execution_store.list_recent(limit=1))
        return recent[0] if recent else None

    def _campaign_execution_ids(self) -> tuple[str, ...]:
        """读取当前 Campaign 自己创建的执行，避免把旧实验显示成最新决定。"""
        if not self._campaign_state_path.is_file():
            return ()
        try:
            payload = json.loads(
                self._campaign_state_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return ()
        if not isinstance(payload, dict):
            return ()
        raw_ids = payload.get("execution_ids")
        if not isinstance(raw_ids, list):
            return ()
        return tuple(
            value.strip()
            for item in raw_ids
            if (value := str(item or "").strip())
        )

    def _risk_runtime_state(
        self,
        broker: str,
        environment: str,
        account: str,
    ) -> Any | None:
        if not broker or not environment or not account:
            return None
        getter = getattr(self._worker_store, "get_risk_runtime_state", None)
        if not callable(getter):
            return None
        return getter(f"{broker}:{environment}:{account}")

    @staticmethod
    def _normalise_account_route(
        route: tuple[str, str],
        *,
        field_name: str,
    ) -> tuple[str, str]:
        if not isinstance(route, tuple) or len(route) != 2:
            raise TypeError(f"{field_name} 必须是 (broker, account_profile) 元组")
        broker = str(route[0] or "").strip().lower()
        account_profile = str(route[1] or "").strip().lower()
        valid_profiles = {
            "okx": {"okx-demo", "okx-live"},
            "longbridge": {"paper", "comprehensive", "intraday"},
        }
        if (
            broker not in valid_profiles
            or account_profile not in valid_profiles[broker]
        ):
            raise ValueError(
                f"{field_name} 不是受支持的账户路由："
                f"{broker or '空'}/{account_profile or '空'}"
            )
        return broker, account_profile

    @staticmethod
    def _risk_route_identity(
        account_route: tuple[str, str],
    ) -> tuple[str, str, str]:
        broker, account_profile = account_route
        if broker == "okx" and account_profile in {"okx-demo", "okx-live"}:
            environment = "demo" if account_profile == "okx-demo" else "live"
            return broker, environment, "okx"
        if broker == "longbridge" and account_profile in {
            "paper",
            "comprehensive",
            "intraday",
        }:
            environment = "demo" if account_profile == "paper" else "live"
            return broker, environment, account_profile
        return "", "", ""

    @staticmethod
    def _route_alignment_fact(
        account_route: tuple[str, str],
        control_route: tuple[str, str],
        observed_at: str,
    ) -> ReadFact:
        account_label = WorkbenchReadModel._account_route_label(account_route)
        control_label = WorkbenchReadModel._account_route_label(control_route)
        source = "read-model account_route + execution control route"
        if not all(account_route) or not all(control_route):
            return ReadFact(
                value=(
                    "路由不匹配："
                    f"页面 {account_label}，控制器 {control_label}"
                ),
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
        if account_route != control_route:
            return ReadFact(
                value=(
                    "路由不匹配："
                    f"页面 {account_label}，控制器 {control_label}"
                ),
                certainty=FactCertainty.CONFIRMED,
                source=source,
                observed_at=observed_at,
            )
        return ReadFact(
            value=f"路由匹配（{account_label}）",
            certainty=FactCertainty.CONFIRMED,
            source=source,
            observed_at=observed_at,
        )

    @staticmethod
    def _account_route_label(route: tuple[str, str]) -> str:
        broker, account_profile = route
        if not broker or not account_profile:
            return "未配置"
        return f"{broker}/{account_profile}"

    @staticmethod
    def _risk_stop_fact(state: Any | None, observed_at: str) -> ReadFact:
        if state is None:
            return ReadFact(
                value="尚无可信风险基线",
                certainty=FactCertainty.UNKNOWN,
                source="records/execution_control.sqlite3",
                observed_at=observed_at,
            )
        if bool(getattr(state, "kill_active", False)):
            reason = str(getattr(state, "kill_reason", "") or "未知原因")
            reason_label = {
                "drawdown_threshold_exceeded": "账户回撤达到停止线",
                "account_identity_changed": "账户身份与已确认账户不一致",
                "bill_chain_disconnected": "账户账单链不完整",
                "risk_runtime_IncompleteRead": "风险账户数据读取中断",
                "risk_runtime_BrokerTransportError": "风险账户数据暂时读取失败",
            }.get(
                reason,
                (
                    "风险账户数据读取失败"
                    if reason.startswith("risk_runtime_")
                    else reason
                ),
            )
            return ReadFact(
                value=f"已停止新增风险：{reason_label}",
                certainty=FactCertainty.CONFIRMED,
                source="records/execution_control.sqlite3 / risk_runtime_state",
                observed_at=observed_at,
            )
        drawdown = getattr(state, "drawdown_fraction", None)
        drawdown_text = (
            f"；当前回撤 {Decimal(str(drawdown)) * 100:.2f}%"
            if drawdown is not None
            else ""
        )
        return ReadFact(
            value=f"允许新增风险{drawdown_text}",
            certainty=FactCertainty.CONFIRMED,
            source="records/execution_control.sqlite3 / risk_runtime_state",
            observed_at=observed_at,
        )

    @staticmethod
    def _risk_gate_fact(
        *,
        risk_runtime_state: Any | None,
        risk_stop: ReadFact,
        heartbeat: WorkerHeartbeat | None,
        heartbeat_fact: ReadFact,
        reconcile: ReadFact,
        account: ReadFact,
        route_alignment: ReadFact,
        observed_at: str,
    ) -> ReadFact:
        source = (
            "risk_runtime_state + worker_heartbeats + "
            "last_successful_reconcile_at + account_snapshots + route_alignment"
        )

        if bool(getattr(risk_runtime_state, "kill_active", False)):
            return ReadFact(
                value=risk_stop.value,
                certainty=FactCertainty.CONFIRMED,
                source=source,
                observed_at=observed_at,
            )

        def unavailable(reason: str) -> ReadFact:
            return ReadFact(
                value=f"新增风险不可用：{reason}",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )

        if risk_runtime_state is None:
            return unavailable("尚无可信风险基线")
        if (
            route_alignment.certainty is not FactCertainty.CONFIRMED
            or not route_alignment.value.startswith("路由匹配")
        ):
            return unavailable("页面账户与实际控制器路由不一致")
        if heartbeat is None:
            return unavailable("未观察到交易后台心跳")
        if heartbeat.state not in {
            WorkerState.RUNNING,
            WorkerState.RECONCILING,
        }:
            state_label = {
                WorkerState.STARTING: "启动中",
                WorkerState.NEEDS_ATTENTION: "需要人工处理",
                WorkerState.STOPPING: "正在停止",
            }.get(heartbeat.state, heartbeat.state.value)
            return unavailable(f"交易后台状态为{state_label}")
        if heartbeat_fact.value != "新鲜":
            return unavailable("交易后台心跳陈旧")
        if reconcile.certainty is not FactCertainty.CONFIRMED:
            return unavailable("交易后台尚未完成首次对账")
        if reconcile.value != "新鲜":
            return unavailable("交易后台最近成功对账已陈旧")
        if account.certainty is not FactCertainty.CONFIRMED:
            return unavailable(account.value)
        return ReadFact(
            value="允许新增风险",
            certainty=FactCertainty.CONFIRMED,
            source=source,
            observed_at=observed_at,
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
            route = getattr(execution, "okx", None)
            profile = "okx-demo" if bool(getattr(route, "simulated", False)) else "okx-live"
            return broker, profile
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
        try:
            captured_at = datetime.fromisoformat(snapshot.captured_at)
            observed = datetime.fromisoformat(observed_at)
        except (TypeError, ValueError):
            return ReadFact(
                value=f"账户快照时间无效（{route}）",
                certainty=FactCertainty.UNKNOWN,
                source="records/execution.sqlite3 / account_snapshots / captured_at",
                observed_at=observed_at,
            )
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            return ReadFact(
                value=f"账户快照时间无时区（{route}）",
                certainty=FactCertainty.UNKNOWN,
                source="records/execution.sqlite3 / account_snapshots / captured_at",
                observed_at=observed_at,
            )
        age_seconds = (observed - captured_at.astimezone(UTC)).total_seconds()
        if age_seconds > _ACCOUNT_SNAPSHOT_STALE_SECONDS:
            return ReadFact(
                value=f"账户快照陈旧（{route}，{int(age_seconds)}秒）",
                certainty=FactCertainty.UNKNOWN,
                source="records/execution.sqlite3 / account_snapshots / captured_at",
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

    def _campaign_facts(
        self,
        observed_at: str,
    ) -> tuple[ReadFact, ReadFact, ReadFact, ReadFact, ReadFact]:
        source = str(self._campaign_state_path)
        if not self._campaign_state_path.is_file():
            missing = ReadFact(
                value="未启动",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
            return missing, missing, missing, missing, missing

        try:
            payload = json.loads(
                self._campaign_state_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            invalid = ReadFact(
                value=f"状态读取失败（{type(exc).__name__}）",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
            return invalid, invalid, invalid, invalid, invalid
        if not isinstance(payload, dict):
            invalid = ReadFact(
                value="状态格式无效",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
            return invalid, invalid, invalid, invalid, invalid

        status = str(payload.get("status") or "").strip()
        updated_text = str(payload.get("updated_at") or "").strip()
        try:
            updated_at = datetime.fromisoformat(updated_text)
            observed = datetime.fromisoformat(observed_at)
        except ValueError:
            invalid = ReadFact(
                value="状态时间无效",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
            return invalid, invalid, invalid, invalid, invalid
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            invalid = ReadFact(
                value="状态时间无时区",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
            return invalid, invalid, invalid, invalid, invalid

        raw_age_seconds = int(
            (observed - updated_at.astimezone(UTC)).total_seconds()
        )
        age_seconds = max(0, raw_age_seconds)
        status_labels = {
            "active": "运行中",
            "stopping": "正在安全收口",
            "completed": "已完成",
            "needs_attention": "需要人工处理",
        }
        status_value = status_labels.get(status, f"未知状态 {status or '—'}")
        status_certainty = (
            FactCertainty.CONFIRMED
            if status in status_labels
            else FactCertainty.UNKNOWN
        )
        if raw_age_seconds < -60:
            status_value = (
                f"状态时间比本机快 {-raw_age_seconds} 秒，无法确认是否新鲜"
            )
            status_certainty = FactCertainty.UNKNOWN
        elif age_seconds > _CAMPAIGN_STALE_SECONDS:
            status_value = (
                f"状态文件显示{status_labels.get(status, status or '未知状态')}"
                f"，但已 {age_seconds} 秒未更新"
            )
            status_certainty = FactCertainty.UNKNOWN
        elif status == "active":
            status_value = f"运行中（{age_seconds} 秒前更新）"
        elif status in status_labels:
            status_value = f"{status_labels[status]}（{age_seconds} 秒前更新）"
        campaign_state = ReadFact(
            value=status_value,
            certainty=status_certainty,
            source=source,
            observed_at=observed_at,
        )

        count_fields = (
            "analyses_completed",
            "analyses_failed",
            "executions_prepared",
        )
        counts: list[int] = []
        for field in count_fields:
            value = payload.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                invalid = ReadFact(
                    value=f"计数字段无效（{field}）",
                    certainty=FactCertainty.UNKNOWN,
                    source=source,
                    observed_at=observed_at,
                )
                risk, alignment = self._campaign_risk_facts(
                    payload,
                    status_certainty,
                    source,
                    observed_at,
                )
                return campaign_state, invalid, invalid, risk, alignment
            counts.append(value)
        campaign_progress = ReadFact(
            value=(
                f"分析 {counts[0]} / 失败 {counts[1]} / "
                f"生成执行 {counts[2]}"
            ),
            certainty=status_certainty,
            source=source,
            observed_at=observed_at,
        )
        last_result = str(payload.get("last_plan_result") or "").strip()
        last_action = str(payload.get("last_supervisor_action") or "").strip()
        last_error = str(payload.get("last_error") or "").strip()
        readable_result = self._campaign_result_text(
            last_result=last_result,
            last_action=last_action,
            has_error=bool(last_error),
        )
        campaign_last_result = ReadFact(
            value=readable_result,
            certainty=status_certainty,
            source=source,
            observed_at=observed_at,
        )
        campaign_risk_parameters, campaign_config_alignment = (
            self._campaign_risk_facts(
                payload,
                status_certainty,
                source,
                observed_at,
            )
        )
        return (
            campaign_state,
            campaign_progress,
            campaign_last_result,
            campaign_risk_parameters,
            campaign_config_alignment,
        )

    @staticmethod
    def _campaign_result_text(
        *,
        last_result: str,
        last_action: str,
        has_error: bool,
    ) -> str:
        """把持久状态码转成主界面文案，原始异常只留在技术日志。"""
        exact_labels = {
            "blocked:no_order": "PA 本轮判断不下单",
            (
                "blocked:risk:leverage:"
                "user_max_leverage_capacity_insufficient"
            ): "风险目标需要超过最大杠杆，本轮不下单",
            "execution:ready": "已生成执行计划，等待确认",
            "execution:submitting": "执行请求已排队，等待交易后台",
            "execution:entry_pending": "入场单已提交，等待成交",
            "execution:partially_filled": "入场单部分成交",
            "execution:protecting": "成交完成，正在建立保护",
            "execution:open": "持仓与保护已确认",
            "execution:exit_pending": "离场请求已提交，等待成交",
            "execution:closed": "本轮执行已关闭",
            "execution:canceled": "本轮执行已撤销",
            "execution:blocked": "执行被门禁阻断，订单未发出",
            "execution:rejected": "券商已明确拒绝请求",
            "execution:unknown": "执行结果待只读对账",
            "execution:error": "执行失败，查看技术详情",
            "failed:network_error": "模型或网络暂时不可用，本轮已跳过",
        }
        readable = exact_labels.get(last_result)
        if readable is not None:
            return readable
        if last_result.startswith("failed:"):
            return "PA 分析失败，本轮未生成订单"
        if last_result.startswith("blocked:risk:"):
            return "风险检查未通过，本轮不下单"
        if last_result.startswith("blocked:submit:"):
            return "提交前门禁已阻断，订单未发出"
        if last_result.startswith("blocked:"):
            return "本轮未生成订单"
        if last_result.startswith("script:hold:"):
            return "已有持仓正在受控管理"
        if last_result.startswith("execution:"):
            return "执行计划正在处理"
        if last_result:
            return "本轮结果待核对"
        if has_error:
            return "本轮处理失败，查看技术详情"
        return "监督流程已更新" if last_action else "尚无结果"

    def _campaign_risk_facts(
        self,
        payload: dict[str, Any],
        status_certainty: FactCertainty,
        source: str,
        observed_at: str,
    ) -> tuple[ReadFact, ReadFact]:
        names = (
            "frozen_risk_capital_cap_usdt",
            "frozen_risk_percent",
            "frozen_maximum_leverage",
        )
        raw_values = [payload.get(name) for name in names]
        if any(value is None for value in raw_values):
            missing = ReadFact(
                value="未记录（需由新的自动交易任务启动时冻结）",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
            return missing, missing
        try:
            capital, risk_percent, leverage = (
                Decimal(str(value)) for value in raw_values
            )
        except (InvalidOperation, ValueError):
            invalid = ReadFact(
                value="冻结风险参数格式无效",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
            return invalid, invalid
        if (
            capital < 0
            or not Decimal("0") < risk_percent <= Decimal("1")
            or leverage < 1
        ):
            invalid = ReadFact(
                value="冻结风险参数范围无效",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
            return invalid, invalid

        sizing_mode = str(
            payload.get("frozen_sizing_mode") or "risk_budget"
        ).strip()
        fixed_quantity: Decimal | None = None
        if sizing_mode == "fixed_quantity":
            try:
                fixed_quantity = Decimal(
                    str(payload.get("frozen_fixed_quantity"))
                )
            except (InvalidOperation, ValueError):
                fixed_quantity = None
            if fixed_quantity is None or fixed_quantity <= 0:
                invalid = ReadFact(
                    value="冻结固定张数无效",
                    certainty=FactCertainty.UNKNOWN,
                    source=source,
                    observed_at=observed_at,
                )
                return invalid, invalid
        elif sizing_mode != "risk_budget":
            invalid = ReadFact(
                value=f"冻结定仓模式无效（{sizing_mode or '空'}）",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
            return invalid, invalid

        if sizing_mode == "fixed_quantity":
            mode_text = f"固定 {fixed_quantity} 张"
        else:
            mode_text = f"按风险预算自动算张数 / 单笔风险 {risk_percent * 100}%"
        risk_fact = ReadFact(
            value=(
                f"{mode_text} / 资金上限 {capital} USDT / "
                f"杠杆上限 {leverage}×"
            ),
            certainty=status_certainty,
            source=source,
            observed_at=observed_at,
        )
        if status_certainty is FactCertainty.UNKNOWN:
            alignment = ReadFact(
                value="无法确认：自动交易状态不新鲜或无效",
                certainty=FactCertainty.UNKNOWN,
                source=source,
                observed_at=observed_at,
            )
            return risk_fact, alignment

        configured = self._settings.execution.okx
        configured_mode = str(
            getattr(configured, "sizing_mode", "risk_budget")
        )
        configured_fixed_quantity: Decimal | None = None
        if configured_mode == "fixed_quantity":
            try:
                configured_fixed_quantity = Decimal(
                    str(configured.quantity)
                )
            except (InvalidOperation, ValueError):
                configured_fixed_quantity = None
        current = (
            Decimal(str(configured.risk_capital_cap_usdt)),
            Decimal(str(configured.risk_percent)),
            Decimal(str(configured.maximum_leverage)),
            configured_mode,
            configured_fixed_quantity,
        )
        frozen = (
            capital,
            risk_percent,
            leverage,
            sizing_mode,
            fixed_quantity,
        )
        if current == frozen:
            alignment_value = "一致：界面配置与运行中的自动交易相同"
        else:
            alignment_value = (
                "不一致：运行中的自动交易仍使用上述启动值，"
                "界面新值尚未生效"
            )
        alignment = ReadFact(
            value=alignment_value,
            certainty=FactCertainty.CONFIRMED,
            source=f"{source} + settings.json",
            observed_at=observed_at,
        )
        return risk_fact, alignment
