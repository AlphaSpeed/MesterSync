"""Validated, output-only settings for the guided editor."""
from __future__ import annotations

import math
import os
import uuid
from pathlib import Path

from file_utils import sanitize_base_name
from presets import arg_value
from safe_files import promote_temp_no_overwrite

RESOLUTIONS = {"Original size": None, "720p (1280 × 720)": (1280, 720),
               "1080p (1920 × 1080)": (1920, 1080), "4K UHD (3840 × 2160)": (3840, 2160)}
FRAME_RATES = ("Original frame rate", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60")
KEEP = "Keep current"
ENCODERS = {"H.264 · CPU": "libx264", "H.265 / HEVC · CPU": "libx265",
            "H.264 · NVIDIA": "h264_nvenc", "H.265 / HEVC · NVIDIA": "hevc_nvenc"}
CONTAINERS = {"MP4 (.mp4)": "mp4", "MOV (.mov)": "mov", "MKV (.mkv)": "matroska"}
AUDIO_CODECS = {"AAC": "aac", "MP3": "libmp3lame", "AC-3": "ac3"}


def split_filters(text):
    """Split a simple chain without splitting commas in quoted expressions."""
    parts, current, quote, depth, escaped = [], [], "", 0, False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if char in "'\"":
            quote = "" if quote == char else (char if not quote else quote)
        if not quote:
            depth += (char == "(") - (char == ")")
        if char == "," and not quote and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if quote or depth or escaped:
        raise ValueError("This preset has a complex filter. Edit it in Advanced settings instead.")
    if current:
        parts.append("".join(current).strip())
    return parts


def without_options(args, names):
    result, index = [], 0
    while index < len(args):
        option = args[index]
        if option in names:
            if index + 1 >= len(args):
                raise ValueError(f"Missing value for {option} in the existing preset.")
            index += 2
        else:
            result.append(option)
            index += 1
    return result


def apply_video_options(base, resolution=KEEP, fps=KEEP, bitrate=KEEP):
    args = list(base)
    if resolution == fps == bitrate == KEEP:
        return args
    codec = arg_value(args, "-c:v", "-vcodec")
    if codec not in ENCODERS.values():
        raise ValueError("Use an H.264 or HEVC encoding preset with this editor. Other encoders remain available in Advanced.")
    # Avoid silently overriding advanced stream-specific or complex settings.
    if any(arg.startswith(("-filter_complex", "-filter:v:", "-c:v:", "-b:v:", "-r:v:", "-s:v:")) for arg in args):
        raise ValueError("This preset uses advanced stream settings. Edit those in Advanced instead.")
    if resolution != KEEP or fps != KEEP:
        filter_values = [args[index + 1] for index, arg in enumerate(args[:-1]) if arg in {"-vf", "-filter:v"}]
        if len(filter_values) > 1:
            raise ValueError("This preset has multiple video filter chains. Edit them in Advanced instead.")
        filters = split_filters(filter_values[0]) if filter_values else []
        if any(";" in item or "[" in item or "]" in item for item in filters):
            raise ValueError("This preset has a complex filter chain. Edit it in Advanced instead.")
        if resolution != KEEP:
            if resolution not in RESOLUTIONS:
                raise ValueError("Choose a supported resolution.")
            if any(item.split("=", 1)[0].strip().startswith(("scale_", "zscale")) for item in filters):
                raise ValueError("Hardware or HDR scaling must be edited in Advanced.")
            simple = {"scale", "pad", "setsar", "fps", "yadif", "bwdif", "format"}
            if any(item.split("=", 1)[0].strip() not in simple for item in filters):
                raise ValueError("This preset has additional image filters. Change its resolution in Advanced to preserve their order.")
            filters = [item for item in filters if item.split("=", 1)[0].strip() not in {"scale", "pad", "setsar"}]
            size = RESOLUTIONS[resolution]
            if size:
                width, height = size
                filters.extend(["scale=trunc(iw*sar/2)*2:ih", "setsar=1",
                                f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2", "setsar=1"])
            else:
                filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
            args = without_options(args, {"-s", "-s:v", "-aspect", "-aspect:v"})
        if fps != KEEP:
            if fps not in FRAME_RATES:
                raise ValueError("Choose a supported frame rate.")
            filters = [item for item in filters if item.split("=", 1)[0].strip() != "fps"]
            args = without_options(args, {"-r", "-r:v", "-fpsmax", "-fpsmax:v", "-vsync", "-fps_mode", "-fps_mode:v"})
            if fps != "Original frame rate":
                args += ["-r", fps]
        args = without_options(args, {"-vf", "-filter:v"})
        if filters:
            args += ["-vf", ",".join(filters)]
    if bitrate != KEEP:
        try:
            mbps = float(bitrate)
        except (TypeError, ValueError):
            raise ValueError("Enter a target bitrate in Mbps, for example 30.") from None
        if not math.isfinite(mbps) or not 1 <= mbps <= 200:
            raise ValueError("Target bitrate must be between 1 and 200 Mbps.")
        if any(arg in args for arg in ("-x264-params", "-x264opts", "-x265-params")):
            raise ValueError("This preset has custom encoder parameters. Edit bitrate in Advanced instead.")
        args = without_options(args, {"-b:v", "-vb", "-minrate", "-minrate:v", "-maxrate", "-maxrate:v",
                                      "-bufsize", "-bufsize:v", "-cq", "-cq:v", "-crf", "-crf:v", "-qp", "-qp:v", "-q:v", "-qscale:v", "-rc", "-rc:v"})
        args += ["-b:v", f"{mbps:g}M", "-maxrate", f"{mbps * 1.5:g}M", "-bufsize", f"{mbps * 2:g}M"]
        if codec.endswith("_nvenc"):
            args += ["-rc", "vbr"]
    return args


def apply_media_options(base, container=KEEP, audio=KEEP, audio_kbps=KEEP):
    args = list(base)
    if container != KEEP:
        if container not in CONTAINERS:
            raise ValueError("Choose MP4, MOV or MKV.")
        args = without_options(args, {"-f", "-movflags", "-tag:v"})
        args += ["-f", CONTAINERS[container]]
        if CONTAINERS[container] in {"mp4", "mov"}:
            args += ["-movflags", "+faststart"]
            if arg_value(args, "-c:v", "-vcodec") in {"libx265", "hevc_nvenc"}:
                args += ["-tag:v", "hvc1"]
    if audio != KEEP or audio_kbps != KEEP:
        if any(str(arg).startswith(("-c:a:", "-b:a:", "-filter_complex")) for arg in args):
            raise ValueError("This preset uses advanced audio routing. Edit it in Advanced instead.")
        codec = AUDIO_CODECS.get(audio) if audio != KEEP else arg_value(args, "-c:a", "-acodec")
        if codec not in AUDIO_CODECS.values():
            raise ValueError("Choose an audio format before changing its bitrate.")
        if audio != KEEP:
            args = without_options(args, {"-c:a", "-acodec", "-b:a", "-ab", "-q:a", "-aq"})
            args = [arg for arg in args if arg != "-an"]
            args += ["-c:a", codec, "-b:a", "192k"]
        if audio_kbps != KEEP:
            try:
                rate = int(audio_kbps)
            except (ValueError, TypeError):
                raise ValueError("Choose an audio bitrate in kbps.") from None
            if rate not in {64, 96, 128, 160, 192, 224, 256, 320}:
                raise ValueError("Choose an audio bitrate between 64 and 320 kbps from the list.")
            args = without_options(args, {"-b:a", "-ab", "-q:a", "-aq"})
            args += ["-b:a", f"{rate}k"]
        if codec == "libmp3lame":
            args = without_options(args, {"-ac", "-ar"}) + ["-ac", "2", "-ar", "48000"]
    return args


def estimated_output_bytes(args, duration):
    """Target-rate estimate; unknown quality/copy rates must not look exact."""
    import re
    def rate(value):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmMgG]?)", value or "")
        return float(match[1]) * {"": 1, "k": 1000, "m": 1000000, "g": 1000000000}[match[2].lower()] if match else None
    video = rate(arg_value(list(args), "-b:v", "-vb"))
    audio = rate(arg_value(list(args), "-b:a", "-ab"))
    if not duration or not video or not audio or arg_value(list(args), "-c:a", "-acodec") == "copy":
        return None
    return int(float(duration) * (video + audio) / 8)


def new_preset_options(encoder, resolution, fps, bitrate):
    if encoder not in ENCODERS:
        raise ValueError("Choose an encoder.")
    codec = ENCODERS[encoder]
    args = ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", codec,
            "-preset", "p5" if codec.endswith("_nvenc") else "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000", "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", "-f", "mp4"]
    if codec in {"libx265", "hevc_nvenc"}:
        args += ["-tag:v", "hvc1"]
    return apply_video_options(args, resolution, fps, bitrate)


def save_new_preset(folder: Path, name: str, args):
    clean = sanitize_base_name(name.strip(), False)
    if not clean or clean != name.strip() or clean.lower() == "current settings" or any(ord(char) < 32 for char in clean) or len(clean) > 80:
        raise ValueError("Choose a preset name without path separators or special filename characters.")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{clean}.txt"
    temporary = folder / f".mestersync_preset_{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write("\n".join(args) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        errors = []
        if not promote_temp_no_overwrite(temporary, target, errors.append):
            if target.exists():
                raise FileExistsError("A preset with this name already exists. Choose another name.")
            raise OSError(errors[-1] if errors else "The preset folder could not be written.")
    finally:
        temporary.unlink(missing_ok=True)
    return target
