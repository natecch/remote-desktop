@echo off
rem Opens the Remote Desktop Host control panel with administrator rights.
cd /d "%~dp0"

rem Check if running as administrator
net session >nul 2>&1
if %errorLevel% == 0 (
    if exist ".venv\Scripts\pythonw.exe" (
        start "" ".venv\Scripts\pythonw.exe" app.py %*
    ) else (
        start "" pythonw.exe app.py %*
    )
    exit /b
)

rem Elevate via PowerShell if not already admin
set "PYEXE=.venv\Scripts\pythonw.exe"
if not exist "%PYEXE%" set "PYEXE=pythonw.exe"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Start-Process -FilePath '%~dp0%PYEXE%' -ArgumentList 'app.py %*' -Verb RunAs -WorkingDirectory '%~dp0'"
