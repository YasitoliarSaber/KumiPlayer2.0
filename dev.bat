@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM build_tauri.bat cleans node_modules as a regenerable cache after build;
REM this self-heals by reinstalling deps when node_modules is missing.
if not exist "node_modules" (
    echo [dev] node_modules missing, installing frontend dependencies...
    call npm install
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] npm install failed.
        pause
        exit /b 1
    )
)
npm run tauri dev
