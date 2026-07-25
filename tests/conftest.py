"""全测试套件的运行态隔离。

任何测试若调用默认 ``save_settings``, 也只能写入本用例临时目录, 绝不能
覆盖用户正在使用的 ``config/settings.json``。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

_BROKER_ENV_KEYS = (
    "PA_AGENT_LIVE_TRADING_ENABLED",
    "PA_AGENT_PAPER_TRADING_ENABLED",
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_API_SECRET",
    "OKX_PASSPHRASE",
    "OKX_DEMO_API_KEY",
    "OKX_DEMO_SECRET_KEY",
    "OKX_DEMO_API_SECRET",
    "OKX_DEMO_PASSPHRASE",
    "OKX_LIVE_API_KEY",
    "OKX_LIVE_SECRET_KEY",
    "OKX_LIVE_API_SECRET",
    "OKX_LIVE_PASSPHRASE",
    "OKX_LIVE_ENABLED",
    "LONGBRIDGE_APP_KEY",
    "LONGBRIDGE_APP_SECRET",
    "LONGBRIDGE_ACCESS_TOKEN",
    "LONGBRIDGE_PAPER_APP_KEY",
    "LONGBRIDGE_PAPER_APP_SECRET",
    "LONGBRIDGE_PAPER_ACCESS_TOKEN",
    "LONGBRIDGE_PAPER_ACCOUNT_ID",
    "LONGBRIDGE_COMPREHENSIVE_APP_KEY",
    "LONGBRIDGE_COMPREHENSIVE_APP_SECRET",
    "LONGBRIDGE_COMPREHENSIVE_ACCESS_TOKEN",
    "LONGBRIDGE_COMPREHENSIVE_ACCOUNT_ID",
    "LONGBRIDGE_INTRADAY_APP_KEY",
    "LONGBRIDGE_INTRADAY_APP_SECRET",
    "LONGBRIDGE_INTRADAY_ACCESS_TOKEN",
    "LONGBRIDGE_INTRADAY_ACCOUNT_ID",
)

# 这些隔离必须在 pytest 收集测试模块之前完成。子进程继承同一环境，
# 因而也无法回退读取正式 Quant\env 或进程中的真实券商凭据。
_COLLECTION_BROKER_ENV = (
    Path(tempfile.gettempdir())
    / f"pa-agent-pytest-{os.getpid()}"
    / "broker.env"
)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["_PA_AGENT_TEST_SHARED_ENV_PATH"] = str(_COLLECTION_BROKER_ENV)
for _broker_env_key in _BROKER_ENV_KEYS:
    os.environ.pop(_broker_env_key, None)

import pytest  # noqa: E402


def _digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _isolate_runtime_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import pa_agent.config.paths as config_paths
    import pa_agent.config.settings as settings_module
    real_path = Path(config_paths.PROJECT_ROOT) / "config" / "settings.json"
    before = _digest(real_path)
    isolated_path = tmp_path / "runtime" / "settings.json"
    isolated_broker_env = tmp_path / "runtime" / "broker.env"
    monkeypatch.setattr(config_paths, "SETTINGS_JSON_PATH", isolated_path)
    monkeypatch.setenv(
        "_PA_AGENT_TEST_SHARED_ENV_PATH",
        str(isolated_broker_env),
    )
    for key in _BROKER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    original_save_settings = settings_module.save_settings

    def isolated_save_settings(settings, path: Path | None = None) -> None:
        target = Path(path) if path is not None else isolated_path
        if target.resolve() == real_path.resolve():
            target = isolated_path
        original_save_settings(settings, target)

    monkeypatch.setattr(settings_module, "save_settings", isolated_save_settings)

    # 部分模块在 fixture 执行前已缓存常量或函数; 同步替换这些旧引用。
    # fixture 执行后才导入的模块会直接拿到上面已经替换的新引用。
    for module_name, module in tuple(sys.modules.items()):
        if not module_name.startswith("pa_agent.") or module is None:
            continue
        if hasattr(module, "SETTINGS_JSON_PATH"):
            monkeypatch.setattr(module, "SETTINGS_JSON_PATH", isolated_path)
        for attr_name, value in tuple(vars(module).items()):
            if value is original_save_settings:
                monkeypatch.setattr(module, attr_name, isolated_save_settings)
    yield
    assert _digest(real_path) == before, "测试改写了真实 config/settings.json"
