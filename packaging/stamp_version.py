"""Writes version.py for a build: stamp_version.py <git ref name> <owner/repo>.
A tag such as v1.2.0 becomes the version; other refs keep the version in the file."""

import re
import sys
from pathlib import Path

path = Path(__file__).resolve().parent.parent / "version.py"
ref, repo = (sys.argv[1:] + ["", ""])[:2]
text = path.read_text(encoding="utf-8")
match = re.fullmatch(r"v(\d+\.\d+\.\d+)", ref.strip())
if match:
    text = re.sub(r'^VERSION = ".*"', f'VERSION = "{match.group(1)}"', text, flags=re.M)
if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repo.strip()):
    text = re.sub(r'^REPO = ".*?"', f'REPO = "{repo.strip()}"', text, flags=re.M)
path.write_text(text, encoding="utf-8")
print(text)
