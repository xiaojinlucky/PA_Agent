from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from pa_agent.config.settings import Settings
from pa_agent.execution.models import ExecutionState
from pa_agent.gui.read_models import FactCertainty
from pa_agent.gui.trading_workbench import (
    TradingWorkbench,
    _event_label,
    _local_time,
    _protection_overview,
    _result_text,
    _risk_text,
    _take_profit_text,
)


class _Service:
    def __init__(self) -> None:
        self.is_armed = False
        self.disarm_calls = 0
        self.reload_calls = 0
        self.recovery_calls = 0

    def disarm(self):
        self.is_armed = False
        self.disarm_calls += 1

    def reload_settings(self, _settings):
        self.is_armed = False
        self.reload_calls += 1

    def list_recent(self, *, limit=12):
        assert limit == 100
        return []

    def latest_execution(self):
        return None

    def get_execution(self, _execution_id):
        return None

    def events(self, _execution_id):
        return []

    def recover_transient_risk_stop(self):
        self.recovery_calls += 1


class _ReadModel:
    def capture(self):
        return SimpleNamespace(
            captured_at="2026-07-25T01:00:00+00:00",
            campaign_state=SimpleNamespace(
                value="运行中（5 秒前更新）",
                source="campaign.json",
            ),
            worker_state=SimpleNamespace(value="运行中"),
            reconcile=SimpleNamespace(value="新鲜"),
            risk_stop=SimpleNamespace(
                value="允许新增风险；当前回撤 1.00%",
                certainty=FactCertainty.CONFIRMED,
            ),
            campaign_risk_parameters=SimpleNamespace(
                value=(
                    "按风险预算自动算张数 / 单笔风险 10.00% / "
                    "资金上限 20000 USDT / 杠杆上限 20×"
                )
            ),
            campaign_config_alignment=SimpleNamespace(
                value="一致：GUI 配置与运行中 Campaign 相同",
                certainty=FactCertainty.CONFIRMED,
            ),
            campaign_last_result=SimpleNamespace(value="PA 本轮判断不下单"),
            campaign_execution_ids=(),
            account=SimpleNamespace(
                source="execution.sqlite3",
                certainty=FactCertainty.UNKNOWN,
            ),
            account_snapshot=None,
            latest_execution=None,
            risk_runtime_state=None,
        )


def _widget(qtbot):
    settings = Settings()
    settings.execution.selected_broker = "okx"
    settings.execution.okx.simulated = True
    settings.execution.okx.sizing_mode = "risk_budget"
    settings.execution.okx.risk_capital_cap_usdt = 20000
    settings.execution.okx.risk_percent = 0.10
    settings.execution.okx.maximum_leverage = 20
    settings.execution.okx.quantity = "1000"
    service = _Service()
    widget = TradingWorkbench(
        settings=settings,
        service=service,
        read_model=_ReadModel(),
    )
    qtbot.addWidget(widget)
    return widget, settings, service


def _execution_record(*, state=ExecutionState.OPEN):
    return SimpleNamespace(
        id="current-execution",
        state=state,
        state_reason="",
        last_error="",
        realized_pnl=None,
        pnl_currency="USDT",
        broker_state={"protection_targets": []},
        client_order_id="",
        broker_order_id="",
        plan=SimpleNamespace(
            broker="okx",
            environment="demo",
            instrument="XAU-USDT-SWAP",
            direction="short",
            quantity=Decimal("100"),
            entry_price=Decimal("4059.4"),
            stop_loss=Decimal("4064.8"),
            take_profit_1=Decimal("4057.5"),
            take_profit_2=Decimal("4050.2"),
            authorized_sizing_mode="risk_budget",
            authorized_risk_budget_usdt=Decimal("2000"),
            authorized_risk_used_usdt=Decimal("1900"),
            authorized_risk_percent=Decimal("0.10"),
            authorized_effective_risk_capital_usdt=Decimal("20000"),
            authorized_worst_case_loss_per_contract_usdt=Decimal("2"),
            authorized_contract_notional_usdt=Decimal("10"),
        ),
    )


def test_workbench_exposes_spacious_top_level_sections(qtbot, monkeypatch):
    monkeypatch.setattr(
        "pa_agent.gui.trading_workbench.okx_fixed_proxy_label",
        lambda: "橘子云 / V1-137|美国|x2.0",
    )
    widget, _settings, _service = _widget(qtbot)

    assert widget.findChild(type(widget._sizing_mode), "sizingMode") is not None
    assert widget._campaign_status.text().startswith("模拟交易：运行中")
    assert widget._active_config.text().startswith(
        "当前运行参数：按风险预算"
    )
    assert widget._execution_table.columnCount() == 7
    assert widget._technical_group.isChecked() is False
    assert widget._technical_body.isHidden() is True
    assert widget._submit_button.isVisible() is False
    assert widget._cancel_button.isHidden() is True
    assert widget._exit_button.isHidden() is True
    assert "橘子云 / V1-137|美国|x2.0" in widget._network_route.text()
    assert "127.0.0.1:10981" in widget._network_route.text()
    assert "独立于 v2rayN 当前节点" in widget._network_route.text()


def test_switching_to_fixed_quantity_disarms_old_manual_session(qtbot):
    widget, _settings, service = _widget(qtbot)

    widget._sizing_mode.setCurrentIndex(
        widget._sizing_mode.findData("fixed_quantity")
    )
    widget._fixed_quantity.setValue(1234)
    candidate = widget._candidate_settings()

    assert service.disarm_calls == 1
    assert candidate.execution.okx.sizing_mode == "fixed_quantity"
    assert candidate.execution.okx.quantity == "1234"
    assert candidate.execution.okx.risk_percent == Decimal("0.10")
    assert widget._mode_stack.currentIndex() == 1
    assert widget._save_button.isEnabled() is True
    assert "反算结果" in widget._fixed_preview.text()
    widget.refresh_now()
    assert "当前编辑尚未保存" in widget._alignment.text()
    assert widget._dirty_banner.isHidden() is False


def test_cancel_configuration_edit_restores_saved_values(qtbot):
    widget, _settings, _service = _widget(qtbot)

    widget._capital_cap.setValue(12345)
    assert widget._configuration_dirty is True

    widget._cancel_configuration_edit()

    assert widget._capital_cap.value() == 20000
    assert widget._configuration_dirty is False
    assert widget._dirty_banner.isHidden() is True
    assert widget._save_button.isEnabled() is False


def test_risk_budget_mode_keeps_quantity_read_only_and_updates_risk(qtbot):
    widget, _settings, _service = _widget(qtbot)

    widget._risk_percent.setValue(7.5)
    candidate = widget._candidate_settings()

    assert candidate.execution.okx.sizing_mode == "risk_budget"
    assert candidate.execution.okx.risk_percent == Decimal("0.075")
    assert candidate.execution.okx.quantity == "1000"
    assert widget._save_button.isEnabled() is True


def test_backend_failure_codes_are_not_exposed_in_primary_result_text():
    record = SimpleNamespace(
        last_error="submit_failed_before_worker_claim_pending_health_race",
        state_reason="",
        realized_pnl=None,
    )

    assert _result_text(record) == "提交前健康检查失败，订单没有发出"


def test_backend_error_with_spaces_prefers_plain_state_reason():
    record = SimpleNamespace(
        last_error="route fingerprint mismatch",
        state_reason="交易配置已变化，旧计划没有执行",
        realized_pnl=None,
    )

    assert _result_text(record) == "交易配置已变化，旧计划没有执行"


def test_ascii_backend_state_reason_is_not_exposed():
    record = SimpleNamespace(
        last_error="",
        state_reason="submit failed before worker claim pending health race",
        realized_pnl=None,
        state=ExecutionState.BLOCKED,
    )

    assert _result_text(record) == "已阻断"


def test_okx_realized_pnl_is_labeled_as_broker_value_before_fees():
    record = SimpleNamespace(
        last_error="",
        state_reason="",
        realized_pnl=Decimal("-164.77"),
        pnl_currency="USDT",
        state=ExecutionState.CLOSED,
    )

    assert _result_text(record) == (
        "券商已实现盈亏（未扣费用） -164.77 USDT"
    )


def test_backend_event_codes_are_not_exposed_in_primary_timeline():
    assert _event_label("reconciled") == "持仓与保护状态已核对"
    assert _event_label("new_backend_event") == "执行状态已更新"


def test_utc_runtime_timestamp_is_shown_in_local_time():
    assert _local_time("2026-07-24T19:21:54+00:00") == "03:21:54"


def test_old_campaign_execution_is_not_shown_as_latest_current_decision(qtbot):
    widget, _settings, _service = _widget(qtbot)
    snapshot = _ReadModel().capture()
    snapshot.latest_execution = SimpleNamespace(id="old-campaign-execution")

    widget._refresh_decision(snapshot)

    assert widget._decision_direction.text() == "本轮不下单 / 等待"
    assert widget._decision_state.text() == "本轮没有生成订单"


def test_stale_account_marks_latest_execution_as_local_ledger_only(qtbot):
    widget, _settings, _service = _widget(qtbot)
    snapshot = _ReadModel().capture()
    snapshot.latest_execution = _execution_record()
    snapshot.campaign_execution_ids = ("current-execution",)

    widget._refresh_decision(snapshot)

    assert widget._decision_direction.text().startswith("本地账本记录：")
    assert widget._decision_state.text() == "本地账本记录，待券商核对"
    assert "不能当作当前真实仓位" in widget._decision_reason.text()


def test_authorized_risk_is_rendered_from_durable_execution_plan():
    record = SimpleNamespace(
        plan=SimpleNamespace(
            authorized_sizing_mode="risk_budget",
            authorized_risk_budget_usdt=Decimal("123.45"),
            authorized_risk_used_usdt=Decimal("120"),
            authorized_risk_percent=Decimal("0.061725"),
            authorized_effective_risk_capital_usdt=Decimal("2000"),
        )
    )

    assert _risk_text(record) == (
        "风险预算 123.45 USDT / 预计最坏损失 120 USDT / "
        "占有效资本 6.17% / "
        "有效资本 2,000 USDT"
    )


def test_dynamic_take_profit_text_uses_real_protection_targets():
    record = SimpleNamespace(
        broker_state={
            "protection_targets": [
                {"take_profit": "4057.5"},
                {"take_profit": "4050.2"},
                {"take_profit": "4050.2"},
            ]
        },
        plan=SimpleNamespace(
            take_profit_1=Decimal("1"),
            take_profit_2=Decimal("2"),
        ),
    )

    assert _take_profit_text(record) == "止盈1 4,057.5，止盈2 4,050.2"


def test_protection_overview_requires_exact_remaining_quantity():
    record = SimpleNamespace(
        remaining_quantity=Decimal("56862"),
        needs_attention=False,
        broker_state={
            "protection_targets": [
                {
                    "quantity": "56862",
                    "filled_quantity": "0",
                    "state": "live",
                    "algo_id": "3772100521721503744",
                }
            ]
        },
    )

    assert _protection_overview(record) == (
        "当前保护：完整 · 1 档有效 · 覆盖 56,862 张"
    )


def test_transient_risk_stop_shows_only_safe_recheck_action(qtbot):
    widget, _settings, _service = _widget(qtbot)
    snapshot = _ReadModel().capture()
    snapshot.risk_stop = SimpleNamespace(
        value="已停止新增风险：风险账户数据暂时读取失败",
        certainty=FactCertainty.CONFIRMED,
    )
    snapshot.risk_runtime_state = SimpleNamespace(
        kill_active=True,
        kill_reason="risk_runtime_BrokerTransportError",
    )

    widget._refresh_risk_alert(snapshot)

    assert widget._risk_alert.isHidden() is False
    assert widget._risk_recheck_button.isHidden() is False

    snapshot.risk_runtime_state.kill_reason = "account_identity_changed"
    widget._refresh_risk_alert(snapshot)
    assert widget._risk_recheck_button.isHidden() is True


def test_stale_account_snapshot_never_renders_old_money_or_positions(qtbot):
    widget, _settings, _service = _widget(qtbot)
    snapshot = _ReadModel().capture()
    snapshot.account = SimpleNamespace(
        certainty=FactCertainty.UNKNOWN,
        source="execution.sqlite3",
    )
    snapshot.account_snapshot = SimpleNamespace(
        equity=Decimal("999999"),
        available=Decimal("888888"),
        unrealized_pnl=Decimal("777777"),
        raw_summary={
            "account_total_equity": "999999",
            "usdt_equity": "999999",
        },
        positions=(
            SimpleNamespace(
                quantity=Decimal("1"),
                raw={},
                instrument="XAU-USDT-SWAP",
                direction="long",
            ),
        ),
    )

    widget._refresh_account(snapshot)

    assert widget._total_equity.text() == "—"
    assert widget._usdt_equity.text() == "—"
    assert "不显示旧数值" in widget._positions.text()


def test_capture_failure_discards_previous_snapshot_and_closes_actions(qtbot):
    widget, _settings, _service = _widget(qtbot)
    snapshot = _ReadModel().capture()
    snapshot.account = SimpleNamespace(
        certainty=FactCertainty.CONFIRMED,
        source="execution.sqlite3",
    )
    widget._last_snapshot = snapshot
    widget._refresh_action_buttons(_execution_record())
    widget._total_equity.setText("999,999 USD")
    assert widget._exit_button.isHidden() is False

    widget._read_model = SimpleNamespace(
        capture=lambda: (_ for _ in ()).throw(RuntimeError("read_failed"))
    )
    widget.refresh_now()

    assert widget._last_snapshot is None
    assert widget._total_equity.text() == "—"
    assert widget._exit_button.isHidden() is True
    assert widget._cancel_button.isHidden() is True
    assert widget._submit_button.isEnabled() is False
    assert "上一轮界面数据已作废" in widget._decision_reason.text()


def test_disabled_primary_button_has_distinct_visual_style(qtbot):
    widget, _settings, _service = _widget(qtbot)

    assert "QPushButton#primaryButton:disabled" in widget.styleSheet()
    assert widget._save_button.isEnabled() is False


def test_fixed_quantity_preview_marks_cap_overflow_as_blocked(qtbot):
    widget, _settings, _service = _widget(qtbot)
    snapshot = _ReadModel().capture()
    snapshot.account = SimpleNamespace(
        certainty=FactCertainty.CONFIRMED,
        source="execution.sqlite3",
    )
    snapshot.account_snapshot = SimpleNamespace(equity=Decimal("20000"))
    snapshot.latest_execution = _execution_record()
    snapshot.campaign_execution_ids = ("current-execution",)
    widget._sizing_mode.setCurrentIndex(
        widget._sizing_mode.findData("fixed_quantity")
    )
    widget._fixed_quantity.setValue(100000)

    widget._refresh_fixed_preview(snapshot)

    assert widget._fixed_preview.objectName() == "errorText"
    assert "下单会整笔阻断" in widget._fixed_preview.text()


def test_action_error_never_exposes_raw_worker_code(qtbot):
    widget, _settings, _service = _widget(qtbot)
    widget._last_action_error_detail = "route_fingerprint_mismatch"

    widget._show_action_error("提交失败：route_fingerprint_mismatch")

    assert "route_fingerprint_mismatch" not in widget._decision_reason.text()
    assert widget._decision_reason.text() == "操作未完成，详情见技术信息"
    assert "route_fingerprint_mismatch" in widget._technical.text()


def test_okx_demo_table_filters_out_other_brokers_and_live_accounts(qtbot):
    widget, _settings, service = _widget(qtbot)
    service.list_recent = lambda *, limit: [
        SimpleNamespace(
            id="longbridge-old",
            plan=SimpleNamespace(broker="longbridge", environment="live"),
        ),
        SimpleNamespace(
            id="okx-live-old",
            plan=SimpleNamespace(broker="okx", environment="live"),
        ),
    ]

    widget._refresh_executions()

    assert widget._execution_table.rowCount() == 0
    assert widget._selected_execution() is None
    assert widget._submit_button.isEnabled() is False
