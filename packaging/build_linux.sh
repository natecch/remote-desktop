#!/usr/bin/env bash
# Builds the Linux app, a .deb installer and a .tar.gz.  Run ON Linux:
#   sudo apt install python3-venv python3-tk python3-dev dpkg-dev curl   (once)
#   bash packaging/build_linux.sh
# Output: build/installer/remote-desktop_<version>_<arch>.deb and .tar.gz
#
# Remote control on Linux needs an X11 session; Wayland blocks injected input.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
VERSION="${VERSION:-$(python3 -c 'import version; print(version.VERSION)')}"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"

python3 -m venv build/venv
# shellcheck disable=SC1091
. build/venv/bin/activate
pip install --quiet -r requirements.txt pyinstaller

mkdir -p build/bin build/installer
curl -fsSL -o build/bin/cloudflared \
  "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}"
chmod +x build/bin/cloudflared

pyinstaller --noconfirm --clean --windowed --name RemoteDesktop \
  --add-data "$ROOT/viewer.html:." --add-data "$ROOT/assets/icon-256.png:assets" \
  --add-binary "$ROOT/build/bin/cloudflared:bin" \
  --hidden-import mss.linux --hidden-import PIL._tkinter_finder \
  --collect-submodules websockets --collect-submodules pynput --collect-submodules Xlib \
  --hidden-import pynput.keyboard._xorg --hidden-import pynput.mouse._xorg \
  --hidden-import pynput._util.xorg --hidden-import pynput._util.xorg_keysyms \
  --hidden-import pystray._xorg --collect-data soundcard --hidden-import soundcard.pulseaudio \
  --distpath "$ROOT/build/dist" --workpath "$ROOT/build/work" --specpath "$ROOT/build" app.py
# pynput picks its backend at run time, so PyInstaller cannot see it; without a display it
# also cannot import it to collect it. The explicit hidden imports above cover that.

# ---- check the built app actually starts and finds everything (needs a display) ----
if [ -n "${DISPLAY:-}" ]; then
  build/dist/RemoteDesktop/RemoteDesktop --selftest
elif command -v xvfb-run >/dev/null; then
  xvfb-run -a build/dist/RemoteDesktop/RemoteDesktop --selftest
else
  echo "WARNING: no display and no xvfb-run; skipping the self-test" >&2
fi

# ---- .deb: app in /opt, launcher in /usr/bin, menu entry ----
PKG="build/deb/remote-desktop_${VERSION}_${ARCH}"
rm -rf build/deb
ICONS="$PKG/usr/share/icons/hicolor"
mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/bin" "$PKG/usr/share/applications" \
  "$ICONS/256x256/apps" "$ICONS/1024x1024/apps"
cp -r build/dist/RemoteDesktop "$PKG/opt/remote-desktop"
ln -s /opt/remote-desktop/RemoteDesktop "$PKG/usr/bin/remote-desktop"
cp assets/icon-256.png "$ICONS/256x256/apps/remote-desktop.png"
cp assets/icon.png "$ICONS/1024x1024/apps/remote-desktop.png"
cat > "$PKG/usr/share/applications/remote-desktop.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Remote Desktop
Comment=Share this computer or control another one
Exec=/opt/remote-desktop/RemoteDesktop
Icon=remote-desktop
Terminal=false
Categories=Network;RemoteAccess;
EOF
cat > "$PKG/DEBIAN/control" <<EOF
Package: remote-desktop
Version: ${VERSION}
Section: net
Priority: optional
Architecture: ${ARCH}
Maintainer: Remote Desktop <noreply@localhost>
Description: Share this computer or control another one
 Screen sharing with mouse and keyboard control, file transfer and chat.
EOF
dpkg-deb --build --root-owner-group "$PKG" "build/installer/remote-desktop_${VERSION}_${ARCH}.deb"

# ---- plain archive for other distributions ----
cp assets/icon.png build/dist/RemoteDesktop/remote-desktop.png
tar -C build/dist -czf "build/installer/remote-desktop_${VERSION}_${ARCH}.tar.gz" RemoteDesktop
ls -lh build/installer
