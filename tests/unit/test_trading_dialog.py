from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from pa_agent.config.settings import Settings
from pa_agent.execution.models import (
    AccountSnapshot,
    ExecutionEvent,
    ExecutionPlan,
    ExecutionRecord,
    ExecutionState,
    PositionSnapshot,
)
from pa_agent.execution.worker_protocol import WorkerCommandStatus
from pa_agent.gui.read_models import FactCertainty
from pa_agent.gui.trading_dialog import TradingDialog


class EmptyStore:
    def list_recent(self, limit=30):
        return []

    def get(self, _execution_id):
        return None


class FakeService:
    def __init__(self, *, armed=False):
        self.is_armed = armed
        self.store = EmptyStore()
        self.disarm_calls = 0
        self.reload_calls = 0
        self.refresh_account_calls = []
        self.health_snapshot = {
            "available": False,
            "process_healthy": False,
            "reconcile_healthy": False,
            "state": "missing",
            "last_successful_reconcile_at": None,
            "last_error_code": "",
        }

    def latest_execution(self):
        rows = self.store.list_recent(limit=1)
        return rows[0] if rows else None

    def get_execution(self, execution_id):
        return self.store.get(execution_id)

    def list_recent(self, *, limit=30):
        return self.store.list_recent(limit=limit)

    def events(self, execution_id):
        method = getattr(self.store, "events", None)
        return method(execution_id) if callable(method) else []

    def latest_account_snapshot(self, broker, account_profile):
        method = getattr(self.store, "latest_account_snapshot", None)
        if callable(method):
            return method(broker, account_profile)
        return None

    def disarm(self):
        self.is_armed = False
        self.disarm_calls += 1

    def reload_settings(self, _settings):
        self.is_armed = False
        self.reload_calls += 1

    def refresh_account(self, execution_id=None):
        self.refresh_account_calls.append(execution_id)

    def worker_health_snapshot(self):
        return self.health_snapshot


class _ErrorBus:
    def __init__(self):
        self.errors = []
        self.event = threading.Event()

    def emit_execution_error(self, message):
        self.errors.append(message)
        self.event.set()


class _CommandService(FakeService):
    def __init__(self, result=None, error=None):
        super().__init__()
        self.result = result
        self.error = error

    def refresh_account(self, execution_id=None):
        super().refresh_account(execution_id)
        return SimpleNamespace(id="command-1")

    def wait_for_command(self, command_id, *, timeout):
        assert command_id == "command-1"
        assert timeout == 30.0
        if self.error:
            raise self.error
        return self.result


class _CampaignReadModel:
    def capture(self):
        return SimpleNamespace(
            campaign_state=SimpleNamespace(value="运行中（5 秒前更新）"),
            campaign_progress=SimpleNamespace(
                value="分析 4 / 失败 0 / 生成执行 0"
            ),
            campaign_last_result=SimpleNamespace(value="blocked:no_order"),
            campaign_risk_parameters=SimpleNamespace(
                value=(
                    "资金上限 20000 USDT / 单笔风险 10.00% / "
                    "杠杆上限 20×"
                )
            ),
            campaign_config_alignment=SimpleNamespace(
                value="一致：GUI 配置与运行中 Campaign 相同",
                certainty=FactCertainty.CONFIRMED,
            ),
        )


def _execution(
    execution_id: str,
    *,
    broker: str = "okx",
    environment: str = "demo",
    account: str = "okx",
    error: str = "",
) -> ExecutionRecord:
    product = "swap" if broker == "okx" else "securities"
    instrument = "XAU-USDT-SWAP" if broker == "okx" else "GLD.US"
    plan = ExecutionPlan(
        id=f"plan-{execution_id}",
        analysis_digest=f"digest-{execution_id}",
        analysis_record_path=f"records/{execution_id}.json",
        broker=broker,
        environment=environment,
        product=product,
        requested_account=account,
        source_symbol="XAUUSD",
        instrument=instrument,
        direction="long",
        entry_type="limit",
        quantity=Decimal("2"),
        entry_price=Decimal("100"),
        take_profit_1=Decimal("110"),
        take_profit_2=Decimal("120"),
        stop_loss=Decimal("95"),
        trade_confidence=88,
        created_at="2026-07-18T00:00:00+00:00",
        config_fingerprint=f"fingerprint-{execution_id}",
        okx_margin_mode="cross" if broker == "okx" else "",
    )
    broker_state = (
        {
            "protection_targets": [
                {"algo_id": "algo-1", "state": "live"},
                {"client_order_id": "protect-2", "state": "planned"},
            ],
            "exit_order": {
                "client_order_id": "exit-client",
                "state": "submitting",
            },
        }
        if broker == "okx"
        else {
            "stop_order": {"order_id": "stop-1", "state": "submitted"},
            "partial_exit": {"order_id": "exit-1", "state": "pending"},
        }
    )
    return ExecutionRecord(
        id=execution_id,
        plan=plan,
        state=ExecutionState.UNKNOWN if error else ExecutionState.OPEN,
        selected_account=account,
        client_order_id=f"client-{execution_id}",
        broker_order_id=f"broker-{execution_id}",
        filled_quantity=Decimal("1.5"),
        remaining_quantity=Decimal("0.5"),
        broker_state=broker_state,
        state_reason="等待只读对账" if error else "保护已建立",
        last_error=error,
        needs_attention=bool(error),
    )


class LifecycleStore:
    def __init__(self, records, snapshots):
        self._records = list(records)
        self._snapshots = {
            (snapshot.broker, snapshot.account_profile): snapshot
            for snapshot in snapshots
        }

    def list_recent(self, limit=30):
        return self._records[:limit]

    def get(self, execution_id):
        return next(
            (
                record
                for record in self._records
                if record.id == execution_id
            ),
            None,
        )

    def events(self, execution_id):
        return [
            ExecutionEvent(execution_id=execution_id, kind="plan_ready"),
            ExecutionEvent(execution_id=execution_id, kind="reconciled"),
        ]

    def latest_account_snapshot(self, broker, account_profile):
        return self._snapshots.get((broker, account_profile))


def test_dialog_round_trips_longbridge_route_without_credentials(qtbot):
    settings = Settings()
    dialog = TradingDialog(
        settings=settings,
        service=FakeService(),
    )
    qtbot.addWidget(dialog)

    dialog._enabled.setChecked(True)
    dialog._auto_execute.setChecked(True)
    dialog._broker.setCurrentIndex(dialog._broker.findData("longbridge"))
    dialog._lb_source.setText("GLD.US")
    dialog._lb_instrument.setText("GLD.US")
    dialog._lb_quantity.setText("10")
    dialog._lb_account.setCurrentIndex(
        dialog._lb_account.findData("intraday")
    )
    dialog._lb_fallback.setChecked(True)
    dialog._apply_widgets()

    assert settings.execution.enabled is True
    assert settings.execution.auto_execute is True
    assert settings.execution.longbridge.instrument == "GLD.US"
    assert settings.execution.longbridge.quantity == "10"
    assert settings.execution.longbridge.preferred_account == "intraday"
    assert settings.execution.longbridge.allow_comprehensive_fallback is True


def test_dialog_round_trips_okx_fixed_risk_controls(qtbot):
    settings = Settings()
    dialog = TradingDialog(
        settings=settings,
        service=FakeService(),
    )
    qtbot.addWidget(dialog)

    dialog._broker.setCurrentIndex(dialog._broker.findData("okx"))
    dialog._okx_risk_capital_cap.setValue(5000)
    dialog._okx_risk_percent.setValue(8)
    dialog._okx_maximum_leverage.setValue(25)
    dialog._apply_widgets()

    assert settings.execution.okx.risk_capital_cap_usdt == Decimal("5000")
    assert settings.execution.okx.risk_percent == Decimal("0.08")
    assert settings.execution.okx.maximum_leverage == Decimal("25")


def test_dialog_shows_okx_fixed_proxy_route(qtbot, monkeypatch):
    monkeypatch.setattr(
        "pa_agent.gui.trading_dialog.okx_fixed_proxy_label",
        lambda: "橘子云 / V1-137|美国|x2.0",
    )
    dialog = TradingDialog(
        settings=Settings(),
        service=FakeService(),
    )
    qtbot.addWidget(dialog)

    assert "橘子云 / V1-137|美国|x2.0" in dialog._okx_network_route.text()
    assert "127.0.0.1:10981" in dialog._okx_network_route.text()
    assert "不跟随 v2rayN 当前节点" in dialog._okx_network_route.text()


def test_dialog_shows_same_campaign_state_as_backend(qtbot):
    dialog = TradingDialog(
        settings=Settings(),
        service=FakeService(),
        read_model=_CampaignReadModel(),
    )
    qtbot.addWidget(dialog)

    assert "运行中（5 秒前更新）" in dialog._campaign_status_label.text()
    assert "分析 4 / 失败 0 / 生成执行 0" in (
        dialog._campaign_status_label.text()
    )
    assert "blocked:no_order" in dialog._campaign_status_label.text()


@pytest.mark.parametrize(
    ("result", "error", "expected"),
    [
        (
            SimpleNamespace(
                status=WorkerCommandStatus.FAILED,
                failure_code="hard_gate_closed",
            ),
            None,
            "hard_gate_closed",
        ),
        (
            SimpleNamespace(
                status=WorkerCommandStatus.UNCERTAIN,
                failure_code="",
            ),
            None,
            "禁止重复提交",
        ),
        (
            None,
            TimeoutError("still running"),
            "结果尚未确定",
        ),
    ],
)
def test_async_command_terminal_state_is_visible(
    qtbot,
    result,
    error,
    expected,
):
    bus = _ErrorBus()
    service = _CommandService(result=result, error=error)
    dialog = TradingDialog(
        settings=Settings(),
        service=service,
    )
    dialog._event_bus = bus
    qtbot.addWidget(dialog)

    dialog._run_async(
        lambda: service.refresh_account(),
        "账户刷新失败",
    )

    assert bus.event.wait(2)
    assert expected in bus.errors[-1]
    if error is not None:
        assert "command-1" in bus.errors[-1]


def test_dialog_switches_paper_live_profiles_without_cross_environment_options(qtbot):
    settings = Settings()
    settings.execution.longbridge.allow_outside_rth = True
    dialog = TradingDialog(settings=settings, service=FakeService())
    qtbot.addWidget(dialog)

    dialog._broker.setCurrentIndex(dialog._broker.findData("longbridge"))
    dialog._lb_account.setCurrentIndex(dialog._lb_account.findData("paper"))

    assert dialog._lb_fallback.isEnabled() is False
    assert dialog._lb_outside_rth.isEnabled() is False
    assert dialog._arm_button.text() == "启用本次模拟会话"

    dialog._apply_widgets()
    assert settings.execution.longbridge.preferred_account == "paper"

    dialog._lb_account.setCurrentIndex(dialog._lb_account.findData("intraday"))
    assert dialog._lb_fallback.isEnabled() is True
    assert dialog._lb_outside_rth.isEnabled() is True


@pytest.mark.parametrize(
    ("saved_profile", "selected_profile"),
    [
        ("comprehensive", "paper"),
        ("paper", "comprehensive"),
        ("intraday", "paper"),
    ],
)
def test_unsaved_profile_switch_disarms_and_blocks_trade_actions(
    qtbot,
    saved_profile,
    selected_profile,
):
    settings = Settings()
    settings.execution.longbridge.preferred_account = saved_profile
    service = FakeService(armed=True)
    dialog = TradingDialog(settings=settings, service=service)
    qtbot.addWidget(dialog)

    dialog._lb_account.setCurrentIndex(
        dialog._lb_account.findData(selected_profile)
    )

    assert dialog._configuration_dirty is True
    assert service.disarm_calls == 1
    assert service.is_armed is False
    assert dialog._arm_button.isEnabled() is False
    assert dialog._execute_button.isEnabled() is False
    assert dialog._cancel_button.isEnabled() is True
    assert dialog._exit_button.isEnabled() is True
    assert "请先保存配置" in dialog._arm_label.text()


def test_saving_profile_switch_clears_dirty_guard_and_keeps_session_disarmed(
    qtbot,
    monkeypatch,
):
    settings = Settings()
    settings.execution.longbridge.preferred_account = "comprehensive"
    service = FakeService(armed=True)
    dialog = TradingDialog(settings=settings, service=service)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        "pa_agent.gui.trading_dialog.save_settings",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "pa_agent.gui.trading_dialog.QMessageBox.information",
        lambda *_args, **_kwargs: None,
    )

    dialog._lb_account.setCurrentIndex(dialog._lb_account.findData("paper"))
    dialog._save_configuration()

    assert settings.execution.longbridge.preferred_account == "paper"
    assert dialog._configuration_dirty is False
    assert service.reload_calls == 1
    assert service.is_armed is False
    assert dialog._arm_button.isEnabled() is True
    assert dialog._execute_button.isEnabled() is True


def test_saving_order_modes_and_atr_multiples_updates_settings(
    qtbot,
    monkeypatch,
):
    settings = Settings()
    service = FakeService()
    dialog = TradingDialog(settings=settings, service=service)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        "pa_agent.gui.trading_dialog.save_settings",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "pa_agent.gui.trading_dialog.QMessageBox.information",
        lambda *_args, **_kwargs: None,
    )

    dialog._entry_order_mode.setCurrentIndex(
        dialog._entry_order_mode.findData("limit_with_slippage")
    )
    dialog._exit_order_mode.setCurrentIndex(
        dialog._exit_order_mode.findData("limit")
    )
    dialog._entry_slippage_atr.setValue(0.75)
    dialog._exit_slippage_atr.setValue(0.25)
    dialog._save_configuration()

    assert settings.execution.entry_order_mode == "limit_with_slippage"
    assert settings.execution.exit_order_mode == "limit"
    assert settings.execution.entry_slippage_atr_multiple == Decimal("0.75")
    assert settings.execution.exit_slippage_atr_multiple == Decimal("0.25")
    assert service.reload_calls == 1


def test_save_failure_keeps_memory_disk_and_service_on_old_configuration(
    qtbot,
    monkeypatch,
    tmp_path,
):
    settings = Settings()
    settings.execution.okx.risk_capital_cap_usdt = Decimal("20000")
    service = FakeService()
    dialog = TradingDialog(settings=settings, service=service)
    qtbot.addWidget(dialog)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("old-settings", encoding="utf-8")
    monkeypatch.setattr(
        "pa_agent.gui.trading_dialog.SETTINGS_JSON_PATH",
        settings_path,
    )

    def _fail_save(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        "pa_agent.gui.trading_dialog.save_settings",
        _fail_save,
    )
    monkeypatch.setattr(
        "pa_agent.gui.trading_dialog.QMessageBox.critical",
        lambda *_args, **_kwargs: None,
    )
    dialog._okx_risk_capital_cap.setValue(5000)

    dialog._save_configuration()

    assert settings.execution.okx.risk_capital_cap_usdt == Decimal("20000")
    assert settings_path.read_text(encoding="utf-8") == "old-settings"
    assert service.reload_calls == 0


def test_dialog_switches_okx_product_and_margin_controls(qtbot):
    settings = Settings()
    dialog = TradingDialog(settings=settings, service=FakeService())
    qtbot.addWidget(dialog)

    dialog._broker.setCurrentIndex(dialog._broker.findData("okx"))
    dialog._okx_product.setCurrentIndex(dialog._okx_product.findData("spot"))
    dialog._sync_okx_margin_enabled()
    assert dialog._route_stack.currentIndex() == 1
    assert dialog._okx_margin.isEnabled() is False

    dialog._okx_product.setCurrentIndex(dialog._okx_product.findData("swap"))
    dialog._sync_okx_margin_enabled()
    assert dialog._okx_margin.isEnabled() is True


def test_okx_demo_is_clearly_labelled_as_simulated(qtbot):
    settings = Settings()
    dialog = TradingDialog(settings=settings, service=FakeService())
    qtbot.addWidget(dialog)

    dialog._broker.setCurrentIndex(dialog._broker.findData("okx"))
    dialog._okx_simulated.setChecked(True)

    assert dialog._arm_button.text() == "启用本次模拟会话"
    dialog._configuration_dirty = False
    dialog._update_arm_state(True)
    assert dialog._arm_label.text() == "桌面手动会话：已启用模拟写操作"


def test_dialog_separates_worker_heartbeat_from_reconciliation_health(qtbot):
    settings = Settings()
    service = FakeService()
    service.health_snapshot = {
        "available": True,
        "process_healthy": True,
        "reconcile_healthy": False,
        "state": "running",
        "last_successful_reconcile_at": datetime(
            2026,
            7,
            20,
            8,
            30,
            tzinfo=UTC,
        ),
        "last_error_code": "",
    }
    dialog = TradingDialog(settings=settings, service=service)
    qtbot.addWidget(dialog)

    assert "心跳正常 / running" in dialog._worker_health_label.text()
    assert "最近成功对账：2026-07-20T08:30:00+00:00（已陈旧）" in (
        dialog._worker_health_label.text()
    )


def test_invalid_route_edit_does_not_partially_mutate_saved_settings(qtbot):
    settings = Settings()
    dialog = TradingDialog(settings=settings, service=FakeService())
    qtbot.addWidget(dialog)
    dialog._enabled.setChecked(True)
    dialog._okx_base_url.setText("http://insecure.example")

    with pytest.raises(ValueError, match="https"):
        dialog._apply_widgets()

    assert settings.execution.enabled is False
    assert settings.execution.okx.api_base_url == "https://www.okx.com"


def test_account_update_renders_and_clears_positions(qtbot):
    settings = Settings()
    dialog = TradingDialog(settings=settings, service=FakeService())
    qtbot.addWidget(dialog)
    snapshot = AccountSnapshot(
        broker="okx",
        account_profile="okx-live",
        base_currency="USDT",
        positions=[
            PositionSnapshot(
                instrument="XAUT",
                direction="long",
                quantity=Decimal("1.5"),
                available_quantity=Decimal("0.5"),
                currency="XAUT",
                raw={"kind": "spot_balance"},
            ),
            PositionSnapshot(
                instrument="BTC-USDT-SWAP",
                direction="short",
                quantity=Decimal("2"),
                unrealized_pnl=Decimal("3"),
                currency="USDT",
            ),
        ],
    )

    dialog._on_account_update(snapshot)

    assert dialog._positions_table.rowCount() == 2
    assert dialog._positions_table.item(0, 1).text() == "持币"
    assert dialog._positions_table.item(1, 1).text() == "空"

    dialog._on_account_update(
        snapshot.model_copy(update={"positions": []})
    )
    assert dialog._positions_table.rowCount() == 0


def test_selected_execution_shows_lifecycle_error_and_matching_account(qtbot):
    settings = Settings()
    okx_record = _execution("okx-demo")
    longbridge_record = _execution(
        "lb-live",
        broker="longbridge",
        environment="live",
        account="comprehensive",
        error="券商返回状态未知",
    )
    service = FakeService()
    service.store = LifecycleStore(
        [okx_record, longbridge_record],
        [
            AccountSnapshot(
                broker="okx",
                account_profile="okx-demo",
                base_currency="USDT",
                equity=Decimal("5000"),
            ),
            AccountSnapshot(
                broker="longbridge",
                account_profile="comprehensive",
                base_currency="USD",
                equity=Decimal("1200"),
            ),
        ],
    )
    dialog = TradingDialog(settings=settings, service=service)
    qtbot.addWidget(dialog)

    dialog._execution_table.selectRow(1)

    assert "成交：1.5" in dialog._execution_detail.text()
    assert "剩余：0.5" in dialog._execution_detail.text()
    assert "stop-1" in dialog._execution_detail.text()
    assert "exit-1" in dialog._execution_detail.text()
    assert "券商返回状态未知" in dialog._execution_detail.text()
    assert dialog._account_profile_label.text() == "longbridge / comprehensive"
    assert dialog._equity_label.text() == "1200 USD"
    assert dialog._execution_table.item(1, 10).text() == "券商返回状态未知"


def test_refresh_account_uses_selected_historical_execution_route(qtbot):
    settings = Settings()
    settings.execution.selected_broker = "okx"
    longbridge_record = _execution(
        "lb-live",
        broker="longbridge",
        environment="live",
        account="intraday",
    )
    service = FakeService()
    service.store = LifecycleStore([longbridge_record], [])
    dialog = TradingDialog(settings=settings, service=service)
    qtbot.addWidget(dialog)
    dialog._configuration_dirty = False
    dialog._run_async = lambda action, _label: action()

    dialog._execution_table.selectRow(0)
    dialog._refresh_account()

    assert service.refresh_account_calls == ["lb-live"]
