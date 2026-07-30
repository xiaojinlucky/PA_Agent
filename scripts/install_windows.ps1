[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [string]$SourcePath = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonCommand = "python",
    [string]$GitCommand = "git",
    [string]$ShortcutPath = "",
    [switch]$ReplaceShortcut
)

$ErrorActionPreference = "Stop"
$sourceRoot = (Resolve-Path -LiteralPath $SourcePath).Path
$pyprojectPath = Join-Path $sourceRoot "pyproject.toml"
if (-not (Test-Path -LiteralPath $pyprojectPath -PathType Leaf)) {
    throw "SourcePath 不是 PA_Agent 源码目录：缺少 pyproject.toml"
}
$projectText = Get-Content -Raw -LiteralPath $pyprojectPath
if ($projectText -notmatch '(?m)^name\s*=\s*"pa-agent"\s*$') {
    throw "SourcePath 的项目名称不是 pa-agent"
}

$pythonVersion = (& $PythonCommand -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne "3.12") {
    throw "PA_Agent v0.1.0 只接受 Python 3.12；当前为 $pythonVersion"
}
$gitExecutable = Get-Command $GitCommand -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $gitExecutable) {
    throw "Git for Windows is required because a dependency is pinned to a Git commit"
}
$gitVersion = (& $gitExecutable.Source --version).Trim()
if ($LASTEXITCODE -ne 0 -or $gitVersion -notmatch '^git version ') {
    throw "Git for Windows cannot run"
}

$venvPath = Join-Path $sourceRoot ".venv"
if (Test-Path -LiteralPath $venvPath) {
    throw "拒绝覆盖已有虚拟环境：$venvPath"
}
if ([string]::IsNullOrWhiteSpace($ShortcutPath)) {
    $ShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "PA_Agent.lnk"
}
$shortcutFullPath = [IO.Path]::GetFullPath($ShortcutPath)
if ((Test-Path -LiteralPath $shortcutFullPath) -and -not $ReplaceShortcut) {
    throw "快捷方式已存在；如确需替换，请显式使用 -ReplaceShortcut：$shortcutFullPath"
}

if ($PSCmdlet.ShouldProcess($sourceRoot, "创建 Python 3.12 源码部署虚拟环境")) {
    & $PythonCommand -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw "创建虚拟环境失败"
    }
    $venvPython = Join-Path $venvPath "Scripts\python.exe"
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "升级 pip 失败"
    }
    & $venvPython -m pip install -e $sourceRoot
    if ($LASTEXITCODE -ne 0) {
        throw "editable 源码安装失败"
    }
    $paAgentExe = Join-Path $venvPath "Scripts\pa-agent.exe"
    $workerExe = Join-Path $venvPath "Scripts\pa-execution-worker.exe"
    foreach ($entrypoint in @($paAgentExe, $workerExe)) {
        if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
            throw "安装后缺少命令入口：$entrypoint"
        }
    }
    & $paAgentExe --self-check
    if ($LASTEXITCODE -ne 0) {
        throw "PA_Agent 离线自检失败"
    }

    $shortcutDirectory = Split-Path -Parent $shortcutFullPath
    if (-not (Test-Path -LiteralPath $shortcutDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $shortcutDirectory | Out-Null
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutFullPath)
    $shortcut.TargetPath = $paAgentExe
    $shortcut.WorkingDirectory = $sourceRoot
    $shortcut.Description = "PA Agent v0.1.0（Python 3.12 源码部署版）"
    $shortcut.Save()
}

[pscustomobject]@{
    SourcePath = $sourceRoot
    VirtualEnvironment = $venvPath
    Shortcut = $shortcutFullPath
    Version = "0.1.0"
}
