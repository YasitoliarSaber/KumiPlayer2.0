param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$resolvedRoot = [IO.Path]::GetFullPath($ProjectRoot)
$runtimeSource = [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'third_party\mpv\runtime'))
$configSource = [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'resources\mpv-runtime\portable_config'))
$layerSource = [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'resources\mpv-runtime\kumiplayer'))
$manifestSource = [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'third_party\mpv\runtime-manifest.json'))
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'packaging\runtime'))
$target = [IO.Path]::GetFullPath((Join-Path $runtimeRoot 'mpv'))
$staging = [IO.Path]::GetFullPath((Join-Path $runtimeRoot '.mpv-runtime-staging'))
$obsoleteTargets = @(
    [IO.Path]::GetFullPath((Join-Path $runtimeRoot 'mpv-plugins'))
)

# 安全边界：只允许处理 packaging/runtime 下的固定暂存目标，绝不触碰开发或用户目录。
foreach ($candidate in @($target, $staging) + $obsoleteTargets) {
    if ((Split-Path -Parent $candidate) -ne $runtimeRoot) {
        throw "MPV runtime staging refused for an unexpected target: $candidate"
    }
}

if (-not (Test-Path -LiteralPath $runtimeSource -PathType Container)) {
    throw "MPV runtime staging refused: clean MPV runtime directory not found: $runtimeSource"
}
if (-not (Test-Path -LiteralPath (Join-Path $runtimeSource 'mpv.exe') -PathType Leaf)) {
    throw 'MPV runtime staging refused: mpv.exe missing from the clean runtime directory.'
}
if (-not (Test-Path -LiteralPath (Join-Path $configSource 'mpv.conf') -PathType Leaf)) {
    throw 'MPV runtime staging refused: KumiPlayer mpv.conf missing from the config source.'
}
if (-not (Test-Path -LiteralPath (Join-Path $layerSource 'mpv.conf') -PathType Leaf)) {
    throw 'MPV runtime staging refused: KumiPlayer forced mpv.conf missing from the kumiplayer layer.'
}
if (-not (Test-Path -LiteralPath (Join-Path $layerSource 'scripts') -PathType Container)) {
    throw 'MPV runtime staging refused: KumiPlayer layer scripts directory missing.'
}
if (-not (Test-Path -LiteralPath $manifestSource -PathType Leaf)) {
    throw 'MPV runtime staging refused: runtime-manifest.json missing.'
}

# 校验清单当前允许进入正式安装包，否则明确拦截。
$manifest = Get-Content -LiteralPath $manifestSource -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.distribution_status -eq 'development-only') {
    throw 'MPV runtime staging refused: distribution_status is development-only. 第三方许可证与发布材料尚未补齐，内置 MPV 暂不能进入正式安装包。'
}

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
if (Test-Path -LiteralPath $staging) {
    Remove-Item -LiteralPath $staging -Recurse -Force
}
New-Item -ItemType Directory -Path $staging -Force | Out-Null

# 1. 干净 MPV 运行文件（mpv.exe、mpv.com、DLL、bat），排除 README 说明文件
Get-ChildItem -LiteralPath $runtimeSource -Force |
    Where-Object { $_.Name -ne 'README.md' } |
    Copy-Item -Destination $staging -Recurse -Force

# 2. KumiPlayer 自有 portable_config（可替换层，用户可整体替换为整合包）
$stagedConfig = Join-Path $staging 'portable_config'
New-Item -ItemType Directory -Path $stagedConfig -Force | Out-Null
Get-ChildItem -LiteralPath $configSource -Force |
    Copy-Item -Destination $stagedConfig -Recurse -Force

# 2b. KumiPlayer 自有层（kumiplayer，不可替换：自有脚本 + 强制配置）
$stagedLayer = Join-Path $staging 'kumiplayer'
New-Item -ItemType Directory -Path $stagedLayer -Force | Out-Null
Get-ChildItem -LiteralPath $layerSource -Force |
    Copy-Item -Destination $stagedLayer -Recurse -Force

# 3. 实际 runtime-manifest.json
Copy-Item -LiteralPath $manifestSource -Destination (Join-Path $staging 'runtime-manifest.json')

# 移除旧的 mpv-plugins 暂存目标（仅暂存目录，不碰开发/用户 MPV）
foreach ($oldTarget in $obsoleteTargets) {
    if (Test-Path -LiteralPath $oldTarget) {
        Remove-Item -LiteralPath $oldTarget -Recurse -Force
    }
}
Move-Item -LiteralPath $staging -Destination $target

Write-Output "KumiPlayer built-in MPV runtime staged to: $target"
