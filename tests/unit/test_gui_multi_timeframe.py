from __future__ import annotations

from types import SimpleNamespace

from pa_agent.data.base import KlineBar
from pa_agent.gui.main_window import _AnalysisWorker
from tests.unit.test_multi_timeframe import _frame


class _MultiTimeframeSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def latest_snapshot_for_timeframe(
        self,
        timeframe: str,
        count: int,
    ) -> list[KlineBar]:
        self.calls.append((timeframe, count))
        interval_ms = 3_600_000 if timeframe == "1h" else 14_400_000
        return [
            KlineBar(
                seq=index + 1,
                ts_open=1_784_300_400_000 - index * interval_ms,
                open=4000 + index,
                high=4002 + index,
                low=3998 + index,
                close=4001 + index,
                volume=1,
                closed=True,
            )
            for index in range(count)
        ]


class _CaptureOrchestrator:
    def __init__(self) -> None:
        self.kwargs = {}

    def submit(self, frame, cancel_token, on_event, **kwargs):
        del frame, cancel_token, on_event
        self.kwargs = kwargs
        return SimpleNamespace(stage2_decision={}, exception=None)


def test_gui_analysis_worker_passes_same_source_1h_4h_context(qapp) -> None:
    del qapp
    source = _MultiTimeframeSource()
    orchestrator = _CaptureOrchestrator()
    worker = _AnalysisWorker(
        orchestrator=orchestrator,
        frame=_frame("15m", 4000, 3999, 3998),
        cancel_token=SimpleNamespace(),
        data_source=source,
    )

    worker.run()

    assert source.calls == [("1h", 70), ("4h", 70)]
    context = orchestrator.kwargs["higher_timeframe_text"]
    assert "主周期=15m" in context
    assert "背景 1h" in context
    assert "背景 4h" in context
