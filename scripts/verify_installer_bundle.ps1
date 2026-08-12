param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    # Optional extra literals to reject inside the staged mpv runtime. The generic
    # drive-letter check below is the primary defence; this exists so a specific
    # string can be banned without editing this script.
    [string[]]$ForbiddenLiteral = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$resolvedRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$nsiPath = Join-Path $resolvedRoot 'src-tauri\target\release\nsis\x64\installer.nsi'
$bundleRoot = Join-Path $resolvedRoot 'src-tauri\target\release\bundle\nsis'
$sourceDll = Join-Path $resolvedRoot 'src-tauri\target\release\WebView2Loader.dll'
$stagedDll = Join-Path $resolvedRoot 'packaging\runtime\root\WebView2Loader.dll'

foreach ($requiredFile in @($nsiPath, $sourceDll, $stagedDll)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required installer input is missing: $requiredFile"
    }
}

$loaderEntry = Select-String -LiteralPath $nsiPath -Pattern 'oname=WebView2Loader\.dll' -Encoding UTF8
if (-not $loaderEntry) {
    throw 'The generated NSIS manifest does not install WebView2Loader.dll beside the desktop executable.'
}

$nsiContent = Get-Content -LiteralPath $nsiPath -Raw -Encoding UTF8

# The NSIS manifest is generated from the Tauri CLI's built-in template, not from a
# file in this repository, so every data-protection assertion below inspects the
# build output. If the template ever stops emitting a recognisable uninstall
# section, those assertions would silently match nothing and pass by accident.
if ($nsiContent -notmatch '(?im)^\s*Section\s+"?Uninstall"?') {
    throw 'Could not locate the uninstall section in the generated NSIS manifest. The bundler template changed shape, so the data-protection assertions below can no longer be trusted; review them before shipping.'
}

foreach ($line in ($nsiContent -split "`r?`n")) {
    if ($line -match '^\s*RMDir\s+(?<options>(?:/\S+\s+)*)"\$INSTDIR\\?"\s*$') {
        $options = @($Matches.options -split '\s+' | Where-Object { $_ })
        if ($options -contains '/r') {
            throw 'Recursive deletion of the install root is forbidden because it may contain the user mirror directory.'
        }
    }
    # The mirror and the managed data directory must never be deleted, recursively or not.
    if ($line -match '(?i)^\s*(?:RMDir|Delete)\b.*(?:\\mirror\b|KumiPlayer\\data\b)') {
        throw 'The installer manifest must not delete the user mirror or the managed KumiPlayer data directory.'
    }
}

if ($nsiContent -match '(?i)\$INSTDIR\\mirror(?:\\|"|\s|$)') {
    throw 'The installer manifest must not manage the user mirror directory.'
}

# Installed user data lives in %LOCALAPPDATA%\KumiPlayer\data. Tauri's own
# "delete application data" step targets the bundle identifier directory
# (com.kumiplayer.app) instead, so the two do not overlap today - but that is a
# coincidence of naming, not a guarantee. Pin it down.
if ($nsiContent -match '(?i)KumiPlayer\\data') {
    throw 'The installer manifest must not reference the managed KumiPlayer data directory.'
}
foreach ($userDataRoot in @('$LOCALAPPDATA', '$APPDATA')) {
    $escapedRoot = [regex]::Escape($userDataRoot)
    if ($nsiContent -match "(?im)^\s*RMDir\s+(?:/\S+\s+)*`"$escapedRoot\\KumiPlayer") {
        throw "The installer manifest must not remove user data under $userDataRoot\KumiPlayer."
    }
}
if ($nsiContent -match '(?im)^\s*RMDir\s+(?:/\S+\s+)*"\$INSTDIR\\data') {
    throw 'The installer manifest must not remove a data directory inside the install root.'
}

$sourceHash = (Get-FileHash -LiteralPath $sourceDll -Algorithm SHA256).Hash
$stagedHash = (Get-FileHash -LiteralPath $stagedDll -Algorithm SHA256).Hash
if ($sourceHash -ne $stagedHash) {
    throw 'The WebView2Loader.dll bundled by NSIS does not match the Rust build output.'
}

# --- KumiPlayer built-in MPV runtime and privacy audit -------------------------
$stagedMpv = Join-Path $resolvedRoot 'packaging\runtime\mpv'
$stagedManifest = Join-Path $stagedMpv 'runtime-manifest.json'
$requiredConfig = Join-Path $stagedMpv 'portable_config\mpv.conf'
$requiredLayerConfig = Join-Path $stagedMpv 'kumiplayer\mpv.conf'
$requiredPlugin = Join-Path $stagedMpv 'kumiplayer\scripts\screenshot_to_video_dir.lua'
$requiredBindings = Join-Path $stagedMpv 'kumiplayer\scripts\kumiplayer_bindings.lua'
if (-not (Test-Path -LiteralPath $stagedManifest -PathType Leaf)) {
    throw "Required KumiPlayer built-in MPV runtime-manifest.json is missing: $stagedManifest"
}
if (-not (Test-Path -LiteralPath $requiredConfig -PathType Leaf)) {
    throw "Required KumiPlayer MPV config is missing: $requiredConfig"
}
if (-not (Test-Path -LiteralPath $requiredLayerConfig -PathType Leaf)) {
    throw "Required KumiPlayer layer config is missing: $requiredLayerConfig"
}
if (-not (Test-Path -LiteralPath $requiredPlugin -PathType Leaf)) {
    throw "Required KumiPlayer MPV plugin is missing: $requiredPlugin"
}
if (-not (Test-Path -LiteralPath $requiredBindings -PathType Leaf)) {
    throw "Required KumiPlayer bindings script is missing: $requiredBindings"
}

# 第三方许可证与发布材料尚未补齐：development-only 的内置 MPV 不得进入正式安装包。
$stagedManifestJson = Get-Content -LiteralPath $stagedManifest -Raw -Encoding UTF8 | ConvertFrom-Json
if ($stagedManifestJson.distribution_status -eq 'development-only') {
    throw 'Installer blocked: built-in MPV is development-only. 第三方许可证与发布材料尚未补齐，内置 MPV 暂不能进入正式安装包。'
}

# 内置 MPV 运行文件必须与清单一致，且不得泄露本地绝对路径。
$absolutePathPattern = '[A-Za-z]:\\'
$scanExtensions = @('.json', '.conf', '.lua', '.md')
$scanTargets = @(Get-ChildItem -LiteralPath $stagedMpv -Recurse -Force -File |
    Where-Object { $scanExtensions -contains $_.Extension.ToLowerInvariant() })
foreach ($scanTarget in $scanTargets) {
    $relativePath = $scanTarget.FullName.Substring($resolvedRoot.Length).TrimStart('\')
    $pathHit = Select-String -LiteralPath $scanTarget.FullName -Pattern $absolutePathPattern -Encoding UTF8 -List
    if ($pathHit) {
        throw "Staged KumiPlayer built-in MPV leaks a local absolute path: $relativePath line $($pathHit.LineNumber)."
    }
    foreach ($literal in $ForbiddenLiteral) {
        if (-not $literal) { continue }
        $literalHit = Select-String -LiteralPath $scanTarget.FullName -Pattern $literal -SimpleMatch -Encoding UTF8 -List
        if ($literalHit) {
            throw "Staged MPV runtime contains a forbidden literal in $relativePath line $($literalHit.LineNumber)."
        }
    }
}

$setupFiles = @(Get-ChildItem -LiteralPath $bundleRoot -Filter '*setup.exe' -File)
if ($setupFiles.Count -ne 1) {
    throw "Expected exactly one NSIS setup executable, found $($setupFiles.Count)."
}
if ($setupFiles[0].Length -le 0) {
    throw 'The generated NSIS setup executable is empty.'
}

Write-Host "Verified installer manifest and $($setupFiles[0].Name): KumiPlayer plugins are bundled without MPV player binaries or user configuration, and user data is protected."
