from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from file_utils import format_size, free_space_bytes, no_window_flags, validate_writable_folder
from media_validation import find_ffprobe


def folder_space_message(label: str, folder: Path) -> str:
    free = free_space_bytes(folder)
    if free is None:
        return f"WARNING {label}: could not read free space for {folder}"
    return f"OK {label}: {format_size(free)} free"


def check_folder_health(cfg: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    folders = [
        ("input_folder", "Importfolder", True),
        ("output_folder", "Output folder", True),
        ("nas_folder", "NAS folder", False),
    ]
    for key, label, required in folders:
        raw = str(cfg.get(key, "")).strip()
        if not raw:
            if required:
                lines.append(f"ERROR {label} is not configured.")
            else:
                lines.append(f"INFO {label} is optional and not configured.")
            continue
        folder = Path(raw)
        error = validate_writable_folder(raw, label, required=required)
        if error:
            lines.append(f"ERROR {error}")
            continue
        lines.append(f"OK {label} exists and is writable: {folder}")
        lines.append(folder_space_message(label, folder))
    return lines


def parse_video_encoders(text: str) -> Set[str]:
    return set(re.findall(r"^\s*[A-Z.]{6}\s+(\S+)", text, flags=re.MULTILINE))


def query_video_encoders(ffmpeg: Path) -> Set[str]:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=no_window_flags(),
    )
    text = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"FFmpeg exited with code {result.returncode}").strip()
        raise RuntimeError(detail)
    return parse_video_encoders(text)


def requested_video_encoders(ffmpeg_args: object) -> List[str]:
    args_text = " ".join(str(value) for value in ffmpeg_args) if isinstance(ffmpeg_args, list) else ""
    return re.findall(r"-c:v\s+(\S+)", args_text)


def check_ffmpeg_health(cfg: Dict[str, Any]) -> Tuple[List[str], Optional[Set[str]]]:
    lines: List[str] = []
    ffmpeg_value = str(cfg.get("ffmpeg_path", "")).strip()
    if not ffmpeg_value:
        return ["ERROR FFmpeg.exe is not configured."], None
    ffmpeg = Path(ffmpeg_value)
    if not ffmpeg.is_file():
        return [f"ERROR FFmpeg not found: {ffmpeg}"], None
    lines.append(f"OK FFmpeg found: {ffmpeg}")
    ffprobe = find_ffprobe(ffmpeg)
    if ffprobe is None:
        lines.append("WARNING FFprobe was not found beside FFmpeg or on PATH.")
    else:
        lines.append(f"OK FFprobe found: {ffprobe}")
    try:
        encoders = query_video_encoders(ffmpeg)
        wanted = requested_video_encoders(cfg.get("ffmpeg_args", [])) or ["hevc_nvenc"]
        for encoder in wanted:
            if encoder in encoders:
                lines.append(f"OK video encoder available: {encoder}")
            else:
                lines.append(f"WARNING video encoder not listed by FFmpeg: {encoder}")
        return lines, encoders
    except Exception as exc:
        lines.append(f"WARNING could not query FFmpeg encoders: {exc}")
        return lines, None


def run_diagnostics(cfg: Dict[str, Any]) -> Tuple[List[str], Optional[Set[str]]]:
    lines = check_folder_health(cfg)
    ffmpeg_lines, encoders = check_ffmpeg_health(cfg)
    lines.extend(ffmpeg_lines)
    return lines, encoders
