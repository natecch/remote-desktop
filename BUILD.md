# Building the installers

Each installer has to be built on the operating system it is for.

| System  | Run this                                                             | You get                                   |
|---------|----------------------------------------------------------------------|-------------------------------------------|
| Windows | `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1` | `build\installer\RemoteDesktopSetup.exe`  |
| Linux   | `bash packaging/build_linux.sh`                                      | `build/installer/*.deb` and `*.tar.gz`    |
| macOS   | `bash packaging/build_macos.sh`                                      | `build/installer/*.dmg`                   |

Windows needs Python 3 and Inno Setup 6. Linux needs
`python3-venv python3-tk python3-dev dpkg-dev curl`. macOS needs Python 3 with Tk.
Each script bundles Cloudflare's `cloudflared`, which provides internet access.

## Without a Linux or Mac computer

Put this folder in a GitHub repository and run the **Build installers** workflow
(`.github/workflows/build.yml`) from the repository's Actions tab. It builds all
three on GitHub's machines and offers them as downloads.

## Status

- Windows: built and tested (install, run, uninstall).
- Linux (.deb, .tar.gz, x86-64) and macOS (.dmg, Apple Silicon): built by the GitHub
  workflow. Each build launches the finished app with `--selftest` (screen capture,
  mouse/keyboard backend, viewer page, icon, tunnel program, app window) and fails if
  anything is missing. Not yet used on a real Linux desktop or Mac by a person.

Run `RemoteDesktop --selftest` on any system to repeat those checks.

## Platform notes

- **Windows:** Remote control of UAC prompts and elevated windows (Task Manager, installers)
  requires the app to run as Administrator and moves UAC prompts off the Secure Desktop.
  Use `start.bat` (which auto-elevates) or run `fix_uac.bat` to disable the Secure Desktop.
- **Linux:** controlling the computer needs an X11 session; Wayland blocks injected input.
- **macOS:** the app is not code-signed, so open it the first time with right-click > Open.
  Allow it under System Settings > Privacy & Security > Screen Recording and > Accessibility.
- **Linux and macOS:** the saved password is kept in a settings file only your user can
  read; on Windows it is encrypted by Windows.
