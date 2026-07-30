from __future__ import annotations

import json
from pathlib import Path

from pa_agent.config.settings import Settings
from pa_agent.release_contract import (
    EXPECTED_PROMPT_RESOURCE_COUNT,
    EXPECTED_PROMPT_RESOURCE_PATHS,
    EXPECTED_REQUIRED_SOURCE_FILES,
    offline_self_check,
    version_payload,
)
from pa_agent.safety_defaults import new_risk_route_supported


def _write_source_contract(root: Path, *, prompt_count: int = 37) -> None:
    (root / "pa_agent").mkdir(parents=True)
    (root / "prompt_engineering").mkdir()
    (root / "pa_agent" / "__init__.py").write_text(
        '__version__ = "0.1.0"\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        """
[project]
name = "pa-agent"
dynamic = ["version"]
requires-python = ">=3.12,<3.13"
dependencies = [
  "tvdatafeed @ git+https://github.com/rongardF/tvdatafeed.git@e6f6aaa7de439ac6e454d9b26d2760ded8dc4923",
]

[project.scripts]
pa-agent = "pa_agent.main:main"
pa-execution-worker = "pa_agent.execution.worker_cli:main"

[tool.setuptools.dynamic]
version = {attr = "pa_agent.__version__"}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    for relative in EXPECTED_REQUIRED_SOURCE_FILES:
        path = root / relative
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    for relative in sorted(EXPECTED_PROMPT_RESOURCE_PATHS)[:prompt_count]:
        path = root / "prompt_engineering" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "fixture\n",
            encoding="utf-8",
        )


def test_offline_self_check_proves_source_contract_without_runtime_access(
    tmp_path: Path,
) -> None:
    _write_source_contract(tmp_path)

    report = offline_self_check(tmp_path)

    assert report["status"] == "pass"
    assert report["delivery"] == "windows-python-3.12-source"
    assert report["prompt_resources"] == EXPECTED_PROMPT_RESOURCE_COUNT == 37
    assert report["entrypoints"] == ["pa-agent", "pa-execution-worker"]
    assert report["default_trading"] == {
        "execution_enabled": False,
        "auto_execute": False,
        "okx_live": False,
        "longbridge_trading": False,
        "new_risk_routes": ["okx:demo"],
    }
    json.dumps(report, ensure_ascii=False)


def test_offline_self_check_fails_closed_when_prompt_resource_is_missing(
    tmp_path: Path,
) -> None:
    _write_source_contract(tmp_path, prompt_count=36)

    report = offline_self_check(tmp_path)

    assert report["status"] == "fail"
    assert "prompt_resources" in report["failed_checks"]


def test_offline_self_check_fails_when_entrypoint_module_is_missing(
    tmp_path: Path,
) -> None:
    _write_source_contract(tmp_path)
    (tmp_path / "pa_agent" / "main.py").unlink()

    report = offline_self_check(tmp_path)

    assert report["status"] == "fail"
    assert "required_source_files" in report["failed_checks"]


def test_version_payload_has_one_version_truth_and_full_build_identity() -> None:
    payload = version_payload("pa-agent")

    assert payload["program"] == "pa-agent"
    assert payload["version"] == "0.1.0"
    assert payload["delivery"] == "windows-python-3.12-source"
    assert (
        payload["git_sha"] == "unavailable"
        or len(payload["git_sha"]) == 40
    )


def test_v010_only_supports_okx_demo_new_risk() -> None:
    assert new_risk_route_supported("okx", "demo") is True
    assert new_risk_route_supported("okx", "live") is False
    assert new_risk_route_supported("longbridge", "demo") is False
    assert new_risk_route_supported("longbridge", "live") is False


def test_fresh_settings_select_only_supported_new_risk_route() -> None:
    settings = Settings()

    assert settings.execution.selected_broker == "okx"
    assert settings.execution.okx.simulated is True
    assert settings.execution.enabled is False
    assert settings.execution.auto_execute is False
