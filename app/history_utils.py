from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, TypeVar

from file_utils import path_key


Record = TypeVar("Record")


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
