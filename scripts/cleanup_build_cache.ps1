param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$resolvedRoot = [IO.Path]::GetFullPath($ProjectRoot)
$resolvedRootPrefix = $resolvedRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not (Test-Path -LiteralPath (Join-Path $resolvedRoot 'package.json') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $resolvedRoot 'src-tauri') -PathType Container)) {
    throw 'Cleanup refused: the path is not the KumiPlayer project root.'
}

# User works, artwork, caches, credentials, and player runtime data are never build caches.
$protectedRoots = @(
    [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'data'));
    [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'mpv'));
    [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'release'))
)

function Assert-WorkspacePath {
    param(
        [string]$Path,
        [string]$ExpectedLeaf = ''
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($resolvedRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Cleanup refused outside the project root: $resolved"
    }
    if ($ExpectedLeaf -and (Split-Path -Leaf $resolved) -ne $ExpectedLeaf) {
        throw "Cleanup refused for a mismatched path name: $resolved"
    }
    foreach ($protectedRoot in $protectedRoots) {
        $protectedPrefix = $protectedRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        if ($resolved.Equals($protectedRoot, [StringComparison]::OrdinalIgnoreCase) -or
            $resolved.StartsWith($protectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Cleanup refused for protected user/runtime data: $resolved"
        }
    }
    return $resolved
}

$recursiveTargets = @(
    [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'src-tauri\target'))
    [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'node_modules'))
    [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'src-tauri\gen'))
    [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'src-tauri\icons\android'))
    [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'src-tauri\icons\ios'))
)

foreach ($target in $recursiveTargets) {
    Assert-WorkspacePath -Path $target | Out-Null
}

foreach ($target in $recursiveTargets) {
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

$cacheSearchRoots = @(
    (Join-Path $resolvedRoot 'backend')
    (Join-Path $resolvedRoot 'scripts')
)
foreach ($searchRoot in $cacheSearchRoots) {
    if (-not (Test-Path -LiteralPath $searchRoot -PathType Container)) {
        continue
    }
    foreach ($cacheDir in Get-ChildItem -LiteralPath $searchRoot -Directory -Filter '__pycache__' -Recurse) {
        $cachePath = Assert-WorkspacePath -Path $cacheDir.FullName -ExpectedLeaf '__pycache__'
        Remove-Item -LiteralPath $cachePath -Recurse -Force
    }
}

$generatedFiles = @(
    (Get-ChildItem -LiteralPath $resolvedRoot -File -Filter '*.tsbuildinfo')
    (Get-Item -LiteralPath (Join-Path $resolvedRoot 'vite.config.d.ts') -ErrorAction SilentlyContinue)
    (Get-Item -LiteralPath (Join-Path $resolvedRoot 'src-tauri\icons\64x64.png') -ErrorAction SilentlyContinue)
    (Get-Item -LiteralPath (Join-Path $resolvedRoot 'src-tauri\icons\icon.png') -ErrorAction SilentlyContinue)
    (Get-Item -LiteralPath (Join-Path $resolvedRoot 'src-tauri\icons\StoreLogo.png') -ErrorAction SilentlyContinue)
    (Get-ChildItem -LiteralPath (Join-Path $resolvedRoot 'src-tauri\icons') -File -Filter 'Square*Logo.png' -ErrorAction SilentlyContinue)
) | Where-Object { $_ }
foreach ($file in $generatedFiles) {
    $filePath = Assert-WorkspacePath -Path $file.FullName
    Remove-Item -LiteralPath $filePath -Force
}

$knownEmptyDirectories = @(
    (Join-Path $resolvedRoot 'scripts\build')
    (Join-Path $resolvedRoot 'scripts\dev')
    (Join-Path $resolvedRoot 'src\lib')
)
foreach ($directory in $knownEmptyDirectories) {
    $directoryPath = Assert-WorkspacePath -Path $directory
    if ((Test-Path -LiteralPath $directoryPath -PathType Container) -and
        -not (Get-ChildItem -LiteralPath $directoryPath -Force)) {
        Remove-Item -LiteralPath $directoryPath -Force
    }
}

Write-Output 'Regenerable build and source caches removed.'
