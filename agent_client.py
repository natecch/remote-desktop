"""Talks to the Windows session agent (see winagent.py), when it is running.

The agent runs as SYSTEM in the signed-in user's session, started by the Remote Desktop
service. It can read and control whichever desktop has the input: the normal desktop,
the UAC secure desktop or the lock screen. The app's sharing server uses it for screen
capture, mouse/keyboard and the pointer, and falls back to doing those itself (without
UAC/lock-screen access) whenever the agent is not there.

Wire format, both ways: u32 JSON length, u32 binary length, JSON, binary (little-endian).
"""

import json
import socket
import struct
import threading
import time

PORT = 47614
MAX_JSON, MAX_BLOB = 1 << 20, 64 << 20
CONNECT_TIMEOUT = 0.3   # a closed port on Windows can otherwise take ~2 s to refuse
CALL_TIMEOUT = 5.0
RETRY_AFTER = 5.0       # after a failure, work without the agent for this long


def send(sock, obj, blob=b""):
    data = json.dumps(obj, separators=(",", ":")).encode()
    sock.sendall(struct.pack("<II", len(data), len(blob)) + data + blob)


def _exact(sock, n):
    parts, got = [], 0
    while got < n:
        chunk = sock.recv(min(n - got, 1 << 20))
        if not chunk:
            raise ConnectionError("agent connection closed")
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def recv(sock):
    jlen, blen = struct.unpack("<II", _exact(sock, 8))
    if jlen > MAX_JSON or blen > MAX_BLOB:
        raise ConnectionError("oversized agent message")
    return json.loads(_exact(sock, jlen)), _exact(sock, blen)


class Conn:
    def __init__(self):
        self.sock = socket.create_connection(("127.0.0.1", PORT), timeout=CONNECT_TIMEOUT)
        self.sock.settimeout(CALL_TIMEOUT)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def call(self, obj):
        send(self.sock, obj)
        reply, blob = recv(self.sock)
        if "err" in reply:
            raise OSError(f"agent: {reply['err']}")
        return reply, blob

    def close(self):
        sock = getattr(self, "sock", None)  # missing when the connection never opened
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    __del__ = close


_status = {"ok": False, "checked": -1e9, "desktop": None}


def available(max_age=5.0):
    """True when the agent is running and accepts this process (it must be elevated).
    Cached for `max_age` seconds; may block for up to ~0.5 s, so call it off the event loop."""
    if time.monotonic() - _status["checked"] < max_age:
        return _status["ok"]
    try:
        conn = Conn()
        try:
            reply, _ = conn.call({"op": "hello"})
            _status.update(ok=True, desktop=reply.get("desktop"))
        finally:
            conn.close()
    except (OSError, ValueError):
        _status["ok"] = False
    _status["checked"] = time.monotonic()
    return _status["ok"]


def mark_down():
    _status.update(ok=False, checked=time.monotonic())


def monitors():
    conn = Conn()
    try:
        return conn.call({"op": "monitors"})[0]["r"]
    finally:
        conn.close()


class View:
    """One viewer's screen stream through the agent. The agent keeps the last picture
    sent, so only changes travel; this object stands in for that picture in the host."""

    def __init__(self):
        self.conn = Conn()
        self.fast = False


def grab(index, monitor, scale, quality, previous, previous_version, earliest=0.0, latest=0.0):
    """Same contract as host.grab_update, with the capture done by the agent."""
    view = previous if isinstance(previous, View) else View()
    now = time.perf_counter()
    reply, blob = view.conn.call({
        "op": "grab", "i": index, "m": monitor, "s": scale, "q": quality,
        "reset": view is not previous,
        "wait": max(0.0, earliest - now), "window": max(0.0, latest - now)})
    view.fast = bool(reply.get("fast"))
    return view, reply["v"], (blob or None), time.perf_counter() - float(reply.get("age", 0.0))


class Input:
    """Mouse, keyboard and pointer through the agent, falling back to `local` (input_win)."""

    FUNCS = ("mouse_move", "mouse_button", "mouse_wheel", "key_event", "type_text", "cursor_info")

    def __init__(self, local):
        self.local = local
        self.BUTTONS = local.BUTTONS
        self.conn = None
        self.retry_at = 0.0
        self.lock = threading.Lock()

    def _call(self, name, *args):
        with self.lock:
            if self.conn is None and time.monotonic() >= self.retry_at:
                try:
                    self.conn = Conn()
                except OSError:
                    self.retry_at = time.monotonic() + RETRY_AFTER
            if self.conn is not None:
                try:
                    return self.conn.call({"op": "in", "f": name, "a": list(args)})[0].get("r")
                except (OSError, ValueError):
                    self.conn.close()
                    self.conn = None
                    self.retry_at = time.monotonic() + RETRY_AFTER
        return getattr(self.local, name)(*args)

    def mouse_move(self, monitor, nx, ny):
        return self._call("mouse_move", monitor, nx, ny)

    def mouse_button(self, button, down):
        return self._call("mouse_button", button, down)

    def mouse_wheel(self, dx, dy):
        return self._call("mouse_wheel", dx, dy)

    def key_event(self, code, down):
        return self._call("key_event", code, down)

    def type_text(self, text):
        return self._call("type_text", text)

    def cursor_info(self):
        result = self._call("cursor_info")
        return tuple(result) if result else None

    def close(self):
        with self.lock:
            if self.conn:
                self.conn.close()
                self.conn = None
