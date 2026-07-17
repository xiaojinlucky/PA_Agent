"""Functional live-trading configuration and lifecycle status dialog."""
from __future__ import annotations

import threading
from decimal import Decimal

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import save_settings
from pa_agent.execution.models import AccountSnapshot, ExecutionRecord


def _decimal_text(value: Decimal | None) -> str:
    return "—" if value is None else format(value, "f")


class TradingDialog(QDialog):
    """Configure one active route and operate the latest durable execution."""

    def __init__(self, *, settings, service, event_bus=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("实盘交易")
        self.setMinimumSize(780, 680)
        self._settings = settings
        self._service = service
        self._event_bus = event_bus
        self._setup_ui()
        self._load_values()
        self._connect_bus()
        self._refresh_recent()
        self._update_arm_state(self._service.is_armed)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        gate_group = QGroupBox("运行状态")
        gate_layout = QHBoxLayout(gate_group)
        self._arm_label = QLabel()
        gate_layout.addWidget(self._arm_label, 1)
        self._arm_button = QPushButton("启用本次实盘会话")
        self._arm_button.clicked.connect(self._arm_session)
        gate_layout.addWidget(self._arm_button)
        self._disarm_button = QPushButton("立即停用")
        self._disarm_button.clicked.connect(self._disarm_session)
        gate_layout.addWidget(self._disarm_button)
        root.addWidget(gate_group)

        common_group = QGroupBox("执行配置（不保存密钥）")
        common_form = QFormLayout(common_group)
        self._enabled = QCheckBox("允许 PA 为有效分析生成执行计划")
        common_form.addRow("执行模块:", self._enabled)
        self._auto_execute = QCheckBox("会话已启用时自动提交；否则只生成待确认计划")
        common_form.addRow("分析完成后:", self._auto_execute)
        self._broker = QComboBox()
        self._broker.addItem("Longbridge", "longbridge")
        self._broker.addItem("OKX", "okx")
        self._broker.currentIndexChanged.connect(self._on_broker_changed)
        common_form.addRow("目标券商:", self._broker)
        self._min_confidence = QSpinBox()
        self._min_confidence.setRange(0, 100)
        self._min_confidence.setSuffix(" %")
        common_form.addRow("实盘置信度门槛:", self._min_confidence)
        root.addWidget(common_group)

        self._route_stack = QStackedWidget()
        self._route_stack.addWidget(self._build_longbridge_page())
        self._route_stack.addWidget(self._build_okx_page())
        root.addWidget(self._route_stack)

        config_actions = QHBoxLayout()
        save_button = QPushButton("保存配置")
        save_button.clicked.connect(self._save_configuration)
        config_actions.addWidget(save_button)
        account_button = QPushButton("只读刷新账户")
        account_button.clicked.connect(self._refresh_account)
        config_actions.addWidget(account_button)
        config_actions.addStretch(1)
        root.addLayout(config_actions)

        account_group = QGroupBox("账户资金与盈亏")
        account_form = QFormLayout(account_group)
        self._account_profile_label = QLabel("—")
        self._equity_label = QLabel("—")
        self._available_label = QLabel("—")
        self._buying_power_label = QLabel("—")
        self._total_pnl_label = QLabel("—")
        self._unrealized_label = QLabel("—")
        self._realized_label = QLabel("—")
        account_form.addRow("账户:", self._account_profile_label)
        account_form.addRow("净资产/权益:", self._equity_label)
        account_form.addRow("可用资金:", self._available_label)
        account_form.addRow("购买力:", self._buying_power_label)
        account_form.addRow("账户总盈亏:", self._total_pnl_label)
        account_form.addRow("未实现盈亏:", self._unrealized_label)
        account_form.addRow("已实现盈亏:", self._realized_label)
        self._positions_table = QTableWidget(0, 8)
        self._positions_table.setHorizontalHeaderLabels(
            ["品种/资产", "方向", "数量", "可用", "均价", "标记价", "未实现", "币种"]
        )
        self._positions_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._positions_table.setMaximumHeight(170)
        self._positions_table.horizontalHeader().setStretchLastSection(True)
        account_form.addRow("持仓:", self._positions_table)
        root.addWidget(account_group)

        lifecycle_group = QGroupBox("PA 执行生命周期")
        lifecycle_layout = QVBoxLayout(lifecycle_group)
        self._execution_table = QTableWidget(0, 7)
        self._execution_table.setHorizontalHeaderLabels(
            ["时间", "券商/账户", "产品", "品种", "方向/数量", "状态", "盈亏"]
        )
        self._execution_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._execution_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._execution_table.horizontalHeader().setStretchLastSection(True)
        lifecycle_layout.addWidget(self._execution_table)
        action_row = QHBoxLayout()
        self._execute_button = QPushButton("执行选中/最新待确认计划")
        self._execute_button.clicked.connect(self._submit_selected)
        action_row.addWidget(self._execute_button)
        self._cancel_button = QPushButton("撤销入场")
        self._cancel_button.clicked.connect(self._cancel_selected)
        action_row.addWidget(self._cancel_button)
        self._exit_button = QPushButton("主动离场")
        self._exit_button.clicked.connect(self._exit_selected)
        action_row.addWidget(self._exit_button)
        action_row.addStretch(1)
        lifecycle_layout.addLayout(action_row)
        self._execution_detail = QLabel("尚无执行记录")
        self._execution_detail.setWordWrap(True)
        lifecycle_layout.addWidget(self._execution_detail)
        root.addWidget(lifecycle_group, 1)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        root.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)

    def _build_longbridge_page(self) -> QWidget:
        group = QGroupBox("Longbridge 路由")
        form = QFormLayout(group)
        self._lb_source = QLineEdit()
        self._lb_source.setPlaceholderText("例如 GLD.US 或当前 PA 品种")
        form.addRow("PA 来源品种:", self._lb_source)
        self._lb_instrument = QLineEdit()
        self._lb_instrument.setPlaceholderText("例如 GLD.US、AAPL.US、700.HK")
        form.addRow("Longbridge 品种:", self._lb_instrument)
        self._lb_quantity = QLineEdit()
        self._lb_quantity.setPlaceholderText("券商数量，例如 10")
        form.addRow("数量:", self._lb_quantity)
        self._lb_account = QComboBox()
        self._lb_account.addItem("综合账户", "comprehensive")
        self._lb_account.addItem("日内融资子账户", "intraday")
        form.addRow("首选账户:", self._lb_account)
        self._lb_fallback = QCheckBox("日内账户预检数量不足时，提交前改用综合账户")
        form.addRow("安全回退:", self._lb_fallback)
        self._lb_outside_rth = QCheckBox("美股允许盘前/盘后（以账户权限为准）")
        form.addRow("交易时段:", self._lb_outside_rth)
        return group

    def _build_okx_page(self) -> QWidget:
        group = QGroupBox("OKX 路由")
        form = QFormLayout(group)
        self._okx_source = QLineEdit()
        self._okx_source.setPlaceholderText("例如 XAUUSD、BTCUSD")
        form.addRow("PA 来源品种:", self._okx_source)
        self._okx_instrument = QLineEdit()
        self._okx_instrument.setPlaceholderText("例如 XAUT-USDT、BTC-USDT-SWAP")
        form.addRow("OKX instId:", self._okx_instrument)
        self._okx_quantity = QLineEdit()
        self._okx_quantity.setPlaceholderText("现货=基础币数量；永续=合约张数")
        form.addRow("数量:", self._okx_quantity)
        self._okx_product = QComboBox()
        self._okx_product.addItem("现货", "spot")
        self._okx_product.addItem("永续合约", "swap")
        self._okx_product.currentIndexChanged.connect(self._sync_okx_margin_enabled)
        form.addRow("产品:", self._okx_product)
        self._okx_margin = QComboBox()
        self._okx_margin.addItem("全仓", "cross")
        self._okx_margin.addItem("逐仓", "isolated")
        form.addRow("永续保证金模式:", self._okx_margin)
        self._okx_simulated = QCheckBox("使用 OKX 模拟交易环境")
        form.addRow("环境:", self._okx_simulated)
        self._okx_base_url = QLineEdit()
        form.addRow("API 地址:", self._okx_base_url)
        return group

    def _connect_bus(self) -> None:
        bus = self._event_bus
        if bus is None:
            return
        bus.execution_update.connect(self._on_execution_update)
        bus.account_update.connect(self._on_account_update)
        bus.execution_error.connect(self._on_execution_error)
        bus.execution_armed.connect(self._update_arm_state)

    def _load_values(self) -> None:
        execution = self._settings.execution
        self._enabled.setChecked(execution.enabled)
        self._auto_execute.setChecked(execution.auto_execute)
        self._min_confidence.setValue(execution.min_trade_confidence)
        broker_index = self._broker.findData(execution.selected_broker)
        self._broker.setCurrentIndex(max(0, broker_index))

        lb = execution.longbridge
        self._lb_source.setText(lb.source_symbol)
        self._lb_instrument.setText(lb.instrument)
        self._lb_quantity.setText(lb.quantity)
        index = self._lb_account.findData(lb.preferred_account)
        self._lb_account.setCurrentIndex(max(0, index))
        self._lb_fallback.setChecked(lb.allow_comprehensive_fallback)
        self._lb_outside_rth.setChecked(lb.allow_outside_rth)

        okx = execution.okx
        self._okx_source.setText(okx.source_symbol)
        self._okx_instrument.setText(okx.instrument)
        self._okx_quantity.setText(okx.quantity)
        index = self._okx_product.findData(okx.product)
        self._okx_product.setCurrentIndex(max(0, index))
        index = self._okx_margin.findData(okx.margin_mode)
        self._okx_margin.setCurrentIndex(max(0, index))
        self._okx_simulated.setChecked(okx.simulated)
        self._okx_base_url.setText(okx.api_base_url)
        self._on_broker_changed()
        self._sync_okx_margin_enabled()

    def _apply_widgets(self) -> None:
        execution = self._settings.execution.model_copy(deep=True)
        execution.enabled = self._enabled.isChecked()
        execution.auto_execute = self._auto_execute.isChecked()
        execution.selected_broker = self._broker.currentData()
        execution.min_trade_confidence = self._min_confidence.value()

        lb = execution.longbridge
        lb.source_symbol = self._lb_source.text().strip()
        lb.instrument = self._lb_instrument.text().strip()
        lb.quantity = self._lb_quantity.text().strip()
        lb.preferred_account = self._lb_account.currentData()
        lb.allow_comprehensive_fallback = self._lb_fallback.isChecked()
        lb.allow_outside_rth = self._lb_outside_rth.isChecked()

        okx = execution.okx
        okx.source_symbol = self._okx_source.text().strip()
        okx.instrument = self._okx_instrument.text().strip()
        okx.quantity = self._okx_quantity.text().strip()
        okx.product = self._okx_product.currentData()
        okx.margin_mode = self._okx_margin.currentData()
        okx.simulated = self._okx_simulated.isChecked()
        okx.api_base_url = self._okx_base_url.text().strip()
        self._settings.execution = execution

    def _save_configuration(self) -> None:
        try:
            self._apply_widgets()
            save_settings(self._settings, SETTINGS_JSON_PATH)
            self._service.reload_settings(self._settings)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        QMessageBox.information(
            self,
            "已保存",
            "交易配置已保存；实盘会话已保持停用，需重新确认启用。",
        )
        self._update_arm_state(False)

    def _arm_session(self) -> None:
        text, accepted = QInputDialog.getText(
            self,
            "启用本次实盘会话",
            "真实订单可能产生资金损失。\n请输入：启用实盘交易",
        )
        if not accepted:
            return
        try:
            self._service.arm(text)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "未启用", str(exc))

    def _disarm_session(self) -> None:
        self._service.disarm()

    def _refresh_account(self) -> None:
        try:
            self._apply_widgets()
            self._service.reload_settings(self._settings)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "账户读取失败", str(exc))
            return
        execution = self._selected_execution()
        execution_id = (
            execution.id
            if execution is not None
            and execution.plan.broker
            == self._settings.execution.selected_broker
            else None
        )
        self._run_async(
            lambda: self._service.refresh_account(execution_id),
            "账户读取失败",
        )

    def _selected_execution(self) -> ExecutionRecord | None:
        row = self._execution_table.currentRow()
        if row >= 0:
            item = self._execution_table.item(row, 0)
            if item is not None:
                execution_id = item.data(Qt.ItemDataRole.UserRole)
                if execution_id:
                    return self._service.store.get(str(execution_id))
        return self._service.latest_execution()

    def _submit_selected(self) -> None:
        execution = self._selected_execution()
        if execution is None:
            QMessageBox.information(self, "没有计划", "尚无可执行的 PA 分析计划。")
            return
        self._run_async(
            lambda: self._service.submit(execution.id),
            "提交失败",
        )

    def _cancel_selected(self) -> None:
        execution = self._selected_execution()
        if execution is None:
            return
        self._run_async(
            lambda: self._service.cancel_entry(execution.id),
            "撤单失败",
        )

    def _exit_selected(self) -> None:
        execution = self._selected_execution()
        if execution is None:
            return
        answer = QMessageBox.question(
            self,
            "确认主动离场",
            f"确认按市价退出 {execution.plan.instrument} "
            f"剩余 {execution.remaining_quantity}？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run_async(
            lambda: self._service.request_exit(execution.id),
            "离场请求失败",
        )

    def _run_async(self, action, label: str) -> None:
        """Run broker I/O without freezing the Qt event loop."""

        def _run() -> None:
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                bus = self._event_bus
                message = f"{label}：{exc}"
                if bus is not None and hasattr(bus, "emit_execution_error"):
                    bus.emit_execution_error(message)

        threading.Thread(
            target=_run,
            name="pa-trading-dialog-action",
            daemon=True,
        ).start()

    def _on_broker_changed(self) -> None:
        self._route_stack.setCurrentIndex(
            0 if self._broker.currentData() == "longbridge" else 1
        )

    def _sync_okx_margin_enabled(self) -> None:
        self._okx_margin.setEnabled(self._okx_product.currentData() == "swap")

    def _update_arm_state(self, armed: bool) -> None:
        if armed:
            self._arm_label.setText("本次会话：已启用实盘写操作")
        else:
            self._arm_label.setText("本次会话：停用（仍可只读监控账户与订单）")
        self._arm_button.setEnabled(not armed)
        self._disarm_button.setEnabled(armed)

    def _on_execution_update(self, record: ExecutionRecord) -> None:
        self._execution_detail.setText(
            f"{record.plan.broker} / {record.selected_account or record.plan.requested_account} / "
            f"{record.plan.instrument} / {record.state.value}\n"
            f"{record.state_reason}"
            + (f"\n需要处理：{record.last_error}" if record.last_error else "")
        )
        self._refresh_recent()

    def _on_account_update(self, snapshot: AccountSnapshot) -> None:
        currency = snapshot.base_currency
        suffix = f" {currency}" if currency else ""
        self._account_profile_label.setText(
            f"{snapshot.broker} / {snapshot.account_profile}"
        )
        self._equity_label.setText(_decimal_text(snapshot.equity) + suffix)
        self._available_label.setText(_decimal_text(snapshot.available) + suffix)
        self._buying_power_label.setText(
            _decimal_text(snapshot.buying_power) + suffix
        )
        self._total_pnl_label.setText(_decimal_text(snapshot.total_pnl) + suffix)
        self._unrealized_label.setText(
            _decimal_text(snapshot.unrealized_pnl) + suffix
        )
        self._realized_label.setText(_decimal_text(snapshot.realized_pnl) + suffix)
        self._positions_table.setRowCount(len(snapshot.positions))
        for row, position in enumerate(snapshot.positions):
            direction = (
                "持币"
                if position.raw.get("kind") == "spot_balance"
                else {"long": "多", "short": "空", "flat": "平"}.get(
                    position.direction,
                    position.direction,
                )
            )
            values = [
                position.instrument,
                direction,
                _decimal_text(position.quantity),
                _decimal_text(position.available_quantity),
                _decimal_text(position.average_price),
                _decimal_text(position.mark_price),
                _decimal_text(position.unrealized_pnl),
                position.currency,
            ]
            for column, text in enumerate(values):
                self._positions_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(text),
                )

    def _on_execution_error(self, message: str) -> None:
        self._execution_detail.setText(message)

    def _refresh_recent(self) -> None:
        records = self._service.store.list_recent(limit=30)
        self._execution_table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = [
                record.created_at[:19].replace("T", " "),
                f"{record.plan.broker}/{record.selected_account or record.plan.requested_account}",
                record.plan.product,
                record.plan.instrument,
                f"{record.plan.direction}/{record.plan.quantity}",
                record.state.value,
                (
                    f"R {_decimal_text(record.realized_pnl)} / "
                    f"U {_decimal_text(record.unrealized_pnl)}"
                ),
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record.id)
                if record.needs_attention:
                    item.setToolTip(record.last_error or record.state_reason)
                self._execution_table.setItem(row, column, item)
