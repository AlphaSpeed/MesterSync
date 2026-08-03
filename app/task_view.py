from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from tasks import TaskState


@dataclass(frozen=True)
class TaskViewSnapshot:
    task_id: str
    display_name: str
    rename_base: str
    output_ext: str
    stage: str
    progress: int
    detail: str
    imported_at: Optional[str]
    converted_at: Optional[str]
    transferred_at: Optional[str]
    eta_seconds: Optional[float]
    skipped: bool
    skip_archive_due: Optional[float]
    preset_name: str
    ffmpeg_args: Tuple[str, ...]
    conversion_started: bool
    thumbnail_path: Optional[str]
    thumbnail_preview_paths: Tuple[str, ...]
    local_input_path: Optional[Path]
    output_path: Optional[Path]
    priority: int
    selected: bool

    @classmethod
    def from_task(cls, task: TaskState, priority: int, selected: bool) -> "TaskViewSnapshot":
        return cls(
            task_id=task.task_id,
            display_name=task.display_name,
            rename_base=task.rename_base,
            output_ext=task.output_ext,
            stage=task.stage,
            progress=task.progress,
            detail=task.detail,
            imported_at=task.imported_at,
            converted_at=task.converted_at,
            transferred_at=task.transferred_at,
            eta_seconds=task.eta_seconds,
            skipped=task.skipped,
            skip_archive_due=task.skip_archive_due,
            preset_name=task.preset_name,
            ffmpeg_args=tuple(task.ffmpeg_args or ()),
            conversion_started=task.conversion_started,
            thumbnail_path=task.thumbnail_path,
            thumbnail_preview_paths=tuple(task.thumbnail_preview_paths or ()),
            local_input_path=task.local_input_path,
            output_path=task.output_path,
            priority=max(1, int(priority)),
            selected=bool(selected),
        )
