@echo off
rem Fixes Remote Desktop losing control when UAC prompts appear
rem by moving UAC prompts off the Secure Desktop to the normal desktop.
cd /d "%~dp0"

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Configuring Windows UAC for remote administration...
rem Elevate the signed-in administrator without a UAC prompt. A UAC prompt can be
rem seen remotely but never clicked (Windows blocks injected input to it), so the
rem only way to use admin tasks remotely is to stop Windows showing the prompt.
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v "ConsentPromptBehaviorAdmin" /t REG_DWORD /d 0 /f >nul
if %errorlevel% neq 0 (
    echo [ERROR] Failed to update registry. Please run as administrator.
    pause
    exit /b %errorlevel%
)

rem Keep any prompt that does appear (e.g. a non-admin UAC) off the secure desktop.
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v "PromptOnSecureDesktop" /t REG_DWORD /d 0 /f >nul

echo.
echo ==============================================================================
echo [SUCCESS] Admin actions on this PC will now run WITHOUT a UAC prompt.
echo Remote Desktop can now run admin tasks remotely without getting stuck on UAC.
echo.
echo This is less secure: any admin action elevates without asking.
echo Run restore_uac.bat to put the normal UAC prompt back.
echo ==============================================================================
echo.
pause
