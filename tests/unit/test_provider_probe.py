"""Unit tests for the real-provider probe service boundary."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pa_agent.ai.deepseek_client import CancelledError
from pa_agent.ai import provider_probe
from pa_agent.ai.provider_probe import ProbeStatus, probe_ai_provider
from pa_agent.config.settings import AIProviderSettings
from pa_agent.util.threading import CancelToken

_CHALLENGE = "PA_AGENT_PROBE_TEST1234"


def _settings(**updates: object) -> AIProviderSettings:
    values: dict[str, object] = {
        "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com",
        "api_key": "dummy",
        "adapter_id": "deepseek",
        "thinking": True,
        "reasoning_effort": "max",
    }
    values.update(updates)
    return AIProviderSettings.model_validate(values)


def test_success_returns_three_structured_observations_and_forwards_controls() -> None:
    token = CancelToken()
    client = MagicMock()
    client.stream_chat.return_value = SimpleNamespace(
        reasoning_content="brief thought", content=_CHALLENGE
    )

    with (
        patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client) as create,
        patch(
            "pa_agent.ai.provider_probe._new_probe_challenge",
            return_value=_CHALLENGE,
        ),
    ):
        result = probe_ai_provider(_settings(), cancel_token=token, timeout_s=7.5)

    assert result.connection_auth is ProbeStatus.PASSED
    assert result.parameter_acceptance is ProbeStatus.PASSED
    assert result.reasoning_observed is True
    assert result.response_observed is True
    assert result.challenge_matched is True
    assert result.verification_passed is True
    assert result.verification_checks() == {
        "connection_auth": True,
        "parameter_acceptance": True,
        "response_observed": True,
        "challenge_matched": True,
    }
    assert result.error_code == ""
    assert result.tested_at
    create.assert_called_once()
    kwargs = client.stream_chat.call_args.kwargs
    assert kwargs == {
        "thinking": True,
        "reasoning_effort": "max",
        "cancel_token": token,
        "timeout_s": 7.5,
        "max_output_tokens": 2_048,
    }
    assert client.stream_chat.call_args.args == (
        [
            {
                "role": "user",
                "content": (
                    "Reply with exactly this text and nothing else: "
                    f"{_CHALLENGE}"
                ),
            }
        ],
    )


def test_no_reasoning_is_an_observation_not_a_verification_failure() -> None:
    client = MagicMock()
    client.stream_chat.return_value = SimpleNamespace(
        reasoning_content="", content=_CHALLENGE
    )

    with (
        patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client),
        patch(
            "pa_agent.ai.provider_probe._new_probe_challenge",
            return_value=_CHALLENGE,
        ),
    ):
        result = probe_ai_provider(_settings(thinking=False))

    assert result.reasoning_observed is False
    assert result.response_observed is True
    assert result.challenge_matched is True
    assert result.verification_passed is True


def test_empty_response_does_not_create_a_passed_verification() -> None:
    client = MagicMock()
    client.stream_chat.return_value = SimpleNamespace(
        reasoning_content="brief thought", content=""
    )

    with patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client):
        result = probe_ai_provider(_settings())

    assert result.connection_auth is ProbeStatus.PASSED
    assert result.parameter_acceptance is ProbeStatus.PASSED
    assert result.response_observed is False
    assert result.challenge_matched is False
    assert result.verification_passed is False
    assert result.error_code == "empty_response"


def test_nonempty_but_unrelated_response_does_not_pass_challenge() -> None:
    client = MagicMock()
    client.stream_chat.return_value = SimpleNamespace(
        reasoning_content="", content="gateway welcome"
    )

    with (
        patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client),
        patch(
            "pa_agent.ai.provider_probe._new_probe_challenge",
            return_value=_CHALLENGE,
        ),
    ):
        result = probe_ai_provider(_settings())

    assert result.response_observed is True
    assert result.challenge_matched is False
    assert result.verification_passed is False
    assert result.error_code == "challenge_mismatch"


def test_pre_cancelled_probe_never_constructs_a_client() -> None:
    token = CancelToken()
    token.set()

    with patch("pa_agent.ai.provider_probe.create_ai_client") as create:
        result = probe_ai_provider(_settings(), cancel_token=token)

    create.assert_not_called()
    assert result.cancelled is True
    assert result.connection_auth is ProbeStatus.UNKNOWN
    assert result.parameter_acceptance is ProbeStatus.UNKNOWN
    assert result.reasoning_observed is None


def test_client_cancellation_returns_cancelled_result() -> None:
    client = MagicMock()
    client.stream_chat.side_effect = CancelledError("secret response")

    with patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client):
        result = probe_ai_provider(_settings())

    assert result.cancelled is True
    assert result.error_code == "cancelled"
    assert "secret" not in result.message


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_status_is_classified_without_raw_error(status: int) -> None:
    class AuthenticationFailure(Exception):
        status_code = status

    client = MagicMock()
    client.stream_chat.side_effect = AuthenticationFailure(
        "dummy Reply only OK. raw-provider-body"
    )

    with patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client):
        result = probe_ai_provider(_settings())

    assert result.connection_auth is ProbeStatus.FAILED
    assert result.parameter_acceptance is ProbeStatus.UNKNOWN
    assert result.reasoning_observed is None
    assert result.error_code == "authentication_failed"
    assert result.http_status == status
    assert "secret" not in result.message
    assert "raw-provider-body" not in result.message
    assert "Reply only OK" not in result.message


@pytest.mark.parametrize("status", [400, 404, 422])
def test_parameter_rejection_does_not_claim_authentication_success(status: int) -> None:
    class ParameterFailure(Exception):
        status_code = status

    client = MagicMock()
    client.stream_chat.side_effect = ParameterFailure("raw response")

    with patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client):
        result = probe_ai_provider(_settings())

    assert result.connection_auth is ProbeStatus.UNKNOWN
    assert result.parameter_acceptance is ProbeStatus.FAILED
    assert result.error_code == "parameters_rejected"
    assert result.http_status == status


def test_timeout_keeps_connection_status_unknown() -> None:
    client = MagicMock()
    client.stream_chat.side_effect = TimeoutError("secret raw response")

    with patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client):
        result = probe_ai_provider(_settings())

    assert result.connection_auth is ProbeStatus.UNKNOWN
    assert result.parameter_acceptance is ProbeStatus.UNKNOWN
    assert result.error_code == "timeout"
    assert "secret" not in result.message


def test_probe_enforces_end_to_end_deadline_when_client_never_returns() -> None:
    release = threading.Event()
    returned = threading.Event()
    token = CancelToken()
    client = MagicMock()

    def _block_forever(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        release.wait(timeout=2.0)
        returned.set()
        return SimpleNamespace(reasoning_content="", content=_CHALLENGE)

    client.stream_chat.side_effect = _block_forever

    try:
        started = time.monotonic()
        with patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client):
            result = probe_ai_provider(
                _settings(),
                cancel_token=token,
                timeout_s=0.05,
            )
        elapsed = time.monotonic() - started

        assert result.error_code == "timeout"
        assert result.connection_auth is ProbeStatus.UNKNOWN
        assert result.parameter_acceptance is ProbeStatus.UNKNOWN
        assert token.is_set()
        assert elapsed < 0.5
        with patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client) as create:
            busy = probe_ai_provider(_settings(), timeout_s=0.05)
        assert busy.error_code == "probe_busy"
        create.assert_not_called()
    finally:
        release.set()

    assert returned.wait(timeout=0.5)
    assert provider_probe._PROBE_SLOT.acquire(timeout=0.5)
    provider_probe._PROBE_SLOT.release()


def test_unknown_provider_error_keeps_all_checks_unknown_and_discards_body() -> None:
    client = MagicMock()
    client.stream_chat.side_effect = RuntimeError(
        "dummy Reply only OK. private response body"
    )

    with patch("pa_agent.ai.provider_probe.create_ai_client", return_value=client):
        result = probe_ai_provider(_settings())

    assert result.connection_auth is ProbeStatus.UNKNOWN
    assert result.parameter_acceptance is ProbeStatus.UNKNOWN
    assert result.error_code == "provider_error"
    assert "dummy" not in result.message
    assert "Reply only OK" not in result.message
    assert "private response body" not in result.message


def test_invalid_adapter_and_missing_fields_fail_before_network() -> None:
    cases = [
        (_settings(adapter_id="unknown"), "parameters_rejected"),
        (_settings(api_key=""), "credential_missing"),
        (_settings(base_url=""), "base_url_missing"),
        (_settings(model=""), "model_missing"),
    ]

    with patch("pa_agent.ai.provider_probe.create_ai_client") as create:
        results = [probe_ai_provider(settings) for settings, _ in cases]

    create.assert_not_called()
    assert [result.error_code for result in results] == [expected for _, expected in cases]


def test_invalid_timeout_fails_before_network() -> None:
    with patch("pa_agent.ai.provider_probe.create_ai_client") as create:
        result = probe_ai_provider(_settings(), timeout_s=0)

    create.assert_not_called()
    assert result.parameter_acceptance is ProbeStatus.FAILED
    assert result.error_code == "invalid_timeout"
