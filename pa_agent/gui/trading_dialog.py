"""Functional live-trading configuration and lifecycle status dialog."""
from __future__ import annotations

import threading
from decimal import Decimal

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
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
from pa_agent.execution.worker_protocol import WorkerCommandStatus


def _decimal_text(value: Decimal | None) -> str:
    return "—" if value is None else format(value, "f")


def _order_reference(order: object) -> str:
    if not isinstance(order, dict) or not order:
        return "—"
    order_id = next(
        (
            str(order.get(key) or "").strip()
            for key in (
                "order_id",
                "algo_id",
                "broker_order_id",
                "client_order_id",
                "request_id",
            )
            if str(order.get(key) or "").strip()
        ),
        "",
    )
    state = str(order.get("state") or "").strip()
    if order_id and state:
        return f"{order_id} / {state}"
    return order_id or state or "已计划"


def _protection_text(record: ExecutionRecord) -> str:
    state = record.broker_state
    targets = state.get("protection_targets")
    if isinstance(targets, list) and targets:
        summaries = [
            _order_reference(target)
            for target in targets
            if isinstance(target, dict)
        ]
        return f"{len(targets)} 笔：" + "；".join(summaries)
    stop = state.get("stop_order")
    if isinstance(stop, dict) and stop:
        return f"止损：{_order_reference(stop)}"
    return "—"


def _exit_text(record: ExecutionRecord) -> str:
    state = record.broker_state
    for key in ("exit_order", "partial_exit"):
        order = state.get(key)
        if isinstance(order, dict) and order:
            return _order_reference(order)
    completed = state.get("take_profit_completed")
    if isinstance(completed, list) and completed:
        return f"已完成止盈：{len(completed)} 笔"
    return "—"


class TradingDialog(QDialog):
    """Configure one active route and operate the latest durable execution."""

    def __init__(self, *, settings, service, event_bus=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("实盘交易")
        self.setMinimumSize(780, 680)
        self._settings = settings
        self._service = service
        self._event_bus = event_bus
        self._configuration_dirty = False
        self._setup_ui()
        self._load_values()
        self._connect_bus()
        self._connect_configuration_guard()
        self._refresh_recent()
        self._update_arm_state(self._service.is_armed)
        self._refresh_worker_health()
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(1000)
        self._health_timer.timeout.connect(self._refresh_worker_health)
        self._health_timer.start()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        gate_group = QGroupBox("运行状态")
        gate_layout = QHBoxLayout(gate_group)
        labels = QVBoxLayout()
        self._arm_label = QLabel()
        labels.addWidget(self._arm_label)
        self._worker_health_label = QLabel()
        labels.addWidget(self._worker_health_label)
        gate_layout.addLayout(labels, 1)
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

        order_group = QGroupBox("入场 / 主动离场下单方式")
        order_form = QFormLayout(order_group)
        self._entry_order_mode = QComboBox()
        self._entry_order_mode.addItem("采用 AI 信号单型（不覆盖）", "signal")
        self._entry_order_mode.addItem("强制限价单（按 PA 价格挂单）", "limit")
        self._entry_order_mode.addItem("强制限价单 + ATR 滑点", "limit_with_slippage")
        self._entry_order_mode.addItem("强制市价单", "market")
        self._entry_order_mode.setToolTip(
            "入场独立选择：限价单保持 PA 产生的价格；限价+滑点按方向把价格向成交侧移动；"
            "市价单直接成交。跟随 PA 信号只为兼容旧配置。"
        )
        order_form.addRow("入场方式:", self._entry_order_mode)

        self._exit_order_mode = QComboBox()
        self._exit_order_mode.addItem("限价单", "limit")
        self._exit_order_mode.addItem("限价单 + 允许滑点", "limit_with_slippage")
        self._exit_order_mode.addItem("市价单", "market")
        self._exit_order_mode.setToolTip(
            "只控制主动离场/收口单。原生止盈止损保护单仍由券商按保护价托管。"
        )
        order_form.addRow("主动离场方式:", self._exit_order_mode)

        self._entry_slippage_atr = QDoubleSpinBox()
        self._entry_slippage_atr.setRange(0, 5)
        self._entry_slippage_atr.setDecimals(2)
        self._entry_slippage_atr.setSingleStep(0.05)
        self._entry_slippage_atr.setSuffix(" × ATR")
        self._entry_slippage_atr.setToolTip(
            "仅在入场选择“限价单 + 允许滑点”时生效；按分析时最新已收盘 ATR14 移动价格。"
        )
        order_form.addRow("入场 ATR 滑点:", self._entry_slippage_atr)

        self._exit_slippage_atr = QDoubleSpinBox()
        self._exit_slippage_atr.setRange(0, 5)
        self._exit_slippage_atr.setDecimals(2)
        self._exit_slippage_atr.setSingleStep(0.05)
        self._exit_slippage_atr.setSuffix(" × ATR")
        self._exit_slippage_atr.setToolTip(
            "仅在主动离场选择“限价单 + 允许滑点”时生效；使用该执行计划捕获的 ATR14。"
        )
        order_form.addRow("离场 ATR 滑点:", self._exit_slippage_atr)
        self._entry_order_mode.currentIndexChanged.connect(
            self._sync_order_slippage_controls
        )
        self._exit_order_mode.currentIndexChanged.connect(
            self._sync_order_slippage_controls
        )
        root.addWidget(order_group)

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
        self._execution_table = QTableWidget(0, 11)
        self._execution_table.setHorizontalHeaderLabels(
            [
                "时间",
                "券商/账户",
                "产品",
                "品种",
                "方向/数量",
                "成交/剩余",
                "入场单",
                "保护/离场",
                "状态",
                "盈亏",
                "错误",
            ]
        )
        self._execution_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._execution_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self._execution_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._execution_table.itemSelectionChanged.connect(
            self._show_selected_execution
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
        self._lb_account.addItem("模拟账户", "paper")
        self._lb_account.addItem("综合账户", "comprehensive")
        self._lb_account.addItem("日内融资子账户", "intraday")
        self._lb_account.currentIndexChanged.connect(
            self._sync_longbridge_profile_controls
        )
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
        self._okx_simulated.toggled.connect(
            self._sync_longbridge_profile_controls
        )
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

    def _connect_configuration_guard(self) -> None:
        checkboxes = (
            self._enabled,
            self._auto_execute,
            self._lb_fallback,
            self._lb_outside_rth,
            self._okx_simulated,
        )
        combos = (
            self._broker,
            self._lb_account,
            self._okx_product,
            self._okx_margin,
            self._entry_order_mode,
            self._exit_order_mode,
        )
        line_edits = (
            self._lb_source,
            self._lb_instrument,
            self._lb_quantity,
            self._okx_source,
            self._okx_instrument,
            self._okx_quantity,
            self._okx_base_url,
        )
        for checkbox in checkboxes:
            checkbox.toggled.connect(self._mark_configuration_dirty)
        for combo in combos:
            combo.currentIndexChanged.connect(self._mark_configuration_dirty)
        for line_edit in line_edits:
            line_edit.textChanged.connect(self._mark_configuration_dirty)
        self._min_confidence.valueChanged.connect(self._mark_configuration_dirty)
        self._entry_slippage_atr.valueChanged.connect(self._mark_configuration_dirty)
        self._exit_slippage_atr.valueChanged.connect(self._mark_configuration_dirty)

    def _mark_configuration_dirty(self, *_args) -> None:
        if self._configuration_dirty:
            return
        self._configuration_dirty = True
        self._service.disarm()
        self._update_arm_state(False)

    def _reject_unsaved_action(self) -> bool:
        if not self._configuration_dirty:
            return False
        QMessageBox.warning(
            self,
            "配置尚未保存",
            "交易配置已有未保存变更。请先点击“保存配置”，再启用新增风险或刷新所选账户。",
        )
        return True

    def _load_values(self) -> None:
        execution = self._settings.execution
        self._enabled.setChecked(execution.enabled)
        self._auto_execute.setChecked(execution.auto_execute)
        self._min_confidence.setValue(execution.min_trade_confidence)
        entry_mode_index = self._entry_order_mode.findData(
            getattr(execution, "entry_order_mode", "signal")
        )
        self._entry_order_mode.setCurrentIndex(max(0, entry_mode_index))
        exit_mode_index = self._exit_order_mode.findData(
            getattr(execution, "exit_order_mode", "market")
        )
        self._exit_order_mode.setCurrentIndex(max(0, exit_mode_index))
        self._entry_slippage_atr.setValue(
            float(getattr(execution, "entry_slippage_atr_multiple", Decimal("0.50")))
        )
        self._exit_slippage_atr.setValue(
            float(getattr(execution, "exit_slippage_atr_multiple", Decimal("0.50")))
        )
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
        self._sync_order_slippage_controls()

    def _apply_widgets(self) -> None:
        execution = self._settings.execution.model_copy(deep=True)
        execution.enabled = self._enabled.isChecked()
        execution.auto_execute = self._auto_execute.isChecked()
        execution.selected_broker = self._broker.currentData()
        execution.min_trade_confidence = self._min_confidence.value()
        execution.entry_order_mode = self._entry_order_mode.currentData()
        execution.exit_order_mode = self._exit_order_mode.currentData()
        execution.entry_slippage_atr_multiple = Decimal(
            str(self._entry_slippage_atr.value())
        )
        execution.exit_slippage_atr_multiple = Decimal(
            str(self._exit_slippage_atr.value())
        )

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

    def _sync_order_slippage_controls(self) -> None:
        self._entry_slippage_atr.setEnabled(
            self._entry_order_mode.currentData() == "limit_with_slippage"
        )
        self._exit_slippage_atr.setEnabled(
            self._exit_order_mode.currentData() == "limit_with_slippage"
        )

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
            "交易配置已保存；交易会话已保持停用，需重新确认启用。",
        )
        self._configuration_dirty = False
        self._update_arm_state(False)

    def _arm_session(self) -> None:
        if self._reject_unsaved_action():
            return
        confirmation = self._service.arm_confirmation_text()
        paper = self._is_demo_route()
        text, accepted = QInputDialog.getText(
            self,
            "启用本次模拟交易会话" if paper else "启用本次实盘会话",
            (
                "模拟订单不会进入真实证券账户，但仍会按真实行情撮合。"
                if paper
                else "真实订单可能产生资金损失。"
            )
            + f"\n请输入：{confirmation}",
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
        if self._reject_unsaved_action():
            return
        execution = self._selected_execution()
        execution_id = execution.id if execution is not None else None
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
                    return self._service.get_execution(str(execution_id))
        return self._service.latest_execution()

    def _submit_selected(self) -> None:
        if self._reject_unsaved_action():
            return
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
            command_id = ""
            try:
                command = action()
                command_id = str(
                    getattr(command, "id", "") or ""
                ).strip()
                waiter = getattr(self._service, "wait_for_command", None)
                if command_id and callable(waiter):
                    result = waiter(command_id, timeout=30.0)
                    if result.status is WorkerCommandStatus.UNCERTAIN:
                        raise RuntimeError(
                            "后台返回结果不明，禁止重复提交；"
                            "请先等待券商对账或人工核对"
                        )
                    if result.status is WorkerCommandStatus.FAILED:
                        raise RuntimeError(
                            result.failure_code or "交易后台拒绝了该请求"
                        )
                    if result.status is not WorkerCommandStatus.SUCCEEDED:
                        raise RuntimeError(
                            f"交易后台命令状态异常：{result.status.value}"
                        )
            except TimeoutError:
                bus = self._event_bus
                action_name = label.removesuffix("失败")
                message = (
                    f"{action_name}结果尚未确定：等待交易后台超时，"
                    "请勿重复操作；继续观察订单与对账状态"
                )
                if command_id:
                    message += f"（命令 {command_id}）"
                if bus is not None and hasattr(bus, "emit_execution_error"):
                    bus.emit_execution_error(message)
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
        self._sync_longbridge_profile_controls()

    def _is_demo_route(self) -> bool:
        if self._broker.currentData() == "longbridge":
            return self._lb_account.currentData() == "paper"
        return bool(self._okx_simulated.isChecked())

    def _sync_longbridge_profile_controls(self) -> None:
        profile = self._lb_account.currentData()
        self._lb_fallback.setEnabled(profile == "intraday")
        self._lb_fallback.setToolTip(
            "仅日内融资子账户可在提交前因数量不足回退综合账户"
        )
        self._lb_outside_rth.setEnabled(profile != "paper")
        self._lb_outside_rth.setToolTip(
            "Longbridge 模拟账户只支持常规交易时段"
            if profile == "paper"
            else "以账户与品种权限为准"
        )
        paper = self._is_demo_route()
        self._arm_button.setText(
            "启用本次模拟会话" if paper else "启用本次实盘会话"
        )

    def _sync_okx_margin_enabled(self) -> None:
        self._okx_margin.setEnabled(self._okx_product.currentData() == "swap")

    def _update_arm_state(self, armed: bool) -> None:
        if self._configuration_dirty:
            self._arm_label.setText("配置有未保存变更：已停用旧会话，请先保存配置")
            self._arm_button.setEnabled(False)
            self._disarm_button.setEnabled(False)
            self._execute_button.setEnabled(False)
            self._cancel_button.setEnabled(True)
            self._exit_button.setEnabled(True)
            return
        if armed:
            self._arm_label.setText(
                "本次会话：已启用模拟写操作"
                if self._is_demo_route()
                else "本次会话：已启用实盘写操作"
            )
        else:
            self._arm_label.setText("本次会话：停用（仍可只读监控账户与订单）")
        self._arm_button.setEnabled(not armed)
        self._disarm_button.setEnabled(armed)
        self._execute_button.setEnabled(True)
        self._cancel_button.setEnabled(True)
        self._exit_button.setEnabled(True)

    def _refresh_worker_health(self) -> None:
        snapshot_method = getattr(
            self._service,
            "worker_health_snapshot",
            None,
        )
        if not callable(snapshot_method):
            self._worker_health_label.setText(
                "交易后台：当前运行方式未提供健康状态"
            )
            return
        try:
            snapshot = snapshot_method()
        except Exception as exc:  # noqa: BLE001
            self._worker_health_label.setText(
                f"交易后台：状态读取失败（{type(exc).__name__}）"
            )
            return
        if not bool(snapshot.get("available")):
            self._worker_health_label.setText(
                "交易后台：尚未启动；最近成功对账：无"
            )
            return
        process_text = (
            "心跳正常"
            if bool(snapshot.get("process_healthy"))
            else "心跳已停止"
        )
        reconcile_at = snapshot.get("last_successful_reconcile_at")
        if reconcile_at is None:
            reconcile_text = "无"
        else:
            isoformat = getattr(reconcile_at, "isoformat", None)
            reconcile_text = (
                isoformat(timespec="seconds")
                if callable(isoformat)
                else str(reconcile_at)
            )
            if not bool(snapshot.get("reconcile_healthy")):
                reconcile_text += "（已陈旧）"
        state = str(snapshot.get("state") or "unknown")
        error_code = str(snapshot.get("last_error_code") or "")
        error_text = f"；最近错误：{error_code}" if error_code else ""
        self._worker_health_label.setText(
            f"交易后台：{process_text} / {state}；"
            f"最近成功对账：{reconcile_text}{error_text}"
        )

    def _on_execution_update(self, record: ExecutionRecord) -> None:
        self._refresh_recent()

    def _on_account_update(self, snapshot: AccountSnapshot) -> None:
        selected = self._selected_execution()
        if selected is not None:
            expected_broker, expected_profile = self._snapshot_route(selected)
            if (
                snapshot.broker != expected_broker
                or snapshot.account_profile != expected_profile
            ):
                return
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

    @staticmethod
    def _snapshot_route(record: ExecutionRecord) -> tuple[str, str]:
        if record.plan.broker == "okx":
            return "okx", (
                "okx-demo" if record.plan.environment == "demo" else "okx-live"
            )
        return "longbridge", (
            record.selected_account or record.plan.requested_account
        )

    def _show_selected_execution(self) -> None:
        record = self._selected_execution()
        if record is None:
            self._execution_detail.setText("尚无执行记录")
            return
        events_method = getattr(self._service, "events", None)
        events = events_method(record.id) if callable(events_method) else []
        event_kinds = " → ".join(event.kind for event in events[-8:]) or "—"
        account = record.selected_account or record.plan.requested_account
        environment = "模拟" if record.plan.environment == "demo" else "实盘"
        attention = "是" if record.needs_attention else "否"
        mode_labels = {
            "signal": "采用 AI 信号单型",
            "limit": "限价",
            "limit_with_slippage": "限价 + ATR 滑点",
            "market": "市价",
        }
        detail = [
            (
                f"{record.plan.broker} / {account} / {environment} / "
                f"{record.plan.product} / {record.plan.instrument}"
            ),
            (
                f"状态：{record.state.value}；成交：{_decimal_text(record.filled_quantity)}；"
                f"剩余：{_decimal_text(record.remaining_quantity)}；需要人工处理：{attention}"
            ),
            (
                f"入场单：客户单号 {record.client_order_id or '—'}；"
                f"券商单号 {record.broker_order_id or '—'}"
            ),
            (
                f"方式：入场 {mode_labels.get(record.plan.entry_order_mode, record.plan.entry_order_mode)}；"
                f"主动离场 {mode_labels.get(record.plan.exit_order_mode, record.plan.exit_order_mode)}；"
                f"ATR14 {record.plan.entry_atr or '—'}；"
                f"ATR 倍数 {record.plan.entry_slippage_atr_multiple}/"
                f"{record.plan.exit_slippage_atr_multiple}"
            ),
            f"保护：{_protection_text(record)}",
            f"离场：{_exit_text(record)}",
            f"最近事件：{event_kinds}",
            f"状态说明：{record.state_reason or '—'}",
            f"错误：{record.last_error or '—'}",
        ]
        self._execution_detail.setText("\n".join(detail))

        snapshot_method = getattr(
            self._service,
            "latest_account_snapshot",
            None,
        )
        if callable(snapshot_method):
            broker, profile = self._snapshot_route(record)
            snapshot = snapshot_method(broker, profile)
            if snapshot is not None:
                self._on_account_update(snapshot)

    def _refresh_recent(self) -> None:
        selected = self._selected_execution()
        selected_id = selected.id if selected is not None else ""
        records = self._service.list_recent(limit=30)
        self._execution_table.blockSignals(True)
        self._execution_table.setRowCount(len(records))
        selected_row = -1
        for row, record in enumerate(records):
            if record.id == selected_id:
                selected_row = row
            values = [
                record.created_at[:19].replace("T", " "),
                f"{record.plan.broker}/{record.selected_account or record.plan.requested_account}",
                record.plan.product,
                record.plan.instrument,
                f"{record.plan.direction}/{record.plan.quantity}",
                (
                    f"{_decimal_text(record.filled_quantity)} / "
                    f"{_decimal_text(record.remaining_quantity)}"
                ),
                (
                    f"{record.client_order_id or '—'} / "
                    f"{record.broker_order_id or '—'}"
                ),
                (
                    f"保护 {_protection_text(record)}；"
                    f"离场 {_exit_text(record)}"
                ),
                record.state.value,
                (
                    f"R {_decimal_text(record.realized_pnl)} / "
                    f"U {_decimal_text(record.unrealized_pnl)}"
                ),
                record.last_error or "—",
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record.id)
                if record.needs_attention:
                    item.setToolTip(record.last_error or record.state_reason)
                self._execution_table.setItem(row, column, item)
        self._execution_table.blockSignals(False)
        if records:
            self._execution_table.selectRow(
                selected_row if selected_row >= 0 else 0
            )
        else:
            self._show_selected_execution()
