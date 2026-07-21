from __future__ import annotations

from typing import Callable, Dict, List, Optional

import tkinter as tk
from tkinter import ttk


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
        self.combo = ttk.Combobox(row, values=self.options, state="readonly", width=14)
        self.combo.pack(side="left", fill="x", expand=True)
        tk.Button(row, text="Add", command=self.add_selected, bg=colors["blue"], fg="white", relief="flat", padx=10, pady=5).pack(side="left", padx=(8, 0))
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
            tk.Button(chip, text="x", command=lambda v=value: self.remove(v), bg=self.colors["card3"], fg=self.colors["text"], relief="flat", padx=6).pack(side="left")

    def get(self) -> List[str]:
        return list(self.values)
