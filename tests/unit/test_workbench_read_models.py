"""工作台只读读取层测试。"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from pa_agent.config.settings import Settings
from pa_agent.execution.models import AccountSnapshot, ExecutionState
from pa_agent.execution.worker_protocol import WorkerHeartbeat, WorkerState
from pa_agent.gui.read_models import FactCertainty, WorkbenchReadModel


class _ExecutionStore:
    def __init__(
        self,
        *,
        active=(),
        recent=(),
        account=None,
        accounts=None,
        by_id=None,
    ) -> None:
        self.active = list(active)
        self.recent = list(recent)
        self.account = account
        self.accounts = dict(accounts or {})
        self.by_id = dict(by_id or {})
        self.account_route: tuple[str, str] | None = None

    def list_active(self):
        return self.active

    def list_recent(self, limit: int = 50):
        assert limit == 1
        return self.recent

    def get(self, execution_id: str):
        return self.by_id.get(execution_id)

    def latest_account_snapshot(self, broker: str, account_profile: str):
        self.account_route = (broker, account_profile)
        if self.accounts:
            return self.accounts.get((broker, account_profile))
        return self.account


class _WorkerStore:
    def __init__(
        self,
        heartbeat: WorkerHeartbeat | None,
        *,
        risk_state=None,
        risk_states=None,
    ) -> None:
        self.heartbeat = heartbeat
        self.heartbeat_stale = False
        self.reconcile_stale = False
        self.risk_state = risk_state
        self.risk_states = dict(risk_states or {})
        self.risk_route: str | None = None

    def latest_heartbeat(self):
        return self.heartbeat

    def is_heartbeat_stale(self, worker_id: str, *, stale_after_seconds: int):
        assert worker_id == self.heartbeat.worker_id
        assert stale_after_seconds == 10
        return self.heartbeat_stale

    def is_reconcile_stale(self, worker_id: str, *, stale_after_seconds: int):
        assert worker_id == self.heartbeat.worker_id
        assert stale_after_seconds == 30
        return self.reconcile_stale

    def get_risk_runtime_state(self, route_key: str):
        self.risk_route = route_key
        if self.risk_states:
            return self.risk_states.get(route_key)
        return self.risk_state


def _settings() -> Settings:
    settings = Settings()
    settings.general.last_data_source = "okx"
    settings.general.last_symbol = "XAU-USDT-SWAP"
    settings.general.last_timeframe = "15m"
    settings.execution.selected_broker = "okx"
    settings.execution.okx.simulated = True
    settings.execution.okx.risk_capital_cap_usdt = 20000
    settings.execution.okx.risk_percent = 0.10
    settings.execution.okx.maximum_leverage = 20
    return settings


def _source(*, connected: bool, symbol: str, timeframe: str):
    return SimpleNamespace(
        _connected=connected,
        _symbol=symbol,
        _timeframe=timeframe,
    )


def _model(
    *,
    source,
    execution_store,
    worker_store,
    campaign_state_path: Path = Path("unit-test-campaign-does-not-exist.json"),
    settings: Settings | None = None,
    account_route: tuple[str, str] | None = None,
    control_route: tuple[str, str] | None = None,
):
    return WorkbenchReadModel(
        settings=settings or _settings(),
        data_source=source,
        execution_store=execution_store,
        worker_store=worker_store,
        clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        campaign_state_path=campaign_state_path,
        account_route=account_route,
        control_route=control_route,
    )


def _heartbeat(
    *,
    state: WorkerState = WorkerState.RUNNING,
    reconciled: bool = True,
) -> WorkerHeartbeat:
    now = datetime(2026, 7, 23, 11, 59, tzinfo=UTC)
    return WorkerHeartbeat(
        worker_id="worker-1",
        pid=123,
        started_at=now - timedelta(minutes=1),
        last_seen_at=now,
        last_successful_reconcile_at=now if reconciled else None,
        state=state,
    )


def _account(
    broker: str = "okx",
    account_profile: str = "okx-demo",
    *,
    captured_at: str = "2026-07-23T11:59:00+00:00",
) -> AccountSnapshot:
    return AccountSnapshot(
        broker=broker,
        account_profile=account_profile,
        captured_at=captured_at,
    )


def _risk_state(
    *,
    kill_active: bool = False,
    kill_reason: str = "",
):
    return SimpleNamespace(
        kill_active=kill_active,
        kill_reason=kill_reason,
        drawdown_fraction=0,
    )


def test_capture_separates_confirmed_subscription_from_plan_and_local_lifecycle():
    now = datetime(2026, 7, 23, 11, 59, tzinfo=UTC)
    heartbeat = WorkerHeartbeat(
        worker_id="worker-1",
        pid=123,
        started_at=now - timedelta(minutes=1),
        last_seen_at=now,
        last_successful_reconcile_at=now,
        state=WorkerState.RUNNING,
    )
    latest = SimpleNamespace(id="exec-1", state=ExecutionState.OPEN)
    execution_store = _ExecutionStore(
        active=[latest],
        recent=[latest],
        account=AccountSnapshot(
            broker="okx",
            account_profile="okx-demo",
            captured_at="2026-07-23T11:59:00+00:00",
        ),
    )
    worker_store = _WorkerStore(heartbeat)

    snapshot = _model(
        source=_source(
            connected=True,
            symbol="XAU-USDT-SWAP",
            timeframe="15m",
        ),
        execution_store=execution_store,
        worker_store=worker_store,
    ).capture()

    assert snapshot.source_kind.certainty is FactCertainty.PLAN
    assert snapshot.connection == snapshot.connection.__class__(
        value="已连接",
        certainty=FactCertainty.CONFIRMED,
        source="data source connection marker",
        observed_at="2026-07-23T12:00:00+00:00",
    )
    assert snapshot.symbol.certainty is FactCertainty.CONFIRMED
    assert snapshot.timeframe.certainty is FactCertainty.CONFIRMED
    assert snapshot.worker_state.certainty is FactCertainty.CONFIRMED
    assert snapshot.heartbeat.value == "新鲜"
    assert snapshot.reconcile.value == "新鲜"
    assert snapshot.account.certainty is FactCertainty.CONFIRMED
    assert snapshot.active_execution_count.value == "1"
    assert snapshot.latest_execution_state.value == "open"
    assert snapshot.latest_execution_state.certainty is FactCertainty.PLAN
    assert execution_store.account_route == ("okx", "okx-demo")


def test_capture_marks_old_account_snapshot_unknown():
    execution_store = _ExecutionStore(
        account=AccountSnapshot(
            broker="okx",
            account_profile="okx-demo",
            captured_at="2026-07-23T10:00:00+00:00",
        )
    )
    worker_store = _WorkerStore(None)

    snapshot = _model(
        source=_source(connected=False, symbol="", timeframe=""),
        execution_store=execution_store,
        worker_store=worker_store,
    ).capture()

    assert snapshot.account.certainty is FactCertainty.UNKNOWN
    assert "陈旧" in snapshot.account.value


def test_capture_keeps_missing_worker_and_account_as_unknown():
    execution_store = _ExecutionStore()
    worker_store = _WorkerStore(None)

    snapshot = _model(
        source=_source(connected=False, symbol="", timeframe=""),
        execution_store=execution_store,
        worker_store=worker_store,
    ).capture()

    assert snapshot.connection.value == "未连接"
    assert snapshot.connection.certainty is FactCertainty.CONFIRMED
    assert snapshot.symbol.certainty is FactCertainty.PLAN
    assert snapshot.timeframe.certainty is FactCertainty.PLAN
    assert snapshot.worker_state.certainty is FactCertainty.UNKNOWN
    assert snapshot.heartbeat.certainty is FactCertainty.UNKNOWN
    assert snapshot.reconcile.certainty is FactCertainty.UNKNOWN
    assert snapshot.account.certainty is FactCertainty.UNKNOWN
    assert snapshot.latest_execution_state.value == "无"
    assert snapshot.active_executions == ()
    assert snapshot.campaign_state.value == "未启动"
    assert snapshot.campaign_state.certainty is FactCertainty.UNKNOWN


@pytest.mark.parametrize(
    (
        "broker",
        "account_profile",
        "simulated",
        "expected_account_route",
        "expected_risk_route",
    ),
    [
        (
            "okx",
            "okx-demo",
            True,
            ("okx", "okx-demo"),
            "okx:demo:okx",
        ),
        (
            "okx",
            "okx-live",
            False,
            ("okx", "okx-live"),
            "okx:live:okx",
        ),
        (
            "longbridge",
            "paper",
            None,
            ("longbridge", "paper"),
            "longbridge:demo:paper",
        ),
    ],
)
def test_capture_maps_account_and_risk_routes_consistently(
    broker,
    account_profile,
    simulated,
    expected_account_route,
    expected_risk_route,
):
    settings = _settings()
    settings.execution.selected_broker = broker
    if broker == "okx":
        settings.execution.okx.simulated = simulated
    else:
        settings.execution.longbridge.preferred_account = account_profile
    execution_store = _ExecutionStore(
        account=_account(broker, account_profile),
    )
    worker_store = _WorkerStore(
        _heartbeat(),
        risk_state=_risk_state(),
    )

    snapshot = _model(
        settings=settings,
        source=_source(connected=True, symbol="XAU-USDT-SWAP", timeframe="10m"),
        execution_store=execution_store,
        worker_store=worker_store,
    ).capture()

    assert execution_store.account_route == expected_account_route
    assert worker_store.risk_route == expected_risk_route
    assert snapshot.route_alignment.certainty is FactCertainty.CONFIRMED
    assert snapshot.route_alignment.value.startswith("路由匹配")
    assert snapshot.risk_gate.value == "允许新增风险"
    assert snapshot.risk_gate.certainty is FactCertainty.CONFIRMED


def test_explicit_okx_demo_account_route_never_reads_selected_longbridge_data():
    settings = _settings()
    settings.execution.selected_broker = "longbridge"
    settings.execution.longbridge.preferred_account = "paper"
    okx_snapshot = _account("okx", "okx-demo")
    longbridge_snapshot = _account("longbridge", "paper")
    execution_store = _ExecutionStore(
        accounts={
            ("okx", "okx-demo"): okx_snapshot,
            ("longbridge", "paper"): longbridge_snapshot,
        }
    )
    worker_store = _WorkerStore(
        _heartbeat(),
        risk_states={
            "okx:demo:okx": _risk_state(),
            "longbridge:demo:paper": _risk_state(),
        },
    )

    snapshot = _model(
        settings=settings,
        account_route=("okx", "okx-demo"),
        source=_source(connected=True, symbol="XAU-USDT-SWAP", timeframe="10m"),
        execution_store=execution_store,
        worker_store=worker_store,
    ).capture()

    assert execution_store.account_route == ("okx", "okx-demo")
    assert worker_store.risk_route == "okx:demo:okx"
    assert snapshot.account_snapshot is okx_snapshot
    assert snapshot.route_alignment.certainty is FactCertainty.CONFIRMED
    assert snapshot.route_alignment.value == (
        "路由不匹配：页面 okx/okx-demo，控制器 longbridge/paper"
    )
    assert snapshot.risk_gate.certainty is FactCertainty.UNKNOWN
    assert "路由不一致" in snapshot.risk_gate.value


@pytest.mark.parametrize(
    "worker_state",
    [WorkerState.RUNNING, WorkerState.RECONCILING],
)
def test_risk_gate_allows_only_fresh_matched_active_route(worker_state):
    settings = _settings()
    settings.execution.selected_broker = "longbridge"
    settings.execution.longbridge.preferred_account = "paper"
    execution_store = _ExecutionStore(account=_account())
    worker_store = _WorkerStore(
        _heartbeat(state=worker_state),
        risk_state=_risk_state(),
    )

    snapshot = _model(
        settings=settings,
        account_route=("okx", "okx-demo"),
        control_route=("okx", "okx-demo"),
        source=_source(connected=True, symbol="XAU-USDT-SWAP", timeframe="10m"),
        execution_store=execution_store,
        worker_store=worker_store,
    ).capture()

    assert snapshot.route_alignment.value == "路由匹配（okx/okx-demo）"
    assert snapshot.route_alignment.certainty is FactCertainty.CONFIRMED
    assert snapshot.risk_stop.value.startswith("允许新增风险")
    assert snapshot.risk_gate.value == "允许新增风险"
    assert snapshot.risk_gate.certainty is FactCertainty.CONFIRMED


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("missing_risk_state", "尚无可信风险基线"),
        ("missing_heartbeat", "未观察到交易后台心跳"),
        ("inactive_worker", "交易后台状态为需要人工处理"),
        ("stale_heartbeat", "交易后台心跳陈旧"),
        ("missing_reconcile", "交易后台尚未完成首次对账"),
        ("stale_reconcile", "交易后台最近成功对账已陈旧"),
        ("missing_account", "未读取到账户快照"),
        ("stale_account", "账户快照陈旧"),
        ("route_mismatch", "页面账户与实际控制器路由不一致"),
    ],
)
def test_risk_gate_fails_closed_when_any_required_fact_is_missing_or_stale(
    failure,
    expected_reason,
):
    heartbeat = _heartbeat(
        state=(
            WorkerState.NEEDS_ATTENTION
            if failure == "inactive_worker"
            else WorkerState.RUNNING
        ),
        reconciled=failure != "missing_reconcile",
    )
    if failure == "missing_heartbeat":
        heartbeat = None
    account = (
        None
        if failure == "missing_account"
        else _account(
            captured_at=(
                "2026-07-23T10:00:00+00:00"
                if failure == "stale_account"
                else "2026-07-23T11:59:00+00:00"
            )
        )
    )
    worker_store = _WorkerStore(
        heartbeat,
        risk_state=(
            None
            if failure == "missing_risk_state"
            else _risk_state()
        ),
    )
    worker_store.heartbeat_stale = failure == "stale_heartbeat"
    worker_store.reconcile_stale = failure == "stale_reconcile"

    snapshot = _model(
        account_route=("okx", "okx-demo"),
        control_route=(
            ("okx", "okx-live")
            if failure == "route_mismatch"
            else ("okx", "okx-demo")
        ),
        source=_source(connected=True, symbol="XAU-USDT-SWAP", timeframe="10m"),
        execution_store=_ExecutionStore(account=account),
        worker_store=worker_store,
    ).capture()

    assert snapshot.risk_gate.certainty is FactCertainty.UNKNOWN
    assert snapshot.risk_gate.value.startswith("新增风险不可用：")
    assert expected_reason in snapshot.risk_gate.value


def test_persisted_kill_state_always_has_priority_over_other_failures():
    worker_store = _WorkerStore(
        None,
        risk_state=_risk_state(
            kill_active=True,
            kill_reason="drawdown_threshold_exceeded",
        ),
    )

    snapshot = _model(
        account_route=("okx", "okx-demo"),
        control_route=("okx", "okx-live"),
        source=_source(connected=False, symbol="", timeframe=""),
        execution_store=_ExecutionStore(),
        worker_store=worker_store,
    ).capture()

    assert snapshot.route_alignment.value.startswith("路由不匹配")
    assert snapshot.risk_stop.value == (
        "已停止新增风险：账户回撤达到停止线"
    )
    assert snapshot.risk_gate.value == snapshot.risk_stop.value
    assert snapshot.risk_gate.certainty is FactCertainty.CONFIRMED


def test_capture_uses_latest_current_campaign_execution_instead_of_global_latest(
    tmp_path,
):
    campaign_state_path = tmp_path / "okx_demo_campaign.json"
    campaign_state_path.write_text(
        json.dumps(
            {
                "status": "active",
                "execution_ids": ["campaign-old", "campaign-current"],
                "analyses_completed": 2,
                "analyses_failed": 0,
                "executions_prepared": 2,
                "last_plan_result": "execution:open",
                "frozen_risk_capital_cap_usdt": "20000",
                "frozen_risk_percent": "0.10",
                "frozen_maximum_leverage": "20",
                "updated_at": "2026-07-23T11:59:30+00:00",
            }
        ),
        encoding="utf-8",
    )
    foreign_newer = SimpleNamespace(
        id="controlled-canary",
        state=ExecutionState.CLOSED,
    )
    campaign_old = SimpleNamespace(
        id="campaign-old",
        state=ExecutionState.CANCELED,
    )
    campaign_current = SimpleNamespace(
        id="campaign-current",
        state=ExecutionState.OPEN,
    )

    snapshot = _model(
        source=_source(
            connected=True,
            symbol="XAU-USDT-SWAP",
            timeframe="10m",
        ),
        execution_store=_ExecutionStore(
            recent=[foreign_newer],
            by_id={
                "campaign-old": campaign_old,
                "campaign-current": campaign_current,
            },
        ),
        worker_store=_WorkerStore(None),
        campaign_state_path=campaign_state_path,
    ).capture()

    assert snapshot.campaign_execution_ids == (
        "campaign-old",
        "campaign-current",
    )
    assert snapshot.latest_execution is campaign_current
    assert snapshot.latest_execution_state.value == "open"


def test_risk_stop_hides_backend_exception_name_from_primary_ui():
    fact = WorkbenchReadModel._risk_stop_fact(
        SimpleNamespace(
            kill_active=True,
            kill_reason="risk_runtime_IncompleteRead",
        ),
        "2026-07-23T12:00:00+00:00",
    )

    assert fact.value == "已停止新增风险：风险账户数据读取中断"
    assert "IncompleteRead" not in fact.value


def test_capture_reads_fresh_ten_minute_campaign_state(tmp_path):
    campaign_state_path = tmp_path / "okx_demo_campaign.json"
    campaign_state_path.write_text(
        json.dumps(
            {
                "status": "active",
                "analyses_completed": 4,
                "analyses_failed": 0,
                "executions_prepared": 0,
                "last_plan_result": "blocked:no_order",
                "last_supervisor_action": "",
                "last_error": "",
                "frozen_risk_capital_cap_usdt": "20000",
                "frozen_risk_percent": "0.10",
                "frozen_maximum_leverage": "20",
                "updated_at": "2026-07-23T11:59:30+00:00",
            }
        ),
        encoding="utf-8",
    )

    snapshot = _model(
        source=_source(connected=True, symbol="XAU-USDT-SWAP", timeframe="10m"),
        execution_store=_ExecutionStore(),
        worker_store=_WorkerStore(None),
        campaign_state_path=campaign_state_path,
    ).capture()

    assert snapshot.campaign_state.value == "运行中（30 秒前更新）"
    assert snapshot.campaign_state.certainty is FactCertainty.CONFIRMED
    assert snapshot.campaign_progress.value == "分析 4 / 失败 0 / 生成执行 0"
    assert snapshot.campaign_last_result.value == "PA 本轮判断不下单"
    assert (
        snapshot.campaign_risk_parameters.value
        == "按风险预算自动算张数 / 单笔风险 10.00% / "
        "资金上限 20000 USDT / 杠杆上限 20×"
    )
    assert snapshot.campaign_config_alignment.value.startswith("一致")
    assert (
        snapshot.campaign_config_alignment.certainty
        is FactCertainty.CONFIRMED
    )


@pytest.mark.parametrize(
    ("last_plan_result", "raw_error", "expected"),
    [
        (
            "failed:network_error",
            "BrokerTransportError api_key=must-not-render",
            "模型或网络暂时不可用，本轮已跳过",
        ),
        (
            "failed:model_schema_error",
            "ValidationError internal field dump",
            "PA 分析失败，本轮未生成订单",
        ),
        (
            "blocked:risk:risk_runtime_BrokerTransportError",
            "risk_runtime_BrokerTransportError",
            "风险检查未通过，本轮不下单",
        ),
        (
            "execution:submitting",
            "",
            "执行请求已排队，等待交易后台",
        ),
        (
            "execution:protecting",
            "",
            "成交完成，正在建立保护",
        ),
        (
            "execution:closed",
            "",
            "本轮执行已关闭",
        ),
    ],
)
def test_campaign_primary_result_uses_structured_code_not_raw_error(
    tmp_path,
    last_plan_result,
    raw_error,
    expected,
):
    campaign_state_path = tmp_path / "okx_demo_campaign.json"
    campaign_state_path.write_text(
        json.dumps(
            {
                "status": "active",
                "analyses_completed": 4,
                "analyses_failed": 1,
                "executions_prepared": 1,
                "last_plan_result": last_plan_result,
                "last_supervisor_action": "",
                "last_error": raw_error,
                "frozen_risk_capital_cap_usdt": "20000",
                "frozen_risk_percent": "0.10",
                "frozen_maximum_leverage": "20",
                "updated_at": "2026-07-23T11:59:30+00:00",
            }
        ),
        encoding="utf-8",
    )

    snapshot = _model(
        source=_source(
            connected=True,
            symbol="XAU-USDT-SWAP",
            timeframe="10m",
        ),
        execution_store=_ExecutionStore(),
        worker_store=_WorkerStore(None),
        campaign_state_path=campaign_state_path,
    ).capture()

    assert snapshot.campaign_last_result.value == expected
    if raw_error:
        assert raw_error not in snapshot.campaign_last_result.value


def test_campaign_primary_result_does_not_render_raw_supervisor_action():
    assert (
        WorkbenchReadModel._campaign_result_text(
            last_result="",
            last_action="internal_supervisor_action:payload",
            has_error=False,
        )
        == "监督流程已更新"
    )


def test_capture_marks_stale_active_campaign_unknown(tmp_path):
    campaign_state_path = tmp_path / "okx_demo_campaign.json"
    campaign_state_path.write_text(
        json.dumps(
            {
                "status": "active",
                "analyses_completed": 4,
                "analyses_failed": 0,
                "executions_prepared": 0,
                "last_plan_result": "blocked:no_order",
                "last_supervisor_action": "",
                "last_error": "",
                "frozen_risk_capital_cap_usdt": "20000",
                "frozen_risk_percent": "0.10",
                "frozen_maximum_leverage": "20",
                "updated_at": "2026-07-23T11:40:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    snapshot = _model(
        source=_source(connected=True, symbol="XAU-USDT-SWAP", timeframe="10m"),
        execution_store=_ExecutionStore(),
        worker_store=_WorkerStore(None),
        campaign_state_path=campaign_state_path,
    ).capture()

    assert "已 1200 秒未更新" in snapshot.campaign_state.value
    assert snapshot.campaign_state.certainty is FactCertainty.UNKNOWN
    assert snapshot.campaign_progress.certainty is FactCertainty.UNKNOWN
    assert snapshot.campaign_last_result.certainty is FactCertainty.UNKNOWN
    assert (
        snapshot.campaign_risk_parameters.certainty
        is FactCertainty.UNKNOWN
    )
    assert snapshot.campaign_config_alignment.certainty is FactCertainty.UNKNOWN


@pytest.mark.parametrize(
    ("status", "updated_at", "expected"),
    [
        ("unexpected", "2026-07-23T11:59:30+00:00", "未知状态 unexpected"),
        (
            "active",
            "2026-07-23T12:02:00+00:00",
            "状态时间比本机快 120 秒",
        ),
    ],
)
def test_capture_does_not_confirm_unknown_or_future_campaign_state(
    tmp_path,
    status,
    updated_at,
    expected,
):
    campaign_state_path = tmp_path / "okx_demo_campaign.json"
    campaign_state_path.write_text(
        json.dumps(
            {
                "status": status,
                "analyses_completed": 4,
                "analyses_failed": 0,
                "executions_prepared": 0,
                "last_plan_result": "blocked:no_order",
                "frozen_risk_capital_cap_usdt": "20000",
                "frozen_risk_percent": "0.10",
                "frozen_maximum_leverage": "20",
                "updated_at": updated_at,
            }
        ),
        encoding="utf-8",
    )

    snapshot = _model(
        source=_source(connected=True, symbol="XAU-USDT-SWAP", timeframe="10m"),
        execution_store=_ExecutionStore(),
        worker_store=_WorkerStore(None),
        campaign_state_path=campaign_state_path,
    ).capture()

    assert expected in snapshot.campaign_state.value
    assert snapshot.campaign_state.certainty is FactCertainty.UNKNOWN
    assert snapshot.campaign_progress.certainty is FactCertainty.UNKNOWN
    assert snapshot.campaign_last_result.certainty is FactCertainty.UNKNOWN
    assert snapshot.campaign_config_alignment.certainty is FactCertainty.UNKNOWN


@pytest.mark.parametrize("status", ["stopping", "needs_attention", "completed"])
def test_capture_marks_every_stale_campaign_status_unknown(tmp_path, status):
    campaign_state_path = tmp_path / "okx_demo_campaign.json"
    campaign_state_path.write_text(
        json.dumps(
            {
                "status": status,
                "analyses_completed": 4,
                "analyses_failed": 0,
                "executions_prepared": 0,
                "last_plan_result": "blocked:no_order",
                "frozen_risk_capital_cap_usdt": "20000",
                "frozen_risk_percent": "0.10",
                "frozen_maximum_leverage": "20",
                "updated_at": "2026-07-23T11:40:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    snapshot = _model(
        source=_source(connected=True, symbol="XAU-USDT-SWAP", timeframe="10m"),
        execution_store=_ExecutionStore(),
        worker_store=_WorkerStore(None),
        campaign_state_path=campaign_state_path,
    ).capture()

    assert "已 1200 秒未更新" in snapshot.campaign_state.value
    assert snapshot.campaign_state.certainty is FactCertainty.UNKNOWN
    assert snapshot.campaign_progress.certainty is FactCertainty.UNKNOWN
    assert snapshot.campaign_last_result.certainty is FactCertainty.UNKNOWN
    assert snapshot.campaign_config_alignment.certainty is FactCertainty.UNKNOWN


def test_capture_exposes_frozen_campaign_risk_mismatch(tmp_path):
    campaign_state_path = tmp_path / "okx_demo_campaign.json"
    campaign_state_path.write_text(
        json.dumps(
            {
                "status": "active",
                "analyses_completed": 4,
                "analyses_failed": 0,
                "executions_prepared": 0,
                "frozen_risk_capital_cap_usdt": "5000",
                "frozen_risk_percent": "0.08",
                "frozen_maximum_leverage": "25",
                "updated_at": "2026-07-23T11:59:30+00:00",
            }
        ),
        encoding="utf-8",
    )

    snapshot = _model(
        source=_source(connected=True, symbol="XAU-USDT-SWAP", timeframe="10m"),
        execution_store=_ExecutionStore(),
        worker_store=_WorkerStore(None),
        campaign_state_path=campaign_state_path,
    ).capture()

    assert "资金上限 5000 USDT" in snapshot.campaign_risk_parameters.value
    assert snapshot.campaign_config_alignment.value.startswith("不一致")
    assert (
        snapshot.campaign_config_alignment.certainty
        is FactCertainty.CONFIRMED
    )


def test_capture_rejects_naive_clock():
    model = WorkbenchReadModel(
        settings=_settings(),
        data_source=_source(connected=False, symbol="", timeframe=""),
        execution_store=_ExecutionStore(),
        worker_store=_WorkerStore(None),
        clock=lambda: datetime(2026, 7, 23, 12, 0),
    )

    with pytest.raises(RuntimeError, match="带时区"):
        model.capture()
