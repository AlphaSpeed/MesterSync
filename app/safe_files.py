from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from file_utils import force_delete, format_size, free_space_bytes, path_key, sha256_file


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, int], None]


def _delete_windows_file_if_checksum_matches(path: Path, expected: str, log: LogCallback) -> bool:
    """Hash and mark the same exclusive Windows handle for deletion.

    Keeping this handle open without write/delete sharing prevents another
    process from replacing the verified file between hashing and deletion.
    Never fall back to deleting by pathname when the exclusive open fails.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    set_information.restype = wintypes.BOOL

    # GENERIC_READ | DELETE; no sharing; OPEN_EXISTING; sequential read,
    # OPEN_REPARSE_POINT so a replaced symlink cannot redirect cleanup.
    handle = create_file(str(path), 0x80000000 | 0x00010000, 0, None, 3, 0x08200000, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (2, 3):  # File/path genuinely absent.
            return True
        log(f"Kept working file because it could not be locked for safe cleanup: {path} ({ctypes.WinError(error)})")
        return False
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        close_handle(handle)
        raise
    with os.fdopen(descriptor, "rb") as stream:
        opened_stat = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened_stat.st_mode) or getattr(opened_stat, "st_file_attributes", 0) & 0x400:
            log(f"Kept file because it became a link or special file before cleanup: {path}")
            return False
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
        if digest.hexdigest() != expected:
            log(f"Kept file because its content changed after MesterSync created it: {path}")
            return False
        # FILE_DISPOSITION_INFO contains one BOOLEAN, FileDispositionInfo = 4.
        disposition = ctypes.c_ubyte(1)
        if not set_information(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            error = ctypes.get_last_error()
            log(f"Kept verified working file because safe cleanup was refused: {path} ({ctypes.WinError(error)})")
            return False
    return True


def delete_file_if_checksum_matches(path: Path, expected_checksum: str, log: LogCallback) -> bool:
    """Delete only when the file still has the exact content MesterSync expects.

    This protects against deleting a different recording that was edited or put
    at the same path after the app created its working copy.
    """
    expected = str(expected_checksum or "").strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        log(f"Kept file because no trusted checksum was available before cleanup: {path}")
        return False
    try:
        initial_stat = path.lstat()
        if not stat.S_ISREG(initial_stat.st_mode) or getattr(initial_stat, "st_file_attributes", 0) & 0x400:
            log(f"Kept file because safe cleanup cannot delete a link or special file: {path}")
            return False
        if os.name == "nt":
            return _delete_windows_file_if_checksum_matches(path, expected, log)
    except FileNotFoundError:
        return True
    except OSError as exc:
        log(f"Kept working file because it could not be checked for safe cleanup: {path}: {exc}")
        return False
    actual = sha256_file(path)
    if not actual:
        log(f"Kept file because it changed or could not be checked before cleanup: {path}")
        return False
    if actual.lower() != expected:
        log(f"Kept file because its content changed after MesterSync created it: {path}")
        return False
    try:
        final_stat = path.lstat()
        if (initial_stat.st_dev, initial_stat.st_ino, initial_stat.st_size, initial_stat.st_mtime_ns) != (
            final_stat.st_dev, final_stat.st_ino, final_stat.st_size, final_stat.st_mtime_ns
        ):
            log(f"Kept file because it changed before cleanup: {path}")
            return False
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError as exc:
        log(f"Could not delete verified working file: {path}: {exc}")
        return False
    return True


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
                if should_stop():
                    raise InterruptedError()
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
        if should_stop():
            raise InterruptedError()
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
