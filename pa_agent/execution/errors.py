"""Typed execution errors used to distinguish safe failures from unknown submits."""
from __future__ import annotations


class ExecutionError(RuntimeError):
    """Base error for the execution subsystem."""


class PlanBlocked(ExecutionError):
    """The analysis/configuration cannot produce a safe execution plan."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CredentialError(ExecutionError):
    """Required broker credentials are absent or incomplete."""


class LiveTradingDisabled(ExecutionError):
    """A write was requested while the hard gate or session gate is disabled."""


class NewRiskLeaseUnavailable(LiveTradingDisabled):
    """The single new-risk lease is temporarily held by another session."""


class PreflightError(ExecutionError):
    """A deterministic broker preflight rejected the proposed order."""


class FallbackEligiblePreflightError(PreflightError):
    """A pre-submit capacity result may safely try the configured fallback account."""


class BrokerRejected(ExecutionError):
    """The broker definitively rejected a request."""


class SubmissionUnknown(ExecutionError):
    """The broker may have received a write, but its outcome is not yet known."""


class ReconciliationError(ExecutionError):
    """A persisted execution could not be reconciled safely."""


class BrokerApiError(ExecutionError):
    """A broker returned a structured non-success response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code
        self.message = message


class BrokerTransportError(ExecutionError):
    """The HTTP/WebSocket transport failed before a response was confirmed."""

    def __init__(self, message: str, *, write_may_have_reached: bool) -> None:
        super().__init__(message)
        self.write_may_have_reached = write_may_have_reached
