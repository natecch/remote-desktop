"""Windows background service and session agent: how UAC prompts and the lock screen
are controlled remotely without turning any of Windows' protection off (the approach
RustDesk and TeamViewer use).

* The service ("Remote Desktop Service") runs as LocalSystem. It does nothing itself
  except keep one agent running in the session of whoever is signed in at the console,
  restarting it when that user signs out, switches user or the agent exits.
* The agent (`RemoteDesktop.exe --agent`) runs as SYSTEM inside that session. Before each
  capture or input it moves its worker threads onto the desktop that currently has the
  input: the normal desktop, the UAC secure desktop, or the sign-in/lock screen. Windows
  lets a SYSTEM program read and control those; an ordinary program, even an
  administrator one, cannot, which is why UAC prompts were unclickable before.
* The agent listens on 127.0.0.1 only, and answers only elevated (administrator)
  processes, i.e. the Remote Desktop app itself. Sharing, the password, the tunnel and
  everything a viewer touches stay in the app; the agent only captures and injects.

The service may only be installed from a copy of the app in a folder ordinary users
cannot change (Program Files): a SYSTEM service running a program users can replace would
hand them SYSTEM rights.
"""

import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
import queue
from ctypes import wintypes
from pathlib import Path

import agent_client as proto

SERVICE_NAME = "RemoteDesktopService"
DISPLAY_NAME = "Remote Desktop Service"
DESCRIPTION = ("Lets Remote Desktop show and control UAC prompts and the sign-in screen "
               "during a remote session. Only the Remote Desktop app can use it.")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi")
HANDLE, DWORD, BOOL = ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL


def _check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return ok


kernel32.OpenProcess.argtypes = (DWORD, BOOL, DWORD)
kernel32.OpenProcess.restype = HANDLE
kernel32.CloseHandle.argtypes = (HANDLE,)
kernel32.GetCurrentProcess.restype = HANDLE
kernel32.WaitForSingleObject.argtypes = (HANDLE, DWORD)
kernel32.WaitForSingleObject.restype = DWORD
kernel32.TerminateProcess.argtypes = (HANDLE, wintypes.UINT)
kernel32.WTSGetActiveConsoleSessionId.restype = DWORD
advapi32.OpenProcessToken.argtypes = (HANDLE, DWORD, ctypes.POINTER(HANDLE))
advapi32.GetTokenInformation.argtypes = (HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD))
advapi32.DuplicateTokenEx.argtypes = (HANDLE, DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                      ctypes.POINTER(HANDLE))
advapi32.SetTokenInformation.argtypes = (HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD)

WAIT_TIMEOUT = 0x102
NO_SESSION = 0xFFFFFFFF


def program_dir():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def data_dir():
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "RemoteDesktop"


def log(msg):
    try:
        path = data_dir() / "service.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 512 * 1024:
            path.replace(path.with_suffix(".old.log"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Following the input desktop (Default / Winlogon) on a thread
# --------------------------------------------------------------------------

user32.OpenInputDesktop.argtypes = (DWORD, BOOL, DWORD)
user32.OpenInputDesktop.restype = HANDLE
user32.SetThreadDesktop.argtypes = (HANDLE,)
user32.CloseDesktop.argtypes = (HANDLE,)
user32.GetUserObjectInformationW.argtypes = (HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD))
GENERIC_ALL, MAXIMUM_ALLOWED, UOI_NAME = 0x10000000, 0x02000000, 2


def _desktop_name(handle):
    buf = ctypes.create_unicode_buffer(256)
    needed = DWORD()
    if user32.GetUserObjectInformationW(handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
        return buf.value
    return None


class DesktopFollower:
    """Keeps the calling thread on the desktop that receives input. The thread must own
    no windows (SetThreadDesktop fails otherwise), so it is only used on worker threads."""

    def __init__(self):
        self.name = None
        self.handle = None

    def sync(self):
        """Switch if the input desktop changed. True when it switched."""
        handle = user32.OpenInputDesktop(0, False, GENERIC_ALL) or user32.OpenInputDesktop(0, False, MAXIMUM_ALLOWED)
        if not handle:
            return False  # no access (not SYSTEM): stay where we are
        name = _desktop_name(handle)
        if name == self.name or not user32.SetThreadDesktop(handle):
            user32.CloseDesktop(handle)
            return False
        if self.handle:
            user32.CloseDesktop(self.handle)
        self.handle, self.name = handle, name
        return True


class Worker:
    """A thread that runs calls one at a time on the current input desktop."""

    def __init__(self, name, on_switch=None):
        self.jobs = queue.Queue()
        self.follow = DesktopFollower()
        self.on_switch = on_switch
        threading.Thread(target=self._run, name=name, daemon=True).start()

    def _run(self):
        while True:
            fn, args, box, done = self.jobs.get()
            try:
                if self.follow.sync() and self.on_switch:
                    self.on_switch(self.follow.name)
                box.append((True, fn(*args)))
            except Exception as exc:
                box.append((False, exc))
            done.set()

    def call(self, fn, *args, timeout=10.0):
        box, done = [], threading.Event()
        self.jobs.put((fn, args, box, done))
        if not done.wait(timeout):
            raise TimeoutError("worker did not answer")
        ok, value = box[0]
        if ok:
            return value
        raise value


# --------------------------------------------------------------------------
# Who is connecting? Only elevated processes may use the agent.
# --------------------------------------------------------------------------

class _TcpRow(ctypes.Structure):
    _fields_ = [(name, DWORD) for name in ("state", "laddr", "lport", "raddr", "rport", "pid")]


def peer_pid(conn):
    """Process id at the other end of a 127.0.0.1 connection, or None."""
    mine, theirs = conn.getsockname()[1], conn.getpeername()[1]
    size = DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, socket.AF_INET, 5, 0)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = DWORD(len(buf))
        if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, socket.AF_INET, 5, 0) == 0:
            break
    else:
        return None
    count = DWORD.from_buffer(buf).value
    rows = (_TcpRow * count).from_buffer(buf, 4)
    for row in rows:
        if (socket.ntohs(row.lport & 0xFFFF) == theirs and socket.ntohs(row.rport & 0xFFFF) == mine
                and row.pid != os.getpid()):
            return row.pid
    return None


def is_elevated(pid):
    process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return False
    token = HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return False
        elevated, size = DWORD(), DWORD()
        ok = advapi32.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size))  # TokenElevation
        return bool(ok and elevated.value)
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------

INPUT_FUNCS = set(proto.Input.FUNCS)


def run_agent(service_pid=None):
    import host  # screen capture and input, done here on the input desktop

    def switched(name):
        log(f"agent: input desktop is now {name}")
        if host._grabber is not None:
            host._grabber.reset()

    capture = Worker("capture", on_switch=switched)
    inputs = Worker("input")

    def grab(state, req):
        now = time.perf_counter()
        previous = None if req.get("reset") else state.get("frame")
        frame, version, update, captured = host.grab_update(
            int(req["i"]), req["m"], float(req["s"]), int(req["q"]), previous,
            state.get("version", -1), now + float(req["wait"]), now + float(req["window"]))
        state["frame"], state["version"] = frame, version
        return {"v": version, "fast": host._screen().fast(int(req["i"])),
                "age": time.perf_counter() - captured}, update or b""

    def serve(conn):
        with conn:
            pid = peer_pid(conn)
            if not pid or not is_elevated(pid):
                return  # not an administrator process: refuse silently
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            state = {}
            while True:
                try:
                    req, _ = proto.recv(conn)
                except (OSError, ValueError):
                    return
                try:
                    op = req.get("op")
                    if op == "hello":
                        reply, blob = {"ok": True, "desktop": capture.follow.name}, b""
                    elif op == "monitors":
                        reply, blob = {"r": capture.call(host.list_monitors)}, b""
                    elif op == "grab":
                        reply, blob = capture.call(grab, state, req)
                    elif op == "in" and req.get("f") in INPUT_FUNCS:
                        reply, blob = {"r": inputs.call(getattr(host.inp, req["f"]), *req.get("a", []))}, b""
                    else:
                        reply, blob = {"err": "unknown request"}, b""
                except Exception as exc:
                    reply, blob = {"err": f"{type(exc).__name__}: {exc}"}, b""
                try:
                    proto.send(conn, reply, blob)
                except OSError:
                    return

    if service_pid:  # end together with the service that started us
        def watch():
            process = kernel32.OpenProcess(0x00100000, False, service_pid)  # SYNCHRONIZE
            if process:
                kernel32.WaitForSingleObject(process, 0xFFFFFFFF)
            os._exit(0)
        threading.Thread(target=watch, daemon=True).start()

    deadline = time.monotonic() + 15
    while True:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            srv.bind(("127.0.0.1", proto.PORT))
            srv.listen(8)
            break
        except OSError as exc:
            srv.close()
            if time.monotonic() > deadline:
                log(f"agent: cannot listen on port {proto.PORT}: {exc}")
                return 1
            time.sleep(0.5)
    log(f"agent: running in session {_session_of(os.getpid())} (pid {os.getpid()})")
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


def _session_of(pid):
    session = DWORD()
    return session.value if kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)) else None


# --------------------------------------------------------------------------
# The service: keeps an agent running in the console session
# --------------------------------------------------------------------------

class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD),
                ("dwYSize", DWORD), ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
                ("dwFillAttribute", DWORD), ("dwFlags", DWORD), ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


advapi32.CreateProcessAsUserW.argtypes = (
    HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, BOOL, DWORD,
    ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION))


def agent_command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--agent", str(os.getpid())]
    return [sys.executable, str(Path(__file__).resolve().parent / "app.py"), "--agent", str(os.getpid())]


def launch_in_session(session, cmd):
    """Start `cmd` as SYSTEM in `session` on its interactive window station. Needs SYSTEM."""
    token, dup = HANDLE(), HANDLE()
    _check(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0xF01FF, ctypes.byref(token)))
    try:
        _check(advapi32.DuplicateTokenEx(token, MAXIMUM_ALLOWED, None, 2, 1, ctypes.byref(dup)))
        sid = DWORD(session)
        _check(advapi32.SetTokenInformation(dup, 12, ctypes.byref(sid), 4))  # TokenSessionId
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = "winsta0\\default"
        pi = PROCESS_INFORMATION()
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
        _check(advapi32.CreateProcessAsUserW(dup, None, line, None, None, False, 0x08000000,  # CREATE_NO_WINDOW
                                             None, str(program_dir()), ctypes.byref(si), ctypes.byref(pi)))
        kernel32.CloseHandle(pi.hThread)
        return pi.hProcess, pi.dwProcessId
    finally:
        for handle in (dup, token):
            if handle:
                kernel32.CloseHandle(handle)


def supervise(stop):
    """Keep one agent in the active console session until `stop` (threading.Event) is set."""
    process = pid = session = None
    last_start = -1e9
    while not stop.is_set():
        active = kernel32.WTSGetActiveConsoleSessionId()
        alive = process is not None and kernel32.WaitForSingleObject(process, 0) == WAIT_TIMEOUT
        if not (alive and session == active):
            if process is not None:
                if alive:
                    kernel32.TerminateProcess(process, 0)
                kernel32.CloseHandle(process)
                process = None
            if active not in (NO_SESSION, 0) and time.monotonic() - last_start > 3:
                last_start = time.monotonic()
                try:
                    process, pid = launch_in_session(active, agent_command())
                    session = active
                    log(f"service: started agent {pid} in session {active}")
                except OSError as exc:
                    log(f"service: could not start the agent in session {active}: {exc}")
        stop.wait(1.0)
    if process is not None:
        kernel32.TerminateProcess(process, 0)
        kernel32.CloseHandle(process)


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [(name, DWORD) for name in (
        "dwServiceType", "dwCurrentState", "dwControlsAccepted", "dwWin32ExitCode",
        "dwServiceSpecificExitCode", "dwCheckPoint", "dwWaitHint")]


HANDLER_EX = ctypes.WINFUNCTYPE(DWORD, DWORD, DWORD, ctypes.c_void_p, ctypes.c_void_p)
SERVICE_MAIN = ctypes.WINFUNCTYPE(None, DWORD, ctypes.c_void_p)


class SERVICE_TABLE_ENTRYW(ctypes.Structure):
    _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", SERVICE_MAIN)]


advapi32.RegisterServiceCtrlHandlerExW.argtypes = (wintypes.LPCWSTR, HANDLER_EX, ctypes.c_void_p)
advapi32.RegisterServiceCtrlHandlerExW.restype = HANDLE
advapi32.SetServiceStatus.argtypes = (HANDLE, ctypes.POINTER(SERVICE_STATUS))
advapi32.StartServiceCtrlDispatcherW.argtypes = (ctypes.POINTER(SERVICE_TABLE_ENTRYW),)


def run_service():
    """Entry point when Windows starts the service (`RemoteDesktop.exe --service`)."""
    stop = threading.Event()
    status = SERVICE_STATUS(0x10, 2, 0, 0, 0, 0, 3000)  # own process, START_PENDING
    keep = {}

    def report(state, accepted=0):
        status.dwCurrentState, status.dwControlsAccepted = state, accepted
        advapi32.SetServiceStatus(keep["handle"], ctypes.byref(status))

    def handler(control, _event, _data, _ctx):
        if control in (1, 5):  # STOP, SHUTDOWN
            report(3)  # STOP_PENDING
            stop.set()
        return 0

    def main(_argc, _argv):
        keep["handler"] = HANDLER_EX(handler)
        keep["handle"] = advapi32.RegisterServiceCtrlHandlerExW(SERVICE_NAME, keep["handler"], None)
        if not keep["handle"]:
            return
        report(4, 0x1 | 0x4)  # RUNNING; accepts STOP and SHUTDOWN
        log("service: started")
        try:
            supervise(stop)
        except Exception as exc:
            log(f"service: stopped by an error: {exc!r}")
        log("service: stopped")
        report(1)  # STOPPED

    keep["main"] = SERVICE_MAIN(main)
    table = (SERVICE_TABLE_ENTRYW * 2)(SERVICE_TABLE_ENTRYW(SERVICE_NAME, keep["main"]),
                                       SERVICE_TABLE_ENTRYW(None, SERVICE_MAIN()))
    if not advapi32.StartServiceCtrlDispatcherW(table):
        error = ctypes.get_last_error()
        if error == 1063:  # not started by Windows
            print("This is the Remote Desktop service; Windows starts it. Use --install-service.")
        return 1
    return 0


# --------------------------------------------------------------------------
# Installing, removing and querying the service (administrator only)
# --------------------------------------------------------------------------

advapi32.OpenSCManagerW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, DWORD)
advapi32.OpenSCManagerW.restype = HANDLE
advapi32.OpenServiceW.argtypes = (HANDLE, wintypes.LPCWSTR, DWORD)
advapi32.OpenServiceW.restype = HANDLE
advapi32.QueryServiceStatus.argtypes = (HANDLE, ctypes.POINTER(SERVICE_STATUS))
advapi32.CloseServiceHandle.argtypes = (HANDLE,)


def service_state():
    """"running", "stopped" (installed, not running), "missing", or None if unknown."""
    manager = advapi32.OpenSCManagerW(None, None, 0x0001)  # SC_MANAGER_CONNECT
    if not manager:
        return None
    try:
        service = advapi32.OpenServiceW(manager, SERVICE_NAME, 0x0004)  # SERVICE_QUERY_STATUS
        if not service:
            return "missing" if ctypes.get_last_error() == 1060 else None
        try:
            status = SERVICE_STATUS()
            if not advapi32.QueryServiceStatus(service, ctypes.byref(status)):
                return None
            return "running" if status.dwCurrentState in (2, 4) else "stopped"
        finally:
            advapi32.CloseServiceHandle(service)
    finally:
        advapi32.CloseServiceHandle(manager)


def install_problem():
    """Why the service cannot be installed from this copy, or None if it can."""
    if not getattr(sys, "frozen", False):
        return "the service runs only from the installed app (RemoteDesktopSetup.exe), not from source"
    folder = program_dir()
    protected = [Path(os.environ[v]).resolve() for v in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
                 if os.environ.get(v)]
    if not any(folder == root or root in folder.parents for root in protected):
        return ("this copy is not in Program Files, so other users could replace it; "
                "reinstall with the latest RemoteDesktopSetup.exe")
    return None


def _sc(*args):
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def install_service():
    """Install (or repair) and start the service. Raises OSError with a readable reason."""
    problem = install_problem()
    if problem:
        raise OSError(f"Cannot install the service: {problem}.")
    bin_path = subprocess.list2cmdline([sys.executable, "--service"])
    if service_state() == "missing":
        result = _sc("create", SERVICE_NAME, "binPath=", bin_path, "start=", "auto",
                     "DisplayName=", DISPLAY_NAME)
    else:
        _sc("stop", SERVICE_NAME)
        result = _sc("config", SERVICE_NAME, "binPath=", bin_path, "start=", "auto")
    if result.returncode != 0:
        raise OSError((result.stdout or result.stderr).strip() or "sc.exe failed")
    _sc("description", SERVICE_NAME, DESCRIPTION)
    _sc("failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/5000/restart/5000/restart/60000")
    for _ in range(20):  # a stop just requested may still be finishing
        result = _sc("start", SERVICE_NAME)
        if result.returncode == 0 or "1056" in result.stdout:  # 1056: already running
            return
        time.sleep(0.5)
    raise OSError((result.stdout or result.stderr).strip() or "The service did not start.")


def uninstall_service():
    if service_state() == "missing":
        return
    _sc("stop", SERVICE_NAME)
    for _ in range(20):
        if service_state() != "running":
            break
        time.sleep(0.5)
    result = _sc("delete", SERVICE_NAME)
    if result.returncode != 0 and service_state() != "missing":
        raise OSError((result.stdout or result.stderr).strip() or "sc.exe failed")
