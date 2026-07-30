from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from jsonschema import Draft202012Validator

from pa_agent.release_pipeline import _desktop_evidence_contract

_ROOT = Path(__file__).resolve().parents[2]


def test_release_assets_and_windows_scripts_are_present_and_guarded() -> None:
    install_path = _ROOT / "scripts" / "install_windows.ps1"
    uninstall_path = _ROOT / "scripts" / "uninstall_windows.ps1"
    attributes_path = _ROOT / ".gitattributes"
    required = [
        attributes_path,
        _ROOT / "CHANGELOG.md",
        _ROOT / "docs" / "RELEASE_CHECKLIST.md",
        _ROOT / "docs" / "SOURCE_INSTALL_WINDOWS.md",
        _ROOT / "docs" / "evidence" / "capability-index.json",
        _ROOT / "docs" / "evidence" / "schemas" / "capability-index.schema.json",
        _ROOT / "docs" / "evidence" / "schemas" / "runtime-snapshot.schema.json",
        _ROOT / "docs" / "evidence" / "schemas" / "market-acceptance.schema.json",
        _ROOT / "docs" / "evidence" / "schemas" / "desktop-acceptance.schema.json",
        _ROOT / "docs" / "evidence" / "schemas" / "release-acceptance.schema.json",
        install_path,
        uninstall_path,
        _ROOT / "scripts" / "release_pipeline.py",
        _ROOT / "scripts" / "publish_release.ps1",
        _ROOT / ".github" / "workflows" / "release.yml",
    ]
    assert [str(path.relative_to(_ROOT)) for path in required if not path.is_file()] == []

    release_attributes = set(
        attributes_path.read_text(encoding="utf-8").splitlines()
    )
    install_text = install_path.read_text(encoding="utf-8")
    uninstall_text = uninstall_path.read_text(encoding="utf-8")
    release_workflow_text = (
        _ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    ci_workflow_text = (
        _ROOT / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    publish_text = (
        _ROOT / "scripts" / "publish_release.ps1"
    ).read_text(encoding="utf-8")
    assert "3.12" in install_text
    assert "Git for Windows" in install_text
    assert "PA_Agent.lnk" in install_text
    assert "ConfirmUninstall" in uninstall_text
    assert "Remove-Item" in uninstall_text
    assert "D:\\Desktop" not in install_text
    assert {
        "/experience export-ignore",
        "/logs export-ignore",
        "/records export-ignore",
        "/trade_records export-ignore",
    } <= release_attributes
    assert '$tests -lt 2245' in ci_workflow_text
    assert '$tests -lt 2245' in release_workflow_text
    assert ci_workflow_text.count("sanitize-junit") == 2
    assert "scan-tree scratch/ci-evidence" in ci_workflow_text
    assert "id: evidence_guard" in ci_workflow_text
    assert "id: evidence_files" in ci_workflow_text
    assert (
        "steps.evidence_guard.outcome == 'success'"
        in ci_workflow_text
    )
    assert (
        "steps.evidence_files.outcome == 'success'"
        in ci_workflow_text
    )
    assert "sanitize-junit" in release_workflow_text
    assert "scratch/release-junit.xml" in release_workflow_text
    assert "prepare-candidate-index" in release_workflow_text
    assert "--output capability-index.json" in release_workflow_text
    assert (
        'validate-index `\n'
        "            --path capability-index.json `\n"
        "            --sha $env:GITHUB_SHA"
        in release_workflow_text
    )
    compress_position = release_workflow_text.index(
        "          Compress-Archive -Path"
    )
    final_candidate_position = release_workflow_text.index(
        "          python scripts/release_pipeline.py "
        "validate-candidate-archive"
    )
    manifest_position = release_workflow_text.index(
        "          python scripts/release_pipeline.py manifest"
    )
    assert (
        compress_position
        < final_candidate_position
        < manifest_position
    )
    assert (
        "notofonts/noto-cjk/"
        "523d033d6cb47f4a80c58a35753646f5c3608a78/"
        in release_workflow_text
    )
    assert (
        "2c76254f6fc379fddfce0a7e84fb5385"
        "bb135d3e399294f6eeb6680d0365b74b"
        in release_workflow_text
    )
    assert "$expectedBytes = 16437364" in release_workflow_text
    assert "PA_AGENT_VISUAL_FONT_PATH=" in release_workflow_text
    visual_script_text = (
        _ROOT / "tests" / "visual" / "generate_market_workspace_screenshot.py"
    ).read_text(encoding="utf-8")
    assert "_CJK_SAMPLE + _REQUIRED_UI_GLYPHS" in visual_script_text
    assert '"required_ui_glyphs_supported": True' in visual_script_text
    assert (
        '$venvPython = Join-Path $sourceRoot.FullName '
        '".venv\\Scripts\\python.exe"'
    ) in release_workflow_text
    assert "-Confirm:$false" in release_workflow_text
    assert '$Repository = "xiaojinlucky/PA_Agent"' in publish_text
    assert 'Get-GreenWorkflowRun "ci.yml"' in publish_text
    assert 'Get-GreenWorkflowRun "release.yml"' in publish_text
    assert "--require-fresh-now" in publish_text
    assert "--draft" in publish_text
    assert "--draft=false" in publish_text
    assert "downloaded asset hash mismatch" in publish_text
    assert "origin/main moved before draft publication" in publish_text
    assert "upload-scan" in publish_text
    assert "scan-tree $uploadScanRoot --reject-private-paths" in publish_text
    assert publish_text.count("Confirm-ReleaseAssets") >= 3
    assert "Published Release asset verification failed" in publish_text


def test_release_workflow_uses_desktop_validator_scale_names(
    tmp_path: Path,
) -> None:
    release_workflow_text = (
        _ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    start = release_workflow_text.index("          $scaleCases = @(")
    end = release_workflow_text.index("          $pngCount =", start)
    workflow_block = textwrap.dedent(release_workflow_text[start:end])
    capture = tmp_path / "capture.cmd"
    capture.write_text("@echo off\r\necho %*\r\n", encoding="ascii")
    capture_path = str(capture).replace("'", "''")
    command = (
        f"$venvPython = '{capture_path}'\n"
        "$script = 'fixture.py'\n"
        "$output = 'evidence'\n"
        f"{workflow_block}"
    )
    powershell = shutil.which("powershell")
    assert powershell is not None
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    actual_stems = sorted(
        Path(line.rsplit("--output ", 1)[1].strip('"')).stem
        for line in result.stdout.splitlines()
        if "--output " in line
    )
    expected_stems = sorted(
        stem
        for stem in _desktop_evidence_contract()
        if stem.startswith("scale-")
    )
    assert actual_stems == expected_stems


def test_capability_index_has_exact_five_layers_and_honest_blockers() -> None:
    path = _ROOT / "docs" / "evidence" / "capability-index.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["release_version"] == "0.1.0"
    expected_layers = {"code", "tests", "external", "gui", "runtime"}
    by_id = {
        item["capability_id"]: item
        for item in payload["capabilities"]
    }
    assert by_id
    assert all(set(item["layers"]) == expected_layers for item in by_id.values())
    assert by_id["worker-v5-runtime"]["layers"]["runtime"] == "blocked"
    assert by_id["multi-market-workspace"]["layers"]["gui"] == "blocked"
    assert payload["stable_release_ready"] is False


def test_evidence_index_and_samples_match_published_schemas() -> None:
    evidence_root = _ROOT / "docs" / "evidence"
    index_schema = json.loads(
        (evidence_root / "schemas" / "capability-index.schema.json").read_text(
            encoding="utf-8"
        )
    )
    index = json.loads(
        (evidence_root / "capability-index.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(index_schema).validate(index)

    pairs = [
        ("runtime-snapshot", "runtime-snapshot.example.json"),
        ("market-acceptance", "market-acceptance.example.json"),
        ("desktop-acceptance", "desktop-acceptance.example.json"),
        ("release-acceptance", "release-acceptance.example.json"),
    ]
    for schema_stem, sample_name in pairs:
        schema = json.loads(
            (
                evidence_root
                / "schemas"
                / f"{schema_stem}.schema.json"
            ).read_text(encoding="utf-8")
        )
        sample = json.loads(
            (evidence_root / "samples" / sample_name).read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(schema).validate(sample)


def test_windows_install_and_uninstall_scripts_parse() -> None:
    script_paths = [
        _ROOT / "scripts" / "install_windows.ps1",
        _ROOT / "scripts" / "uninstall_windows.ps1",
        _ROOT / "scripts" / "publish_release.ps1",
    ]
    for script_path in script_paths:
        assert script_path.read_bytes().startswith(b"\xef\xbb\xbf"), (
            f"{script_path.name} must use UTF-8 BOM for Windows PowerShell 5.1"
        )

    command = r"""
$paths = @(
  'scripts/install_windows.ps1',
  'scripts/uninstall_windows.ps1',
  'scripts/publish_release.ps1'
)
foreach ($path in $paths) {
  $tokens = $null
  $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $path), [ref]$tokens, [ref]$errors
  ) | Out-Null
  if ($errors.Count -ne 0) {
    $errors | ForEach-Object { Write-Error $_.Message }
    exit 1
  }
}
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_uninstall_rejects_unrelated_shortcut(tmp_path: Path) -> None:
    source = tmp_path / "PA_Agent-v0.1.0"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "pa-agent"\n',
        encoding="utf-8",
    )
    shortcut = tmp_path / "PA_Agent.lnk"
    command = rf"""
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut('{shortcut}')
$link.TargetPath = "$env:SystemRoot\System32\notepad.exe"
$link.Save()
& '{_ROOT / "scripts" / "uninstall_windows.ps1"}' `
  -SourcePath '{source}' `
  -ShortcutPath '{shortcut}' `
  -ConfirmUninstall
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert shortcut.is_file()
    assert "does not belong" in (result.stdout + result.stderr)


def test_install_refuses_machine_without_git(tmp_path: Path) -> None:
    source = tmp_path / "PA_Agent-v0.1.0"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "pa-agent"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_ROOT / "scripts" / "install_windows.ps1"),
            "-SourcePath",
            str(source),
            "-PythonCommand",
            sys.executable,
            "-GitCommand",
            str(tmp_path / "missing-git.exe"),
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "Git for Windows" in (result.stdout + result.stderr)
    assert not (source / ".venv").exists()


def test_install_selects_one_git_executable_when_path_has_duplicates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "PA_Agent-v0.1.0"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "pa-agent"\n',
        encoding="utf-8",
    )
    first_bin = tmp_path / "first-bin"
    second_bin = tmp_path / "second-bin"
    first_bin.mkdir()
    second_bin.mkdir()
    (first_bin / "git.cmd").write_text(
        "@echo off\r\necho git version 2.99.1\r\n",
        encoding="ascii",
    )
    (second_bin / "git.cmd").write_text(
        "@echo off\r\nexit /b 9\r\n",
        encoding="ascii",
    )
    powershell = shutil.which("powershell")
    assert powershell is not None
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(first_bin), str(second_bin)))
    discovered = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-Command",
            (
                "$commands = @(Get-Command git -CommandType Application); "
                "Write-Output $commands.Count; "
                "Write-Output $commands[0].Source"
            ),
        ],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    discovered_lines = discovered.stdout.splitlines()
    assert discovered.returncode == 0, discovered.stdout + discovered.stderr
    assert discovered_lines[0] == "2"
    assert discovered_lines[1].casefold() == str(
        first_bin / "git.cmd"
    ).casefold()

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_ROOT / "scripts" / "install_windows.ps1"),
            "-SourcePath",
            str(source),
            "-PythonCommand",
            sys.executable,
            "-GitCommand",
            "git",
            "-ShortcutPath",
            str(tmp_path / "PA_Agent.lnk"),
            "-WhatIf",
        ],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (source / ".venv").exists()


def test_publish_script_requires_explicit_stable_release_confirmation(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_ROOT / "scripts" / "publish_release.ps1"),
            "-ReleaseRoot",
            str(tmp_path),
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "-ConfirmStableRelease" in (result.stdout + result.stderr)
