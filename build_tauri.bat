@echo off
setlocal EnableExtensions
chcp 65001 >nul
echo ============================================
echo KumiPlayer Tauri desktop build
echo ============================================

cd /d "%~dp0"

REM Refuse active instances and safely stop only a verified orphaned backend.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\ensure_kumiplayer_runtime_stopped.ps1"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] KumiPlayer runtime preflight failed.
    pause
    exit /b 1
)

REM Check Rust.
where rustc >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Rust toolchain was not found.
    echo Install it from https://rustup.rs.
    pause
    exit /b 1
)

REM Install frontend dependencies.
echo.
echo [1/5] Checking frontend dependencies...
if not exist "package.json" (
    echo [ERROR] package.json was not found. Run this script from the project root.
    pause
    exit /b 1
)
if not exist "node_modules" (
    call npm install
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] npm install failed.
        pause
        exit /b 1
    )
) else (
    echo node_modules exists. Skipping npm install.
)

echo.
echo [2/5] Running TypeScript checks...

REM Inject build provenance (Git SHA / branch / build time) into the Vite build.
REM These values are read at build time only; no runtime git/network calls.
for /f "delims=" %%G in ('git rev-parse HEAD 2^>nul') do set "KUMI_BUILD_SHA=%%G"
for /f "delims=" %%G in ('git branch --show-current 2^>nul') do set "KUMI_BUILD_BRANCH=%%G"
if not defined KUMI_BUILD_SHA set "KUMI_BUILD_SHA=unknown"
if not defined KUMI_BUILD_BRANCH set "KUMI_BUILD_BRANCH=unknown"
REM ISO 8601 local build timestamp, e.g. 2026-08-18T14:30
for /f "delims=" %%G in ('powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-ddTHH:mm'"') do set "KUMI_BUILD_TIME=%%G"
echo Build provenance: branch=%KUMI_BUILD_BRANCH% sha=%KUMI_BUILD_SHA% time=%KUMI_BUILD_TIME%

call npx tsc --noEmit
if %ERRORLEVEL% neq 0 (
    echo [ERROR] TypeScript checks failed.
    pause
    exit /b 1
)

REM Build Tauri (includes frontend and Rust compilation).
echo.
echo [3/5] Building Tauri desktop app...
echo Raw executable only; MSI and NSIS installers are skipped.
call npm run tauri build -- --no-bundle
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Tauri build failed.
    pause
    exit /b 1
)

echo.
echo [4/5] Verifying desktop release files...
if not exist "src-tauri\target\release\tauri-shell.exe" (
    echo [ERROR] Tauri executable was not generated.
    pause
    exit /b 1
)
if not exist "src-tauri\target\release\WebView2Loader.dll" (
    echo [ERROR] WebView2Loader.dll was not generated.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\verify_tauri_build.ps1"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Frontend bundle does not include desktop backend URL 127.0.0.1:37821.
    pause
    exit /b 1
)

REM Stage both runtime files before replacing the release files.
if exist "release\.staging" rmdir /s /q "release\.staging"
mkdir "release\.staging"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Could not create the staging release directory.
    pause
    exit /b 1
)
copy /y "src-tauri\target\release\tauri-shell.exe" "release\.staging\KumiPlayer.exe" >nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Could not copy the executable to the staging directory.
    rmdir /s /q "release\.staging"
    pause
    exit /b 1
)
copy /y "src-tauri\target\release\WebView2Loader.dll" "release\.staging\WebView2Loader.dll" >nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Could not copy WebView2Loader.dll to the staging directory.
    rmdir /s /q "release\.staging"
    pause
    exit /b 1
)
if not exist "release" mkdir "release"
move /y "release\.staging\KumiPlayer.exe" "release\KumiPlayer.exe" >nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Could not replace release\KumiPlayer.exe. Close KumiPlayer and build again.
    rmdir /s /q "release\.staging"
    pause
    exit /b 1
)
move /y "release\.staging\WebView2Loader.dll" "release\WebView2Loader.dll" >nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Could not replace release\WebView2Loader.dll.
    rmdir /s /q "release\.staging"
    pause
    exit /b 1
)
rmdir /s /q "release\.staging"
if not exist "release\KumiPlayer.exe" (
    echo [ERROR] Release executable was not written.
    pause
    exit /b 1
)
if not exist "release\WebView2Loader.dll" (
    echo [ERROR] Release WebView2 runtime was not written.
    pause
    exit /b 1
)

echo Executable: release\KumiPlayer.exe
echo Required runtime: release\WebView2Loader.dll
if /I "%KUMIPLAYER_KEEP_BUILD_CACHE%"=="1" (
    echo Rust build cache retained because KUMIPLAYER_KEEP_BUILD_CACHE=1.
) else (
    echo Removing regenerable Rust and frontend dependency caches...
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\cleanup_build_cache.ps1"
    if %ERRORLEVEL% neq 0 (
        echo [WARNING] Release succeeded, but one or more build caches could not be removed.
    )
)
echo.
echo You can now run release\KumiPlayer.exe.
pause
endlocal
