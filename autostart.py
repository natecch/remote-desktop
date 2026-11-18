"""Open the app when the user signs in to the computer (per user, no admin rights).

Windows: a value under HKCU\\...\\Run.  macOS: a LaunchAgent.  Linux: an XDG autostart entry.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

NAME = "RemoteDesktop"
LABEL = "com.remotedesktop.app"


def command():
    """The command that starts this app minimised, packaged or run from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--minimized"]
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    if sys.platform == "win32" and windowless.exists():
        python = windowless
    return [str(python), str(Path(__file__).resolve().parent / "app.py"), "--minimized"]


def _mac_plist():
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _linux_entry():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / "remote-desktop.desktop"


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# A sign-in entry in the Run key cannot start a program as administrator, so the
# elevated variant is a scheduled task with "run with highest privileges".
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{exe}</Command><Arguments>{args}</Arguments></Exec></Actions>
</Task>
"""


def _schtasks(*args):
    return subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _task_exists():
    return _schtasks("/Query", "/TN", NAME).returncode == 0


def _run_value(enabled, cmd):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
        else:
            try:
                winreg.DeleteValue(key, NAME)
            except FileNotFoundError:
                pass


def _set_windows(enabled, elevated, cmd):
    if enabled and elevated:
        import tempfile
        from xml.sax.saxutils import escape
        user = f"{os.environ.get('USERDOMAIN', '.')}\\{os.environ.get('USERNAME', '')}"
        xml = TASK_XML.format(user=escape(user), exe=escape(cmd[0]),
                              args=escape(subprocess.list2cmdline(cmd[1:])))
        with tempfile.NamedTemporaryFile("w", suffix=".xml", encoding="utf-16", delete=False) as f:
            f.write(xml)
        try:
            result = _schtasks("/Create", "/F", "/TN", NAME, "/XML", f.name)
        finally:
            os.unlink(f.name)
        if result.returncode != 0:
            raise OSError((result.stderr or result.stdout).strip() or "Could not create the startup task.")
        _run_value(False, cmd)
        return
    if _task_exists():  # left from when the app ran as administrator
        result = _schtasks("/Delete", "/F", "/TN", NAME)
        if result.returncode != 0:
            raise OSError("This startup entry was created as administrator. "
                          "Restart the app as administrator to change it.")
    _run_value(enabled, cmd)


def is_enabled():
    try:
        if sys.platform == "win32":
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                    winreg.QueryValueEx(key, NAME)
                return True
            except OSError:
                return _task_exists()
        return (_mac_plist() if sys.platform == "darwin" else _linux_entry()).exists()
    except OSError:
        return False


def set_enabled(enabled, elevated=False):
    """Turn start-at-sign-in on or off. `elevated` (Windows) starts it as administrator.
    Raises OSError if it cannot be changed."""
    cmd = command()
    if sys.platform == "win32":
        _set_windows(enabled, elevated, cmd)
    elif sys.platform == "darwin":
        path = _mac_plist()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                plistlib.dump({"Label": LABEL, "ProgramArguments": cmd, "RunAtLoad": True}, f)
        else:
            path.unlink(missing_ok=True)
    else:
        path = _linux_entry()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            quoted = " ".join('"' + part.replace('"', '\\"') + '"' for part in cmd)
            path.write_text("[Desktop Entry]\nType=Application\nName=Remote Desktop\n"
                            f"Exec={quoted}\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
