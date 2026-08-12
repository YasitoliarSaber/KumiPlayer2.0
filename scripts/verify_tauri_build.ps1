$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

if (-not (Test-Path -LiteralPath 'src-tauri\target\release\tauri-shell.exe' -PathType Leaf)) {
    Write-Error 'Missing Tauri executable.'
    exit 1
}
if (-not (Test-Path -LiteralPath 'src-tauri\target\release\WebView2Loader.dll' -PathType Leaf)) {
    Write-Error 'Missing WebView2Loader.dll.'
    exit 1
}

$assets = Get-ChildItem -Path 'dist\assets' -Filter '*.js' -File -ErrorAction SilentlyContinue
if ($null -eq $assets) {
    Write-Error 'No frontend JavaScript bundle was found.'
    exit 1
}

$hasDesktopApiUrl = $false
foreach ($asset in $assets) {
    if (Select-String -LiteralPath $asset.FullName -SimpleMatch 'http://127.0.0.1:37821' -Quiet) {
        $hasDesktopApiUrl = $true
        break
    }
}

if (-not $hasDesktopApiUrl) {
    Write-Error 'The frontend bundle does not target http://127.0.0.1:37821.'
    exit 1
}

Write-Host 'Desktop release verification passed.'
