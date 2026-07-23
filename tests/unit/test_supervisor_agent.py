from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pa_agent.agents.supervisor import (
    SupervisorAgent,
    SupervisorGate,
    build_supervisor_input,
)
from pa_agent.agents.supervisor_models import (
    SupervisorDecision,
    SupervisorInputSnapshot,
    snapshot_digest,
)
from pa_agent.config.settings import Settings
from pa_agent.records.supervisor_writer import SupervisorWriter
from tests.unit.test_execution_plan_builder import _record


def _snapshot() -> SupervisorInputSnapshot:
    return SupervisorInputSnapshot(
        campaign_id="campaign-one",
        analysis_digest="analysis-one",
        symbol="XAU-USDT-SWAP",
        timeframe="15m",
        closed_bar_ts_open_ms=1_784_300_400_000,
        closed_bar={"ts_open": 1_784_300_400_000, "close": 4005},
        stage1_diagnosis={"gate_result": "proceed", "direction": "long"},
        stage2_decision={
            "decision": {
                "order_type": "市价单",
                "order_direction": "做多",
                "entry_price": 4005,
                "stop_loss_price": 3990,
            }
        },
        active_execution_count=0,
        account_equity_usdt="5000",
        max_buy="500",
        max_sell="500",
        technical_plan_quantity="120",
    )


class _FakeClient:
    def __init__(self, outputs=None, errors=None):
        self.outputs = list(outputs or [])
        self.errors = list(errors or [])
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.errors:
            raise self.errors.pop(0)
        output = self.outputs.pop(0)
        return SimpleNamespace(content=output)


class _FakeStreamClient:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def stream_chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(content=self.output)


def _agent(primary, backup=None) -> SupervisorAgent:
    return SupervisorAgent(
        primary_client=primary,
        primary_profile_id="primary-profile",
        primary_model_id="primary-model",
        backup_client=backup,
        backup_profile_id="backup-profile" if backup else "",
        backup_model_id="backup-model" if backup else "",
        prompt_text="只返回严格 JSON。",
    )


def test_primary_allow_is_strict_and_does_not_call_backup():
    primary = _FakeClient(outputs=['{"action":"allow_entry","reason":"结构和账户快照一致"}'])
    backup = _FakeClient(outputs=['{"action":"block_entry","reason":"备用不应被调用"}'])

    result = _agent(primary, backup).decide(_snapshot())

    assert result.action == "allow_entry"
    assert result.fallback_level == "primary"
    assert result.profile_id == "primary-profile"
    assert len(primary.calls) == 1
    assert backup.calls == []


def test_stream_only_client_uses_existing_ai_client_contract():
    client = _FakeStreamClient('{"action":"allow_entry","reason":"流式客户端"}')

    result = _agent(client).decide(_snapshot())

    assert result.action == "allow_entry"
    assert len(client.calls) == 1
    assert client.calls[0][1]["cancel_token"].is_set() is False


def test_primary_failure_uses_backup_with_the_same_snapshot_digest():
    primary = _FakeClient(errors=[TimeoutError("timeout")])
    backup = _FakeClient(outputs=['{"action":"block_entry","reason":"备用审查拒绝"}'])
    snapshot = _snapshot()

    result = _agent(primary, backup).decide(snapshot)

    assert result.action == "block_entry"
    assert result.fallback_level == "backup"
    assert result.profile_id == "backup-profile"
    primary_digest = json.loads(primary.calls[0][0][1]["content"])["input_snapshot_digest"]
    backup_digest = json.loads(backup.calls[0][0][1]["content"])["input_snapshot_digest"]
    assert primary_digest == backup_digest == snapshot_digest(snapshot)
    assert json.loads(primary.calls[0][0][1]["content"])["snapshot"] == json.loads(
        backup.calls[0][0][1]["content"]
    )["snapshot"]


@pytest.mark.parametrize(
    "content",
    [
        '{"action":"allow_entry","reason":"ok","quantity":"999"}',
        '{"action":"maybe","reason":"unknown"}',
        "not-json",
    ],
)
def test_invalid_supervisor_output_cannot_become_allow(content):
    primary = _FakeClient(outputs=[content])

    result = _agent(primary).decide(_snapshot())

    assert result.action == "block_entry"
    assert result.fallback_level == "deterministic"
    assert "主模型失败" in result.reason


def test_two_model_failures_are_deterministic_block():
    primary = _FakeClient(errors=[RuntimeError("primary")])
    backup = _FakeClient(errors=[ValueError("backup")])

    result = _agent(primary, backup).decide(_snapshot())

    assert result.action == "block_entry"
    assert result.fallback_level == "deterministic"
    assert "备用模型失败" in result.reason


def test_supervisor_decision_rejects_extra_fields():
    with pytest.raises(ValueError):
        SupervisorDecision.model_validate(
            {"action": "allow_entry", "reason": "ok", "instrument": "BTC-USDT"}
        )


def test_supervisor_snapshot_deeply_rejects_nested_mutation():
    snapshot = _snapshot()

    with pytest.raises(TypeError):
        snapshot.closed_bar["close"] = 9999
    with pytest.raises(TypeError):
        snapshot.stage1_diagnosis["new_field"] = "forbidden"
    with pytest.raises(TypeError):
        snapshot.stage2_decision["decision"] = {}


def test_ai_role_bindings_round_trip_without_copying_provider_secrets():
    settings = Settings()
    settings.ai_roles.supervisor_primary_profile_id = "primary"
    settings.ai_roles.supervisor_backup_profile_id = "backup"
    settings.ai_roles.pa_primary_profile_id = "pa-primary"
    settings.ai_roles.pa_backup_profile_id = "pa-backup"

    loaded = Settings.model_validate(settings.model_dump())

    assert loaded.ai_roles.supervisor_primary_profile_id == "primary"
    assert loaded.ai_roles.supervisor_backup_profile_id == "backup"
    assert loaded.ai_roles.pa_primary_profile_id == "pa-primary"
    assert loaded.ai_roles.pa_backup_profile_id == "pa-backup"
    assert "api_key" in loaded.provider.model_dump()


def test_gate_reuses_persisted_conclusion_without_second_model_call(tmp_path):
    primary = _FakeClient(outputs=['{"action":"allow_entry","reason":"首次放行"}'])
    writer = SupervisorWriter(tmp_path)
    gate = SupervisorGate(_agent(primary), writer)
    record = _record(symbol="XAU-USDT-SWAP").model_copy(
        update={
            "kline_data": [
                {
                    "seq": 1,
                    "ts_open": 1_784_300_400_000,
                    "open": 4000,
                    "high": 4010,
                    "low": 3990,
                    "close": 4005,
                    "volume": 100,
                    "closed": True,
                }
            ]
        }
    )
    sizing = SimpleNamespace(
        equity_usdt="5000",
        max_buy="10000",
        max_sell="10000",
        quantity="120",
    )
    review_kwargs = {
        "campaign_id": "campaign-one",
        "record": record,
        "bar_ms": 1_784_300_400_000,
        "analysis_digest": "analysis-one",
        "active_execution_count": 0,
        "sizing": sizing,
    }

    first = gate.review(**review_kwargs)
    second = gate.review(**review_kwargs)

    assert first.action == "allow_entry"
    assert second == first
    assert len(primary.calls) == 1


def test_new_active_execution_overrides_old_allow_without_model_call(tmp_path):
    primary = _FakeClient(
        outputs=['{"action":"allow_entry","reason":"不应再次调用"}']
    )
    writer = SupervisorWriter(tmp_path)
    gate = SupervisorGate(_agent(primary), writer)
    record = _record(symbol="XAU-USDT-SWAP").model_copy(
        update={
            "kline_data": [
                {
                    "seq": 1,
                    "ts_open": 1_784_300_400_000,
                    "open": 4000,
                    "high": 4010,
                    "low": 3990,
                    "close": 4005,
                    "volume": 100,
                    "closed": True,
                }
            ]
        }
    )
    sizing = SimpleNamespace(
        equity_usdt="5000",
        max_buy="10000",
        max_sell="10000",
        quantity="120",
    )
    snapshot = build_supervisor_input(
        campaign_id="campaign-one",
        record=record,
        bar_ms=1_784_300_400_000,
        analysis_digest="analysis-one",
        active_execution_count=0,
        sizing=sizing,
    )
    writer.save_durable(
        SupervisorAgent._record(
            snapshot,
            SupervisorDecision(action="allow_entry", reason="先前放行"),
            profile_id="primary-profile",
            model_id="primary-model",
            fallback_level="primary",
        )
    )

    result = gate.review(
        campaign_id="campaign-one",
        record=record,
        bar_ms=1_784_300_400_000,
        analysis_digest="analysis-one",
        active_execution_count=1,
        sizing=sizing,
    )

    assert result.action == "block_entry"
    assert result.fallback_level == "deterministic"
    assert primary.calls == []
    assert (
        writer.load_for_key(
            campaign_id="campaign-one",
            bar_ms=1_784_300_400_000,
            analysis_digest="analysis-one",
        ).action
        == "allow_entry"
    )
