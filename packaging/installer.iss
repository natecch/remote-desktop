; Inno Setup script for Remote Desktop. Installs for the current user only, so it
; needs no administrator rights. Built by packaging\build_windows.ps1.
#define AppName "Remote Desktop"
#define AppVersion "1.0.0"
#define AppExe "RemoteDesktop.exe"

[Setup]
AppId={{8F6B1C1E-5A0B-4E0B-9C55-2B7D1E2A9F31}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\RemoteDesktop
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build\installer
OutputBaseFilename=RemoteDesktopSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=..\assets\icon.ico
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\build\dist\RemoteDesktop\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; If the user turned on "Open the app when I sign in", remove that entry on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "RemoteDesktop"; ValueType: none; Flags: dontcreatekey uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent
