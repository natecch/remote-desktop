"""Sets the version for a build: stamp_version.py <git ref name or version> [owner/repo].

A tag such as v1.2.0 (or just 1.2.0) becomes the version in both version.py (what the
app shows and compares updates against) and pyproject.toml (what uv sees). Other refs
keep the current version. A repository name sets where updates come from.
"""

import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
ref, repo = (sys.argv[1:] + ["", ""])[:2]
version = re.fullmatch(r"v?(\d+\.\d+\.\d+)", ref.strip())

app = root / "version.py"
text = app.read_text(encoding="utf-8")
if version:
    text = re.sub(r'^VERSION = ".*"', f'VERSION = "{version.group(1)}"', text, flags=re.M)
if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repo.strip()):
    text = re.sub(r'^REPO = ".*?"', f'REPO = "{repo.strip()}"', text, flags=re.M)
app.write_text(text, encoding="utf-8")

if version:
    project = root / "pyproject.toml"
    toml = project.read_text(encoding="utf-8")
    toml = re.sub(r'^version = "[^"]*"', f'version = "{version.group(1)}"', toml, count=1, flags=re.M)
    project.write_text(toml, encoding="utf-8")

print(text)
