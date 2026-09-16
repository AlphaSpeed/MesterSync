"""Approximate encoding time; never promises an end-to-end transfer deadline."""
import math

from file_utils import format_duration
from tasks import (STAGE_CHECKING, STAGE_DETECTED, STAGE_IMPORTING, STAGE_IMPORTED,
                   STAGE_QUEUED, STAGE_WAITING, STAGE_CONVERTING, STAGE_CONVERTING_IN_PLACE)


def positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError):
        return None


def queue_estimate(tasks, current_id, rates, default_args, enabled=True, paused=False):
    eligible = [task for task in tasks if not task.skipped and not (task.duplicate and not task.force_import)
                and task.stage in {STAGE_CHECKING, STAGE_DETECTED, STAGE_IMPORTING, STAGE_IMPORTED,
                                   STAGE_QUEUED, STAGE_WAITING, STAGE_CONVERTING, STAGE_CONVERTING_IN_PLACE}]
    if not eligible:
        return "Queue: no conversions waiting"
    if paused:
        return f"Queue paused · {len(eligible)} videos"
    stopped = 0
    if not enabled:
        forced = [task for task in eligible if task.convert_in_place or task.task_id == current_id]
        stopped = len(eligible) - len(forced)
        if not forced:
            return f"Queue stopped · {len(eligible)} videos"
        eligible = forced
    known, unknown = 0.0, 0
    for task in eligible:
        if task.task_id == current_id:
            remaining = positive(task.eta_seconds)
            if remaining is not None:
                known += remaining
            elif task.progress >= 100:
                pass
            else:
                unknown += 1
            continue
        duration = positive(task.source_duration_seconds)
        rate = positive(rates.get(tuple(task.ffmpeg_args or default_args)))
        if duration and rate:
            known += duration / rate
        else:
            unknown += 1
    if unknown:
        text = (f"Queue: ~{format_duration(known)} + {unknown} estimating" if known else
                f"Queue: estimating {unknown} videos…")
    else:
        text = f"Queue: ~{format_duration(known)} encoding"
    return text + (f" · {stopped} stopped" if stopped else "")
