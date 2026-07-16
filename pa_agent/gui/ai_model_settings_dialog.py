"""可保存、验证并一键切换的 AI 模型档案设置界面。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import Qt, QThread, QUrl
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
    QSpinBox,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from pa_agent.ai.provider_capabilities import (
    ProviderCapability,
    normalise_reasoning_effort,
    resolve_provider_capability,
)
from pa_agent.ai.provider_probe import ProbeStatus, ProviderProbeResult, probe_ai_provider
from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import AIProviderSettings, Settings, save_settings
from pa_agent.util.threading import CancelToken

_API_KEY_HELP_URL = "https://my.feishu.cn/wiki/CUV1wUKWxiQGhekQdRvcZQQ2ncf"
_AGENT_TUTORIAL_URL = (
    "https://my.feishu.cn/wiki/BEdFwGJhaiATbukuD2HccSXCnrb?from=from_copylink"
)
logger = logging.getLogger(__name__)

_ADAPTER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("auto", "自动识别（兼容旧配置）"),
    ("deepseek", "DeepSeek 原生"),
    ("openai", "OpenAI 原生"),
    ("anthropic_adaptive", "Anthropic Adaptive（兼容网关）"),
    ("anthropic_adaptive_always", "Anthropic 固定 Thinking（兼容网关）"),
    ("anthropic_budget", "Anthropic Budget（兼容网关）"),
    ("minimax_m3", "MiniMax M3"),
    ("minimax_m2", "MiniMax M2.x（固定 Thinking）"),
    ("mimo", "MiMo"),
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
}


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
        self.result = probe_ai_provider(
            self._provider,
            cancel_token=self._cancel_token,
            timeout_s=20.0,
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

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("例如：DeepSeek 深度分析")
        form.addRow("档案名称", self._name_edit)

        self._adapter_combo = QComboBox()
        for adapter_id, label in _ADAPTER_OPTIONS:
            self._adapter_combo.addItem(label, adapter_id)
        form.addRow("协议适配", self._adapter_combo)

        self._model_edit = QLineEdit()
        self._model_edit.setPlaceholderText("模型 ID，例如 deepseek-chat / gpt-5")
        form.addRow("模型 ID", self._model_edit)

        self._base_url_edit = QLineEdit()
        self._base_url_edit.setPlaceholderText("例如 https://api.deepseek.com")
        form.addRow("Base URL", self._base_url_edit)

        api_row = QHBoxLayout()
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("输入 API Key")
        self._api_key_edit.editingFinished.connect(
            lambda: self._show_key_btn.setChecked(False)
        )
        api_row.addWidget(self._api_key_edit, stretch=1)
        self._show_key_btn = QPushButton("显示")
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.setFixedWidth(68)
        self._show_key_btn.toggled.connect(self._toggle_api_key_visibility)
        api_row.addWidget(self._show_key_btn)
        form.addRow("API Key", api_row)

        self._context_window_spin = QSpinBox()
        self._context_window_spin.setRange(1_024, 100_000_000)
        self._context_window_spin.setSingleStep(1_000)
        self._context_window_spin.setGroupSeparatorShown(True)
        self._context_window_spin.setSuffix(" tokens")
        self._context_window_spin.setToolTip(
            "按该模型官方文档填写；此值用于上下文用量和预警，不会自动猜测。"
        )
        form.addRow("上下文窗口", self._context_window_spin)

        self._thinking_check = QCheckBox("启用模型原生 Thinking / 深度推理")
        form.addRow("Thinking", self._thinking_check)

        self._effort_combo = QComboBox()
        form.addRow("推理强度", self._effort_combo)
        layout.addWidget(profile_group)

        capability_group = QGroupBox("当前适配能力")
        capability_form = QFormLayout(capability_group)
        self._transport_value = self._capability_label()
        self._thinking_value = self._capability_label()
        self._effort_value = self._capability_label()
        self._speed_value = self._capability_label(word_wrap=True)
        capability_form.addRow("请求方式", self._transport_value)
        capability_form.addRow("Thinking", self._thinking_value)
        capability_form.addRow("强度档位", self._effort_value)
        capability_form.addRow("速度说明", self._speed_value)
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

        for edit in (self._name_edit, self._model_edit, self._base_url_edit, self._api_key_edit):
            edit.textEdited.connect(self._on_form_changed)
        self._adapter_combo.currentIndexChanged.connect(self._on_form_changed)
        self._context_window_spin.valueChanged.connect(self._on_form_changed)
        self._thinking_check.toggled.connect(self._on_form_changed)
        self._effort_combo.currentIndexChanged.connect(self._on_form_changed)
        return pane

    @staticmethod
    def _capability_label(*, word_wrap: bool = False) -> QLabel:
        label = QLabel("—")
        label.setObjectName("capabilityValue")
        label.setWordWrap(word_wrap)
        return label

    @staticmethod
    def _probe_label() -> QLabel:
        label = QLabel("未测试")
        label.setObjectName("probeIdle")
        return label

    # ── 档案列表与表单 ───────────────────────────────────────────────────────

    def _populate_profile_list(self, *, select_id: str = "") -> None:
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
        self._loading = False

    def _load_profile(self, profile_id: str) -> None:
        profile = self._working.ai_profiles[profile_id]
        self._loading = True
        self._show_key_btn.setChecked(False)
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._show_key_btn.setText("显示")
        self._selected_profile_id = profile_id
        self._name_edit.setText(profile.display_name)
        self._model_edit.setText(profile.provider.model)
        self._base_url_edit.setText(profile.provider.base_url)
        self._api_key_edit.setText(profile.provider.api_key)
        self._context_window_spin.setValue(profile.provider.context_window)
        adapter_index = self._adapter_combo.findData(profile.provider.adapter_id)
        self._adapter_combo.setCurrentIndex(max(0, adapter_index))
        self._thinking_check.setChecked(profile.provider.thinking)
        self._refresh_capability_controls(preferred_effort=profile.provider.reasoning_effort)
        self._show_verification(profile.verification)
        self._dirty = False
        self._loading = False
        self._refresh_action_state()

    def _on_profile_item_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        if self._loading or current is None:
            return
        if self._dirty and previous is not None:
            answer = QMessageBox.question(
                self,
                "放弃未测试修改？",
                "当前字段尚未完成测试，因此不会保存。是否放弃这些修改并切换档案？",
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
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "放弃未测试修改？",
            "当前字段尚未完成测试，因此不会保存。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _new_profile(self) -> None:
        if not self._confirm_discard_dirty():
            return
        if self._dirty:
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
        if self._dirty:
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
        self._dirty = True
        self._refresh_capability_controls()
        self._show_unverified("配置已修改，需要重新测试。")
        self._refresh_action_state()

    def _preview_provider(self) -> AIProviderSettings:
        return AIProviderSettings(
            model=self._model_edit.text().strip(),
            base_url=self._base_url_edit.text().strip(),
            api_key=self._api_key_edit.text().strip(),
            adapter_id=str(self._adapter_combo.currentData() or "auto"),
            thinking=self._thinking_check.isChecked(),
            reasoning_effort=self._effort_combo.currentData() or "high",
            context_window=self._context_window_spin.value(),
        )

    def _refresh_capability_controls(self, *, preferred_effort: str | None = None) -> None:
        try:
            capability = resolve_provider_capability(self._preview_provider())
        except ValueError:
            self._transport_value.setText("无法识别")
            self._thinking_value.setText("—")
            self._effort_value.setText("—")
            self._speed_value.setText("—")
            return

        self._transport_value.setText(
            f"{capability.adapter_id} · {_TRANSPORT_LABELS[capability.thinking_transport]}"
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
        self._speed_value.setText(self._speed_explanation(capability))

        self._loading = True
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
        self._loading = False

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
        if not provider.api_key:
            raise ValueError("请填写 API Key。")
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
        except ValueError as exc:
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
        if worker is None or worker.result is None:
            self._set_probe_running(False)
            self._probe_worker = None
            self._probe_cancel_token = None
            self._probe_message.setText("连接测试未返回结果，请重试。")
            return
        self._on_probe_finished(worker.result)

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

        # 活动档案的成功修改必须由 MainWindow 与客户端一起原子提交。
        if result.verification_passed and (
            profile_id == self._working.active_ai_profile_id
        ):
            self._runtime_candidate = self._working.model_copy(deep=True)
            self._runtime_refresh_required = True
            self._activation_requested_id = profile_id
            self._dirty = False
            self._show_verification(verification)
            self._probe_message.setText("测试通过，正在应用当前档案…")
            self.accept()
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
        self._working = self._settings.model_copy(deep=True)
        self._persisted_profile_ids = set(self._settings.ai_profiles)
        self._persisted_changes = True
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
        profile = self._settings.ai_profiles.get(self._selected_profile_id)
        if profile is None or not profile.verification.is_current_for(profile.provider):
            QMessageBox.information(self, "请先测试", "该档案尚未通过当前配置的真实连接测试。")
            return
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
            self._base_url_edit,
            self._api_key_edit,
            self._show_key_btn,
            self._context_window_spin,
            self._thinking_check,
            self._effort_combo,
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
            self._show_unverified("配置未测试；旧档案仍可运行，但新切换必须先测试通过。")
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

    def focus_api_key_field(self) -> None:
        self._api_key_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._api_key_edit.selectAll()

    def _toggle_api_key_visibility(self, checked: bool) -> None:
        self._api_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self._show_key_btn.setText("隐藏" if checked else "显示")

    def reject(self) -> None:
        if self._probe_worker is not None and self._probe_worker.isRunning():
            QMessageBox.information(self, "测试进行中", "请先取消测试，或等待测试完成。")
            return
        if not self._confirm_discard_dirty():
            return
        super().reject()
