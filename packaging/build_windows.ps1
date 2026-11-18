# Builds the Windows app and, if Inno Setup is installed, RemoteDesktopSetup.exe.
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Output: build\dist\RemoteDesktop\ (the app) and build\installer\RemoteDesktopSetup.exe
param(
    [string]$Python = "",        # Python to build with (default: the project's .venv, else "python")
    [string]$Cloudflared = ""    # path to cloudflared.exe to bundle (default: the installed one)
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Python) {
    $Python = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { "python" }
}
& $Python -m pip install --quiet -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not $Cloudflared) {
    $Cloudflared = @("${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe", "$env:ProgramFiles\cloudflared\cloudflared.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Cloudflared) {
    # Not installed on this machine (for example a CI runner): fetch Cloudflare's official build.
    New-Item -ItemType Directory -Force "$root\build\bin" | Out-Null
    $Cloudflared = "$root\build\bin\cloudflared.exe"
    Invoke-WebRequest -UseBasicParsing -OutFile $Cloudflared `
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
}

& $Python -m PyInstaller --noconfirm --clean --windowed --name RemoteDesktop `
    --uac-admin --icon "$root\assets\icon.ico" `
    --add-data "$root\viewer.html;." --add-data "$root\assets\icon-256.png;assets" `
    --add-binary "$Cloudflared;bin" `
    --hidden-import mss.windows --collect-submodules websockets `
    --collect-submodules dxcam --hidden-import comtypes.stream `
    --distpath "$root\build\dist" --workpath "$root\build\work" --specpath "$root\build" app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "App built: $root\build\dist\RemoteDesktop\RemoteDesktop.exe"

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc /Qp "$root\packaging\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "Installer built: $root\build\installer\RemoteDesktopSetup.exe"
} else {
    Write-Host "Inno Setup 6 is not installed, so no Setup.exe was made (winget install JRSoftware.InnoSetup)."
}
