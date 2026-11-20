"""Windows: which folder is under a point on the screen, for files dropped onto the viewer.

folder_at(x, y) returns
  ("path", folder)     an Explorer window or the desktop whose folder is known
  ("explorer", hwnd)   an Explorer window whose folder could not be read (the caller
                       pastes the file into it instead, see put_files_on_clipboard)
  None                 anything else (the file stays in Received files)
"""

import ctypes
import os
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32")

user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)
user32.SetClipboardData.restype = ctypes.c_void_p
user32.RegisterClipboardFormatW.argtypes = (wintypes.LPCWSTR,)
user32.RegisterClipboardFormatW.restype = wintypes.UINT
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)

DESKTOP_CLASSES = {"Progman", "WorkerW"}
EXPLORER_CLASSES = {"CabinetWClass", "ExploreWClass"}


def _text(fn, hwnd):
    buf = ctypes.create_unicode_buffer(512)
    fn(hwnd, buf, 512)
    return buf.value


def desktop_folder():
    buf = ctypes.create_unicode_buffer(260)
    if shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:  # CSIDL_DESKTOPDIRECTORY
        return Path(buf.value)
    return Path.home() / "Desktop"


def _explorer_folder(hwnd):
    """The folder shown in an Explorer window (its visible tab), or None."""
    try:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
    except Exception:
        return None
    try:
        found = []
        for window in comtypes.client.CreateObject("Shell.Application").Windows():
            try:
                if int(window.HWND) == int(hwnd):
                    url = str(window.LocationURL)  # file:///C:/Users/... for ordinary folders
                    if url.lower().startswith("file:"):
                        from urllib.parse import urlparse
                        from urllib.request import url2pathname
                        parsed = urlparse(url)
                        path = url2pathname(parsed.path)
                        if parsed.netloc:  # a network share: \\server\share\...
                            path = "\\\\" + parsed.netloc + path
                        found.append((str(window.LocationName), path))
            except Exception:
                continue
        if len(found) > 1:  # Windows 11 tabs share one window: the title names the visible tab
            title = _text(user32.GetWindowTextW, hwnd)
            found = [f for f in found if f[0] and title.startswith(f[0])] or found[:0]
        if len(found) == 1 and os.path.isdir(found[0][1]):
            return Path(found[0][1])
        return None
    except Exception:
        return None
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def folder_at(x, y):
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not hwnd:
        return None
    root = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    cls = _text(user32.GetClassNameW, root)
    if cls in DESKTOP_CLASSES:
        return ("path", desktop_folder())
    if cls in EXPLORER_CLASSES:
        folder = _explorer_folder(root)
        return ("path", folder) if folder else ("explorer", root)
    return None


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.c_void_p)
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)


def activate(hwnd):
    """Bring a window to the front. True if it is now the foreground window."""
    current = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    mine = kernel32.GetCurrentThreadId()
    attached = current and current != mine and user32.AttachThreadInput(mine, current, True)
    try:
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(mine, current, False)
    time.sleep(0.1)
    return user32.GetForegroundWindow() == hwnd


def put_files_on_clipboard(paths, move=True):
    """Put files on the clipboard as Explorer does for Ctrl+X (move) or Ctrl+C."""
    names = "".join(str(p) + "\0" for p in paths) + "\0"
    header = bytes(ctypes.c_uint32(20)) + bytes(8) + bytes(4) + bytes(ctypes.c_uint32(1))  # DROPFILES, wide
    blob = header + names.encode("utf-16-le")
    effect = bytes(ctypes.c_uint32(2 if move else 1))  # DROPEFFECT_MOVE / COPY
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return False
    try:
        user32.EmptyClipboard()
        for fmt, payload in ((15, blob), (user32.RegisterClipboardFormatW("Preferred DropEffect"), effect)):
            handle = kernel32.GlobalAlloc(0x0042, len(payload))  # GMEM_MOVEABLE | GMEM_ZEROINIT
            pointer = kernel32.GlobalLock(handle) if handle else None
            if not pointer:
                return False
            ctypes.memmove(pointer, payload, len(payload))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(fmt, handle):
                kernel32.GlobalFree(handle)
                return False
        return True
    finally:
        user32.CloseClipboard()


user32.GetParent.argtypes = (wintypes.HWND,)
user32.GetParent.restype = wintypes.HWND


def _in_file_view(hwnd):
    """True when the window is (inside) the file list of Explorer or the desktop."""
    while hwnd:
        if _text(user32.GetClassNameW, hwnd) == "SHELLDLL_DefView":
            return True
        hwnd = user32.GetParent(hwnd)
    return False


def dragged_files(x, y):
    """The files selected in the Explorer window or desktop at (x, y), when (x, y) is on its
    file list: what a drag that started there is carrying. [] for anything else."""
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not hwnd or not _in_file_view(hwnd):
        return []
    root = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    cls = _text(user32.GetClassNameW, root)
    if cls not in DESKTOP_CLASSES and cls not in EXPLORER_CLASSES:
        return []
    try:
        import comtypes
        import comtypes.client
        from comtypes.automation import VARIANT
        comtypes.CoInitialize()
    except Exception:
        return []
    try:
        shell = comtypes.client.CreateObject("Shell.Application")
        views = []
        if cls in DESKTOP_CLASSES:
            _, browser = shell.Windows().FindWindowSW(VARIANT(0), VARIANT(), 8, 1)  # SWC_DESKTOP
            views.append(("", browser.Document))
        else:
            for window in shell.Windows():
                try:
                    if int(window.HWND) == int(root):
                        views.append((str(window.LocationName), window.Document))
                except Exception:
                    continue
            if len(views) > 1:  # Windows 11 tabs share one window: the title names the visible tab
                title = _text(user32.GetWindowTextW, root)
                views = [v for v in views if v[0] and title.startswith(v[0])][:1]
        if len(views) != 1:
            return []
        paths = [str(item.Path) for item in views[0][1].SelectedItems()]
        return [p for p in paths if os.path.exists(p)]
    except Exception:
        return []
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass
