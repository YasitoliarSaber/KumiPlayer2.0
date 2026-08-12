param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$resolvedRoot = [IO.Path]::GetFullPath($ProjectRoot)
$entrypoint = Join-Path $resolvedRoot 'backend\desktop_backend.py'
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'packaging\runtime'))
$target = [IO.Path]::GetFullPath((Join-Path $runtimeRoot 'backend'))
$distRoot = [IO.Path]::GetFullPath((Join-Path $resolvedRoot '.build\backend-sidecar-dist'))
$workRoot = [IO.Path]::GetFullPath((Join-Path $resolvedRoot '.build\backend-sidecar-work'))

if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
    throw 'Backend sidecar build refused: backend\desktop_backend.py was not found.'
}
if ((Split-Path -Leaf $target) -ne 'backend' -or (Split-Path -Parent $target) -ne $runtimeRoot) {
    throw "Backend sidecar build refused for an unexpected target: $target"
}

python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'The build machine is missing PyInstaller. Run: python -m pip install pyinstaller'
}

foreach ($generatedPath in @($distRoot, $workRoot)) {
    if (Test-Path -LiteralPath $generatedPath) {
        Remove-Item -LiteralPath $generatedPath -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

$arguments = @(
    '-m', 'PyInstaller',
    '--noconfirm', '--clean', '--onedir', '--noconsole',
    '--name', 'KumiPlayerBackend',
    '--paths', (Join-Path $resolvedRoot 'backend'),
    '--distpath', $distRoot,
    '--workpath', $workRoot,
    '--specpath', $workRoot,
    '--hidden-import', 'uvicorn.logging',
    '--hidden-import', 'uvicorn.loops.auto',
    '--hidden-import', 'uvicorn.protocols.http.auto',
    '--hidden-import', 'uvicorn.protocols.websockets.auto',
    $entrypoint
)
& python @arguments
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller backend sidecar build failed.'
}

$builtDirectory = Join-Path $distRoot 'KumiPlayerBackend'
$builtExecutable = Join-Path $builtDirectory 'KumiPlayerBackend.exe'
if (-not (Test-Path -LiteralPath $builtExecutable -PathType Leaf)) {
    throw 'PyInstaller completed without KumiPlayerBackend.exe.'
}
if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}
Move-Item -LiteralPath $builtDirectory -Destination $target
Remove-Item -LiteralPath $distRoot -Recurse -Force
Remove-Item -LiteralPath $workRoot -Recurse -Force
Write-Output "Backend sidecar staged: $target"
