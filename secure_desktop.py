"""Optional, opt-in: let the signed-in administrator run elevated tasks remotely.

A UAC prompt (consent.exe) runs at a higher integrity level than this app, even
when the app itself runs as administrator, so Windows silently discards any click
the host injects at it. Moving the prompt off the secure desktop makes it visible
to a remote viewer but still does not make it clickable. The only way an ordinary
(non-service) remote tool lets you get past a UAC prompt is to stop Windows from
showing one: set ConsentPromptBehaviorAdmin = 0, "elevate without prompting", so
admin actions by the signed-in administrator just proceed.

That is a real security reduction - any admin action on this PC then elevates
without asking - so it is off by default and the user turns it on explicitly for
a machine they use for remote administration. Unticking it (or restore_uac.bat)
puts Windows back to its defaults.

These values live under an HKLM policy key, so changing them needs administrator
rights (which the host already needs to control elevated windows at all).
"""

import sys

POLICY_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
VALUE = "PromptOnSecureDesktop"
CONSENT_VALUE = "ConsentPromptBehaviorAdmin"
CONSENT_PROMPT = 5   # Windows default: ask for consent on the secure desktop
CONSENT_SILENT = 0   # elevate the admin without any prompt


def _hklm():
    import winreg
    return winreg.HKEY_LOCAL_MACHINE


def is_secure_desktop_on(root=None, key=POLICY_KEY, value=VALUE):
    """True when UAC uses the secure desktop (the Windows default).
    None when it cannot be read (not Windows, or no permission)."""
    if sys.platform != "win32":
        return None
    import winreg
    root = _hklm() if root is None else root
    try:
        with winreg.OpenKey(root, key) as handle:
            data, _ = winreg.QueryValueEx(handle, value)
            return bool(data)
    except FileNotFoundError:
        return True  # absent means the default, which is on
    except OSError:
        return None


def set_secure_desktop(enabled, root=None, key=POLICY_KEY, value=VALUE):
    """Turn the secure desktop on or off. Needs administrator rights.
    Raises OSError if the value cannot be written."""
    if sys.platform != "win32":
        raise OSError("Windows only")
    import winreg
    root = _hklm() if root is None else root
    with winreg.OpenKey(root, key, 0, winreg.KEY_SET_VALUE) as handle:
        winreg.SetValueEx(handle, value, 0, winreg.REG_DWORD, 1 if enabled else 0)


def get_admin_prompt(root=None, key=POLICY_KEY):
    """ConsentPromptBehaviorAdmin: 5 = prompt (default), 0 = silent. None if unreadable."""
    if sys.platform != "win32":
        return None
    import winreg
    root = _hklm() if root is None else root
    try:
        with winreg.OpenKey(root, key) as handle:
            data, _ = winreg.QueryValueEx(handle, CONSENT_VALUE)
            return int(data)
    except FileNotFoundError:
        return CONSENT_PROMPT
    except OSError:
        return None


def set_admin_prompt(prompt, root=None, key=POLICY_KEY):
    """prompt=True restores the normal UAC prompt (5); prompt=False elevates the admin
    silently with no prompt (0). Needs administrator rights."""
    if sys.platform != "win32":
        raise OSError("Windows only")
    import winreg
    root = _hklm() if root is None else root
    with winreg.OpenKey(root, key, 0, winreg.KEY_SET_VALUE) as handle:
        winreg.SetValueEx(handle, CONSENT_VALUE, 0, winreg.REG_DWORD,
                          CONSENT_PROMPT if prompt else CONSENT_SILENT)


def is_remote_admin_on(root=None, key=POLICY_KEY):
    """True when admin elevation is set to not prompt (remote-admin mode is on)."""
    return get_admin_prompt(root=root, key=key) == CONSENT_SILENT


def set_remote_admin(enabled, root=None, key=POLICY_KEY):
    """Turn remote-admin mode on or off. On: the admin elevates with no UAC prompt, and any
    prompt that does appear is on the normal desktop. Off: Windows defaults (prompt + secure
    desktop). Needs administrator rights; raises OSError if a value cannot be written."""
    set_admin_prompt(not enabled, root=root, key=key)
    set_secure_desktop(not enabled, root=root, key=key)
