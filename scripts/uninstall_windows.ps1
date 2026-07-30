[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$SourcePath = (Split-Path -Parent $PSScriptRoot),
    [string]$ShortcutPath = "",
    [switch]$ConfirmUninstall
)

$ErrorActionPreference = "Stop"
if (-not $ConfirmUninstall) {
    throw "卸载会删除源码目录内的 .venv 和桌面快捷方式；请显式使用 -ConfirmUninstall"
}

$sourceRoot = (Resolve-Path -LiteralPath $SourcePath).Path
$driveRoot = [IO.Path]::GetPathRoot($sourceRoot).TrimEnd("\")
$normalizedSource = $sourceRoot.TrimEnd("\")
$userHome = [Environment]::GetFolderPath("UserProfile").TrimEnd("\")
if ($normalizedSource -eq $driveRoot -or $normalizedSource -eq $userHome) {
    throw "拒绝对磁盘根目录或用户主目录执行卸载"
}
$pyprojectPath = Join-Path $sourceRoot "pyproject.toml"
if (-not (Test-Path -LiteralPath $pyprojectPath -PathType Leaf)) {
    throw "SourcePath 不是 PA_Agent 源码目录"
}
$projectText = Get-Content -Raw -LiteralPath $pyprojectPath
if ($projectText -notmatch '(?m)^name\s*=\s*"pa-agent"\s*$') {
    throw "SourcePath 的项目名称不是 pa-agent"
}

$venvPath = [IO.Path]::GetFullPath((Join-Path $sourceRoot ".venv"))
if (-not $venvPath.StartsWith($sourceRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw "虚拟环境路径越出源码目录"
}
$expectedTarget = [IO.Path]::GetFullPath(
    (Join-Path $venvPath "Scripts\pa-agent.exe")
)
if ([string]::IsNullOrWhiteSpace($ShortcutPath)) {
    $ShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "PA_Agent.lnk"
}
$shortcutFullPath = [IO.Path]::GetFullPath($ShortcutPath)

if (Test-Path -LiteralPath $shortcutFullPath) {
    if ([IO.Path]::GetExtension($shortcutFullPath) -ne ".lnk") {
        throw "Refusing to remove a non-shortcut file: $shortcutFullPath"
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutFullPath)
    $actualTarget = [IO.Path]::GetFullPath($shortcut.TargetPath)
    if (-not $actualTarget.Equals(
        $expectedTarget,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Shortcut does not belong to this PA_Agent source deployment"
    }
    if ($PSCmdlet.ShouldProcess($shortcutFullPath, "Remove PA_Agent shortcut")) {
        Remove-Item -LiteralPath $shortcutFullPath -Force
    }
}
if ((Test-Path -LiteralPath $venvPath) -and $PSCmdlet.ShouldProcess($venvPath, "删除 PA_Agent 虚拟环境")) {
    Remove-Item -LiteralPath $venvPath -Recurse -Force
}

[pscustomobject]@{
    SourcePathPreserved = $sourceRoot
    VirtualEnvironmentRemoved = -not (Test-Path -LiteralPath $venvPath)
    ShortcutRemoved = -not (Test-Path -LiteralPath $shortcutFullPath)
}
