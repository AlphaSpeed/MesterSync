from __future__ import annotations

import re
from typing import Iterable, List

from file_utils import sanitize_base_name


def batch_rename_bases(
    current_bases: Iterable[str],
    batch_text: str,
    auto_underscores: bool,
    prefix: bool = False,
    suffix: bool = False,
) -> List[str]:
    """Build sanitized batch names while preserving the existing affix behavior."""
    prepared_text = re.sub(r"\s+", "_", batch_text) if auto_underscores else batch_text
    text = sanitize_base_name(prepared_text, auto_underscores)
    if not text:
        return []
    results: List[str] = []
    for index, current_value in enumerate(current_bases, start=1):
        current = sanitize_base_name(current_value, auto_underscores) or current_value
        if prefix:
            new_base = f"{text}{current}"
        elif suffix:
            new_base = f"{current}{text}"
        else:
            new_base = f"{text}_{index:02d}"
        results.append(sanitize_base_name(new_base, auto_underscores) or current)
    return results
