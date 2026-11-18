"""Numeric IDs: map a 9-digit number to this PC's current internet address.

There is no server of our own, so the mapping is kept on ntfy.sh, a free public
message service. The host posts its current address to a topic derived from
its number; a partner reads the newest message on that topic.

The address is not a secret (the password is what protects the PC), and a
record on a public service cannot be trusted, so the partner app never loads a
page from the looked-up address: it opens its own local viewer and only makes a
password-challenge connection to it.
"""

import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.request

SERVICE = "https://ntfy.sh"
TUNNEL_URL = re.compile(r"^https://[a-z0-9-]{1,63}\.trycloudflare\.com$")
TIMEOUT = 10
MAX_AGE = 12 * 3600  # ntfy keeps messages about this long


def new_id():
    return str(secrets.randbelow(900_000_000) + 100_000_000)


def digits(text):
    return re.sub(r"[\s-]", "", text.strip())


def is_id(text):
    return bool(re.fullmatch(r"\d{9}", digits(text)))


def pretty(ident):
    d = digits(ident)
    return f"{d[:3]} {d[3:6]} {d[6:]}"


def topic(ident):
    return "rd-" + hashlib.sha256(b"remote-desktop-id:" + digits(ident).encode()).hexdigest()[:40]


def _request(url, data=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "remote-desktop-app"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(200_000).decode("utf-8", "replace")


def publish(ident, url, timeout=TIMEOUT):
    """Record `url` as the current address for `ident` ('' = offline). Raises OSError on failure."""
    body = json.dumps({"v": 1, "url": url}).encode()
    _request(f"{SERVICE}/{topic(ident)}", data=body, timeout=timeout)


def lookup(ident):
    """Return the current address for `ident`, or None if unknown or offline. Raises OSError."""
    text = _request(f"{SERVICE}/{topic(ident)}/json?poll=1&since=12h")
    newest, newest_time = None, -1
    for line in text.splitlines():
        try:
            event = json.loads(line)
            if event.get("event") != "message":
                continue
            record = json.loads(event["message"])
            posted = int(event["time"])  # set by the service, not by whoever posted
            if isinstance(record, dict) and posted >= newest_time:
                newest, newest_time = record, posted
        except (ValueError, KeyError, TypeError):
            continue  # anyone can post to a public topic: ignore anything malformed
    if not newest or time.time() - newest_time > MAX_AGE:
        return None
    url = newest.get("url")
    return url if isinstance(url, str) and TUNNEL_URL.match(url) else None
