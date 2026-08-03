from __future__ import annotations

import base64
import threading
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Iterable, Optional

import tkinter as tk


def scale_photo(img: tk.PhotoImage, max_width: int, max_height: int) -> tk.PhotoImage:
    x = max(1, (img.width() + max_width - 1) // max_width)
    y = max(1, (img.height() + max_height - 1) // max_height)
    factor = max(x, y)
    return img.subsample(factor, factor) if factor > 1 else img


def scrub_index_from_event(event: tk.Event, count: int, fallback: float = 0.5) -> int:
    if count <= 1:
        return 0
    try:
        widget = event.widget
        relative = (int(event.x_root) - widget.winfo_rootx()) / max(1, widget.winfo_width())
    except Exception:
        relative = fallback
    relative = max(0.0, min(0.999, relative))
    return min(count - 1, int(relative * count))


class ImageCache:
    def __init__(self, max_items: int) -> None:
        self.max_items = max_items
        self.images: Dict[str, tk.PhotoImage] = {}
        self.order: Deque[str] = deque()
        self.source_data: Dict[str, str] = {}
        self.source_order: Deque[str] = deque()
        self.lock = threading.RLock()

    def has_source(self, path: str) -> bool:
        with self.lock:
            return path in self.source_data or any(key.startswith(f"{path}::") for key in self.images)

    def put_source_bytes(self, path: str, data: bytes) -> None:
        if not data:
            return
        encoded = base64.b64encode(data).decode("ascii")
        with self.lock:
            self.source_data[path] = encoded
            try:
                self.source_order.remove(path)
            except ValueError:
                pass
            self.source_order.append(path)
            while len(self.source_order) > self.max_items:
                old_path = self.source_order.popleft()
                self.source_data.pop(old_path, None)

    def remove(self, key: str) -> None:
        with self.lock:
            matching = [cache_key for cache_key in self.images if cache_key == key or cache_key.startswith(f"{key}::")]
            for cache_key in matching:
                self.images.pop(cache_key, None)
            self.source_data.pop(key, None)
            try:
                self.source_order.remove(key)
            except ValueError:
                pass
            for cache_key in matching:
                try:
                    self.order.remove(cache_key)
                except ValueError:
                    pass

    def put(self, key: str, image: tk.PhotoImage) -> None:
        self.images[key] = image
        try:
            self.order.remove(key)
        except ValueError:
            pass
        self.order.append(key)
        while len(self.order) > self.max_items:
            old_key = self.order.popleft()
            self.images.pop(old_key, None)

    def get_thumbnail(self, path: str, max_width: int = 222, max_height: int = 125) -> Optional[tk.PhotoImage]:
        cache_key = f"{path}::{max_width}x{max_height}"
        if cache_key not in self.images:
            try:
                with self.lock:
                    encoded = self.source_data.get(path)
                if encoded:
                    try:
                        img = tk.PhotoImage(data=encoded)
                    except Exception:
                        img = tk.PhotoImage(file=path)
                else:
                    if not Path(path).exists():
                        return None
                    img = tk.PhotoImage(file=path)
                if img.width() > max_width or img.height() > max_height:
                    img = scale_photo(img, max_width, max_height)
                elif img.width() < 200 and img.height() < 110:
                    zx = max(1, min(4, max_width // max(1, img.width())))
                    zy = max(1, min(4, max_height // max(1, img.height())))
                    img = img.zoom(min(zx, zy), min(zx, zy))
                self.put(cache_key, img)
            except Exception:
                return None
        return self.images[cache_key]

    def configure_label(self, label: tk.Label, path: str, max_width: int = 222, max_height: int = 125) -> bool:
        img = self.get_thumbnail(path, max_width, max_height)
        if not img:
            return False
        label.configure(image=img, text="")
        label.image = img
        return True


class ThumbnailPopup:
    def __init__(self, root: tk.Tk, zoom: int = 3) -> None:
        self.root = root
        self.zoom = zoom
        self.popup: Optional[tk.Toplevel] = None
        self.label: Optional[tk.Label] = None
        self.paths: list[str] = []
        self.images: Dict[str, tk.PhotoImage] = {}
        self.index: Optional[int] = None

    def close(self) -> None:
        if self.popup:
            try:
                self.popup.destroy()
            except Exception:
                pass
        self.popup = None
        self.label = None
        self.paths = []
        self.images = {}
        self.index = None

    def set_image(self, index: int) -> None:
        if not self.label or not self.paths:
            return
        index = max(0, min(len(self.paths) - 1, index))
        if self.index == index:
            return
        path = self.paths[index]
        if path not in self.images:
            try:
                self.images[path] = tk.PhotoImage(file=path).zoom(self.zoom, self.zoom)
            except Exception:
                return
        img = self.images[path]
        self.label.configure(image=img)
        self.label.image = img
        self.index = index

    def scrub(self, event: tk.Event) -> None:
        self.set_image(scrub_index_from_event(event, len(self.paths)))

    def show(self, paths: Iterable[str]) -> str:
        self.close()
        self.paths = [p for p in paths if Path(p).exists()]
        if not self.paths:
            return "break"
        popup = tk.Toplevel(self.root)
        self.popup = popup
        popup.configure(bg="#000000")
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.geometry(f"{self.root.winfo_width()}x{self.root.winfo_height()}+{self.root.winfo_rootx()}+{self.root.winfo_rooty()}")
        frame = tk.Frame(popup, bg="#000000")
        frame.pack(fill="both", expand=True)
        try:
            label = tk.Label(frame, bg="#000000")
            label.pack(expand=True)
            self.label = label
            self.set_image(len(self.paths) // 2)
            hint = tk.Label(frame, text="Click to close", bg="#000000", fg="#8f99a8", font=("Segoe UI", 11, "bold"))
            hint.pack(pady=(0, 18))
            hint.bind("<Button-1>", lambda e: self.close())
            label.bind("<Button-1>", lambda e: self.close())
            label.bind("<Motion>", self.scrub)
            label.bind("<Enter>", self.scrub)
        except Exception:
            label = tk.Label(frame, text="Preview unavailable", bg="#000000", fg="white", font=("Segoe UI", 18, "bold"))
            label.pack(expand=True)
            label.bind("<Button-1>", lambda e: self.close())
        for widget in [popup, frame]:
            widget.bind("<Button-1>", lambda e: self.close())
            widget.bind("<Motion>", self.scrub)
            widget.bind("<Enter>", self.scrub)
            widget.bind("<Escape>", lambda e: self.close())
        popup.focus_set()
        return "break"
