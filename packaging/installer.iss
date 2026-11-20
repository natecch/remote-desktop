; Inno Setup script for Remote Desktop. Built by packaging\build_windows.ps1, which passes
; /DAppVersion=... from version.py.
;
; Installs for all users into Program Files (administrator), because the optional
; background service runs as SYSTEM: its program must sit where ordinary users cannot
; replace it.
#define AppName "Remote Desktop"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppExe "RemoteDesktop.exe"
#define ServiceName "RemoteDesktopService"

[Setup]
AppId={{8F6B1C1E-5A0B-4E0B-9C55-2B7D1E2A9F31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Remote Desktop
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\RemoteDesktop
DisableProgramGroupPage=yes
PrivilegesRequired=admin
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
RestartApplications=no

[Tasks]
Name: "service"; Description: "Install the background service (control UAC prompts and the sign-in screen remotely)"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\build\dist\RemoteDesktop\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "RemoteDesktop.App"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon; AppUserModelID: "RemoteDesktop.App"

[Registry]
; If the user turned on "Open the app when I sign in", remove that entry on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "RemoteDesktop"; ValueType: none; Flags: dontcreatekey uninsdeletevalue

[Run]
; Tasks that start the app with full rights and no UAC prompt (on demand, and at sign-in).
Filename: "{app}\{#AppExe}"; Parameters: "--setup-tasks"; StatusMsg: "Setting up..."; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Parameters: "--install-service"; StatusMsg: "Starting the background service..."; Flags: runhidden waituntilterminated; Tasks: service
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent
; After a silent update (started by the app itself), start it again quietly, in the tray.
Filename: "{app}\{#AppExe}"; Parameters: "--minimized"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--uninstall-service"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveService"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN RemoteDesktop"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveStartupTask"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""RemoteDesktop Launch"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveLaunchTask"
Filename: "{sys}
etsh.exe"; Parameters: "advfirewall firewall delete rule ""name=Remote Desktop"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveFirewallRule"

[Code]
// The service and its agent keep RemoteDesktop.exe open; stop them before files are replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop {#ServiceName}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(1500);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
