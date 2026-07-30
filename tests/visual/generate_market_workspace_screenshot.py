"""生成 PRD11 多市场页的离屏视觉证据。

这里只使用真实 Controller/读模型和合成只读行情源。脚本不启动
AppContext.bootstrap，不访问网络、数据库或 execution，也不会写入设置。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        required=True,
        choices=(
            "normal",
            "loading",
            "empty",
            "stale",
            "auth_failed",
            "calendar_unknown",
            "switch_failed",
            "analysis_running",
            "analysis_failed",
        ),
    )
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


ARGS = _parse_args()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["QT_SCALE_FACTOR"] = str(ARGS.scale)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt6.QtGui import (  # noqa: E402
    QFont,
    QFontDatabase,
    QFontInfo,
    QFontMetrics,
    QRawFont,
)
from PyQt6.QtWidgets import QApplication, QPushButton  # noqa: E402

from pa_agent.app_context import AppContext  # noqa: E402
from pa_agent.build_info import runtime_sha  # noqa: E402
from pa_agent.config.settings import Settings  # noqa: E402
from pa_agent.data.base import KlineBar  # noqa: E402
from pa_agent.data.market_workspace import (  # noqa: E402
    QuoteFailureKind,
    QuoteSnapshot,
)
from pa_agent.data.market_workspace_controller import (  # noqa: E402
    AnalysisFailureKind,
    AnalysisFailureStage,
    MarketWorkspaceController,
)
from pa_agent.data.market_workspace_runtime import (  # noqa: E402
    MarketWorkspaceRuntime,
)
from pa_agent.gui.main_window import MainWindow  # noqa: E402
from pa_agent.gui.multi_market_workbench import (  # noqa: E402
    _REQUIRED_UI_GLYPHS,
)
from pa_agent.release_pipeline import (  # noqa: E402
    render_widget_synchronously,
)

_AS_OF = int(
    datetime(2026, 7, 29, 14, 30, tzinfo=UTC).timestamp() * 1_000
)
_INTERVALS = {
    "10m": 10 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
}
_CJK_SAMPLE = "多市场看盘分析工作台美股港股A股加密开始刷新失败加载过期"
_FONT_COVERAGE_SAMPLE = _CJK_SAMPLE + _REQUIRED_UI_GLYPHS
_CAPTURE_CONTRACT = "synchronous-widget-render-v1"


def _configure_cjk_font(app: QApplication) -> str:
    candidates: list[str] = []
    font_path_text = os.environ.get("PA_AGENT_VISUAL_FONT_PATH", "").strip()
    if font_path_text:
        font_path = Path(font_path_text).resolve()
        if not font_path.is_file():
            raise RuntimeError("离屏证据中文字体文件不存在")
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id < 0:
            raise RuntimeError("离屏证据中文字体加载失败")
        candidates.extend(QFontDatabase.applicationFontFamilies(font_id))
    candidates.extend(
        (
            "Microsoft YaHei UI",
            "Microsoft YaHei",
            "Noto Sans CJK SC",
            "SimHei",
            "SimSun",
        )
    )
    available_families = set(QFontDatabase.families())
    seen: set[str] = set()
    for family in candidates:
        if family in seen or family not in available_families:
            continue
        seen.add(family)
        font = QFont(family)
        raw_font = QRawFont.fromFont(font)
        if raw_font.isValid() and all(
            raw_font.supportsCharacter(character)
            for character in _FONT_COVERAGE_SAMPLE
        ):
            app.setFont(font)
            return QFontInfo(font).family()
    raise RuntimeError("离屏证据环境没有覆盖核心中文和界面符号的字体")


def _unavailable_calendar(_market: str, _as_of_utc_ms: int) -> Any:
    raise RuntimeError("fixture calendar unavailable")


class _FixtureSource:
    """确定性的只读行情源；只实现多市场运行层需要的读取方法。"""

    def __init__(self, *, price_tick: str | None) -> None:
        self.price_tick = price_tick
        self.symbol = ""

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def subscribe(self, symbol: str, timeframe: str) -> None:
        del timeframe
        self.symbol = symbol

    def batch_quote_snapshots(self, token: Any) -> tuple[QuoteSnapshot, ...]:
        market_prices = {
            "Crypto": ("2378.4", "2362.1", "USDT"),
            "US": ("214.62", "211.27", "USD"),
            "HK": ("345.60", "341.20", "HKD"),
            "CN": ("1488.88", "1471.06", "CNY"),
        }
        last, previous, currency = market_prices[token.market]
        names = {
            "XAU-USDT-SWAP": "黄金 USDT 永续",
            "AAPL.US": "Apple Inc.",
            "700.HK": "腾讯控股",
            "600519.SH": "贵州茅台",
        }
        return tuple(
            QuoteSnapshot.from_prices(
                selection_generation=token.selection_generation,
                request_sequence=token.watchlist_refresh_sequence,
                symbol=symbol,
                market=token.market,
                source=token.source,
                name=names.get(symbol, symbol),
                currency=currency,
                last=last,
                prev_close=previous,
                price_tick=self.price_tick,
                quote_ts_utc_ms=_AS_OF - 800,
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
        interval = _INTERVALS[timeframe]
        bases = {
            "XAU-USDT-SWAP": {
                "10m": 2378.0,
                "1h": 2364.0,
                "4h": 2328.0,
            },
            "AAPL.US": {"10m": 212.0, "1h": 207.0, "4h": 198.0},
            "700.HK": {"10m": 343.0, "1h": 334.0, "4h": 318.0},
            "600519.SH": {
                "10m": 1486.0,
                "1h": 1462.0,
                "4h": 1418.0,
            },
        }
        base = bases.get(self.symbol, bases["XAU-USDT-SWAP"])[timeframe]
        bars: list[KlineBar] = []
        for index in range(n):
            wave = math.sin(index / 3.2) * 7.5
            drift = -index * 0.24
            open_price = base + wave + drift
            close_price = open_price + math.cos(index / 2.1) * 2.8
            bars.append(
                KlineBar(
                    seq=index + 1,
                    ts_open=analysis_as_of_utc_ms - interval * (index + 1),
                    open=open_price,
                    high=max(open_price, close_price) + 2.2,
                    low=min(open_price, close_price) - 2.0,
                    close=close_price,
                    volume=900 + index * 11,
                    amount=(900 + index * 11) * close_price,
                    closed=True,
                    price_tick=self.price_tick,
                )
            )
        return bars

    @staticmethod
    def closed_bar_end_utc_ms(
        bar: KlineBar,
        timeframe: str,
    ) -> int:
        return int(bar.ts_open) + _INTERVALS[timeframe]


class _LegacySource:
    """让旧工作台保持可构造，但不连接任何外部数据。"""

    _connected = False

    @staticmethod
    def list_symbols() -> list[str]:
        return []

    @staticmethod
    def supported_timeframes() -> list[str]:
        return ["15m"]


class _NeverRuntime:
    """截图窗口不得触发的新读取边界。"""

    def __init__(self) -> None:
        self.calls = 0

    def load_market_data(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("离屏截图不得重新读取行情")

    def load_watchlist(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("离屏截图不得重新读取自选")

    def close(self) -> None:
        return None


def _settings(
    *,
    market: str = "Crypto",
    symbol: str = "XAU-USDT-SWAP",
) -> Settings:
    settings = Settings()
    settings.general.analysis_bar_count = 53
    settings.market_workspace.selected_market = market
    settings.market_workspace.last_symbols_by_market[market] = symbol
    return settings


def _committed_controller(
    *,
    market: str = "Crypto",
    symbol: str = "XAU-USDT-SWAP",
    source_name: str = "okx",
    price_tick: str | None = "0.1",
    unknown_calendar: bool = False,
) -> tuple[Settings, MarketWorkspaceController]:
    settings = _settings(market=market, symbol=symbol)
    controller = MarketWorkspaceController(
        settings,
        clock_utc_ms=lambda: _AS_OF,
    )
    runtime = MarketWorkspaceRuntime(
        sources={
            source_name: _FixtureSource(price_tick=price_tick),
        },
        clock_utc_ms=lambda: _AS_OF,
        session_state_loader=(
            _unavailable_calendar
            if unknown_calendar
            else None
        ),
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
    return settings, controller


def _scenario() -> tuple[Settings, MarketWorkspaceController]:
    scenario = ARGS.scenario
    if scenario == "empty":
        settings = _settings()
        return settings, MarketWorkspaceController(
            settings,
            clock_utc_ms=lambda: _AS_OF,
        )
    if scenario in {"auth_failed", "calendar_unknown"}:
        settings, controller = _committed_controller(
            market="US",
            symbol="AAPL.US",
            source_name="longbridge",
            price_tick=None,
            unknown_calendar=scenario == "calendar_unknown",
        )
    else:
        settings, controller = _committed_controller()

    if scenario == "loading":
        controller.begin_selection(
            market="US",
            symbol="AAPL.US",
            display_timeframe="10m",
        )
    elif scenario == "stale":
        request = controller.refresh_current()
        assert controller.fail_market_data(
            request,
            QuoteFailureKind.TRANSPORT_FAILED,
        )
    elif scenario == "auth_failed":
        request = controller.refresh_current()
        assert controller.fail_market_data(
            request,
            QuoteFailureKind.AUTH_FAILED,
        )
    elif scenario == "switch_failed":
        request = controller.begin_selection(
            market="US",
            symbol="AAPL.US",
            display_timeframe="10m",
        )
        assert controller.fail_market_data(
            request,
            QuoteFailureKind.SYMBOL_UNSUPPORTED,
        )
    elif scenario == "analysis_running":
        controller.begin_analysis()
    elif scenario == "analysis_failed":
        request = controller.begin_analysis()
        assert controller.fail_analysis(
            request,
            AnalysisFailureKind.WORKER_FAILED,
            stage=AnalysisFailureStage.DECISION_GENERATION,
        )
    return settings, controller


def _focus_order(root: Any) -> list[str]:
    order: list[str] = []
    first = next(
        (
            button
            for button in root._market_buttons.values()
            if button.isChecked()
        ),
        next(iter(root._market_buttons.values())),
    )
    first.setFocus()
    QApplication.processEvents()
    seen: set[int] = set()
    while (current := QApplication.focusWidget()) is not None:
        if current is not root and not root.isAncestorOf(current):
            break
        if id(current) in seen:
            break
        seen.add(id(current))
        text = getattr(current, "text", lambda: "")()
        name = (
            current.accessibleName()
            or str(text)
            or current.objectName()
            or type(current).__name__
        )
        order.append(name)
        root.focusNextPrevChild(True)
        QApplication.processEvents()
    return order


def _metadata(
    window: MainWindow,
    image: Any,
    runtime: _NeverRuntime,
    *,
    cjk_font_family: str,
    focus_order: list[str],
) -> dict[str, Any]:
    page = window._market_workspace
    symbol_font = QFontInfo(page._symbol_label.font())
    body_font = QFontInfo(page._name_label.font())
    button_texts = sorted(
        button.text()
        for button in page.findChildren(QPushButton)
    )
    return {
        "git_sha": runtime_sha(),
        "scenario": ARGS.scenario,
        "logical_window": [window.width(), window.height()],
        "physical_image": [image.width(), image.height()],
        "device_pixel_ratio": image.devicePixelRatio(),
        "requested_scale": ARGS.scale,
        "geometry": {
            "menu": window.menuBar().height(),
            "tabs": window._central.tabBar().height(),
            "page": [page.width(), page.height()],
            "context": page._context_bar.height(),
            "body": page._body.height(),
            "status": page._status_bar.height(),
            "columns": [
                page._left_panel.width(),
                page._center_panel.width(),
                page._right_panel.width(),
            ],
            "watchlist_viewport": page._watchlist_table.viewport().width(),
            "watchlist_columns": [
                page._watchlist_table.columnWidth(index)
                for index in range(3)
            ],
            "center_rows": [
                page._summary_panel.height(),
                page._chart_panel.height(),
                page._analysis_panel.height(),
            ],
        },
        "font": {
            "family": cjk_font_family,
            "cjk_sample_supported": True,
            "required_ui_glyphs_supported": True,
            "symbol_pixel_size": symbol_font.pixelSize(),
            "symbol_height": QFontMetrics(page._symbol_label.font()).height(),
            "body_pixel_size": body_font.pixelSize(),
            "body_height": QFontMetrics(page._name_label.font()).height(),
        },
        "focus_order": focus_order,
        "button_texts": button_texts,
        "ui_runtime_read_calls": runtime.calls,
        "selection_state": page._bridge.snapshot().selection_state.value,
        "bundle_current": page._bridge.snapshot().bundle_current,
        "analysis_state": page._bridge.snapshot().analysis_state.value,
        "analysis_button": {
            "text": page._analysis_button.text(),
            "enabled": page._analysis_button.isEnabled(),
        },
        "gate_conclusion": page._gate_conclusion.text(),
        "market_clock_status": page._clock_values["状态"].text(),
    }


def main() -> int:
    app = QApplication.instance() or QApplication([])
    cjk_font_family = _configure_cjk_font(app)
    settings, controller = _scenario()
    runtime = _NeverRuntime()
    window = MainWindow(
        AppContext(
            settings=settings,
            data_source=_LegacySource(),
            market_workspace_controller=controller,
            market_workspace_runtime=runtime,
        )
    )
    # 截图只消费已经由真实 Controller 接受的冻结状态。
    window._market_workspace_bridge.close()
    if ARGS.scenario == "analysis_running":
        active = controller.view.active_analysis_token
        assert active is not None
        with window._market_workspace_bridge._analysis_lock:
            window._market_workspace_bridge._analysis_phases[
                active
            ] = "市场诊断"
    window._startup_ai_auth_check_done = True
    window._startup_tv_connectivity_check_done = True
    window.resize(ARGS.width, ARGS.height)
    window.show()
    for _ in range(5):
        app.processEvents()
        time.sleep(0.01)
    window._market_workspace.render()
    for _ in range(3):
        app.processEvents()
    window.ensurePolished()
    window.update()
    app.sendPostedEvents()
    app.processEvents()
    focus_order = _focus_order(window._market_workspace)
    app.sendPostedEvents()
    app.processEvents()
    image = render_widget_synchronously(window)

    ARGS.output.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(ARGS.output)):
        raise RuntimeError("离屏截图保存失败")
    metadata = _metadata(
        window,
        image,
        runtime,
        cjk_font_family=cjk_font_family,
        focus_order=focus_order,
    )
    metadata["capture_contract"] = _CAPTURE_CONTRACT
    metadata["image_sha256"] = hashlib.sha256(
        ARGS.output.read_bytes()
    ).hexdigest()
    metadata_path = ARGS.output.with_suffix(".json")
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "image": str(ARGS.output),
                "metadata": str(metadata_path),
                "logical_window": metadata["logical_window"],
                "physical_image": metadata["physical_image"],
                "ui_runtime_read_calls": runtime.calls,
            },
            ensure_ascii=False,
        )
    )
    window.close()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
