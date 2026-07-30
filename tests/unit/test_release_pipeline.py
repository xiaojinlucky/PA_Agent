from __future__ import annotations

import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

import pa_agent.release_pipeline as release_pipeline
from pa_agent.release_contract import (
    EXPECTED_PROMPT_RESOURCE_PATHS,
    EXPECTED_REQUIRED_SOURCE_FILES,
)
from pa_agent.release_pipeline import (
    ReleaseValidationError,
    build_release_manifest,
    scan_release_tree,
    validate_capability_index,
    validate_desktop_evidence,
    validate_source_archive,
    write_sha256sums,
)

_FULL_SHA = "a" * 40
_ROOT = Path(__file__).resolve().parents[2]
_RECORD_SOURCE_PATHS = frozenset(
    {
        "pa_agent/records/__init__.py",
        "pa_agent/records/analysis_history.py",
        "pa_agent/records/experience_reader.py",
        "pa_agent/records/pending_writer.py",
        "pa_agent/records/schema.py",
        "pa_agent/records/supervisor_writer.py",
        "pa_agent/records/trade_logger.py",
    }
)
_SCHEMA_ROOT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "evidence"
    / "schemas"
)
_CAPABILITY_IDS = (
    "new-risk-one-shot-token",
    "worker-v5-runtime",
    "longbridge-readonly-contract",
    "market-workspace-controller",
    "multi-market-workspace",
    "release-source-deployment",
)
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


def _valid_archive(
    path: Path,
    *,
    extra: dict[str, bytes] | None = None,
    omit: set[str] | None = None,
) -> None:
    prefix = "PA_Agent-v0.1.0"
    files: dict[str, bytes] = {
        f"{prefix}/pyproject.toml": b"""
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
""",
        f"{prefix}/pa_agent/__init__.py": b'__version__ = "0.1.0"\n',
        f"{prefix}/pa_agent/build_info.py": (
            f'_ARCHIVE_SHA = "{_FULL_SHA}"\n'.encode()
        ),
    }
    for relative in EXPECTED_REQUIRED_SOURCE_FILES:
        files.setdefault(f"{prefix}/{relative}", b"fixture\n")
    for relative in EXPECTED_PROMPT_RESOURCE_PATHS:
        files[f"{prefix}/prompt_engineering/{relative}"] = b"ok\n"
    files.update(extra or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            if name in (omit or set()):
                continue
            archive.writestr(name, content)


def _desktop_evidence(root: Path) -> None:
    from PyQt6.QtGui import QColor, QImage

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
    root.mkdir()
    for stem, (scenario, width, height, scale) in contract.items():
        physical = [round(width * scale), round(height * scale)]
        image = QImage(
            physical[0],
            physical[1],
            QImage.Format.Format_RGB32,
        )
        image.fill(QColor("#101820"))
        assert image.save(str(root / f"{stem}.png"))
        metadata = {
            "git_sha": _FULL_SHA,
            "scenario": scenario,
            "logical_window": [width, height],
            "physical_image": physical,
            "device_pixel_ratio": scale,
            "requested_scale": scale,
            "capture_contract": "synchronous-widget-render-v1",
            "image_sha256": hashlib.sha256(
                (root / f"{stem}.png").read_bytes()
            ).hexdigest(),
            "font": {
                "family": "Microsoft YaHei UI",
                "cjk_sample_supported": True,
                "required_ui_glyphs_supported": True,
                "symbol_pixel_size": 22,
                "body_pixel_size": 14,
            },
            "button_texts": ["刷新", "开始分析"],
            "ui_runtime_read_calls": 0,
        }
        (root / f"{stem}.json").write_text(
            json.dumps(metadata),
            encoding="utf-8",
        )


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _stable_bundle(
    tmp_path: Path,
    *,
    collected_now: datetime | None = None,
) -> tuple[Path, Path, dict, dict[str, Path]]:
    bundle = tmp_path / "external-evidence"
    bundle.mkdir()
    artifacts_root = tmp_path / "release-artifacts"
    artifacts_root.mkdir()
    source_archive = artifacts_root / "PA_Agent-v0.1.0-source.zip"
    _valid_archive(source_archive)
    source_sha = hashlib.sha256(source_archive.read_bytes()).hexdigest()
    now = collected_now or datetime.now(UTC)
    collected_at = now.isoformat().replace("+00:00", "Z")
    reconciled_at = (now - timedelta(minutes=1)).isoformat().replace(
        "+00:00",
        "Z",
    )
    worker_started_at = (now - timedelta(minutes=30)).isoformat().replace(
        "+00:00",
        "Z",
    )
    target_commit_at = (now - timedelta(hours=1)).isoformat().replace(
        "+00:00",
        "Z",
    )

    runtime = {
        "$schema": "runtime-snapshot.schema.json",
        "schema_version": 1,
        "sample": False,
        "collected_at": collected_at,
        "git_sha": _FULL_SHA,
        "broker": "okx",
        "environment": "demo",
        "worker_schema_version": 5,
        "target_commit_at": target_commit_at,
        "worker_started_at": worker_started_at,
        "config_fingerprint": "b" * 64,
        "last_reconciled_at": reconciled_at,
        "migration_preserved_risk_stop": True,
        "risk_stop": {"active": False, "reason_code": None},
        "database_quick_check": "ok",
        "control_database_quick_check": "ok",
        "active_execution_count": 0,
        "unresolved_command_count": 0,
        "active_new_risk_lease_count": 0,
        "broker_position_count": 0,
        "broker_pending_order_count": 0,
        "broker_pending_algo_order_count": 0,
        "campaign_state": "stopped",
        "controlled_reproducible": {
            "entry_submitted": True,
            "entry_filled": True,
            "new_risk_command_count": 1,
            "new_risk_lease_count": 1,
            "lease_command_binding_unique": True,
            "worker_route_verified": True,
            "worker_requester_verified": True,
            "worker_config_fingerprint_verified": True,
            "native_protection_count": 2,
            "active_exit_requested": True,
            "closed": True,
            "final_reconciliation": True,
        },
        "result": "pass",
    }
    _write_json(bundle / "runtime.json", runtime)

    market_paths: list[str] = []
    for market, symbol in (
        ("US", "AAPL.US"),
        ("HK", "700.HK"),
        ("CN", "600519.SH"),
    ):
        file_name = f"market-{market.lower()}.json"
        market_payload = {
            "$schema": "market-acceptance.schema.json",
            "schema_version": 1,
            "sample": False,
            "git_sha": _FULL_SHA,
            "market": market,
            "symbol": symbol,
            "source": "longbridge",
            "analysis_as_of_utc_ms": 1785312000000,
            "permission": "realtime",
            "permission_derivation": (
                "longbridge_server_quote_level_package"
            ),
            "server_quote_level": "Lv1",
            "server_packages": [],
            "price_tick_authoritative": False,
            "timeframes": [
                {
                    "timeframe": "10m",
                    "bar_count": 60,
                    "first_utc_ms": 1785276000000,
                    "last_closed_utc_ms": 1785311400000,
                    "missing_bars": False,
                }
            ],
            "result": "display_only",
        }
        _write_json(bundle / file_name, market_payload)
        market_paths.append(file_name)

    desktop = {
        "$schema": "desktop-acceptance.schema.json",
        "schema_version": 1,
        "sample": False,
        "git_sha": _FULL_SHA,
        "launch": "official_shortcut",
        "logical_size": [1440, 900],
        "scale_percent": 100,
        "scenario": "normal",
        "markets": ["US", "HK", "CN", "Crypto"],
        "logical_sizes_checked": [[1440, 900], [1920, 1080]],
        "scales_checked": [100, 125, 150],
        "symbols_checked": [
            "AAPL.US",
            "700.HK",
            "600519.SH",
            "XAU-USDT-SWAP",
        ],
        "scenarios_checked": [
            "fast_switch",
            "analysis_during_switch",
        ],
        "user_accepted": True,
        "full_git_sha_visible": True,
        "no_trading_controls": True,
        "execution_access_count": 0,
        "contains_sensitive_data": False,
        "result": "pass",
    }
    _write_json(bundle / "desktop.json", desktop)

    release = {
        "$schema": "release-acceptance.schema.json",
        "schema_version": 1,
        "sample": False,
        "git_sha": _FULL_SHA,
        "python_version": "3.12.12",
        "editable_install": "pass",
        "prompt_resources": 37,
        "entrypoints": ["pa-agent", "pa-execution-worker"],
        "shortcut": "pass",
        "default_trading": "off",
        "sensitive_file_scan": "pass",
        "source_archive_sha256": source_sha,
        "result": "pass",
    }
    _write_json(bundle / "release.json", release)
    (bundle / "generic.txt").write_text("pass\n", encoding="utf-8")

    evidence_by_capability = {
        "new-risk-one-shot-token": [
            {"path": "generic.txt", "kind": "test-report"}
        ],
        "worker-v5-runtime": [
            {"path": "runtime.json", "kind": "runtime-acceptance"}
        ],
        "longbridge-readonly-contract": [
            {"path": path, "kind": "market-acceptance"}
            for path in market_paths
        ],
        "market-workspace-controller": [
            {"path": "generic.txt", "kind": "test-report"}
        ],
        "multi-market-workspace": [
            {"path": "desktop.json", "kind": "desktop-acceptance"}
        ],
        "release-source-deployment": [
            {"path": "release.json", "kind": "release-acceptance"}
        ],
    }
    capabilities = []
    for capability_id in _CAPABILITY_IDS:
        evidence = []
        for entry in evidence_by_capability[capability_id]:
            evidence.append(
                {
                    **entry,
                    "sha256": hashlib.sha256(
                        (bundle / entry["path"]).read_bytes()
                    ).hexdigest(),
                }
            )
        capabilities.append(
            {
                "evidence_id": f"E-{capability_id}",
                "capability_id": capability_id,
                "capability": capability_id,
                "git_sha": _FULL_SHA,
                "layers": {
                    "code": "verified",
                    "tests": "verified",
                    "external": "verified",
                    "gui": "not_applicable",
                    "runtime": "not_applicable",
                },
                "evidence": evidence,
                "superseded_by": None,
            }
        )
    index = {
        "$schema": "capability-index.schema.json",
        "schema_version": 1,
        "release_version": "0.1.0",
        "as_of_git_sha": _FULL_SHA,
        "collected_at": collected_at,
        "stable_release_ready": True,
        "blockers": [],
        "capabilities": capabilities,
    }
    index_path = bundle / "capability-index.json"
    _write_json(index_path, index)
    evidence_archive = (
        artifacts_root / "PA_Agent-v0.1.0-evidence.zip"
    )
    with zipfile.ZipFile(
        evidence_archive,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())
    manifest_path = artifacts_root / "release-manifest.json"
    manifest = build_release_manifest(
        version="0.1.0",
        git_sha=_FULL_SHA,
        source_archive=source_archive,
        evidence_archive=evidence_archive,
        created_at_utc=collected_at,
    )
    _write_json(manifest_path, manifest)
    sums_path = artifacts_root / "SHA256SUMS"
    write_sha256sums(
        [source_archive, evidence_archive, manifest_path],
        sums_path,
    )
    return bundle, index_path, index, {
        "source_archive": source_archive,
        "evidence_archive": evidence_archive,
        "release_manifest": manifest_path,
        "checksums": sums_path,
    }


def _refresh_evidence_hash(
    index: dict,
    *,
    capability_id: str,
    path: str,
    bundle: Path,
) -> None:
    capability = next(
        item
        for item in index["capabilities"]
        if item["capability_id"] == capability_id
    )
    entry = next(item for item in capability["evidence"] if item["path"] == path)
    entry["sha256"] = hashlib.sha256((bundle / path).read_bytes()).hexdigest()


def _validate_stable(
    *,
    tmp_path: Path,
    bundle: Path,
    index_path: Path,
    artifacts: dict[str, Path],
    require_fresh_now: bool = False,
) -> dict:
    return validate_capability_index(
        index_path,
        stable=True,
        expected_sha=_FULL_SHA,
        expected_version="0.1.0",
        repo_root=tmp_path,
        evidence_root=bundle,
        schema_root=_SCHEMA_ROOT,
        require_fresh_now=require_fresh_now,
        **artifacts,
    )


def _refresh_artifact_metadata(
    artifacts: dict[str, Path],
    *,
    collected_at: str,
) -> None:
    manifest = build_release_manifest(
        version="0.1.0",
        git_sha=_FULL_SHA,
        source_archive=artifacts["source_archive"],
        evidence_archive=artifacts["evidence_archive"],
        created_at_utc=collected_at,
    )
    _write_json(artifacts["release_manifest"], manifest)
    write_sha256sums(
        [
            artifacts["source_archive"],
            artifacts["evidence_archive"],
            artifacts["release_manifest"],
        ],
        artifacts["checksums"],
    )


def test_source_archive_contract_accepts_only_complete_clean_source(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "PA_Agent-v0.1.0-source.zip"
    _valid_archive(archive_path)

    result = validate_source_archive(
        archive_path,
        expected_version="0.1.0",
        expected_sha=_FULL_SHA,
    )

    assert result["prompt_resources"] == 37
    assert result["entrypoints"] == ["pa-agent", "pa-execution-worker"]
    assert result["forbidden_entries"] == []
    assert result["git_sha"] == _FULL_SHA


def test_archive_source_is_byte_identical_across_host_timezones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", "UTC")
    utc_archive = release_pipeline.archive_source(
        repo_root=_ROOT,
        output_dir=tmp_path / "utc",
        ref="HEAD",
        version="0.1.0",
    )
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    shanghai_archive = release_pipeline.archive_source(
        repo_root=_ROOT,
        output_dir=tmp_path / "shanghai",
        ref="HEAD",
        version="0.1.0",
    )

    assert (
        hashlib.sha256(utc_archive.read_bytes()).hexdigest()
        == hashlib.sha256(shanghai_archive.read_bytes()).hexdigest()
    )


def test_repository_archive_excludes_only_root_runtime_directories(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "PA_Agent-v0.1.0-source.zip"
    sha_result = subprocess.run(
        ["git", "-C", str(_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert sha_result.returncode == 0, sha_result.stderr
    git_sha = sha_result.stdout.strip()
    archive_result = subprocess.run(
        [
            "git",
            "-C",
            str(_ROOT),
            "archive",
            "--worktree-attributes",
            "--format=zip",
            "--prefix=PA_Agent-v0.1.0/",
            "--output",
            str(archive_path),
            "HEAD",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert archive_result.returncode == 0, archive_result.stderr

    with zipfile.ZipFile(archive_path) as archive:
        names = {
            info.filename
            for info in archive.infolist()
            if not info.is_dir()
        }
    assert not any(
        name.startswith(
            (
                "PA_Agent-v0.1.0/experience/",
                "PA_Agent-v0.1.0/logs/",
                "PA_Agent-v0.1.0/records/",
                "PA_Agent-v0.1.0/trade_records/",
            )
        )
        for name in names
    )
    assert {
        f"PA_Agent-v0.1.0/{relative}"
        for relative in _RECORD_SOURCE_PATHS
    } <= names
    result = validate_source_archive(
        archive_path,
        expected_version="0.1.0",
        expected_sha=git_sha,
    )
    assert result["forbidden_entries"] == []


def test_source_archive_rejects_missing_runtime_entrypoint_module(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "missing-entrypoint.zip"
    _valid_archive(
        archive_path,
        omit={"PA_Agent-v0.1.0/pa_agent/execution/worker_cli.py"},
    )

    with pytest.raises(ReleaseValidationError, match=r"worker_cli\.py"):
        validate_source_archive(
            archive_path,
            expected_version="0.1.0",
            expected_sha=_FULL_SHA,
        )


def test_source_archive_rejects_substituted_prompt_resource(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "wrong-prompt.zip"
    removed = sorted(EXPECTED_PROMPT_RESOURCE_PATHS)[0]
    _valid_archive(
        archive_path,
        omit={f"PA_Agent-v0.1.0/prompt_engineering/{removed}"},
        extra={"PA_Agent-v0.1.0/prompt_engineering/replacement.txt": b"ok\n"},
    )

    with pytest.raises(ReleaseValidationError, match="路径集合"):
        validate_source_archive(
            archive_path,
            expected_version="0.1.0",
            expected_sha=_FULL_SHA,
        )


@pytest.mark.parametrize(
    "entry",
    [
        "../outside.txt",
        "PA_Agent-v0.1.0/.env",
        "PA_Agent-v0.1.0/config/settings.json",
        "PA_Agent-v0.1.0/records/execution.sqlite3",
        "PA_Agent-v0.1.0/logs/pa_agent.log",
        "PA_Agent-v0.1.0/.venv/Scripts/python.exe",
        "PA_Agent-v0.1.0/dist/pa_agent-0.1.0-py3-none-any.whl",
    ],
)
def test_source_archive_rejects_runtime_secret_and_binary_entries(
    tmp_path: Path,
    entry: str,
) -> None:
    archive_path = tmp_path / "unsafe.zip"
    _valid_archive(archive_path, extra={entry: b"not allowed"})

    with pytest.raises(ReleaseValidationError):
        validate_source_archive(
            archive_path,
            expected_version="0.1.0",
            expected_sha=_FULL_SHA,
        )


def test_source_archive_rejects_runtime_gitkeep_placeholders(
    tmp_path: Path,
) -> None:
    for runtime_dir in ("experience", "logs", "records", "trade_records"):
        archive_path = tmp_path / f"unsafe-{runtime_dir}.zip"
        _valid_archive(
            archive_path,
            extra={
                f"PA_Agent-v0.1.0/{runtime_dir}/.gitkeep": b"",
            },
        )
        with pytest.raises(ReleaseValidationError, match="runtime_data"):
            validate_source_archive(
                archive_path,
                expected_version="0.1.0",
                expected_sha=_FULL_SHA,
            )


@pytest.mark.parametrize(
    "content",
    [
        b"-----BEGIN " + b"PRIVATE KEY-----\nnot-a-real-key\n",
        b"token=" + b"ghp_" + b"1234567890abcdefghijklmn\n",
    ],
    ids=["private-key", "known-token"],
)
def test_source_archive_rejects_secret_content(
    tmp_path: Path,
    content: bytes,
) -> None:
    archive_path = tmp_path / "unsafe-content.zip"
    _valid_archive(
        archive_path,
        extra={"PA_Agent-v0.1.0/notes.txt": content},
    )

    with pytest.raises(ReleaseValidationError):
        validate_source_archive(
            archive_path,
            expected_version="0.1.0",
            expected_sha=_FULL_SHA,
        )


def test_release_evidence_tree_rejects_private_runner_path(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "pip-freeze.txt").write_text(
        "pa-agent @ file:///C:/Users/runner/work/PA_Agent\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match="本机绝对路径"):
        scan_release_tree(
            evidence,
            reject_private_paths=True,
        )


def test_release_evidence_tree_accepts_pinned_https_vcs_dependency(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "pip-freeze.txt").write_text(
        "tvdatafeed @ git+https://github.com/rongardF/tvdatafeed.git"
        "@e6f6aaa7de439ac6e454d9b26d2760ded8dc4923\n",
        encoding="utf-8",
    )

    result = scan_release_tree(evidence, reject_private_paths=True)

    assert result["files_scanned"] == 1
    assert result["text_files_scanned"] == 1


def test_sanitize_junit_report_redacts_private_skip_path_and_preserves_counts(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites tests="1" failures="0" errors="0" skipped="1">
  <testsuite name="pytest" tests="1" failures="0" errors="0" skipped="1">
    <testcase classname="tests.unit.test_datetime_ts"
              name="test_naive_local_to_utc_uses_host_offset">
      <skipped message="host is UTC">D:\\a\\PA_Agent\\tests\\unit\\test_datetime_ts.py:23: host is UTC</skipped>
    </testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )
    with pytest.raises(ReleaseValidationError, match="本机绝对路径"):
        scan_release_tree(tmp_path, reject_private_paths=True)

    result = release_pipeline.sanitize_junit_report(report)

    root = ET.parse(report).getroot()
    assert root.attrib == {
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    }
    suite = root.find("testsuite")
    assert suite is not None
    assert suite.attrib == {
        "name": "pytest",
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    }
    case = suite.find("testcase")
    assert case is not None
    assert case.attrib == {
        "classname": "tests.unit.test_datetime_ts",
        "name": "test_naive_local_to_utc_uses_host_offset",
    }
    skipped = case.find("skipped")
    assert skipped is not None
    assert skipped.attrib["message"] == "host is UTC"
    assert skipped.text == "[REDACTED_PRIVATE_PATH]"
    assert result == {"paths_redacted": 1, "result": "pass"}
    scan_release_tree(tmp_path, reject_private_paths=True)


@pytest.mark.parametrize(
    "name",
    ["report.xml", "check.cmd", "verify.sh", "NOTICE"],
)
def test_release_tree_scans_text_extensions_and_extensionless_files(
    tmp_path: Path,
    name: str,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    secret = "ghp_" + "1234567890abcdefghijklmn"
    (evidence / name).write_text(secret, encoding="utf-8")

    with pytest.raises(ReleaseValidationError, match="已知密钥格式"):
        scan_release_tree(evidence, reject_private_paths=True)


def test_release_evidence_tree_rejects_any_windows_absolute_path(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    private_path = "D:" + "\\runner\\work\\PA_Agent\\artifact.xml"
    (evidence / "report.xml").write_text(private_path, encoding="utf-8")

    with pytest.raises(ReleaseValidationError, match="本机绝对路径"):
        scan_release_tree(evidence, reject_private_paths=True)


def test_release_manifest_and_checksums_bind_every_artifact(
    tmp_path: Path,
) -> None:
    source = tmp_path / "PA_Agent-v0.1.0-source.zip"
    source.write_bytes(b"source")
    evidence = tmp_path / "PA_Agent-v0.1.0-evidence.zip"
    evidence.write_bytes(b"evidence")
    manifest_path = tmp_path / "release-manifest.json"

    manifest = build_release_manifest(
        version="0.1.0",
        git_sha=_FULL_SHA,
        source_archive=source,
        evidence_archive=evidence,
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    sums_path = tmp_path / "SHA256SUMS"
    write_sha256sums([source, evidence, manifest_path], sums_path)

    assert manifest["delivery"] == "windows-python-3.12-source"
    assert manifest["artifacts"]["source"]["sha256"] == hashlib.sha256(
        b"source"
    ).hexdigest()
    lines = sums_path.read_text(encoding="utf-8").splitlines()
    assert [PurePosixPath(line.split("  ", 1)[1]).name for line in lines] == [
        source.name,
        evidence.name,
        manifest_path.name,
    ]


def test_stable_capability_gate_requires_external_post_commit_bundle(
    tmp_path: Path,
) -> None:
    _bundle, index_path, _index, _artifacts = _stable_bundle(tmp_path)

    with pytest.raises(ReleaseValidationError, match="外部证据目录"):
        validate_capability_index(
            index_path,
            stable=True,
            expected_sha=_FULL_SHA,
            expected_version="0.1.0",
            repo_root=tmp_path,
            schema_root=_SCHEMA_ROOT,
        )


def test_stable_capability_gate_accepts_full_hashed_external_evidence(
    tmp_path: Path,
) -> None:
    bundle, index_path, _index, artifacts = _stable_bundle(tmp_path)

    result = _validate_stable(
        tmp_path=tmp_path,
        bundle=bundle,
        index_path=index_path,
        artifacts=artifacts,
    )

    assert result["capability_count"] == 6
    assert result["stable_release_ready"] is True
    assert {
        "artifacts",
        "content_scan",
        "desktop",
        "market",
        "release",
        "runtime",
    }.issubset(result["verified_evidence"])


def test_stable_capability_gate_is_durably_recheckable_but_publish_is_fresh(
    tmp_path: Path,
) -> None:
    old = datetime.now(UTC) - timedelta(days=2)
    bundle, index_path, _index, artifacts = _stable_bundle(
        tmp_path,
        collected_now=old,
    )

    durable = _validate_stable(
        tmp_path=tmp_path,
        bundle=bundle,
        index_path=index_path,
        artifacts=artifacts,
    )
    assert durable["stable_release_ready"] is True

    with pytest.raises(ReleaseValidationError, match="fresh_at_publish_time"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
            require_fresh_now=True,
        )


def test_stable_capability_gate_rejects_short_all_green_index(
    tmp_path: Path,
) -> None:
    bundle, index_path, index, artifacts = _stable_bundle(tmp_path)
    malicious = deepcopy(index)
    malicious["capabilities"] = malicious["capabilities"][:1]
    _write_json(index_path, malicious)

    with pytest.raises(ReleaseValidationError, match="固定六项能力"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_stable_capability_gate_rejects_target_sha_mismatch(
    tmp_path: Path,
) -> None:
    bundle, index_path, index, artifacts = _stable_bundle(tmp_path)
    index["as_of_git_sha"] = "b" * 40
    _write_json(index_path, index)

    with pytest.raises(ReleaseValidationError, match="目标 Git SHA"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_candidate_capability_gate_rejects_target_sha_mismatch() -> None:
    with pytest.raises(ReleaseValidationError, match="目标 Git SHA"):
        validate_capability_index(
            _ROOT / "docs" / "evidence" / "capability-index.json",
            stable=False,
            expected_sha="b" * 40,
            expected_version="0.1.0",
            repo_root=_ROOT,
        )


def _write_candidate_release_evidence(
    evidence_root: Path,
    *,
    sha: str = _FULL_SHA,
    tests: int = 2295,
    skipped: int = 1,
) -> None:
    fresh_install = evidence_root / "fresh-install"
    fresh_install.mkdir(parents=True)
    (evidence_root / "full-git-sha.txt").write_text(
        sha + "\n",
        encoding="utf-8",
    )
    (evidence_root / "pytest-deterministic-junit.xml").write_text(
        f'<testsuites><testsuite tests="{tests}" failures="0" '
        f'errors="0" skipped="{skipped}"/></testsuites>\n',
        encoding="utf-8",
    )
    self_check = {
        "checks": {
            "default_trading": True,
            "entrypoints": True,
            "pinned_vcs_dependency": True,
            "prompt_resources": True,
            "python_3_12": True,
            "python_contract": True,
            "required_source_files": True,
            "version_truth": True,
            "windows": True,
        },
        "default_trading": {
            "auto_execute": False,
            "execution_enabled": False,
            "longbridge_trading": False,
            "new_risk_routes": ["okx:demo"],
            "okx_live": False,
        },
        "delivery": "windows-python-3.12-source",
        "entrypoints": ["pa-agent", "pa-execution-worker"],
        "failed_checks": [],
        "git_sha": sha,
        "platform": "Windows",
        "prompt_resources": 37,
        "python": "3.12.10",
        "status": "pass",
        "version": "0.1.0",
    }
    for name in (
        "pa-agent-self-check.json",
        "worker-self-check.json",
    ):
        (fresh_install / name).write_text(
            json.dumps(self_check) + "\n",
            encoding="utf-8",
        )


def test_candidate_index_snapshot_binds_sha_and_release_evidence(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    _write_candidate_release_evidence(evidence_root)
    output = evidence_root / "capability-index.json"

    result = release_pipeline.build_candidate_capability_index(
        _ROOT / "docs" / "evidence" / "capability-index.json",
        output,
        evidence_root=evidence_root,
        schema_root=_SCHEMA_ROOT,
        expected_sha=_FULL_SHA,
        expected_version="0.1.0",
        repo_root=_ROOT,
        collected_at=datetime(2026, 7, 30, 6, 0, tzinfo=UTC),
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    release = next(
        item
        for item in payload["capabilities"]
        if item["capability_id"] == "release-source-deployment"
    )
    assert result["stable_release_ready"] is False
    assert payload["as_of_git_sha"] == _FULL_SHA
    assert payload["collected_at"] == "2026-07-30T06:00:00+00:00"
    assert release["git_sha"] == _FULL_SHA
    assert release["layers"] == {
        "code": "verified",
        "tests": "verified",
        "external": "not_applicable",
        "gui": "not_applicable",
        "runtime": "verified",
    }
    assert payload["blockers"] == [
        "multi-market-workspace.gui",
        "worker-v5-runtime.external",
        "worker-v5-runtime.runtime",
    ]
    assert {
        entry["path"]
        for entry in release["evidence"]
    } == {
        "fresh-install/pa-agent-self-check.json",
        "fresh-install/worker-self-check.json",
        "full-git-sha.txt",
        "pytest-deterministic-junit.xml",
    }
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        for entry in release["evidence"]
    )


def test_candidate_index_snapshot_rejects_sha_file_mismatch(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    _write_candidate_release_evidence(evidence_root, sha="b" * 40)

    with pytest.raises(ReleaseValidationError, match="完整 SHA"):
        release_pipeline.build_candidate_capability_index(
            _ROOT / "docs" / "evidence" / "capability-index.json",
            evidence_root / "capability-index.json",
            evidence_root=evidence_root,
            schema_root=_SCHEMA_ROOT,
            expected_sha=_FULL_SHA,
            expected_version="0.1.0",
            repo_root=_ROOT,
        )


def test_candidate_index_snapshot_rejects_weak_junit_gate(
    tmp_path: Path,
) -> None:
    for name, tests, skipped in (
        ("too-few", 1, 0),
        ("too-many-skips", 2295, 4),
    ):
        evidence_root = tmp_path / name
        _write_candidate_release_evidence(
            evidence_root,
            tests=tests,
            skipped=skipped,
        )
        with pytest.raises(ReleaseValidationError, match="JUnit"):
            release_pipeline.build_candidate_capability_index(
                _ROOT / "docs" / "evidence" / "capability-index.json",
                evidence_root / "capability-index.json",
                evidence_root=evidence_root,
                schema_root=_SCHEMA_ROOT,
                expected_sha=_FULL_SHA,
                expected_version="0.1.0",
                repo_root=_ROOT,
            )


def test_candidate_index_snapshot_rejects_invalid_junit_structure(
    tmp_path: Path,
) -> None:
    invalid_reports = (
        '<not-junit><testsuite tests="2295" failures="0" '
        'errors="0" skipped="0"/></not-junit>\n',
        '<testsuites><testsuite tests="2295" failures="0" '
        'errors="0" skipped="-1"/></testsuites>\n',
    )
    for index, report in enumerate(invalid_reports):
        evidence_root = tmp_path / f"case-{index}"
        _write_candidate_release_evidence(evidence_root, tests=2295)
        (
            evidence_root / "pytest-deterministic-junit.xml"
        ).write_text(report, encoding="utf-8")
        with pytest.raises(ReleaseValidationError, match="JUnit"):
            release_pipeline.build_candidate_capability_index(
                _ROOT / "docs" / "evidence" / "capability-index.json",
                evidence_root / "capability-index.json",
                evidence_root=evidence_root,
                schema_root=_SCHEMA_ROOT,
                expected_sha=_FULL_SHA,
                expected_version="0.1.0",
                repo_root=_ROOT,
            )


def test_candidate_index_snapshot_rejects_incomplete_self_check(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    _write_candidate_release_evidence(evidence_root)
    incomplete = {
        "status": "pass",
        "git_sha": _FULL_SHA,
        "version": "0.1.0",
    }
    (
        evidence_root
        / "fresh-install"
        / "worker-self-check.json"
    ).write_text(
        json.dumps(incomplete) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match="全新安装"):
        release_pipeline.build_candidate_capability_index(
            _ROOT / "docs" / "evidence" / "capability-index.json",
            evidence_root / "capability-index.json",
            evidence_root=evidence_root,
            schema_root=_SCHEMA_ROOT,
            expected_sha=_FULL_SHA,
            expected_version="0.1.0",
            repo_root=_ROOT,
        )


def test_candidate_archive_rechecks_final_hashed_evidence(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    _write_candidate_release_evidence(evidence_root)
    _desktop_evidence(evidence_root / "desktop")
    index_path = evidence_root / "capability-index.json"
    release_pipeline.build_candidate_capability_index(
        _ROOT / "docs" / "evidence" / "capability-index.json",
        index_path,
        evidence_root=evidence_root,
        schema_root=_SCHEMA_ROOT,
        expected_sha=_FULL_SHA,
        expected_version="0.1.0",
        repo_root=_ROOT,
    )

    def write_archive(path: Path) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            for file_path in sorted(evidence_root.rglob("*")):
                if file_path.is_file():
                    archive.write(
                        file_path,
                        file_path.relative_to(evidence_root).as_posix(),
                    )

    valid_archive = tmp_path / "valid-evidence.zip"
    write_archive(valid_archive)
    result = release_pipeline.validate_candidate_evidence_archive(
        valid_archive,
        capability_index=index_path,
        evidence_root=evidence_root,
        schema_root=_SCHEMA_ROOT,
        expected_sha=_FULL_SHA,
        expected_version="0.1.0",
        repo_root=_ROOT,
    )
    assert result["result"] == "pass"
    assert result["desktop_evidence_count"] == 16

    desktop_metadata_path = (
        evidence_root / "desktop" / "1440x900-calendar_unknown.json"
    )
    desktop_metadata = json.loads(
        desktop_metadata_path.read_text(encoding="utf-8")
    )
    invalid_desktop_metadata = dict(desktop_metadata)
    invalid_desktop_metadata["capture_contract"] = "backing-store-grab-v0"
    desktop_metadata_path.write_text(
        json.dumps(invalid_desktop_metadata) + "\n",
        encoding="utf-8",
    )
    invalid_desktop_archive = tmp_path / "invalid-desktop-evidence.zip"
    write_archive(invalid_desktop_archive)
    with pytest.raises(
        ReleaseValidationError,
        match="capture_contract",
    ):
        release_pipeline.validate_candidate_evidence_archive(
            invalid_desktop_archive,
            capability_index=index_path,
            evidence_root=evidence_root,
            schema_root=_SCHEMA_ROOT,
            expected_sha=_FULL_SHA,
            expected_version="0.1.0",
            repo_root=_ROOT,
        )
    desktop_metadata_path.write_text(
        json.dumps(desktop_metadata) + "\n",
        encoding="utf-8",
    )

    (evidence_root / "pytest-deterministic-junit.xml").write_text(
        '<testsuites><testsuite tests="1" failures="0" '
        'errors="0" skipped="0"/></testsuites>\n',
        encoding="utf-8",
    )
    mutated_archive = tmp_path / "mutated-evidence.zip"
    write_archive(mutated_archive)
    with pytest.raises(ReleaseValidationError, match=r"哈希|JUnit"):
        release_pipeline.validate_candidate_evidence_archive(
            mutated_archive,
            capability_index=index_path,
            evidence_root=evidence_root,
            schema_root=_SCHEMA_ROOT,
            expected_sha=_FULL_SHA,
            expected_version="0.1.0",
            repo_root=_ROOT,
        )


def test_candidate_archive_requires_one_internal_index(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    _write_candidate_release_evidence(evidence_root, tests=2295)
    index_path = evidence_root / "capability-index.json"
    release_pipeline.build_candidate_capability_index(
        _ROOT / "docs" / "evidence" / "capability-index.json",
        index_path,
        evidence_root=evidence_root,
        schema_root=_SCHEMA_ROOT,
        expected_sha=_FULL_SHA,
        expected_version="0.1.0",
        repo_root=_ROOT,
    )
    archive_path = tmp_path / "evidence.zip"

    def write_archive() -> None:
        with zipfile.ZipFile(archive_path, "w") as archive:
            for file_path in sorted(evidence_root.rglob("*")):
                if file_path.is_file():
                    archive.write(
                        file_path,
                        file_path.relative_to(evidence_root).as_posix(),
                    )

    write_archive()
    outside_index = tmp_path / "outside-index.json"
    outside_index.write_bytes(index_path.read_bytes())
    with pytest.raises(ReleaseValidationError, match="证据目录"):
        release_pipeline.validate_candidate_evidence_archive(
            archive_path,
            capability_index=outside_index,
            evidence_root=evidence_root,
            schema_root=_SCHEMA_ROOT,
            expected_sha=_FULL_SHA,
            expected_version="0.1.0",
            repo_root=_ROOT,
        )

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    release = next(
        item
        for item in payload["capabilities"]
        if item["capability_id"] == "release-source-deployment"
    )
    release["evidence"].append(dict(release["evidence"][0]))
    index_path.write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )
    write_archive()
    with pytest.raises(ReleaseValidationError, match="完整绑定"):
        release_pipeline.validate_candidate_evidence_archive(
            archive_path,
            capability_index=index_path,
            evidence_root=evidence_root,
            schema_root=_SCHEMA_ROOT,
            expected_sha=_FULL_SHA,
            expected_version="0.1.0",
            repo_root=_ROOT,
        )


def test_stable_capability_gate_rejects_incomplete_runtime_cycle(
    tmp_path: Path,
) -> None:
    bundle, index_path, index, artifacts = _stable_bundle(tmp_path)
    runtime_path = bundle / "runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["controlled_reproducible"]["closed"] = False
    _write_json(runtime_path, runtime)
    _refresh_evidence_hash(
        index,
        capability_id="worker-v5-runtime",
        path="runtime.json",
        bundle=bundle,
    )
    _write_json(index_path, index)

    with pytest.raises(ReleaseValidationError, match="cycle_closed"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_stable_capability_gate_rejects_desktop_without_crypto(
    tmp_path: Path,
) -> None:
    bundle, index_path, index, artifacts = _stable_bundle(tmp_path)
    desktop_path = bundle / "desktop.json"
    desktop = json.loads(desktop_path.read_text(encoding="utf-8"))
    desktop["symbols_checked"].remove("XAU-USDT-SWAP")
    _write_json(desktop_path, desktop)
    _refresh_evidence_hash(
        index,
        capability_id="multi-market-workspace",
        path="desktop.json",
        bundle=bundle,
    )
    _write_json(index_path, index)

    with pytest.raises(ReleaseValidationError, match="crypto_symbol"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_stable_capability_gate_rejects_evidence_hash_mismatch(
    tmp_path: Path,
) -> None:
    bundle, index_path, index, artifacts = _stable_bundle(tmp_path)
    index["capabilities"][0]["evidence"][0]["sha256"] = "0" * 64
    _write_json(index_path, index)

    with pytest.raises(ReleaseValidationError, match="哈希不匹配"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


@pytest.mark.parametrize(
    ("field", "value", "expected_failure"),
    [
        ("source", "okx", "source"),
        ("market", "Crypto", "symbol_market"),
        ("permission_derivation", None, "permission_derivation"),
    ],
)
def test_stable_market_gate_rejects_false_contract_claims(
    tmp_path: Path,
    field: str,
    value: object,
    expected_failure: str,
) -> None:
    bundle, index_path, index, artifacts = _stable_bundle(tmp_path)
    market_path = bundle / "market-us.json"
    market = json.loads(market_path.read_text(encoding="utf-8"))
    market[field] = value
    _write_json(market_path, market)
    _refresh_evidence_hash(
        index,
        capability_id="longbridge-readonly-contract",
        path="market-us.json",
        bundle=bundle,
    )
    _write_json(index_path, index)

    with pytest.raises(ReleaseValidationError, match=expected_failure):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_stable_market_gate_rejects_impossible_bar_timeline(
    tmp_path: Path,
) -> None:
    bundle, index_path, index, artifacts = _stable_bundle(tmp_path)
    market_path = bundle / "market-us.json"
    market = json.loads(market_path.read_text(encoding="utf-8"))
    market["timeframes"][0]["first_utc_ms"] = (
        market["analysis_as_of_utc_ms"] + 1
    )
    _write_json(market_path, market)
    _refresh_evidence_hash(
        index,
        capability_id="longbridge-readonly-contract",
        path="market-us.json",
        bundle=bundle,
    )
    _write_json(index_path, index)

    with pytest.raises(ReleaseValidationError, match="time_order"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_stable_gate_rejects_private_path_anywhere_in_bundle(
    tmp_path: Path,
) -> None:
    bundle, index_path, index, artifacts = _stable_bundle(tmp_path)
    generic = bundle / "generic.txt"
    generic.write_text(
        "D:" + "\\private\\runner\\account-report.xml\n",
        encoding="utf-8",
    )
    for capability_id in (
        "new-risk-one-shot-token",
        "market-workspace-controller",
    ):
        _refresh_evidence_hash(
            index,
            capability_id=capability_id,
            path="generic.txt",
            bundle=bundle,
        )
    _write_json(index_path, index)

    with pytest.raises(ReleaseValidationError, match="本机绝对路径"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_stable_gate_binds_release_acceptance_to_actual_source_zip(
    tmp_path: Path,
) -> None:
    bundle, index_path, index, artifacts = _stable_bundle(tmp_path)
    release_path = bundle / "release.json"
    release = json.loads(release_path.read_text(encoding="utf-8"))
    release["source_archive_sha256"] = "d" * 64
    _write_json(release_path, release)
    _refresh_evidence_hash(
        index,
        capability_id="release-source-deployment",
        path="release.json",
        bundle=bundle,
    )
    _write_json(index_path, index)

    with pytest.raises(ReleaseValidationError, match="archive_sha"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_stable_gate_rejects_manifest_not_bound_to_target_sha(
    tmp_path: Path,
) -> None:
    bundle, index_path, _index, artifacts = _stable_bundle(tmp_path)
    manifest = json.loads(
        artifacts["release_manifest"].read_text(encoding="utf-8")
    )
    manifest["git_sha"] = "d" * 40
    _write_json(artifacts["release_manifest"], manifest)

    with pytest.raises(ReleaseValidationError, match="未绑定真实产物"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_stable_gate_rejects_sensitive_extra_manifest_field_even_if_rehashed(
    tmp_path: Path,
) -> None:
    bundle, index_path, _index, artifacts = _stable_bundle(tmp_path)
    manifest = json.loads(
        artifacts["release_manifest"].read_text(encoding="utf-8")
    )
    manifest["api_key"] = "sk-" + ("a" * 48)
    _write_json(artifacts["release_manifest"], manifest)
    write_sha256sums(
        [
            artifacts["source_archive"],
            artifacts["evidence_archive"],
            artifacts["release_manifest"],
        ],
        artifacts["checksums"],
    )

    with pytest.raises(
        ReleaseValidationError,
        match=r"release-manifest|密钥",
    ):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_stable_gate_rejects_unlisted_extra_evidence_file(
    tmp_path: Path,
) -> None:
    bundle, index_path, index, artifacts = _stable_bundle(tmp_path)
    with zipfile.ZipFile(
        artifacts["evidence_archive"],
        "a",
        zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("raw-market.json", b'{"price": 1}\n')
    _refresh_artifact_metadata(
        artifacts,
        collected_at=index["collected_at"],
    )

    with pytest.raises(ReleaseValidationError, match="只能包含"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_stable_gate_rejects_incorrect_sha256sums(
    tmp_path: Path,
) -> None:
    bundle, index_path, _index, artifacts = _stable_bundle(tmp_path)
    artifacts["checksums"].write_text(
        f"{'0' * 64}  {artifacts['source_archive'].name}\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match="SHA256SUMS"):
        _validate_stable(
            tmp_path=tmp_path,
            bundle=bundle,
            index_path=index_path,
            artifacts=artifacts,
        )


def test_desktop_evidence_requires_decodable_exact_matrix(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "desktop"
    _desktop_evidence(evidence)

    result = validate_desktop_evidence(
        evidence,
        expected_sha=_FULL_SHA,
    )

    assert result["evidence_count"] == 16
    assert result["fixture_only"] is True
    assert result["runtime_reads"] == 0


def test_desktop_evidence_rejects_trading_control_and_wrong_build(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "desktop"
    _desktop_evidence(evidence)
    metadata_path = evidence / "1440x900-normal.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["git_sha"] = "b" * 40
    metadata["button_texts"].append("买入")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(
        ReleaseValidationError,
        match=r"git_sha|no_trading_buttons",
    ):
        validate_desktop_evidence(
            evidence,
            expected_sha=_FULL_SHA,
        )


def test_desktop_evidence_rejects_missing_cjk_font_coverage(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "desktop"
    _desktop_evidence(evidence)
    metadata_path = evidence / "1440x900-normal.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["font"]["family"] = "Segoe UI"
    metadata["font"]["cjk_sample_supported"] = False
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(
        ReleaseValidationError,
        match=r"cjk_font_family|cjk_sample_supported",
    ):
        validate_desktop_evidence(
            evidence,
            expected_sha=_FULL_SHA,
        )


def test_desktop_evidence_rejects_missing_required_ui_glyph_coverage(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "desktop"
    _desktop_evidence(evidence)
    metadata_path = evidence / "1440x900-normal.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["font"]["required_ui_glyphs_supported"] = False
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(
        ReleaseValidationError,
        match=r"required_ui_glyphs_supported",
    ):
        validate_desktop_evidence(
            evidence,
            expected_sha=_FULL_SHA,
        )


def test_desktop_evidence_rejects_asynchronous_capture_contract(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "desktop"
    _desktop_evidence(evidence)
    metadata_path = evidence / "1440x900-calendar_unknown.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["capture_contract"] = "backing-store-grab-v0"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(
        ReleaseValidationError,
        match=r"capture_contract",
    ):
        validate_desktop_evidence(
            evidence,
            expected_sha=_FULL_SHA,
        )


def test_desktop_evidence_rejects_unbound_png_hash(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "desktop"
    _desktop_evidence(evidence)
    metadata_path = evidence / "1440x900-calendar_unknown.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["image_sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(
        ReleaseValidationError,
        match=r"image_sha256",
    ):
        validate_desktop_evidence(
            evidence,
            expected_sha=_FULL_SHA,
        )


def test_synchronous_widget_render_uses_current_paint_state() -> None:
    from PyQt6.QtGui import QColor, QPainter
    from PyQt6.QtWidgets import QApplication, QWidget

    class StateWidget(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.colour = QColor("#d7263d")

        def paintEvent(self, _event: object) -> None:
            painter = QPainter(self)
            painter.fillRect(self.rect(), self.colour)
            painter.end()

    app = QApplication.instance() or QApplication([])
    widget = StateWidget()
    widget.resize(32, 32)
    widget.show()
    app.processEvents()
    widget.colour = QColor("#1b998b")

    image = release_pipeline.render_widget_synchronously(widget)

    assert image.pixelColor(
        image.width() // 2,
        image.height() // 2,
    ) == QColor("#1b998b")
    widget.close()
