param(
    [Parameter(Mandatory = $true)]
    [string]$V2rayNRoot,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedProfileId,

    [Parameter(Mandatory = $true)]
    [string]$NodeLabel,

    [int]$ListenPort = 10981,

    [string]$StartupValueName = "PAAgentOkxFixedProxy",

    [string]$RuntimeDirectory = ""
)

$ErrorActionPreference = "Stop"

if (-not $RuntimeDirectory) {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $RuntimeDirectory = Join-Path $projectRoot "records\okx_fixed_proxy"
}

$resolvedV2rayNRoot = (Resolve-Path -LiteralPath $V2rayNRoot).Path
$guiConfigPath = Join-Path $resolvedV2rayNRoot "guiConfigs\guiNConfig.json"
$sourceConfigPath = Join-Path $resolvedV2rayNRoot "binConfigs\config.json"
$sourceCorePath = Join-Path $resolvedV2rayNRoot "bin\sing_box\sing-box.exe"

foreach ($requiredPath in @(
    $guiConfigPath,
    $sourceConfigPath,
    $sourceCorePath
)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "缺少固定代理部署所需文件：$requiredPath"
    }
}

$guiConfig = Get-Content -LiteralPath $guiConfigPath -Raw -Encoding UTF8 |
    ConvertFrom-Json
if ([string]$guiConfig.IndexId -ne $ExpectedProfileId) {
    throw (
        "v2rayN 当前节点已经变化。预期节点 ID=$ExpectedProfileId，" +
        "实际节点 ID=$($guiConfig.IndexId)。拒绝复制错误节点。"
    )
}

$config = Get-Content -LiteralPath $sourceConfigPath -Raw -Encoding UTF8 |
    ConvertFrom-Json
if ($config.inbounds.Count -ne 1 -or $config.inbounds[0].type -ne "mixed") {
    throw "当前 sing-box 配置不是单一 mixed 本机入口，拒绝猜测修改。"
}
if (-not ($config.outbounds | Where-Object { $_.tag -eq "proxy" })) {
    throw "当前 sing-box 配置缺少 proxy 出口，拒绝部署。"
}

$config.inbounds[0].listen = "127.0.0.1"
$config.inbounds[0].listen_port = $ListenPort
$config.inbounds[0].tag = "pa-agent-okx"

if ($null -ne $config.experimental.cache_file) {
    $config.experimental.cache_file.path = (
        Join-Path $RuntimeDirectory "cache.db"
    )
}
if ($null -ne $config.experimental.clash_api) {
    $config.experimental.PSObject.Properties.Remove("clash_api")
}

if (Get-NetTCPConnection -State Listen -LocalPort $ListenPort -ErrorAction SilentlyContinue) {
    throw "本机端口 $ListenPort 已被占用，拒绝覆盖。"
}
$startupRegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$existingStartup = Get-ItemProperty `
    -Path $startupRegistryPath `
    -Name $StartupValueName `
    -ErrorAction SilentlyContinue
if ($null -ne $existingStartup) {
    throw "登录自启动项 $StartupValueName 已存在，拒绝覆盖。"
}

New-Item -ItemType Directory -Path $RuntimeDirectory -Force | Out-Null
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $RuntimeDirectory `
    /inheritance:r `
    /grant:r `
    "${currentIdentity}:(OI)(CI)F" `
    "SYSTEM:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "无法把固定代理运行目录权限收紧到当前用户和 SYSTEM。"
}
$runtimeCorePath = Join-Path $RuntimeDirectory "sing-box.exe"
$runtimeConfigPath = Join-Path $RuntimeDirectory "config.json"
$metadataPath = Join-Path $RuntimeDirectory "metadata.json"
$supervisorPath = Join-Path $RuntimeDirectory "run-hidden.ps1"
Copy-Item -LiteralPath $sourceCorePath -Destination $runtimeCorePath

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$renderedConfig = $config | ConvertTo-Json -Depth 100
[System.IO.File]::WriteAllText(
    $runtimeConfigPath,
    $renderedConfig,
    $utf8NoBom
)

& $runtimeCorePath check -c $runtimeConfigPath
if ($LASTEXITCODE -ne 0) {
    throw "独立 sing-box 配置检查失败，退出码 $LASTEXITCODE。"
}

$metadata = [ordered]@{
    schema_version = 1
    node_label = $NodeLabel
    profile_id = $ExpectedProfileId
    listen_host = "127.0.0.1"
    listen_port = $ListenPort
    source_config_sha256 = (
        Get-FileHash -LiteralPath $sourceConfigPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    generated_at = [DateTimeOffset]::Now.ToString("o")
}
[System.IO.File]::WriteAllText(
    $metadataPath,
    ($metadata | ConvertTo-Json),
    $utf8NoBom
)

$supervisor = @'
$ErrorActionPreference = "Continue"
$runtimeDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$corePath = Join-Path $runtimeDirectory "sing-box.exe"
$configPath = Join-Path $runtimeDirectory "config.json"
$listenPort = __LISTEN_PORT__
$mutex = [System.Threading.Mutex]::new(
    $false,
    "Global\PAAgentOkxFixedProxy-$listenPort"
)
if (-not $mutex.WaitOne(0)) {
    $mutex.Dispose()
    exit 0
}

try {
    while ($true) {
        $existing = Get-NetTCPConnection `
            -State Listen `
            -LocalAddress 127.0.0.1 `
            -LocalPort $listenPort `
            -ErrorAction SilentlyContinue
        if ($existing) {
            Start-Sleep -Seconds 10
            continue
        }
        $process = Start-Process `
            -FilePath $corePath `
            -ArgumentList @("run", "-c", $configPath) `
            -WorkingDirectory $runtimeDirectory `
            -WindowStyle Hidden `
            -PassThru
        $process.WaitForExit()
        Start-Sleep -Seconds 5
    }
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
'@
$supervisor = $supervisor.Replace(
    "__LISTEN_PORT__",
    [string]$ListenPort
)
[System.IO.File]::WriteAllText(
    $supervisorPath,
    $supervisor,
    $utf8NoBom
)

$startupCommand = (
    "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden " +
    "-ExecutionPolicy Bypass -File `"$supervisorPath`""
)
try {
    New-ItemProperty `
        -Path $startupRegistryPath `
        -Name $StartupValueName `
        -Value $startupCommand `
        -PropertyType String | Out-Null
    $supervisorProcess = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @(
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            $supervisorPath
        ) `
        -WindowStyle Hidden `
        -PassThru

    $deadline = [DateTimeOffset]::Now.AddSeconds(20)
    do {
        $listener = Get-NetTCPConnection `
            -State Listen `
            -LocalAddress 127.0.0.1 `
            -LocalPort $ListenPort `
            -ErrorAction SilentlyContinue
        if ($listener) {
            break
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTimeOffset]::Now -lt $deadline)

    if (-not $listener) {
        throw "固定代理已启动，但端口 $ListenPort 未在 20 秒内监听。"
    }
    $listenerPath = (Get-Process -Id $listener.OwningProcess -ErrorAction Stop).Path
    if (
        [System.IO.Path]::GetFullPath($listenerPath) -ne
        [System.IO.Path]::GetFullPath($runtimeCorePath)
    ) {
        throw "端口 $ListenPort 的监听者不是本次固定代理。"
    }
}
catch {
    if ($null -ne $supervisorProcess) {
        Stop-Process -Id $supervisorProcess.Id -Force -ErrorAction SilentlyContinue
    }
    $failedListener = Get-NetTCPConnection `
        -State Listen `
        -LocalAddress 127.0.0.1 `
        -LocalPort $ListenPort `
        -ErrorAction SilentlyContinue
    if ($failedListener) {
        $failedListenerProcess = Get-Process `
            -Id $failedListener.OwningProcess `
            -ErrorAction SilentlyContinue
        if (
            $null -ne $failedListenerProcess -and
            [System.IO.Path]::GetFullPath($failedListenerProcess.Path) -eq
            [System.IO.Path]::GetFullPath($runtimeCorePath)
        ) {
            Stop-Process `
                -Id $failedListener.OwningProcess `
                -Force `
                -ErrorAction SilentlyContinue
        }
    }
    Remove-ItemProperty `
        -Path $startupRegistryPath `
        -Name $StartupValueName `
        -ErrorAction SilentlyContinue
    throw
}

[ordered]@{
    status = "running"
    startup_value = $StartupValueName
    node_label = $NodeLabel
    listen = "127.0.0.1:$ListenPort"
    supervisor_process_id = $supervisorProcess.Id
    proxy_process_id = $listener.OwningProcess
    runtime_directory = $RuntimeDirectory
} | ConvertTo-Json
