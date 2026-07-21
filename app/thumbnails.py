from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from storage import thumbnail_dir


def extract_video_thumbnails(
    ffmpeg: Path,
    src: Path,
    task_id: str,
    duration: Optional[float],
    no_window_flags: Callable[[], int],
) -> Tuple[Optional[Path], List[str]]:
    thumb = thumbnail_dir() / f"{task_id}.png"
    preview_paths: List[str] = []
    vf = "scale=444:250:force_original_aspect_ratio=increase,crop=444:250"

    def make_frame(offset: float, destination: Path) -> bool:
        cmd = [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, offset):.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            vf,
            str(destination),
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, creationflags=no_window_flags())
            return destination.exists() and destination.stat().st_size > 0
        except Exception:
            return False

    offsets = [1.0]
    if duration and duration > 2:
        offsets = [max(0.5, duration * (i / 11)) for i in range(1, 11)]
    main_offset = offsets[len(offsets) // 2] if offsets else 1.0
    if not make_frame(main_offset, thumb):
        fallback = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vf", f"thumbnail,{vf}", "-frames:v", "1", str(thumb)]
        try:
            subprocess.run(fallback, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, creationflags=no_window_flags())
        except Exception:
            return None, []
    for index, offset in enumerate(offsets[:10], start=1):
        preview = thumbnail_dir() / f"{task_id}_preview_{index}.png"
        if make_frame(offset, preview):
            preview_paths.append(str(preview))
    if thumb.exists() and thumb.stat().st_size > 0:
        if str(thumb) not in preview_paths:
            preview_paths.insert(min(len(preview_paths) // 2, len(preview_paths)), str(thumb))
        return thumb, preview_paths[:10]
    return None, []
