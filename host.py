"""Remote desktop host (Windows, Linux, macOS).

Run this on the computer you want to control, then open the printed URL in a
browser on another device and enter the password.

    .venv\\Scripts\\python.exe host.py
    .venv\\Scripts\\python.exe host.py --password "my secret" --port 8765
    .venv\\Scripts\\python.exe host.py --cert cert.pem --key key.pem   (HTTPS/WSS)

The host runs as a visible console program and logs every connection.
Press Ctrl+C to stop it.
"""

import argparse
import asyncio
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

import audio
import capture

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # also right when packaged

# Mouse/keyboard injection and cursor lookup differ per operating system.
if sys.platform == "win32":
    import input_win as inp
    import agent_client  # the SYSTEM agent, for UAC prompts and the lock screen (winagent.py)
    import drop_target   # which folder a dropped file lands in
else:
    import input_pynput as inp
    agent_client = drop_target = None

# --------------------------------------------------------------------------
# Screen capture (all capture happens on one dedicated thread)
# --------------------------------------------------------------------------

CAPTURE = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
_grabber = None


def _screen():
    global _grabber
    if _grabber is None:
        _grabber = capture.Grabber()
    return _grabber


def list_monitors():
    if agent_client and agent_client.available():
        try:
            return agent_client.monitors()
        except OSError:
            agent_client.mark_down()
    return _screen().monitors()


def grab_any(index, monitor, scale, quality, previous, previous_version, earliest=0.0, latest=0.0):
    """grab_update through the agent when it is running (it can see the secure desktop),
    otherwise here. A stream that loses the agent carries on locally from a full picture."""
    if agent_client and (isinstance(previous, agent_client.View)
                         or (previous is None and agent_client.available())):
        try:
            return agent_client.grab(index, monitor, scale, quality, previous, previous_version,
                                     earliest, latest)
        except OSError:
            agent_client.mark_down()
            previous, previous_version = None, -1
    return grab_update(index, monitor, scale, quality, previous, previous_version, earliest, latest)


def is_fast(index, frame):
    if agent_client and isinstance(frame, agent_client.View):
        return frame.fast
    return _screen().fast(index)


CAPTURE_TIMEOUT = 5.0  # normal capture calls take milliseconds


class CaptureStuck(Exception):
    """A capture call did not return in time; the capture thread has been replaced."""


async def capture_call(fn, *args):
    """Run `fn` on the capture thread without ever waiting on it for ever.

    If the thread hangs (a graphics driver or capture library that never returns), it is
    abandoned and a fresh one is started, so one stuck capture cannot block every login.
    """
    global CAPTURE, _grabber
    executor = CAPTURE
    future = asyncio.get_running_loop().run_in_executor(executor, fn, *args)
    try:
        return await asyncio.wait_for(future, CAPTURE_TIMEOUT)
    except asyncio.TimeoutError:
        if executor is CAPTURE:  # the first caller to notice replaces the thread
            log("Screen capture stopped responding; restarting it (DirectX capture paused for a minute).")
            capture.DX_BLOCKED_UNTIL = time.monotonic() + 60
            CAPTURE = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
            _grabber = None
        raise CaptureStuck() from None


def grab_update(index, monitor, scale, quality, previous, previous_version, earliest=0.0, latest=0.0):
    """Return (picture, version, message). message is None when the screen has not changed.

    Frame pacing happens here, on the capture thread, because its sleep is precise;
    the event loop's timers on Windows only tick every ~16 ms, which would halve 60 fps.
    Waits until `earliest`, then until the screen changes or `latest` (time.perf_counter values).
    The fourth value is when the returned capture began, which the caller paces from
    (capturing and encoding take several milliseconds themselves).
    """
    grabber = _screen()
    while True:
        now = time.perf_counter()
        if now < earliest:
            time.sleep(earliest - now)
            now = time.perf_counter()
        version, frame = grabber.frame(index, monitor)
        if previous is None or version != previous_version:
            if scale < 0.999:
                frame = capture.scaled(frame, scale)
            update = capture.build_update(frame, previous, quality)
            if update:
                return frame, version, update, now
            previous, previous_version = frame, version  # redrawn, but identical
        if not grabber.fast(index) or time.perf_counter() >= latest:
            return previous, previous_version, None, now
        time.sleep(0.002)


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

PASSWORD = ""
VIEW_ONLY = False
MAX_FAILS, LOCKOUT_SECONDS = 5, 60
GLOBAL_MAX_FAILS = 30  # across all addresses, in case an attacker rotates IPs
fails = {}  # ip (or "*" for everyone) -> [count, locked_until]
LOG_SINK = None  # the control panel sets this to receive log lines
SESSIONS = {}  # websocket -> {"ip": str, "since": float} for authenticated clients


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    if LOG_SINK:
        LOG_SINK(line)
    else:
        print(line, flush=True)


def client_ip(ws):
    """Peer address. Behind the local Cloudflare tunnel, use the visitor's real IP."""
    ip = ws.remote_address[0]
    if ip in ("127.0.0.1", "::1"):
        forwarded = ws.request.headers.get("CF-Connecting-IP")
        if forwarded:
            return forwarded.strip()[:64]
    return ip


def locked_out(ip):
    now = time.monotonic()
    return any(e and e[1] > now for e in (fails.get(ip), fails.get("*")))


def record_failure(ip):
    for key, limit in ((ip, MAX_FAILS), ("*", GLOBAL_MAX_FAILS)):
        entry = fails.setdefault(key, [0, 0.0])
        entry[0] += 1
        if entry[0] >= limit:
            entry[0] = 0
            entry[1] = time.monotonic() + LOCKOUT_SECONDS
            who = ip if key == ip else "everyone"
            log(f"Too many wrong passwords: {who} locked out for {LOCKOUT_SECONDS}s")


class Session:
    def __init__(self):
        self.monitor = 0
        self.quality = 60
        self.scale = 1.0
        self.fps = 30
        self.pending = 0      # screen updates sent but not yet acknowledged
        self.frame = None     # the picture the viewer currently has
        self.version = -1
        self.wake = asyncio.Event()   # set when input arrives, to capture again at once
        self.active = 0.0     # when the screen last changed or input last arrived
        self.clip_seq = inp.clipboard_seq()  # only things copied during the session are shared
        self.clip_text = None
        self.keys = set()
        self.buttons = set()
        self.ip = ""
        self.cursor = None    # last pointer state sent to the viewer
        self.capture_error = False  # so a repeating capture error is logged once
        self.upload = None   # file currently being received from the viewer
        self.download = None  # task sending a file to the viewer
        self.io = inp         # mouse/keyboard/pointer: here, or through the agent
        self.audio_task = None  # sends this computer's sound while the viewer has it on


async def authenticate(ws, ip):
    nonce = secrets.token_hex(16)
    await ws.send(json.dumps({"t": "hello", "nonce": nonce}))
    try:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
        mac = msg["mac"] if msg.get("t") == "auth" else ""
    except Exception:
        return False
    expected = hmac.new(PASSWORD.encode(), nonce.encode(), "sha256").hexdigest()
    if isinstance(mac, str) and hmac.compare_digest(mac.encode(), expected.encode()):
        fails.pop(ip, None)
        return True
    record_failure(ip)
    await asyncio.sleep(1)
    return False


MAX_IN_FLIGHT = 3     # screen updates the viewer may have outstanding
IDLE_AFTER = 1.5      # seconds without change or input before slow capture eases off
IDLE_INTERVAL = 0.1
POLL_WINDOW = 0.016   # how long one capture call may wait for the screen to change
MAX_CLIP = 200_000    # characters of clipboard text carried either way


async def stream(ws, st, monitors):
    loop = asyncio.get_running_loop()
    earliest = 0.0  # when the next screen update may be sent (time.perf_counter)
    while True:
        started = loop.time()
        fast = is_fast(st.monitor, st.frame)
        if st.wake.is_set():  # input or an acknowledgement arrived
            st.wake.clear()
        if st.pending < MAX_IN_FLIGHT:
            try:
                st.frame, st.version, update, captured_at = await capture_call(
                    grab_any, st.monitor, monitors[st.monitor], st.scale, st.quality,
                    st.frame, st.version, earliest, time.perf_counter() + POLL_WINDOW)
            except CaptureStuck:
                continue  # the capture thread was replaced; carry on with the new one
            except Exception as exc:  # a capture failure must not end the session
                if not st.capture_error:
                    st.capture_error = True
                    log(f"Screen capture error: {type(exc).__name__}: {exc}")
                await asyncio.sleep(0.5)
                continue
            st.capture_error = False
            if update:
                st.pending += 1
                st.active = started
                earliest = captured_at + 1 / st.fps - 0.004  # a little early, to catch the next refresh
                await ws.send(update)  # first byte: 2 = screen regions, 1 = file data
            elif not fast:  # slow capture (no DirectX): pause between attempts, longer when idle
                idle = started - st.active > IDLE_AFTER
                try:
                    await asyncio.wait_for(st.wake.wait(), IDLE_INTERVAL if idle else 1 / st.fps)
                    st.active = loop.time()  # woken by input
                except asyncio.TimeoutError:
                    pass
        else:  # the viewer is behind: wait for it to acknowledge
            try:
                await asyncio.wait_for(st.wake.wait(), 0.05)
            except asyncio.TimeoutError:
                pass
        if not VIEW_ONLY:  # text copied on this computer goes to the viewer's clipboard
            seq = inp.clipboard_seq()
            if seq != st.clip_seq:
                st.clip_seq = seq
                text = inp.clipboard_get()
                if text and text != st.clip_text and len(text) <= MAX_CLIP:
                    st.clip_text = text
                    await ws.send(json.dumps({"t": "clip", "s": text}))
        cursor = await asyncio.get_running_loop().run_in_executor(None, st.io.cursor_info) \
            if st.io is not inp else inp.cursor_info()  # the picture has no pointer in it, so report it separately
        if cursor:
            mon = monitors[st.monitor]
            state = (round((cursor[0] - mon["left"]) / max(mon["width"] - 1, 1), 4),
                     round((cursor[1] - mon["top"]) / max(mon["height"] - 1, 1), 4), cursor[2], cursor[3])
            if state != st.cursor:
                st.cursor = state
                await ws.send(json.dumps({"t": "cur", "x": state[0], "y": state[1],
                                          "c": state[2], "v": state[3]}))


def handle_input(st, monitors, msg):
    t = msg.get("t")
    mon = monitors[st.monitor]
    io = st.io
    if t == "m":
        io.mouse_move(mon, float(msg["x"]), float(msg["y"]))
    elif t in ("d", "u"):
        button, down = int(msg["b"]), t == "d"
        if button not in inp.BUTTONS:
            return
        io.mouse_move(mon, float(msg["x"]), float(msg["y"]))
        io.mouse_button(button, down)
        (st.buttons.add if down else st.buttons.discard)(button)
    elif t == "w":
        io.mouse_wheel(float(msg.get("dx", 0)), float(msg.get("dy", 0)))
    elif t == "k":
        code, down = str(msg["c"]), bool(msg["down"])
        if io.key_event(code, down):
            (st.keys.add if down else st.keys.discard)(code)
    elif t == "text":
        io.type_text(str(msg["s"])[:20000])
    elif t == "clip":  # text copied on the viewer's computer, about to be pasted here
        text = str(msg["s"])[:MAX_CLIP]
        if text and inp.clipboard_set(text):
            st.clip_text = text
            st.clip_seq = inp.clipboard_seq()
    elif t == "rel":
        release_all(st)


def release_all(st):
    for code in list(st.keys):
        st.io.key_event(code, False)
    for button in list(st.buttons):
        st.io.mouse_button(button, False)
    st.keys.clear()
    st.buttons.clear()


# --------------------------------------------------------------------------
# File transfer
# --------------------------------------------------------------------------

CHUNK = 256 * 1024
MAX_DOWNLOAD = 2 * 1024 ** 3
RECEIVED_DIR = Path.home() / "Downloads" / "Remote Desktop"
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                  *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def fmt_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def safe_name(name):
    """Reduce a name sent by the viewer to a plain, valid Windows file name."""
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")[:200]
    if not name or name.split(".")[0].upper() in RESERVED_NAMES:
        name = "_" + name
    return name


def unique_path(folder, name):
    path = folder / name
    stem, suffix = path.stem, path.suffix
    counter = 1
    while path.exists():
        path = folder / f"{stem} ({counter}){suffix}"
        counter += 1
    return path


def list_dir(path):
    if not path:  # top level: common folders and drives
        home = Path.home()
        RECEIVED_DIR.mkdir(parents=True, exist_ok=True)
        entries = [{"n": label, "p": str(p), "d": True}
                   for label, p in (("Received files", RECEIVED_DIR), ("Desktop", home / "Desktop"),
                                    ("Documents", home / "Documents"), ("Downloads", home / "Downloads"))
                   if p.is_dir()]
        if sys.platform == "win32":
            entries += [{"n": f"{c}:\\", "p": f"{c}:\\", "d": True}
                        for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
        else:
            entries += [{"n": "Home", "p": str(home), "d": True}, {"n": "/", "p": "/", "d": True}]
        return {"path": "", "parent": None, "entries": entries}
    folder = Path(path)
    entries = []
    with os.scandir(folder) as it:
        for entry in it:
            try:
                is_dir = entry.is_dir()
                entries.append({"n": entry.name, "p": entry.path, "d": is_dir,
                                "s": 0 if is_dir else entry.stat().st_size})
            except OSError:
                continue
            if len(entries) >= 5000:
                break
    entries.sort(key=lambda e: (not e["d"], e["n"].lower()))
    return {"path": str(folder), "parent": "" if folder.parent == folder else str(folder.parent),
            "entries": entries}


def upload_abort(st):
    up, st.upload = st.upload, None
    if up:
        up["f"].close()
        up["tmp"].unlink(missing_ok=True)


def upload_start(st, msg):
    upload_abort(st)
    size = int(msg["size"])
    if size < 0:
        raise ValueError("bad size")
    RECEIVED_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(RECEIVED_DIR).free < size + 200 * 1024 * 1024:
        raise OSError("Not enough free disk space on the remote computer")
    tmp = RECEIVED_DIR / f".{secrets.token_hex(6)}.part"
    at = None  # where on the screen it was dropped (fractions of the shown monitor)
    if "x" in msg and "y" in msg:
        x, y = float(msg["x"]), float(msg["y"])
        if 0 <= x <= 1 and 0 <= y <= 1:
            at = (x, y)
    st.upload = {"id": msg["id"], "name": safe_name(msg["name"]), "size": size, "got": 0,
                 "tmp": tmp, "f": open(tmp, "wb"), "at": at}


def upload_chunk(st, data):
    up = st.upload
    if up:
        up["got"] += len(data)
        if up["got"] > up["size"]:
            raise OSError("Received more data than announced")
        up["f"].write(data)


def upload_finish(st):
    up = st.upload
    if up["got"] != up["size"]:
        upload_abort(st)
        raise OSError("Transfer incomplete")
    st.upload = None
    up["f"].close()
    final = unique_path(RECEIVED_DIR, up["name"])
    up["tmp"].replace(final)
    return final


async def place_dropped(st, final, at, monitors):
    """Move a file dropped onto the remote screen into the folder under the pointer
    (an Explorer window or the desktop). Returns (path, folder description or None)."""
    if not (drop_target and at):
        return final, None
    loop = asyncio.get_running_loop()
    mon = monitors[st.monitor]
    px = mon["left"] + at[0] * (mon["width"] - 1)
    py = mon["top"] + at[1] * (mon["height"] - 1)
    try:
        target = await loop.run_in_executor(None, drop_target.folder_at, px, py)
    except Exception:
        target = None
    if not target:
        return final, None
    kind, value = target
    if kind == "path":
        if Path(value) == RECEIVED_DIR:
            return final, None
        try:
            dest = unique_path(Path(value), final.name)
            await loop.run_in_executor(None, shutil.move, str(final), str(dest))
            return dest, str(value)
        except OSError:
            return final, None
    # An Explorer window whose folder could not be read: paste the file into it (a move).
    try:
        ok = (await loop.run_in_executor(None, drop_target.put_files_on_clipboard, [final], True)
              and await loop.run_in_executor(None, drop_target.activate, value))
    except Exception:
        ok = False
    if not ok:
        return final, None
    st.clip_seq = inp.clipboard_seq()  # not something to copy to the viewer
    for code, down in (("ControlLeft", True), ("KeyV", True), ("KeyV", False), ("ControlLeft", False)):
        st.io.key_event(code, down)
    return final, "the open folder"


# Drag-out downloads: the Files panel lets the viewer drag a remote file onto its own
# desktop. Browsers download such a drop from a plain URL, so each listed file gets an
# unguessable link that works only while that viewer stays connected.
LINKS = {}  # token -> {"path": str, "ws": websocket, "expires": float}
LINK_MAX = 256 * 1024 * 1024
LINK_SECONDS = 600


def add_links(ws, listing):
    now = time.monotonic()
    for key in [k for k, v in LINKS.items() if v["expires"] < now]:
        del LINKS[key]
    for entry in listing["entries"]:
        if not entry["d"] and entry.get("s", 0) <= LINK_MAX:
            token = secrets.token_urlsafe(18)
            LINKS[token] = {"path": entry["p"], "ws": ws, "expires": now + LINK_SECONDS}
            entry["u"] = "/dl/" + token


def drop_links(ws):
    for key in [k for k, v in LINKS.items() if v["ws"] is ws]:
        del LINKS[key]


def _read_link(path):
    target = Path(path)
    size = target.stat().st_size
    if not target.is_file() or size > LINK_MAX:
        raise OSError("not available")
    return target.name, target.read_bytes()


async def serve_link(token):
    link = LINKS.get(token)
    if not link or link["expires"] < time.monotonic() or link["ws"] not in SESSIONS:
        return http_response(404, "Not Found", b"This link has expired. Open the Files panel again.")
    try:
        name, body = await asyncio.get_running_loop().run_in_executor(None, _read_link, link["path"])
    except OSError:
        return http_response(404, "Not Found", b"File not available")
    from urllib.parse import quote
    response = http_response(200, "OK", body, "application/octet-stream")
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    log(f"{SESSIONS[link['ws']]['ip']}: took a copy of {link['path']} ({fmt_size(len(body))})")
    return response


async def send_file(ws, st, path, mid):
    loop = asyncio.get_running_loop()
    try:
        target = Path(path)
        if not target.is_file():
            raise OSError("Not a file")
        size = target.stat().st_size
        if size > MAX_DOWNLOAD:
            raise OSError("File is larger than the 2 GB transfer limit")
        with open(target, "rb") as f:
            await ws.send(json.dumps({"t": "file_start", "id": mid, "name": target.name, "size": size}))
            while chunk := await loop.run_in_executor(None, f.read, CHUNK):
                await ws.send(b"\x01" + chunk)
        await ws.send(json.dumps({"t": "file_end", "id": mid}))
        log(f"{st.ip}: took a copy of {target} ({fmt_size(size)})")
    except OSError as exc:
        await ws.send(json.dumps({"t": "file_err", "id": mid, "msg": exc.strerror or str(exc)}))
    except ConnectionClosed:
        pass


def cancel_download(st):
    if st.download and not st.download.done():
        st.download.cancel()
    st.download = None


async def handle_files(ws, st, msg, monitors):
    """Handle a file-transfer message. Returns False if the message is something else."""
    t, mid = msg.get("t"), msg.get("id")

    async def reply(**fields):
        await ws.send(json.dumps(fields))

    if t == "ls":
        try:
            listing = await asyncio.get_running_loop().run_in_executor(
                None, list_dir, str(msg.get("path") or ""))
            add_links(ws, listing)
            await reply(t="ls", **listing)
        except OSError as exc:
            await reply(t="ls_err", msg=exc.strerror or str(exc))
    elif t == "up_start":
        try:
            upload_start(st, msg)
            await reply(t="up_ready", id=mid)
        except (OSError, KeyError, ValueError, TypeError) as exc:
            await reply(t="up_err", id=mid, msg=getattr(exc, "strerror", None) or str(exc))
    elif t == "up_end":
        if st.upload and st.upload["id"] == mid:
            try:
                size, at = st.upload["size"], st.upload["at"]
                final = upload_finish(st)
                final, where = await place_dropped(st, final, at, monitors)
                log(f"{st.ip}: sent file {final.name} ({fmt_size(size)}) -> {where or final.parent}")
                await reply(t="up_ok", id=mid, name=final.name, where=where or "Received files")
            except OSError as exc:
                await reply(t="up_err", id=mid, msg=exc.strerror or str(exc))
    elif t == "up_cancel":
        upload_abort(st)
    elif t == "get":
        cancel_download(st)
        st.download = asyncio.create_task(send_file(ws, st, str(msg["path"]), mid))
    elif t == "get_cancel":
        cancel_download(st)
    else:
        return False
    return True


AUDIO_QUEUE = 10  # 20 ms chunks waiting to be sent; beyond this, drop (keep latency low)


async def set_audio(ws, st, on):
    """Start or stop sending this computer's sound to one viewer."""
    loop = asyncio.get_running_loop()
    stop_audio(ws, st)
    if not on:
        return
    problem = await loop.run_in_executor(None, audio.CAPTURE.problem)
    if problem:
        await ws.send(json.dumps({"t": "audio", "error": problem}))
        return
    chunks = asyncio.Queue()

    def push(chunk):
        if chunks.qsize() < AUDIO_QUEUE:
            chunks.put_nowait(chunk)

    async def sender():
        while True:
            chunk = await chunks.get()
            await ws.send(b"\x03" + chunk)  # first byte 3 = sound

    st.audio_task = asyncio.create_task(sender())
    audio.CAPTURE.add(ws, lambda chunk: loop.call_soon_threadsafe(push, chunk))
    await ws.send(json.dumps({"t": "audio", "rate": audio.RATE, "ch": audio.CHANNELS}))
    log(f"{st.ip}: sound on")


def stop_audio(ws, st):
    audio.CAPTURE.remove(ws)
    if st.audio_task:
        st.audio_task.cancel()
        st.audio_task = None


MAX_CHAT = 2000
CHAT_SINK = None  # the control panel sets this to receive (ip, text) chat messages


async def send_chat(text):
    """Send a chat message from the host to every connected viewer. Returns how many got it."""
    payload = json.dumps({"t": "chat", "s": text[:MAX_CHAT]})
    sent = 0
    for ws in list(SESSIONS):
        try:
            await ws.send(payload)
            sent += 1
        except ConnectionClosed:
            pass
    return sent


async def receive(ws, st, monitors):
    async for raw in ws:
        if not isinstance(raw, str):  # binary = a chunk of the file being uploaded
            if st.upload and not VIEW_ONLY:
                try:
                    upload_chunk(st, raw)
                except OSError as exc:
                    mid = st.upload["id"]
                    upload_abort(st)
                    await ws.send(json.dumps({"t": "up_err", "id": mid, "msg": exc.strerror or str(exc)}))
            continue
        try:
            msg = json.loads(raw)
            t = msg.get("t")
            if t == "ack":
                st.pending = max(0, st.pending - 1)
                st.wake.set()
            elif t == "cfg":
                st.cursor = None  # resend the pointer for the newly selected screen
                if "monitor" in msg:
                    st.monitor = min(max(int(msg["monitor"]), 0), len(monitors) - 1)
                if "quality" in msg:
                    st.quality = min(max(int(msg["quality"]), 10), 95)
                if "scale" in msg:
                    st.scale = min(max(float(msg["scale"]), 0.2), 1.0)
                if "fps" in msg:
                    st.fps = min(max(int(msg["fps"]), 1), 60)
                st.frame = None  # send the whole picture again
                st.wake.set()
            elif t == "audio":  # sound on/off; allowed in view-only mode too
                await set_audio(ws, st, bool(msg.get("on")))
            elif t == "chat":  # allowed in view-only mode too
                text = str(msg["s"]).strip()[:MAX_CHAT]
                if text and CHAT_SINK:
                    CHAT_SINK(st.ip, text)
                elif text:
                    log(f"{st.ip} says: {text}")
            elif not VIEW_ONLY and not await handle_files(ws, st, msg, monitors):
                handle_input(st, monitors, msg)
                st.wake.set()  # capture again right away instead of waiting for the next tick
        except (KeyError, ValueError, TypeError):
            continue  # ignore malformed messages


async def handler(ws):
    ip = client_ip(ws)
    if locked_out(ip):
        await ws.close(1008, "locked out")
        return
    try:
        if not await authenticate(ws, ip):
            log(f"{ip}: authentication failed")
            await ws.send(json.dumps({"t": "denied"}))
            await ws.close(1008, "denied")
            return
    except ConnectionClosed:
        return

    monitors = None
    for _ in range(2):  # a stuck capture thread is replaced on the first try
        try:
            monitors = await capture_call(list_monitors)
            break
        except CaptureStuck:
            continue
        except Exception as exc:
            log(f"{ip}: could not read the screen layout: {type(exc).__name__}: {exc}")
            break
    if not monitors:
        await ws.send(json.dumps({"t": "busy"}))
        await ws.close(1011, "screen unavailable")
        return
    st = Session()
    st.ip = ip
    agent = bool(agent_client) and await asyncio.get_running_loop().run_in_executor(None, agent_client.available)
    if agent:
        st.io = agent_client.Input(inp)
    SESSIONS[ws] = {"ip": ip, "since": time.time()}
    log(f"{ip}: CONNECTED" + (" (view only)" if VIEW_ONLY else " (full control)")
        + (" - UAC prompts and the lock screen included" if agent else ""))
    streamer = None
    try:
        await ws.send(json.dumps({
            "t": "ready",
            "viewOnly": VIEW_ONLY,
            "host": socket.gethostname(),
            "monitors": [{"w": m["width"], "h": m["height"]} for m in monitors],
        }))
        streamer = asyncio.create_task(stream(ws, st, monitors))
        await receive(ws, st, monitors)
    except ConnectionClosed:
        pass
    finally:
        if streamer:
            streamer.cancel()
        release_all(st)  # never leave keys or buttons stuck down
        if st.io is not inp:
            st.io.close()
        if agent_client and isinstance(st.frame, agent_client.View):
            st.frame.conn.close()
        upload_abort(st)  # discard a half-received file
        cancel_download(st)
        drop_links(ws)
        stop_audio(ws, st)
        SESSIONS.pop(ws, None)
        log(f"{ip}: disconnected")


def http_response(status, reason, body, content_type="text/plain; charset=utf-8"):
    headers = Headers([
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("Connection", "close"),
    ])
    return Response(status, reason, headers, body)


async def process_request(connection, request):
    path = request.path.split("?", 1)[0]
    if path == "/ws":
        return None  # continue with the WebSocket handshake
    if path.startswith("/dl/"):
        return await serve_link(path[4:])
    if path in ("/", "/index.html"):
        body = (HERE / "viewer.html").read_bytes()
        return http_response(200, "OK", body, "text/html; charset=utf-8")
    return http_response(404, "Not Found", b"not found")


def local_addresses():
    try:
        addrs = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        addrs = []
    addrs = [a for a in addrs if not a.startswith("127.")]
    if not addrs:  # common on Linux, where the hostname maps to 127.0.1.1
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("10.255.255.255", 1))  # picks the outgoing interface; sends nothing
                addrs = [probe.getsockname()[0]]
        except OSError:
            pass
    return [a for a in addrs if not a.startswith("127.")]


async def run_server(bind, port, ssl_context=None, stop=None, on_ready=None):
    """Serve until `stop` (an asyncio.Event) is set, or forever without one."""
    async with serve(handler, bind, port, process_request=process_request,
                     ssl=ssl_context, compression=None, max_size=1 << 20):
        if on_ready:
            on_ready()
        await (stop.wait() if stop else asyncio.Future())


async def main():
    global PASSWORD, VIEW_ONLY
    ap = argparse.ArgumentParser(description="Remote desktop host")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="address to listen on (127.0.0.1 = this PC only)")
    ap.add_argument("--password", default=os.environ.get("RD_PASSWORD"),
                    help="access password (default: random, printed at startup)")
    ap.add_argument("--view-only", action="store_true", help="stream the screen but ignore input")
    ap.add_argument("--cert", help="TLS certificate (PEM) to serve over HTTPS")
    ap.add_argument("--key", help="TLS private key (PEM)")
    args = ap.parse_args()

    generated = not args.password
    PASSWORD = args.password or secrets.token_urlsafe(9)
    VIEW_ONLY = args.view_only

    ctx = None
    if args.cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
    scheme = "https" if ctx else "http"

    def banner():
        print("Remote desktop host is running. Press Ctrl+C to stop.\n")
        print(f"  This PC:   {scheme}://localhost:{args.port}")
        if args.bind not in ("127.0.0.1", "localhost"):
            for addr in local_addresses():
                print(f"  Network:   {scheme}://{addr}:{args.port}")
        if generated:
            print(f"\n  Password:  {PASSWORD}")
        print(f"  Mode:      {'view only' if VIEW_ONLY else 'full control'}\n", flush=True)

    await run_server(args.bind, args.port, ctx, on_ready=banner)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
