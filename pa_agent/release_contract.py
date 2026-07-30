"""Windows + Python 3.12 源码部署版的离线发布合同。"""

from __future__ import annotations

import json
import platform
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from pa_agent import __version__
from pa_agent.build_info import runtime_sha
from pa_agent.safety_defaults import (
    DEFAULT_AUTO_EXECUTE,
    DEFAULT_EXECUTION_ENABLED,
    DEFAULT_LONGBRIDGE_TRADING,
    DEFAULT_OKX_LIVE,
    SUPPORTED_NEW_RISK_ROUTES,
)

DELIVERY = "windows-python-3.12-source"
EXPECTED_PROMPT_RESOURCE_COUNT = 37
EXPECTED_PROMPT_RESOURCE_PATHS = frozenset(
    {
        "_reference/abbrev_glossary.md",
        "_reference/kb_concept_map.md",
        "_reference/pattern_enum.md",
        "二元决策.txt",
        "极速上涨分析识别.txt",
        "极速上涨交易策略.txt",
        "极速下跌分析识别.txt",
        "极速下跌交易策略.txt",
        "交易监督智能体.txt",
        "上涨通道分析识别.txt",
        "上涨通道交易策略.txt",
        "市场规则_港股.txt",
        "市场规则_加密.txt",
        "市场规则_美股.txt",
        "市场规则_A股.txt",
        "市场诊断框架.txt",
        "提示词大纲_人设与思维方式.txt",
        "文件13-窄通道与宽通道策略.txt",
        "文件14-楔形形态分析交易.txt",
        "文件15-二次入场机会.txt",
        "文件16-K线信号识别.txt",
        "文件17-止损和止盈与仓位管理.txt",
        "文件18-突破失败与突破测试.txt",
        "文件19-H1H2-L1L2计数.txt",
        "文件20-AlwaysIn与20GB.txt",
        "文件21-铁丝网与无交易环境.txt",
        "文件22-信号失败后的磁力位.txt",
        "文件23-MeasuredMove与结构目标.txt",
        "文件24-最终旗形与趋势末端.txt",
        "文件25-主要趋势反转MTR.txt",
        "文件27-三角形与收敛形态.txt",
        "文件28-双重顶底与微型结构.txt",
        "下跌通道分析识别.txt",
        "下跌通道交易策略.txt",
        "震荡区间分析识别.txt",
        "震荡区间交易策略.txt",
        "逐棒分析检查单.txt",
    }
)
EXPECTED_REQUIRED_SOURCE_FILES = frozenset(
    {
        "pyproject.toml",
        "pa_agent/__init__.py",
        "pa_agent/build_info.py",
        "pa_agent/main.py",
        "pa_agent/execution/worker_cli.py",
        "pa_agent/release_contract.py",
        "pa_agent/release_pipeline.py",
        "pa_agent/safety_defaults.py",
        "scripts/install_windows.ps1",
        "scripts/publish_release.ps1",
        "scripts/uninstall_windows.ps1",
        "scripts/release_pipeline.py",
        "docs/SOURCE_INSTALL_WINDOWS.md",
        "tests/visual/generate_market_workspace_screenshot.py",
    }
)
SUPPORTED_PYTHON = (3, 12)
EXPECTED_ENTRYPOINTS = {
    "pa-agent": "pa_agent.main:main",
    "pa-execution-worker": "pa_agent.execution.worker_cli:main",
}
EXPECTED_PINNED_VCS_DEPENDENCY = (
    "tvdatafeed @ "
    "git+https://github.com/rongardF/tvdatafeed.git"
    "@e6f6aaa7de439ac6e454d9b26d2760ded8dc4923"
)
_VERSION_RE = re.compile(
    r"""(?m)^__version__\s*=\s*["']([^"']+)["']\s*$"""
)


def source_root() -> Path:
    """返回源码部署根目录。"""

    return Path(__file__).resolve().parents[1]


def _source_version(root: Path) -> str | None:
    try:
        text = (root / "pa_agent" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match is not None else None


def _project_contract(root: Path) -> dict[str, Any]:
    try:
        payload = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return payload


def version_payload(program: str) -> dict[str, str]:
    """返回不含账号、路径、配置或环境变量的版本信息。"""

    return {
        "program": str(program),
        "version": __version__,
        "git_sha": runtime_sha(),
        "delivery": DELIVERY,
    }


def offline_self_check(root: Path | str | None = None) -> dict[str, Any]:
    """只读校验源码部署合同；不启动 Qt、网络或数据库。"""

    project_root = Path(root).resolve() if root is not None else source_root()
    project = _project_contract(project_root)
    project_meta = project.get("project", {})
    project_scripts = project_meta.get("scripts", {})
    dynamic = project_meta.get("dynamic", [])
    dynamic_version = (
        project.get("tool", {})
        .get("setuptools", {})
        .get("dynamic", {})
        .get("version", {})
        .get("attr")
    )
    try:
        prompt_resource_paths = {
            path.relative_to(
                project_root / "prompt_engineering"
            ).as_posix()
            for path in (project_root / "prompt_engineering").rglob("*")
            if path.is_file()
        }
    except OSError:
        prompt_resource_paths = set()
    prompt_resources = len(prompt_resource_paths)

    source_version_value = _source_version(project_root)
    required_source_files_present = all(
        (project_root / relative).is_file()
        for relative in EXPECTED_REQUIRED_SOURCE_FILES
    )
    checks = {
        "python_3_12": sys.version_info[:2] == SUPPORTED_PYTHON,
        "windows": platform.system() == "Windows",
        "version_truth": (
            source_version_value == __version__
            and "version" in dynamic
            and dynamic_version == "pa_agent.__version__"
            and "version" not in project_meta
        ),
        "python_contract": (
            project_meta.get("requires-python") == ">=3.12,<3.13"
        ),
        "prompt_resources": (
            prompt_resource_paths == EXPECTED_PROMPT_RESOURCE_PATHS
            and prompt_resources == EXPECTED_PROMPT_RESOURCE_COUNT
        ),
        "entrypoints": project_scripts == EXPECTED_ENTRYPOINTS,
        "required_source_files": required_source_files_present,
        "pinned_vcs_dependency": (
            EXPECTED_PINNED_VCS_DEPENDENCY
            in project_meta.get("dependencies", [])
        ),
        "default_trading": (
            DEFAULT_EXECUTION_ENABLED is False
            and DEFAULT_AUTO_EXECUTE is False
            and DEFAULT_OKX_LIVE is False
            and DEFAULT_LONGBRIDGE_TRADING is False
            and frozenset({("okx", "demo")})
            == SUPPORTED_NEW_RISK_ROUTES
        ),
    }
    failed_checks = sorted(name for name, passed in checks.items() if not passed)
    return {
        "status": "pass" if not failed_checks else "fail",
        "delivery": DELIVERY,
        "version": __version__,
        "git_sha": runtime_sha(),
        "python": (
            f"{sys.version_info.major}."
            f"{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        ),
        "platform": platform.system(),
        "prompt_resources": prompt_resources,
        "entrypoints": sorted(project_scripts),
        "default_trading": {
            "execution_enabled": DEFAULT_EXECUTION_ENABLED,
            "auto_execute": DEFAULT_AUTO_EXECUTE,
            "okx_live": DEFAULT_OKX_LIVE,
            "longbridge_trading": DEFAULT_LONGBRIDGE_TRADING,
            "new_risk_routes": [
                f"{broker}:{environment}"
                for broker, environment in sorted(
                    SUPPORTED_NEW_RISK_ROUTES
                )
            ],
        },
        "checks": checks,
        "failed_checks": failed_checks,
    }


def handle_offline_info_args(
    argv: list[str],
    *,
    program: str,
) -> int | None:
    """处理两个不会进入应用运行态的信息参数。"""

    args = list(argv)
    if args and not args[0].startswith("-"):
        args = args[1:]
    if args == ["--version"]:
        payload = version_payload(program)
        print(
            f"{payload['program']} {payload['version']} "
            f"({payload['git_sha']})"
        )
        return 0
    if args == ["--self-check"]:
        report = offline_self_check()
        print(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0 if report["status"] == "pass" else 1
    return None
