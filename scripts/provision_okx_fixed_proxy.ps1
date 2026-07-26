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

function Invoke-CheckedIcacls {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    & icacls.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage（icacls 退出码 $LASTEXITCODE）。"
    }
}

function Get-RuntimeTreeItems {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RuntimeDirectory
    )

    $runtimeRoot = Get-Item -LiteralPath $RuntimeDirectory -Force -ErrorAction Stop
    $children = @(
        Get-ChildItem `
            -LiteralPath $RuntimeDirectory `
            -Force `
            -Recurse `
            -ErrorAction Stop
    )
    return @($runtimeRoot) + @(
        $children | Sort-Object {
            $_.FullName.Split(
                [System.IO.Path]::DirectorySeparatorChar
            ).Count
        }, FullName
    )
}

function Assert-NotReparsePoint {
    param(
        [Parameter(Mandatory = $true)]
        [System.IO.FileSystemInfo]$Item
    )

    if (
        ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "固定代理运行目录禁止使用重解析点：$($Item.FullName)"
    }
}

function ConvertTo-SidValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Identity
    )

    try {
        return (
            New-Object System.Security.Principal.NTAccount($Identity)
        ).Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
    }
    catch {
        return (
            New-Object System.Security.Principal.SecurityIdentifier($Identity)
        ).Value
    }
}

function Assert-RuntimeDirectorySecurity {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RuntimeDirectory,

        [Parameter(Mandatory = $true)]
        [string]$CurrentSid
    )

    $expectedSids = @($CurrentSid, "S-1-5-18") | Sort-Object
    $runtimeRootPath = [System.IO.Path]::GetFullPath($RuntimeDirectory)
    foreach ($item in Get-RuntimeTreeItems -RuntimeDirectory $RuntimeDirectory) {
        Assert-NotReparsePoint -Item $item
        if ($item.PSIsContainer) {
            $acl = [System.IO.Directory]::GetAccessControl($item.FullName)
        }
        else {
            $acl = [System.IO.File]::GetAccessControl($item.FullName)
        }
        $ownerSid = $acl.Owner
        try {
            $ownerSid = ConvertTo-SidValue -Identity $acl.Owner
        }
        catch {
            throw "无法核验固定代理文件 Owner：$($item.FullName)"
        }
        if ($ownerSid -ne $CurrentSid) {
            throw "固定代理文件 Owner 不是当前用户：$($item.FullName)"
        }

        $rules = @($acl.Access)
        if (
            $rules | Where-Object {
                $_.AccessControlType -eq (
                    [System.Security.AccessControl.AccessControlType]::Deny
                )
            }
        ) {
            throw "固定代理文件存在 Deny 权限：$($item.FullName)"
        }
        $allowRules = @(
            $rules | Where-Object {
                $_.AccessControlType -eq (
                    [System.Security.AccessControl.AccessControlType]::Allow
                )
            }
        )
        $actualSids = @(
            $allowRules | ForEach-Object {
                $_.IdentityReference.Translate(
                    [System.Security.Principal.SecurityIdentifier]
                ).Value
            }
        ) | Sort-Object
        if (
            $allowRules.Count -ne 2 -or
            (Compare-Object $expectedSids $actualSids)
        ) {
            throw "固定代理文件授权身份异常：$($item.FullName)"
        }

        $isRuntimeRoot = (
            [System.IO.Path]::GetFullPath($item.FullName) -eq $runtimeRootPath
        )
        if ($isRuntimeRoot -and -not $acl.AreAccessRulesProtected) {
            throw "固定代理运行目录仍允许继承外部 DACL。"
        }
        if (-not $isRuntimeRoot -and $acl.AreAccessRulesProtected) {
            throw "固定代理子项仍使用受保护 DACL：$($item.FullName)"
        }
        foreach ($rule in $allowRules) {
            if (
                ($rule.FileSystemRights -band (
                    [System.Security.AccessControl.FileSystemRights]::FullControl
                )) -ne (
                    [System.Security.AccessControl.FileSystemRights]::FullControl
                )
            ) {
                throw "固定代理文件没有完整控制权限：$($item.FullName)"
            }
            if ($isRuntimeRoot -and $rule.IsInherited) {
                throw "固定代理运行目录存在意外的继承权限。"
            }
            if (
                $isRuntimeRoot -and
                $rule.InheritanceFlags -ne (
                    [System.Security.AccessControl.InheritanceFlags]::ContainerInherit `
                    -bor `
                    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
                )
            ) {
                throw "固定代理运行目录权限没有向子项完整继承。"
            }
            if (
                $isRuntimeRoot -and
                $rule.PropagationFlags -ne (
                    [System.Security.AccessControl.PropagationFlags]::None
                )
            ) {
                throw "固定代理运行目录权限使用了异常传播方式。"
            }
            if (-not $isRuntimeRoot -and -not $rule.IsInherited) {
                throw "固定代理子项没有继承运行目录权限：$($item.FullName)"
            }
        }
    }
}

function Protect-RuntimeDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RuntimeDirectory,

        [Parameter(Mandatory = $true)]
        [System.Security.Principal.SecurityIdentifier]$CurrentSidObject
    )

    # 修改任何 DACL 之前先完整枚举一次；发现 Junction、符号链接等重解析点
    # 立即终止，绝不沿着链接改到运行目录之外。
    $initialItems = @(
        Get-RuntimeTreeItems -RuntimeDirectory $RuntimeDirectory
    )
    foreach ($item in $initialItems) {
        Assert-NotReparsePoint -Item $item
    }

    $currentSid = $CurrentSidObject.Value
    $systemSid = New-Object `
        System.Security.Principal.SecurityIdentifier("S-1-5-18")
    Invoke-CheckedIcacls `
        -Arguments @($RuntimeDirectory, "/reset") `
        -FailureMessage "无法清除固定代理运行目录既有 DACL"
    Invoke-CheckedIcacls `
        -Arguments @(
            $RuntimeDirectory,
            "/setowner",
            "*$currentSid"
        ) `
        -FailureMessage "无法设置固定代理运行目录 Owner"
    Invoke-CheckedIcacls `
        -Arguments @(
            $RuntimeDirectory,
            "/grant:r",
            "*${currentSid}:(OI)(CI)F"
        ) `
        -FailureMessage "无法取得固定代理运行目录 DACL 控制权"

    $rootAcl = New-Object `
        System.Security.AccessControl.DirectorySecurity
    $rootAcl.SetOwner($CurrentSidObject)
    $rootAcl.SetAccessRuleProtection($true, $false)
    foreach ($identitySid in @($CurrentSidObject, $systemSid)) {
        $rootRule = New-Object `
            System.Security.AccessControl.FileSystemAccessRule(
                $identitySid,
                [System.Security.AccessControl.FileSystemRights]::FullControl,
                (
                    [System.Security.AccessControl.InheritanceFlags]::ContainerInherit `
                    -bor `
                    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
                ),
                [System.Security.AccessControl.PropagationFlags]::None,
                [System.Security.AccessControl.AccessControlType]::Allow
            )
        [void]$rootAcl.AddAccessRule($rootRule)
    }
    [System.IO.Directory]::SetAccessControl($RuntimeDirectory, $rootAcl)

    # 必须逐项处理，不能启用“遇错继续”吞掉单个失败。父目录先于子项，
    # 包括隐藏文件和隐藏目录，确保每个子项最终只继承根目录的两条权限。
    foreach ($item in $initialItems | Select-Object -Skip 1) {
        if ($item.PSIsContainer) {
            $itemAcl = [System.IO.Directory]::GetAccessControl($item.FullName)
        }
        else {
            $itemAcl = [System.IO.File]::GetAccessControl($item.FullName)
        }
        try {
            $itemOwnerSid = ConvertTo-SidValue -Identity $itemAcl.Owner
        }
        catch {
            $itemOwnerSid = $null
        }
        if ($itemOwnerSid -ne $currentSid) {
            Invoke-CheckedIcacls `
                -Arguments @(
                    $item.FullName,
                    "/setowner",
                    "*$currentSid"
                ) `
                -FailureMessage "无法设置固定代理子项 Owner：$($item.FullName)"
        }
        Invoke-CheckedIcacls `
            -Arguments @($item.FullName, "/reset") `
            -FailureMessage "无法重置固定代理子项 DACL：$($item.FullName)"
    }

    Assert-RuntimeDirectorySecurity `
        -RuntimeDirectory $RuntimeDirectory `
        -CurrentSid $currentSid
}

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
$currentSidObject = (
    [System.Security.Principal.WindowsIdentity]::GetCurrent().User
)
Protect-RuntimeDirectory `
    -RuntimeDirectory $RuntimeDirectory `
    -CurrentSidObject $currentSidObject
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
    (New-Object System.Text.UTF8Encoding($true))
)

# 文件全部生成后重做一次目录和全部递归子项的安全边界。最终核验之后
# 才允许执行复制进来的 sing-box，避免在不可信 ACL 下加载可执行文件。
Protect-RuntimeDirectory `
    -RuntimeDirectory $RuntimeDirectory `
    -CurrentSidObject $currentSidObject
Assert-RuntimeDirectorySecurity `
    -RuntimeDirectory $RuntimeDirectory `
    -CurrentSid $currentSidObject.Value

& $runtimeCorePath check -c $runtimeConfigPath
if ($LASTEXITCODE -ne 0) {
    throw "独立 sing-box 配置检查失败，退出码 $LASTEXITCODE。"
}

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
