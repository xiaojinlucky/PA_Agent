"""Kimi 原生保留式思考的多轮与校验重试测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pa_agent.ai.json_validator import Ok, ValidationError
from pa_agent.config.settings import AIProviderSettings, ValidationSettings
from pa_agent.orchestrator.validation_retry import (
    append_assistant_turn,
    validate_with_retry,
)


def _provider(model: str, *, thinking: bool) -> AIProviderSettings:
    return AIProviderSettings(
        adapter_id="kimi",
        base_url="https://api.moonshot.cn/v1",
        model=model,
        thinking=thinking,
    )


@pytest.mark.parametrize(
    ("model", "thinking", "expected"),
    [
        ("kimi-k2.5", True, False),
        ("kimi-k2.6", False, False),
        ("kimi-k2.6", True, True),
        ("kimi-k2.7-code", False, True),
        ("kimi-k3", False, True),
    ],
)
def test_append_assistant_turn_matches_kimi_preserved_thinking(
    model: str,
    thinking: bool,
    expected: bool,
) -> None:
    reply = SimpleNamespace(content="answer", reasoning_content="reasoning")

    messages = append_assistant_turn(
        [{"role": "user", "content": "question"}],
        reply,
        provider_settings=_provider(model, thinking=thinking),
    )

    assistant = messages[-1]
    assert ("reasoning_content" in assistant) is expected


def test_kimi_k26_validation_retry_resends_reasoning_history() -> None:
    first_reply = SimpleNamespace(
        content="not-json",
        reasoning_content="first reasoning",
    )
    second_reply = SimpleNamespace(
        content='{"fixed": true}',
        reasoning_content="second reasoning",
    )
    validator = MagicMock()
    validator.validate.side_effect = [
        ValidationError(
            category="d",
            stage="stage1",
            raw_text="not-json",
            message="需要 JSON",
        ),
        Ok({"fixed": True}),
    ]
    call_api = MagicMock(return_value=second_reply)

    result = validate_with_retry(
        stage="stage1",
        messages=[{"role": "user", "content": "return json"}],
        reply=first_reply,
        validator=validator,
        validation_settings=ValidationSettings(retry_max=1),
        validate_kwargs={},
        call_api=call_api,
        provider_settings=_provider("kimi-k2.6", thinking=True),
    )

    retry_messages = call_api.call_args.args[0]
    failed_assistant = next(
        message
        for message in retry_messages
        if message.get("role") == "assistant"
    )
    assert failed_assistant["reasoning_content"] == "first reasoning"
    assert isinstance(result.result, Ok)
