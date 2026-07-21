from __future__ import annotations

from collections import deque
from threading import Condition, Event
from typing import Callable, Optional, Set, Tuple


def wait_for_conversion_task(
    condition: Condition,
    conversion_queue: deque[str],
    queued_ids: Set[str],
    force_ids: Set[str],
    conversion_enabled: Callable[[], bool],
    pause_event: Event,
    shutdown_event: Event,
) -> Optional[Tuple[str, bool]]:
    with condition:
        while (
            not conversion_queue
            or (not conversion_enabled() and not any(task_id in force_ids for task_id in conversion_queue))
            or pause_event.is_set()
        ) and not shutdown_event.is_set():
            condition.wait(timeout=0.5)
        if shutdown_event.is_set():
            return None
        task_id = conversion_queue.popleft()
        queued_ids.discard(task_id)
        force = task_id in force_ids
        if not conversion_enabled() and not force:
            conversion_queue.append(task_id)
            queued_ids.add(task_id)
            condition.wait(timeout=0.5)
            return None
        return task_id, force


def wait_for_transfer_task(
    condition: Condition,
    transfer_queue: deque[str],
    queued_ids: Set[str],
    transfer_enabled: Callable[[], bool],
    pause_event: Event,
    shutdown_event: Event,
) -> Optional[str]:
    with condition:
        while (not transfer_queue or not transfer_enabled() or pause_event.is_set()) and not shutdown_event.is_set():
            condition.wait(timeout=0.5)
        if shutdown_event.is_set():
            return None
        task_id = transfer_queue.popleft()
        queued_ids.discard(task_id)
        return task_id
