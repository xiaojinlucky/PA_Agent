"""Application-level execution coordinator, monitor and live-session gate."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pa_agent.execution.base import BrokerAdapter
from pa_agent.execution.credentials import (
    hard_live_gate_enabled,
    load_longbridge_account_credentials,
    load_okx_credentials,
    okx_live_gate_enabled,
    paper_trading_gate_enabled,
)
from pa_agent.execution.errors import (
    BrokerApiError,
    BrokerRejected,
    BrokerTransportError,
    CredentialError,
    LiveTradingDisabled,
    PlanBlocked,
    PreflightError,
    SubmissionUnknown,
)
from pa_agent.execution.leverage_authorization import (
    LeverageAuthorizationError,
    validate_current_leverage_policy,
    validate_leverage_authorization,
)
from pa_agent.execution.longbridge_adapter import LongbridgeAdapter
from pa_agent.execution.longbridge_session import LongbridgeSession
from pa_agent.execution.models import (
    AccountSnapshot,
    ExecutionPlan,
    ExecutionRecord,
    ExecutionState,
    PreflightResult,
    utc_now_iso,
)
from pa_agent.execution.okx_adapter import OkxAdapter
from pa_agent.execution.okx_client import OkxRestClient
from pa_agent.execution.plan_builder import (
    build_execution_plan,
    execution_route_fingerprint,
)
from pa_agent.execution.store import ExecutionStore
from pa_agent.execution.worker_protocol import (
    SetLeverageResult,
    WorkerCommand,
    WorkerCommandAction,
)
from pa_agent.risk.runtime import (
    RiskRuntime,
    RiskRuntimeBlocked,
    RiskRuntimeState,
    route_key,
)
from pa_agent.risk.sizing import RiskCalculationFailure
from pa_agent.safety_defaults import new_risk_route_supported

_ARM_CONFIRMATION = "启用实盘交易"
_PAPER_ARM_CONFIRMATION = "启用模拟交易"
_NEW_RISK = "new_risk"
_RISK_REDUCING = "risk_reducing"
_OKX_DEMO_CAMPAIGN_API_BASE_URL = "https://www.okx.com"
_OKX_DEMO_CAMPAIGN_INSTRUMENT = "XAU-USDT-SWAP"
_IDLE_ACCOUNT_REFRESH_INTERVAL_SECONDS = 60.0
_RiskKind = Literal["new_risk", "risk_reducing"]


@dataclass(frozen=True)
class _LeverageWritePlan:
    id: str
    broker: Literal["okx"]
    environment: Literal["demo"]
    requested_account: Literal["okx"]
    config_fingerprint: str
    product: Literal["swap"]
    instrument: str


class ExecutionService:
    """Owns broker adapters and guarantees intent persistence before writes."""

    def __init__(
        self,
        *,
        settings,
        pending_writer,
        event_bus=None,
        store: ExecutionStore | None = None,
        adapter_factories: dict[
            str, Callable[[ExecutionPlan], BrokerAdapter]
        ] | None = None,
        leverage_adapter_factory: Callable[[WorkerCommand], OkxAdapter]
        | None = None,
        risk_runtime: RiskRuntime | None = None,
        gate_checker: Callable[[], bool] | None = None,
        paper_gate_checker: Callable[[], bool] | None = None,
        okx_live_gate_checker: Callable[[], bool] | None = None,
        new_risk_authorizer: Callable[[ExecutionPlan, str], bool] | None = None,
        new_risk_revoker: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._pending_writer = pending_writer
        self._event_bus = event_bus
        self._store = store or ExecutionStore()
        self._adapter_factories = adapter_factories or {}
        self._leverage_adapter_factory = leverage_adapter_factory
        self._risk_runtime = risk_runtime
        self._gate_checker = gate_checker or hard_live_gate_enabled
        self._paper_gate_checker = (
            paper_gate_checker or paper_trading_gate_enabled
        )
        self._okx_live_gate_checker = (
            okx_live_gate_checker or okx_live_gate_enabled
        )
        self._new_risk_authorizer = new_risk_authorizer
        self._new_risk_revoker = new_risk_revoker
        self._logger = logger or logging.getLogger(__name__)
        self._adapters: dict[tuple[str, ...], BrokerAdapter] = {}
        self._armed = False
        self._armed_broker: str | None = None
        self._armed_environment: str | None = None
        self._armed_account: str | None = None
        self._lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._write_scope = threading.local()
        self._runtime_writes_blocked = False
        self._risk_reducing_writes_blocked = False
        self._runtime_id = str(uuid.uuid4())
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._monotonic_clock = time.monotonic
        self._idle_account_next_refresh: dict[
            tuple[str, str, str, str], float
        ] = {}
        self._idle_account_refresh_in_progress: set[
            tuple[str, str, str, str]
        ] = set()

    @property
    def store(self) -> ExecutionStore:
        return self._store

    @property
    def is_armed(self) -> bool:
        with self._write_lock:
            return self._armed and self._armed_gate_enabled()

    def _selected_route_identity(self) -> tuple[str, str, str]:
        execution = self._settings.execution
        broker = str(execution.selected_broker)
        if broker == "longbridge":
            account = str(execution.longbridge.preferred_account)
            environment = "demo" if account == "paper" else "live"
            return broker, environment, account
        environment = "demo" if bool(execution.okx.simulated) else "live"
        return broker, environment, "okx"

    def _armed_gate_enabled(self) -> bool:
        if not self._armed:
            return False
        if self._armed_environment == "demo":
            return bool(self._paper_gate_checker())
        return bool(self._gate_checker())

    def arm_confirmation_text(self) -> str:
        _broker, environment, _account = self._selected_route_identity()
        if environment == "demo":
            return _PAPER_ARM_CONFIRMATION
        return _ARM_CONFIRMATION

    def arm(self, confirmation: str) -> None:
        with self._write_lock:
            expected = self.arm_confirmation_text()
            if confirmation.strip() != expected:
                raise LiveTradingDisabled("交易启用确认文字不匹配")
            if not bool(self._settings.execution.enabled):
                raise LiveTradingDisabled("执行模块尚未在 PA 配置中启用")
            broker, environment, account = self._selected_route_identity()
            if environment == "demo":
                if not self._paper_gate_checker():
                    raise LiveTradingDisabled(
                        "共享 env 中 PA_AGENT_PAPER_TRADING_ENABLED 不是 true"
                    )
            elif not self._gate_checker():
                raise LiveTradingDisabled(
                    "共享 env 中 PA_AGENT_LIVE_TRADING_ENABLED 不是 true"
                )
            if (
                broker == "okx"
                and environment == "live"
                and not self._okx_live_gate_checker()
            ):
                raise LiveTradingDisabled("共享 env 中 OKX_LIVE_ENABLED 不是 true")
            self._armed = True
            self._armed_broker = broker
            self._armed_environment = environment
            self._armed_account = account
            self._emit_armed()

    def disarm(self, *, revoke_external: bool = True) -> None:
        with self._write_lock:
            self._armed = False
            self._armed_broker = None
            self._armed_environment = None
            self._armed_account = None
            if revoke_external and self._new_risk_revoker is not None:
                self._new_risk_revoker()
            self._emit_armed()

    def _invalidate_runtime_after_uncertain_local_failure(self) -> None:
        """Stop writes and make persisted in-flight markers belong to an old runtime."""
        with self._write_lock:
            self._armed = False
            self._armed_broker = None
            self._armed_environment = None
            self._armed_account = None
            self._runtime_writes_blocked = True
            self._risk_reducing_writes_blocked = True
            self._runtime_id = str(uuid.uuid4())
            self._adapters.clear()
            if self._new_risk_revoker is not None:
                self._new_risk_revoker()
            self._emit_armed()

    def reload_settings(
        self,
        settings=None,
        *,
        revoke_new_risk: bool = True,
    ) -> None:
        """Apply saved non-secret route settings and invalidate broker clients."""
        with self._lock:
            if settings is not None:
                self._settings = settings
            self._adapters.clear()
            self._idle_account_next_refresh.clear()
            self._idle_account_refresh_in_progress.clear()
            self.disarm(revoke_external=revoke_new_risk)

    def _emit_armed(self) -> None:
        if self._event_bus is not None and hasattr(
            self._event_bus, "emit_execution_armed"
        ):
            self._event_bus.emit_execution_armed(self.is_armed)

    def _emit_record(self, record: ExecutionRecord) -> None:
        if self._event_bus is not None and hasattr(
            self._event_bus, "emit_execution_update"
        ):
            self._event_bus.emit_execution_update(record)

    def _emit_account(self, snapshot: AccountSnapshot) -> None:
        if self._event_bus is not None and hasattr(
            self._event_bus, "emit_account_update"
        ):
            self._event_bus.emit_account_update(snapshot)

    def _emit_error(self, message: str) -> None:
        self._logger.warning("Execution: %s", message)
        if self._event_bus is not None and hasattr(
            self._event_bus, "emit_execution_error"
        ):
            self._event_bus.emit_execution_error(message)

    def _execute_broker_write(
        self,
        plan: ExecutionPlan,
        operation: Callable[[], Any],
        *,
        risk_kind: _RiskKind | None = None,
        effective_account: str | None = None,
    ) -> Any:
        """Linearize every broker write against session disarm/route changes."""
        with self._write_lock:
            effective_kind = risk_kind or getattr(
                self._write_scope,
                "risk_kind",
                _NEW_RISK,
            )
            actual_account = (
                str(effective_account or "").strip()
                or str(
                    getattr(self._write_scope, "effective_account", "")
                ).strip()
                or plan.requested_account
            )
            self._require_plan_writes(
                plan,
                risk_kind=effective_kind,
                effective_account=actual_account,
            )
            previous_account = getattr(
                self._write_scope,
                "effective_account",
                None,
            )
            self._write_scope.effective_account = actual_account
            try:
                return operation()
            finally:
                if previous_account is None:
                    try:
                        del self._write_scope.effective_account
                    except AttributeError:
                        pass
                else:
                    self._write_scope.effective_account = previous_account

    @contextmanager
    def _broker_write_scope(self, risk_kind: _RiskKind):
        """Apply one permission class to writes nested inside an adapter call."""
        previous = getattr(self._write_scope, "risk_kind", None)
        self._write_scope.risk_kind = risk_kind
        try:
            yield
        finally:
            if previous is None:
                try:
                    del self._write_scope.risk_kind
                except AttributeError:
                    pass
            else:
                self._write_scope.risk_kind = previous

    def _save_after_possible_write(
        self,
        record: ExecutionRecord,
        *,
        event_kind: str,
        event_payload: dict | None = None,
    ) -> ExecutionRecord:
        """Fail closed when a broker result cannot be durably recorded."""
        try:
            return self._store.save(
                record,
                event_kind=event_kind,
                event_payload=event_payload,
            )
        except Exception:
            self._invalidate_runtime_after_uncertain_local_failure()
            raise

    def _adapter(self, plan: ExecutionPlan) -> BrokerAdapter:
        cache_key = (
            plan.id,
            plan.broker,
            plan.environment,
            plan.requested_account,
            plan.product,
            plan.instrument,
            plan.okx_api_base_url,
            plan.config_fingerprint,
        )
        cached = self._adapters.get(cache_key)
        if cached is not None:
            return cached
        custom = self._adapter_factories.get(plan.broker)
        if custom is not None:
            adapter = custom(plan)
        elif plan.broker == "okx":
            if not plan.okx_api_base_url or not plan.okx_margin_mode:
                raise PreflightError(
                    "旧 OKX 执行缺少不可变路由快照，禁止连接或写入"
                )
            client = OkxRestClient(
                load_okx_credentials(plan.environment),
                base_url=plan.okx_api_base_url,
                simulated=plan.environment == "demo",
            )
            adapter = OkxAdapter(
                client,
                margin_mode=plan.okx_margin_mode,
                entry_timeout_seconds=plan.entry_timeout_seconds,
                runtime_id=self._runtime_id,
                write_executor=lambda operation: self._execute_broker_write(
                    plan,
                    operation,
                ),
            )
        elif plan.broker == "longbridge":
            def _session_factory(profile: str):
                return LongbridgeSession(
                    load_longbridge_account_credentials(profile)
                )

            adapter = LongbridgeAdapter(
                _session_factory,
                allow_outside_rth=plan.longbridge_allow_outside_rth,
                entry_timeout_seconds=plan.entry_timeout_seconds,
                runtime_id=self._runtime_id,
                write_executor=lambda operation: self._execute_broker_write(
                    plan,
                    operation,
                ),
            )
        else:
            raise PreflightError(f"未知执行券商：{plan.broker}")
        bind_runtime = getattr(adapter, "bind_runtime_id", None)
        if callable(bind_runtime):
            bind_runtime(self._runtime_id)
        bind_write = getattr(adapter, "bind_write_executor", None)
        if callable(bind_write):
            bind_write(
                lambda operation: self._execute_broker_write(plan, operation)
            )
        self._adapters[cache_key] = adapter
        return adapter

    @staticmethod
    def _risk_route_key(plan: ExecutionPlan) -> str:
        return route_key(
            broker=plan.broker,
            environment=plan.environment,
            account=plan.requested_account,
        )

    @staticmethod
    def _risk_failure_code(exc: BaseException) -> str:
        raw = str(getattr(exc, "code", "") or type(exc).__name__)
        normalized = "".join(
            character if character.isalnum() or character in "_.:-" else "_"
            for character in raw
        ).strip("_")
        return (normalized or "read_failed")[:96]

    def _refresh_risk_runtime(
        self,
        plan: ExecutionPlan | _LeverageWritePlan,
        adapter: BrokerAdapter,
        *,
        snapshot: AccountSnapshot | None = None,
        raise_on_failure: bool,
    ) -> tuple[RiskRuntimeState | None, AccountSnapshot | None]:
        if self._risk_runtime is None or plan.broker != "okx":
            return None, snapshot
        account_snapshot = snapshot
        try:
            account_snapshot = account_snapshot or adapter.account_snapshot(plan)
            total_equity = account_snapshot.raw_summary.get(
                "account_total_equity",
                "",
            )
            identity = adapter.account_identity(plan)
            read_bills = getattr(adapter, "account_bills", None)
            if not callable(read_bills):
                raise RuntimeError("OKX adapter 缺少资金账单读取能力")
            state = self._risk_runtime.refresh(
                broker=plan.broker,
                environment=plan.environment,
                account=plan.requested_account,
                account_identity=identity,
                total_equity_usd=total_equity,
                bill_rows=read_bills(),
            )
        except Exception as exc:
            code = f"risk_runtime_{self._risk_failure_code(exc)}"
            state = self._risk_runtime.mark_failure(
                broker=plan.broker,
                environment=plan.environment,
                account=plan.requested_account,
                reason=code,
            )
            if raise_on_failure:
                raise RiskRuntimeBlocked(
                    state.kill_reason,
                    "账户总权益或资金流水读取失败, 已关闭新增风险",
                ) from exc
            self._emit_error(
                f"{plan.broker}/{plan.environment}/{plan.requested_account} "
                f"风险运行态刷新失败: {code}"
            )
            return state, account_snapshot
        return state, account_snapshot

    def _prepare_new_risk_record(
        self,
        record: ExecutionRecord,
        adapter: BrokerAdapter,
    ) -> tuple[ExecutionRecord, BrokerAdapter]:
        """在券商预检前刷新回撤闸门并按固定资本上限定仓。"""

        if self._risk_runtime is None or record.plan.broker != "okx":
            return record, adapter
        state, account_snapshot = self._refresh_risk_runtime(
            record.plan,
            adapter,
            raise_on_failure=True,
        )
        assert state is not None
        assert account_snapshot is not None
        state = self._risk_runtime.require_new_risk(
            self._risk_route_key(record.plan)
        )
        calculate_size = getattr(adapter, "calculate_risk_size", None)
        if not callable(calculate_size):
            raise RiskRuntimeBlocked(
                "risk_sizing_unavailable",
                "OKX 执行适配器缺少通用风险定仓能力",
            )
        risk_equity = account_snapshot.equity
        if (
            risk_equity is None
            or not risk_equity.is_finite()
            or risk_equity <= 0
        ):
            raise RiskRuntimeBlocked(
                "risk_sizing_equity_unavailable",
                "OKX 结算币权益无效，禁止新增风险",
            )
        sizing = calculate_size(
            record.plan,
            account_equity_usd=risk_equity,
            risk_capital_cap_usdt=(
                self._settings.execution.okx.risk_capital_cap_usdt
            ),
            risk_percent=self._settings.execution.okx.risk_percent,
        )
        if sizing is None:
            return record, adapter
        authorized_risk_values = (
            record.plan.authorized_risk_capital_cap_usdt,
            record.plan.authorized_effective_risk_capital_usdt,
            record.plan.authorized_risk_percent,
            record.plan.authorized_risk_budget_usdt,
            record.plan.authorized_risk_used_usdt,
            record.plan.authorized_contract_notional_usdt,
            record.plan.authorized_worst_case_loss_per_contract_usdt,
        )
        if (
            record.plan.risk_equity_basis
            != "fixed_cap_or_usdt_equity_whichever_lower"
            or any(value is None for value in authorized_risk_values)
            or (
                record.plan.authorized_sizing_mode == "fixed_quantity"
                and record.plan.authorized_fixed_quantity
                != record.plan.quantity
            )
        ):
            raise RiskRuntimeBlocked(
                "risk_sizing_authorization_missing",
                "耐久计划缺少固定资本风险授权快照",
            )
        if (
            sizing.risk_capital_cap_usdt
            != record.plan.authorized_risk_capital_cap_usdt
            or sizing.effective_risk_capital_usdt
            != record.plan.authorized_effective_risk_capital_usdt
            or sizing.risk_percent != record.plan.authorized_risk_percent
            or sizing.risk_budget_usdt
            != record.plan.authorized_risk_budget_usdt
            or sizing.risk_used_usdt
            != record.plan.authorized_risk_used_usdt
            or sizing.contract_notional_usdt
            != record.plan.authorized_contract_notional_usdt
            or sizing.worst_case_loss_per_contract_usdt
            != (
                record.plan.authorized_worst_case_loss_per_contract_usdt
            )
            or sizing.target_contract_size != record.plan.quantity
        ):
            raise RiskRuntimeBlocked(
                "risk_sizing_changed_after_authorization",
                "提交前有效风险资本、预算、单张损失或数量已变化，"
                "旧脚本计划作废",
            )
        updated_plan = record.plan.model_copy(
            update={"quantity": sizing.target_contract_size}
        )
        updated_record = record.model_copy(
            update={
                "plan": updated_plan,
                "remaining_quantity": sizing.target_contract_size,
                "broker_state": {
                    **record.broker_state,
                    "risk_sizing": {
                        "sizing_mode": record.plan.authorized_sizing_mode,
                        "equity_basis": record.plan.risk_equity_basis,
                        "account_equity_usdt": str(
                            sizing.account_equity_usdt
                        ),
                        "account_total_equity_usd": str(
                            state.last_total_equity_usd
                        ),
                        "risk_capital_cap_usdt": str(
                            sizing.risk_capital_cap_usdt
                        ),
                        "effective_risk_capital_usdt": str(
                            sizing.effective_risk_capital_usdt
                        ),
                        "risk_percent": str(sizing.risk_percent),
                        "target_contract_size": str(
                            sizing.target_contract_size
                        ),
                        "risk_budget_usdt": str(sizing.risk_budget_usdt),
                        "risk_used_usdt": str(sizing.risk_used_usdt),
                        "stop_distance_usdt": str(sizing.stop_distance_usdt),
                        "worst_case_loss_per_contract_usdt": str(
                            sizing.worst_case_loss_per_contract_usdt
                        ),
                        "maximum_size": str(sizing.maximum_size),
                    },
                },
            }
        )
        saved = self._store.save(
            updated_record,
            event_kind="risk_sizing_calculated",
            event_payload={
                "equity_basis": record.plan.risk_equity_basis,
                "account_equity_usdt": str(sizing.account_equity_usdt),
                "account_total_equity_usd": str(
                    state.last_total_equity_usd
                ),
                "risk_capital_cap_usdt": str(
                    sizing.risk_capital_cap_usdt
                ),
                "effective_risk_capital_usdt": str(
                    sizing.effective_risk_capital_usdt
                ),
                "risk_percent": str(sizing.risk_percent),
                "target_contract_size": str(sizing.target_contract_size),
                "maximum_size": str(sizing.maximum_size),
            },
        )
        self._emit_record(saved)
        return saved, adapter

    @staticmethod
    def _validate_leverage_command(command: WorkerCommand):
        parameters = command.parameters
        if (
            command.action is not WorkerCommandAction.SET_LEVERAGE
            or parameters is None
            or command.broker != "okx"
            or command.environment != "demo"
            or command.account != "okx"
        ):
            raise PreflightError("只支持严格建模的 OKX Demo 杠杆命令")
        return parameters

    def _leverage_adapter(self, command: WorkerCommand) -> OkxAdapter:
        parameters = self._validate_leverage_command(command)
        if self._leverage_adapter_factory is not None:
            return self._leverage_adapter_factory(command)
        client = OkxRestClient(
            load_okx_credentials(command.environment),
            base_url=parameters.okx_api_base_url,
            simulated=True,
        )
        return OkxAdapter(
            client,
            margin_mode=parameters.margin_mode,
            runtime_id=self._runtime_id,
        )

    def reconcile_leverage(
        self,
        command: WorkerCommand,
    ) -> SetLeverageResult:
        """Read current Demo leverage/capacity without broker writes."""
        parameters = self._validate_leverage_command(command)
        if self._store.list_active():
            raise PreflightError("存在活动 execution，禁止调整杠杆")
        return self._leverage_adapter(command).read_leverage_state(
            parameters,
            environment=command.environment,
        )

    def set_leverage(
        self,
        command: WorkerCommand,
    ) -> SetLeverageResult:
        """Execute one Demo leverage command through the OKX adapter."""
        parameters = self._validate_leverage_command(command)
        if self._store.list_active():
            raise PreflightError("存在活动 execution，禁止调整杠杆")
        try:
            validate_current_leverage_policy(parameters, self._settings)
            validate_leverage_authorization(parameters)
        except LeverageAuthorizationError as exc:
            raise PreflightError(
                f"杠杆命令没有匹配的耐久脚本授权：{exc}"
            ) from exc
        plan = _LeverageWritePlan(
            id=parameters.analysis_digest,
            broker="okx",
            environment="demo",
            requested_account="okx",
            config_fingerprint=parameters.config_fingerprint,
            product="swap",
            instrument=parameters.instrument,
        )
        self._require_plan_writes(plan)
        adapter = self._leverage_adapter(command)
        if self._risk_runtime is not None:
            state, _snapshot = self._refresh_risk_runtime(
                plan,
                adapter,
                raise_on_failure=True,
            )
            assert state is not None
            self._risk_runtime.require_new_risk(
                self._risk_route_key(plan)
            )
        adapter.bind_write_executor(
            lambda operation: self._execute_broker_write(
                plan,
                operation,
                effective_account=command.account,
            )
        )
        return adapter.set_leverage(
            parameters,
            environment=command.environment,
            maximum_leverage_cap=(
                self._settings.execution.okx.maximum_leverage
            ),
        )

    def _require_current_leverage_within_user_cap(
        self,
        record: ExecutionRecord,
        preflight: PreflightResult,
    ) -> None:
        """在入场 POST 前用 Worker 当前配置核对券商实际杠杆。"""

        if record.plan.broker != "okx" or record.plan.product != "swap":
            return
        raw_leverage = preflight.broker_metadata.get("current_leverage")
        try:
            current_leverage = Decimal(str(raw_leverage))
            maximum_leverage = Decimal(
                str(self._settings.execution.okx.maximum_leverage)
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PreflightError(
                "OKX 当前杠杆或用户最大杠杆无效，禁止新增风险"
            ) from exc
        if (
            not current_leverage.is_finite()
            or current_leverage <= 0
            or not maximum_leverage.is_finite()
            or maximum_leverage <= 0
        ):
            raise PreflightError(
                "OKX 当前杠杆或用户最大杠杆无效，禁止新增风险"
            )
        if current_leverage > maximum_leverage:
            raise PreflightError(
                "OKX 当前杠杆高于用户设置上限，禁止新增风险"
            )

    @staticmethod
    def _expected_account_identity(record: ExecutionRecord) -> str:
        identity = str(record.account_identity or "").strip()
        if not identity:
            raise CredentialError(
                "活动执行缺少实际账户身份指纹，禁止连接或恢复写操作"
            )
        return identity

    def _verify_account_identity(
        self,
        adapter: BrokerAdapter,
        record: ExecutionRecord,
    ) -> None:
        try:
            expected = self._expected_account_identity(record)
            actual = adapter.account_identity(
                record.plan,
                account_profile=record.selected_account or None,
            )
        except (
            CredentialError,
            PreflightError,
            BrokerApiError,
            BrokerTransportError,
        ) as exc:
            self.disarm()
            raise CredentialError(
                f"无法验证活动执行的实际账户身份：{exc}"
            ) from exc
        if actual != expected:
            self.disarm()
            raise CredentialError(
                "当前凭据对应的实际账户与活动执行不一致，已阻断订单和账户读取"
            )

    def prepare_analysis(
        self,
        record,
        *,
        is_demo_replay: bool = False,
    ) -> ExecutionRecord:
        path = Path(self._pending_writer.full_path(record))
        plan = build_execution_plan(
            record,
            self._settings,
            record_path=path,
            is_demo_replay=is_demo_replay,
        )
        execution, created = self._store.create(plan)
        self._emit_record(execution)
        codex_live_requires_review = False
        if plan.environment == "live":
            from pa_agent.ai.provider_capabilities import resolve_provider_capability

            codex_live_requires_review = (
                resolve_provider_capability(self._settings.provider).client_kind
                == "codex_cli"
            )
            if created and codex_live_requires_review:
                self._store.append_event(
                    execution.id,
                    "human_review_required",
                    {
                        "reason": "codex_subscription_live_trade",
                        "auto_execute_blocked": True,
                    },
                )
        if (
            created
            and bool(self._settings.execution.auto_execute)
            and self.is_armed
            and not codex_live_requires_review
        ):
            return self.submit(execution.id)
        return execution

    def _require_writes(self) -> None:
        if not bool(self._settings.execution.enabled):
            self._armed = False
            self._emit_armed()
            raise LiveTradingDisabled("执行模块尚未在 PA 配置中启用")
        if not self._armed:
            raise LiveTradingDisabled("本次 PA 会话尚未启用交易写操作")
        if not self._armed_gate_enabled():
            paper = self._armed_environment == "demo"
            self.disarm()
            gate = (
                "PA_AGENT_PAPER_TRADING_ENABLED"
                if paper
                else "PA_AGENT_LIVE_TRADING_ENABLED"
            )
            raise LiveTradingDisabled(f"{gate} 未开启，禁止券商写操作")

    def _hard_plan_gate_enabled(self, plan: ExecutionPlan) -> bool:
        if plan.environment == "demo":
            return bool(self._paper_gate_checker())
        if not self._gate_checker():
            return False
        if plan.broker == "okx":
            return bool(self._okx_live_gate_checker())
        return True

    def _require_hard_plan_gate(self, plan: ExecutionPlan) -> None:
        if plan.environment == "demo":
            if not self._paper_gate_checker():
                raise LiveTradingDisabled(
                    "PA_AGENT_PAPER_TRADING_ENABLED 未开启，禁止模拟券商写操作"
                )
            return
        if not self._gate_checker():
            raise LiveTradingDisabled(
                "PA_AGENT_LIVE_TRADING_ENABLED 未开启，禁止实盘券商写操作"
            )
        if plan.broker == "okx" and not self._okx_live_gate_checker():
            raise LiveTradingDisabled(
                "OKX Live 写操作还需要 OKX_LIVE_ENABLED=true"
            )

    def _plan_writes_enabled(
        self,
        plan: ExecutionPlan,
        *,
        risk_kind: _RiskKind = _NEW_RISK,
        effective_account: str | None = None,
    ) -> bool:
        if self._runtime_writes_blocked:
            return False
        if risk_kind == _RISK_REDUCING:
            return (
                not self._risk_reducing_writes_blocked
                and self._hard_plan_gate_enabled(plan)
            )
        if (
            self._new_risk_authorizer is not None
            and not new_risk_route_supported(
                plan.broker,
                plan.environment,
            )
        ):
            return False
        if self._new_risk_authorizer is not None:
            if not self._hard_plan_gate_enabled(plan):
                return False
            try:
                return bool(
                    self._new_risk_authorizer(
                        plan,
                        str(effective_account or plan.requested_account),
                    )
                )
            except Exception:  # noqa: BLE001
                return False
        if not self.is_armed:
            return False
        if self._armed_broker != plan.broker:
            return False
        if self._armed_environment != plan.environment:
            return False
        if self._armed_account != plan.requested_account:
            return False
        if plan.broker == "okx" and plan.environment == "live":
            return bool(self._okx_live_gate_checker())
        return True

    def _require_plan_writes(
        self,
        plan: ExecutionPlan,
        *,
        risk_kind: _RiskKind = _NEW_RISK,
        effective_account: str | None = None,
    ) -> None:
        if self._runtime_writes_blocked:
            raise LiveTradingDisabled(
                "当前执行进程发生过无法确认的本地落盘故障，重启并完成对账前禁止券商写操作"
            )
        if risk_kind == _RISK_REDUCING:
            if self._risk_reducing_writes_blocked:
                raise LiveTradingDisabled(
                    "减险写操作此前出现拒绝或未知结果，重启并完成只读对账前禁止再次写入"
                )
            self._require_hard_plan_gate(plan)
            return
        if (
            self._new_risk_authorizer is not None
            and not new_risk_route_supported(
                plan.broker,
                plan.environment,
            )
        ):
            raise LiveTradingDisabled(
                "PA_Agent v0.1.0 只允许 OKX Demo 新增风险；"
                "OKX Live 和 Longbridge 交易不在本版本范围内"
            )
        if self._new_risk_authorizer is not None:
            self._require_hard_plan_gate(plan)
            try:
                authorized = bool(
                    self._new_risk_authorizer(
                        plan,
                        str(effective_account or plan.requested_account),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                raise LiveTradingDisabled(
                    f"无法核验新增风险授权租约：{exc}"
                ) from exc
            if not authorized:
                raise LiveTradingDisabled(
                    "新增风险授权租约不存在、已过期或与执行账户不一致"
                )
            return
        self._require_writes()
        if self._armed_broker != plan.broker:
            armed_broker = self._armed_broker or "无"
            self.disarm()
            raise LiveTradingDisabled(
                f"本次会话只启用了 {armed_broker}，不能写入 {plan.broker}"
            )
        if not self._plan_writes_enabled(plan, risk_kind=_NEW_RISK):
            okx_live_gate_missing = (
                plan.broker == "okx"
                and plan.environment == "live"
                and not self._okx_live_gate_checker()
            )
            self.disarm()
            if okx_live_gate_missing:
                raise LiveTradingDisabled(
                    "OKX Live 写操作还需要 OKX_LIVE_ENABLED=true"
                )
            raise LiveTradingDisabled("本次会话启用的账户或环境与执行计划不一致")

    def _active_conflict(
        self,
        record: ExecutionRecord,
        *,
        selected_account: str,
        account_identity: str,
    ) -> ExecutionRecord | None:
        for active in self._store.list_active():
            if active.id == record.id:
                continue
            if active.account_identity and account_identity:
                same_account = active.account_identity == account_identity
            else:
                # Legacy active records have no concrete fingerprint; fail closed
                # for the same logical profile until they are resolved.
                same_account = active.selected_account == selected_account
            if (
                active.plan.broker == record.plan.broker
                and active.plan.instrument == record.plan.instrument
                and same_account
            ):
                return active
        return None

    def submit(self, execution_id: str) -> ExecutionRecord:
        with self._lock:
            record = self._store.get(execution_id)
            if record is None:
                raise KeyError(f"未知 execution id: {execution_id}")
            self._require_plan_writes(record.plan)
            if record.state != ExecutionState.READY:
                raise PreflightError(f"当前状态 {record.state.value} 不能重复提交")
            # GUI 模式没有外部租约，仍以当前保存配置阻断旧计划。
            # 独立 Worker 则以不可变 plan + 短租约为唯一授权来源，不能错误地
            # 用 Worker 日常配置否决 campaign 的隔离路由。
            if self._new_risk_authorizer is None:
                try:
                    current_fingerprint = execution_route_fingerprint(
                        self._settings,
                        record.plan.broker,
                    )
                except (PlanBlocked, ValueError) as exc:
                    current_fingerprint = ""
                    route_error = str(exc)
                else:
                    route_error = ""
                if current_fingerprint != record.plan.config_fingerprint:
                    blocked = record.model_copy(
                        update={
                            "state": ExecutionState.BLOCKED,
                            "state_reason": "执行配置已变化，旧计划禁止提交",
                            "last_error": route_error or "route fingerprint mismatch",
                            "needs_attention": True,
                        }
                    )
                    blocked = self._store.save(
                        blocked,
                        event_kind="stale_plan_blocked",
                    )
                    self._emit_record(blocked)
                    return blocked
            try:
                adapter = self._adapter(record.plan)
                record, adapter = self._prepare_new_risk_record(
                    record,
                    adapter,
                )
            except RiskRuntimeBlocked as exc:
                risk_state = (
                    self._risk_runtime.get(
                        self._risk_route_key(record.plan)
                    )
                    if self._risk_runtime is not None
                    else None
                )
                blocked = record.model_copy(
                    update={
                        "state": ExecutionState.BLOCKED,
                        "state_reason": "资金流/回撤风险闸门阻断新增风险",
                        "last_error": exc.code,
                        "needs_attention": True,
                    }
                )
                blocked = self._store.save(
                    blocked,
                    event_kind="risk_runtime_blocked",
                    event_payload={
                        "code": exc.code,
                        "drawdown_fraction": (
                            str(risk_state.drawdown_fraction)
                            if risk_state is not None
                            and risk_state.drawdown_fraction is not None
                            else ""
                        ),
                        "adjusted_high_water": (
                            str(risk_state.adjusted_high_water_usd)
                            if risk_state is not None
                            and risk_state.adjusted_high_water_usd is not None
                            else ""
                        ),
                    },
                )
                self._emit_record(blocked)
                return blocked
            except RiskCalculationFailure as exc:
                blocked = record.model_copy(
                    update={
                        "state": ExecutionState.BLOCKED,
                        "state_reason": "通用风险定仓未通过",
                        "last_error": exc.code,
                        "needs_attention": True,
                    }
                )
                blocked = self._store.save(
                    blocked,
                    event_kind="risk_sizing_blocked",
                    event_payload={
                        "code": exc.code,
                        "required_size": (
                            str(exc.required_size)
                            if exc.required_size is not None
                            else ""
                        ),
                        "maximum_size": (
                            str(exc.maximum_size)
                            if exc.maximum_size is not None
                            else ""
                        ),
                    },
                )
                self._emit_record(blocked)
                return blocked
            try:
                preflight = adapter.preflight(record.plan)
                self._require_current_leverage_within_user_cap(
                    record,
                    preflight,
                )
            except (
                PreflightError,
                CredentialError,
                BrokerApiError,
                BrokerTransportError,
            ) as exc:
                blocked = record.model_copy(
                    update={
                        "state": ExecutionState.BLOCKED,
                        "state_reason": "券商只读预检未通过",
                        "last_error": str(exc),
                        "needs_attention": True,
                    }
                )
                blocked = self._store.save(
                    blocked,
                    event_kind="preflight_blocked",
                    event_payload={"error": str(exc)},
                )
                self._emit_record(blocked)
                return blocked
            account_identity = str(preflight.account_identity or "").strip()
            if not account_identity:
                blocked = record.model_copy(
                    update={
                        "state": ExecutionState.BLOCKED,
                        "selected_account": preflight.selected_account,
                        "preflight": preflight,
                        "state_reason": "券商预检未返回实际账户身份",
                        "last_error": "account identity missing",
                        "needs_attention": True,
                    }
                )
                blocked = self._store.save(
                    blocked,
                    event_kind="account_identity_blocked",
                )
                self._emit_record(blocked)
                return blocked

            conflict = self._active_conflict(
                record,
                selected_account=preflight.selected_account,
                account_identity=account_identity,
            )
            if conflict is not None:
                blocked = record.model_copy(
                    update={
                        "state": ExecutionState.BLOCKED,
                        "selected_account": preflight.selected_account,
                        "preflight": preflight,
                        "state_reason": "同一账户和品种已有活动执行",
                        "last_error": f"冲突 execution: {conflict.id}",
                        "needs_attention": True,
                    }
                )
                blocked = self._store.save(
                    blocked,
                    event_kind="active_execution_conflict",
                    event_payload={"conflict_execution_id": conflict.id},
                )
                self._emit_record(blocked)
                return blocked

            claimed_record = record.model_copy(
                update={
                    "selected_account": preflight.selected_account,
                    "account_identity": account_identity,
                    "preflight": preflight,
                }
            )
            conflict = self._store.acquire_route_claim(
                claimed_record,
                account_identity=account_identity,
            )
            if conflict is not None:
                blocked = claimed_record.model_copy(
                    update={
                        "state": ExecutionState.BLOCKED,
                        "state_reason": "同一实际账户和品种已有活动执行",
                        "last_error": f"冲突 execution: {conflict.id}",
                        "needs_attention": True,
                    }
                )
                blocked = self._store.save(
                    blocked,
                    event_kind="atomic_route_claim_conflict",
                    event_payload={"conflict_execution_id": conflict.id},
                )
                self._emit_record(blocked)
                return blocked

            record = record.model_copy(
                update={
                    "preflight": preflight,
                    "selected_account": preflight.selected_account,
                    "account_identity": account_identity,
                    "state_reason": "券商只读预检通过",
                }
            )
            record = self._store.save(
                record,
                event_kind="preflight_passed",
                event_payload={
                    "broker": record.plan.broker,
                    "account": preflight.selected_account,
                    "instrument": record.plan.instrument,
                    "quantity": str(preflight.quantity),
                },
            )
            prepared = adapter.prepare_submit(record)
            prepared = self._store.save(
                prepared,
                event_kind="submit_intent",
                event_payload={
                    "client_order_id": prepared.client_order_id,
                    "account": prepared.selected_account,
                },
            )
            try:
                submitted = self._execute_broker_write(
                    prepared.plan,
                    lambda: adapter.submit_entry(prepared),
                    effective_account=prepared.selected_account,
                )
            except LiveTradingDisabled as exc:
                blocked = prepared.model_copy(
                    update={
                        "state": ExecutionState.BLOCKED,
                        "state_reason": "写入前会话已停用，未调用券商提交接口",
                        "last_error": str(exc),
                        "needs_attention": True,
                    }
                )
                blocked = self._store.save(
                    blocked,
                    event_kind="entry_write_disarmed",
                )
                self._emit_record(blocked)
                return blocked
            except BrokerRejected as exc:
                rejected = prepared.model_copy(
                    update={
                        "state": ExecutionState.REJECTED,
                        "state_reason": "券商拒绝入场订单",
                        "last_error": str(exc),
                        "needs_attention": True,
                    }
                )
                rejected = self._store.save(
                    rejected,
                    event_kind="entry_rejected",
                    event_payload={"error": str(exc)},
                )
                self._emit_record(rejected)
                return rejected
            except (SubmissionUnknown, BrokerTransportError) as exc:
                unknown = prepared.model_copy(
                    update={
                        "state": ExecutionState.UNKNOWN,
                        "state_reason": "入场提交结果不明，禁止重发并等待对账",
                        "last_error": str(exc),
                        "needs_attention": True,
                    }
                )
                unknown = self._save_after_possible_write(
                    unknown,
                    event_kind="entry_submit_unknown",
                    event_payload={"error": str(exc)},
                )
                self.disarm()
                self._emit_record(unknown)
                return unknown
            submitted = self._save_after_possible_write(
                submitted,
                event_kind="entry_accepted",
                event_payload={"broker_order_id": submitted.broker_order_id},
            )
            self._emit_record(submitted)
            return submitted

    def expire_unsubmitted(
        self,
        execution_id: str,
        *,
        reason: str,
    ) -> ExecutionRecord:
        """只在本地作废从未送达券商的 READY 计划。"""
        with self._lock:
            current = self._store.get(execution_id)
            if current is None:
                raise KeyError(f"未知 execution id: {execution_id}")
            if current.state != ExecutionState.READY:
                raise PreflightError(
                    f"当前状态 {current.state.value} 不能按未提交计划作废"
                )
            if (
                current.preflight is not None
                or current.client_order_id
                or current.broker_order_id
                or current.filled_quantity != 0
            ):
                raise PreflightError(
                    "执行记录已有预检、订单号或成交，禁止按未提交计划作废"
                )
            expired = current.model_copy(
                update={
                    "state": ExecutionState.CANCELED,
                    "state_reason": str(reason),
                    "last_error": "",
                    "needs_attention": False,
                }
            )
            expired = self._store.save(
                expired,
                event_kind="ready_expired",
                event_payload={"reason": str(reason)},
            )
            self._emit_record(expired)
            return expired

    @staticmethod
    def _materially_changed(
        before: ExecutionRecord,
        after: ExecutionRecord,
    ) -> bool:
        excluded = {"updated_at", "revision"}
        before_data = before.model_dump(exclude=excluded)
        after_data = after.model_dump(exclude=excluded)
        return before_data != after_data

    @staticmethod
    def _persistent_risk_reducing_blocked(
        record: ExecutionRecord,
    ) -> bool:
        broker_state = record.broker_state
        return bool(
            broker_state.get("risk_reducing_writes_blocked")
            or broker_state.get("write_unknown")
            or broker_state.get("identity_or_route_blocked")
            or record.state is ExecutionState.UNKNOWN
        )

    @staticmethod
    def _with_risk_reducing_block(
        record: ExecutionRecord,
        reason: str,
    ) -> ExecutionRecord:
        broker_state = dict(record.broker_state)
        broker_state["risk_reducing_writes_blocked"] = str(reason)
        return record.model_copy(update={"broker_state": broker_state})

    def _require_record_risk_reducing_unblocked(
        self,
        record: ExecutionRecord,
    ) -> None:
        if self._persistent_risk_reducing_blocked(record):
            raise LiveTradingDisabled(
                "该执行存在持久停写标记，必须先完成成功的只读对账"
            )

    def reconcile_once(
        self,
        execution_ids: list[str] | None = None,
    ) -> list[ExecutionRecord]:
        updates: list[ExecutionRecord] = []
        with self._lock:
            writes_paused = False
            active_records = self._store.list_active()
            if execution_ids is not None:
                allowed_ids = set(execution_ids)
                active_records = [
                    record
                    for record in active_records
                    if record.id in allowed_ids
                ]
            for current in active_records:
                if current.state == ExecutionState.SUBMITTING:
                    current = self._store.save(
                        current.model_copy(
                            update={
                                "state": ExecutionState.UNKNOWN,
                                "state_reason": (
                                    "PA 在提交意图后中断；按客户订单号对账，禁止重发"
                                ),
                                "needs_attention": True,
                            }
                        ),
                        event_kind="submit_interrupted",
                    )
                    self._emit_record(current)
                try:
                    adapter = self._adapter(current.plan)
                    self._verify_account_identity(adapter, current)
                    try:
                        claim_conflict = self._store.acquire_route_claim(
                            current,
                            account_identity=current.account_identity,
                        )
                    except RuntimeError as exc:
                        raise CredentialError(
                            f"活动路由占用校验失败：{exc}"
                        ) from exc
                    if claim_conflict is not None:
                        raise CredentialError(
                            "活动路由已被其他 execution 占用："
                            f"{claim_conflict.id}"
                        )
                    adapter_record = current
                    if current.broker_state.get("identity_or_route_blocked"):
                        broker_state = dict(current.broker_state)
                        broker_state.pop("identity_or_route_blocked", None)
                        if (
                            broker_state.get("risk_reducing_writes_blocked")
                            == "identity_or_route_blocked"
                        ):
                            broker_state.pop(
                                "risk_reducing_writes_blocked",
                                None,
                            )
                        adapter_record = current.model_copy(
                            update={
                                "broker_state": broker_state,
                                "needs_attention": False,
                                "last_error": "",
                                "state_reason": (
                                    "实际账户身份与活动路由已重新验证"
                                ),
                            }
                        )
                    persistent_write_block = (
                        self._persistent_risk_reducing_blocked(adapter_record)
                    )
                    allow_risk_reducing_writes = (
                        not writes_paused
                        and not persistent_write_block
                        and self._plan_writes_enabled(
                            current.plan,
                            risk_kind=_RISK_REDUCING,
                        )
                    )
                    with self._broker_write_scope(_RISK_REDUCING):
                        updated = adapter.reconcile(
                            adapter_record,
                            allow_writes=allow_risk_reducing_writes,
                        )
                    if updated.broker_state.get("write_unknown"):
                        updated = self._with_risk_reducing_block(
                            updated,
                            "broker_write_unknown",
                        )
                        self._risk_reducing_writes_blocked = True
                        self.disarm()
                        writes_paused = True
                    elif (
                        persistent_write_block
                        and updated.state is not ExecutionState.UNKNOWN
                        and not updated.needs_attention
                        and not updated.broker_state.get(
                            "identity_or_route_blocked"
                        )
                    ):
                        broker_state = dict(updated.broker_state)
                        broker_state.pop(
                            "risk_reducing_writes_blocked",
                            None,
                        )
                        updated = updated.model_copy(
                            update={"broker_state": broker_state}
                        )
                except (CredentialError, PreflightError) as exc:
                    broker_state = dict(current.broker_state)
                    broker_state["identity_or_route_blocked"] = True
                    broker_state["risk_reducing_writes_blocked"] = (
                        "identity_or_route_blocked"
                    )
                    updated = current.model_copy(
                        update={
                            "broker_state": broker_state,
                            "needs_attention": True,
                            "last_error": str(exc),
                            "state_reason": (
                                "实际账户身份或活动路由不一致，已阻断订单与账户读取"
                            ),
                        }
                    )
                    self._risk_reducing_writes_blocked = True
                    self.disarm()
                    writes_paused = True
                except BrokerRejected as exc:
                    updated = self._with_risk_reducing_block(
                        current.model_copy(
                            update={
                                "state_reason": "券商拒绝执行阶段操作；已停写并保留监控",
                                "last_error": str(exc),
                                "needs_attention": True,
                            }
                        ),
                        "broker_rejected",
                    )
                    self._risk_reducing_writes_blocked = True
                    self.disarm()
                    writes_paused = True
                except SubmissionUnknown as exc:
                    updated = self._with_risk_reducing_block(
                        current.model_copy(
                            update={
                                "state": (
                                    ExecutionState.UNKNOWN
                                    if current.state
                                    in {
                                        ExecutionState.ENTRY_PENDING,
                                        ExecutionState.PARTIALLY_FILLED,
                                        ExecutionState.UNKNOWN,
                                    }
                                    else current.state
                                ),
                                "state_reason": "券商写操作结果不明，已停用本次实盘会话",
                                "last_error": str(exc),
                                "needs_attention": True,
                            }
                        ),
                        "submission_unknown",
                    )
                    self._risk_reducing_writes_blocked = True
                    self.disarm()
                    writes_paused = True
                except Exception as exc:  # noqa: BLE001
                    self._invalidate_runtime_after_uncertain_local_failure()
                    writes_paused = True
                    updated = self._with_risk_reducing_block(
                        current.model_copy(
                            update={
                                "needs_attention": True,
                                "last_error": str(exc),
                                "state_reason": "执行对账发生错误，等待下一轮或人工处理",
                            }
                        ),
                        "reconciliation_error",
                    )
                if not self._materially_changed(current, updated):
                    continue
                saved = self._save_after_possible_write(
                    updated,
                    event_kind="reconciled",
                    event_payload={
                        "from_state": current.state.value,
                        "to_state": updated.state.value,
                        "reason": updated.state_reason,
                    },
                )
                updates.append(saved)
                self._emit_record(saved)
            if not any(
                self._persistent_risk_reducing_blocked(record)
                for record in self._store.list_active()
            ):
                self._risk_reducing_writes_blocked = False
        return updates

    def request_exit(self, execution_id: str, *, reason: str = "主动离场") -> ExecutionRecord:
        with self._lock:
            current = self._store.get(execution_id)
            if current is None:
                raise KeyError(f"未知 execution id: {execution_id}")
            self._require_record_risk_reducing_unblocked(current)
            self._require_plan_writes(
                current.plan,
                risk_kind=_RISK_REDUCING,
            )
            adapter = self._adapter(current.plan)
            self._verify_account_identity(adapter, current)
            intent_state = dict(current.broker_state)
            intent_state["manual_exit_intent_at"] = utc_now_iso()
            intent_state["manual_exit_intent"] = True
            intent_state["manual_exit_reason"] = str(reason or "主动离场")
            intent_state["exit_runtime_id"] = self._runtime_id
            current = self._store.save(
                current.model_copy(update={"broker_state": intent_state}),
                event_kind="exit_intent",
                event_payload={"reason": reason},
            )
            try:
                with self._broker_write_scope(_RISK_REDUCING):
                    planned = adapter.request_exit(current, reason=reason)
            except (BrokerRejected, BrokerApiError) as exc:
                planned = self._with_risk_reducing_block(
                    current.model_copy(
                        update={
                            "needs_attention": True,
                            "last_error": str(exc),
                            "state_reason": (
                                "券商拒绝主动离场；已停写并保留只读监控"
                            ),
                        }
                    ),
                    "request_exit_rejected",
                )
                self._risk_reducing_writes_blocked = True
                self.disarm()
            except BrokerTransportError as exc:
                if exc.write_may_have_reached:
                    broker_state = dict(current.broker_state)
                    broker_state["exit_requested"] = True
                    broker_state["exit_status"] = "unknown"
                    broker_state["write_unknown"] = "request_exit"
                    broker_state["risk_reducing_writes_blocked"] = (
                        "request_exit_unknown"
                    )
                    planned = current.model_copy(
                        update={
                            "state": ExecutionState.UNKNOWN,
                            "broker_state": broker_state,
                            "needs_attention": True,
                            "last_error": str(exc),
                            "state_reason": (
                                "主动离场结果不明，等待只读对账"
                            ),
                        }
                    )
                else:
                    planned = self._with_risk_reducing_block(
                        current.model_copy(
                            update={
                                "needs_attention": True,
                                "last_error": str(exc),
                                "state_reason": (
                                    "主动离场请求未送达；已停写并保留只读监控"
                                ),
                            }
                        ),
                        "request_exit_transport_failed",
                    )
                self._risk_reducing_writes_blocked = True
                self.disarm()
            except SubmissionUnknown as exc:
                broker_state = dict(current.broker_state)
                broker_state["exit_requested"] = True
                broker_state["exit_status"] = "unknown"
                broker_state["write_unknown"] = "request_exit"
                broker_state["risk_reducing_writes_blocked"] = (
                    "request_exit_unknown"
                )
                planned = current.model_copy(
                    update={
                        "state": ExecutionState.UNKNOWN,
                        "broker_state": broker_state,
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": "主动离场结果不明，等待只读对账",
                    }
                )
                self._risk_reducing_writes_blocked = True
                self.disarm()
            if planned.broker_state.get("write_unknown"):
                planned = self._with_risk_reducing_block(
                    planned,
                    "request_exit_unknown",
                )
                self._risk_reducing_writes_blocked = True
                self.disarm()
            saved = self._save_after_possible_write(
                planned,
                event_kind="exit_requested",
                event_payload={"reason": reason},
            )
            self._emit_record(saved)
            return saved

    def cancel_entry(self, execution_id: str) -> ExecutionRecord:
        with self._lock:
            current = self._store.get(execution_id)
            if current is None:
                raise KeyError(f"未知 execution id: {execution_id}")
            self._require_record_risk_reducing_unblocked(current)
            self._require_plan_writes(
                current.plan,
                risk_kind=_RISK_REDUCING,
            )
            intent_state = dict(current.broker_state)
            intent_state["manual_cancel_intent_at"] = utc_now_iso()
            intent_state["entry_cancel_intent"] = True
            intent_state["entry_cancel_runtime_id"] = self._runtime_id
            current = self._store.save(
                current.model_copy(update={"broker_state": intent_state}),
                event_kind="cancel_entry_intent",
            )
            adapter = self._adapter(current.plan)
            self._verify_account_identity(adapter, current)
            try:
                with self._broker_write_scope(_RISK_REDUCING):
                    updated = self._execute_broker_write(
                        current.plan,
                        lambda: adapter.cancel_entry(current),
                        risk_kind=_RISK_REDUCING,
                    )
            except BrokerRejected as exc:
                broker_state = dict(current.broker_state)
                broker_state.pop("entry_cancel_intent", None)
                broker_state.pop("entry_cancel_runtime_id", None)
                broker_state.pop("entry_cancel_requested", None)
                broker_state["entry_cancel_status"] = "rejected"
                updated = self._with_risk_reducing_block(
                    current.model_copy(
                        update={
                            "broker_state": broker_state,
                            "needs_attention": True,
                            "last_error": str(exc),
                            "state_reason": "券商拒绝撤销入场；已停写并保留订单监控",
                        }
                    ),
                    "cancel_entry_rejected",
                )
                self._risk_reducing_writes_blocked = True
                self.disarm()
            except SubmissionUnknown as exc:
                broker_state = dict(current.broker_state)
                broker_state["entry_cancel_requested"] = True
                broker_state["entry_cancel_status"] = "unknown"
                broker_state["write_unknown"] = "cancel_entry"
                broker_state["risk_reducing_writes_blocked"] = (
                    "cancel_entry_unknown"
                )
                updated = current.model_copy(
                    update={
                        "state": ExecutionState.UNKNOWN,
                        "broker_state": broker_state,
                        "needs_attention": True,
                        "last_error": str(exc),
                        "state_reason": "撤销入场结果不明，等待对账",
                    }
                )
                self._risk_reducing_writes_blocked = True
                self.disarm()
            if updated.broker_state.get("write_unknown"):
                updated = self._with_risk_reducing_block(
                    updated,
                    "cancel_entry_unknown",
                )
                self._risk_reducing_writes_blocked = True
                self.disarm()
            saved = self._save_after_possible_write(
                updated,
                event_kind="cancel_entry_requested",
            )
            self._emit_record(saved)
            return saved

    def refresh_account(self, execution_id: str | None = None) -> AccountSnapshot:
        with self._lock:
            if execution_id:
                execution = self._store.get(execution_id)
                if execution is None:
                    raise KeyError(f"未知 execution id: {execution_id}")
                plan = execution.plan
                account_profile = execution.selected_account or None
                broker_metadata = (
                    execution.preflight.broker_metadata
                    if execution.preflight is not None
                    else None
                )
            else:
                plan = self._target_plan_from_settings()
                account_profile = None
                broker_metadata = None
            return self._refresh_account_plan(
                plan,
                account_profile=account_profile,
                broker_metadata=broker_metadata,
                execution=execution if execution_id else None,
                raise_on_risk_failure=True,
            )

    def refresh_account_route(
        self,
        *,
        broker: str,
        environment: str,
        account: str,
        raise_on_risk_failure: bool = True,
    ) -> AccountSnapshot:
        """Read the fixed OKX Demo campaign route without changing settings."""
        with self._lock:
            route_identity = (
                str(broker or "").strip().lower(),
                str(environment or "").strip().lower(),
                str(account or "").strip().lower(),
            )
            if route_identity != ("okx", "demo", "okx"):
                raise PreflightError(
                    "跨日常设置的账户读取只允许 OKX Demo campaign"
                )
            plan = ExecutionPlan(
                id="account-read-only-okx-demo-campaign",
                analysis_digest="account-read-only",
                analysis_record_path="",
                broker="okx",
                environment="demo",
                product="swap",
                requested_account="okx",
                allow_account_fallback=False,
                source_symbol=_OKX_DEMO_CAMPAIGN_INSTRUMENT,
                instrument=_OKX_DEMO_CAMPAIGN_INSTRUMENT,
                direction="long",
                entry_type="market",
                quantity=Decimal("1"),
                entry_price=Decimal("1"),
                take_profit_1=Decimal("2"),
                take_profit_2=Decimal("3"),
                stop_loss=Decimal("0.5"),
                trade_confidence=100,
                created_at=utc_now_iso(),
                config_fingerprint=(
                    "account-read-only:okx:demo:okx:swap:"
                    f"{_OKX_DEMO_CAMPAIGN_INSTRUMENT}:"
                    f"{_OKX_DEMO_CAMPAIGN_API_BASE_URL}"
                ),
                okx_api_base_url=_OKX_DEMO_CAMPAIGN_API_BASE_URL,
                okx_margin_mode="cross",
                entry_timeout_seconds=int(
                    self._settings.execution.entry_timeout_seconds
                ),
            )
            return self._refresh_account_plan(
                plan,
                raise_on_risk_failure=raise_on_risk_failure,
            )

    def clear_drawdown_stop(
        self,
        *,
        broker: str,
        environment: str,
        account: str,
    ) -> RiskRuntimeState:
        """经 Worker 控制命令显式清除停止，并重锚当前总权益。"""

        with self._lock:
            if self._risk_runtime is None:
                raise PreflightError("当前执行服务未接入风险运行态")
            route_identity = (
                str(broker or "").strip().lower(),
                str(environment or "").strip().lower(),
                str(account or "").strip().lower(),
            )
            if route_identity != ("okx", "demo", "okx"):
                raise PreflightError("资金流回撤停止只允许固定 OKX Demo 路由")
            self.refresh_account_route(
                broker=route_identity[0],
                environment=route_identity[1],
                account=route_identity[2],
                raise_on_risk_failure=True,
            )
            return self._risk_runtime.clear(
                route_key(
                    broker=route_identity[0],
                    environment=route_identity[1],
                    account=route_identity[2],
                )
            )

    def recover_transient_risk_stop(
        self,
        *,
        broker: str,
        environment: str,
        account: str,
    ) -> RiskRuntimeState:
        """新鲜完整读取成功后，显式恢复临时券商读取故障。"""

        with self._lock:
            if self._risk_runtime is None:
                raise PreflightError("当前执行服务未接入风险运行态")
            route_identity = (
                str(broker or "").strip().lower(),
                str(environment or "").strip().lower(),
                str(account or "").strip().lower(),
            )
            if route_identity != ("okx", "demo", "okx"):
                raise PreflightError("风险停止复核只允许固定 OKX Demo 路由")
            self.refresh_account_route(
                broker=route_identity[0],
                environment=route_identity[1],
                account=route_identity[2],
                raise_on_risk_failure=True,
            )
            return self._risk_runtime.recover_transient_read_failure(
                route_key(
                    broker=route_identity[0],
                    environment=route_identity[1],
                    account=route_identity[2],
                )
            )

    def _refresh_account_plan(
        self,
        plan: ExecutionPlan,
        *,
        account_profile: str | None = None,
        broker_metadata: dict | None = None,
        execution: ExecutionRecord | None = None,
        raise_on_risk_failure: bool = False,
    ) -> AccountSnapshot:
        adapter = self._adapter(plan)
        if execution is not None and (
            execution.account_identity
            or execution.state != ExecutionState.READY
        ):
            self._verify_account_identity(adapter, execution)
        try:
            snapshot = adapter.account_snapshot(
                plan,
                account_profile=account_profile,
                broker_metadata=broker_metadata,
            )
        except Exception as exc:
            if self._risk_runtime is not None and plan.broker == "okx":
                self._risk_runtime.mark_failure(
                    broker=plan.broker,
                    environment=plan.environment,
                    account=plan.requested_account,
                    reason=(
                        "risk_runtime_"
                        f"{self._risk_failure_code(exc)}"
                    ),
                )
            raise
        self._store.save_account_snapshot(snapshot)
        self._emit_account(snapshot)
        self._refresh_risk_runtime(
            plan,
            adapter,
            snapshot=snapshot,
            raise_on_failure=raise_on_risk_failure,
        )
        return snapshot

    def _target_plan_for_route(
        self,
        *,
        broker: str,
        environment: str,
        account: str,
    ) -> ExecutionPlan:
        broker = str(broker or "").strip().lower()
        environment = str(environment or "").strip().lower()
        account = str(account or "").strip().lower()
        if broker == "okx":
            if account != "okx" or environment not in {"demo", "live"}:
                raise PreflightError("OKX 账户只读路由无效")
            route = self._settings.execution.okx
            product = str(route.product or "").strip().lower()
            requested = "okx"
        elif broker == "longbridge":
            expected_environment = {
                "paper": "demo",
                "comprehensive": "live",
                "intraday": "live",
            }.get(account)
            if expected_environment is None or environment != expected_environment:
                raise PreflightError("Longbridge 账户只读路由无效")
            route = self._settings.execution.longbridge
            product = "securities"
            requested = account
        else:
            raise PreflightError("不支持的账户只读券商路由")

        execution = self._settings.execution
        quantity_text = str(route.quantity or "1")
        try:
            quantity = Decimal(quantity_text)
        except (InvalidOperation, ValueError):
            quantity = Decimal("1")
        if not quantity.is_finite() or quantity <= 0:
            quantity = Decimal("1")
        instrument = str(route.instrument or "").strip().upper()
        if not instrument:
            raise PreflightError("请先配置券商品种，再读取账户")
        return ExecutionPlan(
            id=f"account-read-only-{broker}-{environment}-{requested}",
            analysis_digest="account-read-only",
            analysis_record_path="",
            broker=broker,
            environment=environment,
            product=product,
            requested_account=requested,
            allow_account_fallback=False,
            source_symbol=str(
                route.source_symbol or instrument
            ).strip().upper(),
            instrument=instrument,
            direction="long",
            entry_type="market",
            quantity=quantity,
            entry_price=Decimal("1"),
            take_profit_1=Decimal("2"),
            take_profit_2=Decimal("3"),
            stop_loss=Decimal("0.5"),
            trade_confidence=100,
            created_at=utc_now_iso(),
            config_fingerprint=(
                f"account-read-only:{broker}:{environment}:{requested}:"
                f"{product}:{instrument}:"
                f"{route.api_base_url if broker == 'okx' else ''}"
            ),
            okx_api_base_url=(
                route.api_base_url if broker == "okx" else ""
            ),
            okx_margin_mode=(
                route.margin_mode if broker == "okx" else ""
            ),
            longbridge_allow_outside_rth=(
                bool(route.allow_outside_rth)
                if broker == "longbridge"
                else False
            ),
            entry_timeout_seconds=int(execution.entry_timeout_seconds),
        )

    def _target_plan_from_settings(self) -> ExecutionPlan:
        execution = self._settings.execution
        broker = execution.selected_broker
        if broker == "longbridge":
            account = str(execution.longbridge.preferred_account)
            environment = "demo" if account == "paper" else "live"
        else:
            account = "okx"
            environment = (
                "demo" if bool(execution.okx.simulated) else "live"
            )
        return self._target_plan_for_route(
            broker=broker,
            environment=environment,
            account=account,
        )

    def refresh_execution_accounts(
        self,
        execution_ids: list[str] | None = None,
        *,
        raise_on_error: bool = False,
    ) -> list[AccountSnapshot]:
        """Refresh each active broker/account once, including a just-closed execution."""
        records: list[ExecutionRecord] = []
        if execution_ids is None:
            records = self._store.list_active()
        else:
            for execution_id in execution_ids:
                record = self._store.get(execution_id)
                if record is not None:
                    records.append(record)
        snapshots: list[AccountSnapshot] = []
        failures: list[str] = []
        seen: set[tuple[str, str, str, str]] = set()
        for record in records:
            account = record.selected_account or record.plan.requested_account
            key = (
                record.plan.broker,
                record.plan.environment,
                account,
                record.plan.okx_api_base_url,
            )
            if key in seen:
                continue
            seen.add(key)
            try:
                snapshots.append(self.refresh_account(record.id))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{record.plan.broker}/{account}")
                self._emit_error(
                    f"{record.plan.broker}/{account} 账户快照刷新失败：{exc}"
                )
        if failures and raise_on_error:
            raise RuntimeError(
                "账户快照刷新失败：" + ", ".join(sorted(failures))
            )
        return snapshots

    def latest_execution(self) -> ExecutionRecord | None:
        rows = self._store.list_recent(limit=1)
        return rows[0] if rows else None

    def start_monitoring(self) -> None:
        with self._lock:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="pa-execution-monitor",
                daemon=True,
            )
            self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        interval = float(self._settings.execution.poll_interval_seconds)
        while not self._stop_event.wait(interval):
            try:
                self.monitor_once()
            except Exception as exc:  # noqa: BLE001
                self._emit_error(f"交易监控轮询失败：{exc}")

    def _begin_idle_account_refresh(
        self,
        route: tuple[str, str, str, str],
    ) -> bool:
        with self._lock:
            if route in self._idle_account_refresh_in_progress:
                return False
            if float(self._monotonic_clock()) < (
                self._idle_account_next_refresh.get(route, 0.0)
            ):
                return False
            self._idle_account_refresh_in_progress.add(route)
            return True

    def _finish_idle_account_refresh(
        self,
        route: tuple[str, str, str, str],
    ) -> None:
        with self._lock:
            self._idle_account_refresh_in_progress.discard(route)
            self._idle_account_next_refresh[route] = (
                float(self._monotonic_clock())
                + _IDLE_ACCOUNT_REFRESH_INTERVAL_SECONDS
            )

    def monitor_once(
        self,
    ) -> tuple[list[ExecutionRecord], list[AccountSnapshot]]:
        """对账并刷新活动账户；无活动执行时仍刷新当前选定账户。"""
        active_records = self._store.list_active()
        active_ids = [record.id for record in active_records]
        updates = self.reconcile_once()
        snapshots = self.refresh_execution_accounts(
            active_ids,
            raise_on_error=True,
        )
        target_plan = self._target_plan_from_settings()
        target_key = (
            target_plan.broker,
            target_plan.environment,
            target_plan.requested_account,
            target_plan.okx_api_base_url,
        )
        active_keys = {
            (
                record.plan.broker,
                record.plan.environment,
                record.selected_account or record.plan.requested_account,
                record.plan.okx_api_base_url,
            )
            for record in active_records
        }
        if (
            target_key not in active_keys
            and self._begin_idle_account_refresh(target_key)
        ):
            try:
                snapshots.append(self.refresh_account())
            finally:
                self._finish_idle_account_refresh(target_key)
        return updates, snapshots

    def _monitor_once(self) -> None:
        """Backward-compatible alias for older callers and tests."""
        self.monitor_once()

    def stop_monitoring(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._monitor_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, min(float(timeout), 5.0)))
        if thread is not None and thread.is_alive():
            self._logger.warning("交易监控线程未在截止时间内退出")
        else:
            self._monitor_thread = None
        self.disarm()
