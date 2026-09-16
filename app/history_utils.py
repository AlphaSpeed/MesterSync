from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Any, Dict, List, Optional, Sequence, Set, TypeVar

from file_utils import path_key
from duplicates import cached_quick_fingerprint, quick_fingerprint


Record = TypeVar("Record")


def retained_media_record(path: Path) -> Optional[Dict[str, Any]]:
    """Remember an in-place result without permanently ignoring its filename."""
    try:
        before = path.stat()
        quick = quick_fingerprint(path)
        after = path.stat()
        signature = [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns]
        if not quick or signature != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns]:
            return None
        return {"path": str(path), "signature": signature, "quick_fingerprint": quick}
    except OSError:
        return None


def retained_media_from_history(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    retained: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        for item in record.get("retained_media", []) if isinstance(record.get("retained_media"), list) else []:
            if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].strip():
                retained.setdefault(path_key(item["path"]), dict(item))
    return retained


def retained_media_is_unchanged(path: Path, record: Dict[str, Any], cache: Dict[str, Any], stop: Event) -> bool:
    if stop.is_set() or not isinstance(record, dict) or path_key(path) != path_key(str(record.get("path") or "")):
        return False
    try:
        stat = path.stat()
        signature = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
        if record.get("signature") != signature:
            return False
        quick = cached_quick_fingerprint(path, cache, stop)
        after = path.stat()
        if signature != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns]:
            return False
        return bool(quick and quick == record.get("quick_fingerprint"))
    except OSError:
        return False


def history_page(records: Sequence[Record], limit: int) -> List[Record]:
    return list(records[: max(0, int(limit))])


def skipped_input_path_keys(records: Sequence[Dict[str, Any]]) -> Set[str]:
    """Restore files deliberately skipped from the persisted History records."""
    return {
        path_key(record.get("input_path"))
        for record in records
        if record.get("status") == "skipped" and str(record.get("input_path") or "").strip()
    }


def recorded_input_file(record: Dict[str, Any], fallback_input_folder: str = "") -> Optional[Path]:
    """Resolve a skipped file only when it remains inside its recorded import folder."""
    if record.get("status") != "skipped" or not record.get("input_path"):
        return None
    root_value = str(record.get("input_folder") or fallback_input_folder).strip()
    if not root_value:
        return None
    try:
        root = Path(root_value).resolve()
        candidate = Path(str(record["input_path"])).resolve()
        if candidate == root or root not in candidate.parents or not candidate.is_file():
            return None
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None
