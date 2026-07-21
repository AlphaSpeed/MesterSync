from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from file_utils import format_duration, no_window_flags


MediaInfo = Dict[str, Any]


def find_ffprobe(ffmpeg_path: Path) -> Optional[Path]:
    """Find FFprobe beside FFmpeg first, then on PATH."""
    executable = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    beside_ffmpeg = Path(ffmpeg_path).parent / executable
    if beside_ffmpeg.exists():
        return beside_ffmpeg
    found = shutil.which("ffprobe")
    return Path(found) if found else None


def get_duration(input_path: Path, ffmpeg_path: Path) -> Optional[float]:
    ffprobe = find_ffprobe(ffmpeg_path)
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(input_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=no_window_flags(),
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def ffprobe_media_info(path: Path, ffmpeg_path: Path) -> Tuple[Optional[MediaInfo], str]:
    ffprobe = find_ffprobe(ffmpeg_path)
    if ffprobe is None:
        return None, "FFprobe is required for post-conversion safety checks."
    try:
        result = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration,size:stream=codec_type,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=no_window_flags(),
        )
        if result.returncode != 0:
            return None, (result.stderr or "FFprobe could not read the converted file.").strip()
        info = json.loads(result.stdout or "{}")
        return (info, "") if isinstance(info, dict) else (None, "")
    except Exception as exc:
        return None, f"FFprobe validation failed: {exc}"


def _streams(info: Optional[MediaInfo]) -> List[MediaInfo]:
    if not info or not isinstance(info.get("streams"), list):
        return []
    return [stream for stream in info["streams"] if isinstance(stream, dict)]


def validate_media_info(
    output_info: MediaInfo,
    source_info: Optional[MediaInfo],
    ffmpeg_args: List[str],
    expected_duration: Optional[float],
) -> Optional[str]:
    streams = _streams(output_info)
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not video_streams:
        return "Converted output contains no video stream."
    try:
        invalid_video_size = any(
            int(stream.get("width") or 0) <= 0 or int(stream.get("height") or 0) <= 0
            for stream in video_streams
        )
    except (TypeError, ValueError):
        invalid_video_size = True
    if invalid_video_size:
        return "Converted output has an invalid video size."
    try:
        output_duration = float((output_info.get("format") or {}).get("duration") or 0)
    except (AttributeError, TypeError, ValueError):
        output_duration = 0
    if output_duration <= 0:
        return "Converted output has no readable duration."
    if expected_duration and expected_duration > 1:
        tolerance = max(5.0, expected_duration * 0.02)
        if abs(output_duration - expected_duration) > tolerance:
            return f"Converted duration differs from the source by more than {format_duration(tolerance)}."

    source_has_audio = any(stream.get("codec_type") == "audio" for stream in _streams(source_info))
    audio_disabled = "-an" in [str(arg).lower() for arg in ffmpeg_args]
    output_has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    if source_has_audio and not audio_disabled and not output_has_audio:
        return "Source audio exists, but the converted output contains no audio stream."
    return None


def validate_converted_output(
    output_path: Path,
    source_path: Path,
    ffmpeg_path: Path,
    ffmpeg_args: List[str],
    expected_duration: Optional[float],
) -> Optional[str]:
    if not output_path.exists() or output_path.stat().st_size <= 0:
        return "Converted output is missing or empty."
    output_info, output_error = ffprobe_media_info(output_path, ffmpeg_path)
    if not output_info:
        return output_error or "Converted output could not be validated."
    source_info, _source_error = ffprobe_media_info(source_path, ffmpeg_path)
    return validate_media_info(output_info, source_info, ffmpeg_args, expected_duration)
