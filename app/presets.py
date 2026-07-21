from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple


def detect_output_extension(ffmpeg_args: Iterable[str]) -> str:
    args = list(ffmpeg_args)
    fmt_map = {"mp4": ".mp4", "mov": ".mov", "matroska": ".mkv", "avi": ".avi", "webm": ".webm"}
    for i, arg in enumerate(args[:-1]):
        if arg == "-f":
            return fmt_map.get(args[i + 1].strip().lower(), f".{args[i + 1].strip().lower().lstrip('.')}")
    return ".mp4"


def arg_value(args: List[str], *names: str) -> Optional[str]:
    lowered = [str(x).lower() for x in args]
    for name in names:
        name = name.lower()
        if name in lowered:
            idx = lowered.index(name)
            if idx + 1 < len(args):
                return str(args[idx + 1])
    return None


def analyze_preset_args(ffmpeg_args: Iterable[str]) -> List[str]:
    args = [str(arg).strip() for arg in ffmpeg_args if str(arg).strip()]
    lowered = [arg.lower() for arg in args]
    warnings: List[str] = []
    if not any(arg.startswith("0:a") or arg == "0:a:0" for idx, arg in enumerate(lowered) if idx > 0 and lowered[idx - 1] == "-map"):
        warnings.append("Audio may be missing: no '-map 0:a...' entry found.")
    if "-c:a" not in lowered and "-acodec" not in lowered:
        warnings.append("Audio codec is not set; FFmpeg may choose a default.")
    video_codec = (arg_value(args, "-c:v", "-vcodec") or "").lower()
    if not video_codec:
        warnings.append("Video codec is not set; FFmpeg may choose a large/default output.")
    if video_codec in {"copy", "rawvideo", "ffv1", "prores", "prores_ks", "dnxhd", "dnxhr"}:
        warnings.append(f"Video codec '{video_codec}' can create very large files or skip compression.")
    fmt = (arg_value(args, "-f") or "").lower()
    if fmt in {"avi", "matroska", "mov"}:
        warnings.append(f"Output format '{fmt}' can be larger or less compatible than mp4 for this workflow.")
    bitrate = (arg_value(args, "-b:v") or "").lower()
    if bitrate and bitrate not in {"0", "0k"}:
        match = re.match(r"(\d+(?:\.\d+)?)([kmg]?)", bitrate)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            mbps = value / 1000 if unit == "k" else value * 1000 if unit == "g" else value
            if unit in {"m", "g"} and mbps >= 50:
                warnings.append(f"Video bitrate '{bitrate}' is high and may create large files.")
    cq = arg_value(args, "-cq:v", "-cq")
    if cq:
        try:
            if float(cq) < 16:
                warnings.append(f"Quality value '{cq}' is very high quality and may create larger files.")
        except ValueError:
            pass
    if "-map_metadata" not in lowered:
        warnings.append("Metadata is not stripped; private/camera metadata may remain.")
    return warnings


def preset_badges(name: str, ffmpeg_args: Iterable[str]) -> List[Tuple[str, str]]:
    args = [str(arg).strip() for arg in ffmpeg_args if str(arg).strip()]
    video = (arg_value(args, "-c:v", "-vcodec") or "auto").upper()
    audio = (arg_value(args, "-c:a", "-acodec") or "auto").upper()
    output = detect_output_extension(args).lstrip(".").upper()
    vf = arg_value(args, "-vf") or ""
    size = "SCALE" if "scale" in vf.lower() else "ORIGINAL SIZE"
    warnings = analyze_preset_args(args)
    safety = "SAFETY OK" if not warnings else f"{len(warnings)} WARNING"
    return [
        (name or "Current settings", "#2f6fed"),
        (video, "#1f8a5b"),
        (audio, "#805ad5"),
        (output, "#b7791f"),
        (size, "#4a5568"),
        (safety, "#1f8a5b" if not warnings else "#c05621"),
    ]
