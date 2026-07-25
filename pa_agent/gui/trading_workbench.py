"""OKX Demo 一级实盘工作台。

这个页面只组合现有只读快照和 ExecutionController。它不直接连接券商，
也不创建第二套执行账本。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.paths import PROJECT_ROOT, SETTINGS_JSON_PATH
from pa_agent.config.settings import save_settings
from pa_agent.execution.models import ExecutionRecord, ExecutionState
from pa_agent.execution.okx_client import (
    okx_fixed_proxy_label,
    okx_fixed_proxy_url,
)
from pa_agent.execution.worker_protocol import WorkerCommandStatus
from pa_agent.gui.read_models import FactCertainty
from pa_agent.risk.runtime import RECOVERABLE_TRANSIENT_RISK_STOP_REASONS

_STATE_LABELS = {
    ExecutionState.READY: "等待确认",
    ExecutionState.SUBMITTING: "正在提交",
    ExecutionState.ENTRY_PENDING: "等待成交",
    ExecutionState.PARTIALLY_FILLED: "部分成交",
    ExecutionState.PROTECTING: "正在建立保护",
    ExecutionState.OPEN: "持仓中",
    ExecutionState.EXIT_PENDING: "正在离场",
    ExecutionState.CLOSED: "已关闭",
    ExecutionState.BLOCKED: "已阻断",
    ExecutionState.CANCELED: "已撤销",
    ExecutionState.REJECTED: "已拒绝",
    ExecutionState.UNKNOWN: "结果待核对",
    ExecutionState.ERROR: "执行失败",
}

_EVENT_LABELS = {
    "plan_created": "PA 计划已生成",
    "risk_sizing_calculated": "风险与张数已计算",
    "supervisor_allowed": "监督已放行",
    "supervisor_blocked": "监督已拒绝",
    "preflight_passed": "账户与订单条件已核对",
    "preflight_blocked": "账户或订单条件未通过",
    "submit_intent": "提交入场意图已记录",
    "submit_started": "开始提交入场",
    "submitted": "入场单已提交",
    "entry_accepted": "券商已受理入场",
    "entry_filled": "入场已成交",
    "entry_partially_filled": "入场部分成交",
    "protection_submitted": "保护单已提交",
    "protected": "止损止盈已建立",
    "entry_cancel_requested": "已请求撤销入场",
    "entry_canceled": "入场已撤销",
    "exit_intent": "主动离场意图已记录",
    "exit_requested": "已请求主动离场",
    "exit_submitted": "离场单已提交",
    "closed": "执行已关闭",
    "reconciled": "持仓与保护状态已核对",
    "risk_runtime_blocked": "风险闸门已阻断",
    "ready_expired": "待确认计划已过期",
}

_WORKER_LABELS = {
    "starting": "正在启动",
    "reconciling": "正在核对账户",
    "running": "运行正常",
    "active": "运行中",
    "stopping": "正在收口",
    "completed": "已完成",
    "needs_attention": "需要人工核对",
    "stopped": "已停止",
    "stale": "状态过期",
    "unknown": "状态未知",
}


def _event_label(kind: str) -> str:
    return _EVENT_LABELS.get(str(kind), "执行状态已更新")


def _plain_status(value: object) -> str:
    text = str(value or "").strip()
    for raw, label in _WORKER_LABELS.items():
        if text == raw:
            return label
        if text.startswith(raw + "（"):
            return label + text[len(raw) :]
    return text.replace("Campaign", "自动交易")


def _number(value: object, *, decimals: int = 2) -> str:
    if value is None or value == "":
        return "—"
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if not parsed.is_finite():
        return "—"
    rendered = f"{parsed:,.{decimals}f}"
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _local_time(value: object) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text[11:19] if len(text) >= 19 else text
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.strftime("%H:%M:%S")
    return parsed.astimezone().strftime("%H:%M:%S")


def _direction(record: ExecutionRecord) -> str:
    return "做多" if record.plan.direction == "long" else "做空"


def _state_label(record: ExecutionRecord) -> str:
    return _STATE_LABELS.get(record.state, record.state.value)


def _result_text(record: ExecutionRecord) -> str:
    code = str(record.last_error or "").strip()
    reason = str(record.state_reason or "").strip()
    reason_is_user_facing = bool(reason) and not re.fullmatch(
        r"[A-Za-z0-9_.:/ -]+",
        reason,
    ) and not re.search(
        r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b",
        reason,
        flags=re.IGNORECASE,
    )
    if code:
        if code == "risk_runtime_IncompleteRead":
            return "风险账户数据读取中断，订单没有发出"
        if code.startswith("risk_runtime_"):
            return "风险账户数据读取失败，订单没有发出"
        if code.startswith(
            ("submit_failed_before_worker_claim", "submit failed before worker claim")
        ):
            return "提交前健康检查失败，订单没有发出"
        if "旧 Worker schema" in code:
            return "交易后台版本未就绪，未继续执行"
        if reason_is_user_facing:
            return reason
        return "执行被安全阻断，详情见技术信息"
    if reason_is_user_facing:
        return reason
    return (
        "券商已实现盈亏（未扣费用）"
        f" {_number(record.realized_pnl)} "
        f"{str(getattr(record, 'pnl_currency', '') or 'USDT')}"
        if record.realized_pnl is not None
        else (
            _STATE_LABELS.get(record.state, "执行状态已更新")
            if reason
            else "—"
        )
    )


def _risk_text(record: ExecutionRecord) -> str:
    plan = record.plan
    risk_budget = plan.authorized_risk_budget_usdt
    risk_used = plan.authorized_risk_used_usdt
    risk_percent = plan.authorized_risk_percent
    effective_capital = plan.authorized_effective_risk_capital_usdt
    if (
        risk_budget is None
        or risk_percent is None
        or effective_capital is None
    ):
        return "风险审计待生成"
    if plan.authorized_sizing_mode == "fixed_quantity":
        amount_text = (
            f"反算最坏损失 {_number(risk_used or risk_budget)} USDT"
        )
    elif risk_used is not None:
        amount_text = (
            f"风险预算 {_number(risk_budget)} USDT / "
            f"预计最坏损失 {_number(risk_used)} USDT"
        )
    else:
        amount_text = f"风险预算 {_number(risk_budget)} USDT"
    return (
        f"{amount_text} / "
        f"占有效资本 {_number(risk_percent * 100)}% / "
        f"有效资本 {_number(effective_capital)} USDT"
    )


def _protection_overview(record: ExecutionRecord | None) -> str:
    """把原生保护证据转换成用户可以直接判断的结论。"""
    if record is None or record.remaining_quantity <= 0:
        return "当前保护：空仓，无需保护"
    targets = record.broker_state.get("protection_targets")
    if not isinstance(targets, list) or not targets:
        return "当前保护：尚未确认完整保护"

    remaining_protected = Decimal("0")
    active_targets = 0
    uncertain = False
    for target in targets:
        if not isinstance(target, dict):
            uncertain = True
            continue
        try:
            quantity = Decimal(str(target.get("quantity") or "0"))
            filled = Decimal(str(target.get("filled_quantity") or "0"))
        except InvalidOperation:
            uncertain = True
            continue
        outstanding = max(quantity - filled, Decimal("0"))
        if outstanding <= 0:
            continue
        state = str(target.get("state") or "").lower()
        if (
            not target.get("algo_id")
            or state
            in {
                "unknown",
                "known_algo_absent",
                "confirmed_absent",
                "canceled",
                "order_failed",
                "rejected",
            }
        ):
            uncertain = True
            continue
        remaining_protected += outstanding
        active_targets += 1

    if (
        not uncertain
        and active_targets > 0
        and remaining_protected == record.remaining_quantity
        and not record.needs_attention
    ):
        return (
            f"当前保护：完整 · {active_targets} 档有效 · "
            f"覆盖 {_number(remaining_protected, decimals=0)} 张"
        )
    if uncertain or record.needs_attention:
        return "当前保护：状态未知，等待交易后台完成只读核对"
    return (
        "当前保护：不完整 · "
        f"仅确认 {_number(remaining_protected, decimals=0)} / "
        f"{_number(record.remaining_quantity, decimals=0)} 张"
    )


def _take_profit_text(record: ExecutionRecord) -> str:
    targets = record.broker_state.get("protection_targets")
    values: list[object] = []
    if isinstance(targets, list):
        values = [
            target.get("take_profit")
            for target in targets
            if isinstance(target, dict) and target.get("take_profit")
        ]
    if not values:
        values = [record.plan.take_profit_1, record.plan.take_profit_2]
    unique: list[str] = []
    for value in values:
        rendered = _number(value)
        if rendered not in unique:
            unique.append(rendered)
    return "，".join(
        f"止盈{index} {value}" for index, value in enumerate(unique, start=1)
    )


class TradingWorkbench(QWidget):
    """把 OKX Demo 运行、账户、风险和执行反馈放到同一个一级页面。"""

    configuration_saved = pyqtSignal()
    _action_failed = pyqtSignal(str)
    _action_completed = pyqtSignal()

    def __init__(
        self,
        *,
        settings,
        service,
        event_bus=None,
        read_model=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("tradingWorkbench")
        self._settings = settings
        self._service = service
        self._event_bus = event_bus
        self._read_model = read_model
        self._loading = False
        self._configuration_dirty = False
        self._last_snapshot = None
        self._last_action_error_detail = ""
        self._code_loaded_at = datetime.now().astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self._setup_ui()
        self._load_values()
        self._connect_events()
        self._action_failed.connect(self._show_action_error)
        self._action_completed.connect(self.refresh_now)
        self._timer = QTimer(self)
        self._timer.setInterval(2_000)
        self._timer.timeout.connect(self.refresh_now)
        self._timer.start()
        self.refresh_now()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("实盘交易")
        title.setObjectName("tradingPageTitle")
        subtitle = QLabel(
            "OKX 模拟账户 · PA 每 10 分钟自然判断 · "
            "订单只经唯一交易后台执行"
        )
        subtitle.setObjectName("tradingPageSubtitle")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        title_row.addLayout(title_block, 1)
        self._refresh_button = QPushButton("刷新显示")
        self._refresh_button.clicked.connect(self.refresh_now)
        title_row.addWidget(self._refresh_button)
        root.addLayout(title_row)

        self._dirty_banner = QFrame()
        self._dirty_banner.setObjectName("tradingDirtyBanner")
        dirty_layout = QHBoxLayout(self._dirty_banner)
        dirty_layout.setContentsMargins(12, 8, 12, 8)
        self._dirty_banner_text = QLabel(
            "配置正在编辑；当前运行任务不变。保存后将在下一次启动自动交易时生效。"
        )
        self._dirty_banner_text.setWordWrap(True)
        dirty_layout.addWidget(self._dirty_banner_text, 1)
        self._dirty_banner.setVisible(False)
        root.addWidget(self._dirty_banner)

        status = QFrame()
        status.setObjectName("tradingStatusStrip")
        status_layout = QGridLayout(status)
        status_layout.setContentsMargins(14, 10, 14, 10)
        status_layout.setHorizontalSpacing(16)
        status_layout.setVerticalSpacing(6)
        self._environment_badge = QLabel("OKX 模拟盘")
        self._environment_badge.setObjectName("pillBlue")
        self._campaign_status = QLabel("模拟交易：读取中")
        self._worker_status = QLabel("交易后台：读取中")
        self._risk_status = QLabel("风险闸门：读取中")
        self._network_route = QLabel(
            f"网络出口：{okx_fixed_proxy_label()} · "
            f"{okx_fixed_proxy_url()} · 独立于 v2rayN 当前节点"
        )
        self._network_route.setObjectName("mutedLabel")
        self._network_route.setWordWrap(True)
        self._captured_at = QLabel("更新时间：—")
        self._captured_at.setObjectName("mutedLabel")
        status_layout.addWidget(self._environment_badge, 0, 0, 2, 1)
        status_layout.addWidget(self._campaign_status, 0, 1)
        status_layout.addWidget(self._worker_status, 0, 2)
        status_layout.addWidget(self._risk_status, 1, 1, 1, 2)
        status_layout.addWidget(self._captured_at, 0, 3)
        status_layout.addWidget(self._network_route, 2, 0, 1, 4)
        status_layout.setColumnStretch(1, 1)
        status_layout.setColumnStretch(2, 1)
        root.addWidget(status)

        self._risk_alert = QFrame()
        self._risk_alert.setObjectName("tradingRiskAlert")
        risk_alert_layout = QHBoxLayout(self._risk_alert)
        risk_alert_layout.setContentsMargins(12, 8, 12, 8)
        self._risk_alert_text = QLabel(
            "账户状态无法确认，系统不会新增风险。"
        )
        self._risk_alert_text.setWordWrap(True)
        risk_alert_layout.addWidget(self._risk_alert_text, 1)
        self._risk_recheck_button = QPushButton("重新检查账户状态")
        self._risk_recheck_button.clicked.connect(self._recheck_risk_state)
        self._risk_recheck_button.setVisible(False)
        risk_alert_layout.addWidget(self._risk_recheck_button)
        self._risk_alert.setVisible(False)
        root.addWidget(self._risk_alert)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        body.addWidget(self._build_main_column())
        body.addWidget(self._build_side_column())
        body.setStretchFactor(0, 72)
        body.setStretchFactor(1, 28)
        body.setSizes([1080, 430])
        root.addWidget(body, 1)

        self.setStyleSheet(
            """
            QWidget#tradingWorkbench { font-size: 13px; }
            QLabel#tradingPageTitle {
                font-size: 22px; font-weight: 700; color: #e7ecf4;
            }
            QLabel#tradingPageSubtitle { color: #9aa3b2; font-size: 13px; }
            QFrame#tradingStatusStrip {
                background: #10131a; border: 1px solid #252b36;
                border-radius: 6px;
            }
            QFrame#tradingDirtyBanner {
                background: rgba(230, 162, 60, 0.12);
                border: 1px solid #e6a23c;
                border-radius: 6px;
                color: #f6c86f;
            }
            QFrame#tradingRiskAlert {
                background: rgba(240, 82, 82, 0.12);
                border: 1px solid #f05252;
                border-radius: 6px;
                color: #ffb4b4;
            }
            QFrame#tradingSection {
                background: #10131a; border: 1px solid #252b36;
                border-radius: 6px;
            }
            QLabel#sectionTitle {
                color: #e7ecf4; font-size: 15px; font-weight: 700;
            }
            QLabel#metricValue {
                color: #e7ecf4; font-size: 17px; font-weight: 650;
                font-family: "JetBrains Mono", "Cascadia Mono", "Consolas";
            }
            QLabel#decisionValue {
                color: #e7ecf4; font-size: 17px; font-weight: 650;
            }
            QLabel#configCaption {
                color: #9aa3b2; font-size: 12px; font-weight: 700;
            }
            QLabel#warningText { color: #f6c86f; }
            QLabel#errorText { color: #ffb4b4; font-weight: 600; }
            QGroupBox { font-size: 13px; border-radius: 6px; }
            QTableWidget { font-size: 13px; }
            QPushButton { font-size: 13px; min-height: 36px; }
            QComboBox, QDoubleSpinBox { font-size: 13px; min-height: 32px; }
            QWidget#tradingWorkbench QPushButton#primaryButton {
                background: #2f8dff; border-color: #2f8dff;
                color: #ffffff; font-weight: 700;
            }
            QWidget#tradingWorkbench QPushButton#primaryButton:hover {
                background: #57a3ff; border-color: #57a3ff;
            }
            QWidget#tradingWorkbench QPushButton#primaryButton:disabled {
                background: #1b2633; border-color: #303846;
                color: #697386;
            }
            QWidget#tradingWorkbench QPushButton#dangerButton {
                color: #ff8f8f; border-color: #f05252;
            }
            """
        )

    def _section(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("tradingSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        return frame, layout

    def _build_main_column(self) -> QWidget:
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        decision_frame, decision_layout = self._section("最新 PA 决策")
        decision_grid = QGridLayout()
        decision_grid.setHorizontalSpacing(24)
        decision_grid.setVerticalSpacing(7)
        self._decision_direction = QLabel("等待下一轮判断")
        self._decision_direction.setObjectName("decisionValue")
        self._decision_quantity = QLabel("—")
        self._decision_prices = QLabel("—")
        self._decision_state = QLabel("当前没有执行")
        self._decision_risk = QLabel("等待下一条有效信号后计算")
        self._decision_reason = QLabel("自然策略没有信号时，系统会继续等待下一根 10 分钟 K 线。")
        self._decision_reason.setWordWrap(True)
        decision_grid.addWidget(QLabel("结论"), 0, 0)
        decision_grid.addWidget(self._decision_direction, 1, 0)
        decision_grid.addWidget(QLabel("合约张数"), 0, 1)
        decision_grid.addWidget(self._decision_quantity, 1, 1)
        decision_grid.addWidget(QLabel("入场 / 止损 / 止盈计划"), 0, 2)
        decision_grid.addWidget(self._decision_prices, 1, 2)
        decision_grid.addWidget(QLabel("执行状态"), 0, 3)
        decision_grid.addWidget(self._decision_state, 1, 3)
        decision_grid.addWidget(QLabel("授权风险"), 2, 0)
        decision_grid.addWidget(self._decision_risk, 2, 1, 1, 3)
        decision_grid.setColumnStretch(2, 2)
        decision_layout.addLayout(decision_grid)
        decision_layout.addWidget(self._decision_reason)
        layout.addWidget(decision_frame)

        execution_frame, execution_layout = self._section("订单与持仓生命周期")
        self._execution_table = QTableWidget(0, 7)
        self._execution_table.setHorizontalHeaderLabels(
            ["时间", "方向", "张数", "入场 / 成交", "保护 / 离场", "状态", "结果"]
        )
        self._execution_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._execution_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._execution_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._execution_table.setAlternatingRowColors(True)
        self._execution_table.verticalHeader().setVisible(False)
        self._execution_table.verticalHeader().setDefaultSectionSize(32)
        header = self._execution_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self._execution_table.itemSelectionChanged.connect(
            self._refresh_selected_execution
        )
        execution_layout.addWidget(self._execution_table, 1)
        actions = QHBoxLayout()
        self._cancel_button = QPushButton("撤销未成交入场")
        self._cancel_button.clicked.connect(self._cancel_selected)
        self._cancel_button.setVisible(False)
        self._exit_button = QPushButton("主动离场")
        self._exit_button.setObjectName("dangerButton")
        self._exit_button.clicked.connect(self._exit_selected)
        self._exit_button.setVisible(False)
        actions.addWidget(self._cancel_button)
        actions.addWidget(self._exit_button)
        actions.addStretch(1)
        execution_layout.addLayout(actions)
        layout.addWidget(execution_frame, 3)

        timeline_frame, timeline_layout = self._section("最近事件")
        self._timeline = QListWidget()
        self._timeline.setObjectName("timelineList")
        self._timeline.setMaximumHeight(170)
        timeline_layout.addWidget(self._timeline)
        layout.addWidget(timeline_frame)
        return column

    def _metric(self, grid: QGridLayout, row: int, column: int, title: str) -> QLabel:
        block = QVBoxLayout()
        label = QLabel(title)
        label.setObjectName("mutedLabel")
        value = QLabel("—")
        value.setObjectName("metricValue")
        block.addWidget(label)
        block.addWidget(value)
        grid.addLayout(block, row, column)
        return value

    def _build_side_column(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 2, 0)
        layout.setSpacing(12)

        account_frame, account_layout = self._section("账户与风险")
        account_grid = QGridLayout()
        self._total_equity = self._metric(account_grid, 0, 0, "账户总权益（回撤口径）")
        self._usdt_equity = self._metric(account_grid, 0, 1, "USDT 权益（定仓口径）")
        self._available = self._metric(account_grid, 1, 0, "可用 USDT")
        self._unrealized = self._metric(account_grid, 1, 1, "未实现盈亏")
        account_layout.addLayout(account_grid)
        self._positions = QLabel("当前持仓：读取中")
        self._positions.setWordWrap(True)
        account_layout.addWidget(self._positions)
        self._protection_status = QLabel("当前保护：读取中")
        self._protection_status.setWordWrap(True)
        account_layout.addWidget(self._protection_status)
        layout.addWidget(account_frame)

        config_frame, config_layout = self._section("定仓与杠杆")
        active_caption = QLabel("当前运行参数（只读）")
        active_caption.setObjectName("configCaption")
        config_layout.addWidget(active_caption)
        self._active_config = QLabel("当前生效：读取中")
        self._active_config.setWordWrap(True)
        self._active_config.setObjectName("pillBlue")
        config_layout.addWidget(self._active_config)

        pending_caption = QLabel("下次启动参数")
        pending_caption.setObjectName("configCaption")
        config_layout.addWidget(pending_caption)
        self._pending_config = QLabel("已保存配置：读取中")
        self._pending_config.setWordWrap(True)
        self._alignment = QLabel("配置一致性：读取中")
        self._alignment.setWordWrap(True)
        config_layout.addWidget(self._pending_config)
        config_layout.addWidget(self._alignment)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("定仓方式"))
        self._sizing_mode = QComboBox()
        self._sizing_mode.setObjectName("sizingMode")
        self._sizing_mode.addItem("按风险预算自动算张数", "risk_budget")
        self._sizing_mode.addItem("用户固定合约张数", "fixed_quantity")
        self._sizing_mode.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._sizing_mode, 1)
        config_layout.addLayout(mode_row)

        common_grid = QGridLayout()
        common_grid.addWidget(QLabel("资金上限"), 0, 0)
        self._capital_cap = QDoubleSpinBox()
        self._capital_cap.setObjectName("capitalCap")
        self._capital_cap.setRange(0, 1_000_000_000)
        self._capital_cap.setDecimals(2)
        self._capital_cap.setSingleStep(1_000)
        self._capital_cap.setSuffix(" USDT")
        common_grid.addWidget(self._capital_cap, 0, 1)
        common_grid.addWidget(QLabel("最大允许杠杆"), 1, 0)
        self._maximum_leverage = QDoubleSpinBox()
        self._maximum_leverage.setObjectName("maximumLeverage")
        self._maximum_leverage.setRange(1, 125)
        self._maximum_leverage.setDecimals(2)
        self._maximum_leverage.setSingleStep(1)
        self._maximum_leverage.setSuffix(" ×")
        common_grid.addWidget(self._maximum_leverage, 1, 1)
        config_layout.addLayout(common_grid)

        self._mode_stack = QStackedWidget()
        risk_page = QWidget()
        risk_layout = QGridLayout(risk_page)
        risk_layout.setContentsMargins(0, 0, 0, 0)
        risk_layout.addWidget(QLabel("单笔最坏损失比例"), 0, 0)
        self._risk_percent = QDoubleSpinBox()
        self._risk_percent.setObjectName("riskPercent")
        self._risk_percent.setRange(0.01, 100)
        self._risk_percent.setDecimals(2)
        self._risk_percent.setSingleStep(0.5)
        self._risk_percent.setSuffix(" %")
        risk_layout.addWidget(self._risk_percent, 0, 1)
        risk_help = QLabel(
            "用户设置资金上限、单笔风险和最大杠杆，张数由系统唯一确定；"
            "若张数需要更高杠杆，整笔不下单。"
        )
        risk_help.setWordWrap(True)
        risk_help.setObjectName("mutedLabel")
        risk_layout.addWidget(risk_help, 1, 0, 1, 2)
        self._risk_preview = QLabel(
            "预计结果：等待下一条 PA 给出入场价和止损价"
        )
        self._risk_preview.setWordWrap(True)
        self._risk_preview.setObjectName("pillBlue")
        risk_layout.addWidget(self._risk_preview, 2, 0, 1, 2)
        self._mode_stack.addWidget(risk_page)

        fixed_page = QWidget()
        fixed_layout = QGridLayout(fixed_page)
        fixed_layout.setContentsMargins(0, 0, 0, 0)
        fixed_layout.addWidget(QLabel("固定合约张数"), 0, 0)
        self._fixed_quantity = QDoubleSpinBox()
        self._fixed_quantity.setObjectName("fixedQuantity")
        self._fixed_quantity.setRange(1, 1_000_000_000)
        self._fixed_quantity.setDecimals(0)
        self._fixed_quantity.setSingleStep(1)
        self._fixed_quantity.setSuffix(" 张")
        fixed_layout.addWidget(self._fixed_quantity, 0, 1)
        fixed_help = QLabel(
            "用户设置张数和最大杠杆，系统反算实际最坏损失；"
            "超过资金或杠杆上限时整笔不下单，不自动减张。"
        )
        fixed_help.setWordWrap(True)
        fixed_help.setObjectName("mutedLabel")
        fixed_layout.addWidget(fixed_help, 1, 0, 1, 2)
        self._fixed_preview = QLabel(
            "反算结果：等待下一条 PA 给出入场价和止损价"
        )
        self._fixed_preview.setWordWrap(True)
        self._fixed_preview.setObjectName("pillBlue")
        fixed_layout.addWidget(self._fixed_preview, 2, 0, 1, 2)
        self._mode_stack.addWidget(fixed_page)
        config_layout.addWidget(self._mode_stack)

        config_actions = QHBoxLayout()
        self._save_button = QPushButton("保存下次启动参数")
        self._save_button.setObjectName("primaryButton")
        self._save_button.clicked.connect(self._save_configuration)
        self._save_button.setEnabled(False)
        self._cancel_config_button = QPushButton("取消编辑")
        self._cancel_config_button.clicked.connect(
            self._cancel_configuration_edit
        )
        self._cancel_config_button.setEnabled(False)
        config_actions.addWidget(self._save_button, 1)
        config_actions.addWidget(self._cancel_config_button)
        config_layout.addLayout(config_actions)
        layout.addWidget(config_frame)

        self._technical_group = QGroupBox("技术详情（默认收起）")
        self._technical_group.setCheckable(True)
        self._technical_group.setChecked(False)
        technical_layout = QVBoxLayout(self._technical_group)
        self._technical_body = QWidget()
        technical_body_layout = QVBoxLayout(self._technical_body)
        technical_body_layout.setContentsMargins(0, 4, 0, 0)
        technical_body_layout.setSpacing(8)
        self._manual_session = QLabel(
            "停用。自动 10 分钟模拟交易不依赖这个按钮。"
        )
        self._manual_session.setWordWrap(True)
        technical_body_layout.addWidget(self._manual_session)
        session_actions = QHBoxLayout()
        self._arm_button = QPushButton("启用本次模拟会话")
        self._arm_button.clicked.connect(self._arm_session)
        self._disarm_button = QPushButton("停用手动会话")
        self._disarm_button.clicked.connect(self._disarm_session)
        session_actions.addWidget(self._arm_button)
        session_actions.addWidget(self._disarm_button)
        technical_body_layout.addLayout(session_actions)
        self._submit_button = QPushButton("执行待确认计划（手动会话）")
        self._submit_button.clicked.connect(self._submit_selected)
        technical_body_layout.addWidget(self._submit_button)
        self._technical = QLabel("—")
        self._technical.setWordWrap(True)
        self._technical.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        technical_body_layout.addWidget(self._technical)
        technical_layout.addWidget(self._technical_body)
        self._technical_group.toggled.connect(self._technical_body.setVisible)
        self._technical_body.setVisible(False)
        layout.addWidget(self._technical_group)
        layout.addStretch(1)
        scroll.setWidget(host)
        scroll.setMinimumWidth(360)
        scroll.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )
        return scroll

    def _connect_events(self) -> None:
        for widget, signal_name in (
            (self._capital_cap, "valueChanged"),
            (self._maximum_leverage, "valueChanged"),
            (self._risk_percent, "valueChanged"),
            (self._fixed_quantity, "valueChanged"),
        ):
            getattr(widget, signal_name).connect(self._mark_dirty)
        self._sizing_mode.currentIndexChanged.connect(self._mark_dirty)

        bus = self._event_bus
        if bus is None:
            return
        bus.execution_update.connect(lambda _record: self.refresh_now())
        bus.account_update.connect(lambda _snapshot: self.refresh_now())
        bus.execution_error.connect(self._show_action_error)
        bus.execution_armed.connect(lambda _armed: self._refresh_session())

    def _load_values(self) -> None:
        self._loading = True
        okx = self._settings.execution.okx
        mode_index = self._sizing_mode.findData(
            getattr(okx, "sizing_mode", "risk_budget")
        )
        self._sizing_mode.setCurrentIndex(max(0, mode_index))
        self._capital_cap.setValue(float(okx.risk_capital_cap_usdt))
        self._maximum_leverage.setValue(float(okx.maximum_leverage))
        self._risk_percent.setValue(float(okx.risk_percent * 100))
        try:
            quantity = Decimal(str(okx.quantity or "0"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                f"已保存的 OKX 合约张数无效：{okx.quantity!r}"
            ) from exc
        if not quantity.is_finite() or quantity < 0:
            raise ValueError(
                f"已保存的 OKX 合约张数无效：{okx.quantity!r}"
            )
        self._fixed_quantity.setValue(float(max(quantity, Decimal("1"))))
        self._loading = False
        self._configuration_dirty = False
        self._save_button.setEnabled(False)
        self._cancel_config_button.setEnabled(False)
        self._dirty_banner.setVisible(False)
        self._on_mode_changed()
        self._refresh_pending_config()

    def _on_mode_changed(self, *_args) -> None:
        self._mode_stack.setCurrentIndex(
            1 if self._sizing_mode.currentData() == "fixed_quantity" else 0
        )
        self._refresh_pending_config()

    def _mark_dirty(self, *_args) -> None:
        if self._loading:
            return
        if not self._configuration_dirty:
            self._configuration_dirty = True
            self._service.disarm()
        self._save_button.setEnabled(True)
        self._cancel_config_button.setEnabled(True)
        self._dirty_banner.setVisible(True)
        self._alignment.setText(
            "当前编辑尚未保存；自动交易仍使用原启动值。"
        )
        self._alignment.setObjectName("warningText")
        self._alignment.style().unpolish(self._alignment)
        self._alignment.style().polish(self._alignment)
        self._refresh_pending_config()
        self._refresh_session()

    def _cancel_configuration_edit(self) -> None:
        self._load_values()
        self._refresh_session()
        self.refresh_now()

    def _refresh_pending_config(self) -> None:
        mode = self._sizing_mode.currentData()
        if mode == "fixed_quantity":
            detail = f"固定 {_number(self._fixed_quantity.value(), decimals=0)} 张"
        else:
            detail = f"单笔风险 {_number(self._risk_percent.value())}%"
        prefix = "编辑中" if self._configuration_dirty else "已保存"
        self._pending_config.setText(
            f"{prefix}：{detail} · 资金上限 "
            f"{_number(self._capital_cap.value())} USDT · "
            f"最大 {_number(self._maximum_leverage.value())}×"
        )
        if self._last_snapshot is not None:
            self._refresh_risk_preview(self._last_snapshot)
            self._refresh_fixed_preview(self._last_snapshot)

    def _refresh_risk_preview(self, snapshot) -> None:
        if self._sizing_mode.currentData() != "risk_budget":
            return
        record = snapshot.latest_execution
        if (
            record is None
            or record.id not in set(snapshot.campaign_execution_ids)
        ):
            self._risk_preview.setText(
                "预计结果：等待下一条 PA 给出入场价和止损价"
            )
            return
        plan = record.plan
        if (
            plan.authorized_risk_used_usdt is None
            or plan.authorized_contract_notional_usdt is None
        ):
            self._risk_preview.setText(
                "预计结果：最近一笔计划缺少可复核的定仓证据，"
                "下一条 PA 信号生成时再计算"
            )
            return
        leverage = Decimal(str(self._maximum_leverage.value()))
        notional = plan.quantity * plan.authorized_contract_notional_usdt
        margin = notional / leverage
        self._risk_preview.setText(
            "最近一笔已授权计划参考："
            f"{_number(plan.quantity, decimals=0)} 张 · "
            f"预计最坏损失 {_number(plan.authorized_risk_used_usdt)} USDT · "
            f"按最大杠杆估算的最低保证金 {_number(margin)} USDT。"
            "新配置会在下一条有效信号出现时重新计算。"
        )

    def _refresh_fixed_preview(self, snapshot) -> None:
        if self._sizing_mode.currentData() != "fixed_quantity":
            return
        record = snapshot.latest_execution
        if (
            record is None
            or record.id not in set(snapshot.campaign_execution_ids)
        ):
            self._fixed_preview.setText(
                "反算结果：等待下一条 PA 给出入场价和止损价"
            )
            return
        risk_per_contract = (
            record.plan.authorized_worst_case_loss_per_contract_usdt
        )
        if risk_per_contract is None:
            raw_sizing = record.broker_state.get("risk_sizing")
            if isinstance(raw_sizing, dict):
                try:
                    risk_per_contract = Decimal(
                        str(
                            raw_sizing.get(
                                "worst_case_loss_per_contract_usdt"
                            )
                        )
                    )
                except (InvalidOperation, TypeError, ValueError):
                    risk_per_contract = None
        if risk_per_contract is None or risk_per_contract <= 0:
            self._fixed_preview.setText(
                "反算结果：最近一笔计划缺少可复核的单张风险，"
                "下一条 PA 信号生成时再计算"
            )
            return

        quantity = Decimal(str(self._fixed_quantity.value()))
        risk_amount = quantity * risk_per_contract
        if (
            snapshot.account.certainty is FactCertainty.CONFIRMED
            and snapshot.account_snapshot is not None
            and snapshot.account_snapshot.equity is not None
            and snapshot.account_snapshot.equity > 0
        ):
            effective_capital = min(
                snapshot.account_snapshot.equity,
                Decimal(str(self._capital_cap.value())),
            )
            risk_percent_text = (
                _number(risk_amount / effective_capital * 100) + "%"
            )
        else:
            risk_percent_text = "等待新鲜账户数据"

        notional_per_contract = (
            record.plan.authorized_contract_notional_usdt
        )
        if notional_per_contract is not None:
            total_notional = quantity * notional_per_contract
            leverage = Decimal(str(self._maximum_leverage.value()))
            margin = total_notional / leverage
            notional_text = _number(total_notional) + " USDT"
            margin_text = _number(margin) + " USDT"
        else:
            notional_text = "下一条新计划生成后显示"
            margin_text = "下一条新计划生成后显示"
            margin = None
        effective_capital = Decimal(str(self._capital_cap.value()))
        if (
            snapshot.account.certainty is FactCertainty.CONFIRMED
            and snapshot.account_snapshot is not None
            and snapshot.account_snapshot.equity is not None
            and snapshot.account_snapshot.equity > 0
        ):
            effective_capital = min(
                effective_capital,
                snapshot.account_snapshot.equity,
            )
        warnings = []
        if risk_amount > effective_capital:
            warnings.append("最坏损失超过有效风险资本")
        if margin is not None and margin > effective_capital:
            warnings.append("最低保证金超过有效风险资本")
        suffix = (
            "。超限原因：" + "；".join(warnings) + "，下单会整笔阻断"
            if warnings
            else ""
        )
        self._fixed_preview.setText(
            "按最近一次入场/止损预估："
            f"最坏损失 {_number(risk_amount)} USDT · "
            f"风险比例 {risk_percent_text} · "
            f"名义价值 {notional_text} · "
            f"按最大杠杆估算的最低保证金 {margin_text}"
            f"{suffix}"
        )
        self._fixed_preview.setObjectName(
            "errorText" if warnings else "pillBlue"
        )
        self._fixed_preview.style().unpolish(self._fixed_preview)
        self._fixed_preview.style().polish(self._fixed_preview)

    def _candidate_settings(self):
        candidate = self._settings.model_copy(deep=True)
        okx = candidate.execution.okx
        okx.sizing_mode = self._sizing_mode.currentData()
        okx.risk_capital_cap_usdt = Decimal(
            str(self._capital_cap.value())
        )
        okx.maximum_leverage = Decimal(
            str(self._maximum_leverage.value())
        )
        if okx.risk_capital_cap_usdt <= 0:
            raise ValueError("资金上限必须大于 0")
        if okx.sizing_mode == "fixed_quantity":
            quantity = Decimal(str(self._fixed_quantity.value()))
            if quantity <= 0 or quantity != quantity.to_integral_value():
                raise ValueError("固定合约张数必须是正整数")
            okx.quantity = str(quantity.to_integral_value())
        else:
            okx.risk_percent = Decimal(
                str(self._risk_percent.value())
            ) / Decimal("100")
        return candidate

    def _save_configuration(self) -> None:
        try:
            candidate = self._candidate_settings()
            save_settings(candidate, SETTINGS_JSON_PATH)
            replacement = candidate.model_copy(deep=True)
            for field_name in type(replacement).model_fields:
                setattr(
                    self._settings,
                    field_name,
                    getattr(replacement, field_name),
                )
            self._service.reload_settings(self._settings)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self._configuration_dirty = False
        self._save_button.setEnabled(False)
        self._cancel_config_button.setEnabled(False)
        self._dirty_banner.setVisible(False)
        self._refresh_pending_config()
        self.configuration_saved.emit()
        self.refresh_now()
        QMessageBox.information(
            self,
            "已保存",
            "保存成功；当前运行任务不变，"
            "下一次启动自动交易后生效。",
        )

    def refresh_now(self) -> None:
        self._network_route.setText(
            f"网络出口：{okx_fixed_proxy_label()} · "
            f"{okx_fixed_proxy_url()} · 独立于 v2rayN 当前节点"
        )
        if self._read_model is None:
            self._campaign_status.setText("模拟交易：只读状态未接入")
            return
        try:
            snapshot = self._read_model.capture()
        except Exception as exc:
            self._last_snapshot = None
            self._campaign_status.setText(
                f"模拟交易：状态读取失败（{type(exc).__name__}）"
            )
            self._worker_status.setText(
                "交易服务：状态未知 / 账户核对：状态未知"
            )
            self._risk_status.setText("风险闸门：状态未知")
            self._risk_status.setObjectName("errorText")
            self._captured_at.setText("更新时间：读取失败")
            self._risk_alert.setVisible(True)
            self._risk_alert_text.setText(
                "账户状态无法确认，系统不会新增风险。"
            )
            self._risk_recheck_button.setVisible(False)
            for label in (
                self._total_equity,
                self._usdt_equity,
                self._available,
                self._unrealized,
            ):
                label.setText("—")
            self._positions.setText(
                "当前持仓：状态未知，等待交易后台重新核对"
            )
            self._protection_status.setText(
                "当前保护：状态未知，系统不会新增风险"
            )
            self._decision_direction.setText("状态读取失败 / 等待核对")
            self._decision_quantity.setText("—")
            self._decision_prices.setText("—")
            self._decision_state.setText("当前真实状态未知")
            self._decision_risk.setText("等待账户状态恢复后重新计算")
            self._decision_reason.setText(
                "上一轮界面数据已作废，不作为当前仓位或订单依据。"
            )
            self._execution_table.setRowCount(0)
            self._timeline.clear()
            self._timeline.addItem("状态读取失败，暂不显示旧执行记录")
            self._refresh_action_buttons(None)
            return
        self._last_snapshot = snapshot
        self._captured_at.setText(
            "更新时间：" + _local_time(snapshot.captured_at)
        )
        self._campaign_status.setText(
            f"模拟交易：{_plain_status(snapshot.campaign_state.value)}"
        )
        self._worker_status.setText(
            f"交易服务：{_plain_status(snapshot.worker_state.value)} / "
            f"账户核对：{_plain_status(snapshot.reconcile.value)}"
        )
        self._risk_status.setText(
            f"风险闸门：{snapshot.risk_stop.value}"
        )
        self._risk_status.setObjectName(
            "errorText"
            if (
                snapshot.risk_stop.certainty is FactCertainty.UNKNOWN
                or "已停止" in snapshot.risk_stop.value
            )
            else "pillGreen"
        )
        self._risk_status.style().unpolish(self._risk_status)
        self._risk_status.style().polish(self._risk_status)
        self._active_config.setText(
            "当前运行参数：" + snapshot.campaign_risk_parameters.value
        )
        if self._configuration_dirty:
            self._alignment.setText(
                "当前编辑尚未保存；自动交易仍使用原启动值。"
            )
            self._alignment.setObjectName("warningText")
        else:
            self._alignment.setText(snapshot.campaign_config_alignment.value)
            if (
                snapshot.campaign_config_alignment.certainty
                is FactCertainty.UNKNOWN
                or snapshot.campaign_config_alignment.value.startswith("不一致")
            ):
                self._alignment.setObjectName("warningText")
            else:
                self._alignment.setObjectName("pillGreen")
        self._alignment.style().unpolish(self._alignment)
        self._alignment.style().polish(self._alignment)
        self._refresh_risk_alert(snapshot)
        self._refresh_account(snapshot)
        self._refresh_decision(snapshot)
        self._refresh_risk_preview(snapshot)
        self._refresh_fixed_preview(snapshot)
        self._refresh_executions()
        self._refresh_session()

    def _refresh_risk_alert(self, snapshot) -> None:
        risk_fact = snapshot.risk_stop
        state = getattr(snapshot, "risk_runtime_state", None)
        kill_active = bool(
            getattr(state, "kill_active", False)
            or str(risk_fact.value).startswith("已停止新增风险")
        )
        if (
            risk_fact.certainty is not FactCertainty.UNKNOWN
            and not kill_active
        ):
            self._risk_alert.setVisible(False)
            self._risk_recheck_button.setVisible(False)
            return

        self._risk_alert.setVisible(True)
        if risk_fact.certainty is FactCertainty.UNKNOWN:
            self._risk_alert_text.setText(
                "账户状态无法确认，系统不会新增风险。"
            )
        else:
            self._risk_alert_text.setText(risk_fact.value)
        reason = str(getattr(state, "kill_reason", "") or "")
        can_recheck = (
            reason in RECOVERABLE_TRANSIENT_RISK_STOP_REASONS
            and callable(
                getattr(self._service, "recover_transient_risk_stop", None)
            )
        )
        self._risk_recheck_button.setVisible(can_recheck)

    def _recheck_risk_state(self) -> None:
        self._run_async(
            self._service.recover_transient_risk_stop,
            "账户状态重新检查失败",
        )

    def _refresh_account(self, snapshot) -> None:
        account = snapshot.account_snapshot
        if (
            account is None
            or snapshot.account.certainty is not FactCertainty.CONFIRMED
        ):
            for label in (
                self._total_equity,
                self._usdt_equity,
                self._available,
                self._unrealized,
            ):
                label.setText("—")
            self._positions.setText(
                "当前持仓：账户数据已过期或尚未读取，暂不显示旧数值"
            )
            self._protection_status.setText(
                "当前保护：状态未知，系统不会新增风险"
            )
            self._protection_status.setObjectName("errorText")
            self._protection_status.style().unpolish(
                self._protection_status
            )
            self._protection_status.style().polish(
                self._protection_status
            )
            return
        raw = account.raw_summary
        self._total_equity.setText(
            _number(raw.get("account_total_equity")) + " USD"
        )
        self._usdt_equity.setText(
            _number(raw.get("usdt_equity") or account.equity) + " USDT"
        )
        self._available.setText(
            _number(raw.get("usdt_available_balance") or account.available)
            + " USDT"
        )
        self._unrealized.setText(
            _number(account.unrealized_pnl) + " USDT"
        )
        nonzero = [
            position
            for position in account.positions
            if position.quantity != 0
            and position.raw.get("kind") != "spot_balance"
        ]
        if not nonzero:
            self._positions.setText("当前持仓：空仓")
            protection_text = "当前保护：空仓，无需保护"
        else:
            self._positions.setText(
                "当前持仓："
                + "；".join(
                    f"{position.instrument} "
                    f"{'多' if position.direction == 'long' else '空'} "
                    f"{_number(position.quantity, decimals=0)} 张"
                    for position in nonzero
                )
            )
            record = snapshot.latest_execution
            if record is None or record.id not in set(
                snapshot.campaign_execution_ids
            ):
                protection_text = (
                    "当前保护：状态未知，等待交易后台完成只读核对"
                )
            else:
                protection_text = _protection_overview(record)
        self._protection_status.setText(protection_text)
        self._protection_status.setObjectName(
            "pillGreen"
            if (
                protection_text.startswith("当前保护：完整")
                or "无需保护" in protection_text
            )
            else "errorText"
        )
        self._protection_status.style().unpolish(
            self._protection_status
        )
        self._protection_status.style().polish(
            self._protection_status
        )

    def _refresh_decision(self, snapshot) -> None:
        record = snapshot.latest_execution
        current_campaign_ids = set(snapshot.campaign_execution_ids)
        if record is None or record.id not in current_campaign_ids:
            self._decision_direction.setText("本轮不下单 / 等待")
            self._decision_quantity.setText("—")
            self._decision_prices.setText("—")
            self._decision_state.setText("本轮没有生成订单")
            self._decision_risk.setText("等待下一条有效信号后计算")
            self._decision_reason.setText(
                "最近一轮：" + snapshot.campaign_last_result.value
            )
            self._refresh_technical(snapshot, None)
            return
        self._decision_direction.setText(_direction(record))
        self._decision_quantity.setText(
            _number(record.plan.quantity, decimals=0) + " 张"
        )
        self._decision_prices.setText(
            f"入场 {_number(record.plan.entry_price)} · "
            f"止损 {_number(record.plan.stop_loss)} · "
            f"{_take_profit_text(record)}"
        )
        self._decision_state.setText(_state_label(record))
        self._decision_risk.setText(_risk_text(record))
        result_text = _result_text(record)
        if snapshot.account.certainty is not FactCertainty.CONFIRMED:
            self._decision_direction.setText(
                "本地账本记录：" + _direction(record)
            )
            self._decision_state.setText("本地账本记录，待券商核对")
            self._decision_reason.setText(
                "账户状态尚未确认；方向、数量和价格仅为本地旧记录，"
                "不能当作当前真实仓位。"
            )
        else:
            self._decision_reason.setText(
                "执行正在按脚本生命周期推进。"
                if result_text == "—"
                else result_text
            )
        self._refresh_technical(snapshot, record)

    def _refresh_technical(
        self,
        snapshot,
        record: ExecutionRecord | None,
    ) -> None:
        state = getattr(snapshot, "risk_runtime_state", None)
        lines = [
            f"仓库：{PROJECT_ROOT}",
            f"本窗口代码加载时间：{self._code_loaded_at}",
            f"Campaign 原始状态：{snapshot.campaign_state.value}",
            f"Worker 原始状态：{snapshot.worker_state.value}",
            f"状态来源：{snapshot.campaign_state.source}",
            f"账户来源：{snapshot.account.source}",
        ]
        if record is not None:
            lines.extend(
                [
                    f"execution：{record.id}",
                    (
                        f"券商：{record.plan.broker} / "
                        f"{record.plan.environment}"
                    ),
                    f"品种：{record.plan.instrument}",
                    f"客户单号：{record.client_order_id or '—'}",
                    f"券商单号：{record.broker_order_id or '—'}",
                    f"原始状态：{record.state.value}",
                    f"执行错误码：{record.last_error or '—'}",
                ]
            )
        lines.extend(
            [
                (
                    "风险停止码："
                    + str(getattr(state, "kill_reason", "") or "—")
                ),
                (
                    "最近操作技术信息："
                    + (self._last_action_error_detail or "—")
                ),
                f"读取时间：{snapshot.captured_at}",
            ]
        )
        self._technical.setText("\n".join(lines))

    def _refresh_executions(self) -> None:
        selected = self._selected_execution()
        selected_id = selected.id if selected is not None else ""
        records = [
            record
            for record in self._service.list_recent(limit=100)
            if record.plan.broker == "okx"
            and record.plan.environment == "demo"
        ][:12]
        self._execution_table.blockSignals(True)
        self._execution_table.setRowCount(len(records))
        selected_row = -1
        for row, record in enumerate(records):
            if record.id == selected_id:
                selected_row = row
            protection_text = _protection_overview(record).replace(
                "当前保护：",
                "",
                1,
            )
            result = _result_text(record)
            account_confirmed = bool(
                self._last_snapshot is not None
                and self._last_snapshot.account.certainty
                is FactCertainty.CONFIRMED
            )
            state_text = (
                _state_label(record)
                if account_confirmed
                else "本地记录，待核对"
            )
            values = (
                _local_time(record.created_at),
                _direction(record),
                _number(record.plan.quantity, decimals=0),
                (
                    f"{_number(record.plan.entry_price)} / "
                    f"{_number(record.average_fill_price)}"
                ),
                protection_text,
                state_text,
                result,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record.id)
                if column in {2, 3}:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter
                    )
                self._execution_table.setItem(row, column, item)
        self._execution_table.blockSignals(False)
        if records:
            self._execution_table.selectRow(
                selected_row if selected_row >= 0 else 0
            )
        else:
            self._timeline.clear()
            self._timeline.addItem("暂无执行事件")
            self._refresh_action_buttons(None)

    def _selected_execution(self) -> ExecutionRecord | None:
        row = self._execution_table.currentRow()
        if row >= 0:
            item = self._execution_table.item(row, 0)
            if item is not None:
                execution_id = item.data(Qt.ItemDataRole.UserRole)
                if execution_id:
                    return self._service.get_execution(str(execution_id))
        return None

    def _refresh_selected_execution(self) -> None:
        record = self._selected_execution()
        self._timeline.clear()
        if record is None:
            self._timeline.addItem("暂无执行事件")
            self._refresh_action_buttons(None)
            return
        events = self._service.events(record.id)
        for event in events[-10:]:
            label = _event_label(event.kind)
            self._timeline.addItem(
                f"{_local_time(event.created_at)}  {label}"
            )
        if not events:
            self._timeline.addItem("尚无执行事件")
        self._refresh_action_buttons(record)

    def _refresh_action_buttons(self, record: ExecutionRecord | None) -> None:
        state = record.state if record is not None else None
        account_confirmed = bool(
            self._last_snapshot is not None
            and self._last_snapshot.account.certainty
            is FactCertainty.CONFIRMED
        )
        risk_allowed = bool(
            self._last_snapshot is not None
            and self._last_snapshot.risk_stop.certainty
            is FactCertainty.CONFIRMED
            and str(self._last_snapshot.risk_stop.value).startswith(
                "允许新增风险"
            )
        )
        self._submit_button.setEnabled(
            not self._configuration_dirty
            and account_confirmed
            and risk_allowed
            and state is ExecutionState.READY
        )
        can_cancel = (
            state
            in {
                ExecutionState.SUBMITTING,
                ExecutionState.ENTRY_PENDING,
                ExecutionState.PARTIALLY_FILLED,
            }
        )
        can_exit = account_confirmed and (
            state
            in {
                ExecutionState.PARTIALLY_FILLED,
                ExecutionState.PROTECTING,
                ExecutionState.OPEN,
            }
        )
        self._cancel_button.setVisible(can_cancel)
        self._cancel_button.setEnabled(can_cancel)
        self._exit_button.setVisible(can_exit)
        self._exit_button.setEnabled(can_exit)

    def _refresh_session(self) -> None:
        armed = bool(self._service.is_armed)
        if self._configuration_dirty:
            self._manual_session.setText(
                "配置正在编辑，旧手动会话已停用。请先保存，再重新启用。"
            )
        elif armed:
            self._manual_session.setText(
                "已启用本次模拟写操作。自动交易状态以上方状态带为准。"
            )
        else:
            self._manual_session.setText(
                "停用。自动 10 分钟模拟交易不依赖这个按钮。"
            )
        self._arm_button.setEnabled(not armed and not self._configuration_dirty)
        self._disarm_button.setEnabled(armed)
        self._refresh_action_buttons(self._selected_execution())

    def _arm_session(self) -> None:
        confirmation = self._service.arm_confirmation_text()
        text, accepted = QInputDialog.getText(
            self,
            "启用本次模拟交易会话",
            "模拟订单仍会按真实行情撮合。\n"
            f"请输入：{confirmation}",
        )
        if not accepted:
            return
        try:
            self._service.arm(text)
        except Exception as exc:
            QMessageBox.warning(self, "未启用", str(exc))
        self._refresh_session()

    def _disarm_session(self) -> None:
        self._service.disarm()
        self._refresh_session()

    def _submit_selected(self) -> None:
        record = self._selected_execution()
        if record is not None:
            self._run_async(
                lambda: self._service.submit(record.id),
                "提交失败",
            )

    def _cancel_selected(self) -> None:
        record = self._selected_execution()
        if record is not None:
            self._run_async(
                lambda: self._service.cancel_entry(record.id),
                "撤销失败",
            )

    def _exit_selected(self) -> None:
        record = self._selected_execution()
        if record is None:
            return
        answer = QMessageBox.question(
            self,
            "确认主动离场",
            f"确认退出 {record.plan.instrument} 剩余 "
            f"{_number(record.remaining_quantity, decimals=0)} 张？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run_async(
            lambda: self._service.request_exit(record.id),
            "离场请求失败",
        )

    def _run_async(self, action, label: str) -> None:
        def run() -> None:
            command_id = ""
            try:
                command = action()
                command_id = str(getattr(command, "id", "") or "")
                if command_id:
                    result = self._service.wait_for_command(
                        command_id,
                        timeout=30,
                    )
                    if result.status is WorkerCommandStatus.UNCERTAIN:
                        raise RuntimeError(
                            "结果尚未确定，禁止重复操作；请等待券商对账"
                        )
                    if result.status is WorkerCommandStatus.FAILED:
                        self._last_action_error_detail = (
                            result.failure_code or "worker_command_failed"
                        )
                        self._action_failed.emit(
                            f"{label}：交易后台拒绝了请求，详情见技术信息"
                        )
                        return
                    if result.status is not WorkerCommandStatus.SUCCEEDED:
                        self._last_action_error_detail = (
                            f"worker_status:{result.status.value}"
                        )
                        self._action_failed.emit(
                            f"{label}：交易后台尚未确认请求结果，"
                            "详情见技术信息"
                        )
                        return
            except TimeoutError:
                self._last_action_error_detail = (
                    f"timeout command_id={command_id}"
                    if command_id
                    else "timeout before command id"
                )
                self._action_failed.emit(
                    f"{label}：后台结果尚未确定，请勿重复操作；"
                    "详情见技术信息"
                )
            except Exception as exc:
                self._last_action_error_detail = (
                    f"{type(exc).__name__}: {exc}"
                )
                self._action_failed.emit(
                    f"{label}：操作未完成，详情见技术信息"
                )
            else:
                self._last_action_error_detail = ""
                self._action_completed.emit()

        threading.Thread(
            target=run,
            name="pa-trading-workbench-action",
            daemon=True,
        ).start()

    def _show_action_error(self, message: str) -> None:
        text = str(message)
        safe_phrases = (
            "后台结果尚未确定，请勿重复操作",
            "交易后台拒绝了请求，详情见技术信息",
            "交易后台尚未确认请求结果，详情见技术信息",
            "操作未完成，详情见技术信息",
        )
        self._decision_reason.setText(
            text
            if any(phrase in text for phrase in safe_phrases)
            else "操作未完成，详情见技术信息"
        )
        self._decision_reason.setObjectName("errorText")
        self._decision_reason.style().unpolish(self._decision_reason)
        self._decision_reason.style().polish(self._decision_reason)
        if self._last_snapshot is not None:
            self._refresh_technical(
                self._last_snapshot,
                self._selected_execution(),
            )
