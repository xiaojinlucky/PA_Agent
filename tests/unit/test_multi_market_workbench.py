from __future__ import annotations

import pytest
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QPushButton

from pa_agent.config.settings import Settings
from pa_agent.data.base import KlineBar
from pa_agent.data.market_workspace import QuoteFailureKind, QuoteSnapshot
from pa_agent.data.market_workspace_controller import (
    AnalysisFailureKind,
    AnalysisFailureStage,
    MarketWorkspaceController,
)
from pa_agent.data.market_workspace_runtime import MarketWorkspaceRuntime
from pa_agent.gui.multi_market_workbench import MultiMarketWorkbench

_AS_OF = 1_700_000_600_000


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Bridge(QObject):
    state_changed = pyqtSignal()
    status_changed = pyqtSignal(str)
    analysis_phase_changed = pyqtSignal(str)

    def __init__(self, controller: MarketWorkspaceController) -> None:
        super().__init__()
        self.controller = controller
        self.status = "行情已更新"
        self.analysis_phase = ""
        self.analysis_calls = 0

    def snapshot(self):
        return self.controller.view

    def start_initial_load(self):
        return None

    def select(self, **kwargs):
        self.last_select = kwargs

    def refresh(self):
        return True

    def refresh_watchlist(self):
        return True

    def set_watchlist(self, symbols):
        self.last_watchlist = tuple(symbols)
        return True

    def start_analysis(self):
        self.analysis_calls += 1
        return True

    def close(self):
        return None


class _Source:
    def __init__(self, *, price_tick: str | None) -> None:
        self.price_tick = price_tick

    def connect(self):
        return None

    def disconnect(self):
        return None

    def subscribe(self, symbol, timeframe):
        del symbol, timeframe

    def batch_quote_snapshots(self, token):
        return tuple(
            QuoteSnapshot.from_prices(
                selection_generation=token.selection_generation,
                request_sequence=token.watchlist_refresh_sequence,
                symbol=symbol,
                market=token.market,
                source=token.source,
                name=symbol,
                currency="USDT" if token.market == "Crypto" else "USD",
                last="101",
                prev_close="100",
                price_tick=self.price_tick,
                quote_ts_utc_ms=_AS_OF - 100,
                received_at_utc_ms=_AS_OF,
            )
            for symbol in token.symbols
        )

    def latest_snapshot_for_timeframe(
        self,
        timeframe,
        n,
        *,
        analysis_as_of_utc_ms,
    ):
        interval = {
            "10m": 10 * 60_000,
            "1h": 60 * 60_000,
            "4h": 4 * 60 * 60_000,
        }[timeframe]
        return [
            KlineBar(
                seq=index + 1,
                ts_open=analysis_as_of_utc_ms - interval * (index + 1),
                open=100,
                high=102,
                low=99,
                close=101,
                volume=10,
                amount=1_000,
                closed=True,
                price_tick=self.price_tick,
            )
            for index in range(n)
        ]

    @staticmethod
    def closed_bar_end_utc_ms(
        bar: KlineBar,
        timeframe: str,
    ) -> int:
        interval = {
            "10m": 10 * 60_000,
            "1h": 60 * 60_000,
            "4h": 4 * 60 * 60_000,
        }[timeframe]
        return int(bar.ts_open) + interval


def _committed_controller(
    *,
    market: str,
    symbol: str,
    source_name: str,
    price_tick: str | None,
) -> MarketWorkspaceController:
    settings = Settings()
    settings.general.analysis_bar_count = 2
    settings.market_workspace.selected_market = market
    settings.market_workspace.last_symbols_by_market[market] = symbol
    controller = MarketWorkspaceController(
        settings,
        clock_utc_ms=lambda: _AS_OF,
    )
    runtime = MarketWorkspaceRuntime(
        sources={source_name: _Source(price_tick=price_tick)},
        clock_utc_ms=lambda: _AS_OF,
    )
    request = controller.begin_initial_load()
    loaded = runtime.load_market_data(
        request,
        freeze_request=controller.freeze_analysis_as_of,
    )
    assert controller.complete_market_data(
        loaded.request,
        loaded.bundle,
        loaded.render_payload,
    ).accepted
    runtime.close()
    return controller


def test_workbench_matches_1440_layout_and_contains_no_trading_controls(
    qapp,
) -> None:
    controller = _committed_controller(
        market="Crypto",
        symbol="XAU-USDT-SWAP",
        source_name="okx",
        price_tick="0.1",
    )
    bridge = _Bridge(controller)
    widget = MultiMarketWorkbench(
        controller=controller,
        bridge=bridge,
        runtime_sha="a" * 40,
    )
    widget.resize(1440, 840)
    widget.show()
    qapp.processEvents()

    assert widget._context_bar.height() == 48
    assert widget._status_bar.height() == 24
    assert widget._left_panel.width() == 240
    assert widget._right_panel.width() == 340
    assert widget._summary_panel.height() == 72
    assert widget._chart_panel.height() == 432
    assert widget._analysis_panel.height() == 264
    assert widget._chart._market_read_only is True
    assert widget._chart.focusPolicy() is Qt.FocusPolicy.NoFocus
    assert widget._runtime_sha_label.text().endswith("a" * 40)
    assert widget._watchlist_table.wordWrap() is False
    assert [
        widget._watchlist_table.columnWidth(index)
        for index in range(3)
    ] == [126, 55, 57]
    assert (
        widget._watchlist_model.index(0, 0).data()
        == "XAU-USDT-SWAP"
    )

    button_texts = {
        button.text()
        for button in widget.findChildren(QPushButton)
    }
    assert not button_texts & {
        "买入",
        "卖出",
        "下单",
        "撤单",
        "平仓",
        "设置杠杆",
    }
    widget.close()


def test_stock_without_authoritative_tick_is_display_only(qapp) -> None:
    controller = _committed_controller(
        market="US",
        symbol="AAPL.US",
        source_name="longbridge",
        price_tick=None,
    )
    bridge = _Bridge(controller)
    widget = MultiMarketWorkbench(
        controller=controller,
        bridge=bridge,
        runtime_sha="b" * 40,
    )
    widget.resize(1440, 840)
    widget.show()
    widget.render()
    qapp.processEvents()

    assert widget._gate_conclusion.text() == "⚠ 仅展示，价格分析不可用"
    assert not widget._analysis_button.isEnabled()
    assert widget._price_label.text() == "101"
    widget.close()


def test_empty_state_never_renders_fake_prices(qapp) -> None:
    settings = Settings()
    controller = MarketWorkspaceController(
        settings,
        clock_utc_ms=lambda: _AS_OF,
    )
    bridge = _Bridge(controller)
    widget = MultiMarketWorkbench(
        controller=controller,
        bridge=bridge,
        runtime_sha="c" * 40,
    )
    widget.resize(1440, 840)
    widget.show()
    widget.render()
    qapp.processEvents()

    assert widget._price_label.text() == "—"
    assert widget._chart_state_label.text() == "暂无行情数据"
    assert widget._quote_values["最新价"].text() == "—"
    assert not widget._analysis_button.isEnabled()
    widget.close()


def test_keyboard_focus_visits_only_actionable_page_controls(qapp) -> None:
    controller = _committed_controller(
        market="Crypto",
        symbol="XAU-USDT-SWAP",
        source_name="okx",
        price_tick="0.1",
    )
    bridge = _Bridge(controller)
    widget = MultiMarketWorkbench(
        controller=controller,
        bridge=bridge,
        runtime_sha="d" * 40,
    )
    widget.resize(1440, 840)
    widget.show()
    qapp.processEvents()

    widget._market_buttons["Crypto"].setFocus()
    qapp.processEvents()
    focus_order = []
    for _ in range(7):
        focused = qapp.focusWidget()
        assert focused is not None
        focus_order.append(
            focused.accessibleName()
            or getattr(focused, "text", lambda: "")()
        )
        widget.focusNextPrevChild(True)
        qapp.processEvents()

    assert focus_order == [
        "切换到加密",
        "刷新当前市场行情",
        "筛选当前市场自选标的",
        "添加本地自选",
        "当前市场自选列表",
        "展示 10m K 线",
        "基于当前固定 10m 数据开始只读分析",
    ]
    assert qapp.focusWidget() is widget._market_buttons["Crypto"]
    widget.close()


def test_switch_failure_preserves_old_view_but_disables_analysis(qapp) -> None:
    controller = _committed_controller(
        market="Crypto",
        symbol="XAU-USDT-SWAP",
        source_name="okx",
        price_tick="0.1",
    )
    failed = controller.begin_selection(
        market="US",
        symbol="AAPL.US",
        display_timeframe="10m",
    )
    assert controller.fail_market_data(
        failed,
        QuoteFailureKind.TRANSPORT_FAILED,
    )
    bridge = _Bridge(controller)
    widget = MultiMarketWorkbench(
        controller=controller,
        bridge=bridge,
        runtime_sha="e" * 40,
    )
    widget.resize(1440, 840)
    widget.show()
    widget.render()
    qapp.processEvents()

    assert widget._symbol_label.text() == "XAU-USDT-SWAP"
    assert widget._gate_conclusion.text() == "× 切换失败"
    assert (
        "AAPL.US：行情连接失败"
        in widget._gate_reason_labels[0].text()
    )
    assert not widget._analysis_button.isEnabled()
    widget.close()


def test_analysis_failure_shows_exact_stage_and_allows_retry(qapp) -> None:
    controller = _committed_controller(
        market="Crypto",
        symbol="XAU-USDT-SWAP",
        source_name="okx",
        price_tick="0.1",
    )
    analysis = controller.begin_analysis()
    assert controller.fail_analysis(
        analysis,
        AnalysisFailureKind.WORKER_FAILED,
        stage=AnalysisFailureStage.DECISION_GENERATION,
    )
    bridge = _Bridge(controller)
    widget = MultiMarketWorkbench(
        controller=controller,
        bridge=bridge,
        runtime_sha="f" * 40,
    )
    widget.resize(1440, 840)
    widget.show()
    widget.render()
    qapp.processEvents()

    assert widget._diagnosis_values["周期位置"].text() == "决策生成"
    assert widget._decision_values["终局"].text() == "失败"
    assert widget._decision_values["理由"].text() == "分析服务执行失败"
    assert widget._gate_conclusion.text() == "× 分析失败"
    assert widget._analysis_button.isEnabled()
    widget.close()
