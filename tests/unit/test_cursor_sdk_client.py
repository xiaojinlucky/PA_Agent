"""Unit tests for Cursor SDK stream event mapping."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pa_agent.ai.cursor_sdk_client import (
    CursorSdkClient,
    _consume_cursor_stream_event,
    _ensure_cursor_sdk_patches,
    _patch_cursor_sdk_bridge_auth_tokens,
    _safe_bridge_auth_token,
    _sanitize_cursor_bridge_argv,
)
from pa_agent.config.settings import AIProviderSettings


def _cursor_settings() -> AIProviderSettings:
    return AIProviderSettings(
        adapter_id="cursor_agent",
        model="composer-2.5",
        api_key="cursor-test-key",
    )


def _agent_context(agent: object) -> MagicMock:
    context = MagicMock()
    context.__enter__.return_value = agent
    context.__exit__.return_value = False
    return context


def test_safe_bridge_auth_token_never_starts_with_dash() -> None:
    for _ in range(100):
        assert not _safe_bridge_auth_token().startswith("-")


def test_patch_cursor_sdk_bridge_auth_tokens() -> None:
    _patch_cursor_sdk_bridge_auth_tokens()
    import cursor_sdk._tool_callback as tool_cb  # type: ignore

    for _ in range(20):
        assert not tool_cb._new_auth_token().startswith("-")


def test_sanitize_cursor_bridge_argv_fixes_dash_prefixed_token() -> None:
    argv = [
        "cursor-sdk-bridge.js",
        "--tool-callback-url",
        "http://127.0.0.1:1",
        "--tool-callback-auth-token",
        "-startsWithDash",
    ]
    fixed = _sanitize_cursor_bridge_argv(argv)
    assert fixed[4] != "-startsWithDash"
    assert not fixed[4].startswith("-")


def test_bridge_launches_after_cursor_sdk_patches() -> None:
    _ensure_cursor_sdk_patches()
    from cursor_sdk import CursorClient  # type: ignore

    client = CursorClient.launch_bridge(workspace=".")
    try:
        assert client is not None
    finally:
        client.close()


def test_consume_thinking_delta_emits_reasoning_callback() -> None:
    reasoning: list[str] = []
    content: list[str] = []
    emitted: list[str] = []

    event = SimpleNamespace(
        interaction_update=SimpleNamespace(type="thinking-delta", text="alpha "),
        sdk_message=None,
        step=None,
    )
    _consume_cursor_stream_event(
        event,
        reasoning_parts=reasoning,
        content_parts=content,
        on_reasoning_token=emitted.append,
        on_content_token=None,
    )

    assert reasoning == ["alpha "]
    assert emitted == ["alpha "]
    assert content == []


def test_consume_text_delta_emits_content_callback() -> None:
    reasoning: list[str] = []
    content: list[str] = []
    emitted: list[str] = []

    event = SimpleNamespace(
        interaction_update=SimpleNamespace(type="text-delta", text='{"ok":'),
        sdk_message=None,
        step=None,
    )
    _consume_cursor_stream_event(
        event,
        reasoning_parts=reasoning,
        content_parts=content,
        on_reasoning_token=None,
        on_content_token=emitted.append,
    )

    assert content == ['{"ok":']
    assert emitted == ['{"ok":']
    assert reasoning == []


def test_stream_chat_accepts_probe_output_limit() -> None:
    result = SimpleNamespace(result="probe-ok", id="run-1", status="completed")
    run = MagicMock()
    run.events.return_value = iter(())
    run.wait.return_value = result
    agent = MagicMock()
    agent.send.return_value = run
    bridge = MagicMock()

    with (
        patch("cursor_sdk.CursorClient.launch_bridge", return_value=bridge) as launch,
        patch("cursor_sdk.Agent.create", return_value=_agent_context(agent)),
    ):
        reply = CursorSdkClient(_cursor_settings()).stream_chat(
            [{"role": "user", "content": "probe"}],
            max_output_tokens=2_048,
            timeout_s=1.0,
        )

    assert reply.content == "probe-ok"
    launch.assert_called_once()
    assert launch.call_args.kwargs["timeout"] == 1.0
    assert launch.call_args.kwargs["client_timeout"] == 1.0
    assert launch.call_args.kwargs["max_retries"] == 0
    bridge.close.assert_called_once()


def test_timeout_cancels_cursor_run_and_closes_bridge() -> None:
    timer_holder: dict[str, object] = {}

    class _ControlledTimer:
        daemon = False

        def __init__(self, _seconds: float, callback: object) -> None:
            self.callback = callback
            timer_holder["timer"] = self

        def start(self) -> None:
            return None

        def cancel(self) -> None:
            return None

    run = MagicMock()
    run.supports.return_value = True

    def _events() -> object:
        timer = timer_holder["timer"]
        timer.callback()  # type: ignore[attr-defined]
        return iter(())

    run.events.side_effect = _events
    agent = MagicMock()
    agent.send.return_value = run
    bridge = MagicMock()

    with (
        patch("pa_agent.ai.cursor_sdk_client.threading.Timer", _ControlledTimer),
        patch("cursor_sdk.CursorClient.launch_bridge", return_value=bridge),
        patch("cursor_sdk.Agent.create", return_value=_agent_context(agent)),
    ):
        with pytest.raises(TimeoutError, match="timed out"):
            CursorSdkClient(_cursor_settings()).stream_chat(
                [{"role": "user", "content": "probe"}],
                max_output_tokens=2_048,
                timeout_s=1.0,
            )

    run.cancel.assert_called_once()
    assert bridge.close.call_count >= 1
