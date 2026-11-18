"""Light and dark appearance for the app window, following the system setting.

Windows: "Choose your app mode" (Settings > Personalization > Colors). macOS: Dark
appearance. Linux: GNOME's color-scheme. The setting is checked every few seconds,
so switching it changes the open windows too.

ttk widgets are styled through the "clam" theme, whose colours can all be set (the
native Windows theme ignores most of them). Plain Tk widgets are registered with
paint() and recoloured whenever the mode changes.
"""

import ctypes
import subprocess
import sys
import tkinter as tk
from tkinter import ttk

WINDOWS, MAC = sys.platform == "win32", sys.platform == "darwin"
FONT = "Segoe UI" if WINDOWS else ("SF Pro Text" if MAC else "Helvetica")
MONO = "Cascadia Mono" if WINDOWS else "Menlo" if MAC else "Monospace"

PALETTES = {
    "light": {
        "bg": "#f3f4f6", "card": "#ffffff", "border": "#e3e5e8", "text": "#111827", "muted": "#6b7280",
        "field": "#ffffff", "field_ro": "#f3f4f6", "field_border": "#d1d5db",
        "button": "#f9fafb", "button_hover": "#eef0f3", "button_press": "#e3e6ea", "button_border": "#d1d5db",
        "accent": "#2563eb", "accent_hover": "#1d4ed8",
        "green": "#16a34a", "green_hover": "#15803d", "red": "#dc2626", "red_hover": "#b91c1c",
        "amber": "#d97706", "grey": "#9ca3af", "error": "#dc2626",
        "log": "#f9fafb", "heading": "#f3f4f6", "select": "#dbeafe", "thumb": "#cbd5e1",
    },
    "dark": {
        "bg": "#17181b", "card": "#212226", "border": "#2e3036", "text": "#e8eaed", "muted": "#9aa0a6",
        "field": "#2a2c31", "field_ro": "#26282c", "field_border": "#3a3d44",
        "button": "#2c2e33", "button_hover": "#35383e", "button_press": "#3e4148", "button_border": "#3a3d44",
        "accent": "#3b82f6", "accent_hover": "#2563eb",
        "green": "#22a55b", "green_hover": "#1d8f4f", "red": "#e5484d", "red_hover": "#c93c41",
        "amber": "#d98a00", "grey": "#6b7280", "error": "#f87171",
        "log": "#1b1c1f", "heading": "#26282c", "select": "#1e3a8a", "thumb": "#4b5058",
    },
}


def system_dark():
    """True when the system asks apps to use dark mode."""
    try:
        if WINDOWS:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        if MAC:
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=2).stdout
            return "dark" in out.lower()
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=2).stdout
        return "dark" in out.lower()
    except Exception:
        return False


def _dark_title_bar(window, dark):
    """Windows 10/11: make the window's title bar match the mode."""
    if not WINDOWS:
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (newer, older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), 4) == 0:
                break
    except Exception:
        pass


class Theme:
    def __init__(self, root):
        self.root = root
        self.dark = system_dark()
        self.pal = PALETTES["dark" if self.dark else "light"]
        self.painted = []    # (widget, {option: palette key})
        self.windows = []
        self.on_change = None
        self.style = ttk.Style(root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self._apply_styles()

    # ---- registering plain Tk widgets ----
    def paint(self, widget, **options):
        """Colour a Tk widget from palette keys, now and after every mode change."""
        self.painted.append((widget, options))
        widget.configure(**{opt: self.pal[key] for opt, key in options.items()})
        return widget

    def window(self, win):
        """A Toplevel that should follow the mode (background and title bar)."""
        self.windows.append(win)
        win.configure(bg=self.pal["bg"])
        win.after(10, lambda: _dark_title_bar(win, self.dark))
        return win

    def accent_button(self, parent, text, command, color="accent"):
        """A filled, flat button that darkens under the pointer. `color` is a palette key."""
        button = tk.Button(parent, text=text, command=command, fg="white", activeforeground="white",
                           disabledforeground="#f1f5f9", relief="flat", bd=0, highlightthickness=0,
                           cursor="hand2", font=(FONT, 11, "bold"), padx=16, pady=8)
        button.color = color
        self.set_accent(button, color)
        button.bind("<Enter>", lambda _e: str(button["state"]) != "disabled" and
                    button.configure(bg=self.pal[button.color + "_hover"] if button.color + "_hover" in self.pal
                                     else self.pal[button.color]))
        button.bind("<Leave>", lambda _e: button.configure(bg=self.pal[button.color]))
        return button

    def set_accent(self, button, color):
        button.color = color
        button.configure(bg=self.pal[color], activebackground=self.pal.get(color + "_hover", self.pal[color]))

    # ---- following the system ----
    def follow_system(self, interval_ms=3000):
        def check():
            dark = system_dark()
            if dark != self.dark:
                self.set_dark(dark)
            self.root.after(interval_ms, check)
        self.root.after(interval_ms, check)

    def set_dark(self, dark):
        self.dark = dark
        self.pal = PALETTES["dark" if dark else "light"]
        self._apply_styles()
        alive = []
        for widget, options in self.painted:
            try:
                widget.configure(**{opt: self.pal[key] for opt, key in options.items()})
                alive.append((widget, options))
            except tk.TclError:
                pass  # destroyed
        self.painted = alive
        for widget, _ in alive:
            if hasattr(widget, "color"):
                self.set_accent(widget, widget.color)
        for win in list(self.windows):
            try:
                win.configure(bg=self.pal["bg"])
                _dark_title_bar(win, dark)
            except tk.TclError:
                self.windows.remove(win)
        if self.on_change:
            self.on_change()

    def _checkbox_images(self):
        """Rounded checkboxes with a tick, drawn for the current mode (clam's own shows an X)."""
        try:
            from PIL import Image, ImageDraw, ImageTk
        except ImportError:
            return
        p, mode = self.pal, "dark" if self.dark else "light"
        element = f"Check{mode}.indicator"
        size = round(18 * self.root.winfo_fpixels("1i") / 96)  # follow display scaling
        if not hasattr(self, "_check_images"):
            self._check_images = {}
        if mode not in self._check_images:
            big = 4 * size

            def draw(fill, border, tick):
                img = Image.new("RGBA", (big + big // 2, big), (0, 0, 0, 0))  # room before the label
                d = ImageDraw.Draw(img)
                d.rounded_rectangle((2, 2, big - 3, big - 3), radius=big // 5, fill=fill, outline=border,
                                    width=max(4, big // 16))
                if tick:
                    d.line([(big * .27, big * .52), (big * .43, big * .68), (big * .74, big * .34)],
                           fill=tick, width=max(6, big // 9), joint="curve")
                return ImageTk.PhotoImage(img.resize((size + size // 2, size), Image.LANCZOS), master=self.root)

            images = {
                "off": draw(p["field"], p["field_border"], None),
                "on": draw(p["accent"], p["accent"], "#ffffff"),
                "off_dis": draw(p["field_ro"], p["border"], None),
                "on_dis": draw(p["grey"], p["grey"], "#ffffff"),
            }
            self._check_images[mode] = images
            self.style.element_create(element, "image", images["off"],
                                      ("disabled", "selected", images["on_dis"]),
                                      ("disabled", images["off_dis"]),
                                      ("selected", images["on"]), sticky="w")
        for name in ("TCheckbutton", "Card.TCheckbutton"):
            self.style.layout(name, [("Checkbutton.padding", {"sticky": "nswe", "children": [
                (element, {"side": "left", "sticky": ""}),
                ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                    ("Checkbutton.label", {"sticky": "nswe"})]})]})])
            self.style.configure(name, padding=(0, 2), indicatormargin=0)

    def _apply_styles(self):
        p, s = self.pal, self.style
        base = (FONT, 10)
        s.configure(".", background=p["bg"], foreground=p["text"], fieldbackground=p["field"],
                    bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["card"],
                    focuscolor=p["accent"], selectbackground=p["accent"], selectforeground="#ffffff",
                    insertcolor=p["text"], font=base)
        s.configure("TFrame", background=p["bg"])
        s.configure("Card.TFrame", background=p["card"])
        for name, bg, fg, font in (
                ("TLabel", "bg", "text", base), ("Card.TLabel", "card", "text", base),
                ("Head.TLabel", "card", "text", (FONT, 10, "bold")),
                ("Muted.TLabel", "card", "muted", (FONT, 9)), ("BgMuted.TLabel", "bg", "muted", (FONT, 9)),
                ("Title.TLabel", "card", "text", (FONT, 13, "bold")),
                ("Status.TLabel", "bg", "text", (FONT, 15, "bold")),
                ("Error.TLabel", "card", "error", (FONT, 9)), ("Good.TLabel", "card", "green", base)):
            s.configure(name, background=p[bg], foreground=p[fg], font=font)

        s.configure("TButton", background=p["button"], foreground=p["text"], bordercolor=p["button_border"],
                    lightcolor=p["button"], darkcolor=p["button"], focuscolor=p["button"],
                    padding=(12, 5), relief="raised", font=base)
        s.map("TButton",
              background=[("disabled", p["button"]), ("pressed", p["button_press"]), ("active", p["button_hover"])],
              lightcolor=[("pressed", p["button_press"]), ("active", p["button_hover"])],
              darkcolor=[("pressed", p["button_press"]), ("active", p["button_hover"])],
              foreground=[("disabled", p["grey"])],
              bordercolor=[("focus", p["accent"])])
        s.configure("Small.TButton", padding=(8, 3), font=(FONT, 9))

        s.configure("TEntry", fieldbackground=p["field"], foreground=p["text"], bordercolor=p["field_border"],
                    lightcolor=p["field"], darkcolor=p["field"], insertcolor=p["text"], padding=(8, 5))
        s.map("TEntry",
              fieldbackground=[("readonly", p["field_ro"]), ("disabled", p["field_ro"])],
              foreground=[("disabled", p["muted"])],
              bordercolor=[("focus", p["accent"])],
              lightcolor=[("focus", p["accent"])], darkcolor=[("focus", p["accent"])])

        self._checkbox_images()
        for name, bg in (("TCheckbutton", "bg"), ("Card.TCheckbutton", "card")):
            s.configure(name, background=p[bg], foreground=p["text"], font=base, focuscolor=p[bg],
                        indicatorbackground=p["field"], indicatorforeground="#ffffff",
                        upperbordercolor=p["field_border"], lowerbordercolor=p["field_border"],
                        indicatormargin=(0, 0, 8, 0))
            s.map(name, background=[("active", p[bg])],
                  indicatorbackground=[("disabled", p["field_ro"]), ("selected", p["accent"])],
                  upperbordercolor=[("selected", p["accent"])], lowerbordercolor=[("selected", p["accent"])],
                  foreground=[("disabled", p["muted"])])

        s.configure("Treeview", background=p["field"], fieldbackground=p["field"], foreground=p["text"],
                    bordercolor=p["field_border"], lightcolor=p["field"], darkcolor=p["field"],
                    rowheight=24, font=base)
        s.map("Treeview", background=[("selected", p["select"])], foreground=[("selected", p["text"])])
        s.configure("Treeview.Heading", background=p["heading"], foreground=p["muted"], relief="flat",
                    bordercolor=p["field_border"], lightcolor=p["heading"], darkcolor=p["heading"],
                    font=(FONT, 9, "bold"), padding=(6, 4))
        s.map("Treeview.Heading", background=[("active", p["heading"])])

        # Slim scrollbars without arrow buttons.
        for orient, sticky in (("Vertical", "ns"), ("Horizontal", "ew")):
            s.layout(f"{orient}.TScrollbar", [(f"{orient}.Scrollbar.trough", {"sticky": sticky, "children": [
                (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
            s.configure(f"{orient}.TScrollbar", background=p["thumb"], troughcolor=p["log"],
                        bordercolor=p["log"], lightcolor=p["thumb"], darkcolor=p["thumb"],
                        arrowcolor=p["muted"], arrowsize=8, gripcount=0, width=8)
            s.map(f"{orient}.TScrollbar", background=[("active", p["muted"])],
                  lightcolor=[("active", p["muted"])], darkcolor=[("active", p["muted"])])
