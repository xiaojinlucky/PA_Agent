"""官方 Codex CLI 订阅客户端的隔离与解析测试。"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pa_agent.ai.codex_subscription_client import (
    CodexLoginStatus,
    CodexSubscriptionClient,
    CodexTransientError,
    _codex_executable,
    _parse_codex_jsonl,
    _sanitized_codex_environment,
    codex_login_status,
    start_codex_login,
)
from pa_agent.ai.deepseek_client import CancelledError
from pa_agent.config.settings import AIProviderSettings
from pa_agent.util.threading import CancelToken

_THREAD_ID = "019f6506-f487-72a2-92d2-e7eca30a00f2"


def _jsonl_reply(text: str = "OK") -> str:
    events = [
        {"type": "thread.started", "thread_id": _THREAD_ID},
        {
            "type": "item.completed",
            "item": {"id": "item-1", "type": "agent_message", "text": text},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 5,
            },
        },
    ]
    return "\n".join(json.dumps(event) for event in events)


def test_codex_jsonl_parser_extracts_final_and_usage() -> None:
    content, thread_id, usage, item_types = _parse_codex_jsonl(_jsonl_reply())

    assert content == "OK"
    assert thread_id == _THREAD_ID
    assert usage.prompt_tokens == 100
    assert usage.cached_prompt_tokens == 80
    assert usage.completion_tokens == 5
    assert item_types == ["agent_message"]


def test_codex_environment_does_not_inherit_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OKX_API_KEY", "broker-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-secret")
    monkeypatch.setenv("PATH", "safe-path")

    env = _sanitized_codex_environment()

    assert env["PATH"] == "safe-path"
    assert "OKX_API_KEY" not in env
    assert "DEEPSEEK_API_KEY" not in env


def test_codex_executable_prefers_desktop_cli_exe(
    monkeypatch,
    tmp_path,
) -> None:
    candidate = tmp_path / "OpenAI" / "Codex" / "bin" / "codex.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"test")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    completed = subprocess.CompletedProcess(
        args=[str(candidate), "--version"],
        returncode=0,
        stdout="codex-cli 1.0",
        stderr="",
    )
    with (
        patch(
            "pa_agent.ai.codex_subscription_client.shutil.which",
            return_value=r"C:\Program Files\WindowsApps\resources\codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.run",
            return_value=completed,
        ) as run,
    ):
        executable = _codex_executable()

    assert executable == str(candidate)
    assert run.call_args.args[0] == [str(candidate), "--version"]


def test_codex_executable_skips_unrunnable_candidate(
    monkeypatch,
    tmp_path,
) -> None:
    first = tmp_path / "OpenAI" / "Codex" / "bin" / "codex.exe"
    second = (
        tmp_path / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    )
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"not-runnable")
    second.write_bytes(b"runnable")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fake_run(command: list[str], **_kwargs):
        if command[0] == str(first):
            raise PermissionError("not executable")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="codex-cli 1.0",
            stderr="",
        )

    with (
        patch(
            "pa_agent.ai.codex_subscription_client.shutil.which",
            return_value=None,
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.run",
            side_effect=fake_run,
        ),
    ):
        executable = _codex_executable()

    assert executable == str(second)


@pytest.mark.parametrize(
    "failure",
    [
        OSError("cannot start"),
        subprocess.TimeoutExpired(cmd="codex --version", timeout=3.0),
        subprocess.CompletedProcess(
            args=["codex", "--version"],
            returncode=1,
            stdout="",
            stderr="failed",
        ),
    ],
)
def test_codex_executable_rejects_failed_version_probe(
    monkeypatch,
    tmp_path,
    failure,
) -> None:
    candidate = tmp_path / "OpenAI" / "Codex" / "bin" / "codex.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"test")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    with (
        patch(
            "pa_agent.ai.codex_subscription_client.shutil.which",
            return_value=None,
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.run",
            side_effect=failure
            if isinstance(failure, BaseException)
            else None,
            return_value=None
            if isinstance(failure, BaseException)
            else failure,
        ),
    ):
        executable = _codex_executable()

    assert executable is None


def test_codex_executable_rejects_extensionless_app_resource(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    extensionless = tmp_path / "resources" / "codex"
    extensionless.parent.mkdir(parents=True)
    extensionless.write_bytes(b"test")

    def fake_which(command: str) -> str | None:
        return str(extensionless) if command == "codex" else None

    with (
        patch(
            "pa_agent.ai.codex_subscription_client.shutil.which",
            side_effect=fake_which,
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.run",
        ) as run,
    ):
        executable = _codex_executable()

    assert executable is None
    run.assert_not_called()


def test_codex_login_status_accepts_chatgpt_subscription() -> None:
    completed = subprocess.CompletedProcess(
        args=["codex", "login", "status"],
        returncode=0,
        stdout="",
        stderr="Logged in using ChatGPT\n",
    )
    with (
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.run",
            return_value=completed,
        ),
    ):
        status = codex_login_status()

    assert status.logged_in is True
    assert "ChatGPT" in status.message


def test_codex_login_status_rejects_api_key_login() -> None:
    completed = subprocess.CompletedProcess(
        args=["codex", "login", "status"],
        returncode=0,
        stdout="Logged in using API key\n",
        stderr="",
    )
    with (
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.run",
            return_value=completed,
        ),
    ):
        status = codex_login_status()

    assert status.logged_in is False
    assert "不是 ChatGPT 订阅登录" in status.message


@pytest.mark.parametrize(
    ("device_auth", "expected_command"),
    [
        (False, ["codex", "login"]),
        (True, ["codex", "login", "--device-auth"]),
    ],
)
def test_start_codex_login_uses_official_cli(
    device_auth: bool,
    expected_command: list[str],
) -> None:
    process = MagicMock()
    with (
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.Popen",
            return_value=process,
        ) as popen,
    ):
        assert start_codex_login(device_auth=device_auth) is process

    assert popen.call_args.args[0] == expected_command
    assert "OKX_API_KEY" not in popen.call_args.kwargs["env"]


def test_codex_client_disables_tools_and_returns_reply() -> None:
    process = MagicMock()
    process.returncode = 0
    process.communicate.return_value = (_jsonl_reply("PA_CODEX_OK"), "")
    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return process

    settings = AIProviderSettings(
        adapter_id="codex_subscription",
        model="auto",
        reasoning_effort="high",
    )
    client = CodexSubscriptionClient(settings)
    with (
        patch(
            "pa_agent.ai.codex_subscription_client.codex_login_status",
            return_value=CodexLoginStatus(True, True, "ok"),
        ),
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.Popen",
            side_effect=fake_popen,
        ),
    ):
        reply = client.chat([{"role": "user", "content": "只回复 OK"}])

    assert reply.content == "PA_CODEX_OK"
    command = captured["command"]
    assert command.count("--disable") >= 4
    assert "shell_tool" in command
    assert "web_search" not in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--strict-config" in command
    assert "--ephemeral" in command
    assert 'web_search="disabled"' in command
    assert "skills.include_instructions=false" in command
    assert "browser_use" in command
    assert "computer_use" in command
    assert "image_generation" in command
    assert "plugins" in command
    assert captured["env"].get("OKX_API_KEY") is None


def test_codex_thread_call_persists_then_resumes_same_session() -> None:
    first_process = MagicMock()
    first_process.returncode = 0
    first_process.communicate.return_value = (_jsonl_reply("FIRST"), "")
    second_process = MagicMock()
    second_process.returncode = 0
    second_process.communicate.return_value = (_jsonl_reply("SECOND"), "")
    commands: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        commands.append(command)
        return first_process if len(commands) == 1 else second_process

    client = CodexSubscriptionClient(
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
    )
    with (
        patch(
            "pa_agent.ai.codex_subscription_client.codex_login_status",
            return_value=CodexLoginStatus(True, True, "ok"),
        ),
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.Popen",
            side_effect=fake_popen,
        ),
    ):
        first = client.chat_in_thread([{"role": "user", "content": "第一轮"}])
        second = client.chat_in_thread(
            [{"role": "user", "content": "第二轮"}],
            thread_id=first.request_id or "",
        )

    assert first.request_id == _THREAD_ID
    assert second.content == "SECOND"
    assert "--ephemeral" not in commands[0]
    assert commands[0][1:3] == ["exec", "--json"]
    assert commands[1][1:4] == ["exec", "resume", "--json"]
    assert _THREAD_ID in commands[1]
    assert 'sandbox_mode="read-only"' in commands[1]


def test_codex_thread_resume_rejects_cli_option_injection() -> None:
    client = CodexSubscriptionClient(
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="gpt-5.6-sol",
        )
    )

    with (
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable"
        ) as executable,
        pytest.raises(ValueError, match="线程 ID"),
    ):
        client.chat_in_thread(
            [{"role": "user", "content": "第二轮"}],
            thread_id="--last",
        )

    executable.assert_not_called()


def test_codex_client_rejects_forbidden_tool_event() -> None:
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "command": "whoami"},
                }
            ),
            _jsonl_reply("SHOULD_NOT_RETURN"),
        )
    )
    process = MagicMock()
    process.returncode = 0
    process.communicate.return_value = (stdout, "")
    settings = AIProviderSettings(
        adapter_id="codex_subscription",
        model="auto",
        reasoning_effort="high",
    )

    with (
        patch(
            "pa_agent.ai.codex_subscription_client.codex_login_status",
            return_value=CodexLoginStatus(True, True, "ok"),
        ),
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.Popen",
            return_value=process,
        ),
    ):
        with pytest.raises(RuntimeError, match="被禁止的工具调用"):
            CodexSubscriptionClient(settings).chat(
                [{"role": "user", "content": "只回复 OK"}]
            )


def test_codex_client_treats_unknown_cli_failure_as_transient() -> None:
    process = MagicMock()
    process.returncode = 1
    process.communicate.return_value = (
        json.dumps(
            {
                "type": "turn.failed",
                "error": {"message": "temporary upstream failure"},
            }
        ),
        "raw-secret-that-must-not-be-shown",
    )
    settings = AIProviderSettings(
        adapter_id="codex_subscription",
        model="auto",
        reasoning_effort="high",
    )

    with (
        patch(
            "pa_agent.ai.codex_subscription_client.codex_login_status",
            return_value=CodexLoginStatus(True, True, "ok"),
        ),
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.Popen",
            return_value=process,
        ),
    ):
        with pytest.raises(CodexTransientError) as exc_info:
            CodexSubscriptionClient(settings).chat(
                [{"role": "user", "content": "只回复 OK"}]
            )

    assert "raw-secret" not in str(exc_info.value)


def test_codex_client_treats_zero_exit_failure_event_as_transient() -> None:
    process = MagicMock()
    process.returncode = 0
    process.communicate.return_value = (
        json.dumps(
            {
                "type": "turn.failed",
                "error": {"message": "temporary upstream failure"},
            }
        ),
        "",
    )
    settings = AIProviderSettings(
        adapter_id="codex_subscription",
        model="auto",
        reasoning_effort="high",
    )

    with (
        patch(
            "pa_agent.ai.codex_subscription_client.codex_login_status",
            return_value=CodexLoginStatus(True, True, "ok"),
        ),
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.Popen",
            return_value=process,
        ),
    ):
        with pytest.raises(CodexTransientError):
            CodexSubscriptionClient(settings).chat(
                [{"role": "user", "content": "只回复 OK"}]
            )


def test_codex_client_reports_unsupported_model_as_permanent() -> None:
    process = MagicMock()
    process.returncode = 1
    process.communicate.return_value = (
        json.dumps(
            {
                "type": "error",
                "message": json.dumps(
                    {
                        "status": 400,
                        "error": {
                            "type": "invalid_request_error",
                            "message": "model is not supported",
                        },
                    }
                ),
            }
        ),
        "",
    )
    settings = AIProviderSettings(
        adapter_id="codex_subscription",
        model="removed-model",
        reasoning_effort="high",
    )

    with (
        patch(
            "pa_agent.ai.codex_subscription_client.codex_login_status",
            return_value=CodexLoginStatus(True, True, "ok"),
        ),
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.Popen",
            return_value=process,
        ),
    ):
        with pytest.raises(RuntimeError, match="模型不受支持") as exc_info:
            CodexSubscriptionClient(settings).chat(
                [{"role": "user", "content": "只回复 OK"}]
            )

    assert not isinstance(exc_info.value, CodexTransientError)


def test_codex_client_timeout_terminates_process() -> None:
    process = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = 0
    settings = AIProviderSettings(
        adapter_id="codex_subscription",
        model="auto",
        reasoning_effort="high",
    )

    with (
        patch(
            "pa_agent.ai.codex_subscription_client.codex_login_status",
            return_value=CodexLoginStatus(True, True, "ok"),
        ),
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.Popen",
            return_value=process,
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.time.monotonic",
            side_effect=(0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        ),
    ):
        with pytest.raises(TimeoutError, match="timed out"):
            CodexSubscriptionClient(settings).chat(
                [{"role": "user", "content": "只回复 OK"}],
                timeout_s=0.01,
            )

    process.terminate.assert_called_once()
    process.wait.assert_called_once()


def test_codex_client_timeout_covers_login_preflight() -> None:
    process = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = 0
    process.communicate.side_effect = subprocess.TimeoutExpired(
        cmd="codex login status",
        timeout=0.01,
    )
    client = CodexSubscriptionClient(
        AIProviderSettings(
            adapter_id="codex_subscription",
            model="auto",
        )
    )

    with (
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.Popen",
            return_value=process,
        ),
        pytest.raises(TimeoutError, match="timed out"),
    ):
        client.chat(
            [{"role": "user", "content": "只回复 OK"}],
            timeout_s=0.02,
        )

    process.terminate.assert_called_once()


def test_codex_client_running_cancel_terminates_process() -> None:
    process = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = 0
    process.communicate.side_effect = subprocess.TimeoutExpired(
        cmd="codex",
        timeout=0.1,
    )
    cancel_token = MagicMock(spec=CancelToken)
    cancel_token.is_set.side_effect = (
        False,
        False,
        False,
        False,
        False,
        True,
    )
    settings = AIProviderSettings(
        adapter_id="codex_subscription",
        model="auto",
        reasoning_effort="high",
    )

    with (
        patch(
            "pa_agent.ai.codex_subscription_client.codex_login_status",
            return_value=CodexLoginStatus(True, True, "ok"),
        ),
        patch(
            "pa_agent.ai.codex_subscription_client._codex_executable",
            return_value="codex",
        ),
        patch(
            "pa_agent.ai.codex_subscription_client.subprocess.Popen",
            return_value=process,
        ),
    ):
        with pytest.raises(CancelledError, match="cancelled"):
            CodexSubscriptionClient(settings).chat(
                [{"role": "user", "content": "只回复 OK"}],
                cancel_token=cancel_token,
            )

    process.terminate.assert_called_once()
    process.wait.assert_called_once()
