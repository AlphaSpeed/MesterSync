from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional, Set, Tuple

from file_utils import path_key, sanitize_base_name


NameSnapshot = Iterable[Tuple[str, str]]


def reserve_unique_path(desired: Path, reserved: Set[str], lock: Any) -> Path:
    """Reserve a non-existing path without colliding with another active job."""
    with lock:
        candidate = desired
        counter = 1
        while candidate.exists() or path_key(candidate) in reserved:
            candidate = desired.parent / f"{desired.stem}_{counter}{desired.suffix}"
            counter += 1
        reserved.add(path_key(candidate))
        return candidate


def release_reserved_path(target: Optional[Path], reserved: Set[str], lock: Any) -> None:
    if target is None:
        return
    with lock:
        reserved.discard(path_key(target))


def next_final_name(
    rename_base: str,
    display_name: str,
    output_ext: str,
    auto_underscores: bool,
    existing_names: NameSnapshot,
    reserved_paths: Iterable[str],
    own_output: Optional[Path] = None,
) -> str:
    """Choose the next shared output/NAS name for one recording."""
    display_path = Path(display_name)
    base = sanitize_base_name(rename_base or display_path.stem, auto_underscores) or display_path.stem
    ext = output_ext or ".mp4"
    pattern = re.compile(rf"^{re.escape(base)}(?:_(\d+))?{re.escape(ext)}$", re.IGNORECASE)
    highest: Optional[int] = None

    def inspect_name(name: str) -> None:
        nonlocal highest
        match = pattern.match(name)
        if not match:
            return
        number = int(match.group(1)) if match.group(1) else 0
        highest = number if highest is None else max(highest, number)

    own_output_key = path_key(own_output) if own_output else ""
    for file_key, name in existing_names:
        if own_output_key and file_key == own_output_key:
            continue
        inspect_name(name)
    for reserved in reserved_paths:
        if own_output_key and reserved == own_output_key:
            continue
        inspect_name(Path(reserved).name)
    return f"{base}{ext}" if highest is None else f"{base}_{highest + 1}{ext}"


def reserve_final_target(
    folder: Path,
    rename_base: str,
    display_name: str,
    output_ext: str,
    auto_underscores: bool,
    existing_names: NameSnapshot,
    output_reserved: Set[str],
    nas_reserved: Set[str],
    target_reserved: Set[str],
    lock: Any,
    own_output: Optional[Path] = None,
) -> Path:
    """Atomically select and reserve a name shared by output and NAS locations."""
    names = list(existing_names)
    with lock:
        while True:
            name = next_final_name(
                rename_base,
                display_name,
                output_ext,
                auto_underscores,
                names,
                list(output_reserved) + list(nas_reserved),
                own_output,
            )
            candidate = folder / name
            candidate_key = path_key(candidate)
            own_output_key = path_key(own_output) if own_output else ""
            is_own_output = bool(own_output_key and candidate_key == own_output_key)
            reserved_elsewhere = (
                candidate_key in output_reserved or candidate_key in nas_reserved
            ) and not is_own_output
            if (not candidate.exists() or is_own_output) and not reserved_elsewhere:
                target_reserved.add(candidate_key)
                return candidate
            # The folder may have changed since its snapshot was collected.
            # Include the late collision and calculate the following suffix.
            names.append((candidate_key, candidate.name))
