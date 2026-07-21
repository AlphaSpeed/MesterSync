from __future__ import annotations

import uuid
from pathlib import Path
from typing import List, Tuple

from file_utils import force_delete, path_key, sanitize_base_name, unique_path


def preset_sample_window(duration: float) -> Tuple[float, float]:
    sample_duration = min(15.0, max(0.0, duration))
    return max(0.0, (duration - sample_duration) / 2.0), sample_duration


def preset_test_output_paths(
    folder: Path,
    source: Path,
    preset_name: str,
    extension: str,
) -> Tuple[Path, Path]:
    safe_preset = sanitize_base_name(preset_name, True) or "preset"
    safe_source = sanitize_base_name(source.stem, True) or "video"
    output_path = unique_path(folder / f"{safe_source}_{safe_preset}_15s_test{extension}")
    temp_output = output_path.with_name(f".mestersync_test_{uuid.uuid4().hex}_{output_path.name}")
    return output_path, temp_output


def delete_previous_preset_tests(folder: Path, keep_path: Path) -> Tuple[int, List[str]]:
    """Keep the newly validated test and remove prior completed test outputs."""
    removed = 0
    warnings: List[str] = []
    try:
        files = list(folder.iterdir())
    except OSError as exc:
        return 0, [str(exc)]
    for path in files:
        try:
            if not path.is_file() or path.name.startswith(".mestersync_test_"):
                continue
            if path_key(path) == path_key(keep_path):
                continue
            if force_delete(path):
                removed += 1
            else:
                warnings.append(f"Could not delete previous preset test: {path}")
        except OSError as exc:
            warnings.append(f"Could not inspect previous preset test {path}: {exc}")
    return removed, warnings
