"""Mouse/keyboard injection for Linux and macOS, using pynput.

Linux needs an X11 session (Wayland blocks synthetic input). macOS needs the
app to be allowed under System Settings > Privacy & Security > Accessibility
(for control) and Screen Recording (for the picture).
"""

from pynput import keyboard, mouse

_mouse = mouse.Controller()
_keyboard = keyboard.Controller()
Key = keyboard.Key

# JS MouseEvent.button -> pynput button
BUTTONS = {0: mouse.Button.left, 1: mouse.Button.middle, 2: mouse.Button.right}

_NAMED = {
    "Escape": "esc", "Backspace": "backspace", "Tab": "tab", "Enter": "enter", "NumpadEnter": "enter",
    "ControlLeft": "ctrl_l", "ControlRight": "ctrl_r", "ShiftLeft": "shift_l", "ShiftRight": "shift_r",
    "AltLeft": "alt_l", "AltRight": "alt_r", "MetaLeft": "cmd", "MetaRight": "cmd_r",
    "Space": "space", "CapsLock": "caps_lock", "ArrowUp": "up", "ArrowDown": "down",
    "ArrowLeft": "left", "ArrowRight": "right", "Home": "home", "End": "end",
    "PageUp": "page_up", "PageDown": "page_down", "Insert": "insert", "Delete": "delete",
    "ContextMenu": "menu", "PrintScreen": "print_screen", "Pause": "pause",
    "NumLock": "num_lock", "ScrollLock": "scroll_lock",
    **{f"F{i}": f"f{i}" for i in range(1, 13)},
}
# Not every key exists on every platform (macOS has no Insert, for example).
SPECIAL = {code: getattr(Key, name) for code, name in _NAMED.items() if hasattr(Key, name)}

CHARS = {
    "Minus": "-", "Equal": "=", "BracketLeft": "[", "BracketRight": "]", "Backslash": "\\",
    "Semicolon": ";", "Quote": "'", "Backquote": "`", "Comma": ",", "Period": ".", "Slash": "/",
    "NumpadMultiply": "*", "NumpadAdd": "+", "NumpadSubtract": "-", "NumpadDecimal": ".",
    "NumpadDivide": "/", "IntlBackslash": "\\",
}


def _resolve(code):
    if code in SPECIAL:
        return SPECIAL[code]
    if len(code) == 4 and code.startswith("Key"):
        return code[3].lower()
    if len(code) == 6 and code.startswith("Digit"):
        return code[5]
    if len(code) == 7 and code.startswith("Numpad") and code[6].isdigit():
        return code[6]
    return CHARS.get(code)


def key_event(code, down):
    key = _resolve(code)
    if key is None:
        return False
    (_keyboard.press if down else _keyboard.release)(key)
    return True


def type_text(text):
    _keyboard.type(text.replace("\r\n", "\n"))


def mouse_move(monitor, nx, ny):
    """Move to a position given as 0..1 fractions of the given monitor."""
    nx = min(max(float(nx), 0.0), 1.0)
    ny = min(max(float(ny), 0.0), 1.0)
    _mouse.position = (round(monitor["left"] + nx * (monitor["width"] - 1)),
                       round(monitor["top"] + ny * (monitor["height"] - 1)))


def mouse_button(button, down):
    target = BUTTONS.get(button)
    if target:
        (_mouse.press if down else _mouse.release)(target)


_scroll_rest = [0.0, 0.0]  # browsers send pixels; pynput wants whole notches


def mouse_wheel(dx, dy):
    steps = []
    for i, delta in enumerate((float(dx), -float(dy))):  # browser +Y is down, pynput +Y is up
        _scroll_rest[i] += max(-2400.0, min(2400.0, delta)) / 100.0
        whole = int(_scroll_rest[i])
        _scroll_rest[i] -= whole
        steps.append(whole)
    if any(steps):
        _mouse.scroll(steps[0], steps[1])


def cursor_info():
    """(x, y, css_shape, visible). The pointer's shape is not available here."""
    try:
        x, y = _mouse.position
    except Exception:
        return None
    return x, y, "default", True


# --------------------------------------------------------------------------
# Clipboard text, through the system's command-line tools
# --------------------------------------------------------------------------

import shutil
import subprocess
import sys
import time

if sys.platform == "darwin":
    _READ, _WRITE = ["pbpaste"], ["pbcopy"]
elif shutil.which("xclip"):
    _READ, _WRITE = ["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"]
elif shutil.which("xsel"):
    _READ, _WRITE = ["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"]
else:
    _READ = _WRITE = None  # Linux without xclip/xsel: no clipboard sharing

_clip = {"seq": 0, "text": None, "checked": 0.0}


def clipboard_get():
    if not _READ:
        return None
    try:
        out = subprocess.run(_READ, capture_output=True, timeout=2)
        return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def clipboard_seq():
    """A number that changes when the clipboard text changes (checked about once a second)."""
    now = time.monotonic()
    if _READ and now - _clip["checked"] >= 1.0:
        _clip["checked"] = now
        text = clipboard_get()
        if text != _clip["text"]:
            _clip["text"] = text
            _clip["seq"] += 1
    return _clip["seq"]


def clipboard_set(text):
    if not _WRITE:
        return False
    try:
        ok = subprocess.run(_WRITE, input=text.encode("utf-8"), timeout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    if ok:
        _clip["text"] = text
    return ok
