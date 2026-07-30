from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def test_gui_version_and_self_check_do_not_import_qt_network_or_database() -> None:
    result = _run_isolated(
        """
import json
import sys
from pa_agent.main import main
assert main(["pa-agent", "--version"]) == 0
assert main(["pa-agent", "--self-check"]) == 0
forbidden = ("PyQt6", "requests", "longbridge", "sqlite3")
loaded = sorted(name for name in sys.modules if name.startswith(forbidden))
assert loaded == [], loaded
"""
    )

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines[0].startswith("pa-agent 0.1.0 ")
    assert json.loads(lines[1])["status"] == "pass"


def test_worker_version_and_self_check_do_not_import_worker_runtime() -> None:
    result = _run_isolated(
        """
import json
import sys
from pa_agent.execution.worker_cli import main
assert main(["pa-execution-worker", "--version"]) == 0
assert main(["pa-execution-worker", "--self-check"]) == 0
assert "pa_agent.execution.worker" not in sys.modules
forbidden = ("PyQt6", "requests", "longbridge", "sqlite3")
loaded = sorted(name for name in sys.modules if name.startswith(forbidden))
assert loaded == [], loaded
"""
    )

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines[0].startswith("pa-execution-worker 0.1.0 ")
    assert json.loads(lines[1])["status"] == "pass"
