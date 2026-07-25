"""测试进程必须与用户桌面和真实券商凭据隔离。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

import pa_agent.execution.credentials as credentials_module
from pa_agent.config.paths import PROJECT_ROOT
from tests import conftest as suite_conftest

_REAL_SHARED_ENV = (PROJECT_ROOT.parent / "env").resolve()

# 这些断言在测试模块导入期执行，证明隔离早于测试用例夹具。
assert credentials_module.shared_env_path().resolve() != _REAL_SHARED_ENV
assert all(key not in os.environ for key in suite_conftest._BROKER_ENV_KEYS)


def test_qt_tests_render_offscreen_and_broker_env_is_isolated(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    assert app.platformName() == "offscreen"
    assert credentials_module.shared_env_path() == (
        tmp_path / "runtime" / "broker.env"
    )
    assert all(
        key not in os.environ for key in suite_conftest._BROKER_ENV_KEYS
    )


def test_child_python_process_inherits_broker_isolation() -> None:
    script = "\n".join(
        (
            "import os",
            "from pathlib import Path",
            "from pa_agent.config.paths import PROJECT_ROOT",
            "from pa_agent.execution.credentials import shared_env_path",
            "real = (PROJECT_ROOT.parent / 'env').resolve()",
            "assert shared_env_path().resolve() != real",
            "keys = " + repr(suite_conftest._BROKER_ENV_KEYS),
            "assert all(key not in os.environ for key in keys)",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(PROJECT_ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
