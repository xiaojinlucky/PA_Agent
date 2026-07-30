"""PRD11 原生 PyQt6 多市场只读工作台。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QBrush, QCloseEvent, QColor, QFontDatabase
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pa_agent.data.market_workspace import (
    AnalysisCapabilityState,
    AnalysisGateReason,
    EvidenceState,
    QuoteFailureKind,
    QuoteFreshness,
    normalise_symbol_for_market,
    quote_source_for_market,
)
from pa_agent.data.market_workspace_controller import (
    AnalysisFailureKind,
    AnalysisFailureStage,
    AnalysisState,
    SelectionState,
    SettingsSaveState,
    SourceAuthState,
)
from pa_agent.gui.chart_widget import ChartWidget

_MARKET_LABELS = {
    "US": "美股",
    "HK": "港股",
    "CN": "A股",
    "Crypto": "加密",
}
_SOURCE_LABELS = {
    "longbridge": "Longbridge",
    "okx": "OKX",
}
_EVIDENCE_LABELS = {
    EvidenceState.READY: "就绪",
    EvidenceState.INSUFFICIENT: "不足",
    EvidenceState.STALE: "已过期",
    EvidenceState.UNAVAILABLE: "不可用",
}
_STATUS_ICONS = {
    "ready": "✓",
    "warning": "⚠",
    "error": "×",
    "loading": "↻",
    "muted": "—",
}
_ANALYSIS_FAILURE_LABELS = {
    AnalysisFailureKind.INVALID_RESULT: "分析结果格式无效",
    AnalysisFailureKind.WORKER_FAILED: "分析服务执行失败",
}
_ANALYSIS_STAGE_LABELS = {
    AnalysisFailureStage.SERVICE_INITIALIZATION: "服务初始化",
    AnalysisFailureStage.INPUT_FREEZE: "输入冻结",
    AnalysisFailureStage.MARKET_DIAGNOSIS: "市场诊断",
    AnalysisFailureStage.DECISION_GENERATION: "决策生成",
    AnalysisFailureStage.RESULT_VALIDATION: "结果校验",
}

_WORKBENCH_QSS = """
QWidget#multiMarketWorkbench {
    background: #090B10;
    color: #E7ECF4;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 14px;
}
QWidget#multiMarketWorkbench QLabel {
    color: #E7ECF4;
    font-size: 14px;
}
QFrame#marketContextBar, QFrame#marketStatusBar {
    background: #0D1016;
    border-bottom: 1px solid #252B36;
}
QFrame#marketStatusBar {
    border-top: 1px solid #252B36;
    border-bottom: none;
}
QFrame#marketLeftPanel, QFrame#marketCenterPanel, QFrame#marketRightPanel,
QFrame#marketSummaryPanel, QFrame#marketChartPanel, QFrame#marketAnalysisPanel,
QFrame#marketEvidenceCard, QFrame#marketAnalysisColumn {
    background: #10131A;
    border: 1px solid #252B36;
}
QFrame#marketLeftPanel, QFrame#marketCenterPanel, QFrame#marketRightPanel {
    border-top: none;
    border-bottom: none;
}
QWidget#multiMarketWorkbench QLabel[role="pageTitle"] {
    color: #E7ECF4;
    font-size: 16px;
    font-weight: 600;
}
QWidget#multiMarketWorkbench QLabel[role="symbol"],
QWidget#multiMarketWorkbench QLabel[role="price"] {
    color: #E7ECF4;
    font-size: 22px;
    font-weight: 600;
}
QWidget#multiMarketWorkbench QLabel[role="blockTitle"] {
    color: #E7ECF4;
    font-size: 15px;
    font-weight: 600;
}
QWidget#multiMarketWorkbench QLabel[role="secondary"] {
    color: #9AA3B2;
}
QWidget#multiMarketWorkbench QLabel[role="mono"],
QWidget#multiMarketWorkbench QLabel[role="price"] {
    font-family: "Cascadia Mono", "Consolas", monospace;
}
QWidget#multiMarketWorkbench QLabel[stale="true"] {
    color: #737C8B;
}
QLabel[quoteDirection="up"] {
    color: #E5484D;
}
QLabel[quoteDirection="down"] {
    color: #2EBD85;
}
QLabel[stateTone="ready"] { color: #18B26B; }
QLabel[stateTone="warning"] { color: #E6A23C; }
QLabel[stateTone="error"] { color: #F05252; }
QLabel[stateTone="loading"] { color: #2F8DFF; }
QLabel[stateTone="muted"] { color: #9AA3B2; }
QPushButton, QToolButton {
    min-height: 36px;
    padding: 0 12px;
    color: #E7ECF4;
    background: #151922;
    border: 1px solid #252B36;
    border-radius: 3px;
    font-size: 14px;
}
QPushButton:hover, QToolButton:hover {
    border-color: #2F8DFF;
}
QToolButton:checked {
    color: #E7ECF4;
    background: #18283D;
    border-color: #2F8DFF;
}
QPushButton#marketAnalysisButton {
    min-height: 40px;
    background: #2F8DFF;
    border-color: #2F8DFF;
    font-weight: 600;
}
QPushButton#marketAnalysisButton:disabled {
    color: #646D7C;
    background: #151922;
    border-color: #252B36;
}
QLineEdit#marketSearch {
    min-height: 34px;
    color: #E7ECF4;
    background: #090B10;
    border: 1px solid #252B36;
    border-radius: 3px;
    padding: 0 8px;
    font-size: 14px;
}
QLineEdit#marketSearch:focus {
    border-color: #2F8DFF;
}
QTableView#marketWatchlist {
    color: #E7ECF4;
    background: #10131A;
    alternate-background-color: #0D1016;
    border: none;
    gridline-color: #252B36;
    font-size: 14px;
}
QTableView#marketWatchlist::item {
    padding: 0;
    border-bottom: 1px solid #252B36;
}
QTableView#marketWatchlist::item:selected {
    background: #18283D;
    color: #E7ECF4;
}
QHeaderView::section {
    height: 32px;
    color: #9AA3B2;
    background: #0D1016;
    border: none;
    border-bottom: 1px solid #252B36;
    padding: 0 6px;
    font-size: 14px;
    font-weight: 500;
}
QScrollArea#marketReasonScroll {
    background: transparent;
    border: none;
}
QScrollArea#marketReasonScroll > QWidget > QWidget {
    background: transparent;
}
QScrollBar:vertical {
    width: 8px;
    background: #0D1016;
}
QScrollBar::handle:vertical {
    min-height: 24px;
    background: #252B36;
    border-radius: 4px;
}
"""


def _signed(
    value: str | None,
    *,
    decimal_places: int | None = None,
) -> str:
    if value is None:
        return "—"
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    prefix = "+" if parsed > 0 else ""
    if decimal_places is None:
        rendered = format(parsed, "f")
    else:
        rendered = f"{parsed:.{decimal_places}f}"
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
    return f"{prefix}{rendered}"


def _format_utc(utc_ms: int | None, *, seconds: bool = True) -> str:
    if utc_ms is None:
        return "—"
    pattern = "%Y-%m-%d %H:%M:%S UTC" if seconds else "%H:%M UTC"
    return datetime.fromtimestamp(utc_ms / 1000, tz=UTC).strftime(pattern)


def _set_property(widget: QWidget, name: str, value: str) -> None:
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class WatchlistTableModel(QAbstractTableModel):
    """只读自选表；所有价格已经由后端快照计算。"""

    HEADERS = ("标的", "最新价", "涨跌幅")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[tuple[str, str, str, bool], ...] = ()
        self._mono = QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont
        )
        self._mono.setPixelSize(14)

    def set_rows(
        self,
        rows: list[tuple[str, str, str, bool]],
    ) -> None:
        self.beginResetModel()
        self._rows = tuple(rows)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return (
            0
            if parent is not None and parent.isValid()
            else len(self._rows)
        )

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else 3

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            orientation is Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        symbol, price, change_pct, stale = self._rows[index.row()]
        values = (symbol, price, change_pct)
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return symbol
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(
                Qt.AlignmentFlag.AlignVCenter
                | (
                    Qt.AlignmentFlag.AlignLeft
                    if index.column() == 0
                    else Qt.AlignmentFlag.AlignRight
                )
            )
        if role == Qt.ItemDataRole.FontRole and index.column() > 0:
            return self._mono
        if role == Qt.ItemDataRole.ForegroundRole and stale:
            return QBrush(QColor("#737C8B"))
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 2:
            if change_pct.startswith("+"):
                return QBrush(QColor("#E5484D"))
            if change_pct.startswith("-"):
                return QBrush(QColor("#2EBD85"))
        return None


class _EvidenceCard(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("marketEvidenceCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)
        heading = QLabel(title)
        heading.setProperty("role", "blockTitle")
        layout.addWidget(heading)
        self.content_layout = QVBoxLayout()
        self.content_layout.setSpacing(4)
        layout.addLayout(self.content_layout, 1)

    def add_value_row(self, name: str) -> QLabel:
        row = QHBoxLayout()
        row.setSpacing(8)
        key = QLabel(name)
        key.setProperty("role", "secondary")
        value = QLabel("—")
        value.setAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(key)
        row.addWidget(value, 1)
        self.content_layout.addLayout(row)
        return value


class MultiMarketWorkbench(QWidget):
    """三栏高密度只读行情与两阶段分析页面。"""

    request_history_refresh = pyqtSignal()

    def __init__(
        self,
        *,
        controller: Any,
        bridge: Any,
        runtime_sha: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("multiMarketWorkbench")
        self.setStyleSheet(_WORKBENCH_QSS)
        self._controller = controller
        self._bridge = bridge
        self._runtime_sha = str(runtime_sha or "unavailable")
        self._build_ui()
        self.destroyed.connect(self._chart.close)
        bridge.state_changed.connect(self.render)
        bridge.status_changed.connect(self._on_status_changed)
        bridge.analysis_phase_changed.connect(
            self._on_analysis_phase_changed
        )
        self.render()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._context_bar = QFrame()
        self._context_bar.setObjectName("marketContextBar")
        self._context_bar.setFixedHeight(48)
        context = QHBoxLayout(self._context_bar)
        context.setContentsMargins(12, 0, 12, 0)
        context.setSpacing(8)
        title = QLabel("多市场看盘")
        title.setProperty("role", "pageTitle")
        context.addWidget(title)

        self._market_group = QButtonGroup(self)
        self._market_group.setExclusive(True)
        self._market_buttons: dict[str, QToolButton] = {}
        for market, label in _MARKET_LABELS.items():
            button = QToolButton()
            button.setText(label)
            button.setAccessibleName(f"切换到{label}")
            button.setAccessibleDescription(
                f"选择{label}及其固定行情源"
            )
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, selected=market: (
                    self._select_market(selected)
                )
            )
            self._market_group.addButton(button)
            self._market_buttons[market] = button
            context.addWidget(button)
        context.addSpacing(8)
        self._source_label = QLabel("—")
        self._source_label.setProperty("role", "secondary")
        context.addWidget(self._source_label)
        self._connection_label = QLabel("× 不可用")
        self._connection_label.setProperty("stateTone", "error")
        context.addWidget(self._connection_label)
        self._operation_status_label = QLabel("")
        self._operation_status_label.setProperty("stateTone", "warning")
        self._operation_status_label.hide()
        context.addWidget(self._operation_status_label)
        context.addStretch(1)
        self._refresh_button = QPushButton("刷新")
        self._refresh_button.setAccessibleName("刷新当前市场行情")
        self._refresh_button.clicked.connect(self._bridge.refresh)
        context.addWidget(self._refresh_button)
        root.addWidget(self._context_bar)

        self._body = QWidget()
        body = QHBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self._left_panel = self._build_left_panel()
        self._center_panel = self._build_center_panel()
        self._right_panel = self._build_right_panel()
        self._left_panel.setFixedWidth(240)
        self._right_panel.setFixedWidth(340)
        body.addWidget(self._left_panel)
        body.addWidget(self._center_panel, 1)
        body.addWidget(self._right_panel)
        root.addWidget(self._body, 1)

        self._status_bar = QFrame()
        self._status_bar.setObjectName("marketStatusBar")
        self._status_bar.setFixedHeight(24)
        status = QHBoxLayout(self._status_bar)
        status.setContentsMargins(10, 0, 10, 0)
        status.setSpacing(16)
        self._identity_status = QLabel("—")
        self._identity_status.setProperty("role", "secondary")
        self._identity_status.setMinimumWidth(210)
        self._generation_status = QLabel("generation —")
        self._generation_status.setProperty("role", "secondary")
        self._generation_status.setMinimumWidth(100)
        self._refresh_status = QLabel("刷新 —")
        self._refresh_status.setProperty("role", "secondary")
        self._refresh_status.setMinimumWidth(130)
        self._settings_status = QLabel("已保存")
        self._settings_status.setProperty("role", "secondary")
        self._settings_status.setMinimumWidth(80)
        self._runtime_sha_label = QLabel(f"SHA {self._runtime_sha}")
        self._runtime_sha_label.setProperty("role", "secondary")
        status.addWidget(self._identity_status)
        status.addWidget(self._generation_status)
        status.addWidget(self._refresh_status)
        status.addWidget(self._settings_status)
        status.addStretch(1)
        status.addWidget(self._runtime_sha_label)
        root.addWidget(self._status_bar)
        self._configure_focus_order()

    def _configure_focus_order(self) -> None:
        controls: list[QWidget] = [
            *self._market_buttons.values(),
            self._refresh_button,
            self._search,
            self._add_button,
            self._watchlist_table,
            *self._timeframe_buttons.values(),
            self._analysis_button,
        ]
        for current, following in pairwise(controls):
            QWidget.setTabOrder(current, following)
        self._watchlist_table.setAccessibleName("当前市场自选列表")
        self._watchlist_table.setAccessibleDescription(
            "每行依次显示完整标的代码、最新价和涨跌幅"
        )

    def _build_left_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("marketLeftPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QFrame()
        toolbar.setFixedHeight(48)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 6, 8, 6)
        toolbar_layout.setSpacing(6)
        self._left_market_label = QLabel("—")
        self._left_market_label.setProperty("role", "blockTitle")
        self._left_market_label.setFixedWidth(34)
        self._search = QLineEdit()
        self._search.setObjectName("marketSearch")
        self._search.setPlaceholderText("搜索标的")
        self._search.setAccessibleName("筛选当前市场自选标的")
        self._add_button = QPushButton("+")
        self._add_button.setFixedWidth(38)
        self._add_button.setToolTip("添加本地自选")
        self._add_button.setAccessibleName("添加本地自选")
        self._add_button.clicked.connect(self._add_watchlist_symbol)
        toolbar_layout.addWidget(self._left_market_label)
        toolbar_layout.addWidget(self._search, 1)
        toolbar_layout.addWidget(self._add_button)
        layout.addWidget(toolbar)

        self._watchlist_model = WatchlistTableModel(self)
        self._watchlist_proxy = QSortFilterProxyModel(self)
        self._watchlist_proxy.setSourceModel(self._watchlist_model)
        self._watchlist_proxy.setFilterCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive
        )
        self._watchlist_proxy.setFilterKeyColumn(0)
        self._search.textChanged.connect(
            self._watchlist_proxy.setFilterFixedString
        )
        self._watchlist_table = QTableView()
        self._watchlist_table.setObjectName("marketWatchlist")
        self._watchlist_table.setModel(self._watchlist_proxy)
        self._watchlist_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._watchlist_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._watchlist_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._watchlist_table.setAlternatingRowColors(True)
        self._watchlist_table.setWordWrap(False)
        self._watchlist_table.verticalHeader().setVisible(False)
        self._watchlist_table.verticalHeader().setDefaultSectionSize(40)
        self._watchlist_table.horizontalHeader().setFixedHeight(32)
        self._watchlist_table.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        self._watchlist_table.horizontalHeader().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.Fixed,
        )
        self._watchlist_table.horizontalHeader().setSectionResizeMode(
            2,
            QHeaderView.ResizeMode.Fixed,
        )
        self._watchlist_table.setColumnWidth(1, 55)
        self._watchlist_table.setColumnWidth(2, 57)
        self._watchlist_table.clicked.connect(
            self._on_watchlist_clicked
        )
        layout.addWidget(self._watchlist_table, 1)
        return panel

    def _build_center_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("marketCenterPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._summary_panel = self._build_summary_panel()
        self._chart_panel = self._build_chart_panel()
        self._analysis_panel = self._build_analysis_panel()
        layout.addWidget(self._summary_panel)
        layout.addWidget(self._chart_panel)
        layout.addWidget(self._analysis_panel)
        return panel

    def _build_summary_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("marketSummaryPanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(12)
        identity = QVBoxLayout()
        identity.setSpacing(1)
        self._symbol_label = QLabel("—")
        self._symbol_label.setProperty("role", "symbol")
        self._name_label = QLabel("—")
        self._name_label.setProperty("role", "secondary")
        self._identity_badge_label = QLabel("—")
        self._identity_badge_label.setProperty("role", "secondary")
        identity.addWidget(self._symbol_label)
        identity.addWidget(self._name_label)
        identity.addWidget(self._identity_badge_label)
        layout.addLayout(identity, 2)

        quote = QVBoxLayout()
        quote.setSpacing(1)
        self._price_label = QLabel("—")
        self._price_label.setProperty("role", "price")
        self._change_label = QLabel("—")
        self._change_label.setProperty("role", "mono")
        self._quote_time_label = QLabel("—")
        self._quote_time_label.setProperty("role", "secondary")
        quote.addWidget(self._price_label)
        quote.addWidget(self._change_label)
        quote.addWidget(self._quote_time_label)
        layout.addLayout(quote, 2)

        actions = QHBoxLayout()
        actions.setSpacing(4)
        self._timeframe_group = QButtonGroup(self)
        self._timeframe_group.setExclusive(True)
        self._timeframe_buttons: dict[str, QToolButton] = {}
        for timeframe in ("10m", "1h", "4h"):
            button = QToolButton()
            button.setText(timeframe)
            button.setAccessibleName(f"展示 {timeframe} K 线")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, selected=timeframe: (
                    self._select_timeframe(selected)
                )
            )
            self._timeframe_group.addButton(button)
            self._timeframe_buttons[timeframe] = button
            actions.addWidget(button)
        self._analysis_button = QPushButton("开始分析")
        self._analysis_button.setObjectName("marketAnalysisButton")
        self._analysis_button.setAccessibleName(
            "基于当前固定 10m 数据开始只读分析"
        )
        self._analysis_button.clicked.connect(
            self._bridge.start_analysis
        )
        actions.addWidget(self._analysis_button)
        layout.addLayout(actions)
        return panel

    def _build_chart_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("marketChartPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QFrame()
        header.setFixedHeight(28)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 10, 0)
        self._chart_ohlc_label = QLabel("K 线")
        self._chart_ohlc_label.setProperty("role", "secondary")
        self._chart_data_state = QLabel("—")
        self._chart_data_state.setProperty("role", "secondary")
        header_layout.addWidget(self._chart_ohlc_label, 1)
        header_layout.addWidget(self._chart_data_state)
        layout.addWidget(header)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        self._chart = ChartWidget(
            container,
            market_read_only=True,
        )
        container.destroyed.connect(self._chart.close)
        # 只读图表没有键盘操作，避免 Tab 焦点落到没有可执行动作的画布。
        self._chart.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        container_layout.addWidget(self._chart)
        self._chart_state_label = QLabel("暂无行情数据", container)
        self._chart_state_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self._chart_state_label.setProperty("role", "secondary")
        self._chart_state_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._chart_state_label.raise_()
        layout.addWidget(container, 1)
        self._chart_container = container
        return panel

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Stop chart rendering before Qt destroys child axes."""
        self._chart.close()
        super().closeEvent(event)

    def _analysis_column(
        self,
        title: str,
        fields: tuple[str, ...],
    ) -> tuple[QFrame, dict[str, QLabel]]:
        frame = QFrame()
        frame.setObjectName("marketAnalysisColumn")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        heading = QLabel(title)
        heading.setProperty("role", "blockTitle")
        layout.addWidget(heading)
        values: dict[str, QLabel] = {}
        for field in fields:
            row = QHBoxLayout()
            key = QLabel(field)
            key.setProperty("role", "secondary")
            value = QLabel("—")
            value.setAlignment(
                Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter
            )
            row.addWidget(key)
            row.addWidget(value, 1)
            layout.addLayout(row)
            values[field] = value
        layout.addStretch(1)
        return frame, values

    def _build_analysis_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("marketAnalysisPanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        diagnosis, self._diagnosis_values = self._analysis_column(
            "市场诊断",
            ("周期位置", "方向", "判断置信度", "10m", "1h", "4h"),
        )
        decision, self._decision_values = self._analysis_column(
            "决策摘要",
            (
                "订单类型",
                "方向",
                "交易置信度",
                "入场",
                "止损",
                "目标",
                "终局",
                "理由",
            ),
        )
        layout.addWidget(diagnosis, 1)
        layout.addWidget(decision, 1)
        return panel

    def _build_right_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("marketRightPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._quote_card = _EvidenceCard("即时报价")
        self._quote_values: dict[str, QLabel] = {}
        quote_grid = QGridLayout()
        quote_grid.setContentsMargins(0, 0, 0, 0)
        quote_grid.setHorizontalSpacing(8)
        quote_grid.setVerticalSpacing(2)
        quote_rows = (
            (("最新价", "最新价"), ("上一收盘", "上一收盘")),
            (("涨跌额", "涨跌额"), ("涨跌幅", "涨跌幅")),
            (("币种", "币种"), ("行情模式", "行情模式")),
            (("行情源时间", "行情源"), ("本机接收", "本机")),
            (("新鲜度", "新鲜度"),),
        )
        for row_index, row in enumerate(quote_rows):
            for pair_index, (name, visible_name) in enumerate(row):
                column = pair_index * 2
                key = QLabel(visible_name)
                key.setProperty("role", "secondary")
                value = QLabel("—")
                value.setAccessibleName(name)
                value.setAlignment(
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignVCenter
                )
                quote_grid.addWidget(key, row_index, column)
                quote_grid.addWidget(value, row_index, column + 1)
                self._quote_values[name] = value
        quote_grid.setColumnStretch(1, 1)
        quote_grid.setColumnStretch(3, 1)
        self._quote_card.content_layout.addLayout(quote_grid)
        self._clock_card = _EvidenceCard("市场时钟")
        self._clock_values = {
            name: self._clock_card.add_value_row(name)
            for name in ("状态", "当地时间", "下一变化", "时区")
        }
        self._kline_card = _EvidenceCard("K 线证据")
        self._kline_rows: dict[str, QLabel] = {}
        for timeframe in ("10m", "1h", "4h"):
            label = QLabel(f"{timeframe}  —")
            label.setProperty("role", "mono")
            self._kline_card.content_layout.addWidget(label)
            self._kline_rows[timeframe] = label

        self._gate_card = _EvidenceCard("分析门禁")
        self._gate_conclusion = QLabel("等待 10m 数据")
        self._gate_conclusion.setProperty("stateTone", "warning")
        self._gate_conclusion.setWordWrap(True)
        self._gate_card.content_layout.addWidget(
            self._gate_conclusion
        )
        self._gate_reason_labels: list[QLabel] = []
        for _ in range(3):
            label = QLabel("")
            label.setProperty("role", "secondary")
            label.setWordWrap(True)
            self._gate_card.content_layout.addWidget(label)
            self._gate_reason_labels.append(label)
        self._gate_card.content_layout.addStretch(1)

        layout.addWidget(self._quote_card)
        layout.addWidget(self._clock_card)
        layout.addWidget(self._kline_card)
        layout.addWidget(self._gate_card)
        return panel

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_geometry_contract()
        self._chart_state_label.setGeometry(
            self._chart_container.rect()
        )

    def _apply_geometry_contract(self) -> None:
        self._left_panel.setFixedWidth(240)
        self._right_panel.setFixedWidth(
            360 if self.width() >= 1800 else 340
        )
        body_height = max(0, self.height() - 48 - 24)
        self._summary_panel.setFixedHeight(72)
        chart_height = 560 if body_height >= 900 else 432
        analysis_height = max(0, body_height - 72 - chart_height)
        self._chart_panel.setFixedHeight(chart_height)
        self._analysis_panel.setFixedHeight(analysis_height)
        if body_height == 768:
            heights = (164, 204, 188, 212)
        else:
            scale = body_height / 768 if body_height else 1
            first = round(164 * scale)
            second = round(204 * scale)
            third = round(188 * scale)
            heights = (
                first,
                second,
                third,
                max(0, body_height - first - second - third),
            )
        for card, height in zip(
            (
                self._quote_card,
                self._clock_card,
                self._kline_card,
                self._gate_card,
            ),
            heights,
            strict=True,
        ):
            card.setFixedHeight(height)

    def _current_market(self, view: Any) -> str:
        identity = view.staged_identity or view.committed_identity
        if identity is not None:
            return identity.market
        return self._controller.workspace_settings.selected_market

    def _select_market(self, market: str) -> None:
        settings = self._controller.workspace_settings
        self._bridge.select(
            market=market,
            symbol=settings.last_symbols_by_market[market],
            display_timeframe=(
                settings.display_timeframes_by_market[market]
            ),
        )

    def _select_timeframe(self, timeframe: str) -> None:
        view = self._bridge.snapshot()
        identity = view.committed_identity
        if identity is None or view.staged_identity is not None:
            return
        self._bridge.select(
            market=identity.market,
            symbol=identity.symbol,
            display_timeframe=timeframe,
        )

    def _on_watchlist_clicked(self, proxy_index: QModelIndex) -> None:
        source_index = self._watchlist_proxy.mapToSource(proxy_index)
        symbol = self._watchlist_model.data(
            source_index,
            Qt.ItemDataRole.UserRole,
        )
        view = self._bridge.snapshot()
        identity = view.committed_identity
        market = self._current_market(view)
        timeframe = (
            identity.display_timeframe
            if identity is not None and identity.market == market
            else self._controller.workspace_settings.display_timeframes_by_market[
                market
            ]
        )
        self._bridge.select(
            market=market,
            symbol=symbol,
            display_timeframe=timeframe,
        )

    def _add_watchlist_symbol(self) -> None:
        view = self._bridge.snapshot()
        market = self._current_market(view)
        raw = self._search.text().strip()
        if not raw:
            return
        try:
            symbol = normalise_symbol_for_market(market, raw)
        except ValueError:
            _set_property(self._search, "invalid", "true")
            return
        _set_property(self._search, "invalid", "false")
        current = list(
            self._controller.workspace_settings.watchlists_by_market[
                market
            ]
        )
        if symbol not in current:
            current.append(symbol)
            self._bridge.set_watchlist(current)
        self._search.clear()

    def _on_status_changed(self, _text: str) -> None:
        self.render()

    def _on_analysis_phase_changed(self, phase: str) -> None:
        if phase:
            self._analysis_button.setText("分析中")
        self.render()

    def render(self) -> None:
        view = self._bridge.snapshot()
        current_market = self._current_market(view)
        for market, button in self._market_buttons.items():
            button.blockSignals(True)
            button.setChecked(market == current_market)
            button.blockSignals(False)
        source = quote_source_for_market(current_market)
        self._source_label.setText(_SOURCE_LABELS[source])
        self._render_connection(view, source)
        self._render_operation_status(view)
        self._render_watchlist(view, current_market)
        self._render_summary_and_chart(view)
        self._render_quote(view)
        self._render_clock(view, current_market)
        self._render_kline_evidence(view)
        self._render_analysis(view)
        self._render_gate(view)
        self._render_status_bar(view)
        self._apply_geometry_contract()

    def _render_operation_status(self, view: Any) -> None:
        status = str(getattr(self._bridge, "status", "") or "")
        tone = "warning"
        if view.selection_state is SelectionState.STAGING:
            target = view.staged_identity
            text = (
                f"正在切换到 {target.symbol}"
                if target is not None
                else "正在加载"
            )
            tone = "loading"
        elif view.selection_state is SelectionState.SWITCH_FAILED:
            target = view.failed_identity
            text = (
                f"切换到 {target.symbol} 失败"
                if target is not None
                else "切换失败"
            )
            tone = "error"
        elif view.selection_state is SelectionState.AUTH_INVALID:
            # 当前来源和“认证失效”已由相邻连接状态完整表达，避免重复。
            text = ""
            tone = "error"
        elif view.settings_save_state is not SettingsSaveState.SAVED:
            text = "本地设置未保存"
        elif view.analysis_state is AnalysisState.RUNNING:
            text = self._bridge.analysis_phase or "分析进行中"
            tone = "loading"
        elif view.analysis_state is AnalysisState.FAILED:
            text = "分析失败"
            tone = "error"
        elif status not in {"", "未加载", "行情已更新", "分析完成"}:
            text = status
        else:
            text = ""
        self._operation_status_label.setText(
            f"{_STATUS_ICONS[tone]} {text}" if text else ""
        )
        _set_property(self._operation_status_label, "stateTone", tone)
        self._operation_status_label.setVisible(bool(text))

    def _render_connection(self, view: Any, source: str) -> None:
        auth = (
            view.longbridge_auth
            if source == "longbridge"
            else view.okx_auth
        )
        if auth is SourceAuthState.INVALID:
            tone, text = "error", "认证失效"
        elif view.selection_state is SelectionState.STAGING:
            tone, text = "loading", "恢复中"
        elif auth is SourceAuthState.VALID:
            tone, text = "ready", "可用"
        else:
            tone, text = "error", "不可用"
        self._connection_label.setText(
            f"{_STATUS_ICONS[tone]} {text}"
        )
        _set_property(self._connection_label, "stateTone", tone)

    def _render_watchlist(self, view: Any, market: str) -> None:
        settings = self._controller.workspace_settings
        self._left_market_label.setText(_MARKET_LABELS[market])
        symbols = settings.watchlists_by_market[market]
        snapshots: dict[str, Any] = {}
        stale_symbols: set[str] = set()
        if view.watchlist is not None and view.watchlist.token.market == market:
            snapshots = {
                snapshot.symbol: snapshot
                for snapshot in view.watchlist.snapshots
            }
        if (
            view.bundle is not None
            and view.bundle.quote.snapshot is not None
            and view.bundle.quote.snapshot.market == market
        ):
            snapshot = view.bundle.quote.snapshot
            if view.bundle_current or snapshot.symbol not in snapshots:
                snapshots[snapshot.symbol] = snapshot
                if not view.bundle_current:
                    stale_symbols.add(snapshot.symbol)
        rows: list[tuple[str, str, str, bool]] = []
        for symbol in symbols:
            snapshot = snapshots.get(symbol)
            rows.append(
                (
                    symbol,
                    snapshot.last if snapshot is not None else "—",
                    (
                        f"{_signed(snapshot.change_pct, decimal_places=2)}%"
                        if snapshot is not None
                        and snapshot.change_pct is not None
                        else "—"
                    ),
                    symbol in stale_symbols,
                )
            )
        self._watchlist_model.set_rows(rows)

    def _render_summary_and_chart(self, view: Any) -> None:
        identity = view.committed_identity
        snapshot = (
            view.bundle.quote.snapshot
            if view.bundle is not None
            else None
        )
        if identity is None:
            staged = view.staged_identity
            self._symbol_label.setText(
                staged.symbol if staged is not None else "—"
            )
        else:
            self._symbol_label.setText(identity.symbol)
        self._name_label.setText(
            snapshot.name
            if snapshot is not None and snapshot.name
            else self._symbol_label.text()
        )
        self._identity_badge_label.setText(
            f"{_MARKET_LABELS[identity.market]} · "
            f"{_SOURCE_LABELS[identity.source]}"
            if identity is not None
            else "—"
        )
        self._price_label.setText(
            snapshot.last if snapshot is not None else "—"
        )
        if snapshot is None:
            self._change_label.setText("—")
            self._quote_time_label.setText("—")
            _set_property(self._price_label, "quoteDirection", "")
            _set_property(self._change_label, "quoteDirection", "")
        else:
            change = _signed(snapshot.change)
            pct = (
                f"{_signed(snapshot.change_pct, decimal_places=2)}%"
                if snapshot.change_pct is not None
                else "—"
            )
            self._change_label.setText(f"{change}  {pct}")
            self._quote_time_label.setText(
                _format_utc(snapshot.quote_ts_utc_ms)
            )
            direction = (
                "up"
                if snapshot.change is not None
                and Decimal(snapshot.change) > 0
                else "down"
                if snapshot.change is not None
                and Decimal(snapshot.change) < 0
                else ""
            )
            _set_property(
                self._price_label,
                "quoteDirection",
                direction,
            )
            _set_property(
                self._change_label,
                "quoteDirection",
                direction,
            )
        timeframe = (
            identity.display_timeframe if identity is not None else "10m"
        )
        for value, button in self._timeframe_buttons.items():
            button.blockSignals(True)
            button.setChecked(value == timeframe)
            button.blockSignals(False)

        payload = view.render_payload
        frame = (
            payload.display_frame(timeframe)
            if payload is not None
            and identity is not None
            and payload.token.identity == identity
            else None
        )
        if frame is None:
            self._chart.reset()
            self._chart.hide()
            self._chart_state_label.setText(
                "正在加载"
                if view.selection_state is SelectionState.STAGING
                else "暂无行情数据"
            )
            self._chart_state_label.show()
            self._chart_ohlc_label.setText(f"{timeframe} K 线")
            self._chart_data_state.setText("× 不可用")
        else:
            self._chart.show()
            self._chart.set_frame_now(frame, fit_view=True)
            self._chart_state_label.hide()
            newest = frame.bars[0]
            self._chart_ohlc_label.setText(
                f"{timeframe}  O {newest.open:g}  H {newest.high:g}  "
                f"L {newest.low:g}  C {newest.close:g}"
            )
            self._chart_data_state.setText(
                "⚠ 已过期" if not view.bundle_current else "✓ 已冻结"
            )
        stale = (
            "true"
            if snapshot is not None and not view.bundle_current
            else "false"
        )
        _set_property(self._price_label, "stale", stale)
        _set_property(self._change_label, "stale", stale)

    def _render_quote(self, view: Any) -> None:
        quote = view.bundle.quote if view.bundle is not None else None
        snapshot = quote.snapshot if quote is not None else None
        values = self._quote_values
        if snapshot is None:
            for label in values.values():
                label.setText("—")
                _set_property(label, "stale", "false")
            if view.selection_state is SelectionState.AUTH_INVALID:
                values["新鲜度"].setText("× 不可用")
            return
        values["最新价"].setText(snapshot.last)
        values["上一收盘"].setText(snapshot.prev_close or "—")
        values["涨跌额"].setText(_signed(snapshot.change))
        values["涨跌幅"].setText(
            f"{_signed(snapshot.change_pct, decimal_places=2)}%"
            if snapshot.change_pct is not None
            else "—"
        )
        values["币种"].setText(snapshot.currency or "—")
        values["行情源时间"].setText(
            _format_utc(snapshot.quote_ts_utc_ms, seconds=False)
        )
        values["本机接收"].setText(
            _format_utc(snapshot.received_at_utc_ms, seconds=False)
        )
        values["行情模式"].setText(
            "✓ 实时"
            if snapshot.quote_mode == "realtime"
            else "⚠ 延迟"
        )
        freshness = (
            QuoteFreshness.STALE
            if not view.bundle_current
            else quote.freshness
        )
        values["新鲜度"].setText(
            {
                QuoteFreshness.FRESH: "✓ 新鲜",
                QuoteFreshness.SESSION_PAUSED: "✓ 休市保留",
                QuoteFreshness.STALE: "⚠ 已过期",
                QuoteFreshness.UNAVAILABLE: "× 不可用",
            }[freshness]
        )
        stale = (
            "true" if freshness is QuoteFreshness.STALE else "false"
        )
        for name, label in values.items():
            _set_property(
                label,
                "stale",
                stale if name != "新鲜度" else "false",
            )

    def _render_clock(self, view: Any, market: str) -> None:
        payload = view.render_payload
        clock = (
            payload.market_clock
            if payload is not None
            and payload.market_clock.market == market
            else None
        )
        if clock is None:
            for label in self._clock_values.values():
                label.setText("—")
            self._clock_values["状态"].setText("⚠ 状态未知")
            return
        phase = {
            "open": "开市",
            "break": "午休",
            "closed": "闭市",
            "continuous": "连续交易",
            "unknown": "状态未知",
        }[clock.phase]
        if clock.is_half_day:
            phase = f"{phase} / 半日市"
        clock_icon = "⚠" if clock.phase == "unknown" else "✓"
        self._clock_values["状态"].setText(f"{clock_icon} {phase}")
        try:
            timezone = ZoneInfo(clock.timezone_name)
            local = datetime.fromtimestamp(
                clock.as_of_utc_ms / 1000,
                tz=UTC,
            ).astimezone(timezone)
            local_text = local.strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            local_text = "—"
        self._clock_values["当地时间"].setText(local_text)
        self._clock_values["下一变化"].setText(
            _format_utc(clock.next_change_utc_ms, seconds=False)
        )
        self._clock_values["时区"].setText(clock.timezone_name)

    def _render_kline_evidence(self, view: Any) -> None:
        bundle = view.bundle
        evidence_by_timeframe = {
            "10m": bundle.ten_minute if bundle is not None else None,
            "1h": bundle.one_hour if bundle is not None else None,
            "4h": bundle.four_hour if bundle is not None else None,
        }
        for timeframe, label in self._kline_rows.items():
            evidence = evidence_by_timeframe[timeframe]
            if evidence is None:
                label.setText(f"{timeframe}  —  —  × 不可用")
                _set_property(label, "stateTone", "error")
                continue
            state = (
                EvidenceState.STALE
                if not view.bundle_current
                and evidence.state is EvidenceState.READY
                else evidence.state
            )
            tone = (
                "ready"
                if state is EvidenceState.READY
                else "warning"
                if state in {
                    EvidenceState.INSUFFICIENT,
                    EvidenceState.STALE,
                }
                else "error"
            )
            label.setText(
                f"{timeframe}  {evidence.closed_bar_count}/"
                f"{evidence.required_closed_bars}  "
                f"{_format_utc(evidence.latest_closed_ts_utc_ms, seconds=False)}  "
                f"{_STATUS_ICONS[tone]} {_EVIDENCE_LABELS[state]}"
            )
            _set_property(label, "stateTone", tone)

    def _render_analysis(self, view: Any) -> None:
        for values in (
            self._diagnosis_values,
            self._decision_values,
        ):
            for label in values.values():
                label.setText("—")
        bundle = view.bundle
        if bundle is not None:
            for timeframe, evidence in (
                ("10m", bundle.ten_minute),
                ("1h", bundle.one_hour),
                ("4h", bundle.four_hour),
            ):
                if evidence is None:
                    evidence_text = "× 不可用"
                else:
                    tone = (
                        "ready"
                        if evidence.state is EvidenceState.READY
                        else "warning"
                        if evidence.state in {
                            EvidenceState.INSUFFICIENT,
                            EvidenceState.STALE,
                        }
                        else "error"
                    )
                    evidence_text = (
                        f"{_STATUS_ICONS[tone]} "
                        f"{_EVIDENCE_LABELS[evidence.state]}"
                    )
                self._diagnosis_values[timeframe].setText(evidence_text)
        result = view.analysis_result
        if result is None:
            if view.analysis_state is AnalysisState.RUNNING:
                self._diagnosis_values["周期位置"].setText(
                    self._bridge.analysis_phase or "市场诊断"
                )
            elif view.analysis_state is AnalysisState.FAILED:
                self._diagnosis_values["周期位置"].setText(
                    _ANALYSIS_STAGE_LABELS.get(
                        view.analysis_failure_stage,
                        "两阶段分析",
                    )
                )
                self._decision_values["终局"].setText("失败")
                self._decision_values["理由"].setText(
                    _ANALYSIS_FAILURE_LABELS.get(
                        view.analysis_failure,
                        "分析失败",
                    )
                )
            return
        if result.state == "failed":
            self._decision_values["理由"].setText(
                " / ".join(
                    item
                    for item in (
                        result.error_stage,
                        result.error_category,
                    )
                    if item
                )
                or "分析失败"
            )
            return
        self._diagnosis_values["周期位置"].setText(
            result.cycle_position or "—"
        )
        self._diagnosis_values["方向"].setText(result.direction or "—")
        self._diagnosis_values["判断置信度"].setText(
            f"{result.diagnosis_confidence}%"
            if result.diagnosis_confidence is not None
            else "—"
        )
        self._decision_values["订单类型"].setText(
            result.order_type or "—"
        )
        self._decision_values["方向"].setText(
            result.order_direction or "—"
        )
        self._decision_values["交易置信度"].setText(
            f"{result.trade_confidence}%"
            if result.trade_confidence is not None
            else "—"
        )
        self._decision_values["入场"].setText(result.entry_price or "—")
        self._decision_values["止损"].setText(result.stop_loss or "—")
        self._decision_values["目标"].setText(
            " / ".join(
                value
                for value in (
                    result.take_profit,
                    result.take_profit_2,
                )
                if value
            )
            or "—"
        )
        self._decision_values["终局"].setText(
            {
                "trade": "交易机会",
                "wait": "等待",
                "reject": "拒绝",
            }.get(result.terminal_outcome, result.terminal_outcome or "—")
        )
        self._decision_values["理由"].setText(result.reasoning or "—")

    def _gate_state(self, view: Any) -> tuple[str, str, list[str]]:
        if view.analysis_state is AnalysisState.RUNNING:
            return "分析进行中", "loading", [
                self._bridge.analysis_phase or "市场诊断"
            ]
        if view.selection_state is SelectionState.STAGING:
            target = view.staged_identity
            return "等待 10m 数据", "loading", [
                (
                    f"正在切换到 {target.symbol}"
                    if target is not None
                    else "正在加载目标行情"
                ),
                "旧标的数据仅供查看",
            ]
        if view.selection_state is SelectionState.AUTH_INVALID:
            return "认证失效", "error", ["行情源认证已失效"]
        if view.selection_state is SelectionState.SWITCH_FAILED:
            target = view.failed_identity
            reason = {
                QuoteFailureKind.PERMISSION_DENIED: "行情权限不足",
                QuoteFailureKind.SYMBOL_UNSUPPORTED: "标的不受支持",
                QuoteFailureKind.TRANSPORT_FAILED: "行情连接失败",
                QuoteFailureKind.INVALID_RESPONSE: "行情响应无效",
                QuoteFailureKind.AUTH_FAILED: "行情源认证失效",
            }.get(view.last_market_failure, "目标行情不可用")
            return "切换失败", "error", [
                (
                    f"{target.symbol}：{reason}"
                    if target is not None
                    else reason
                ),
                "仍显示原标的",
            ]
        bundle = view.bundle
        if bundle is None:
            return "等待 10m 数据", "warning", ["暂无完整行情证据"]
        if not view.bundle_current:
            return "行情已过期", "warning", ["刷新失败或证据已经过期"]
        if view.analysis_state is AnalysisState.FAILED:
            stage = _ANALYSIS_STAGE_LABELS.get(
                view.analysis_failure_stage,
                "两阶段分析",
            )
            failure = _ANALYSIS_FAILURE_LABELS.get(
                view.analysis_failure,
                "分析失败",
            )
            return "分析失败", "error", [
                f"{stage}：{failure}",
                "可以重新开始只读分析",
            ]
        if bundle.analysis_state is AnalysisCapabilityState.READY:
            return "可以开始分析", "ready", [
                "报价与 10m K 线身份一致",
                "1h / 4h 缺失不会终止 10m",
            ]
        if (
            bundle.analysis_reason
            is AnalysisGateReason.PRICE_TICK_UNAVAILABLE
        ):
            return "仅展示，价格分析不可用", "warning", [
                "行情源没有提供可追溯的最小跳动",
            ]
        if (
            bundle.analysis_reason
            is AnalysisGateReason.QUOTE_NOT_READY
        ):
            return "行情已过期", "warning", ["报价尚未就绪"]
        return "等待 10m 数据", "warning", ["10m 已收盘 K 线不足"]

    def _render_gate(self, view: Any) -> None:
        conclusion, tone, reasons = self._gate_state(view)
        self._gate_conclusion.setText(
            f"{_STATUS_ICONS[tone]} {conclusion}"
        )
        _set_property(self._gate_conclusion, "stateTone", tone)
        for index, label in enumerate(self._gate_reason_labels):
            label.setText(
                f"• {reasons[index]}" if index < len(reasons) else ""
            )
            label.setVisible(index < len(reasons))
        can_analyse = (
            view.bundle is not None
            and view.bundle_current
            and view.bundle.analysis_allowed
            and view.staged_identity is None
            and view.selection_state is SelectionState.COMMITTED
            and view.analysis_state is not AnalysisState.RUNNING
        )
        self._analysis_button.setEnabled(can_analyse)
        self._analysis_button.setText(
            "分析中"
            if view.analysis_state is AnalysisState.RUNNING
            else "开始分析"
        )

    def _render_status_bar(self, view: Any) -> None:
        identity = view.committed_identity
        if identity is None:
            self._identity_status.setText("—")
            self._generation_status.setText("generation —")
            self._refresh_status.setText("刷新 —")
        else:
            self._identity_status.setText(
                f"{_MARKET_LABELS[identity.market]} / "
                f"{identity.symbol} / {identity.display_timeframe}"
            )
            self._generation_status.setText(
                f"generation {identity.selection_generation}"
            )
            payload = view.render_payload
            self._refresh_status.setText(
                "刷新 "
                + (
                    _format_utc(
                        payload.loaded_at_utc_ms,
                        seconds=False,
                    )
                    if payload is not None
                    else "—"
                )
            )
        self._settings_status.setText(
            "✓ 已保存"
            if view.settings_save_state is SettingsSaveState.SAVED
            else "⚠ 未保存"
        )


class AnalysisHistoryWorkbench(QWidget):
    """同一 Controller 的只读分析历史页。"""

    def __init__(
        self,
        *,
        bridge: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("multiMarketWorkbench")
        self.setStyleSheet(_WORKBENCH_QSS)
        self._bridge = bridge
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        heading = QLabel("分析记录")
        heading.setProperty("role", "pageTitle")
        layout.addWidget(heading)
        self._content = QLabel("暂无分析记录")
        self._content.setAlignment(
            Qt.AlignmentFlag.AlignTop
            | Qt.AlignmentFlag.AlignLeft
        )
        self._content.setWordWrap(True)
        scroll = QScrollArea()
        scroll.setObjectName("marketReasonScroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._content)
        layout.addWidget(scroll, 1)
        bridge.state_changed.connect(self.render)
        self.render()

    def render(self) -> None:
        history = self._bridge.snapshot().analysis_history
        if not history:
            self._content.setText("暂无分析记录")
            return
        lines = []
        for item in reversed(history):
            outcome = item.terminal_outcome or item.error_category or "失败"
            lines.append(
                f"{item.market}  {item.symbol}  10m  {outcome}"
            )
        self._content.setText("\n\n".join(lines))
