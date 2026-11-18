"""Custom widgets for the app window: rounded cards, toggle switches and the sidebar.

Tk has no rounded corners or switches, so these draw themselves. Corners and switches
are rendered with Pillow (smooth edges) and redrawn when the light/dark mode changes.
"""

import tkinter as tk
import tkinter.font as tkfont

from PIL import Image, ImageDraw, ImageTk

SCALE = 4  # draw larger, then shrink: smooth edges


def _rgba(color):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4)) + (255,)


def icon_font():
    families = set(tkfont.families())
    for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if name in families:
            return name
    return None


class RoundCard(tk.Canvas):
    """A rounded panel. Put widgets in `.body` (a Frame); the card grows to fit them."""

    def __init__(self, parent, theme, radius=16, pad=(20, 14), fill="card", outline="border", back="bg",
                 fit_width=False):
        super().__init__(parent, highlightthickness=0, bd=0, height=10, width=10)
        self.theme, self.radius, self.pad, self.fit_width = theme, radius, pad, fit_width
        self.fill, self.outline, self.back = fill, outline, back
        self.body = tk.Frame(self, bd=0, highlightthickness=0)
        self.body.columnconfigure(0, weight=1)
        self._win = self.create_window(pad[0], pad[1], window=self.body, anchor="nw")
        self._corners = None
        self.bind("<Configure>", lambda _e: self._draw())
        self.body.bind("<Configure>", lambda _e: self._fit())
        theme.listen(self._recolor)
        self._recolor()
        self.after_idle(self._watch)

    def _recolor(self):
        p = self.theme.pal
        self.configure(bg=p[self.back])
        self.body.configure(bg=p[self.fill])
        self._corners = None
        self._draw()

    def _watch(self):
        # The body's requested size changes as its contents do; follow it.
        try:
            self._fit()
            self.after(300, self._watch)
        except tk.TclError:
            pass  # destroyed

    def _fit(self):
        height = self.body.winfo_reqheight() + 2 * self.pad[1]
        if int(self.cget("height")) != height:
            self.configure(height=height)
        if self.fit_width:
            width = self.body.winfo_reqwidth() + 2 * self.pad[0]
            if int(self.cget("width")) != width:
                self.configure(width=width)

    def _corner_images(self):
        p, r = self.theme.pal, self.radius
        big = 2 * r * SCALE
        img = Image.new("RGBA", (big, big), _rgba(p[self.back]))
        ImageDraw.Draw(img).rounded_rectangle((0, 0, big - 1, big - 1), radius=r * SCALE,
                                              fill=_rgba(p[self.fill]), outline=_rgba(p[self.outline]),
                                              width=SCALE)
        img = img.resize((2 * r, 2 * r), Image.LANCZOS)
        boxes = {"nw": (0, 0, r, r), "ne": (r, 0, 2 * r, r), "sw": (0, r, r, 2 * r), "se": (r, r, 2 * r, 2 * r)}
        return {k: ImageTk.PhotoImage(img.crop(b), master=self) for k, b in boxes.items()}

    def _draw(self):
        w, h, r = self.winfo_width(), self.winfo_height(), self.radius
        if w < 2 * r or h < 2 * r:
            return
        p = self.theme.pal
        if self._corners is None:
            self._corners = self._corner_images()
        self.delete("shape")
        fill, line = p[self.fill], p[self.outline]
        self.create_rectangle(r, 0, w - r, h, fill=fill, width=0, tags="shape")
        self.create_rectangle(0, r, w, h - r, fill=fill, width=0, tags="shape")
        self.create_line(r, 0, w - r, 0, fill=line, tags="shape")
        self.create_line(r, h - 1, w - r, h - 1, fill=line, tags="shape")
        self.create_line(0, r, 0, h - r, fill=line, tags="shape")
        self.create_line(w - 1, r, w - 1, h - r, fill=line, tags="shape")
        for key, x, y in (("nw", 0, 0), ("ne", w - r, 0), ("sw", 0, h - r), ("se", w - r, h - r)):
            self.create_image(x, y, image=self._corners[key], anchor="nw", tags="shape")
        self.tag_lower("shape")
        if not self.fit_width:
            self.itemconfigure(self._win, width=w - 2 * self.pad[0])


class Toggle(tk.Canvas):
    """An on/off switch bound to a BooleanVar. `command` runs after a click."""

    W, H = 44, 24

    def __init__(self, parent, theme, variable, command=None, back="card"):
        super().__init__(parent, width=self.W, height=self.H, highlightthickness=0, bd=0, cursor="hand2")
        self.theme, self.variable, self.command, self.back = theme, variable, command, back
        self.enabled = True
        self._images = {}
        self.bind("<Button-1>", self._click)
        variable.trace_add("write", lambda *_: self._draw())
        theme.listen(self._recolor)
        self._recolor()

    def _recolor(self):
        self._images = {}
        self.configure(bg=self.theme.pal[self.back])
        self._draw()

    def _image(self, on, enabled):
        key = (on, enabled)
        if key not in self._images:
            p = self.theme.pal
            w, h = self.W * SCALE, self.H * SCALE
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            track = p["toggle_on"] if on else p["toggle_off"]
            if not enabled:
                track = p["toggle_dis_on"] if on else p["toggle_dis"]
            d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=_rgba(track))
            m = 3 * SCALE
            x = w - h + m if on else m
            d.ellipse((x, m, x + h - 2 * m, h - m), fill=(255, 255, 255, 255))
            self._images[key] = ImageTk.PhotoImage(img.resize((self.W, self.H), Image.LANCZOS), master=self)
        return self._images[key]

    def _draw(self):
        self.delete("all")
        self.create_image(0, 0, image=self._image(bool(self.variable.get()), self.enabled), anchor="nw")

    def _click(self, _event):
        if not self.enabled:
            return
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()

    def configure(self, cnf=None, **kw):  # lets callers use .configure(state=...) like a Checkbutton
        if "state" in kw:
            self.enabled = kw.pop("state") != "disabled"
            super().configure(cursor="hand2" if self.enabled else "arrow")
            self._draw()
        return super().configure(cnf, **kw) if (cnf or kw) else None

    config = configure


class IconButton(tk.Canvas):
    """A small rounded square with an icon (Copy, New...), sized to sit next to a text field.
    `flash` briefly shows a tick after a click, to confirm a copy."""

    def __init__(self, parent, theme, glyph, command, tooltip="", size=36, back="card", flash=False):
        super().__init__(parent, width=size, height=size, highlightthickness=0, bd=0, cursor="hand2")
        self.theme, self.glyph, self.command, self.size, self.back = theme, glyph, command, size, back
        self.flash, self.enabled, self.hot, self.done = flash, True, False, False
        self.family = icon_font()
        self._images = {}
        self.tip = Tooltip(self, tooltip) if tooltip else None
        self.bind("<Enter>", lambda _e: self._set_hot(True))
        self.bind("<Leave>", lambda _e: self._set_hot(False))
        self.bind("<Button-1>", self._click)
        theme.listen(self._recolor)
        self._recolor()

    def _recolor(self):
        self._images = {}
        self.configure(bg=self.theme.pal[self.back])
        self._draw()

    def _set_hot(self, hot):
        self.hot = hot
        self._draw()

    def _square(self, color):
        if color not in self._images:
            n = self.size * SCALE
            img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
            ImageDraw.Draw(img).rounded_rectangle((0, 0, n - 1, n - 1), radius=n // 4, fill=_rgba(color))
            self._images[color] = ImageTk.PhotoImage(img.resize((self.size, self.size), Image.LANCZOS), master=self)
        return self._images[color]

    def _draw(self):
        p = self.theme.pal
        self.delete("all")
        if not self.enabled:
            fill, ink = p["field_ro"], p["grey"]
        else:
            fill, ink = (p["soft_hover"] if self.hot else p["soft"]), p["accent"]
        self.create_image(0, 0, image=self._square(fill), anchor="nw")
        glyph = "" if self.done else self.glyph  # tick
        if self.family:
            self.create_text(self.size // 2, self.size // 2, text=glyph, fill=ink, font=(self.family, 11))

    def _click(self, _event):
        if not self.enabled:
            return
        self.command()
        if self.flash:
            self.done = True
            self._draw()
            self.after(1200, self._unflash)

    def _unflash(self):
        self.done = False
        try:
            self._draw()
        except tk.TclError:
            pass

    def configure(self, cnf=None, **kw):  # .configure(state=...) like a Button
        if "state" in kw:
            self.enabled = kw.pop("state") != "disabled"
            super().configure(cursor="hand2" if self.enabled else "arrow")
            self._draw()
        return super().configure(cnf, **kw) if (cnf or kw) else None

    config = configure


class Tooltip:
    """A small label that appears under a widget while the pointer rests on it."""

    def __init__(self, widget, text):
        self.widget, self.text, self.win, self.job = widget, text, None, None
        widget.bind("<Enter>", lambda _e: self._schedule(), add="+")
        widget.bind("<Leave>", lambda _e: self._hide(), add="+")
        widget.bind("<Button-1>", lambda _e: self._hide(), add="+")

    def _schedule(self):
        self._hide()
        self.job = self.widget.after(450, self._show)

    def _show(self):
        w = self.widget
        self.win = tk.Toplevel(w)
        self.win.wm_overrideredirect(True)
        self.win.attributes("-topmost", True)
        tk.Label(self.win, text=self.text, bg="#1f2937", fg="#ffffff", font=("Segoe UI", 9),
                 padx=8, pady=3).pack()
        self.win.geometry(f"+{w.winfo_rootx()}+{w.winfo_rooty() + w.winfo_height() + 4}")

    def _hide(self):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.win:
            self.win.destroy()
            self.win = None


class Sidebar(tk.Canvas):
    """The coloured navigation column: logo, page buttons, and a line at the bottom."""

    def __init__(self, parent, theme, title, logo_path, width=180):
        self.s = parent.winfo_fpixels("1i") / 96  # display scaling: fonts grow with it, so must layout
        super().__init__(parent, width=round(width * self.s), highlightthickness=0, bd=0)
        self.theme, self.title, self.items, self.selected = theme, title, [], None
        self.icon_family = icon_font()
        try:
            self.logo = ImageTk.PhotoImage(Image.open(logo_path).resize((round(28 * self.s),) * 2, Image.LANCZOS), master=self)
        except Exception:
            self.logo = None
        self.footer = ""
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda _e: self._hover(None))
        self.bind("<Button-1>", self._click)
        self.hovered = None
        self._pill = {}
        theme.listen(self._recolor)

    def add(self, key, label, glyph, command):
        self.items.append({"key": key, "label": label, "glyph": glyph, "command": command})

    def select(self, key):
        self.selected = key
        self._draw()

    def set_footer(self, text):
        self.footer = text
        self._draw()

    def _recolor(self):
        self._pill = {}
        self._gradient = None
        self._draw()

    def _background(self, w, h):
        """A vertical gradient through the icon's colours."""
        key = (w, h, self.theme.dark)
        if getattr(self, "_gradient", None) is None or self._gradient[0] != key:
            stops = [_rgba(c)[:3] for c in self.theme.pal["side_stops"]]
            column = Image.new("RGB", (1, h))
            for y in range(h):
                f = y / max(h - 1, 1) * (len(stops) - 1)
                i = min(int(f), len(stops) - 2)
                a, b, k = stops[i], stops[i + 1], f - i
                column.putpixel((0, y), tuple(round(a[c] + (b[c] - a[c]) * k) for c in range(3)))
            self._gradient = (key, ImageTk.PhotoImage(column.resize((w, h)), master=self))
        return self._gradient[1]

    def _pill_image(self, alpha, w, h):
        """A translucent white rounded highlight, so the gradient shows through."""
        key = (alpha, w, h)
        if key not in self._pill:
            img = Image.new("RGBA", (w * SCALE, h * SCALE), (0, 0, 0, 0))
            ImageDraw.Draw(img).rounded_rectangle((0, 0, w * SCALE - 1, h * SCALE - 1), radius=10 * SCALE,
                                                  fill=(255, 255, 255, alpha))
            self._pill[key] = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS), master=self)
        return self._pill[key]

    @property
    def item_top(self):
        return round(78 * self.s)

    @property
    def item_h(self):
        return round(34 * self.s)

    def _item_at(self, y):
        if y is None or y < self.item_top:
            return None
        index = (y - self.item_top) // self.item_h
        return self.items[index]["key"] if 0 <= index < len(self.items) else None

    def _hover(self, event):
        key = self._item_at(event.y if event else None)
        if key != self.hovered:
            self.hovered = key
            self.configure(cursor="hand2" if key else "arrow")
            self._draw()

    def _click(self, event):
        key = self._item_at(event.y)
        for item in self.items:
            if item["key"] == key:
                item["command"]()

    def _draw(self):
        p = self.theme.pal
        w, h = self.winfo_width(), self.winfo_height()
        self.delete("all")
        if w < 2 or h < 2:
            return
        self.create_image(0, 0, image=self._background(w, h), anchor="nw")
        s = self.s
        if self.logo:
            self.create_image(round(16 * s), round(24 * s), image=self.logo, anchor="nw")
        # The name must fit beside the logo at any display scaling: shrink the font until it does.
        room = w - round(52 * s) - round(12 * s)
        size = 11
        while size > 8 and tkfont.Font(family="Segoe UI", size=size, weight="bold").measure(self.title) > room:
            size -= 1
        self.create_text(round(52 * s), round(38 * s), text=self.title, anchor="w", fill="#ffffff",
                         font=("Segoe UI", size, "bold"))
        for i, item in enumerate(self.items):
            top, ih = self.item_top + i * self.item_h, self.item_h
            chosen, hot = item["key"] == self.selected, item["key"] == self.hovered
            if chosen or hot:
                self.create_image(round(10 * s), top + 2, anchor="nw",
                                  image=self._pill_image(56 if chosen else 28,
                                                         max(w - round(20 * s), 10), ih - 4))
            color = "#ffffff" if chosen or hot else p["side_text"]
            if self.icon_family:
                self.create_text(round(22 * s), top + ih // 2, text=item["glyph"], fill=color, anchor="w",
                                 font=(self.icon_family, 12))
            self.create_text(round(52 * s), top + ih // 2, text=item["label"], fill=color, anchor="w",
                             font=("Segoe UI Semibold", 10) if chosen else ("Segoe UI", 10))
        if self.footer:
            self.create_text(round(18 * s), h - round(18 * s), text=self.footer, fill=p["side_text"], anchor="w",
                             font=("Segoe UI", 9))
