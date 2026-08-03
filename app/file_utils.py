from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple


def path_key(path: Path | str) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except Exception:
        return str(Path(path)).lower()


def path_is_within(path: Path | str, folder: Path | str) -> bool:
    try:
        candidate = Path(path).resolve()
        root = Path(folder).resolve()
        return candidate == root or root in candidate.parents
    except (OSError, RuntimeError, ValueError):
        return False


def media_folder_overlap_errors(input_folder: Path | str, output_folder: Path | str, nas_folder: Path | str = "") -> List[str]:
    folders = [
        ("Importfolder", str(input_folder).strip()),
        ("Output folder", str(output_folder).strip()),
        ("NAS folder", str(nas_folder).strip()),
    ]
    errors: List[str] = []
    for index, (left_label, left_value) in enumerate(folders):
        if not left_value:
            continue
        for right_label, right_value in folders[index + 1:]:
            if not right_value:
                continue
            if path_is_within(left_value, right_value) or path_is_within(right_value, left_value):
                errors.append(f"{left_label} and {right_label} must be separate and must not contain one another.")
    return errors


def parse_csv_list(value: str) -> List[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def normalize_extensions(items: Iterable[str]) -> set[str]:
    out = set()
    for item in items:
        item = str(item).strip().lower()
        if not item:
            continue
        if item.startswith("*."):
            item = item[1:]
        if not item.startswith("."):
            item = "." + item
        out.add(item)
    return out


def is_video_file(path: Path | str, extensions: Iterable[str]) -> bool:
    return Path(path).suffix.lower() in normalize_extensions(extensions)


def should_exclude_file(filename: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(filename, pattern) for pattern in patterns)


def ensure_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def validate_writable_folder(path: Path | str, label: str, required: bool = True) -> Optional[str]:
    raw_path = str(path).strip()
    if not raw_path:
        return f"{label} is required." if required else None
    try:
        path = Path(raw_path)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".mestersync_write_test_{uuid.uuid4().hex}.tmp"
        with probe.open("xb") as fh:
            fh.write(b"ok")
            fh.flush()
            os.fsync(fh.fileno())
        force_delete(probe)
        return None
    except PermissionError:
        return f"{label} is not writable: {path}"
    except OSError as exc:
        return f"{label} cannot be used: {path} ({exc})"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    parent, stem, suffix = path.parent, path.stem, path.suffix
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def format_size(byte_count: int | float) -> str:
    value = float(byte_count)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024.0:
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} PB"


def format_duration(seconds: Optional[float | int]) -> str:
    if seconds is None:
        return "-"
    seconds = int(max(0, seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def pretty_time(value: Optional[str]) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value


def force_delete(path: Path, attempts: int = 12, delay: float = 0.75) -> bool:
    for attempt in range(attempts):
        try:
            if path.exists():
                os.chmod(path, 0o777)
                path.unlink()
            return True
        except FileNotFoundError:
            return True
        except Exception:
            if attempt == attempts - 1:
                return False
            time.sleep(delay)
    return False


def file_is_ready(path: Path, stable_seconds: int = 10, check_interval: float = 1.0) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        previous = path.stat().st_size
        end = time.time() + max(1, stable_seconds)
        while time.time() < end:
            time.sleep(check_interval)
            current = path.stat().st_size
            if current != previous:
                return False
            previous = current
        return True
    except Exception:
        return False


def no_window_flags() -> int:
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return int(subprocess.CREATE_NO_WINDOW)
    return 0


def open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def sanitize_base_name(value: str, auto_underscores: bool) -> str:
    base = (value or "").strip().strip('"').strip()
    if auto_underscores:
        base = re.sub(r"\s+", "_", base)
    invalid = '<>:"/\\|?*'
    base = "".join("_" if ch in invalid else ch for ch in base).rstrip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if base.split(".", 1)[0].upper() in reserved:
        base = f"_{base}"
    return base


def free_space_bytes(path: Path) -> Optional[int]:
    try:
        target = path if path.exists() else path.parent
        while not target.exists() and target.parent != target:
            target = target.parent
        return shutil.disk_usage(target).free
    except Exception:
        return None


def sha256_file(
    path: Path,
    stop_event: Optional[threading.Event] = None,
    progress_callback: Optional[Callable[[int, int, int], None]] = None,
) -> Optional[str]:
    try:
        h = hashlib.sha256()
        total = path.stat().st_size
        processed = 0
        last_percent = -1
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                if stop_event and stop_event.is_set():
                    return None
                h.update(chunk)
                processed += len(chunk)
                percent = int(processed / total * 100) if total else 100
                if progress_callback and percent != last_percent:
                    last_percent = percent
                    progress_callback(percent, processed, total)
        if progress_callback:
            progress_callback(100, processed, total)
        return h.hexdigest()
    except Exception:
        return None


def list_windows_drives() -> List[str]:
    if os.name != "nt":
        return []
    drives = []
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bitmask & (1 << i):
                drives.append(f"{chr(ord('A') + i)}:")
    except Exception:
        pass
    return drives


def get_drive_type(root: str) -> int:
    if os.name != "nt":
        return 0
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
    except Exception:
        return 0


def get_volume_serial(root: str) -> str:
    if os.name != "nt":
        return root
    try:
        serial = ctypes.c_uint32()
        max_component = ctypes.c_uint32()
        flags = ctypes.c_uint32()
        fs_name = ctypes.create_unicode_buffer(261)
        volume_name = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), volume_name, ctypes.sizeof(volume_name), ctypes.byref(serial),
            ctypes.byref(max_component), ctypes.byref(flags), fs_name, ctypes.sizeof(fs_name)
        )
        if ok:
            return f"{root}:{serial.value}"
    except Exception:
        pass
    return root


def list_candidate_drives(ignored_drives: Iterable[str]) -> List[Tuple[str, str]]:
    if os.name != "nt":
        return []
    ignored = {d.strip().upper().rstrip("\\/") for d in ignored_drives}
    result = []
    for drive in list_windows_drives():
        root = f"{drive}\\"
        if drive.upper() in ignored:
            continue
        dtype = get_drive_type(root)
        if dtype in (2, 3):
            result.append((root, get_volume_serial(root)))
    return result

