"""全测试套件的运行态隔离。

任何测试若调用默认 ``save_settings``, 也只能写入本用例临时目录, 绝不能
覆盖用户正在使用的 ``config/settings.json``。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest


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
    monkeypatch.setattr(config_paths, "SETTINGS_JSON_PATH", isolated_path)

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
