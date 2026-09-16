from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


LEDGER_DIRNAME = ".mestersync"
LEDGER_RECORDS_DIRNAME = "original-checksums"
LEDGER_MARKER_FILENAME = "duplicate-ledger.json"
LEDGER_VERSION = 1
MAX_RECORD_BYTES = 1024 * 1024
MEDIA_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
    ".flv",
    ".wmv",
    ".webm",
    ".mts",
    ".m2ts",
    ".mpg",
    ".mpeg",
}


@dataclass(frozen=True)
class SharedLedgerSync:
    records: Dict[str, Dict[str, Any]]
    initialized: bool
    exported: int = 0
    error: str = ""
    already_present: bool = False


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _valid_digest(value: Any) -> str:
    text = str(value or "").strip().lower()
    if len(text) == 64 and all(ch in "0123456789abcdef" for ch in text):
        return text
    return ""


def _record_paths(value: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    raw_paths = value.get("final_paths")
    if isinstance(raw_paths, list):
        paths.extend(str(path).strip() for path in raw_paths if str(path).strip())
    for key in ("final_path", "path", "imported_path"):
        path = str(value.get(key) or "").strip()
        if path:
            paths.append(path)
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _normalized_record(digest: str, value: Any) -> Optional[Dict[str, Any]]:
    digest = _valid_digest(digest)
    # Older local databases used digest -> path. The checksum is still valid
    # even though these records do not have size/sample metadata yet.
    if isinstance(value, str) and value.strip():
        value = {"final_path": value.strip()}
    if not digest or not isinstance(value, Mapping):
        return None
    try:
        original_size = value.get("original_size")
        original_size = int(original_size) if original_size is not None else None
        if original_size is not None and original_size < 0:
            original_size = None
    except (TypeError, ValueError):
        original_size = None
    quick = _valid_digest(value.get("quick_fingerprint"))
    paths = _record_paths(value)
    record: Dict[str, Any] = {
        "version": LEDGER_VERSION,
        "original_checksum": digest,
        "original_name": str(value.get("original_name") or ""),
        "original_size": original_size,
        "quick_fingerprint": quick,
        "final_paths": paths,
        "updated_at": str(value.get("updated_at") or _now_iso()),
    }
    if paths:
        record["final_path"] = paths[-1]
    return record


def _merge_records(digest: str, old: Any, new: Any) -> Dict[str, Any]:
    old_record = _normalized_record(digest, old) or _normalized_record(digest, {})
    new_record = _normalized_record(digest, new) or _normalized_record(digest, {})
    assert old_record is not None and new_record is not None
    paths = _record_paths(old_record) + _record_paths(new_record)
    merged_paths: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            merged_paths.append(path)
    merged: Dict[str, Any] = {
        "version": LEDGER_VERSION,
        "original_checksum": digest,
        "original_name": new_record.get("original_name") or old_record.get("original_name") or "",
        "original_size": (
            new_record.get("original_size")
            if new_record.get("original_size") is not None
            else old_record.get("original_size")
        ),
        "quick_fingerprint": new_record.get("quick_fingerprint") or old_record.get("quick_fingerprint") or "",
        "final_paths": merged_paths,
    }
    if merged_paths:
        merged["final_path"] = merged_paths[-1]
    comparable_old = dict(old_record)
    comparable_old.pop("updated_at", None)
    if comparable_old == merged:
        merged["updated_at"] = old_record.get("updated_at") or _now_iso()
    else:
        merged["updated_at"] = _now_iso()
    return merged


def _read_json(path: Path) -> Any:
    if path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError(f"record is unexpectedly large: {path.name}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _ledger_paths(nas_folder: Path) -> Tuple[Path, Path, Path]:
    ledger = nas_folder / LEDGER_DIRNAME
    return ledger, ledger / LEDGER_MARKER_FILENAME, ledger / LEDGER_RECORDS_DIRNAME


def _shared_record_path(records_folder: Path, digest: str) -> Path:
    return records_folder / digest[:2] / f"{digest}.json"


def _acquire_record_lock(record_path: Path, timeout_seconds: float = 10.0) -> Path:
    lock_path = record_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with lock_path.open("x", encoding="ascii") as handle:
                handle.write(f"{os.getpid()} {uuid.uuid4().hex}\n")
                handle.flush()
                os.fsync(handle.fileno())
            return lock_path
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 300:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("another computer is updating this checksum record")
            time.sleep(0.1)


def _release_record_lock(lock_path: Optional[Path]) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _load_shared_records(records_folder: Path, required: bool = False) -> Tuple[Dict[str, Dict[str, Any]], list[str]]:
    records: Dict[str, Dict[str, Any]] = {}
    errors: list[str] = []
    if not records_folder.is_dir():
        if required:
            return records, ["the checksum-record folder is missing or unavailable"]
        return records, errors
    paths: list[Path] = []
    # pathlib's recursive glob can silently suppress directory access errors.
    # Keep readable records, but never claim the missing part was checked.
    for root, _dirnames, filenames in os.walk(records_folder, onerror=lambda exc: errors.append(str(exc))):
        paths.extend(Path(root) / name for name in filenames if Path(name).suffix.lower() == ".json")
    for path in paths:
        digest = _valid_digest(path.stem)
        if not digest:
            errors.append(f"invalid record filename {path.name}")
            continue
        try:
            raw = _read_json(path)
            payload_digest = _valid_digest(raw.get("original_checksum")) if isinstance(raw, Mapping) else ""
            if payload_digest != digest:
                raise ValueError("checksum inside the record does not match its filename")
            record = _normalized_record(digest, raw)
            if record is None:
                raise ValueError("invalid checksum record")
            records[digest] = record
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    return records, errors


def _known_media_locations(
    nas_folder: Path,
    records: Mapping[str, Any],
    extra_paths: Iterable[Path | str],
) -> set[str]:
    exact: set[str] = set()

    def remember_if_on_nas(raw_path: Path | str) -> None:
        path = Path(raw_path)
        try:
            resolved = path.resolve()
            root = nas_folder.resolve()
            if resolved != root and root not in resolved.parents:
                return
        except (OSError, RuntimeError, ValueError):
            return
        exact.add(os.path.normcase(os.path.abspath(str(path))))

    for value in records.values():
        if not isinstance(value, Mapping):
            continue
        for raw_path in _record_paths(value):
            remember_if_on_nas(raw_path)
    for raw_path in extra_paths:
        remember_if_on_nas(raw_path)
    return exact


def _first_untracked_media(
    nas_folder: Path,
    records: Mapping[str, Any],
    extra_paths: Iterable[Path | str],
) -> Optional[Path]:
    exact = _known_media_locations(nas_folder, records, extra_paths)
    ledger_name = LEDGER_DIRNAME.lower()
    def raise_scan_error(exc: OSError) -> None:
        raise exc
    try:
        for root, dirnames, filenames in os.walk(nas_folder, onerror=raise_scan_error):
            dirnames[:] = [name for name in dirnames if name.lower() != ledger_name]
            for filename in filenames:
                if Path(filename).suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                if filename.startswith("._"):
                    continue
                path = Path(root) / filename
                key = os.path.normcase(os.path.abspath(str(path)))
                if key not in exact:
                    return path
    except OSError as exc:
        raise OSError(f"could not scan existing NAS media: {exc}") from exc
    return None


def sync_shared_checksum_ledger(
    nas_folder: Path,
    local_records: Mapping[str, Any],
    known_final_paths: Iterable[Path | str] = (),
) -> SharedLedgerSync:
    """Merge local original checksums with a durable per-record NAS ledger.

    One JSON file is used per original checksum so different ingest computers
    cannot erase each other's entries by replacing one shared database file.
    The ledger is not silently initialized over an existing untracked archive:
    that state must pause imports because converted media cannot reconstruct the
    byte-for-byte checksum of its camera original.
    """
    nas_folder = Path(nas_folder)
    try:
        if not nas_folder.is_dir():
            return SharedLedgerSync({}, False, error="The NAS folder is unavailable, so shared duplicate history could not be checked.")
    except OSError:
        return SharedLedgerSync({}, False, error="The NAS folder is unavailable, so shared duplicate history could not be checked.")

    _ledger_folder, marker_path, records_folder = _ledger_paths(nas_folder)
    local: Dict[str, Dict[str, Any]] = {}
    for digest, value in local_records.items():
        normalized = _normalized_record(str(digest), value)
        if normalized is not None:
            local[normalized["original_checksum"]] = normalized

    marker_exists = False
    if marker_path.exists():
        try:
            marker = _read_json(marker_path)
            marker_exists = isinstance(marker, Mapping) and int(marker.get("version", 0)) == LEDGER_VERSION
            if not marker_exists:
                return SharedLedgerSync({}, False, error="The shared NAS duplicate-history marker is invalid.")
        except Exception as exc:
            return SharedLedgerSync({}, False, error=f"The shared NAS duplicate-history marker could not be read: {exc}")

    shared, read_errors = _load_shared_records(records_folder, required=marker_exists)
    if read_errors:
        return SharedLedgerSync(
            shared,
            marker_exists,
            error=f"A shared NAS duplicate-history record could not be read ({read_errors[0]}).",
        )

    if not marker_exists:
        try:
            coverage = dict(shared)
            for digest, value in local.items():
                coverage[digest] = _merge_records(digest, shared.get(digest, {}), value)
            untracked = _first_untracked_media(nas_folder, coverage, known_final_paths)
        except OSError as exc:
            return SharedLedgerSync(shared, False, error=str(exc))
        if untracked is not None:
            return SharedLedgerSync(
                shared,
                False,
                error=(
                    "The NAS folder already contains media without shared original-checksum history "
                    f"({untracked.name}). MesterSync cannot prove a new disk file was not imported before."
                ),
            )

    exported = 0
    try:
        for digest, value in local.items():
            path = _shared_record_path(records_folder, digest)
            previous = shared.get(digest, {})
            if previous and _merge_records(digest, previous, value) == previous:
                # Most records are unchanged. Avoid creating/flushing a NAS
                # lock file for each of those on every disk insertion.
                continue
            # A different computer may have published this same recording since
            # the scan above. Read and merge again under the claim writer's lock.
            merged, written, _present = _update_shared_record(path, digest, value)
            exported += int(written)
            shared[digest] = merged
        if not marker_exists:
            records_folder.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(
                marker_path,
                {
                    "version": LEDGER_VERSION,
                    "created_at": _now_iso(),
                    "description": "MesterSync original recording checksum ledger",
                },
            )
            marker_exists = True
    except Exception as exc:
        return SharedLedgerSync(shared, marker_exists, exported, f"Shared NAS duplicate history could not be saved: {exc}")

    return SharedLedgerSync(shared, marker_exists, exported)


def _update_shared_record(path: Path, digest: str, value: Mapping[str, Any]) -> Tuple[Dict[str, Any], bool, bool]:
    lock_path = _acquire_record_lock(path)
    try:
        old: Dict[str, Any] = {}
        try:
            raw = _read_json(path)
        except FileNotFoundError:
            already_present = False
        else:
            already_present = True
            payload_digest = _valid_digest(raw.get("original_checksum")) if isinstance(raw, Mapping) else ""
            if payload_digest != digest:
                raise ValueError("checksum inside the record does not match its filename")
            old = dict(raw)
        merged = _merge_records(digest, old, value)
        written = not already_present or merged != _normalized_record(digest, old)
        if written:
            _write_json_atomic(path, merged)
        return merged, written, already_present
    finally:
        _release_record_lock(lock_path)


def publish_shared_checksum_record(nas_folder: Path, digest: str, value: Mapping[str, Any]) -> SharedLedgerSync:
    """Publish one completed transfer without rescanning the full shared ledger."""
    nas_folder = Path(nas_folder)
    digest = _valid_digest(digest)
    record = _normalized_record(digest, value)
    if not digest or record is None:
        return SharedLedgerSync({}, False, error="The completed transfer has no valid original checksum to share.")
    try:
        if not nas_folder.is_dir():
            return SharedLedgerSync({}, False, error="The NAS folder is unavailable, so shared duplicate history could not be updated.")
    except OSError:
        return SharedLedgerSync({}, False, error="The NAS folder is unavailable, so shared duplicate history could not be updated.")

    _ledger_folder, marker_path, records_folder = _ledger_paths(nas_folder)
    marker_exists = False
    try:
        marker = _read_json(marker_path)
        marker_exists = isinstance(marker, Mapping) and int(marker.get("version", 0)) == LEDGER_VERSION
        if not marker_exists:
            return SharedLedgerSync({}, False, error="The shared NAS duplicate-history marker is invalid.")
    except FileNotFoundError:
        # Preserve newly learned checksums even when an older archive cannot yet
        # claim complete coverage. Unknown files will continue to pause, while
        # exact matches to these partial records can still be blocked safely.
        marker_exists = False
    except Exception as exc:
        return SharedLedgerSync({}, False, error=f"The shared NAS duplicate-history marker could not be read: {exc}")

    try:
        if marker_exists and not records_folder.is_dir():
            return SharedLedgerSync({}, True, error="The shared checksum-record folder is missing or unavailable.")
        path = _shared_record_path(records_folder, digest)
        merged, written, already_present = _update_shared_record(path, digest, record)
    except Exception as exc:
        return SharedLedgerSync({}, marker_exists, error=f"Shared NAS duplicate history could not be updated: {exc}")
    issue = "" if marker_exists else "Shared NAS duplicate history is not initialized for older archive media."
    return SharedLedgerSync({digest: merged}, marker_exists, exported=int(written), error=issue, already_present=already_present)
