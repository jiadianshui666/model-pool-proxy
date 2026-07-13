@echo off
setlocal

set "ROOT=%~dp0"
set "STATUS_URL=http://127.0.0.1:19190/"

echo ========================================
echo   Model Pool Proxy
echo ========================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%start_proxy.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] Proxy failed to start.
    pause
    exit /b 1
)

echo.
echo [OK] Proxy running on %STATUS_URL%
echo [OK] Opening status page...
start "" "%STATUS_URL%"

echo [OK] Starting Claude Code...
echo.
claude

echo.
echo Claude Code exited. Press any key to stop proxy...
pause >nul

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%stop_proxy.ps1"
echo [OK] Proxy stopped.
