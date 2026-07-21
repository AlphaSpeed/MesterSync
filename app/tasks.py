from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional


STAGE_CHECKING = "Checking"
STAGE_IMPORTING = "Importing"
STAGE_QUEUED = "Queued"
STAGE_WAITING = "Waiting"
STAGE_CONVERTING = "Converting"
STAGE_CONVERTING_IN_PLACE = "Converting in place"
STAGE_WAITING_TRANSFER = "Waiting transfer"
STAGE_TRANSFERRING = "Transferring"
STAGE_IMPORTED = "Imported"
STAGE_CONVERTED = "Converted"
STAGE_CONVERTED_IN_PLACE = "Converted in place"
STAGE_CONVERTED_OUTPUT = "Converted output"
STAGE_COMPLETE = "Complete"
STAGE_STOPPED = "Stopped"
STAGE_STOPPING = "Stopping"
STAGE_ERROR = "Error"
STAGE_SKIPPED = "Skipped"
STAGE_DUPLICATE_REVIEW = "Duplicate review"
STAGE_DETECTED = "Detected"


@dataclass
class TaskState:
    task_id: str
    original_path: Path
    display_name: str
    local_input_path: Optional[Path] = None
    output_path: Optional[Path] = None
    nas_path: Optional[Path] = None
    rename_base: str = ""
    output_ext: str = ".mp4"
    stage: str = STAGE_DETECTED
    progress: int = 0
    detail: str = ""
    imported_at: Optional[str] = None
    converted_at: Optional[str] = None
    transferred_at: Optional[str] = None
    eta_seconds: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    thumbnail_path: Optional[str] = None
    thumbnail_preview_paths: List[str] = field(default_factory=list)
    imported_size: Optional[int] = None
    original_checksum: str = ""
    output_size: Optional[int] = None
    preset_name: str = "Current settings"
    ffmpeg_args: Optional[List[str]] = None
    conversion_started: bool = False
    space_savings: str = ""
    skipped: bool = False
    skip_archive_due: Optional[float] = None
    duplicate: bool = False
    duplicate_reason: str = ""
    error_log: str = ""
    convert_in_place: bool = False
    import_stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    transfer_stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    copy_thread_active: bool = False

    def final_name(self) -> str:
        base = self.rename_base or Path(self.display_name).stem
        return f"{base}{self.output_ext}"

    def source_for_conversion(self) -> Optional[Path]:
        return self.local_input_path or self.original_path

    def total_duration_seconds(self) -> Optional[float]:
        start = self.imported_at
        end = self.transferred_at or self.converted_at
        if start and end:
            try:
                return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
            except Exception:
                return None
        return None
