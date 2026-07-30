"""源码发布包、证据索引与哈希文件的确定性校验。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from pa_agent.release_contract import (
    DELIVERY,
    EXPECTED_ENTRYPOINTS,
    EXPECTED_PINNED_VCS_DEPENDENCY,
    EXPECTED_PROMPT_RESOURCE_COUNT,
    EXPECTED_PROMPT_RESOURCE_PATHS,
    EXPECTED_REQUIRED_SOURCE_FILES,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(
    r"""(?m)^__version__\s*=\s*["']([^"']+)["']\s*$"""
)
_ALLOWED_LAYER_STATUSES = frozenset(
    {
        "not_started",
        "implemented",
        "verified",
        "blocked",
        "not_applicable",
        "superseded",
    }
)
_FIVE_LAYERS = frozenset({"code", "tests", "external", "gui", "runtime"})
_FINAL_LAYER_STATUSES = frozenset({"verified", "not_applicable"})
_REQUIRED_CAPABILITIES = frozenset(
    {
        "new-risk-one-shot-token",
        "worker-v5-runtime",
        "longbridge-readonly-contract",
        "market-workspace-controller",
        "multi-market-workspace",
        "release-source-deployment",
    }
)
_REQUIRED_MARKET_SYMBOLS = frozenset(
    {"AAPL.US", "700.HK", "600519.SH"}
)
_RELEASE_NOT_INCLUDED = [
    "exe",
    "installer",
    "wheel",
    "virtualenv",
    "credentials",
    "runtime_database",
    "logs",
    "market_raw_data",
]
_MARKET_BY_SYMBOL = {
    "AAPL.US": "US",
    "700.HK": "HK",
    "600519.SH": "CN",
}
_FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "scratch",
        "build",
        "dist",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        ".agents",
        ".claude",
    }
)
_FORBIDDEN_SUFFIXES = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".log",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".cer",
        ".crt",
        ".pyc",
        ".whl",
        ".parquet",
        ".feather",
        ".onnx",
        ".pt",
        ".pth",
        ".ckpt",
    }
)
_RUNTIME_DIRS = frozenset({"records", "logs", "experience", "trade_records"})
_MAX_ARCHIVE_ENTRY_BYTES = 50 * 1024 * 1024
_MAX_TEXT_SCAN_BYTES = 2 * 1024 * 1024
_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".cmd",
        ".css",
        ".csv",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".md",
        ".ps1",
        ".py",
        ".qss",
        ".rst",
        ".sh",
        ".bat",
        ".toml",
        ".ts",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
)
_KNOWN_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[A-Z0-9]{16})"
)
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"eyJ[A-Za-z0-9_-]{10,}\."
    r"[A-Za-z0-9_-]{10,}\."
    r"[A-Za-z0-9_-]{10,}"
)
_PRIVATE_PATH_RE = re.compile(
    r"(?:"
    r"(?i:file:///)"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/]+[^<>\r\n\"']+"
    r"|(?i:/(?:home|users)/[^/\s]+/)"
    r")"
)
_REDACTED_PRIVATE_PATH = "[REDACTED_PRIVATE_PATH]"
_DESKTOP_SCENARIOS = (
    "normal",
    "loading",
    "empty",
    "stale",
    "auth_failed",
    "calendar_unknown",
    "switch_failed",
    "analysis_running",
    "analysis_failed",
)
_TRADING_BUTTON_WORDS = (
    "买入",
    "卖出",
    "下单",
    "撤单",
    "平仓",
    "开仓",
    "加仓",
    "减仓",
    "杠杆",
)


class ReleaseValidationError(RuntimeError):
    """发布材料没有满足失败关闭合同。"""


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_utc_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _entry_error(name: str, *, expected_prefix: str) -> str | None:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts:
        return "path_traversal"
    if not pure.parts or pure.parts[0] != expected_prefix:
        return "unexpected_prefix"
    relative = pure.parts[1:]
    if not relative:
        return None
    lowered = tuple(part.lower() for part in relative)
    if any(part in _FORBIDDEN_PARTS for part in lowered):
        return "forbidden_directory"
    file_name = lowered[-1]
    if file_name == ".env" or file_name.startswith(".env."):
        return "secret_environment_file"
    relative_text = "/".join(lowered)
    if relative_text in {
        "config/settings.json",
        "config/settings.local.json",
        "config/secret.key",
        "config/feishu.json",
    }:
        return "runtime_configuration"
    if PurePosixPath(file_name).suffix.lower() in _FORBIDDEN_SUFFIXES:
        return "forbidden_file_type"
    if lowered[0] in _RUNTIME_DIRS:
        return "runtime_data"
    return None


def _archive_file(
    archive: zipfile.ZipFile,
    name: str,
    *,
    max_bytes: int = 2 * 1024 * 1024,
) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > max_bytes:
        raise ReleaseValidationError(f"发布合同文件过大：{name}")
    return archive.read(info)


def _placeholder_secret(value: str) -> bool:
    lowered = value.lower()
    markers = (
        "example",
        "placeholder",
        "redacted",
        "dummy",
        "fake",
        "test",
        "your_",
        "your-",
    )
    return any(marker in lowered for marker in markers)


def _scan_text(
    text: str,
    *,
    label: str,
    reject_private_paths: bool,
) -> None:
    if _PRIVATE_KEY_RE.search(text):
        raise ReleaseValidationError(f"发布材料含私钥头：{label}")
    for pattern, reason in (
        (_KNOWN_SECRET_RE, "已知密钥格式"),
        (_JWT_RE, "JWT 格式令牌"),
    ):
        match = pattern.search(text)
        if match is not None and not _placeholder_secret(match.group(0)):
            raise ReleaseValidationError(
                f"发布材料含{reason}：{label}"
            )
    if reject_private_paths and _PRIVATE_PATH_RE.search(text):
        raise ReleaseValidationError(
            f"发布证据含本机绝对路径：{label}"
        )


def _scan_archive_text(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    *,
    reject_private_paths: bool,
) -> None:
    for info in infos:
        suffix = PurePosixPath(info.filename).suffix.lower()
        if (
            info.is_dir()
            or (suffix and suffix not in _TEXT_SUFFIXES)
        ):
            continue
        if info.file_size > _MAX_TEXT_SCAN_BYTES:
            raise ReleaseValidationError(
                f"发布源码文本文件过大，无法安全扫描：{info.filename}"
            )
        try:
            text = archive.read(info).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseValidationError(
                f"发布源码文本不是 UTF-8：{info.filename}"
            ) from exc
        _scan_text(
            text,
            label=info.filename,
            reject_private_paths=reject_private_paths,
        )


def scan_release_tree(
    directory: Path | str,
    *,
    reject_private_paths: bool,
) -> dict[str, Any]:
    """扫描解包目录或证据目录中的明显密钥与私有绝对路径。"""

    root = Path(directory).resolve()
    if not root.is_dir():
        raise ReleaseValidationError("待扫描目录不存在")
    files_scanned = 0
    text_files_scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        files_scanned += 1
        if path.stat().st_size >= _MAX_ARCHIVE_ENTRY_BYTES:
            raise ReleaseValidationError(
                f"发布材料文件过大：{path.relative_to(root)}"
            )
        suffix = path.suffix.lower()
        if suffix and suffix not in _TEXT_SUFFIXES:
            continue
        if path.stat().st_size > _MAX_TEXT_SCAN_BYTES:
            raise ReleaseValidationError(
                f"发布材料文本文件过大：{path.relative_to(root)}"
            )
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseValidationError(
                f"发布材料文本不是 UTF-8：{path.relative_to(root)}"
            ) from exc
        _scan_text(
            text,
            label=path.relative_to(root).as_posix(),
            reject_private_paths=reject_private_paths,
        )
        text_files_scanned += 1
    return {
        "files_scanned": files_scanned,
        "text_files_scanned": text_files_scanned,
        "private_paths_rejected": reject_private_paths,
        "result": "pass",
    }


def sanitize_junit_report(
    report_path: Path | str,
) -> dict[str, Any]:
    """只脱敏 JUnit 中的本机绝对路径，并保留测试结果合同。"""

    path = Path(report_path).resolve()
    if not path.is_file():
        raise ReleaseValidationError("JUnit 报告不存在")
    if path.stat().st_size > _MAX_TEXT_SCAN_BYTES:
        raise ReleaseValidationError("JUnit 报告过大，无法安全脱敏")
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise ReleaseValidationError("JUnit 报告不是有效 XML") from exc
    root = tree.getroot()
    root_tag = root.tag.rsplit("}", 1)[-1]
    if root_tag not in {"testsuite", "testsuites"}:
        raise ReleaseValidationError("JUnit 报告根节点无效")

    paths_redacted = 0
    for element in root.iter():
        for key, value in tuple(element.attrib.items()):
            redacted, count = _PRIVATE_PATH_RE.subn(
                _REDACTED_PRIVATE_PATH,
                value,
            )
            element.attrib[key] = redacted
            paths_redacted += count
        if element.text is not None:
            element.text, count = _PRIVATE_PATH_RE.subn(
                _REDACTED_PRIVATE_PATH,
                element.text,
            )
            paths_redacted += count
        if element.tail is not None:
            element.tail, count = _PRIVATE_PATH_RE.subn(
                _REDACTED_PRIVATE_PATH,
                element.tail,
            )
            paths_redacted += count

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            tree.write(handle, encoding="utf-8", xml_declaration=True)
            handle.flush()
            os.fsync(handle.fileno())
        sanitized_text = temp_path.read_text(encoding="utf-8")
        if _PRIVATE_PATH_RE.search(sanitized_text):
            raise ReleaseValidationError("JUnit 私有路径脱敏不完整")
        os.replace(temp_path, path)
    except ReleaseValidationError:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)
        raise ReleaseValidationError("无法安全写回 JUnit 报告") from exc
    return {"paths_redacted": paths_redacted, "result": "pass"}


def validate_source_archive(
    archive_path: Path | str,
    *,
    expected_version: str,
    expected_sha: str,
) -> dict[str, Any]:
    """校验 `git archive` 源码 ZIP，禁止运行态和敏感材料混入。"""

    source = Path(archive_path)
    normalized_sha = str(expected_sha).strip().lower()
    if not _SHA_RE.fullmatch(normalized_sha):
        raise ReleaseValidationError("expected_sha 必须是 40 位 Git SHA")
    expected_prefix = f"PA_Agent-v{expected_version}"
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if not infos:
            raise ReleaseValidationError("源码 ZIP 为空")
        forbidden: list[dict[str, str]] = []
        for info in infos:
            reason = _entry_error(info.filename, expected_prefix=expected_prefix)
            if reason is not None:
                forbidden.append({"path": info.filename, "reason": reason})
            if info.file_size >= _MAX_ARCHIVE_ENTRY_BYTES:
                forbidden.append(
                    {"path": info.filename, "reason": "entry_too_large"}
                )
        if forbidden:
            raise ReleaseValidationError(
                "源码 ZIP 含禁止项：" + json.dumps(forbidden, ensure_ascii=False)
            )
        _scan_archive_text(
            archive,
            infos,
            reject_private_paths=False,
        )

        names = {
            info.filename.replace("\\", "/")
            for info in infos
            if not info.is_dir()
        }
        prompt_prefix = f"{expected_prefix}/prompt_engineering/"
        prompt_names = sorted(
            name for name in names if name.startswith(prompt_prefix)
        )
        prompt_relative_names = {
            name[len(prompt_prefix):]
            for name in prompt_names
        }
        if (
            len(prompt_names) != EXPECTED_PROMPT_RESOURCE_COUNT
            or prompt_relative_names != EXPECTED_PROMPT_RESOURCE_PATHS
        ):
            raise ReleaseValidationError(
                "提示词资源路径集合错误："
                + json.dumps(
                    {
                        "count": len(prompt_names),
                        "missing": sorted(
                            EXPECTED_PROMPT_RESOURCE_PATHS
                            - prompt_relative_names
                        ),
                        "extra": sorted(
                            prompt_relative_names
                            - EXPECTED_PROMPT_RESOURCE_PATHS
                        ),
                    },
                    ensure_ascii=False,
                )
            )

        pyproject_name = f"{expected_prefix}/pyproject.toml"
        init_name = f"{expected_prefix}/pa_agent/__init__.py"
        build_info_name = f"{expected_prefix}/pa_agent/build_info.py"
        for required_relative in sorted(EXPECTED_REQUIRED_SOURCE_FILES):
            required = f"{expected_prefix}/{required_relative}"
            if required not in names:
                raise ReleaseValidationError(f"源码 ZIP 缺少：{required}")

        try:
            project = tomllib.loads(
                _archive_file(archive, pyproject_name).decode("utf-8")
            )
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ReleaseValidationError("pyproject.toml 无法解析") from exc
        meta = project.get("project", {})
        dynamic_attr = (
            project.get("tool", {})
            .get("setuptools", {})
            .get("dynamic", {})
            .get("version", {})
            .get("attr")
        )
        if (
            "version" in meta
            or "version" not in meta.get("dynamic", [])
            or dynamic_attr != "pa_agent.__version__"
        ):
            raise ReleaseValidationError("版本真值不是 pa_agent.__version__")
        if meta.get("requires-python") != ">=3.12,<3.13":
            raise ReleaseValidationError("发布包不是 Python 3.12 单版本合同")
        if meta.get("scripts", {}) != EXPECTED_ENTRYPOINTS:
            raise ReleaseValidationError("两个命令入口合同不匹配")
        if EXPECTED_PINNED_VCS_DEPENDENCY not in meta.get(
            "dependencies",
            [],
        ):
            raise ReleaseValidationError("Git 依赖没有固定到完整 commit")

        init_text = _archive_file(archive, init_name).decode("utf-8")
        version_match = _VERSION_RE.search(init_text)
        if version_match is None or version_match.group(1) != expected_version:
            raise ReleaseValidationError("包版本与目标版本不一致")
        build_info_text = _archive_file(archive, build_info_name).decode("utf-8")
        if normalized_sha not in build_info_text.lower():
            raise ReleaseValidationError("git archive 未写入完整构建 SHA")

    return {
        "archive": source.name,
        "sha256": sha256_file(source),
        "size_bytes": source.stat().st_size,
        "version": expected_version,
        "git_sha": normalized_sha,
        "delivery": DELIVERY,
        "prompt_resources": len(prompt_names),
        "entrypoints": sorted(EXPECTED_ENTRYPOINTS),
        "forbidden_entries": [],
    }


def build_release_manifest(
    *,
    version: str,
    git_sha: str,
    source_archive: Path | str,
    evidence_archive: Path | str,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    normalized_sha = str(git_sha).strip().lower()
    if not _SHA_RE.fullmatch(normalized_sha):
        raise ReleaseValidationError("git_sha 必须是 40 位")
    source = Path(source_archive)
    evidence = Path(evidence_archive)
    return {
        "schema_version": 1,
        "version": version,
        "git_sha": normalized_sha,
        "delivery": DELIVERY,
        "created_at_utc": (
            created_at_utc
            or datetime.now(UTC).isoformat(timespec="seconds")
        ),
        "artifacts": {
            "source": {
                "name": source.name,
                "size_bytes": source.stat().st_size,
                "sha256": sha256_file(source),
            },
            "evidence": {
                "name": evidence.name,
                "size_bytes": evidence.stat().st_size,
                "sha256": sha256_file(evidence),
            },
        },
        "not_included": list(_RELEASE_NOT_INCLUDED),
    }


def write_sha256sums(
    paths: Iterable[Path | str],
    output_path: Path | str,
) -> None:
    lines = [
        f"{sha256_file(path)}  {Path(path).name}"
        for path in paths
    ]
    Path(output_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_release_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError("release-manifest.json 无法读取") from exc
    if not isinstance(payload, dict):
        raise ReleaseValidationError("release-manifest.json 顶层不是对象")
    return payload


def _read_sha256sums(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseValidationError("SHA256SUMS 无法读取") from exc
    parsed: dict[str, str] = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-fA-F]{64})  ([^/\\]+)", line)
        if match is None or match.group(2) in parsed:
            raise ReleaseValidationError("SHA256SUMS 格式错误或文件名重复")
        parsed[match.group(2)] = match.group(1).lower()
    return parsed


def validate_release_artifact_set(
    *,
    source_archive: Path | str,
    evidence_archive: Path | str,
    release_manifest: Path | str,
    checksums: Path | str,
    evidence_root: Path | str,
    capability_index: Path | str,
    capabilities: Iterable[dict[str, Any]],
    expected_version: str,
    expected_sha: str,
    expected_index_collected_at: datetime,
) -> dict[str, Any]:
    """把真实源码包、证据包、manifest 和校验和绑定到同一 SHA。"""

    source = Path(source_archive).resolve()
    evidence = Path(evidence_archive).resolve()
    manifest_path = Path(release_manifest).resolve()
    sums_path = Path(checksums).resolve()
    evidence_base = Path(evidence_root).resolve()
    index_path = Path(capability_index).resolve()
    expected_names = {
        "source": f"PA_Agent-v{expected_version}-source.zip",
        "evidence": f"PA_Agent-v{expected_version}-evidence.zip",
        "manifest": "release-manifest.json",
        "checksums": "SHA256SUMS",
    }
    actual_names = {
        "source": source.name,
        "evidence": evidence.name,
        "manifest": manifest_path.name,
        "checksums": sums_path.name,
    }
    if (
        actual_names != expected_names
        or len(set(actual_names.values())) != len(actual_names)
    ):
        raise ReleaseValidationError(
            "发布产物文件名不符合固定合同："
            + json.dumps(actual_names, ensure_ascii=False)
        )

    source_result = validate_source_archive(
        source,
        expected_version=expected_version,
        expected_sha=expected_sha,
    )
    required_files = {index_path}
    for capability in capabilities:
        for entry in capability.get("evidence", []):
            required_files.add(
                _resolve_hashed_evidence(entry, root=evidence_base)
            )
    required_archive_entries: dict[str, str] = {}
    for path in required_files:
        try:
            relative = path.relative_to(evidence_base).as_posix()
        except ValueError as exc:
            raise ReleaseValidationError("证据包文件越出外部证据目录") from exc
        required_archive_entries[relative] = sha256_file(path)

    try:
        archive = zipfile.ZipFile(evidence)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseValidationError("证据 ZIP 无法读取") from exc
    with archive:
        infos = archive.infolist()
        if not infos:
            raise ReleaseValidationError("证据 ZIP 为空")
        normalized_names: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            normalized = info.filename.replace("\\", "/")
            pure = PurePosixPath(normalized)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or normalized in normalized_names
            ):
                raise ReleaseValidationError("证据 ZIP 路径不安全或重复")
            if info.file_size >= _MAX_ARCHIVE_ENTRY_BYTES:
                raise ReleaseValidationError(
                    f"证据 ZIP 文件过大：{normalized}"
                )
            suffix = pure.suffix.lower()
            if suffix in _FORBIDDEN_SUFFIXES:
                raise ReleaseValidationError(
                    f"证据 ZIP 含禁止文件类型：{normalized}"
                )
            normalized_names[normalized] = info
        _scan_archive_text(
            archive,
            infos,
            reject_private_paths=True,
        )
        for relative, expected_hash in required_archive_entries.items():
            info = normalized_names.get(relative)
            if info is None:
                raise ReleaseValidationError(
                    f"证据 ZIP 缺少稳定门文件：{relative}"
                )
            actual_hash = hashlib.sha256(archive.read(info)).hexdigest()
            if actual_hash != expected_hash:
                raise ReleaseValidationError(
                    f"证据 ZIP 文件与外部稳定证据不一致：{relative}"
                )
        actual_files = {
            name
            for name, info in normalized_names.items()
            if not info.is_dir()
        }
        if actual_files != set(required_archive_entries):
            raise ReleaseValidationError(
                "证据 ZIP 必须且只能包含能力索引声明的哈希文件："
                + json.dumps(
                    {
                        "missing": sorted(
                            set(required_archive_entries) - actual_files
                        ),
                        "extra": sorted(
                            actual_files - set(required_archive_entries)
                        ),
                    },
                    ensure_ascii=False,
                )
            )

    for text_path in (manifest_path, sums_path):
        if text_path.stat().st_size > _MAX_TEXT_SCAN_BYTES:
            raise ReleaseValidationError(
                f"发布材料文本文件过大：{text_path.name}"
            )
        try:
            text = text_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseValidationError(
                f"发布材料文本不是 UTF-8：{text_path.name}"
            ) from exc
        _scan_text(
            text,
            label=text_path.name,
            reject_private_paths=True,
        )

    manifest = _read_release_manifest(manifest_path)
    manifest_created_at = _parse_utc_datetime(
        manifest.get("created_at_utc")
    )
    source_hash = sha256_file(source)
    evidence_hash = sha256_file(evidence)
    expected_manifest = {
        "schema_version": 1,
        "version": expected_version,
        "git_sha": expected_sha,
        "delivery": DELIVERY,
        "created_at_utc": manifest.get("created_at_utc"),
        "artifacts": {
            "source": {
                "name": source.name,
                "size_bytes": source.stat().st_size,
                "sha256": source_hash,
            },
            "evidence": {
                "name": evidence.name,
                "size_bytes": evidence.stat().st_size,
                "sha256": evidence_hash,
            },
        },
        "not_included": list(_RELEASE_NOT_INCLUDED),
    }
    manifest_checks = {
        "exact_contract": (
            manifest == expected_manifest
            and type(manifest.get("schema_version")) is int
        ),
        "created_after_index": (
            manifest_created_at is not None
            and expected_index_collected_at
            <= manifest_created_at
            <= expected_index_collected_at + timedelta(minutes=30)
        ),
    }
    failed_manifest = sorted(
        name for name, passed in manifest_checks.items() if not passed
    )
    if failed_manifest:
        raise ReleaseValidationError(
            "release-manifest.json 未绑定真实产物："
            + ", ".join(failed_manifest)
        )

    expected_sums = {
        source.name: source_hash,
        evidence.name: evidence_hash,
        manifest_path.name: sha256_file(manifest_path),
    }
    if _read_sha256sums(sums_path) != expected_sums:
        raise ReleaseValidationError("SHA256SUMS 未精确绑定三个发布文件")
    return {
        "source_sha256": source_hash,
        "evidence_sha256": evidence_hash,
        "manifest_sha256": expected_sums[manifest_path.name],
        "source_contract": source_result,
        "result": "pass",
    }


def validate_capability_index(
    path: Path | str,
    *,
    stable: bool,
    expected_sha: str | None = None,
    expected_version: str | None = None,
    repo_root: Path | str | None = None,
    evidence_root: Path | str | None = None,
    schema_root: Path | str | None = None,
    source_archive: Path | str | None = None,
    evidence_archive: Path | str | None = None,
    release_manifest: Path | str | None = None,
    checksums: Path | str | None = None,
    require_fresh_now: bool = False,
) -> dict[str, Any]:
    repository = Path(repo_root or Path.cwd()).resolve()
    evidence_base = (
        Path(evidence_root).resolve()
        if evidence_root is not None
        else repository
    )
    raw_index_path = Path(path)
    index_path = (
        raw_index_path
        if raw_index_path.is_absolute()
        else evidence_base / raw_index_path
    ).resolve()
    schema_base = (
        Path(schema_root).resolve()
        if schema_root is not None
        else repository / "docs" / "evidence" / "schemas"
    )
    if stable:
        if evidence_root is None or evidence_base == repository:
            raise ReleaseValidationError(
                "稳定发布必须使用提交完成后生成的外部证据目录"
            )
        try:
            index_path.relative_to(evidence_base)
        except ValueError as exc:
            raise ReleaseValidationError(
                "稳定能力索引必须位于外部证据目录内"
            ) from exc
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError("能力索引无法读取") from exc
    schema_path = schema_base / "capability-index.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).validate(payload)
    except (
        OSError,
        json.JSONDecodeError,
        SchemaError,
        ValidationError,
    ) as exc:
        raise ReleaseValidationError("能力索引不符合发布 schema") from exc
    required_version = str(expected_version or "").strip()
    if required_version and payload.get("release_version") != required_version:
        raise ReleaseValidationError("能力索引版本与发布版本不一致")
    normalized_expected_sha = (
        str(expected_sha or "").strip().lower()
    )
    if normalized_expected_sha and not _SHA_RE.fullmatch(
        normalized_expected_sha
    ):
        raise ReleaseValidationError("expected_sha 必须是 40 位 Git SHA")
    if stable:
        if not normalized_expected_sha:
            raise ReleaseValidationError("稳定发布必须绑定目标 Git SHA")
        if payload.get("as_of_git_sha") != normalized_expected_sha:
            raise ReleaseValidationError("能力索引没有绑定目标 Git SHA")
    index_collected_at = _parse_utc_datetime(payload.get("collected_at"))
    if index_collected_at is None:
        raise ReleaseValidationError("能力索引 collected_at 不是带时区时间")

    capabilities = payload.get("capabilities")
    seen: set[str] = set()
    blocked: list[str] = []
    non_final: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for item in capabilities:
        capability_id = str(item.get("capability_id") or "").strip()
        if not capability_id or capability_id in seen:
            raise ReleaseValidationError("能力编号缺失或重复")
        seen.add(capability_id)
        by_id[capability_id] = item
        layers = item.get("layers")
        if not isinstance(layers, dict) or set(layers) != _FIVE_LAYERS:
            raise ReleaseValidationError(
                f"{capability_id} 不是完整五层状态"
            )
        for layer, status in layers.items():
            if status not in _ALLOWED_LAYER_STATUSES:
                raise ReleaseValidationError(
                    f"{capability_id}.{layer} 状态无效：{status}"
                )
            if status == "blocked":
                blocked.append(f"{capability_id}.{layer}")
            if status not in _FINAL_LAYER_STATUSES:
                non_final.append(f"{capability_id}.{layer}")
    if seen != _REQUIRED_CAPABILITIES:
        raise ReleaseValidationError(
            "能力索引必须且只能包含固定六项能力："
            + json.dumps(
                {
                    "missing": sorted(_REQUIRED_CAPABILITIES - seen),
                    "extra": sorted(seen - _REQUIRED_CAPABILITIES),
                },
                ensure_ascii=False,
            )
        )
    declared_blockers = sorted(payload.get("blockers", []))
    if declared_blockers != sorted(non_final):
        raise ReleaseValidationError(
            "能力索引 blockers 与五层非终态不一致"
        )
    ready = payload.get("stable_release_ready") is True
    if ready != (not non_final):
        raise ReleaseValidationError(
            "stable_release_ready 与五层状态不一致"
        )
    if stable:
        if not ready or blocked or non_final:
            raise ReleaseValidationError(
                "稳定发布硬门未通过："
                + json.dumps(
                    {
                        "stable_release_ready": ready,
                        "blocked": blocked,
                        "non_final": non_final,
                    },
                    ensure_ascii=False,
                )
            )
        if not required_version:
            raise ReleaseValidationError("稳定发布必须指定发布版本")
        artifact_inputs = {
            "source_archive": source_archive,
            "evidence_archive": evidence_archive,
            "release_manifest": release_manifest,
            "checksums": checksums,
        }
        missing_artifacts = sorted(
            name for name, value in artifact_inputs.items() if value is None
        )
        if missing_artifacts:
            raise ReleaseValidationError(
                "稳定发布缺少真实发布产物："
                + ", ".join(missing_artifacts)
            )
        evidence_scan = scan_release_tree(
            evidence_base,
            reject_private_paths=True,
        )
        try:
            source_hash = sha256_file(source_archive)
        except OSError as exc:
            raise ReleaseValidationError("稳定发布源码 ZIP 无法读取") from exc
        evidence_payloads = _validate_stable_evidence(
            by_id,
            evidence_root=evidence_base,
            schema_root=schema_base,
            expected_sha=normalized_expected_sha,
            expected_source_sha=source_hash,
            index_collected_at=index_collected_at,
            require_fresh_now=require_fresh_now,
        )
        artifact_result = validate_release_artifact_set(
            source_archive=source_archive,
            evidence_archive=evidence_archive,
            release_manifest=release_manifest,
            checksums=checksums,
            evidence_root=evidence_base,
            capability_index=index_path,
            capabilities=capabilities,
            expected_version=required_version,
            expected_sha=normalized_expected_sha,
            expected_index_collected_at=index_collected_at,
        )
        evidence_payloads["content_scan"] = evidence_scan
        evidence_payloads["artifacts"] = artifact_result
    else:
        evidence_payloads = {}
    return {
        "capability_count": len(capabilities),
        "stable_release_ready": ready,
        "blocked_layers": blocked,
        "non_final_layers": non_final,
        "verified_evidence": evidence_payloads,
    }


def _resolve_hashed_evidence(
    entry: dict[str, Any],
    *,
    root: Path,
) -> Path:
    relative = PurePosixPath(str(entry.get("path") or "").replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ReleaseValidationError("能力证据路径不安全")
    path = (root / Path(*relative.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ReleaseValidationError("能力证据路径越出发布根目录") from exc
    expected_hash = str(entry.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ReleaseValidationError(f"能力证据缺少 SHA-256：{relative}")
    if not path.is_file() or sha256_file(path).lower() != expected_hash:
        raise ReleaseValidationError(f"能力证据文件或哈希不匹配：{relative}")
    return path


def _load_json_evidence(
    entry: dict[str, Any],
    *,
    root: Path,
    schema_root: Path,
) -> dict[str, Any]:
    path = _resolve_hashed_evidence(entry, root=root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(f"JSON 证据无法读取：{path.name}") from exc
    schema_ref = str(payload.get("$schema") or "").strip()
    if not schema_ref:
        raise ReleaseValidationError(f"JSON 证据缺少 $schema：{path.name}")
    relative_schema = PurePosixPath(schema_ref.replace("\\", "/"))
    if relative_schema.is_absolute() or ".." in relative_schema.parts:
        raise ReleaseValidationError(f"JSON 证据 schema 路径不安全：{path.name}")
    schema_path = (schema_root / relative_schema.name).resolve()
    allowed_schema_root = schema_root.resolve()
    try:
        schema_path.relative_to(allowed_schema_root)
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).validate(payload)
    except (
        OSError,
        json.JSONDecodeError,
        SchemaError,
        ValidationError,
        ValueError,
    ) as exc:
        raise ReleaseValidationError(
            f"JSON 证据不符合发布 schema：{path.name}"
        ) from exc
    return payload


def _evidence_entries(
    capability: dict[str, Any],
    *,
    kind: str,
) -> list[dict[str, Any]]:
    return [
        entry
        for entry in capability.get("evidence", [])
        if entry.get("kind") == kind
    ]


def _require_runtime_acceptance(
    capability: dict[str, Any],
    *,
    root: Path,
    schema_root: Path,
    expected_sha: str,
    index_collected_at: datetime,
    require_fresh_now: bool,
) -> dict[str, Any]:
    entries = _evidence_entries(capability, kind="runtime-acceptance")
    if len(entries) != 1:
        raise ReleaseValidationError("稳定发布缺少唯一运行态验收证据")
    payload = _load_json_evidence(
        entries[0],
        root=root,
        schema_root=schema_root,
    )
    target_commit_at = _parse_utc_datetime(payload.get("target_commit_at"))
    worker_started_at = _parse_utc_datetime(payload.get("worker_started_at"))
    last_reconciled_at = _parse_utc_datetime(
        payload.get("last_reconciled_at")
    )
    collected_at = _parse_utc_datetime(payload.get("collected_at"))
    timeline_valid = (
        target_commit_at is not None
        and worker_started_at is not None
        and last_reconciled_at is not None
        and collected_at is not None
        and target_commit_at
        <= worker_started_at
        <= last_reconciled_at
        <= collected_at
    )
    checks = {
        "sample": payload.get("sample") is False,
        "git_sha": payload.get("git_sha") == expected_sha,
        "broker": payload.get("broker") == "okx",
        "environment": payload.get("environment") == "demo",
        "schema_v5": payload.get("worker_schema_version") == 5,
        "timeline": timeline_valid,
        "fresh_reconciliation": (
            timeline_valid
            and collected_at - last_reconciled_at <= timedelta(minutes=15)
        ),
        "snapshot_bound_to_index": (
            timeline_valid
            and collected_at
            <= index_collected_at
            <= collected_at + timedelta(minutes=15)
        ),
        "migration_preserved_risk_stop": (
            payload.get("migration_preserved_risk_stop") is True
        ),
        "config_fingerprint": bool(payload.get("config_fingerprint")),
        "database_ok": payload.get("database_quick_check") == "ok",
        "control_database_ok": (
            payload.get("control_database_quick_check") == "ok"
        ),
        "risk_stop_clear": payload.get("risk_stop", {}).get("active") is False,
        "no_execution": payload.get("active_execution_count") == 0,
        "no_command": payload.get("unresolved_command_count") == 0,
        "no_lease": payload.get("active_new_risk_lease_count") == 0,
        "no_position": payload.get("broker_position_count") == 0,
        "no_order": payload.get("broker_pending_order_count") == 0,
        "no_algo_order": payload.get("broker_pending_algo_order_count") == 0,
        "campaign_stopped": payload.get("campaign_state") == "stopped",
        "cycle_entry_submitted": payload.get(
            "controlled_reproducible",
            {},
        ).get("entry_submitted")
        is True,
        "cycle_entry_filled": payload.get(
            "controlled_reproducible",
            {},
        ).get("entry_filled")
        is True,
        "cycle_one_command": payload.get(
            "controlled_reproducible",
            {},
        ).get("new_risk_command_count")
        == 1,
        "cycle_one_lease": payload.get(
            "controlled_reproducible",
            {},
        ).get("new_risk_lease_count")
        == 1,
        "cycle_unique_binding": payload.get(
            "controlled_reproducible",
            {},
        ).get("lease_command_binding_unique")
        is True,
        "cycle_worker_route": payload.get(
            "controlled_reproducible",
            {},
        ).get("worker_route_verified")
        is True,
        "cycle_worker_requester": payload.get(
            "controlled_reproducible",
            {},
        ).get("worker_requester_verified")
        is True,
        "cycle_worker_fingerprint": payload.get(
            "controlled_reproducible",
            {},
        ).get("worker_config_fingerprint_verified")
        is True,
        "cycle_native_protection": int(
            payload.get("controlled_reproducible", {}).get(
                "native_protection_count",
                0,
            )
        )
        >= 2,
        "cycle_exit_requested": payload.get(
            "controlled_reproducible",
            {},
        ).get("active_exit_requested")
        is True,
        "cycle_closed": payload.get(
            "controlled_reproducible",
            {},
        ).get("closed")
        is True,
        "cycle_final_reconciliation": payload.get(
            "controlled_reproducible",
            {},
        ).get("final_reconciliation")
        is True,
        "result": payload.get("result") == "pass",
    }
    if require_fresh_now:
        now = datetime.now(UTC)
        checks["fresh_at_publish_time"] = (
            timeline_valid
            and now - timedelta(minutes=15)
            <= collected_at
            <= now + timedelta(minutes=5)
        )
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ReleaseValidationError(
            "运行态验收证据未通过：" + ", ".join(failed)
        )
    return {"path": entries[0]["path"], "result": "pass"}


def _require_market_acceptance(
    capability: dict[str, Any],
    *,
    root: Path,
    schema_root: Path,
    expected_sha: str,
) -> dict[str, Any]:
    entries = _evidence_entries(capability, kind="market-acceptance")
    accepted_symbols: set[str] = set()
    for entry in entries:
        payload = _load_json_evidence(
            entry,
            root=root,
            schema_root=schema_root,
        )
        symbol = str(payload.get("symbol") or "").upper()
        timeframes = {
            item.get("timeframe"): item
            for item in payload.get("timeframes", [])
        }
        ten_minute = timeframes.get("10m", {})
        checks = {
            "sample": payload.get("sample") is False,
            "git_sha": payload.get("git_sha") == expected_sha,
            "source": payload.get("source") == "longbridge",
            "symbol_market": (
                _MARKET_BY_SYMBOL.get(symbol) == payload.get("market")
            ),
            "permission": payload.get("permission")
            in {"realtime", "delayed"},
            "permission_derivation": payload.get(
                "permission_derivation"
            )
            == "longbridge_server_quote_level_package",
            "server_quote_level": bool(
                str(payload.get("server_quote_level") or "").strip()
            ),
            "server_packages": isinstance(
                payload.get("server_packages"),
                list,
            ),
            "analysis_as_of": isinstance(
                payload.get("analysis_as_of_utc_ms"),
                int,
            ),
            "10m_count": int(ten_minute.get("bar_count") or 0) > 0,
            "10m_first": isinstance(
                ten_minute.get("first_utc_ms"),
                int,
            ),
            "10m_closed": isinstance(
                ten_minute.get("last_closed_utc_ms"),
                int,
            ),
            "10m_complete": ten_minute.get("missing_bars") is False,
            "time_order": (
                isinstance(ten_minute.get("first_utc_ms"), int)
                and isinstance(
                    ten_minute.get("last_closed_utc_ms"),
                    int,
                )
                and isinstance(payload.get("analysis_as_of_utc_ms"), int)
                and ten_minute["first_utc_ms"]
                <= ten_minute["last_closed_utc_ms"]
                <= payload["analysis_as_of_utc_ms"]
            ),
            "result": payload.get("result") in {"pass", "display_only"},
        }
        if payload.get("price_tick_authoritative") is not True:
            checks["display_only_without_tick"] = (
                payload.get("result") == "display_only"
            )
        failed = sorted(name for name, passed in checks.items() if not passed)
        if failed:
            raise ReleaseValidationError(
                f"行情验收证据 {symbol or 'unknown'} 未通过："
                + ", ".join(failed)
            )
        accepted_symbols.add(symbol)
    missing = _REQUIRED_MARKET_SYMBOLS - accepted_symbols
    if missing:
        raise ReleaseValidationError(
            "稳定发布缺少真实 Longbridge 标的验收："
            + ", ".join(sorted(missing))
        )
    return {
        "paths": sorted(str(entry["path"]) for entry in entries),
        "symbols": sorted(accepted_symbols),
    }


def _require_desktop_acceptance(
    capability: dict[str, Any],
    *,
    root: Path,
    schema_root: Path,
    expected_sha: str,
) -> dict[str, Any]:
    entries = _evidence_entries(capability, kind="desktop-acceptance")
    if len(entries) != 1:
        raise ReleaseValidationError("稳定发布缺少唯一正式快捷方式验收证据")
    payload = _load_json_evidence(
        entries[0],
        root=root,
        schema_root=schema_root,
    )
    checks = {
        "sample": payload.get("sample") is False,
        "git_sha": payload.get("git_sha") == expected_sha,
        "official_shortcut": payload.get("launch") == "official_shortcut",
        "markets": set(payload.get("markets", []))
        == {"US", "HK", "CN", "Crypto"},
        "sensitive": payload.get("contains_sensitive_data") is False,
        "sizes": {
            tuple(item)
            for item in payload.get("logical_sizes_checked", [])
        }
        == {(1440, 900), (1920, 1080)},
        "scales": set(payload.get("scales_checked", []))
        == {100, 125, 150},
        "symbols": _REQUIRED_MARKET_SYMBOLS.issubset(
            {
                str(symbol).upper()
                for symbol in payload.get("symbols_checked", [])
            }
        ),
        "crypto_symbol": "XAU-USDT-SWAP"
        in {
            str(symbol).upper()
            for symbol in payload.get("symbols_checked", [])
        },
        "fast_switch": "fast_switch"
        in payload.get("scenarios_checked", []),
        "analysis_during_switch": "analysis_during_switch"
        in payload.get("scenarios_checked", []),
        "user_accepted": payload.get("user_accepted") is True,
        "full_git_sha_visible": (
            payload.get("full_git_sha_visible") is True
        ),
        "no_trading_controls": payload.get("no_trading_controls") is True,
        "no_execution_access": payload.get("execution_access_count") == 0,
        "result": payload.get("result") == "pass",
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ReleaseValidationError(
            "正式快捷方式验收证据未通过：" + ", ".join(failed)
        )
    return {"path": entries[0]["path"], "result": "pass"}


def _require_release_acceptance(
    capability: dict[str, Any],
    *,
    root: Path,
    schema_root: Path,
    expected_sha: str,
    expected_source_sha: str,
) -> dict[str, Any]:
    entries = _evidence_entries(capability, kind="release-acceptance")
    if len(entries) != 1:
        raise ReleaseValidationError("稳定发布缺少唯一源码部署验收证据")
    payload = _load_json_evidence(
        entries[0],
        root=root,
        schema_root=schema_root,
    )
    checks = {
        "sample": payload.get("sample") is False,
        "git_sha": payload.get("git_sha") == expected_sha,
        "python": str(payload.get("python_version") or "").startswith("3.12."),
        "editable_install": payload.get("editable_install") == "pass",
        "prompt_resources": payload.get("prompt_resources") == 37,
        "entrypoints": sorted(payload.get("entrypoints", []))
        == ["pa-agent", "pa-execution-worker"],
        "shortcut": payload.get("shortcut") == "pass",
        "default_trading": payload.get("default_trading") == "off",
        "sensitive_scan": payload.get("sensitive_file_scan") == "pass",
        "archive_sha": (
            str(payload.get("source_archive_sha256") or "").lower()
            == expected_source_sha
        ),
        "result": payload.get("result") == "pass",
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ReleaseValidationError(
            "源码部署验收证据未通过：" + ", ".join(failed)
        )
    return {"path": entries[0]["path"], "result": "pass"}


def _validate_stable_evidence(
    by_id: dict[str, dict[str, Any]],
    *,
    evidence_root: Path,
    schema_root: Path,
    expected_sha: str,
    expected_source_sha: str,
    index_collected_at: datetime,
    require_fresh_now: bool,
) -> dict[str, Any]:
    for capability_id, capability in by_id.items():
        evidence = capability.get("evidence", [])
        if not evidence:
            raise ReleaseValidationError(
                f"稳定发布能力缺少证据：{capability_id}"
            )
        for entry in evidence:
            _resolve_hashed_evidence(entry, root=evidence_root)
    return {
        "runtime": _require_runtime_acceptance(
            by_id["worker-v5-runtime"],
            root=evidence_root,
            schema_root=schema_root,
            expected_sha=expected_sha,
            index_collected_at=index_collected_at,
            require_fresh_now=require_fresh_now,
        ),
        "market": _require_market_acceptance(
            by_id["longbridge-readonly-contract"],
            root=evidence_root,
            schema_root=schema_root,
            expected_sha=expected_sha,
        ),
        "desktop": _require_desktop_acceptance(
            by_id["multi-market-workspace"],
            root=evidence_root,
            schema_root=schema_root,
            expected_sha=expected_sha,
        ),
        "release": _require_release_acceptance(
            by_id["release-source-deployment"],
            root=evidence_root,
            schema_root=schema_root,
            expected_sha=expected_sha,
            expected_source_sha=expected_source_sha,
        ),
    }


def _desktop_evidence_contract() -> dict[str, tuple[str, int, int, float]]:
    contract = {
        f"1440x900-{scenario}": (scenario, 1440, 900, 1.0)
        for scenario in _DESKTOP_SCENARIOS
    }
    contract["1920x1080-normal"] = ("normal", 1920, 1080, 1.0)
    for scale in (1.0, 1.25, 1.5):
        scale_name = str(scale).replace(".", "p")
        for width, height in ((1440, 900), (1920, 1080)):
            contract[f"scale-{scale_name}-{width}x{height}"] = (
                "normal",
                width,
                height,
                scale,
            )
    return contract


def _decoded_png_size(path: Path) -> tuple[int, int]:
    if path.stat().st_size >= _MAX_ARCHIVE_ENTRY_BYTES:
        raise ReleaseValidationError(f"桌面证据图片过大：{path.name}")
    try:
        from PyQt6.QtGui import QImageReader
    except ImportError as exc:
        raise ReleaseValidationError("无法加载 Qt 图片解码器") from exc
    reader = QImageReader(str(path))
    if not reader.canRead():
        raise ReleaseValidationError(f"桌面证据图片无法读取：{path.name}")
    image = reader.read()
    if image.isNull():
        raise ReleaseValidationError(f"桌面证据图片解码失败：{path.name}")
    return image.width(), image.height()


def validate_desktop_evidence(
    directory: Path | str,
    *,
    expected_sha: str,
) -> dict[str, Any]:
    """校验 16 份离屏图及其元数据，不把 fixture 冒充真实桌面。"""

    normalized_sha = str(expected_sha).strip().lower()
    if not _SHA_RE.fullmatch(normalized_sha):
        raise ReleaseValidationError("expected_sha 必须是 40 位 Git SHA")
    root = Path(directory)
    if not root.is_dir():
        raise ReleaseValidationError("桌面证据目录不存在")
    contract = _desktop_evidence_contract()
    actual_pngs = {path.stem for path in root.glob("*.png")}
    actual_json = {path.stem for path in root.glob("*.json")}
    required = set(contract)
    if actual_pngs != required or actual_json != required:
        raise ReleaseValidationError(
            "桌面证据文件集合错误："
            + json.dumps(
                {
                    "missing_png": sorted(required - actual_pngs),
                    "extra_png": sorted(actual_pngs - required),
                    "missing_json": sorted(required - actual_json),
                    "extra_json": sorted(actual_json - required),
                },
                ensure_ascii=False,
            )
        )

    for stem, (scenario, width, height, scale) in contract.items():
        metadata_path = root / f"{stem}.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseValidationError(
                f"桌面证据元数据无法读取：{metadata_path.name}"
            ) from exc
        expected_physical = [
            round(width * scale),
            round(height * scale),
        ]
        checks = {
            "git_sha": str(metadata.get("git_sha") or "").lower()
            == normalized_sha,
            "scenario": metadata.get("scenario") == scenario,
            "logical_window": metadata.get("logical_window") == [width, height],
            "physical_image": metadata.get("physical_image")
            == expected_physical,
            "requested_scale": metadata.get("requested_scale") == scale,
            "device_pixel_ratio": metadata.get("device_pixel_ratio") == scale,
            "runtime_reads": metadata.get("ui_runtime_read_calls") == 0,
            "cjk_font_family": bool(
                str(metadata.get("font", {}).get("family") or "").strip()
            ),
            "cjk_sample_supported": (
                metadata.get("font", {}).get("cjk_sample_supported") is True
            ),
            "body_font": int(metadata.get("font", {}).get("body_pixel_size", 0))
            >= 14,
            "symbol_font": int(
                metadata.get("font", {}).get("symbol_pixel_size", 0)
            )
            >= 20,
        }
        button_text = " ".join(
            str(item) for item in metadata.get("button_texts", [])
        )
        checks["no_trading_buttons"] = not any(
            word in button_text for word in _TRADING_BUTTON_WORDS
        )
        decoded_size = _decoded_png_size(root / f"{stem}.png")
        checks["decoded_size"] = list(decoded_size) == expected_physical
        failed = sorted(name for name, passed in checks.items() if not passed)
        if failed:
            raise ReleaseValidationError(
                f"桌面证据 {stem} 校验失败：{', '.join(failed)}"
            )

    return {
        "evidence_count": len(contract),
        "git_sha": normalized_sha,
        "scenarios": list(_DESKTOP_SCENARIOS),
        "sizes": ["1440x900", "1920x1080"],
        "scales": [100, 125, 150],
        "fixture_only": True,
        "runtime_reads": 0,
    }


def archive_source(
    *,
    repo_root: Path | str,
    output_dir: Path | str,
    ref: str,
    version: str,
) -> Path:
    root = Path(repo_root).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / f"PA_Agent-v{version}-source.zip"
    if archive_path.exists():
        raise ReleaseValidationError(f"拒绝覆盖已有文件：{archive_path}")
    archive_env = os.environ.copy()
    archive_env["TZ"] = "UTC"
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "archive",
            "--format=zip",
            f"--prefix=PA_Agent-v{version}/",
            "--output",
            str(archive_path),
            ref,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        env=archive_env,
    )
    if completed.returncode != 0:
        raise ReleaseValidationError(
            "git archive 失败：" + completed.stderr.strip()
        )
    return archive_path
