"""Tests for AI client factory routing."""
from __future__ import annotations

import logging
from io import StringIO

from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.cursor_sdk_client import CursorSdkClient
from pa_agent.ai.deepseek_client import DeepSeekClient
from pa_agent.config.settings import AIProviderSettings
from pa_agent.util.logging import MaskingFormatter


def test_create_ai_client_openclaw_cs_uses_cursor_sdk() -> None:
    settings = AIProviderSettings(
        model="openclaw_cs",
        base_url="",
        api_key="crsr_test",
    )
    client = create_ai_client(settings)
    assert isinstance(client, CursorSdkClient)


def test_create_ai_client_openclaw_uses_deepseek_client() -> None:
    settings = AIProviderSettings(
        model="openclaw",
        base_url="http://127.0.0.1:19000/v1",
        api_key="test",
    )
    client = create_ai_client(settings)
    assert isinstance(client, DeepSeekClient)


def test_create_ai_client_honours_explicit_cursor_adapter() -> None:
    settings = AIProviderSettings(
        model="cursor-model-id",
        base_url="",
        api_key="crsr_test",
        adapter_id="cursor_agent",
    )
    client = create_ai_client(settings)
    assert isinstance(client, CursorSdkClient)


def test_client_factory_log_removes_url_credentials_and_query_tokens() -> None:
    settings = AIProviderSettings(
        model="custom-chat",
        base_url="https://user:secret@example.com/v1?token=query-secret#fragment",
        api_key="api-secret",
        adapter_id="generic_openai_compatible",
    )
    stream = StringIO()
    log = logging.getLogger("test.client_factory.safe_url")
    log.handlers = [logging.StreamHandler(stream)]
    log.propagate = False
    log.setLevel(logging.INFO)

    create_ai_client(settings, logger_=log)

    output = stream.getvalue()
    assert "https://example.com/v1" in output
    assert "user" not in output
    assert "secret" not in output
    assert "token=" not in output


def test_masking_formatter_keeps_previous_profile_keys_masked() -> None:
    formatter = MaskingFormatter("%(message)s", api_key="sk-first-secret-key")
    formatter.set_api_key("sk-second-secret-key")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="sk-first-secret-key sk-second-secret-key",
        args=(),
        exc_info=None,
    )

    output = formatter.format(record)

    assert "sk-first-secret-key" not in output
    assert "sk-second-secret-key" not in output
