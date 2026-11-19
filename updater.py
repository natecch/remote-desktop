"""Updates from the GitHub repository's Releases.

check() asks GitHub for the latest release; download() fetches this system's installer
and refuses it unless its SHA-256 matches what GitHub reports for that file (and, when
this copy is code-signed, unless it is signed by the same publisher). On Windows,
install() runs the installer silently; it replaces the app and starts it again.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from version import REPO, VERSION

WINDOWS, MAC = sys.platform == "win32", sys.platform == "darwin"
MAX_SIZE = 500 * 1024 * 1024
HEADERS = {"User-Agent": "RemoteDesktop-Updater", "Accept": "application/vnd.github+json"}


SOURCE = REPO  # the repository in use; the app replaces it with the one set in Settings
REPO_NAME = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")


def normalize(text):
    """'owner/name' from what the user typed (also a github.com link), or None."""
    text = str(text or "").strip()
    text = re.sub(r"^(https?://)?(www\.)?github\.com/", "", text, flags=re.I)
    text = re.sub(r"(\.git)?/*$", "", text)
    text = "/".join(text.split("/")[:2])
    return text if REPO_NAME.fullmatch(text) else None


def configured():
    return bool(SOURCE and REPO_NAME.fullmatch(SOURCE))


def parse(version):
    numbers = [int(n) for n in re.findall(r"\d+", str(version))[:3]]
    return tuple(numbers + [0] * (3 - len(numbers)))


def _open(url, timeout=20):
    if not url.startswith("https://"):
        raise OSError("refusing a non-HTTPS address")
    return urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout)


def _pick(assets):
    names = {a.get("name", ""): a for a in assets}
    if WINDOWS:
        return names.get("RemoteDesktopSetup.exe")
    wanted = (".dmg",) if MAC else ((".deb", ".tar.gz") if shutil.which("dpkg") else (".tar.gz",))
    for suffix in wanted:
        for name, asset in names.items():
            if name.endswith(suffix):
                return asset
    return None


class NoReleases(OSError):
    """The repository exists but has published no release."""


def check():
    """Info about a newer release, or None when this is the latest. Raises OSError."""
    if not configured():
        raise OSError("No update source is set for this build.")
    try:
        with _open(f"https://api.github.com/repos/{SOURCE}/releases/latest") as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:  # e.g. 403/429: GitHub's API allows 60 requests an hour per address
            return _check_without_api()
        # 404 means either no such (public) repository, or one without any release yet.
        try:
            _open(f"https://api.github.com/repos/{SOURCE}").close()
        except urllib.error.HTTPError:
            raise OSError(f"github.com/{SOURCE} was not found. Check the name in Settings "
                          "(the repository must be public).") from None
        raise NoReleases(f"github.com/{SOURCE} has no releases yet, so there is nothing to update to.") from None
    except (ValueError, urllib.error.URLError, OSError):
        return _check_without_api()
    tag = str(data.get("tag_name", ""))
    if parse(tag) <= parse(VERSION):
        return None
    return {"version": tag.lstrip("vV"), "page": data.get("html_url") or f"https://github.com/{SOURCE}/releases",
            "asset": _pick(data.get("assets") or []), "assets": data.get("assets") or []}


def _asset_names(version):
    """The file names the release workflow gives each system's installer."""
    if WINDOWS:
        return "RemoteDesktopSetup.exe", "SHA256SUMS-windows.txt"
    if MAC:
        return f"RemoteDesktop-{version}-arm64.dmg", "SHA256SUMS-macos.txt"
    suffix = ".deb" if shutil.which("dpkg") else ".tar.gz"
    return f"remote-desktop_{version}_amd64{suffix}", "SHA256SUMS-linux.txt"


def _check_without_api():
    """The same check through github.com's ordinary pages, which have no hourly limit:
    /releases/latest redirects to the newest release's tag."""
    try:
        with _open(f"https://github.com/{SOURCE}/releases/latest") as response:
            final = response.geturl()
    except urllib.error.HTTPError as exc:
        raise OSError(f"GitHub answered {exc.code} {exc.reason}.") from None
    match = re.search(r"/releases/tag/([^/?#]+)$", final)
    if not match:  # stayed on /releases: there is no release yet
        raise NoReleases(f"github.com/{SOURCE} has no releases yet, so there is nothing to update to.")
    tag = urllib.parse.unquote(match.group(1))
    if parse(tag) <= parse(VERSION):
        return None
    version = tag.lstrip("vV")
    base = f"https://github.com/{SOURCE}/releases/download/{urllib.parse.quote(tag)}/"
    name, sums = _asset_names(version)
    return {"version": version, "page": final,
            "asset": {"name": name, "browser_download_url": base + name, "size": 0},
            "assets": [{"name": sums, "browser_download_url": base + sums}]}


def _expected_sha256(info):
    asset = info["asset"]
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        return digest[7:].lower()
    for other in info["assets"]:  # fall back to a SHA256SUMS.txt published with the release
        if other.get("name", "").upper().startswith("SHA256SUMS"):
            with _open(other["browser_download_url"]) as response:
                for line in response.read(1 << 20).decode("utf-8", "replace").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1].lstrip("*") == asset["name"]:
                        return parts[0].lower()
    return None


def _download_dir():
    """Where the installer is saved. An administrator copy uses a folder only
    administrators can change, so nothing can swap the file before it runs."""
    if WINDOWS and getattr(sys, "frozen", False):
        folder = Path(sys.executable).resolve().parent / "updates"
        try:
            folder.mkdir(exist_ok=True)
            probe = folder / ".write-test"
            probe.write_bytes(b"")
            probe.unlink()
            return folder
        except OSError:
            pass
    return Path(tempfile.mkdtemp(prefix="RemoteDesktopUpdate-"))


def cleanup():
    """Delete installers left from an earlier update."""
    if WINDOWS and getattr(sys, "frozen", False):
        shutil.rmtree(Path(sys.executable).resolve().parent / "updates", ignore_errors=True)


def _signature(path):
    """(status, publisher) from Windows' Authenticode check."""
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $args[0]; "
              "\"$($s.Status)|$($s.SignerCertificate.Subject)\"")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
                            capture_output=True, text=True, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    status, _, subject = result.stdout.strip().partition("|")
    return status, subject


def download(info, progress=None):
    """Download and verify the installer. Returns its path. Raises OSError."""
    asset = info["asset"]
    if not asset:
        raise OSError("This release has no installer for this system.")
    url = str(asset.get("browser_download_url", ""))
    if not url.startswith("https://github.com/"):
        raise OSError("The installer is not hosted on GitHub.")
    size = int(asset.get("size") or 0)  # 0: not known in advance (checked as it downloads)
    if size < 0 or size > MAX_SIZE:
        raise OSError("The installer has an unexpected size.")
    expected = _expected_sha256(info)
    if not expected:
        raise OSError("GitHub gave no checksum for the installer, so it cannot be verified.")
    target = _download_dir() / Path(asset["name"]).name
    digest, got = hashlib.sha256(), 0
    try:
        with _open(url, timeout=60) as response, open(target, "wb") as out:
            while chunk := response.read(1 << 16):
                got += len(chunk)
                if got > (size or MAX_SIZE):
                    raise OSError("The download is larger than announced.")
                digest.update(chunk)
                out.write(chunk)
                if progress:
                    progress(got, size or got)
        if (size and got != size) or digest.hexdigest() != expected:
            raise OSError("The download is damaged or was changed (checksum mismatch).")
        if WINDOWS and getattr(sys, "frozen", False):
            mine = _signature(sys.executable)
            if mine[0] == "Valid":  # signed builds only accept updates from the same publisher
                theirs = _signature(target)
                if theirs != mine:
                    raise OSError("The update is not signed by the same publisher as this copy.")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def install(path):
    """Windows: run the installer silently; it closes this app, updates it and starts it again."""
    if not WINDOWS:
        raise OSError("Open the downloaded file to install it.")
    args = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"]
    try:
        subprocess.Popen([str(path), *args], close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except OSError as exc:
        if getattr(exc, "winerror", None) != 740:  # 740: the installer needs administrator rights
            raise
        # This copy runs without full rights: ask Windows to start the installer as administrator.
        import ctypes
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        if (shell32.ShellExecuteW(None, "runas", str(path), subprocess.list2cmdline(args), None, 0) or 0) <= 32:
            raise OSError("Administrator permission was not given, so the update was not installed.") from None


def reveal(path):
    """Show a downloaded file (Linux/macOS, where the user installs it)."""
    folder = str(Path(path).parent)
    if MAC:
        subprocess.Popen(["open", folder])
    elif not WINDOWS:
        subprocess.Popen(["xdg-open", folder])
    else:
        os.startfile(folder)
