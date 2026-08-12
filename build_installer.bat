@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo KumiPlayer complete Windows installer build
echo ============================================
echo This build never copies the project data directory.

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\ensure_kumiplayer_runtime_stopped.ps1"
if errorlevel 1 exit /b 1

where python >nul 2>&1 || (echo [ERROR] Python 3.12 is required on the build machine only. & exit /b 1)
where cargo >nul 2>&1 || (echo [ERROR] Rust is required on the build machine only. & exit /b 1)
where npm >nul 2>&1 || (echo [ERROR] Node.js is required on the build machine only. & exit /b 1)

echo [1/9] Building the self-contained Python backend...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\build_backend_sidecar.ps1"
if errorlevel 1 exit /b 1
if not exist "packaging\runtime\backend\KumiPlayerBackend.exe" (
    echo [ERROR] KumiPlayerBackend.exe was not generated.
    exit /b 1
)

echo [2/9] Staging KumiPlayer built-in MPV runtime and portable configuration...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\stage_mpv_runtime.ps1"
if errorlevel 1 exit /b 1

echo [3/9] Checking frontend dependencies...
if not exist "node_modules" call npm install
if errorlevel 1 exit /b 1

echo [4/9] Running TypeScript checks...
call npx tsc --noEmit
if errorlevel 1 exit /b 1

echo [5/9] Building the desktop shell and its native loader...
cargo build --release --manifest-path "src-tauri\Cargo.toml"
if errorlevel 1 exit /b 1
if not exist "src-tauri\target\release\WebView2Loader.dll" (
    echo [ERROR] Rust did not generate WebView2Loader.dll.
    exit /b 1
)

echo [6/9] Staging the native WebView2 loader for NSIS...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\stage_tauri_runtime.ps1"
if errorlevel 1 exit /b 1

echo [7/9] Building the NSIS installer...
call npm run tauri build -- --config src-tauri\tauri.installer.conf.json --bundles nsis
if errorlevel 1 exit /b 1

echo [8/9] Verifying installer contents...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\verify_installer_bundle.ps1"
if errorlevel 1 exit /b 1

echo [9/9] Publishing release\KumiPlayer-Setup.exe...
if not exist "release\.staging" mkdir "release\.staging"
for %%F in ("src-tauri\target\release\bundle\nsis\*setup.exe") do copy /y "%%~fF" "release\.staging\KumiPlayer-Setup.exe" >nul
if not exist "release\.staging\KumiPlayer-Setup.exe" (
    echo [ERROR] The NSIS installer was not generated.
    exit /b 1
)
move /y "release\.staging\KumiPlayer-Setup.exe" "release\KumiPlayer-Setup.exe" >nul
if errorlevel 1 (
    echo [ERROR] Could not replace release\KumiPlayer-Setup.exe.
    exit /b 1
)

echo Complete installer published as release\KumiPlayer-Setup.exe
echo End users do not need Python, Node.js, or Rust. KumiPlayer includes its managed MPV runtime and portable configuration.
endlocal
