from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "provision_okx_fixed_proxy.ps1"
POWERSHELL = shutil.which("powershell.exe")


def _run_windows_powershell(
    script_path: Path,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if POWERSHELL is None:
        pytest.skip("Windows PowerShell 5.1 不可用")
    process_environment = os.environ.copy()
    if environment:
        process_environment.update(environment)
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        cwd=PROJECT_ROOT,
        env=process_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def test_provision_and_generated_supervisor_parse_in_windows_powershell_51(
    tmp_path: Path,
) -> None:
    assert SCRIPT_PATH.read_bytes().startswith(b"\xef\xbb\xbf")
    script = SCRIPT_PATH.read_text(encoding="utf-8-sig")
    supervisor_match = re.search(
        r"\$supervisor = @'\r?\n(?P<body>.*?)\r?\n'@",
        script,
        flags=re.DOTALL,
    )
    assert supervisor_match is not None
    supervisor_path = tmp_path / "run-hidden.ps1"
    supervisor_path.write_text(
        supervisor_match.group("body").replace("__LISTEN_PORT__", "10981"),
        encoding="utf-8-sig",
    )
    assert supervisor_path.read_bytes().startswith(b"\xef\xbb\xbf")

    parser_harness = tmp_path / "parse.ps1"
    parser_harness.write_text(
        """
$ErrorActionPreference = "Stop"
$results = @()
foreach ($path in @($env:PROVISION_PATH, $env:SUPERVISOR_PATH)) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $path,
        [ref]$tokens,
        [ref]$errors
    )
    if ($errors.Count -ne 0) {
        throw (($errors | ForEach-Object { $_.Message }) -join "; ")
    }
    $results += $path
}
[ordered]@{
    version = $PSVersionTable.PSVersion.ToString()
    parsed = $results.Count
} | ConvertTo-Json -Compress
""".lstrip(),
        encoding="utf-8-sig",
    )
    result = _run_windows_powershell(
        parser_harness,
        environment={
            "PROVISION_PATH": str(SCRIPT_PATH),
            "SUPERVISOR_PATH": str(supervisor_path),
        },
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["version"].startswith("5.1.")
    assert payload["parsed"] == 2


def test_provision_security_guards_run_before_copy_write_and_execute() -> None:
    script = SCRIPT_PATH.read_text(encoding="utf-8-sig")
    main_start = script.index('if (-not $RuntimeDirectory) {')
    main = script[main_start:]
    initial_protection = main.index("Protect-RuntimeDirectory")
    first_copy = main.index("Copy-Item")
    first_payload_write = main.index("[System.IO.File]::WriteAllText")
    final_protection = main.rindex("Protect-RuntimeDirectory")
    final_verification = main.rindex("Assert-RuntimeDirectorySecurity")
    sing_box_check = main.index("& $runtimeCorePath check")

    assert initial_protection < first_copy
    assert initial_protection < first_payload_write
    assert first_payload_write < final_protection
    assert final_protection < final_verification < sing_box_check
    assert "-Force `\n            -Recurse" in script
    assert '"/reset"' in script
    assert "/C" not in script
    assert "AreAccessRulesProtected" in script
    assert "AccessControlType]::Deny" in script
    assert "FileAttributes]::ReparsePoint" in script
    assert "Owner 不是当前用户" in script
    assert "New-Object System.Text.UTF8Encoding($true)" in script


def test_acl_helpers_repair_hidden_recursive_items_in_isolated_directory(
    tmp_path: Path,
) -> None:
    runtime_directory = tmp_path / "runtime"
    harness = tmp_path / "acl-harness.ps1"
    harness.write_text(
        """
$ErrorActionPreference = "Stop"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:PROVISION_PATH,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) {
    throw "provision parser errors"
}
$functionDefinitions = $ast.FindAll(
    {
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
    },
    $true
)
Invoke-Expression (
    ($functionDefinitions | ForEach-Object { $_.Extent.Text }) -join "`r`n"
)

$runtimeDirectory = $env:RUNTIME_PATH
$nestedDirectory = Join-Path $runtimeDirectory "nested"
$hiddenDirectory = Join-Path $nestedDirectory ".hidden"
[void](New-Item -ItemType Directory -Path $hiddenDirectory -Force)
$hiddenFile = Join-Path $hiddenDirectory "state.txt"
[System.IO.File]::WriteAllText($hiddenFile, "state")
(Get-Item -LiteralPath $hiddenDirectory -Force).Attributes = (
    (Get-Item -LiteralPath $hiddenDirectory -Force).Attributes -bor
    [System.IO.FileAttributes]::Hidden
)
(Get-Item -LiteralPath $hiddenFile -Force).Attributes = (
    (Get-Item -LiteralPath $hiddenFile -Force).Attributes -bor
    [System.IO.FileAttributes]::Hidden
)

& icacls.exe $nestedDirectory /inheritance:r | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "failed to protect fixture ACL"
}
& icacls.exe $nestedDirectory /grant:r "*S-1-1-0:(OI)(CI)R" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "failed to contaminate fixture ACL"
}

$currentSidObject = (
    [System.Security.Principal.WindowsIdentity]::GetCurrent().User
)
Protect-RuntimeDirectory `
    -RuntimeDirectory $runtimeDirectory `
    -CurrentSidObject $currentSidObject
Assert-RuntimeDirectorySecurity `
    -RuntimeDirectory $runtimeDirectory `
    -CurrentSid $currentSidObject.Value

$items = @(Get-RuntimeTreeItems -RuntimeDirectory $runtimeDirectory)
[ordered]@{
    item_count = $items.Count
    hidden_directory_seen = (
        $items.FullName -contains (
            Get-Item -LiteralPath $hiddenDirectory -Force
        ).FullName
    )
    hidden_file_seen = (
        $items.FullName -contains (
            Get-Item -LiteralPath $hiddenFile -Force
        ).FullName
    )
} | ConvertTo-Json -Compress
""".lstrip(),
        encoding="utf-8-sig",
    )
    result = _run_windows_powershell(
        harness,
        environment={
            "PROVISION_PATH": str(SCRIPT_PATH),
            "RUNTIME_PATH": str(runtime_directory),
        },
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload == {
        "item_count": 4,
        "hidden_directory_seen": True,
        "hidden_file_seen": True,
    }
