#!/usr/bin/env bash
# Builds RemoteDesktop.app and a .dmg installer.  Run ON macOS (python.org or Homebrew Python 3 with Tk):
#   bash packaging/build_macos.sh
# Output: build/installer/RemoteDesktop-<version>-<arch>.dmg
#
# The app is not code-signed. On first launch use right-click > Open. To share
# the screen and be controlled, allow it under System Settings > Privacy &
# Security > Screen Recording and > Accessibility.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
VERSION="${VERSION:-$(sed -n 's/^VERSION = "\(.*\)"/\1/p' version.py)}"
case "$(uname -m)" in arm64) ARCH=arm64 ;; *) ARCH=amd64 ;; esac

# uv installs the exact versions in uv.lock (Python from .python-version) into .venv.
command -v uv >/dev/null || { echo "uv is not installed: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }
uv sync --frozen --group build
# shellcheck disable=SC1091
. .venv/bin/activate

mkdir -p build/bin build/installer
curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-${ARCH}.tgz" \
  | tar -xz -C build/bin cloudflared
chmod +x build/bin/cloudflared

pyinstaller --noconfirm --clean --windowed --name RemoteDesktop \
  --osx-bundle-identifier com.remotedesktop.app --icon "$ROOT/assets/icon.icns" \
  --add-data "$ROOT/viewer.html:." --add-data "$ROOT/assets/icon-256.png:assets" \
  --add-binary "$ROOT/build/bin/cloudflared:bin" \
  --hidden-import mss.darwin --collect-submodules websockets --collect-submodules pynput \
  --collect-data soundcard --hidden-import soundcard.coreaudio \
  --hidden-import pynput.keyboard._darwin --hidden-import pynput.mouse._darwin \
  --hidden-import pynput._util.darwin --hidden-import pynput._util.darwin_vks \
  --distpath "$ROOT/build/dist" --workpath "$ROOT/build/work" --specpath "$ROOT/build" app.py

# ---- check the built app actually starts and finds everything ----
build/dist/RemoteDesktop.app/Contents/MacOS/RemoteDesktop --selftest

DMG="build/installer/RemoteDesktop-${VERSION}-${ARCH}.dmg"
rm -rf build/dmg "$DMG"
mkdir -p build/dmg
cp -R build/dist/RemoteDesktop.app build/dmg/
ln -s /Applications build/dmg/Applications   # drag the app onto this to install
hdiutil create -volname "Remote Desktop" -srcfolder build/dmg -ov -format UDZO "$DMG"
ls -lh build/installer
