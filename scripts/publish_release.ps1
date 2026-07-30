[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory = $true)]
    [string]$ReleaseRoot,
    [string]$Tag = "v0.1.0",
    [string]$PythonCommand = "",
    [string]$GitleaksCommand = "gitleaks",
    [switch]$ConfirmStableRelease
)

$ErrorActionPreference = "Stop"
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $true
}
if (-not $ConfirmStableRelease) {
    throw "Stable release requires -ConfirmStableRelease"
}

$Repository = "xiaojinlucky/PA_Agent"
$repoRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$releasePath = (Resolve-Path -LiteralPath $ReleaseRoot).Path
$evidenceRoot = Join-Path $releasePath "evidence"
$sourceArchive = Join-Path $releasePath "PA_Agent-v0.1.0-source.zip"
$evidenceArchive = Join-Path $releasePath "PA_Agent-v0.1.0-evidence.zip"
$manifest = Join-Path $releasePath "release-manifest.json"
$checksums = Join-Path $releasePath "SHA256SUMS"
$index = Join-Path $evidenceRoot "capability-index.json"
$schemaRoot = Join-Path $repoRoot "docs\evidence\schemas"

foreach ($required in @(
    $evidenceRoot,
    $sourceArchive,
    $evidenceArchive,
    $manifest,
    $checksums,
    $index,
    $schemaRoot
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required release input is missing: $required"
    }
}

if ([string]::IsNullOrWhiteSpace($PythonCommand)) {
    $PythonCommand = Join-Path $repoRoot ".venv\Scripts\python.exe"
}
foreach ($command in @("git", "gh", $GitleaksCommand, $PythonCommand)) {
    if ($null -eq (
        Get-Command $command -CommandType Application -ErrorAction SilentlyContinue
    )) {
        throw "Required command is unavailable: $command"
    }
}

function ConvertTo-GithubRepository([string]$Url) {
    $text = $Url.Trim().ToLowerInvariant().TrimEnd("/")
    $text = $text -replace '\.git$', ''
    if ($text -match '^git@github\.com:(?<repo>[^/]+/[^/]+)$') {
        return $Matches.repo
    }
    if ($text -match '^https://github\.com/(?<repo>[^/]+/[^/]+)$') {
        return $Matches.repo
    }
    if ($text -match '^ssh://git@github\.com/(?<repo>[^/]+/[^/]+)$') {
        return $Matches.repo
    }
    return ""
}

function Get-CurrentMainSha {
    & git -C $repoRoot fetch --no-tags origin main
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to refresh origin/main"
    }
    $head = (& git -C $repoRoot rev-parse HEAD).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to resolve local HEAD"
    }
    $originMain = (
        & git -C $repoRoot rev-parse origin/main
    ).Trim().ToLowerInvariant()
    if (
        $LASTEXITCODE -ne 0 -or
        $head -notmatch '^[0-9a-f]{40}$' -or
        $head -ne $originMain
    ) {
        throw "Local HEAD must equal current origin/main"
    }
    return $head
}

function Get-GreenWorkflowRun([string]$Workflow, [string]$GitSha) {
    $json = & gh run list --repo $Repository --workflow $Workflow `
        --commit $GitSha --limit 20 `
        --json headSha,status,conclusion,url
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub workflow lookup failed: $Workflow"
    }
    $runs = @($json | ConvertFrom-Json)
    $green = @(
        $runs |
            Where-Object {
                $_.headSha -eq $GitSha -and
                $_.status -eq "completed" -and
                $_.conclusion -eq "success"
            }
    )
    if ($green.Count -eq 0) {
        throw "Target SHA has no successful run: $Workflow"
    }
    return $green[0]
}

function Confirm-ReleaseAssets {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Release,
        [Parameter(Mandatory = $true)]
        [string[]]$AssetPaths,
        [Parameter(Mandatory = $true)]
        [string]$DownloadRoot,
        [Parameter(Mandatory = $true)]
        [string]$Phase,
        [Parameter(Mandatory = $true)]
        [string]$RepositoryName,
        [Parameter(Mandatory = $true)]
        [string]$ReleaseTag
    )

    $expectedNames = @($AssetPaths | ForEach-Object {
        [IO.Path]::GetFileName($_)
    } | Sort-Object)
    $actualNames = @($Release.assets | ForEach-Object {
        $_.name
    } | Sort-Object)
    if (@(Compare-Object $expectedNames $actualNames).Count -ne 0) {
        throw "$Phase asset name set is not exact"
    }
    foreach ($assetPath in $AssetPaths) {
        $name = [IO.Path]::GetFileName($assetPath)
        $asset = @($Release.assets | Where-Object { $_.name -eq $name })
        if (
            $asset.Count -ne 1 -or
            [int64]$asset[0].size -ne (Get-Item $assetPath).Length
        ) {
            throw "$Phase asset size mismatch: $name"
        }
    }

    New-Item -ItemType Directory -Path $DownloadRoot | Out-Null
    & gh release download $ReleaseTag --repo $RepositoryName `
        --dir $DownloadRoot
    if ($LASTEXITCODE -ne 0) {
        throw "$Phase asset download failed"
    }
    foreach ($assetPath in $AssetPaths) {
        $name = [IO.Path]::GetFileName($assetPath)
        $downloaded = Join-Path $DownloadRoot $name
        if (
            -not (Test-Path -LiteralPath $downloaded -PathType Leaf) -or
            (Get-FileHash -Algorithm SHA256 $downloaded).Hash -ne
            (Get-FileHash -Algorithm SHA256 $assetPath).Hash
        ) {
            throw "$Phase downloaded asset hash mismatch: $name"
        }
    }
}

$branch = (& git -C $repoRoot branch --show-current).Trim()
if ($LASTEXITCODE -ne 0 -or $branch -ne "main") {
    throw "Stable release must run from main"
}
$trackedStatus = @(& git -C $repoRoot status --porcelain --untracked-files=no)
if ($LASTEXITCODE -ne 0 -or $trackedStatus.Count -ne 0) {
    throw "Tracked working tree must be clean"
}
$fetchUrl = (& git -C $repoRoot remote get-url origin).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Failed to read origin fetch URL"
}
$pushUrl = (& git -C $repoRoot remote get-url --push origin).Trim()
if (
    $LASTEXITCODE -ne 0 -or
    (ConvertTo-GithubRepository $fetchUrl) -ne $Repository.ToLowerInvariant() -or
    (ConvertTo-GithubRepository $pushUrl) -ne $Repository.ToLowerInvariant()
) {
    throw "origin fetch/push URL is not the approved public repository"
}

$targetSha = Get-CurrentMainSha
$repo = (
    & gh repo view $Repository --json nameWithOwner,visibility,viewerPermission |
        ConvertFrom-Json
)
if ($LASTEXITCODE -ne 0) {
    throw "GitHub repository lookup failed"
}
if (
    $repo.nameWithOwner -ne $Repository -or
    $repo.visibility -ne "PUBLIC" -or
    $repo.viewerPermission -notin @("WRITE", "MAINTAIN", "ADMIN")
) {
    throw "GitHub repository identity or permission is not approved"
}

$existingTags = @(
    & git -C $repoRoot ls-remote --tags origin "refs/tags/$Tag"
)
if ($LASTEXITCODE -ne 0) {
    throw "Remote tag lookup failed"
}
$existingReleases = @(
    & gh release list --repo $Repository --limit 100 --json tagName |
        ConvertFrom-Json
)
if ($LASTEXITCODE -ne 0) {
    throw "GitHub Release lookup failed"
}
if (
    $existingTags.Count -ne 0 -or
    @($existingReleases | Where-Object { $_.tagName -eq $Tag }).Count -ne 0
) {
    throw "Target tag or Release already exists"
}

$version = (
    & $PythonCommand -c "import pa_agent; print(pa_agent.__version__)"
).Trim()
if ($LASTEXITCODE -ne 0 -or $Tag -ne "v$version") {
    throw "Tag does not match pa_agent.__version__"
}
$ciRun = Get-GreenWorkflowRun "ci.yml" $targetSha
$candidateRun = Get-GreenWorkflowRun "release.yml" $targetSha

$scratchRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot "scratch"))
$verificationRoot = [IO.Path]::GetFullPath(
    (Join-Path $scratchRoot ("publish-" + [guid]::NewGuid().ToString("N")))
)
if (-not $verificationRoot.StartsWith(
    $scratchRoot + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Temporary verification path escaped scratch"
}
New-Item -ItemType Directory -Path $verificationRoot | Out-Null

try {
    $rebuiltRoot = Join-Path $verificationRoot "rebuilt"
    New-Item -ItemType Directory -Path $rebuiltRoot | Out-Null
    & $PythonCommand (Join-Path $repoRoot "scripts\release_pipeline.py") `
        build-source `
        --repo-root $repoRoot `
        --output-dir $rebuiltRoot `
        --ref $targetSha
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to rebuild source archive from target SHA"
    }
    $rebuiltSource = Join-Path $rebuiltRoot "PA_Agent-v0.1.0-source.zip"
    if (
        (Get-FileHash -Algorithm SHA256 $rebuiltSource).Hash -ne
        (Get-FileHash -Algorithm SHA256 $sourceArchive).Hash
    ) {
        throw "Provided source archive is not git archive of target SHA"
    }
    $extracted = Join-Path $verificationRoot "source"
    Expand-Archive -LiteralPath $sourceArchive -DestinationPath $extracted
    & $GitleaksCommand dir $extracted --no-banner --redact
    if ($LASTEXITCODE -ne 0) {
        throw "Gitleaks rejected the source archive"
    }
    & $GitleaksCommand dir $evidenceRoot --no-banner --redact
    if ($LASTEXITCODE -ne 0) {
        throw "Gitleaks rejected the external evidence bundle"
    }
    $uploadScanRoot = Join-Path $verificationRoot "upload-scan"
    New-Item -ItemType Directory -Path $uploadScanRoot | Out-Null
    foreach ($uploadPath in @(
        $sourceArchive,
        $evidenceArchive,
        $manifest,
        $checksums,
        (Join-Path $repoRoot "CHANGELOG.md")
    )) {
        Copy-Item -LiteralPath $uploadPath -Destination $uploadScanRoot
    }
    & $PythonCommand (Join-Path $repoRoot "scripts\release_pipeline.py") `
        scan-tree $uploadScanRoot --reject-private-paths
    if ($LASTEXITCODE -ne 0) {
        throw "Release upload set contains sensitive text or a private path"
    }
    & $GitleaksCommand dir $uploadScanRoot --no-banner --redact
    if ($LASTEXITCODE -ne 0) {
        throw "Gitleaks rejected the release upload set"
    }

    & $PythonCommand (Join-Path $repoRoot "scripts\release_pipeline.py") `
        validate-index `
        --stable `
        --path $index `
        --repo-root $repoRoot `
        --evidence-root $evidenceRoot `
        --schema-root $schemaRoot `
        --source-archive $sourceArchive `
        --evidence-archive $evidenceArchive `
        --release-manifest $manifest `
        --checksums $checksums `
        --require-fresh-now `
        --sha $targetSha
    if ($LASTEXITCODE -ne 0) {
        throw "Stable release evidence gate failed"
    }

    if ((Get-CurrentMainSha) -ne $targetSha) {
        throw "origin/main moved before draft creation"
    }
    if (-not $PSCmdlet.ShouldProcess(
        "$Repository@$targetSha",
        "Create and publish public GitHub Release $Tag"
    )) {
        [pscustomobject]@{
            Repository = $Repository
            Tag = $Tag
            GitSha = $targetSha
            CiUrl = $ciRun.url
            CandidateUrl = $candidateRun.url
            ReleaseUrl = $null
            Result = "preflight-pass"
        }
        return
    }

    & gh release create $Tag `
        --repo $Repository `
        --target $targetSha `
        --draft `
        --title "PA_Agent v0.1.0" `
        --notes-file (Join-Path $repoRoot "CHANGELOG.md") `
        $sourceArchive `
        $evidenceArchive `
        $manifest `
        $checksums
    if ($LASTEXITCODE -ne 0) {
        throw "Draft GitHub Release creation failed; inspect the draft and tag"
    }

    & git -C $repoRoot fetch --tags origin $Tag
    if ($LASTEXITCODE -ne 0) {
        throw "Draft tag refresh failed"
    }
    $tagSha = (& git -C $repoRoot rev-parse "$Tag^{}").Trim().ToLowerInvariant()
    $draft = (
        & gh release view $Tag --repo $Repository `
            --json url,tagName,targetCommitish,isDraft,isPrerelease,assets |
            ConvertFrom-Json
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Draft Release lookup failed"
    }
    $assetPaths = @($sourceArchive, $evidenceArchive, $manifest, $checksums)
    if (
        $tagSha -ne $targetSha -or
        $draft.tagName -ne $Tag -or
        -not $draft.isDraft -or
        $draft.isPrerelease
    ) {
        throw "Draft tag or Release state is not exact"
    }

    $downloadRoot = Join-Path $verificationRoot "downloaded-assets"
    Confirm-ReleaseAssets `
        -Release $draft `
        -AssetPaths $assetPaths `
        -DownloadRoot $downloadRoot `
        -Phase "Draft" `
        -RepositoryName $Repository `
        -ReleaseTag $Tag

    if ((Get-CurrentMainSha) -ne $targetSha) {
        throw "origin/main moved before draft publication"
    }
    & gh release edit $Tag --repo $Repository --draft=false
    if ($LASTEXITCODE -ne 0) {
        throw "Draft publication failed; inspect the draft and tag"
    }
    if ((Get-CurrentMainSha) -ne $targetSha) {
        throw "origin/main moved during publication"
    }
    & git -C $repoRoot fetch --tags origin $Tag
    if ($LASTEXITCODE -ne 0) {
        throw "Published tag refresh failed"
    }
    $tagSha = (& git -C $repoRoot rev-parse "$Tag^{}").Trim().ToLowerInvariant()
    $release = (
        & gh release view $Tag --repo $Repository `
            --json url,tagName,targetCommitish,isDraft,isPrerelease,assets |
            ConvertFrom-Json
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Published Release lookup failed"
    }
    if (
        $tagSha -ne $targetSha -or
        $release.tagName -ne $Tag -or
        $release.isDraft -or
        $release.isPrerelease
    ) {
        throw "Published main, tag, or Release state is not aligned"
    }
    $publishedDownloadRoot = Join-Path (
        $verificationRoot
    ) "published-assets"
    try {
        Confirm-ReleaseAssets `
            -Release $release `
            -AssetPaths $assetPaths `
            -DownloadRoot $publishedDownloadRoot `
            -Phase "Published" `
            -RepositoryName $Repository `
            -ReleaseTag $Tag
    }
    catch {
        throw (
            "Published Release asset verification failed after publication: " +
            $_.Exception.Message
        )
    }

    [pscustomobject]@{
        Repository = $Repository
        Tag = $Tag
        GitSha = $targetSha
        CiUrl = $ciRun.url
        CandidateUrl = $candidateRun.url
        ReleaseUrl = $release.url
        Result = "published"
    }
}
finally {
    if (
        (Test-Path -LiteralPath $verificationRoot) -and
        $verificationRoot.StartsWith(
            $scratchRoot + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        Remove-Item -LiteralPath $verificationRoot -Recurse -Force
    }
}
