"""Windows mouse/keyboard injection (SendInput) and cursor lookup for the host."""

import ctypes
from ctypes import wintypes

# Real pixels for capture and input, regardless of display scaling.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

user32 = ctypes.WinDLL("user32", use_last_error=True)

# --------------------------------------------------------------------------
# Input injection (SendInput)
# --------------------------------------------------------------------------

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# JS MouseEvent.button -> (down flag, up flag, mouseData)
BUTTONS = {
    0: (0x0002, 0x0004, 0),  # left
    1: (0x0020, 0x0040, 0),  # middle
    2: (0x0008, 0x0010, 0),  # right
    3: (0x0080, 0x0100, 1),  # back
    4: (0x0080, 0x0100, 2),  # forward
}

# JS KeyboardEvent.code -> (scan code, extended). Physical keys, so the host's
# own keyboard layout decides which character is produced.
SCAN = {
    "Escape": (0x01, 0), "Digit1": (0x02, 0), "Digit2": (0x03, 0), "Digit3": (0x04, 0),
    "Digit4": (0x05, 0), "Digit5": (0x06, 0), "Digit6": (0x07, 0), "Digit7": (0x08, 0),
    "Digit8": (0x09, 0), "Digit9": (0x0A, 0), "Digit0": (0x0B, 0), "Minus": (0x0C, 0),
    "Equal": (0x0D, 0), "Backspace": (0x0E, 0), "Tab": (0x0F, 0),
    "KeyQ": (0x10, 0), "KeyW": (0x11, 0), "KeyE": (0x12, 0), "KeyR": (0x13, 0),
    "KeyT": (0x14, 0), "KeyY": (0x15, 0), "KeyU": (0x16, 0), "KeyI": (0x17, 0),
    "KeyO": (0x18, 0), "KeyP": (0x19, 0), "BracketLeft": (0x1A, 0), "BracketRight": (0x1B, 0),
    "Enter": (0x1C, 0), "ControlLeft": (0x1D, 0),
    "KeyA": (0x1E, 0), "KeyS": (0x1F, 0), "KeyD": (0x20, 0), "KeyF": (0x21, 0),
    "KeyG": (0x22, 0), "KeyH": (0x23, 0), "KeyJ": (0x24, 0), "KeyK": (0x25, 0),
    "KeyL": (0x26, 0), "Semicolon": (0x27, 0), "Quote": (0x28, 0), "Backquote": (0x29, 0),
    "ShiftLeft": (0x2A, 0), "Backslash": (0x2B, 0),
    "KeyZ": (0x2C, 0), "KeyX": (0x2D, 0), "KeyC": (0x2E, 0), "KeyV": (0x2F, 0),
    "KeyB": (0x30, 0), "KeyN": (0x31, 0), "KeyM": (0x32, 0), "Comma": (0x33, 0),
    "Period": (0x34, 0), "Slash": (0x35, 0), "ShiftRight": (0x36, 0),
    "NumpadMultiply": (0x37, 0), "AltLeft": (0x38, 0), "Space": (0x39, 0), "CapsLock": (0x3A, 0),
    "F1": (0x3B, 0), "F2": (0x3C, 0), "F3": (0x3D, 0), "F4": (0x3E, 0), "F5": (0x3F, 0),
    "F6": (0x40, 0), "F7": (0x41, 0), "F8": (0x42, 0), "F9": (0x43, 0), "F10": (0x44, 0),
    "ScrollLock": (0x46, 0),
    "Numpad7": (0x47, 0), "Numpad8": (0x48, 0), "Numpad9": (0x49, 0), "NumpadSubtract": (0x4A, 0),
    "Numpad4": (0x4B, 0), "Numpad5": (0x4C, 0), "Numpad6": (0x4D, 0), "NumpadAdd": (0x4E, 0),
    "Numpad1": (0x4F, 0), "Numpad2": (0x50, 0), "Numpad3": (0x51, 0), "Numpad0": (0x52, 0),
    "NumpadDecimal": (0x53, 0), "IntlBackslash": (0x56, 0), "F11": (0x57, 0), "F12": (0x58, 0),
    "NumpadEnter": (0x1C, 1), "ControlRight": (0x1D, 1), "NumpadDivide": (0x35, 1),
    "AltRight": (0x38, 1), "Home": (0x47, 1), "ArrowUp": (0x48, 1), "PageUp": (0x49, 1),
    "ArrowLeft": (0x4B, 1), "ArrowRight": (0x4D, 1), "End": (0x4F, 1), "ArrowDown": (0x50, 1),
    "PageDown": (0x51, 1), "Insert": (0x52, 1), "Delete": (0x53, 1),
    "MetaLeft": (0x5B, 1), "MetaRight": (0x5C, 1), "ContextMenu": (0x5D, 1),
}

# Keys whose scan codes are irregular: send as virtual keys instead.
VKEYS = {"Pause": 0x13, "PrintScreen": 0x2C, "NumLock": 0x90}


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return inp


def _key(vk=0, scan=0, flags=0):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def mouse_move(monitor, nx, ny):
    """Move to a position given as 0..1 fractions of the given monitor."""
    nx = min(max(float(nx), 0.0), 1.0)
    ny = min(max(float(ny), 0.0), 1.0)
    px = monitor["left"] + nx * (monitor["width"] - 1)
    py = monitor["top"] + ny * (monitor["height"] - 1)
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    ax = round((px - vx) * 65535 / max(vw - 1, 1))
    ay = round((py - vy) * 65535 / max(vh - 1, 1))
    _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay))


def mouse_button(button, down):
    spec = BUTTONS.get(button)
    if spec:
        _send(_mouse(spec[0] if down else spec[1], data=spec[2]))


def mouse_wheel(dx, dy):
    # Browser: +deltaY scrolls down, ~100px per notch. Windows: +120 scrolls up.
    clamp = lambda v: int(max(-2400, min(2400, v)))
    if dy:
        _send(_mouse(MOUSEEVENTF_WHEEL, data=clamp(-float(dy) * 1.2)))
    if dx:
        _send(_mouse(MOUSEEVENTF_HWHEEL, data=clamp(float(dx) * 1.2)))


def key_event(code, down):
    up = 0 if down else KEYEVENTF_KEYUP
    if code in SCAN:
        scan, ext = SCAN[code]
        flags = KEYEVENTF_SCANCODE | up | (KEYEVENTF_EXTENDEDKEY if ext else 0)
        _send(_key(scan=scan, flags=flags))
        return True
    if code in VKEYS:
        _send(_key(vk=VKEYS[code], flags=up))
        return True
    return False


def type_text(text):
    """Type arbitrary Unicode text, independent of keyboard layout."""
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            _send(_key(vk=0x0D), _key(vk=0x0D, flags=KEYEVENTF_KEYUP))
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            unit = int.from_bytes(units[i:i + 2], "little")
            _send(_key(scan=unit, flags=KEYEVENTF_UNICODE),
                  _key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))


# --------------------------------------------------------------------------
# Cursor position and shape (so the viewer can draw the remote pointer)
# --------------------------------------------------------------------------

class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", ctypes.c_void_p), ("ptScreenPos", wintypes.POINT)]


user32.LoadCursorW.restype = ctypes.c_void_p
user32.LoadCursorW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
user32.GetCursorInfo.argtypes = (ctypes.POINTER(CURSORINFO),)

# Standard Windows cursors -> CSS cursor names.
_SHAPES = {32512: "default", 32513: "text", 32514: "wait", 32515: "crosshair", 32642: "nwse-resize",
           32643: "nesw-resize", 32644: "ew-resize", 32645: "ns-resize", 32646: "move",
           32648: "not-allowed", 32649: "pointer", 32650: "progress", 32651: "help"}
_HANDLES = {user32.LoadCursorW(None, ident): name for ident, name in _SHAPES.items()}


def cursor_info():
    """(x, y, css_shape, visible) of the pointer in screen pixels, or None."""
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(info)):
        return None
    return (info.ptScreenPos.x, info.ptScreenPos.y,
            _HANDLES.get(info.hCursor, "default"), bool(info.flags & 1))

# --------------------------------------------------------------------------
# Clipboard text (shared with the viewer during a session)
# --------------------------------------------------------------------------

import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)
user32.SetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)


def clipboard_seq():
    """A number that changes whenever the clipboard's contents change."""
    return user32.GetClipboardSequenceNumber()


def _open_clipboard():
    for _ in range(5):  # another program may hold it for a moment
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def clipboard_get():
    """The clipboard's text, or None when it holds no text."""
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT) or not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        pointer = kernel32.GlobalLock(handle) if handle else None
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def clipboard_set(text):
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        pointer = kernel32.GlobalLock(handle) if handle else None
        if not pointer:
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        return True  # the system owns the memory now
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------------------
# Files on the clipboard (copied in Explorer): copy/paste files between computers
# --------------------------------------------------------------------------

CF_HDROP = 15
shell32 = ctypes.WinDLL("shell32")
shell32.DragQueryFileW.argtypes = (ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
shell32.DragQueryFileW.restype = wintypes.UINT


def clipboard_files():
    """Paths of files copied to the clipboard (Explorer's Ctrl+C), or None."""
    if not user32.IsClipboardFormatAvailable(CF_HDROP) or not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_HDROP)
        if not handle:
            return None
        count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        paths = []
        for i in range(min(count, 1000)):
            length = shell32.DragQueryFileW(handle, i, None, 0)
            buf = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(handle, i, buf, length + 1)
            paths.append(buf.value)
        return paths
    finally:
        user32.CloseClipboard()
