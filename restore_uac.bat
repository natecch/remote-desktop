@echo off
rem Restores Windows UAC prompts back to the default Secure Desktop.
cd /d "%~dp0"

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Restoring Windows UAC defaults...
rem Back to prompting the administrator for consent, on the secure desktop.
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v "ConsentPromptBehaviorAdmin" /t REG_DWORD /d 5 /f >nul
if %errorlevel% neq 0 (
    echo [ERROR] Failed to update registry. Please run as administrator.
    pause
    exit /b %errorlevel%
)
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v "PromptOnSecureDesktop" /t REG_DWORD /d 1 /f >nul

echo.
echo ==============================================================================
echo [SUCCESS] The normal UAC prompt and secure desktop have been restored.
echo ==============================================================================
echo.
pause
