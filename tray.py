"""System tray icon, so the app keeps running (and sharing) after its window is closed.

Windows and Linux only. On macOS the tray library needs the main thread, which Tk
already owns, so there the window minimises to the Dock instead.

Menu clicks arrive on the tray's own thread; they are handed to the app through
`post`, which puts them on the app's event queue for the Tk thread to act on.
"""

import sys
import threading

try:
    if sys.platform == "darwin":
        raise ImportError("not used on macOS")
    import pystray
    from PIL import Image
except Exception:  # missing, or no tray available on this desktop
    pystray = None


class Tray:
    def __init__(self, image_path, post):
        self.image_path = image_path
        self.post = post            # post(action) with action in "open", "toggle", "quit"
        self.icon = None
        self.sharing = False
        self.tooltip = "Remote Desktop"

    @property
    def available(self):
        return self.icon is not None

    def start(self):
        """Show the tray icon. Returns False when no tray can be used here."""
        if pystray is None:
            return False
        try:
            image = Image.open(self.image_path)
            menu = pystray.Menu(
                pystray.MenuItem("Open Remote Desktop", lambda: self.post("open"), default=True),
                pystray.MenuItem(lambda item: "Stop sharing" if self.sharing else "Start sharing",
                                 lambda: self.post("toggle")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda: self.post("quit")),
            )
            self.icon = pystray.Icon("RemoteDesktop", image, self.tooltip, menu)
            ready = threading.Event()
            threading.Thread(target=self._run, args=(ready,), daemon=True).start()
            ready.wait(5)
            return True
        except Exception:
            self.icon = None
            return False

    def _run(self, ready):
        def setup(icon):
            icon.visible = True
            ready.set()
        try:
            self.icon.run(setup)
        except Exception:
            ready.set()

    def update(self, sharing, tooltip):
        """Refresh the menu's Start/Stop text and the hover text."""
        self.sharing, self.tooltip = sharing, tooltip
        if self.icon:
            try:
                self.icon.title = tooltip
                self.icon.update_menu()
            except Exception:
                pass

    def notify(self, message, title="Remote Desktop"):
        if self.icon:
            try:
                self.icon.notify(message, title)
            except Exception:
                pass

    def stop(self):
        icon, self.icon = self.icon, None
        if icon:
            try:
                icon.stop()
            except Exception:
                pass
