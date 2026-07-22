"""Session-level token usage ledger (no pricing)."""
from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal

from pa_agent.ai.deepseek_client import AIUsage

logger = logging.getLogger(__name__)


class SessionTokenLedger(QObject):
    """Accumulates token usage across API calls in a session.

    Signals
    -------
    threshold_crossed(str, dict)
        Emitted when context usage crosses warn_pct or 95%.
    updated(dict)
        Emitted after every add() with the current totals dict.
    """

    threshold_crossed = pyqtSignal(str, dict)
    updated = pyqtSignal(dict)

    def __init__(
        self,
        context_window: int | None = 1_000_000,
        warn_pct: float = 80.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._context_window = (
            context_window
            if isinstance(context_window, int) and context_window > 0
            else None
        )
        self._warn_pct = warn_pct
        self._yellow_fired = False
        self._red_fired = False

        self.total_input: int = 0
        self.total_cached_input: int = 0
        self.total_output: int = 0
        self.current_input: int = 0
        self.current_cached_input: int = 0
        self.current_output: int = 0

    @property
    def context_used(self) -> int:
        """最近一次模型请求所占上下文；累计 Token 不能冒充当前上下文。"""
        return self.current_input + self.current_output

    def add(self, usage: AIUsage) -> None:
        """Accumulate usage from one API call and emit signals."""
        self.total_input += usage.prompt_tokens
        self.total_cached_input += usage.cached_prompt_tokens
        self.total_output += usage.completion_tokens
        self.current_input = usage.prompt_tokens
        self.current_cached_input = usage.cached_prompt_tokens
        self.current_output = usage.completion_tokens

        totals = self.breakdown()
        self.updated.emit(totals)
        if self._context_window is None:
            return
        pct = self.context_used / self._context_window * 100.0
        if pct < self._warn_pct:
            # 原生 Compact 后上下文会下降，之后再次跨阈值时应重新告警。
            self._yellow_fired = False
            self._red_fired = False
        elif pct < 95.0:
            self._red_fired = False

        if pct >= 95.0 and not self._red_fired:
            self._red_fired = True
            logger.warning("Context usage >= 95%% (%.1f%%)", pct)
            self.threshold_crossed.emit("red", totals)
        elif pct >= self._warn_pct and not self._yellow_fired:
            self._yellow_fired = True
            logger.warning("Context usage >= %.0f%% (%.1f%%)", self._warn_pct, pct)
            self.threshold_crossed.emit("yellow", totals)

    def reset(self) -> None:
        """Reset all counters (e.g. on symbol/timeframe switch)."""
        self.total_input = 0
        self.total_cached_input = 0
        self.total_output = 0
        self.current_input = 0
        self.current_cached_input = 0
        self.current_output = 0
        self._yellow_fired = False
        self._red_fired = False

    def seed(
        self,
        *,
        total_input: int,
        total_cached_input: int,
        total_output: int,
        current_input: int,
        current_cached_input: int,
        current_output: int,
    ) -> None:
        """Restore audited analysis usage before free-chat follow-ups start."""

        values = {
            "total_input": total_input,
            "total_cached_input": total_cached_input,
            "total_output": total_output,
            "current_input": current_input,
            "current_cached_input": current_cached_input,
            "current_output": current_output,
        }
        if any(not isinstance(value, int) or value < 0 for value in values.values()):
            raise ValueError("token usage values must be non-negative integers")
        if total_cached_input > total_input:
            raise ValueError("total cached input cannot exceed total input")
        if current_cached_input > current_input:
            raise ValueError("current cached input cannot exceed current input")

        self.total_input = total_input
        self.total_cached_input = total_cached_input
        self.total_output = total_output
        self.current_input = current_input
        self.current_cached_input = current_cached_input
        self.current_output = current_output
        self._yellow_fired = False
        self._red_fired = False
        self.updated.emit(self.breakdown())

    def breakdown(self) -> dict:
        """Return current totals as a dict for UI display."""
        pct = (
            self.context_used / self._context_window * 100.0
            if self._context_window is not None
            else None
        )
        return {
            "total_input": self.total_input,
            "total_cached_input": self.total_cached_input,
            "total_output": self.total_output,
            "current_input": self.current_input,
            "current_cached_input": self.current_cached_input,
            "current_output": self.current_output,
            "context_used": self.context_used,
            "context_window": self._context_window,
            "context_pct": round(pct, 2) if pct is not None else None,
        }
