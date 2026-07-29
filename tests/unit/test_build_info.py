from __future__ import annotations

from pa_agent.build_info import runtime_sha


def test_runtime_sha_is_full_source_checkout_sha(monkeypatch) -> None:
    monkeypatch.delenv("PA_AGENT_BUILD_SHA", raising=False)

    value = runtime_sha()

    assert len(value) == 40
    assert all(character in "0123456789abcdef" for character in value)


def test_runtime_sha_accepts_only_full_environment_sha(monkeypatch) -> None:
    monkeypatch.setenv("PA_AGENT_BUILD_SHA", "A" * 40)
    assert runtime_sha() == "a" * 40

    monkeypatch.setenv("PA_AGENT_BUILD_SHA", "short")
    assert runtime_sha() != "short"
