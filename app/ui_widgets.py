from __future__ import annotations

from typing import Callable, Dict, List, Optional

import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageDraw, ImageTk


def _surface_image(root, fill, border=None, size=(512, 64), radius=8, backdrop="#0b1220"):
    scale = 3
    # Composite the rounded edges once. Transparent tiled ttk images force
    # Windows Tk onto its slow alpha-compositing path on every repaint.
    # A broad center also avoids hundreds of tiny image-tile copies per field.
    image = Image.new("RGB", (size[0]*scale, size[1]*scale), backdrop)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((1, 1, size[0]*scale-2, size[1]*scale-2), radius=radius*scale,
                           fill=fill, outline=border, width=scale if border else 1)
    return ImageTk.PhotoImage(image.resize(size, Image.Resampling.LANCZOS), master=root)


def _control_style(widget, kind, options):
    root = widget._root()
    cache = getattr(root, "_rounded_control_styles", None)
    if cache is None:
        cache = root._rounded_control_styles = {}
    try:
        backdrop = widget.master.cget("bg")
    except tk.TclError:
        backdrop = "#131f30"
    key = (kind, backdrop, tuple((name, str(value)) for name, value in sorted(options.items())))
    if key in cache:
        return cache[key][0]
    style = ttk.Style(root)
    name = f"Mester{len(cache)}"
    fill, foreground = options["bg"], options["fg"]
    normal = _surface_image(root, fill, options.get("highlightbackground", "#2c4058"), backdrop=backdrop)
    focused = _surface_image(root, fill, "#5aa9ef", backdrop=backdrop)
    disabled = _surface_image(root, "#19283d", "#243951", backdrop=backdrop)
    refs = [normal, focused, disabled]
    field = f"{name}.border" if kind in {"Button", "Checkbutton"} else f"{name}.field"
    if kind in {"Button", "Checkbutton"}:
        active = _surface_image(root, options.get("activebackground", fill), "#5a7898", backdrop=backdrop)
        refs.append(active)
        selected = _surface_image(root, options.get("selectcolor", "#3f8fd8"), "#5aa9ef", backdrop=backdrop)
        refs.append(selected)
        style.element_create(field, "image", normal, ("disabled", disabled), ("pressed", active),
                             ("selected", selected), ("active", active), ("focus", focused), border=8, padding=0,
                             width=24, height=24, sticky="nsew")
        children = [(f"{kind}.padding", {"sticky": "nsew", "children": [(f"{kind}.label", {"sticky": "nsew"})]})]
    else:
        style.element_create(field, "image", normal, ("disabled", disabled), ("focus", focused), border=8, padding=0,
                             width=24, height=24, sticky="nsew")
        content = f"{kind}.label" if kind == "Label" else f"{kind}.textarea"
        children = [(f"{kind}.padding", {"sticky": "nsew", "children": [(content, {"sticky": "nsew"})]})]
        if kind == "Combobox":
            arrow = Image.new("RGB", (18, 14), fill)
            ImageDraw.Draw(arrow).line(((4, 5), (8, 9), (12, 5)), fill=foreground, width=2)
            arrow_image = ImageTk.PhotoImage(arrow, master=root)
            refs.append(arrow_image)
            arrow_name = f"{name}.downarrow"
            style.element_create(arrow_name, "image", arrow_image)
            children.insert(0, (arrow_name, {"side": "right", "sticky": ""}))
    style_name = f"{name}.T{kind}"
    style.layout(style_name, [(field, {"sticky": "nsew", "children": children})])
    style.configure(style_name, background=backdrop, fieldbackground=fill, foreground=foreground,
                    insertcolor=options.get("insertbackground", foreground), font=options["font"],
                    padding=(options["padx"], options["pady"]), borderwidth=0)
    style.map(style_name, foreground=[("disabled", "#91a4ba")],
              background=[("disabled", backdrop), ("active", backdrop), ("pressed", backdrop)],
              fieldbackground=[("disabled", "#19283d"), ("readonly", fill)])
    cache[key] = (style_name, refs)
    return style_name


class _RoundedControl:
    _kind = "Entry"
    _visual_options = {"bg", "fg", "padx", "pady", "font", "activebackground", "activeforeground",
                       "insertbackground", "readonlybackground", "highlightbackground", "relief", "bd", "highlightthickness",
                       "selectcolor", "indicatoron"}

    def __init__(self, parent, **options):
        self._visual = {"bg": "#243951" if self._kind == "Button" else "#19283d", "fg": "#f1f6ff",
                        "font": ("Segoe UI", 10, "bold") if self._kind == "Button" else ("Segoe UI", 10),
                        "padx": 9, "pady": 5 if self._kind == "Button" else 3}
        native = self._split(options)
        super().__init__(parent, **native)
        self._applied_style = _control_style(self, self._kind, self._visual)
        super().configure(style=self._applied_style)

    def _split(self, options):
        native = {}
        for name, value in options.items():
            name = {"background": "bg", "foreground": "fg", "borderwidth": "bd"}.get(name, name)
            if name in self._visual_options:
                self._visual[name] = value
            else:
                native[name] = value
        return native

    def configure(self, cnf=None, **options):
        if isinstance(cnf, str) or (cnf is None and not options):
            return super().configure(cnf)
        values = dict(cnf or {})
        values.update(options)
        previous = self._visual.copy()
        native = self._split(values)
        if previous != self._visual:
            style = _control_style(self, self._kind, self._visual)
            if style != self._applied_style:
                native["style"] = self._applied_style = style
        if native:
            return super().configure(**native)

    config = configure

    def refresh_style(self):
        style = _control_style(self, self._kind, self._visual)
        if style != self._applied_style:
            self._applied_style = style
            super().configure(style=style)

    def cget(self, name):
        name = {"background": "bg", "foreground": "fg"}.get(name, name)
        if name in self._visual:
            return self._visual[name]
        value = super().cget(name)
        return str(value) if name == "state" else value


class RoundedButton(_RoundedControl, ttk.Button):
    _kind = "Button"


class RoundedEntry(_RoundedControl, ttk.Entry):
    pass


class RoundedCheckbutton(_RoundedControl, ttk.Checkbutton):
    _kind = "Checkbutton"


class RoundedLabel(_RoundedControl, ttk.Label):
    _kind = "Label"


class RoundedCombobox(_RoundedControl, ttk.Combobox):
    _kind = "Combobox"


def configure_scrollbars(root, background="#0b1220"):
    """Arrow-free native scrollbars with a narrow rounded thumb and hover feedback."""
    style = ttk.Style(root)
    refs = []
    for orientation, size in (("Vertical", (10, 512)), ("Horizontal", (512, 10))):
        trough_size = (12, 512) if orientation == "Vertical" else (512, 12)
        trough = _surface_image(root, "#111d2c", size=trough_size, radius=6, backdrop=background)
        thumb = _surface_image(root, "#405b77", size=size, radius=5, backdrop=background)
        active = _surface_image(root, "#6a91b9", size=size, radius=5, backdrop=background)
        inactive = _surface_image(root, "#111d2c", size=size, radius=5, backdrop=background)
        refs.extend((trough, thumb, active, inactive))
        trough_name = f"Mester.{orientation}.Scrollbar.trough"
        thumb_name = f"Mester.{orientation}.Scrollbar.thumb"
        style.element_create(trough_name, "image", trough, border=5, padding=0, width=12, height=12, sticky="nsew")
        minimum = (10, 26) if orientation == "Vertical" else (26, 10)
        style.element_create(thumb_name, "image", thumb, ("disabled", inactive), ("active", active), ("pressed", active),
                             border=4, padding=0, width=minimum[0], height=minimum[1], sticky="nsew")
        style.layout(f"{orientation}.TScrollbar", [(trough_name, {"sticky": "ns" if orientation == "Vertical" else "ew",
                     "children": [(thumb_name, {"sticky": "nsew"})]})])
        style.configure(f"{orientation}.TScrollbar", background=background, troughcolor=background, borderwidth=0,
                        width=12, arrowsize=0)
        style.map(f"{orientation}.TScrollbar", background=[("active", background), ("disabled", background)])
    root._rounded_scrollbar_images = refs


def rounded_rectangle(canvas, x1, y1, x2, y2, radius=16, **kwargs):
    radius = min(radius, max(0, (x2-x1)/2), max(0, (y2-y1)/2))
    return canvas.create_polygon(
        x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
        x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
        x1, y2, x1, y2-radius, x1, y1+radius, x1, y1,
        smooth=True, splinesteps=24, **kwargs)


class RoundedPanel(tk.Frame):
    """A native-Tk rounded surface; no image or third-party dependency."""
    def __init__(self, parent, fill, border, padding=12, **kwargs):
        super().__init__(parent, bg=parent.cget("bg"), **kwargs)
        self.fill, self.border = fill, border
        self.canvas = tk.Canvas(self, bg=self.cget("bg"), highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.inner = tk.Frame(self, bg=fill, padx=padding, pady=padding)
        self.inner.pack(fill="both", expand=True, padx=9, pady=9)
        self._drawn_surface = None
        self.bind("<Configure>", self.draw)

    def draw(self, event=None):
        surface = (self.winfo_width(), self.winfo_height(), self.fill, self.border)
        if surface == self._drawn_surface:
            return
        self._drawn_surface = surface
        self.canvas.delete("all")
        rounded_rectangle(self.canvas, 1, 1, self.winfo_width()-1, self.winfo_height()-1,
                          fill=self.fill, outline=self.border, width=1)

    def set_border(self, color):
        if color == self.border:
            return
        self.border = color
        self.draw()


class ModeSwitch(tk.Canvas):
    """Two-position, keyboard-accessible switch with a shared StringVar."""
    def __init__(self, parent, variable, command, fill, accent, text, muted):
        super().__init__(parent, width=178, height=36, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, takefocus=True, cursor="hand2")
        self.variable, self.command = variable, command
        self.fill, self.accent, self.text, self.muted = fill, accent, text, muted
        self.bind("<Button-1>", self.clicked)
        self.bind("<space>", lambda e: self.choose("select" if variable.get() == "auto" else "auto"))
        self.bind("<Return>", lambda e: self.choose("select" if variable.get() == "auto" else "auto"))
        self.bind("<Left>", lambda e: self.choose("auto"))
        self.bind("<Right>", lambda e: self.choose("select"))
        self.bind("<FocusIn>", self.draw)
        self.bind("<FocusOut>", self.draw)
        self.trace = variable.trace_add("write", lambda *_: self.draw())
        self.draw()

    def clicked(self, event):
        self.focus_set()
        return self.choose("auto" if event.x < 89 else "select")

    def choose(self, mode):
        if self.variable.get() != mode:
            self.variable.set(mode)
            self.command()
        return "break"

    def draw(self, event=None):
        self.delete("all")
        rounded_rectangle(self, 1, 1, 177, 35, radius=17, fill=self.fill,
                          outline=self.accent if self.focus_get() == self else self.fill)
        left = 4 if self.variable.get() == "auto" else 89
        rounded_rectangle(self, left, 4, left+85, 32, radius=14, fill=self.accent, outline="")
        for x, label, mode in ((46, "Auto", "auto"), (131, "Select", "select")):
            self.create_text(x, 18, text=label, fill=self.text if self.variable.get() == mode else self.muted,
                             font=("Segoe UI", 10, "bold"))

    def destroy(self):
        self.variable.trace_remove("write", self.trace)
        super().destroy()


class ChipSelector:
    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        help_text: str,
        initial: List[str],
        options: List[str],
        colors: Dict[str, str],
        on_change: Optional[Callable[[], None]] = None,
    ):
        self.values = list(initial)
        self.options = options[:]
        self.colors = colors
        self.on_change = on_change
        self.frame = tk.Frame(parent, bg=colors["card2"], padx=12, pady=12, highlightthickness=2, highlightbackground=colors["border"])
        tk.Label(self.frame, text=title, bg=colors["card2"], fg=colors["text"], font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(self.frame, text=help_text, bg=colors["card2"], fg=colors["muted"], font=("Segoe UI", 9), wraplength=330, justify="left").pack(anchor="w", pady=(3, 8))
        row = tk.Frame(self.frame, bg=colors["card2"])
        row.pack(fill="x")
        self.combo = RoundedCombobox(row, values=self.options, state="readonly", width=14)
        self.combo.pack(side="left", fill="x", expand=True)
        RoundedButton(row, text="Add", command=self.add_selected, bg=colors["blue"], fg="white", relief="flat", padx=10, pady=5).pack(side="left", padx=(8, 0))
        self.chips = tk.Frame(self.frame, bg=colors["card2"])
        self.chips.pack(fill="x", pady=(8, 0))
        self.refresh()

    def add_selected(self) -> None:
        value = self.combo.get().strip()
        if value and value not in self.values:
            self.values.append(value)
            self.refresh()
            if self.on_change:
                self.on_change()

    def remove(self, value: str) -> None:
        if value in self.values:
            self.values.remove(value)
            self.refresh()
            if self.on_change:
                self.on_change()

    def refresh(self) -> None:
        for child in self.chips.winfo_children():
            child.destroy()
        for value in self.values:
            chip = tk.Frame(self.chips, bg=self.colors["card3"])
            chip.pack(side="left", padx=(0, 6), pady=4)
            tk.Label(chip, text=value, bg=self.colors["card3"], fg=self.colors["text"], font=("Segoe UI", 9), padx=8, pady=4).pack(side="left")
            RoundedButton(chip, text="x", command=lambda v=value: self.remove(v), bg=self.colors["card3"], fg=self.colors["text"], relief="flat", padx=6).pack(side="left")

    def get(self) -> List[str]:
        return list(self.values)
