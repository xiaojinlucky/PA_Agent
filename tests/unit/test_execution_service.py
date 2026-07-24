from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from pa_agent.config.settings import Settings
from pa_agent.execution.errors import (
    BrokerApiError,
    BrokerRejected,
    BrokerTransportError,
    CredentialError,
    LiveTradingDisabled,
    PreflightError,
    SubmissionUnknown,
)
from pa_agent.execution.models import (
    AccountSnapshot,
    ExecutionState,
    PreflightResult,
)
from pa_agent.execution.service import ExecutionService
from pa_agent.execution.store import ExecutionStore
from tests.unit.test_execution_plan_builder import _persist, _record


class FakePendingWriter:
    def __init__(self, path):
        self.path = path

    def full_path(self, _record):
        return self.path


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.submit_error = None
        self.preflight_error = None
        self.reconcile_error = None
        self.cancel_error = None
        self.exit_error = None
        self.account_error = None
        self.reconcile_write_unknown = False
        self.runtime_id = ""
        self.identity = "okx-account-a"
        self.write_executor = lambda operation: operation()

    def bind_runtime_id(self, runtime_id):
        self.runtime_id = runtime_id

    def bind_write_executor(self, executor):
        self.write_executor = executor

    def account_identity(self, _plan, *, account_profile=None):
        return self.identity

    def preflight(self, plan):
        self.calls.append(("preflight", plan.id))
        if self.preflight_error:
            raise self.preflight_error
        return PreflightResult(
            selected_account="okx",
            account_identity=self.identity,
            quantity=plan.quantity,
            entry_price=plan.entry_price,
            take_profit_1=plan.take_profit_1,
            take_profit_2=plan.take_profit_2,
            stop_loss=plan.stop_loss,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
        )

    def prepare_submit(self, record):
        self.calls.append(("prepare_submit", record.id))
        return record.model_copy(
            update={
                "state": ExecutionState.SUBMITTING,
                "selected_account": "okx",
                "client_order_id": "client-entry",
                "broker_state": {
                    "entry_kind": "regular",
                    "entry_submitted_at": "2026-07-17T00:00:00+00:00",
                },
            }
        )

    def submit_entry(self, record):
        self.calls.append(("submit_entry", record.client_order_id))
        if self.submit_error:
            raise self.submit_error
        return record.model_copy(
            update={
                "state": ExecutionState.ENTRY_PENDING,
                "broker_order_id": "broker-entry",
            }
        )

    def reconcile(self, record, *, allow_writes):
        self.calls.append(("reconcile", record.state, allow_writes))
        if self.reconcile_error:
            raise self.reconcile_error
        if self.reconcile_write_unknown:
            return record.model_copy(
                update={
                    "broker_state": {
                        **record.broker_state,
                        "write_unknown": "protection",
                    },
                    "needs_attention": True,
                }
            )
        if record.state == ExecutionState.ENTRY_PENDING:
            return record.model_copy(
                update={
                    "state": ExecutionState.OPEN,
                    "filled_quantity": record.plan.quantity,
                    "remaining_quantity": record.plan.quantity,
                }
            )
        return record

    def request_exit(self, record, *, reason):
        self.calls.append(("request_exit", reason))
        if self.exit_error:
            raise self.exit_error
        return record.model_copy(update={"state": ExecutionState.EXIT_PENDING})

    def cancel_entry(self, record):
        self.calls.append(("cancel_entry", record.id))
        if self.cancel_error:
            raise self.cancel_error
        return record.model_copy(
            update={"broker_state": {**record.broker_state, "cancel": True}}
        )

    def account_snapshot(
        self,
        _plan,
        *,
        account_profile=None,
        broker_metadata=None,
    ):
        del broker_metadata
        self.calls.append(("account_snapshot", account_profile))
        if self.account_error:
            raise self.account_error
        return AccountSnapshot(
            broker="okx",
            account_profile=account_profile or "okx-live",
            equity=Decimal("1000"),
        )


class FallbackFakeAdapter(FakeAdapter):
    def preflight(self, plan):
        result = super().preflight(plan)
        return result.model_copy(
            update={
                "selected_account": "comprehensive",
                "account_identity": "longbridge-comprehensive",
            }
        )

    def prepare_submit(self, record):
        prepared = super().prepare_submit(record)
        return prepared.model_copy(
            update={"selected_account": "comprehensive"}
        )

    def submit_entry(self, record):
        return self.write_executor(
            lambda: FakeAdapter.submit_entry(self, record)
        )


class ClearingAttentionFakeAdapter(FakeAdapter):
    def reconcile(self, record, *, allow_writes):
        self.calls.append(("reconcile", record.state, allow_writes))
        if not allow_writes:
            return record.model_copy(
                update={
                    "needs_attention": False,
                    "last_error": "",
                    "state_reason": "read-only reconciliation confirmed",
                }
            )
        return record


def _settings():
    settings = Settings()
    settings.execution.enabled = True
    settings.execution.selected_broker = "okx"
    settings.execution.okx.source_symbol = "XAU-USDT-SWAP"
    settings.execution.okx.instrument = "XAU-USDT-SWAP"
    settings.execution.okx.quantity = "2"
    settings.execution.okx.product = "swap"
    return settings


def _service(
    tmp_path,
    monkeypatch,
    adapter,
    *,
    gate=True,
    paper_gate=True,
    settings=None,
):
    active_settings = settings or _settings()
    route = (
        active_settings.execution.longbridge
        if active_settings.execution.selected_broker == "longbridge"
        else active_settings.execution.okx
    )
    record = _record(symbol=route.source_symbol)
    monkeypatch.setattr("pa_agent.config.paths.RECORDS_PENDING_DIR", tmp_path)
    path = _persist(record, tmp_path)
    service = ExecutionService(
        settings=active_settings,
        pending_writer=FakePendingWriter(path),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        adapter_factories={
            "okx": lambda _plan: adapter,
            "longbridge": lambda _plan: adapter,
        },
        gate_checker=lambda: gate,
        paper_gate_checker=lambda: paper_gate,
        okx_live_gate_checker=lambda: True,
    )
    return service, record


def test_session_gate_requires_hard_gate_and_exact_confirmation(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, monkeypatch, FakeAdapter(), gate=False)

    with pytest.raises(LiveTradingDisabled):
        service.arm("启用实盘交易")

    assert service.is_armed is False


def test_disabled_execution_module_cannot_be_armed(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, monkeypatch, FakeAdapter())
    service._settings.execution.enabled = False

    with pytest.raises(LiveTradingDisabled, match="执行模块"):
        service.arm("启用实盘交易")

    assert service.is_armed is False


def test_worker_lease_is_the_new_risk_authority_after_plan_creation(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service._new_risk_authorizer = lambda _plan, _account: True
    # GUI/实验脚本签发短租约前已经检查 execution.enabled。Worker 可能读取到
    # 更新后的界面配置，但不能因此悄悄作废已绑定的计划与租约。
    service._settings.execution.enabled = False

    submitted = service.submit(execution.id)

    assert submitted.state is ExecutionState.ENTRY_PENDING


def test_monitor_fails_when_expected_account_snapshot_cannot_refresh(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    service.arm("启用实盘交易")
    execution = service.prepare_analysis(record)
    service.submit(execution.id)
    adapter.account_error = RuntimeError("account snapshot unavailable")

    with pytest.raises(RuntimeError, match="账户快照刷新失败"):
        service.monitor_once()


def test_fallback_account_reaches_the_final_broker_write_authorizer(
    tmp_path,
    monkeypatch,
):
    settings = _settings()
    settings.execution.selected_broker = "longbridge"
    settings.execution.longbridge.source_symbol = "GLD.US"
    settings.execution.longbridge.instrument = "GLD.US"
    settings.execution.longbridge.quantity = "1"
    settings.execution.longbridge.preferred_account = "intraday"
    settings.execution.longbridge.allow_comprehensive_fallback = True
    adapter = FallbackFakeAdapter()
    service, record = _service(
        tmp_path,
        monkeypatch,
        adapter,
        settings=settings,
    )
    execution = service.prepare_analysis(record)
    authorized_accounts = []

    def _authorize(_plan, account):
        authorized_accounts.append(account)
        return account in {"intraday", "comprehensive"}

    service._new_risk_authorizer = _authorize

    submitted = service.submit(execution.id)

    assert submitted.state is ExecutionState.ENTRY_PENDING
    assert authorized_accounts[0] == "intraday"
    assert authorized_accounts[-1] == "comprehensive"
    assert [call for call in adapter.calls if call[0] == "submit_entry"]


def test_okx_live_requires_its_independent_hard_gate(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, monkeypatch, FakeAdapter())
    service._okx_live_gate_checker = lambda: False

    with pytest.raises(LiveTradingDisabled, match="OKX_LIVE_ENABLED"):
        service.arm("启用实盘交易")

    service._settings.execution.okx.simulated = True
    assert service.arm_confirmation_text() == "启用模拟交易"
    service.arm("启用模拟交易")
    assert service.is_armed is True


def test_longbridge_paper_uses_independent_gate_and_rearms_after_switch(
    tmp_path,
    monkeypatch,
):
    settings = _settings()
    settings.execution.selected_broker = "longbridge"
    settings.execution.longbridge.source_symbol = "GLD.US"
    settings.execution.longbridge.instrument = "GLD.US"
    settings.execution.longbridge.quantity = "1"
    settings.execution.longbridge.preferred_account = "paper"
    service, _ = _service(
        tmp_path,
        monkeypatch,
        FakeAdapter(),
        gate=False,
        paper_gate=True,
        settings=settings,
    )

    assert service.arm_confirmation_text() == "启用模拟交易"
    with pytest.raises(LiveTradingDisabled, match="确认文字"):
        service.arm("启用实盘交易")

    service.arm("启用模拟交易")
    assert service.is_armed is True

    settings.execution.longbridge.preferred_account = "comprehensive"
    service.reload_settings(settings)
    assert service.is_armed is False
    assert service.arm_confirmation_text() == "启用实盘交易"
    with pytest.raises(LiveTradingDisabled, match="PA_AGENT_LIVE"):
        service.arm("启用实盘交易")


def test_prepare_is_idempotent_and_never_auto_submits_when_disarmed(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)

    first = service.prepare_analysis(record)
    second = service.prepare_analysis(record)

    assert first.id == second.id
    assert first.state == ExecutionState.READY
    assert not adapter.calls
    assert len(service.store.list_recent()) == 1


def test_codex_subscription_never_auto_submits_live_plan(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    settings = _settings()
    settings.provider.adapter_id = "codex_subscription"
    settings.provider.model = "auto"
    settings.execution.auto_execute = True
    service, record = _service(
        tmp_path,
        monkeypatch,
        adapter,
        settings=settings,
    )
    service.arm("启用实盘交易")

    execution = service.prepare_analysis(record)

    assert execution.state == ExecutionState.READY
    assert not adapter.calls
    assert [event.kind for event in service.store.events(execution.id)] == [
        "plan_created",
        "human_review_required",
    ]


def test_codex_subscription_can_auto_submit_demo_plan(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    settings = _settings()
    settings.provider.adapter_id = "codex_subscription"
    settings.provider.model = "auto"
    settings.execution.auto_execute = True
    settings.execution.okx.simulated = True
    service, record = _service(
        tmp_path,
        monkeypatch,
        adapter,
        settings=settings,
    )
    service.arm("启用模拟交易")

    execution = service.prepare_analysis(record)

    assert execution.state == ExecutionState.ENTRY_PENDING
    assert ("submit_entry", "client-entry") in adapter.calls


def test_expire_unsubmitted_ready_plan_is_local_only(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)

    expired = service.expire_unsubmitted(
        execution.id,
        reason="新的已收盘 K 线已出现",
    )

    assert expired.state == ExecutionState.CANCELED
    assert expired.state_reason == "新的已收盘 K 线已出现"
    assert not adapter.calls
    assert [event.kind for event in service.store.events(execution.id)] == [
        "plan_created",
        "ready_expired",
    ]


def test_submit_persists_intent_before_broker_call(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")

    submitted = service.submit(execution.id)

    assert submitted.state == ExecutionState.ENTRY_PENDING
    assert submitted.client_order_id == "client-entry"
    assert submitted.broker_order_id == "broker-entry"
    assert [event.kind for event in service.store.events(execution.id)] == [
        "plan_created",
        "preflight_passed",
        "submit_intent",
        "entry_accepted",
    ]


def test_unknown_submit_is_not_retried_and_disarms_session(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    adapter.submit_error = SubmissionUnknown("timeout")
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")

    unknown = service.submit(execution.id)

    assert unknown.state == ExecutionState.UNKNOWN
    assert unknown.client_order_id == "client-entry"
    assert service.is_armed is False
    assert [call[0] for call in adapter.calls].count("submit_entry") == 1


def test_unknown_submit_save_failure_rotates_runtime_and_never_resubmits(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    adapter.submit_error = SubmissionUnknown("timeout")
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    old_runtime = service._runtime_id
    original_save = service.store.save

    def fail_unknown_save(record, *, event_kind, event_payload=None):
        if event_kind == "entry_submit_unknown":
            raise RuntimeError("simulated sqlite commit failure")
        return original_save(
            record,
            event_kind=event_kind,
            event_payload=event_payload,
        )

    monkeypatch.setattr(service.store, "save", fail_unknown_save)
    with pytest.raises(RuntimeError, match="sqlite commit failure"):
        service.submit(execution.id)

    assert service.is_armed is False
    assert service._runtime_id != old_runtime
    assert [call[0] for call in adapter.calls].count("submit_entry") == 1

    monkeypatch.setattr(service.store, "save", original_save)
    adapter.submit_error = None
    service.arm("启用实盘交易")
    service.reconcile_once()

    assert [call[0] for call in adapter.calls].count("submit_entry") == 1
    persisted = service.store.get(execution.id)
    assert persisted is not None
    assert persisted.state == ExecutionState.UNKNOWN
    assert "submit_interrupted" in [
        event.kind for event in service.store.events(execution.id)
    ]


def test_disarmed_reconcile_keeps_risk_reducing_writes_available(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    service.disarm()

    updates = service.reconcile_once()

    assert updates[0].state == ExecutionState.OPEN
    assert ("reconcile", submitted.state, True) in adapter.calls


def test_missing_protection_attention_does_not_block_safe_repair_write(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    active = service.store.save(
        execution.model_copy(
            update={
                "state": ExecutionState.OPEN,
                "selected_account": "okx",
                "account_identity": "okx-account-a",
                "filled_quantity": execution.plan.quantity,
                "remaining_quantity": execution.plan.quantity,
                "needs_attention": True,
                "state_reason": "protection missing",
            }
        ),
        event_kind="test_missing_protection",
    )

    service.reconcile_once()

    assert (
        "reconcile",
        active.state,
        True,
    ) in adapter.calls


def test_persistent_write_block_requires_one_clean_read_only_reconcile(
    tmp_path,
    monkeypatch,
):
    adapter = ClearingAttentionFakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    broker_state = {
        "risk_reducing_writes_blocked": "broker_rejected",
    }
    active = service.store.save(
        execution.model_copy(
            update={
                "state": ExecutionState.OPEN,
                "selected_account": "okx",
                "account_identity": "okx-account-a",
                "filled_quantity": execution.plan.quantity,
                "remaining_quantity": execution.plan.quantity,
                "broker_state": broker_state,
                "needs_attention": True,
                "state_reason": "previous broker rejection",
            }
        ),
        event_kind="test_persistent_write_block",
    )

    first_updates = service.reconcile_once()
    first = service.store.get(active.id)
    second_updates = service.reconcile_once()

    assert first_updates[0].id == active.id
    assert first.needs_attention is False
    assert "risk_reducing_writes_blocked" not in first.broker_state
    assert second_updates == []
    assert adapter.calls == [
        ("reconcile", ExecutionState.OPEN, False),
        ("reconcile", ExecutionState.OPEN, True),
    ]


def test_reconcile_filter_never_connects_unowned_execution(
    tmp_path,
    monkeypatch,
):
    owned_adapter = FakeAdapter()
    unrelated_adapter = FakeAdapter()
    unrelated_adapter.identity = "okx-account-b"
    service, owned_record = _service(
        tmp_path,
        monkeypatch,
        owned_adapter,
    )
    owned = service.prepare_analysis(owned_record)

    unrelated_record = _record(direction="做空")
    _persist(unrelated_record, tmp_path)
    unrelated = service.prepare_analysis(unrelated_record)
    owned = service.store.save(
        owned.model_copy(
            update={
                "state": ExecutionState.ENTRY_PENDING,
                "selected_account": "okx",
                "account_identity": "okx-account-a",
            }
        ),
        event_kind="test_owned_active",
    )
    service.store.save(
        unrelated.model_copy(
            update={
                "state": ExecutionState.ENTRY_PENDING,
                "selected_account": "okx",
                "account_identity": "okx-account-b",
            }
        ),
        event_kind="test_unrelated_active",
    )
    service._adapters.clear()
    service._adapter_factories = {
        "okx": lambda plan: (
            owned_adapter
            if plan.id == owned.plan.id
            else unrelated_adapter
        )
    }

    updates = service.reconcile_once(execution_ids=[owned.id])

    assert [record.id for record in updates] == [owned.id]
    assert any(call[0] == "reconcile" for call in owned_adapter.calls)
    assert unrelated_adapter.calls == []


def test_account_refresh_is_read_only_and_persisted(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)

    snapshot = service.refresh_account(execution.id)

    assert snapshot.equity == Decimal("1000")
    persisted = service.store.latest_account_snapshot("okx", "okx-live")
    assert persisted is not None
    assert persisted.equity == Decimal("1000")


def test_route_account_refresh_does_not_mutate_or_use_daily_broker(
    tmp_path,
):
    settings = _settings()
    settings.execution.selected_broker = "longbridge"
    settings.execution.longbridge.preferred_account = "comprehensive"
    settings.execution.okx.instrument = "BTC-USDT"
    settings.execution.okx.source_symbol = "BTC-USDT"
    settings.execution.okx.product = "spot"
    settings.execution.okx.api_base_url = "https://attacker.invalid"
    longbridge_adapter = FakeAdapter()
    okx_adapter = FakeAdapter()
    captured = {}

    def _okx_factory(plan):
        captured["plan"] = plan
        return okx_adapter

    service = ExecutionService(
        settings=settings,
        pending_writer=FakePendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        adapter_factories={
            "longbridge": lambda _plan: longbridge_adapter,
            "okx": _okx_factory,
        },
        gate_checker=lambda: False,
        paper_gate_checker=lambda: False,
        okx_live_gate_checker=lambda: False,
    )

    snapshot = service.refresh_account_route(
        broker="okx",
        environment="demo",
        account="okx",
    )

    assert snapshot.equity == Decimal("1000")
    assert settings.execution.selected_broker == "longbridge"
    assert ("account_snapshot", None) in okx_adapter.calls
    assert longbridge_adapter.calls == []
    assert captured["plan"].instrument == "XAU-USDT-SWAP"
    assert captured["plan"].product == "swap"
    assert captured["plan"].environment == "demo"
    assert captured["plan"].okx_api_base_url == "https://www.okx.com"


def test_route_account_refresh_rejects_invalid_environment_account_pair(
    tmp_path,
):
    settings = _settings()
    service = ExecutionService(
        settings=settings,
        pending_writer=FakePendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        adapter_factories={"okx": lambda _plan: FakeAdapter()},
    )

    with pytest.raises(
        PreflightError,
        match="只允许 OKX Demo campaign",
    ):
        service.refresh_account_route(
            broker="okx",
            environment="demo",
            account="comprehensive",
        )


def test_daily_okx_live_adapter_is_never_reused_for_campaign_demo(
    tmp_path,
):
    settings = _settings()
    settings.execution.okx.simulated = False
    settings.execution.okx.instrument = "BTC-USDT"
    settings.execution.okx.source_symbol = "BTC-USDT"
    settings.execution.okx.product = "spot"
    settings.execution.okx.api_base_url = "https://live-gateway.example"
    captured_plans = []
    created_adapters = []

    def _factory(plan):
        captured_plans.append(plan)
        adapter = FakeAdapter()
        created_adapters.append(adapter)
        return adapter

    service = ExecutionService(
        settings=settings,
        pending_writer=FakePendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        adapter_factories={"okx": _factory},
    )

    service.refresh_account()
    service.refresh_account_route(
        broker="okx",
        environment="demo",
        account="okx",
    )

    assert len(created_adapters) == 2
    assert captured_plans[0].environment == "live"
    assert captured_plans[0].instrument == "BTC-USDT"
    assert captured_plans[0].okx_api_base_url == (
        "https://live-gateway.example"
    )
    assert captured_plans[1].environment == "demo"
    assert captured_plans[1].instrument == "XAU-USDT-SWAP"
    assert captured_plans[1].okx_api_base_url == "https://www.okx.com"
    assert (
        captured_plans[0].config_fingerprint
        != captured_plans[1].config_fingerprint
    )


def test_adapter_cache_never_trusts_fingerprint_as_route_identity(
    tmp_path,
):
    settings = _settings()
    adapters = []

    def _factory(_plan):
        adapter = FakeAdapter()
        adapters.append(adapter)
        return adapter

    service = ExecutionService(
        settings=settings,
        pending_writer=FakePendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        adapter_factories={"okx": _factory},
    )
    live = service._target_plan_for_route(
        broker="okx",
        environment="live",
        account="okx",
    )
    demo = live.model_copy(
        update={
            "environment": "demo",
            "product": "swap",
            "instrument": "XAU-USDT-SWAP",
            "okx_api_base_url": "https://www.okx.com",
            "config_fingerprint": live.config_fingerprint,
        }
    )

    live_adapter = service._adapter(live)
    demo_adapter = service._adapter(demo)

    assert live_adapter is not demo_adapter
    assert len(adapters) == 2


def test_same_route_second_execution_binds_its_own_final_authorizer(
    tmp_path,
):
    settings = _settings()
    adapters = []
    authorized_plan_ids = []

    def _factory(_plan):
        adapter = FakeAdapter()
        adapters.append(adapter)
        return adapter

    def _authorize(plan, _account):
        authorized_plan_ids.append(plan.id)
        return True

    service = ExecutionService(
        settings=settings,
        pending_writer=FakePendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(tmp_path / "execution.sqlite3"),
        adapter_factories={"okx": _factory},
        paper_gate_checker=lambda: True,
        new_risk_authorizer=_authorize,
    )
    base = service._target_plan_for_route(
        broker="okx",
        environment="demo",
        account="okx",
    )
    first_plan = base.model_copy(update={"id": "execution-1"})
    second_plan = base.model_copy(update={"id": "execution-2"})

    first_adapter = service._adapter(first_plan)
    first_adapter.write_executor(lambda: None)
    second_adapter = service._adapter(second_plan)
    second_adapter.write_executor(lambda: None)

    assert first_adapter is not second_adapter
    assert len(adapters) == 2
    assert authorized_plan_ids == ["execution-1", "execution-2"]


def test_legacy_active_execution_without_identity_cannot_refresh_account(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    legacy_active = service.store.save(
        execution.model_copy(
            update={
                "state": ExecutionState.ENTRY_PENDING,
                "selected_account": "okx",
                "account_identity": "",
            }
        ),
        event_kind="legacy_active_fixture",
    )
    service.arm("启用实盘交易")

    with pytest.raises(CredentialError, match="身份指纹"):
        service.refresh_account(legacy_active.id)

    assert service.is_armed is False
    assert not [
        call for call in adapter.calls if call[0] == "account_snapshot"
    ]


def test_monitor_once_refreshes_actual_selected_account(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    service.store.save(
        submitted.model_copy(update={"selected_account": "comprehensive"}),
        event_kind="test_account_fallback",
    )
    adapter.calls.clear()

    service._monitor_once()

    assert ("account_snapshot", "comprehensive") in adapter.calls


def test_monitor_once_refreshes_selected_account_without_active_execution(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, _record = _service(tmp_path, monkeypatch, adapter)

    updates, snapshots = service.monitor_once()

    assert updates == []
    assert len(snapshots) == 1
    assert snapshots[0].account_profile == "okx-live"
    assert ("account_snapshot", None) in adapter.calls


def test_unknown_reconcile_write_disarms_entire_session(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    service.submit(execution.id)
    adapter.reconcile_write_unknown = True

    updates = service.reconcile_once()

    assert updates[0].broker_state["write_unknown"] == "protection"
    assert service.is_armed is False


def test_disarm_is_linearized_with_every_adapter_broker_write(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, analysis = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(analysis)
    service._adapter(execution.plan)
    service.arm("启用实盘交易")
    write_started = threading.Event()
    release_write = threading.Event()
    disarm_started = threading.Event()
    disarm_finished = threading.Event()
    errors: list[BaseException] = []

    def broker_write():
        write_started.set()
        if not release_write.wait(2):
            raise TimeoutError("test write release timeout")
        adapter.calls.append(("guarded_write",))

    def run_write():
        try:
            adapter.write_executor(broker_write)
        except BaseException as exc:
            errors.append(exc)

    def run_disarm():
        disarm_started.set()
        service.disarm()
        disarm_finished.set()

    writer = threading.Thread(target=run_write)
    writer.start()
    assert write_started.wait(2)
    disarmer = threading.Thread(target=run_disarm)
    disarmer.start()
    assert disarm_started.wait(2)
    assert disarm_finished.wait(0.1) is False

    release_write.set()
    writer.join(2)
    disarmer.join(2)

    assert errors == []
    assert disarm_finished.is_set()
    assert service.is_armed is False
    assert [call for call in adapter.calls if call[0] == "guarded_write"] == [
        ("guarded_write",)
    ]
    with pytest.raises(LiveTradingDisabled):
        adapter.write_executor(lambda: adapter.calls.append(("late_write",)))
    assert not [call for call in adapter.calls if call[0] == "late_write"]


def test_active_execution_blocks_account_switch_before_read_or_write(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    active = service.submit(execution.id)
    adapter.calls.clear()
    adapter.identity = "okx-account-b"

    updates = service.reconcile_once()

    assert updates[0].id == active.id
    assert updates[0].needs_attention is True
    assert updates[0].broker_state["identity_or_route_blocked"] is True
    assert service.is_armed is False
    assert not [call for call in adapter.calls if call[0] == "reconcile"]
    with pytest.raises(CredentialError, match="实际账户"):
        service.refresh_account(active.id)
    assert not [call for call in adapter.calls if call[0] == "account_snapshot"]


def test_restored_account_identity_clears_only_identity_write_block(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    service.submit(execution.id)
    adapter.identity = "okx-account-b"
    blocked = service.reconcile_once()[0]
    assert blocked.broker_state["risk_reducing_writes_blocked"] == (
        "identity_or_route_blocked"
    )

    adapter.identity = "okx-account-a"
    adapter.calls.clear()
    recovered = service.reconcile_once()[0]

    assert recovered.state is ExecutionState.OPEN
    assert recovered.needs_attention is False
    assert "identity_or_route_blocked" not in recovered.broker_state
    assert "risk_reducing_writes_blocked" not in recovered.broker_state
    assert ("reconcile", ExecutionState.ENTRY_PENDING, False) in adapter.calls


def test_manual_exit_account_switch_disarms_before_exit_intent(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    opened = service.reconcile_once()[0]
    assert opened.id == submitted.id
    assert opened.state == ExecutionState.OPEN
    adapter.calls.clear()
    adapter.identity = "okx-account-b"

    with pytest.raises(CredentialError, match="实际账户"):
        service.request_exit(opened.id, reason="manual")

    assert service.is_armed is False
    assert not [call for call in adapter.calls if call[0] == "request_exit"]


def test_manual_exit_rejection_is_durable_and_stops_all_writes(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    service.arm("启用实盘交易")
    execution = service.prepare_analysis(record)
    service.submit(execution.id)
    opened = service.reconcile_once()[0]
    adapter.exit_error = BrokerRejected("broker rejected exit")

    rejected = service.request_exit(opened.id, reason="manual")

    assert rejected.state is ExecutionState.OPEN
    assert rejected.needs_attention is True
    assert rejected.last_error == "broker rejected exit"
    assert rejected.broker_state["manual_exit_intent"] is True
    assert rejected.broker_state["manual_exit_reason"] == "manual"
    assert rejected.broker_state["risk_reducing_writes_blocked"] == (
        "request_exit_rejected"
    )
    assert service.is_armed is False
    with pytest.raises(LiveTradingDisabled):
        service.request_exit(opened.id, reason="manual")
    assert [event.kind for event in service.store.events(opened.id)][-2:] == [
        "exit_intent",
        "exit_requested",
    ]


def test_manual_exit_unknown_is_never_retried_and_requires_reconciliation(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    service.arm("启用实盘交易")
    execution = service.prepare_analysis(record)
    service.submit(execution.id)
    opened = service.reconcile_once()[0]
    adapter.exit_error = SubmissionUnknown("exit timeout")

    unknown = service.request_exit(opened.id, reason="manual")

    assert unknown.state is ExecutionState.UNKNOWN
    assert unknown.needs_attention is True
    assert unknown.broker_state["write_unknown"] == "request_exit"
    assert unknown.broker_state["exit_status"] == "unknown"
    assert unknown.broker_state["risk_reducing_writes_blocked"] == (
        "request_exit_unknown"
    )
    assert service.is_armed is False
    with pytest.raises(LiveTradingDisabled):
        service.request_exit(opened.id, reason="manual")
    assert len(
        [call for call in adapter.calls if call[0] == "request_exit"]
    ) == 1


def test_manual_exit_transport_before_delivery_is_durable_attention(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    service.arm("启用实盘交易")
    execution = service.prepare_analysis(record)
    service.submit(execution.id)
    opened = service.reconcile_once()[0]
    adapter.exit_error = BrokerTransportError(
        "connection refused",
        write_may_have_reached=False,
    )

    failed = service.request_exit(opened.id, reason="manual")

    assert failed.state is ExecutionState.OPEN
    assert failed.needs_attention is True
    assert failed.broker_state["manual_exit_intent"] is True
    assert "未送达" in failed.state_reason
    assert service.is_armed is False


def test_persistent_exit_block_survives_restart_until_clean_reconcile(
    tmp_path,
    monkeypatch,
):
    first_adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, first_adapter)
    service.arm("启用实盘交易")
    execution = service.prepare_analysis(record)
    service.submit(execution.id)
    opened = service.reconcile_once()[0]
    first_adapter.exit_error = BrokerRejected("exit rejected")
    rejected = service.request_exit(opened.id, reason="manual")
    restarted_adapter = ClearingAttentionFakeAdapter()
    restarted = ExecutionService(
        settings=_settings(),
        pending_writer=FakePendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(service.store.path),
        adapter_factories={"okx": lambda _plan: restarted_adapter},
        gate_checker=lambda: True,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: True,
    )

    with pytest.raises(LiveTradingDisabled, match="持久停写标记"):
        restarted.request_exit(rejected.id, reason="manual")
    assert not [
        call
        for call in restarted_adapter.calls
        if call[0] == "request_exit"
    ]

    restarted.reconcile_once()
    cleared = restarted.store.get(rejected.id)
    assert "risk_reducing_writes_blocked" not in cleared.broker_state
    exited = restarted.request_exit(rejected.id, reason="manual")

    assert exited.state is ExecutionState.EXIT_PENDING
    assert len(
        [
            call
            for call in restarted_adapter.calls
            if call[0] == "request_exit"
        ]
    ) == 1


def test_persistent_cancel_block_survives_restart_until_clean_reconcile(
    tmp_path,
    monkeypatch,
):
    first_adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, first_adapter)
    service.arm("启用实盘交易")
    execution = service.prepare_analysis(record)
    submitted = service.submit(execution.id)
    first_adapter.cancel_error = BrokerRejected("cancel rejected")
    rejected = service.cancel_entry(submitted.id)
    restarted_adapter = ClearingAttentionFakeAdapter()
    restarted = ExecutionService(
        settings=_settings(),
        pending_writer=FakePendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(service.store.path),
        adapter_factories={"okx": lambda _plan: restarted_adapter},
        gate_checker=lambda: True,
        paper_gate_checker=lambda: True,
        okx_live_gate_checker=lambda: True,
    )

    with pytest.raises(LiveTradingDisabled, match="持久停写标记"):
        restarted.cancel_entry(rejected.id)
    assert not [
        call
        for call in restarted_adapter.calls
        if call[0] == "cancel_entry"
    ]

    restarted.reconcile_once()
    cleared = restarted.store.get(rejected.id)
    assert "risk_reducing_writes_blocked" not in cleared.broker_state
    canceled = restarted.cancel_entry(rejected.id)

    assert canceled.broker_state["cancel"] is True
    assert len(
        [
            call
            for call in restarted_adapter.calls
            if call[0] == "cancel_entry"
        ]
    ) == 1


def test_restart_turns_persisted_submitting_intent_into_unknown_without_resubmit(
    tmp_path,
    monkeypatch,
):
    first_adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, first_adapter)
    execution = service.prepare_analysis(record)
    interrupted = service.store.save(
        execution.model_copy(
            update={
                "state": ExecutionState.SUBMITTING,
                "selected_account": "okx",
                "client_order_id": "persisted-client-id",
                "broker_state": {"entry_kind": "regular"},
            }
        ),
        event_kind="submit_intent",
    )
    restarted_adapter = FakeAdapter()
    restarted = ExecutionService(
        settings=_settings(),
        pending_writer=FakePendingWriter(tmp_path / "unused.json"),
        store=ExecutionStore(service.store.path),
        adapter_factories={"okx": lambda _plan: restarted_adapter},
        gate_checker=lambda: True,
        okx_live_gate_checker=lambda: True,
    )

    restarted.reconcile_once()
    recovered = restarted.store.get(interrupted.id)

    assert restarted.is_armed is False
    assert recovered is not None
    assert recovered.state == ExecutionState.UNKNOWN
    assert recovered.client_order_id == "persisted-client-id"
    assert not [call for call in restarted_adapter.calls if call[0] == "submit_entry"]
    assert "submit_interrupted" in [
        event.kind for event in restarted.store.events(interrupted.id)
    ]


def test_write_result_save_failure_rotates_runtime_and_prevents_reconcile_resubmit(
    tmp_path,
    monkeypatch,
):
    class MarkerAdapter(FakeAdapter):
        def reconcile(self, record, *, allow_writes):
            self.calls.append(("reconcile", record.state, allow_writes))
            marker = str(record.broker_state.get("submit_runtime_id") or "")
            if marker and marker != self.runtime_id:
                return record.model_copy(
                    update={
                        "broker_state": {
                            **record.broker_state,
                            "write_unknown": "protection",
                        },
                        "needs_attention": True,
                    }
                )
            if allow_writes:
                self.write_executor(
                    lambda: self.calls.append(("broker_write", marker))
                )
                return record.model_copy(update={"state": ExecutionState.OPEN})
            return record

    adapter = MarkerAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    old_runtime = adapter.runtime_id
    service.store.save(
        submitted.model_copy(
            update={
                "state": ExecutionState.PROTECTING,
                "broker_state": {
                    **submitted.broker_state,
                    "submit_runtime_id": old_runtime,
                },
            }
        ),
        event_kind="protection_intent_fixture",
    )
    original_save = service.store.save
    failed = False

    def fail_first_reconciled_save(record, *, event_kind, event_payload=None):
        nonlocal failed
        if event_kind == "reconciled" and not failed:
            failed = True
            raise RuntimeError("simulated sqlite commit failure")
        return original_save(
            record,
            event_kind=event_kind,
            event_payload=event_payload,
        )

    monkeypatch.setattr(service.store, "save", fail_first_reconciled_save)
    with pytest.raises(RuntimeError, match="sqlite commit failure"):
        service.reconcile_once()

    assert service.is_armed is False
    assert service._runtime_id != old_runtime
    assert len([call for call in adapter.calls if call[0] == "broker_write"]) == 1

    monkeypatch.setattr(service.store, "save", original_save)
    service.arm("启用实盘交易")
    service.reconcile_once()

    assert len([call for call in adapter.calls if call[0] == "broker_write"]) == 1
    persisted = service.store.get(submitted.id)
    assert persisted is not None
    assert persisted.broker_state["write_unknown"] == "protection"


def test_cancel_result_save_failure_rotates_runtime_and_prevents_cancel_resubmit(
    tmp_path,
    monkeypatch,
):
    class CancelMarkerAdapter(FakeAdapter):
        def cancel_entry(self, record):
            self.write_executor(
                lambda: self.calls.append(("broker_cancel", record.id))
            )
            return record.model_copy(
                update={
                    "broker_state": {
                        **record.broker_state,
                        "entry_cancel_requested": True,
                    }
                }
            )

        def reconcile(self, record, *, allow_writes):
            self.calls.append(("reconcile", record.state, allow_writes))
            marker = str(
                record.broker_state.get("entry_cancel_runtime_id") or ""
            )
            if marker and marker != self.runtime_id:
                return record.model_copy(
                    update={
                        "broker_state": {
                            **record.broker_state,
                            "entry_cancel_status": "unknown",
                            "write_unknown": "cancel_entry",
                        },
                        "needs_attention": True,
                    }
                )
            return record

    adapter = CancelMarkerAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    old_runtime = adapter.runtime_id
    original_save = service.store.save
    failed = False

    def fail_first_cancel_save(record, *, event_kind, event_payload=None):
        nonlocal failed
        if event_kind == "cancel_entry_requested" and not failed:
            failed = True
            raise RuntimeError("simulated sqlite commit failure")
        return original_save(
            record,
            event_kind=event_kind,
            event_payload=event_payload,
        )

    monkeypatch.setattr(service.store, "save", fail_first_cancel_save)
    with pytest.raises(RuntimeError, match="sqlite commit failure"):
        service.cancel_entry(submitted.id)

    assert service.is_armed is False
    assert service._runtime_id != old_runtime
    assert len([call for call in adapter.calls if call[0] == "broker_cancel"]) == 1

    monkeypatch.setattr(service.store, "save", original_save)
    service.arm("启用实盘交易")
    service.reconcile_once()

    assert len([call for call in adapter.calls if call[0] == "broker_cancel"]) == 1
    persisted = service.store.get(submitted.id)
    assert persisted is not None
    assert persisted.broker_state["write_unknown"] == "cancel_entry"


def test_private_preflight_api_error_is_persisted_as_blocked(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    adapter.preflight_error = BrokerApiError("50120", "permission denied")
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")

    blocked = service.submit(execution.id)

    assert blocked.state == ExecutionState.BLOCKED
    assert blocked.needs_attention is True
    assert not [call for call in adapter.calls if call[0] == "submit_entry"]


def test_ready_plan_is_blocked_when_route_changes_before_submit(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service._settings.execution.okx.api_base_url = "https://other.example"
    service.arm("启用实盘交易")

    blocked = service.submit(execution.id)

    assert blocked.state == ExecutionState.BLOCKED
    assert "配置已变化" in blocked.state_reason
    assert not [call for call in adapter.calls if call[0] == "preflight"]


def test_active_okx_execution_rebuilds_adapter_from_plan_snapshot(
    tmp_path,
    monkeypatch,
):
    service, record = _service(tmp_path, monkeypatch, FakeAdapter())
    execution = service.prepare_analysis(record)
    captured = {}

    class CapturingClient:
        def __init__(self, _credentials, *, base_url, simulated):
            captured["base_url"] = base_url
            captured["simulated"] = simulated

    class CapturingAdapter:
        def __init__(
            self,
            _client,
            *,
            margin_mode,
            entry_timeout_seconds,
            runtime_id,
            write_executor,
        ):
            captured["margin_mode"] = margin_mode
            captured["entry_timeout_seconds"] = entry_timeout_seconds
            captured["runtime_id_set"] = bool(runtime_id)
            captured["write_executor_set"] = callable(write_executor)

        def bind_runtime_id(self, _runtime_id):
            pass

        def bind_write_executor(self, _write_executor):
            pass

    monkeypatch.setattr(
        "pa_agent.execution.service.load_okx_credentials",
        lambda environment: captured.setdefault(
            "credential_environment",
            environment,
        )
        or object(),
    )
    monkeypatch.setattr(
        "pa_agent.execution.service.OkxRestClient",
        CapturingClient,
    )
    monkeypatch.setattr(
        "pa_agent.execution.service.OkxAdapter",
        CapturingAdapter,
    )
    service._adapter_factories = {}
    service._settings.execution.okx.api_base_url = "https://new.example"
    service._settings.execution.okx.margin_mode = "isolated"
    service._settings.execution.okx.simulated = True
    service._settings.execution.entry_timeout_seconds = 999
    service.reload_settings(service._settings)

    service._adapter(execution.plan)

    assert captured == {
        "base_url": "https://www.okx.com",
        "simulated": False,
        "margin_mode": "cross",
        "entry_timeout_seconds": 120,
        "runtime_id_set": True,
        "write_executor_set": True,
        "credential_environment": "live",
    }

    service._adapters.clear()
    legacy = execution.plan.model_copy(update={"okx_api_base_url": ""})
    with pytest.raises(PreflightError, match="不可变路由快照"):
        service._adapter(legacy)


def test_post_entry_rejection_keeps_position_monitored_and_disarms(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    service.store.save(
        submitted.model_copy(
            update={
                "state": ExecutionState.PROTECTING,
                "filled_quantity": submitted.plan.quantity,
                "remaining_quantity": submitted.plan.quantity,
            }
        ),
        event_kind="filled",
    )
    adapter.reconcile_error = BrokerRejected("protection rejected")

    service.reconcile_once()
    current = service.store.get(submitted.id)

    assert current is not None
    assert current.state == ExecutionState.PROTECTING
    assert current.needs_attention is True
    assert service.is_armed is False


@pytest.mark.parametrize(
    "active_state",
    [ExecutionState.ENTRY_PENDING, ExecutionState.PARTIALLY_FILLED],
)
def test_reconcile_cancel_rejection_keeps_entry_active_and_disarms(
    tmp_path,
    monkeypatch,
    active_state,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    if active_state == ExecutionState.PARTIALLY_FILLED:
        submitted = service.store.save(
            submitted.model_copy(
                update={
                    "state": active_state,
                    "filled_quantity": Decimal("1"),
                    "remaining_quantity": Decimal("1"),
                }
            ),
            event_kind="test_partial_fill",
        )
    adapter.reconcile_error = BrokerRejected("cancel rejected")

    updated = service.reconcile_once()[0]

    assert updated.state == active_state
    assert updated.needs_attention is True
    assert service.is_armed is False
    assert service.store.route_claim_owner(updated) == updated.id

    adapter.reconcile_error = None
    adapter.calls.clear()
    service.reconcile_once()
    assert ("reconcile", active_state, False) in adapter.calls


def test_manual_cancel_rejection_keeps_entry_monitored_and_disarms(
    tmp_path,
    monkeypatch,
):
    adapter = FakeAdapter()
    service, record = _service(tmp_path, monkeypatch, adapter)
    execution = service.prepare_analysis(record)
    service.arm("启用实盘交易")
    submitted = service.submit(execution.id)
    adapter.cancel_error = BrokerRejected("cancel rejected")

    updated = service.cancel_entry(submitted.id)

    assert updated.state == ExecutionState.ENTRY_PENDING
    assert updated.needs_attention is True
    assert updated.broker_state["entry_cancel_status"] == "rejected"
    assert "entry_cancel_intent" not in updated.broker_state
    assert "entry_cancel_runtime_id" not in updated.broker_state
    assert "entry_cancel_requested" not in updated.broker_state
    assert updated.broker_state["risk_reducing_writes_blocked"] == (
        "cancel_entry_rejected"
    )
    assert service.is_armed is False
