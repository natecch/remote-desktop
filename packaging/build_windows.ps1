# Builds the Windows app and, if Inno Setup is installed, RemoteDesktopSetup.exe.
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Output: build\dist\RemoteDesktop\ (the app) and build\installer\RemoteDesktopSetup.exe
#
# Needs uv (https://docs.astral.sh/uv/): it installs the exact versions in uv.lock,
# with the Python in .python-version, into .venv.
#
# Code signing (removes "Unknown publisher" from Windows' warnings) happens when a
# certificate is given, either:
#   -CertThumbprint <sha1>          a certificate in your Windows certificate store, or
#   $env:SIGN_PFX / $env:SIGN_PFX_PASSWORD   a .pfx file and its password.
param(
    [string]$Cloudflared = "",   # path to cloudflared.exe to bundle (default: the installed one)
    [string]$CertThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = @("$env:APPDATA\Python\Python315\Scripts\uv.exe", "$env:USERPROFILE\.local\bin\uv.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1 }
if (-not $uv) { throw "uv is not installed: pip install uv  (or see https://docs.astral.sh/uv/)" }
& $uv sync --frozen --group build
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
$Python = "$root\.venv\Scripts\python.exe"

$version = (& $Python -c "import version; print(version.VERSION)").Trim()
$parts = ($version.Split(".") + @("0", "0", "0"))[0..3] | ForEach-Object { [int]($_ -replace '\D', '') }
Write-Host "Version $version"

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

# Name, description and version shown in Task Manager and the file's Properties.
New-Item -ItemType Directory -Force "$root\build" | Out-Null
$tuple = $parts -join ", "
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($tuple), prodvers=($tuple)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Remote Desktop'),
      StringStruct('FileDescription', 'Remote Desktop'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'RemoteDesktop'),
      StringStruct('OriginalFilename', 'RemoteDesktop.exe'),
      StringStruct('ProductName', 'Remote Desktop'),
      StringStruct('ProductVersion', '$version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Out-File -Encoding utf8 "$root\build\version_info.txt"

& $Python -m PyInstaller --noconfirm --clean --windowed --name RemoteDesktop `
    --icon "$root\assets\icon.ico" --version-file "$root\build\version_info.txt" `
    --add-data "$root\viewer.html;." --add-data "$root\assets\icon-256.png;assets" `
    --add-data "$root\assets\icon.ico;assets" `
    --add-binary "$Cloudflared;bin" `
    --hidden-import mss.windows --collect-submodules websockets `
    --collect-submodules dxcam --hidden-import comtypes.stream --hidden-import pystray._win32 `
    --collect-data soundcard --hidden-import soundcard.mediafoundation `
    --collect-submodules aiortc --collect-submodules aioice --collect-submodules dns `
    --collect-binaries google_crc32c --collect-binaries pylibsrtp --collect-submodules av `
    --distpath "$root\build\dist" --workpath "$root\build\work" --specpath "$root\build" app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "App built: $root\build\dist\RemoteDesktop\RemoteDesktop.exe"

# ---- code signing (optional) ----
$signtool = $null
if ($CertThumbprint -or $env:SIGN_PFX) {
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    if (-not $signtool) { throw "signtool.exe not found: install the Windows SDK (winget install Microsoft.WindowsSDK.10.0.26100)" }
}
function Sign-File($path) {
    if (-not $signtool) { return }
    $sargs = @("sign", "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256")
    if ($CertThumbprint) { $sargs += @("/sha1", $CertThumbprint) }
    else { $sargs += @("/f", $env:SIGN_PFX); if ($env:SIGN_PFX_PASSWORD) { $sargs += @("/p", $env:SIGN_PFX_PASSWORD) } }
    & $signtool @sargs $path
    if ($LASTEXITCODE -ne 0) { throw "Signing $path failed" }
}
Sign-File "$root\build\dist\RemoteDesktop\RemoteDesktop.exe"

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc /Qp "/DAppVersion=$version" "$root\packaging\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    $setup = "$root\build\installer\RemoteDesktopSetup.exe"
    Sign-File $setup
    $hash = (Get-FileHash -Algorithm SHA256 $setup).Hash.ToLower()
    "$hash  RemoteDesktopSetup.exe" | Out-File -Encoding ascii "$root\build\installer\SHA256SUMS-windows.txt"
    Write-Host "Installer built: $setup"
    if (-not $signtool) { Write-Host "Not code-signed: Windows will show 'Unknown publisher' (see BUILD.md)." }
} else {
    Write-Host "Inno Setup 6 is not installed, so no Setup.exe was made (winget install JRSoftware.InnoSetup)."
}
