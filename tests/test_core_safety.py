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
os.environ.setdefault("MESTERSYNC_DATA_DIR", str(Path(tempfile.gettempdir()) / f"MesterSyncTests-{os.getpid()}"))
sys.path.insert(0, str(APP_DIR))

import duplicates
import duplicate_ledger
import file_utils
import history_utils
import importer
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

    def test_camera_disk_metadata_sidecars_are_never_imported(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            real = root / "HyperDeck_0001.mp4"
            sidecar = root / "._HyperDeck_0001.mp4"
            private = root / ".BlamOS-Private-Items"
            private.mkdir()
            hidden_video = private / "HyperDeck_hidden.mp4"
            real.write_bytes(b"real recording")
            sidecar.write_bytes(b"metadata")
            hidden_video.write_bytes(b"private metadata")

            found, _scanned = importer.scan_drive_importable_videos(
                root,
                {
                    "exclude_files": [],
                    "exclude_folders": [],
                    "import_extensions": [".mp4"],
                },
                lambda: False,
                lambda *_args: None,
            )
            manually_added = importer.collect_import_candidates(
                [root],
                [".mp4"],
                lambda: False,
            )

            self.assertEqual(found, [real])
            self.assertEqual(manually_added, [real])

    def test_reinserted_drive_only_returns_files_not_previously_imported_from_it(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            drive = root / "camera-disk"
            imports = root / "imports"
            drive.mkdir()
            imports.mkdir()
            old_source = drive / "HyperDeck_0001.mp4"
            new_source = drive / "HyperDeck_0002.mp4"
            imported_copy = imports / old_source.name
            old_source.write_bytes(os.urandom(128 * 1024))
            imported_copy.write_bytes(old_source.read_bytes())
            new_source.write_bytes(os.urandom(128 * 1024))
            digest = hashlib.sha256(old_source.read_bytes()).hexdigest()

            app = self.bare_app()
            app.checksum_lock = threading.RLock()
            app.checksum_db = {}
            app.import_cancel_requested = threading.Event()
            app.task_lock = threading.RLock()
            app.tasks = {}
            app.task_by_path = {}
            app.get_config = lambda: {"checksum_database_path": str(root / "checksums.json")}

            saved = app.remember_original_checksum(
                digest,
                old_source.name,
                old_source.stat().st_size,
                imported_copy,
                source_path=old_source,
                source_root=drive,
                source_id="volume:12345678",
            )
            fresh, active_count, remembered_count = app.filter_new_drive_sources(
                drive,
                "volume:12345678",
                [old_source, new_source],
            )

            self.assertTrue(saved)
            self.assertEqual(fresh, [new_source])
            self.assertEqual(active_count, 0)
            self.assertEqual(remembered_count, 1)
            self.assertEqual(
                app.checksum_db[digest]["drive_sources"][0]["relative_path"],
                "hyperdeck_0001.mp4",
            )

    def test_changed_file_at_a_remembered_drive_path_is_checked_as_new(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            drive = root / "camera-disk"
            drive.mkdir()
            source = drive / "HyperDeck_0001.mp4"
            imported_copy = root / "imported.mp4"
            source.write_bytes(b"first recording")
            imported_copy.write_bytes(source.read_bytes())
            digest = hashlib.sha256(source.read_bytes()).hexdigest()

            app = self.bare_app()
            app.checksum_lock = threading.RLock()
            app.checksum_db = {}
            app.import_cancel_requested = threading.Event()
            app.task_lock = threading.RLock()
            app.tasks = {}
            app.task_by_path = {}
            app.get_config = lambda: {"checksum_database_path": str(root / "checksums.json")}
            self.assertTrue(app.remember_original_checksum(
                digest,
                source.name,
                source.stat().st_size,
                imported_copy,
                source_path=source,
                source_root=drive,
                source_id="volume:12345678",
            ))

            source.write_bytes(b"second and different recording")
            fresh, _active_count, remembered_count = app.filter_new_drive_sources(
                drive,
                "volume:12345678",
                [source],
            )

            self.assertEqual(fresh, [source])
            self.assertEqual(remembered_count, 0)

    def test_drive_rescan_never_requeues_a_source_with_an_active_task(self):
        source = Path("E:/HyperDeck_0013.mp4")
        task = app_module.TaskState(
            task_id="currently-converting",
            original_path=source,
            display_name=source.name,
            stage=app_module.STAGE_CONVERTING,
        )
        app = self.bare_app()
        app.task_lock = threading.RLock()
        app.tasks = {task.task_id: task}
        app.task_by_path = {file_utils.path_key(source): task.task_id}
        app.drive_source_was_imported = mock.Mock(return_value=False)

        fresh, active_count, remembered_count = app.filter_new_drive_sources(
            Path("E:/"),
            "volume:12345678",
            [source],
        )

        self.assertEqual(fresh, [])
        self.assertEqual(active_count, 1)
        self.assertEqual(remembered_count, 0)
        app.drive_source_was_imported.assert_not_called()
        self.assertEqual(task.stage, app_module.STAGE_CONVERTING)

    def test_one_missed_windows_drive_scan_does_not_trigger_a_fresh_import(self):
        app = self.bare_app()
        app.processed_drive_ids = {"volume:12345678"}
        app.drive_missing_scan_counts = {}

        app.update_connected_drive_memory([])
        self.assertIn("volume:12345678", app.processed_drive_ids)
        app.update_connected_drive_memory(["volume:12345678"])
        self.assertIn("volume:12345678", app.processed_drive_ids)
        self.assertEqual(app.drive_missing_scan_counts, {})

        app.update_connected_drive_memory([])
        app.update_connected_drive_memory([])
        self.assertNotIn("volume:12345678", app.processed_drive_ids)

    def test_shared_nas_ledger_blocks_an_identical_original_on_another_computer(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            nas = root / "nas"
            nas.mkdir()
            final = nas / "Concert_Master.mp4"
            final.write_bytes(b"converted output")
            source = root / "HyperDeck_0001.mp4"
            source.write_bytes(os.urandom(512 * 1024))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            first_pc_records = {
                digest: {
                    "original_name": source.name,
                    "original_size": source.stat().st_size,
                    "quick_fingerprint": duplicates.quick_fingerprint(source),
                    "final_path": str(final),
                }
            }

            exported = duplicate_ledger.sync_shared_checksum_ledger(nas, first_pc_records, [final])
            renamed_source = root / "camera_copy_with_new_name.mp4"
            renamed_source.write_bytes(source.read_bytes())
            cfg = {
                "nas_folder": str(nas),
                "input_folder": "",
                "checksum_database_path": str(root / "second-pc-checksums.json"),
            }
            second_pc = self.bare_app()
            second_pc.config_lock = threading.RLock()
            second_pc.checksum_lock = threading.RLock()
            second_pc.history_lock = threading.RLock()
            second_pc.checksum_db = {}
            second_pc.history_records = []
            second_pc.import_cancel_requested = threading.Event()
            second_pc.get_config = lambda: cfg
            reason = second_pc.duplicate_reason_for(
                renamed_source,
                cfg,
            )

            self.assertTrue(exported.initialized)
            self.assertEqual(exported.error, "")
            self.assertIn(digest, second_pc.checksum_db)
            self.assertIn("Checksum already imported", reason)

    def test_shared_nas_ledger_does_not_claim_coverage_for_untracked_archive(self):
        with tempfile.TemporaryDirectory() as value:
            nas = Path(value) / "nas"
            nas.mkdir()
            (nas / "old_concert.mp4").write_bytes(b"converted before shared history")

            result = duplicate_ledger.sync_shared_checksum_ledger(nas, {})

            self.assertFalse(result.initialized)
            self.assertIn("already contains media", result.error)
            self.assertFalse((nas / duplicate_ledger.LEDGER_DIRNAME).exists())

    def test_shared_nas_ledger_keeps_all_forced_import_locations(self):
        with tempfile.TemporaryDirectory() as value:
            nas = Path(value) / "nas"
            nas.mkdir()
            first = nas / "concert.mp4"
            first.write_bytes(b"first conversion")
            digest = "a" * 64
            initial = duplicate_ledger.sync_shared_checksum_ledger(
                nas,
                {digest: {"original_size": 123, "final_path": str(first)}},
                [first],
            )
            second = nas / "concert_1.mp4"
            second.write_bytes(b"intentional extra conversion")
            updated = duplicate_ledger.publish_shared_checksum_record(
                nas,
                digest,
                {"original_size": 123, "final_path": str(second)},
            )

            self.assertTrue(initial.initialized)
            self.assertEqual(updated.error, "")
            self.assertEqual(set(updated.records[digest]["final_paths"]), {str(first), str(second)})

    def test_incomplete_old_nas_still_learns_and_blocks_new_exact_records(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            nas = root / "nas"
            nas.mkdir()
            (nas / "untracked_old_concert.mp4").write_bytes(b"old archive media")
            final = nas / "new_concert.mp4"
            final.write_bytes(b"new converted media")
            source = root / "camera_original.mov"
            source.write_bytes(os.urandom(64 * 1024))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            record = {
                "original_size": source.stat().st_size,
                "quick_fingerprint": duplicates.quick_fingerprint(source),
                "final_path": str(final),
            }

            published = duplicate_ledger.publish_shared_checksum_record(nas, digest, record)
            refreshed = duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            reason = duplicates.exact_duplicate_reason(
                source,
                {"input_folder": ""},
                refreshed.records,
                threading.Event(),
            )

            self.assertFalse(published.initialized)
            self.assertEqual(published.exported, 1)
            self.assertIn("already contains media", refreshed.error)
            self.assertIn("Checksum already imported", reason)

    def test_shared_checksum_claim_detects_a_concurrent_second_import(self):
        with tempfile.TemporaryDirectory() as value:
            nas = Path(value) / "nas"
            nas.mkdir()
            initialized = duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            digest = "c" * 64
            record = {"original_size": 456, "original_name": "camera.mp4"}

            first = duplicate_ledger.publish_shared_checksum_record(nas, digest, record)
            second = duplicate_ledger.publish_shared_checksum_record(nas, digest, record)

            self.assertTrue(initialized.initialized)
            self.assertFalse(first.already_present)
            self.assertTrue(second.already_present)

    def test_initialized_ledger_with_missing_record_folder_fails_closed(self):
        with tempfile.TemporaryDirectory() as value:
            nas = Path(value) / "nas"
            nas.mkdir()
            initialized = duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            records_folder = nas / duplicate_ledger.LEDGER_DIRNAME / duplicate_ledger.LEDGER_RECORDS_DIRNAME
            records_folder.rmdir()

            refreshed = duplicate_ledger.sync_shared_checksum_ledger(nas, {})

            self.assertTrue(initialized.initialized)
            self.assertIn("missing or unavailable", refreshed.error)

    def test_unavailable_shared_nas_pauses_unknown_import_for_review(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "new_camera_file.mov"
            source.write_bytes(b"new recording")
            app = self.bare_app()
            app.config_lock = threading.RLock()
            app.checksum_lock = threading.RLock()
            app.history_lock = threading.RLock()
            app.checksum_db = {}
            app.history_records = []
            app.import_cancel_requested = threading.Event()

            reason = app.duplicate_reason_for(
                source,
                {
                    "nas_folder": str(Path(value) / "offline-nas"),
                    "input_folder": "",
                    "checksum_database_path": str(Path(value) / "checksums.json"),
                },
            )

            self.assertIn("NAS folder is unavailable", reason)
            self.assertIn("Import paused", reason)

    def test_local_exact_match_still_wins_when_shared_nas_is_unavailable(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "known.mov"
            source.write_bytes(os.urandom(128 * 1024))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            app = self.bare_app()
            app.config_lock = threading.RLock()
            app.checksum_lock = threading.RLock()
            app.history_lock = threading.RLock()
            app.checksum_db = {
                digest: {
                    "original_size": source.stat().st_size,
                    "quick_fingerprint": duplicates.quick_fingerprint(source),
                    "final_path": "remembered NAS file",
                }
            }
            app.history_records = []
            app.import_cancel_requested = threading.Event()

            reason = app.duplicate_reason_for(
                source,
                {
                    "nas_folder": str(Path(value) / "offline-nas"),
                    "input_folder": "",
                    "checksum_database_path": str(Path(value) / "checksums.json"),
                },
            )

            self.assertIn("Checksum already imported", reason)

    def test_unreadable_possible_duplicate_fails_closed(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "camera.mov"
            source.write_bytes(b"possible duplicate")
            database = {
                "b" * 64: {
                    "original_size": source.stat().st_size,
                    "final_path": "remembered file",
                }
            }
            with mock.patch.object(duplicates, "cached_sha256_file", return_value=None):
                reason = duplicates.exact_duplicate_reason(
                    source,
                    {"input_folder": ""},
                    database,
                    threading.Event(),
                )

            self.assertIn("Import paused", reason)

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
        cancel_button.winfo_manager.return_value = ""
        priority_button = mock.Mock()
        priority_button.winfo_ismapped.return_value = False
        priority_button.winfo_manager.return_value = ""
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
            app.duplicate_reason_for = mock.Mock(return_value="")

            count = app.scan_existing_input_as_detected()

            self.assertEqual(count, 2)
            created_paths = {call.args[0] for call in app.create_task.call_args_list}
            self.assertEqual(created_paths, {first, second})
            for call in app.create_task.call_args_list:
                self.assertEqual(call.kwargs, {"stage": app_module.STAGE_CHECKING, "imported": True, "new_only": True})
            app.queue_imported_batch_for_conversion.assert_called_once()
            self.assertCountEqual(app.queue_imported_batch_for_conversion.call_args.args[0], ["one", "two"])

    def test_importfolder_exact_duplicate_waits_for_import_anyway(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source = folder / "already_imported.mp4"
            source.write_bytes(b"known original")
            app = self.bare_app()
            app.watchfolder_scan_lock = threading.Lock()
            app.task_lock = threading.RLock()
            app.task_by_path = {}
            app.skipped_path_keys = set()
            app.conversion_enabled = True
            app.get_config = lambda: {
                "input_folder": str(folder),
                "conversion_extensions": [".mp4"],
            }
            app.create_task = mock.Mock(return_value="duplicate")
            app.update_task = mock.Mock()
            app.queue_imported_batch_for_conversion = mock.Mock()
            app.duplicate_reason_for = mock.Mock(return_value="Checksum already imported")

            count = app.scan_existing_input_as_detected()

            self.assertEqual(count, 1)
            app.queue_imported_batch_for_conversion.assert_not_called()
            app.update_task.assert_any_call(
                "duplicate",
                stage=app_module.STAGE_DUPLICATE_REVIEW,
                duplicate=True,
                duplicate_reason="Checksum already imported",
                detail="Checksum already imported",
                progress=0,
            )

    def test_importfolder_watcher_ignores_a_completed_file_during_safe_cleanup(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source = folder / "HyperDeck_0019.mp4"
            source.write_bytes(b"verified working copy still being checksummed before deletion")
            app = self.bare_app()
            app.watchfolder_scan_lock = threading.Lock()
            app.task_lock = threading.RLock()
            app.task_by_path = {}
            app.cleanup_path_keys = {file_utils.path_key(source)}
            app.skipped_path_keys = set()
            app.conversion_enabled = True
            app.get_config = lambda: {
                "input_folder": str(folder),
                "output_folder": str(folder.parent / "output"),
                "nas_folder": str(folder.parent / "nas"),
                "conversion_extensions": [".mp4"],
            }
            app.create_task = mock.Mock()
            app.queue_imported_batch_for_conversion = mock.Mock()

            count = app.scan_existing_input_as_detected()

            self.assertEqual(count, 0)
            app.create_task.assert_not_called()
            app.queue_imported_batch_for_conversion.assert_not_called()

    def test_failed_completed_cleanup_stays_quarantined_for_the_session(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "HyperDeck_0019.mp4"
            source.write_bytes(b"keep me safely")
            app = self.bare_app()
            app.task_lock = threading.RLock()
            app.cleanup_path_keys = set()

            app.hold_paths_during_completed_cleanup(source)
            app.release_cleaned_paths(source)
            self.assertIn(file_utils.path_key(source), app.cleanup_path_keys)

            source.unlink()
            app.release_cleaned_paths(source)
            self.assertNotIn(file_utils.path_key(source), app.cleanup_path_keys)

    def test_import_anyway_converts_existing_importfolder_file_without_copying_it_again(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "intentional_duplicate.mp4"
            source.write_bytes(b"intentional duplicate")
            task = app_module.TaskState(
                task_id="watch-override",
                original_path=source,
                local_input_path=source,
                display_name=source.name,
                stage=app_module.STAGE_DUPLICATE_REVIEW,
                duplicate=True,
            )
            app = self.bare_app()
            app.tasks = {task.task_id: task}
            app.conversion_enabled = True
            app.ensure_workers_running = mock.Mock()
            app.enqueue_conversion = mock.Mock()
            app.requeue_duplicate_import = mock.Mock()
            app.show_notification = mock.Mock()

            def update_task(task_id, **changes):
                for key, value in changes.items():
                    setattr(app.tasks[task_id], key, value)

            app.update_task = update_task
            app.import_duplicate_task(task.task_id)

            app.requeue_duplicate_import.assert_not_called()
            app.enqueue_conversion.assert_called_once_with(task.task_id)
            self.assertFalse(task.duplicate)
            self.assertEqual(task.stage, app_module.STAGE_IMPORTED)

    def test_import_anyway_releases_a_verified_post_copy_batch_without_copying_again(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "disk.mp4"
            local = Path(value) / "imports" / "disk.mp4"
            local.parent.mkdir()
            source.write_bytes(b"recording disk original")
            local.write_bytes(source.read_bytes())
            task = app_module.TaskState(
                task_id="post-copy-review",
                original_path=source,
                local_input_path=local,
                display_name=source.name,
                stage=app_module.STAGE_DUPLICATE_REVIEW,
                duplicate=True,
                original_checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            app = self.bare_app()
            app.tasks = {task.task_id: task}
            app.import_job_batch = {task.task_id: "batch"}
            app.conversion_enabled = True
            app.ensure_workers_running = mock.Mock()
            app.complete_import_job = mock.Mock()
            app.enqueue_conversion = mock.Mock()
            app.requeue_duplicate_import = mock.Mock()
            app.show_notification = mock.Mock()

            def update_task(task_id, **changes):
                for key, value in changes.items():
                    setattr(app.tasks[task_id], key, value)

            app.update_task = update_task
            app.import_duplicate_task(task.task_id)

            app.complete_import_job.assert_called_once_with(task.task_id, success=True, blocked=False)
            app.requeue_duplicate_import.assert_not_called()
            app.enqueue_conversion.assert_not_called()

    def test_watchfolder_excludes_nested_output_and_nas_folders(self):
        with tempfile.TemporaryDirectory() as value:
            input_folder = Path(value) / "input"
            output_folder = input_folder / "output"
            nas_folder = input_folder / "nas"
            input_folder.mkdir()
            output_folder.mkdir()
            nas_folder.mkdir()
            source = input_folder / "source.mp4"
            output = output_folder / "converted.mp4"
            nas_copy = nas_folder / "transferred.mp4"
            for path in (source, output, nas_copy):
                path.write_bytes(path.name.encode("utf-8"))

            found = importer.scan_watchfolder_candidates(
                input_folder,
                [".mp4"],
                [output_folder, nas_folder],
            )

            self.assertEqual(found, [source])

    def test_overlapping_media_folders_are_rejected(self):
        with tempfile.TemporaryDirectory() as value:
            input_folder = Path(value) / "input"
            output_folder = input_folder / "output"
            separate = Path(value) / "nas"
            errors = file_utils.media_folder_overlap_errors(input_folder, output_folder, separate)
            self.assertEqual(len(errors), 1)
            self.assertIn("Importfolder and Output folder", errors[0])

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

    def test_version_is_2_2(self):
        self.assertEqual(app_module.APP_VERSION, "2.2")
        self.assertIn('MyAppVersion "2.2"', (ROOT / "packaging" / "MesterSync.iss").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "README.md").read_text(encoding="utf-8").startswith("# MesterSync 2.2"))
        self.assertTrue((APP_DIR / "README_MesterSync.txt").read_text(encoding="utf-8").startswith("MesterSync 2.2"))

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
        app.notification_frame.place.assert_called_once_with(relx=1.0, rely=1.0, x=-48, y=-68, anchor="se", width=570)
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

    def test_notification_history_keeps_the_three_latest_in_newest_first_order(self):
        app = self.bare_app()
        app.notification_history = app_module.deque(maxlen=20)

        for text in ("First", "Second", "Third", "Fourth"):
            app.remember_notification(text, "info")

        latest = [text for _timestamp, text, _level in list(app.notification_history)[:3]]
        self.assertEqual(latest, ["Fourth", "Third", "Second"])

    def test_dismiss_all_notifications_keeps_unresolved_drive_status(self):
        app = self.bare_app()
        app.notification_history = app_module.deque([
            ("12:01", "Drive ready.", "success"),
            ("12:00", "Old transient message", "info"),
        ], maxlen=20)
        app.notification_queue = app_module.deque([("Queued", "info", 2000)], maxlen=6)
        app.persistent_notifications = {"drive": {"text": "Drive ready.", "root": "E:\\"}}
        app.notification_active = True
        app.notification_after_id = None
        app.notification_frame = mock.Mock()

        app.dismiss_all_notifications()

        self.assertEqual(list(app.notification_queue), [])
        self.assertEqual(list(app.notification_history), [("12:01", "Drive ready.", "success")])
        self.assertFalse(app.notification_active)
        app.notification_frame.place_forget.assert_called_once()

    def test_popup_close_control_is_an_x_not_a_dismiss_button(self):
        source = (APP_DIR / "MesterSync.pyw").read_text(encoding="utf-8")
        notification_section = source[source.index("def build_notification_toast"):source.index("def button", source.index("def build_notification_toast"))]
        self.assertIn('text="X"', notification_section)
        self.assertNotIn('"Dismiss"', notification_section)
        self.assertIn('"Dismiss all"', notification_section)

    def test_disk_ready_notification_stays_until_drive_path_disappears(self):
        with tempfile.TemporaryDirectory() as value:
            drive_root = Path(value) / "camera-card"
            drive_root.mkdir()
            app = self.bare_app()
            app.root = mock.Mock()
            app.notification_history = app_module.deque(maxlen=20)
            app.persistent_notifications = {}
            app.persistent_notifications_collapsed = False

            app.show_disk_ready({
                "drive_id": "camera-123",
                "root": str(drive_root),
                "text": "Disk ready to remove.",
            })
            app.prune_removed_drive_notifications()
            self.assertIn("camera-123", app.persistent_notifications)

            drive_root.rmdir()
            app.prune_removed_drive_notifications()
            self.assertNotIn("camera-123", app.persistent_notifications)

    def test_disk_batch_ready_notice_carries_drive_identity_and_root(self):
        app = self.bare_app()
        app.import_batches = {
            "batch": {
                "pending": set(),
                "imported": ["one"],
                "blocked": set(),
                "conversion_released": False,
                "cancelled": False,
                "disk_batch": True,
                "source_label": "E:\\",
                "source_id": "E::1234",
                "ready_notified": False,
                "total": 1,
            }
        }

        conversions, notice = app.import_batch_actions_locked("batch")

        self.assertEqual(conversions, ["one"])
        self.assertEqual(notice["drive_id"], "E::1234")
        self.assertEqual(notice["root"], "E:\\")

    def test_manual_scan_reports_start_and_completion_outside_the_log(self):
        with tempfile.TemporaryDirectory() as value:
            app = self.bare_app()
            app.get_config = lambda: {"input_folder": value}
            app.watchfolder_scan_lock = threading.Lock()
            app.gui_queue = queue.Queue()
            app.clear_task_selection = lambda: None
            app.log = app.test_logs.append
            visible = []
            app.show_notification = lambda text, level="info", duration_ms=None: visible.append((text, level, duration_ms))
            app.scan_existing_input_as_detected = lambda: 2

            app.manual_scan()
            event, payload = app.gui_queue.get(timeout=2)

            self.assertEqual(visible[0], ("Scanning Importfolder for new videos...", "info", 2500))
            self.assertEqual(event, "notification")
            self.assertEqual(payload, ("Scan complete. Added 2 new videos.", "success", 5500))

    def test_unsupported_added_files_receive_visible_feedback(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            unsupported = folder / "notes.txt"
            unsupported.write_text("not a video", encoding="utf-8")
            app = self.bare_app()
            app.import_cancel_requested = threading.Event()
            app.gui_queue = queue.Queue()
            app.get_config = lambda: {
                "input_folder": str(folder),
                "conversion_extensions": [".mov"],
            }

            app.add_paths_to_import([unsupported])
            event, payload = app.gui_queue.get(timeout=2)

            self.assertEqual(event, "notification")
            self.assertEqual(payload, ("No supported video files were found.", "warning", 5500))

    def test_pause_does_not_activate_when_nothing_is_running(self):
        app = self.bare_app()
        app.started = False
        app.import_enabled = False
        app.conversion_enabled = False
        app.transfer_enabled = False
        app.show_notification = mock.Mock()
        app.emit_status = mock.Mock()

        app.toggle_pause()

        self.assertFalse(app.pause_event.is_set())
        app.show_notification.assert_called_once()

    def test_automatic_settings_save_failure_notifies_only_once(self):
        app = self.bare_app()
        app.config = {"checksum_database_path": ""}
        app.config_save_after_id = None
        app.settings_autosave_error_notified = False
        app.unsaved_settings_var = mock.Mock()
        app.set_config_from_ui = lambda: None
        app.log = mock.Mock()
        app.show_notification = mock.Mock()

        with mock.patch.object(app_module, "write_json_file", side_effect=OSError("read only")):
            self.assertFalse(app.flush_config_autosave())
            self.assertFalse(app.flush_config_autosave())

        app.show_notification.assert_called_once()

    def test_dashboard_empty_state_offers_add_and_scan_actions(self):
        source = (APP_DIR / "MesterSync.pyw").read_text(encoding="utf-8")
        dashboard_section = source[source.index("def build_dashboard"):source.index("def build_history")]
        self.assertIn('text="No active files"', dashboard_section)
        self.assertIn('"Add files", self.add_files_dialog', dashboard_section)
        self.assertIn('"Scan Importfolder", self.manual_scan', dashboard_section)

    def test_active_files_header_is_compact_without_selection_tutorial(self):
        source = (APP_DIR / "MesterSync.pyw").read_text(encoding="utf-8")
        dashboard_section = source[source.index("def build_dashboard"):source.index("def build_history")]
        self.assertIn('header.configure(padx=12, pady=6)', dashboard_section)
        self.assertNotIn("Click a row to select it", dashboard_section)

    def test_setup_wizard_uses_a_scrollable_screen_aware_layout(self):
        source = (APP_DIR / "MesterSync.pyw").read_text(encoding="utf-8")
        wizard_section = source[source.index("def run_setup_wizard"):source.index("def available_video_encoders")]
        self.assertIn("wizard.winfo_screenheight()", wizard_section)
        self.assertIn("self.make_canvas_area(wizard)", wizard_section)

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

    def test_about_page_has_one_update_check_action(self):
        source = (APP_DIR / "MesterSync.pyw").read_text(encoding="utf-8")
        self.assertEqual(source.count('"Check for updates"'), 2)
        self.assertNotIn('"Check again"', source)
        self.assertNotIn("update_check_button", source)

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

    def test_batch_rename_requires_a_matching_second_confirmation_click(self):
        app = self.bare_app()
        app.root = mock.Mock()
        app.root.after.return_value = "batch-confirm-timeout"
        first = app_module.TaskState(task_id="first", original_path=Path("first.mov"), display_name="first.mov", rename_base="first")
        second = app_module.TaskState(task_id="second", original_path=Path("second.mov"), display_name="second.mov", rename_base="second")
        app.tasks = {"first": first, "second": second}
        app.task_lock = threading.RLock()
        app.selected_ids = ["first", "second"]
        app.batch_name_var = mock.Mock(get=lambda: "concert", set=lambda _value: None)
        app.batch_prefix_var = mock.Mock(get=lambda: False, set=lambda _value: None)
        app.batch_suffix_var = mock.Mock(get=lambda: False, set=lambda _value: None)
        app.batch_hint_var = mock.Mock()
        app.batch_apply_btn = mock.Mock()
        app.batch_frame = mock.Mock()
        app.batch_frame.winfo_ismapped.return_value = True
        app.batch_rename_row = mock.Mock()
        app.batch_rename_row.winfo_ismapped.return_value = True
        app.get_config = lambda: {"auto_underscore_renames": False}
        app.show_notification = mock.Mock()
        app.log = mock.Mock()
        app.update_batch_affix_buttons = mock.Mock()
        app.clear_task_selection = mock.Mock()
        app.batch_rename_confirmation = None
        app.batch_rename_confirmation_after_id = None

        def update_task(task_id, **changes):
            for key, value in changes.items():
                setattr(app.tasks[task_id], key, value)

        app.update_task = update_task
        app.apply_batch_box_rename()

        self.assertEqual(first.rename_base, "first")
        self.assertEqual(second.rename_base, "second")
        app.batch_apply_btn.configure.assert_any_call(
            text="Confirm rename 2 files",
            bg=app_module.MesterSyncApp.ORANGE,
            activebackground=app_module.MesterSyncApp.ORANGE,
        )

        app.apply_batch_box_rename()

        self.assertEqual(first.rename_base, "concert_01")
        self.assertEqual(second.rename_base, "concert_02")
        app.clear_task_selection.assert_called_once()

    def test_inline_rename_never_changes_other_selected_rows(self):
        app = self.bare_app()
        app.root = mock.Mock()
        first = app_module.TaskState(task_id="first", original_path=Path("first.mov"), display_name="first.mov", rename_base="first")
        second = app_module.TaskState(task_id="second", original_path=Path("second.mov"), display_name="second.mov", rename_base="second")
        app.tasks = {"first": first, "second": second}
        app.selected_ids = ["first", "second"]
        app.row_widgets = {
            "first": {"dirty": True, "rename_var": mock.Mock(get=lambda: "only this one")},
            "second": {"dirty": False, "rename_var": mock.Mock(get=lambda: "second")},
        }
        app.get_config = lambda: {"auto_underscore_renames": False}
        app.log = mock.Mock()

        def update_task(task_id, **changes):
            for key, value in changes.items():
                setattr(app.tasks[task_id], key, value)

        app.update_task = update_task
        app.commit_rename("first")

        self.assertEqual(first.rename_base, "only this one")
        self.assertEqual(second.rename_base, "second")

    def test_focusing_inline_name_exits_multi_selection(self):
        app = self.bare_app()
        app.selected_ids = ["first", "second"]
        app.selection_anchor_id = "first"
        app.row_widgets = {"second": {"dirty": False}}
        app.update_selection_visuals = mock.Mock()
        app.update_batch_box_visibility = mock.Mock()

        app.begin_single_rename("second")

        self.assertEqual(app.selected_ids, ["second"])
        self.assertEqual(app.selection_anchor_id, "second")
        self.assertTrue(app.row_widgets["second"]["dirty"])

    def test_convert_in_place_prefers_verified_local_copy_over_recording_disk(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            recording_disk = folder / "disk" / "clip.mov"
            local_copy = folder / "import" / "clip.mov"
            recording_disk.parent.mkdir()
            local_copy.parent.mkdir()
            recording_disk.write_bytes(b"disk")
            local_copy.write_bytes(b"local")
            app = self.bare_app()
            task = app_module.TaskState(
                task_id="source-order",
                original_path=recording_disk,
                local_input_path=local_copy,
                display_name="clip.mov",
            )

            self.assertEqual(app.find_convert_in_place_source(task), local_copy)

    def test_convert_file_picker_does_not_repurpose_stale_dashboard_selection(self):
        with tempfile.TemporaryDirectory() as value:
            chosen = Path(value) / "chosen.mov"
            chosen.write_bytes(b"video")
            app = self.bare_app()
            app.selected_ids = ["stale-row"]
            app.clear_task_selection = mock.Mock()
            app.validate_ffmpeg_for_conversion = lambda: chosen
            app.create_convert_in_place_task = mock.Mock(return_value="new-task")
            app.queue_convert_in_place_tasks = mock.Mock()

            with mock.patch.object(app_module.filedialog, "askopenfilenames", return_value=[str(chosen)]):
                app.choose_convert_in_place_files()

            app.clear_task_selection.assert_called_once_with()
            app.create_convert_in_place_task.assert_called_once_with(chosen)
            app.queue_convert_in_place_tasks.assert_called_once_with(["new-task"])

    def test_detected_recording_disk_row_cannot_be_repurposed_for_in_place_conversion(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "recording.mov"
            source.write_bytes(b"video")
            app = self.bare_app()
            task = app_module.TaskState(
                task_id="waiting-import",
                original_path=source,
                display_name=source.name,
                stage=app_module.STAGE_DETECTED,
            )
            app.tasks = {task.task_id: task}
            app.selected_ids = [task.task_id]
            app.row_widgets = {}
            app.validate_ffmpeg_for_conversion = lambda: source
            app.log = mock.Mock()
            app.queue_convert_in_place_tasks = mock.Mock()

            app.convert_in_place_selected()

            self.assertFalse(task.convert_in_place)
            self.assertEqual(task.stage, app_module.STAGE_DETECTED)
            app.queue_convert_in_place_tasks.assert_called_once_with([], blocked=1)

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

    def test_renamed_identical_video_is_detected_by_content(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source = folder / "camera_b_take_17.mov"
            source.write_bytes(os.urandom(64 * 1024))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            database = {
                digest: {
                    "original_name": "hyperdeck_02.mp4",
                    "original_size": source.stat().st_size,
                    "quick_fingerprint": duplicates.quick_fingerprint(source),
                    "final_path": "remembered NAS file",
                }
            }

            reason = duplicates.exact_duplicate_reason(
                source,
                {"check_existing_in_input": False},
                database,
                threading.Event(),
            )

            self.assertIn("Checksum already imported", reason)

    def test_quick_fingerprint_skips_full_hash_for_different_same_size_recordings(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source = folder / "camera_a.mov"
            remembered = folder / "camera_b.mov"
            source.write_bytes(b"A" * (8 * 1024 * 1024))
            remembered.write_bytes(b"B" * (8 * 1024 * 1024))
            database = {
                hashlib.sha256(remembered.read_bytes()).hexdigest(): {
                    "original_name": remembered.name,
                    "original_size": remembered.stat().st_size,
                    "quick_fingerprint": duplicates.quick_fingerprint(remembered),
                    "final_path": "remembered NAS file",
                }
            }

            with mock.patch.object(duplicates, "sha256_file", side_effect=AssertionError("full hash should not run")):
                reason = duplicates.exact_duplicate_reason(
                    source,
                    {},
                    database,
                    threading.Event(),
                    fingerprint_cache={},
                )

            self.assertEqual(reason, "")

    def test_quick_fingerprint_match_still_requires_full_hash(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "possible_match.mov"
            source.write_bytes(os.urandom(8 * 1024 * 1024))
            database = {
                "b" * 64: {
                    "original_name": "other_name.mov",
                    "original_size": source.stat().st_size,
                    "quick_fingerprint": duplicates.quick_fingerprint(source),
                    "final_path": "remembered NAS file",
                }
            }

            with mock.patch.object(duplicates, "sha256_file", wraps=file_utils.sha256_file) as full_hash:
                reason = duplicates.exact_duplicate_reason(
                    source,
                    {},
                    database,
                    threading.Event(),
                    fingerprint_cache={},
                )

            self.assertEqual(reason, "")
            full_hash.assert_called_once()

    def test_quick_fingerprint_cache_avoids_rechecking_importfolder_files(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            import_folder = folder / "imports"
            card = folder / "card"
            import_folder.mkdir()
            card.mkdir()
            incoming = card / "hyperdeck_02.mov"
            existing = import_folder / "hyperdeck_01.mov"
            incoming.write_bytes(b"A" * (8 * 1024 * 1024))
            existing.write_bytes(b"B" * (8 * 1024 * 1024))
            cache = {}

            with mock.patch.object(duplicates, "quick_fingerprint", wraps=duplicates.quick_fingerprint) as sampled, mock.patch.object(
                duplicates,
                "sha256_file",
                side_effect=AssertionError("different samples should not need full hashing"),
            ):
                for _ in range(2):
                    reason = duplicates.exact_duplicate_reason(
                        incoming,
                        {"input_folder": str(import_folder)},
                        {},
                        threading.Event(),
                        fingerprint_cache=cache,
                    )
                    self.assertEqual(reason, "")

            self.assertEqual(sampled.call_count, 2)

    def test_duplicate_protection_cannot_be_disabled_by_old_config(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "renamed_camera_file.mov"
            source.write_bytes(os.urandom(64 * 1024))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            app = self.bare_app()
            app.checksum_lock = threading.RLock()
            app.checksum_db = {
                digest: {
                    "original_name": "hyperdeck_02.mp4",
                    "original_size": source.stat().st_size,
                    "final_path": "remembered NAS file",
                }
            }
            app.import_cancel_requested = threading.Event()

            reason = app.duplicate_reason_for(
                source,
                {
                    "enable_duplicate_detection": False,
                    "check_existing_in_input": False,
                },
            )

            self.assertIn("Checksum already imported", reason)

    def test_checksum_database_save_failure_is_reported_as_unsafe(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "camera.mov"
            source.write_bytes(b"concert video")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            app = self.bare_app()
            app.checksum_lock = threading.RLock()
            app.checksum_db = {}
            app.get_config = lambda: {"checksum_database_path": str(Path(value) / "checksums.json")}

            with mock.patch.object(app_module, "save_checksum_records", side_effect=OSError("disk unavailable")):
                saved = app.remember_original_checksum(digest, source.name, source.stat().st_size, source)

            self.assertFalse(saved)
            self.assertIn(digest, app.checksum_db)
            self.assertEqual(app.checksum_db[digest]["quick_fingerprint"], duplicates.quick_fingerprint(source))
            self.assertTrue(any("could not save checksum database" in line.lower() for line in app.test_logs))

    def test_history_recovers_a_missing_duplicate_fingerprint_database(self):
        digest = "a" * 64
        app = self.bare_app()
        app.config_lock = threading.RLock()
        app.checksum_lock = threading.RLock()
        app.get_config = lambda: {"checksum_database_path": "missing-checksums.json"}
        app.history_records = [{
            "display_name": "hyperdeck_02.mp4",
            "path": "D:/Concert archive/hyperdeck_02.mp4",
            "original_checksum": digest,
            "original_size": 123456,
        }]

        with mock.patch.object(app_module, "load_checksum_records", return_value={}):
            app.load_checksum_database()

        self.assertIn(digest, app.checksum_db)
        self.assertEqual(app.checksum_db[digest]["original_name"], "hyperdeck_02.mp4")
        self.assertEqual(app.checksum_db[digest]["original_size"], 123456)

    def test_renamed_identical_video_already_in_importfolder_is_detected(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            import_folder = folder / "imports"
            card = folder / "card"
            import_folder.mkdir()
            card.mkdir()
            payload = os.urandom(64 * 1024)
            existing = import_folder / "concert_master.mov"
            incoming = card / "hyperdeck_02.mp4"
            existing.write_bytes(payload)
            incoming.write_bytes(payload)

            reason = duplicates.exact_duplicate_reason(
                incoming,
                {"check_existing_in_input": False, "input_folder": str(import_folder)},
                {},
                threading.Event(),
            )

            self.assertIn("Exact same video already exists", reason)

    def test_hash_rejects_a_file_that_changes_while_being_read(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "still_recording.mov"
            source.write_bytes(os.urandom(8 * 1024 * 1024))
            changed = False

            def change_timestamp(_percent, _processed, _total):
                nonlocal changed
                if changed:
                    return
                changed = True
                stat = source.stat()
                os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

            self.assertIsNone(file_utils.sha256_file(source, progress_callback=change_timestamp))

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

    def test_exact_duplicate_from_connected_drive_waits_for_import_anyway_review(self):
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
            app.import_single_file = mock.Mock()
            app.emit_status = mock.Mock()

            def finish_once(*_args, **_kwargs):
                app.shutdown_event.set()

            app.complete_import_job = mock.Mock(side_effect=finish_once)
            app.import_worker()

            app.import_single_file.assert_not_called()
            app.update_task.assert_any_call(
                task.task_id,
                stage=app_module.STAGE_DUPLICATE_REVIEW,
                duplicate=True,
                duplicate_reason="Checksum already imported",
                detail="Checksum already imported",
                progress=0,
            )
            app.complete_import_job.assert_called_once_with(task.task_id, success=False, blocked=True)
            self.assertTrue(any("needs review" in line.lower() for line in app.test_logs))

    def test_post_copy_duplicate_claim_keeps_disk_batch_blocked_for_review(self):
        task = app_module.TaskState(
            task_id="post-copy-claim",
            original_path=Path("disk.mov"),
            display_name="disk.mov",
        )
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
        app.duplicate_reason_for = mock.Mock(return_value="")
        app.task_ready_for_conversion_queue = mock.Mock(return_value=False)
        app.emit_status = mock.Mock()

        def finish_copy(*_args, **_kwargs):
            task.stage = app_module.STAGE_DUPLICATE_REVIEW

        app.import_single_file = mock.Mock(side_effect=finish_copy)

        def finish_once(*_args, **_kwargs):
            app.shutdown_event.set()

        app.complete_import_job = mock.Mock(side_effect=finish_once)
        app.import_worker()

        app.complete_import_job.assert_called_once_with(task.task_id, success=False, blocked=True)

    def test_verified_import_pauses_before_conversion_when_another_computer_claimed_it(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = root / "disk" / "camera.mov"
            imports = root / "imports"
            source.parent.mkdir()
            imports.mkdir()
            source.write_bytes(os.urandom(32 * 1024))
            task = app_module.TaskState(
                task_id="claim-race",
                original_path=source,
                display_name=source.name,
            )
            app = self.bare_app()
            app.task_lock = threading.RLock()
            app.tasks = {task.task_id: task}
            app.task_by_path = {file_utils.path_key(source): task.task_id}
            app.import_cancel_requested = threading.Event()
            app.importing_count = 0
            app.get_config = lambda: {
                "input_folder": str(imports),
                "copy_chunk_mb": 1,
                "nas_folder": str(root / "nas"),
            }
            app.reserve_import_target = lambda desired: desired
            app.release_import_target = mock.Mock()
            app.clear_import_progress_snapshot = mock.Mock()
            app.set_import_progress_snapshot = mock.Mock()
            app.emit_status = mock.Mock()
            app.generate_thumbnail_async = mock.Mock()
            app.record_session_stat = mock.Mock()
            app.remember_original_checksum = mock.Mock(return_value=True)
            app.claim_task_in_shared_duplicate_history = mock.Mock(return_value=("", True))

            def copy_safely(src, target, _stop, progress, checksum_callback):
                target.write_bytes(src.read_bytes())
                progress(100, target.stat().st_size, target.stat().st_size)
                checksum_callback(hashlib.sha256(src.read_bytes()).hexdigest())
                return True

            app.copy_file_safely = copy_safely

            def update_task(task_id, **changes):
                for key, item in changes.items():
                    setattr(app.tasks[task_id], key, item)

            app.update_task = update_task
            app.import_single_file(source, task.task_id, False)

            self.assertEqual(task.stage, app_module.STAGE_DUPLICATE_REVIEW)
            self.assertTrue(task.duplicate)
            self.assertTrue(task.local_input_path and task.local_input_path.exists())
            self.assertIn("another MesterSync import", task.duplicate_reason)

    def test_import_anyway_bypasses_duplicate_detection_and_is_persisted(self):
        task = app_module.TaskState(
            task_id="force-import",
            original_path=Path("duplicate.mov"),
            display_name="duplicate.mov",
            stage=app_module.STAGE_DUPLICATE_REVIEW,
            duplicate=True,
            duplicate_reason="Already imported",
            force_import=True,
        )
        app = self.bare_app()
        app.duplicate_reason_for = mock.Mock(return_value="should not be returned")

        self.assertEqual(app.duplicate_reason_for_import_task(task, {}), "")
        app.duplicate_reason_for.assert_not_called()

        restored = storage.record_to_task(storage.task_to_record(task))
        self.assertIsNotNone(restored)
        self.assertTrue(restored.force_import)

    def test_import_worker_never_rechecks_a_forced_import(self):
        task = app_module.TaskState(
            task_id="forced-worker",
            original_path=Path("duplicate.mov"),
            display_name="duplicate.mov",
            force_import=True,
        )
        app = self.bare_app()
        app.task_lock = threading.RLock()
        app.tasks = {task.task_id: task}
        app.import_condition = threading.Condition()
        app.import_queue = app_module.deque([task.task_id])
        app.queued_import_ids = {task.task_id}
        app.import_job_requires_enabled = {task.task_id: False}
        app.import_job_batch = {}
        app.import_batches = {}
        app.import_enabled = False
        app.current_import_id = None
        app.update_task = mock.Mock()
        app.set_import_progress_snapshot = mock.Mock()
        app.clear_import_progress_snapshot = mock.Mock()
        app.duplicate_reason_for = mock.Mock(return_value="duplicate")
        app.complete_import_job = mock.Mock()
        app.emit_status = mock.Mock()

        def import_once(*_args, **_kwargs):
            app.shutdown_event.set()

        app.import_single_file = mock.Mock(side_effect=import_once)
        app.import_worker()

        app.duplicate_reason_for.assert_not_called()
        app.import_single_file.assert_called_once_with(task.original_path, task.task_id, False)

    def test_import_anyway_sets_override_and_requeues_while_import_is_stopped(self):
        task = app_module.TaskState(
            task_id="force-import-click",
            original_path=Path("duplicate.mov"),
            display_name="duplicate.mov",
            stage=app_module.STAGE_DUPLICATE_REVIEW,
            duplicate=True,
            duplicate_reason="Already imported",
        )
        app = self.bare_app()
        app.tasks = {task.task_id: task}
        app.import_cancel_requested = threading.Event()
        app.import_cancel_requested.set()
        app.requeue_duplicate_import = mock.Mock()
        app.ensure_workers_running = mock.Mock()
        app.show_notification = mock.Mock()

        def update_task(task_id, **changes):
            for key, value in changes.items():
                setattr(app.tasks[task_id], key, value)

        app.update_task = update_task
        app.import_duplicate_task(task.task_id)

        self.assertFalse(app.import_cancel_requested.is_set())
        self.assertTrue(task.force_import)
        self.assertFalse(task.duplicate)
        self.assertEqual(task.stage, app_module.STAGE_DETECTED)
        app.requeue_duplicate_import.assert_called_once_with(task.task_id)
        app.ensure_workers_running.assert_called_once()

    def test_interrupted_preimport_states_restore_with_a_restart_action(self):
        for stage in (app_module.STAGE_DETECTED, app_module.STAGE_CHECKING, app_module.STAGE_STOPPING):
            restored = storage.record_to_task({
                "task_id": f"restore-{stage}",
                "original_path": "camera.mov",
                "display_name": "camera.mov",
                "stage": stage,
                "force_import": True,
            })

            self.assertIsNotNone(restored)
            self.assertEqual(restored.stage, app_module.STAGE_STOPPED)
            self.assertTrue(restored.force_import)
            self.assertIn("Ready to resume", restored.detail)

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

    def test_history_clear_and_single_remove_roll_back_when_save_fails(self):
        original = [{"display_name": "one"}, {"display_name": "two"}]
        app = self.bare_app()
        app.history_lock = threading.RLock()
        app.history_records = list(original)
        app.show_notification = mock.Mock()
        app.cleanup_history_thumbnails = mock.Mock()
        app.refresh_history_view = mock.Mock()
        app.save_history = mock.Mock(side_effect=OSError("history unavailable"))

        with mock.patch.object(app_module.messagebox, "askyesno", return_value=True):
            app.clear_history()

        self.assertEqual(app.history_records, original)
        app.cleanup_history_thumbnails.assert_not_called()

        app.remove_history_entry(0)

        self.assertEqual(app.history_records, original)
        app.cleanup_history_thumbnails.assert_not_called()

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
        app.save_pending_tasks = mock.Mock()
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
        app.save_pending_tasks.assert_called_once()

    def test_skip_undo_state_is_persisted_until_history_archive(self):
        app = self.bare_app()
        app.task_lock = threading.RLock()
        app.tasks_save_pending = True
        app.tasks_save_after_id = None
        task = app_module.TaskState(
            task_id="skipped",
            original_path=Path("camera-card.mov"),
            display_name="camera-card.mov",
            stage=app_module.STAGE_SKIPPED,
            skipped=True,
            skip_archive_due=skip_undo.new_skip_deadline(now=100.0),
        )
        app.tasks = {task.task_id: task}

        with mock.patch.object(app_module, "save_task_records") as save_records:
            app.save_pending_tasks(force=True)

        save_records.assert_called_once()
        saved = save_records.call_args.args[0]
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["skipped"])
        self.assertEqual(saved[0]["skip_archive_due"], 160.0)

    def test_restored_skip_undo_task_remains_excluded_from_folder_watch(self):
        source = Path("C:/Import/keep-skipped.mov")
        task = app_module.TaskState(
            task_id="skipped",
            original_path=source,
            local_input_path=source,
            display_name=source.name,
            stage=app_module.STAGE_SKIPPED,
            skipped=True,
            skip_archive_due=160.0,
        )
        app = self.bare_app()
        app.task_lock = threading.RLock()
        app.tasks = {}
        app.task_by_path = {}
        app.skipped_path_keys = set()
        app.emit_task = mock.Mock()
        app.log = mock.Mock()

        with mock.patch.object(app_module, "load_task_records", return_value=[storage.task_to_record(task)]):
            app.load_pending_tasks()

        self.assertIn(file_utils.path_key(source), app.skipped_path_keys)
        self.assertTrue(app.tasks[task.task_id].skipped)

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
                "original_checksum": hashlib.sha256(original.read_bytes()).hexdigest(),
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
                local_input_path=source,
                display_name=source.name,
                rename_base=source.stem,
                stage=app_module.STAGE_IMPORTED,
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

    def test_cleanup_deletes_only_the_exact_verified_working_copy(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            working = folder / "working.mov"
            original = b"MesterSync managed copy"
            expected = hashlib.sha256(original).hexdigest()
            logs = []

            working.write_bytes(b"different recording now at this path")
            self.assertFalse(safe_files.delete_file_if_checksum_matches(working, expected, logs.append))
            self.assertTrue(working.exists())
            self.assertEqual(working.read_bytes(), b"different recording now at this path")

            working.write_bytes(original)
            self.assertTrue(safe_files.delete_file_if_checksum_matches(working, expected, logs.append))
            self.assertFalse(working.exists())

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
