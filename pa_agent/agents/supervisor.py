"""交易监督智能体：只负责 Demo 入场放行或拒绝。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pa_agent.agents.supervisor_models import (
    SupervisorDecision,
    SupervisorDecisionRecord,
    SupervisorInputSnapshot,
    snapshot_digest,
)
from pa_agent.ai.client_factory import create_ai_client
from pa_agent.config.settings import Settings
from pa_agent.records.schema import AnalysisRecord
from pa_agent.records.supervisor_writer import SupervisorWriter
from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)


class SupervisorConfigurationError(RuntimeError):
    """监督角色档案不存在、未验证或无法创建客户端。"""


class SupervisorAgent:
    """用主/备客户端对同一个不可变输入快照做严格决策。"""

    def __init__(
        self,
        *,
        primary_client: Any,
        primary_profile_id: str,
        primary_model_id: str,
        backup_client: Any | None = None,
        backup_profile_id: str = "",
        backup_model_id: str = "",
        prompt_text: str,
        timeout_s: float = 120.0,
    ) -> None:
        if not str(prompt_text or "").strip():
            raise ValueError("监督提示词不能为空")
        self.primary_client = primary_client
        self.primary_profile_id = str(primary_profile_id or "")
        self.primary_model_id = str(primary_model_id or "")
        self.backup_client = backup_client
        self.backup_profile_id = str(backup_profile_id or "")
        self.backup_model_id = str(backup_model_id or "")
        self.prompt_text = prompt_text
        self.timeout_s = float(timeout_s)

    @staticmethod
    def _parse_reply(reply: Any) -> SupervisorDecision:
        content = str(getattr(reply, "content", "") or "").strip()
        if not content:
            raise ValueError("监督模型返回空正文")
        payload = json.loads(content)
        return SupervisorDecision.model_validate(payload)

    def _call(self, client: Any, snapshot: SupervisorInputSnapshot) -> SupervisorDecision:
        messages = self._build_messages(snapshot)
        chat = getattr(client, "chat", None)
        if callable(chat):
            reply = chat(
                messages,
                thinking=False,
                reasoning_effort="medium",
                timeout_s=self.timeout_s,
                max_output_tokens=512,
            )
        else:
            stream_chat = getattr(client, "stream_chat", None)
            if not callable(stream_chat):
                raise TypeError("监督 AI 客户端缺少 chat/stream_chat 接口")
            reply = stream_chat(
                messages,
                thinking=False,
                reasoning_effort="medium",
                cancel_token=CancelToken(),
                timeout_s=self.timeout_s,
                max_output_tokens=512,
            )
        return self._parse_reply(reply)

    def _build_messages(self, snapshot: SupervisorInputSnapshot) -> list[dict[str, str]]:
        digest = snapshot_digest(snapshot)
        payload = {
            "input_snapshot_digest": digest,
            "snapshot": snapshot.model_dump(mode="json"),
        }
        return [
            {"role": "system", "content": self.prompt_text},
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]

    def decide(self, snapshot: SupervisorInputSnapshot) -> SupervisorDecisionRecord:
        errors: list[str] = []
        try:
            decision = self._call(self.primary_client, snapshot)
        except Exception as exc:
            errors.append(f"主模型失败:{type(exc).__name__}")
        else:
            return self._record(
                snapshot,
                decision,
                profile_id=self.primary_profile_id,
                model_id=self.primary_model_id,
                fallback_level="primary",
            )

        if self.backup_client is not None:
            try:
                # _call 重新从同一个 frozen snapshot 组装消息；摘要必须相同。
                decision = self._call(self.backup_client, snapshot)
            except Exception as exc:
                errors.append(f"备用模型失败:{type(exc).__name__}")
            else:
                return self._record(
                    snapshot,
                    decision,
                    profile_id=self.backup_profile_id,
                    model_id=self.backup_model_id,
                    fallback_level="backup",
                )
        else:
            errors.append("未配置备用模型")

        reason = "监督模型未能形成严格决策，确定性阻断新增风险。" + "；".join(errors)
        return self._record(
            snapshot,
            SupervisorDecision(action="block_entry", reason=reason),
            profile_id="",
            model_id="",
            fallback_level="deterministic",
        )

    @staticmethod
    def _record(
        snapshot: SupervisorInputSnapshot,
        decision: SupervisorDecision,
        *,
        profile_id: str,
        model_id: str,
        fallback_level: str,
    ) -> SupervisorDecisionRecord:
        return SupervisorDecisionRecord(
            record_id=SupervisorWriter.record_id(snapshot),
            campaign_id=snapshot.campaign_id,
            analysis_digest=snapshot.analysis_digest,
            closed_bar_ts_open_ms=snapshot.closed_bar_ts_open_ms,
            input_snapshot_digest=snapshot_digest(snapshot),
            input_snapshot=snapshot,
            action=decision.action,
            reason=decision.reason,
            profile_id=profile_id,
            model_id=model_id,
            fallback_level=fallback_level,
            created_at=datetime.now(UTC).isoformat(),
        )

def build_supervisor_input(
    *,
    campaign_id: str,
    record: AnalysisRecord,
    bar_ms: int,
    analysis_digest: str,
    active_execution_count: int,
    sizing: Any,
) -> SupervisorInputSnapshot:
    """从已经完成的 PA 记录和只读账户预检构造监督快照。"""

    if not record.kline_data or not isinstance(record.kline_data[0], dict):
        raise SupervisorConfigurationError("PA 记录缺少监督所需的已收盘 K 线")
    closed_bar = dict(record.kline_data[0])
    try:
        record_bar_ms = int(float(closed_bar["ts_open"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise SupervisorConfigurationError(
            "PA 记录的已收盘 K 线时间无效"
        ) from exc
    if record_bar_ms != int(bar_ms):
        raise SupervisorConfigurationError(
            "监督 K 线时间与 PA 耐久记录不一致"
        )
    if closed_bar.get("closed") is not True:
        raise SupervisorConfigurationError("监督只允许使用已收盘 K 线")
    stage1 = record.stage1_diagnosis
    stage2 = record.stage2_decision
    if not isinstance(stage1, dict) or not isinstance(stage2, dict):
        raise SupervisorConfigurationError("PA 记录缺少完整结构化决策")
    input_mode = (
        "controlled_reproducible"
        if (
            stage2.get("origin") == "controlled_reproducible_demo_s"
            and record.meta.market_data_provenance
            == "okx_public_5m_utc_pair_aggregation_controlled_reproducible"
        )
        else "natural_pa"
    )
    stage2_response = (
        record.stage2_response
        if isinstance(record.stage2_response, dict)
        else {}
    )
    leverage_intent = stage2_response.get("leverage_intent")
    if leverage_intent is not None and not isinstance(
        leverage_intent,
        dict,
    ):
        raise SupervisorConfigurationError("杠杆意图不是结构化对象")
    decision = stage2.get("decision")
    if not isinstance(decision, dict):
        raise SupervisorConfigurationError("PA 阶段二缺少可执行决策")
    signal_entry_price = str(decision.get("entry_price") or "")
    stop_loss_price = str(decision.get("stop_loss_price") or "")
    if not signal_entry_price or not stop_loss_price:
        raise SupervisorConfigurationError("PA 阶段二缺少入场价或止损价")
    stage2_snapshot = dict(stage2)
    stage2_snapshot["execution_context"] = {
        "signal_entry_price": signal_entry_price,
        "effective_entry_price": str(
            getattr(sizing, "reference_price_usdt", signal_entry_price)
        ),
        "stop_loss_price": stop_loss_price,
        "risk_equity_basis": str(
            getattr(sizing, "equity_basis", "usdt_equity")
        ),
        "risk_equity_usdt": str(sizing.equity_usdt),
        "risk_budget_usdt": str(
            getattr(sizing, "risk_budget_usdt", "")
        ),
        "technical_plan_quantity": str(sizing.quantity),
    }
    return SupervisorInputSnapshot(
        input_mode=input_mode,
        campaign_id=campaign_id,
        analysis_digest=analysis_digest,
        symbol=record.meta.symbol,
        timeframe=record.meta.timeframe,
        closed_bar_ts_open_ms=int(bar_ms),
        closed_bar=closed_bar,
        stage1_diagnosis=dict(stage1),
        stage2_decision=stage2_snapshot,
        leverage_intent=(
            dict(leverage_intent)
            if isinstance(leverage_intent, dict)
            else None
        ),
        active_execution_count=int(active_execution_count),
        account_equity_usdt=str(sizing.equity_usdt),
        max_buy=str(sizing.max_buy),
        max_sell=str(sizing.max_sell),
        technical_plan_quantity=str(sizing.quantity),
    )


class SupervisorGate:
    """先复用同一 K 线结论，再持久化新结论，最后才允许建计划。"""

    def __init__(self, agent: SupervisorAgent, writer: SupervisorWriter) -> None:
        self.agent = agent
        self.writer = writer

    def review(
        self,
        *,
        campaign_id: str,
        record: AnalysisRecord,
        bar_ms: int,
        analysis_digest: str,
        active_execution_count: int,
        sizing: Any,
    ) -> SupervisorDecisionRecord:
        # 同一 Campaign、同一 K 线、同一 PA 记录优先复用原快照，不拿变化后的账户状态
        # 重新调用模型，也不把新动态字段伪装成“同一次”监督输入。
        existing = self.writer.load_for_key(
            campaign_id=campaign_id,
            bar_ms=bar_ms,
            analysis_digest=analysis_digest,
        )
        if existing is not None and (
            active_execution_count == 0 or existing.action == "block_entry"
        ):
            return existing

        snapshot = build_supervisor_input(
            campaign_id=campaign_id,
            record=record,
            bar_ms=bar_ms,
            analysis_digest=analysis_digest,
            active_execution_count=active_execution_count,
            sizing=sizing,
        )

        if active_execution_count > 0:
            result = SupervisorAgent._record(
                snapshot,
                SupervisorDecision(
                    action="block_entry",
                    reason="当前存在活动 execution，禁止新增风险。",
                ),
                profile_id="",
                model_id="",
                fallback_level="deterministic",
            )
        else:
            result = self.agent.decide(snapshot)
        if existing is None:
            self.writer.save_durable(result)
        return result


def resolve_verified_profile(
    settings: Settings,
    profile_id: str,
    role_name: str,
) -> tuple[str, Any]:
    selected_id = str(profile_id or settings.active_ai_profile_id).strip()
    profile = settings.ai_profiles.get(selected_id)
    if profile is None:
        raise SupervisorConfigurationError(
            f"{role_name} AI 档案不存在: {selected_id}"
        )
    if not profile.verification.is_current_for(profile.provider):
        raise SupervisorConfigurationError(
            f"{role_name} AI 档案尚未通过当前配置验证: {selected_id}"
        )
    return selected_id, profile


def build_supervisor_gate(
    settings: Settings,
    *,
    prompt_path: Path,
    writer: SupervisorWriter,
    logger_: logging.Logger | None = None,
) -> SupervisorGate:
    """从已验证的角色档案构造生产监督门。"""

    log = logger_ or logger
    roles = settings.ai_roles
    primary_id, primary_profile = resolve_verified_profile(
        settings,
        roles.supervisor_primary_profile_id,
        "监督主模型",
    )
    backup_id = str(roles.supervisor_backup_profile_id or "").strip()
    backup_profile = None
    if backup_id:
        if backup_id == primary_id:
            raise SupervisorConfigurationError("监督主模型和备用模型不能绑定同一档案")
        backup_id, backup_profile = resolve_verified_profile(
            settings,
            backup_id,
            "监督备用模型",
        )

    prompt_text = prompt_path.read_text(encoding="utf-8")
    primary_client = create_ai_client(primary_profile.provider, logger_=log)
    backup_client = (
        create_ai_client(backup_profile.provider, logger_=log)
        if backup_profile is not None
        else None
    )
    return SupervisorGate(
        SupervisorAgent(
            primary_client=primary_client,
            primary_profile_id=primary_id,
            primary_model_id=primary_profile.provider.model,
            backup_client=backup_client,
            backup_profile_id=backup_id,
            backup_model_id=(backup_profile.provider.model if backup_profile else ""),
            prompt_text=prompt_text,
        ),
        writer,
    )
