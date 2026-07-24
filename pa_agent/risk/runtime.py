"""账户总权益、外部资金流和新增风险停止状态。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from pa_agent.risk.cashflow import (
    CashflowReconciliationFailure,
    classify_okx_external_cashflows,
    reconcile_equity_cashflows,
)

RISK_DRAWDOWN_STOP_FRACTION = Decimal("0.50")
_OKX_BILL_WINDOW = timedelta(days=7)


class RiskRuntimeStateStore(Protocol):
    def get_risk_runtime_state(
        self,
        route_key: str,
    ) -> RiskRuntimeState | None: ...

    def save_risk_runtime_state(
        self,
        state: RiskRuntimeState,
    ) -> RiskRuntimeState: ...


@dataclass(frozen=True)
class RiskRuntimeState:
    """一个不可变账户路由的资金流与新增风险状态。"""

    route_key: str
    broker: str
    environment: str
    account: str
    account_identity: str
    last_external_cashflow_bill_id: str
    last_account_bill_id: str
    last_account_bill_timestamp_ms: int | None
    last_bill_scan_at: datetime | None
    adjusted_high_water_usd: Decimal | None
    last_total_equity_usd: Decimal | None
    drawdown_usd: Decimal | None
    drawdown_fraction: Decimal | None
    kill_active: bool
    kill_reason: str
    kill_activated_at: datetime | None
    updated_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "last_bill_scan_at",
            "kill_activated_at",
            "updated_at",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"{field_name} 必须带 UTC 时区")


class RiskRuntimeBlocked(RuntimeError):
    """新增风险被资金流、权益或回撤状态明确阻断。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


def route_key(*, broker: str, environment: str, account: str) -> str:
    """返回不含凭据的稳定账户路由键。"""

    return ":".join(
        (
            str(broker).strip().lower(),
            str(environment).strip().lower(),
            str(account).strip().lower(),
        )
    )


def _now_utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("risk runtime clock 必须返回带时区时间")
    return value.astimezone(UTC)


def _positive_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CashflowReconciliationFailure(
            "invalid_total_equity",
            f"{field_name} 缺失或不是数字",
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CashflowReconciliationFailure(
            "invalid_total_equity",
            f"{field_name} 不是有效数字",
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise CashflowReconciliationFailure(
            "invalid_total_equity",
            f"{field_name} 必须是有限正数",
        )
    return parsed


class RiskRuntime:
    """把只读账户事实转换成持久化的新增风险状态。"""

    def __init__(
        self,
        store: RiskRuntimeStateStore,
        *,
        clock: Callable[[], datetime] | None = None,
        stop_fraction: Decimal = RISK_DRAWDOWN_STOP_FRACTION,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop_fraction = Decimal(stop_fraction)
        if not self._stop_fraction.is_finite() or self._stop_fraction <= 0:
            raise ValueError("回撤停止比例必须是有限正数")

    def get(self, route_key_value: str) -> RiskRuntimeState | None:
        return self._store.get_risk_runtime_state(route_key_value)

    def _blocked_state(
        self,
        *,
        route_key_value: str,
        broker: str,
        environment: str,
        account: str,
        account_identity: str,
        reason: str,
        previous: RiskRuntimeState | None,
        now: datetime,
    ) -> RiskRuntimeState:
        state = previous or RiskRuntimeState(
            route_key=route_key_value,
            broker=broker,
            environment=environment,
            account=account,
            account_identity=account_identity,
            last_external_cashflow_bill_id="",
            last_account_bill_id="",
            last_account_bill_timestamp_ms=None,
            last_bill_scan_at=None,
            adjusted_high_water_usd=None,
            last_total_equity_usd=None,
            drawdown_usd=None,
            drawdown_fraction=None,
            kill_active=True,
            kill_reason=reason,
            kill_activated_at=now,
            updated_at=now,
        )
        return replace(
            state,
            account_identity=account_identity or state.account_identity,
            kill_active=True,
            kill_reason=reason,
            kill_activated_at=state.kill_activated_at or now,
            updated_at=now,
        )

    def mark_failure(
        self,
        *,
        broker: str,
        environment: str,
        account: str,
        account_identity: str = "",
        reason: str,
    ) -> RiskRuntimeState:
        """记录读取或分类失败, 并立即关闭新增风险。"""

        route_key_value = route_key(
            broker=broker,
            environment=environment,
            account=account,
        )
        previous = self._store.get_risk_runtime_state(route_key_value)
        now = _now_utc(self._clock)
        state = self._blocked_state(
            route_key_value=route_key_value,
            broker=broker,
            environment=environment,
            account=account,
            account_identity=account_identity,
            reason=reason,
            previous=previous,
            now=now,
        )
        return self._store.save_risk_runtime_state(state)

    def refresh(
        self,
        *,
        broker: str,
        environment: str,
        account: str,
        account_identity: str,
        total_equity_usd: object,
        bill_rows: Iterable[dict[str, object]],
    ) -> RiskRuntimeState:
        """刷新权益、增量资金流和 50% 新增风险停止状态。"""

        route_key_value = route_key(
            broker=broker,
            environment=environment,
            account=account,
        )
        previous = self._store.get_risk_runtime_state(route_key_value)
        now = _now_utc(self._clock)
        try:
            current_equity = _positive_decimal(
                total_equity_usd,
                "account_total_equity_usd",
            )
            if not account_identity.strip():
                raise CashflowReconciliationFailure(
                    "missing_account_identity",
                    "账户身份指纹缺失",
                )
            raw_rows = tuple(bill_rows)
            ordered_rows = self._ordered_bill_rows(raw_rows)
            newest_bill_id = (
                ordered_rows[-1][1] if ordered_rows else ""
            )
            newest_bill_timestamp_ms = (
                ordered_rows[-1][0] if ordered_rows else None
            )
            needs_baseline = previous is None or (
                previous.last_total_equity_usd is None
                and previous.adjusted_high_water_usd is None
                and previous.last_bill_scan_at is None
            )
            if needs_baseline:
                if (
                    previous is not None
                    and previous.account_identity
                    and previous.account_identity != account_identity
                ):
                    raise CashflowReconciliationFailure(
                        "account_identity_changed",
                        "账户身份指纹发生变化",
                    )
                state = RiskRuntimeState(
                    route_key=route_key_value,
                    broker=broker,
                    environment=environment,
                    account=account,
                    account_identity=account_identity,
                    # 首次建立的是“当前总权益”基线；更早账单已经包含在
                    # 这份权益里，不能再次当作基线后的资金流重复调整。
                    last_external_cashflow_bill_id="",
                    last_account_bill_id=newest_bill_id,
                    last_account_bill_timestamp_ms=newest_bill_timestamp_ms,
                    last_bill_scan_at=now,
                    adjusted_high_water_usd=current_equity,
                    last_total_equity_usd=current_equity,
                    drawdown_usd=Decimal("0"),
                    drawdown_fraction=Decimal("0"),
                    kill_active=False,
                    kill_reason="",
                    kill_activated_at=None,
                    updated_at=now,
                )
                return self._store.save_risk_runtime_state(state)

            if (
                previous.account_identity
                and previous.account_identity != account_identity
            ):
                raise CashflowReconciliationFailure(
                    "account_identity_changed",
                    "账户身份指纹发生变化",
                )
            if (
                previous.last_total_equity_usd is None
                or previous.adjusted_high_water_usd is None
            ):
                raise CashflowReconciliationFailure(
                    "invalid_persisted_state",
                    "已保存的风险运行态缺少权益或历史高水位",
                )
            new_rows = self._rows_after_scan_boundary(
                ordered_rows,
                previous=previous,
                now=now,
            )
            new_events = classify_okx_external_cashflows(new_rows)
            result = reconcile_equity_cashflows(
                equity_basis="account_total_equity_usd",
                previous_equity_usd=previous.last_total_equity_usd,
                current_equity_usd=current_equity,
                previous_adjusted_high_water_usd=(
                    previous.adjusted_high_water_usd
                ),
                external_cashflows=new_events,
            )
        except CashflowReconciliationFailure as exc:
            return self.mark_failure(
                broker=broker,
                environment=environment,
                account=account,
                account_identity=account_identity,
                reason=f"risk_runtime_{exc.code}",
            )

        kill_active = previous.kill_active or (
            result.drawdown_fraction >= self._stop_fraction
        )
        kill_reason = previous.kill_reason
        activated_at = previous.kill_activated_at
        if not previous.kill_active and kill_active:
            kill_reason = "drawdown_threshold_exceeded"
            activated_at = now
        state = RiskRuntimeState(
            route_key=route_key_value,
            broker=broker,
            environment=environment,
            account=account,
            account_identity=account_identity,
            last_external_cashflow_bill_id=(
                result.last_external_cashflow_bill_id
                or previous.last_external_cashflow_bill_id
            ),
            last_account_bill_id=newest_bill_id,
            last_account_bill_timestamp_ms=newest_bill_timestamp_ms,
            last_bill_scan_at=now,
            adjusted_high_water_usd=result.adjusted_high_water_usd,
            last_total_equity_usd=current_equity,
            drawdown_usd=result.drawdown_usd,
            drawdown_fraction=result.drawdown_fraction,
            kill_active=kill_active,
            kill_reason=kill_reason if kill_active else "",
            kill_activated_at=activated_at if kill_active else None,
            updated_at=now,
        )
        return self._store.save_risk_runtime_state(state)

    @staticmethod
    def _ordered_bill_rows(
        rows: tuple[dict[str, object], ...],
    ) -> tuple[tuple[int, str, dict[str, object]], ...]:
        ordered: list[tuple[int, str, dict[str, object]]] = []
        for row in rows:
            bill_id = str(row.get("billId") or "").strip()
            timestamp_text = str(row.get("ts") or "").strip()
            if not bill_id or not timestamp_text.isdigit():
                raise CashflowReconciliationFailure(
                    "invalid_bill_scan_boundary",
                    "OKX 账单缺少可持久化的 ID 或时间边界",
                )
            ordered.append((int(timestamp_text), bill_id, row))
        return tuple(sorted(ordered, key=lambda item: (item[0], item[1])))

    @staticmethod
    def _rows_after_scan_boundary(
        ordered_rows: tuple[tuple[int, str, dict[str, object]], ...],
        *,
        previous: RiskRuntimeState,
        now: datetime,
    ) -> tuple[dict[str, object], ...]:
        previous_bill_id = previous.last_account_bill_id
        if previous_bill_id:
            ids = [item[1] for item in ordered_rows]
            if previous_bill_id in ids:
                index = ids.index(previous_bill_id)
                return tuple(item[2] for item in ordered_rows[index + 1 :])
        if previous.last_bill_scan_at is None:
            raise CashflowReconciliationFailure(
                "bill_scan_boundary_missing",
                "已保存风险状态缺少 OKX 账单扫描边界",
            )
        if now - previous.last_bill_scan_at >= _OKX_BILL_WINDOW:
            raise CashflowReconciliationFailure(
                "bill_scan_window_gap",
                "OKX 账单连续扫描中断超过七天",
            )
        boundary_ms = previous.last_account_bill_timestamp_ms
        if boundary_ms is None:
            boundary_ms = int(previous.last_bill_scan_at.timestamp() * 1000)
        boundary = (boundary_ms, previous_bill_id)
        return tuple(
            item[2]
            for item in ordered_rows
            if (item[0], item[1]) > boundary
        )

    def require_new_risk(self, route_key_value: str) -> RiskRuntimeState:
        state = self._store.get_risk_runtime_state(route_key_value)
        if state is None:
            raise RiskRuntimeBlocked(
                "risk_runtime_unavailable",
                "账户总权益风险运行态尚未建立",
            )
        if state.kill_active:
            raise RiskRuntimeBlocked(
                state.kill_reason or "risk_runtime_blocked",
                "账户回撤或资金流风险闸门已关闭新增风险",
            )
        if (
            state.last_total_equity_usd is None
            or state.adjusted_high_water_usd is None
        ):
            raise RiskRuntimeBlocked(
                "risk_runtime_incomplete",
                "账户总权益或调整后历史高水位缺失",
            )
        return state

    def clear(self, route_key_value: str) -> RiskRuntimeState:
        """只接受显式人工命令, 并以最新已读取权益重锚高水位。"""

        state = self._store.get_risk_runtime_state(route_key_value)
        if state is None or state.last_total_equity_usd is None:
            raise RiskRuntimeBlocked(
                "risk_runtime_unavailable",
                "没有可用于人工清除停止状态的有效账户总权益",
            )
        now = _now_utc(self._clock)
        cleared = replace(
            state,
            adjusted_high_water_usd=state.last_total_equity_usd,
            drawdown_usd=Decimal("0"),
            drawdown_fraction=Decimal("0"),
            kill_active=False,
            kill_reason="",
            kill_activated_at=None,
            updated_at=now,
        )
        return self._store.save_risk_runtime_state(cleared)
