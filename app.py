"""Remote Desktop - the app window (Windows, Linux, macOS).

Share this computer (ID + password) or control another one. Run from source
with start.bat / `python app.py`, or install it with the packaged installer.
`--minimized` starts with the window minimised (used when opening at sign-in).
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

WINDOWS, MAC = sys.platform == "win32", sys.platform == "darwin"
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # also right when packaged
TUNNEL_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
MIN_PASSWORD = 10
GREEN, GREY, AMBER = "#22a35a", "#9aa0a6", "#d98a00"


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


class LocalViewer:
    """Serves the viewer page to this PC only, for controlling another computer.

    The page comes from this program, never from the remote address, so a wrong
    or forged address cannot show a fake login page. Each new window collects
    its connection details once, through a single-use token.
    """

    def __init__(self):
        self.httpd = None
        self.sessions = {}

    def start(self):
        if self.httpd:
            return
        sessions = self.sessions

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

            def do_GET(self):
                port = self.server.server_address[1]
                if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    return self.reply(403, b"forbidden")
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    return self.reply(200, (HERE / "viewer.html").read_bytes(), "text/html; charset=utf-8")
                if path.startswith("/session/"):
                    session = sessions.pop(path[len("/session/"):], None)
                    if session and session["expires"] > time.monotonic():
                        body = json.dumps({"ws": session["ws"], "password": session["password"]})
                        return self.reply(200, body.encode(), "application/json")
                self.reply(404, b"not found")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def open(self, ws_url, password):
        self.start()
        token = secrets.token_urlsafe(24)
        self.sessions[token] = {"ws": ws_url, "password": password, "expires": time.monotonic() + 60}
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
        found = False
        for line in proc.stderr:
            match = None if found else TUNNEL_URL.search(line)
            if match:
                found = True
                self.events.put(("tunnel", match.group(0)))
        proc.wait()
        if proc is self.proc:  # exited on its own, not stopped by us
            self.events.put(("tunnel_closed", None))

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
        self.viewer = LocalViewer()
        saved_id = str(saved.get("id", ""))
        self.number = saved_id if rendezvous.is_id(saved_id) else rendezvous.new_id()
        self.registered = None      # None = not yet known, True/False = number lookup works or not
        self.last_publish = 0.0
        self.auto_share = tk.BooleanVar(value=bool(saved.get("auto_share", False)))
        self.auto_launch = tk.BooleanVar(value=autostart.is_enabled())
        # Remote-admin mode: elevate the admin without a UAC prompt so remote admin tasks aren't blocked.
        saved_uac = saved.get("show_uac")
        if saved_uac is not None:
            self.show_uac = tk.BooleanVar(value=bool(saved_uac))
        else:
            self.show_uac = tk.BooleanVar(value=(secure_desktop.is_remote_admin_on() is True))

        root.title("Remote Desktop")
        try:  # window/taskbar icon; True also applies it to the chat window
            self.icon = tk.PhotoImage(file=str(HERE / "assets" / "icon-256.png"))
            root.iconphoto(True, self.icon)
        except tk.TclError:
            pass
        root.minsize(860, 0)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build()
        self.my_id.set(rendezvous.pretty(self.number))
        if WINDOWS and is_admin() and self.show_uac.get():
            try:
                secure_desktop.set_remote_admin(True)
            except OSError:
                pass
        if WINDOWS and secure_desktop.is_remote_admin_on() is True:
            self.note("Note: remote-admin mode is on — admin actions on this PC run without a UAC prompt.")
        self.refresh()
        if "--minimized" in sys.argv:
            root.iconify()
        if "--share" in sys.argv:  # restarted as administrator while sharing: wait for the old copy to exit
            root.after(2500, lambda: self.start(quiet=True))
        elif self.auto_share.get():
            root.after(600, lambda: self.start(quiet=True))
        root.after(150, self.poll)
        root.after(1000, self.tick)

    # ---- layout ----
    def build(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        head = ttk.Frame(main)
        head.grid(sticky="ew")
        head.columnconfigure(1, weight=1)
        self.dot = tk.Canvas(head, width=18, height=18, highlightthickness=0)
        self.dot_id = self.dot.create_oval(3, 3, 15, 15, fill=GREY, outline="")
        self.dot.grid(row=0, column=0, padx=(0, 8))
        self.status = ttk.Label(head, text="", font=("Segoe UI", 13, "bold"))
        self.status.grid(row=0, column=1, sticky="w")
        ttk.Button(head, text="Chat", width=8, command=self.open_chat).grid(row=0, column=2, padx=(0, 8))
        self.toggle_btn = ttk.Button(head, text="Start sharing", width=16, command=self.toggle)
        self.toggle_btn.grid(row=0, column=3)

        panes = ttk.Frame(main)
        panes.grid(sticky="ew", pady=(14, 0))
        panes.columnconfigure(0, weight=1, uniform="pane")
        panes.columnconfigure(1, weight=1, uniform="pane")
        big = ("Segoe UI", 11)

        allow = ttk.LabelFrame(panes, text="Allow remote control", padding=8)
        allow.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        allow.columnconfigure(0, weight=1)
        ttk.Label(allow, text="Give this ID and password to the person who will connect.",
                  wraplength=370, foreground="#555").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(allow, text="Your ID (9 digits)").grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 2))
        self.id_entry = ttk.Entry(allow, textvariable=self.my_id, font=big + ("bold",))
        self.id_entry.grid(row=2, column=0, sticky="ew")
        ttk.Button(allow, text="Copy", width=6,
                   command=lambda: self.id_ready and self.copy(self.my_id.get())
                   ).grid(row=2, column=1, padx=(6, 0))
        self.new_id_btn = ttk.Button(allow, text="New", width=6, command=self.new_number)
        self.new_id_btn.grid(row=2, column=2, padx=(6, 0))
        ttk.Label(allow, text="Password").grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 2))
        self.pw_entry = ttk.Entry(allow, textvariable=self.password, show="•", font=big)
        self.pw_entry.grid(row=4, column=0, sticky="ew")
        ttk.Button(allow, text="Copy", width=6,
                   command=lambda: self.copy(self.password.get())).grid(row=4, column=1, padx=(6, 0))
        self.new_btn = ttk.Button(allow, text="New", width=6, command=self.new_password)
        self.new_btn.grid(row=4, column=2, padx=(6, 0))
        ttk.Checkbutton(allow, text="Show ID and password", variable=self.show_pw, command=self.toggle_show
                        ).grid(row=5, column=0, sticky="w", pady=(6, 0))
        ttk.Label(allow, text="Both stay the same until you change them. Stop sharing to edit.",
                  wraplength=370, foreground="#555").grid(row=6, column=0, columnspan=3, sticky="w")

        ctrl = ttk.LabelFrame(panes, text="Control a remote computer", padding=8)
        ctrl.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ctrl.columnconfigure(0, weight=1)
        ttk.Label(ctrl, text="Enter the ID and password shown on the other computer.",
                  wraplength=370, foreground="#555").grid(row=0, column=0, sticky="w")
        ttk.Label(ctrl, text="Partner ID").grid(row=1, column=0, sticky="w", pady=(10, 2))
        partner_entry = ttk.Entry(ctrl, textvariable=self.partner, font=big)
        partner_entry.grid(row=2, column=0, sticky="ew")
        ttk.Label(ctrl, text="Password").grid(row=3, column=0, sticky="w", pady=(10, 2))
        partner_pw_entry = ttk.Entry(ctrl, textvariable=self.partner_pw, show="•", font=big)
        partner_pw_entry.grid(row=4, column=0, sticky="ew")
        for entry in (partner_entry, partner_pw_entry):
            entry.bind("<Return>", lambda _e: self.connect_partner())
        self.connect_btn = ttk.Button(ctrl, text="Connect", width=12, command=self.connect_partner)
        self.connect_btn.grid(row=5, column=0, sticky="e", pady=(8, 0))
        self.partner_msg = ttk.Label(ctrl, text="", wraplength=370, foreground="#c0392b")
        self.partner_msg.grid(row=6, column=0, sticky="w")

        access = ttk.LabelFrame(main, text="Options", padding=8)
        access.grid(sticky="ew", pady=(8, 0))
        self.control_chk = ttk.Checkbutton(
            access, text="Allow mouse and keyboard control", variable=self.control)
        self.control_chk.grid(row=0, column=0, sticky="w")
        self.internet_chk = ttk.Checkbutton(
            access, text="Allow connections from the internet", variable=self.internet)
        self.internet_chk.grid(row=0, column=1, sticky="w", padx=(24, 0))
        access.columnconfigure(2, weight=1)
        ttk.Label(access, text="Port").grid(row=0, column=3, sticky="e", padx=(0, 8))
        self.port_entry = ttk.Entry(access, textvariable=self.port, width=8)
        self.port_entry.grid(row=0, column=4, sticky="e")
        ttk.Checkbutton(access, text="Start sharing when the app opens", variable=self.auto_share,
                        command=self.save).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Checkbutton(access, text="Open the app when I sign in to this computer",
                        variable=self.auto_launch, command=self.toggle_auto_launch
                        ).grid(row=1, column=1, columnspan=4, sticky="w", padx=(24, 0), pady=(4, 0))
        if WINDOWS and is_admin():
            ttk.Label(access, text="Running as administrator: Task Manager and other administrator "
                                   "windows can be controlled.", foreground="#22a35a"
                      ).grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))
        elif WINDOWS:
            admin = ttk.Frame(access)
            admin.grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))
            ttk.Button(admin, text="Restart as administrator", command=self.restart_as_admin).pack(side="left")
            ttk.Label(admin, text="  Needed to control Task Manager and other administrator windows.",
                      foreground="#555").pack(side="left")
        if WINDOWS:
            self.uac_chk = ttk.Checkbutton(
                access, text="Run admin tasks remotely without a UAC prompt (less secure)",
                variable=self.show_uac, command=self.toggle_uac)
            self.uac_chk.grid(row=3, column=0, columnspan=5, sticky="w", pady=(4, 0))

        links = ttk.LabelFrame(main, text="Or open one of these in any browser (no app needed)", padding=8)
        links.grid(sticky="ew", pady=(8, 0))
        links.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate((("local", "This PC"), ("lan", "Same network"), ("net", "Internet"))):
            ttk.Label(links, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            self.addr_entries[key] = ttk.Entry(links, textvariable=self.addr[key], state="readonly")
            self.addr_entries[key].grid(row=row, column=1, sticky="ew", pady=2)
            ttk.Button(links, text="Copy", width=7,
                       command=lambda k=key: self.copy(self.addr[k].get(), url=True)
                       ).grid(row=row, column=2, padx=(8, 0), pady=2)

        bottom = ttk.Frame(main)  # devices and activity share one row to keep the window short
        bottom.grid(sticky="nsew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1, uniform="bottom")
        bottom.columnconfigure(1, weight=1, uniform="bottom")
        bottom.rowconfigure(0, weight=1)

        devices = ttk.LabelFrame(bottom, text="Connected devices", padding=8)
        devices.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        devices.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(devices, columns=("ip", "since"), show="headings", height=2, selectmode="none")
        self.tree.heading("ip", text="Address", anchor="w")
        self.tree.heading("since", text="Since", anchor="w")
        self.tree.column("ip", width=150, anchor="w")
        self.tree.column("since", width=80, anchor="w")
        self.tree.grid(row=0, column=0, sticky="ew")
        self.kick_btn = ttk.Button(devices, text="Disconnect all", command=self.disconnect_all)
        self.kick_btn.grid(row=0, column=1, sticky="new", padx=(10, 0))
        ttk.Button(devices, text="Received files", command=self.open_received
                   ).grid(row=0, column=1, sticky="sew", padx=(10, 0))

        activity = ttk.LabelFrame(bottom, text="Activity", padding=8)
        activity.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(0, weight=1)
        main.rowconfigure(4, weight=1)
        self.logbox = tk.Text(activity, height=3, width=30, state="disabled", wrap="word",
                              relief="flat", font=("Consolas", 9))
        scroll = ttk.Scrollbar(activity, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=scroll.set)
        self.logbox.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

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
        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.chat_box = tk.Text(frame, height=12, width=48, state="disabled", wrap="word",
                                relief="flat", font=("Segoe UI", 10))
        self.chat_box.grid(row=0, column=0, columnspan=2, sticky="nsew")
        entry = ttk.Entry(frame, textvariable=self.chat_text, font=("Segoe UI", 10))
        entry.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        entry.bind("<Return>", lambda _e: self.send_chat())
        ttk.Button(frame, text="Send", command=self.send_chat).grid(row=1, column=1, padx=(8, 0), pady=(8, 0))
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
                          show_uac=self.show_uac.get())
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

    def toggle_uac(self):
        """Move UAC prompts off (or back onto) the secure desktop so a remote viewer can see and control them."""
        if not is_admin():
            self.show_uac.set(not self.show_uac.get())  # keep previous state
            if messagebox.askyesno(
                    "Administrator rights required",
                    "Controlling UAC prompts remotely requires running Remote Desktop as administrator.\n\n"
                    "Would you like to restart the app as administrator now?"):
                self.restart_as_admin()
            return

        if self.show_uac.get():
            if not messagebox.askokcancel(
                    "Run admin tasks remotely without a UAC prompt?",
                    "A UAC prompt can be seen remotely but never clicked — Windows protects it "
                    "from injected input. To use this PC's admin tasks remotely, Windows must stop "
                    "showing the prompt.\n\nThis sets \"elevate without prompting\" for the signed-in "
                    "administrator: admin actions on this PC will run WITHOUT asking. That is a real "
                    "security reduction — only turn it on for a machine you use for remote "
                    "administration.\n\nUnchecking this restores the normal UAC prompt."):
                self.show_uac.set(False)
                return
            try:
                secure_desktop.set_remote_admin(True)
                self.save()
                self.note("Remote-admin mode on: admin actions now run without a UAC prompt.")
            except OSError as exc:
                self.show_uac.set(False)
                messagebox.showwarning(
                    "Could not change the setting",
                    "This needs the app to be running as administrator.\n\n" + str(exc))
        else:
            try:
                secure_desktop.set_remote_admin(False)
                self.save()
                self.note("Normal UAC prompt restored.")
            except OSError as exc:
                messagebox.showwarning("Could not restore the setting", str(exc))

    def toggle_auto_launch(self):
        try:
            autostart.set_enabled(self.auto_launch.get(), elevated=is_admin())
        except OSError as exc:
            self.auto_launch.set(autostart.is_enabled())
            messagebox.showwarning("Could not change startup", str(exc))

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
        if WINDOWS and not host.VIEW_ONLY and not is_admin():
            self.note("WARNING: Not running as administrator! You will lose control if a UAC prompt appears.")
        elif WINDOWS and is_admin() and self.show_uac.get():
            try:
                secure_desktop.set_remote_admin(True)
            except OSError:
                pass
        if self.internet.get():
            self.addr["net"].set("Creating secure address…")
            try:
                self.tunnel.start(find_cloudflared(), port)
            except OSError as exc:
                self.addr["net"].set("Could not start the tunnel")
                self.note(f"Tunnel failed to start: {exc}")
        else:
            self.addr["net"].set("Off")
        self.refresh()

    def on_stopped(self):
        self.state = "stopped"
        self.server = None
        for var in self.addr.values():
            var.set("")
        self.my_id.set(rendezvous.pretty(self.number))
        self.note("Sharing stopped.")
        self.refresh()

    def update_id(self):
        """The ID to hand out: this PC's number, or its network address without internet access."""
        net, lan = self.addr["net"].get(), self.addr["lan"].get()
        tunnel = TUNNEL_ID.match(net)
        self.id_ready = True
        if self.state != "running":
            self.toggle_show()  # not sharing: the box holds the editable number, leave it alone
            return
        if tunnel and self.registered:
            ident = rendezvous.pretty(self.number)
        elif tunnel and self.registered is False:
            ident = tunnel.group(1)  # number service unreachable: fall back to the tunnel's name
        elif tunnel or net.startswith("Creating"):
            ident, self.id_ready = "Creating ID…", False
        elif lan.startswith("http://"):
            ident = lan[len("http://"):]
        else:
            ident, self.id_ready = "No network connection", False
        self.my_id.set(ident)
        self.toggle_show()

    def connect_partner(self):
        text, password = self.partner.get(), self.partner_pw.get()
        self.partner_msg.configure(text="")
        if rendezvous.is_id(text):  # a number: look up its current address first
            self.connect_btn.configure(state="disabled")

            def work():
                try:
                    self.events.put(("lookup", (rendezvous.lookup(text), password, None)))
                except OSError as exc:
                    self.events.put(("lookup", (None, password, str(exc))))
            threading.Thread(target=work, daemon=True).start()
            return
        url = partner_url(text)
        if not url:
            self.partner_msg.configure(
                text="That doesn't look like an ID. Use the 9-digit ID from the other computer "
                     "(or a network address such as 192.168.1.20:8765).")
            return
        self.launch_viewer(url, password)

    def launch_viewer(self, url, password):
        ws_url = url.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/ws"
        self.note("Opening the viewer window.")
        self.viewer.open(ws_url, password)
        self.partner_pw.set("")

    def refresh(self):
        self.update_id()
        running = self.state == "running"
        label = {"stopped": "Not sharing", "starting": "Starting…",
                 "running": "Sharing this screen", "stopping": "Stopping…"}[self.state]
        self.status.configure(text=label)
        self.dot.itemconfigure(self.dot_id, fill=GREEN if running else GREY if self.state == "stopped" else AMBER)
        self.toggle_btn.configure(text="Stop sharing" if running else "Start sharing",
                                  state="normal" if self.state in ("stopped", "running") else "disabled")
        editable = "normal" if self.state == "stopped" else "disabled"
        for widget in (self.pw_entry, self.port_entry, self.new_btn, self.new_id_btn,
                       self.control_chk, self.internet_chk):
            widget.configure(state=editable)
        self.id_entry.configure(state="normal" if self.state == "stopped" else "readonly")
        self.kick_btn.configure(state="normal" if running else "disabled")

    # ---- event pump ----
    def poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self.log(data)
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
                elif kind == "lookup":
                    url, password, error = data
                    self.connect_btn.configure(state="normal")
                    if error:
                        self.partner_msg.configure(text="Could not reach the ID service. Check your internet connection.")
                    elif not url:
                        self.partner_msg.configure(text="No computer is sharing under that ID right now.")
                    else:
                        self.launch_viewer(url, password)
                elif kind == "tunnel_closed" and self.state == "running":
                    self.addr["net"].set("Tunnel stopped - restart sharing to reconnect")
                    self.update_id()
                    self.note("The internet tunnel stopped unexpectedly.")
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


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
