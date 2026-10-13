"""Small guided preset builder, also used for one-video overrides."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from conversion_options import (ENCODERS, FRAME_RATES, KEEP, RESOLUTIONS, CONTAINERS, AUDIO_CODECS,
                                apply_video_options, apply_media_options, estimated_output_bytes, new_preset_options)
from presets import arg_value, analyze_preset_args
from file_utils import format_size
from thumbnail_ui import scale_photo
from ui_widgets import RoundedEntry, RoundedCombobox


class ConversionEditor(tk.Toplevel):
    def __init__(self, app, base_args, on_save, new_preset=False, filename="", task=None):
        super().__init__(app.root)
        self.app, self.base_args, self.on_save = app, list(base_args), on_save
        self.new_preset, self.step = new_preset, 0
        self.task = task
        self.title("Make new preset" if new_preset else "Change conversion settings")
        self.transient(app.root)
        self.configure(bg=app.CARD)
        self.geometry(f"{min(680, self.winfo_screenwidth() - 60)}x{min(650, self.winfo_screenheight() - 100)}")
        self.name = tk.StringVar(self, value="")
        self.resolution = tk.StringVar(self, value="1080p (1920 × 1080)" if new_preset else KEEP)
        self.fps = tk.StringVar(self, value=FRAME_RATES[0] if new_preset else KEEP)
        self.bitrate = tk.StringVar(self, value="20" if new_preset else KEEP)
        self.container = tk.StringVar(self, value=KEEP)
        self.audio = tk.StringVar(self, value=KEEP)
        self.audio_kbps = tk.StringVar(self, value=KEEP)
        self.estimate = tk.StringVar(self, value="Estimated size: waiting for video duration")
        self.advice = tk.StringVar(self)
        codec = arg_value(self.base_args, "-c:v", "-vcodec")
        encoder = next((label for label, value in ENCODERS.items() if value == codec), "H.264 · CPU")
        self.current_encoder_name = next((label for label, value in ENCODERS.items() if value == codec), codec or "Current preset")
        self.encoder = tk.StringVar(self, value=encoder)
        footer = tk.Frame(self, bg=app.CARD, padx=18, pady=12)
        footer.pack(side="bottom", fill="x")
        self.error = tk.StringVar(self)
        tk.Label(footer, textvariable=self.error, bg=app.CARD, fg="#ffadb7", wraplength=610, justify="left", anchor="w").pack(fill="x", pady=(0, 8))
        buttons = tk.Frame(footer, bg=app.CARD)
        buttons.pack(fill="x")
        app.small_button(buttons, "Cancel", self.destroy, app.CARD3).pack(side="left")
        self.next_button = app.small_button(buttons, "Next", self.next_step, app.BLUE)
        self.next_button.pack(side="right")
        self.back_button = app.small_button(buttons, "Back", self.previous_step, app.CARD3)
        self.back_button.pack(side="right", padx=8)
        header = tk.Frame(self, bg=app.CARD, padx=18, pady=14)
        header.pack(fill="x")
        self.heading = tk.StringVar(self)
        tk.Label(header, textvariable=self.heading, bg=app.CARD, fg=app.TEXT, font=("Segoe UI", 16, "bold"), anchor="w").pack(fill="x")
        subtitle = "A reusable MP4 preset, in three steps." if new_preset else f"Only this video: {filename}"
        tk.Label(header, text=subtitle, bg=app.CARD, fg=app.MUTED, font=("Segoe UI", 10), wraplength=610, anchor="w", justify="left").pack(fill="x", pady=(6, 0))
        # Scrollable body keeps Save/Cancel available on smaller screens.
        canvas = tk.Canvas(self, bg=app.CARD, highlightthickness=0)
        scroll = ttk.Scrollbar(self, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True, padx=(18, 0))
        self.body = tk.Frame(canvas, bg=app.CARD, padx=0, pady=4)
        item = canvas.create_window((0, 0), window=self.body, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        self.body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        self.canvas = canvas
        self.bind("<MouseWheel>", self.scroll_body)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.draw_step()
        for variable in (self.resolution, self.fps, self.bitrate, self.container, self.audio, self.audio_kbps):
            variable.trace_add("write", lambda *_: self.update_estimate())
        self.update_estimate()

    def scroll_body(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120) * 3, "units")
        return "break"

    def field(self, title, variable, explanation, options=None, editable=False):
        app = self.app
        group = tk.Frame(self.body, bg=app.CARD, pady=8)
        group.pack(fill="x", padx=(0, 18))
        tk.Label(group, text=title, bg=app.CARD, fg=app.TEXT, font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x")
        tk.Label(group, text=explanation, bg=app.CARD, fg=app.MUTED, font=("Segoe UI", 10), anchor="w", wraplength=580, justify="left").pack(fill="x", pady=(3, 6))
        if options is None:
            widget = RoundedEntry(group, textvariable=variable, font=("Segoe UI", 11))
        else:
            widget = RoundedCombobox(group, textvariable=variable, values=list(options), state="normal" if editable else "readonly", font=("Segoe UI", 11))
        widget.pack(fill="x", ipady=3)
        return widget

    def draw_step(self):
        for child in self.body.winfo_children():
            child.destroy()
        self.error.set("")
        app = self.app
        if not self.new_preset:
            self.heading.set("One-video settings")
            self.back_button.pack_forget()
            self.next_button.configure(text="Apply to this video")
            tk.Label(self.body, text="Changing settings restarts this video's conversion from the beginning if it has already started. Existing finished files are kept.", bg=app.CARD, fg="#ffd36a", wraplength=580, justify="left", anchor="w").pack(fill="x", pady=8)
            if self.task:
                previews = list(dict.fromkeys([*(self.task.thumbnail_preview_paths or []), self.task.thumbnail_path]))
                strip = tk.Frame(self.body, bg=app.CARD)
                strip.pack(fill="x")
                self.preview_images = []
                for path in [p for p in previews if p][:3]:
                    try:
                        photo = scale_photo(tk.PhotoImage(file=path, master=self), 175, 100)
                        self.preview_images.append(photo)
                        tk.Label(strip, image=photo, bg=app.CARD).pack(side="left", padx=(0, 8))
                    except (OSError, tk.TclError):
                        pass
                if not self.preview_images:
                    tk.Label(strip, text="Preview not available yet", bg=app.CARD, fg=app.MUTED).pack(anchor="w")
            self.field("Resolution", self.resolution, "Fit without cropping; bars are added if needed.", [KEEP, *RESOLUTIONS])
            self.field("Frame rate", self.fps, "Frames per second. Keep current leaves the preset unchanged.", [KEEP, *FRAME_RATES])
            self.field("Target bitrate (Mbps)", self.bitrate, "1–200 Mbps. Higher values mean larger files, not extra detail from the original.", [KEEP, "8", "12", "20", "30", "50", "100", "150", "200"], True)
            self.field("File format", self.container, "MP4 is widely compatible. MOV and MKV are also available.", [KEEP, *CONTAINERS])
            self.field("Audio format", self.audio, "AAC is recommended. MP3 uses stereo audio.", [KEEP, *AUDIO_CODECS])
            self.field("Audio bitrate (kbps)", self.audio_kbps, "192 kbps is a useful starting point; higher values use more space.", [KEEP, "64", "96", "128", "160", "192", "224", "256", "320"])
            tk.Label(self.body, textvariable=self.estimate, bg=app.CARD, fg=app.TEXT, anchor="w", font=("Segoe UI", 12, "bold")).pack(fill="x", pady=8)
            tk.Label(self.body, textvariable=self.advice, bg=app.CARD, fg="#ffd36a", wraplength=580, justify="left", anchor="w").pack(fill="x")
            tk.Label(self.body, text=f"Video encoder: {self.current_encoder_name}", bg=app.CARD, fg=app.MUTED, anchor="w").pack(fill="x", pady=8)
        elif self.step == 0:
            self.heading.set("1 of 3 · Picture")
            self.back_button.configure(state="disabled")
            self.next_button.configure(text="Next")
            self.field("Resolution", self.resolution, "Fit without cropping. Choosing 4K cannot add detail to a smaller original.", RESOLUTIONS)
            self.field("Frame rate", self.fps, "Use Original frame rate unless you need a specific output rate.", FRAME_RATES)
        elif self.step == 1:
            self.heading.set("2 of 3 · Encoding")
            self.back_button.configure(state="normal")
            self.next_button.configure(text="Review")
            self.field("Encoder", self.encoder, "H.264 is widely compatible. NVIDIA is faster, but requires a compatible GPU.", ENCODERS)
            self.field("Target bitrate (Mbps)", self.bitrate, "1–200 Mbps. This is a target, not an exact constant rate.", ["8", "12", "20", "30", "50", "100", "150", "200"], True)
        else:
            self.heading.set("3 of 3 · Review and save")
            self.back_button.configure(state="normal")
            self.next_button.configure(text="Save preset")
            summary = (f"{self.resolution.get()}\n{self.fps.get()} fps" if self.fps.get() != FRAME_RATES[0] else f"{self.resolution.get()}\n{FRAME_RATES[0]}")
            summary += f"\n{self.bitrate.get()} Mbps target · {self.encoder.get()}\nMP4 · first audio track as stereo AAC"
            tk.Label(self.body, text=summary, bg=app.CARD2, fg=app.TEXT, font=("Segoe UI", 11), padx=12, pady=12, anchor="w", justify="left", wraplength=580).pack(fill="x", padx=(0, 18))
            self.field("Preset name", self.name, "Saved in your preset folder. Existing presets are never replaced.")
        self.canvas.yview_moveto(0)

    def arguments(self):
        if self.new_preset:
            return new_preset_options(self.encoder.get(), self.resolution.get(), self.fps.get(), self.bitrate.get())
        return apply_media_options(apply_video_options(self.base_args, self.resolution.get(), self.fps.get(), self.bitrate.get()), self.container.get(), self.audio.get(), self.audio_kbps.get())

    def update_estimate(self):
        if self.new_preset:
            return
        try:
            args = self.arguments()
            size = estimated_output_bytes(args, getattr(self.task, "source_duration_seconds", None))
            self.estimate.set(f"Estimated size: ~{format_size(size)}" if size else "Estimated size: needs a known duration and target bitrates")
            warnings = analyze_preset_args(args)
            if self.resolution.get().startswith("4K") or self.fps.get() != KEEP:
                warnings.append("A larger picture or higher frame rate cannot restore detail or motion missing from the original.")
            self.advice.set("\n".join(warnings) + "\nSize is approximate; actual bitrate and audio tracks can change it.")
        except ValueError as exc:
            self.estimate.set("Estimated size: —")
            self.advice.set(str(exc))

    def next_step(self):
        try:
            if self.new_preset and self.step < 2:
                if self.step == 1:
                    self.arguments()
                self.step += 1
                self.draw_step()
                return
            args = self.arguments()
            self.on_save(self.name.get(), args)
        except Exception as exc:
            self.error.set(str(exc))
            return
        self.destroy()

    def previous_step(self):
        self.step = max(0, self.step - 1)
        self.draw_step()
