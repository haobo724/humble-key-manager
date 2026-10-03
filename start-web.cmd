@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-web.ps1" %*
if errorlevel 1 (
    echo.
    echo Startup failed. See the error above.
    pause
)
