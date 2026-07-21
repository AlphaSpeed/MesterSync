from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from tasks import (
    STAGE_CONVERTING,
    STAGE_CONVERTING_IN_PLACE,
    STAGE_IMPORTING,
    STAGE_QUEUED,
    STAGE_STOPPED,
    STAGE_TRANSFERRING,
    STAGE_WAITING,
    STAGE_WAITING_TRANSFER,
    TaskState,
)


CONFIG_FILENAME = "mestersync_config.json"
HISTORY_FILENAME = "mestersync_history.json"
TASKS_FILENAME = "mestersync_tasks.json"
THUMBNAIL_DIRNAME = ".mestersync_thumbnails"
DATA_DIRNAME = "data"
CHECKSUM_DIRNAME = "checksums"
CHECKSUM_FILENAME = "mestersync_checksums.json"
PRESET_DIRNAME = "presets"
PRESET_TEST_DIRNAME = "preset_tests"


_json_write_locks_guard = threading.Lock()
_json_write_locks: Dict[str, threading.RLock] = {}


def json_write_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).lower()
    with _json_write_locks_guard:
        lock = _json_write_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _json_write_locks[key] = lock
        return lock


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def install_dir() -> Path:
    app = app_dir()
    if app.name.lower() == "app":
        return app.parent
    return app


def data_dir() -> Path:
    path = install_dir() / DATA_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def checksum_dir() -> Path:
    path = data_dir() / CHECKSUM_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_checksum_path() -> Path:
    return checksum_dir() / CHECKSUM_FILENAME


def storage_warning_path() -> Path:
    return data_dir() / "storage_warnings.log"


def log_storage_warning(message: str) -> None:
    try:
        with storage_warning_path().open("a", encoding="utf-8") as fh:
            fh.write(message.rstrip() + "\n")
    except Exception:
        pass


def resource_path(name: str) -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / name
    return app_dir() / name


def config_path() -> Path:
    return data_dir() / CONFIG_FILENAME


def history_path() -> Path:
    return data_dir() / HISTORY_FILENAME


def tasks_path() -> Path:
    return data_dir() / TASKS_FILENAME


def thumbnail_dir() -> Path:
    path = data_dir() / THUMBNAIL_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def preset_test_dir() -> Path:
    path = data_dir() / PRESET_TEST_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def preset_dir() -> Path:
    path = install_dir() / PRESET_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json_file(path: Path, default: Any) -> Any:
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        log_storage_warning(f"Could not read JSON file {path}: {exc}")
    try:
        if backup.exists():
            recovered = json.loads(backup.read_text(encoding="utf-8-sig"))
            log_storage_warning(f"Recovered JSON data from backup {backup}")
            return recovered
    except Exception as backup_exc:
        log_storage_warning(f"Could not read JSON backup {backup}: {backup_exc}")
    return default


def write_json_file(path: Path, data: Any) -> None:
    """Write JSON without exposing a partially written live state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    backup = path.with_suffix(path.suffix + ".bak")
    backup_temp = backup.with_name(f".{backup.name}.{uuid.uuid4().hex}.tmp")
    with json_write_lock(path):
        try:
            with temp.open("x", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            if path.exists():
                try:
                    json.loads(path.read_text(encoding="utf-8-sig"))
                except Exception as exc:
                    log_storage_warning(f"Did not replace good JSON backup with invalid primary {path}: {exc}")
                else:
                    shutil.copy2(path, backup_temp)
                    os.replace(backup_temp, backup)
            os.replace(temp, path)
        except Exception as exc:
            for leftover in (temp, backup_temp):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
            log_storage_warning(f"Could not atomically write JSON file {path}: {exc}")
            raise


def load_history_records() -> list[Dict[str, Any]]:
    data = read_json_file(history_path(), [])
    return data if isinstance(data, list) else []


def save_history_records(records: list[Dict[str, Any]]) -> None:
    write_json_file(history_path(), records)


def load_task_records() -> list[Dict[str, Any]]:
    data = read_json_file(tasks_path(), [])
    return data if isinstance(data, list) else []


def save_task_records(records: list[Dict[str, Any]]) -> None:
    write_json_file(tasks_path(), records)


def normalize_config_data(config_data: Dict[str, Any], default_ffmpeg_args: list[str]) -> Dict[str, Any]:
    cfg = dict(config_data)
    if not isinstance(cfg.get("ffmpeg_args"), list):
        cfg["ffmpeg_args"] = default_ffmpeg_args[:]
    checksum_path = str(cfg.get("checksum_database_path", "")).strip()
    if not checksum_path or Path(checksum_path).name.lower() in {"mestersync_checksums.json", ".checksums.json"}:
        cfg["checksum_database_path"] = str(default_checksum_path())
    preset_folder = str(cfg.get("preset_folder", "")).strip()
    preset_path = Path(preset_folder) if preset_folder else None
    if (
        not preset_folder
        or (preset_path and preset_path.name.lower() == "ffmpeg_presets")
        or (preset_path and preset_path.name.lower() == PRESET_DIRNAME and not preset_path.exists())
    ):
        cfg["preset_folder"] = str(preset_dir())
    return cfg


def load_config_data(default_config: Dict[str, Any], default_ffmpeg_args: list[str]) -> Dict[str, Any]:
    cfg = dict(default_config)
    saved = read_json_file(config_path(), {})
    if isinstance(saved, dict):
        cfg.update(saved)
    return normalize_config_data(cfg, default_ffmpeg_args)


def load_checksum_records(path: Path) -> Dict[str, Any]:
    data = read_json_file(path, {})
    return dict(data) if isinstance(data, dict) else {}


def save_checksum_records(path: Path, records: Dict[str, Any]) -> None:
    write_json_file(path, records)


def converted_history_count(records: list[Dict[str, Any]]) -> int:
    return sum(1 for rec in records if rec.get("status") in {"transferred", "converted_only", "converted_in_place"})


def task_to_record(task: TaskState) -> Dict[str, Any]:
    def path_value(value: Optional[Path]) -> str:
        return str(value) if value else ""

    return {
        "task_id": task.task_id,
        "original_path": path_value(task.original_path),
        "display_name": task.display_name,
        "local_input_path": path_value(task.local_input_path),
        "output_path": path_value(task.output_path),
        "nas_path": path_value(task.nas_path),
        "rename_base": task.rename_base,
        "output_ext": task.output_ext,
        "stage": task.stage,
        "progress": task.progress,
        "detail": task.detail,
        "imported_at": task.imported_at,
        "converted_at": task.converted_at,
        "transferred_at": task.transferred_at,
        "eta_seconds": task.eta_seconds,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "thumbnail_path": task.thumbnail_path,
        "thumbnail_preview_paths": task.thumbnail_preview_paths,
        "imported_size": task.imported_size,
        "original_checksum": task.original_checksum,
        "output_size": task.output_size,
        "preset_name": task.preset_name,
        "ffmpeg_args": task.ffmpeg_args,
        "conversion_started": task.conversion_started,
        "space_savings": task.space_savings,
        "skipped": task.skipped,
        "skip_archive_due": task.skip_archive_due,
        "duplicate": task.duplicate,
        "duplicate_reason": task.duplicate_reason,
        "error_log": task.error_log,
        "convert_in_place": task.convert_in_place,
    }


def record_to_task(record: Dict[str, Any]) -> Optional[TaskState]:
    try:
        original_path = Path(record.get("original_path") or "")
        if not str(original_path):
            return None
        task = TaskState(
            task_id=str(record.get("task_id") or uuid.uuid4().hex),
            original_path=original_path,
            display_name=str(record.get("display_name") or original_path.name),
            local_input_path=Path(record["local_input_path"]) if record.get("local_input_path") else None,
            output_path=Path(record["output_path"]) if record.get("output_path") else None,
            nas_path=Path(record["nas_path"]) if record.get("nas_path") else None,
            rename_base=str(record.get("rename_base") or original_path.stem),
            output_ext=str(record.get("output_ext") or ".mp4"),
            stage=str(record.get("stage") or STAGE_STOPPED),
            progress=int(record.get("progress") or 0),
            detail=str(record.get("detail") or "Restored from previous session"),
            imported_at=record.get("imported_at"),
            converted_at=record.get("converted_at"),
            transferred_at=record.get("transferred_at"),
            eta_seconds=record.get("eta_seconds"),
            created_at=float(record.get("created_at") or time.time()),
            started_at=float(record.get("started_at") or time.time()),
            thumbnail_path=record.get("thumbnail_path"),
            thumbnail_preview_paths=record.get("thumbnail_preview_paths") if isinstance(record.get("thumbnail_preview_paths"), list) else [],
            imported_size=record.get("imported_size"),
            original_checksum=str(record.get("original_checksum") or ""),
            output_size=record.get("output_size"),
            preset_name=str(record.get("preset_name") or "Current settings"),
            ffmpeg_args=record.get("ffmpeg_args") if isinstance(record.get("ffmpeg_args"), list) else None,
            conversion_started=bool(record.get("conversion_started", False)),
            space_savings=str(record.get("space_savings") or ""),
            skipped=bool(record.get("skipped", False)),
            skip_archive_due=record.get("skip_archive_due"),
            duplicate=bool(record.get("duplicate", False)),
            duplicate_reason=str(record.get("duplicate_reason") or ""),
            error_log=str(record.get("error_log") or ""),
            convert_in_place=bool(record.get("convert_in_place", False)),
        )
        if task.stage in {STAGE_IMPORTING, STAGE_QUEUED, STAGE_CONVERTING, STAGE_CONVERTING_IN_PLACE, STAGE_WAITING_TRANSFER, STAGE_TRANSFERRING, STAGE_WAITING}:
            task.stage = STAGE_STOPPED
            task.detail = "Restored after app restart. Press Restart/Resume to continue."
            task.progress = 0
        return task
    except Exception:
        return None
