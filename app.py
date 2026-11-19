"""Remote Desktop - the app window (Windows, Linux, macOS).

Share this computer (ID + password) or control another one. Run from source
with start.bat / `python app.py`, or install it with the packaged installer.
`--minimized` starts hidden in the tray (used when opening at sign-in). Closing the
window keeps the app running in the background; quit from the tray icon.
"""

import asyncio
import atexit
import base64
import ctypes
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import messagebox, ttk

import autostart
import host
import rendezvous
import secure_desktop
import theme
import tray
import ui
import updater
from version import VERSION

if sys.platform == "win32":
    import winagent
else:
    winagent = None

# One copy at a time. A running copy listens here (this computer only); opening the app
# again asks it to show its window instead of starting a second copy that would fight
# over the sharing port and add a second tray icon.
INSTANCE_PORT = 47613


def ask_running_copy(action):
    """Send `action` ("show" or "ping") to a copy that is already running. True if one answered."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1.5) as conn:
            conn.sendall(action.encode() + b"\n")
            return conn.recv(16).startswith(b"ok")
    except OSError:
        return False


class InstanceServer:
    """Answers ask_running_copy() for the copy that is running."""

    def __init__(self, post):
        self.post = post
        self.sock = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        deadline = time.monotonic() + 8  # a copy that is closing (elevated restart) may still hold it
        while True:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                srv.bind(("127.0.0.1", INSTANCE_PORT))
                srv.listen(4)
                break
            except OSError:
                srv.close()
                if time.monotonic() > deadline:
                    return
                time.sleep(0.5)
        self.sock = srv
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return  # closed by stop()
            with conn:
                try:
                    conn.settimeout(2)
                    line = conn.recv(64).decode("ascii", "ignore").strip()
                    if line in ("show", "ping"):
                        if line == "show":
                            self.post("open")
                        conn.sendall(b"ok\n")
                except OSError:
                    pass

    def stop(self):
        sock, self.sock = self.sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

WINDOWS, MAC = sys.platform == "win32", sys.platform == "darwin"
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # also right when packaged
TUNNEL_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
MIN_PASSWORD = 10
MAX_PARTNERS = 12


def find_cloudflared():
    name = "cloudflared.exe" if WINDOWS else "cloudflared"
    candidates = [HERE / "bin" / name]  # bundled with the installed app
    if WINDOWS:
        candidates += [Path(os.environ[var]) / "cloudflared" / name
                       for var in ("ProgramFiles(x86)", "ProgramFiles") if os.environ.get(var)]
    else:
        candidates += [Path(folder) / name for folder in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin")]
    for path in candidates:
        if path.exists():
            return str(path)
    return shutil.which("cloudflared")


def is_admin():
    """True when running as administrator on Windows. Windows only lets an administrator
    program send input to administrator windows such as Task Manager."""
    try:
        return WINDOWS and bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def config_dir():
    """Per-user settings folder (an installed app cannot write next to its own files)."""
    if WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "RemoteDesktop"


SETTINGS_FILE = config_dir() / "settings.json"
OLD_SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"  # where earlier versions kept it
TUNNEL_ID = re.compile(r"^https://([a-z0-9-]+)\.trycloudflare\.com/?$")
ID_WORDS = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
HOST_PORT = re.compile(r"^([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::(\d{1,5}))?$")
FULL_URL = re.compile(r"^https?://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::\d{1,5})?/?$")


def partner_url(text):
    """Turn what the user typed as a partner ID into the viewer address, or None."""
    text = text.strip().lower()
    tunnel = TUNNEL_ID.match(text)
    if tunnel:
        text = tunnel.group(1)
    if ID_WORDS.match(text):  # an internet ID: the tunnel's name
        return f"https://{text}.trycloudflare.com/"
    if FULL_URL.match(text):
        return text
    local = HOST_PORT.match(text)
    if local and (local.group(2) is None or 1 <= int(local.group(2)) <= 65535):
        return f"http://{local.group(1)}:{local.group(2) or 8765}/"
    return None


def open_viewer(url):
    """Open the viewer in its own window (a browser's 'app mode'), or else in the default browser."""
    if WINDOWS:
        roots = [Path(os.environ[var]) for var in ("ProgramFiles(x86)", "ProgramFiles") if os.environ.get(var)]
        browsers = [root / "Microsoft" / "Edge" / "Application" / "msedge.exe" for root in roots]
        browsers += [root / "Google" / "Chrome" / "Application" / "chrome.exe" for root in roots]
    elif MAC:
        browsers = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    else:
        found = (shutil.which(name) for name in
                 ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"))
        browsers = [Path(path) for path in found if path]
    for browser in browsers:
        if browser.exists():
            subprocess.Popen([str(browser), f"--app={url}"])
            return
    webbrowser.open(url)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    """Encrypt/decrypt with Windows DPAPI: only this Windows user account can decrypt."""
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = _Blob(len(data), buf), _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(dst.pbData, ctypes.c_void_p))


def load_settings():
    """Saved settings, with the password decrypted. Empty dict if none or unreadable."""
    for path in (SETTINGS_FILE, OLD_SETTINGS_FILE):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            blob = data.pop("password_protected", None)
            if blob and WINDOWS:
                data["password"] = _dpapi(base64.b64decode(blob), False).decode("utf-8")
            elif "password_plain" in data:
                data["password"] = data.pop("password_plain")
            return data
        except (OSError, ValueError):
            continue
    return {}


def save_settings(password, **fields):
    """Save settings. The password is encrypted for this user on Windows; elsewhere the
    file is readable only by this user (mode 600)."""
    if WINDOWS:
        secret = {"password_protected": base64.b64encode(_dpapi(password.encode("utf-8"), True)).decode("ascii")}
    else:
        secret = {"password_plain": password}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.touch(mode=0o600, exist_ok=True)
    if not WINDOWS:
        os.chmod(SETTINGS_FILE, 0o600)
    SETTINGS_FILE.write_text(json.dumps({**secret, **fields}, indent=2), encoding="utf-8")


def to_ws(url):
    """http(s)://host/ -> ws(s)://host/ws"""
    return url.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/ws"


def protect_secret(text):
    """A saved password: encrypted for this Windows user; elsewhere kept in the settings
    file that only this user can read."""
    if WINDOWS:
        return "dpapi:" + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")
    return "plain:" + text


def reveal_secret(value):
    try:
        kind, _, data = str(value).partition(":")
        if kind == "dpapi" and WINDOWS:
            return _dpapi(base64.b64decode(data), False).decode("utf-8")
        if kind == "plain":
            return data
    except (OSError, ValueError):
        pass
    return ""


# ---- copy/paste files between this computer and the one being controlled (Windows) ----
CLIP_FILE_LIMIT = 2 * 1024 ** 3


def clipboard_get_request(handler, links, rest, full_path):
    """GET /clip/<link>            -> files copied on this computer since the last paste here
       GET /clip/<link>/<seq>/<i>  -> one of those files' contents
       Files already sent, or ones this app itself put there from the remote side, are not offered."""
    parts = rest.split("?", 1)[0].split("/")
    link = links.get(parts[0])
    if not link:
        return handler.reply(404, b"not found")
    if len(parts) == 1:
        seq = host.inp.clipboard_seq()
        if seq in (link.get("sent_seq"), link.get("own_seq")):
            return handler.reply(200, b'{"files": []}', "application/json")
        files = []
        for path in (host.inp.clipboard_files() or [])[:50]:
            try:
                if os.path.isfile(path) and os.path.getsize(path) <= CLIP_FILE_LIMIT:
                    files.append(path)
            except OSError:
                continue
        link["offer"] = {"seq": seq, "paths": files}
        body = {"seq": seq, "files": [{"i": i, "n": os.path.basename(f), "s": os.path.getsize(f)}
                                      for i, f in enumerate(files)]}
        return handler.reply(200, json.dumps(body).encode(), "application/json")
    offer = link.get("offer") or {}
    try:
        seq, index = int(parts[1]), int(parts[2])
        path = offer["paths"][index] if offer.get("seq") == seq else None
    except (ValueError, IndexError, KeyError):
        path = None
    if not path:
        return handler.reply(404, b"not found")
    if index == len(offer["paths"]) - 1:
        link["sent_seq"] = seq  # the whole set has been handed over: don't offer it again
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            handler.send_response(200)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", str(size))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            while chunk := f.read(1 << 20):
                handler.wfile.write(chunk)
    except OSError:
        pass


def clipboard_post_request(handler, links, rest):
    """POST /clip/<link> {"base": ..., "files": [{"n","s","u"}]}: download files copied on the
    remote computer and put them on this computer's clipboard, ready for Ctrl+V."""
    link = links.get(rest)
    if not link:
        return handler.reply(404, b"not found")
    try:
        length = int(handler.headers.get("Content-Length") or 0)
        request = json.loads(handler.rfile.read(min(length, 1 << 20)))
        base, files = str(request["base"]), list(request["files"])[:50]
    except (ValueError, KeyError, TypeError):
        return handler.reply(400, b"bad request")
    # Only from the computer this window is connected to.
    allowed = link["ws"].replace("wss://", "https://", 1).replace("ws://", "http://", 1)[:-len("/ws")]
    if base.rstrip("/") != allowed.rstrip("/"):
        return handler.reply(403, b"not this window's computer")
    import tempfile
    import urllib.request
    folder = Path(tempfile.mkdtemp(prefix="RemoteDesktopClip-"))
    saved = []
    try:
        for item in files:
            u = str(item.get("u", ""))
            if not u.startswith("/dl/"):
                continue
            name = host.safe_name(item.get("n", "file"))
            target = host.unique_path(folder, name)
            with urllib.request.urlopen(allowed + u, timeout=60) as response, open(target, "wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
            saved.append(target)
    except OSError as exc:
        return handler.reply(502, json.dumps({"ok": False, "error": str(exc)}).encode(), "application/json")
    import drop_target
    ok = bool(saved) and drop_target.put_files_on_clipboard(saved, move=False)
    link["own_seq"] = host.inp.clipboard_seq()  # don't send these straight back on the next paste
    body = json.dumps({"ok": ok, "count": len(saved)})
    handler.reply(200, body.encode(), "application/json")


class LocalViewer:
    """Serves the viewer page to this PC only, for controlling another computer.

    The page comes from this program, never from the remote address, so a wrong
    or forged address cannot show a fake login page. Each new window collects
    its connection details once, through a single-use token.
    """

    def __init__(self, on_report=None):
        self.httpd = None
        self.sessions = {}
        # Per viewer window, for as long as the app runs: a secret link to look up the partner's
        # current address again (it changes when the partner restarts) and one to report its name.
        self.links = {}
        self.on_report = on_report

    def start(self):
        if self.httpd:
            return
        sessions, links, viewer = self.sessions, self.links, self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body, content_type="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def local_only(self):
                port = self.server.server_address[1]
                if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    self.reply(403, b"forbidden")
                    return False
                return True

            def do_GET(self):
                if not self.local_only():
                    return
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    return self.reply(200, (HERE / "viewer.html").read_bytes(), "text/html; charset=utf-8")
                if path.startswith("/session/"):
                    session = sessions.pop(path[len("/session/"):], None)
                    if session and session["expires"] > time.monotonic():
                        body = json.dumps({"ws": session["ws"], "password": session["password"],
                                           "resolve": "/resolve/" + session["link"],
                                           "report": "/report/" + session["link"],
                                           "clip": "/clip/" + session["link"] if WINDOWS else None})
                        return self.reply(200, body.encode(), "application/json")
                if path.startswith("/clip/") and WINDOWS:
                    return clipboard_get_request(self, links, path[len("/clip/"):], self.path)
                if path.startswith("/resolve/"):
                    link = links.get(path[len("/resolve/"):])
                    if link and link["id"]:
                        try:
                            url = rendezvous.lookup(link["id"])
                        except OSError:
                            url = None
                        if url:
                            link["ws"] = to_ws(url)
                            return self.reply(200, json.dumps({"ws": link["ws"]}).encode(), "application/json")
                self.reply(404, b"not found")

            def do_POST(self):
                if not self.local_only():
                    return
                path, _, query = self.path.partition("?")
                if path.startswith("/clip/") and WINDOWS:
                    return clipboard_post_request(self, links, path[len("/clip/"):])
                link = links.get(path[len("/report/"):]) if path.startswith("/report/") else None
                if not link:
                    return self.reply(404, b"not found")
                from urllib.parse import parse_qs
                host = (parse_qs(query).get("host") or [""])[0].strip()[:80]
                if host and viewer.on_report:
                    viewer.on_report(link["key"], host)
                self.reply(204, b"")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def open(self, ws_url, password, key=""):
        """Open a viewer window. `key` is what the user typed (a 9-digit ID or an address)."""
        self.start()
        token, link = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.links[link] = {"key": key, "id": rendezvous.digits(key) if rendezvous.is_id(key) else None,
                            "ws": ws_url}
        self.sessions[token] = {"ws": ws_url, "password": password, "link": link,
                                "expires": time.monotonic() + 60}
        open_viewer(f"http://127.0.0.1:{self.httpd.server_address[1]}/?s={token}")


class Server:
    """Runs the host's asyncio server on a background thread."""

    def __init__(self, events):
        self.events = events
        self.loop = None
        self.stop_event = None

    def start(self, port):
        threading.Thread(target=self._run, args=(port,), daemon=True).start()

    def _run(self, port):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.stop_event = asyncio.Event()
        deadline = time.monotonic() + 6  # a copy that is shutting down (e.g. an elevated restart) may still hold the port
        try:
            while True:
                try:
                    self.loop.run_until_complete(host.run_server(
                        "0.0.0.0", port, None, self.stop_event,
                        on_ready=lambda: self.events.put(("started", port))))
                    self.events.put(("stopped", None))
                    return
                except OSError as exc:
                    # "address already in use": 48/98 (POSIX), 10048/10013 (Windows winsock).
                    codes = {getattr(exc, "winerror", None), exc.errno}
                    in_use = bool(codes & {48, 98, 10048, 10013})
                    if in_use and time.monotonic() < deadline and not self.stop_event.is_set():
                        time.sleep(0.4)
                        continue
                    self.events.put(("error", f"Could not start on port {port}:\n{exc}"))
                    return
        finally:
            self.loop.close()

    def _call(self, fn):
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn)

    def stop(self):
        self._call(lambda: self.stop_event.set())

    def send_chat(self, text):
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(host.send_chat(text), self.loop)

    def disconnect_all(self):
        def close_all():
            for ws in list(host.SESSIONS):
                asyncio.ensure_future(ws.close(1000, "disconnected by host"))
        self._call(close_all)


class Tunnel:
    """A Cloudflare quick tunnel: a temporary public HTTPS address for the local port."""

    def __init__(self, events):
        self.events = events
        self.proc = None

    def start(self, exe, port):
        self.proc = subprocess.Popen(
            [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        found, last_error = False, None
        for line in proc.stderr:
            match = None if found else TUNNEL_URL.search(line)
            if match:
                found = True
                self.events.put(("tunnel", match.group(0)))
            elif " ERR " in line:
                last_error = line.split(" ERR ", 1)[1].strip()[:200]
        proc.wait()
        if proc is self.proc:  # exited on its own, not stopped by us
            self.events.put(("tunnel_closed", last_error))

    def stop(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.server = None
        self.tunnel = Tunnel(self.events)
        self.state = "stopped"  # stopped | starting | running | stopping
        host.LOG_SINK = lambda line: self.events.put(("log", line))
        atexit.register(self.tunnel.stop)

        saved = load_settings()
        self.password = tk.StringVar(value=saved.get("password") or secrets.token_urlsafe(12))
        self.port = tk.StringVar(value=str(saved.get("port", 8765)))
        self.control = tk.BooleanVar(value=bool(saved.get("control", True)))
        self.internet = tk.BooleanVar(value=bool(saved.get("internet", True)) and bool(find_cloudflared()))
        self.show_pw = tk.BooleanVar(value=False)
        self.addr = {k: tk.StringVar() for k in ("local", "lan", "net")}
        self.my_id = tk.StringVar()
        self.addr_entries = {}
        self.chat_text = tk.StringVar()
        self.chat_lines = []
        self.chat_win = None
        self.chat_box = None
        host.CHAT_SINK = lambda ip, text: self.events.put(("chat", (ip, text)))
        self.id_ready = False
        self.partner = tk.StringVar()
        self.partner_pw = tk.StringVar()
        self.viewer = LocalViewer(on_report=lambda key, host: self.events.put(("partner_name", (key, host))))
        # Recent and saved computers: [{"key", "name", "pw" (protected), "last"}], newest first.
        self.partners = [p for p in saved.get("partners", []) if isinstance(p, dict) and p.get("key")][:MAX_PARTNERS]
        self.remember_pw = tk.BooleanVar(value=False)
        saved_id = str(saved.get("id", ""))
        self.number = saved_id if rendezvous.is_id(saved_id) else rendezvous.new_id()
        self.registered = None      # None = not yet known, True/False = number lookup works or not
        self.last_publish = 0.0
        # Start sharing when the app opens: on by default (switched on once for existing settings too).
        self.auto_share = tk.BooleanVar(value=bool(saved.get("auto_share", True))
                                        or not saved.get("auto_share_default_done", False))
        self.auto_share_default_done = True
        # Opening at sign-in is on by default; once applied, the user's own choice is kept.
        self.launch_default_pending = not saved.get("launch_default_done", False)
        self.auto_launch = tk.BooleanVar(value=autostart.is_enabled() or self.launch_default_pending)
        self.selftest = "--selftest" in sys.argv
        self.tray = tray.Tray(str(HERE / "assets" / "icon-256.png"),
                              lambda action: self.events.put(("tray", action)))
        self.instance = InstanceServer(lambda action: self.events.put(("tray", action)))
        self.closed = False
        self.uac_restored = bool(saved.get("uac_restored", False))
        self.last_update_check = float(saved.get("last_update_check", 0))
        self.update_busy = False
        self.auto_update = tk.BooleanVar(value=bool(saved.get("auto_update", True)))
        self.update_auto_run = False   # the update in progress was started automatically
        self.pending_update = None     # found automatically while someone was connected: install later
        # Sharing was on when an automatic update restarted the app: carry on sharing.
        self.resume_share = bool(saved.get("resume_share", False))

        root.title(f"Remote Desktop {VERSION}")
        try:  # window and taskbar icon; on Windows the .ico has crisp small sizes
            if WINDOWS:
                root.iconbitmap(default=str(HERE / "assets" / "icon.ico"))
            else:
                self.icon = tk.PhotoImage(file=str(HERE / "assets" / "icon-256.png"))
                root.iconphoto(True, self.icon)
        except tk.TclError:
            pass
        scale = root.winfo_fpixels("1i") / 96  # sizes below are at 100% display scaling
        root.minsize(round(860 * scale), round(560 * scale))
        root.geometry(f"{round(900 * scale)}x{round(620 * scale)}")
        root.protocol("WM_DELETE_WINDOW", self.hide_window)  # closing keeps it running in the background
        # Windows is restarting or signing out: quit at once (hiding here would hold the restart up).
        root.protocol("WM_SAVE_YOURSELF", lambda: self.on_close(force=True))
        self.theme = theme.Theme(root)  # light or dark, following the system setting
        self.theme.window(root)
        self.build()
        self.theme.on_change = self.refresh  # the status dot and share button colours
        self.theme.follow_system()
        self.my_id.set(rendezvous.pretty(self.number))
        if not self.selftest:
            self.restore_uac()
            self.tray.start()
            self.instance.start()
            root.after(1500, self.apply_autostart)
            root.after(5000, self.update_timer)
            threading.Thread(target=updater.cleanup, daemon=True).start()
        self.refresh_service()
        self.refresh()
        if "--minimized" in sys.argv:  # opened at sign-in: start out of the way
            if self.tray.available:
                root.withdraw()
            else:
                root.iconify()
        if "--share" in sys.argv:  # restarted as administrator while sharing: wait for the old copy to exit
            root.after(2500, lambda: self.start(quiet=True))
        elif self.auto_share.get() or self.resume_share:  # resume_share: sharing before an update
            self.resume_share = False
            root.after(600, lambda: self.start(quiet=True))
        root.after(150, self.poll)
        root.after(1000, self.tick)

    # ---- layout ----
    def build(self):
        """Sidebar navigation (Remote, Devices, Chat, Settings) with rounded cards on each page."""
        root, th = self.root, self.theme
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        def card(parent, title=None, subtitle=None, **grid):
            c = ui.RoundCard(parent, th)
            c.grid(**grid)
            if title:
                ttk.Label(c.body, text=title, style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
            if subtitle:
                ttk.Label(c.body, text=subtitle, style="Muted.TLabel", wraplength=440
                          ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 8))
            return c.body

        def icon_btn(parent, glyph, command, tip, size, flash=False):
            return ui.IconButton(parent, th, glyph, command, tip, size=size, flash=flash)

        def accent(parent, text, color, command):
            return th.accent_button(parent, text, command, color)

        def toggle_row(parent, row, text, desc, variable, command=None):
            ttk.Label(parent, text=text, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=(10, 0))
            if desc:
                ttk.Label(parent, text=desc, style="Muted.TLabel").grid(row=row + 1, column=0, sticky="w")
            switch = ui.Toggle(parent, th, variable, command)
            switch.grid(row=row, column=1, rowspan=2, sticky="e", pady=(10, 0))
            return switch

        # ---- sidebar ----
        self.sidebar = ui.Sidebar(root, th, "Remote Desktop", str(HERE / "assets" / "icon-256.png"))
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.add("remote", "Remote", "", lambda: self.show_page("remote"))
        self.sidebar.add("devices", "Devices", "", lambda: self.show_page("devices"))
        self.sidebar.add("chat", "Chat", "", self.open_chat)
        self.sidebar.add("settings", "Settings", "", lambda: self.show_page("settings"))
        self.sidebar.set_footer(f"Version {VERSION}")

        content = ttk.Frame(root, padding=(26, 16, 26, 18))
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        # ---- top bar: page title, update status, sharing status ----
        top = ttk.Frame(content)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        top.columnconfigure(0, weight=1)
        self.page_title = ttk.Label(top, text="", style="Page.TLabel")
        self.page_title.grid(row=0, column=0, sticky="w")
        self.update_label = ttk.Label(top, text=f"Version {VERSION}", style="BgMuted.TLabel")
        self.update_label.grid(row=0, column=1, padx=(0, 16))
        pill = ui.RoundCard(top, th, radius=15, pad=(14, 6), fit_width=True)
        pill.grid(row=0, column=2)
        self.dot = th.paint(tk.Canvas(pill.body, width=12, height=12, highlightthickness=0), bg="card")
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=th.pal["grey"], outline="")
        self.dot.grid(row=0, column=0, padx=(0, 8))
        self.status = ttk.Label(pill.body, text="", style="Pill.TLabel")
        self.status.grid(row=0, column=1)

        self.pages = {}
        for key in ("remote", "devices", "settings"):
            page = ttk.Frame(content)
            page.columnconfigure(0, weight=1)
            self.pages[key] = page

        # ==== Remote page ====
        page = self.pages["remote"]
        cards = ttk.Frame(page)
        cards.grid(row=0, column=0, sticky="ew")
        cards.columnconfigure(0, weight=1, uniform="card")
        cards.columnconfigure(1, weight=1, uniform="card")

        share = card(cards, "Allow remote control", "Give this ID and password to the person who will connect.",
                     row=0, column=0, sticky="nsew", padx=(0, 10))
        id_row = ttk.Frame(share, style="Card.TFrame")
        id_row.grid(row=2, column=0, columnspan=3, sticky="ew")
        ttk.Label(id_row, text="Your ID", style="Muted.TLabel").pack(side="left")
        self.id_note = ttk.Label(id_row, text="", style="Warn.TLabel")  # when the ID can't be used right now
        self.id_note.pack(side="left", padx=(8, 0))
        self.id_entry = ttk.Entry(share, textvariable=self.my_id, font=(theme.FONT, 18, "bold"), width=14)
        self.id_entry.grid(row=3, column=0, sticky="ew")
        size = round(36 * root.winfo_fpixels("1i") / 96)  # one size for every icon button
        icon_btn(share, "\uE8C8", lambda: self.id_ready and self.copy(self.my_id.get()), "Copy ID", size,
                 flash=True).grid(row=3, column=1, padx=(8, 0))
        self.new_id_btn = icon_btn(share, "\uE72C", self.new_number, "New ID", size)
        self.new_id_btn.grid(row=3, column=2, padx=(6, 0))
        ttk.Label(share, text="Password", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(12, 0))
        self.pw_entry = ttk.Entry(share, textvariable=self.password, show="•", font=(theme.FONT, 14), width=14)
        self.pw_entry.grid(row=5, column=0, sticky="ew")
        icon_btn(share, "\uE8C8", lambda: self.copy(self.password.get()), "Copy password", size,
                 flash=True).grid(row=5, column=1, padx=(8, 0))
        self.new_btn = icon_btn(share, "\uE72C", self.new_password, "New password", size)
        self.new_btn.grid(row=5, column=2, padx=(6, 0))
        ttk.Checkbutton(share, text="Show ID and password", variable=self.show_pw, command=self.toggle_show,
                        style="Card.TCheckbutton").grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.toggle_btn = accent(share, "Start sharing", "accent", self.toggle)
        self.toggle_btn.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(14, 0))

        ctrl = card(cards, "Control a remote computer", "Enter the ID and password shown on the other computer.",
                    row=0, column=1, sticky="nsew", padx=(10, 0))
        ttk.Label(ctrl, text="Partner ID  (or pick a recent computer)", style="Muted.TLabel"
                  ).grid(row=2, column=0, columnspan=2, sticky="w")
        self.partner_box = th.combobox(ttk.Combobox(ctrl, textvariable=self.partner, font=(theme.FONT, 18)))
        self.partner_box.grid(row=3, column=0, sticky="ew")
        self.partner_box.bind("<<ComboboxSelected>>", lambda _e: self.pick_partner())
        self.forget_btn = icon_btn(ctrl, "\uE74D", self.forget_partner, "Remove from the list", size)
        self.forget_btn.grid(row=3, column=1, padx=(8, 0))
        ttk.Label(ctrl, text="Password", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(12, 0))
        partner_pw_entry = ttk.Entry(ctrl, textvariable=self.partner_pw, show="•", font=(theme.FONT, 14))
        partner_pw_entry.grid(row=5, column=0, columnspan=2, sticky="ew")
        for entry in (self.partner_box, partner_pw_entry):
            entry.bind("<Return>", lambda _e: self.connect_partner())
        ttk.Checkbutton(ctrl, text="Remember password", variable=self.remember_pw, style="Card.TCheckbutton"
                        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.partner_msg = ttk.Label(ctrl, text="", style="Error.TLabel", wraplength=420)
        self.partner_msg.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ctrl.rowconfigure(7, weight=1)
        self.connect_btn = accent(ctrl, "Connect", "indigo", self.connect_partner)
        self.connect_btn.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.refresh_partners()

        links = card(page, "Browser access", row=1, column=0, sticky="ew", pady=(16, 0))
        links.columnconfigure(0, weight=0)
        links.columnconfigure(1, weight=1)
        ttk.Label(links, text="Open one of these in a browser on the other computer, no app needed.",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 4))
        for i, (key, label) in enumerate((("local", "This PC"), ("lan", "Same network"), ("net", "Internet")), 2):
            ttk.Label(links, text=label, style="Card.TLabel").grid(row=i, column=0, sticky="w", padx=(0, 14), pady=(4, 0))
            self.addr_entries[key] = ttk.Entry(links, textvariable=self.addr[key], state="readonly")
            self.addr_entries[key].grid(row=i, column=1, sticky="ew", pady=(4, 0))
            icon_btn(links, "\uE8C8", lambda k=key: self.copy(self.addr[k].get(), url=True), "Copy link",
                     size, flash=True).grid(row=i, column=2, padx=(8, 0), pady=(4, 0))

        # ==== Devices page ====
        page = self.pages["devices"]
        page.rowconfigure(1, weight=1)
        devices = card(page, "Connected devices", "Everyone connected to this computer right now.",
                       row=0, column=0, sticky="ew")
        devices.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(devices, columns=("ip", "since"), show="headings", height=4, selectmode="none")
        self.tree.heading("ip", text="Address", anchor="w")
        self.tree.heading("since", text="Connected since", anchor="w")
        self.tree.column("ip", width=260, anchor="w")
        self.tree.column("since", width=140, anchor="w")
        self.tree.grid(row=2, column=0, columnspan=3, sticky="ew")
        buttons = tk.Frame(devices, bd=0)
        th.paint(buttons, bg="card")
        buttons.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.kick_btn = ttk.Button(buttons, text="Disconnect all", command=self.disconnect_all)
        self.kick_btn.pack(side="left")
        ttk.Button(buttons, text="Open received files", command=self.open_received).pack(side="left", padx=(8, 0))

        activity_card = ui.RoundCard(page, th)
        activity_card.grid(row=1, column=0, sticky="nsew", pady=(16, 0))
        activity = activity_card.body
        activity.columnconfigure(0, weight=1)
        ttk.Label(activity, text="Activity", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.logbox = th.paint(tk.Text(activity, height=12, width=30, state="disabled", wrap="word", relief="flat",
                                       font=(theme.MONO, 9), padx=10, pady=8, highlightthickness=1, bd=0),
                               bg="log", fg="text", highlightbackground="border", highlightcolor="border",
                               insertbackground="text")
        scroll = ttk.Scrollbar(activity, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=scroll.set)
        self.logbox.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")

        # ==== Settings page ====
        page = self.pages["settings"]
        cols = ttk.Frame(page)
        cols.grid(row=0, column=0, sticky="ew")
        cols.columnconfigure(0, weight=1, uniform="set")
        cols.columnconfigure(1, weight=1, uniform="set")

        sharing = card(cols, "Sharing", row=0, column=0, sticky="nsew", padx=(0, 10))
        self.control_chk = toggle_row(sharing, 1, "Mouse and keyboard control",
                                      "Let the other person use this computer", self.control)
        self.internet_chk = toggle_row(sharing, 3, "Connections from the internet",
                                       "Off: only computers on this network", self.internet)
        ttk.Label(sharing, text="Port", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=(14, 0))
        self.port_entry = ttk.Entry(sharing, textvariable=self.port, width=8, justify="center")
        self.port_entry.grid(row=5, column=1, sticky="e", pady=(14, 0))
        ttk.Label(sharing, text="Change these while not sharing.", style="Muted.TLabel"
                  ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

        startup = card(cols, "Startup", row=0, column=1, sticky="nsew", padx=(10, 0))
        toggle_row(startup, 1, "Open when I sign in", "Starts hidden in the tray", self.auto_launch,
                   self.toggle_auto_launch)
        toggle_row(startup, 3, "Start sharing when the app opens", "Ready for remote access right away",
                   self.auto_share, self.save)

        row = 1
        if WINDOWS:
            win = card(page, "Windows", row=row, column=0, sticky="ew", pady=(16, 0))
            win.columnconfigure(0, weight=1)
            if is_admin():
                ttk.Label(win, text="Running as administrator: Task Manager and other administrator windows "
                                    "can be controlled.", style="Good.TLabel").grid(row=1, column=0, sticky="w",
                                                                                    pady=(8, 0))
            else:
                ttk.Label(win, text="Restart as administrator to control Task Manager and other administrator "
                                    "windows.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
                ttk.Button(win, text="Restart as administrator", command=self.restart_as_admin
                           ).grid(row=1, column=1, sticky="e", pady=(8, 0))
            self.svc_label = ttk.Label(win, text="", style="Muted.TLabel", wraplength=520)
            self.svc_label.grid(row=2, column=0, sticky="w", pady=(10, 0))
            self.svc_btn = ttk.Button(win, text="Install service", width=16, command=self.toggle_service)
            self.svc_btn.grid(row=2, column=1, sticky="e", pady=(10, 0))
            row += 1

        upd = card(page, "Updates", row=row, column=0, sticky="ew", pady=(16, 0))
        toggle_row(upd, 1, "Install updates automatically",
                   "New versions install by themselves when no one is connected", self.auto_update, self.save)
        self.update_btn = accent(upd, "Check for updates", "accent", lambda: self.check_updates(manual=True))
        self.update_btn.configure(font=(theme.FONT, 10, "bold"), pady=5)
        self.update_btn.grid(row=3, column=0, columnspan=2, sticky="w", pady=(14, 0))

        self.show_page("remote")

    def show_page(self, key):
        for name, page in self.pages.items():
            if name == key:
                page.grid(row=1, column=0, sticky="nsew")
            else:
                page.grid_remove()
        self.page_title.configure(text={"remote": "Remote control", "devices": "Devices",
                                        "settings": "Settings"}[key])
        self.sidebar.select(key)

    def toggle_settings(self):
        self.show_window()
        self.show_page("settings")

    # ---- small actions ----
    def toggle_show(self):
        """Mask the ID, password and internet address unless 'Show' is ticked."""
        reveal = self.show_pw.get()
        dots = "•"
        self.pw_entry.configure(show="" if reveal else dots)
        # Status texts such as "Creating ID..." are not secrets and stay readable.
        self.id_entry.configure(show="" if reveal or not self.id_ready else dots)
        secret_url = self.addr["net"].get().startswith("https://")
        self.addr_entries["net"].configure(show="" if reveal or not secret_url else dots)

    def new_password(self):
        self.password.set(secrets.token_urlsafe(12))

    def copy(self, text, url=False):
        if url and not text.startswith("http"):
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def log(self, line):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", line + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def note(self, msg):
        self.log(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def disconnect_all(self):
        if self.server:
            self.server.disconnect_all()

    # ---- chat ----
    def open_chat(self):
        if self.chat_win and self.chat_win.winfo_exists():
            self.chat_win.deiconify()
            self.chat_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("Chat - Remote Desktop")
        win.minsize(640, 480)
        self.theme.window(win)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.chat_box = self.theme.paint(
            tk.Text(frame, height=12, width=48, state="disabled", wrap="word", relief="flat", bd=0,
                    font=(theme.FONT, 10), padx=10, pady=8, highlightthickness=1),
            bg="card", fg="text", highlightbackground="border", highlightcolor="border")
        self.chat_box.grid(row=0, column=0, columnspan=2, sticky="nsew")
        entry = ttk.Entry(frame, textvariable=self.chat_text, font=(theme.FONT, 10))
        entry.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        entry.bind("<Return>", lambda _e: self.send_chat())
        self.theme.accent_button(frame, "Send", self.send_chat).grid(row=1, column=1, padx=(8, 0), pady=(10, 0))
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self.chat_win = win
        for line in self.chat_lines:
            self._chat_insert(line)
        entry.focus_set()

    def _chat_insert(self, line):
        self.chat_box.configure(state="normal")
        self.chat_box.insert("end", line + "\n")
        self.chat_box.see("end")
        self.chat_box.configure(state="disabled")

    def chat_append(self, line):
        self.chat_lines.append(line)
        if self.chat_win and self.chat_win.winfo_exists():
            self._chat_insert(line)

    def send_chat(self):
        text = self.chat_text.get().strip()
        if not text:
            return
        if not (self.server and host.SESSIONS):
            self.chat_append("(No one is connected, so the message was not sent.)")
            return
        self.server.send_chat(text)
        self.chat_append(f"[{time.strftime('%H:%M')}] You: {text}")
        self.chat_text.set("")

    def open_received(self):
        host.RECEIVED_DIR.mkdir(parents=True, exist_ok=True)
        if WINDOWS:
            os.startfile(host.RECEIVED_DIR)
        else:
            subprocess.Popen(["open" if MAC else "xdg-open", str(host.RECEIVED_DIR)])

    # ---- settings ----
    def save(self):
        """Remember the ID, password and options for next time."""
        try:
            port = int(self.port.get())
        except ValueError:
            port = 8765
        try:
            save_settings(self.password.get(), port=port, control=self.control.get(),
                          internet=self.internet.get(), id=self.number,
                          auto_share=self.auto_share.get(),
                          uac_restored=self.uac_restored,
                          last_update_check=self.last_update_check,
                          auto_update=self.auto_update.get(),
                          partners=self.partners,
                          auto_share_default_done=self.auto_share_default_done,
                          resume_share=self.resume_share,
                          launch_default_done=not self.launch_default_pending)
        except OSError as exc:
            self.note(f"Could not save settings: {exc}")

    def new_number(self):
        self.number = rendezvous.new_id()
        self.my_id.set(rendezvous.pretty(self.number))

    def restart_as_admin(self):
        """Reopen the app with administrator rights (Windows asks for permission)."""
        if host.SESSIONS and not messagebox.askokcancel(
                "Restart as administrator?", "A device is connected. Restarting will disconnect it."):
            return
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, []
        else:
            python = Path(sys.executable)
            windowless = python.with_name("pythonw.exe")  # avoid a console window
            exe = str(windowless if windowless.exists() else python)
            params = [str(Path(__file__).resolve())]
        if self.state == "running":
            params.append("--share")  # carry on sharing after the restart
        params.append("--restarted")  # this copy is closing; the new one must not hand over to it
        if autostart.launch_task_exists():  # full rights without a UAC prompt
            self.resume_share = self.state == "running"
            self.save()
            self.instance.stop()  # so the new copy doesn't find this one and step aside
            if autostart.start_elevated():
                self.on_close(force=True, handoff=True)
                return
            self.instance.start()
        self.save()
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        # SW_SHOWMINNOACTIVE (7) so the restarting window doesn't steal focus from the UAC prompt.
        result = shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(params), None, 7)
        if (result or 0) > 32:
            # Hand off: keep the ID registered (the new copy takes it over) and release the port.
            self.on_close(force=True, handoff=True)
        else:
            self.note("Administrator permission was not given; nothing changed.")

    # ---- UAC: the background service replaces the old "no UAC prompt" mode ----
    def restore_uac(self):
        """Earlier versions could switch UAC to 'elevate without prompting' (and fix_uac.bat
        did the same). That mode is gone; put Windows' normal UAC prompt back, once."""
        if not WINDOWS or self.uac_restored or not is_admin():
            return
        try:
            if secure_desktop.is_remote_admin_on() is True or secure_desktop.is_secure_desktop_on() is False:
                secure_desktop.set_remote_admin(False)  # prompt for consent, on the secure desktop
                self.note("Windows' normal UAC prompt has been turned back on. With the background "
                          "service running, prompts can still be answered remotely.")
            self.uac_restored = True
            self.save()
        except OSError as exc:
            self.note(f"Could not restore the normal UAC prompt: {exc}")

    def refresh_service(self):
        if not WINDOWS:
            return
        state = winagent.service_state()
        problem = winagent.install_problem()
        if state in ("running", "stopped"):
            self.svc_btn.configure(text="Remove service", state="normal")
            text = ("Service on: UAC prompts and the sign-in screen can be controlled remotely."
                    if state == "running" else "Service installed but not running.")
            if not is_admin():
                text += " Needs the app running as administrator."
        else:
            self.svc_btn.configure(text="Install service", state="disabled" if problem else "normal")
            text = "Service off: UAC prompts and the sign-in screen can't be controlled remotely."
            if problem and not getattr(sys, "frozen", False):
                text += " (Installed app only.)"
            elif problem:
                text += " Reinstall with the latest installer to enable it."
        self.svc_label.configure(text=text)

    def toggle_service(self):
        if not is_admin():
            if messagebox.askyesno("Administrator rights required",
                                   "Installing or removing the service needs administrator rights.\n\n"
                                   "Restart the app as administrator now?"):
                self.restart_as_admin()
            return
        installed = winagent.service_state() in ("running", "stopped")
        if installed and not messagebox.askokcancel(
                "Remove the service?", "Without it, UAC prompts and the sign-in screen cannot be "
                                       "controlled remotely."):
            return
        self.svc_btn.configure(state="disabled")

        def work():
            try:
                (winagent.uninstall_service if installed else winagent.install_service)()
                self.events.put(("service", None))
            except OSError as exc:
                self.events.put(("service", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    # ---- updates (GitHub Releases) ----
    AUTO_CHECK_HOURS = 6

    def update_timer(self):
        """Look for updates now and then while the app runs (it may run for weeks in the tray)."""
        if self.pending_update and not host.SESSIONS and not self.update_busy:
            info, self.pending_update = self.pending_update, None
            self.start_update(info, auto=True)  # the person who was connected has left
        else:
            self.check_updates(manual=False)
        self.root.after(10 * 60 * 1000, self.update_timer)

    def check_updates(self, manual):
        if self.update_busy or not updater.configured():
            return
        if not manual and time.time() - self.last_update_check < self.AUTO_CHECK_HOURS * 3600:
            return
        self.update_busy = True
        self.update_btn.configure(state="disabled")
        self.update_label.configure(text="Checking for updates…")

        def work():
            try:
                self.events.put(("update", (manual, updater.check(), None)))
            except updater.NoReleases as exc:
                self.events.put(("update", (manual, "none", str(exc))))
            except OSError as exc:
                self.events.put(("update", (manual, None, str(exc))))
        threading.Thread(target=work, daemon=True).start()

    def on_update_info(self, manual, info, error):
        self.update_busy = False
        self.update_btn.configure(state="normal")
        # Only the version and whether it is up to date are shown; no dialog unless there is an update.
        if error and info != "none":
            self.update_label.configure(text=f"Version {VERSION}")  # the reason goes to Activity
            self.note(f"Could not check for updates: {error}")
            return
        self.last_update_check = time.time()
        self.save()
        if not info or info == "none":  # no newer release (or none published yet)
            self.update_label.configure(text=f"Version {VERSION} · Up to date")
            return
        self.update_label.configure(text=f"Version {VERSION} · {info['version']} available")
        self.note(f"Version {info['version']} is available.")
        if not manual and self.auto_update.get() and info["asset"] and WINDOWS:
            if host.SESSIONS:  # never restart under someone's remote session: wait until they leave
                self.pending_update = info
                self.note(f"Version {info['version']} will install once no one is connected.")
            else:
                self.start_update(info, auto=True)
            return
        if not manual and not self.root.winfo_viewable():  # running hidden: don't pop up a dialog
            self.tray.notify(f"Version {info['version']} is available. Open Remote Desktop to update.")
            return
        if not info["asset"]:
            if messagebox.askyesno("Update available", f"Version {info['version']} is available, but it has "
                                                       "no installer for this system. Open the release page?"):
                webbrowser.open(info["page"])
            return
        busy = " A device is connected and will be disconnected." if host.SESSIONS else ""
        if not messagebox.askyesno(
                "Update available",
                f"Version {info['version']} is available (you have {VERSION}).\n\n"
                f"Download and install it now?{busy}"):
            return
        self.start_update(info, auto=False)

    def start_update(self, info, auto):
        """Download, verify and install `info` (from updater.check)."""
        self.update_auto_run = auto
        if auto:
            self.note(f"Updating to version {info['version']} automatically.")
        self.update_busy = True
        self.update_btn.configure(state="disabled")
        self.update_label.configure(text=f"Downloading version {info['version']}…")

        def progress(got, size):
            self.events.put(("update_progress", (info["version"], got, size)))

        def work():
            try:
                self.events.put(("update_ready", (updater.download(info, progress), None)))
            except OSError as exc:
                self.events.put(("update_ready", (None, str(exc))))
        threading.Thread(target=work, daemon=True).start()

    def on_update_ready(self, path, error):
        self.update_busy = False
        self.update_btn.configure(state="normal")
        if error:
            self.update_label.configure(text=f"Version {VERSION} · update failed")
            self.note(f"Update failed: {error}")
            if not self.update_auto_run:
                messagebox.showwarning("Update failed", error)
            return
        if not WINDOWS:
            self.update_label.configure(text=f"Downloaded {path.name}")
            messagebox.showinfo("Update downloaded", f"Open {path.name} to install the new version.")
            updater.reveal(path)
            return
        try:
            updater.install(path)
        except OSError as exc:
            messagebox.showwarning("Update failed", str(exc))
            return
        self.note("Installing the update; Remote Desktop will start again by itself.")
        if self.update_auto_run:
            self.tray.notify("Updating Remote Desktop. It will start again in a moment.")
        self.resume_share = self.state == "running"  # the new version carries on sharing
        self.on_close(force=True)

    def toggle_auto_launch(self):
        try:
            autostart.set_enabled(self.auto_launch.get(), elevated=is_admin())
        except OSError as exc:
            self.auto_launch.set(autostart.is_enabled())
            messagebox.showwarning("Could not change startup", str(exc))
        self.launch_default_pending = False  # the user has chosen; keep their choice
        self.save()

    def apply_autostart(self):
        """Make sure 'open at sign-in' matches the checkbox, using the current program path.

        Runs on every start: it switches an older sign-in entry over to the administrator
        task (an administrator app cannot start from the plain sign-in list) and follows the
        app if it was reinstalled elsewhere. It is on by default the first time."""
        if not self.auto_launch.get():
            return
        admin = is_admin()

        def work():
            if WINDOWS and admin:
                try:
                    autostart.ensure_launch_task()
                except OSError:
                    pass
            try:
                if WINDOWS and not admin and autostart._task_exists():
                    pass  # already set up by the administrator copy; this copy cannot change it
                else:
                    autostart.set_enabled(True, elevated=admin)
                self.events.put(("autostart", None))
            except OSError as exc:
                self.events.put(("autostart", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    # ---- background (tray) ----
    def hide_window(self):
        """Closing the window keeps the app running (and sharing) in the background."""
        if not self.tray.available:
            self.root.iconify()  # no tray here (macOS): stay in the Dock / taskbar
            return
        self.root.withdraw()  # quietly: no "still running in the background" notification

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)  # bring it in front of other windows
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def quit_app(self):
        if host.SESSIONS:
            self.show_window()  # so the "a device is connected" question is visible
        self.on_close()

    # ---- start / stop ----
    def toggle(self):
        if self.state == "stopped":
            self.start()
        elif self.state == "running":
            self.stop()

    def start(self, quiet=False):
        """Start sharing. With quiet=True (automatic start) problems go to the log, not a dialog."""
        if self.state != "stopped":
            return

        def problem(title, text):
            if quiet:
                self.note(f"Sharing not started: {title}. {text}")
            else:
                messagebox.showwarning(title, text)

        password = self.password.get()
        if len(password) < MIN_PASSWORD:
            return problem("Password too short",
                           f"Use a password of at least {MIN_PASSWORD} characters, or click New.")
        if not rendezvous.is_id(self.my_id.get()):
            return problem("Invalid ID", "Your ID must be exactly 9 digits, or click New.")
        self.number = rendezvous.digits(self.my_id.get())
        self.my_id.set(rendezvous.pretty(self.number))
        try:
            port = int(self.port.get())
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            return problem("Invalid port", "Port must be a number between 1024 and 65535.")
        if self.internet.get() and not find_cloudflared():
            self.internet.set(False)
            return problem(
                "Internet access needs cloudflared",
                "Internet access uses Cloudflare's free tunnel program, which was not found. "
                "Install cloudflared, then tick the internet option and start again.")
        self.save()
        host.PASSWORD = password
        host.VIEW_ONLY = not self.control.get()
        self.state = "starting"
        self.refresh()
        self.server = Server(self.events)
        self.server.start(port)

    def stop(self):
        self.state = "stopping"
        self.refresh()
        self.cancel_tunnel_jobs()
        self.tunnel.stop()
        self.server.stop()
        if self.registered:  # mark the number as offline, in the background
            self.register("")
        self.registered = None

    def register(self, url):
        """Publish this PC's address ('' = offline) under its number, without blocking the window."""
        self.last_publish = time.monotonic()

        def work():
            try:
                rendezvous.publish(self.number, url)
                ok = True
            except OSError:
                ok = False
            if url:
                self.events.put(("registered", (ok, url)))
        threading.Thread(target=work, daemon=True).start()

    def on_started(self, port):
        self.state = "running"
        self.addr["local"].set(f"http://localhost:{port}")
        lan = host.local_addresses()
        self.addr["lan"].set(f"http://{lan[0]}:{port}" if lan else "No network connection found")
        mode = "view only" if host.VIEW_ONLY else "full control"
        self.note(f"Sharing started ({mode}).")
        if WINDOWS and not host.VIEW_ONLY:
            if not is_admin():
                self.note("Not running as administrator: Task Manager, UAC prompts and other "
                          "administrator windows cannot be controlled.")
            elif winagent.service_state() != "running":
                self.note("The background service is off: UAC prompts and the sign-in screen "
                          "cannot be controlled. Click Install service.")
        if self.internet.get():
            self.tunnel_port, self.tunnel_tries = port, 0
            self.start_tunnel()
        else:
            self.addr["net"].set("Off")
        self.refresh()

    # ---- internet tunnel: keep trying until it is up (DNS or network blips must not leave
    #      this computer unreachable until someone restarts sharing by hand) ----
    TUNNEL_WAIT = 60  # seconds for a new tunnel to report its address

    def start_tunnel(self):
        self.tunnel_job = None
        if self.state != "running" or not self.internet.get():
            return
        self.addr["net"].set("Creating secure address…")
        self.update_id()
        try:
            self.tunnel.start(find_cloudflared(), self.tunnel_port)
        except OSError as exc:
            return self.retry_tunnel(f"could not start it: {exc}")
        self.tunnel_watch = self.root.after(self.TUNNEL_WAIT * 1000, self.tunnel_too_slow)

    def tunnel_too_slow(self):
        self.tunnel_watch = None
        if self.state == "running" and not TUNNEL_ID.match(self.addr["net"].get()):
            self.tunnel.stop()
            self.retry_tunnel("no address after a minute")

    def retry_tunnel(self, reason):
        if self.state != "running" or not self.internet.get():
            return
        self.cancel_tunnel_jobs()
        self.tunnel_tries += 1
        delay = min(5 * 2 ** (self.tunnel_tries - 1), 60)
        self.addr["net"].set(f"Reconnecting to the internet in {delay} s…")
        self.update_id()
        self.note(f"Internet link down ({reason}); trying again in {delay} s.")
        self.tunnel_job = self.root.after(delay * 1000, self.start_tunnel)

    def cancel_tunnel_jobs(self):
        for name in ("tunnel_job", "tunnel_watch"):
            job = getattr(self, name, None)
            if job:
                self.root.after_cancel(job)
            setattr(self, name, None)

    def on_stopped(self):
        self.state = "stopped"
        self.server = None
        for var in self.addr.values():
            var.set("")
        self.my_id.set(rendezvous.pretty(self.number))
        self.note("Sharing stopped.")
        self.refresh()

    def update_id(self):
        """Your ID always stays this PC's 9-digit number. When it cannot be used right now (no
        internet link), a short note beside it says why and what to use instead."""
        net, lan = self.addr["net"].get(), self.addr["lan"].get()
        tunnel = TUNNEL_ID.match(net)
        self.id_ready = True
        note = ""
        if self.state != "running":
            self.id_note.configure(text="")
            self.toggle_show()  # not sharing: the box holds the editable number, leave it alone
            return
        ident = rendezvous.pretty(self.number)
        same_network = f"use {lan[len('http://'):]}" if lan.startswith("http://") else ""  # on the same network
        if tunnel and self.registered is False:
            note = "ID service down · use the Internet link"
        elif tunnel or net.startswith("Creating"):
            if not self.registered:
                ident, self.id_ready = "Creating ID…", False
        elif not lan.startswith("http://"):
            note = "No network connection"
        elif net == "Off":
            note = f"Internet off · {same_network}"
        else:  # the tunnel could not start or stopped
            note = f"No internet link · {same_network}"
        self.id_note.configure(text=note)
        self.my_id.set(ident)
        self.toggle_show()

    # ---- recent and saved computers ----
    @staticmethod
    def partner_key(text):
        text = text.strip()
        return rendezvous.digits(text) if rendezvous.is_id(text) else text.lower()

    @staticmethod
    def partner_label(entry):
        key = entry["key"]
        shown = rendezvous.pretty(key) if rendezvous.is_id(key) else key
        mark = "  🔒" if entry.get("pw") else ""
        return f"{entry['name']}  ·  {shown}{mark}" if entry.get("name") else shown + mark

    def refresh_partners(self):
        self.partner_box.configure(values=[self.partner_label(e) for e in self.partners])

    def find_partner(self, key):
        return next((e for e in self.partners if e["key"] == key), None)

    def pick_partner(self):
        index = self.partner_box.current()
        if not 0 <= index < len(self.partners):
            return
        entry = self.partners[index]
        key = entry["key"]
        self.partner.set(rendezvous.pretty(key) if rendezvous.is_id(key) else key)
        saved_pw = reveal_secret(entry["pw"]) if entry.get("pw") else ""
        self.partner_pw.set(saved_pw)
        self.remember_pw.set(bool(saved_pw))
        self.partner_box.icursor("end")

    def forget_partner(self):
        entry = self.find_partner(self.partner_key(self.partner.get()))
        if not entry:
            return
        self.partners.remove(entry)
        self.partner.set("")
        self.partner_pw.set("")
        self.remember_pw.set(False)
        self.refresh_partners()
        self.save()

    def remember_partner(self, key, password):
        entry = self.find_partner(key) or {"key": key, "name": "", "pw": ""}
        if entry in self.partners:
            self.partners.remove(entry)
        entry["last"] = time.time()
        entry["pw"] = protect_secret(password) if self.remember_pw.get() and password else ""
        self.partners.insert(0, entry)
        del self.partners[MAX_PARTNERS:]
        self.refresh_partners()
        self.save()

    def name_partner(self, key, host):
        entry = self.find_partner(key)
        if entry and entry.get("name") != host:
            entry["name"] = host
            self.refresh_partners()
            self.save()

    def connect_partner(self):
        text, password = self.partner.get(), self.partner_pw.get()
        self.partner_msg.configure(text="")
        if rendezvous.is_id(text):  # a number: look up its current address first
            self.connect_btn.configure(state="disabled")
            key = self.partner_key(text)

            def work():
                try:
                    self.events.put(("lookup", (rendezvous.lookup(text), password, None, key)))
                except OSError as exc:
                    self.events.put(("lookup", (None, password, str(exc), key)))
            threading.Thread(target=work, daemon=True).start()
            return
        url = partner_url(text)
        if not url:
            self.partner_msg.configure(
                text="That doesn't look like an ID. Use the 9-digit ID from the other computer "
                     "(or a network address such as 192.168.1.20:8765).")
            return
        self.launch_viewer(url, password, self.partner_key(text))

    def launch_viewer(self, url, password, key=""):
        self.note("Opening the viewer window.")
        if key:
            self.remember_partner(key, password)
        self.viewer.open(to_ws(url), password, key)
        if not self.remember_pw.get():
            self.partner_pw.set("")

    def refresh(self):
        self.update_id()
        running = self.state == "running"
        label = {"stopped": "Not sharing", "starting": "Starting…",
                 "running": "Sharing this screen", "stopping": "Stopping…"}[self.state]
        self.status.configure(text=label)
        pal = self.theme.pal
        self.dot.itemconfigure(self.dot_id, fill=pal["green"] if running else pal["grey"] if self.state == "stopped"
                               else pal["amber"])
        self.theme.set_accent(self.toggle_btn, "red" if running else "accent" if self.state == "stopped" else "amber")
        self.toggle_btn.configure(text="Stop sharing" if running else label if self.state != "stopped"
                                  else "Start sharing",
                                  state="normal" if self.state in ("stopped", "running") else "disabled")
        editable = "normal" if self.state == "stopped" else "disabled"
        for widget in (self.pw_entry, self.port_entry, self.new_btn, self.new_id_btn,
                       self.control_chk, self.internet_chk):
            widget.configure(state=editable)
        self.id_entry.configure(state="normal" if self.state == "stopped" else "readonly")
        self.kick_btn.configure(state="normal" if running else "disabled")
        self.tray.update(running, "Remote Desktop - " + label)

    # ---- event pump ----
    def poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self.log(data)
                elif kind == "tray":
                    if data == "open":
                        self.show_window()
                    elif data == "toggle":
                        self.toggle()
                    elif data == "quit":
                        self.quit_app()
                        if self.closed:
                            return  # the window is gone (it stays if the user cancelled)
                elif kind == "autostart":
                    if data:
                        self.auto_launch.set(autostart.is_enabled())
                        self.note(f"Could not set up opening at sign-in: {data}")
                    elif self.launch_default_pending:
                        self.launch_default_pending = False
                        self.save()
                        self.note("Remote Desktop will now open in the background when you sign in.")
                elif kind == "service":
                    if data:
                        messagebox.showwarning("Service", data)
                    else:
                        self.note("Background service " + ("installed and started." if winagent.service_state()
                                                           == "running" else "removed."))
                    self.refresh_service()
                elif kind == "update":
                    self.on_update_info(*data)
                elif kind == "update_progress":
                    version, got, size = data
                    self.update_label.configure(text=f"Downloading version {version}… {got * 100 // max(size, 1)}%")
                elif kind == "update_ready":
                    self.on_update_ready(*data)
                    if self.closed:
                        return
                elif kind == "chat":
                    self.open_chat()
                    self.chat_append(f"[{time.strftime('%H:%M')}] {data[0]}: {data[1]}")
                    self.root.bell()
                elif kind == "started":
                    self.on_started(data)
                elif kind == "stopped":
                    self.on_stopped()
                elif kind == "error":
                    self.state = "stopped"
                    self.server = None
                    self.refresh()
                    messagebox.showerror("Could not start", data)
                elif kind == "tunnel" and self.state == "running":
                    self.cancel_tunnel_jobs()
                    self.tunnel_tries = 0
                    self.addr["net"].set(data)
                    self.note("Internet address ready.")
                    self.registered = None
                    self.register(data)
                    self.update_id()
                elif kind == "registered" and self.state == "running" and data[1] == self.addr["net"].get():
                    if self.registered is None and not data[0]:
                        self.note("The ID service could not be reached; showing a word ID instead.")
                    self.registered = data[0] or self.registered is True  # one failed refresh changes nothing
                    self.update_id()
                elif kind == "partner_name":
                    self.name_partner(*data)
                elif kind == "lookup":
                    url, password, error, key = data
                    self.connect_btn.configure(state="normal")
                    if error:
                        self.partner_msg.configure(text="Could not reach the ID service. Check your internet connection.")
                    elif not url:
                        self.partner_msg.configure(text="No computer is sharing under that ID right now.")
                    else:
                        self.launch_viewer(url, password, key)
                elif kind == "tunnel_closed" and self.state == "running":
                    self.registered = None
                    self.retry_tunnel(data or "the tunnel stopped")
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def tick(self):
        # Re-post the address every half hour so the number never expires at the ID service.
        net = self.addr["net"].get()
        if self.state == "running" and TUNNEL_ID.match(net) and time.monotonic() - self.last_publish > 1800:
            self.register(net)
        rows = [(s["ip"], time.strftime("%H:%M:%S", time.localtime(s["since"])))
                for s in list(host.SESSIONS.values())]
        current = [tuple(self.tree.item(i, "values")) for i in self.tree.get_children()]
        if rows != current:
            self.tree.delete(*self.tree.get_children())
            for row in rows:
                self.tree.insert("", "end", values=row)
        self.root.after(1000, self.tick)

    def on_close(self, force=False, handoff=False):
        if not force and host.SESSIONS and not messagebox.askokcancel(
                "Stop sharing?", "A device is connected. Closing will disconnect it."):
            return
        if self.state == "stopped" and rendezvous.is_id(self.my_id.get()):
            self.number = rendezvous.digits(self.my_id.get())  # keep an ID typed but not yet used
        self.save()
        self.tunnel.stop()
        if self.server:
            self.server.stop()
        # On a handoff (restart as administrator) the new copy keeps the ID, so don't mark it offline.
        if self.registered and not handoff:
            try:
                rendezvous.publish(self.number, "", timeout=3)  # mark the number as offline
            except OSError:
                pass
        self.tray.stop()
        self.instance.stop()
        self.closed = True
        self.root.destroy()


def selftest():
    """Check that a packaged build can load and use everything it needs (used by CI).
    Prints one line per check; returns 0 when all pass."""
    results = []

    def check(name, fn):
        try:
            results.append((True, name, fn() or ""))
        except Exception as exc:  # report every failure, not just the first
            results.append((False, name, f"{type(exc).__name__}: {exc}"))

    def tunnel():
        path = find_cloudflared()
        if not path:
            raise FileNotFoundError("cloudflared not bundled")
        return path

    def window():
        root = tk.Tk()
        root.withdraw()
        App(root)
        root.update()
        root.destroy()
        return "built"

    monitors = []
    check("screen layout", lambda: f"{len(monitors.extend(host.list_monitors()) or monitors)} monitor(s)")
    check("screen capture", lambda: f"{len(host.grab_update(0, monitors[0], 1.0, 60, None, -1)[2])} bytes")
    check("input backend", lambda: host.inp.__name__ + " " + str(host.inp.cursor_info()))
    check("viewer page", lambda: f"{len((HERE / 'viewer.html').read_bytes())} bytes")
    check("icon", lambda: f"{(HERE / 'assets' / 'icon-256.png').stat().st_size} bytes")
    check("tunnel program", tunnel)
    check("app window", window)
    for ok, name, detail in results:
        print("OK  " if ok else "FAIL", name, "-", detail, flush=True)
    return 0 if all(ok for ok, _, _ in results) else 1


def setup_tasks():
    """Installer step (as administrator): the no-prompt launch task and, unless the user turned it
    off, opening at sign-in, so the app never needs a UAC prompt to start."""
    try:
        autostart.ensure_launch_task()
        saved = load_settings()
        turned_off = saved.get("launch_default_done") and not autostart.is_enabled()
        if not turned_off:
            autostart.set_enabled(True, elevated=True)
        return 0
    except OSError as exc:
        winagent.log(f"--setup-tasks: {exc}")
        return 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if WINDOWS:  # background service roles (winagent.py); no window
        if "--service" in sys.argv:
            sys.exit(winagent.run_service())
        if "--agent" in sys.argv:
            rest = sys.argv[sys.argv.index("--agent") + 1:]
            sys.exit(winagent.run_agent(int(rest[0]) if rest and rest[0].isdigit() else None))
        if "--setup-tasks" in sys.argv:  # run by the installer, as administrator
            sys.exit(setup_tasks())
        for flag, fn in (("--install-service", winagent.install_service),
                         ("--uninstall-service", winagent.uninstall_service)):
            if flag in sys.argv:  # used by the installer and uninstaller
                try:
                    fn()
                    sys.exit(0)
                except OSError as exc:
                    winagent.log(f"{flag}: {exc}")
                    sys.exit(1)
        try:  # its own taskbar button and icon, not Python's (when run from source)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RemoteDesktop.App")
        except (AttributeError, OSError):
            pass
    if "--restarted" not in sys.argv and "--share" not in sys.argv:
        # Already running (perhaps hidden in the tray)? Bring that copy forward instead.
        # At sign-in (--minimized) just leave the running copy alone.
        if ask_running_copy("ping" if "--minimized" in sys.argv else "show"):
            return
    # Started without administrator rights (Start menu, sign-in entry, after an update): hand over
    # to the task the installer created, which starts the app with full rights and no UAC prompt.
    if WINDOWS and not is_admin() and "--no-elevate" not in sys.argv and autostart.launch_task_exists():
        if "--share" in sys.argv or "--restarted" in sys.argv:
            time.sleep(1.5)  # a copy that is handing over is still closing
        if autostart.start_elevated(minimized="--minimized" in sys.argv):
            return
    root = tk.Tk()
    App(root)

    root.mainloop()


if __name__ == "__main__":
    main()
