from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from file_utils import path_key, sha256_file


def checksum_record_path(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("final_path") or value.get("path") or value.get("imported_path") or "")
    return str(value or "")


def checksum_db_has_same_name_and_size(path: Path, checksum_db: Dict[str, Any]) -> bool:
    try:
        source_size = path.stat().st_size
    except OSError:
        return False
    for value in checksum_db.values():
        if isinstance(value, dict):
            try:
                if str(value.get("original_name") or "").lower() == path.name.lower() and int(value.get("original_size") or -1) == source_size:
                    return True
            except (TypeError, ValueError):
                pass
        try:
            saved_path = checksum_record_path(value)
            candidate = Path(saved_path)
            if candidate.exists() and candidate.name.lower() == path.name.lower() and candidate.stat().st_size == source_size:
                return True
        except OSError:
            continue
    return False


def exact_duplicate_reason(
    path: Path,
    cfg: Dict[str, object],
    checksum_db: Dict[str, Any],
    stop_event: threading.Event,
    progress_callback: Optional[Callable[[str, int, int, int], None]] = None,
) -> str:
    try:
        source_size = path.stat().st_size
    except OSError:
        source_size = None

    digest = None
    def hash_file(candidate: Path, phase: str) -> Optional[str]:
        callback = None
        if progress_callback:
            callback = lambda percent, processed, total: progress_callback(phase, percent, processed, total)
        return sha256_file(candidate, stop_event, callback)

    # Full-file hashing is intentionally rare. Large camera files can be tens of
    # GB, so only pay that cost when a same-name/same-size candidate exists.
    if checksum_db and checksum_db_has_same_name_and_size(path, checksum_db):
        digest = hash_file(path, "Checking exact duplicate")
    if digest and digest in checksum_db:
        saved_path = checksum_record_path(checksum_db[digest])
        return f"Checksum already imported as: {saved_path or 'a previous MesterSync import'}"

    if bool(cfg.get("check_existing_in_input", True)):
        existing = Path(str(cfg.get("input_folder", ""))) / path.name
        if existing.exists() and path_key(existing) != path_key(path):
            try:
                existing_size = existing.stat().st_size
            except OSError:
                existing_size = None
            if source_size is not None and existing_size is not None and source_size != existing_size:
                return ""
            if digest is None:
                digest = hash_file(path, "Checking exact duplicate")
            existing_digest = hash_file(existing, "Comparing importfolder copy")
            if digest and existing_digest and digest == existing_digest:
                return f"Exact same video already exists in importfolder: {existing}"
    return ""


def remove_missing_checksum_entries(checksum_db: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Remove malformed records while retaining fingerprints for moved media.

    A final NAS path is only the last known location. It must not control whether
    the content fingerprint remains valid because operators reorganize the NAS.
    """
    kept: Dict[str, Any] = {}
    removed = 0
    for digest, value in checksum_db.items():
        try:
            is_digest = len(str(digest)) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in str(digest))
            valid_value = isinstance(value, dict) or bool(str(value or "").strip())
            if not is_digest or not valid_value:
                removed += 1
                continue
            kept[digest] = value
        except Exception:
            removed += 1
    return kept, removed
