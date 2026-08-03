from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Tuple

from file_utils import is_video_file, should_exclude_file


StopCheck = Callable[[], bool]
ScanProgress = Callable[[int, int], None]


def collect_import_candidates(paths: Iterable[Path], extensions: Iterable[str], should_stop: StopCheck) -> List[Path]:
    candidates: List[Path] = []
    for path in paths:
        if should_stop():
            break
        if path.is_dir():
            for sub in path.rglob("*"):
                if should_stop():
                    break
                if sub.is_file() and is_video_file(sub, extensions):
                    candidates.append(sub)
        elif path.is_file() and is_video_file(path, extensions):
            candidates.append(path)
    return candidates


def scan_watchfolder_candidates(input_folder: Path, extensions: Iterable[str], excluded_folders: Iterable[Path] = ()) -> List[Path]:
    candidates: List[Path] = []
    if not input_folder.exists():
        return candidates
    excluded = [Path(folder).resolve() for folder in excluded_folders if str(folder).strip()]
    for path in input_folder.rglob("*"):
        if excluded:
            try:
                resolved = path.resolve()
                if any(resolved == folder or folder in resolved.parents for folder in excluded):
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
        if not path.is_file() or path.suffix.lower() == ".part" or not is_video_file(path, extensions):
            continue
        candidates.append(path)
    return candidates


def scan_drive_importable_videos(
    root: Path,
    cfg: Dict[str, object],
    should_stop: StopCheck,
    on_progress: ScanProgress,
) -> Tuple[List[Path], int]:
    found: List[Path] = []
    exclude_folders = set(str(x).lower() for x in cfg.get("exclude_folders", []))
    scanned = 0
    last_scan_update = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in exclude_folders]
        for filename in filenames:
            if should_stop():
                return found, scanned
            scanned += 1
            now = time.time()
            if now - last_scan_update >= 1.0:
                last_scan_update = now
                on_progress(scanned, len(found))
            if should_exclude_file(filename, cfg.get("exclude_files", [])):
                continue
            path = Path(dirpath) / filename
            if is_video_file(path, cfg.get("import_extensions", [])):
                found.append(path)
    return found, scanned
