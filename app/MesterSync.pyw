#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
import ctypes
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    DND_FILES = None
    TkinterDnD = None

from config_defaults import COMMON_VIDEO_EXTENSIONS, DEFAULT_CONFIG, DEFAULT_FFMPEG_ARGS
from diagnostics import query_video_encoders, run_diagnostics
from duplicates import exact_duplicate_reason, remove_missing_checksum_entries
from file_utils import (
    ensure_folder,
    file_is_ready,
    force_delete,
    format_duration,
    format_size,
    free_space_bytes,
    list_candidate_drives,
    list_windows_drives,
    media_folder_overlap_errors,
    no_window_flags,
    now_iso,
    now_text,
    open_path,
    parse_csv_list,
    path_key,
    pretty_time,
    sanitize_base_name,
    sha256_file,
    unique_path,
    validate_writable_folder,
)
from importer import collect_import_candidates, scan_drive_importable_videos, scan_watchfolder_candidates
from history_utils import history_page, recorded_input_file, skipped_input_path_keys
from media_validation import (
    ffprobe_media_info as probe_media_info,
    get_duration as probe_duration,
    validate_converted_output as validate_media_output,
)
from output_names import (
    next_final_name,
    release_reserved_path,
    reserve_final_target,
    reserve_unique_path,
)
from presets import analyze_preset_args, arg_value, detect_output_extension, preset_badges
from preset_testing import delete_previous_preset_tests, preset_sample_window, preset_test_output_paths
from rename_utils import batch_rename_bases
from safe_files import (
    copy_file_safely as copy_path_safely,
    move_file_no_overwrite as move_path_no_overwrite,
    promote_temp_no_overwrite as promote_path_no_overwrite,
)
from skip_undo import new_skip_deadline, skip_is_due, skip_undo_button_text, skip_undo_detail
from storage import (
    app_dir,
    config_path,
    converted_history_count,
    default_checksum_path,
    load_checksum_records,
    load_config_data,
    load_history_records,
    load_task_records,
    normalize_config_data,
    install_dir,
    preset_dir,
    preset_test_dir,
    record_to_task,
    resource_path,
    save_checksum_records,
    save_history_records,
    save_task_records,
    task_to_record,
    thumbnail_dir,
    write_json_file,
)
from tasks import (
    STAGE_CHECKING,
    STAGE_COMPLETE,
    STAGE_CONVERTED,
    STAGE_CONVERTED_IN_PLACE,
    STAGE_CONVERTED_OUTPUT,
    STAGE_CONVERTING,
    STAGE_CONVERTING_IN_PLACE,
    STAGE_DETECTED,
    STAGE_DUPLICATE_REVIEW,
    STAGE_ERROR,
    STAGE_IMPORTED,
    STAGE_IMPORTING,
    STAGE_QUEUED,
    STAGE_SKIPPED,
    STAGE_STOPPED,
    STAGE_STOPPING,
    STAGE_TRANSFERRING,
    STAGE_WAITING,
    STAGE_WAITING_TRANSFER,
    TaskState,
)
from task_view import TaskViewSnapshot
from thumbnails import extract_video_thumbnails
from thumbnail_ui import ImageCache, ThumbnailPopup, scale_photo, scrub_index_from_event
from ui_widgets import ChipSelector
from ui_performance import bounded_log_count, compact_notification_text, inertial_scroll_step, virtual_row_window
from worker_utils import wait_for_conversion_task, wait_for_transfer_task
from updater import (
    ReleaseInfo,
    download_installer,
    fetch_latest_release,
    is_newer_version,
    record_update_check,
    should_check_for_updates,
    start_installer,
    verify_installer,
)
from version import APP_VERSION

APP_NAME = "MesterSync"
APP_USER_MODEL_ID = "MesterSync.VideoWorkflow.App"
LOGO_FILENAME = "mestersync_logo.png"
ICON_FILENAME = "mestersync_icon.ico"
ICON_PNG_FILENAME = "mestersync_icon.png"
MAX_IMAGE_CACHE_ITEMS = 160
HISTORY_PAGE_SIZE = 25
TASK_RENDER_INTERVAL_SECONDS = 0.25
DASHBOARD_VIRTUAL_THRESHOLD = 40
DASHBOARD_VIRTUAL_OVERSCAN = 5
HISTORY_RENDER_BATCH_SIZE = 4
LOG_MAX_LINES = 1000
THUMBNAIL_SETTLE_DELAY_SECONDS = 1.25

class MesterSyncApp:
    BG = "#06111f"
    CARD = "#0d1e36"
    CARD2 = "#102642"
    CARD3 = "#18365c"
    BORDER = "#345982"
    TEXT = "#f1f6ff"
    MUTED = "#a9bfdb"
    BLUE = "#3f8fd8"
    GREEN = "#18834c"
    DARK_GREEN = "#082c1c"
    DARK_ORANGE = "#3a1d08"
    DARK_PURPLE = "#24143d"
    INACTIVE_STAGE_BG = "#171b21"
    RED = "#9d2337"
    YELLOW = "#9b7b13"
    PAUSE_BG = "#3b3213"
    PURPLE = "#865bd7"
    ORANGE = "#d87a22"
    HISTORY_YELLOW = "#3a3114"

    ACTIVE_STAGES = {STAGE_CHECKING, STAGE_IMPORTING, STAGE_QUEUED, STAGE_CONVERTING, STAGE_CONVERTING_IN_PLACE, STAGE_WAITING_TRANSFER, STAGE_TRANSFERRING, STAGE_DETECTED}

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("1500x940")
        self.root.minsize(1180, 760)
        self.root.configure(bg=self.BG)
        icon = resource_path(ICON_FILENAME)
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except Exception:
                pass
        icon_png = resource_path(ICON_PNG_FILENAME)
        if icon_png.exists():
            try:
                self.window_icon_img = tk.PhotoImage(file=str(icon_png))
                self.root.iconphoto(True, self.window_icon_img)
            except Exception:
                pass

        self.config_lock = threading.RLock()
        self.task_lock = threading.RLock()
        self.history_lock = threading.RLock()
        self.current_process_lock = threading.Lock()
        self.checksum_lock = threading.RLock()
        self.import_condition = threading.Condition()
        self.queue_condition = threading.Condition()
        self.transfer_condition = threading.Condition()

        self.config = self.load_config()
        self.settings_built = False
        self.settings_building = False
        self.initialize_settings_state()
        self.compact_dashboard = bool(self.config.get("compact_dashboard", True))
        self.history_records: List[Dict[str, Any]] = self.load_history()
        self.checksum_db: Dict[str, Any] = {}
        self.load_checksum_database()

        self.gui_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.import_queue: Deque[str] = deque()
        self.conversion_queue: Deque[str] = deque()
        self.transfer_queue: Deque[str] = deque()
        self.queued_import_ids: set[str] = set()
        self.queued_conversion_ids: set[str] = set()
        self.force_conversion_ids: set[str] = set()
        self.queued_transfer_ids: set[str] = set()
        self.import_job_requires_enabled: Dict[str, bool] = {}
        self.import_job_batch: Dict[str, str] = {}
        self.import_enqueue_after_success: set[str] = set()
        self.import_batches: Dict[str, Dict[str, Any]] = {}

        self.tasks: Dict[str, TaskState] = {}
        self.task_by_path: Dict[str, str] = {}
        self.reserved_import_paths: set[str] = set()
        self.reserved_output_paths: set[str] = set()
        self.reserved_nas_paths: set[str] = set()
        self.skipped_path_keys: set[str] = skipped_input_path_keys(self.history_records)
        self.selected_ids: List[str] = []
        self.selection_anchor_id: Optional[str] = None
        self.saved_settings_snapshot: Optional[Dict[str, Any]] = None
        self.row_widgets: Dict[str, Dict[str, Any]] = {}
        self.deferred_dashboard_task_ids: set[str] = set()
        self.throttled_dashboard_task_ids: set[str] = set()
        self.last_task_render_at: Dict[str, float] = {}
        self.task_event_lock = threading.Lock()
        self.pending_task_events: Dict[str, bool] = {}
        self.dashboard_virtual_range: Tuple[int, int] = (0, 0)
        self.dashboard_virtual_ids: set[str] = set()
        self.dashboard_virtual_total = -1
        self.dashboard_virtual_order: Tuple[str, ...] = ()
        self.dashboard_virtual_refresh_after_id: Optional[str] = None
        self.smooth_scroll_velocity: Dict[int, float] = {}
        self.smooth_scroll_after_ids: Dict[int, str] = {}
        self.smooth_scroll_regions: List[Tuple[tk.Widget, tk.Canvas]] = []
        self.smooth_scroll_bound = False
        self.history_widgets: List[tk.Widget] = []
        self.history_render_after_id: Optional[str] = None
        self.history_render_generation = 0
        self.history_loading_widget: Optional[tk.Widget] = None
        self.current_tab = ""
        self.history_view_dirty = True
        self.history_display_limit = HISTORY_PAGE_SIZE
        self.preset_menu_values_cache: Optional[List[str]] = None
        self.thumbnail_images = ImageCache(MAX_IMAGE_CACHE_ITEMS)
        self.encoder_cache: Dict[str, set[str]] = {}
        self.encoder_queries_pending: set[str] = set()
        self.health_check_running = False
        self.session_stats = {"imported": 0, "converted": 0, "transferred": 0, "skipped": 0, "errors": 0}
        self.session_bytes_saved = 0
        self.session_error_ids: set[str] = set()
        self.session_summary_hide_due: Optional[float] = None
        self.session_summary_hidden_after_idle = False
        self.tasks_save_pending = False
        self.history_save_pending = False
        self.tasks_save_after_id: Optional[str] = None
        self.history_save_after_id: Optional[str] = None
        self.config_save_after_id: Optional[str] = None
        self.settings_autosave_ready = False
        self.settings_ui_refreshing = False
        self.log_line_count = 0
        self.log_entries: Deque[str] = deque(maxlen=LOG_MAX_LINES)
        self.log_text: Optional[scrolledtext.ScrolledText] = None
        self.log_visible = False
        self.stage_card_view_cache: Dict[str, Tuple[str, int, str]] = {}
        self.status_view_cache: Optional[Tuple[Any, ...]] = None
        self.notification_after_id: Optional[str] = None
        self.notification_active = False
        self.notification_queue: Deque[Tuple[str, str, int]] = deque(maxlen=6)
        self.latest_release: Optional[ReleaseInfo] = None
        self.downloaded_update: Optional[Path] = None
        self.update_check_running = False
        self.update_download_running = False

        self.shutdown_event = threading.Event()
        self.pause_event = threading.Event()
        self.started = False
        self.import_enabled = False
        self.conversion_enabled = False
        self.transfer_enabled = False
        self.importing_count = 0
        self.current_import_id: Optional[str] = None
        self.import_cancel_requested = threading.Event()
        self.import_progress_lock = threading.Lock()
        self.watchfolder_scan_lock = threading.Lock()
        self.import_progress_by_task: Dict[str, Tuple[str, int, Optional[float]]] = {}
        self.import_cycle_task_id: Optional[str] = None
        self.import_cycle_last_switch = 0.0
        self.processed_drive_ids: set[str] = set()
        self.current_process: Optional[subprocess.Popen[str]] = None
        self.current_conversion_id: Optional[str] = None
        self.current_transfer_id: Optional[str] = None
        self.current_process_paused = False
        self.sleep_prevention_active = False
        self.import_priority_enabled = False
        self.thumbnail_popup = ThumbnailPopup(self.root, zoom=3)
        self.thumbnail_generation_requested: set[str] = set()
        self.thumbnail_queue: "queue.Queue[Tuple[str, Path, float]]" = queue.Queue()
        self.thumbnail_worker_started = False
        self.thumbnail_display_queue: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self.thumbnail_display_requested: set[str] = set()
        self.thumbnail_display_worker_started = False
        self.history_thumbnail_labels: Dict[str, Tuple[tk.Label, str]] = {}
        self.checksum_queue: "queue.Queue[Path]" = queue.Queue()
        self.checksum_generation_requested: set[str] = set()
        self.checksum_worker_started = False
        self.final_name_snapshot_lock = threading.Lock()
        self.final_name_snapshot: List[Tuple[str, str]] = []
        self.final_name_snapshot_at = 0.0
        self.final_name_snapshot_refreshing = False
        self.preset_test_process_lock = threading.Lock()
        self.preset_test_process: Optional[subprocess.Popen[str]] = None
        self.preset_test_cancel = threading.Event()
        self.preset_test_running = False
        self.preset_test_last_path: Optional[Path] = None

        self.configure_style()
        self.build_gui()
        self.settings_autosave_ready = True
        self.load_pending_tasks()
        self.saved_settings_snapshot = self.current_settings_snapshot()
        # History is intentionally built only when its tab is opened. Large
        # histories otherwise make startup and unrelated navigation feel slow.
        self.history_view_dirty = True
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_keyboard_shortcuts()
        self.ensure_thumbnail_worker_running()
        self.ensure_thumbnail_display_worker_running()
        self.ensure_checksum_worker_running()
        self.root.after(100, self.process_gui_events)
        self.root.after(1000, self.tick)
        self.root.after(3500, self.check_for_updates)
        self.root.after(
            1500,
            lambda: threading.Thread(
                target=self.cleanup_thumbnail_cache,
                kwargs={"notify": False},
                daemon=True,
                name="ThumbnailCleanup",
            ).start(),
        )
        self.log("Ready. Use Add files or Start all.")
        self.update_startup_warning()
        if self.required_startup_settings_missing():
            self.root.after(300, self.run_setup_wizard)

    def bind_keyboard_shortcuts(self) -> None:
        self.keyboard_shortcuts = [
            ("<Delete>", self.delete_selected_tasks),
            ("<Control-a>", self.select_all_tasks),
            ("<Control-A>", self.select_all_tasks),
            ("<space>", self.toggle_pause_shortcut),
            ("<Return>", self.open_selected_task),
            ("r", self.retry_selected_tasks),
            ("R", self.retry_selected_tasks),
            ("<Control-Up>", self.move_selected_to_top),
            ("<Alt-Up>", self.move_selected_priority_up),
            ("<Alt-Down>", self.move_selected_priority_down),
            ("<Up>", self.move_selection_up),
            ("<Down>", self.move_selection_down),
            ("<Shift-Up>", self.extend_selection_up),
            ("<Shift-Down>", self.extend_selection_down),
        ]
        for sequence, callback in self.keyboard_shortcuts:
            self.root.bind_all(sequence, callback, add="+")
            self.root.bind(sequence, callback, add="+")
        for widget in [getattr(self, "dashboard_frame", None), getattr(self, "dashboard_canvas", None), getattr(self, "dashboard_inner", None)]:
            if widget:
                self.bind_shortcuts_to_widget(widget)
        for row in getattr(self, "row_widgets", {}).values():
            for widget in row.values():
                if isinstance(widget, tk.Widget):
                    self.bind_shortcuts_to_widget(widget)

    def bind_shortcuts_to_widget(self, widget: tk.Widget) -> None:
        for sequence, callback in getattr(self, "keyboard_shortcuts", []):
            widget.bind(sequence, callback, add="+")

    # ---------------- configuration ----------------
    def load_config(self) -> Dict[str, Any]:
        return load_config_data(DEFAULT_CONFIG, DEFAULT_FFMPEG_ARGS)

    def get_config(self) -> Dict[str, Any]:
        with self.config_lock:
            return json.loads(json.dumps(self.config))

    def ui_int(self, var: tk.StringVar, default: int, minimum: Optional[int] = None) -> int:
        try:
            value = int((var.get() or "").strip())
        except ValueError:
            value = default
        return max(minimum, value) if minimum is not None else value

    def ui_ffmpeg_args(self) -> List[str]:
        if hasattr(self, "ffmpeg_args_text"):
            return [line.strip() for line in self.ffmpeg_args_text.get("1.0", "end").splitlines() if line.strip()]
        return list(self.config.get("ffmpeg_args", DEFAULT_FFMPEG_ARGS))

    def initialize_settings_state(self) -> None:
        """Create lightweight settings variables without constructing the page."""
        self.input_folder_var = tk.StringVar(value=self.config["input_folder"])
        self.output_folder_var = tk.StringVar(value=self.config["output_folder"])
        self.nas_folder_var = tk.StringVar(value=self.config["nas_folder"])
        self.ffmpeg_path_var = tk.StringVar(value=self.config["ffmpeg_path"])
        self.exclude_files_var = tk.StringVar(value=", ".join(self.config["exclude_files"]))
        self.exclude_folders_var = tk.StringVar(value=", ".join(self.config["exclude_folders"]))
        self.auto_delay_var = tk.StringVar(value=str(self.config["auto_import_delay_seconds"]))
        self.scan_interval_var = tk.StringVar(value=str(self.config["scan_interval_seconds"]))
        self.stable_seconds_var = tk.StringVar(value=str(self.config["file_stable_seconds"]))
        self.copy_chunk_var = tk.StringVar(value=str(self.config["copy_chunk_mb"]))
        self.max_wait_var = tk.StringVar(value=str(self.config["max_ready_wait_seconds"]))
        self.low_disk_warning_var = tk.StringVar(value=str(self.config.get("low_disk_warning_gb", 20)))
        self.checksum_path_var = tk.StringVar(value=self.config["checksum_database_path"])
        self.duplicate_var = tk.BooleanVar(value=bool(self.config["enable_duplicate_detection"]))
        self.check_input_var = tk.BooleanVar(value=bool(self.config["check_existing_in_input"]))
        self.auto_underscore_var = tk.BooleanVar(value=bool(self.config.get("auto_underscore_renames", False)))
        self.prevent_sleep_var = tk.BooleanVar(value=bool(self.config.get("prevent_sleep_while_working", True)))
        self.preset_folder_var = tk.StringVar(value=self.config.get("preset_folder") or str(preset_dir()))

    def set_config_from_ui(self) -> None:
        with self.config_lock:
            self.config["input_folder"] = self.input_folder_var.get().strip()
            self.config["output_folder"] = self.output_folder_var.get().strip()
            self.config["nas_folder"] = self.nas_folder_var.get().strip()
            self.config["ffmpeg_path"] = self.ffmpeg_path_var.get().strip()
            if self.settings_built:
                self.config["ignored_drives"] = self.ignored_drives_widget.get()
                self.config["import_extensions"] = self.import_ext_widget.get()
                self.config["conversion_extensions"] = self.convert_ext_widget.get()
            self.config["exclude_files"] = parse_csv_list(self.exclude_files_var.get())
            self.config["exclude_folders"] = parse_csv_list(self.exclude_folders_var.get())
            self.config["auto_import_delay_seconds"] = self.ui_int(self.auto_delay_var, 5)
            self.config["scan_interval_seconds"] = self.ui_int(self.scan_interval_var, 3, minimum=1)
            self.config["enable_duplicate_detection"] = bool(self.duplicate_var.get())
            self.config["checksum_database_path"] = self.checksum_path_var.get().strip()
            self.config["check_existing_in_input"] = bool(self.check_input_var.get())
            self.config["file_stable_seconds"] = self.ui_int(self.stable_seconds_var, 10, minimum=1)
            self.config["copy_chunk_mb"] = self.ui_int(self.copy_chunk_var, 4, minimum=1)
            self.config["max_ready_wait_seconds"] = self.ui_int(self.max_wait_var, 7200, minimum=60)
            self.config["low_disk_warning_gb"] = self.ui_int(self.low_disk_warning_var, 20, minimum=0)
            self.config["auto_underscore_renames"] = bool(self.auto_underscore_var.get())
            self.config["prevent_sleep_while_working"] = bool(self.prevent_sleep_var.get())
            self.config["compact_dashboard"] = bool(self.compact_dashboard)
            if hasattr(self, "preset_folder_var"):
                preset_folder_value = self.preset_folder_var.get().strip()
                if preset_folder_value != str(self.config.get("preset_folder", "")):
                    self.preset_menu_values_cache = None
                self.config["preset_folder"] = preset_folder_value
            if hasattr(self, "top_preset_var"):
                self.config["default_preset_name"] = self.top_preset_var.get() or "Current settings"
            self.config["ffmpeg_args"] = self.ui_ffmpeg_args()
            self.config["setup_complete"] = True

    def current_settings_snapshot(self) -> Dict[str, Any]:
        """Read settings directly from the UI without saving them."""
        try:
            return {
                "input_folder": self.input_folder_var.get().strip(),
                "output_folder": self.output_folder_var.get().strip(),
                "nas_folder": self.nas_folder_var.get().strip(),
                "ffmpeg_path": self.ffmpeg_path_var.get().strip(),
                "ignored_drives": self.ignored_drives_widget.get() if self.settings_built else list(self.config.get("ignored_drives", [])),
                "import_extensions": self.import_ext_widget.get() if self.settings_built else list(self.config.get("import_extensions", [])),
                "conversion_extensions": self.convert_ext_widget.get() if self.settings_built else list(self.config.get("conversion_extensions", [])),
                "exclude_files": parse_csv_list(self.exclude_files_var.get()),
                "exclude_folders": parse_csv_list(self.exclude_folders_var.get()),
                "auto_import_delay_seconds": self.ui_int(self.auto_delay_var, 5),
                "scan_interval_seconds": self.ui_int(self.scan_interval_var, 3, minimum=1),
                "enable_duplicate_detection": bool(self.duplicate_var.get()),
                "checksum_database_path": self.checksum_path_var.get().strip(),
                "check_existing_in_input": bool(self.check_input_var.get()),
                "file_stable_seconds": self.ui_int(self.stable_seconds_var, 10, minimum=1),
                "copy_chunk_mb": self.ui_int(self.copy_chunk_var, 4, minimum=1),
                "max_ready_wait_seconds": self.ui_int(self.max_wait_var, 7200, minimum=60),
                "low_disk_warning_gb": self.ui_int(self.low_disk_warning_var, 20, minimum=0) if hasattr(self, "low_disk_warning_var") else int(self.config.get("low_disk_warning_gb", 20)),
                "auto_underscore_renames": bool(self.auto_underscore_var.get()),
                "prevent_sleep_while_working": bool(self.prevent_sleep_var.get()) if hasattr(self, "prevent_sleep_var") else bool(self.config.get("prevent_sleep_while_working", True)),
                "compact_dashboard": bool(self.compact_dashboard),
                "preset_folder": self.preset_folder_var.get().strip() if hasattr(self, "preset_folder_var") else str(self.get_preset_folder()),
                "default_preset_name": self.top_preset_var.get() if hasattr(self, "top_preset_var") else self.config.get("default_preset_name", "Current settings"),
                "ffmpeg_args": self.ui_ffmpeg_args(),
            }
        except Exception:
            return {}

    def update_unsaved_settings_warning(self) -> None:
        if not hasattr(self, "unsaved_settings_var"):
            return
        if self.saved_settings_snapshot is None:
            self.unsaved_settings_var.set("")
            return
        self.unsaved_settings_var.set("Warning: Settings not saved" if self.current_settings_snapshot() != self.saved_settings_snapshot else "")

    def missing_required_settings(self) -> List[str]:
        missing = []
        for key, label in [("input_folder", "Importfolder"), ("output_folder", "Output folder")]:
            if not str(self.config.get(key, "")).strip():
                missing.append(label)
        ffmpeg = str(self.config.get("ffmpeg_path", "")).strip()
        if not ffmpeg or not Path(ffmpeg).is_file():
            missing.append("FFmpeg.exe")
        return missing

    def required_startup_settings_missing(self) -> bool:
        return (not self.config.get("setup_complete")) or bool(self.missing_required_settings())

    def update_startup_warning(self) -> None:
        if not hasattr(self, "startup_warning_var"):
            return
        missing = self.missing_required_settings()
        if missing:
            self.startup_warning_var.set("Setup needed: choose " + ", ".join(missing) + " before starting.")
            if not self.startup_warning_frame.winfo_ismapped():
                self.startup_warning_frame.pack(side="left", padx=(12, 0))
        else:
            self.startup_warning_var.set("")
            if self.startup_warning_frame.winfo_ismapped():
                self.startup_warning_frame.pack_forget()

    def save_config(self) -> None:
        try:
            if self.config_save_after_id is not None:
                self.root.after_cancel(self.config_save_after_id)
                self.config_save_after_id = None
            self.set_config_from_ui()
            warnings = self.preset_safety_warnings(self.config.get("ffmpeg_args", []), check_encoder=True)
            self.update_preset_status(check_encoder=True)
            if warnings:
                text = "\n".join(f"- {warning}" for warning in warnings)
                if not messagebox.askyesno(APP_NAME, f"Preset safety warnings:\n\n{text}\n\nSave these settings anyway?"):
                    return
            write_json_file(config_path(), self.config)
            self.save_current_preset_default()
            self.load_checksum_database()
            self.processed_drive_ids.clear()
            self.saved_settings_snapshot = self.current_settings_snapshot()
            self.update_unsaved_settings_warning()
            self.update_startup_warning()
            self.log("Settings saved.")
            self.show_notification("Settings saved.", "success")
        except Exception as exc:
            self.show_notification(f"Could not save settings:\n{exc}", "error")

    def schedule_config_autosave(self) -> None:
        if not self.settings_autosave_ready or self.settings_ui_refreshing:
            return
        if self.config_save_after_id is not None:
            self.root.after_cancel(self.config_save_after_id)
        self.config_save_after_id = self.root.after(900, self.flush_config_autosave)

    def flush_config_autosave(self) -> bool:
        self.config_save_after_id = None
        try:
            old_checksum_path = str(self.config.get("checksum_database_path", ""))
            self.set_config_from_ui()
            write_json_file(config_path(), self.config)
            if str(self.config.get("checksum_database_path", "")) != old_checksum_path:
                self.load_checksum_database()
            self.saved_settings_snapshot = self.current_settings_snapshot()
            self.update_unsaved_settings_warning()
            self.update_startup_warning()
            return True
        except Exception as exc:
            self.unsaved_settings_var.set("Settings could not be saved")
            self.log(f"Automatic settings save failed: {exc}")
            return False

    def refresh_settings_ui_from_config(self) -> None:
        if not hasattr(self, "input_folder_var"):
            return
        self.settings_ui_refreshing = True
        try:
            self.input_folder_var.set(self.config.get("input_folder", ""))
            self.output_folder_var.set(self.config.get("output_folder", ""))
            self.nas_folder_var.set(self.config.get("nas_folder", ""))
            self.ffmpeg_path_var.set(self.config.get("ffmpeg_path", ""))
            self.exclude_files_var.set(", ".join(self.config.get("exclude_files", [])))
            self.exclude_folders_var.set(", ".join(self.config.get("exclude_folders", [])))
            self.auto_delay_var.set(str(self.config.get("auto_import_delay_seconds", 5)))
            self.scan_interval_var.set(str(self.config.get("scan_interval_seconds", 3)))
            self.stable_seconds_var.set(str(self.config.get("file_stable_seconds", 10)))
            self.copy_chunk_var.set(str(self.config.get("copy_chunk_mb", 4)))
            self.max_wait_var.set(str(self.config.get("max_ready_wait_seconds", 7200)))
            self.low_disk_warning_var.set(str(self.config.get("low_disk_warning_gb", 20)))
            self.checksum_path_var.set(self.config.get("checksum_database_path", str(default_checksum_path())))
            self.duplicate_var.set(bool(self.config.get("enable_duplicate_detection", True)))
            self.check_input_var.set(bool(self.config.get("check_existing_in_input", True)))
            self.auto_underscore_var.set(bool(self.config.get("auto_underscore_renames", False)))
            self.prevent_sleep_var.set(bool(self.config.get("prevent_sleep_while_working", True)))
            self.compact_dashboard = bool(self.config.get("compact_dashboard", True))
            self.preset_folder_var.set(self.config.get("preset_folder") or str(preset_dir()))
            if not self.settings_built:
                self.update_compact_dashboard_button()
                self.apply_dashboard_density()
                return
            self.ignored_drives_widget.values = list(self.config.get("ignored_drives", []))
            self.import_ext_widget.values = list(self.config.get("import_extensions", []))
            self.convert_ext_widget.values = list(self.config.get("conversion_extensions", []))
            self.ignored_drives_widget.refresh()
            self.import_ext_widget.refresh()
            self.convert_ext_widget.refresh()
            self.ffmpeg_args_text.delete("1.0", "end")
            self.ffmpeg_args_text.insert("1.0", "\n".join(self.config.get("ffmpeg_args", DEFAULT_FFMPEG_ARGS)))
            self.refresh_preset_menus()
            self.update_preset_status()
            self.update_unsaved_settings_warning()
            self.update_compact_dashboard_button()
            self.apply_dashboard_density()
        finally:
            self.settings_ui_refreshing = False

    def export_settings_backup(self) -> None:
        try:
            self.set_config_from_ui()
            target = filedialog.asksaveasfilename(
                title="Export settings backup",
                defaultextension=".json",
                initialfile="mestersync_settings_backup.json",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            )
            if not target:
                return
            write_json_file(Path(target), self.config)
            self.log(f"Settings backup exported: {target}")
            self.show_notification("Settings backup exported.", "success")
        except Exception as exc:
            self.show_notification(f"Could not export settings backup:\n{exc}", "error")

    def import_settings_backup(self) -> None:
        try:
            source = filedialog.askopenfilename(
                title="Import settings backup",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            )
            if not source:
                return
            data = json.loads(Path(source).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                self.show_notification("That backup file did not contain settings.", "error")
                return
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(data)
            cfg = normalize_config_data(cfg, DEFAULT_FFMPEG_ARGS)
            with self.config_lock:
                self.config = cfg
                write_json_file(config_path(), self.config)
            self.load_checksum_database()
            self.refresh_settings_ui_from_config()
            self.saved_settings_snapshot = self.current_settings_snapshot()
            self.log(f"Settings backup imported: {source}")
            self.show_notification("Settings backup imported.", "success")
        except Exception as exc:
            self.show_notification(f"Could not import settings backup:\n{exc}", "error")

    def run_setup_wizard(self) -> None:
        wizard = tk.Toplevel(self.root)
        wizard.title(f"{APP_NAME} setup")
        wizard.configure(bg=self.BG)
        wizard.transient(self.root)
        wizard.grab_set()
        wizard.geometry("1040x900")
        wizard.minsize(920, 820)

        values = {
            "input_folder": tk.StringVar(value=str(self.config.get("input_folder", ""))),
            "output_folder": tk.StringVar(value=str(self.config.get("output_folder", ""))),
            "nas_folder": tk.StringVar(value=str(self.config.get("nas_folder", ""))),
            "ffmpeg_path": tk.StringVar(value=str(self.config.get("ffmpeg_path", ""))),
        }
        configured_ignored = set(self.config.get("ignored_drives", ["C:"]) or ["C:"])
        drive_choices = list_windows_drives() or ["C:"]
        if "C:" not in drive_choices:
            drive_choices.insert(0, "C:")
        ignored_drive_vars = {drive: tk.BooleanVar(value=drive in configured_ignored) for drive in drive_choices}

        wrap = tk.Frame(wizard, bg=self.BG, padx=18, pady=16)
        wrap.pack(fill="both", expand=True)
        title_row = tk.Frame(wrap, bg=self.BG)
        title_row.pack(fill="x")
        tk.Label(title_row, text=f"Welcome to {APP_NAME}", bg=self.BG, fg=self.TEXT, font=("Segoe UI", 22, "bold")).pack(side="left")
        intro = (
            "MesterSync safely imports video files, converts them with FFmpeg, and can copy the finished videos to a NAS folder. "
            "It never intentionally overwrites videos: imports, conversion outputs, and NAS transfers are written to temporary or unique names first, then verified before local cleanup."
        )
        tk.Label(wrap, text=intro, bg=self.BG, fg=self.MUTED, font=("Segoe UI", 10), wraplength=850, justify="left").pack(anchor="w", pady=(8, 14))

        def field(label: str, help_text: str, key: str, browse_folder: bool = True) -> None:
            box = tk.Frame(wrap, bg=self.CARD2, padx=12, pady=10, highlightthickness=2, highlightbackground=self.BORDER)
            box.pack(fill="x", pady=(0, 10))
            tk.Label(box, text=label, bg=self.CARD2, fg=self.TEXT, font=("Segoe UI", 12, "bold")).pack(anchor="w")
            tk.Label(box, text=help_text, bg=self.CARD2, fg=self.MUTED, font=("Segoe UI", 9), wraplength=820, justify="left").pack(anchor="w", pady=(2, 8))
            row = tk.Frame(box, bg=self.CARD2)
            row.pack(fill="x")
            tk.Entry(row, textvariable=values[key], bg=self.CARD3, fg=self.TEXT, insertbackground=self.TEXT, relief="flat").pack(side="left", fill="x", expand=True, ipady=5)
            def choose() -> None:
                if browse_folder:
                    selected = filedialog.askdirectory(parent=wizard)
                else:
                    selected = filedialog.askopenfilename(parent=wizard, filetypes=[("FFmpeg executable", "ffmpeg.exe"), ("All files", "*.*")])
                if selected:
                    values[key].set(selected)
            self.small_button(row, "Browse", choose, self.CARD3).pack(side="left", padx=(8, 0))

        field("Importfolder", "MesterSync copies added videos here first. This should be a local working folder with plenty of free space. Original files are not deleted by importing.", "input_folder")
        field("Output folder", "Converted files are created here. If Transfer is off, finished files stay here. MesterSync checks this folder at the moment it chooses a final filename.", "output_folder")
        field("NAS folder", "Optional final destination. When Transfer is on, converted files are copied here and verified before local converted/source files are deleted.", "nas_folder")
        field("FFmpeg.exe", "FFmpeg does the actual video conversion and thumbnail extraction. Choose ffmpeg.exe, usually inside an FFmpeg bin folder.", "ffmpeg_path", browse_folder=False)

        drives_box = tk.Frame(wrap, bg=self.CARD2, padx=12, pady=10, highlightthickness=2, highlightbackground=self.BORDER)
        drives_box.pack(fill="x", pady=(0, 10))
        tk.Label(drives_box, text="Ignored drives", bg=self.CARD2, fg=self.TEXT, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(drives_box, text="MesterSync watches for newly connected drives. Select internal/system drives to ignore so they are not scanned as camera/card imports. C: is ignored by default.", bg=self.CARD2, fg=self.MUTED, font=("Segoe UI", 9), wraplength=820, justify="left").pack(anchor="w", pady=(2, 8))
        drive_grid = tk.Frame(drives_box, bg=self.CARD2)
        drive_grid.pack(fill="x")
        for index, (drive, var) in enumerate(ignored_drive_vars.items()):
            tk.Checkbutton(drive_grid, text=drive, variable=var, indicatoron=False, bg=self.CARD3, fg=self.TEXT, selectcolor=self.BLUE, activebackground=self.BLUE, activeforeground=self.TEXT, relief="flat", bd=0, padx=10, pady=5).grid(row=index // 8, column=index % 8, sticky="w", padx=(0, 8), pady=(0, 6))

        wizard_notice_var = tk.StringVar(value="")
        wizard_notice = tk.Label(
            wrap,
            textvariable=wizard_notice_var,
            bg="#421f27",
            fg="#ffb4bd",
            font=("Segoe UI", 9, "bold"),
            justify="left",
            anchor="w",
            padx=10,
            pady=8,
            wraplength=850,
        )

        def set_wizard_notice(text: str, success: bool = False) -> None:
            wizard_notice_var.set(text)
            bg = self.DARK_GREEN if success else "#421f27"
            fg = "#a9f3c5" if success else "#ffb4bd"
            wizard_notice.configure(bg=bg, fg=fg)
            if not wizard_notice.winfo_ismapped():
                wizard_notice.pack(fill="x", pady=(0, 8))

        buttons = tk.Frame(wrap, bg=self.BG)
        buttons.pack(fill="x", pady=(8, 0))
        def save_setup(close_after_save: bool = False) -> None:
            missing = [label for key, label in [("input_folder", "Importfolder"), ("output_folder", "Output folder"), ("ffmpeg_path", "FFmpeg.exe")] if not values[key].get().strip()]
            if missing:
                set_wizard_notice("Please choose: " + ", ".join(missing))
                return
            ffmpeg_candidate = Path(values["ffmpeg_path"].get().strip())
            if not ffmpeg_candidate.is_file():
                set_wizard_notice(f"FFmpeg.exe was not found: {ffmpeg_candidate}")
                return
            folder_errors = [
                validate_writable_folder(values["input_folder"].get().strip(), "Importfolder"),
                validate_writable_folder(values["output_folder"].get().strip(), "Output folder"),
                validate_writable_folder(values["nas_folder"].get().strip(), "NAS folder", required=False),
            ]
            folder_errors.extend(media_folder_overlap_errors(
                values["input_folder"].get(),
                values["output_folder"].get(),
                values["nas_folder"].get(),
            ))
            folder_errors = [error for error in folder_errors if error]
            if folder_errors:
                set_wizard_notice("MesterSync cannot safely use these folders:\n" + "\n".join(f"- {error}" for error in folder_errors))
                return
            with self.config_lock:
                self.config["input_folder"] = values["input_folder"].get().strip()
                self.config["output_folder"] = values["output_folder"].get().strip()
                self.config["nas_folder"] = values["nas_folder"].get().strip()
                self.config["ffmpeg_path"] = values["ffmpeg_path"].get().strip()
                self.config["ignored_drives"] = [drive for drive, var in ignored_drive_vars.items() if var.get()]
                self.config["checksum_database_path"] = str(default_checksum_path())
                self.config["setup_complete"] = True
                write_json_file(config_path(), self.config)
            self.input_folder_var.set(self.config["input_folder"])
            self.output_folder_var.set(self.config["output_folder"])
            self.nas_folder_var.set(self.config["nas_folder"])
            self.ffmpeg_path_var.set(self.config["ffmpeg_path"])
            self.checksum_path_var.set(self.config["checksum_database_path"])
            if self.settings_built:
                self.ignored_drives_widget.values = list(self.config["ignored_drives"])
                self.ignored_drives_widget.refresh()
            self.processed_drive_ids.clear()
            self.load_checksum_database()
            self.saved_settings_snapshot = self.current_settings_snapshot()
            self.update_unsaved_settings_warning()
            self.update_startup_warning()
            self.log("Setup wizard saved settings.")
            set_wizard_notice("Setup settings saved.", success=True)
            if close_after_save:
                wizard.destroy()
        self.button(buttons, "Save and close", lambda: save_setup(True), self.GREEN).pack(side="right")
        self.button(buttons, "Save", lambda: save_setup(False), self.BLUE).pack(side="right", padx=(0, 8))
        self.button(buttons, "Close", wizard.destroy, self.CARD3).pack(side="right", padx=(0, 8))

    def available_video_encoders(self, ffmpeg: Path) -> Optional[set[str]]:
        key = str(ffmpeg)
        if key in self.encoder_cache:
            return self.encoder_cache[key]
        if not ffmpeg.is_file():
            return None
        if key not in self.encoder_queries_pending:
            self.encoder_queries_pending.add(key)

            def query() -> None:
                try:
                    encoders = query_video_encoders(ffmpeg)
                    self.gui_queue.put(("encoder_cache_ready", (key, encoders, "")))
                except Exception as exc:
                    self.gui_queue.put(("encoder_cache_ready", (key, None, str(exc))))

            threading.Thread(target=query, daemon=True, name="FFmpegEncoderQuery").start()
        return None

    def preset_safety_warnings(self, ffmpeg_args: Iterable[str], check_encoder: bool = False) -> List[str]:
        args = [str(arg).strip() for arg in ffmpeg_args if str(arg).strip()]
        warnings = analyze_preset_args(args)
        if check_encoder:
            codec = (arg_value(args, "-c:v", "-vcodec") or "").strip()
            if codec and codec.lower() not in {"copy"}:
                encoders = self.available_video_encoders(Path(self.get_config().get("ffmpeg_path", "")))
                if encoders is not None and codec not in encoders:
                    warnings.append(f"Video encoder '{codec}' is not listed by your FFmpeg build.")
        return warnings

    def preset_warning_text(self, ffmpeg_args: Iterable[str], check_encoder: bool = False) -> str:
        warnings = self.preset_safety_warnings(ffmpeg_args, check_encoder=check_encoder)
        if not warnings:
            return "Preset safety: OK"
        return "Preset safety warnings: " + "  ".join(warnings)

    def run_health_check(self) -> None:
        if self.health_check_running:
            self.log("Health check is already running.")
            return
        try:
            self.set_config_from_ui()
            cfg = self.get_config()
        except Exception as exc:
            self.show_notification(f"Could not read settings:\n{exc}", "error")
            return
        self.health_check_running = True
        if hasattr(self, "health_check_button"):
            self.health_check_button.configure(text="Checking...", state="disabled")
        self.log("Health check started.")

        def check() -> None:
            try:
                lines, encoders = run_diagnostics(cfg)
                payload = {
                    "text": "\n".join(lines),
                    "ffmpeg_key": str(cfg.get("ffmpeg_path", "")).strip(),
                    "encoders": encoders,
                    "error": "",
                }
            except Exception as exc:
                payload = {"text": "", "ffmpeg_key": "", "encoders": None, "error": str(exc)}
            self.gui_queue.put(("health_check_complete", payload))

        threading.Thread(target=check, daemon=True, name="HealthCheck").start()

    def finish_health_check(self, payload: Dict[str, Any]) -> None:
        self.health_check_running = False
        if hasattr(self, "health_check_button"):
            self.health_check_button.configure(text="Run health check", state="normal")
        error = str(payload.get("error") or "")
        if error:
            self.log(f"Health check failed: {error}")
            self.show_notification(f"Health check failed:\n{error}", "error")
            return
        ffmpeg_key = str(payload.get("ffmpeg_key") or "")
        encoders = payload.get("encoders")
        if ffmpeg_key and isinstance(encoders, set):
            self.encoder_cache[ffmpeg_key] = encoders
            self.encoder_queries_pending.discard(ffmpeg_key)
            self.update_preset_status(check_encoder=True)
        health_text = str(payload.get("text") or "Health check completed.")
        self.log("Health check complete.\n" + health_text)
        level = "warning" if "WARNING" in health_text or "ERROR" in health_text else "success"
        issue_count = health_text.count("WARNING") + health_text.count("ERROR")
        summary = f"Health check complete with {issue_count} warning(s). Open Log for details." if issue_count else "Health check complete. Everything looks healthy."
        self.show_notification(summary, level, duration_ms=8000)

    def load_history(self) -> List[Dict[str, Any]]:
        return load_history_records()

    def save_history(self, force: bool = False) -> None:
        if not force:
            self.history_save_pending = True
            if self.history_save_after_id is None:
                self.gui_queue.put(("schedule_history_save", None))
            return
        self.history_save_after_id = None
        with self.history_lock:
            self.history_save_pending = False
            save_history_records(self.history_records)

    def total_converted_count(self) -> int:
        with self.history_lock:
            return converted_history_count(self.history_records)

    def thumbnail_paths_from_record(self, record: Dict[str, Any]) -> set[str]:
        paths = set()
        if record.get("thumbnail_path"):
            paths.add(str(record.get("thumbnail_path")))
        for path in record.get("thumbnail_preview_paths") or []:
            if path:
                paths.add(str(path))
        return paths

    def cleanup_thumbnail_paths(self, paths: Iterable[str]) -> int:
        removed = 0
        for path in paths:
            try:
                p = Path(path)
                if p.exists() and path_key(p.parent) == path_key(thumbnail_dir()):
                    force_delete(p)
                    self.thumbnail_images.remove(str(p))
                    removed += 1
            except Exception as exc:
                self.log(f"Thumbnail cleanup warning: {exc}")
        return removed

    def cleanup_history_thumbnails(self, record: Dict[str, Any]) -> None:
        self.cleanup_thumbnail_paths(self.thumbnail_paths_from_record(record))

    def cleanup_task_thumbnails(self, task: Any) -> None:
        paths = set()
        if getattr(task, "thumbnail_path", None):
            paths.add(str(task.thumbnail_path))
        for path in getattr(task, "thumbnail_preview_paths", None) or []:
            if path:
                paths.add(str(path))
        self.cleanup_thumbnail_paths(paths)

    def referenced_thumbnail_paths(self) -> set[str]:
        referenced = set()
        with self.task_lock:
            tasks = list(self.tasks.values())
        for task in tasks:
            if task.thumbnail_path:
                referenced.add(path_key(task.thumbnail_path))
            for path in task.thumbnail_preview_paths or []:
                if path:
                    referenced.add(path_key(path))
        with self.history_lock:
            records = list(self.history_records)
        for record in records:
            for path in self.thumbnail_paths_from_record(record):
                referenced.add(path_key(path))
        return referenced

    def cleanup_thumbnail_cache(self, notify: bool = True) -> None:
        folder = thumbnail_dir()
        before = 0
        removed = 0
        kept = 0
        referenced = self.referenced_thumbnail_paths()
        try:
            if folder.exists():
                for path in folder.glob("*.png"):
                    before += 1
                    if path_key(path) in referenced:
                        kept += 1
                        continue
                    force_delete(path)
                    if notify:
                        self.thumbnail_images.remove(str(path))
                    removed += 1
                try:
                    if not any(folder.iterdir()):
                        folder.rmdir()
                except OSError:
                    pass
        except Exception as exc:
            (self.log if notify else self.emit_log)(f"Thumbnail cleanup warning: {exc}")
            if notify:
                self.show_notification(f"Could not finish thumbnail cleanup:\n{exc}", "warning")
            return
        (self.log if notify else self.emit_log)(f"Cleaned thumbnails: removed {removed}, kept {kept}.")
        if notify:
            self.show_notification(f"Thumbnail cleanup complete. Removed: {removed} · Kept: {kept} · Before: {before}", "success")

    def save_pending_tasks(self, force: bool = False) -> None:
        if not force:
            self.tasks_save_pending = True
            if self.tasks_save_after_id is None:
                self.gui_queue.put(("schedule_tasks_save", None))
            return
        self.tasks_save_after_id = None
        with self.task_lock:
            self.tasks_save_pending = False
            records = [
                task_to_record(task)
                for task in self.tasks.values()
                if task.stage not in {STAGE_COMPLETE, STAGE_CONVERTED_OUTPUT}
            ]
        save_task_records(records)

    def load_pending_tasks(self) -> None:
        records = load_task_records()
        restored = 0
        with self.task_lock:
            for record in records:
                if not isinstance(record, dict):
                    continue
                task = record_to_task(record)
                if not task:
                    continue
                self.tasks[task.task_id] = task
                for p in [task.original_path, task.local_input_path, task.output_path, task.nas_path]:
                    if p:
                        self.task_by_path[path_key(p)] = task.task_id
                        if task.skipped:
                            self.skipped_path_keys.add(path_key(p))
                restored += 1
        for task_id in list(self.tasks.keys()):
            self.emit_task(task_id)
        if restored:
            self.log(f"Restored {restored} pending task(s) from the previous session.")

    def load_checksum_database(self) -> None:
        cfg = self.get_config() if hasattr(self, "config_lock") else self.config
        with self.checksum_lock:
            self.checksum_db = load_checksum_records(Path(cfg.get("checksum_database_path", "")))

    def save_checksum_database(self) -> None:
        try:
            path = Path(self.get_config().get("checksum_database_path", ""))
            with self.checksum_lock:
                records = dict(self.checksum_db)
                save_checksum_records(path, records)
        except Exception as exc:
            self.emit_log(f"Warning: could not save checksum database: {exc}")

    def duplicate_reason_for(self, path: Path, cfg: Dict[str, Any], task_id: Optional[str] = None) -> str:
        if not bool(cfg.get("enable_duplicate_detection", True)):
            return ""
        with self.checksum_lock:
            checksum_db = dict(self.checksum_db)
        progress_phase = ""
        started = time.time()
        def on_progress(phase: str, percent: int, checked: int, total: int) -> None:
            nonlocal progress_phase, started
            if phase != progress_phase:
                progress_phase = phase
                started = time.time()
            elapsed = time.time() - started
            eta = (elapsed / percent * (100 - percent)) if percent > 0 else None
            if task_id:
                self.update_task(
                    task_id,
                    stage=STAGE_CHECKING,
                    progress=percent,
                    detail=f"{phase}: {format_size(checked)} / {format_size(total)}",
                    eta_seconds=eta,
                )
                self.set_import_progress_snapshot(task_id, f"Import/watching: {phase.lower()} for {path.name}", percent, eta)
        return exact_duplicate_reason(path, cfg, checksum_db, self.import_cancel_requested, on_progress)

    def remember_checksum(self, path: Path) -> None:
        cfg = self.get_config()
        if not bool(cfg.get("enable_duplicate_detection", True)) or not path.exists():
            return
        digest = sha256_file(path)
        if digest:
            try:
                size = path.stat().st_size if path.exists() else None
            except OSError:
                size = None
            self.remember_original_checksum(digest, path.name, size, path)

    def remember_original_checksum(self, digest: str, original_name: str, original_size: Optional[int], path: Path) -> None:
        if not digest:
            return
        with self.checksum_lock:
            old = self.checksum_db.get(digest)
            record = dict(old) if isinstance(old, dict) else {}
            record.update(
                {
                    "original_name": original_name,
                    "original_size": original_size,
                    "path": str(path),
                    "final_path": str(path),
                    "updated_at": now_iso(),
                }
            )
            self.checksum_db[digest] = record
        self.save_checksum_database()

    def update_checksum_final_path(self, task: TaskState, final_path: Path) -> None:
        digest = task.original_checksum
        if not digest:
            return
        with self.checksum_lock:
            old = self.checksum_db.get(digest)
            record = dict(old) if isinstance(old, dict) else {}
            record.update(
                {
                    "original_name": task.display_name,
                    "original_size": task.imported_size,
                    "path": str(final_path),
                    "final_path": str(final_path),
                    "updated_at": now_iso(),
                }
            )
            self.checksum_db[digest] = record
        self.save_checksum_database()

    def remember_checksum_async(self, path: Path) -> None:
        cfg = self.get_config()
        if not bool(cfg.get("enable_duplicate_detection", True)) or not path.exists():
            return
        key = path_key(path)
        if key in self.checksum_generation_requested:
            return
        self.checksum_generation_requested.add(key)
        self.checksum_queue.put(path)
        self.ensure_checksum_worker_running()

    def ensure_task_source_fingerprint(self, task_id: str, source: Path) -> bool:
        """Remember directly watched sources before their working copy is removed."""
        cfg = self.get_config()
        if not bool(cfg.get("enable_duplicate_detection", True)):
            return True
        task = self.tasks.get(task_id)
        if not task or task.convert_in_place or task.original_checksum:
            return True
        try:
            source_size = source.stat().st_size
        except OSError:
            return False
        started = time.time()

        def on_progress(percent: int, checked: int, total: int) -> None:
            elapsed = time.time() - started
            eta = (elapsed / percent * (100 - percent)) if percent > 0 else None
            self.update_task(
                task_id,
                stage=STAGE_CONVERTING,
                progress=percent,
                detail=f"Remembering source fingerprint {format_size(checked)} / {format_size(total)}",
                eta_seconds=eta,
            )
            self.emit_progress("conversion", f"Remembering source: {source.name}", percent, eta)

        digest = sha256_file(source, task.import_stop_event, on_progress)
        if not digest:
            return False
        with self.task_lock:
            current = self.tasks.get(task_id)
            if not current:
                return False
            current.original_checksum = digest
        self.remember_original_checksum(digest, source.name, source_size, source)
        return True

    def ensure_checksum_worker_running(self) -> None:
        if self.checksum_worker_started:
            return
        self.checksum_worker_started = True
        threading.Thread(target=self.checksum_worker_loop, daemon=True, name="ChecksumWorker").start()

    def checksum_worker_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                path = self.checksum_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            key = path_key(path)
            try:
                self.remember_checksum(path)
            except Exception as exc:
                self.emit_log(f"Checksum worker error for {path.name}: {exc}")
            finally:
                self.checksum_generation_requested.discard(key)
                self.checksum_queue.task_done()

    def cleanup_checksum_database(self) -> None:
        with self.checksum_lock:
            empty = not self.checksum_db
        if empty:
            self.load_checksum_database()
        with self.checksum_lock:
            before = len(self.checksum_db)
            kept, removed = remove_missing_checksum_entries(self.checksum_db)
            self.checksum_db = kept
        self.save_checksum_database()
        self.log(f"Cleaned checksum database: removed {removed} invalid entr{'y' if removed == 1 else 'ies'}, kept {len(kept)} remembered fingerprint(s).")
        self.show_notification(
            f"Checksum cleanup complete. Removed invalid entries: {removed} · Kept fingerprints: {len(kept)} · Before: {before}. Moved NAS files remain remembered.",
            "success",
        )

    # ---------------- UI ----------------
    def configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Green.Horizontal.TProgressbar", troughcolor=self.CARD3, background=self.GREEN, lightcolor=self.GREEN, darkcolor=self.GREEN, bordercolor=self.CARD3, thickness=12)
        style.configure("Purple.Horizontal.TProgressbar", troughcolor=self.CARD3, background=self.PURPLE, lightcolor=self.PURPLE, darkcolor=self.PURPLE, bordercolor=self.CARD3, thickness=12)
        style.configure("Orange.Horizontal.TProgressbar", troughcolor=self.CARD3, background=self.ORANGE, lightcolor=self.ORANGE, darkcolor=self.ORANGE, bordercolor=self.CARD3, thickness=12)
        style.configure("Thin.Horizontal.TProgressbar", troughcolor=self.CARD3, background=self.GREEN, lightcolor=self.GREEN, darkcolor=self.GREEN, bordercolor=self.CARD3, thickness=10)
        style.configure("RowImport.Horizontal.TProgressbar", troughcolor=self.CARD3, background=self.ORANGE, lightcolor=self.ORANGE, darkcolor=self.ORANGE, bordercolor=self.CARD3, thickness=10)
        style.configure("RowConvert.Horizontal.TProgressbar", troughcolor=self.CARD3, background=self.GREEN, lightcolor=self.GREEN, darkcolor=self.GREEN, bordercolor=self.CARD3, thickness=10)
        style.configure("RowTransfer.Horizontal.TProgressbar", troughcolor=self.CARD3, background=self.PURPLE, lightcolor=self.PURPLE, darkcolor=self.PURPLE, bordercolor=self.CARD3, thickness=10)
        style.configure("RowError.Horizontal.TProgressbar", troughcolor=self.CARD3, background=self.RED, lightcolor=self.RED, darkcolor=self.RED, bordercolor=self.CARD3, thickness=10)
        # Readable dropdowns. The rest of the UI is dark, but Tk's dropdown list
        # becomes hard to read with white text on a grey native listbox.
        style.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff", foreground="#000000", arrowcolor="#000000")
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")], foreground=[("readonly", "#000000")], selectbackground=[("readonly", "#d7e8ff")], selectforeground=[("readonly", "#000000")])
        self.root.option_add("*TCombobox*Listbox.background", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.foreground", "#000000")
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#d7e8ff")
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#000000")
        style.configure("Vertical.TScrollbar", background=self.CARD3, troughcolor=self.CARD, bordercolor=self.BORDER, arrowcolor=self.TEXT)

    def build_notification_toast(self) -> None:
        self.notification_frame = tk.Frame(self.main, bg=self.BORDER, highlightthickness=0)
        self.notification_inner = tk.Frame(self.notification_frame, bg=self.CARD2, padx=12, pady=9)
        self.notification_inner.pack(fill="both", expand=True, padx=2, pady=2)
        self.notification_var = tk.StringVar(value="")
        self.notification_label = tk.Label(
            self.notification_inner,
            textvariable=self.notification_var,
            bg=self.CARD2,
            fg=self.TEXT,
            font=("Segoe UI", 9, "bold"),
            justify="left",
            anchor="w",
            wraplength=520,
        )
        self.notification_label.pack(side="left", fill="x", expand=True)
        self.small_button(self.notification_inner, "Dismiss", self.hide_notification, self.CARD3).pack(side="right", padx=(12, 0))

    def show_notification(self, text: str, level: str = "info", duration_ms: Optional[int] = None) -> None:
        if not hasattr(self, "notification_frame"):
            self.log(str(text))
            return
        delay = duration_ms if duration_ms is not None else (9000 if level == "error" else 5500)
        payload = (compact_notification_text(text), level, max(1000, int(delay)))
        if self.notification_active:
            self.notification_queue.append(payload)
            return
        self.display_notification(payload)

    def display_notification(self, payload: Tuple[str, str, int]) -> None:
        text, level, delay = payload
        colors = {
            "info": ("#102b46", "#b9dcff"),
            "success": (self.DARK_GREEN, "#a9f3c5"),
            "warning": ("#3a3114", "#ffd36a"),
            "error": ("#421f27", "#ffb4bd"),
        }
        bg, fg = colors.get(level, colors["info"])
        self.notification_active = True
        self.notification_var.set(text)
        self.notification_inner.configure(bg=bg)
        self.notification_label.configure(bg=bg, fg=fg)
        for child in self.notification_inner.winfo_children():
            if child is not self.notification_label and isinstance(child, tk.Button):
                child.configure(bg=self.CARD3, activebackground=self.CARD3)
        self.notification_frame.place(relx=1.0, rely=1.0, x=-24, y=-24, anchor="se", width=570)
        self.notification_frame.lift()
        if self.notification_after_id is not None:
            try:
                self.root.after_cancel(self.notification_after_id)
            except Exception:
                pass
        self.notification_after_id = self.root.after(delay, self.hide_notification)

    def hide_notification(self) -> None:
        if self.notification_after_id is not None:
            try:
                self.root.after_cancel(self.notification_after_id)
            except Exception:
                pass
            self.notification_after_id = None
        if hasattr(self, "notification_frame"):
            self.notification_frame.place_forget()
        self.notification_active = False
        if self.notification_queue:
            payload = self.notification_queue.popleft()
            self.notification_active = True
            self.root.after(80, lambda p=payload: self.display_notification(p))

    def button(self, parent: tk.Widget, text: str, command: Callable[[], None], bg: str, fg: str = "white", padx: int = 12) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg, activebackground=bg, activeforeground=fg, relief="flat", bd=0, highlightthickness=2, highlightbackground=self.BORDER, padx=padx, pady=8, font=("Segoe UI", 10, "bold"), cursor="hand2")

    def small_button(self, parent: tk.Widget, text: str, command: Callable[[], None], bg: str = None) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=bg or self.CARD3, fg="white", activebackground=bg or self.CARD3, activeforeground="white", relief="flat", bd=0, highlightthickness=2, highlightbackground=self.BORDER, padx=8, pady=5, font=("Segoe UI", 9), cursor="hand2")

    def card_frame(self, parent: tk.Widget, bg: str = None, **pack_kwargs: Any) -> tk.Frame:
        outer = tk.Frame(parent, bg=self.BORDER, bd=0)
        inner = tk.Frame(outer, bg=bg or self.CARD, padx=14, pady=12)
        inner.pack(fill="both", expand=True, padx=2, pady=2)
        if pack_kwargs:
            outer.pack(**pack_kwargs)
        inner.outer = outer  # type: ignore[attr-defined]
        return inner

    def build_gui(self) -> None:
        self.main = tk.Frame(self.root, bg=self.BG)
        self.main.pack(fill="both", expand=True)
        self.build_header()
        self.build_top_bar()
        self.build_notification_toast()
        self.build_stage_cards()
        self.build_body()
        self.update_status()

    def build_header(self) -> None:
        header = tk.Frame(self.main, bg=self.BG)
        header.pack(fill="x", padx=18, pady=(8, 6))
        self.header_frame = header
        left = tk.Frame(header, bg=self.BG)
        left.pack(side="left")
        logo = resource_path(LOGO_FILENAME)
        if logo.exists():
            try:
                img = tk.PhotoImage(file=str(logo))
                self.logo_img = scale_photo(img, 230, 52)
                self.logo_label = tk.Label(left, image=self.logo_img, bg=self.BG)
                self.logo_label.pack(anchor="w")
            except Exception:
                self.logo_label = tk.Label(left, text=APP_NAME, bg=self.BG, fg=self.TEXT, font=("Segoe UI", 26, "bold"))
                self.logo_label.pack(anchor="w")
        else:
            self.logo_label = tk.Label(left, text=APP_NAME, bg=self.BG, fg=self.TEXT, font=("Segoe UI", 26, "bold"))
            self.logo_label.pack(anchor="w")

        status_outer = tk.Frame(header, bg=self.BORDER)
        status_outer.pack(side="right", pady=(7, 0))
        self.header_status_outer = status_outer
        status = tk.Frame(status_outer, bg=self.CARD, padx=10, pady=6)
        status.pack(padx=2, pady=2)
        tk.Label(status, text="STATUS", bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 8, "bold")).pack(side="left")
        self.status_dot = tk.Canvas(status, width=12, height=12, bg=self.CARD, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(8, 6))
        self.status_var = tk.StringVar(value="Stopped")
        tk.Label(status, textvariable=self.status_var, bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 10, "bold")).pack(side="left")
        self.activity_var = tk.StringVar(value="")
        tk.Label(status, textvariable=self.activity_var, bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))

    def build_top_bar(self) -> None:
        bar = self.card_frame(self.main, fill="x", padx=18, pady=(0, 10))
        self.start_btn = self.button(bar, "Start all", self.toggle_start_all, self.GREEN)
        self.start_btn.pack(side="left")
        self.pause_btn = self.button(bar, "Pause", self.toggle_pause, self.YELLOW)
        self.pause_btn.pack(side="left", padx=(10, 0))
        self.log_toggle = self.button(bar, "Log", self.toggle_log, self.CARD3)
        self.log_toggle.pack(side="left", padx=(10, 0))
        self.top_log_outer = tk.Frame(bar, bg=self.BORDER, height=48)
        self.top_log_outer.pack_propagate(False)
        top_log_panel = tk.Frame(self.top_log_outer, bg="#07111e", padx=5, pady=4)
        top_log_panel.pack(fill="both", expand=True, padx=2, pady=2)
        self.log_text = scrolledtext.ScrolledText(
            top_log_panel,
            height=2,
            bg="#07111e",
            fg="#d7e8ff",
            insertbackground="#d7e8ff",
            relief="flat",
            font=("Consolas", 8),
            state="disabled",
            wrap="word",
        )
        self.log_text.pack(fill="both", expand=True)
        self.startup_warning_var = tk.StringVar(value="")
        self.startup_warning_frame = tk.Frame(bar, bg="#3a3114", padx=10, pady=6)
        tk.Label(self.startup_warning_frame, textvariable=self.startup_warning_var, bg="#3a3114", fg="#ffd36a", font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left")
        self.small_button(self.startup_warning_frame, "Setup", self.run_setup_wizard, "#5c4517").pack(side="left", padx=(10, 0))
        self.thumbnail_queue_var = tk.StringVar(value="Previews: idle")
        self.thumbnail_queue_label = tk.Label(bar, textvariable=self.thumbnail_queue_var, bg=self.CARD2, fg=self.MUTED, font=("Segoe UI", 9, "bold"), anchor="w", padx=10, pady=6)
        self.disk_ready_var = tk.StringVar(value="")
        self.disk_ready_frame = tk.Frame(bar, bg=self.DARK_GREEN, padx=10, pady=6)
        tk.Label(self.disk_ready_frame, textvariable=self.disk_ready_var, bg=self.DARK_GREEN, fg="#8ff0b8", font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left")
        self.small_button(self.disk_ready_frame, "Dismiss", self.hide_disk_ready, self.GREEN).pack(side="left", padx=(10, 0))
        self.unsaved_settings_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.unsaved_settings_var, bg=self.CARD, fg=self.RED, font=("Segoe UI", 12, "bold")).pack(side="left", padx=(18, 0))

        preset_area = tk.Frame(bar, bg=self.CARD)
        preset_area.pack(side="right")
        tk.Label(preset_area, text="Default preset", bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        top_preset_values = self.preset_menu_values()
        saved_top_preset = self.config.get("default_preset_name", "Current settings")
        if saved_top_preset not in top_preset_values:
            saved_top_preset = "Current settings"
            with self.config_lock:
                self.config["default_preset_name"] = saved_top_preset
        self.top_preset_var = tk.StringVar(value=saved_top_preset)
        self.top_preset_combo = ttk.Combobox(preset_area, textvariable=self.top_preset_var, values=top_preset_values, state="readonly", width=26)
        self.top_preset_combo.pack(side="left")
        self.top_preset_combo.bind("<<ComboboxSelected>>", lambda e: self.load_top_preset())

    def build_stage_cards(self) -> None:
        wrap = tk.Frame(self.main, bg=self.BG)
        wrap.pack(fill="x", padx=18, pady=(0, 10))
        self.stage_cards: Dict[str, Dict[str, Any]] = {}
        configs = [("import", "Import/watching", "Orange.Horizontal.TProgressbar"), ("conversion", "Conversion", "Green.Horizontal.TProgressbar"), ("transfer", "Transfer", "Purple.Horizontal.TProgressbar")]
        for idx, (key, title, style) in enumerate(configs):
            outer = tk.Frame(wrap, bg=self.BORDER, height=126)
            outer.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 10, 0))
            outer.grid_propagate(False)
            wrap.grid_columnconfigure(idx, weight=1)
            card = tk.Frame(outer, bg=self.CARD, padx=12, pady=9, height=122)
            card.pack(fill="both", expand=True, padx=2, pady=2)
            card.pack_propagate(False)
            head = tk.Frame(card, bg=self.CARD)
            head.pack(fill="x")
            dot = tk.Canvas(head, width=14, height=14, bg=self.CARD, highlightthickness=0)
            dot.pack(side="left")
            tk.Label(head, text=title, bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 14, "bold")).pack(side="left", padx=(9, 0))
            btn = self.small_button(head, "Stop", lambda k=key: self.toggle_stage(k), self.RED)
            btn.pack(side="right")
            cancel_btn = None
            priority_btn = None
            if key == "import":
                priority_btn = self.small_button(head, "Prioritize import", self.toggle_import_priority, self.CARD3)
                priority_btn.pack(side="right", padx=(0, 8))
                cancel_btn = self.small_button(head, "Cancel import", self.cancel_current_import, self.CARD3)
                cancel_btn.pack(side="right", padx=(0, 8))
            label_var = tk.StringVar(value=f"{title}: idle")
            eta_var = tk.StringVar(value="ETA: -")
            detail_row = tk.Frame(card, bg=self.CARD)
            detail_row.pack(fill="x", pady=(6, 1))
            tk.Label(detail_row, textvariable=eta_var, bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9), anchor="e").pack(side="right", padx=(8, 0))
            tk.Label(detail_row, textvariable=label_var, bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9), anchor="w", width=1).pack(side="left", fill="x", expand=True)
            pvar = tk.IntVar(value=0)
            bar = ttk.Progressbar(card, maximum=100, variable=pvar, style=style)
            bar.pack(fill="x", pady=(6, 3))
            percent = tk.StringVar(value="0%")
            tk.Label(card, textvariable=percent, bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 10, "bold"), anchor="e").pack(fill="x")
            self.stage_cards[key] = {"outer": outer, "card": card, "dot": dot, "label": label_var, "eta": eta_var, "progress": pvar, "percent": percent, "button": btn, "cancel_button": cancel_btn, "priority_button": priority_btn, "title": title}

    def build_body(self) -> None:
        body = tk.Frame(self.main, bg=self.BG)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        left = self.card_frame(body, side="left", fill="y", padx=(0, 10))
        left.configure(width=160)
        left.pack_propagate(False)
        self.tab_buttons: Dict[str, tk.Button] = {}
        for key, label in [("dashboard", "Dashboard"), ("history", "History"), ("settings", "Settings")]:
            btn = self.button(left, label, lambda k=key: self.show_tab(k), self.CARD3)
            btn.pack(fill="x", pady=(0, 10))
            self.tab_buttons[key] = btn
        tk.Frame(left, bg=self.CARD).pack(fill="both", expand=True)
        self.dashboard_side_controls = tk.Frame(left, bg=self.CARD)
        self.dashboard_side_controls.pack(fill="x", side="bottom")
        self.compact_dashboard_btn = self.button(
            self.dashboard_side_controls,
            "",
            self.toggle_compact_dashboard,
            self.BLUE if self.compact_dashboard else self.CARD3,
        )
        self.compact_dashboard_btn.pack(fill="x", pady=(0, 10))
        self.update_compact_dashboard_button()
        self.content = tk.Frame(body, bg=self.BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)
        self.dashboard_frame = tk.Frame(self.content, bg=self.BG)
        self.history_frame = tk.Frame(self.content, bg=self.BG)
        self.settings_frame = tk.Frame(self.content, bg=self.BG)
        for frame in (self.dashboard_frame, self.history_frame, self.settings_frame):
            frame.grid(row=0, column=0, sticky="nsew")
        self.build_dashboard()
        self.build_history()
        self.settings_loading_label = tk.Label(
            self.settings_frame,
            text="Loading Settings...",
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 13, "bold"),
        )
        self.settings_loading_label.pack(pady=30)
        self.show_tab("dashboard")

    def show_tab(self, key: str) -> None:
        if key not in self.tab_buttons:
            return
        if self.current_tab == "history" and key != "history":
            self.cancel_history_render(mark_dirty=True)
        for k, b in self.tab_buttons.items():
            b.configure(bg=self.BLUE if k == key else self.CARD3)
        getattr(self, f"{key}_frame").tkraise()
        changed = self.current_tab != key
        self.current_tab = key
        if key == "settings" and not self.settings_built and not self.settings_building:
            self.settings_building = True
            self.root.after(10, self.finish_lazy_settings_build)
        if hasattr(self, "dashboard_side_controls"):
            if key == "dashboard" and not self.dashboard_side_controls.winfo_ismapped():
                self.dashboard_side_controls.pack(fill="x", side="bottom")
            elif key != "dashboard" and self.dashboard_side_controls.winfo_ismapped():
                self.dashboard_side_controls.pack_forget()
        if key == "dashboard" and changed:
            self.refresh_dashboard_rows()
            self.deferred_dashboard_task_ids.clear()
        elif key == "history" and self.history_view_dirty:
            self.refresh_history_view()

    def finish_lazy_settings_build(self) -> None:
        if self.settings_built:
            self.settings_building = False
            return
        if self.current_tab != "settings":
            self.settings_building = False
            return
        try:
            for child in self.settings_frame.winfo_children():
                child.destroy()
            self.build_settings()
            self.refresh_settings_ui_from_config()
        except Exception as exc:
            self.settings_building = False
            self.log(f"Settings page build warning: {exc}")
            tk.Label(
                self.settings_frame,
                text=f"Settings could not be opened: {exc}",
                bg=self.BG,
                fg=self.RED,
                font=("Segoe UI", 11, "bold"),
            ).pack(pady=30)

    def row_widget_alive(self, task_id: str) -> bool:
        row = self.row_widgets.get(task_id)
        if not row:
            return False
        try:
            return bool(row.get("outer") and row["outer"].winfo_exists())
        except Exception:
            return False

    def refresh_dashboard_rows(self) -> None:
        self.refresh_dashboard_virtual_rows(force=True)
        self.update_batch_box_visibility()

    def dashboard_row_extent(self) -> int:
        return 108 if self.compact_dashboard else 174

    def schedule_dashboard_virtual_refresh(self) -> None:
        if self.dashboard_virtual_refresh_after_id is not None:
            return
        self.dashboard_virtual_refresh_after_id = self.root.after_idle(self.refresh_dashboard_virtual_rows)

    def dashboard_task_should_render(self, task_id: str) -> bool:
        with self.task_lock:
            if task_id not in self.tasks:
                return False
            total = len(self.tasks)
        return total <= DASHBOARD_VIRTUAL_THRESHOLD or task_id in self.dashboard_virtual_ids

    def refresh_dashboard_virtual_rows(self, force: bool = False) -> None:
        self.dashboard_virtual_refresh_after_id = None
        if not hasattr(self, "dashboard_canvas"):
            return
        order = self.ordered_task_ids()
        extent = self.dashboard_row_extent()
        try:
            scroll_top = float(self.dashboard_canvas.canvasy(0))
            viewport = max(1, int(self.dashboard_canvas.winfo_height()))
        except Exception:
            scroll_top, viewport = 0.0, 800
        start, end = virtual_row_window(
            len(order),
            scroll_top,
            viewport,
            extent,
            DASHBOARD_VIRTUAL_THRESHOLD,
            DASHBOARD_VIRTUAL_OVERSCAN,
        )
        visible_order = tuple(order[start:end])
        if (
            not force
            and (start, end) == self.dashboard_virtual_range
            and len(order) == self.dashboard_virtual_total
            and visible_order == self.dashboard_virtual_order
        ):
            return
        self.dashboard_virtual_range = (start, end)
        self.dashboard_virtual_total = len(order)
        self.dashboard_virtual_order = visible_order
        visible = set(visible_order)
        self.dashboard_virtual_ids = visible
        for task_id in list(self.row_widgets):
            if task_id not in visible or task_id not in self.tasks:
                row = self.row_widgets.pop(task_id, None)
                if row:
                    try:
                        row["outer"].destroy()
                    except Exception:
                        pass
        top_height = start * extent
        bottom_height = max(0, len(order) - end) * extent
        self.dashboard_top_spacer.configure(height=max(1, top_height))
        self.dashboard_bottom_spacer.configure(height=max(1, bottom_height))
        for task_id in order[start:end]:
            self.render_task(task_id)
        for task_id in order[start:end]:
            row = self.row_widgets.get(task_id)
            if row:
                row["outer"].pack_forget()
                row["outer"].pack(fill="x", pady=3 if self.compact_dashboard else 7, before=self.dashboard_bottom_spacer)

    def make_canvas_area(self, parent: tk.Widget) -> Tuple[tk.Canvas, tk.Frame]:
        holder = tk.Frame(parent, bg=self.BG)
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=self.BG, highlightthickness=0, bd=0, yscrollincrement=1)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=self.BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas._view_changed_callback = None  # type: ignore[attr-defined]
        def update_scrollbar(first: str, last: str) -> None:
            scrollbar.set(first, last)
            callback = getattr(canvas, "_view_changed_callback", None)
            if callback:
                callback()
        canvas.configure(yscrollcommand=update_scrollbar)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        def update_scrollregion(event: Optional[tk.Event] = None) -> None:
            height = max(inner.winfo_reqheight(), canvas.winfo_height())
            canvas.configure(scrollregion=(0, 0, max(1, canvas.winfo_width()), height))
            top, _ = canvas.yview()
            if top <= 0:
                canvas.yview_moveto(0)
        inner.bind("<Configure>", update_scrollregion)
        canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(win, width=e.width), update_scrollregion()))
        self.bind_mousewheel(canvas, holder)
        return canvas, inner

    def bind_mousewheel(self, canvas: tk.Canvas, region: Optional[tk.Widget] = None) -> None:
        self.smooth_scroll_regions.append((region or canvas, canvas))
        if not self.smooth_scroll_bound:
            self.root.bind_all("<MouseWheel>", self.handle_smooth_mousewheel, add="+")
            self.smooth_scroll_bound = True

    def handle_smooth_mousewheel(self, event: tk.Event) -> Optional[str]:
        delta = float(getattr(event, "delta", 0) or 0)
        if not delta:
            return None
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            widget = None
        canvas: Optional[tk.Canvas] = None
        for region, candidate in reversed(self.smooth_scroll_regions):
            current = widget
            while current is not None:
                if current is region:
                    canvas = candidate
                    break
                current = getattr(current, "master", None)
            if canvas is not None:
                break
        if canvas is None:
            return None
        ident = id(canvas)
        current_velocity = self.smooth_scroll_velocity.get(ident, 0.0)
        impulse = max(-240.0, min(240.0, -delta * 0.9))
        self.smooth_scroll_velocity[ident] = max(-720.0, min(720.0, current_velocity + impulse))
        if ident not in self.smooth_scroll_after_ids:
            self.smooth_scroll_after_ids[ident] = self.root.after(16, lambda: self.animate_smooth_scroll(canvas))
        return "break"

    def animate_smooth_scroll(self, canvas: tk.Canvas) -> None:
        ident = id(canvas)
        self.smooth_scroll_after_ids.pop(ident, None)
        if not canvas.winfo_exists():
            self.smooth_scroll_velocity.pop(ident, None)
            return
        velocity = self.smooth_scroll_velocity.get(ident, 0.0)
        distance, next_velocity = inertial_scroll_step(velocity)
        top, bottom = canvas.yview()
        if (distance < 0 and top <= 0) or (distance > 0 and bottom >= 1):
            next_velocity = 0.0
        elif distance:
            canvas.yview_scroll(distance, "units")
        if next_velocity:
            self.smooth_scroll_velocity[ident] = next_velocity
            self.smooth_scroll_after_ids[ident] = self.root.after(16, lambda: self.animate_smooth_scroll(canvas))
        else:
            self.smooth_scroll_velocity.pop(ident, None)

    def enable_drop_target(self, widget: tk.Widget) -> bool:
        """Make a widget accept dropped files/folders when tkinterdnd2 is available."""
        if not DND_FILES or not hasattr(widget, "drop_target_register"):
            return False
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self.handle_drop)
            return True
        except Exception:
            return False

    def enable_dashboard_drop_targets(self, *widgets: tk.Widget) -> None:
        """Register the full Dashboard surface as a drop zone.

        Earlier builds only registered the empty canvas. Once rows/header widgets
        covered that canvas, dropping onto a visible row or the Active files
        header could miss the drop event. Registering the dashboard frame,
        canvas, inner list, header, and each row restores the old behavior.
        """
        targets: List[tk.Widget] = [self.root, self.dashboard_frame]
        for attr in ("dashboard_canvas", "dashboard_inner"):
            widget = getattr(self, attr, None)
            if widget is not None:
                targets.append(widget)
        targets.extend(widgets)
        index = 0
        while index < len(targets):
            widget = targets[index]
            try:
                targets.extend(widget.winfo_children())
            except Exception:
                pass
            index += 1
        enabled = False
        seen: set[int] = set()
        for widget in targets:
            try:
                ident = id(widget)
                if ident in seen:
                    continue
                seen.add(ident)
                enabled = self.enable_drop_target(widget) or enabled
            except Exception:
                pass
        if not enabled:
            try:
                self.log("Drag-and-drop is unavailable. Install/rebuild with tkinterdnd2, or use Add files.")
            except Exception:
                pass

    def build_dashboard(self) -> None:
        header = self.card_frame(self.dashboard_frame, fill="x", pady=(0, 10))
        header.configure(padx=10, pady=7)
        left = tk.Frame(header, bg=self.CARD)
        left.pack(side="left", fill="both", expand=True)
        title_row = tk.Frame(left, bg=self.CARD)
        title_row.pack(fill="x")
        tk.Label(title_row, text="Active files", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 15, "bold")).pack(side="left")
        self.session_summary_var = tk.StringVar(value="")
        self.session_summary_label = tk.Label(title_row, textvariable=self.session_summary_var, bg=self.CARD2, fg="#b9c7d8", font=("Segoe UI", 9, "bold"), anchor="w", padx=8, pady=3)
        self.update_session_summary()
        right = tk.Frame(header, bg=self.CARD)
        right.pack(side="right")
        self.add_files_button = self.button(right, "Add files", self.add_files_dialog, self.CARD3)
        self.add_files_button.pack(side="left", padx=(0, 8))
        self.scan_button = self.button(right, "Scan now", self.manual_scan, self.CARD3)
        self.scan_button.pack(side="left", padx=(0, 8))
        self.convert_in_place_button = self.button(right, "Convert in place", self.convert_in_place_selected, self.CARD3)
        self.convert_in_place_button.pack(side="left")
        self.batch_frame = tk.Frame(left, bg=self.CARD2, padx=10, pady=10)
        self.batch_name_var = tk.StringVar()
        self.batch_prefix_var = tk.BooleanVar(value=False)
        self.batch_suffix_var = tk.BooleanVar(value=False)
        tk.Label(self.batch_frame, text="Batch rename", bg=self.CARD2, fg=self.TEXT, font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Entry(self.batch_frame, textvariable=self.batch_name_var, bg=self.CARD3, fg=self.TEXT, insertbackground=self.TEXT, relief="flat", width=26).pack(side="left", padx=(10, 8), ipady=3)
        self.batch_prefix_btn = tk.Checkbutton(
            self.batch_frame,
            text="Prefix",
            variable=self.batch_prefix_var,
            command=lambda: self.on_batch_affix_toggle("prefix"),
            indicatoron=False,
            bg=self.CARD3,
            fg=self.TEXT,
            selectcolor=self.BLUE,
            activebackground=self.BLUE,
            activeforeground=self.TEXT,
            relief="flat",
            bd=0,
            padx=10,
            pady=5,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        self.batch_prefix_btn.pack(side="left", padx=(0, 6))
        self.batch_suffix_btn = tk.Checkbutton(
            self.batch_frame,
            text="Suffix",
            variable=self.batch_suffix_var,
            command=lambda: self.on_batch_affix_toggle("suffix"),
            indicatoron=False,
            bg=self.CARD3,
            fg=self.TEXT,
            selectcolor=self.BLUE,
            activebackground=self.BLUE,
            activeforeground=self.TEXT,
            relief="flat",
            bd=0,
            padx=10,
            pady=5,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        self.batch_suffix_btn.pack(side="left", padx=(0, 8))
        self.small_button(self.batch_frame, "Apply", self.apply_batch_box_rename, self.GREEN).pack(side="left")
        self.small_button(self.batch_frame, "Skip selected", self.skip_selected_tasks, self.RED).pack(side="left", padx=(8, 0))
        self.batch_hint_var = tk.StringVar(value="")
        tk.Label(self.batch_frame, textvariable=self.batch_hint_var, bg=self.CARD2, fg=self.MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(10, 0))
        self.dashboard_canvas, self.dashboard_inner = self.make_canvas_area(self.dashboard_frame)
        self.dashboard_top_spacer = tk.Frame(self.dashboard_inner, bg=self.BG, height=1)
        self.dashboard_top_spacer.pack(fill="x")
        self.dashboard_top_spacer.pack_propagate(False)
        self.dashboard_bottom_spacer = tk.Frame(self.dashboard_inner, bg=self.BG, height=1)
        self.dashboard_bottom_spacer.pack(fill="x")
        self.dashboard_bottom_spacer.pack_propagate(False)
        self.dashboard_canvas._view_changed_callback = self.schedule_dashboard_virtual_refresh  # type: ignore[attr-defined]
        self.enable_dashboard_drop_targets(header, left, right, self.dashboard_frame, self.dashboard_canvas, self.dashboard_inner)
    def build_history(self) -> None:
        header = self.card_frame(self.history_frame, fill="x", pady=(0, 10))
        left = tk.Frame(header, bg=self.CARD)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="History", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(left, text="Completed, converted-only, and skipped files.", bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 0))
        self.button(header, "Clear history", self.clear_history, self.CARD3).pack(side="right")
        self.history_canvas, self.history_inner = self.make_canvas_area(self.history_frame)

    def update_compact_dashboard_button(self) -> None:
        button = getattr(self, "compact_dashboard_btn", None)
        if not button:
            return
        button.configure(
            text="Compact: On" if self.compact_dashboard else "Compact: Off",
            bg=self.BLUE if self.compact_dashboard else self.CARD3,
            activebackground=self.BLUE if self.compact_dashboard else self.CARD3,
        )

    def toggle_compact_dashboard(self) -> None:
        self.compact_dashboard = not self.compact_dashboard
        self.update_compact_dashboard_button()
        self.apply_dashboard_density()
        self.schedule_config_autosave()

    def apply_dashboard_density(self) -> None:
        for task_id in list(self.row_widgets.keys()):
            self.apply_row_density(task_id)
            if not self.compact_dashboard:
                self.render_task(task_id)
        self.refresh_dashboard_virtual_rows(force=True)

    def apply_row_density(self, task_id: str) -> None:
        row = self.row_widgets.get(task_id)
        if not row:
            return
        compact = self.compact_dashboard
        thumb_width, thumb_height = (142, 80) if compact else (222, 125)
        row["outer"].pack_configure(pady=3 if compact else 7)
        row["card"].configure(padx=8 if compact else 12, pady=7 if compact else 12)
        row["thumb_frame"].configure(width=thumb_width, height=thumb_height)
        row["right"].pack_configure(padx=(10 if compact else 14, 0))
        row["rename_entry"].configure(font=("Segoe UI", 10 if compact else 11))
        row["rename_entry"].pack_configure(ipady=2 if compact else 4)
        row["stage"].configure(padx=8 if compact else 10, pady=3 if compact else 4)
        row["preset_badges"].pack_configure(pady=(3 if compact else 5, 0))
        row["detail"].pack_configure(pady=(4 if compact else 8, 0))
        row["buttons"].pack_configure(pady=(5 if compact else 8, 0))
        row["preset_combo"].configure(width=16 if compact else 22)
        if compact:
            row["safe_name"].pack_forget()
            row["times"].pack_forget()
        else:
            if not row["safe_name"].winfo_ismapped():
                row["safe_name"].pack(fill="x", pady=(8, 0), before=row["preset_badges"])
            if not row["times"].winfo_ismapped():
                row["times"].pack(fill="x", pady=(5, 6), before=row["progress_wrap"])
        row["displayed_thumb_path"] = None
        self.apply_thumbnail(task_id)

    def build_settings(self) -> None:
        if self.settings_built:
            return
        canvas, inner = self.make_canvas_area(self.settings_frame)
        self.settings_canvas = canvas
        self.settings_inner = inner

        category_nav = self.card_frame(inner, fill="x", pady=(0, 10))
        tk.Label(category_nav, text="Settings", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 16, "bold")).pack(side="left", padx=(0, 16))
        self.settings_category_buttons: Dict[str, tk.Button] = {}
        categories = [
            ("folders", "Folders & tools"),
            ("drives", "Drives & file types"),
            ("safety", "Safety & timing"),
            ("presets", "Presets & test"),
            ("updates", "About & updates"),
        ]
        for key, label in categories:
            button = self.small_button(category_nav, label, lambda k=key: self.show_settings_category(k), self.CARD3)
            button.pack(side="left", padx=(0, 8))
            self.settings_category_buttons[key] = button
        self.settings_category_host = tk.Frame(inner, bg=self.BG)
        self.settings_category_host.pack(fill="both", expand=True)
        self.settings_categories = {
            key: tk.Frame(self.settings_category_host, bg=self.BG)
            for key, _label in categories
        }

        paths = self.card_frame(self.settings_categories["folders"], fill="x", pady=(0, 10))
        tk.Label(paths, text="Folders and FFmpeg", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        grid = tk.Frame(paths, bg=self.CARD)
        grid.pack(fill="x", pady=(10, 0))
        grid.grid_columnconfigure(0, weight=1)
        grid.grid_columnconfigure(1, weight=1)
        self.add_path_box(grid, 0, 0, "Importfolder", "Files imported from drives or added manually are copied here before conversion.", self.input_folder_var, True)
        self.add_path_box(grid, 0, 1, "Output folder", "Converted files stay here if Transfer is turned off. They are copied from here to the NAS when Transfer is on.", self.output_folder_var, True)
        self.add_path_box(grid, 1, 0, "NAS folder", "Files end up here after a verified transfer. Local files are deleted only after verification.", self.nas_folder_var, True)
        self.add_path_box(grid, 1, 1, "FFmpeg.exe", "Path to ffmpeg.exe used for conversion and thumbnails.", self.ffmpeg_path_var, False)
        settings_buttons = tk.Frame(paths, bg=self.CARD)
        settings_buttons.pack(anchor="e", pady=(10, 0))
        self.small_button(settings_buttons, "Run setup wizard", self.run_setup_wizard, self.CARD3).pack(side="left", padx=(0, 8))
        self.small_button(settings_buttons, "Export settings backup", self.export_settings_backup, self.CARD3).pack(side="left", padx=(0, 8))
        self.small_button(settings_buttons, "Import settings backup", self.import_settings_backup, self.CARD3).pack(side="left", padx=(0, 8))
        self.health_check_button = self.small_button(settings_buttons, "Run health check", self.run_health_check, self.BLUE)
        self.health_check_button.pack(side="left")

        dex = self.card_frame(self.settings_categories["drives"], fill="x", pady=(0, 10))
        tk.Label(dex, text="Drives and extensions", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        cgrid = tk.Frame(dex, bg=self.CARD)
        cgrid.pack(fill="x", pady=(10, 0))
        for i in range(3):
            cgrid.grid_columnconfigure(i, weight=1, uniform="chips")
        colors = {"card2": self.CARD2, "card3": self.CARD3, "border": self.BORDER, "text": self.TEXT, "muted": self.MUTED, "blue": self.BLUE}
        drive_options = list_windows_drives() or [f"{chr(65+i)}:" for i in range(26)]
        self.ignored_drives_widget = ChipSelector(cgrid, "Ignored drives", "Drives to ignore. This should probably be all your internal drives. If editing manually, separate drives with commas.", self.config.get("ignored_drives", []), drive_options, colors, self.on_setting_changed)
        self.import_ext_widget = ChipSelector(cgrid, "Import extensions", "Extensions to import from a new detected drive. Default: .mp4, .mov. If editing manually, separate extensions with commas.", self.config.get("import_extensions", []), COMMON_VIDEO_EXTENSIONS, colors, self.on_setting_changed)
        self.convert_ext_widget = ChipSelector(cgrid, "Convert extensions", "Extensions the converter accepts. Default includes mp4, mkv, avi, mov, m4v, flv, wmv and webm. If editing manually, separate extensions with commas.", self.config.get("conversion_extensions", []), COMMON_VIDEO_EXTENSIONS, colors, self.on_setting_changed)
        self.ignored_drives_widget.frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.import_ext_widget.frame.grid(row=0, column=1, sticky="nsew", padx=8)
        self.convert_ext_widget.frame.grid(row=0, column=2, sticky="nsew", padx=(8, 0))

        misc = self.card_frame(self.settings_categories["safety"], fill="x", pady=(0, 10))
        tk.Label(misc, text="Safety and timing", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        mgrid = tk.Frame(misc, bg=self.CARD)
        mgrid.pack(fill="x", pady=(10, 0))
        for i in range(4):
            mgrid.grid_columnconfigure(i, weight=1)
        self.add_small_field(mgrid, 0, 0, "Auto-import delay", "Delay before auto-importing from a new detected drive. Default = 5 seconds.", self.auto_delay_var)
        self.add_small_field(mgrid, 0, 1, "Scan interval", "How often the app checks for newly inserted drives. Default = 3 seconds.", self.scan_interval_var)
        self.add_small_field(mgrid, 0, 2, "Stable seconds", "How long a file size must stay unchanged before conversion. Default = 10 seconds.", self.stable_seconds_var)
        self.add_small_field(mgrid, 0, 3, "Copy chunk MB", "Copy buffer size. Default = 4 MB.", self.copy_chunk_var)
        self.add_small_field(mgrid, 1, 0, "Max ready wait", "Maximum wait for a file to become ready. Default = 7200 seconds.", self.max_wait_var)
        self.add_small_field(mgrid, 1, 1, "Low disk warning GB", "Warn before import, conversion, or transfer unless free space is at least file size plus this reserve. Default = 20 GB.", self.low_disk_warning_var)
        self.add_small_field(mgrid, 1, 2, "Exclude files", "Ignored filename patterns. Default excludes Mac metadata files. Separate multiple patterns with commas.", self.exclude_files_var)
        self.add_small_field(mgrid, 1, 3, "Exclude folders", "Ignored folder names. Default excludes Mac system folders. Separate multiple folder names with commas.", self.exclude_folders_var)
        self.add_browse_field(
            mgrid,
            2,
            0,
            "Checksum database",
            "Stores duplicate hashes used to avoid importing exact duplicate videos. Default is inside the data folder.",
            self.checksum_path_var,
            save_file=True,
            action_text="Clean",
            action_command=self.cleanup_checksum_database,
        )
        checkrow = tk.Frame(misc, bg=self.CARD)
        checkrow.pack(fill="x", pady=(10, 0))
        self.add_checkbox(checkrow, "Enable duplicate detection", self.duplicate_var)
        self.add_checkbox(checkrow, "Check same-name watchfolder files by exact content", self.check_input_var)
        self.add_checkbox(checkrow, "Change spaces to underscores in renamed files", self.auto_underscore_var)
        self.add_checkbox(checkrow, "Prevent sleep while working", self.prevent_sleep_var)
        self.small_button(checkrow, "Clean thumbnails", self.cleanup_thumbnail_cache, self.CARD3).pack(side="left", padx=(12, 0))

        ff = self.card_frame(self.settings_categories["presets"], fill="both", expand=True, pady=(0, 10))
        tk.Label(ff, text="FFmpeg parameters and presets", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        folder_row = tk.Frame(ff, bg=self.CARD2, padx=10, pady=10, highlightthickness=2, highlightbackground=self.BORDER)
        folder_row.pack(fill="x", pady=(10, 8))
        tk.Label(folder_row, text="FFmpeg preset folder", bg=self.CARD2, fg=self.TEXT, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(folder_row, text="Choose the folder that contains .txt preset files. Press Refresh list after adding new presets to this folder.", bg=self.CARD2, fg=self.MUTED, font=("Segoe UI", 8), wraplength=780, justify="left").pack(anchor="w", pady=(2, 8))
        folder_controls = tk.Frame(folder_row, bg=self.CARD2)
        folder_controls.pack(fill="x")
        tk.Entry(folder_controls, textvariable=self.preset_folder_var, bg=self.CARD3, fg=self.TEXT, insertbackground=self.TEXT, relief="flat").pack(side="left", fill="x", expand=True, ipady=5)
        self.small_button(folder_controls, "Browse", self.browse_preset_folder, self.CARD3).pack(side="left", padx=(8, 0))
        self.small_button(folder_controls, "Refresh list", self.refresh_preset_menus, self.BLUE).pack(side="left", padx=(8, 0))

        preset_row = tk.Frame(ff, bg=self.CARD)
        preset_row.pack(fill="x", pady=(0, 8))
        self.preset_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(preset_row, textvariable=self.preset_var, values=self.list_presets(), state="readonly", width=28)
        self.preset_combo.pack(side="left")
        self.preset_combo.bind("<<ComboboxSelected>>", lambda e: self.load_selected_preset())
        self.small_button(preset_row, "Import .txt", self.import_preset, self.CARD3).pack(side="left", padx=(8, 0))
        self.small_button(preset_row, "Refresh list", self.refresh_preset_menus, self.CARD3).pack(side="left", padx=(8, 0))
        self.small_button(preset_row, "Export .txt", self.export_preset, self.CARD3).pack(side="left", padx=(8, 0))
        self.small_button(preset_row, "Reset defaults", self.reset_ffmpeg_defaults, self.CARD3).pack(side="left", padx=(8, 0))
        self.output_ext_var = tk.StringVar(value=f"Detected output extension: {detect_output_extension(self.config['ffmpeg_args'])}")
        tk.Label(preset_row, textvariable=self.output_ext_var, bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9)).pack(side="right")
        self.preset_warning_var = tk.StringVar(value=self.preset_warning_text(self.config["ffmpeg_args"]))
        tk.Label(ff, textvariable=self.preset_warning_var, bg=self.CARD, fg=self.YELLOW, font=("Segoe UI", 9, "bold"), anchor="w", justify="left", wraplength=1180).pack(fill="x", pady=(0, 8))
        self.ffmpeg_args_text = scrolledtext.ScrolledText(ff, height=12, wrap="none", font=("Consolas", 9), bg="#07111e", fg="#d7e8ff", insertbackground="#d7e8ff", relief="flat")
        self.ffmpeg_args_text.pack(fill="both", expand=True)
        self.ffmpeg_args_text.insert("1.0", "\n".join(self.config["ffmpeg_args"]))
        self.ffmpeg_args_text.bind("<KeyRelease>", lambda e: self.on_ffmpeg_args_changed())

        self.build_preset_test(self.settings_categories["presets"])

        updates = self.card_frame(self.settings_categories["updates"], fill="x", pady=(0, 10))
        tk.Label(updates, text="About and updates", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(
            updates,
            text="MesterSync checks GitHub Releases in the background at most once per day. Updates are downloaded only when you choose to install them, and the installer is verified before it can run.",
            bg=self.CARD,
            fg=self.MUTED,
            font=("Segoe UI", 9),
            justify="left",
            wraplength=900,
        ).pack(anchor="w", pady=(6, 12))
        self.update_status_var = tk.StringVar(value=f"Installed version: {APP_VERSION}")
        tk.Label(updates, textvariable=self.update_status_var, bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 11, "bold"), justify="left").pack(anchor="w")
        update_actions = tk.Frame(updates, bg=self.CARD)
        update_actions.pack(anchor="w", pady=(12, 0))
        self.update_action_button = self.small_button(update_actions, "Check for updates", lambda: self.check_for_updates(manual=True), self.BLUE)
        self.update_action_button.pack(side="left", padx=(0, 8))
        self.update_check_button = self.small_button(update_actions, "Check again", lambda: self.check_for_updates(manual=True), self.CARD3)
        self.update_check_button.pack(side="left")
        self.refresh_update_ui()

        footer = tk.Frame(inner, bg=self.BG)
        footer.pack(fill="x", pady=(0, 20))
        tk.Label(footer, text=f"{APP_NAME} v{APP_VERSION}", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 20))
        self.total_converted_var = tk.StringVar(value=f"Total converted with MesterSync: {self.total_converted_count()}")
        tk.Label(footer, textvariable=self.total_converted_var, bg=self.BG, fg=self.MUTED, font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Label(footer, text="Settings save automatically", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(side="right", padx=(0, 10))

        for var in [
            self.input_folder_var,
            self.output_folder_var,
            self.nas_folder_var,
            self.ffmpeg_path_var,
            self.exclude_files_var,
            self.exclude_folders_var,
            self.auto_delay_var,
            self.scan_interval_var,
            self.stable_seconds_var,
            self.copy_chunk_var,
            self.max_wait_var,
            self.low_disk_warning_var,
            self.checksum_path_var,
            self.preset_folder_var,
        ]:
            var.trace_add("write", lambda *_args: self.on_setting_changed())
        self.show_settings_category("folders")
        self.settings_built = True
        self.settings_building = False

    def show_settings_category(self, key: str) -> None:
        categories = getattr(self, "settings_categories", {})
        if key not in categories:
            return
        for name, frame in categories.items():
            if name == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for name, button in getattr(self, "settings_category_buttons", {}).items():
            color = self.BLUE if name == key else self.CARD3
            button.configure(bg=color, activebackground=color)
        canvas = getattr(self, "settings_canvas", None)
        if canvas is not None:
            self.root.after_idle(lambda: canvas.yview_moveto(0) if canvas.winfo_exists() else None)

    def build_preset_test(self, parent: tk.Widget) -> None:
        card = self.card_frame(parent, fill="x", pady=(0, 10))
        tk.Label(card, text="Preset test", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 18, "bold")).pack(anchor="w")
        tk.Label(
            card,
            text="Convert a 15-second sample from the middle of a selected video using the current FFmpeg parameters from Settings. The selected source is never changed. After a successful test, the previous test video is deleted automatically.",
            bg=self.CARD,
            fg=self.MUTED,
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=900,
        ).pack(fill="x", pady=(6, 18))
        status_box = tk.Frame(card, bg=self.CARD2, padx=14, pady=14, highlightthickness=2, highlightbackground=self.BORDER)
        status_box.pack(fill="x")
        self.preset_test_status_var = tk.StringVar(value="No test has been run this session.")
        self.preset_test_status_label = tk.Label(status_box, textvariable=self.preset_test_status_var, bg=self.CARD2, fg=self.MUTED, font=("Segoe UI", 10, "bold"), anchor="w")
        self.preset_test_status_label.pack(fill="x")
        buttons = tk.Frame(card, bg=self.CARD)
        buttons.pack(anchor="w", pady=(14, 0))
        self.preset_test_run_btn = self.button(buttons, "Run 15s test", self.start_or_cancel_preset_test, self.BLUE)
        self.preset_test_run_btn.pack(side="left")
        self.preset_test_open_btn = self.button(buttons, "Open last test", self.open_last_preset_test, self.CARD3)
        self.preset_test_open_btn.pack(side="left", padx=(10, 0))
        self.preset_test_open_btn.configure(state="disabled")

    def update_total_converted_label(self) -> None:
        if hasattr(self, "total_converted_var"):
            self.total_converted_var.set(f"Total converted with MesterSync: {self.total_converted_count()}")

    def update_session_summary(self) -> None:
        if not hasattr(self, "session_summary_var"):
            return
        has_activity = any(self.session_stats.values()) or self.session_bytes_saved > 0
        if not has_activity or self.session_summary_hidden_after_idle:
            self.session_summary_var.set("")
            if hasattr(self, "session_summary_label") and self.session_summary_label.winfo_ismapped():
                self.session_summary_label.pack_forget()
            return
        saved = f" | Saved: {format_size(self.session_bytes_saved)}" if self.session_bytes_saved > 0 else ""
        self.session_summary_var.set(
            f"This session: Imported {self.session_stats['imported']} | Converted {self.session_stats['converted']} | "
            f"Transferred {self.session_stats['transferred']} | Skipped {self.session_stats['skipped']} | Errors {self.session_stats['errors']}{saved}"
        )
        if hasattr(self, "session_summary_label") and not self.session_summary_label.winfo_ismapped():
            self.session_summary_label.pack(side="left", padx=(12, 0))

    def record_session_stat(self, key: str, amount: int = 1) -> None:
        if key not in self.session_stats:
            return
        self.session_stats[key] += amount
        self.session_summary_hide_due = None
        self.session_summary_hidden_after_idle = False
        self.gui_queue.put(("session", None))

    def record_session_saved_bytes(self, byte_count: int) -> None:
        if byte_count <= 0:
            return
        self.session_bytes_saved += byte_count
        self.session_summary_hide_due = None
        self.session_summary_hidden_after_idle = False
        self.gui_queue.put(("session", None))

    def refresh_session_summary_idle_timer(self) -> None:
        if not hasattr(self, "session_summary_var"):
            return
        has_activity = any(self.session_stats.values()) or self.session_bytes_saved > 0
        if not has_activity:
            self.session_summary_hide_due = None
            self.session_summary_hidden_after_idle = False
            self.update_session_summary()
            return
        with self.task_lock:
            has_active_files = any(
                not task.skip_archive_due
                and task.stage not in {STAGE_COMPLETE, STAGE_CONVERTED_OUTPUT, STAGE_CONVERTED_IN_PLACE}
                for task in self.tasks.values()
            )
        if has_active_files:
            self.session_summary_hide_due = None
            if self.session_summary_hidden_after_idle:
                self.session_summary_hidden_after_idle = False
                self.update_session_summary()
            return
        if self.session_summary_hide_due is None:
            self.session_summary_hide_due = time.time() + 300
            return
        if not self.session_summary_hidden_after_idle and time.time() >= self.session_summary_hide_due:
            self.session_summary_hidden_after_idle = True
            self.update_session_summary()

    def update_thumbnail_queue_status(self, active: bool = False) -> None:
        if not hasattr(self, "thumbnail_queue_var"):
            return
        queued = self.thumbnail_queue.qsize()
        pending = len(self.thumbnail_generation_requested)
        if active:
            self.thumbnail_queue_var.set(f"Previews: generating now ({max(0, pending - 1)} queued)")
        elif pending:
            self.thumbnail_queue_var.set(f"Previews: {pending} queued, starting soon")
        elif queued:
            self.thumbnail_queue_var.set(f"Previews: {queued} queued")
        else:
            self.thumbnail_queue_var.set("Previews: idle")
        if hasattr(self, "thumbnail_queue_label"):
            should_show = active or pending > 0 or queued > 0
            if should_show and not self.thumbnail_queue_label.winfo_ismapped():
                self.thumbnail_queue_label.pack(side="left", padx=(12, 0))
            elif not should_show and self.thumbnail_queue_label.winfo_ismapped():
                self.thumbnail_queue_label.pack_forget()

    def add_path_box(self, parent: tk.Widget, row: int, col: int, label: str, help_text: str, var: tk.StringVar, folder: bool) -> None:
        box = tk.Frame(parent, bg=self.CARD2, padx=12, pady=12, highlightthickness=2, highlightbackground=self.BORDER)
        box.grid(row=row, column=col, sticky="nsew", padx=7, pady=7)
        tk.Label(box, text=label, bg=self.CARD2, fg=self.TEXT, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(box, text=help_text, bg=self.CARD2, fg=self.MUTED, font=("Segoe UI", 9), wraplength=500, justify="left").pack(anchor="w", pady=(3, 8))
        entry = tk.Entry(box, textvariable=var, bg=self.CARD3, fg=self.TEXT, insertbackground=self.TEXT, relief="flat")
        entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.small_button(box, "Browse", (lambda v=var: self.browse_folder(v)) if folder else (lambda v=var: self.browse_file(v)), self.CARD3).pack(side="left", padx=(8, 0))

    def add_small_field(self, parent: tk.Widget, row: int, col: int, label: str, help_text: str, var: tk.StringVar) -> None:
        box = tk.Frame(parent, bg=self.CARD2, padx=10, pady=10, highlightthickness=2, highlightbackground=self.BORDER)
        box.grid(row=row, column=col, sticky="nsew", padx=7, pady=7)
        tk.Label(box, text=label, bg=self.CARD2, fg=self.TEXT, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(box, text=help_text, bg=self.CARD2, fg=self.MUTED, font=("Segoe UI", 8), wraplength=240, justify="left").pack(anchor="w", pady=(2, 8))
        tk.Entry(box, textvariable=var, bg=self.CARD3, fg=self.TEXT, insertbackground=self.TEXT, relief="flat").pack(fill="x", ipady=5)

    def add_browse_field(
        self,
        parent: tk.Widget,
        row: int,
        col: int,
        label: str,
        help_text: str,
        var: tk.StringVar,
        save_file: bool = False,
        action_text: str = "",
        action_command: Optional[Callable[[], None]] = None,
    ) -> None:
        box = tk.Frame(parent, bg=self.CARD2, padx=10, pady=10, highlightthickness=2, highlightbackground=self.BORDER)
        box.grid(row=row, column=col, sticky="nsew", padx=7, pady=7)
        tk.Label(box, text=label, bg=self.CARD2, fg=self.TEXT, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(box, text=help_text, bg=self.CARD2, fg=self.MUTED, font=("Segoe UI", 8), wraplength=240, justify="left").pack(anchor="w", pady=(2, 8))
        row_frame = tk.Frame(box, bg=self.CARD2)
        row_frame.pack(fill="x")
        tk.Entry(row_frame, textvariable=var, bg=self.CARD3, fg=self.TEXT, insertbackground=self.TEXT, relief="flat").pack(side="left", fill="x", expand=True, ipady=5)
        command = (lambda v=var: self.browse_save_file(v)) if save_file else (lambda v=var: self.browse_file(v))
        self.small_button(row_frame, "Browse", command, self.CARD3).pack(side="left", padx=(8, 0))
        if action_text and action_command:
            self.small_button(row_frame, action_text, action_command, self.CARD3).pack(side="left", padx=(8, 0))

    def add_checkbox(self, parent: tk.Widget, label: str, var: tk.BooleanVar) -> None:
        tk.Checkbutton(parent, text=label, variable=var, command=self.on_setting_changed, bg=self.CARD, fg=self.TEXT, selectcolor=self.CARD3, activebackground=self.CARD, activeforeground=self.TEXT).pack(anchor="w")

    def on_setting_changed(self) -> None:
        # Settings edits should never remove or rebuild active Dashboard tasks.
        # They only mark the config as unsaved. Active conversions keep their
        # existing task state and FFmpeg arguments.
        self.update_unsaved_settings_warning()
        self.update_batch_box_visibility()
        if hasattr(self, "prevent_sleep_var") and not self.prevent_sleep_var.get():
            self.set_sleep_prevention(False)
        self.schedule_config_autosave()

    def on_ffmpeg_args_changed(self) -> None:
        self.update_preset_status()
        self.on_setting_changed()

    def show_disk_ready(self, text: str) -> None:
        if not text:
            self.hide_disk_ready()
            return
        self.disk_ready_var.set(text)
        if not self.disk_ready_frame.winfo_ismapped():
            self.disk_ready_frame.pack(side="left", padx=(12, 0))
        try:
            self.root.bell()
        except tk.TclError:
            pass

    def hide_disk_ready(self) -> None:
        if hasattr(self, "disk_ready_frame") and self.disk_ready_frame.winfo_ismapped():
            self.disk_ready_frame.pack_forget()
        if hasattr(self, "disk_ready_var"):
            self.disk_ready_var.set("")

    def browse_folder(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if path:
            var.set(path)

    def browse_file(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(initialdir=str(Path(var.get()).parent) if var.get() else str(Path.home()))
        if path:
            var.set(path)

    def browse_save_file(self, var: tk.StringVar) -> None:
        initial = Path(var.get()) if var.get() else default_checksum_path()
        path = filedialog.asksaveasfilename(
            initialdir=str(initial.parent),
            initialfile=initial.name,
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if path:
            var.set(path)

    # ---------------- presets ----------------
    def current_ffmpeg_args_from_ui(self) -> List[str]:
        if hasattr(self, "ffmpeg_args_text"):
            args = [line.strip() for line in self.ffmpeg_args_text.get("1.0", "end").splitlines() if line.strip()]
            if args:
                return args
        return list(self.get_config().get("ffmpeg_args", DEFAULT_FFMPEG_ARGS))

    def update_preset_status(self, check_encoder: bool = False) -> None:
        args = self.current_ffmpeg_args_from_ui()
        if hasattr(self, "output_ext_var"):
            self.output_ext_var.set(f"Detected output extension: {detect_output_extension(args)}")
        if hasattr(self, "preset_warning_var"):
            text = self.preset_warning_text(args, check_encoder=check_encoder)
            self.preset_warning_var.set(text)
        self.update_unsaved_settings_warning()

    def get_preset_folder(self) -> Path:
        value = ""
        try:
            if hasattr(self, "preset_folder_var"):
                value = self.preset_folder_var.get().strip()
            else:
                value = str(self.get_config().get("preset_folder", "")).strip()
        except Exception:
            value = ""
        folder = Path(value) if value else preset_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            return preset_dir()
        return folder

    def browse_preset_folder(self) -> None:
        initial = str(self.get_preset_folder())
        selected = filedialog.askdirectory(initialdir=initial)
        if selected:
            self.preset_folder_var.set(selected)
            self.refresh_preset_menus()
            self.update_unsaved_settings_warning()

    def read_preset_args(self, name: str) -> Optional[List[str]]:
        if not name or name == "Current settings":
            return None
        path = self.get_preset_folder() / f"{name}.txt"
        if not path.exists():
            return None
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def preset_menu_values(self) -> List[str]:
        cached = getattr(self, "preset_menu_values_cache", None)
        if cached is None:
            cached = ["Current settings"] + self.list_presets()
            self.preset_menu_values_cache = cached
        return list(cached)

    def refresh_preset_menus(self) -> None:
        self.preset_menu_values_cache = None
        values = self.preset_menu_values()
        if hasattr(self, "top_preset_combo"):
            self.top_preset_combo.configure(values=values)
            if self.top_preset_var.get() not in values:
                self.top_preset_var.set("Current settings")
                self.save_top_preset_choice()
        if hasattr(self, "preset_combo"):
            self.preset_combo.configure(values=self.list_presets())
        for row in getattr(self, "row_widgets", {}).values():
            combo = row.get("preset_combo")
            if combo is not None:
                combo.configure(values=values)
                row["preset_values_key"] = tuple(values)
        try:
            self.log(f"Preset list refreshed from: {self.get_preset_folder()}")
        except Exception:
            pass

    def load_preset_into_settings(self, name: str) -> None:
        args = self.read_preset_args(name)
        if not args:
            return
        if hasattr(self, "ffmpeg_args_text"):
            self.ffmpeg_args_text.delete("1.0", "end")
            self.ffmpeg_args_text.insert("1.0", "\n".join(args))
        if hasattr(self, "preset_var"):
            self.preset_var.set(name)
        if hasattr(self, "top_preset_var"):
            self.top_preset_var.set(name)
        self.update_preset_status()
        self.schedule_config_autosave()

    def load_top_preset(self) -> None:
        name = self.top_preset_var.get() or "Current settings"
        if name != "Current settings":
            self.load_preset_into_settings(name)
        self.save_top_preset_choice()

    def save_top_preset_choice(self) -> None:
        """Persist the top-right default preset immediately, without requiring Save settings."""
        name = self.top_preset_var.get() if hasattr(self, "top_preset_var") else "Current settings"
        name = name or "Current settings"
        args = self.read_preset_args(name) if name != "Current settings" else None
        try:
            if self.config_save_after_id is not None:
                self.root.after_cancel(self.config_save_after_id)
                self.config_save_after_id = None
            with self.config_lock:
                if hasattr(self, "preset_folder_var"):
                    self.config["preset_folder"] = self.preset_folder_var.get().strip()
                self.config["default_preset_name"] = name
                if args:
                    self.config["ffmpeg_args"] = args
                write_json_file(config_path(), self.config)
            self.saved_settings_snapshot = self.current_settings_snapshot()
            self.update_unsaved_settings_warning()
            self.log(f"Default preset saved: {name}")
        except Exception as exc:
            self.log(f"Warning: could not auto-save default preset: {exc}")

    def ensure_default_preset(self) -> None:
        default = self.get_preset_folder() / "Default_HEVC_NVENC.txt"
        if not default.exists():
            default.write_text("\n".join(DEFAULT_FFMPEG_ARGS), encoding="utf-8")

    def list_presets(self) -> List[str]:
        self.ensure_default_preset()
        return [p.stem for p in sorted(self.get_preset_folder().glob("*.txt"))]

    def load_selected_preset(self) -> None:
        name = self.preset_var.get()
        self.load_preset_into_settings(name)

    def import_preset(self) -> None:
        src = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if src:
            dst = unique_path(self.get_preset_folder() / Path(src).name)
            shutil.copy2(src, dst)
            self.refresh_preset_menus()
            self.preset_var.set(dst.stem)
            self.load_selected_preset()

    def export_preset(self) -> None:
        dst = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt")])
        if dst:
            Path(dst).write_text(self.ffmpeg_args_text.get("1.0", "end").strip() + "\n", encoding="utf-8")

    def save_current_preset_default(self) -> None:
        self.ensure_default_preset()

    def reset_ffmpeg_defaults(self) -> None:
        self.ffmpeg_args_text.delete("1.0", "end")
        self.ffmpeg_args_text.insert("1.0", "\n".join(DEFAULT_FFMPEG_ARGS))
        if hasattr(self, "top_preset_var"):
            self.top_preset_var.set("Current settings")
        if hasattr(self, "preset_var"):
            self.preset_var.set("")
        self.update_preset_status()
        self.schedule_config_autosave()

    def update_preset_test_ui(self, status: str, running: bool, path_value: str = "", error: bool = False, show_error: bool = False) -> None:
        if hasattr(self, "preset_test_status_var"):
            self.preset_test_status_var.set(status)
        if hasattr(self, "preset_test_status_label"):
            color = self.RED if error else (self.YELLOW if running else self.MUTED)
            self.preset_test_status_label.configure(fg=color)
        if hasattr(self, "preset_test_run_btn"):
            self.preset_test_run_btn.configure(
                text="Cancel test" if running else "Run 15s test",
                bg=self.RED if running else self.BLUE,
                activebackground=self.RED if running else self.BLUE,
            )
        if path_value:
            self.preset_test_last_path = Path(path_value)
        if hasattr(self, "preset_test_open_btn"):
            available = bool(self.preset_test_last_path and self.preset_test_last_path.exists())
            self.preset_test_open_btn.configure(state="normal" if available else "disabled")
        if show_error:
            self.show_notification(status, "error")

    def start_or_cancel_preset_test(self) -> None:
        with self.preset_test_process_lock:
            running = self.preset_test_running
        if running:
            self.cancel_preset_test()
            return
        ffmpeg = self.resolve_ffmpeg_executable(self.ffmpeg_path_var.get())
        if not ffmpeg:
            self.show_notification("FFmpeg.exe was not found. Choose it in Settings before testing a preset.", "error")
            return
        source_value = filedialog.askopenfilename(
            title="Choose a video for the 15-second preset test",
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.avi *.m4v *.mts *.m2ts *.wmv *.webm"), ("All files", "*.*")],
        )
        if not source_value:
            return
        source = Path(source_value)
        args = self.current_ffmpeg_args_from_ui()
        warnings = self.preset_safety_warnings(args, check_encoder=False)
        if warnings:
            warning_text = "\n".join(f"- {warning}" for warning in warnings)
            if not messagebox.askyesno(APP_NAME, f"Preset safety warnings:\n\n{warning_text}\n\nRun the test anyway?"):
                return
        if self.current_conversion_id and not messagebox.askyesno(APP_NAME, "A normal conversion is already running. The preset test may slow it down.\n\nRun the test anyway?"):
            return
        preset_name = self.preset_var.get() or self.top_preset_var.get() or "Current settings"
        self.preset_test_cancel.clear()
        with self.preset_test_process_lock:
            self.preset_test_running = True
        self.update_preset_test_ui(f"Preparing a middle sample from {source.name}...", True)
        threading.Thread(
            target=self.run_preset_test_worker,
            args=(source, ffmpeg, list(args), preset_name),
            daemon=True,
            name="PresetTest",
        ).start()
        self.refresh_sleep_prevention()

    def cancel_preset_test(self) -> None:
        self.preset_test_cancel.set()
        self.update_preset_test_ui("Cancelling preset test...", True)
        with self.preset_test_process_lock:
            process = self.preset_test_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except Exception as exc:
                self.log(f"Preset test cancel warning: {exc}")

    def finish_preset_test(self, status: str, path: Optional[Path] = None, error: bool = False, show_error: bool = False) -> None:
        with self.preset_test_process_lock:
            self.preset_test_running = False
            self.preset_test_process = None
        if path:
            self.preset_test_last_path = path
        self.gui_queue.put(("preset_test_ui", {
            "status": status,
            "running": False,
            "path_value": str(path) if path else "",
            "error": error,
            "show_error": show_error,
        }))
        self.emit_status()

    def run_preset_test_worker(self, source: Path, ffmpeg: Path, ffmpeg_args: List[str], preset_name: str) -> None:
        output_path: Optional[Path] = None
        temp_output: Optional[Path] = None
        process: Optional[subprocess.Popen[str]] = None
        try:
            duration = self.get_duration(source, ffmpeg)
            if not duration or duration <= 0:
                self.finish_preset_test("Could not read the selected video's duration.", error=True, show_error=True)
                return
            start_at, sample_duration = preset_sample_window(duration)
            if self.preset_test_cancel.is_set() or self.shutdown_event.is_set():
                self.finish_preset_test("Preset test cancelled.")
                return
            folder = preset_test_dir()
            extension = detect_output_extension(ffmpeg_args)
            output_path, temp_output = preset_test_output_paths(folder, source, preset_name, extension)
            safe_args = [arg for arg in ffmpeg_args if str(arg).strip().lower() not in {"-y", "-n"}]
            command = [
                str(ffmpeg), "-n", "-hide_banner", "-ss", f"{start_at:.3f}", "-i", str(source),
            ] + safe_args + ["-t", f"{sample_duration:.3f}", str(temp_output)]
            self.emit_log(f"Starting preset test at {format_duration(start_at)}: {source.name} | Preset: {preset_name}")
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=no_window_flags(),
            )
            with self.preset_test_process_lock:
                self.preset_test_process = process
            time_pattern = re.compile(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
            last_percent = -1
            last_lines: List[str] = []
            assert process.stderr is not None
            for line in iter(process.stderr.readline, ""):
                if self.preset_test_cancel.is_set() or self.shutdown_event.is_set():
                    if process.poll() is None:
                        process.terminate()
                    break
                line = line.rstrip()
                if line:
                    last_lines.append(line)
                    last_lines = last_lines[-8:]
                match = time_pattern.search(line)
                if match:
                    current = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                    percent = min(100, int(current / sample_duration * 100)) if sample_duration else 100
                    if percent != last_percent:
                        last_percent = percent
                        self.gui_queue.put(("preset_test_ui", {
                            "status": f"Testing {preset_name}: {percent}%",
                            "running": True,
                        }))
            return_code = process.wait()
            if self.preset_test_cancel.is_set() or self.shutdown_event.is_set():
                if temp_output:
                    force_delete(temp_output)
                self.finish_preset_test("Preset test cancelled.")
                return
            if return_code != 0 or not temp_output.exists() or temp_output.stat().st_size <= 0:
                force_delete(temp_output)
                detail = " | ".join(last_lines[-3:]) or f"FFmpeg return code {return_code}"
                self.emit_log(f"Preset test failed: {detail}")
                self.finish_preset_test(f"Preset test failed: {detail[:220]}", error=True, show_error=True)
                return
            if not self.promote_temp_no_overwrite(temp_output, output_path):
                force_delete(temp_output)
                self.finish_preset_test("Preset test could not be saved without overwriting another file.", error=True, show_error=True)
                return
            validation_error = self.validate_converted_output(output_path, source, ffmpeg, safe_args, sample_duration)
            if validation_error:
                force_delete(output_path)
                self.finish_preset_test(f"Preset test failed validation: {validation_error}", error=True, show_error=True)
                return
            removed, cleanup_warnings = delete_previous_preset_tests(folder, output_path)
            if removed:
                self.emit_log(f"Deleted {removed} previous preset test video(s).")
            for warning in cleanup_warnings:
                self.emit_log(f"Preset test cleanup warning: {warning}")
            self.emit_log(f"Preset test complete: {output_path}")
            self.finish_preset_test(f"Ready: {output_path.name}", path=output_path)
        except Exception as exc:
            if temp_output:
                force_delete(temp_output)
            self.emit_log(f"Preset test error: {exc}")
            self.finish_preset_test(f"Preset test error: {exc}", error=True, show_error=True)
        finally:
            if process and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                except OSError as exc:
                    self.emit_log(f"Preset test process cleanup warning: {exc}")
            with self.preset_test_process_lock:
                if self.preset_test_process is process:
                    self.preset_test_process = None

    def open_last_preset_test(self) -> None:
        if not self.preset_test_last_path or not self.preset_test_last_path.exists():
            self.preset_test_last_path = None
            self.update_preset_test_ui("The last preset test is no longer available.", False)
            return
        self.open_path_with_feedback(self.preset_test_last_path, "preset test")

    # ---------------- updates ----------------
    def refresh_update_ui(self, status: Optional[str] = None) -> None:
        if not hasattr(self, "update_status_var"):
            return
        if status:
            self.update_status_var.set(status)
        elif self.downloaded_update and self.downloaded_update.exists() and self.latest_release:
            self.update_status_var.set(f"Version {self.latest_release.version} is downloaded and ready to install.")
        elif self.latest_release and is_newer_version(self.latest_release.version, APP_VERSION):
            self.update_status_var.set(f"Version {self.latest_release.version} is available. Installed: {APP_VERSION}")
        else:
            self.update_status_var.set(f"Installed version: {APP_VERSION}")

        if self.update_download_running:
            label = "Downloading..."
            command = lambda: None
            color = self.CARD3
            state = "disabled"
        elif self.update_check_running:
            label = "Checking..."
            command = lambda: None
            color = self.CARD3
            state = "disabled"
        elif self.downloaded_update and self.downloaded_update.exists():
            label = "Install downloaded update"
            command = self.install_downloaded_update
            color = self.GREEN
            state = "normal"
        elif self.latest_release and is_newer_version(self.latest_release.version, APP_VERSION):
            label = f"Download update {self.latest_release.version}"
            command = self.download_available_update
            color = self.GREEN
            state = "normal"
        else:
            label = "Check for updates"
            command = lambda: self.check_for_updates(manual=True)
            color = self.BLUE
            state = "normal"
        self.update_action_button.configure(text=label, command=command, bg=color, activebackground=color, state=state)

    def check_for_updates(self, manual: bool = False) -> None:
        if self.update_check_running or self.update_download_running or self.shutdown_event.is_set():
            return
        if not manual:
            try:
                if not should_check_for_updates():
                    return
            except Exception as exc:
                self.log(f"Could not read the update-check cache: {exc}")
        self.update_check_running = True
        self.refresh_update_ui("Checking GitHub for updates...")

        def worker() -> None:
            try:
                release = fetch_latest_release()
                try:
                    record_update_check(release)
                except Exception as exc:
                    self.emit_log(f"Could not save the update-check time: {exc}")
                self.gui_queue.put(("update_check_complete", (release, manual, None)))
            except Exception as exc:
                self.gui_queue.put(("update_check_complete", (None, manual, str(exc))))

        threading.Thread(target=worker, daemon=True, name="UpdateCheck").start()

    def finish_update_check(self, release: Optional[ReleaseInfo], manual: bool, error: Optional[str]) -> None:
        self.update_check_running = False
        if error:
            self.refresh_update_ui(f"Update check could not finish: {error}")
            if manual:
                self.show_notification(error, "warning")
            else:
                self.log(error)
            return
        self.latest_release = release
        if release and is_newer_version(release.version, APP_VERSION):
            self.refresh_update_ui()
            self.show_notification(f"MesterSync {release.version} is available in Settings → About & updates.", "info", duration_ms=8000)
        else:
            self.refresh_update_ui(f"MesterSync {APP_VERSION} is up to date.")
            if manual:
                self.show_notification(f"MesterSync {APP_VERSION} is up to date.", "success")

    def download_available_update(self) -> None:
        release = self.latest_release
        if not release or not is_newer_version(release.version, APP_VERSION) or self.update_download_running:
            return
        self.update_download_running = True
        self.refresh_update_ui(f"Downloading MesterSync {release.version}... 0%")

        def progress(downloaded: int, total: int) -> None:
            percent = int(downloaded * 100 / max(1, total))
            self.gui_queue.put(("update_download_progress", percent))

        def worker() -> None:
            try:
                installer = download_installer(release, progress=progress)
                self.gui_queue.put(("update_download_complete", (installer, None)))
            except Exception as exc:
                self.gui_queue.put(("update_download_complete", (None, str(exc))))

        threading.Thread(target=worker, daemon=True, name="UpdateDownload").start()

    def finish_update_download(self, installer: Optional[Path], error: Optional[str]) -> None:
        self.update_download_running = False
        if error or installer is None:
            self.refresh_update_ui(f"Update download failed: {error or 'Unknown error'}")
            self.show_notification(error or "The update download failed.", "error")
            return
        self.downloaded_update = installer
        self.refresh_update_ui()
        self.show_notification("The verified update is ready. Use Install downloaded update when current work is stopped.", "success", duration_ms=9000)

    def install_downloaded_update(self) -> None:
        installer = self.downloaded_update
        if not installer or not installer.exists():
            self.downloaded_update = None
            self.refresh_update_ui("The downloaded installer is no longer available. Download it again.")
            return
        with self.preset_test_process_lock:
            preset_test_running = self.preset_test_running
        active_work = self.started and (self.import_enabled or self.conversion_enabled or self.transfer_enabled)
        worker_active = any((self.current_import_id, self.current_conversion_id, self.current_transfer_id)) or self.importing_count > 0
        if active_work or worker_active or preset_test_running or self.current_process is not None:
            self.show_notification("Stop all imports, conversions, transfers, and preset tests before installing the update.", "warning", duration_ms=9000)
            return
        if self.config_save_after_id is not None:
            self.root.after_cancel(self.config_save_after_id)
            self.config_save_after_id = None
        if not self.flush_config_autosave():
            self.show_notification("The update was not started because Settings could not be saved.", "error")
            return
        try:
            self.save_pending_tasks(force=True)
            self.save_history(force=True)
            if not self.latest_release:
                raise RuntimeError("The downloaded update no longer has matching release information.")
            verify_installer(installer, self.latest_release)
            start_installer(installer, install_dir())
        except Exception as exc:
            self.show_notification(f"The update could not be started: {exc}", "error")
            return
        self.set_sleep_prevention(False)
        self.shutdown_event.set()
        self.root.destroy()

    # ---------------- UI events/status ----------------
    def emit_log(self, text: str) -> None:
        self.gui_queue.put(("log", text))

    def emit_task(self, task_id: str, immediate: bool = True) -> None:
        enqueue = False
        with self.task_event_lock:
            if task_id in self.pending_task_events:
                self.pending_task_events[task_id] = self.pending_task_events[task_id] or bool(immediate)
            else:
                self.pending_task_events[task_id] = bool(immediate)
                enqueue = True
        if enqueue:
            self.gui_queue.put(("task", task_id))

    def emit_status(self) -> None:
        self.gui_queue.put(("status", None))

    def emit_progress(self, stage: str, label: str, value: int, eta: Optional[float] = None) -> None:
        self.gui_queue.put(("progress", (stage, label, value, eta)))

    def emit_error_message(self, text: str) -> None:
        self.gui_queue.put(("message_error", text))

    def set_import_progress_snapshot(self, task_id: str, label: str, value: int, eta: Optional[float]) -> None:
        with self.import_progress_lock:
            self.import_progress_by_task[task_id] = (label, max(0, min(100, int(value))), eta)
            if self.import_cycle_task_id not in self.import_progress_by_task:
                self.import_cycle_task_id = task_id
                self.import_cycle_last_switch = 0.0

    def clear_import_progress_snapshot(self, task_id: str) -> None:
        with self.import_progress_lock:
            self.import_progress_by_task.pop(task_id, None)
            if self.import_cycle_task_id == task_id:
                self.import_cycle_task_id = None
                self.import_cycle_last_switch = 0.0

    def refresh_import_progress_cycle(self) -> None:
        idle = False
        with self.import_progress_lock:
            items = list(self.import_progress_by_task.items())
            if not items:
                self.import_cycle_task_id = None
                idle = True
            if idle:
                label = "Import/watching: watching Importfolder" if self.import_enabled else "Import/watching: stopped"
                value = 0
                eta = None
            else:
                ids = [task_id for task_id, _ in items]
                now = time.time()
                if self.import_cycle_task_id not in ids or now - self.import_cycle_last_switch >= 5:
                    if self.import_cycle_task_id in ids:
                        index = (ids.index(self.import_cycle_task_id) + 1) % len(ids)
                    else:
                        index = 0
                    self.import_cycle_task_id = ids[index]
                    self.import_cycle_last_switch = now
                task_id = self.import_cycle_task_id
                label, value, eta = self.import_progress_by_task.get(task_id, items[0][1])
                if len(items) > 1:
                    label = f"{label} ({ids.index(task_id) + 1}/{len(items)})" if task_id in ids else f"{label} (1/{len(items)})"
        self.update_stage_card("import", label, value, eta)

    def process_gui_events(self) -> None:
        # Never drain an unlimited queue in one Tk callback. During fast imports a
        # worker can enqueue many progress updates; processing them all at once
        # starves the Tk event loop and makes Windows show "Not responding".
        processed = 0
        max_events = 80
        pending_task_renders: List[str] = []
        pending_task_render_seen: set[str] = set()
        pending_task_render_immediate: set[str] = set()
        pending_logs: List[str] = []
        pending_status_update = False
        pending_session_update = False
        pending_history_refresh = False
        pending_thumbnail_status: Optional[bool] = None
        pending_progress: Dict[str, Tuple[str, str, float, str]] = {}

        def queue_task_render(task_id: str, immediate: bool = False) -> None:
            if task_id not in pending_task_render_seen:
                pending_task_render_seen.add(task_id)
                pending_task_renders.append(task_id)
            if immediate:
                pending_task_render_immediate.add(task_id)

        while processed < max_events:
            try:
                event, payload = self.gui_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            try:
                if event == "log":
                    pending_logs.append(str(payload))
                elif event == "task":
                    if isinstance(payload, tuple) and len(payload) == 2:
                        queue_task_render(str(payload[0]), bool(payload[1]))
                    else:
                        task_id = str(payload)
                        with self.task_event_lock:
                            immediate = self.pending_task_events.pop(task_id, True)
                        queue_task_render(task_id, immediate)
                elif event == "status":
                    pending_status_update = True
                elif event == "progress":
                    stage, label, value, eta = payload
                    pending_progress[str(stage)] = (stage, label, value, eta)
                elif event == "history":
                    pending_history_refresh = True
                elif event == "thumbnail":
                    queue_task_render(str(payload), True)
                    pending_thumbnail_status = False
                elif event == "thumbnail_prefetched":
                    target = str(payload)
                    if target.startswith("history:"):
                        self.apply_history_thumbnail(target)
                    else:
                        queue_task_render(target, True)
                elif event == "thumbnail_status":
                    pending_thumbnail_status = bool(payload)
                elif event == "session":
                    pending_session_update = True
                elif event == "message_error":
                    self.show_notification(str(payload), "error")
                elif event == "health_check_complete":
                    self.finish_health_check(dict(payload))
                elif event == "update_check_complete":
                    release, manual, error = payload
                    self.finish_update_check(release, bool(manual), error)
                elif event == "update_download_progress":
                    version = self.latest_release.version if self.latest_release else ""
                    self.refresh_update_ui(f"Downloading MesterSync {version}... {int(payload)}%")
                elif event == "update_download_complete":
                    installer, error = payload
                    self.finish_update_download(installer, error)
                elif event == "encoder_cache_ready":
                    key, encoders, error = payload
                    self.encoder_queries_pending.discard(str(key))
                    if isinstance(encoders, set):
                        self.encoder_cache[str(key)] = encoders
                        self.update_preset_status(check_encoder=True)
                    elif error:
                        self.log(f"Could not query FFmpeg encoders: {error}")
                elif event == "archive_ui":
                    self.remove_archived_task_ui(str(payload))
                elif event == "disk_ready":
                    self.show_disk_ready(str(payload))
                elif event == "preset_test_ui":
                    self.update_preset_test_ui(**dict(payload))
                elif event == "safe_names_ready":
                    for task_id in list(self.tasks.keys()):
                        queue_task_render(task_id)
                elif event == "schedule_tasks_save":
                    self.schedule_pending_tasks_flush()
                elif event == "schedule_history_save":
                    self.schedule_history_flush()
                elif event == "flush_tasks":
                    if self.tasks_save_pending:
                        self.save_pending_tasks(force=True)
                elif event == "flush_history":
                    if self.history_save_pending:
                        self.save_history(force=True)
            except Exception as exc:
                # Keep the Tk event pump alive even if one row update races with
                # a tab/layout change. The worker keeps running and the Dashboard
                # row is rebuilt by refresh_dashboard_rows().
                try:
                    self.log(f"GUI update warning: {exc}")
                except Exception:
                    pass
        if pending_logs:
            try:
                self.append_log_lines(pending_logs)
            except Exception:
                pass
        for stage, label, value, eta in pending_progress.values():
            try:
                self.update_stage_card(stage, label, value, eta)
            except Exception as exc:
                try:
                    self.log(f"Progress update warning: {exc}")
                except Exception:
                    pass
        now = time.monotonic()
        for task_id in list(self.throttled_dashboard_task_ids):
            if now - self.last_task_render_at.get(task_id, 0.0) >= TASK_RENDER_INTERVAL_SECONDS:
                queue_task_render(task_id)
                self.throttled_dashboard_task_ids.discard(task_id)
        for task_id in pending_task_renders:
            try:
                if getattr(self, "current_tab", "dashboard") == "dashboard":
                    immediate = task_id in pending_task_render_immediate
                    if immediate or now - self.last_task_render_at.get(task_id, 0.0) >= TASK_RENDER_INTERVAL_SECONDS:
                        self.render_task(task_id)
                        self.last_task_render_at[task_id] = time.monotonic()
                        self.throttled_dashboard_task_ids.discard(task_id)
                    else:
                        self.throttled_dashboard_task_ids.add(task_id)
                else:
                    self.deferred_dashboard_task_ids.add(task_id)
            except Exception as exc:
                try:
                    self.log(f"Dashboard row update warning: {exc}")
                except Exception:
                    pass
        if pending_thumbnail_status is not None:
            try:
                self.update_thumbnail_queue_status(pending_thumbnail_status)
            except Exception as exc:
                try:
                    self.log(f"Preview status update warning: {exc}")
                except Exception:
                    pass
        if pending_status_update:
            try:
                self.update_status()
            except Exception as exc:
                try:
                    self.log(f"Status update warning: {exc}")
                except Exception:
                    pass
        if pending_session_update:
            try:
                self.update_session_summary()
            except Exception as exc:
                try:
                    self.log(f"Session summary update warning: {exc}")
                except Exception:
                    pass
        if pending_history_refresh:
            try:
                self.request_history_view_refresh()
            except Exception as exc:
                try:
                    self.log(f"History refresh warning: {exc}")
                except Exception:
                    pass
        busy = processed >= max_events or bool(self.throttled_dashboard_task_ids)
        self.root.after(25 if busy else 100, self.process_gui_events)

    def schedule_pending_tasks_flush(self) -> None:
        if self.tasks_save_after_id is not None:
            return
        self.tasks_save_after_id = self.root.after(400, self.flush_pending_tasks_from_timer)

    def schedule_history_flush(self) -> None:
        if self.history_save_after_id is not None:
            return
        self.history_save_after_id = self.root.after(400, self.flush_history_from_timer)

    def flush_pending_tasks_from_timer(self) -> None:
        self.tasks_save_after_id = None
        if self.tasks_save_pending:
            self.save_pending_tasks(force=True)

    def flush_history_from_timer(self) -> None:
        self.history_save_after_id = None
        if self.history_save_pending:
            self.save_history(force=True)

    def tick(self) -> None:
        try:
            self.refresh_skip_undo_rows()
            self.check_skip_countdowns()
            self.refresh_import_progress_cycle()
            self.refresh_session_summary_idle_timer()
            self.refresh_sleep_prevention()
            self.refresh_import_priority()
            # Safety net: a settings/tab focus change must not make a live task vanish
            # from the Dashboard. If a row widget is missing but the task still exists,
            # rebuild it without touching conversion/transfer state.
            with self.task_lock:
                active_ids = [tid for tid, t in self.tasks.items() if t.stage in self.ACTIVE_STAGES or t.stage in {STAGE_WAITING, STAGE_CONVERTING, STAGE_CONVERTING_IN_PLACE, STAGE_STOPPED, STAGE_ERROR}]
            for task_id in active_ids:
                if self.dashboard_task_should_render(task_id) and not self.row_widget_alive(task_id):
                    if self.current_tab == "dashboard":
                        self.row_widgets.pop(task_id, None)
                        self.render_task(task_id)
                    else:
                        self.deferred_dashboard_task_ids.add(task_id)
        except Exception as exc:
            self.log(f"Background UI check warning: {exc}")
        finally:
            self.root.after(1000, self.tick)

    def has_active_work(self) -> bool:
        with self.preset_test_process_lock:
            if self.preset_test_running:
                return True
        with self.task_lock:
            if self.importing_count > 0:
                return True
            return any(
                task.stage in {
                    STAGE_CHECKING,
                    STAGE_IMPORTING,
                    STAGE_QUEUED,
                    STAGE_WAITING,
                    STAGE_CONVERTING,
                    STAGE_CONVERTING_IN_PLACE,
                    STAGE_WAITING_TRANSFER,
                    STAGE_TRANSFERRING,
                }
                for task in self.tasks.values()
                if not task.skipped
            )

    def set_sleep_prevention(self, enabled: bool) -> None:
        if os.name != "nt" or self.sleep_prevention_active == enabled:
            return
        try:
            continuous = 0x80000000
            system_required = 0x00000001
            awaymode_required = 0x00000040
            flags = continuous | system_required | awaymode_required if enabled else continuous
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
            self.sleep_prevention_active = enabled
            self.log("Sleep prevention enabled while work is active." if enabled else "Sleep prevention released.")
        except Exception as exc:
            self.sleep_prevention_active = False
            self.log(f"Sleep prevention warning: {exc}")

    def refresh_sleep_prevention(self) -> None:
        enabled = bool(self.config.get("prevent_sleep_while_working", True)) and self.has_active_work()
        self.set_sleep_prevention(enabled)

    def log(self, text: str) -> None:
        self.append_log_lines([text])

    def append_log_lines(self, messages: Iterable[str]) -> None:
        rendered: List[str] = []
        for message in messages:
            parts = str(message).splitlines() or [""]
            timestamp = now_text()
            rendered.extend(f"[{timestamp}] {part}\n" for part in parts)
        if not rendered:
            return
        previous_count = len(self.log_entries)
        self.log_entries.extend(rendered)
        self.log_line_count = len(self.log_entries)
        widget = self.log_text
        if widget is None:
            return
        try:
            if not widget.winfo_exists():
                self.log_text = None
                return
            trim, _count = bounded_log_count(previous_count, len(rendered), LOG_MAX_LINES)
            widget.configure(state="normal")
            widget.insert("end", "".join(rendered))
            if trim:
                widget.delete("1.0", f"{trim + 1}.0")
            widget.configure(state="disabled")
            widget.see("end")
        except tk.TclError:
            self.log_text = None

    def toggle_log(self) -> None:
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.top_log_outer.pack(side="left", fill="x", expand=True, padx=(10, 10), after=self.log_toggle)
            self.log_toggle.configure(text="Log", bg=self.BLUE)
            if self.log_text is not None:
                self.log_text.see("end")
        else:
            self.top_log_outer.pack_forget()
            self.log_toggle.configure(text="Log", bg=self.CARD3)

    def draw_dot(self, canvas: tk.Canvas, color: str) -> None:
        canvas.delete("all")
        size = min(int(canvas.cget("width")), int(canvas.cget("height")))
        canvas.create_oval(2, 2, size - 2, size - 2, fill=color, outline=color)

    def update_status(self) -> None:
        with self.preset_test_process_lock:
            preset_test_running = self.preset_test_running
        if self.pause_event.is_set():
            text = "Paused"
            color = self.YELLOW
        elif preset_test_running and not (self.started and (self.import_enabled or self.conversion_enabled or self.transfer_enabled)):
            text = "Preset test"
            color = self.BLUE
        elif self.started and (self.import_enabled or self.conversion_enabled or self.transfer_enabled):
            text = "Running" if (self.import_enabled and self.conversion_enabled and self.transfer_enabled) else "Partially running"
            color = self.GREEN
        else:
            text = "Stopped"
            color = self.RED
        with self.task_lock:
            importing_now = self.importing_count > 0
        activity = "File importing..." if importing_now else ("Testing preset..." if preset_test_running else "")
        running_any = self.started and (self.import_enabled or self.conversion_enabled or self.transfer_enabled)
        view = (
            text,
            color,
            activity,
            running_any,
            self.pause_event.is_set(),
            self.import_enabled,
            self.conversion_enabled,
            self.transfer_enabled,
        )
        if view == self.status_view_cache:
            return
        self.status_view_cache = view
        self.status_var.set(text)
        self.activity_var.set(activity)
        self.draw_dot(self.status_dot, color)
        self.start_btn.configure(text="Stop all" if running_any else "Start all", bg=self.RED if running_any else self.GREEN)
        self.pause_btn.configure(text="Resume" if self.pause_event.is_set() else "Pause")
        self.update_stage_enabled_buttons()

    def update_stage_card(self, stage: str, label: str, value: int, eta: Optional[float] = None) -> None:
        card = self.stage_cards[stage]
        progress = max(0, min(100, int(value)))
        eta_text = f"ETA: {format_duration(eta)}" if eta is not None else "ETA: -"
        view = (label, progress, eta_text)
        previous = self.stage_card_view_cache.get(stage)
        if previous != view:
            if not previous or previous[0] != label:
                card["label"].set(label)
            if not previous or previous[1] != progress:
                card["progress"].set(progress)
                card["percent"].set(f"{progress}%")
            if not previous or previous[2] != eta_text:
                card["eta"].set(eta_text)
            self.stage_card_view_cache[stage] = view
        if stage == "import":
            self.update_import_cancel_button()

    def update_stage_enabled_buttons(self) -> None:
        active_backgrounds = {
            "import": self.DARK_ORANGE,
            "conversion": self.DARK_GREEN,
            "transfer": self.DARK_PURPLE,
        }
        active_colors = {
            "import": self.ORANGE,
            "conversion": self.GREEN,
            "transfer": self.PURPLE,
        }
        for key in ["import", "conversion", "transfer"]:
            enabled = getattr(self, f"{key}_enabled")
            card = self.stage_cards[key]
            card["button"].configure(text="Stop" if enabled else "Start", bg=self.RED if enabled else self.GREEN)
            self.draw_dot(card["dot"], active_colors[key] if enabled else self.RED)
            bg = active_backgrounds[key] if enabled else self.INACTIVE_STAGE_BG
            card["card"].configure(bg=bg)
            for child in card["card"].winfo_children():
                try:
                    child.configure(bg=bg)
                    for grand in child.winfo_children():
                        try:
                            if isinstance(grand, tk.Label) or isinstance(grand, tk.Frame) or isinstance(grand, tk.Canvas):
                                grand.configure(bg=bg)
                        except Exception:
                            pass
                except Exception:
                    pass
        self.update_import_cancel_button()

    def import_cancel_available(self) -> bool:
        with self.import_progress_lock:
            has_progress = bool(self.import_progress_by_task)
        with self.import_condition:
            has_queued = bool(self.import_queue)
        with self.task_lock:
            has_import_task = any(t.stage in {STAGE_CHECKING, STAGE_IMPORTING, STAGE_STOPPING} or t.copy_thread_active for t in self.tasks.values())
        return bool(has_progress or has_queued or has_import_task)

    def update_import_cancel_button(self) -> None:
        card = self.stage_cards.get("import", {})
        btn = card.get("cancel_button")
        priority_btn = card.get("priority_button")
        try:
            available = self.import_cancel_available()
            if btn:
                if available:
                    if not btn.winfo_ismapped():
                        btn.pack(side="right", padx=(0, 8))
                    btn.configure(state="normal", bg=self.RED, activebackground=self.RED, fg="white")
                elif btn.winfo_ismapped():
                    btn.pack_forget()
            if priority_btn:
                if not priority_btn.winfo_ismapped():
                    priority_btn.pack(side="right", padx=(0, 8))
                active = self.import_priority_enabled
                priority_btn.configure(
                    text="Import priority on" if active else "Prioritize import",
                    bg=self.YELLOW if active else self.CARD3,
                    activebackground=self.YELLOW if active else self.CARD3,
                    fg="white",
                )
        except Exception as exc:
            self.emit_log(f"Import cancel button update warning: {exc}")

    def import_priority_blocks_conversion(self) -> bool:
        return bool(self.import_priority_enabled and self.import_cancel_available())

    def refresh_import_priority(self) -> None:
        if not self.import_priority_enabled and not self.current_process_paused:
            return
        if self.import_priority_blocks_conversion():
            self.suspend_current_process()
            return
        if not self.pause_event.is_set():
            self.resume_current_process()
        with self.queue_condition:
            self.queue_condition.notify_all()

    def toggle_import_priority(self) -> None:
        self.import_priority_enabled = not self.import_priority_enabled
        if self.import_priority_enabled:
            self.log("Import priority enabled. Conversion will wait while imports are active.")
        else:
            self.log("Import priority disabled. Conversion can run during imports.")
        self.refresh_import_priority()
        self.emit_status()

    # ---------------- controls ----------------
    def ensure_workers_running(self) -> None:
        """Start background workers without enabling every stage."""
        if not self.started:
            self.shutdown_event.clear()
            threading.Thread(target=self.drive_monitor_loop, daemon=True, name="DriveMonitor").start()
            threading.Thread(target=self.import_worker, daemon=True, name="ImportWorker").start()
            threading.Thread(target=self.conversion_worker, daemon=True, name="ConversionWorker").start()
            threading.Thread(target=self.transfer_worker, daemon=True, name="TransferWorker").start()
            self.started = True

    def queue_ready_conversions(self) -> None:
        """Queue imported/stopped files when Conversion is turned on later."""
        if not self.conversion_enabled:
            return
        ready: List[str] = []
        recoverable_stages = {
            STAGE_IMPORTED,
            STAGE_STOPPED,
            STAGE_QUEUED,
            STAGE_WAITING,
            STAGE_CONVERTING,
            STAGE_CONVERTING_IN_PLACE,
        }
        with self.task_lock:
            for task in self.tasks.values():
                if task.skipped or task.task_id == self.current_conversion_id or task.task_id in self.queued_conversion_ids:
                    continue
                if task.output_path and task.output_path.exists():
                    continue
                if task.stage not in recoverable_stages:
                    continue
                # Never convert directly from a recording disk. Normal jobs need
                # a verified local import; convert-in-place is the explicit exception.
                source = task.source_for_conversion() if (task.convert_in_place or task.local_input_path) else None
                if source and source.exists():
                    ready.append(task.task_id)
        for task_id in ready:
            task = self.tasks.get(task_id)
            force = bool(task and task.convert_in_place)
            self.update_task(task_id, stage=STAGE_QUEUED, detail="Waiting for in-place conversion" if force else "Waiting for conversion", progress=0)
            self.enqueue_conversion(task_id, force=force)
        if ready:
            self.log(f"Queued {len(ready)} ready file(s) for conversion.")

    def queue_recoverable_transfers(self) -> None:
        candidates: List[str] = []
        with self.task_lock:
            for task in self.tasks.values():
                if task.skipped or task.task_id == self.current_transfer_id or task.task_id in self.queued_transfer_ids:
                    continue
                if task.stage not in {STAGE_CONVERTED, STAGE_WAITING_TRANSFER, STAGE_TRANSFERRING, STAGE_STOPPED}:
                    continue
                if task.output_path and task.output_path.exists():
                    candidates.append(task.task_id)
        for task_id in candidates:
            if self.transfer_enabled:
                self.enqueue_transfer(task_id)
            else:
                self.finalize_converted_only(task_id)
        if candidates:
            self.log(f"Recovered {len(candidates)} converted file(s) after restart.")

    def validate_ffmpeg_for_conversion(self) -> Optional[Path]:
        try:
            self.set_config_from_ui()
            cfg = self.get_config()
            ffmpeg = Path(str(cfg.get("ffmpeg_path", "")).strip())
            if ffmpeg.exists() and ffmpeg.is_file():
                return ffmpeg
        except Exception as exc:
            self.log(f"FFmpeg validation warning: {exc}")
        self.show_notification("FFmpeg.exe is missing. Choose FFmpeg in Settings or the setup wizard before converting.", "error")
        return None

    def create_convert_in_place_task(self, source: Path) -> Optional[str]:
        if not source.exists() or not source.is_file():
            return None
        folder_error = validate_writable_folder(source.parent, "Source folder")
        if folder_error:
            self.show_notification(f"Cannot convert in place:\n{folder_error}", "error")
            return None
        preset_name, ffmpeg_args = self.default_task_preset_snapshot()
        ext = detect_output_extension(ffmpeg_args)
        task_id = uuid.uuid4().hex
        task = TaskState(
            task_id=task_id,
            original_path=source,
            display_name=source.name,
            local_input_path=source,
            rename_base=source.stem,
            output_ext=ext,
            stage=STAGE_QUEUED,
            detail="Waiting for in-place conversion",
            imported_at=now_iso(),
            preset_name=preset_name,
            ffmpeg_args=ffmpeg_args,
            convert_in_place=True,
        )
        with self.task_lock:
            self.tasks[task_id] = task
        self.save_pending_tasks()
        self.emit_task(task_id)
        self.generate_thumbnail_async(task_id, source)
        return task_id

    def convert_in_place_selected(self) -> None:
        if not self.validate_ffmpeg_for_conversion():
            return
        task_ids: List[str] = []
        selected = [tid for tid in self.selected_ids if tid in self.tasks]
        if selected:
            for task_id in selected:
                task = self.tasks.get(task_id)
                if not task:
                    continue
                planned_name = self.sync_task_rename_from_row(task_id)
                if task_id == self.current_conversion_id or task.stage in {STAGE_CONVERTING, STAGE_CONVERTING_IN_PLACE, STAGE_TRANSFERRING}:
                    self.log(f"Already busy, not queued for in-place conversion: {task.display_name}")
                    continue
                source = self.find_existing_task_media(task)
                if not source:
                    self.update_task(task_id, stage=STAGE_ERROR, detail="In-place conversion failed: source file is missing", progress=0)
                    continue
                folder_error = validate_writable_folder(source.parent, "Source folder")
                if folder_error:
                    self.update_task(task_id, stage=STAGE_ERROR, detail=f"In-place conversion failed: {folder_error}", progress=0)
                    continue
                preset_name, ffmpeg_args = self.default_task_preset_snapshot()
                self.update_task(
                    task_id,
                    local_input_path=source,
                    rename_base=planned_name or source.stem,
                    output_ext=detect_output_extension(ffmpeg_args),
                    preset_name=preset_name,
                    ffmpeg_args=ffmpeg_args,
                    convert_in_place=True,
                    skipped=False,
                    stage=STAGE_QUEUED,
                    detail="Waiting for in-place conversion",
                    progress=0,
                )
                task_ids.append(task_id)
        else:
            files = filedialog.askopenfilenames(title="Convert in place", filetypes=[("Video files", "*.*")])
            for file_name in files:
                task_id = self.create_convert_in_place_task(Path(file_name))
                if task_id:
                    task_ids.append(task_id)
        if not task_ids:
            return
        self.ensure_workers_running()
        for task_id in task_ids:
            self.enqueue_conversion(task_id, force=True)
        with self.queue_condition:
            self.queue_condition.notify_all()
        self.emit_status()
        self.log(f"Queued {len(task_ids)} file(s) for in-place conversion.")

    def toggle_start_all(self) -> None:
        if self.started and (self.import_enabled or self.conversion_enabled or self.transfer_enabled):
            self.stop_all()
        else:
            self.start_all()

    def start_all(self) -> None:
        try:
            self.set_config_from_ui()
            cfg = self.get_config()
            folder_errors = [
                validate_writable_folder(str(cfg.get("input_folder", "")), "Importfolder"),
                validate_writable_folder(str(cfg.get("output_folder", "")), "Output folder"),
                validate_writable_folder(str(cfg.get("nas_folder", "")), "NAS folder", required=False),
            ]
            folder_errors.extend(media_folder_overlap_errors(
                cfg.get("input_folder", ""),
                cfg.get("output_folder", ""),
                cfg.get("nas_folder", ""),
            ))
            folder_errors = [error for error in folder_errors if error]
            if folder_errors:
                self.show_notification("MesterSync cannot safely start with these folders:\n" + "\n".join(f"- {error}" for error in folder_errors), "error")
                return
            ffmpeg_value = str(cfg.get("ffmpeg_path", "")).strip()
            ffmpeg = Path(ffmpeg_value) if ffmpeg_value else None
            conversion_allowed = bool(ffmpeg and ffmpeg.is_file())
            if not conversion_allowed:
                shown_path = str(ffmpeg) if ffmpeg else "Not configured"
                if not messagebox.askyesno(
                    APP_NAME,
                    f"FFmpeg was not found at:\n{shown_path}\n\nStart importing without conversion?",
                ):
                    return
            transfer_allowed = bool(str(cfg.get("nas_folder", "")).strip())
        except Exception as exc:
            self.show_notification(f"Could not start safely: {exc}\nCheck that the configured folders are writable and not protected system folders.", "error")
            return
        self.ensure_workers_running()
        self.import_cancel_requested.clear()
        self.import_enabled = True
        self.conversion_enabled = conversion_allowed
        self.transfer_enabled = transfer_allowed
        self.pause_event.clear()
        self.queue_ready_conversions()
        self.queue_recoverable_transfers()
        with self.import_condition:
            self.import_condition.notify_all()
        with self.queue_condition:
            self.queue_condition.notify_all()
        with self.transfer_condition:
            self.transfer_condition.notify_all()
        if not conversion_allowed:
            self.log("Started import only. Conversion is off because FFmpeg is not configured.")
        else:
            self.log("Started all services." if transfer_allowed else "Started import/conversion. Transfer is off because no NAS folder is configured.")
        self.emit_status()

    def stop_all(self) -> None:
        self.import_enabled = False
        self.conversion_enabled = False
        self.transfer_enabled = False
        self.import_priority_enabled = False
        self.pause_event.clear()
        self.cancel_import_queue("Import cancelled by Stop all")
        current_transfer = self.tasks.get(self.current_transfer_id or "")
        if current_transfer:
            current_transfer.transfer_stop_event.set()
        with self.current_process_lock:
            proc = self.current_process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        self.log("Stopped all services. Current FFmpeg process was asked to stop.")
        self.emit_status()

    def toggle_pause(self) -> None:
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.resume_current_process()
            self.log("Resumed.")
        else:
            self.pause_event.set()
            self.suspend_current_process()
            self.log("Paused.")
        with self.import_condition:
            self.import_condition.notify_all()
        with self.queue_condition:
            self.queue_condition.notify_all()
        with self.transfer_condition:
            self.transfer_condition.notify_all()
        self.emit_status()

    def toggle_stage(self, key: str) -> None:
        current = getattr(self, f"{key}_enabled")
        if key == "transfer" and not current and not str(self.get_config().get("nas_folder", "")).strip():
            self.show_notification("Choose a NAS folder in Settings before enabling Transfer.", "warning")
            return
        setattr(self, f"{key}_enabled", not current)
        if not current:
            self.ensure_workers_running()
        if key == "import" and not current:
            self.import_cancel_requested.clear()
        if key == "import" and current:
            self.cancel_import_queue("Import cancelled")
        if key == "conversion" and not current:
            self.queue_ready_conversions()
        if key == "conversion" and current:
            with self.current_process_lock:
                proc = self.current_process
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        if key == "transfer" and current:
            current_transfer = self.tasks.get(self.current_transfer_id or "")
            if current_transfer:
                current_transfer.transfer_stop_event.set()
            # Transfer was just disabled. Anything already waiting for NAS transfer
            # should not block or sit in the Dashboard; keep it in Output and archive
            # it as converted-only. The conversion queue remains independent.
            self.finalize_waiting_transfers_as_converted_only()
        if key == "transfer" and not current:
            self.requeue_waiting_transfers()
        with self.import_condition:
            self.import_condition.notify_all()
        with self.queue_condition:
            self.queue_condition.notify_all()
        with self.transfer_condition:
            self.transfer_condition.notify_all()
        self.log(f"{key.capitalize()} {'enabled' if not current else 'disabled'}.")
        self.emit_status()

    def cancel_import_queue(self, reason: str = "Import cancelled") -> None:
        """Cancel active import/copy work immediately and prevent queued import work from starting."""
        self.import_cancel_requested.set()
        affected: List[str] = []
        with self.import_condition:
            queued = list(self.import_queue)
            self.import_queue.clear()
            self.queued_import_ids.clear()
            for task_id in queued:
                batch_id = self.import_job_batch.pop(task_id, "")
                self.import_job_requires_enabled.pop(task_id, None)
                self.import_enqueue_after_success.discard(task_id)
                batch = self.import_batches.get(batch_id)
                if batch:
                    batch["pending"].discard(task_id)
                    batch["cancelled"] = True
            active_batch_id = self.import_job_batch.get(self.current_import_id or "", "")
            active_batch = self.import_batches.get(active_batch_id)
            if active_batch:
                active_batch["cancelled"] = True
            self.import_condition.notify_all()
        with self.task_lock:
            for task in self.tasks.values():
                if task.skipped:
                    continue
                if task.copy_thread_active or task.task_id in queued or task.stage in {STAGE_CHECKING, STAGE_IMPORTING}:
                    task.import_stop_event.set()
                    affected.append(task.task_id)
        for task_id in affected:
            self.clear_import_progress_snapshot(task_id)
            task = self.tasks.get(task_id)
            if not task:
                continue
            if task.copy_thread_active:
                self.update_task(task_id, stage=STAGE_STOPPING, detail=reason, progress=task.progress)
            else:
                self.update_task(task_id, stage=STAGE_STOPPED, detail=reason, progress=0)
        if affected:
            self.log(f"Cancelled {len(affected)} import item(s).")
        else:
            self.log("Import stopped. No active import items were running.")
        self.emit_status()

    def cancel_current_import(self) -> None:
        self.cancel_import_queue("Import cancelled by user")
        self.import_cancel_requested.set()
        with self.import_progress_lock:
            ids = list(self.import_progress_by_task.keys())
        for task_id in ids:
            self.clear_import_progress_snapshot(task_id)
        self.update_stage_card("import", "Import/watching: cancelled", 0, None)
        self.update_import_cancel_button()

    def requeue_waiting_transfers(self) -> None:
        with self.task_lock:
            candidates = [
                t for t in self.tasks.values()
                if t.stage in {STAGE_WAITING_TRANSFER, STAGE_STOPPED}
                and t.output_path
                and t.output_path.exists()
                and t.task_id not in self.queued_transfer_ids
            ]
        for task in candidates:
            self.enqueue_transfer(task.task_id)

    def finalize_waiting_transfers_as_converted_only(self) -> None:
        with self.transfer_condition:
            queued = list(self.transfer_queue)
            self.transfer_queue.clear()
            self.queued_transfer_ids.clear()
            self.transfer_condition.notify_all()

        for task_id in queued:
            task = self.tasks.get(task_id)
            if not task or task.skipped:
                continue
            if task.output_path and task.output_path.exists():
                self.emit_log(f"Transfer disabled; keeping converted file in output folder: {task.output_path.name}")
                self.finalize_converted_only(task_id)
            else:
                self.update_task(task_id, stage=STAGE_ERROR, detail="Transfer disabled, but converted output was missing")

    def on_close(self) -> None:
        with self.preset_test_process_lock:
            preset_test_running = self.preset_test_running
        if (self.started and (self.import_enabled or self.conversion_enabled or self.transfer_enabled)) or preset_test_running:
            if not messagebox.askyesno(APP_NAME, "Stop all work and close?"):
                return
        if self.config_save_after_id is not None:
            self.root.after_cancel(self.config_save_after_id)
            self.config_save_after_id = None
        if not self.flush_config_autosave():
            self.show_notification("Settings could not be saved, so MesterSync will stay open. Check the data folder and try again.", "error")
            return
        try:
            self.save_pending_tasks(force=True)
            self.save_history(force=True)
        except Exception as exc:
            self.show_notification(f"Work state could not be saved, so MesterSync will stay open.\n{exc}", "error")
            return
        self.set_sleep_prevention(False)
        self.shutdown_event.set()
        self.preset_test_cancel.set()
        with self.preset_test_process_lock:
            preset_process = self.preset_test_process
        if preset_process and preset_process.poll() is None:
            try:
                preset_process.terminate()
            except Exception:
                pass
        with self.current_process_lock:
            proc = self.current_process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        self.root.destroy()

    def wait_if_paused(self) -> None:
        while self.pause_event.is_set() and not self.shutdown_event.is_set():
            self.shutdown_event.wait(0.2)

    def suspend_current_process(self) -> None:
        if os.name != "nt":
            return
        with self.current_process_lock:
            proc = self.current_process
        if not proc or proc.poll() is not None or self.current_process_paused:
            return
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, proc.pid)
            if handle:
                ctypes.windll.ntdll.NtSuspendProcess(handle)
                ctypes.windll.kernel32.CloseHandle(handle)
                self.current_process_paused = True
        except Exception as exc:
            self.emit_log(f"Could not pause the current conversion process: {exc}")

    def resume_current_process(self) -> None:
        if os.name != "nt":
            return
        with self.current_process_lock:
            proc = self.current_process
        if not proc or proc.poll() is not None or not self.current_process_paused:
            return
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, proc.pid)
            if handle:
                ctypes.windll.ntdll.NtResumeProcess(handle)
                ctypes.windll.kernel32.CloseHandle(handle)
                self.current_process_paused = False
        except Exception as exc:
            self.emit_log(f"Could not resume the current conversion process: {exc}")

    # ---------------- tasks/rows ----------------
    def default_task_preset_snapshot(self) -> Tuple[str, List[str]]:
        """Return the default preset and FFmpeg args without touching Tk widgets.

        Import/copy workers run in background threads. Tkinter variables and text
        widgets must only be read on the UI thread, so background task creation
        uses the saved config snapshot instead. The top-right preset selector
        auto-saves to this config when changed.
        """
        cfg = self.get_config()
        name = str(cfg.get("default_preset_name") or "Current settings")
        args: Optional[List[str]] = None
        if name != "Current settings":
            try:
                path = self.get_preset_folder_from_config(cfg) / f"{name}.txt"
                if path.exists():
                    args = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            except Exception:
                args = None
        if not args:
            args = list(cfg.get("ffmpeg_args", DEFAULT_FFMPEG_ARGS))
        return name, args

    def get_preset_folder_from_config(self, cfg: Optional[Dict[str, Any]] = None) -> Path:
        cfg = cfg or self.get_config()
        value = str(cfg.get("preset_folder", "")).strip()
        folder = Path(value) if value else preset_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        except Exception:
            return preset_dir()

    def create_task(self, original_path: Path, stage: str = STAGE_DETECTED, imported: bool = False, allow_duplicate: bool = False, generate_thumbnail: bool = True) -> str:
        key = path_key(original_path)
        with self.task_lock:
            if not allow_duplicate and key in self.task_by_path:
                return self.task_by_path[key]
            preset_name, ffmpeg_args = self.default_task_preset_snapshot()
            ext = detect_output_extension(ffmpeg_args)
            task_id = uuid.uuid4().hex
            task = TaskState(
                task_id=task_id,
                original_path=original_path,
                display_name=original_path.name,
                local_input_path=original_path if imported else None,
                rename_base=original_path.stem,
                output_ext=ext,
                preset_name=preset_name,
                ffmpeg_args=ffmpeg_args,
                stage=stage,
                imported_at=now_iso() if imported else None,
            )
            self.tasks[task_id] = task
            # Manual/drop imports deliberately allow the same original file to be
            # added more than once. In that case we do not map the original path
            # as unique; every add becomes its own independent task. The imported
            # copy path is still mapped after the copy finishes.
            if not allow_duplicate:
                self.task_by_path[key] = task_id
                if imported:
                    self.task_by_path[path_key(original_path)] = task_id
        self.save_pending_tasks()
        self.emit_task(task_id)
        if generate_thumbnail:
            self.generate_thumbnail_async(task_id, original_path)
        return task_id

    def reserve_unique_target(self, desired: Path, reserved_attr: str) -> Path:
        """Reserve a unique media-file destination inside this running app.

        The app can have several active jobs whose final file does not exist yet
        (for example an import copy or an FFmpeg output). Checking only the file
        system is not enough in that case, so we also keep a short-lived in-memory
        reservation set for import, output, and NAS destinations.
        """
        reserved: set[str] = getattr(self, reserved_attr)
        return reserve_unique_path(desired, reserved, self.task_lock)

    def release_unique_target(self, target: Optional[Path], reserved_attr: str) -> None:
        if not target:
            return
        reserved: set[str] = getattr(self, reserved_attr)
        release_reserved_path(target, reserved, self.task_lock)

    def reserve_import_target(self, desired: Path) -> Path:
        """Reserve a unique import/watchfolder destination before copying starts."""
        return self.reserve_unique_target(desired, "reserved_import_paths")

    def release_import_target(self, target: Optional[Path]) -> None:
        self.release_unique_target(target, "reserved_import_paths")

    def reserve_output_target(self, desired: Path) -> Path:
        """Reserve a unique output-folder destination before FFmpeg starts."""
        return self.reserve_unique_target(desired, "reserved_output_paths")

    def release_output_target(self, target: Optional[Path]) -> None:
        self.release_unique_target(target, "reserved_output_paths")

    def reserve_nas_target(self, desired: Path) -> Path:
        """Reserve a unique NAS destination before transfer starts."""
        return self.reserve_unique_target(desired, "reserved_nas_paths")

    def release_nas_target(self, target: Optional[Path]) -> None:
        self.release_unique_target(target, "reserved_nas_paths")

    def disk_warning_reserve_bytes(self, cfg: Optional[Dict[str, Any]] = None) -> int:
        cfg = cfg or self.get_config()
        try:
            gb = max(0, int(cfg.get("low_disk_warning_gb", 20)))
        except Exception:
            gb = 20
        return gb * 1024 * 1024 * 1024

    def low_disk_warning(self, destination: Path, source_size: int, action: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
        free = free_space_bytes(destination)
        if free is None or source_size <= 0:
            return None
        reserve = self.disk_warning_reserve_bytes(cfg)
        wanted = source_size + reserve
        if free < source_size:
            return f"{action} cannot start: not enough free space. Need at least {format_size(source_size)}, free {format_size(free)}."
        if reserve and free < wanted:
            return f"{action} low disk warning: {format_size(free)} free, recommended at least {format_size(wanted)} for this file plus reserve."
        return None

    def final_name_folders(self) -> List[Path]:
        cfg = self.get_config()
        folders: List[Path] = []
        for key in ("output_folder", "nas_folder"):
            value = str(cfg.get(key, "")).strip()
            if value:
                folders.append(Path(value))
        return folders

    def collect_final_name_snapshot(self) -> List[Tuple[str, str]]:
        names: List[Tuple[str, str]] = []
        for folder in self.final_name_folders():
            try:
                if folder.exists():
                    names.extend((path_key(child), child.name) for child in folder.iterdir() if child.is_file())
            except OSError as exc:
                self.emit_log(f"Could not scan final-name folder {folder}: {exc}")
        return names

    def request_final_name_snapshot(self) -> None:
        with self.final_name_snapshot_lock:
            if self.final_name_snapshot_refreshing or time.time() - self.final_name_snapshot_at < 5:
                return
            self.final_name_snapshot_refreshing = True

        def refresh() -> None:
            try:
                names = self.collect_final_name_snapshot()
                with self.final_name_snapshot_lock:
                    self.final_name_snapshot = names
                    self.final_name_snapshot_at = time.time()
                self.gui_queue.put(("safe_names_ready", None))
            finally:
                with self.final_name_snapshot_lock:
                    self.final_name_snapshot_refreshing = False

        threading.Thread(target=refresh, daemon=True, name="FinalNameSnapshot").start()

    def safe_final_name_now(self, task: TaskState, fresh: bool = False) -> str:
        if fresh:
            names = self.collect_final_name_snapshot()
            with self.final_name_snapshot_lock:
                self.final_name_snapshot = names
                self.final_name_snapshot_at = time.time()
        else:
            self.request_final_name_snapshot()
            with self.final_name_snapshot_lock:
                names = list(self.final_name_snapshot)
        with self.task_lock:
            reserved = list(self.reserved_output_paths) + list(self.reserved_nas_paths)
        cfg = self.get_config()
        return next_final_name(
            task.rename_base,
            task.display_name,
            task.output_ext,
            bool(cfg.get("auto_underscore_renames", False)),
            names,
            reserved,
            task.output_path,
        )

    def reserve_safe_final_target(self, task: TaskState, folder: Path, reserved_attr: str) -> Path:
        ensure_folder(folder)
        names = self.collect_final_name_snapshot()
        with self.final_name_snapshot_lock:
            self.final_name_snapshot = names
            self.final_name_snapshot_at = time.time()
        cfg = self.get_config()
        target_reserved: set[str] = getattr(self, reserved_attr)
        return reserve_final_target(
            folder,
            task.rename_base,
            task.display_name,
            task.output_ext,
            bool(cfg.get("auto_underscore_renames", False)),
            names,
            self.reserved_output_paths,
            self.reserved_nas_paths,
            target_reserved,
            self.task_lock,
            task.output_path,
        )

    def promote_temp_no_overwrite(self, temp: Path, destination: Path) -> bool:
        return promote_path_no_overwrite(temp, destination, self.emit_log)

    def move_file_no_overwrite(self, source: Path, destination: Path) -> bool:
        return move_path_no_overwrite(source, destination, self.emit_log)

    def update_task(self, task_id: str, **kwargs: Any) -> None:
        should_save = False
        with self.task_lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            old_stage = task.stage
            changed_keys = set()
            for key, value in kwargs.items():
                if hasattr(task, key) and getattr(task, key) != value:
                    setattr(task, key, value)
                    changed_keys.add(key)
            if not changed_keys:
                return
            if task.stage == STAGE_ERROR and task_id not in self.session_error_ids:
                self.session_error_ids.add(task_id)
                self.record_session_stat("errors")
            should_save = old_stage != task.stage or any(key not in {"progress", "eta_seconds", "detail"} for key in changed_keys)
        if should_save:
            self.save_pending_tasks()
        self.emit_task(task_id, immediate=should_save)

    def task_progress_style(self, task: TaskState) -> str:
        if task.stage == STAGE_ERROR:
            return "RowError.Horizontal.TProgressbar"
        if task.stage == STAGE_IMPORTING:
            return "RowImport.Horizontal.TProgressbar"
        if task.stage in {STAGE_CONVERTING, STAGE_CONVERTING_IN_PLACE, STAGE_CONVERTED, STAGE_COMPLETE, STAGE_CONVERTED_OUTPUT}:
            return "RowConvert.Horizontal.TProgressbar"
        if task.stage in {STAGE_WAITING_TRANSFER, STAGE_TRANSFERRING}:
            return "RowTransfer.Horizontal.TProgressbar"
        return "Thin.Horizontal.TProgressbar"

    def update_preset_badges(self, row: Dict[str, Any], task: TaskState, bg: str) -> None:
        holder = row.get("preset_badges")
        if not holder:
            return
        try:
            key = (task.preset_name, tuple(task.ffmpeg_args or self.get_config().get("ffmpeg_args", DEFAULT_FFMPEG_ARGS)))
            if row.get("preset_badges_key") == key and row.get("preset_badges_bg") == bg:
                return
            holder.configure(bg=bg)
            if row.get("preset_badges_key") == key:
                row["preset_badges_bg"] = bg
                return
            for child in holder.winfo_children():
                child.destroy()
            for text, color in preset_badges(task.preset_name, task.ffmpeg_args or self.get_config().get("ffmpeg_args", DEFAULT_FFMPEG_ARGS)):
                tk.Label(holder, text=text, bg=color, fg="white", font=("Segoe UI", 8, "bold"), padx=7, pady=2).pack(side="left", padx=(0, 5), pady=(0, 2))
            row["preset_badges_key"] = key
            row["preset_badges_bg"] = bg
        except Exception:
            pass

    def task_preset_locked(self, task: TaskState) -> bool:
        """Once conversion has started, the preset is locked because FFmpeg args are already chosen."""
        return bool(getattr(task, "conversion_started", False)) or task.stage in {
            STAGE_WAITING, STAGE_CONVERTING, STAGE_CONVERTING_IN_PLACE, STAGE_CONVERTED, STAGE_CONVERTED_OUTPUT,
            STAGE_WAITING_TRANSFER, STAGE_TRANSFERRING, STAGE_COMPLETE, STAGE_STOPPED, STAGE_ERROR,
        }

    def render_task(self, task_id: str) -> None:
        with self.task_lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            try:
                priority = self.dashboard_virtual_range[0] + self.dashboard_virtual_order.index(task_id) + 1
            except ValueError:
                priority = next((index for index, current_id in enumerate(self.tasks, 1) if current_id == task_id), 1)
            view = TaskViewSnapshot.from_task(task, priority, task_id in self.selected_ids)
        if not self.dashboard_task_should_render(task_id):
            row = self.row_widgets.pop(task_id, None)
            if row:
                try:
                    row["outer"].destroy()
                except Exception:
                    pass
            self.schedule_dashboard_virtual_refresh()
            return
        if task_id in self.row_widgets and not self.row_widget_alive(task_id):
            self.row_widgets.pop(task_id, None)
        if task_id not in self.row_widgets:
            self.create_task_row(task)
            return
        row = self.row_widgets[task_id]
        cache = row["render_cache"]
        selected = view.selected
        is_error = view.stage == STAGE_ERROR
        active_green = view.stage == STAGE_CONVERTING
        skipped_bg = "#221b1e" if view.skipped else None
        error_bg = "#3a1018" if is_error else None
        card_bg = error_bg or (self.DARK_GREEN if active_green else (skipped_bg or self.CARD))
        border = "#ffffff" if selected else self.BORDER
        if cache.get("border") != border:
            row["outer"].configure(bg=border)
            cache["border"] = border
        if cache.get("card_bg") != card_bg:
            row["card"].configure(bg=card_bg)
            for widget_key in ["right", "top", "safe_name", "preset_badges", "times", "detail", "src", "ext", "buttons", "preset_holder"]:
                try:
                    row[widget_key].configure(bg=card_bg)
                except Exception:
                    pass
            cache["card_bg"] = card_bg
        source_text = f"{view.display_name}  ->  "
        if cache.get("source_text") != source_text:
            row["src"].configure(text=source_text)
            cache["source_text"] = source_text
        if not row.get("dirty") and cache.get("rename_base") != view.rename_base:
            row["rename_var"].set(view.rename_base)
            cache["rename_base"] = view.rename_base
        if cache.get("output_ext") != view.output_ext:
            row["ext"].configure(text=view.output_ext)
            cache["output_ext"] = view.output_ext
        stage_text = f"#{view.priority}  {view.stage}"
        if cache.get("stage_text") != stage_text:
            row["stage"].configure(text=stage_text)
            cache["stage_text"] = stage_text
        self.update_preset_badges(row, view, card_bg)
        if cache.get("progress") != view.progress:
            row["progress_var"].set(view.progress)
            row["progress_text"].configure(text=f"{view.progress}%", bg=card_bg)
            cache["progress"] = view.progress
        progress_style = self.task_progress_style(view)
        if cache.get("progress_style") != progress_style:
            row["progressbar"].configure(style=progress_style)
            cache["progress_style"] = progress_style
        preset_locked = self.task_preset_locked(view)
        if "preset_holder" in row and cache.get("preset_locked") != preset_locked:
            if preset_locked:
                if row["preset_holder"].winfo_ismapped():
                    row["preset_holder"].pack_forget()
            else:
                if not row["preset_holder"].winfo_ismapped():
                    row["preset_holder"].pack(side="right")
            cache["preset_locked"] = preset_locked
        if "preset_var" in row and not preset_locked and not row.get("preset_dirty"):
            preset_values = tuple(self.preset_menu_values())
            if row.get("preset_values_key") != preset_values:
                row["preset_combo"].configure(values=preset_values)
                row["preset_values_key"] = preset_values
            if cache.get("preset_name") != view.preset_name:
                row["preset_var"].set(view.preset_name)
                cache["preset_name"] = view.preset_name
        if not self.compact_dashboard:
            try:
                safe_key = (view.rename_base, view.output_ext)
                if row.get("safe_name_key") != safe_key or time.time() - float(row.get("safe_name_checked", 0)) > 5:
                    row["safe_name"].configure(text=f"Safe final name now: {self.safe_final_name_now(view)}", bg=card_bg)
                    row["safe_name_key"] = safe_key
                    row["safe_name_checked"] = time.time()
                else:
                    row["safe_name"].configure(bg=card_bg)
            except Exception:
                pass
        times_key = (view.imported_at, view.converted_at, view.transferred_at, int(view.eta_seconds or 0))
        if cache.get("times_key") != times_key:
            row["times"].configure(text=f"Imported: {pretty_time(view.imported_at)}    Converted: {pretty_time(view.converted_at)}    Transferred: {pretty_time(view.transferred_at)}    ETA: {format_duration(view.eta_seconds)}")
            cache["times_key"] = times_key
        if is_error:
            detail_view = (f"ERROR: {view.detail}", "#ffffff", ("Segoe UI", 12, "bold"))
        elif view.skipped:
            detail_view = (skip_undo_detail(view.skip_archive_due), "#ffd0d8", ("Segoe UI", 10, "bold"))
        else:
            detail_view = (view.detail, self.MUTED, ("Segoe UI", 9))
        if cache.get("detail_view") != detail_view:
            row["detail"].configure(text=detail_view[0], fg=detail_view[1], font=detail_view[2])
            cache["detail_view"] = detail_view
        self.update_task_buttons(task_id)
        thumbnail_key = (view.thumbnail_path, view.thumbnail_preview_paths, view.local_input_path, view.output_path)
        if cache.get("thumbnail_key") != thumbnail_key or not row.get("displayed_thumb_path"):
            self.apply_thumbnail(task_id)
            cache["thumbnail_key"] = thumbnail_key

    def create_task_row(self, task: TaskState) -> None:
        outer = tk.Frame(self.dashboard_inner, bg=self.BORDER)
        outer.pack(fill="x", pady=7, before=self.dashboard_bottom_spacer)
        card = tk.Frame(outer, bg=self.CARD, padx=12, pady=12)
        card.pack(fill="x", padx=2, pady=2)
        thumb_frame = tk.Frame(card, bg=self.CARD3, width=222, height=125, highlightthickness=2, highlightbackground=self.BORDER)
        thumb_frame.pack(side="left")
        thumb_frame.pack_propagate(False)
        thumb = tk.Label(thumb_frame, text="Preview", bg=self.CARD3, fg=self.MUTED, width=1, height=1)
        thumb.pack(fill="both", expand=True)
        right = tk.Frame(card, bg=self.CARD)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))
        top = tk.Frame(right, bg=self.CARD)
        top.pack(fill="x")
        src = tk.Label(top, text=f"{task.display_name}  ->  ", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 11, "bold"), anchor="w")
        src.pack(side="left")
        rename_var = tk.StringVar(value=task.rename_base)
        rename_entry = tk.Entry(top, textvariable=rename_var, bg=self.CARD3, fg=self.TEXT, insertbackground=self.TEXT, relief="flat", font=("Segoe UI", 11), width=34)
        rename_entry.pack(side="left", fill="x", expand=True, padx=(2, 3), ipady=4)
        ext = tk.Label(top, text=task.output_ext, bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 11))
        ext.pack(side="left", padx=(2, 8))
        stage = tk.Label(top, text=task.stage, bg=self.CARD3, fg=self.TEXT, font=("Segoe UI", 9, "bold"), padx=10, pady=4)
        stage.pack(side="right")
        safe_name = tk.Label(right, text="", bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9, "bold"), anchor="w")
        safe_name.pack(fill="x", pady=(8, 0))
        preset_badges_frame = tk.Frame(right, bg=self.CARD)
        preset_badges_frame.pack(fill="x", pady=(5, 0))
        times = tk.Label(right, text="", bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9), anchor="w")
        times.pack(fill="x", pady=(5, 6))
        pwrap = tk.Frame(right, bg=self.CARD)
        pwrap.pack(fill="x")
        pvar = tk.IntVar(value=task.progress)
        pbar = ttk.Progressbar(pwrap, maximum=100, variable=pvar, style="Thin.Horizontal.TProgressbar")
        pbar.pack(side="left", fill="x", expand=True)
        ptxt = tk.Label(pwrap, text="0%", bg=self.CARD, fg=self.TEXT, font=("Segoe UI", 9, "bold"))
        ptxt.pack(side="left", padx=(10, 0))
        detail = tk.Label(right, text=task.detail, bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9), anchor="w", wraplength=900, justify="left")
        detail.pack(fill="x", pady=(8, 0))
        buttons = tk.Frame(right, bg=self.CARD)
        buttons.pack(fill="x", pady=(8, 0))
        skip_btn = self.small_button(buttons, "Skip", lambda tid=task.task_id: self.skip_task(tid), self.CARD3)
        skip_btn.pack(side="left", padx=(0, 6))
        resume_btn = self.small_button(buttons, "Resume", lambda tid=task.task_id: self.resume_task(tid), self.GREEN)
        remove_btn = self.small_button(buttons, "Remove", lambda tid=task.task_id: self.remove_skipped_task(tid), self.RED)
        top_btn = self.small_button(buttons, "Move to top", lambda tid=task.task_id: self.move_to_top(tid), self.CARD3)
        top_btn.pack(side="left", padx=(0, 6))
        up_btn = self.small_button(buttons, "Up", lambda tid=task.task_id: self.move_task_priority(tid, -1), self.CARD3)
        down_btn = self.small_button(buttons, "Down", lambda tid=task.task_id: self.move_task_priority(tid, 1), self.CARD3)
        import_anyway_btn = self.small_button(buttons, "Import anyway", lambda tid=task.task_id: self.import_duplicate_task(tid), self.ORANGE)
        retry_btn = self.small_button(buttons, "Retry", lambda tid=task.task_id: self.retry_task(tid), self.ORANGE)
        restart_btn = self.small_button(buttons, "Restart/Resume", lambda tid=task.task_id: self.restart_or_resume_task(tid), self.GREEN)
        copy_error_btn = self.small_button(buttons, "Copy error", lambda tid=task.task_id: self.copy_task_error(tid), self.CARD3)
        preset_holder = tk.Frame(buttons, bg=self.CARD)
        preset_holder.pack(side="right")
        tk.Label(preset_holder, text="Preset", bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        preset_var = tk.StringVar(value=task.preset_name)
        preset_combo = ttk.Combobox(preset_holder, textvariable=preset_var, values=self.preset_menu_values(), state="readonly", width=22)
        preset_combo.pack(side="left")
        preset_combo.bind("<<ComboboxSelected>>", lambda e, tid=task.task_id: self.set_task_preset(tid))
        self.row_widgets[task.task_id] = {
            "outer": outer, "card": card, "thumb_frame": thumb_frame, "thumb": thumb,
            "right": right, "top": top, "src": src, "rename_var": rename_var, "rename_entry": rename_entry, "ext": ext,
            "stage": stage, "safe_name": safe_name, "preset_badges": preset_badges_frame, "times": times, "progress_wrap": pwrap, "progress_var": pvar, "progressbar": pbar, "progress_text": ptxt, "detail": detail,
            "buttons": buttons, "skip_btn": skip_btn, "resume_btn": resume_btn, "remove_btn": remove_btn, "top_btn": top_btn, "up_btn": up_btn, "down_btn": down_btn, "import_anyway_btn": import_anyway_btn, "retry_btn": retry_btn, "restart_btn": restart_btn, "copy_error_btn": copy_error_btn,
            "preset_holder": preset_holder, "preset_var": preset_var, "preset_combo": preset_combo, "preset_dirty": False,
            "dirty": False, "preview_index": None, "displayed_thumb_path": None, "safe_name_key": None, "safe_name_checked": 0.0,
            "button_layout": None, "preset_values_key": None,
            "render_cache": {},
        }
        row_drop_widgets = [outer, card, thumb_frame, thumb, right, top, src, ext, safe_name, preset_badges_frame, times, detail, pwrap, ptxt, buttons, stage]
        for w in row_drop_widgets:
            w.bind("<Button-1>", lambda e, tid=task.task_id: (self.root.focus_set(), self.select_task(tid, ctrl=bool(e.state & 0x0004), shift=bool(e.state & 0x0001))))
            w.bind("<Double-Button-1>", lambda e, tid=task.task_id: self.open_task_source(tid))
            self.bind_shortcuts_to_widget(w)
        for w in [thumb_frame, thumb]:
            w.bind("<Button-1>", lambda e, tid=task.task_id: self.show_thumbnail_popup(tid))
            w.bind("<Enter>", lambda e, tid=task.task_id: self.update_thumbnail_preview_from_mouse(tid, e))
            w.bind("<Motion>", lambda e, tid=task.task_id: self.update_thumbnail_preview_from_mouse(tid, e))
            w.bind("<Leave>", lambda e, tid=task.task_id: self.stop_thumbnail_preview(tid))
        # Rows sit above the canvas, so they must also be drop targets.
        for w in row_drop_widgets + [rename_entry, preset_holder, preset_combo]:
            self.enable_drop_target(w)
        rename_entry.bind("<FocusIn>", lambda e, tid=task.task_id: self.mark_dirty(tid))
        rename_entry.bind("<Return>", lambda e, tid=task.task_id: self.commit_rename_and_exit(tid))
        rename_entry.bind("<Escape>", lambda e, tid=task.task_id: self.cancel_rename(tid))
        rename_entry.bind("<FocusOut>", lambda e, tid=task.task_id: self.commit_rename(tid))
        self.apply_row_density(task.task_id)
        self.render_task(task.task_id)

    def update_task_buttons(self, task_id: str) -> None:
        row = self.row_widgets.get(task_id)
        task = self.tasks.get(task_id)
        if not row or not task:
            return
        waiting = task_id not in {self.current_import_id, self.current_conversion_id, self.current_transfer_id} and (
            task_id in self.queued_import_ids or task_id in self.queued_conversion_ids or task_id in self.queued_transfer_ids
        )
        layout = (task.skipped, task.stage, waiting)
        if task.skipped:
            row["resume_btn"].configure(text=skip_undo_button_text(task.skip_archive_due))
        if row.get("button_layout") == layout:
            return
        row["button_layout"] = layout
        # Rearranging packed buttons on every FFmpeg progress event causes
        # needless geometry recalculation. Only do it when visibility changes.
        for key in ["resume_btn", "remove_btn", "top_btn", "up_btn", "down_btn", "import_anyway_btn", "retry_btn", "restart_btn", "copy_error_btn"]:
            row[key].pack_forget()
        if task.skipped:
            row["skip_btn"].pack_forget()
            row["resume_btn"].pack(side="left", padx=(0, 6))
            row["remove_btn"].pack(side="left", padx=(0, 6))
        else:
            if not row["skip_btn"].winfo_ismapped():
                row["skip_btn"].pack(side="left", padx=(0, 6))
            if task.stage == STAGE_ERROR:
                row["retry_btn"].pack(side="left", padx=(0, 6))
                row["copy_error_btn"].pack(side="left", padx=(0, 6))
            if task.stage == STAGE_DUPLICATE_REVIEW:
                row["import_anyway_btn"].pack(side="left", padx=(0, 6))
                row["remove_btn"].pack(side="left", padx=(0, 6))
            if task.stage == STAGE_STOPPED:
                row["restart_btn"].pack(side="left", padx=(0, 6))
            if waiting:
                row["up_btn"].pack(side="left", padx=(0, 6))
                row["down_btn"].pack(side="left", padx=(0, 6))
                row["top_btn"].pack(side="left", padx=(0, 6))

    def copy_task_error(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        text = task.error_log or task.detail or "No error detail was recorded."
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log(f"Copied error details for {task.display_name}.")

    def import_duplicate_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        task.duplicate = False
        task.duplicate_reason = ""
        task.import_stop_event = threading.Event()
        self.requeue_duplicate_import(task_id)
        self.ensure_workers_running()

    def set_task_preset(self, task_id: str) -> None:
        row = self.row_widgets.get(task_id)
        task = self.tasks.get(task_id)
        if not row or not task:
            return
        if self.task_preset_locked(task):
            row["preset_var"].set(task.preset_name)
            self.log(f"Preset is locked because conversion has already started: {task.display_name}")
            return
        name = row["preset_var"].get() or "Current settings"
        args = self.read_preset_args(name) or self.current_ffmpeg_args_from_ui()
        ext = detect_output_extension(args)
        self.update_task(task_id, preset_name=name, ffmpeg_args=args, output_ext=ext)
        self.log(f"Preset for {self.tasks[task_id].display_name}: {name}")
        warnings = self.preset_safety_warnings(args, check_encoder=True)
        if warnings:
            self.log(f"Preset warnings for {self.tasks[task_id].display_name}: {'; '.join(warnings)}")

    def mark_dirty(self, task_id: str) -> None:
        if task_id in self.row_widgets:
            self.row_widgets[task_id]["dirty"] = True

    def skip_selected_tasks(self) -> None:
        selected = [tid for tid in self.selected_ids if tid in self.tasks]
        if not selected:
            return
        for task_id in list(selected):
            self.skip_task(task_id)
        self.update_batch_box_visibility()
        self.log(f"Skipped {len(selected)} selected file(s).")

    def selected_editing_text(self) -> bool:
        widget = self.root.focus_get()
        return isinstance(widget, (tk.Entry, tk.Text, scrolledtext.ScrolledText, ttk.Combobox))

    def delete_selected_tasks(self, event: Optional[tk.Event] = None) -> str:
        if self.selected_editing_text():
            return ""
        selected = [tid for tid in self.selected_ids if tid in self.tasks]
        if not selected:
            return "break"
        for task_id in list(selected):
            task = self.tasks.get(task_id)
            if not task:
                continue
            if task.skipped or task.stage == STAGE_DUPLICATE_REVIEW:
                self.remove_skipped_task(task_id)
            else:
                self.skip_task(task_id)
        return "break"

    def select_all_tasks(self, event: Optional[tk.Event] = None) -> str:
        if self.selected_editing_text():
            return ""
        self.selected_ids = list(self.tasks.keys())
        if self.selected_ids:
            self.selection_anchor_id = self.selected_ids[0]
        self.update_selection_visuals()
        self.update_batch_box_visibility()
        return "break"

    def toggle_pause_shortcut(self, event: Optional[tk.Event] = None) -> str:
        if self.selected_editing_text():
            return ""
        self.toggle_pause()
        return "break"

    def open_selected_task(self, event: Optional[tk.Event] = None) -> str:
        if self.selected_editing_text():
            return ""
        selected = [tid for tid in self.selected_ids if tid in self.tasks]
        if selected:
            return self.open_task_source(selected[-1])
        return "break"

    def retry_selected_tasks(self, event: Optional[tk.Event] = None) -> str:
        if self.selected_editing_text():
            return ""
        selected = [tid for tid in self.selected_ids if tid in self.tasks]
        for task_id in selected:
            task = self.tasks.get(task_id)
            if not task:
                continue
            if task.stage == STAGE_ERROR:
                self.retry_task(task_id)
            elif task.stage == STAGE_STOPPED:
                self.restart_or_resume_task(task_id)
        return "break"

    def move_selected_to_top(self, event: Optional[tk.Event] = None) -> str:
        if self.selected_editing_text():
            return ""
        selected = [tid for tid in self.selected_ids if tid in self.tasks]
        if selected:
            self.move_to_top(selected[-1])
        return "break"

    def move_selected_priority_up(self, event: Optional[tk.Event] = None) -> str:
        if not self.selected_editing_text() and self.selected_ids:
            self.move_task_priority(self.selected_ids[-1], -1)
            return "break"
        return ""

    def move_selected_priority_down(self, event: Optional[tk.Event] = None) -> str:
        if not self.selected_editing_text() and self.selected_ids:
            self.move_task_priority(self.selected_ids[-1], 1)
            return "break"
        return ""

    def ordered_task_ids(self) -> List[str]:
        return [tid for tid in self.tasks.keys() if tid in self.tasks]

    def update_selection_visuals(self) -> None:
        for tid, row in self.row_widgets.items():
            try:
                row["outer"].configure(bg="#ffffff" if tid in self.selected_ids else self.BORDER)
            except Exception:
                pass

    def select_index(self, index: int, extend: bool = False) -> str:
        if self.selected_editing_text():
            return ""
        order = self.ordered_task_ids()
        if not order:
            return "break"
        index = max(0, min(len(order) - 1, index))
        if extend:
            anchor = self.selection_anchor_id if self.selection_anchor_id in order else (self.selected_ids[0] if self.selected_ids else order[index])
            self.selection_anchor_id = anchor
            a, b = order.index(anchor), index
            lo, hi = sorted((a, b))
            self.selected_ids = order[lo:hi + 1]
        else:
            self.selected_ids = [order[index]]
            self.selection_anchor_id = order[index]
        self.update_selection_visuals()
        self.update_batch_box_visibility()
        self.scroll_task_into_view(order[index])
        return "break"

    def scroll_task_into_view(self, task_id: str) -> None:
        row = self.row_widgets.get(task_id)
        canvas = getattr(self, "dashboard_canvas", None)
        inner = getattr(self, "dashboard_inner", None)
        if not canvas or not inner:
            return
        if not row:
            order = self.ordered_task_ids()
            if task_id not in order:
                return
            total_height = max(1, len(order) * self.dashboard_row_extent())
            canvas.yview_moveto(max(0.0, min(1.0, order.index(task_id) * self.dashboard_row_extent() / total_height)))
            self.refresh_dashboard_virtual_rows(force=True)
            row = self.row_widgets.get(task_id)
            if not row:
                return
        try:
            self.root.update_idletasks()
            widget = row["outer"]
            row_top = widget.winfo_y()
            row_bottom = row_top + widget.winfo_height()
            view_top = canvas.canvasy(0)
            view_bottom = view_top + canvas.winfo_height()
            total_height = max(1, inner.winfo_height())
            margin = 18
            if row_top < view_top + margin:
                canvas.yview_moveto(max(0.0, (row_top - margin) / total_height))
            elif row_bottom > view_bottom - margin:
                canvas.yview_moveto(min(1.0, (row_bottom - canvas.winfo_height() + margin) / total_height))
        except Exception:
            pass

    def current_selection_index(self, fallback_end: bool = False) -> int:
        order = self.ordered_task_ids()
        if not order:
            return 0
        for tid in reversed(self.selected_ids):
            if tid in order:
                return order.index(tid)
        return len(order) - 1 if fallback_end else 0

    def move_selection_up(self, event: Optional[tk.Event] = None) -> str:
        return self.select_index(self.current_selection_index() - 1)

    def move_selection_down(self, event: Optional[tk.Event] = None) -> str:
        return self.select_index(self.current_selection_index() + 1)

    def extend_selection_up(self, event: Optional[tk.Event] = None) -> str:
        return self.select_index(self.current_selection_index() - 1, extend=True)

    def extend_selection_down(self, event: Optional[tk.Event] = None) -> str:
        return self.select_index(self.current_selection_index() + 1, extend=True)

    def on_batch_affix_toggle(self, mode: str) -> None:
        if mode == "prefix" and self.batch_prefix_var.get():
            self.batch_suffix_var.set(False)
        elif mode == "suffix" and self.batch_suffix_var.get():
            self.batch_prefix_var.set(False)
        self.update_batch_affix_buttons()
        self.update_batch_box_visibility()

    def update_batch_affix_buttons(self) -> None:
        for attr, var in [("batch_prefix_btn", self.batch_prefix_var), ("batch_suffix_btn", self.batch_suffix_var)]:
            btn = getattr(self, attr, None)
            if not btn:
                continue
            active = bool(var.get())
            btn.configure(
                bg=self.BLUE if active else self.CARD3,
                activebackground=self.BLUE if active else self.CARD3,
                fg="white" if active else self.TEXT,
                activeforeground="white" if active else self.TEXT,
            )

    def apply_batch_box_rename(self) -> None:
        selected = [tid for tid in self.selected_ids if tid in self.tasks]
        if len(selected) < 2:
            return
        auto = bool(self.auto_underscore_var.get()) if hasattr(self, "auto_underscore_var") else bool(self.get_config().get("auto_underscore_renames", False))
        with self.task_lock:
            ordered = sorted([self.tasks[tid] for tid in selected], key=lambda t: t.created_at)
        prefix = bool(self.batch_prefix_var.get())
        suffix = bool(self.batch_suffix_var.get())
        current_bases = [task.rename_base or Path(task.display_name).stem for task in ordered]
        new_bases = batch_rename_bases(current_bases, self.batch_name_var.get(), auto, prefix, suffix)
        if not new_bases:
            return
        for task, new_base in zip(ordered, new_bases):
            self.update_task(task.task_id, rename_base=new_base)
        self.log(f"Batch rename applied to {len(ordered)} selected files.")

    def update_batch_box_visibility(self) -> None:
        if not hasattr(self, "batch_frame"):
            return
        selected_count = len([tid for tid in self.selected_ids if tid in self.tasks])
        if selected_count > 1:
            if hasattr(self, "batch_hint_var"):
                if getattr(self, "batch_prefix_var", None) and self.batch_prefix_var.get():
                    hint = f"{selected_count} selected. Prefix is added before each current name."
                elif getattr(self, "batch_suffix_var", None) and self.batch_suffix_var.get():
                    hint = f"{selected_count} selected. Suffix is added after each current name."
                else:
                    hint = f"{selected_count} selected. Output: name_01, name_02..."
                self.batch_hint_var.set(hint)
            if not self.batch_frame.winfo_ismapped():
                self.batch_frame.pack(anchor="w", pady=(10, 0))
        else:
            if self.batch_frame.winfo_ismapped():
                self.batch_frame.pack_forget()

    def commit_rename(self, task_id: str) -> None:
        row = self.row_widgets.get(task_id)
        if not row:
            return
        raw_base = row["rename_var"].get()
        task = self.tasks.get(task_id)
        if not task:
            return
        selected = [tid for tid in self.selected_ids if tid in self.tasks]
        # Switching tabs or clicking a settings checkbox can fire FocusOut from
        # a rename box. If the text did not really change, do nothing; do not
        # re-render active conversion rows just because focus moved.
        if not row.get("dirty") and not (len(selected) > 1 and task_id in selected):
            return
        if len(selected) <= 1 and raw_base == task.rename_base:
            row["dirty"] = False
            return
        auto = bool(self.auto_underscore_var.get()) if hasattr(self, "auto_underscore_var") else bool(self.get_config().get("auto_underscore_renames", False))
        if len(selected) > 1 and task_id in selected:
            base = sanitize_base_name(raw_base or "renamed_video", auto)
            with self.task_lock:
                ordered = sorted([self.tasks[tid] for tid in selected], key=lambda t: t.created_at)
            for i, task in enumerate(ordered, start=1):
                self.update_task(task.task_id, rename_base=f"{base}_{i:02d}")
            self.log(f"Batch rename applied to {len(ordered)} selected files.")
        else:
            base = sanitize_base_name(raw_base or Path(self.tasks[task_id].display_name).stem, auto)
            self.update_task(task_id, rename_base=base)
            self.log(f"Updated planned name: {base}{self.tasks[task_id].output_ext}")
        row["dirty"] = False
        self.root.focus_set()

    def sync_task_rename_from_row(self, task_id: str) -> str:
        """Persist text still being edited before a toolbar action uses it."""
        task = self.tasks.get(task_id)
        if not task:
            return ""
        row = self.row_widgets.get(task_id)
        if not row or not row.get("dirty"):
            return task.rename_base
        auto = bool(self.auto_underscore_var.get()) if hasattr(self, "auto_underscore_var") else bool(self.get_config().get("auto_underscore_renames", False))
        base = sanitize_base_name(row["rename_var"].get() or Path(task.display_name).stem, auto)
        self.update_task(task_id, rename_base=base)
        row["dirty"] = False
        return base

    def commit_rename_and_exit(self, task_id: str) -> str:
        self.commit_rename(task_id)
        self.root.focus_set()
        return "break"

    def cancel_rename(self, task_id: str) -> str:
        row = self.row_widgets.get(task_id)
        task = self.tasks.get(task_id)
        if row and task:
            row["rename_var"].set(task.rename_base)
            row["dirty"] = False
        self.root.focus_set()
        return "break"

    def select_task(self, task_id: str, ctrl: bool = False, shift: bool = False) -> None:
        order = list(self.tasks.keys())
        if shift and self.selected_ids and task_id in order and self.selected_ids[-1] in order:
            a, b = order.index(self.selected_ids[-1]), order.index(task_id)
            lo, hi = sorted((a, b))
            self.selected_ids = order[lo:hi + 1]
        elif ctrl:
            if task_id in self.selected_ids:
                self.selected_ids.remove(task_id)
            else:
                self.selected_ids.append(task_id)
                self.selection_anchor_id = self.selection_anchor_id or task_id
        else:
            self.selected_ids = [task_id]
            self.selection_anchor_id = task_id
        if shift and task_id in self.selected_ids:
            self.selection_anchor_id = self.selection_anchor_id or task_id
        self.update_selection_visuals()
        self.update_batch_box_visibility()

    def open_task_source(self, task_id: str) -> str:
        task = self.tasks.get(task_id)
        if not task:
            return "break"
        candidate = self.find_existing_task_media(task)
        if candidate:
            try:
                open_path(candidate)
                self.log(f"Opened video: {candidate.name}")
            except Exception as exc:
                self.show_notification(f"Could not open file: {candidate}\n{exc}", "error")
            return "break"
        self.show_notification(f"No existing file was found for: {task.display_name}", "warning")
        return "break"

    def open_path_with_feedback(self, path_value: Any, description: str = "item") -> None:
        path = Path(str(path_value or ""))
        if not str(path_value or "").strip() or not path.exists():
            self.show_notification(f"The {description} is no longer available at: {path}", "warning")
            return
        try:
            open_path(path)
        except Exception as exc:
            self.log(f"Could not open {description} {path}: {exc}")
            self.show_notification(f"Could not open {description}: {path}\n{exc}", "error")

    def candidate_media_bases(self) -> List[Path]:
        bases = [app_dir()]
        cfg = self.get_config()
        for key in ("input_folder", "output_folder", "nas_folder"):
            raw = str(cfg.get(key, "") or "").strip()
            if raw:
                bases.append(Path(raw))
        return bases

    def resolve_existing_media_path(self, candidate: Optional[Path]) -> Optional[Path]:
        if not candidate:
            return None
        path = Path(candidate)
        try:
            if path.exists() and path.is_file():
                return path
        except Exception:
            pass
        if not path.is_absolute():
            for base in self.candidate_media_bases():
                try:
                    combined = base / path
                    if combined.exists() and combined.is_file():
                        return combined
                except Exception:
                    continue
        return None

    def find_existing_task_media(self, task: TaskState) -> Optional[Path]:
        candidates = [task.original_path, task.local_input_path, task.output_path, task.nas_path]
        for candidate in candidates:
            resolved = self.resolve_existing_media_path(candidate)
            if resolved:
                return resolved
        filenames = []
        for candidate in candidates:
            if candidate:
                name = Path(candidate).name
                if name and name not in filenames:
                    filenames.append(name)
        for base in self.candidate_media_bases():
            for name in filenames:
                try:
                    path = base / name
                    if path.exists() and path.is_file():
                        return path
                except Exception:
                    continue
        return None

    def apply_thumbnail(self, task_id: str) -> None:
        row = self.row_widgets.get(task_id)
        task = self.tasks.get(task_id)
        if not row or not task:
            return
        if (
            task.thumbnail_path
            and row.get("displayed_thumb_path") == task.thumbnail_path
            and (len(task.thumbnail_preview_paths or []) >= 2 or task_id in self.thumbnail_generation_requested)
        ):
            return
        if task.thumbnail_path and Path(task.thumbnail_path).exists():
            if not self.thumbnail_images.has_source(task.thumbnail_path):
                self.request_thumbnail_display(task_id, task.thumbnail_path)
                row["thumb"].configure(image="", text="Loading preview...")
                row["thumb"].image = None
                return
            self.set_thumb_image(task_id, task.thumbnail_path)
            existing_previews = [p for p in (task.thumbnail_preview_paths or []) if Path(p).exists()]
            if len(existing_previews) >= 2 or task_id in self.thumbnail_generation_requested:
                return
            source = self.find_existing_task_media(task)
            if source:
                self.generate_thumbnail_async(task_id, source)
            return
        if task_id in self.thumbnail_generation_requested:
            return
        source = self.find_existing_task_media(task)
        if source:
            row["thumb"].configure(image="", text="Preview queued")
            row["thumb"].image = None
            self.generate_thumbnail_async(task_id, source)

    def load_thumbnail_image(self, path: str) -> Optional[tk.PhotoImage]:
        width, height = (142, 80) if self.compact_dashboard else (222, 125)
        return self.thumbnail_images.get_thumbnail(path, width, height)

    def set_thumb_image(self, task_id: str, path: str) -> None:
        row = self.row_widgets.get(task_id)
        if not row:
            return
        if row.get("displayed_thumb_path") == path:
            return
        img = self.load_thumbnail_image(path)
        if not img:
            return
        row["thumb"].configure(image=img, text="")
        row["thumb"].image = img
        row["displayed_thumb_path"] = path

    def close_thumbnail_popup(self) -> None:
        self.thumbnail_popup.close()

    def show_thumbnail_popup(self, task_id: str) -> str:
        task = self.tasks.get(task_id)
        if not task or not task.thumbnail_path or not Path(task.thumbnail_path).exists():
            return "break"
        paths = [p for p in (task.thumbnail_preview_paths or [task.thumbnail_path]) if Path(p).exists()]
        return self.thumbnail_popup.show(paths or [task.thumbnail_path])

    def update_thumbnail_preview_from_mouse(self, task_id: str, event: tk.Event) -> None:
        row = self.row_widgets.get(task_id)
        task = self.tasks.get(task_id)
        if not row or not task or len(task.thumbnail_preview_paths) < 2:
            return
        paths = [p for p in task.thumbnail_preview_paths if Path(p).exists()]
        if not paths:
            return
        index = scrub_index_from_event(event, len(paths))
        if row.get("preview_index") == index:
            return
        self.set_thumb_image(task_id, paths[index])
        row["preview_index"] = index

    def stop_thumbnail_preview(self, task_id: str) -> None:
        row = self.row_widgets.get(task_id)
        task = self.tasks.get(task_id)
        if not row:
            return
        row["preview_index"] = None
        if task and task.thumbnail_path:
            self.set_thumb_image(task_id, task.thumbnail_path)

    # ---------------- thumbnails ----------------
    def request_thumbnail_display(self, task_id: str, path: str) -> None:
        if self.thumbnail_images.has_source(path) or path in self.thumbnail_display_requested:
            return
        self.thumbnail_display_requested.add(path)
        self.thumbnail_display_queue.put((task_id, path))
        self.ensure_thumbnail_display_worker_running()

    def request_history_thumbnail_display(self, token: str, path: str) -> None:
        if self.thumbnail_images.has_source(path):
            self.apply_history_thumbnail(token)
            return
        # History cards are short-lived. Queue their token independently so the
        # correct live label is notified even when the same source is already
        # being prepared for a Dashboard row.
        self.thumbnail_display_queue.put((token, path))
        self.ensure_thumbnail_display_worker_running()

    def apply_history_thumbnail(self, token: str) -> None:
        target = self.history_thumbnail_labels.pop(token, None)
        if not target:
            return
        label, path = target
        try:
            if label.winfo_exists():
                self.thumbnail_images.configure_label(label, path)
        except tk.TclError:
            pass

    def ensure_thumbnail_display_worker_running(self) -> None:
        if self.thumbnail_display_worker_started:
            return
        self.thumbnail_display_worker_started = True
        threading.Thread(target=self.thumbnail_display_worker, daemon=True, name="ThumbnailDisplayPrep").start()

    def thumbnail_display_worker(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                task_id, path = self.thumbnail_display_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if not self.thumbnail_images.has_source(path):
                    data = Path(path).read_bytes()
                    self.thumbnail_images.put_source_bytes(path, data)
                self.gui_queue.put(("thumbnail_prefetched", task_id))
            except OSError as exc:
                self.emit_log(f"Preview display preparation warning for {Path(path).name}: {exc}")
            finally:
                self.thumbnail_display_requested.discard(path)
                self.thumbnail_display_queue.task_done()

    def generate_thumbnail_async(self, task_id: str, src: Path) -> None:
        if task_id in self.thumbnail_generation_requested:
            return
        self.thumbnail_generation_requested.add(task_id)
        self.thumbnail_queue.put((task_id, src, time.time()))
        self.gui_queue.put(("thumbnail_status", False))
        self.ensure_thumbnail_worker_running()

    def ensure_thumbnail_worker_running(self) -> None:
        if self.thumbnail_worker_started:
            return
        self.thumbnail_worker_started = True
        threading.Thread(target=self.thumbnail_worker, daemon=True, name="ThumbnailWorker").start()

    def thumbnail_worker(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                task_id, src, queued_at = self.thumbnail_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                while not self.shutdown_event.is_set() and self.pause_event.is_set():
                    self.shutdown_event.wait(0.2)
                remaining = THUMBNAIL_SETTLE_DELAY_SECONDS - (time.time() - queued_at)
                if remaining > 0:
                    self.shutdown_event.wait(remaining)
                if self.shutdown_event.is_set():
                    continue
                self.gui_queue.put(("thumbnail_status", True))
                self.generate_thumbnail(task_id, src)
            except Exception as exc:
                self.emit_log(f"Preview worker error for {src.name}: {exc}")
            finally:
                self.thumbnail_generation_requested.discard(task_id)
                self.gui_queue.put(("thumbnail_status", False))
                self.thumbnail_queue.task_done()

    def resolve_ffmpeg_executable(self, configured: str) -> Optional[Path]:
        configured = str(configured or "").strip().strip('"')
        candidates: List[Path] = []
        if configured:
            candidates.append(Path(configured))
            found = shutil.which(configured)
            if found:
                candidates.append(Path(found))
        found = shutil.which("ffmpeg")
        if found:
            candidates.append(Path(found))
        candidates.append(Path(r"C:\ffmpeg\bin\ffmpeg.exe"))
        for candidate in candidates:
            try:
                if candidate.exists() and candidate.is_file():
                    return candidate
            except Exception:
                continue
        return None

    def generate_thumbnail(self, task_id: str, src: Path) -> None:
        cfg = self.get_config()
        ffmpeg = self.resolve_ffmpeg_executable(str(cfg.get("ffmpeg_path", "")))
        src = self.resolve_existing_media_path(src) or src
        if not ffmpeg or not src.exists():
            self.thumbnail_generation_requested.discard(task_id)
            return
        duration = self.get_duration(src, ffmpeg)
        thumb, preview_paths = extract_video_thumbnails(ffmpeg, src, task_id, duration, no_window_flags)
        if thumb:
            self.update_task(task_id, thumbnail_path=str(thumb), thumbnail_preview_paths=preview_paths)
            self.gui_queue.put(("thumbnail", task_id))
        self.thumbnail_generation_requested.discard(task_id)

    # ---------------- add/import/scan ----------------
    def handle_drop(self, event: Any) -> None:
        paths = [Path(x) for x in self.root.tk.splitlist(event.data)]
        threading.Thread(target=self.add_paths_to_import, args=(paths,), daemon=True).start()

    def add_files_dialog(self) -> None:
        files = filedialog.askopenfilenames(title="Add video files", filetypes=[("Video files", "*.*")])
        if files:
            threading.Thread(target=self.add_paths_to_import, args=([Path(x) for x in files],), daemon=True).start()

    def queue_import_batch(
        self,
        items: List[Tuple[Path, str]],
        require_import_enabled: bool,
        source_label: str,
        disk_batch: bool = False,
    ) -> str:
        if not items:
            return ""
        self.ensure_workers_running()
        self.import_cancel_requested.clear()
        batch_id = uuid.uuid4().hex
        with self.import_condition:
            accepted = [
                (src, task_id)
                for src, task_id in items
                if task_id not in self.queued_import_ids
                and task_id != self.current_import_id
                and task_id not in self.import_job_batch
            ]
            if not accepted:
                return ""
            self.import_batches[batch_id] = {
                "pending": {task_id for _, task_id in accepted},
                "imported": [],
                "blocked": set(),
                "conversion_released": False,
                "cancelled": False,
                "disk_batch": disk_batch,
                "source_label": source_label,
                "ready_notified": False,
                "total": len(accepted),
            }
            for _src, task_id in accepted:
                self.import_queue.append(task_id)
                self.queued_import_ids.add(task_id)
                self.import_job_requires_enabled[task_id] = require_import_enabled
                self.import_job_batch[task_id] = batch_id
            self.import_condition.notify_all()
        for position, (_src, task_id) in enumerate(accepted, start=1):
            self.update_task(task_id, stage=STAGE_DETECTED, detail=f"Waiting for import ({position}/{len(accepted)}). Use Up/Down to change priority.", progress=0)
        self.emit_status()
        return batch_id

    def import_worker(self) -> None:
        while not self.shutdown_event.is_set():
            task_id: Optional[str] = None
            with self.import_condition:
                while not self.shutdown_event.is_set():
                    if self.pause_event.is_set() or not self.import_queue:
                        self.import_condition.wait(timeout=0.5)
                        continue
                    if self.import_enabled:
                        task_id = self.import_queue[0]
                    else:
                        task_id = next((tid for tid in self.import_queue if not self.import_job_requires_enabled.get(tid, False)), None)
                    if task_id is None:
                        self.import_condition.wait(timeout=0.5)
                        continue
                    self.import_queue.remove(task_id)
                    self.queued_import_ids.discard(task_id)
                    break
            if not task_id or self.shutdown_event.is_set():
                continue
            task = self.tasks.get(task_id)
            if not task or task.skipped:
                self.complete_import_job(task_id, success=False, blocked=False)
                continue
            self.current_import_id = task_id
            try:
                cfg = self.get_config()
                source = task.original_path
                self.update_task(task_id, stage=STAGE_CHECKING, detail="Checking for an exact duplicate before import...", progress=0)
                self.set_import_progress_snapshot(task_id, f"Import/watching: checking duplicate for {source.name}", 0, None)
                duplicate_reason = self.duplicate_reason_for(source, cfg, task_id)
                self.clear_import_progress_snapshot(task_id)
                if task_id not in self.tasks or task.skipped:
                    self.complete_import_job(task_id, success=False, blocked=False)
                    continue
                if duplicate_reason:
                    with self.import_condition:
                        batch = self.import_batches.get(self.import_job_batch.get(task_id, ""))
                        automatic_disk_duplicate = bool(batch and batch.get("disk_batch"))
                    if automatic_disk_duplicate:
                        self.emit_log(f"Already imported; skipped exact duplicate from disk: {source.name}")
                        self.discard_detected_task(task_id)
                        self.complete_import_job(task_id, success=False, blocked=False)
                        continue
                    self.update_task(task_id, stage=STAGE_DUPLICATE_REVIEW, duplicate=True, duplicate_reason=duplicate_reason, detail=duplicate_reason, progress=0)
                    self.emit_log(f"Duplicate needs review: {source.name}")
                    self.complete_import_job(task_id, success=False, blocked=True)
                    continue
                self.update_task(task_id, stage=STAGE_IMPORTING, detail="Starting import...", progress=0)
                self.import_single_file(source, task_id, False)
                import_success = self.task_ready_for_conversion_queue(task_id)
                current_task = self.tasks.get(task_id)
                import_blocked = bool(current_task and current_task.stage == STAGE_ERROR)
                self.complete_import_job(task_id, success=import_success, blocked=import_blocked)
            except Exception as exc:
                self.update_task(task_id, stage=STAGE_ERROR, detail=f"Import worker error: {exc}", error_log=traceback.format_exc())
                self.emit_log(f"Import worker error for {task.display_name}: {exc}")
                self.complete_import_job(task_id, success=False, blocked=True)
            finally:
                self.current_import_id = None
                self.emit_status()

    def discard_detected_task(self, task_id: str) -> None:
        """Remove an unimported auto-detected row without touching any media file."""
        with self.task_lock:
            task = self.tasks.pop(task_id, None)
            if not task:
                return
            for path in (task.original_path, task.local_input_path, task.output_path, task.nas_path):
                if path:
                    self.task_by_path.pop(path_key(path), None)
        try:
            self.save_pending_tasks(force=True)
        except Exception:
            with self.task_lock:
                self.tasks[task_id] = task
                for path in (task.original_path, task.local_input_path, task.output_path, task.nas_path):
                    if path:
                        self.task_by_path[path_key(path)] = task_id
            raise
        self.gui_queue.put(("archive_ui", task_id))

    def import_batch_actions_locked(self, batch_id: str) -> Tuple[List[str], str]:
        batch = self.import_batches.get(batch_id)
        if not batch:
            return [], ""
        conversions: List[str] = []
        if not batch["pending"] and not batch["conversion_released"]:
            batch["conversion_released"] = True
            conversions = list(batch["imported"])
        ready_message = ""
        if (
            batch["disk_batch"]
            and not batch["pending"]
            and not batch["blocked"]
            and not batch["cancelled"]
            and not batch["ready_notified"]
        ):
            batch["ready_notified"] = True
            ready_message = f"Disk {batch['source_label']} ready to remove ({batch['total']} video(s))."
        if not batch["pending"] and not batch["blocked"] and batch["conversion_released"]:
            self.import_batches.pop(batch_id, None)
        return conversions, ready_message

    def complete_import_job(self, task_id: str, success: bool, blocked: bool) -> None:
        conversions: List[str] = []
        ready_message = ""
        enqueue_now = False
        with self.import_condition:
            batch_id = self.import_job_batch.get(task_id, "")
            batch = self.import_batches.get(batch_id)
            enqueue_now = task_id in self.import_enqueue_after_success
            self.import_enqueue_after_success.discard(task_id)
            self.import_job_requires_enabled.pop(task_id, None)
            if batch:
                batch["pending"].discard(task_id)
                if success and task_id not in batch["imported"]:
                    batch["imported"].append(task_id)
                if blocked:
                    batch["blocked"].add(task_id)
                else:
                    batch["blocked"].discard(task_id)
                    self.import_job_batch.pop(task_id, None)
                conversions, ready_message = self.import_batch_actions_locked(batch_id)
            else:
                self.import_job_batch.pop(task_id, None)
        if success or blocked:
            # A verified import or a review/error state must be durable before a
            # recording disk can be declared ready to remove.
            self.save_pending_tasks(force=True)
        if conversions:
            self.queue_imported_batch_for_conversion(conversions)
        elif enqueue_now and success:
            self.queue_imported_batch_for_conversion([task_id])
        if ready_message:
            self.emit_log(ready_message)
            self.gui_queue.put(("disk_ready", ready_message))

    def requeue_duplicate_import(self, task_id: str) -> None:
        with self.import_condition:
            batch_id = self.import_job_batch.get(task_id, "")
            batch = self.import_batches.get(batch_id)
            if not batch:
                batch_id = self.queue_import_batch([(self.tasks[task_id].original_path, task_id)], False, "Added files")
                return
            batch["blocked"].discard(task_id)
            batch["pending"].add(task_id)
            if batch["conversion_released"]:
                self.import_enqueue_after_success.add(task_id)
            if task_id not in self.queued_import_ids:
                self.import_queue.append(task_id)
                self.queued_import_ids.add(task_id)
            self.import_job_requires_enabled[task_id] = False
            self.import_condition.notify_all()
        self.update_task(task_id, stage=STAGE_DETECTED, detail="Waiting for import after duplicate review", progress=0)

    def remove_import_job(self, task_id: str, resolve_batch: bool = False) -> None:
        conversions: List[str] = []
        ready_message = ""
        with self.import_condition:
            if task_id == self.current_import_id and not resolve_batch:
                self.import_condition.notify_all()
                return
            try:
                self.import_queue.remove(task_id)
            except ValueError:
                pass
            self.queued_import_ids.discard(task_id)
            self.import_job_requires_enabled.pop(task_id, None)
            self.import_enqueue_after_success.discard(task_id)
            batch_id = self.import_job_batch.pop(task_id, "")
            batch = self.import_batches.get(batch_id)
            if batch and resolve_batch:
                batch["pending"].discard(task_id)
                batch["blocked"].discard(task_id)
                conversions, ready_message = self.import_batch_actions_locked(batch_id)
            self.import_condition.notify_all()
        if conversions:
            self.queue_imported_batch_for_conversion(conversions)
        if ready_message:
            self.emit_log(ready_message)
            self.gui_queue.put(("disk_ready", ready_message))

    def add_paths_to_import(self, paths: List[Path]) -> None:
        self.import_cancel_requested.clear()
        cfg = self.get_config()
        folder_error = validate_writable_folder(str(cfg.get("input_folder", "")), "Importfolder")
        if folder_error:
            self.emit_log(f"Add files stopped: {folder_error}")
            self.emit_error_message(f"Choose a valid Importfolder in Settings first.\n\n{folder_error}")
            return
        input_folder = Path(cfg["input_folder"])
        ensure_folder(input_folder)
        candidates = collect_import_candidates(
            paths,
            cfg.get("conversion_extensions", COMMON_VIDEO_EXTENSIONS),
            lambda: self.import_cancel_requested.is_set() or self.shutdown_event.is_set(),
        )
        queued_items: List[Tuple[Path, str]] = []
        for src in candidates:
            if self.import_cancel_requested.is_set() or self.shutdown_event.is_set():
                break
            task_id = self.create_task(src, stage=STAGE_DETECTED, imported=False, allow_duplicate=True, generate_thumbnail=False)
            self.update_task(task_id, detail="Waiting for import check...", progress=0)
            queued_items.append((src, task_id))
        started = len(queued_items)
        if started:
            self.queue_import_batch(queued_items, False, "Added files")
            self.emit_log(f"Added {started} file(s). Import queue will process them one at a time.")
        if candidates and self.import_cancel_requested.is_set():
            self.emit_log("Add files was cancelled before import started.")

    def task_ready_for_conversion_queue(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        return bool(
            task
            and not task.skipped
            and task.stage == STAGE_IMPORTED
            and task.local_input_path
            and task.local_input_path.exists()
        )

    def queue_imported_batch_for_conversion(self, task_ids: List[str]) -> None:
        priority = {task_id: index for index, task_id in enumerate(self.ordered_task_ids())}
        ready = sorted(
            [task_id for task_id in task_ids if self.task_ready_for_conversion_queue(task_id)],
            key=lambda task_id: priority.get(task_id, len(priority)),
        )
        if not ready:
            return
        if not self.conversion_enabled:
            for task_id in ready:
                self.update_task(task_id, stage=STAGE_IMPORTED, progress=100, detail="Imported to watchfolder. Conversion is currently stopped.", eta_seconds=0)
            self.emit_log(f"Imported {len(ready)} file(s). Conversion is currently stopped.")
            return
        self.ensure_workers_running()
        for task_id in ready:
            self.enqueue_conversion(task_id)
        with self.queue_condition:
            self.queue_condition.notify_all()
        self.emit_log(f"Import batch complete. Queued {len(ready)} file(s) for conversion.")

    def import_single_file(self, src: Path, task_id: str, enqueue_after: bool) -> None:
        if self.import_cancel_requested.is_set() or self.shutdown_event.is_set():
            self.update_task(task_id, stage=STAGE_STOPPED, detail="Import cancelled before copy started", progress=0)
            return
        cfg = self.get_config()
        input_folder = Path(cfg["input_folder"])
        ensure_folder(input_folder)
        target = self.reserve_import_target(input_folder / src.name)
        with self.task_lock:
            task = self.tasks.get(task_id)
            if not task:
                self.release_import_target(target)
                return
            task.copy_thread_active = True
            stop_event = task.import_stop_event
        with self.task_lock:
            self.importing_count += 1
        self.emit_status()
        start_time = time.time()
        try:
            source_size = src.stat().st_size if src.exists() else 0
            disk_warning = self.low_disk_warning(target, source_size, "Import", cfg)
            if disk_warning:
                if "cannot start" in disk_warning:
                    self.update_task(task_id, stage=STAGE_ERROR, detail=disk_warning, error_log=disk_warning)
                    self.emit_log(disk_warning)
                    return
                self.update_task(task_id, detail=disk_warning, progress=0)
                self.emit_log(disk_warning)
            def on_progress(percent: int, copied: int, total: int) -> None:
                elapsed = time.time() - start_time
                eta = (elapsed / percent * (100 - percent)) if percent > 0 else None
                self.set_import_progress_snapshot(task_id, f"Import/watching: {src.name}", percent, eta)
                self.update_task(task_id, stage=STAGE_IMPORTING, progress=percent, detail=f"Importing {format_size(copied)} / {format_size(total)}", eta_seconds=eta)
            copied_digest = ""
            def on_checksum(digest: str) -> None:
                nonlocal copied_digest
                copied_digest = digest

            ok = self.copy_file_safely(src, target, stop_event, on_progress, on_checksum)
            if not ok:
                current_task = self.tasks.get(task_id)
                if self.import_cancel_requested.is_set() or stop_event.is_set() or (current_task and current_task.skipped):
                    self.update_task(task_id, stage=STAGE_SKIPPED if current_task and current_task.skipped else STAGE_STOPPED, detail="Import stopped", progress=0)
                else:
                    error = "Import copy failed; the recording disk file was kept unchanged"
                    self.update_task(task_id, stage=STAGE_ERROR, detail=error, progress=0, error_log=f"Copy failed: {src} -> {target}")
                    self.emit_log(f"{error}: {src}")
                return
            current_task = self.tasks.get(task_id)
            if not current_task:
                self.emit_log(f"Imported copy was kept because its dashboard task was removed: {target}")
                return
            if self.import_cancel_requested.is_set() or stop_event.is_set():
                self.update_task(task_id, stage=STAGE_STOPPED, detail="Import cancelled after copy; conversion was not queued", progress=100)
                return
            with self.task_lock:
                task = self.tasks.get(task_id)
                if not task:
                    return
                task.local_input_path = target
                task.imported_at = now_iso()
                task.imported_size = target.stat().st_size if target.exists() else None
                task.original_checksum = copied_digest
                self.task_by_path[path_key(target)] = task_id
            if copied_digest:
                verify_start = time.time()
                def on_verify_progress(percent: int, checked: int, total: int) -> None:
                    elapsed = time.time() - verify_start
                    eta = (elapsed / percent * (100 - percent)) if percent > 0 else None
                    self.set_import_progress_snapshot(task_id, f"Import/watching: verifying copy of {src.name}", percent, eta)
                    self.update_task(task_id, stage=STAGE_IMPORTING, progress=percent, detail=f"Verifying imported copy {format_size(checked)} / {format_size(total)}", eta_seconds=eta)
                verified_digest = sha256_file(target, stop_event, on_verify_progress)
                if not verified_digest or verified_digest != copied_digest:
                    force_delete(target)
                    self.update_task(task_id, local_input_path=None, original_checksum=None)
                    error = "Imported copy failed checksum verification. The recording disk file was kept unchanged."
                    self.update_task(task_id, stage=STAGE_ERROR, detail=error, progress=0, error_log=f"Checksum verification failed for imported copy: {target}")
                    self.emit_log(f"{error} Source: {src}")
                    return
                self.remember_original_checksum(copied_digest, src.name, source_size, target)
            else:
                force_delete(target)
                self.update_task(task_id, local_input_path=None, original_checksum=None)
                error = "Imported copy could not be checksummed. The recording disk file was kept unchanged."
                self.update_task(task_id, stage=STAGE_ERROR, detail=error, progress=0, error_log=f"No checksum was produced while copying: {src}")
                self.emit_log(error)
                return
            self.generate_thumbnail_async(task_id, target)
            self.record_session_stat("imported")
            self.update_task(task_id, stage=STAGE_IMPORTED, progress=100, detail="Imported to watchfolder", eta_seconds=0)
            current_task = self.tasks.get(task_id)
            if enqueue_after and self.conversion_enabled and current_task and not current_task.skipped:
                self.ensure_workers_running()
                self.enqueue_conversion(task_id)
            elif enqueue_after and current_task and not current_task.skipped:
                self.update_task(task_id, stage=STAGE_IMPORTED, progress=100, detail="Imported to watchfolder. Conversion is currently stopped.", eta_seconds=0)
        finally:
            with self.task_lock:
                task = self.tasks.get(task_id)
                if task:
                    task.copy_thread_active = False
            with self.task_lock:
                self.importing_count = max(0, self.importing_count - 1)
            self.clear_import_progress_snapshot(task_id)
            self.release_import_target(target)
            self.emit_status()

    def copy_file_safely(
        self,
        source: Path,
        destination: Path,
        stop_event: threading.Event,
        callback: Callable[[int, int, int], None],
        checksum_callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        chunk_bytes = max(1, int(self.get_config().get("copy_chunk_mb", 4))) * 1024 * 1024
        return copy_path_safely(
            source,
            destination,
            chunk_bytes,
            lambda: self.shutdown_event.is_set() or stop_event.is_set(),
            self.wait_if_paused,
            callback,
            self.promote_temp_no_overwrite,
            self.emit_log,
            checksum_callback,
        )

    def manual_scan(self) -> None:
        threading.Thread(target=self.scan_existing_input_as_detected, daemon=True).start()
        self.log("Scan now started. New videos in Importfolder will be queued when Conversion is on.")

    def scan_existing_input_as_detected(self) -> int:
        if not self.watchfolder_scan_lock.acquire(blocking=False):
            return 0
        count = 0
        queued_ids: List[str] = []
        try:
            cfg = self.get_config()
            input_value = str(cfg.get("input_folder", "")).strip()
            input_folder = Path(input_value) if input_value else None
            if not input_folder or not input_folder.is_dir():
                self.emit_log("Importfolder watch is waiting for a valid folder in Settings.")
                return 0
            exts = cfg.get("conversion_extensions", COMMON_VIDEO_EXTENSIONS)
            excluded_folders = [
                Path(value)
                for value in (cfg.get("output_folder", ""), cfg.get("nas_folder", ""))
                if str(value).strip()
            ]
            for path in scan_watchfolder_candidates(input_folder, exts, excluded_folders):
                key = path_key(path)
                if key in self.skipped_path_keys:
                    continue
                with self.task_lock:
                    if key in self.task_by_path:
                        continue
                task_id = self.create_task(path, stage=STAGE_IMPORTED, imported=True)
                self.update_task(task_id, progress=100, detail="Detected in Importfolder; waiting for conversion")
                queued_ids.append(task_id)
                count += 1
            if queued_ids:
                self.queue_imported_batch_for_conversion(queued_ids)
                action = "queued for conversion" if self.conversion_enabled else "ready when Conversion is started"
                self.emit_log(f"Importfolder watch found {count} new video(s); {action}.")
            return count
        finally:
            self.watchfolder_scan_lock.release()

    def drive_monitor_loop(self) -> None:
        while not self.shutdown_event.is_set():
            self.wait_if_paused()
            if not self.import_enabled:
                self.shutdown_event.wait(0.5)
                continue
            cfg = self.get_config()
            try:
                self.scan_existing_input_as_detected()
                drives = list_candidate_drives(cfg.get("ignored_drives", []))
                current_ids = {d for _, d in drives}
                self.processed_drive_ids.intersection_update(current_ids)
                for root, drive_id in drives:
                    if drive_id in self.processed_drive_ids:
                        continue
                    self.processed_drive_ids.add(drive_id)
                    self.import_drive(Path(root))
            except Exception as exc:
                self.emit_log(f"Drive monitor error: {exc}")
            self.shutdown_event.wait(max(1, int(cfg.get("scan_interval_seconds", 3))))

    def import_drive(self, root: Path) -> None:
        self.import_cancel_requested.clear()
        cfg = self.get_config()
        self.gui_queue.put(("disk_ready", ""))
        self.emit_log(f"New drive detected: {root}")
        scan_progress_id = f"drive_scan_{path_key(root)}"
        self.set_import_progress_snapshot(scan_progress_id, f"Import/watching: scanning {root}", 0, None)
        try:
            found, _scanned = scan_drive_importable_videos(
                root,
                cfg,
                lambda: not self.import_enabled or self.import_cancel_requested.is_set() or self.shutdown_event.is_set(),
                lambda scanned, found_count: self.set_import_progress_snapshot(scan_progress_id, f"Import/watching: scanning {root} ({scanned} files checked, {found_count} videos found)", 0, None),
            )
            if not self.import_enabled or self.import_cancel_requested.is_set() or self.shutdown_event.is_set():
                return
            if not found:
                self.emit_log(f"No importable videos found on {root}.")
                self.set_import_progress_snapshot(scan_progress_id, f"Import/watching: no videos found on {root}", 0, None)
                time.sleep(2)
                return
            self.emit_log(f"Found {len(found)} importable video(s) on {root}.")
            self.set_import_progress_snapshot(scan_progress_id, f"Import/watching: found {len(found)} video(s), starting soon", 0, None)
            delay = int(cfg.get("auto_import_delay_seconds", 5))
            for remaining in range(delay, 0, -1):
                if not self.import_enabled or self.shutdown_event.is_set():
                    return
                self.set_import_progress_snapshot(scan_progress_id, f"Import/watching: starting in {remaining}s", 0, None)
                time.sleep(1)
            queued_items: List[Tuple[Path, str]] = []
            total = len(found)
            for src in found:
                if not self.import_enabled or self.import_cancel_requested.is_set() or self.shutdown_event.is_set():
                    return
                task_id = self.create_task(src, stage=STAGE_DETECTED, imported=False, generate_thumbnail=False)
                self.update_task(task_id, detail="Waiting for import check...", progress=0)
                queued_items.append((src, task_id))
            self.queue_import_batch(queued_items, True, str(root), disk_batch=True)
            self.emit_log(f"Queued {total} video(s) from {root}. Imports will run one at a time.")
        finally:
            self.clear_import_progress_snapshot(scan_progress_id)

    # ---------------- conversion ----------------
    def enqueue_conversion(self, task_id: str, force: bool = False) -> None:
        with self.queue_condition:
            if force:
                self.force_conversion_ids.add(task_id)
            if task_id not in self.queued_conversion_ids:
                self.conversion_queue.append(task_id)
                self.queued_conversion_ids.add(task_id)
                self.queue_condition.notify_all()
        detail = "Waiting for in-place conversion" if force else "Waiting for conversion"
        self.update_task(task_id, stage=STAGE_QUEUED, detail=detail, progress=0)

    def move_to_top(self, task_id: str) -> None:
        self.set_task_priority_index(task_id, 0)

    def move_task_priority(self, task_id: str, direction: int) -> None:
        order = self.ordered_task_ids()
        if task_id not in order:
            return
        current = order.index(task_id)
        self.set_task_priority_index(task_id, current + direction)

    def set_task_priority_index(self, task_id: str, target_index: int) -> None:
        if task_id in {self.current_import_id, self.current_conversion_id, self.current_transfer_id}:
            self.log("The active item cannot be reordered until its current step finishes.")
            return
        with self.task_lock:
            order = list(self.tasks.keys())
            if task_id not in order:
                return
            old_index = order.index(task_id)
            target_index = max(0, min(len(order) - 1, target_index))
            if old_index == target_index:
                return
            order.pop(old_index)
            order.insert(target_index, task_id)
            ordered_tasks = [(tid, self.tasks[tid]) for tid in order]
            self.tasks.clear()
            self.tasks.update(ordered_tasks)
        rank = {tid: index for index, tid in enumerate(order)}
        with self.import_condition:
            reordered = sorted(self.import_queue, key=lambda tid: rank.get(tid, len(rank)))
            self.import_queue.clear()
            self.import_queue.extend(reordered)
            self.import_condition.notify_all()
        with self.queue_condition:
            reordered = sorted(self.conversion_queue, key=lambda tid: rank.get(tid, len(rank)))
            self.conversion_queue.clear()
            self.conversion_queue.extend(reordered)
            self.queue_condition.notify_all()
        with self.transfer_condition:
            reordered = sorted(self.transfer_queue, key=lambda tid: rank.get(tid, len(rank)))
            self.transfer_queue.clear()
            self.transfer_queue.extend(reordered)
            self.transfer_condition.notify_all()
        for row in self.row_widgets.values():
            row.get("render_cache", {}).pop("stage_text", None)
        self.refresh_dashboard_virtual_rows(force=True)
        self.save_pending_tasks()
        self.log(f"Priority updated: {self.tasks[task_id].display_name} is #{target_index + 1}.")

    def conversion_worker(self) -> None:
        while not self.shutdown_event.is_set():
            next_item = wait_for_conversion_task(
                self.queue_condition,
                self.conversion_queue,
                self.queued_conversion_ids,
                self.force_conversion_ids,
                lambda: self.conversion_enabled and not self.import_priority_blocks_conversion(),
                self.pause_event,
                self.shutdown_event,
            )
            if next_item is None:
                continue
            task_id, _force = next_item
            task = self.tasks.get(task_id)
            if not task or task.skipped:
                with self.queue_condition:
                    self.force_conversion_ids.discard(task_id)
                continue
            self.current_conversion_id = task_id
            try:
                self.convert_task(task_id)
            except Exception as exc:
                self.update_task(task_id, stage=STAGE_ERROR, detail=f"Conversion worker error: {exc}", error_log=traceback.format_exc())
                self.emit_log(f"Conversion worker error for {task.display_name}: {exc}")
            finally:
                with self.queue_condition:
                    self.force_conversion_ids.discard(task_id)
                self.current_conversion_id = None
                self.emit_status()

    def get_duration(self, input_path: Path, ffmpeg_path: Path) -> Optional[float]:
        return probe_duration(input_path, ffmpeg_path)

    def ffprobe_media_info(self, path: Path, ffmpeg_path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
        return probe_media_info(path, ffmpeg_path)

    def validate_converted_output(
        self,
        output_path: Path,
        source_path: Path,
        ffmpeg_path: Path,
        ffmpeg_args: List[str],
        expected_duration: Optional[float],
    ) -> Optional[str]:
        return validate_media_output(output_path, source_path, ffmpeg_path, ffmpeg_args, expected_duration)

    def convert_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or task.skipped:
            return
        cfg = self.get_config()
        source = task.source_for_conversion()
        if not source or not source.exists():
            self.update_task(task_id, stage=STAGE_ERROR, detail="Source file missing before conversion")
            return
        if task.convert_in_place and not task.conversion_started:
            for remaining in range(10, 0, -1):
                current_task = self.tasks.get(task_id)
                if not current_task or current_task.skipped or self.shutdown_event.is_set():
                    self.update_task(task_id, stage=STAGE_STOPPED, detail="In-place conversion cancelled before start", progress=0)
                    return
                self.update_task(task_id, stage=STAGE_QUEUED, detail=f"Converting in place starts in {remaining}s. You can still change the preset.", progress=0)
                time.sleep(1)
                self.wait_if_paused()
            task = self.tasks.get(task_id)
            if not task or task.skipped:
                self.update_task(task_id, stage=STAGE_STOPPED, detail="In-place conversion cancelled before start", progress=0)
                return
            source = task.source_for_conversion()
            if not source or not source.exists():
                self.update_task(task_id, stage=STAGE_ERROR, detail="Source file missing before conversion")
                return
        stable = int(cfg.get("file_stable_seconds", 10))
        max_wait = int(cfg.get("max_ready_wait_seconds", 7200))
        start_wait = time.time()
        self.update_task(task_id, conversion_started=True, stage=STAGE_WAITING, detail="Waiting for source file to be stable", progress=0)
        while not file_is_ready(source, stable, 1.0):
            if time.time() - start_wait > max_wait or (not self.conversion_enabled and not task.convert_in_place) or self.shutdown_event.is_set() or task.skipped:
                self.update_task(task_id, stage=STAGE_STOPPED, detail="Conversion did not start")
                return
            self.wait_if_paused()
        raw_ffmpeg_args = list(task.ffmpeg_args or cfg.get("ffmpeg_args", DEFAULT_FFMPEG_ARGS))
        # A preset must never be able to force overwriting the selected output.
        # Strip FFmpeg's global overwrite flags from user presets and add our own
        # safe no-overwrite flag when building the command below.
        ffmpeg_args = [arg for arg in raw_ffmpeg_args if str(arg).strip().lower() not in {"-y", "-n"}]
        if len(ffmpeg_args) != len(raw_ffmpeg_args):
            self.emit_log("Removed FFmpeg overwrite flag from preset for safety.")
        ext = detect_output_extension(ffmpeg_args)
        if task.convert_in_place:
            output_folder = source.parent
            desired_base = sanitize_base_name(task.rename_base or source.stem, bool(cfg.get("auto_underscore_renames", False))) or source.stem
            output_path = self.reserve_output_target(output_folder / f"{desired_base}{ext}")
        else:
            folder_error = validate_writable_folder(str(cfg.get("output_folder", "")), "Output folder")
            if folder_error:
                self.update_task(task_id, stage=STAGE_ERROR, detail=folder_error, error_log=folder_error)
                return
            output_folder = Path(cfg["output_folder"])
            ensure_folder(output_folder)
            output_path = self.reserve_output_target(output_folder / f"{source.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}")
        ffmpeg_path = Path(cfg["ffmpeg_path"])
        duration = self.get_duration(source, ffmpeg_path)
        source_size = source.stat().st_size if source.exists() else 0
        free = free_space_bytes(output_path)
        if free is not None and source_size and free < source_size:
            self.update_task(task_id, stage=STAGE_ERROR, detail=f"Not enough free space in output folder. Need about {format_size(source_size)}, free {format_size(free)}.", error_log=f"Output folder free space check failed for {output_path.parent}")
            self.release_output_target(output_path)
            return
        disk_warning = self.low_disk_warning(output_path, source_size, "Conversion", cfg)
        if disk_warning:
            if "cannot start" in disk_warning:
                self.update_task(task_id, stage=STAGE_ERROR, detail=disk_warning, error_log=disk_warning)
                self.release_output_target(output_path)
                self.emit_log(disk_warning)
                return
            self.update_task(task_id, detail=disk_warning)
            self.emit_log(disk_warning)
        mode_text = "in place" if task.convert_in_place else "to output folder"
        stage_text = STAGE_CONVERTING_IN_PLACE if task.convert_in_place else STAGE_CONVERTING
        self.update_task(task_id, stage=stage_text, detail=f"Output: {output_path.name} | Preset: {task.preset_name}", output_path=output_path, output_ext=ext, progress=0)
        self.emit_log(f"Starting conversion {mode_text}: {source.name}")
        time_pattern = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
        fps_pattern = re.compile(r"fps=\s*([0-9.]+)")
        speed_pattern = re.compile(r"speed=\s*([0-9.]+x)")
        # Use -n instead of -y for the main conversion output. The output path is
        # reserved by this app. FFmpeg writes a unique temp file first; only after
        # success do we promote it into the final name without overwriting.
        temp_output = output_path.with_name(f".mestersync_{uuid.uuid4().hex}_{output_path.name}")
        cmd = [str(ffmpeg_path), "-n", "-hide_banner", "-i", str(source)] + ffmpeg_args + [str(temp_output)]
        start = time.time()
        last_lines: List[str] = []
        proc: Optional[subprocess.Popen[str]] = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1, creationflags=no_window_flags())
            with self.current_process_lock:
                self.current_process = proc
                self.current_process_paused = False
            assert proc.stderr is not None
            for line in iter(proc.stderr.readline, ""):
                if (not self.conversion_enabled and not task.convert_in_place) or self.shutdown_event.is_set() or self.tasks.get(task_id, task).skipped:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
                self.wait_if_paused()
                line = line.rstrip()
                if line:
                    last_lines.append(line)
                    last_lines = last_lines[-8:]
                m = time_pattern.search(line)
                if m and duration:
                    current = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    progress = min(100, int(current / duration * 100))
                    elapsed = time.time() - start
                    eta = (elapsed / progress * (100 - progress)) if progress > 0 else None
                    fps_match = fps_pattern.search(line)
                    speed_match = speed_pattern.search(line)
                    speed_bits = []
                    if fps_match:
                        speed_bits.append(f"FPS: {fps_match.group(1)}")
                    if speed_match:
                        speed_bits.append(f"Speed: {speed_match.group(1)}")
                    preset_text = f"Preset: {task.preset_name}"
                    speed_text = f" | {' | '.join(speed_bits)}" if speed_bits else ""
                    self.update_task(task_id, stage=stage_text, progress=progress, detail=f"Elapsed: {format_duration(elapsed)} | {preset_text}{speed_text}", eta_seconds=eta)
                    self.emit_progress("conversion", f"Conversion: {source.name} | {preset_text}{speed_text}", progress, eta)
            code = proc.wait()
            with self.current_process_lock:
                self.current_process = None
                self.current_process_paused = False
            if (not self.conversion_enabled and not task.convert_in_place) or self.shutdown_event.is_set() or self.tasks.get(task_id, task).skipped:
                force_delete(temp_output)
                self.update_task(task_id, stage=STAGE_STOPPED, detail="Conversion stopped", progress=0)
                return
            if code == 0 and temp_output.exists():
                if not self.promote_temp_no_overwrite(temp_output, output_path):
                    force_delete(temp_output)
                    self.update_task(task_id, stage=STAGE_ERROR, detail=f"Could not finalize without overwriting existing file: {output_path.name}", progress=0, error_log=f"Final output appeared before promotion: {output_path}")
                    self.emit_log(f"Conversion could not finalize because destination appeared: {output_path}")
                    return
                validation_error = self.validate_converted_output(output_path, source, ffmpeg_path, ffmpeg_args, duration)
                if validation_error:
                    self.update_task(
                        task_id,
                        stage=STAGE_ERROR,
                        detail=f"Post-conversion safety check failed; source kept: {validation_error}",
                        progress=0,
                        output_path=None,
                        error_log=f"Post-conversion validation failed for {output_path}: {validation_error}",
                    )
                    self.emit_log(f"Converted file kept for inspection, but source was not deleted: {output_path} | {validation_error}")
                    return
                output_size = output_path.stat().st_size
                saved_bytes = 0
                if source_size > 0:
                    saved = max(0, source_size - output_size)
                    saved_bytes = saved
                    saved_pct = (saved / source_size) * 100
                    space_savings = f"Reduced from {format_size(source_size)} to {format_size(output_size)} ({saved_pct:.1f}% saved)"
                else:
                    space_savings = f"Converted size: {format_size(output_size)}"
                if not self.ensure_task_source_fingerprint(task_id, source):
                    self.update_task(
                        task_id,
                        stage=STAGE_ERROR,
                        detail="Conversion finished, but the source fingerprint could not be saved. Output and source were both kept.",
                        progress=100,
                        output_path=None,
                        output_size=output_size,
                        error_log=f"Source fingerprint failed. Converted output kept at: {output_path}",
                    )
                    self.emit_log(f"Source fingerprint failed; kept both source and converted output: {source}")
                    return
                if saved_bytes:
                    self.record_session_saved_bytes(saved_bytes)
                self.record_session_stat("converted")
                self.update_task(task_id, stage=STAGE_CONVERTED, converted_at=now_iso(), progress=100, detail=space_savings, output_path=output_path, output_size=output_size, imported_size=source_size, space_savings=space_savings, eta_seconds=0)
                self.emit_log(f"Conversion complete: {output_path.name} | {space_savings}")
                if task.convert_in_place:
                    self.finalize_convert_in_place(task_id)
                else:
                    self.after_conversion(task_id)
                if task_id in self.tasks:
                    # Persist the output path and its next stage immediately. A
                    # restart can then resume transfer without reconverting.
                    self.save_pending_tasks(force=True)
            else:
                force_delete(temp_output)
                msg = " | ".join(last_lines[-4:]) if last_lines else f"Return code {code}"
                self.update_task(task_id, stage=STAGE_ERROR, detail=f"FFmpeg failed: {msg[:220]}", progress=0, error_log="\n".join(last_lines) or f"Return code {code}")
                self.emit_log(f"Conversion failed for {source.name}: {msg}")
        except Exception as exc:
            force_delete(temp_output)
            self.update_task(task_id, stage=STAGE_ERROR, detail=f"Conversion error: {exc}", error_log=str(exc))
            self.emit_log(f"Conversion error for {source.name}: {exc}")
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                except OSError as exc:
                    self.emit_log(f"FFmpeg process cleanup warning for {source.name}: {exc}")
            with self.current_process_lock:
                if self.current_process is proc:
                    self.current_process = None
                    self.current_process_paused = False
            self.release_output_target(output_path)
            self.emit_progress("conversion", "Conversion: idle", 0, None)

    def after_conversion(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or not task.output_path:
            return
        if self.transfer_enabled:
            self.enqueue_transfer(task_id)
        else:
            self.emit_log(f"Transfer is disabled; keeping converted file in output folder: {task.output_path.name}")
            self.finalize_converted_only(task_id)

    def finalize_converted_only(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or not task.output_path or not task.output_path.exists():
            self.update_task(task_id, stage=STAGE_ERROR, detail="Converted output missing before output-folder finalize")
            return
        target = self.reserve_safe_final_target(task, task.output_path.parent, "reserved_output_paths")
        try:
            if path_key(target) != path_key(task.output_path):
                if self.move_file_no_overwrite(task.output_path, target):
                    task.output_path = target
                    task.rename_base = target.stem
                else:
                    self.update_task(task_id, stage=STAGE_ERROR, detail=f"Could not rename without overwriting existing file: {target.name}")
                    self.emit_log(f"Could not rename output because target already exists: {target}")
                    return
            task.stage = STAGE_CONVERTED_OUTPUT
            task.detail = "Converted to output folder; not transferred"
            task.progress = 100
            self.update_checksum_final_path(task, task.output_path)
            self.archive_task(task_id, status="converted_only")
            # Persist the completed record before cleanup. A crash can then only
            # leave an extra local source, never an unrecorded converted result.
            if task.local_input_path and task.local_input_path.exists():
                if force_delete(task.local_input_path):
                    self.emit_log(f"Deleted local imported source after output conversion: {task.local_input_path.name}")
                else:
                    self.emit_log(f"Could not delete local imported source: {task.local_input_path}")
        finally:
            self.release_output_target(target)

    def finalize_convert_in_place(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or not task.output_path or not task.output_path.exists():
            self.update_task(task_id, stage=STAGE_ERROR, detail="Converted output missing before in-place finalize")
            return
        task.rename_base = task.output_path.stem
        task.stage = STAGE_CONVERTED_IN_PLACE
        task.detail = f"Converted beside original: {task.output_path.name}"
        task.progress = 100
        self.update_checksum_final_path(task, task.output_path)
        self.archive_task(task_id, status="converted_in_place")

    # ---------------- transfer ----------------
    def enqueue_transfer(self, task_id: str) -> None:
        with self.task_lock:
            task = self.tasks.get(task_id)
            if task and task_id != self.current_transfer_id and task.transfer_stop_event.is_set():
                task.transfer_stop_event = threading.Event()
        with self.transfer_condition:
            if task_id not in self.queued_transfer_ids:
                self.transfer_queue.append(task_id)
                self.queued_transfer_ids.add(task_id)
                self.transfer_condition.notify_all()
        self.update_task(task_id, stage=STAGE_WAITING_TRANSFER, detail="Waiting for transfer", progress=100)

    def transfer_worker(self) -> None:
        while not self.shutdown_event.is_set():
            task_id = wait_for_transfer_task(
                self.transfer_condition,
                self.transfer_queue,
                self.queued_transfer_ids,
                lambda: self.transfer_enabled,
                self.pause_event,
                self.shutdown_event,
            )
            if task_id is None:
                continue
            task = self.tasks.get(task_id)
            if not task or task.skipped:
                continue
            self.current_transfer_id = task_id
            try:
                self.transfer_task(task_id)
            except Exception as exc:
                self.update_task(task_id, stage=STAGE_ERROR, detail=f"Transfer worker error: {exc}", error_log=traceback.format_exc())
                self.emit_log(f"Transfer worker error for {task.display_name}: {exc}")
            finally:
                self.current_transfer_id = None

    def transfer_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or not task.output_path:
            return
        source = task.output_path
        if not source.exists():
            self.update_task(task_id, stage=STAGE_ERROR, detail=f"Converted file missing before transfer: {source}")
            self.emit_log(f"Transfer failed because converted file is missing: {source}")
            return
        cfg = self.get_config()
        nas_folder = Path(cfg["nas_folder"])
        dest = self.reserve_safe_final_target(task, nas_folder, "reserved_nas_paths")
        try:
            source_size = source.stat().st_size if source.exists() else 0
            disk_warning = self.low_disk_warning(dest, source_size, "Transfer", cfg)
            if disk_warning:
                if "cannot start" in disk_warning:
                    self.update_task(task_id, stage=STAGE_ERROR, detail=disk_warning, error_log=disk_warning)
                    self.emit_log(disk_warning)
                    return
                self.update_task(task_id, detail=disk_warning)
                self.emit_log(disk_warning)
            self.update_task(task_id, stage=STAGE_TRANSFERRING, detail=f"To NAS: {dest.name}", progress=0)
            self.emit_log(f"Starting NAS transfer: {source.name}")
            start = time.time()
            def on_progress(percent: int, copied: int, total: int) -> None:
                elapsed = time.time() - start
                eta = (elapsed / percent * (100 - percent)) if percent > 0 else None
                self.update_task(task_id, stage=STAGE_TRANSFERRING, progress=percent, detail=f"{format_size(copied)} / {format_size(total)}", eta_seconds=eta)
                self.emit_progress("transfer", f"Transfer: {dest.name}", percent, eta)
            copied_digest = ""
            def on_checksum(digest: str) -> None:
                nonlocal copied_digest
                copied_digest = digest

            ok = self.copy_file_safely(source, dest, task.transfer_stop_event, on_progress, on_checksum)
            if not ok:
                if task.transfer_stop_event.is_set() or task.skipped:
                    self.update_task(task_id, stage=STAGE_SKIPPED if task.skipped else STAGE_STOPPED, detail="Transfer stopped; converted file kept in output folder")
                    return
                self.update_task(task_id, stage=STAGE_ERROR, detail="Transfer failed; converted file kept in output folder")
                return
            if task.transfer_stop_event.is_set() or task.skipped:
                force_delete(dest)
                self.update_task(task_id, stage=STAGE_SKIPPED if task.skipped else STAGE_STOPPED, detail="Transfer stopped; converted file kept in output folder")
                return
            if not dest.exists() or dest.stat().st_size != source.stat().st_size:
                force_delete(dest)
                self.update_task(task_id, stage=STAGE_ERROR, detail="NAS verification failed; local files kept")
                self.emit_log(f"NAS verification failed for {dest}")
                return
            verify_start = time.time()
            def on_verify_progress(percent: int, checked: int, total: int) -> None:
                elapsed = time.time() - verify_start
                eta = (elapsed / percent * (100 - percent)) if percent > 0 else None
                self.update_task(
                    task_id,
                    stage=STAGE_TRANSFERRING,
                    progress=percent,
                    detail=f"Verifying NAS copy {format_size(checked)} / {format_size(total)}",
                    eta_seconds=eta,
                )
                self.emit_progress("transfer", f"Verifying NAS copy: {dest.name}", percent, eta)

            verified_digest = sha256_file(dest, task.transfer_stop_event, on_verify_progress)
            if task.transfer_stop_event.is_set() or task.skipped:
                force_delete(dest)
                self.update_task(task_id, stage=STAGE_SKIPPED if task.skipped else STAGE_STOPPED, detail="NAS verification stopped; local files kept")
                return
            if not copied_digest or not verified_digest or copied_digest != verified_digest:
                force_delete(dest)
                error = "NAS copy failed checksum verification; local files were kept"
                self.update_task(task_id, stage=STAGE_ERROR, detail=error, error_log=f"Checksum verification failed for NAS copy: {dest}")
                self.emit_log(f"{error}: {dest}")
                return
            self.emit_log(f"Transfer checksum verified on NAS: {dest.name}")
            task.rename_base = dest.stem
            self.update_task(task_id, stage=STAGE_COMPLETE, progress=100, transferred_at=now_iso(), nas_path=dest, detail=f"Transferred to NAS: {dest.name}", eta_seconds=0)
            self.update_checksum_final_path(task, dest)
            self.archive_task(task_id, status="transferred")
            # History/checksum state is durable before local cleanup. If cleanup
            # is interrupted, the verified NAS file remains the source of truth.
            if force_delete(source):
                self.emit_log(f"Deleted local converted file: {source.name}")
            else:
                self.emit_log(f"Could not delete local converted file: {source}")
            if task.local_input_path and task.local_input_path.exists():
                if force_delete(task.local_input_path):
                    self.emit_log(f"Deleted local imported source after NAS verification: {task.local_input_path.name}")
                else:
                    self.emit_log(f"Could not delete local imported source: {task.local_input_path}")
        finally:
            self.release_nas_target(dest)
            self.emit_progress("transfer", "Transfer: idle", 0, None)

    # ---------------- skip/remove/archive ----------------
    def restart_or_resume_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        task.skipped = False
        task.skip_archive_due = None
        task.import_stop_event = threading.Event()
        task.transfer_stop_event = threading.Event()
        for p in [task.original_path, task.local_input_path, task.output_path]:
            if p:
                self.skipped_path_keys.discard(path_key(p))

        if task.output_path and task.output_path.exists() and task.stage in {STAGE_WAITING_TRANSFER, STAGE_STOPPED}:
            self.ensure_workers_running()
            if self.transfer_enabled:
                self.update_task(task_id, stage=STAGE_WAITING_TRANSFER, detail="Restart/resume queued for NAS transfer", progress=100)
                self.enqueue_transfer(task_id)
            else:
                self.finalize_converted_only(task_id)
            self.emit_status()
            return

        if not task.local_input_path and not task.convert_in_place and task.original_path.exists():
            self.import_cancel_requested.clear()
            if task_id in self.import_job_batch:
                self.requeue_duplicate_import(task_id)
            else:
                self.queue_import_batch([(task.original_path, task_id)], False, "Resumed import")
            self.log(f"Restart/resume queued for import: {task.display_name}")
            return

        source = task.source_for_conversion()
        if source and source.exists():
            self.ensure_workers_running()
            self.conversion_enabled = True
            self.update_task(task_id, stage=STAGE_QUEUED, detail="Restart/resume queued for conversion", progress=0)
            self.enqueue_conversion(task_id)
            with self.queue_condition:
                self.queue_condition.notify_all()
            self.emit_status()
            self.log(f"Restart/resume queued: {task.display_name}")
            return

        self.update_task(task_id, stage=STAGE_ERROR, detail="Restart/resume failed: source file is missing", progress=0)

    def retry_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        task.skipped = False
        task.skip_archive_due = None
        task.import_stop_event = threading.Event()
        task.transfer_stop_event = threading.Event()
        self.skipped_path_keys.discard(path_key(task.original_path))
        if task.local_input_path:
            self.skipped_path_keys.discard(path_key(task.local_input_path))
        if task.output_path:
            self.skipped_path_keys.discard(path_key(task.output_path))

        if task.output_path and task.output_path.exists():
            self.ensure_workers_running()
            if self.transfer_enabled:
                self.update_task(task_id, stage=STAGE_WAITING_TRANSFER, detail="Retry queued for transfer", progress=100)
                self.enqueue_transfer(task_id)
            else:
                self.finalize_converted_only(task_id)
            return

        if not task.local_input_path and not task.convert_in_place and task.original_path.exists():
            self.import_cancel_requested.clear()
            if task_id in self.import_job_batch:
                self.requeue_duplicate_import(task_id)
            else:
                self.queue_import_batch([(task.original_path, task_id)], False, "Retried import")
            self.log(f"Retry queued for import: {task.display_name}")
            return

        source = task.source_for_conversion()
        if source and source.exists():
            self.ensure_workers_running()
            self.conversion_enabled = True
            self.update_task(task_id, stage=STAGE_QUEUED, detail="Retry queued for conversion", progress=0)
            self.enqueue_conversion(task_id)
            with self.queue_condition:
                self.queue_condition.notify_all()
            self.emit_status()
            return

        self.update_task(task_id, stage=STAGE_ERROR, detail="Retry failed: source file is missing", progress=0)

    def skip_task(self, task_id: str) -> None:
        with self.task_lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.skipped = True
            task.stage = STAGE_SKIPPED
            task.skip_archive_due = new_skip_deadline()
            task.detail = skip_undo_detail(task.skip_archive_due)
            task.import_stop_event.set()
            task.transfer_stop_event.set()
            for p in [task.original_path, task.local_input_path, task.output_path]:
                if p:
                    self.skipped_path_keys.add(path_key(p))
        # Skip is a UI action, so redraw the row synchronously before doing any
        # queue/process cleanup. This also avoids relying on update_task after
        # the values above have already been assigned.
        if task_id in getattr(self, "row_widgets", {}):
            self.render_task(task_id)
        self.save_pending_tasks()
        self.emit_task(task_id)
        with self.queue_condition:
            try:
                self.conversion_queue.remove(task_id)
            except ValueError:
                pass
            self.queued_conversion_ids.discard(task_id)
            self.force_conversion_ids.discard(task_id)
        with self.transfer_condition:
            try:
                self.transfer_queue.remove(task_id)
            except ValueError:
                pass
            self.queued_transfer_ids.discard(task_id)
        self.remove_import_job(task_id, resolve_batch=task_id != self.current_import_id)
        if task_id == self.current_conversion_id:
            with self.current_process_lock:
                proc = self.current_process
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self.log(f"Skipped: {task.display_name}")

    def resume_task(self, task_id: str) -> None:
        with self.task_lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.skipped = False
            task.skip_archive_due = None
            task.import_stop_event = threading.Event()
            task.transfer_stop_event = threading.Event()
            for p in [task.original_path, task.local_input_path, task.output_path]:
                if p:
                    self.skipped_path_keys.discard(path_key(p))
            task.detail = "Resuming..."
            task.skip_archive_due = None
        self.save_pending_tasks()
        self.emit_task(task_id)
        self.route_resumed_task(task_id)

    def route_resumed_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        self.ensure_workers_running()
        if task.convert_in_place:
            source = task.source_for_conversion()
            if source and source.exists():
                self.update_task(task_id, stage=STAGE_QUEUED, detail="Resume queued for in-place conversion", progress=0)
                self.enqueue_conversion(task_id, force=True)
                self.log(f"Resumed: {task.display_name}")
                return
            self.update_task(task_id, stage=STAGE_ERROR, detail="Resume failed: source file is missing", progress=0)
            return
        if task.output_path and task.output_path.exists():
            if self.transfer_enabled:
                self.update_task(task_id, stage=STAGE_WAITING_TRANSFER, detail="Resume queued for NAS transfer", progress=100)
                self.enqueue_transfer(task_id)
            else:
                self.finalize_converted_only(task_id)
            self.log(f"Resumed: {task.display_name}")
            return
        if task.local_input_path and task.local_input_path.exists():
            if self.conversion_enabled:
                self.update_task(task_id, stage=STAGE_QUEUED, detail="Resume queued for conversion", progress=0)
                self.enqueue_conversion(task_id)
            else:
                self.update_task(task_id, stage=STAGE_IMPORTED, detail="Resumed. Conversion is currently stopped.", progress=100)
            self.log(f"Resumed: {task.display_name}")
            return
        if task.original_path and task.original_path.exists():
            self.import_cancel_requested.clear()
            self.update_task(task_id, stage=STAGE_DETECTED, detail="Resume queued for import", progress=0)
            threading.Thread(target=self.resume_import_when_ready, args=(task_id,), daemon=True).start()
            self.log(f"Resumed: {task.display_name}")
            return
        self.update_task(task_id, stage=STAGE_ERROR, detail="Resume failed: source file is missing", progress=0)

    def resume_import_when_ready(self, task_id: str) -> None:
        for _ in range(100):
            task = self.tasks.get(task_id)
            if not task or self.shutdown_event.is_set():
                return
            if not task.copy_thread_active:
                break
            self.update_task(task_id, stage=STAGE_IMPORTING, detail="Waiting for skipped import to stop before resuming...", progress=0)
            self.shutdown_event.wait(0.1)
        task = self.tasks.get(task_id)
        if not task or task.skipped:
            return
        source = task.original_path
        if not source or not source.exists():
            self.update_task(task_id, stage=STAGE_ERROR, detail="Resume failed: source file is missing", progress=0)
            return
        self.queue_import_batch([(source, task_id)], False, "Resumed import")

    def remove_skipped_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        if task.stage == STAGE_DUPLICATE_REVIEW:
            self.remove_task(task_id)
            self.log(f"Removed from list: {task.display_name}")
            return
        if not task.skipped:
            return
        task.import_stop_event.set()
        task.transfer_stop_event.set()
        if task.copy_thread_active:
            self.update_task(task_id, stage=STAGE_STOPPING, detail="Waiting for the active import copy to stop before removal")
            self.log(f"Waiting for import to stop safely before removing: {task.display_name}")
            return
        delete_local = False
        if task.local_input_path and task.local_input_path.exists() and not task.convert_in_place:
            delete_local = messagebox.askyesno(APP_NAME, f"Delete the imported file from the importfolder too?\n\n{task.local_input_path}")
        local_to_delete = task.local_input_path if delete_local else None
        self.remove_task(task_id)
        if local_to_delete:
            force_delete(local_to_delete)
        self.log(f"Removed skipped file from list: {task.display_name}")

    def remove_task(self, task_id: str) -> None:
        self.remove_import_job(task_id, resolve_batch=True)
        with self.task_lock:
            task = self.tasks.pop(task_id, None)
            if task:
                for p in [task.original_path, task.local_input_path, task.output_path, task.nas_path]:
                    if p:
                        self.task_by_path.pop(path_key(p), None)
        with self.queue_condition:
            self.force_conversion_ids.discard(task_id)
        try:
            self.save_pending_tasks(force=True)
        except Exception:
            if task:
                with self.task_lock:
                    self.tasks[task_id] = task
                    for path in (task.original_path, task.local_input_path, task.output_path, task.nas_path):
                        if path:
                            self.task_by_path[path_key(path)] = task_id
            raise
        if task:
            self.cleanup_task_thumbnails(task)
        row = self.row_widgets.pop(task_id, None)
        self.last_task_render_at.pop(task_id, None)
        self.throttled_dashboard_task_ids.discard(task_id)
        self.deferred_dashboard_task_ids.discard(task_id)
        with self.task_event_lock:
            self.pending_task_events.pop(task_id, None)
        if row:
            row["outer"].destroy()
        if task_id in self.selected_ids:
            self.selected_ids.remove(task_id)
        self.update_batch_box_visibility()
        self.schedule_dashboard_virtual_refresh()

    def remove_archived_task_ui(self, task_id: str) -> None:
        row = self.row_widgets.pop(task_id, None)
        self.last_task_render_at.pop(task_id, None)
        self.throttled_dashboard_task_ids.discard(task_id)
        self.deferred_dashboard_task_ids.discard(task_id)
        with self.task_event_lock:
            self.pending_task_events.pop(task_id, None)
        if row:
            try:
                row["outer"].destroy()
            except tk.TclError as exc:
                self.log(f"Dashboard cleanup warning: {exc}")
        if task_id in self.selected_ids:
            self.selected_ids.remove(task_id)
        self.update_batch_box_visibility()
        self.schedule_dashboard_virtual_refresh()

    def check_skip_countdowns(self) -> None:
        now = time.time()
        due: List[str] = []
        with self.task_lock:
            for tid, task in self.tasks.items():
                if task.skipped and skip_is_due(task.skip_archive_due, now):
                    due.append(tid)
        for tid in due:
            self.archive_task(tid, status="skipped")

    def refresh_skip_undo_rows(self) -> None:
        if getattr(self, "current_tab", "dashboard") != "dashboard":
            return
        now = time.time()
        with self.task_lock:
            skipped = [
                (tid, task.skip_archive_due)
                for tid, task in self.tasks.items()
                if task.skipped and task.skip_archive_due
            ]
        for task_id, deadline in skipped:
            row = self.row_widgets.get(task_id)
            if not row or not self.row_widget_alive(task_id):
                continue
            try:
                row["resume_btn"].configure(text=skip_undo_button_text(deadline, now))
                row["detail"].configure(text=skip_undo_detail(deadline, now), fg="#ffd0d8", font=("Segoe UI", 10, "bold"))
            except tk.TclError:
                continue

    def archive_task(self, task_id: str, status: str) -> None:
        with self.task_lock:
            task = self.tasks.pop(task_id, None)
            if not task:
                return
            for p in [task.original_path, task.local_input_path, task.output_path, task.nas_path]:
                if p:
                    self.task_by_path.pop(path_key(p), None)
        self.remove_import_job(task_id, resolve_batch=True)
        with self.queue_condition:
            self.force_conversion_ids.discard(task_id)
            self.queued_conversion_ids.discard(task_id)
            try:
                self.conversion_queue.remove(task_id)
            except ValueError:
                pass
        with self.transfer_condition:
            self.queued_transfer_ids.discard(task_id)
            try:
                self.transfer_queue.remove(task_id)
            except ValueError:
                pass
        if status == "skipped":
            detail = "Skipped"
            final_path = ""
            self.record_session_stat("skipped")
        elif status == "converted_only":
            detail = "Converted to output folder; not transferred"
            final_path = str(task.output_path) if task.output_path else ""
        elif status == "converted_in_place":
            detail = "Converted in place"
            final_path = str(task.output_path) if task.output_path else ""
        else:
            detail = "Transferred to NAS"
            final_path = str(task.nas_path) if task.nas_path else ""
            self.record_session_stat("transferred")
        record = {
            "status": status,
            "display_name": task.display_name,
            "final_name": task.final_name(),
            "detail": detail,
            "space_savings": task.space_savings,
            "path": final_path,
            "folder": str(Path(final_path).parent) if final_path else "",
            "imported_at": task.imported_at,
            "converted_at": task.converted_at,
            "transferred_at": task.transferred_at,
            "total_duration": task.total_duration_seconds(),
            "thumbnail_path": task.thumbnail_path,
            "thumbnail_preview_paths": task.thumbnail_preview_paths,
            "original_checksum": task.original_checksum,
            "input_path": str(task.local_input_path) if status == "skipped" and task.local_input_path else "",
            "input_folder": str(self.get_config().get("input_folder", "")) if status == "skipped" and task.local_input_path else "",
        }
        try:
            self.save_pending_tasks(force=True)
            with self.history_lock:
                self.history_records.insert(0, record)
                try:
                    self.save_history(force=True)
                except Exception:
                    if self.history_records and self.history_records[0] is record:
                        self.history_records.pop(0)
                    raise
        except Exception:
            # Atomic file writes preserve the previous files, and restoring the
            # in-memory task keeps the failure visible/retryable in this session.
            with self.task_lock:
                self.tasks[task_id] = task
                task.stage = STAGE_ERROR
                task.detail = "Could not save completion state; final media was kept"
                task.skip_archive_due = None
                for path in (task.original_path, task.local_input_path, task.output_path, task.nas_path):
                    if path:
                        self.task_by_path[path_key(path)] = task_id
            try:
                self.save_pending_tasks(force=True)
            except Exception as recovery_exc:
                self.emit_log(f"Could not persist restored completion task {task.display_name}: {recovery_exc}")
            self.emit_task(task_id)
            raise
        self.gui_queue.put(("archive_ui", task_id))
        self.gui_queue.put(("history", None))

    # ---------------- history UI ----------------
    def request_history_view_refresh(self, reset_limit: bool = True) -> None:
        self.history_view_dirty = True
        if reset_limit:
            self.history_display_limit = HISTORY_PAGE_SIZE
        if getattr(self, "current_tab", "") == "history":
            self.refresh_history_view()

    def show_more_history(self) -> None:
        self.history_display_limit += HISTORY_PAGE_SIZE
        self.refresh_history_view()

    def cancel_history_render(self, mark_dirty: bool = False) -> None:
        self.history_render_generation += 1
        self.history_thumbnail_labels.clear()
        if self.history_render_after_id is not None:
            try:
                self.root.after_cancel(self.history_render_after_id)
            except Exception:
                pass
            self.history_render_after_id = None
        if mark_dirty:
            self.history_view_dirty = True

    def refresh_history_view(self) -> None:
        self.cancel_history_render()
        self.history_view_dirty = False
        for widget in self.history_widgets:
            try:
                widget.destroy()
            except Exception:
                pass
        self.history_widgets.clear()
        with self.history_lock:
            records = list(self.history_records)
        self.update_total_converted_label()
        if not records:
            label = tk.Label(self.history_inner, text="No history yet.", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 11))
            label.pack(anchor="w", pady=16)
            self.history_widgets.append(label)
            return
        visible_records = history_page(records, self.history_display_limit)
        loading = tk.Label(
            self.history_inner,
            text=f"Loading history... 0 of {len(visible_records)}",
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 10, "bold"),
        )
        loading.pack(anchor="w", pady=16)
        self.history_loading_widget = loading
        self.history_widgets.append(loading)
        generation = self.history_render_generation
        self.history_render_after_id = self.root.after_idle(
            lambda: self.render_history_batch(generation, records, visible_records, 0)
        )

    def render_history_batch(
        self,
        generation: int,
        all_records: List[Dict[str, Any]],
        visible_records: List[Dict[str, Any]],
        cursor: int,
    ) -> None:
        self.history_render_after_id = None
        if generation != self.history_render_generation or self.current_tab != "history":
            self.history_view_dirty = True
            return
        if cursor == 0 and self.history_loading_widget is not None:
            try:
                self.history_loading_widget.destroy()
            except Exception:
                pass
            try:
                self.history_widgets.remove(self.history_loading_widget)
            except ValueError:
                pass
            self.history_loading_widget = None
        stop = min(len(visible_records), cursor + HISTORY_RENDER_BATCH_SIZE)
        for index in range(cursor, stop):
            self.build_history_record_card(visible_records[index], index, generation)
        if stop < len(visible_records):
            self.history_render_after_id = self.root.after(
                8,
                lambda: self.render_history_batch(generation, all_records, visible_records, stop),
            )
            return
        if len(all_records) > len(visible_records):
            footer = tk.Frame(self.history_inner, bg=self.CARD, padx=12, pady=10)
            footer.pack(fill="x", pady=(8, 4))
            tk.Label(
                footer,
                text=f"Showing {len(visible_records)} of {len(all_records)} history items",
                bg=self.CARD,
                fg=self.MUTED,
                font=("Segoe UI", 9, "bold"),
            ).pack(side="left")
            remaining = min(HISTORY_PAGE_SIZE, len(all_records) - len(visible_records))
            self.small_button(footer, f"Show {remaining} more", self.show_more_history, self.CARD3).pack(side="right")
            self.history_widgets.append(footer)
        self.update_total_converted_label()

    def build_history_record_card(self, rec: Dict[str, Any], index: int, generation: int) -> None:
        status = rec.get("status", "")
        if status == "transferred":
            bg = self.DARK_GREEN
            headline = "Transferred to NAS"
        elif status == "converted_only":
            bg = self.HISTORY_YELLOW
            headline = "Converted to output folder; not transferred"
        elif status == "converted_in_place":
            bg = self.HISTORY_YELLOW
            headline = "Converted in place"
        elif status == "skipped":
            bg = self.CARD
            headline = "Skipped"
        else:
            bg = self.CARD
            headline = rec.get("detail", "")
        outer = tk.Frame(self.history_inner, bg=self.BORDER)
        outer.pack(fill="x", pady=7)
        card = tk.Frame(outer, bg=bg, padx=12, pady=12)
        card.pack(fill="x", padx=2, pady=2)
        left = tk.Frame(card, bg=bg)
        left.pack(side="left")
        thumb_frame = tk.Frame(left, bg=self.CARD3, width=222, height=125, highlightthickness=2, highlightbackground=self.BORDER)
        thumb_frame.pack()
        thumb_frame.pack_propagate(False)
        thumb = tk.Label(thumb_frame, text="Preview", bg=self.CARD3, fg=self.MUTED, width=222, height=125)
        thumb.pack(fill="both", expand=True)
        tpath = rec.get("thumbnail_path")
        if tpath and Path(tpath).exists():
            if self.thumbnail_images.has_source(tpath):
                self.thumbnail_images.configure_label(thumb, tpath)
            else:
                token = f"history:{generation}:{index}"
                thumb.configure(text="Loading preview...")
                self.history_thumbnail_labels[token] = (thumb, tpath)
                self.request_history_thumbnail_display(token, tpath)
        right = tk.Frame(card, bg=bg)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))
        title = tk.Label(right, text=f"{rec.get('display_name', '')}  ->  {rec.get('final_name', '')}", bg=bg, fg=self.TEXT, font=("Segoe UI", 12, "bold"), anchor="w")
        title.pack(fill="x")
        path_value = rec.get("path")
        if path_value:
            title.bind("<Double-Button-1>", lambda e, p=path_value: self.open_path_with_feedback(p, "file"))
        tk.Label(right, text=headline, bg=bg, fg=self.TEXT, font=("Segoe UI", 14, "bold") if status in {"converted_only", "converted_in_place"} else ("Segoe UI", 11, "bold"), anchor="w").pack(fill="x", pady=(8, 3))
        if rec.get("space_savings"):
            tk.Label(right, text=rec.get("space_savings"), bg=bg, fg=self.TEXT, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", pady=(0, 3))
        times = f"Imported: {pretty_time(rec.get('imported_at'))}    Converted: {pretty_time(rec.get('converted_at'))}    Transferred: {pretty_time(rec.get('transferred_at'))}    Total: {format_duration(rec.get('total_duration'))}"
        tk.Label(right, text=times, bg=bg, fg=self.MUTED, font=("Segoe UI", 9), anchor="w").pack(fill="x")
        btns = tk.Frame(right, bg=bg)
        btns.pack(fill="x", pady=(9, 0))
        if status != "skipped" and rec.get("folder"):
            self.small_button(btns, "Open folder", lambda p=rec.get("folder"): self.open_path_with_feedback(p, "folder"), self.CARD3).pack(side="left", padx=(0, 6))
        if status in {"converted_only", "converted_in_place"} and rec.get("path"):
            self.small_button(btns, "Open converted file", lambda p=rec.get("path"): self.open_path_with_feedback(p, "converted file"), self.GREEN).pack(side="left", padx=(0, 6))
        if status == "skipped" and rec.get("original_delete_pending"):
            tk.Label(btns, text="Deleting original...", bg=bg, fg=self.MUTED, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        elif status == "skipped" and rec.get("input_path"):
            self.small_button(btns, "Delete original file", lambda i=index: self.delete_history_original_file(i), self.RED).pack(side="left", padx=(0, 6))
        self.small_button(btns, "Remove", lambda i=index: self.remove_history_entry(i), self.RED).pack(side="left")
        self.history_widgets.append(outer)

    def clear_history(self) -> None:
        if not messagebox.askyesno(APP_NAME, "Clear history?"):
            return
        with self.history_lock:
            records = list(self.history_records)
            self.history_records.clear()
            self.save_history(force=True)
        for rec in records:
            self.cleanup_history_thumbnails(rec)
        self.refresh_history_view()

    def remove_history_entry(self, index: int) -> None:
        removed: Optional[Dict[str, Any]] = None
        with self.history_lock:
            if 0 <= index < len(self.history_records):
                removed = self.history_records.pop(index)
                self.save_history(force=True)
        if removed:
            self.cleanup_history_thumbnails(removed)
        self.refresh_history_view()

    def history_original_file(self, record: Dict[str, Any]) -> Optional[Path]:
        return recorded_input_file(record, str(self.get_config().get("input_folder", "")))

    def delete_history_original_file(self, index: int) -> None:
        with self.history_lock:
            if not 0 <= index < len(self.history_records):
                return
            record = self.history_records[index]
            candidate = self.history_original_file(record)
        if not candidate:
            self.show_notification("The original file is missing or is no longer inside the recorded import folder.", "warning")
            self.refresh_history_view()
            return
        try:
            size_text = format_size(candidate.stat().st_size)
        except OSError:
            size_text = "unknown size"
        if not messagebox.askyesno(APP_NAME, f"Permanently delete this original import-folder file?\n\n{candidate}\n\nSize: {size_text}"):
            return
        with self.history_lock:
            if 0 <= index < len(self.history_records) and self.history_records[index] is record:
                record["original_delete_pending"] = True
        self.refresh_history_view()
        threading.Thread(
            target=self.delete_history_original_worker,
            args=(record, candidate),
            daemon=True,
            name="HistoryOriginalDelete",
        ).start()

    def delete_history_original_worker(self, record: Dict[str, Any], candidate: Path) -> None:
        if not force_delete(candidate):
            with self.history_lock:
                record["original_delete_pending"] = False
            self.emit_error_message(f"Could not delete the original file. It may still be in use:\n\n{candidate}")
            self.gui_queue.put(("history", None))
            return
        try:
            with self.history_lock:
                record["input_path"] = ""
                record["original_delete_pending"] = False
                record["original_file_deleted_at"] = now_iso()
                self.save_history(force=True)
            self.emit_log(f"Deleted original import-folder file from History: {candidate.name}")
        except Exception as exc:
            self.emit_error_message(f"The original file was deleted, but History could not be updated:\n\n{exc}")
        finally:
            self.gui_queue.put(("history", None))


def main() -> None:
    try:
        if os.name == "nt":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
            except Exception:
                pass
        root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
        MesterSyncApp(root)
        root.mainloop()
    except Exception:
        try:
            (app_dir() / "mestersync_startup_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()


