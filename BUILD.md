# Building the installers

Each installer has to be built on the operating system it is for.

| System  | Run this                                                             | You get                                   |
|---------|----------------------------------------------------------------------|-------------------------------------------|
| Windows | `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1` | `build\installer\RemoteDesktopSetup.exe`  |
| Linux   | `bash packaging/build_linux.sh`                                      | `build/installer/*.deb` and `*.tar.gz`    |
| macOS   | `bash packaging/build_macos.sh`                                      | `build/installer/*.dmg`                   |

Every system needs [uv](https://docs.astral.sh/uv/) (`pip install uv`, or
`winget install astral-sh.uv` / `curl -LsSf https://astral.sh/uv/install.sh | sh`).
uv fetches the Python version in `.python-version` and installs the exact package
versions in `uv.lock` into `.venv`. Windows also needs Inno Setup 6; Linux needs
`dpkg-dev curl`. Each script bundles Cloudflare's `cloudflared`, which provides
internet access.

## Running from source

```
uv sync
uv run app.py
```

(`start.bat` does the same on Windows, as administrator.) Dependencies live in
`pyproject.toml`: add one with `uv add <name>`, then commit `uv.lock`. PyInstaller is in
the `build` group, installed by `uv sync --group build`.

## Without a Linux or Mac computer

Put this folder in a GitHub repository and run the **Build installers** workflow
(`.github/workflows/build.yml`) from the repository's Actions tab. It builds all
three on GitHub's machines and offers them as downloads.

## Releases and automatic updates

1. Put this folder in a GitHub repository (public, so installed copies can read its Releases).
2. Push a version tag: `git tag v1.2.0` then `git push origin v1.2.0`.
3. The workflow builds all installers and publishes them as a GitHub Release, with
   `SHA256SUMS-*.txt` checksum files. `version.py` is stamped with the tag's version
   and the repository name, which is where installed copies look for updates.

The app checks about once a day (and with **Check for updates**). It downloads the
installer only after you agree, verifies its SHA-256 checksum against GitHub's, and on
Windows installs it silently and restarts. For a local build, set `REPO` in `version.py`.

## "Windows protected your PC" / "isn't commonly downloaded"

Windows SmartScreen and Edge/Chrome warn about any program that is **not code-signed**
or that few people have downloaded yet. Nothing in the program itself can turn this off;
the fix is to sign it:

- Get a code-signing certificate (for example Microsoft's Azure Trusted Signing, or an
  OV certificate from a certificate authority).
- Build with `-CertThumbprint <thumbprint>`, or set `SIGN_PFX` and `SIGN_PFX_PASSWORD`
  (on GitHub: secrets `SIGN_PFX_BASE64` and `SIGN_PFX_PASSWORD`). Both the app and
  `RemoteDesktopSetup.exe` are signed.

Signed, the publisher name replaces "Unknown publisher". The warning itself fades as the
signed installer builds up download reputation. Until then: click **More info** >
**Run anyway**, and in the browser's downloads list choose **Keep**.

## Status

- Windows: built and tested (install, run, uninstall).
- Linux (.deb, .tar.gz, x86-64) and macOS (.dmg, Apple Silicon): built by the GitHub
  workflow. Each build launches the finished app with `--selftest` (screen capture,
  mouse/keyboard backend, viewer page, icon, tunnel program, app window) and fails if
  anything is missing. Not yet used on a real Linux desktop or Mac by a person.

Run `RemoteDesktop --selftest` on any system to repeat those checks.

## Platform notes

- **Windows:** the installer puts the app in Program Files and (unless unticked) installs
  the **Remote Desktop Service**. Like RustDesk's, it runs as SYSTEM and keeps a small agent
  in the signed-in user's session that captures and controls whichever desktop is active,
  including UAC prompts on the secure desktop and the sign-in/lock screen. UAC stays fully
  on. The agent only accepts the (administrator) Remote Desktop app, on this computer.
  Elevated windows such as Task Manager also need the app itself to run as administrator.
- **Linux:** controlling the computer needs an X11 session; Wayland blocks injected input.
- **macOS:** the app is not code-signed, so open it the first time with right-click > Open.
  Allow it under System Settings > Privacy & Security > Screen Recording and > Accessibility.
- **Linux and macOS:** the saved password is kept in a settings file only your user can
  read; on Windows it is encrypted by Windows.
