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
    STAGE_CHECKING,
    STAGE_COMPLETE,
    STAGE_CONVERTED,
    STAGE_CONVERTED_IN_PLACE,
    STAGE_CONVERTED_OUTPUT,
    STAGE_CONVERTING,
    STAGE_CONVERTING_IN_PLACE,
    STAGE_DETECTED,
    STAGE_DUPLICATE_REVIEW,
    STAGE_IMPORTED,
    STAGE_IMPORTING,
    STAGE_QUEUED,
    STAGE_STOPPED,
    STAGE_STOPPING,
    STAGE_TRANSFERRING,
    STAGE_WAITING,
    STAGE_WAITING_TRANSFER,
    TaskState,
)


CONFIG_FILENAME = "mestersync_config.json"
HISTORY_FILENAME = "mestersync_history.json"
TASKS_FILENAME = "mestersync_tasks.json"
RETAINED_MEDIA_FILENAME = "retained_media.json"
THUMBNAIL_DIRNAME = ".mestersync_thumbnails"
DATA_DIRNAME = "data"
CHECKSUM_DIRNAME = "checksums"
CHECKSUM_FILENAME = "mestersync_checksums.json"
PRESET_DIRNAME = "presets"
PRESET_TEST_DIRNAME = "preset_tests"
APP_DATA_DIRNAME = "MesterSync"
UPDATE_DIRNAME = "updates"


_json_write_locks_guard = threading.Lock()
_json_write_locks: Dict[str, threading.RLock] = {}
_storage_setup_lock = threading.RLock()
_storage_setup_complete = False


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


def user_storage_root() -> Path:
    """Return the writable per-user home for installed and portable builds."""
    override = os.environ.get("MESTERSYNC_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_DATA_DIRNAME
        return Path.home() / "AppData" / "Local" / APP_DATA_DIRNAME
    return Path.home() / f".{APP_DATA_DIRNAME.lower()}"


def _command_line_migration_root() -> Optional[Path]:
    prefix = "--migrate-from="
    for index, value in enumerate(sys.argv[1:]):
        if value.lower().startswith(prefix):
            candidate = value[len(prefix):].strip().strip('"')
            return Path(candidate).expanduser() if candidate else None
        if value.lower() == "--migrate-from" and index + 2 < len(sys.argv):
            candidate = sys.argv[index + 2].strip().strip('"')
            return Path(candidate).expanduser() if candidate else None
    env_value = os.environ.get("MESTERSYNC_MIGRATE_FROM", "").strip()
    return Path(env_value).expanduser() if env_value else None


def _copy_missing_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _legacy_storage_roots() -> list[Path]:
    candidates: list[Path] = []
    explicit = _command_line_migration_root()
    if explicit is not None:
        candidates.append(explicit)
    candidates.append(install_dir())
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def _write_setup_warning(destination_data: Path, message: str) -> None:
    try:
        destination_data.mkdir(parents=True, exist_ok=True)
        with (destination_data / "storage_warnings.log").open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except OSError:
        pass


def ensure_user_storage() -> None:
    """Seed writable app data without deleting or overwriting portable data."""
    global _storage_setup_complete
    with _storage_setup_lock:
        if _storage_setup_complete:
            return
        root = user_storage_root()
        destination_data = root / DATA_DIRNAME
        destination_presets = root / PRESET_DIRNAME
        destination_data.mkdir(parents=True, exist_ok=True)
        destination_presets.mkdir(parents=True, exist_ok=True)

        for legacy_root in _legacy_storage_roots():
            if same_location(legacy_root, root):
                continue
            try:
                # Support both the current portable layout and releases that
                # kept JSON/cache files beside the launcher or in app/.
                legacy_data_locations = [legacy_root / DATA_DIRNAME, legacy_root, legacy_root / "app"]
                for legacy_data in legacy_data_locations:
                    for filename in (CONFIG_FILENAME, HISTORY_FILENAME, TASKS_FILENAME, RETAINED_MEDIA_FILENAME):
                        for suffix in ("", ".bak"):
                            source = legacy_data / f"{filename}{suffix}"
                            target = destination_data / source.name
                            if source.is_file() and not target.exists():
                                shutil.copy2(source, target)
                    for dirname in (THUMBNAIL_DIRNAME, CHECKSUM_DIRNAME, PRESET_TEST_DIRNAME):
                        _copy_missing_tree(legacy_data / dirname, destination_data / dirname)
                for preset_source in (
                    legacy_root / PRESET_DIRNAME,
                    legacy_root / "ffmpeg_presets",
                    legacy_root / "app" / "ffmpeg_presets",
                ):
                    _copy_missing_tree(preset_source, destination_presets)
            except OSError as exc:
                _write_setup_warning(destination_data, f"Could not migrate portable data from {legacy_root}: {exc}")

        bundled_presets = resource_path(PRESET_DIRNAME) if getattr(sys, "frozen", False) else install_dir() / PRESET_DIRNAME
        if not same_location(bundled_presets, destination_presets):
            try:
                _copy_missing_tree(bundled_presets, destination_presets)
            except OSError as exc:
                _write_setup_warning(destination_data, f"Could not seed bundled presets from {bundled_presets}: {exc}")
        _storage_setup_complete = True


def data_dir() -> Path:
    ensure_user_storage()
    path = user_storage_root() / DATA_DIRNAME
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


def retained_media_path() -> Path:
    return data_dir() / RETAINED_MEDIA_FILENAME


def thumbnail_dir() -> Path:
    path = data_dir() / THUMBNAIL_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def preset_test_dir() -> Path:
    path = data_dir() / PRESET_TEST_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def preset_dir() -> Path:
    ensure_user_storage()
    path = user_storage_root() / PRESET_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def update_dir() -> Path:
    ensure_user_storage()
    path = user_storage_root() / UPDATE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def same_location(left: Path | str, right: Path | str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))
    except Exception:
        return str(left) == str(right)


def migrate_thumbnail_path(value: Any) -> str:
    """Relocate a thumbnail reference from an older MesterSync installation.

    Only paths whose immediate parent is MesterSync's private thumbnail-cache
    directory are eligible. Media paths and other user-selected locations are
    deliberately left untouched.
    """
    raw_path = str(value or "").strip()
    if not raw_path:
        return ""
    saved_path = Path(raw_path)
    if saved_path.parent.name.lower() != THUMBNAIL_DIRNAME.lower():
        return raw_path
    current_folder = thumbnail_dir()
    if same_location(saved_path.parent, current_folder):
        return raw_path
    return str(current_folder / saved_path.name)


def migrate_record_thumbnail_paths(record: Dict[str, Any]) -> Dict[str, Any]:
    migrated = dict(record)
    if migrated.get("thumbnail_path"):
        migrated["thumbnail_path"] = migrate_thumbnail_path(migrated["thumbnail_path"])
    previews = migrated.get("thumbnail_preview_paths")
    if isinstance(previews, list):
        migrated["thumbnail_preview_paths"] = [migrate_thumbnail_path(path) for path in previews if path]
    return migrated


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
    if not isinstance(data, list):
        return []
    return [migrate_record_thumbnail_paths(record) for record in data if isinstance(record, dict)]


def save_history_records(records: list[Dict[str, Any]]) -> None:
    write_json_file(history_path(), records)


def load_task_records() -> list[Dict[str, Any]]:
    data = read_json_file(tasks_path(), [])
    if not isinstance(data, list):
        return []
    return [migrate_record_thumbnail_paths(record) for record in data if isinstance(record, dict)]


def save_task_records(records: list[Dict[str, Any]]) -> None:
    write_json_file(tasks_path(), records)


def normalize_config_data(config_data: Dict[str, Any], default_ffmpeg_args: list[str]) -> Dict[str, Any]:
    cfg = dict(config_data)
    if not isinstance(cfg.get("ffmpeg_args"), list):
        cfg["ffmpeg_args"] = default_ffmpeg_args[:]
    else:
        legacy_default_args = default_ffmpeg_args[:]
        try:
            legacy_default_args[legacy_default_args.index("0:a:0?")] = "0:a:0"
        except ValueError:
            pass
        if cfg["ffmpeg_args"] == legacy_default_args:
            cfg["ffmpeg_args"] = default_ffmpeg_args[:]
    checksum_path = str(cfg.get("checksum_database_path", "")).strip()
    if not checksum_path or Path(checksum_path).name.lower() in {"mestersync_checksums.json", ".checksums.json"}:
        cfg["checksum_database_path"] = str(default_checksum_path())
    preset_folder = str(cfg.get("preset_folder", "")).strip()
    preset_path = Path(preset_folder) if preset_folder else None
    current_preset_dir = preset_dir()
    if (
        not preset_folder
        or (preset_path and preset_path.name.lower() == "ffmpeg_presets")
        or (
            preset_path
            and preset_path.name.lower() == PRESET_DIRNAME
            and not same_location(preset_path, current_preset_dir)
        )
    ):
        cfg["preset_folder"] = str(current_preset_dir)
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


def _validated_retained_media_records(data: Any) -> Dict[str, Dict[str, Any]]:
    from file_utils import path_key

    if not isinstance(data, dict):
        raise ValueError("Retained video safety records must be a mapping.")
    records: Dict[str, Dict[str, Any]] = {}
    for value in data.values():
        if not isinstance(value, dict) or not isinstance(value.get("path"), str) or not value["path"].strip():
            raise ValueError("A retained video safety record has no valid file location.")
        signature = value.get("signature")
        quick = value.get("quick_fingerprint")
        if (
            not isinstance(signature, list)
            or len(signature) != 5
            or any(type(item) is not int for item in signature)
            or not isinstance(quick, str)
            or len(quick) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in quick)
        ):
            raise ValueError("A retained video safety record has an invalid file fingerprint.")
        normalized = dict(value)
        normalized["quick_fingerprint"] = quick.lower()
        records[path_key(value["path"])] = normalized
    return records


def load_retained_media_records() -> Dict[str, Dict[str, Any]]:
    """Restore automatic-conversion exclusions independently of visible History.

    Existing unreadable state must not silently become an empty map: that could
    make a deliberately retained converted video eligible for conversion again.
    """
    path = retained_media_path()
    backup = path.with_suffix(path.suffix + ".bak")
    if not path.exists() and not backup.exists():
        return {}
    data = read_json_file(path, None)
    if data is None:
        raise ValueError("Retained video safety records could not be read or recovered from backup.")
    return _validated_retained_media_records(data)


def save_retained_media_records(records: Dict[str, Dict[str, Any]]) -> None:
    """Persist retained video identities before releasing completed task paths."""
    write_json_file(retained_media_path(), _validated_retained_media_records(records))


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
        "source_duration_seconds": task.source_duration_seconds,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "thumbnail_path": task.thumbnail_path,
        "thumbnail_preview_paths": task.thumbnail_preview_paths,
        "imported_size": task.imported_size,
        "original_checksum": task.original_checksum,
        "import_verified": task.import_verified,
        "output_size": task.output_size,
        "output_validated": task.output_validated,
        "preset_name": task.preset_name,
        "ffmpeg_args": task.ffmpeg_args,
        "conversion_started": task.conversion_started,
        "pending_conversion_args": task.pending_conversion_args,
        "conversion_stop_requested": task.conversion_stop_event.is_set(),
        "space_savings": task.space_savings,
        "skipped": task.skipped,
        "skip_archive_due": task.skip_archive_due,
        "duplicate": task.duplicate,
        "duplicate_reason": task.duplicate_reason,
        "force_import": task.force_import,
        "error_log": task.error_log,
        "convert_in_place": task.convert_in_place,
    }


def record_to_task(record: Dict[str, Any]) -> Optional[TaskState]:
    try:
        original_value = str(record.get("original_path") or "").strip()
        if not original_value:
            return None
        original_path = Path(original_value)
        saved_stage = str(record.get("stage") or STAGE_STOPPED)
        checksum = str(record.get("original_checksum") or "").strip().lower()
        valid_checksum = len(checksum) == 64 and all(ch in "0123456789abcdef" for ch in checksum)
        # Older versions did not persist verification separately. Only infer it
        # from a stage that was reached after the relevant check completed;
        # Interrupted/Error/Stopped tasks may still contain unchecked media.
        completed_output_stages = {
            STAGE_CONVERTED, STAGE_WAITING_TRANSFER, STAGE_TRANSFERRING,
            STAGE_COMPLETE, STAGE_CONVERTED_OUTPUT, STAGE_CONVERTED_IN_PLACE,
        }
        verified_import_stages = completed_output_stages | {
            STAGE_IMPORTED, STAGE_QUEUED, STAGE_WAITING, STAGE_CONVERTING,
            STAGE_CONVERTING_IN_PLACE, STAGE_DUPLICATE_REVIEW,
        }
        legacy_import_verified = bool(
            record.get("local_input_path") and record.get("imported_at")
            and valid_checksum and saved_stage in verified_import_stages
        )
        legacy_output_validated = bool(
            record.get("output_path") and record.get("converted_at")
            and saved_stage in completed_output_stages
        )
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
            source_duration_seconds=record.get("source_duration_seconds"),
            created_at=float(record.get("created_at") or time.time()),
            started_at=float(record.get("started_at") or time.time()),
            thumbnail_path=record.get("thumbnail_path"),
            thumbnail_preview_paths=record.get("thumbnail_preview_paths") if isinstance(record.get("thumbnail_preview_paths"), list) else [],
            imported_size=record.get("imported_size"),
            original_checksum=str(record.get("original_checksum") or ""),
            import_verified=record.get("import_verified", legacy_import_verified) is True,
            output_size=record.get("output_size"),
            output_validated=record.get("output_validated", legacy_output_validated) is True,
            preset_name=str(record.get("preset_name") or "Current settings"),
            ffmpeg_args=record.get("ffmpeg_args") if isinstance(record.get("ffmpeg_args"), list) else None,
            conversion_started=bool(record.get("conversion_started", False)),
            pending_conversion_args=record.get("pending_conversion_args") if isinstance(record.get("pending_conversion_args"), list) else None,
            space_savings=str(record.get("space_savings") or ""),
            skipped=bool(record.get("skipped", False)),
            skip_archive_due=record.get("skip_archive_due"),
            duplicate=bool(record.get("duplicate", False)),
            duplicate_reason=str(record.get("duplicate_reason") or ""),
            force_import=bool(record.get("force_import", False)),
            error_log=str(record.get("error_log") or ""),
            convert_in_place=bool(record.get("convert_in_place", False)),
        )
        if record.get("conversion_stop_requested") is True:
            task.conversion_stop_event.set()
        if task.stage in {
            STAGE_CHECKING,
            STAGE_DETECTED,
            STAGE_IMPORTING,
            STAGE_QUEUED,
            STAGE_STOPPING,
            STAGE_CONVERTING,
            STAGE_CONVERTING_IN_PLACE,
            STAGE_WAITING_TRANSFER,
            STAGE_TRANSFERRING,
            STAGE_WAITING,
        }:
            task.stage = STAGE_STOPPED
            task.detail = "Ready to resume after restart."
            task.progress = 0
        return task
    except Exception:
        return None
