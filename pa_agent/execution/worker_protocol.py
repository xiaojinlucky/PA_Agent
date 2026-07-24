"""Typed protocol shared by the execution worker and its control plane."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BrokerName = Literal["longbridge", "okx"]
ExecutionEnvironment = Literal["demo", "live"]


class WorkerCommandAction(StrEnum):
    SUBMIT = "submit"
    SET_LEVERAGE = "set_leverage"
    CANCEL_ENTRY = "cancel_entry"
    REQUEST_EXIT = "request_exit"
    REFRESH_ACCOUNT = "refresh_account"
    RECONCILE = "reconcile"
    CLEAR_DRAWDOWN_STOP = "clear_drawdown_stop"


class WorkerCommandStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class LeverageCapacityPoint(BaseModel):
    """One broker-read capacity point on the bounded policy grid."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    leverage: Decimal = Field(gt=0, le=125)
    capacity: Decimal = Field(gt=0)


class SetLeverageParameters(BaseModel):
    """Immutable deterministic inputs for one OKX leverage adjustment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    analysis_record_path: str = Field(default="", max_length=1024)
    config_fingerprint: str = Field(min_length=1, max_length=256)
    instrument: str = Field(min_length=1, max_length=128)
    direction: Literal["long", "short"]
    margin_mode: Literal["cross"]
    position_mode: Literal["net_mode"]
    current_leverage: Decimal = Field(gt=0)
    target_leverage: Decimal = Field(gt=0, le=125)
    current_capacity: Decimal = Field(gt=0)
    target_capacity: Decimal = Field(gt=0)
    maximum_leverage: Decimal = Field(gt=0, le=125)
    maximum_capacity: Decimal = Field(gt=0)
    planning_method: Literal["bounded_sequential_policy_grid_v1"]
    policy_grid_step: Decimal = Field(gt=0)
    verified_grid: tuple[LeverageCapacityPoint, ...] = Field(
        min_length=2,
        max_length=64,
    )
    required_quantity: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    expected_account_identity: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    okx_api_base_url: str = Field(min_length=1, max_length=512)
    leverage_intent_digest: str = Field(
        default="",
        max_length=64,
        pattern=r"^(|[0-9a-f]{64})$",
    )
    supervisor_record_id: str = Field(default="", max_length=256)
    supervisor_record_path: str = Field(default="", max_length=1024)
    supervisor_record_digest: str = Field(
        default="",
        max_length=64,
        pattern=r"^(|[0-9a-f]{64})$",
    )

    @field_validator(
        "analysis_digest",
        "analysis_record_path",
        "config_fingerprint",
        "instrument",
        "expected_account_identity",
        "okx_api_base_url",
        "leverage_intent_digest",
        "supervisor_record_id",
        "supervisor_record_path",
        "supervisor_record_digest",
        mode="before",
    )
    @classmethod
    def _strip_leverage_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_leverage_increase(self) -> SetLeverageParameters:
        if self.target_leverage <= self.current_leverage:
            raise ValueError("目标杠杆必须高于已确认的当前杠杆")
        if self.target_leverage > self.maximum_leverage:
            raise ValueError("目标杠杆超过 OKX 已确认最大杠杆")
        if self.current_capacity >= self.required_quantity:
            raise ValueError("当前容量已经足够，不应创建杠杆命令")
        if self.target_capacity < self.required_quantity:
            raise ValueError("目标杠杆容量不足以容纳风险目标数量")
        points = self.verified_grid
        if (
            points[0].leverage != self.current_leverage
            or points[0].capacity != self.current_capacity
            or points[-1].leverage != self.maximum_leverage
            or points[-1].capacity != self.maximum_capacity
        ):
            raise ValueError("容量验证网格首尾与杠杆计划不一致")
        for previous, current in zip(points, points[1:], strict=False):
            if current.leverage <= previous.leverage:
                raise ValueError("容量验证网格杠杆必须严格递增")
            if current.leverage - previous.leverage > self.policy_grid_step:
                raise ValueError("容量验证网格存在未验证的杠杆空档")
            if current.capacity < previous.capacity:
                raise ValueError("容量验证网格不是单调递增")
        sufficient = [
            point
            for point in points
            if point.capacity >= self.required_quantity
        ]
        if (
            not sufficient
            or sufficient[0].leverage != self.target_leverage
            or sufficient[0].capacity != self.target_capacity
        ):
            raise ValueError("目标杠杆不是策略网格内首个容量充足点")
        if not self.okx_api_base_url.startswith("https://"):
            raise ValueError("OKX API 地址必须使用 HTTPS")
        expected_intent_digest = leverage_intent_digest(self)
        if (
            self.leverage_intent_digest
            and self.leverage_intent_digest != expected_intent_digest
        ):
            raise ValueError("杠杆意图摘要与不可变参数不一致")
        object.__setattr__(
            self,
            "leverage_intent_digest",
            expected_intent_digest,
        )
        return self


def leverage_intent_payload(
    parameters: SetLeverageParameters,
) -> dict[str, object]:
    """Return the exact leverage facts that supervision must authorize."""
    return {
        "schema_version": 1,
        "analysis_record_path": parameters.analysis_record_path,
        "config_fingerprint": parameters.config_fingerprint,
        "instrument": parameters.instrument,
        "direction": parameters.direction,
        "margin_mode": parameters.margin_mode,
        "position_mode": parameters.position_mode,
        "current_leverage": str(parameters.current_leverage),
        "target_leverage": str(parameters.target_leverage),
        "current_capacity": str(parameters.current_capacity),
        "target_capacity": str(parameters.target_capacity),
        "maximum_leverage": str(parameters.maximum_leverage),
        "maximum_capacity": str(parameters.maximum_capacity),
        "planning_method": parameters.planning_method,
        "policy_grid_step": str(parameters.policy_grid_step),
        "verified_grid": [
            {
                "leverage": str(point.leverage),
                "capacity": str(point.capacity),
            }
            for point in parameters.verified_grid
        ],
        "required_quantity": str(parameters.required_quantity),
        "entry_price": str(parameters.entry_price),
        "expected_account_identity": parameters.expected_account_identity,
        "okx_api_base_url": parameters.okx_api_base_url,
    }


def leverage_intent_digest(parameters: SetLeverageParameters) -> str:
    encoded = json.dumps(
        leverage_intent_payload(parameters),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def leverage_intent_snapshot(
    parameters: SetLeverageParameters,
) -> dict[str, object]:
    return {
        **leverage_intent_payload(parameters),
        "leverage_intent_digest": parameters.leverage_intent_digest,
    }


class SetLeverageResult(BaseModel):
    """Broker read-back proving the leverage change and resulting capacity."""

    model_config = ConfigDict(extra="forbid")

    instrument: str = Field(min_length=1, max_length=128)
    confirmed_leverage: Decimal = Field(gt=0)
    confirmed_max_size: Decimal = Field(gt=0)
    broker_position_count: int = Field(ge=0)
    broker_pending_order_count: int = Field(ge=0)
    broker_pending_algo_order_count: int = Field(ge=0)
    account_identity: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    confirmed_at: datetime

    @field_validator("instrument", "account_identity", mode="before")
    @classmethod
    def _strip_result_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("confirmed_at")
    @classmethod
    def _normalise_confirmed_at(cls, value: datetime) -> datetime:
        return _utc(value)


class WorkerCommandResolutionEvidence(BaseModel):
    """Sanitized facts used to resolve one uncertain broker write."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1, max_length=256)
    command_action: Literal[
        "submit",
        "set_leverage",
        "cancel_entry",
        "request_exit",
    ]
    command_failure_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    broker: BrokerName
    environment: ExecutionEnvironment
    account: str = Field(min_length=1, max_length=256)
    instrument: str = Field(min_length=1, max_length=128)
    execution_state: Literal["blocked", "canceled", "rejected", "closed"]
    broker_order_id_present: bool
    client_order_id_present: bool
    filled_quantity: Decimal = Field(ge=0)
    event_kinds: tuple[str, ...] = Field(min_length=1, max_length=64)
    active_execution_count: int = Field(ge=0)
    new_risk_lease_present: bool
    broker_position_count: int = Field(ge=0)
    broker_pending_order_count: int = Field(ge=0)
    broker_pending_algo_order_count: int = Field(ge=0)
    broker_account_identity_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    observed_at: datetime

    @field_validator(
        "execution_id",
        "command_failure_code",
        "account",
        "instrument",
        "broker_account_identity_digest",
        mode="before",
    )
    @classmethod
    def _strip_evidence_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("event_kinds", mode="before")
    @classmethod
    def _normalise_event_kinds(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        return tuple(
            item.strip() if isinstance(item, str) else item
            for item in value
        )

    @field_validator("event_kinds")
    @classmethod
    def _validate_event_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item
            or len(item) > 128
            or not all(character.isalnum() or character in "_.:-" for character in item)
            for item in value
        ):
            raise ValueError("事件类型必须是简短安全代码")
        return value

    @field_validator("observed_at")
    @classmethod
    def _normalise_observed_at(cls, value: datetime) -> datetime:
        return _utc(value)


class SetLeverageResolutionEvidence(BaseModel):
    """Read-back facts that settle one uncertain leverage write."""

    model_config = ConfigDict(extra="forbid")

    analysis_digest: str = Field(min_length=64, max_length=64)
    command_action: Literal["set_leverage"]
    command_failure_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    broker: Literal["okx"]
    environment: Literal["demo"]
    account: str = Field(min_length=1, max_length=256)
    instrument: str = Field(min_length=1, max_length=128)
    target_leverage: Decimal = Field(gt=0)
    confirmed_leverage: Decimal = Field(gt=0)
    required_quantity: Decimal = Field(gt=0)
    confirmed_max_size: Decimal = Field(gt=0)
    active_execution_count: int = Field(ge=0)
    new_risk_lease_present: bool
    broker_position_count: int = Field(ge=0)
    broker_pending_order_count: int = Field(ge=0)
    broker_pending_algo_order_count: int = Field(ge=0)
    broker_account_identity_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    observed_at: datetime

    @field_validator(
        "analysis_digest",
        "command_failure_code",
        "account",
        "instrument",
        "broker_account_identity_digest",
        mode="before",
    )
    @classmethod
    def _strip_leverage_evidence_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("observed_at")
    @classmethod
    def _normalise_leverage_observed_at(
        cls,
        value: datetime,
    ) -> datetime:
        return _utc(value)


class WorkerCommandResolution(BaseModel):
    """Durable proof that an uncertain broker write was explicitly resolved."""

    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=128)
    resolution_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    evidence: (
        WorkerCommandResolutionEvidence
        | SetLeverageResolutionEvidence
    )
    evidence_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    resolved_by: str = Field(min_length=1, max_length=128)
    resolved_at: datetime

    @field_validator(
        "command_id",
        "resolution_code",
        "resolved_by",
        mode="before",
    )
    @classmethod
    def _strip_resolution_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("resolved_at")
    @classmethod
    def _normalise_resolution_time(cls, value: datetime) -> datetime:
        return _utc(value)


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
    parameters: SetLeverageParameters | None = None
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
    result: SetLeverageResult | None = None

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
        elif self.action is WorkerCommandAction.SET_LEVERAGE:
            if not self.new_risk_lease_id:
                raise ValueError("set_leverage 命令必须绑定 NEW_RISK 租约")
            if self.execution_id:
                raise ValueError("set_leverage 命令不得引用尚未创建的 execution")
        elif self.new_risk_lease_id:
            raise ValueError("只有新增风险命令可以绑定 NEW_RISK 租约")
        if self.action is WorkerCommandAction.REQUEST_EXIT:
            if not self.reason_code:
                raise ValueError("request_exit 命令必须提供 reason_code")
        elif self.reason_code:
            raise ValueError("只有 request_exit 命令可以提供 reason_code")
        if self.action is WorkerCommandAction.SET_LEVERAGE:
            if self.parameters is None:
                raise ValueError("set_leverage 命令必须提供严格参数")
            if not (
                self.parameters.analysis_record_path
                and self.parameters.supervisor_record_id
                and self.parameters.supervisor_record_path
                and self.parameters.supervisor_record_digest
            ):
                raise ValueError("set_leverage 命令缺少耐久分析或监督授权证据")
            if (
                self.status is WorkerCommandStatus.SUCCEEDED
                and self.result is None
            ):
                raise ValueError("成功的 set_leverage 命令必须保存回读结果")
        elif self.parameters is not None or self.result is not None:
            raise ValueError("只有 set_leverage 命令可以保存杠杆参数或结果")
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
