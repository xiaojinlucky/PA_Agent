from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from pa_agent.config.settings import Settings
from pa_agent.data.base import KlineBar
from pa_agent.data.market_workspace import QuoteSnapshot
from pa_agent.data.market_workspace_controller import MarketWorkspaceController
from pa_agent.data.market_workspace_runtime import MarketWorkspaceRuntime
from pa_agent.gui.market_workspace_bridge import MarketWorkspaceQtBridge
from pa_agent.util.threading import OrchestratorEvent

_AS_OF = 1_700_000_600_000


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(
    qapp: QApplication,
    predicate,
    *,
    timeout: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("异步状态没有在限定时间内完成")


class _Source:
    def __init__(
        self,
        *,
        price_tick: str | None,
        block_first_quote: bool = False,
    ) -> None:
        self.price_tick = price_tick
        self.symbol = ""
        self.first_quote_started = threading.Event()
        self.release_first_quote = threading.Event()
        self.block_first_quote = block_first_quote
        self.quote_calls = 0

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def subscribe(self, symbol: str, timeframe: str) -> None:
        del timeframe
        self.symbol = symbol

    def batch_quote_snapshots(self, token):
        self.quote_calls += 1
        if self.block_first_quote and self.quote_calls == 1:
            self.first_quote_started.set()
            if not self.release_first_quote.wait(timeout=2):
                raise RuntimeError("test quote was not released")
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
        timeframe: str,
        n: int,
        *,
        analysis_as_of_utc_ms: int,
    ) -> list[KlineBar]:
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


class _BlockingOrchestrator:
    def __init__(
        self,
        *,
        phase: OrchestratorEvent,
        started: threading.Event,
        release: threading.Event,
    ) -> None:
        self._phase = phase
        self._started = started
        self._release = release
        self.cancel_token = None

    def submit(self, frame, cancel_token, callback, **kwargs):
        del kwargs
        self.cancel_token = cancel_token
        callback(self._phase)
        self._started.set()
        if not self._release.wait(timeout=2):
            raise RuntimeError("test analysis was not released")
        return SimpleNamespace(
            meta=SimpleNamespace(
                symbol=frame.symbol,
                timeframe="10m",
                data_source="okx",
            ),
            stage1_diagnosis={},
            stage2_decision={
                "decision": {"order_type": "不下单"},
                "terminal": {"outcome": "wait"},
            },
            exception=None,
        )


def _bridge(
    *,
    market: str,
    symbol: str,
    source_name: str,
    source: _Source,
    orchestrator_factory=None,
) -> tuple[MarketWorkspaceQtBridge, MarketWorkspaceController]:
    settings = Settings()
    settings.general.analysis_bar_count = 2
    settings.market_workspace.selected_market = market
    settings.market_workspace.last_symbols_by_market[market] = symbol
    controller = MarketWorkspaceController(
        settings,
        clock_utc_ms=lambda: _AS_OF,
    )
    runtime = MarketWorkspaceRuntime(
        sources={source_name: source},
        clock_utc_ms=lambda: _AS_OF,
    )
    bridge = MarketWorkspaceQtBridge(
        controller=controller,
        runtime=runtime,
        settings_path=None,
        orchestrator_factory=orchestrator_factory,
    )
    return bridge, controller


def test_bridge_ignores_old_result_after_fast_symbol_switch(qapp) -> None:
    source = _Source(price_tick="0.1", block_first_quote=True)
    bridge, controller = _bridge(
        market="Crypto",
        symbol="BTC-USDT",
        source_name="okx",
        source=source,
    )
    bridge.start_initial_load()
    assert source.first_quote_started.wait(timeout=1)

    bridge.select(
        market="Crypto",
        symbol="ETH-USDT",
        display_timeframe="10m",
    )
    source.release_first_quote.set()

    _wait_until(
        qapp,
        lambda: (
            controller.view.committed_identity is not None
            and controller.view.committed_identity.symbol == "ETH-USDT"
            and controller.view.bundle_current
        ),
    )
    assert controller.view.render_payload is not None
    assert (
        controller.view.render_payload.token.identity.symbol
        == "ETH-USDT"
    )
    bridge.close()


def test_bridge_never_calls_ai_when_stock_tick_is_unavailable(qapp) -> None:
    calls = []
    source = _Source(price_tick=None)
    bridge, controller = _bridge(
        market="US",
        symbol="AAPL.US",
        source_name="longbridge",
        source=source,
        orchestrator_factory=lambda: calls.append("called"),
    )
    bridge.start_initial_load()
    _wait_until(qapp, lambda: controller.view.bundle_current)

    assert bridge.start_analysis() is False
    assert calls == []
    assert controller.view.bundle is not None
    assert controller.view.bundle.analysis_state == "display_only"
    bridge.close()


def test_bridge_module_has_no_execution_dependency() -> None:
    path = (
        Path(__file__).parents[2]
        / "pa_agent"
        / "gui"
        / "market_workspace_bridge.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any(
        name == "pa_agent.execution"
        or name.startswith("pa_agent.execution.")
        for name in imported
    )


def test_deferred_initial_load_is_harmless_after_window_closes() -> None:
    source = _Source(price_tick="0.1")
    bridge, controller = _bridge(
        market="Crypto",
        symbol="BTC-USDT",
        source_name="okx",
        source=source,
    )

    bridge.close()
    bridge.start_initial_load()

    assert controller.view.committed_identity is None
    assert source.quote_calls == 0


def test_old_analysis_completion_cannot_clear_new_analysis_phase(
    qapp,
) -> None:
    first_started = threading.Event()
    first_release = threading.Event()
    second_started = threading.Event()
    second_release = threading.Event()
    first_orchestrator = _BlockingOrchestrator(
        phase=OrchestratorEvent.Stage1Started,
        started=first_started,
        release=first_release,
    )
    second_orchestrator = _BlockingOrchestrator(
        phase=OrchestratorEvent.Stage2Started,
        started=second_started,
        release=second_release,
    )
    orchestrators = [first_orchestrator, second_orchestrator]
    source = _Source(price_tick="0.1")
    bridge, controller = _bridge(
        market="Crypto",
        symbol="BTC-USDT",
        source_name="okx",
        source=source,
        orchestrator_factory=lambda: orchestrators.pop(0),
    )
    bridge.start_initial_load()
    _wait_until(qapp, lambda: controller.view.bundle_current)

    assert bridge.start_analysis()
    assert first_started.wait(timeout=1)
    bridge.select(
        market="Crypto",
        symbol="ETH-USDT",
        display_timeframe="10m",
    )
    _wait_until(
        qapp,
        lambda: (
            controller.view.committed_identity is not None
            and controller.view.committed_identity.symbol == "ETH-USDT"
            and controller.view.bundle_current
        ),
    )
    assert first_orchestrator.cancel_token is not None
    assert first_orchestrator.cancel_token.is_set()
    assert bridge.start_analysis()
    assert second_started.wait(timeout=1)
    assert bridge.analysis_phase == "决策生成"

    first_release.set()
    _wait_until(qapp, lambda: len(controller.view.analysis_history) == 1)

    assert controller.view.analysis_state == "running"
    assert bridge.analysis_phase == "决策生成"
    assert bridge.status == "分析进行中"

    second_release.set()
    _wait_until(qapp, lambda: controller.view.analysis_state == "succeeded")
    bridge.close()
