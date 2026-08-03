from __future__ import annotations

import math
from typing import Tuple


def virtual_row_window(
    total: int,
    scroll_top_px: float,
    viewport_px: float,
    row_extent_px: int,
    threshold: int = 40,
    overscan: int = 5,
) -> Tuple[int, int]:
    total = max(0, int(total))
    if total <= threshold:
        return 0, total
    extent = max(1, int(row_extent_px))
    start = max(0, int(float(scroll_top_px) // extent) - max(0, int(overscan)))
    visible = max(1, int(math.ceil(max(1.0, float(viewport_px)) / extent)))
    end = min(total, start + visible + max(0, int(overscan)) * 2)
    if end == total:
        start = max(0, min(start, end - visible - max(0, int(overscan))))
    return start, end


def inertial_scroll_step(velocity: float, damping: float = 0.78) -> Tuple[int, float]:
    distance = int(round(float(velocity) * 0.28))
    next_velocity = float(velocity) * float(damping)
    if abs(next_velocity) < 0.5:
        next_velocity = 0.0
    return distance, next_velocity


def bounded_log_count(current: int, added: int, limit: int) -> Tuple[int, int]:
    """Return the number of old lines to trim and the resulting line count."""
    current = max(0, int(current))
    added = max(0, int(added))
    limit = max(1, int(limit))
    total = current + added
    trimmed = max(0, total - limit)
    return trimmed, min(total, limit)


def compact_notification_text(text: object, max_chars: int = 520, max_lines: int = 5) -> str:
    lines = str(text or "").strip().splitlines()
    line_limit = max(1, int(max_lines))
    clipped = "\n".join(lines[:line_limit])
    char_limit = max(20, int(max_chars))
    truncated = len(lines) > line_limit or len(clipped) > char_limit
    if len(clipped) > char_limit:
        clipped = clipped[: max(1, char_limit - 1)].rstrip()
    return clipped + ("…" if truncated else "")
