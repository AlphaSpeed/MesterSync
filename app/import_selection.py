"""Device-triggered, virtualized intake review inside the dashboard."""
from __future__ import annotations

import queue
import threading
import uuid
import math
import tkinter as tk
from tkinter import ttk

from file_utils import no_window_flags, path_key
from thumbnail_ui import ImageCache, ThumbnailPopup, scrub_index_from_event
from thumbnails import extract_video_thumbnails
from ui_performance import virtual_row_window
from ui_widgets import RoundedPanel, RoundedCombobox


class ImportSelection:
    def __init__(self, app):
        self.app = app
        self.items = {}
        self.session_id = uuid.uuid4().hex
        self.previews = {}
        self.tokens = {}
        self.requested = set()
        self.override_keys = set()
        self.base_presets = {}
        self.jobs, self.results = queue.Queue(), queue.Queue()
        self.cache = ImageCache(100)
        self.labels, self.cards, self.preset_meta = {}, {}, {}
        self.preview_frames = {}
        self.card_combos = {}
        self.preview_after = {}
        self.card_positions = {}
        self.card_density = None
        self.columns = 2
        self.busy = False
        self.popup = ThumbnailPopup(app.root)
        self.worker_started = False
        self.resize_after = self.viewport_after = None
        self.viewport_size = None
        self.last_layout = None
        self.build()

    def build(self):
        app = self.app
        self.frame = app.selection_frame
        header = app.card_frame(self.frame, fill="x", pady=(0, 10))
        title_row = tk.Frame(header, bg=app.CARD)
        title_row.pack(fill="x", pady=(0, 8))
        tk.Label(title_row, text="Choose your videos", bg=app.CARD, fg=app.TEXT,
                 font=("Segoe UI", 18, "bold")).pack(side="left")
        self.cancel_button = app.small_button(title_row, "Cancel", app.cancel_import_selection)
        self.cancel_button.pack(side="right")
        # Keep one toolbar row at every width; small screens can scroll it sideways.
        self.toolbar = tk.Canvas(header, height=37, bg=app.CARD, highlightthickness=0)
        self.toolbar.pack(fill="x")
        tools = tk.Frame(self.toolbar, bg=app.CARD)
        self.toolbar.create_window((0, 0), window=tools, anchor="nw")
        tools.bind("<Configure>", lambda e: self.toolbar.configure(
            scrollregion=self.toolbar.bbox("all"), height=tools.winfo_reqheight()+4))
        self.toolbar_scroll = ttk.Scrollbar(header, orient="horizontal", command=self.toolbar.xview)
        self.toolbar.configure(xscrollcommand=self.update_toolbar_scroll)
        self.toolbar.bind("<Configure>", lambda e: self.update_toolbar_scroll(*self.toolbar.xview()))
        self.toolbar.bind("<Shift-MouseWheel>", lambda e: (self.toolbar.xview_scroll(-1 if e.delta > 0 else 1, "units"), "break")[-1])
        self.add_button = app.small_button(tools, "Add files", app.add_files_dialog, app.BLUE)
        self.add_button.pack(side="left")
        self.scan_button = app.small_button(tools, "Scan folder", app.manual_scan)
        self.scan_button.pack(side="left", padx=5)
        self.all_button = app.small_button(tools, "Select all", lambda: self.select_all(True))
        self.all_button.pack(side="left", padx=(6, 5))
        self.none_button = app.small_button(tools, "Select none", lambda: self.select_all(False))
        self.none_button.pack(side="left")
        tk.Label(tools, text="Group preset", bg=app.CARD, fg=app.MUTED).pack(side="left", padx=(12, 6))
        self.group_preset = tk.StringVar(value=app.get_config().get("default_preset_name", "Current settings"))
        self.group_combo = RoundedCombobox(tools, textvariable=self.group_preset,
                                        values=app.preset_menu_values(), state="readonly", width=20)
        self.group_combo.pack(side="left")
        self.group_apply = app.small_button(tools, "Apply to selected", self.apply_group_preset)
        self.group_apply.pack(side="left", padx=6)
        self.feedback = tk.StringVar(value="")
        self.feedback_label = tk.Label(header, textvariable=self.feedback, bg=app.CARD, fg=app.MUTED, anchor="w")
        footer = app.card_frame(self.frame, side="bottom", fill="x", pady=(10, 0))
        self.status = tk.Label(footer, bg=app.CARD, fg=app.MUTED)
        self.status.pack(side="left")
        self.submit = app.button(footer, "Import selected (0)", self.confirm, app.GREEN)
        self.submit.pack(side="right")
        self.canvas, self.grid = app.make_canvas_area(self.frame)
        self.top_spacer = tk.Frame(self.grid, bg=app.BG)
        self.bottom_spacer = tk.Frame(self.grid, bg=app.BG)
        self.empty_label = tk.Label(self.grid, text="No videos waiting for selection", bg=app.BG, fg=app.MUTED, pady=30)
        self.canvas.bind("<Configure>", self.resize, add="+")
        self.canvas._view_changed_callback = self.schedule_viewport
        app.enable_dashboard_drop_targets(self.frame, header, self.canvas, self.grid)
        self.render()
        app.root.after(100, self.poll)

    def update_toolbar_scroll(self, first, last):
        self.toolbar_scroll.set(first, last)
        if float(last)-float(first) < .999:
            if not self.toolbar_scroll.winfo_manager():
                self.toolbar_scroll.pack(fill="x", pady=(3, 0))
        else:
            self.toolbar_scroll.pack_forget()

    def set_feedback(self, text):
        self.feedback.set(text)
        if text:
            self.feedback_label.pack(fill="x", pady=(5, 0))
        else:
            self.feedback_label.pack_forget()

    def add(self, payload):
        paths, context = payload
        name, _ = self.app.default_task_preset_snapshot()
        added = 0
        for path in paths:
            key = path_key(path)
            if key not in self.items:
                self.items[key] = (path, context, tk.BooleanVar(value=False), tk.StringVar(value=name))
                self.base_presets[key] = name
                self.tokens[key] = uuid.uuid4().hex
                added += 1
        self.render()
        if context.get("disk_batch") and added and self.app.get_config().get("import_mode") == "select":
            self.show()

    def show(self):
        if not self.items or self.app.get_config().get("import_mode") != "select":
            return
        self.app.select_view = True
        self.app.show_tab("dashboard")
        self.render(force=False)

    def cancel(self):
        if self.busy:
            return
        self.session_id = uuid.uuid4().hex
        with self.app.task_lock:
            self.app.selection_path_keys.difference_update(self.items)
        self.popup.close()
        for key in list(self.cards):
            self.remove_card(key)
        self.last_layout = None
        self.items.clear()
        self.tokens.clear()
        self.requested.clear()
        self.override_keys.clear()
        self.base_presets.clear()
        paths = {path for previews in self.previews.values() for path in previews}
        self.previews.clear()
        self.cache = ImageCache(100)
        self.app.cleanup_thumbnail_paths(paths)
        self.set_feedback("")
        self.app.show_processing_queue()
        self.render()

    def resize(self, event):
        self.columns = 3 if event.width >= 960 else 2
        size = (event.width, getattr(event, "height", self.canvas.winfo_height()))
        if size == self.viewport_size:
            return
        self.viewport_size = size
        if not self.is_active():
            return
        if self.resize_after:
            self.app.root.after_cancel(self.resize_after)
        self.resize_after = self.app.root.after(100, self.render)

    def is_active(self):
        return (self.app.current_tab == "dashboard" and self.app.select_view
                and self.app.get_config().get("import_mode") == "select")

    def schedule_viewport(self):
        if self.is_active() and self.resize_after is None and self.viewport_after is None:
            self.viewport_after = self.app.root.after_idle(self.render_viewport)

    def render_viewport(self):
        self.viewport_after = None
        if self.resize_after is None:
            self.render(force=False)

    def select_all(self, value):
        if not self.busy:
            for _, _, selected, _ in self.items.values():
                selected.set(value)
            self.update_count()

    def toggle_card(self, key):
        if not self.busy and key in self.items:
            selected = self.items[key][2]
            selected.set(not selected.get())
            self.update_count()

    def bind_card_selection(self, widget, key, excluded):
        if widget in excluded or isinstance(widget, (ttk.Combobox, tk.Checkbutton)):
            return
        widget.bind("<Button-1>", lambda e, k=key: self.toggle_card(k), add="+")
        for child in widget.winfo_children():
            self.bind_card_selection(child, key, excluded)

    def update_count(self):
        count = sum(item[2].get() for item in self.items.values())
        state = "normal" if count and not self.busy else "disabled"
        self.submit.configure(text=f"Import selected ({count})", state=state)
        self.group_apply.configure(state=state)
        self.group_combo.configure(state="disabled" if self.busy else "readonly")
        self.cancel_button.configure(state="disabled" if self.busy else "normal")
        self.all_button.configure(state="disabled" if self.busy else "normal")
        self.none_button.configure(state="disabled" if self.busy else "normal")
        self.status.configure(text="Preparing imports…" if self.busy else f"{count} selected · {len(self.items)} available")
        for key, card in self.cards.items():
            card.set_border(self.app.BLUE if self.items[key][2].get() else self.app.BORDER)

    def apply_group_preset(self):
        if self.busy:
            return
        changed = kept = 0
        for key, (_, _, selected, preset) in self.items.items():
            if selected.get():
                self.base_presets[key] = self.group_preset.get()
                if key in self.override_keys:
                    kept += 1
                else:
                    preset.set(self.group_preset.get())
                    changed += 1
        self.set_feedback(f"Applied to {changed} videos · Kept {kept} individual overrides.")
        self.update_count()

    def mark_override(self, key):
        self.override_keys.add(key)
        self.update_preset_note(key)

    def use_group(self, key):
        if not self.busy:
            self.override_keys.discard(key)
            self.items[key][3].set(self.base_presets[key])
            self.update_preset_note(key)

    def update_preset_note(self, key):
        if key not in self.preset_meta:
            return
        note, reset = self.preset_meta[key]
        overridden = key in self.override_keys
        note.configure(text="Individual preset" if overridden else "Follows group preset",
                       fg=self.app.BLUE if overridden else self.app.MUTED)
        if overridden:
            reset.pack(side="right")
        else:
            reset.pack_forget()

    def render(self, force=True):
        if self.resize_after:
            self.app.root.after_cancel(self.resize_after)
            self.resize_after = None
        if not self.is_active():
            return
        width = max(240, self.canvas.winfo_width())
        if width <= 240:
            width = max(600, self.app.content.winfo_width())
        preview_width = max(80, width//self.columns-36)
        preview_height = round(preview_width*9/16)
        compact = self.app.compact_dashboard
        extra = 142 if compact else 176
        self.row_extent = preview_height+max(extra, round(extra*self.frame.winfo_fpixels("1i")/96))
        keys = list(self.items)
        rows = math.ceil(len(keys)/self.columns)
        start, end = virtual_row_window(rows, self.canvas.canvasy(0), self.canvas.winfo_height(), self.row_extent, threshold=4, overscan=1)
        layout = (tuple(keys), start, end, self.columns, width, self.busy, compact)
        if not force and layout == self.last_layout:
            return
        self.last_layout = layout
        visible_keys = keys[start*self.columns:end*self.columns]
        density = (compact, self.busy)
        for key in list(self.cards):
            if key not in visible_keys or density != self.card_density:
                self.remove_card(key)
        self.card_density = density
        app = self.app
        presets = app.preset_menu_values()
        self.group_combo.configure(values=presets)
        for column in range(3):
            self.grid.columnconfigure(column, weight=1 if column < self.columns else 0,
                                      uniform="video" if column < self.columns else "", minsize=0)
        self.top_spacer.configure(height=max(1, start*self.row_extent))
        self.top_spacer.grid(row=0, column=0, columnspan=self.columns, sticky="ew")
        if not keys:
            self.empty_label.grid(row=1, column=0, columnspan=self.columns)
        else:
            self.empty_label.grid_remove()
        for index, key in enumerate(visible_keys):
            position = (index//self.columns+1, index%self.columns)
            if key in self.cards:
                surface = self.cards[key]
                combo = self.card_combos[key]
                if tuple(combo.cget("values")) != tuple(presets):
                    combo.configure(values=presets)
                if self.card_positions[key] != position:
                    surface.grid_configure(row=position[0], column=position[1])
                    self.card_positions[key] = position
                if int(surface.cget("height")) != self.row_extent-10:
                    surface.configure(height=self.row_extent-10)
                    self.preview_frames[key].configure(height=preview_height)
                continue
            path, context, selected, preset = self.items[key]
            surface = RoundedPanel(self.grid, app.CARD, app.BORDER, padding=2 if compact else 4, height=self.row_extent-10)
            surface.grid(row=index//self.columns+1, column=index%self.columns, sticky="nsew", padx=5, pady=5)
            surface.pack_propagate(False)
            self.cards[key] = surface
            self.card_positions[key] = position
            card = surface.inner
            preview = tk.Frame(card, bg=app.CARD2, height=preview_height)
            preview.pack(fill="x")
            preview.pack_propagate(False)
            self.preview_frames[key] = preview
            label = tk.Label(preview, text="Preparing preview…", bg=app.CARD2, fg=app.MUTED, cursor="hand2", bd=0, padx=0, pady=0)
            label.place(x=0, y=0, relwidth=1, relheight=1)
            self.labels[key] = label
            label.bind("<Configure>", lambda e, k=key: self.schedule_preview(k))
            label.bind("<Motion>", lambda e, k=key: self.display(k, scrub_index_from_event(e, len(self.previews.get(k, [])))))
            label.bind("<Leave>", lambda e, k=key: self.display(k))
            label.bind("<Button-1>", lambda e, k=key: self.fullscreen(k))
            check = tk.Checkbutton(card, text=path.name, variable=selected, command=self.update_count,
                                  bg=app.CARD, fg=app.TEXT, activebackground=app.CARD, activeforeground=app.TEXT,
                                  selectcolor=app.CARD2, anchor="w", font=("Segoe UI", 9 if compact else 10, "bold"),
                                  state="disabled" if self.busy else "normal")
            check.pack(fill="x", pady=(4 if compact else 8, 2))
            tk.Label(card, text=str(path.parent), bg=app.CARD, fg=app.MUTED, anchor="w", width=1).pack(fill="x")
            combo = RoundedCombobox(card, textvariable=preset, values=presets,
                                 state="disabled" if self.busy else "readonly", width=12,
                                 font=("Segoe UI", 9 if compact else 10), pady=1 if compact else 3)
            combo.pack(fill="x", pady=(6 if compact else 10, 2 if compact else 4))
            self.card_combos[key] = combo
            combo.bind("<<ComboboxSelected>>", lambda e, k=key: self.mark_override(k))
            meta = tk.Frame(card, bg=app.CARD, height=25 if compact else 29)
            meta.pack(fill="x")
            meta.pack_propagate(False)
            note = tk.Label(meta, bg=app.CARD, font=("Segoe UI", 9))
            note.pack(side="left")
            reset = app.small_button(meta, "Use group", lambda k=key: self.use_group(k))
            reset.configure(pady=2, state="disabled" if self.busy else "normal")
            self.preset_meta[key] = (note, reset)
            self.update_preset_note(key)
            self.bind_card_selection(surface, key, {preview})
            if key in self.previews:
                self.display(key)
            elif key not in self.requested:
                self.requested.add(key)
                self.jobs.put((key, path, self.tokens[key]))
                if not self.worker_started:
                    self.worker_started = True
                    threading.Thread(target=self.worker, daemon=True, name="ImportSelectionPreviews").start()
        remaining = max(0, rows-end)*self.row_extent
        self.bottom_spacer.configure(height=max(1, remaining))
        self.bottom_spacer.grid(row=end-start+1, column=0, columnspan=self.columns, sticky="ew")
        self.update_count()

    def remove_card(self, key):
        pending = self.preview_after.pop(key, None)
        if pending is not None:
            self.app.root.after_cancel(pending)
        self.cards.pop(key).destroy()
        self.labels.pop(key, None)
        self.preset_meta.pop(key, None)
        self.preview_frames.pop(key, None)
        self.card_combos.pop(key, None)
        self.card_positions.pop(key, None)

    def schedule_preview(self, key):
        if not self.is_active():
            return
        pending = self.preview_after.pop(key, None)
        if pending is not None:
            self.app.root.after_cancel(pending)
        self.preview_after[key] = self.app.root.after(60, lambda: self.refresh_preview(key))

    def refresh_preview(self, key):
        self.preview_after.pop(key, None)
        self.display(key)

    def fullscreen(self, key):
        self.popup.show(self.previews.get(key, []))
        if self.popup.popup:
            self.popup.popup.overrideredirect(False)
            self.popup.popup.attributes("-fullscreen", True)

    def display(self, key, index=None):
        if not self.is_active():
            return
        label = self.labels.get(key)
        paths = self.previews.get(key, [])
        if label is None:
            return
        if not paths:
            label.configure(text="Preview unavailable")
            return
        path = paths[len(paths)//2 if index is None else min(index, len(paths)-1)]
        self.cache.configure_cover(label, path, label.winfo_width(), label.winfo_height())

    def worker(self):
        while not self.app.shutdown_event.is_set():
            try:
                key, path, token = self.jobs.get(timeout=.5)
            except queue.Empty:
                continue
            paths = []
            try:
                ffmpeg = self.app.resolve_ffmpeg_executable(self.app.get_config().get("ffmpeg_path", ""))
                if self.tokens.get(key) == token and ffmpeg and path.is_file():
                    _, paths = extract_video_thumbnails(ffmpeg, path, "select_" + token,
                                                       self.app.get_duration(path, ffmpeg), no_window_flags,
                                                       lambda: self.app.shutdown_event.is_set() or self.tokens.get(key) != token)
            except Exception:
                pass
            self.results.put((key, token, paths))
            self.jobs.task_done()

    def poll(self):
        for _ in range(12):
            try:
                key, token, paths = self.results.get_nowait()
            except queue.Empty:
                break
            if self.tokens.get(key) != token:
                self.app.cleanup_thumbnail_paths(paths)
                continue
            self.previews[key] = paths
            self.display(key)
        if self.frame.winfo_exists():
            self.app.root.after(100, self.poll)

    def confirm(self):
        if self.busy:
            return
        selected, preset_args = [], {}
        for key, (path, context, checked, preset) in self.items.items():
            if not checked.get():
                continue
            name = preset.get()
            try:
                if name not in preset_args:
                    preset_args[name] = list(self.app.get_config()["ffmpeg_args"]) if name == "Current settings" else self.app.read_preset_args(name)
                args = preset_args[name]
            except (OSError, UnicodeError):
                args = None
            if not args:
                self.set_feedback(f"Preset unavailable: {name}. Choose another preset before importing.")
                return
            selected.append((key, path, context, name, list(args)))
        if selected:
            self.busy = True
            self.app.import_cancel_requested.clear()
            self.render()
            threading.Thread(target=self.app.import_selected_candidates, args=(selected,), daemon=True, name="SelectedImports").start()

    def finished(self, keys):
        for key in keys:
            self.items.pop(key, None)
            self.tokens.pop(key, None)
            self.base_presets.pop(key, None)
            self.override_keys.discard(key)
            self.requested.discard(key)
        self.busy = False
        self.render()
        # Return to normal processing once every checked candidate was accepted.
        if keys and not any(item[2].get() for item in self.items.values()):
            self.cancel()
