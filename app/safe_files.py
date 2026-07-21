from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from file_utils import force_delete, format_size, free_space_bytes, path_key


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, int], None]


def promote_temp_no_overwrite(temp: Path, destination: Path, log: LogCallback) -> bool:
    """Move a finished temporary file into place without replacing a real file."""
    if destination.exists():
        return False
    try:
        if os.name == "nt":
            os.rename(temp, destination)
            return True
        try:
            os.link(temp, destination)
            force_delete(temp)
            return True
        except FileExistsError:
            return False
        except OSError:
            fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                with os.fdopen(fd, "wb") as dst, temp.open("rb") as src:
                    shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                force_delete(destination)
                raise
            force_delete(temp)
            return True
    except FileExistsError:
        return False
    except Exception as exc:
        log(f"Could not move file into place without overwrite: {destination}: {exc}")
        return False


def move_file_no_overwrite(source: Path, destination: Path, log: LogCallback) -> bool:
    """Rename or move one finished media file without replacing the destination."""
    if path_key(source) == path_key(destination):
        return True
    if destination.exists():
        return False
    try:
        if os.name == "nt":
            os.rename(source, destination)
            return True
        try:
            os.link(source, destination)
            force_delete(source)
            return True
        except FileExistsError:
            return False
        except OSError:
            fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
                    shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                force_delete(destination)
                raise
            force_delete(source)
            return True
    except FileExistsError:
        return False
    except Exception as exc:
        log(f"Could not move file without overwrite: {source} -> {destination}: {exc}")
        return False


def copy_file_safely(
    source: Path,
    destination: Path,
    chunk_bytes: int,
    should_stop: Callable[[], bool],
    wait_if_paused: Callable[[], None],
    progress_callback: ProgressCallback,
    promote_temp: Callable[[Path, Path], bool],
    log: LogCallback,
    checksum_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """Copy through a unique temp file, detect source changes, and promote safely."""
    chunk = max(1, int(chunk_bytes))
    temp = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.part")
    try:
        if destination.exists():
            log(f"Copy stopped because destination already exists: {destination}")
            return False
        source_stat = source.stat()
        total = source_stat.st_size
        free = free_space_bytes(destination)
        if free is not None and free < total:
            log(f"Copy stopped: not enough free space for {destination}. Need {format_size(total)}, free {format_size(free)}.")
            return False
        copied = 0
        last_percent = -1
        last_callback_time = 0.0
        hasher = hashlib.sha256() if checksum_callback else None

        def report(percent: int, force: bool = False) -> None:
            nonlocal last_percent, last_callback_time
            now = time.time()
            percent = max(0, min(100, int(percent)))
            if force or percent != last_percent and now - last_callback_time >= 0.25:
                last_percent = percent
                last_callback_time = now
                progress_callback(percent, copied, total)

        report(0, force=True)
        with source.open("rb") as src, temp.open("xb") as dst:
            while True:
                if should_stop():
                    raise InterruptedError()
                wait_if_paused()
                data = src.read(chunk)
                if not data:
                    break
                if hasher:
                    hasher.update(data)
                dst.write(data)
                copied += len(data)
                report(int(copied / total * 100) if total else 100)
            dst.flush()
            os.fsync(dst.fileno())
        final_source_stat = source.stat()
        if copied != total or final_source_stat.st_size != total or final_source_stat.st_mtime_ns != source_stat.st_mtime_ns:
            force_delete(temp)
            log(f"Copy stopped because the source changed while it was being read: {source}")
            return False
        if not promote_temp(temp, destination):
            force_delete(temp)
            log(f"Copy stopped because destination appeared during copy: {destination}")
            return False
        try:
            shutil.copystat(source, destination)
        except OSError as exc:
            log(f"Copy completed, but file dates could not be preserved for {destination.name}: {exc}")
        if hasher and checksum_callback:
            checksum_callback(hasher.hexdigest())
        progress_callback(100, total, total)
        return True
    except InterruptedError:
        force_delete(temp)
        return False
    except Exception as exc:
        force_delete(temp)
        log(f"Copy error: {source} -> {destination}: {exc}")
        return False
