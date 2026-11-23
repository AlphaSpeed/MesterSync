"""Whole workflow checks on disposable recordings; no installed app data is used."""
import hashlib
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from test_core_safety import app_module
import storage
from tasks import TaskState, STAGE_IMPORTED, STAGE_DUPLICATE_REVIEW, STAGE_CONVERTING, STAGE_ERROR
from file_utils import path_key


FFMPEG = os.environ.get("MESTERSYNC_TEST_FFMPEG") or shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"


class AppAuditTests(unittest.TestCase):
    def make_app(self, folder):
        app = app_module.MesterSyncApp.__new__(app_module.MesterSyncApp)
        app.tasks = {}
        app.task_by_path = {}
        app.checksum_db = {}
        app.history_records = []
        app.retained_watchfolder_files = {}
        app.retained_media_load_issue = ""
        app.retained_media_lock = threading.RLock()
        app.archived = []
        app.gui_queue = queue.Queue()
        for name in ("task_lock", "checksum_lock", "history_lock", "shared_duplicate_lock", "current_process_lock"):
            setattr(app, name, threading.RLock())
        for name in ("import_condition", "queue_condition", "transfer_condition"):
            setattr(app, name, threading.Condition())
        for name in ("shutdown_event", "pause_event", "import_cancel_requested"):
            setattr(app, name, threading.Event())
        for name in ("reserved_import_paths", "reserved_output_paths", "reserved_nas_paths", "cleanup_path_keys",
                     "skipped_path_keys", "queued_import_ids", "queued_conversion_ids", "queued_transfer_ids", "force_conversion_ids"):
            setattr(app, name, set())
        app.watchfolder_scan_lock = threading.Lock()
        app.import_job_batch = {}
        app.import_batches = {}
        app.conversion_queue = deque()
        app.transfer_queue = deque()
        app.current_import_id = app.current_conversion_id = app.current_transfer_id = app.current_process = None
        app.current_process_paused = False
        app.importing_count = 0
        app.conversion_enabled = app.transfer_enabled = app.import_enabled = True
        for name in ("imports", "output", "nas", "card"):
            (folder / name).mkdir()
        cfg = {
            "input_folder": str(folder / "imports"), "output_folder": str(folder / "output"),
            "nas_folder": str(folder / "nas"), "checksum_database_path": str(folder / "checksums.json"),
            "ffmpeg_path": FFMPEG, "ffmpeg_args": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-c:a", "aac"],
            "copy_chunk_mb": 1, "file_stable_seconds": 0, "conversion_extensions": [".mp4"],
        }
        app.get_config = lambda: cfg
        for name in ("save_pending_tasks", "emit_task", "emit_status", "emit_progress", "emit_log", "log",
                     "generate_thumbnail_async", "record_session_stat", "record_session_saved_bytes", "ensure_workers_running",
                     "set_import_progress_snapshot", "clear_import_progress_snapshot", "show_notification", "wait_if_paused"):
            setattr(app, name, mock.Mock())
        app.low_disk_warning = mock.Mock(return_value=None)

        def update(task_id, **changes):
            for key, value in changes.items():
                setattr(app.tasks[task_id], key, value)
        app.update_task = update
        app.reserve_safe_final_target = lambda task, dest, attr: app.reserve_unique_target(dest / task.final_name(), attr)

        def archive(task_id, status):
            if status == "converted_in_place":
                retained_task = app.tasks[task_id]
                app.remember_retained_media(retained_task.local_input_path, retained_task.output_path)
            task = app.tasks.pop(task_id)
            app.archived.append((task, status))
            for path in (task.original_path, task.local_input_path, task.output_path, task.nas_path):
                if path and app.task_by_path.get(path_key(path)) == task_id:
                    app.task_by_path.pop(path_key(path))
            # Reproduce the original cleanup loop: scan after the active row has
            # been archived, while its working input still exists.
            self.assertEqual(app.scan_existing_input_as_detected(), 0)
        app.archive_task = archive
        return app

    def recording(self, path):
        result = subprocess.run([
            FFMPEG, "-n", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1.5",
            "-c:v", "mpeg4", "-c:a", "aac", str(path),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def pending_transfer(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(mock.patch.dict(os.environ, {"MESTERSYNC_DATA_DIR": str(folder / "state")}))
        patches.enter_context(mock.patch.object(storage, "_legacy_storage_roots", return_value=[]))
        app = self.make_app(folder)
        del app.archive_task
        del app.save_pending_tasks
        app.tasks_save_after_id = None
        app.ensure_output_validated = mock.Mock(return_value=True)
        app.remove_import_job = mock.Mock()
        original = folder / "card" / "camera.mp4"
        local = folder / "imports" / original.name
        output = folder / "output" / original.name
        original.write_bytes(b"untouched card original")
        local.write_bytes(original.read_bytes())
        output.write_bytes(b"converted recording")
        task = TaskState("camera", original, original.name, rename_base=original.stem,
                         local_input_path=local, output_path=output, import_verified=True,
                         output_validated=True, imported_size=local.stat().st_size,
                         original_checksum=hashlib.sha256(local.read_bytes()).hexdigest())
        app.tasks[task.task_id] = task
        return app, task, folder / "nas" / original.name

    def test_retry_after_metadata_failure_verifies_one_copy_and_archives_once(self):
        app, task, dest = self.pending_transfer()
        with mock.patch.object(app_module, "save_retained_media_records", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                app.transfer_task(task.task_id)
        saved = storage.load_task_records()[0]
        self.assertEqual(saved["nas_path"], str(dest))
        self.assertEqual(saved["transfer_checksum"], hashlib.sha256(dest.read_bytes()).hexdigest())
        self.assertTrue(task.local_input_path.exists() and task.output_path.exists())
        self.assertFalse(storage.load_history_records())
        app.update_task(task.task_id, stage=STAGE_ERROR)
        app.retry_task(task.task_id)
        self.assertEqual(list(app.transfer_queue), [task.task_id])
        with mock.patch.object(app, "copy_file_safely", side_effect=AssertionError("recopied completed transfer")):
            app.transfer_task(task.task_id)
        self.assertEqual(list(dest.parent.glob("*.mp4")), [dest])
        self.assertEqual([r["path"] for r in storage.load_history_records()], [str(dest)])
        self.assertEqual(storage.load_task_records(), [])
        self.assertEqual(task.original_path.read_bytes(), b"untouched card original")
        self.assertFalse(task.local_input_path.exists() or task.output_path.exists())

    def test_restart_after_promotion_rejects_changed_destination_and_can_resume(self):
        app, task, dest = self.pending_transfer()
        copy = app.copy_file_safely
        def interrupted_copy(*args):
            self.assertTrue(copy(*args))
            raise OSError("interruption after destination appeared")
        with mock.patch.object(app, "copy_file_safely", side_effect=interrupted_copy):
            with self.assertRaises(OSError):
                app.transfer_task(task.task_id)
        self.assertEqual(storage.load_task_records()[0]["pending_nas_path"], str(dest))
        app.tasks.clear()
        app.load_pending_tasks()
        restored = app.tasks[task.task_id]
        app.queue_recoverable_transfers()
        self.assertEqual(list(app.transfer_queue), [task.task_id])
        output = task.output_path.read_bytes()
        changed = b"x" * len(output)
        dest.write_bytes(changed)
        with mock.patch.object(app, "copy_file_safely", side_effect=AssertionError("unexpected copy")):
            app.transfer_task(task.task_id)
            self.assertEqual(restored.stage, STAGE_ERROR)
            self.assertEqual(task.output_path.read_bytes(), output)
            self.assertTrue(task.local_input_path.is_file())
            self.assertEqual(dest.read_bytes(), changed)
            self.assertFalse(storage.load_history_records())
            dest.write_bytes(output)
            app.retry_task(task.task_id)
            app.transfer_task(task.task_id)
        self.assertEqual(list(dest.parent.glob("*.mp4")), [dest])
        self.assertEqual(len(storage.load_history_records()), 1)

    def test_failed_queue_save_after_history_commit_is_reconciled_on_restart(self):
        app, task, dest = self.pending_transfer()
        def save_until_archived(records):
            if not records:
                raise OSError("disk unavailable")
            storage.save_task_records(records)
        with mock.patch.object(app_module, "save_task_records", side_effect=save_until_archived):
            app.transfer_task(task.task_id)
        self.assertEqual([r["task_id"] for r in storage.load_task_records()], [task.task_id])
        app.history_records = storage.load_history_records()
        app.load_pending_tasks()
        self.assertFalse(app.tasks)
        self.assertEqual(storage.load_task_records(), [])
        self.assertEqual([r["path"] for r in app.history_records], [str(dest)])

    def test_retry_waits_for_a_previously_verified_nas_file_to_return(self):
        app, task, dest = self.pending_transfer()
        with mock.patch.object(app_module, "save_history_records", side_effect=OSError("history unavailable")):
            with self.assertRaises(OSError):
                app.transfer_task(task.task_id)
        self.assertEqual(task.stage, STAGE_ERROR)
        self.assertFalse(storage.load_history_records())
        # Older pending records only stored nas_path after verifying the transfer.
        task.pending_nas_path = None
        dest.rename(dest.with_suffix(".offline"))
        with mock.patch.object(app, "copy_file_safely", side_effect=AssertionError("replaced verified file")):
            app.retry_task(task.task_id)
            app.transfer_task(task.task_id)
            self.assertEqual(task.stage, STAGE_ERROR)
            self.assertIn("Restore access", task.detail)
            self.assertFalse(dest.exists())
            self.assertTrue(task.local_input_path.exists() and task.output_path.exists())
            dest.with_suffix(".offline").rename(dest)
            app.retry_task(task.task_id)
            app.transfer_task(task.task_id)
        self.assertEqual([r["path"] for r in storage.load_history_records()], [str(dest)])

    def test_cancel_after_import_copy_keeps_verified_resume_and_watcher_ownership(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            app = self.make_app(folder)
            src = folder / "card" / "camera.mp4"
            src.write_bytes(b"card bytes")
            task = TaskState("cancelled", src, src.name)
            app.tasks[task.task_id] = task
            copy = app.copy_file_safely
            def stop_after_copy(*args):
                result = copy(*args)
                task.import_stop_event.set()
                return result
            with mock.patch.object(app, "copy_file_safely", side_effect=stop_after_copy):
                app.import_single_file(src, task.task_id, False)
            self.assertEqual(task.stage, app_module.STAGE_STOPPED)
            self.assertFalse(task.import_verified)
            self.assertEqual(task.local_input_path.read_bytes(), src.read_bytes())
            self.assertEqual(task.original_checksum, hashlib.sha256(src.read_bytes()).hexdigest())
            self.assertEqual(app.scan_existing_input_as_detected(), 0)

    def test_changed_verified_working_copy_blocks_conversion(self):
        app, task, _ = self.pending_transfer()
        task.local_input_path.write_bytes(b"another camera recording")
        self.assertFalse(app.prepare_conversion_source(task, task.local_input_path, app.get_config()))
        self.assertEqual(task.stage, STAGE_ERROR)
        self.assertEqual(task.original_path.read_bytes(), b"untouched card original")

    def test_restored_import_reports_local_bytes_before_ffmpeg_without_its_card(self):
        app, task, _ = self.pending_transfer()
        task.stage = app_module.STAGE_VERIFYING_SOURCE
        task.eta_seconds = 5400
        task = storage.record_to_task(storage.task_to_record(task))
        self.assertEqual(task.stage, app_module.STAGE_STOPPED)
        self.assertIsNone(task.eta_seconds)
        app.tasks[task.task_id] = task
        task.original_path.unlink()
        content = b"x" * (9 * 1024 * 1024)
        task.local_input_path.write_bytes(content)
        task.original_checksum = hashlib.sha256(content).hexdigest()
        events = []
        app.emit_progress = lambda *args: events.append((task.stage, task.detail, args))
        with mock.patch.object(app_module, "path_key", side_effect=AssertionError("resolved the old card")):
            self.assertTrue(app.prepare_conversion_source(task, task.local_input_path, app.get_config()))
        self.assertTrue(all(stage == app_module.STAGE_VERIFYING_SOURCE for stage, _, _ in events))
        self.assertTrue(any("4.00 MB / 9.00 MB" in detail and 0 < args[2] < 100 for _, detail, args in events))
        self.assertEqual(events[-1][2][2], 100)

    def test_scan_error_on_one_drive_allows_next_drive_and_retries_first(self):
        with tempfile.TemporaryDirectory() as value:
            app = self.make_app(Path(value))
            app.processed_drive_ids = set()
            app.update_connected_drive_memory = mock.Mock()
            app.scan_existing_input_as_detected = mock.Mock(side_effect=OSError("watch folder offline"))
            attempts = []
            def scan(root, drive_id):
                attempts.append(drive_id)
                if attempts == ["first"]:
                    raise OSError("unavailable")
            app.import_drive = scan
            with mock.patch.object(app_module, "list_candidate_drives", return_value=[("D:/", "first"), ("E:/", "second")]), \
                    mock.patch.object(app.shutdown_event, "wait", side_effect=[None, None]), \
                    mock.patch.object(app.shutdown_event, "is_set", side_effect=[False, False, True]):
                app.drive_monitor_loop()
            self.assertEqual(attempts, ["first", "second", "first"])

    @unittest.skipUnless(Path(FFMPEG).is_file(), "FFmpeg not available for real-media integration")
    def test_real_import_conversion_transfer_and_rescan_preserve_card(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            app = self.make_app(folder)
            source = folder / "card" / "HyperDeck_0019.mp4"
            self.recording(source)
            original = source.read_bytes()
            task = TaskState("concert", source, source.name, rename_base=source.stem)
            app.tasks[task.task_id] = task
            app.task_by_path[path_key(source)] = task.task_id
            app.import_single_file(source, task.task_id, False)
            self.assertEqual(task.stage, STAGE_IMPORTED)
            self.assertTrue(task.import_verified)
            self.assertEqual(app.scan_existing_input_as_detected(), 0)
            app.convert_task(task.task_id)
            self.assertTrue(task.output_validated)
            self.assertEqual(list(app.transfer_queue), [task.task_id])
            app.transfer_task(task.task_id)
            self.assertEqual(app.archived[0][1], "transferred")
            self.assertTrue(task.nas_path.is_file())
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(task.local_input_path.exists())
            self.assertFalse(task.output_path.exists())
            self.assertEqual(app.scan_existing_input_as_detected(), 0)
            renamed = folder / "card" / "different_name.mp4"
            renamed.write_bytes(original)
            self.assertIn("Checksum already imported", app.duplicate_reason_for(renamed, app.get_config()))

    @unittest.skipUnless(Path(FFMPEG).is_file(), "FFmpeg not available for real-media integration")
    def test_real_in_place_conversion_keeps_original_with_transfer_enabled(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            app = self.make_app(folder)
            source = folder / "imports" / "camera.mp4"
            self.recording(source)
            original = source.read_bytes()
            task = TaskState("in-place", source, source.name, local_input_path=source,
                             convert_in_place=True, conversion_started=True, rename_base=source.stem)
            app.tasks[task.task_id] = task
            app.convert_task(task.task_id)
            self.assertEqual(app.archived[0][1], "converted_in_place")
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(task.output_path.is_file())
            self.assertNotEqual(task.output_path, source)
            self.assertEqual(list((folder / "nas").glob("*.mp4")), [])
            self.assertFalse(app.cleanup_path_keys)
            self.assertEqual(app.scan_existing_input_as_detected(), 0)

    def test_kept_output_is_not_watched_after_restart_or_history_clear(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            app = self.make_app(folder)
            source = folder / "imports" / "camera.mp4"
            output = folder / "imports" / "camera_1.mp4"
            source.write_bytes(b"original recording")
            output.write_bytes(b"converted recording")
            with mock.patch.dict(os.environ, {"MESTERSYNC_DATA_DIR": str(folder / "state")}):
                app.hold_paths_during_completed_cleanup(source, output)
                app.remember_retained_media(source, output)
                app.history_records.clear()
                app.retained_watchfolder_files = app_module.load_retained_media_records()
                self.assertFalse(app.cleanup_path_keys)
                self.assertEqual(app.scan_existing_input_as_detected(), 0)
                source.write_bytes(b"different recording using the same name")
                app.tasks["new-recording"] = TaskState("new-recording", source, source.name, local_input_path=source)
                app.duplicate_reason_for = mock.Mock(return_value="")
                app.queue_imported_batch_for_conversion = mock.Mock()
                app.create_task = mock.Mock(return_value="new-recording")
                self.assertEqual(app.scan_existing_input_as_detected(), 1)
                app.create_task.assert_called_once()
                self.assertEqual(app.create_task.call_args.args[0], source)

    def test_rejected_output_can_be_retried_without_deleting_either_file(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            app = self.make_app(folder)
            source = folder / "imports" / "camera.mp4"
            output = folder / "output" / "incomplete.mp4"
            source.write_bytes(b"original recording")
            output.write_bytes(b"incomplete converted recording")
            task = TaskState("retry", source, source.name, local_input_path=source, output_path=output,
                             import_verified=True, output_validated=True)
            app.tasks[task.task_id] = task
            app.get_duration = mock.Mock(return_value=3600)
            app.validate_converted_output = mock.Mock(return_value="Converted duration differs from the source")
            with mock.patch.dict(os.environ, {"MESTERSYNC_DATA_DIR": str(folder / "state")}):
                self.assertFalse(app.ensure_output_validated(task))
                self.assertIsNone(task.output_path)
                self.assertFalse(task.output_validated)
                app.restart_or_resume_task(task.task_id)
                self.assertEqual(list(app.conversion_queue), [task.task_id])
                self.assertEqual(source.read_bytes(), b"original recording")
                self.assertEqual(output.read_bytes(), b"incomplete converted recording")

    def test_unavailable_source_keeps_recovered_output_assigned(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            app = self.make_app(folder)
            output = folder / "output" / "finished.mp4"
            output.write_bytes(b"converted recording")
            task = TaskState("retry", folder / "missing.mp4", "missing.mp4", output_path=output,
                             output_validated=True)
            app.tasks[task.task_id] = task
            self.assertFalse(app.ensure_output_validated(task))
            self.assertEqual(task.output_path, output)
            self.assertFalse(task.output_validated)
            self.assertEqual(output.read_bytes(), b"converted recording")

    def test_in_place_completion_save_failure_keeps_task_and_both_files(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            app = self.make_app(folder)
            source = folder / "imports" / "camera.mp4"
            output = folder / "imports" / "camera_1.mp4"
            source.write_bytes(b"original recording")
            output.write_bytes(b"converted recording")
            task = TaskState("in-place", source, source.name, local_input_path=source,
                             output_path=output, convert_in_place=True)
            app.tasks[task.task_id] = task
            app.hold_paths_during_completed_cleanup(source, output)
            with mock.patch.object(app_module, "save_retained_media_records", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    app_module.MesterSyncApp.archive_task(app, task.task_id, "converted_in_place")
            self.assertIs(app.tasks[task.task_id], task)
            self.assertEqual(source.read_bytes(), b"original recording")
            self.assertEqual(output.read_bytes(), b"converted recording")
            self.assertEqual(app.cleanup_path_keys, {path_key(source), path_key(output)})

    def test_unreadable_completed_file_memory_pauses_automatic_scan(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            app = self.make_app(folder)
            (folder / "imports" / "camera.mp4").write_bytes(b"recording")
            app.retained_media_load_issue = "unreadable safety records"
            self.assertEqual(app.scan_existing_input_as_detected(), 0)
            self.assertFalse(app.tasks)

    def test_import_checksum_mismatch_keeps_both_files(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            app = self.make_app(folder)
            source = folder / "card" / "camera.mp4"
            source.write_bytes(b"original camera recording")
            task = TaskState("bad-copy", source, source.name)
            app.tasks[task.task_id] = task
            def corrupted_copy(src, dest, stop, progress, checksum):
                dest.write_bytes(b"different file arrived during verification")
                checksum(hashlib.sha256(src.read_bytes()).hexdigest())
                return True
            app.copy_file_safely = corrupted_copy
            app.import_single_file(source, task.task_id, False)
            self.assertEqual(task.stage, STAGE_ERROR)
            self.assertFalse(task.import_verified)
            self.assertEqual(source.read_bytes(), b"original camera recording")
            self.assertEqual(task.local_input_path.read_bytes(), b"different file arrived during verification")

    def test_active_conversion_and_duplicate_review_cannot_be_queued_twice(self):
        with tempfile.TemporaryDirectory() as value:
            app = self.make_app(Path(value))
            task = TaskState("busy", Path(value) / "camera.mp4", "camera.mp4", stage=STAGE_CONVERTING)
            app.tasks[task.task_id] = task
            app.current_conversion_id = task.task_id
            app.enqueue_conversion(task.task_id)
            self.assertEqual(task.stage, STAGE_CONVERTING)
            self.assertFalse(app.conversion_queue)
            app.current_conversion_id = None
            task.stage = STAGE_DUPLICATE_REVIEW
            task.duplicate = True
            app.enqueue_conversion(task.task_id)
            self.assertEqual(task.stage, STAGE_DUPLICATE_REVIEW)
            self.assertFalse(app.conversion_queue)

    def test_reserved_import_is_not_rediscovered_before_path_ownership_is_set(self):
        with tempfile.TemporaryDirectory() as value:
            app = self.make_app(Path(value))
            source = Path(app.get_config()["input_folder"]) / "camera.mp4"
            source.write_bytes(b"newly promoted file")
            app.reserved_import_paths.add(path_key(source))
            self.assertEqual(app.scan_existing_input_as_detected(), 0)
            self.assertFalse(app.tasks)

    def test_active_drive_notice_remains_visible_after_many_transient_messages(self):
        app = app_module.MesterSyncApp.__new__(app_module.MesterSyncApp)
        app.persistent_notifications = {"drive": {"text": "Disk D ready to remove"}}
        app.notification_history = deque((str(i), f"Message {i}", "info") for i in range(10))
        items = app.notification_center_items()
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0][1], "Disk D ready to remove")
        self.assertEqual([item[1] for item in items[1:]], ["Message 0", "Message 1", "Message 2"])

    def test_queued_notice_has_no_uncancellable_delayed_display(self):
        app = app_module.MesterSyncApp.__new__(app_module.MesterSyncApp)
        app.notification_active = False
        app.notification_history_open = False
        app.notification_queue = deque([("Message", "info", 5000)])
        app.root = mock.Mock()
        app.display_notification = mock.Mock()
        app.show_next_queued_notification()
        app.display_notification.assert_called_once_with(("Message", "info", 5000))
        app.root.after.assert_not_called()
