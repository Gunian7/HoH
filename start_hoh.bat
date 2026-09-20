@echo off
title HoH Control Plane
color 0A

echo ===================================================
echo           HoH (Harness of Harness) Control Plane
echo ===================================================
echo.

set PYTHONPATH=F:\Tools\HOH
set PYTHON_EXE=F:\hermes\hermes-agent\venv-win\Scripts\python.exe
set HOH_DIR=F:\Tools\HOH

cd /d "%HOH_DIR%"

echo Checking existing service on port 8765...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

echo Starting HoH API Service on 127.0.0.1:8765 ...
echo Launching Web Dashboard in browser...
start http://127.0.0.1:8765/

echo.
echo Service running. Close this window or press Ctrl+C to stop.
echo.

"%PYTHON_EXE%" -m hoh.cli serve --host 127.0.0.1 --port 8765

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Service exited with code: %ERRORLEVEL%
    pause
)
