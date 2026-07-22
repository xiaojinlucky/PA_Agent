"""Typed protocol shared by the execution worker and its control plane."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BrokerName = Literal["longbridge", "okx"]
ExecutionEnvironment = Literal["demo", "live"]


class WorkerCommandAction(StrEnum):
    SUBMIT = "submit"
    CANCEL_ENTRY = "cancel_entry"
    REQUEST_EXIT = "request_exit"
    REFRESH_ACCOUNT = "refresh_account"
    RECONCILE = "reconcile"


class WorkerCommandStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class WorkerState(StrEnum):
    STARTING = "starting"
    RECONCILING = "reconciling"
    RUNNING = "running"
    NEEDS_ATTENTION = "needs_attention"
    STOPPING = "stopping"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含 UTC 时区")
    return value.astimezone(UTC)


class WorkerCommand(BaseModel):
    """One durable worker instruction without credentials or arbitrary payloads."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    action: WorkerCommandAction
    execution_id: str = Field(default="", max_length=256)
    requester: str = Field(min_length=1, max_length=128)
    broker: BrokerName
    environment: ExecutionEnvironment
    account: str = Field(min_length=1, max_length=256)
    new_risk_lease_id: str = Field(default="", max_length=128)
    reason_code: str = Field(
        default="",
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]*$",
    )
    status: WorkerCommandStatus = WorkerCommandStatus.PENDING
    worker_id: str = Field(default="", max_length=128)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_code: str = Field(
        default="",
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]*$",
    )
    failure_code: str = Field(
        default="",
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]*$",
    )

    @field_validator(
        "id",
        "execution_id",
        "requester",
        "account",
        "new_risk_lease_id",
        "reason_code",
        "worker_id",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def _normalise_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def _validate_action_fields(self) -> WorkerCommand:
        execution_actions = {
            WorkerCommandAction.SUBMIT,
            WorkerCommandAction.CANCEL_ENTRY,
            WorkerCommandAction.REQUEST_EXIT,
        }
        if self.action in execution_actions and not self.execution_id:
            raise ValueError(f"{self.action.value} 命令必须指定 execution_id")
        if self.action is WorkerCommandAction.SUBMIT:
            if not self.new_risk_lease_id:
                raise ValueError("submit 命令必须绑定 NEW_RISK 租约")
        elif self.new_risk_lease_id:
            raise ValueError("只有 submit 命令可以绑定 NEW_RISK 租约")
        if self.action is WorkerCommandAction.REQUEST_EXIT:
            if not self.reason_code:
                raise ValueError("request_exit 命令必须提供 reason_code")
        elif self.reason_code:
            raise ValueError("只有 request_exit 命令可以提供 reason_code")
        return self


class NewRiskLease(BaseModel):
    """Short-lived authority to create new market exposure on one route."""

    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(min_length=1, max_length=128)
    worker_id: str = Field(min_length=1, max_length=128)
    config_fingerprint: str = Field(min_length=1, max_length=256)
    requester: str = Field(min_length=1, max_length=128)
    broker: BrokerName
    environment: ExecutionEnvironment
    account: str = Field(min_length=1, max_length=256)
    granted_at: datetime
    expires_at: datetime

    @field_validator(
        "lease_id",
        "worker_id",
        "config_fingerprint",
        "requester",
        "account",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("granted_at", "expires_at")
    @classmethod
    def _normalise_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _validate_expiry(self) -> NewRiskLease:
        if self.expires_at <= self.granted_at:
            raise ValueError("NEW_RISK 租约到期时间必须晚于授予时间")
        return self


class WorkerHeartbeat(BaseModel):
    """Latest liveness record for one headless execution worker."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1, max_length=128)
    pid: int = Field(ge=0)
    started_at: datetime
    last_seen_at: datetime
    last_successful_reconcile_at: datetime | None = None
    state: WorkerState
    last_error_code: str = Field(
        default="",
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]*$",
    )

    @field_validator("worker_id", mode="before")
    @classmethod
    def _strip_worker_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "started_at",
        "last_seen_at",
        "last_successful_reconcile_at",
    )
    @classmethod
    def _normalise_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def _validate_ordering(self) -> WorkerHeartbeat:
        if self.last_seen_at < self.started_at:
            raise ValueError("worker last_seen_at 不能早于 started_at")
        return self
