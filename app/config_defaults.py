from __future__ import annotations

from typing import Any, Dict, List


DEFAULT_FFMPEG_ARGS: List[str] = [
    "-map", "0:v:0",
    "-map", "0:a:0",
    "-map", "-0:d",
    "-map", "-0:t",
    "-map", "-0:s",
    "-map", "-0:v:m:attached_pic",
    "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease",
    "-c:v", "hevc_nvenc",
    "-preset", "p5",
    "-profile:v", "main",
    "-pix_fmt", "yuv420p",
    "-rc", "vbr",
    "-cq:v", "22",
    "-b:v", "0",
    "-maxrate", "12M",
    "-bufsize", "24M",
    "-tag:v", "hvc1",
    "-color_primaries", "bt709",
    "-color_trc", "bt709",
    "-colorspace", "bt709",
    "-bf", "3",
    "-rc-lookahead", "32",
    "-spatial-aq", "1",
    "-temporal-aq", "1",
    "-g", "120",
    "-c:a", "aac",
    "-b:a", "192k",
    "-ac", "2",
    "-ar", "48000",
    "-map_metadata", "-1",
    "-map_chapters", "-1",
    "-movflags", "+faststart",
    "-f", "mp4",
]

COMMON_VIDEO_EXTENSIONS = [
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".flv", ".wmv", ".webm", ".mts", ".m2ts", ".mpg", ".mpeg"
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "input_folder": "",
    "output_folder": "",
    "nas_folder": "",
    "ffmpeg_path": "",
    "ffmpeg_args": DEFAULT_FFMPEG_ARGS,
    "ignored_drives": ["C:"],
    "import_extensions": [".mp4", ".mov"],
    "conversion_extensions": [".mp4", ".mkv", ".avi", ".mov", ".m4v", ".flv", ".wmv", ".webm"],
    "exclude_files": ["._*", ".DS_Store", "._.DS_Store"],
    "exclude_folders": ["__MACOSX", ".TemporaryItems", ".Trashes"],
    "auto_import_delay_seconds": 5,
    "scan_interval_seconds": 3,
    "enable_duplicate_detection": True,
    "checksum_database_path": "",
    "check_existing_in_input": True,
    "file_stable_seconds": 10,
    "copy_chunk_mb": 4,
    "max_ready_wait_seconds": 7200,
    "low_disk_warning_gb": 20,
    "auto_underscore_renames": False,
    "prevent_sleep_while_working": True,
    "compact_dashboard": True,
    "preset_folder": "",
    "default_preset_name": "Current settings",
    "setup_complete": False,
}
