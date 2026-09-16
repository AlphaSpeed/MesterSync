from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from file_utils import path_key, sha256_file


QUICK_SAMPLE_BYTES = 2 * 1024 * 1024
QUICK_FULL_FILE_LIMIT = QUICK_SAMPLE_BYTES * 3
FINGERPRINT_CACHE_LIMIT = 4096
UNCERTAIN_DUPLICATE_REASON = "Duplicate safety check could not read the complete file. Import paused for review."


def _file_signature(path: Path) -> Optional[Tuple[int, int, int, int, int]]:
    try:
        stat = path.stat()
        return (
            int(stat.st_dev),
            int(stat.st_ino),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
        )
    except OSError:
        return None


def _valid_fingerprint(value: Any) -> str:
    text = str(value or "").strip().lower()
    if len(text) == 64 and all(ch in "0123456789abcdef" for ch in text):
        return text
    return ""


def quick_fingerprint(path: Path, stop_event: Optional[threading.Event] = None) -> Optional[str]:
    """Hash a few fixed regions as a fast, one-way duplicate prefilter.

    A mismatch proves the files differ. A match is never accepted as proof of a
    duplicate; it only decides whether the full SHA-256 comparison is needed.
    """
    import hashlib

    initial = _file_signature(path)
    if initial is None:
        return None
    total = initial[2]
    hasher = hashlib.sha256()
    hasher.update(b"MesterSyncQuickFingerprintV1\0")
    hasher.update(total.to_bytes(16, "little", signed=False))
    try:
        with path.open("rb") as handle:
            if total <= QUICK_FULL_FILE_LIMIT:
                offsets = [0]
                lengths = [total]
            else:
                offsets = [
                    0,
                    max(0, total // 2 - QUICK_SAMPLE_BYTES // 2),
                    max(0, total - QUICK_SAMPLE_BYTES),
                ]
                lengths = [QUICK_SAMPLE_BYTES] * len(offsets)
            for offset, wanted in zip(offsets, lengths):
                if stop_event and stop_event.is_set():
                    return None
                handle.seek(offset)
                data = handle.read(wanted)
                if len(data) != wanted:
                    return None
                hasher.update(offset.to_bytes(16, "little", signed=False))
                hasher.update(len(data).to_bytes(8, "little", signed=False))
                hasher.update(data)
    except OSError:
        return None
    if _file_signature(path) != initial:
        return None
    return hasher.hexdigest()


def _cache_entry(path: Path, cache: Dict[str, Any]) -> Tuple[str, Optional[Tuple[int, int, int, int, int]], Dict[str, Any]]:
    key = path_key(path)
    signature = _file_signature(path)
    entry = cache.get(key)
    if not isinstance(entry, dict) or entry.get("signature") != signature:
        entry = {"signature": signature}
        if key not in cache and len(cache) >= FINGERPRINT_CACHE_LIMIT:
            try:
                cache.pop(next(iter(cache)))
            except (KeyError, StopIteration):
                pass
        cache[key] = entry
    return key, signature, entry


def cached_quick_fingerprint(path: Path, cache: Optional[Dict[str, Any]], stop_event: threading.Event) -> Optional[str]:
    if stop_event.is_set():
        return None
    if cache is None:
        return quick_fingerprint(path, stop_event)
    key, signature, entry = _cache_entry(path, cache)
    if signature is None:
        return None
    remembered = _valid_fingerprint(entry.get("quick"))
    if remembered:
        return remembered
    fingerprint = quick_fingerprint(path, stop_event)
    if _file_signature(path) != signature:
        return None
    if fingerprint:
        entry["quick"] = fingerprint
        cache[key] = entry
    return fingerprint


def cached_sha256_file(
    path: Path,
    cache: Optional[Dict[str, Any]],
    stop_event: threading.Event,
    progress_callback: Optional[Callable[[int, int, int], None]] = None,
) -> Optional[str]:
    if stop_event.is_set():
        return None
    if cache is None:
        key, signature, entry = "", _file_signature(path), {}
    else:
        key, signature, entry = _cache_entry(path, cache)
    if signature is None:
        return None
    remembered = _valid_fingerprint(entry.get("sha256"))
    if remembered:
        return remembered
    digest = sha256_file(path, stop_event, progress_callback)
    if _file_signature(path) != signature:
        return None
    if digest and cache is not None:
        entry["sha256"] = digest
        cache[key] = entry
    return digest


def checksum_record_path(value: Any) -> str:
    if isinstance(value, dict):
        paths = value.get("final_paths")
        if isinstance(paths, list):
            latest = next((str(path).strip() for path in reversed(paths) if str(path).strip()), "")
            if latest:
                return latest
        return str(value.get("final_path") or value.get("path") or value.get("imported_path") or "")
    return str(value or "")


def checksum_db_may_contain_size(path: Path, checksum_db: Dict[str, Any]) -> bool:
    """Return whether a remembered original could have the same byte length.

    Names are deliberately ignored. Different cameras commonly reuse names such
    as ``hyperdeck_02.mp4``, and operators may rename a recording after import.
    Exact content is decided by the full checksum below. Legacy records without
    a saved size are treated as possible matches so they remain protected too.
    """
    try:
        source_size = path.stat().st_size
    except OSError:
        return False
    for value in checksum_db.values():
        if isinstance(value, dict):
            try:
                saved_size = value.get("original_size")
                if saved_size is None or int(saved_size) == source_size:
                    return True
            except (TypeError, ValueError):
                return True
        else:
            # Older checksum databases stored only a path. Their dictionary key
            # is still a valid full-file checksum, so hash the incoming file.
            return True
    return False


def exact_duplicate_reason(
    path: Path,
    cfg: Dict[str, object],
    checksum_db: Dict[str, Any],
    stop_event: threading.Event,
    progress_callback: Optional[Callable[[str, int, int, int], None]] = None,
    fingerprint_cache: Optional[Dict[str, Any]] = None,
) -> str:
    if stop_event.is_set():
        return ""
    initial_signature = _file_signature(path)
    if initial_signature is None:
        return UNCERTAIN_DUPLICATE_REASON
    source_size = initial_signature[2]

    def finish(reason: str = "") -> str:
        # A recording may be replaced or still be growing while the fast filters
        # run. A result for those old bytes cannot clear the current file.
        if _file_signature(path) != initial_signature and not stop_event.is_set():
            return UNCERTAIN_DUPLICATE_REASON
        return reason

    digest: Optional[str] = None
    source_quick: Optional[str] = None
    def hash_file(candidate: Path, phase: str) -> Optional[str]:
        callback = None
        if progress_callback:
            callback = lambda percent, processed, total: progress_callback(phase, percent, processed, total)
        return cached_sha256_file(candidate, fingerprint_cache, stop_event, callback)

    def quick_file(candidate: Path) -> Optional[str]:
        return cached_quick_fingerprint(candidate, fingerprint_cache, stop_event)

    # Equal files must have equal sizes, so size is a safe inexpensive filter.
    # New records also have a small sampled fingerprint. A mismatch safely avoids
    # a full-file read; a match (or a legacy record) is always confirmed by hashing
    # every byte. The filename is never part of either decision.
    possible_quick_fingerprints = set()
    full_hash_required = False
    if checksum_db and source_size is not None:
        for value in checksum_db.values():
            if isinstance(value, dict):
                saved_size = value.get("original_size")
                try:
                    if saved_size is not None and int(saved_size) != source_size:
                        continue
                except (TypeError, ValueError):
                    pass
                remembered_quick = _valid_fingerprint(value.get("quick_fingerprint"))
                if remembered_quick:
                    possible_quick_fingerprints.add(remembered_quick)
                else:
                    full_hash_required = True
            else:
                full_hash_required = True
        if possible_quick_fingerprints:
            source_quick = quick_file(path)
            if not source_quick or source_quick in possible_quick_fingerprints:
                full_hash_required = True
    elif checksum_db and checksum_db_may_contain_size(path, checksum_db):
        full_hash_required = True
    if full_hash_required:
        digest = hash_file(path, "Checking exact duplicate")
        if not digest and not stop_event.is_set():
            return UNCERTAIN_DUPLICATE_REASON
    if digest and digest in checksum_db:
        saved_path = checksum_record_path(checksum_db[digest])
        return finish(f"Checksum already imported as: {saved_path or 'a previous MesterSync import'}")

    input_folder_value = str(cfg.get("input_folder", "")).strip()
    if input_folder_value:
        input_folder = Path(input_folder_value)
        try:
            existing_files = [candidate for candidate in input_folder.rglob("*") if candidate.is_file()]
        except OSError:
            existing_files = []
        for existing in existing_files:
            if stop_event.is_set() or path_key(existing) == path_key(path):
                continue
            try:
                existing_size = existing.stat().st_size
            except OSError:
                continue
            if source_size is None or existing_size != source_size:
                continue
            if source_quick is None:
                source_quick = quick_file(path)
            existing_quick = quick_file(existing)
            if source_quick and existing_quick and source_quick != existing_quick:
                continue
            if digest is None:
                digest = hash_file(path, "Checking exact duplicate")
            if not digest:
                if not stop_event.is_set():
                    return UNCERTAIN_DUPLICATE_REASON
                return ""
            existing_digest = hash_file(existing, "Comparing importfolder copy")
            if not existing_digest:
                if not stop_event.is_set():
                    return UNCERTAIN_DUPLICATE_REASON
                return ""
            if digest and existing_digest and digest == existing_digest:
                return finish(f"Exact same video already exists in importfolder: {existing}")
    return finish()


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
