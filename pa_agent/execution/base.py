"""Protocol implemented by concrete broker lifecycle adapters."""
from __future__ import annotations

from typing import Protocol

from pa_agent.execution.models import (
    AccountSnapshot,
    ExecutionPlan,
    ExecutionRecord,
    PreflightResult,
)


class BrokerAdapter(Protocol):
    def account_identity(
        self,
        plan: ExecutionPlan,
        *,
        account_profile: str | None = None,
    ) -> str: ...

    def preflight(self, plan: ExecutionPlan) -> PreflightResult: ...

    def prepare_submit(self, record: ExecutionRecord) -> ExecutionRecord: ...

    def submit_entry(self, record: ExecutionRecord) -> ExecutionRecord: ...

    def reconcile(
        self,
        record: ExecutionRecord,
        *,
        allow_writes: bool,
    ) -> ExecutionRecord: ...

    def request_exit(
        self,
        record: ExecutionRecord,
        *,
        reason: str,
    ) -> ExecutionRecord: ...

    def cancel_entry(self, record: ExecutionRecord) -> ExecutionRecord: ...

    def account_snapshot(
        self,
        plan: ExecutionPlan,
        *,
        account_profile: str | None = None,
        broker_metadata: dict | None = None,
    ) -> AccountSnapshot: ...
