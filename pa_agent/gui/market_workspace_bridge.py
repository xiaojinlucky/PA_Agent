"""多市场页的 Qt 异步桥。

界面只通过本桥发起 Controller 请求。后台线程执行只读行情、设置分区保存
和独立两阶段分析；分析完成只回写 Controller，绝不进入旧交易准备回调。
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from pa_agent.config.settings import (
    SettingsConflictError,
    save_market_workspace_settings,
)
from pa_agent.data.market_workspace import QuoteFailureKind
from pa_agent.data.market_workspace_controller import (
    AnalysisFailureKind,
    AnalysisFailureStage,
    AnalysisRequest,
    MarketDataRequest,
    MarketWorkspaceController,
    SettingsSaveFailureKind,
    WatchlistDataRequest,
    WorkspaceSaveRequest,
    WorkspaceViewState,
)
from pa_agent.data.market_workspace_runtime import (
    MarketWorkspaceRuntime,
    MarketWorkspaceRuntimeError,
)


class MarketWorkspaceQtBridge(QObject):
    """把 Qt 事件转换为 Controller 请求，并拒绝后台自行决定页面状态。"""

    state_changed = pyqtSignal()
    status_changed = pyqtSignal(str)
    analysis_phase_changed = pyqtSignal(str)

    def __init__(
        self,
        *,
        controller: MarketWorkspaceController,
        runtime: MarketWorkspaceRuntime,
        settings_path: Path | None,
        orchestrator_factory: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._runtime = runtime
        self._settings_path = settings_path
        self._orchestrator_factory = orchestrator_factory
        self._controller_lock = threading.RLock()
        self._market_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="market-workspace",
        )
        self._settings_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="market-settings",
        )
        self._analysis_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="market-analysis",
        )
        self._futures_lock = threading.Lock()
        self._futures: set[Future[Any]] = set()
        self._analysis_lock = threading.RLock()
        self._analysis_phases: dict[Any, str] = {}
        self._analysis_cancel_tokens: dict[Any, Any] = {}
        self._closed = False
        self._status = "未加载"

    @property
    def controller(self) -> MarketWorkspaceController:
        return self._controller

    @property
    def status(self) -> str:
        return self._status

    @property
    def analysis_phase(self) -> str:
        with self._controller_lock:
            token = self._controller.view.active_analysis_token
        if token is None:
            return ""
        with self._analysis_lock:
            return self._analysis_phases.get(token, "")

    def snapshot(self) -> WorkspaceViewState:
        with self._controller_lock:
            return self._controller.view

    def _set_status(self, text: str) -> None:
        self._status = str(text)
        with suppress(RuntimeError):
            self.status_changed.emit(self._status)

    def _emit_state(self) -> None:
        with suppress(RuntimeError):
            self.state_changed.emit()

    def _track(self, future: Future[Any]) -> None:
        with self._futures_lock:
            self._futures.add(future)

        def done(completed: Future[Any]) -> None:
            with self._futures_lock:
                self._futures.discard(completed)

        future.add_done_callback(done)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("多市场 Qt bridge 已经关闭")

    @staticmethod
    def _set_cancel_token(token: Any) -> None:
        try:
            token.cancel()
        except AttributeError:
            token.set()

    def _cancel_superseded_analysis(self) -> None:
        with self._controller_lock:
            active = self._controller.view.active_analysis_token
            with self._analysis_lock:
                stale = tuple(
                    cancel_token
                    for token, cancel_token
                    in self._analysis_cancel_tokens.items()
                    if token != active
                )
        for cancel_token in stale:
            self._set_cancel_token(cancel_token)

    def start_initial_load(self) -> None:
        # MainWindow 用 singleShot(0) 延后首读；窗口若在事件触发前关闭，
        # 该回调应静默失效，不能在 Qt 事件循环里抛出未捕获异常。
        if self._closed:
            return
        self._ensure_open()
        with self._controller_lock:
            request = self._controller.begin_initial_load()
        self._cancel_superseded_analysis()
        self._set_status("正在加载")
        self._emit_state()
        self._submit_market_request(request)

    def select(
        self,
        *,
        market: str,
        symbol: str,
        display_timeframe: str,
    ) -> None:
        self._ensure_open()
        with self._controller_lock:
            request = self._controller.begin_selection(
                market=market,
                symbol=symbol,
                display_timeframe=display_timeframe,
            )
        self._cancel_superseded_analysis()
        self._set_status(f"正在切换到 {request.identity.symbol}")
        self._emit_state()
        self._submit_market_request(request)

    def refresh(self) -> bool:
        self._ensure_open()
        try:
            with self._controller_lock:
                request = self._controller.refresh_current()
        except ValueError:
            self._set_status("当前不可刷新")
            self._emit_state()
            return False
        self._cancel_superseded_analysis()
        self._set_status("正在刷新")
        self._emit_state()
        self._submit_market_request(request)
        return True

    def _freeze_request(
        self,
        request: MarketDataRequest,
    ) -> MarketDataRequest:
        with self._controller_lock:
            return self._controller.freeze_analysis_as_of(request)

    def _submit_market_request(self, request: MarketDataRequest) -> None:
        future = self._market_executor.submit(
            self._run_market_request,
            request,
        )
        self._track(future)

    def _run_market_request(self, request: MarketDataRequest) -> None:
        try:
            loaded = self._runtime.load_market_data(
                request,
                freeze_request=self._freeze_request,
            )
        except MarketWorkspaceRuntimeError as exc:
            with self._controller_lock:
                accepted = self._controller.fail_market_data(
                    request,
                    exc.failure,
                )
            self._cancel_superseded_analysis()
            if accepted:
                self._set_status(self._market_failure_text(exc.failure))
                self._emit_state()
            return
        except ValueError:
            # Controller 已经判定为迟到、被替换或快照被篡改；旧结果不能
            # 变成当前页面错误。
            self._emit_state()
            return
        except Exception:
            with self._controller_lock:
                accepted = self._controller.fail_market_data(
                    request,
                    QuoteFailureKind.INVALID_RESPONSE,
                )
            self._cancel_superseded_analysis()
            if accepted:
                self._set_status("行情响应无效")
                self._emit_state()
            return

        try:
            with self._controller_lock:
                result = self._controller.complete_market_data(
                    loaded.request,
                    loaded.bundle,
                    loaded.render_payload,
                )
        except ValueError:
            self._set_status("行情证据无效")
            self._emit_state()
            return
        if not result.accepted:
            self._emit_state()
            return
        self._set_status("行情已更新")
        self._emit_state()
        if result.save_request is not None:
            self._schedule_settings_save(result.save_request)
        self.refresh_watchlist()

    @staticmethod
    def _market_failure_text(failure: QuoteFailureKind) -> str:
        return {
            QuoteFailureKind.AUTH_FAILED: "认证失效",
            QuoteFailureKind.PERMISSION_DENIED: "行情权限不足",
            QuoteFailureKind.SYMBOL_UNSUPPORTED: "标的不受支持",
            QuoteFailureKind.TRANSPORT_FAILED: "行情连接失败",
            QuoteFailureKind.INVALID_RESPONSE: "行情响应无效",
        }[failure]

    def refresh_watchlist(self) -> bool:
        if self._closed:
            return False
        try:
            with self._controller_lock:
                request = self._controller.begin_watchlist_refresh()
        except ValueError:
            return False
        if request is None:
            return False
        future = self._market_executor.submit(
            self._run_watchlist_request,
            request,
        )
        self._track(future)
        return True

    def _run_watchlist_request(
        self,
        request: WatchlistDataRequest,
    ) -> None:
        try:
            result = self._runtime.load_watchlist(request)
        except MarketWorkspaceRuntimeError as exc:
            with self._controller_lock:
                accepted = self._controller.fail_watchlist(
                    request,
                    exc.failure,
                )
            self._cancel_superseded_analysis()
            if accepted:
                self._emit_state()
            return
        except Exception:
            with self._controller_lock:
                accepted = self._controller.fail_watchlist(
                    request,
                    QuoteFailureKind.INVALID_RESPONSE,
                )
            self._cancel_superseded_analysis()
            if accepted:
                self._emit_state()
            return
        with self._controller_lock:
            accepted = self._controller.complete_watchlist(
                request,
                result,
            )
        if accepted:
            self._emit_state()

    def set_watchlist(self, symbols: list[str] | tuple[str, ...]) -> bool:
        self._ensure_open()
        try:
            with self._controller_lock:
                save_request = self._controller.set_watchlist(symbols)
        except ValueError:
            self._set_status("当前不能修改自选")
            self._emit_state()
            return False
        self._emit_state()
        if save_request is not None:
            self._schedule_settings_save(save_request)
        self.refresh_watchlist()
        return True

    def _schedule_settings_save(
        self,
        request: WorkspaceSaveRequest,
    ) -> None:
        future = self._settings_executor.submit(
            self._run_settings_save,
            request,
        )
        self._track(future)

    def _run_settings_save(
        self,
        request: WorkspaceSaveRequest,
    ) -> None:
        try:
            if self._settings_path is None:
                saved = self._controller.settings_snapshot
                saved.market_workspace = request.workspace.model_copy(
                    deep=True
                )
                saved.revision = max(
                    saved.revision,
                    request.baseline.revision,
                ) + 1
            else:
                saved = save_market_workspace_settings(
                    request.baseline,
                    request.workspace,
                    self._settings_path,
                )
        except SettingsConflictError:
            with self._controller_lock:
                self._controller.fail_settings_save(
                    request,
                    SettingsSaveFailureKind.REVISION_CONFLICT,
                )
            self._set_status("本地设置未保存")
            self._emit_state()
            return
        except (OSError, ValueError):
            with self._controller_lock:
                self._controller.fail_settings_save(
                    request,
                    SettingsSaveFailureKind.WRITE_FAILED,
                )
            self._set_status("本地设置未保存")
            self._emit_state()
            return

        with self._controller_lock:
            next_request = self._controller.complete_settings_save(
                request,
                saved,
            )
        self._emit_state()
        if next_request is not None:
            self._schedule_settings_save(next_request)

    def retry_settings_save(self) -> bool:
        self._ensure_open()
        try:
            with self._controller_lock:
                request = self._controller.retry_settings_save()
        except ValueError:
            return False
        self._schedule_settings_save(request)
        self._emit_state()
        return True

    def start_analysis(self) -> bool:
        self._ensure_open()
        try:
            with self._controller_lock:
                request = self._controller.begin_analysis()
        except ValueError:
            self._set_status("当前数据不可分析")
            self._emit_state()
            return False
        if not callable(self._orchestrator_factory):
            with self._controller_lock:
                self._controller.fail_analysis(
                    request,
                    AnalysisFailureKind.WORKER_FAILED,
                    stage=AnalysisFailureStage.SERVICE_INITIALIZATION,
                )
            self._set_status("分析服务不可用")
            self._emit_state()
            return False
        with self._analysis_lock:
            self._analysis_phases[request.token] = "市场诊断"
        self.analysis_phase_changed.emit("市场诊断")
        self._set_status("分析进行中")
        self._emit_state()
        future = self._analysis_executor.submit(
            self._run_analysis,
            request,
        )
        self._track(future)
        return True

    def _run_analysis(self, request: AnalysisRequest) -> None:
        from pa_agent.data.multi_timeframe import (
            render_higher_timeframe_context,
        )
        from pa_agent.util.threading import (
            CancelToken,
            OrchestratorEvent,
        )

        cancel_token = CancelToken()
        with self._analysis_lock:
            self._analysis_cancel_tokens[request.token] = cancel_token
        self._cancel_superseded_analysis()

        def on_event(event: OrchestratorEvent) -> None:
            if event in {
                OrchestratorEvent.Stage1Started,
                OrchestratorEvent.Stage1Retry,
            }:
                phase = "市场诊断"
            elif event in {
                OrchestratorEvent.Stage2Started,
                OrchestratorEvent.Stage2Retry,
            }:
                phase = "决策生成"
            else:
                return
            with self._controller_lock:
                is_current = (
                    self._controller.view.active_analysis_token
                    == request.token
                )
                with self._analysis_lock:
                    self._analysis_phases[request.token] = phase
                if is_current:
                    with suppress(RuntimeError):
                        self.analysis_phase_changed.emit(phase)

        base_frame = request.render_payload.analysis_frame("10m")
        if base_frame is None:
            with self._controller_lock:
                self._controller.fail_analysis(
                    request,
                    AnalysisFailureKind.INVALID_RESULT,
                    stage=AnalysisFailureStage.INPUT_FREEZE,
                )
            with self._analysis_lock:
                self._analysis_phases.pop(request.token, None)
                self._analysis_cancel_tokens.pop(request.token, None)
            with suppress(RuntimeError):
                self.analysis_phase_changed.emit(self.analysis_phase)
            self._emit_state()
            return
        higher_frames = {
            timeframe: frame
            for timeframe in request.bundle.ready_higher_timeframes
            if (
                frame
                := request.render_payload.analysis_frame(timeframe)
            )
            is not None
        }
        higher_text = (
            render_higher_timeframe_context(
                base_frame,
                higher_frames,
            )
            if higher_frames
            else ""
        )
        try:
            orchestrator = self._orchestrator_factory()
            if orchestrator is None:
                raise RuntimeError("orchestrator unavailable")
            record = orchestrator.submit(
                base_frame,
                cancel_token,
                on_event,
                higher_timeframe_text=higher_text,
            )
            with self._controller_lock:
                was_current = (
                    self._controller.view.active_analysis_token
                    == request.token
                )
                result = self._controller.complete_analysis(
                    request,
                    record,
                )
                if (
                    was_current
                    and self._controller.view.analysis_result == result
                ):
                    self._set_status("分析完成")
        except Exception:
            with self._analysis_lock:
                request_phase = self._analysis_phases.get(
                    request.token,
                    "",
                )
            failure_stage = (
                AnalysisFailureStage.DECISION_GENERATION
                if request_phase == "决策生成"
                else AnalysisFailureStage.MARKET_DIAGNOSIS
            )
            with self._controller_lock:
                accepted = self._controller.fail_analysis(
                    request,
                    AnalysisFailureKind.WORKER_FAILED,
                    stage=failure_stage,
                )
                if accepted:
                    self._set_status("分析失败")
        finally:
            with self._analysis_lock:
                self._analysis_phases.pop(request.token, None)
                self._analysis_cancel_tokens.pop(request.token, None)
            with suppress(RuntimeError):
                self.analysis_phase_changed.emit(self.analysis_phase)
            self._emit_state()

    def close(self) -> None:
        """停止接收新任务；运行中的有界只读调用完成后退出。"""

        if self._closed:
            return
        self._closed = True
        with self._analysis_lock:
            for token in tuple(self._analysis_cancel_tokens.values()):
                self._set_cancel_token(token)
            self._analysis_cancel_tokens.clear()
            self._analysis_phases.clear()
        self._market_executor.shutdown(
            wait=False,
            cancel_futures=True,
        )
        self._settings_executor.shutdown(
            wait=False,
            cancel_futures=True,
        )
        self._analysis_executor.shutdown(
            wait=False,
            cancel_futures=True,
        )
        cleanup = threading.Thread(
            target=self._runtime.close,
            name="market-workspace-close",
            daemon=True,
        )
        cleanup.start()
