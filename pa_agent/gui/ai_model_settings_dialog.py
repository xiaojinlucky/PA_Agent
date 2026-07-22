"""可保存、验证并一键切换的 AI 模型档案设置界面。"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import Qt, QThread, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from pa_agent.ai.provider_capabilities import (
    ProviderCapability,
    fixed_model_context_window,
    normalise_reasoning_effort,
    resolve_provider_capability,
)
from pa_agent.ai.provider_model_catalog import (
    ModelCatalogEntry,
    ModelCatalogError,
    builtin_provider_model_catalog,
    canonicalize_model_id,
    fetch_provider_model_catalog,
    merge_model_catalogs,
)
from pa_agent.ai.provider_probe import ProbeStatus, ProviderProbeResult, probe_ai_provider
from pa_agent.ai.provider_registry import (
    get_provider_preset,
    preset_runtime_defaults,
    provider_auth_configured,
    resolve_provider_runtime_settings,
)
from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import (
    AIProviderSettings,
    Settings,
    SettingsConflictError,
    provider_config_fingerprint,
    save_settings,
)
from pa_agent.util.threading import CancelToken

_API_KEY_HELP_URL = "https://my.feishu.cn/wiki/CUV1wUKWxiQGhekQdRvcZQQ2ncf"
_AGENT_TUTORIAL_URL = (
    "https://my.feishu.cn/wiki/BEdFwGJhaiATbukuD2HccSXCnrb?from=from_copylink"
)
logger = logging.getLogger(__name__)

_ADAPTER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("codex_subscription", "订阅登录｜Codex（ChatGPT）"),
    ("deepseek", "API Key｜DeepSeek"),
    ("kimi", "API Key｜Kimi"),
    ("auto", "兼容模式｜自动识别旧配置"),
    ("openai", "兼容模式｜OpenAI 原生 API"),
    ("anthropic_adaptive", "Anthropic Adaptive（兼容网关）"),
    ("anthropic_adaptive_always", "Anthropic 固定 Thinking（兼容网关）"),
    ("anthropic_budget", "Anthropic Budget（兼容网关）"),
    ("minimax_m3", "MiniMax M3"),
    ("minimax_m2", "MiniMax M2.x（固定 Thinking）"),
    ("mimo", "小米 MiMo API"),
    ("cursor_agent", "Cursor Agent SDK"),
    ("generic_openai_compatible", "通用 OpenAI 兼容（无 Thinking 参数）"),
    ("generic_reasoning_compatible", "通用 OpenAI 兼容（reasoning_effort）"),
)

_TRANSPORT_LABELS = {
    "none": "无独立 Thinking 参数",
    "reasoning_effort": "reasoning_effort",
    "deepseek_toggle": "DeepSeek thinking + reasoning_effort",
    "anthropic_adaptive": "Anthropic adaptive thinking",
    "anthropic_budget": "Anthropic thinking budget",
    "minimax_adaptive": "MiniMax thinking.type",
    "mimo_toggle": "MiMo thinking.type",
    "kimi_toggle": "Kimi thinking.type",
    "kimi_preserved": "Kimi 固定保留式思考",
}


class _EditableModelCombo(QComboBox):
    """默认只能从目录选择；用户明确开启高级模式后才可手输。"""

    def __init__(self) -> None:
        super().__init__()
        self.setEditable(False)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

    def _index_for_model_id(self, value: str) -> int:
        wanted = str(value or "").strip().casefold()
        for index in range(self.count()):
            model_id = str(
                self.itemData(index, Qt.ItemDataRole.UserRole) or ""
            ).strip()
            if model_id.casefold() == wanted:
                return index
        return -1

    def text(self) -> str:
        if self.isEditable():
            line_edit = self.lineEdit()
            visible_text = line_edit.text() if line_edit is not None else self.currentText()
            current_index = self.currentIndex()
            if (
                current_index >= 0
                and visible_text == self.itemText(current_index)
            ):
                model_id = self.itemData(
                    current_index,
                    Qt.ItemDataRole.UserRole,
                )
                if model_id is not None:
                    return str(model_id)
            return visible_text
        model_id = self.currentData(Qt.ItemDataRole.UserRole)
        return str(model_id if model_id is not None else self.currentText())

    def setText(self, value: str) -> None:
        model_id = str(value or "").strip()
        index = self._index_for_model_id(model_id)
        if index >= 0:
            self.setCurrentIndex(index)
            return
        if not self.isEditable():
            self.setEditable(True)
        self.setEditText(model_id)

    def setPlaceholderText(self, value: str) -> None:
        super().setPlaceholderText(value)
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText(value)

    def set_manual_entry_enabled(self, enabled: bool) -> None:
        current_model = self.text()
        if enabled:
            if not self.isEditable():
                self.setEditable(True)
            self.setEditText(current_model)
            return
        index = self._index_for_model_id(current_model)
        if self.isEditable():
            self.setEditable(False)
        if index >= 0:
            self.setCurrentIndex(index)


class _ProviderProbeWorker(QThread):
    """在后台执行真实连接探测，避免阻塞 Qt 主线程。"""

    def __init__(
        self,
        provider: AIProviderSettings,
        cancel_token: CancelToken,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider.model_copy(deep=True)
        self._cancel_token = cancel_token
        self.result: ProviderProbeResult | None = None

    def run(self) -> None:
        try:
            self.result = probe_ai_provider(
                self._provider,
                cancel_token=self._cancel_token,
                timeout_s=20.0,
            )
        except Exception:  # noqa: BLE001 - Qt 线程异常绝不能逃逸并终止整个进程
            self.result = ProviderProbeResult(
                adapter_id=str(self._provider.adapter_id or "auto"),
                tested_at=datetime.now(UTC).isoformat(timespec="seconds"),
                connection_auth=ProbeStatus.UNKNOWN,
                parameter_acceptance=ProbeStatus.UNKNOWN,
                reasoning_observed=None,
                error_code="probe_internal_error",
                message="连接测试发生内部错误，PA_Agent 已保持运行；请检查日志后重试。",
            )


class _ModelCatalogWorker(QThread):
    """异步拉取模型列表，避免网络请求冻结设置窗口。"""

    def __init__(
        self,
        provider: AIProviderSettings,
        request_signature: tuple[str, str, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider.model_copy(deep=True)
        self.request_signature = request_signature
        self.entries: tuple[ModelCatalogEntry, ...] = ()
        self.error_message = ""

    def run(self) -> None:
        try:
            self.entries = fetch_provider_model_catalog(self._provider, timeout_s=10.0)
        except ModelCatalogError as exc:
            self.error_message = str(exc)
        except Exception:  # noqa: BLE001 - 不把外部异常带入 Qt 事件循环
            self.error_message = "读取模型列表发生内部错误，请稍后重试。"


class _CodexLoginStatusWorker(QThread):
    """后台检查 Codex 登录，避免“检测现有登录”冻结设置窗口。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.status: Any = None

    def run(self) -> None:
        from pa_agent.ai.codex_subscription_client import (
            CodexLoginStatus,
            codex_login_status,
        )

        try:
            self.status = codex_login_status(timeout_s=5.0)
        except Exception:  # noqa: BLE001 - 登录状态异常不能终止 GUI
            self.status = CodexLoginStatus(
                installed=True,
                logged_in=False,
                message="检测 Codex 登录状态失败，请稍后重试。",
            )


class AIModelSettingsDialog(QDialog):
    """管理多个 AI 档案；只有当前配置测试通过后才允许激活。"""

    def __init__(
        self,
        settings: Settings,
        parent: QWidget | None = None,
        *,
        settings_path: Path | None = None,
        activation_allowed: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("aiModelSettingsDialog")
        self.setWindowTitle("AI 模型设置")
        self.setMinimumSize(900, 680)

        self._settings = settings
        self._working = settings.model_copy(deep=True)
        self._persisted_profile_ids = set(settings.ai_profiles)
        self._settings_path = settings_path or SETTINGS_JSON_PATH
        self._activation_allowed = activation_allowed
        self._selected_profile_id = ""
        self._loading = False
        self._dirty = False
        self._persisted_changes = False
        self._runtime_refresh_required = False
        self._activation_requested_id: str | None = None
        self._runtime_candidate: Settings | None = None
        self._probe_worker: _ProviderProbeWorker | None = None
        self._probe_cancel_token: CancelToken | None = None
        self._probe_profile_id = ""
        self._catalog_worker: _ModelCatalogWorker | None = None
        self._codex_login_worker: _CodexLoginStatusWorker | None = None
        self._catalog_entries: tuple[ModelCatalogEntry, ...] = ()
        self._catalog_is_authoritative = False
        self._api_key_from_env = False
        self._runtime_api_key_value = ""
        self._context_window_value = settings.provider.context_window
        self._context_window_source = settings.provider.context_window_source
        self._context_window_model = settings.provider.model

        self._setup_ui()
        self._populate_profile_list(select_id=self._working.active_ai_profile_id)

    @property
    def activation_requested_id(self) -> str | None:
        return self._activation_requested_id

    @property
    def persisted_changes(self) -> bool:
        return self._persisted_changes

    @property
    def runtime_refresh_required(self) -> bool:
        return self._runtime_refresh_required

    @property
    def runtime_candidate(self) -> Settings | None:
        """Candidate settings for an edited active profile, not yet persisted."""
        if self._runtime_candidate is None:
            return None
        return self._runtime_candidate.model_copy(deep=True)

    # ── UI ──────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_profile_pane())
        editor_scroll = QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor_scroll.setWidget(self._build_editor_pane())
        splitter.addWidget(editor_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

        footer = QHBoxLayout()
        self._api_key_help_btn = QPushButton("API Key 帮助")
        self._api_key_help_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(_API_KEY_HELP_URL))
        )
        footer.addWidget(self._api_key_help_btn)
        self._tutorial_btn = QPushButton("使用教程")
        self._tutorial_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(_AGENT_TUTORIAL_URL))
        )
        footer.addWidget(self._tutorial_btn)
        footer.addStretch(1)
        self._test_btn = QPushButton("测试并保存")
        self._test_btn.setObjectName("primaryButton")
        self._test_btn.clicked.connect(self._on_test_or_cancel)
        footer.addWidget(self._test_btn)
        self._activate_btn = QPushButton("激活此档案")
        self._activate_btn.clicked.connect(self._request_activation)
        footer.addWidget(self._activate_btn)
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self.reject)
        footer.addWidget(self._close_btn)
        root.addLayout(footer)

    def _build_profile_pane(self) -> QWidget:
        pane = QFrame()
        pane.setObjectName("profilePane")
        layout = QVBoxLayout(pane)

        title = QLabel("已保存档案")
        title.setObjectName("toolbarTitle")
        layout.addWidget(title)
        self._profile_list = QListWidget()
        self._profile_list.setObjectName("profileList")
        self._profile_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._profile_list.currentItemChanged.connect(self._on_profile_item_changed)
        layout.addWidget(self._profile_list, stretch=1)

        row = QHBoxLayout()
        self._new_btn = QPushButton("新建")
        self._new_btn.clicked.connect(self._new_profile)
        row.addWidget(self._new_btn)
        self._duplicate_btn = QPushButton("复制")
        self._duplicate_btn.clicked.connect(self._duplicate_profile)
        row.addWidget(self._duplicate_btn)
        self._delete_btn = QPushButton("删除")
        self._delete_btn.setObjectName("dangerButton")
        self._delete_btn.clicked.connect(self._delete_profile)
        row.addWidget(self._delete_btn)
        layout.addLayout(row)

        hint = QLabel("当前运行档案和最后一次测试状态会保留到下次启动。")
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return pane

    def _build_editor_pane(self) -> QWidget:
        pane = QFrame()
        pane.setObjectName("editorPane")
        pane.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(pane)

        profile_group = QGroupBox("连接配置")
        form = QFormLayout(profile_group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._connection_form = form

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("例如：DeepSeek 深度分析")
        form.addRow("档案名称", self._name_edit)

        self._adapter_combo = QComboBox()
        for adapter_id, label in _ADAPTER_OPTIONS:
            self._adapter_combo.addItem(label, adapter_id)
        form.addRow("协议适配", self._adapter_combo)

        model_row = QHBoxLayout()
        self._model_edit = _EditableModelCombo()
        self._model_edit.setPlaceholderText("请从目录选择精确模型 ID")
        model_row.addWidget(self._model_edit, stretch=1)
        self._fetch_models_btn = QPushButton("同步并展开")
        self._fetch_models_btn.clicked.connect(self._fetch_model_catalog)
        model_row.addWidget(self._fetch_models_btn)
        form.addRow("模型 ID", model_row)
        self._manual_model_check = QCheckBox(
            "手动填写模型 ID（仅在目录没有该模型时使用）"
        )
        self._manual_model_check.toggled.connect(self._on_manual_model_toggled)
        form.addRow("", self._manual_model_check)
        self._model_catalog_status = QLabel("尚未拉取模型列表")
        self._model_catalog_status.setObjectName("mutedLabel")
        self._model_catalog_status.setWordWrap(True)
        form.addRow("模型目录", self._model_catalog_status)

        self._base_url_edit = QLineEdit()
        self._base_url_edit.setPlaceholderText("例如 https://api.deepseek.com")
        form.addRow("Base URL", self._base_url_edit)

        api_row = QHBoxLayout()
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("输入 API Key")
        api_row.addWidget(self._api_key_edit, stretch=1)
        self._show_key_btn = QPushButton("显示")
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.setFixedWidth(68)
        self._show_key_btn.toggled.connect(self._toggle_api_key_visibility)
        api_row.addWidget(self._show_key_btn)
        form.addRow("API Key", api_row)
        self._api_key_row = api_row

        self._context_window_label = QLabel("尚未确认")
        self._context_window_label.setObjectName("capabilityValue")
        self._context_window_label.setToolTip(
            "由模型和当前接入方式决定。PA 仅展示并用于用量预警，不能手动修改。"
        )
        form.addRow("模型上下文上限", self._context_window_label)

        self._thinking_check = QCheckBox("启用模型原生 Thinking / 深度推理")
        form.addRow("Thinking", self._thinking_check)

        self._effort_combo = QComboBox()
        form.addRow("推理强度", self._effort_combo)

        self._speed_combo = QComboBox()
        form.addRow("分析速度", self._speed_combo)
        layout.addWidget(profile_group)

        self._subscription_auth_group = QGroupBox("Codex 订阅登录")
        subscription_layout = QVBoxLayout(self._subscription_auth_group)
        self._codex_login_status = QLabel("尚未检查登录状态")
        self._codex_login_status.setWordWrap(True)
        subscription_layout.addWidget(self._codex_login_status)
        subscription_actions = QHBoxLayout()
        self._codex_browser_login_btn = QPushButton("网页登录")
        self._codex_browser_login_btn.setToolTip(
            "启动官方 codex login；浏览器中完成 ChatGPT 订阅登录。"
        )
        self._codex_browser_login_btn.clicked.connect(
            lambda: self._start_codex_login(device_auth=False)
        )
        subscription_actions.addWidget(self._codex_browser_login_btn)
        self._codex_device_login_btn = QPushButton("设备码登录")
        self._codex_device_login_btn.setToolTip(
            "启动官方 codex login --device-auth；按终端中的设备码完成登录。"
        )
        self._codex_device_login_btn.clicked.connect(
            lambda: self._start_codex_login(device_auth=True)
        )
        subscription_actions.addWidget(self._codex_device_login_btn)
        self._codex_refresh_login_btn = QPushButton("检测现有登录")
        self._codex_refresh_login_btn.clicked.connect(
            self._start_codex_login_status_check
        )
        subscription_actions.addWidget(self._codex_refresh_login_btn)
        subscription_actions.addStretch(1)
        subscription_layout.addLayout(subscription_actions)
        subscription_hint = QLabel(
            "PA 只调用官方 Codex CLI，不读取、不复制 OAuth Token；"
            "如果 Codex CLI 已登录，直接点击“检测现有登录”；"
            "否则完成登录后再检测并执行“测试并保存”。"
        )
        subscription_hint.setObjectName("mutedLabel")
        subscription_hint.setWordWrap(True)
        subscription_layout.addWidget(subscription_hint)
        self._subscription_auth_group.hide()
        layout.addWidget(self._subscription_auth_group)

        capability_group = QGroupBox("当前适配能力")
        capability_form = QFormLayout(capability_group)
        self._transport_value = self._capability_label()
        self._thinking_value = self._capability_label()
        self._effort_value = self._capability_label()
        self._speed_value = self._capability_label(word_wrap=True)
        self._context_management_value = self._capability_label(word_wrap=True)
        capability_form.addRow("请求方式", self._transport_value)
        capability_form.addRow("Thinking", self._thinking_value)
        capability_form.addRow("强度档位", self._effort_value)
        capability_form.addRow("速度说明", self._speed_value)
        capability_form.addRow("上下文管理", self._context_management_value)
        layout.addWidget(capability_group)

        probe_group = QGroupBox("连接测试")
        probe_form = QFormLayout(probe_group)
        self._connection_probe_value = self._probe_label()
        self._parameter_probe_value = self._probe_label()
        self._response_probe_value = self._probe_label()
        self._reasoning_probe_value = self._probe_label()
        probe_form.addRow("连接 / 认证", self._connection_probe_value)
        probe_form.addRow("参数接受", self._parameter_probe_value)
        probe_form.addRow("挑战响应", self._response_probe_value)
        probe_form.addRow("观察到 reasoning", self._reasoning_probe_value)
        self._probe_message = QLabel("")
        self._probe_message.setObjectName("mutedLabel")
        self._probe_message.setWordWrap(True)
        probe_form.addRow("结果", self._probe_message)
        layout.addWidget(probe_group)

        layout.addStretch(1)

        for edit in (self._name_edit, self._base_url_edit):
            edit.textEdited.connect(self._on_form_changed)
        self._api_key_edit.textEdited.connect(self._on_api_key_text_edited)
        self._adapter_combo.currentIndexChanged.connect(self._on_adapter_changed)
        self._model_edit.currentTextChanged.connect(self._on_model_text_changed)
        self._thinking_check.toggled.connect(self._on_form_changed)
        self._effort_combo.currentIndexChanged.connect(self._on_form_changed)
        self._speed_combo.currentIndexChanged.connect(self._on_form_changed)
        return pane

    @staticmethod
    def _capability_label(*, word_wrap: bool = False) -> QLabel:
        label = QLabel("—")
        label.setObjectName("capabilityValue")
        label.setWordWrap(word_wrap)
        return label

    def _set_context_window_display(self, value: int | None) -> None:
        if isinstance(value, int) and value > 0:
            self._context_window_label.setText(f"{value:,} tokens（模型固定）")
        else:
            self._context_window_label.setText("尚未确认（模型固定）")

    @staticmethod
    def _probe_label() -> QLabel:
        label = QLabel("未测试")
        label.setObjectName("probeIdle")
        return label

    # ── 档案列表与表单 ───────────────────────────────────────────────────────

    def _populate_profile_list(self, *, select_id: str = "") -> None:
        was_loading = self._loading
        self._loading = True
        self._profile_list.clear()
        selected_row = 0
        for row, (profile_id, profile) in enumerate(self._working.ai_profiles.items()):
            status = profile.verification.status
            status_text = {"passed": "已测试", "failed": "测试失败"}.get(status, "未测试")
            active_text = " · 当前" if profile_id == self._working.active_ai_profile_id else ""
            item = QListWidgetItem(
                f"{profile.display_name}{active_text}\n{profile.provider.model} · {status_text}"
            )
            item.setData(Qt.ItemDataRole.UserRole, profile_id)
            item.setToolTip(profile.provider.base_url or "本地 / SDK 路由")
            self._profile_list.addItem(item)
            if profile_id == select_id:
                selected_row = row
        if self._profile_list.count():
            self._profile_list.setCurrentRow(selected_row)
            item = self._profile_list.currentItem()
            if item is not None:
                self._selected_profile_id = str(item.data(Qt.ItemDataRole.UserRole))
                self._load_profile(self._selected_profile_id)
        self._loading = was_loading

    def _load_profile(self, profile_id: str) -> None:
        profile = self._working.ai_profiles[profile_id]
        self._loading = True
        self._model_edit.clear()
        self._model_edit.addItem(profile.provider.model, profile.provider.model)
        self._show_key_btn.setChecked(False)
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._show_key_btn.setText("显示")
        self._selected_profile_id = profile_id
        self._name_edit.setText(profile.display_name)
        self._model_edit.setText(profile.provider.model)
        self._base_url_edit.setText(profile.provider.base_url)
        runtime_provider = resolve_provider_runtime_settings(profile.provider)
        self._api_key_from_env = (
            not bool(profile.provider.api_key.strip())
            and bool(runtime_provider.api_key.strip())
        )
        self._runtime_api_key_value = (
            runtime_provider.api_key if self._api_key_from_env else ""
        )
        self._api_key_edit.setText(
            self._runtime_api_key_value
            if self._api_key_from_env
            else profile.provider.api_key
        )
        self._context_window_value = profile.provider.context_window
        self._context_window_source = profile.provider.context_window_source
        self._context_window_model = profile.provider.model
        self._set_context_window_display(profile.provider.context_window)
        adapter_index = self._adapter_combo.findData(profile.provider.adapter_id)
        self._adapter_combo.setCurrentIndex(max(0, adapter_index))
        self._thinking_check.setChecked(profile.provider.thinking)
        self._load_builtin_catalog(profile.provider, profile.provider.model)
        self._refresh_capability_controls(
            preferred_effort=profile.provider.reasoning_effort,
            preferred_service_tier=profile.provider.service_tier,
        )
        self._refresh_codex_login_status()
        self._show_verification(profile.verification)
        self._dirty = False
        self._loading = False
        self._refresh_action_state()

    def _load_builtin_catalog(
        self,
        provider: AIProviderSettings,
        current_model: str,
    ) -> None:
        entries = builtin_provider_model_catalog(provider)
        if entries:
            self._apply_model_catalog(
                entries,
                status_text=(
                    f"基础目录 {len(entries)} 个；点击“刷新模型”读取当前账号或"
                    "本机 Codex 的最新列表。"
                ),
                normalize_alias=False,
                mark_dirty=False,
                authoritative=False,
            )
        else:
            was_loading = self._loading
            self._loading = True
            self._catalog_entries = ()
            self._catalog_is_authoritative = False
            self._model_edit.clear()
            if current_model:
                self._model_edit.addItem(current_model, current_model)
                self._model_edit.setText(current_model)
            self._loading = was_loading
            self._model_catalog_status.setText(
                "该兼容适配器没有可靠的基础目录，请按供应商文档手动填写精确模型 ID。"
            )
        exact_model = any(
            entry.model_id.casefold() == current_model.strip().casefold()
            for entry in entries
        )
        manual = bool(current_model.strip()) and not exact_model
        was_loading = self._loading
        self._loading = True
        self._manual_model_check.setChecked(manual)
        self._model_edit.set_manual_entry_enabled(manual)
        self._loading = was_loading

    def _on_profile_item_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        if self._loading or current is None:
            return
        if (self._dirty or self._runtime_candidate is not None) and previous is not None:
            answer = QMessageBox.question(
                self,
                (
                    "放弃已测试但未激活的修改？"
                    if self._runtime_candidate is not None
                    else "放弃未测试修改？"
                ),
                (
                    "当前配置已经测试通过，但尚未激活和保存。"
                    "是否放弃这些修改并切换档案？"
                    if self._runtime_candidate is not None
                    else "当前字段尚未完成测试，因此不会保存。"
                    "是否放弃这些修改并切换档案？"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._loading = True
                self._profile_list.setCurrentItem(previous)
                self._loading = False
                return
            target_id = str(current.data(Qt.ItemDataRole.UserRole))
            self._discard_current_changes()
            self._populate_profile_list(select_id=target_id)
            return
        profile_id = str(current.data(Qt.ItemDataRole.UserRole))
        self._load_profile(profile_id)

    def _confirm_discard_dirty(self) -> bool:
        if not self._dirty and self._runtime_candidate is None:
            return True
        if self._runtime_candidate is not None:
            title = "放弃已测试但未激活的修改？"
            message = (
                "当前配置已经测试通过，但尚未激活和保存。"
                "关闭后这次修改会丢失，是否继续？"
            )
        else:
            title = "放弃未测试修改？"
            message = "当前字段尚未完成测试，因此不会保存。是否继续？"
        answer = QMessageBox.question(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _new_profile(self) -> None:
        if not self._confirm_discard_dirty():
            return
        if self._dirty or self._runtime_candidate is not None:
            self._discard_current_changes()
        profile_id = f"profile-{uuid4().hex[:8]}"
        provider = self._working.provider.model_copy(deep=True)
        provider.api_key = ""
        self._working.save_ai_profile(profile_id, "新模型档案", provider)
        self._populate_profile_list(select_id=profile_id)
        self._name_edit.selectAll()
        self._name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._dirty = True
        self._show_unverified("新档案需要完成真实连接测试后才会保存。")
        self._refresh_action_state()

    def _duplicate_profile(self) -> None:
        if not self._selected_profile_id or not self._confirm_discard_dirty():
            return
        if self._dirty or self._runtime_candidate is not None:
            previous_id = self._selected_profile_id
            self._discard_current_changes()
            if previous_id not in self._working.ai_profiles:
                previous_id = self._working.active_ai_profile_id
            self._populate_profile_list(select_id=previous_id)
        source = self._working.ai_profiles[self._selected_profile_id]
        profile_id = f"profile-{uuid4().hex[:8]}"
        self._working.save_ai_profile(
            profile_id,
            f"{source.display_name} 副本",
            source.provider,
        )
        self._populate_profile_list(select_id=profile_id)
        self._dirty = True
        self._show_unverified("副本不会继承原档案的测试结果，请重新测试。")
        self._refresh_action_state()

    def _discard_current_changes(self) -> None:
        """丢弃当前表单草稿，确保之后提交不会夹带未测试配置。"""
        profile_id = self._selected_profile_id
        if not profile_id:
            return
        if profile_id not in self._persisted_profile_ids:
            self._working.ai_profiles.pop(profile_id, None)
        else:
            persisted = self._settings.ai_profiles.get(profile_id)
            if persisted is not None:
                self._working.ai_profiles[profile_id] = persisted.model_copy(deep=True)
        active = self._working.ai_profiles.get(self._working.active_ai_profile_id)
        if active is not None:
            self._working.provider = active.provider.model_copy(deep=True)
        self._dirty = False
        self._runtime_candidate = None
        self._runtime_refresh_required = False

    def _delete_profile(self) -> None:
        profile_id = self._selected_profile_id
        if not profile_id:
            return
        if len(self._working.ai_profiles) <= 1:
            QMessageBox.information(self, "不能删除", "至少需要保留一个 AI 模型档案。")
            return
        if profile_id == self._working.active_ai_profile_id:
            QMessageBox.information(
                self,
                "不能删除",
                "当前运行中的档案不能删除，请先激活其他档案。",
            )
            return
        profile = self._working.ai_profiles[profile_id]
        answer = QMessageBox.question(
            self,
            "删除模型档案",
            f"确定删除“{profile.display_name}”吗？此操作会立即保存。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        del self._working.ai_profiles[profile_id]
        if not self._commit_working():
            self._working = self._settings.model_copy(deep=True)
        self._populate_profile_list(select_id=self._working.active_ai_profile_id)

    def _on_form_changed(self, *_args: Any) -> None:
        if self._loading:
            return
        self._runtime_candidate = None
        self._runtime_refresh_required = False
        self._dirty = True
        self._refresh_capability_controls()
        self._show_unverified("配置已修改，需要重新测试。")
        self._refresh_action_state()

    def _on_api_key_text_edited(self, _text: str) -> None:
        if self._loading:
            return
        self._api_key_from_env = False
        self._runtime_api_key_value = ""
        self._on_form_changed()

    def _on_model_text_changed(self, _text: str) -> None:
        if self._loading:
            return
        current = self._model_edit.text().strip().casefold()
        if (
            self._manual_model_check.isChecked()
            and any(
                entry.model_id.casefold() == current
                for entry in self._catalog_entries
            )
        ):
            self._loading = True
            self._manual_model_check.setChecked(False)
            self._loading = False
        self._on_form_changed()

    def _on_manual_model_toggled(self, checked: bool) -> None:
        was_loading = self._loading
        self._loading = True
        self._model_edit.set_manual_entry_enabled(checked)
        self._loading = was_loading
        if was_loading:
            return
        current = self._model_edit.text().strip()
        if (
            not checked
            and self._catalog_entries
            and not any(
                entry.model_id.casefold() == current.casefold()
                for entry in self._catalog_entries
            )
        ):
            was_loading = self._loading
            self._loading = True
            self._model_edit.setText(self._catalog_entries[0].model_id)
            self._loading = was_loading
        self._on_form_changed()

    def _on_adapter_changed(self, *_args: Any) -> None:
        if self._loading:
            return
        adapter_id = str(self._adapter_combo.currentData() or "auto")
        preset = get_provider_preset(adapter_id)
        # 供应商发生变化时，旧供应商的显式 Key 绝不能串到新供应商。
        # 留空后由运行时按新适配器读取各自的 Quant/env 配置。
        self._show_key_btn.setChecked(False)
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._show_key_btn.setText("显示")
        self._api_key_from_env = False
        self._runtime_api_key_value = ""
        self._api_key_edit.clear()
        if preset is not None:
            model, base_url = preset_runtime_defaults(adapter_id)
            self._loading = True
            self._model_edit.clear()
            self._model_edit.addItem(model, model)
            self._model_edit.setText(model)
            self._base_url_edit.setText(base_url)
            self._loading = False
            preview = self._preview_provider()
            self._load_builtin_catalog(preview, model)
            runtime = resolve_provider_runtime_settings(preview)
            if runtime.api_key.strip():
                self._loading = True
                self._api_key_from_env = True
                self._runtime_api_key_value = runtime.api_key
                self._api_key_edit.setText(runtime.api_key)
                self._loading = False
        elif adapter_id != "auto":
            self._loading = True
            self._model_edit.clear()
            self._base_url_edit.clear()
            self._loading = False
            self._load_builtin_catalog(self._preview_provider(), "")
        else:
            self._load_builtin_catalog(
                self._preview_provider(),
                self._model_edit.text(),
            )
        self._on_form_changed()
        self._refresh_codex_login_status()

    def _preview_provider(self) -> AIProviderSettings:
        raw_key = self._api_key_edit.text().strip()
        api_key = (
            ""
            if self._api_key_from_env and raw_key == self._runtime_api_key_value
            else raw_key
        )
        return AIProviderSettings(
            model=self._model_edit.text().strip(),
            base_url=self._base_url_edit.text().strip(),
            api_key=api_key,
            adapter_id=str(self._adapter_combo.currentData() or "auto"),
            thinking=self._thinking_check.isChecked(),
            reasoning_effort=self._effort_combo.currentData() or "high",
            service_tier=self._speed_combo.currentData() or "default",
            context_window=self._context_window_value,
            context_window_source=self._context_window_source,
        )

    def _current_catalog_entry(self) -> ModelCatalogEntry | None:
        model = self._model_edit.text().strip().casefold()
        return next(
            (
                entry
                for entry in self._catalog_entries
                if entry.model_id.casefold() == model
            ),
            None,
        )

    def _refresh_capability_controls(
        self,
        *,
        preferred_effort: str | None = None,
        preferred_service_tier: str | None = None,
    ) -> None:
        try:
            capability = resolve_provider_capability(self._preview_provider())
        except ValueError:
            self._transport_value.setText("无法识别")
            self._thinking_value.setText("—")
            self._effort_value.setText("—")
            self._speed_value.setText("—")
            return
        entry = self._current_catalog_entry()
        if entry is not None:
            if entry.supports_thinking_on is not None:
                capability = replace(
                    capability,
                    supports_thinking_on=entry.supports_thinking_on,
                )
            if entry.supports_thinking_off is not None:
                capability = replace(
                    capability,
                    supports_thinking_off=entry.supports_thinking_off,
                )
            if entry.supported_efforts:
                capability = replace(
                    capability,
                    supported_efforts=entry.supported_efforts,
                )

        self._transport_value.setText(
            f"{capability.adapter_id} · {_TRANSPORT_LABELS[capability.thinking_transport]}"
        )
        selected_model = self._model_edit.text().strip()
        preserve_persisted_catalog_value = (
            not self._catalog_is_authoritative
            and self._context_window_source == "catalog"
            and self._context_window_value is not None
            and self._context_window_model.casefold() == selected_model.casefold()
        )
        if preserve_persisted_catalog_value:
            context_window = self._context_window_value
            context_window_source = "catalog"
        elif entry is not None and entry.context_window is not None:
            context_window = entry.context_window
            context_window_source = (
                "catalog" if self._catalog_is_authoritative else "builtin"
            )
        else:
            context_window = fixed_model_context_window(self._preview_provider())
            context_window_source = "builtin" if context_window is not None else "unknown"
        if context_window is None:
            self._context_window_value = None
            self._context_window_source = "unknown"
            self._context_window_model = selected_model
            self._set_context_window_display(None)
        else:
            self._context_window_value = context_window
            self._context_window_source = context_window_source
            self._context_window_model = selected_model
            self._set_context_window_display(context_window)
        codex_subscription = capability.client_kind == "codex_cli"
        self._subscription_auth_group.setVisible(codex_subscription)
        self._connection_form.setRowVisible(
            self._base_url_edit,
            not codex_subscription,
        )
        self._connection_form.setRowVisible(
            self._api_key_row,
            not codex_subscription,
        )
        self._base_url_edit.setEnabled(not codex_subscription)
        self._api_key_edit.setEnabled(not codex_subscription)
        self._show_key_btn.setEnabled(
            not codex_subscription and bool(self._api_key_edit.text())
        )
        if codex_subscription:
            self._base_url_edit.setPlaceholderText("无需 Base URL，使用官方 Codex CLI")
            self._api_key_edit.setPlaceholderText("无需 API Key，使用 codex login")
        else:
            self._base_url_edit.setPlaceholderText("例如 https://api.deepseek.com")
            self._api_key_edit.setPlaceholderText(
                "已从 Quant/env 读取；显示后也不会写入档案"
                if self._api_key_from_env
                else "输入 API Key"
            )
        if not capability.supports_thinking_on:
            thinking_text = "该 SDK 没有独立 Thinking 开关"
        elif not capability.supports_thinking_off:
            thinking_text = "模型原生固定开启，不能关闭"
        else:
            thinking_text = "支持开启和关闭"
        self._thinking_value.setText(thinking_text)

        if capability.supported_efforts:
            self._effort_value.setText(" / ".join(capability.supported_efforts))
        else:
            self._effort_value.setText("模型不接受通用强度档位")
        self._speed_value.setText(
            entry.speed_description
            if entry is not None and entry.speed_description
            else self._speed_explanation(capability)
        )
        if capability.client_kind == "codex_cli":
            self._context_management_value.setText(
                "自由追问复用官方 Codex 线程；达到模型阈值时由 Codex 自动 Compact。"
            )
        else:
            self._context_management_value.setText(
                "当前接入不提供 Codex 原生 Compact；PA 展示最近一轮上下文与累计用量。"
            )

        was_loading = self._loading
        self._loading = True
        self._connection_form.setRowVisible(
            self._thinking_check,
            (
                capability.supports_thinking_on
                and capability.supports_thinking_off
            ),
        )
        self._connection_form.setRowVisible(
            self._effort_combo,
            bool(capability.supported_efforts),
        )
        if not capability.supports_thinking_on:
            self._thinking_check.setChecked(False)
            self._thinking_check.setEnabled(False)
        elif not capability.supports_thinking_off:
            self._thinking_check.setChecked(True)
            self._thinking_check.setEnabled(False)
        else:
            self._thinking_check.setEnabled(True)

        selected_effort = preferred_effort or self._effort_combo.currentData() or "high"
        self._effort_combo.clear()
        for effort in capability.supported_efforts:
            self._effort_combo.addItem(effort, effort)
        effort_index = self._effort_combo.findData(selected_effort)
        if effort_index < 0 and self._effort_combo.count():
            normalized = normalise_reasoning_effort(capability, selected_effort)
            effort_index = self._effort_combo.findData(normalized)
        if effort_index < 0 and self._effort_combo.count():
            effort_index = self._effort_combo.findData("high")
        if effort_index < 0 and self._effort_combo.count():
            effort_index = 0
        if effort_index >= 0:
            self._effort_combo.setCurrentIndex(effort_index)
        self._effort_combo.setEnabled(
            bool(capability.supported_efforts) and self._thinking_check.isChecked()
        )
        if not capability.supported_efforts:
            self._effort_combo.addItem("由模型决定", "high")

        selected_tier = (
            preferred_service_tier
            or self._speed_combo.currentData()
            or "default"
        )
        self._speed_combo.clear()
        self._speed_combo.addItem("标准（模型默认）", "default")
        service_tiers = entry.service_tiers if entry is not None else ()
        for tier in service_tiers:
            label = "Fast（更快、增加订阅用量）" if tier == "fast" else tier
            self._speed_combo.addItem(label, tier)
        tier_index = self._speed_combo.findData(selected_tier)
        self._speed_combo.setCurrentIndex(max(0, tier_index))
        self._speed_combo.setEnabled(bool(service_tiers))
        self._connection_form.setRowVisible(
            self._speed_combo,
            bool(service_tiers),
        )
        self._loading = was_loading

    @staticmethod
    def _catalog_signature(
        provider: AIProviderSettings,
    ) -> tuple[str, str, str]:
        runtime = resolve_provider_runtime_settings(provider)
        auth_fingerprint = (
            sha256(runtime.api_key.encode("utf-8")).hexdigest()
            if runtime.api_key
            else ""
        )
        return (
            str(provider.adapter_id or "auto").strip().lower(),
            str(provider.base_url or "").strip().rstrip("/").lower(),
            auth_fingerprint,
        )

    def _fetch_model_catalog(self) -> None:
        if self._catalog_worker is not None and self._catalog_worker.isRunning():
            return
        try:
            provider = self._preview_provider()
            resolve_provider_capability(provider)
        except (RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, "无法读取模型", str(exc))
            return
        signature = self._catalog_signature(provider)
        self._catalog_worker = _ModelCatalogWorker(provider, signature, parent=self)
        self._catalog_worker.finished.connect(self._on_model_catalog_finished)
        self._fetch_models_btn.setEnabled(False)
        self._fetch_models_btn.setText("正在同步…")
        self._model_catalog_status.setText("正在读取当前账号可用模型…")
        self._catalog_worker.start()

    def _on_model_catalog_finished(self) -> None:
        worker = self._catalog_worker
        self._catalog_worker = None
        self._fetch_models_btn.setEnabled(True)
        self._fetch_models_btn.setText("同步并展开")
        if worker is None:
            return
        try:
            current_signature = self._catalog_signature(self._preview_provider())
            if worker.request_signature != current_signature:
                self._model_catalog_status.setText("供应商已切换，已忽略旧模型列表。")
                return
            if worker.error_message:
                self._model_catalog_status.setText(
                    f"刷新失败：{worker.error_message}；"
                    f"已保留现有 {len(self._catalog_entries)} 个模型。"
                )
                return
            base_entries = builtin_provider_model_catalog(
                self._preview_provider()
            )
            merged = merge_model_catalogs(base_entries, worker.entries)
            refreshed_at = datetime.now().astimezone().strftime("%H:%M:%S")
            self._apply_model_catalog(
                merged,
                status_text=(
                    f"已刷新 · {refreshed_at} · 当前接口返回 "
                    f"{len(worker.entries)} 个，可选 {len(merged)} 个。"
                ),
                authoritative=True,
            )
            if self.isVisible() and self._model_edit.count() > 0:
                QTimer.singleShot(0, self._model_edit.showPopup)
        except Exception:  # noqa: BLE001 - Qt 槽异常不得终止应用
            self._model_catalog_status.setText("应用模型列表时发生内部错误，请重试。")
        finally:
            worker.deleteLater()

    def _apply_model_catalog(
        self,
        entries: tuple[ModelCatalogEntry, ...],
        *,
        status_text: str | None = None,
        normalize_alias: bool = True,
        mark_dirty: bool = True,
        authoritative: bool = False,
    ) -> None:
        current = self._model_edit.text().strip()
        before_provider = self._preview_provider()
        self._catalog_entries = entries
        self._catalog_is_authoritative = authoritative
        canonical = current
        if current and normalize_alias:
            try:
                canonical = canonicalize_model_id(current, entries, strict=False)
            except ModelCatalogError:
                canonical = current
        was_loading = self._loading
        self._loading = True
        self._model_edit.clear()
        for entry in entries:
            label = (
                entry.model_id
                if entry.display_name.strip().casefold() == entry.model_id.casefold()
                else f"{entry.display_name} — {entry.model_id}"
            )
            self._model_edit.addItem(label, entry.model_id)
            index = self._model_edit.count() - 1
            self._model_edit.setItemData(
                index,
                f"{entry.display_name}\n模型 ID：{entry.model_id}",
                Qt.ItemDataRole.ToolTipRole,
            )
        if canonical:
            self._model_edit.setText(canonical)
        exact_model = any(
            entry.model_id.casefold() == canonical.casefold()
            for entry in entries
        )
        manual = bool(canonical) and not exact_model
        self._manual_model_check.setChecked(manual)
        self._model_edit.set_manual_entry_enabled(manual)
        self._loading = was_loading
        self._model_catalog_status.setText(
            status_text
            or f"已加载 {len(entries)} 个模型；下拉框显示精确模型 ID。"
        )
        self._refresh_capability_controls()
        after_provider = self._preview_provider()
        if mark_dirty and (
            canonical != current
            or provider_config_fingerprint(after_provider)
            != provider_config_fingerprint(before_provider)
        ):
            self._dirty = True
            if canonical != current:
                message = f"已把“{current}”规范为官方模型 ID“{canonical}”。"
            else:
                message = "模型目录中的能力信息已更新，需要重新测试后才能激活。"
            self._show_unverified(message)
        self._refresh_action_state()

    @staticmethod
    def _speed_explanation(capability: ProviderCapability) -> str:
        if capability.supported_efforts:
            return "较低强度通常减少推理耗时；实际速度仍由模型和服务线路决定。"
        if capability.adapter_id == "minimax_m3":
            return "M3 的 Thinking 可切换；高速能力应通过提供商公布的模型 ID 选择。"
        if capability.adapter_id == "minimax_m2":
            return "M2.x Thinking 固定开启；如需高速线路，请选择提供商对应的 highspeed 模型。"
        return "没有可靠的通用速度参数；请通过模型 ID 选择 Flash / Highspeed 等原生版本。"

    # ── 测试、保存与激活 ────────────────────────────────────────────────────

    def _provider_from_form(self) -> AIProviderSettings:
        display_name = self._name_edit.text().strip()
        model = self._model_edit.text().strip()
        base_url = self._base_url_edit.text().strip()
        if not display_name:
            raise ValueError("请填写档案名称。")
        if not model:
            raise ValueError("请填写模型 ID。")
        if model.startswith(("http://", "https://")) and not base_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("模型 ID 与 Base URL 似乎填反了。")

        provider = self._preview_provider()
        capability = resolve_provider_capability(provider)
        entries = self._catalog_entries
        if capability.client_kind == "codex_cli" and not entries:
            entries = fetch_provider_model_catalog(provider, timeout_s=5.0)
            self._catalog_entries = entries
            self._catalog_is_authoritative = True
        canonical_model = canonicalize_model_id(
            model,
            entries,
            strict=(
                bool(entries)
                and not self._manual_model_check.isChecked()
            ),
        )
        if canonical_model != model:
            self._loading = True
            self._model_edit.setText(canonical_model)
            self._loading = False
        provider.model = canonical_model
        entry = next(
            (
                item
                for item in entries
                if item.model_id.casefold() == canonical_model.casefold()
            ),
            None,
        )
        if entry is not None and entry.context_window is not None:
            provider.context_window = entry.context_window
            provider.context_window_source = (
                "catalog" if self._catalog_is_authoritative else "builtin"
            )
        if (
            provider.service_tier != "default"
            and (
                entry is None
                or provider.service_tier not in entry.service_tiers
            )
        ):
            raise ValueError("当前模型不支持所选分析速度，请重新选择。")
        from pa_agent.ai.cursor_connector import is_openclaw_cs_model
        from pa_agent.ai.qclaw_connector import is_openclaw_model
        from pa_agent.ai.workbuddy_connector import is_openclaw_wb_model

        if provider.adapter_id == "auto" and (
            is_openclaw_model(model)
            or is_openclaw_cs_model(model)
            or is_openclaw_wb_model(model)
        ):
            temporary = Settings(provider=provider)
            if is_openclaw_wb_model(model):
                from pa_agent.ai.workbuddy_connector import apply_workbuddy_provider_to_settings

                error = apply_workbuddy_provider_to_settings(temporary, preferred_model=model)
            elif is_openclaw_cs_model(model):
                from pa_agent.ai.cursor_connector import apply_cursor_provider_to_settings

                error = apply_cursor_provider_to_settings(temporary, preferred_model=model)
                temporary.provider.adapter_id = "cursor_agent"
            else:
                from pa_agent.ai.qclaw_connector import apply_qclaw_provider_to_settings

                error = apply_qclaw_provider_to_settings(temporary, preferred_model=model)
            if error:
                raise ValueError(error)
            provider = temporary.provider.model_copy(deep=True)

        capability = resolve_provider_capability(provider)
        if capability.client_kind == "openai_chat" and not provider.base_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("当前适配器需要有效的 Base URL。")
        # Codex 登录由随后启动的后台真实探测检查，不能在按钮槽函数里
        # 同步调用外部 CLI；否则“测试并保存”会冻结 Qt 主线程。
        if (
            capability.client_kind != "codex_cli"
            and not provider_auth_configured(provider)
        ):
            raise ValueError("请填写 API Key，或在 Quant/env 配置该供应商的 Key。")
        return provider

    def _store_current_in_working(self) -> AIProviderSettings:
        profile_id = self._selected_profile_id
        if not profile_id:
            raise ValueError("请先选择模型档案。")
        provider = self._provider_from_form()
        existing = self._working.ai_profiles[profile_id]
        if (
            self._dirty
            or existing.provider != provider
            or existing.display_name != self._name_edit.text().strip()
        ):
            self._working.save_ai_profile(
                profile_id,
                self._name_edit.text().strip(),
                provider,
                replace=True,
            )
        return self._working.ai_profiles[profile_id].provider.model_copy(deep=True)

    def _on_test_or_cancel(self) -> None:
        if self._probe_worker is not None and self._probe_worker.isRunning():
            if self._probe_cancel_token is not None:
                self._probe_cancel_token.set()
            self._test_btn.setText("正在取消…")
            self._test_btn.setEnabled(False)
            return
        try:
            provider = self._store_current_in_working()
        except (ModelCatalogError, RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, "模型配置有误", str(exc))
            return

        self._probe_profile_id = self._selected_profile_id
        self._probe_cancel_token = CancelToken()
        self._probe_worker = _ProviderProbeWorker(
            provider,
            self._probe_cancel_token,
            parent=self,
        )
        self._probe_worker.finished.connect(self._on_probe_worker_finished)
        self._set_probe_running(True)
        self._show_probe_running()
        self._probe_worker.start()

    def _on_probe_worker_finished(self) -> None:
        worker = self._probe_worker
        if worker is None:
            return
        try:
            if worker.result is None:
                self._set_probe_running(False)
                self._probe_worker = None
                self._probe_cancel_token = None
                self._probe_message.setText("连接测试未返回结果，请重试。")
                return
            self._on_probe_finished(worker.result)
        except Exception:  # noqa: BLE001 - Qt 槽异常不得终止应用
            logger.exception("AI provider probe result handling failed")
            self._set_probe_running(False)
            self._probe_worker = None
            self._probe_cancel_token = None
            self._probe_message.setText(
                "处理连接测试结果时发生内部错误；档案没有保存，PA_Agent 已保持运行。"
            )
        finally:
            worker.deleteLater()

    def _on_probe_finished(self, result: ProviderProbeResult) -> None:
        profile_id = self._probe_profile_id
        self._set_probe_running(False)
        self._probe_worker = None
        self._probe_cancel_token = None
        if not profile_id or profile_id not in self._working.ai_profiles:
            return
        if result.cancelled:
            self._probe_message.setText("测试已取消，档案没有保存。")
            return

        profile = self._working.ai_profiles[profile_id]
        if profile_id == self._working.active_ai_profile_id:
            self._working.provider = profile.provider.model_copy(deep=True)
        observations = {
            "connection_unknown": result.connection_auth is ProbeStatus.UNKNOWN,
            "parameter_unknown": result.parameter_acceptance is ProbeStatus.UNKNOWN,
            "response_unknown": result.response_observed is None,
            "challenge_unknown": result.challenge_matched is None,
        }
        if result.reasoning_observed is not None:
            observations["reasoning_observed"] = result.reasoning_observed
        verification = self._working.mark_ai_profile_verification(
            profile_id,
            passed=result.verification_passed,
            tested_at=result.tested_at,
            adapter_id=result.adapter_id,
            checks=result.verification_checks(),
            observations=observations,
            error=result.message,
        )

        # 当前活动档案没有改变连接参数时，只需保存新的验证状态；
        # 设置窗口保持打开，避免用户误以为 PA_Agent 退出后又重启。
        if result.verification_passed and (
            profile_id == self._working.active_ai_profile_id
        ):
            runtime_profile = self._settings.ai_profiles.get(profile_id)
            same_runtime_config = (
                runtime_profile is not None
                and provider_config_fingerprint(runtime_profile.provider)
                == provider_config_fingerprint(profile.provider)
            )
            if same_runtime_config:
                if not self._commit_working():
                    self._dirty = True
                    self._refresh_action_state()
                    return
                self._dirty = False
                self._populate_profile_list(select_id=profile_id)
                if profile.provider.adapter_id == "codex_subscription":
                    message = (
                        "测试通过并已保存。当前档案继续使用 Codex 订阅登录，"
                        "不需要 API Key。"
                    )
                    self._set_probe_label(
                        self._codex_login_status,
                        "真实连接测试已确认 ChatGPT 订阅登录可用。",
                        "passed",
                    )
                    self._codex_refresh_login_btn.setText("已登录")
                else:
                    message = "测试通过并已保存。当前档案已通过真实连接测试。"
                self._probe_message.setText(message)
                self._refresh_action_state()
                return

            # 连接参数已经改变时先保留已验证候选，等待用户明确点击
            # “激活此档案”，再由 MainWindow 原子切换客户端与设置。
            self._runtime_candidate = self._working.model_copy(deep=True)
            self._runtime_refresh_required = False
            self._dirty = False
            self._show_verification(verification)
            self._probe_message.setText(
                "测试通过。请点击“激活此档案”应用更改；窗口不会自动关闭或重开。"
            )
            self._refresh_action_state()
            return

        # 非活动档案保存成功或失败状态；活动档案失败时不覆盖运行配置。
        should_persist = profile_id != self._working.active_ai_profile_id
        if should_persist:
            if not self._commit_working():
                self._dirty = True
                self._refresh_action_state()
                return
            self._dirty = False
            self._populate_profile_list(select_id=profile_id)
        else:
            # 恢复内存中的正式活动档案，但保留失败字段在表单中供用户修正。
            persisted = self._settings.ai_profiles[profile_id]
            self._working.ai_profiles[profile_id] = persisted.model_copy(deep=True)
            self._working.provider = self._settings.provider.model_copy(deep=True)
            self._dirty = True
            self._show_verification(verification)
            self._probe_message.setText(
                f"{result.message} 当前运行档案未被失败配置覆盖，请修正后重试。"
            )
        self._refresh_action_state()

    def _commit_working(self) -> bool:
        candidate = self._settings.model_copy(deep=True)
        candidate.ai_profiles = {
            key: value.model_copy(deep=True)
            for key, value in self._working.ai_profiles.items()
        }
        candidate.active_ai_profile_id = self._working.active_ai_profile_id
        active = candidate.ai_profiles[candidate.active_ai_profile_id]
        candidate.provider = active.provider.model_copy(deep=True)
        try:
            save_settings(candidate, self._settings_path)
        except SettingsConflictError:
            QMessageBox.warning(
                self,
                "配置已被其他窗口更新",
                "磁盘上的设置已经变化。为避免覆盖其他模型档案，本次没有保存；"
                "请关闭设置窗口后重新打开。",
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI profile settings save failed (%s)", type(exc).__name__)
            QMessageBox.warning(
                self,
                "保存失败",
                "AI 模型档案没有保存，请检查配置目录是否可写后重试。",
            )
            return False
        self._settings.ai_profiles = {
            key: value.model_copy(deep=True)
            for key, value in candidate.ai_profiles.items()
        }
        self._settings.active_ai_profile_id = candidate.active_ai_profile_id
        self._settings.provider = candidate.provider.model_copy(deep=True)
        self._settings.revision = candidate.revision
        self._working = self._settings.model_copy(deep=True)
        self._persisted_profile_ids = set(self._settings.ai_profiles)
        self._persisted_changes = True
        self._runtime_candidate = None
        self._runtime_refresh_required = False
        return True

    def _request_activation(self) -> None:
        if not self._activation_allowed:
            QMessageBox.information(
                self,
                "暂不能切换",
                "分析进行中不能切换 AI 模型，请等待或停止本轮分析。",
            )
            return
        if self._dirty:
            QMessageBox.information(self, "请先测试", "当前配置已修改，请先点击“测试并保存”。")
            return
        profile = self._working.ai_profiles.get(self._selected_profile_id)
        if profile is None or not profile.verification.is_current_for(profile.provider):
            QMessageBox.information(self, "请先测试", "该档案尚未通过当前配置的真实连接测试。")
            return
        if self._selected_profile_id == self._working.active_ai_profile_id:
            persisted = self._settings.ai_profiles.get(self._selected_profile_id)
            if (
                persisted is None
                or provider_config_fingerprint(persisted.provider)
                != provider_config_fingerprint(profile.provider)
            ):
                self._runtime_candidate = self._working.model_copy(deep=True)
                self._runtime_refresh_required = True
        self._activation_requested_id = self._selected_profile_id
        self.accept()

    def _set_probe_running(self, running: bool) -> None:
        for widget in (
            self._profile_list,
            self._new_btn,
            self._duplicate_btn,
            self._delete_btn,
            self._name_edit,
            self._adapter_combo,
            self._model_edit,
            self._manual_model_check,
            self._fetch_models_btn,
            self._base_url_edit,
            self._api_key_edit,
            self._show_key_btn,
            self._context_window_label,
            self._thinking_check,
            self._effort_combo,
            self._speed_combo,
            self._codex_browser_login_btn,
            self._codex_device_login_btn,
            self._codex_refresh_login_btn,
            self._activate_btn,
            self._close_btn,
        ):
            widget.setEnabled(not running)
        self._test_btn.setEnabled(True)
        self._test_btn.setText("取消测试" if running else "测试并保存")

    def _refresh_action_state(self) -> None:
        profile = self._working.ai_profiles.get(self._selected_profile_id)
        can_activate = (
            self._activation_allowed
            and not self._dirty
            and profile is not None
            and profile.verification.is_current_for(profile.provider)
        )
        self._activate_btn.setEnabled(bool(can_activate))
        self._delete_btn.setEnabled(
            bool(profile)
            and len(self._working.ai_profiles) > 1
            and self._selected_profile_id != self._working.active_ai_profile_id
        )

    # ── 测试状态显示 ─────────────────────────────────────────────────────────

    def _show_probe_running(self) -> None:
        self._set_probe_label(self._connection_probe_value, "正在连接…", "unknown")
        self._set_probe_label(self._parameter_probe_value, "等待响应…", "idle")
        self._set_probe_label(self._response_probe_value, "等待响应…", "idle")
        self._set_probe_label(self._reasoning_probe_value, "等待响应…", "idle")
        self._probe_message.setText("正在发起一次最短真实请求；不会保存提示词或响应正文。")

    def _show_unverified(self, message: str) -> None:
        self._set_probe_label(self._connection_probe_value, "未测试", "idle")
        self._set_probe_label(self._parameter_probe_value, "未测试", "idle")
        self._set_probe_label(self._response_probe_value, "未测试", "idle")
        self._set_probe_label(self._reasoning_probe_value, "未观察", "idle")
        self._probe_message.setText(message)

    def _show_verification(self, verification: Any) -> None:
        if verification.status == "untested":
            self._show_unverified("配置未测试；完成真实连接测试后才能激活和运行。")
            return
        checks = verification.checks or {}
        observations = verification.observations or {}
        if observations.get("connection_unknown"):
            self._set_probe_label(self._connection_probe_value, "未确认", "unknown")
        else:
            self._set_probe_bool(
                self._connection_probe_value,
                checks.get("connection_auth"),
            )
        if observations.get("parameter_unknown"):
            self._set_probe_label(self._parameter_probe_value, "未确认", "unknown")
        else:
            self._set_probe_bool(
                self._parameter_probe_value,
                checks.get("parameter_acceptance"),
            )
        if observations.get("response_unknown") or observations.get("challenge_unknown"):
            self._set_probe_label(self._response_probe_value, "未确认", "unknown")
        else:
            self._set_probe_bool(
                self._response_probe_value,
                checks.get("challenge_matched"),
            )
        observed = observations.get("reasoning_observed")
        if observed is True:
            self._set_probe_label(self._reasoning_probe_value, "已观察到", "passed")
        elif observed is False:
            self._set_probe_label(
                self._reasoning_probe_value,
                "未观察到（不影响连接可用）",
                "unknown",
            )
        else:
            self._set_probe_label(self._reasoning_probe_value, "未记录", "idle")
        if verification.status == "passed":
            self._probe_message.setText(f"测试通过 · {verification.tested_at}")
        else:
            self._probe_message.setText(
                f"测试失败 · {verification.error or '未分类错误'} · {verification.tested_at}"
            )

    @staticmethod
    def _set_probe_bool(label: QLabel, value: bool | None) -> None:
        if value is True:
            AIModelSettingsDialog._set_probe_label(label, "通过", "passed")
        elif value is False:
            AIModelSettingsDialog._set_probe_label(label, "失败", "failed")
        else:
            AIModelSettingsDialog._set_probe_label(label, "未确认", "unknown")

    @staticmethod
    def _set_probe_label(label: QLabel, text: str, state: str) -> None:
        object_name = {
            "passed": "probePassed",
            "failed": "probeFailed",
            "unknown": "probeUnknown",
        }.get(state, "probeIdle")
        label.setText(text)
        label.setObjectName(object_name)
        label.style().unpolish(label)
        label.style().polish(label)

    # ── 其他 ────────────────────────────────────────────────────────────────

    def _refresh_codex_login_status(self) -> None:
        """刷新订阅登录区域，但绝不在 Qt 主线程里执行外部 CLI。"""

        try:
            capability = resolve_provider_capability(self._preview_provider())
        except ValueError:
            self._subscription_auth_group.hide()
            return
        if capability.client_kind != "codex_cli":
            self._subscription_auth_group.hide()
            return
        self._subscription_auth_group.show()
        if (
            self._codex_login_worker is not None
            and self._codex_login_worker.isRunning()
        ):
            return
        self._codex_refresh_login_btn.setText("检测现有登录")
        self._codex_refresh_login_btn.setEnabled(True)
        self._set_probe_label(
            self._codex_login_status,
            "尚未检测。点击“检测现有登录”即可检查 ChatGPT 订阅登录；"
            "Codex 订阅不需要 API Key。",
            "unknown",
        )

    def _start_codex_login_status_check(self) -> None:
        """Start an explicit, visible login check requested by the user."""

        if (
            self._codex_login_worker is not None
            and self._codex_login_worker.isRunning()
        ):
            return
        self._codex_refresh_login_btn.setEnabled(False)
        self._codex_refresh_login_btn.setText("正在检测…")
        self._test_btn.setEnabled(False)
        self._set_probe_label(
            self._codex_login_status,
            "正在调用官方 Codex CLI 检测 ChatGPT 订阅登录…",
            "unknown",
        )
        worker = _CodexLoginStatusWorker(parent=self)
        self._codex_login_worker = worker
        worker.finished.connect(self._on_codex_login_status_finished)
        worker.start()

    def _on_codex_login_status_finished(self) -> None:
        worker = self._codex_login_worker
        self._codex_login_worker = None
        if worker is None:
            return
        status = worker.status
        logged_in = bool(status is not None and status.logged_in)
        message = (
            status.message
            if status is not None
            else "检测 Codex 登录状态失败，请稍后重试。"
        )
        self._set_probe_label(
            self._codex_login_status,
            message,
            "passed" if logged_in else "failed",
        )
        self._codex_refresh_login_btn.setText(
            "已登录" if logged_in else "重新检测"
        )
        self._codex_refresh_login_btn.setEnabled(True)
        if self._probe_worker is None:
            self._test_btn.setEnabled(True)
        if logged_in:
            self._test_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        worker.deleteLater()

    def _start_codex_login(self, *, device_auth: bool) -> None:
        from pa_agent.ai.codex_subscription_client import start_codex_login

        try:
            start_codex_login(device_auth=device_auth)
        except RuntimeError as exc:
            QMessageBox.warning(self, "无法启动 Codex 登录", str(exc))
            return
        method = "设备码" if device_auth else "网页"
        self._set_probe_label(
            self._codex_login_status,
            f"已打开官方 Codex {method}登录窗口；完成后点击“检测现有登录”。",
            "unknown",
        )

    def focus_auth_field(self) -> None:
        try:
            capability = resolve_provider_capability(self._preview_provider())
        except ValueError:
            capability = None
        if capability is not None and capability.client_kind == "codex_cli":
            self._start_codex_login_status_check()
            self._codex_refresh_login_btn.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self._api_key_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._api_key_edit.selectAll()

    def focus_api_key_field(self) -> None:
        """兼容旧调用；实际按订阅登录或 API Key 聚焦对应认证入口。"""
        self.focus_auth_field()

    def _toggle_api_key_visibility(self, checked: bool) -> None:
        self._api_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self._show_key_btn.setText("隐藏" if checked else "显示")

    def reject(self) -> None:
        if (
            self._codex_login_worker is not None
            and self._codex_login_worker.isRunning()
        ):
            QMessageBox.information(self, "正在检测登录", "请等待登录状态检测完成后再关闭。")
            return
        if self._catalog_worker is not None and self._catalog_worker.isRunning():
            QMessageBox.information(self, "正在读取模型", "请等待模型列表读取完成后再关闭。")
            return
        if self._probe_worker is not None and self._probe_worker.isRunning():
            QMessageBox.information(self, "测试进行中", "请先取消测试，或等待测试完成。")
            return
        if not self._confirm_discard_dirty():
            return
        super().reject()
