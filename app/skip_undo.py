from __future__ import annotations

import math
import time
from typing import Optional


SKIP_UNDO_SECONDS = 60


def new_skip_deadline(now: Optional[float] = None) -> float:
    return (time.time() if now is None else float(now)) + SKIP_UNDO_SECONDS


def skip_seconds_remaining(deadline: Optional[float], now: Optional[float] = None) -> int:
    if deadline is None:
        return 0
    current = time.time() if now is None else float(now)
    return max(0, int(math.ceil(float(deadline) - current)))


def skip_is_due(deadline: Optional[float], now: Optional[float] = None) -> bool:
    return bool(deadline is not None and skip_seconds_remaining(deadline, now) == 0)


def skip_undo_button_text(deadline: Optional[float], now: Optional[float] = None) -> str:
    remaining = skip_seconds_remaining(deadline, now)
    return f"Undo skip ({remaining}s)" if remaining else "Undo skip"


def skip_undo_detail(deadline: Optional[float], now: Optional[float] = None) -> str:
    remaining = skip_seconds_remaining(deadline, now)
    if remaining:
        return f"Skipped - moving to History in {remaining}s. Undo keeps it in the queue."
    return "Skipped - moving to History..."
