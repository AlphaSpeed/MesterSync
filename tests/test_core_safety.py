import hashlib
import importlib.machinery
import importlib.util
import os
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import duplicates
import file_utils
import history_utils
import media_validation
import preset_testing
import rename_utils
import safe_files
import skip_undo
import storage
import ui_performance
from task_view import TaskViewSnapshot
from config_defaults import DEFAULT_FFMPEG_ARGS


loader = importlib.machinery.SourceFileLoader("mestersync_test_app", str(APP_DIR / "MesterSync.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
app_module = importlib.util.module_from_spec(spec)
loader.exec_module(app_module)


class CoreSafetyTests(unittest.TestCase):
    def bare_app(self):
        app = app_module.MesterSyncApp.__new__(app_module.MesterSyncApp)
        app.shutdown_event = threading.Event()
        app.pause_event = threading.Event()
        app.get_config = lambda: {"copy_chunk_mb": 1}
        app.test_logs = []
        app.emit_log = app.test_logs.append
        return app

    def test_missing_source_task_record_is_rejected(self):
        self.assertIsNone(storage.record_to_task({}))
        self.assertIsNone(storage.record_to_task({"original_path": "  "}))

    def test_windows_device_names_are_made_safe(self):
        self.assertEqual(file_utils.sanitize_base_name("CON", False), "_CON")
        self.assertEqual(file_utils.sanitize_base_name("LPT1.video", False), "_LPT1.video")
        self.assertEqual(file_utils.sanitize_base_name("concert", False), "concert")

    def test_skip_undo_countdown_rounds_up_and_expires_cleanly(self):
        deadline = skip_undo.new_skip_deadline(now=100.0)
        self.assertEqual(deadline, 160.0)
        self.assertEqual(skip_undo.skip_seconds_remaining(deadline, now=100.1), 60)
        self.assertEqual(skip_undo.skip_seconds_remaining(deadline, now=159.1), 1)
        self.assertEqual(skip_undo.skip_undo_button_text(deadline, now=159.1), "Undo skip (1s)")
        self.assertIn("moving to History in 1s", skip_undo.skip_undo_detail(deadline, now=159.1))
        self.assertTrue(skip_undo.skip_is_due(deadline, now=160.0))

    def test_skipped_importfolder_files_stay_skipped_after_restart(self):
        records = [
            {"status": "skipped", "input_path": "C:/Import/keep-skipped.mov"},
            {"status": "converted_only", "input_path": "C:/Import/converted.mov"},
            {"status": "skipped", "input_path": ""},
        ]
        restored = history_utils.skipped_input_path_keys(records)
        self.assertEqual(restored, {file_utils.path_key("C:/Import/keep-skipped.mov")})

    def test_preset_menu_values_are_cached_between_row_redraws(self):
        app = self.bare_app()
        calls = []
        app.preset_menu_values_cache = None
        app.list_presets = lambda: calls.append(True) or ["Editing", "Archive"]

        first = app.preset_menu_values()
        second = app.preset_menu_values()

        self.assertEqual(first, ["Current settings", "Editing", "Archive"])
        self.assertEqual(second, first)
        self.assertEqual(calls, [True])

    def test_hidden_history_refresh_is_deferred_and_page_limit_resets(self):
        app = self.bare_app()
        app.current_tab = "dashboard"
        app.history_view_dirty = False
        app.history_display_limit = 500
        refreshed = []
        app.refresh_history_view = lambda: refreshed.append(True)

        app.request_history_view_refresh()

        self.assertTrue(app.history_view_dirty)
        self.assertEqual(app.history_display_limit, app_module.HISTORY_PAGE_SIZE)
        self.assertEqual(refreshed, [])
        app.current_tab = "history"
        app.request_history_view_refresh(reset_limit=False)
        self.assertEqual(refreshed, [True])

    def test_history_pages_are_bounded_without_mutating_records(self):
        records = [{"id": index} for index in range(80)]
        page = history_utils.history_page(records, 50)
        self.assertEqual(len(page), 50)
        self.assertEqual(page[-1]["id"], 49)
        self.assertEqual(len(records), 80)

    def test_virtual_dashboard_window_is_bounded_and_overscanned(self):
        self.assertEqual(ui_performance.virtual_row_window(20, 0, 600, 100), (0, 20))
        start, end = ui_performance.virtual_row_window(200, 5000, 600, 100)
        self.assertLess(start, 50)
        self.assertGreater(end, 56)
        self.assertLess(end - start, 25)

    def test_all_active_file_progress_styles_are_registered(self):
        app = self.bare_app()
        app.root = mock.Mock()
        style = mock.Mock()
        with mock.patch.object(app_module.ttk, "Style", return_value=style):
            app.configure_style()
        configured = {call.args[0] for call in style.configure.call_args_list if call.args}
        self.assertTrue({
            "Thin.Horizontal.TProgressbar",
            "RowImport.Horizontal.TProgressbar",
            "RowConvert.Horizontal.TProgressbar",
            "RowTransfer.Horizontal.TProgressbar",
            "RowError.Horizontal.TProgressbar",
        }.issubset(configured))
        colors = {
            call.args[0]: call.kwargs.get("background")
            for call in style.configure.call_args_list
            if call.args
        }
        self.assertEqual(colors["RowImport.Horizontal.TProgressbar"], app.ORANGE)
        self.assertEqual(colors["RowConvert.Horizontal.TProgressbar"], app.GREEN)
        self.assertEqual(colors["RowTransfer.Horizontal.TProgressbar"], app.PURPLE)
        self.assertEqual(colors["RowError.Horizontal.TProgressbar"], app.RED)

    def test_thumbnail_generation_does_not_have_a_long_artificial_delay(self):
        self.assertLessEqual(app_module.THUMBNAIL_SETTLE_DELAY_SECONDS, 2.0)

    def test_task_progress_events_are_coalesced_and_urgent_state_wins(self):
        app = self.bare_app()
        app.gui_queue = queue.Queue()
        app.task_event_lock = threading.Lock()
        app.pending_task_events = {}

        app.emit_task("video", immediate=False)
        app.emit_task("video", immediate=False)
        app.emit_task("video", immediate=True)

        self.assertEqual(app.gui_queue.qsize(), 1)
        self.assertTrue(app.pending_task_events["video"])

    def test_import_priority_button_stays_visible_while_import_is_idle(self):
        app = self.bare_app()
        cancel_button = mock.Mock()
        cancel_button.winfo_ismapped.return_value = False
        priority_button = mock.Mock()
        priority_button.winfo_ismapped.return_value = False
        app.stage_cards = {
            "import": {
                "cancel_button": cancel_button,
                "priority_button": priority_button,
            }
        }
        app.import_priority_enabled = False
        app.import_cancel_available = lambda: False

        app.update_import_cancel_button()

        priority_button.pack.assert_called_once_with(side="right", padx=(0, 8))
        priority_button.pack_forget.assert_not_called()
        priority_button.configure.assert_called_once_with(
            text="Prioritize import",
            bg=app.CARD3,
            activebackground=app.CARD3,
            fg="white",
        )

    def test_importfolder_watch_queues_existing_videos_when_conversion_is_on(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            first = folder / "one.mp4"
            second = folder / "two.mov"
            ignored = folder / "notes.txt"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            ignored.write_text("ignore", encoding="utf-8")

            app = self.bare_app()
            app.watchfolder_scan_lock = threading.Lock()
            app.task_lock = threading.RLock()
            app.task_by_path = {}
            app.skipped_path_keys = set()
            app.conversion_enabled = True
            app.get_config = lambda: {
                "input_folder": str(folder),
                "conversion_extensions": [".mp4", ".mov"],
            }
            app.create_task = mock.Mock(side_effect=["one", "two"])
            app.update_task = mock.Mock()
            app.queue_imported_batch_for_conversion = mock.Mock()

            count = app.scan_existing_input_as_detected()

            self.assertEqual(count, 2)
            created_paths = {call.args[0] for call in app.create_task.call_args_list}
            self.assertEqual(created_paths, {first, second})
            for call in app.create_task.call_args_list:
                self.assertEqual(call.kwargs, {"stage": app_module.STAGE_IMPORTED, "imported": True})
            app.queue_imported_batch_for_conversion.assert_called_once()
            self.assertCountEqual(app.queue_imported_batch_for_conversion.call_args.args[0], ["one", "two"])

    def test_progress_only_task_updates_use_throttled_rendering(self):
        app = self.bare_app()
        task = app_module.TaskState(task_id="video", original_path=Path("video.mov"), display_name="video.mov")
        app.task_lock = threading.RLock()
        app.tasks = {"video": task}
        app.session_error_ids = set()
        app.save_pending_tasks = mock.Mock()
        app.record_session_stat = mock.Mock()
        app.emit_task = mock.Mock()

        app.update_task("video", progress=25, detail="Working")
        app.emit_task.assert_called_once_with("video", immediate=False)
        app.save_pending_tasks.assert_not_called()

        app.emit_task.reset_mock()
        app.update_task("video", stage=app_module.STAGE_IMPORTING)
        app.emit_task.assert_called_once_with("video", immediate=True)
        app.save_pending_tasks.assert_called_once()

    def test_inertial_scroll_decelerates(self):
        distance, velocity = ui_performance.inertial_scroll_step(120.0)
        self.assertGreater(distance, 0)
        self.assertGreater(velocity, 0)
        self.assertLess(velocity, 120.0)

    def test_live_log_stays_bounded_during_long_sessions(self):
        self.assertEqual(ui_performance.bounded_log_count(100, 20, 1000), (0, 120))
        self.assertEqual(ui_performance.bounded_log_count(990, 25, 1000), (15, 1000))
        self.assertEqual(ui_performance.bounded_log_count(1000, 80, 1000), (80, 1000))

    def test_version_is_1_9(self):
        self.assertEqual(app_module.APP_VERSION, "1.9")

    def test_launcher_has_python_fallbacks_and_visible_failure_message(self):
        launcher = (APP_DIR / "MesterSync.vbs").read_text(encoding="utf-8-sig").lower()
        self.assertIn("pyw.exe", launcher)
        self.assertIn("pythonw.exe", launcher)
        self.assertIn("py.exe -3", launcher)
        self.assertIn("python.exe", launcher)
        self.assertIn("msgbox", launcher)

    def test_hidden_log_box_keeps_only_recent_entries(self):
        app = self.bare_app()
        app.log_entries = app_module.deque(maxlen=3)
        app.log_line_count = 0
        app.log_text = None

        app.append_log_lines(["one", "two", "three", "four"])

        self.assertEqual(len(app.log_entries), 3)
        self.assertIn("two", app.log_entries[0])
        self.assertIn("four", app.log_entries[-1])

    def test_non_blocking_notification_replaces_current_toast(self):
        app = self.bare_app()
        app.root = mock.Mock()
        app.root.after.return_value = "notification-job"
        app.notification_after_id = None
        app.notification_active = False
        app.notification_queue = app_module.deque(maxlen=6)
        app.notification_frame = mock.Mock()
        app.notification_inner = mock.Mock()
        app.notification_inner.winfo_children.return_value = []
        app.notification_label = mock.Mock()
        app.notification_var = mock.Mock()

        app.show_notification("Conversion finished", "success", duration_ms=1200)

        app.notification_var.set.assert_called_once_with("Conversion finished")
        app.notification_frame.place.assert_called_once()
        app.notification_frame.lift.assert_called_once()
        app.root.after.assert_called_once_with(1200, app.hide_notification)

    def test_notifications_queue_instead_of_overwriting_active_message(self):
        app = self.bare_app()
        app.notification_frame = mock.Mock()
        app.notification_active = True
        app.notification_queue = app_module.deque(maxlen=6)

        app.show_notification("Second message", "warning", duration_ms=2000)

        self.assertEqual(list(app.notification_queue), [("Second message", "warning", 2000)])
        app.notification_frame.place.assert_not_called()

    def test_notification_text_is_kept_compact(self):
        text = "\n".join(f"line {index}" for index in range(10))
        compact = ui_performance.compact_notification_text(text, max_chars=80, max_lines=3)
        self.assertEqual(compact.count("\n"), 2)
        self.assertTrue(compact.endswith("…"))
        self.assertNotIn("line 3", compact)

    def test_settings_categories_show_only_the_selected_section(self):
        app = self.bare_app()
        app.root = mock.Mock()
        app.root.after_idle.side_effect = lambda callback: callback()
        folders = mock.Mock()
        presets = mock.Mock()
        folders_button = mock.Mock()
        presets_button = mock.Mock()
        canvas = mock.Mock()
        canvas.winfo_exists.return_value = True
        app.settings_categories = {"folders": folders, "presets": presets}
        app.settings_category_buttons = {"folders": folders_button, "presets": presets_button}
        app.settings_canvas = canvas

        app.show_settings_category("presets")

        folders.pack_forget.assert_called_once()
        presets.pack.assert_called_once_with(fill="both", expand=True)
        canvas.yview_moveto.assert_called_once_with(0)

    def test_routine_messageboxes_have_been_removed(self):
        source = (APP_DIR / "MesterSync.pyw").read_text(encoding="utf-8")
        self.assertNotIn("messagebox.showinfo", source)
        self.assertNotIn("messagebox.showwarning", source)
        self.assertNotIn("messagebox.showerror", source)

    def test_thumbnail_source_bytes_can_be_prepared_off_ui_thread(self):
        cache = app_module.ImageCache(2)
        cache.put_source_bytes("preview.png", b"png bytes")
        self.assertTrue(cache.has_source("preview.png"))
        cache.remove("preview.png")
        self.assertFalse(cache.has_source("preview.png"))

    def test_task_view_snapshot_is_immutable_from_worker_changes(self):
        task = app_module.TaskState(
            task_id="snapshot",
            original_path=Path("source.mov"),
            display_name="source.mov",
            progress=10,
            detail="Starting",
        )
        snapshot = TaskViewSnapshot.from_task(task, priority=3, selected=True)
        task.progress = 90
        task.detail = "Almost done"
        self.assertEqual(snapshot.progress, 10)
        self.assertEqual(snapshot.detail, "Starting")
        self.assertEqual(snapshot.priority, 3)

    def test_batch_rename_uses_current_underscore_setting(self):
        renamed = rename_utils.batch_rename_bases(
            ["first clip", "second clip"],
            "Summer Concert",
            auto_underscores=True,
        )
        self.assertEqual(renamed, ["Summer_Concert_01", "Summer_Concert_02"])
        prefixed = rename_utils.batch_rename_bases(
            ["first clip"],
            "Summer ",
            auto_underscores=True,
            prefix=True,
        )
        suffixed = rename_utils.batch_rename_bases(
            ["first clip"],
            " final",
            auto_underscores=True,
            suffix=True,
        )
        self.assertEqual(prefixed, ["Summer_first_clip"])
        self.assertEqual(suffixed, ["first_clip_final"])

    def test_preset_test_keeps_only_the_new_validated_output(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            previous = folder / "previous_test.mp4"
            older = folder / "older_test.mp4"
            keep = folder / "new_test.mp4"
            unfinished = folder / ".mestersync_test_active.mp4"
            for path in (previous, older, keep, unfinished):
                path.write_bytes(path.name.encode("utf-8"))

            removed, warnings = preset_testing.delete_previous_preset_tests(folder, keep)

            self.assertEqual(removed, 2)
            self.assertEqual(warnings, [])
            self.assertTrue(keep.exists())
            self.assertTrue(unfinished.exists())
            self.assertFalse(previous.exists())
            self.assertFalse(older.exists())

    def test_preset_test_sample_is_centered_and_limited_to_fifteen_seconds(self):
        self.assertEqual(preset_testing.preset_sample_window(60), (22.5, 15.0))
        self.assertEqual(preset_testing.preset_sample_window(10), (0.0, 10.0))

    def test_default_audio_mapping_allows_silent_video(self):
        map_index = DEFAULT_FFMPEG_ARGS.index("0:a:0?")
        self.assertEqual(DEFAULT_FFMPEG_ARGS[map_index - 1], "-map")

    def test_only_exact_legacy_default_audio_mapping_is_migrated(self):
        legacy_args = DEFAULT_FFMPEG_ARGS[:]
        legacy_args[legacy_args.index("0:a:0?")] = "0:a:0"
        migrated = storage.normalize_config_data({"ffmpeg_args": legacy_args}, DEFAULT_FFMPEG_ARGS)
        self.assertEqual(migrated["ffmpeg_args"], DEFAULT_FFMPEG_ARGS)

        customized = legacy_args + ["-metadata", "comment=custom"]
        preserved = storage.normalize_config_data({"ffmpeg_args": customized}, DEFAULT_FFMPEG_ARGS)
        self.assertEqual(preserved["ffmpeg_args"], customized)

    def test_same_name_and_size_is_not_enough_to_mark_duplicate(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source = folder / "camera_01.mov"
            source.write_bytes(b"A" * 4096)
            other_digest = hashlib.sha256(b"B" * 4096).hexdigest()
            database = {
                other_digest: {
                    "original_name": source.name,
                    "original_size": source.stat().st_size,
                    "final_path": "moved elsewhere",
                }
            }
            reason = duplicates.exact_duplicate_reason(
                source,
                {"check_existing_in_input": False},
                database,
                threading.Event(),
            )
            self.assertEqual(reason, "")

            exact_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            database[exact_digest] = {
                "original_name": source.name,
                "original_size": source.stat().st_size,
                "final_path": "remembered NAS file",
            }
            reason = duplicates.exact_duplicate_reason(
                source,
                {"check_existing_in_input": False},
                database,
                threading.Event(),
            )
            self.assertIn("Checksum already imported", reason)

    def test_directly_watched_source_gets_a_durable_duplicate_fingerprint(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "camera_01.mov"
            source.write_bytes(b"video-data")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            task = app_module.TaskState(
                task_id="watched",
                original_path=source,
                local_input_path=source,
                display_name=source.name,
            )
            app = self.bare_app()
            app.task_lock = threading.RLock()
            app.tasks = {task.task_id: task}
            app.get_config = lambda: {"enable_duplicate_detection": True}
            app.update_task = mock.Mock()
            app.emit_progress = mock.Mock()
            app.remember_original_checksum = mock.Mock()

            with mock.patch.object(app_module, "sha256_file", return_value=digest):
                remembered = app.ensure_task_source_fingerprint(task.task_id, source)

            self.assertTrue(remembered)
            self.assertEqual(task.original_checksum, digest)
            app.remember_original_checksum.assert_called_once_with(
                digest,
                source.name,
                source.stat().st_size,
                source,
            )

    def test_exact_duplicate_from_connected_drive_is_skipped_without_copying(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "camera_01.mov"
            source.write_bytes(b"already imported")
            task = app_module.TaskState(task_id="disk", original_path=source, display_name=source.name)
            app = self.bare_app()
            app.task_lock = threading.RLock()
            app.tasks = {task.task_id: task}
            app.import_condition = threading.Condition()
            app.import_queue = app_module.deque([task.task_id])
            app.queued_import_ids = {task.task_id}
            app.import_job_requires_enabled = {task.task_id: True}
            app.import_job_batch = {task.task_id: "disk-batch"}
            app.import_batches = {"disk-batch": {"disk_batch": True}}
            app.import_enabled = True
            app.current_import_id = None
            app.update_task = mock.Mock()
            app.set_import_progress_snapshot = mock.Mock()
            app.clear_import_progress_snapshot = mock.Mock()
            app.duplicate_reason_for = mock.Mock(return_value="Checksum already imported")
            app.discard_detected_task = mock.Mock()
            app.import_single_file = mock.Mock()
            app.emit_status = mock.Mock()

            def finish_once(*_args, **_kwargs):
                app.shutdown_event.set()

            app.complete_import_job = mock.Mock(side_effect=finish_once)
            app.import_worker()

            app.discard_detected_task.assert_called_once_with(task.task_id)
            app.import_single_file.assert_not_called()
            self.assertTrue(any("skipped exact duplicate from disk" in line for line in app.test_logs))

    def test_atomic_json_write_keeps_previous_backup(self):
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "state.json"
            storage.write_json_file(path, {"version": 1})
            storage.write_json_file(path, {"version": 2})
            self.assertEqual(storage.read_json_file(path, {}), {"version": 2})
            self.assertEqual(storage.read_json_file(path.with_suffix(".json.bak"), {}), {"version": 1})

    def test_managed_paths_move_with_install_but_media_path_does_not(self):
        old_thumbnail = Path("C:/Users/example/Desktop/MesterSync/data/.mestersync_thumbnails/task.png")
        migrated = storage.migrate_record_thumbnail_paths(
            {
                "thumbnail_path": str(old_thumbnail),
                "thumbnail_preview_paths": [str(old_thumbnail.with_name("task_preview_1.png"))],
                "path": "C:/Videos/final.mp4",
            }
        )
        self.assertEqual(Path(migrated["thumbnail_path"]), storage.thumbnail_dir() / "task.png")
        self.assertEqual(
            Path(migrated["thumbnail_preview_paths"][0]),
            storage.thumbnail_dir() / "task_preview_1.png",
        )
        self.assertEqual(migrated["path"], "C:/Videos/final.mp4")

        with tempfile.TemporaryDirectory() as value:
            old_presets = Path(value) / "presets"
            old_presets.mkdir()
            config = storage.normalize_config_data(
                {"preset_folder": str(old_presets), "ffmpeg_args": []},
                DEFAULT_FFMPEG_ARGS,
            )
            self.assertEqual(Path(config["preset_folder"]), storage.preset_dir())

    def test_safe_final_name_checks_output_and_nas_locations(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            output = folder / "output"
            nas = folder / "nas"
            output.mkdir()
            nas.mkdir()
            (output / "recording.mp4").write_bytes(b"output")
            (nas / "recording_1.mp4").write_bytes(b"nas")

            app = self.bare_app()
            app.task_lock = threading.RLock()
            app.final_name_snapshot_lock = threading.Lock()
            app.final_name_snapshot = []
            app.final_name_snapshot_at = 0.0
            app.final_name_snapshot_refreshing = False
            app.reserved_output_paths = set()
            app.reserved_nas_paths = set()
            app.get_config = lambda: {
                "auto_underscore_renames": False,
                "output_folder": str(output),
                "nas_folder": str(nas),
            }
            task = app_module.TaskState(
                task_id="safe-name",
                original_path=folder / "source.mov",
                display_name="recording.mov",
                rename_base="recording",
                output_ext=".mp4",
            )

            self.assertEqual(app.safe_final_name_now(task, fresh=True), "recording_2.mp4")

    def test_history_save_failure_restores_pending_task_durably(self):
        app = self.bare_app()
        app.task_lock = threading.RLock()
        app.history_lock = threading.RLock()
        app.queue_condition = threading.Condition()
        app.transfer_condition = threading.Condition()
        app.tasks = {}
        app.task_by_path = {}
        app.force_conversion_ids = set()
        app.queued_conversion_ids = set()
        app.conversion_queue = app_module.deque()
        app.queued_transfer_ids = set()
        app.transfer_queue = app_module.deque()
        app.history_records = []
        app.gui_queue = queue.Queue()
        app.remove_import_job = lambda *_args, **_kwargs: None
        app.emit_task = lambda *_args: None
        pending_snapshots = []
        app.save_pending_tasks = lambda force=False: pending_snapshots.append(set(app.tasks))

        def fail_history_save(force=False):
            raise OSError("history unavailable")

        app.save_history = fail_history_save
        task = app_module.TaskState(
            task_id="archive-recovery",
            original_path=Path("source.mov"),
            display_name="source.mov",
        )
        app.tasks[task.task_id] = task
        app.task_by_path[file_utils.path_key(task.original_path)] = task.task_id

        with self.assertRaises(OSError):
            app.archive_task(task.task_id, status="converted_only")

        self.assertIs(app.tasks[task.task_id], task)
        self.assertEqual(task.stage, app_module.STAGE_ERROR)
        self.assertEqual(pending_snapshots, [set(), {task.task_id}])

    def test_skip_before_import_redraws_immediately_and_stays_for_history(self):
        app = self.bare_app()
        app.task_lock = threading.RLock()
        app.queue_condition = threading.Condition()
        app.transfer_condition = threading.Condition()
        app.tasks = {}
        app.skipped_path_keys = set()
        app.conversion_queue = app_module.deque()
        app.queued_conversion_ids = set()
        app.force_conversion_ids = set()
        app.transfer_queue = app_module.deque()
        app.queued_transfer_ids = set()
        app.current_import_id = None
        app.current_conversion_id = None
        app.current_process_lock = threading.Lock()
        app.row_widgets = {"not-imported": {}}
        rendered = []
        emitted = []
        app.render_task = rendered.append
        app.emit_task = emitted.append
        app.remove_import_job = lambda *_args, **_kwargs: None
        app.log = lambda *_args: None
        task = app_module.TaskState(
            task_id="not-imported",
            original_path=Path("camera-card.mov"),
            display_name="camera-card.mov",
        )
        app.tasks[task.task_id] = task

        app.skip_task(task.task_id)

        self.assertIs(app.tasks[task.task_id], task)
        self.assertTrue(task.skipped)
        self.assertEqual(task.stage, app_module.STAGE_SKIPPED)
        self.assertIsNotNone(task.skip_archive_due)
        self.assertEqual(rendered, [task.task_id])
        self.assertEqual(emitted, [task.task_id])

    def test_history_original_delete_is_limited_to_recorded_input_folder(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            input_folder = folder / "input"
            outside_folder = folder / "outside"
            input_folder.mkdir()
            outside_folder.mkdir()
            original = input_folder / "large.mov"
            outside = outside_folder / "keep.mov"
            original.write_bytes(b"large original")
            outside.write_bytes(b"keep")
            app = self.bare_app()
            app.history_lock = threading.RLock()
            app.get_config = lambda: {"input_folder": str(input_folder)}
            record = {
                "status": "skipped",
                "input_path": str(original),
                "input_folder": str(input_folder),
            }
            app.history_records = [record]
            app.save_history = lambda force=False: None
            app.refresh_history_view = lambda: None
            app.log = lambda *_args: None

            self.assertEqual(app.history_original_file(record), original.resolve())
            unsafe = dict(record, input_path=str(outside))
            self.assertIsNone(app.history_original_file(unsafe))
            app.emit_log = lambda *_args: None
            app.emit_error_message = lambda *_args: None
            app.gui_queue = queue.Queue()
            app.delete_history_original_worker(record, original.resolve())

            self.assertFalse(original.exists())
            self.assertTrue(outside.exists())
            self.assertEqual(record["input_path"], "")
            self.assertTrue(record.get("original_file_deleted_at"))

    def test_convert_in_place_keeps_name_still_being_edited(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "source.mov"
            source.write_bytes(b"video")
            app = self.bare_app()
            task = app_module.TaskState(
                task_id="in-place-name",
                original_path=source,
                display_name=source.name,
                rename_base=source.stem,
            )
            app.tasks = {task.task_id: task}
            app.selected_ids = [task.task_id]
            app.row_widgets = {
                task.task_id: {
                    "dirty": True,
                    "rename_var": mock.Mock(get=lambda: "new rendered name"),
                }
            }
            app.current_conversion_id = None
            app.queue_condition = threading.Condition()
            app.validate_ffmpeg_for_conversion = lambda: source
            app.find_existing_task_media = lambda _task: source
            app.default_task_preset_snapshot = lambda: ("Current settings", list(DEFAULT_FFMPEG_ARGS))
            app.get_config = lambda: {"auto_underscore_renames": False}
            app.ensure_workers_running = lambda: None
            app.enqueue_conversion = lambda *_args, **_kwargs: None
            app.emit_status = lambda: None
            app.log = lambda *_args: None

            def update_task(task_id, **changes):
                for key, item in changes.items():
                    setattr(app.tasks[task_id], key, item)

            app.update_task = update_task
            with mock.patch.object(app_module, "validate_writable_folder", return_value=None):
                app.convert_in_place_selected()

            self.assertEqual(task.rename_base, "new rendered name")
            self.assertTrue(task.convert_in_place)

    def test_copy_never_overwrites_and_rejects_source_changes(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source = folder / "source.bin"
            source.write_bytes(os.urandom(2 * 1024 * 1024))
            app = self.bare_app()

            copied_destination = folder / "copied.bin"
            copied_digests = []
            self.assertTrue(
                app.copy_file_safely(
                    source,
                    copied_destination,
                    threading.Event(),
                    lambda *_: None,
                    copied_digests.append,
                )
            )
            self.assertEqual(copied_destination.read_bytes(), source.read_bytes())
            self.assertEqual(copied_digests, [hashlib.sha256(source.read_bytes()).hexdigest()])

            destination = folder / "destination.bin"
            destination.write_bytes(b"existing")
            self.assertFalse(app.copy_file_safely(source, destination, threading.Event(), lambda *_: None))
            self.assertEqual(destination.read_bytes(), b"existing")

            raced_destination = folder / "raced.bin"
            destination_created = False

            def create_destination_during_copy(*_args):
                nonlocal destination_created
                if not destination_created:
                    raced_destination.write_bytes(b"appeared during copy")
                    destination_created = True

            self.assertFalse(
                app.copy_file_safely(source, raced_destination, threading.Event(), create_destination_during_copy)
            )
            self.assertEqual(raced_destination.read_bytes(), b"appeared during copy")
            self.assertFalse(list(folder.glob("*.part")))

            changed_destination = folder / "changed.bin"
            callbacks = 0

            def change_source_timestamp(*_args):
                nonlocal callbacks
                callbacks += 1
                if callbacks == 1:
                    stat = source.stat()
                    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

            self.assertFalse(
                app.copy_file_safely(source, changed_destination, threading.Event(), change_source_timestamp)
            )
            self.assertFalse(changed_destination.exists())
            self.assertFalse(list(folder.glob("*.part")))
            self.assertTrue(any("source changed" in line.lower() for line in app.test_logs))

    def test_promote_and_move_helpers_never_overwrite(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            logs = []

            temp = folder / "finished.part"
            destination = folder / "final.mp4"
            temp.write_bytes(b"new")
            destination.write_bytes(b"existing")
            self.assertFalse(safe_files.promote_temp_no_overwrite(temp, destination, logs.append))
            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertEqual(temp.read_bytes(), b"new")

            destination.unlink()
            self.assertTrue(safe_files.promote_temp_no_overwrite(temp, destination, logs.append))
            self.assertEqual(destination.read_bytes(), b"new")
            self.assertFalse(temp.exists())

            source = folder / "source.mp4"
            source.write_bytes(b"source")
            occupied = folder / "occupied.mp4"
            occupied.write_bytes(b"occupied")
            self.assertFalse(safe_files.move_file_no_overwrite(source, occupied, logs.append))
            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(occupied.read_bytes(), b"occupied")

            moved = folder / "moved.mp4"
            self.assertTrue(safe_files.move_file_no_overwrite(source, moved, logs.append))
            self.assertFalse(source.exists())
            self.assertEqual(moved.read_bytes(), b"source")

    def test_media_validation_rejects_incomplete_conversions(self):
        source_info = {
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio"},
            ]
        }
        valid_output = {
            "format": {"duration": "60.0"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio"},
            ],
        }
        self.assertIsNone(media_validation.validate_media_info(valid_output, source_info, [], 60.0))

        no_video = {"format": {"duration": "60.0"}, "streams": [{"codec_type": "audio"}]}
        self.assertIn("no video stream", media_validation.validate_media_info(no_video, source_info, [], 60.0))

        truncated = dict(valid_output, format={"duration": "40.0"})
        self.assertIn("duration differs", media_validation.validate_media_info(truncated, source_info, [], 60.0))

        silent = {
            "format": {"duration": "60.0"},
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
        }
        self.assertIn("contains no audio", media_validation.validate_media_info(silent, source_info, [], 60.0))
        self.assertIsNone(media_validation.validate_media_info(silent, source_info, ["-an"], 60.0))

    def test_media_validation_rejects_missing_output_before_ffprobe(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            result = media_validation.validate_converted_output(
                folder / "missing.mp4",
                folder / "source.mov",
                folder / "ffmpeg.exe",
                [],
                None,
            )
            self.assertEqual(result, "Converted output is missing or empty.")

    def test_encoder_query_returns_without_waiting_for_ffmpeg(self):
        with tempfile.TemporaryDirectory() as value:
            ffmpeg = Path(value) / "ffmpeg.exe"
            ffmpeg.write_bytes(b"placeholder")
            app = self.bare_app()
            app.encoder_cache = {}
            app.encoder_queries_pending = set()
            app.gui_queue = queue.Queue()
            started = threading.Event()
            release = threading.Event()

            def slow_query(_ffmpeg):
                started.set()
                release.wait(timeout=2)
                return {"libx264"}

            with mock.patch.object(app_module, "query_video_encoders", side_effect=slow_query):
                result = app.available_video_encoders(ffmpeg)
                self.assertIsNone(result)
                self.assertTrue(started.wait(timeout=1))
                self.assertEqual(app.available_video_encoders(ffmpeg), None)
                release.set()
                event, payload = app.gui_queue.get(timeout=2)

            self.assertEqual(event, "encoder_cache_ready")
            self.assertEqual(payload[1], {"libx264"})

    def test_health_check_returns_while_diagnostics_run(self):
        app = self.bare_app()
        app.health_check_running = False
        app.gui_queue = queue.Queue()
        app.set_config_from_ui = lambda: None
        app.get_config = lambda: {"ffmpeg_path": "ffmpeg.exe"}
        app.log = app.test_logs.append
        started = threading.Event()
        release = threading.Event()

        def slow_diagnostics(_cfg):
            started.set()
            release.wait(timeout=2)
            return ["OK diagnostics"], {"libx264"}

        with mock.patch.object(app_module, "run_diagnostics", side_effect=slow_diagnostics):
            app.run_health_check()
            self.assertTrue(app.health_check_running)
            self.assertTrue(started.wait(timeout=1))
            release.set()
            event, payload = app.gui_queue.get(timeout=2)

        self.assertEqual(event, "health_check_complete")
        self.assertEqual(payload["text"], "OK diagnostics")


if __name__ == "__main__":
    unittest.main()
