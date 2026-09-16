import importlib.machinery
import importlib.util
import hashlib
import os
import queue
import sys
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
os.environ.setdefault("MESTERSYNC_DATA_DIR", str(Path(tempfile.gettempdir()) / f"MesterSyncTests-{os.getpid()}"))
sys.path.insert(0, str(APP_DIR))

import storage
import tasks
import worker_utils

loader = importlib.machinery.SourceFileLoader("mestersync_queue_audit_app", str(APP_DIR / "MesterSync.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
app_module = importlib.util.module_from_spec(spec)
loader.exec_module(app_module)


class QueueAuditTests(unittest.TestCase):
    def test_explicit_in_place_work_bypasses_stopped_queue_without_reordering_it(self):
        ordinary = [f"stopped-{index}" for index in range(1000)]
        waiting = deque([*ordinary, "in-place"])
        queued = set(waiting)
        condition = threading.Condition()
        with mock.patch.object(condition, "wait", side_effect=AssertionError("An eligible task should start immediately")):
            result = worker_utils.wait_for_conversion_task(
                condition, waiting, queued, {"in-place"}, lambda: False,
                threading.Event(), threading.Event(),
            )
        self.assertEqual(result, ("in-place", True))
        self.assertEqual(list(waiting), ordinary)
        self.assertEqual(queued, set(ordinary))

    def bare_app(self, task):
        app = app_module.MesterSyncApp.__new__(app_module.MesterSyncApp)
        app.tasks = {task.task_id: task}
        app.task_lock = threading.RLock()
        app.queue_condition = threading.Condition()
        app.transfer_condition = threading.Condition()
        app.conversion_queue = deque()
        app.transfer_queue = deque()
        app.queued_conversion_ids = set()
        app.queued_transfer_ids = set()
        app.force_conversion_ids = set()
        app.current_conversion_id = None
        app.current_transfer_id = None
        app.current_import_id = None
        app.skipped_path_keys = set()
        app.import_cancel_requested = threading.Event()
        app.shutdown_event = threading.Event()
        app.pause_event = threading.Event()
        app.import_job_batch = {}
        app.conversion_enabled = True
        app.transfer_enabled = True
        app.gui_queue = queue.Queue()
        app.ensure_workers_running = mock.Mock()
        app.emit_status = mock.Mock()
        app.emit_task = mock.Mock()
        app.log = mock.Mock()
        app.emit_log = mock.Mock()
        app.show_notification = mock.Mock()
        app.save_pending_tasks = mock.Mock()
        app.enqueue_conversion = mock.Mock()
        app.enqueue_transfer = mock.Mock()
        app.finalize_convert_in_place = mock.Mock()
        app.finalize_converted_only = mock.Mock()
        app.queue_import_batch = mock.Mock()
        app.requeue_duplicate_import = mock.Mock()

        def update(task_id, **values):
            for key, value in values.items():
                setattr(app.tasks[task_id], key, value)

        app.update_task = update
        return app

    def test_verification_evidence_roundtrips_independently_of_stage(self):
        original = tasks.TaskState(
            "verified", Path("D:/card/recording.mp4"), "recording.mp4",
            local_input_path=Path("C:/imports/recording.mp4"),
            output_path=Path("C:/outputs/recording.mp4"),
            stage=tasks.STAGE_STOPPED,
            import_verified=True,
            output_validated=True,
        )
        restored = storage.record_to_task(storage.task_to_record(original))
        self.assertTrue(restored.import_verified)
        self.assertTrue(restored.output_validated)

    def test_interrupted_legacy_copy_does_not_gain_verification_on_restart(self):
        for stage in (tasks.STAGE_IMPORTING, tasks.STAGE_CHECKING, tasks.STAGE_ERROR, tasks.STAGE_STOPPED):
            with self.subTest(stage=stage):
                restored = storage.record_to_task({
                    "original_path": "D:/card/recording.mp4",
                    "local_input_path": "C:/imports/recording.mp4",
                    "original_checksum": "a" * 64,
                    "imported_at": "2026-09-07T12:00:00",
                    "stage": stage,
                })
                self.assertFalse(restored.import_verified)

    def test_legacy_post_import_task_retains_completed_verification(self):
        restored = storage.record_to_task({
            "original_path": "D:/card/recording.mp4",
            "local_input_path": "C:/imports/recording.mp4",
            "original_checksum": "a" * 64,
            "imported_at": "2026-09-07T12:00:00",
            "stage": tasks.STAGE_DUPLICATE_REVIEW,
            "duplicate": True,
        })
        self.assertTrue(restored.import_verified)
        self.assertTrue(restored.duplicate)
        self.assertEqual(restored.stage, tasks.STAGE_DUPLICATE_REVIEW)

    def test_interrupted_conversion_does_not_trust_existing_output_path(self):
        for stage in (tasks.STAGE_CONVERTING, tasks.STAGE_CONVERTING_IN_PLACE, tasks.STAGE_ERROR, tasks.STAGE_STOPPED):
            with self.subTest(stage=stage):
                restored = storage.record_to_task({
                    "original_path": "D:/card/recording.mp4",
                    "output_path": "C:/outputs/recording.mp4",
                    # A previous attempt may have left this timestamp behind.
                    "converted_at": "2026-09-06T12:00:00",
                    "stage": stage,
                })
                self.assertFalse(restored.output_validated)

    def test_legacy_transfer_can_keep_completed_conversion_evidence(self):
        restored = storage.record_to_task({
            "original_path": "D:/card/recording.mp4",
            "output_path": "C:/outputs/recording.mp4",
            "converted_at": "2026-09-07T12:00:00",
            "stage": tasks.STAGE_TRANSFERRING,
        })
        self.assertTrue(restored.output_validated)
        self.assertEqual(restored.stage, tasks.STAGE_STOPPED)

    def test_explicit_unverified_flags_are_not_overridden_by_legacy_inference(self):
        restored = storage.record_to_task({
            "original_path": "D:/card/recording.mp4",
            "local_input_path": "C:/imports/recording.mp4",
            "output_path": "C:/outputs/recording.mp4",
            "original_checksum": "a" * 64,
            "imported_at": "2026-09-07T11:00:00",
            "converted_at": "2026-09-07T12:00:00",
            "stage": tasks.STAGE_TRANSFERRING,
            "import_verified": False,
            "output_validated": False,
        })
        self.assertFalse(restored.import_verified)
        self.assertFalse(restored.output_validated)

    def test_recovery_buttons_cannot_bypass_duplicate_review(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "recording.mp4"
            source.write_bytes(b"recording already exists in archive")
            for action in ("restart_or_resume_task", "retry_task", "resume_task", "route_resumed_task"):
                with self.subTest(action=action):
                    task = tasks.TaskState(
                        "duplicate", source, source.name,
                        local_input_path=source,
                        stage=tasks.STAGE_DUPLICATE_REVIEW,
                        duplicate=True,
                        duplicate_reason="Exact recording already imported",
                        import_verified=True,
                    )
                    app = self.bare_app(task)
                    getattr(app, action)(task.task_id)
                    app.enqueue_conversion.assert_not_called()
                    app.enqueue_transfer.assert_not_called()
                    app.queue_import_batch.assert_not_called()
                    app.requeue_duplicate_import.assert_not_called()
                    self.assertEqual(task.stage, tasks.STAGE_DUPLICATE_REVIEW)
                    self.assertTrue(task.duplicate)

    def test_completed_in_place_recovery_never_uses_deleting_finalizers(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "recording.mov"
            output = Path(value) / "recording.mp4"
            source.write_bytes(b"original concert recording")
            output.write_bytes(b"converted concert recording")
            for action in ("restart_or_resume_task", "retry_task", "route_resumed_task", "queue_recoverable_transfers"):
                for transfer_enabled in (True, False):
                    with self.subTest(action=action, transfer_enabled=transfer_enabled):
                        task = tasks.TaskState(
                            "in-place", source, source.name,
                            local_input_path=source,
                            output_path=output,
                            stage=tasks.STAGE_STOPPED,
                            convert_in_place=True,
                            output_validated=True,
                        )
                        app = self.bare_app(task)
                        app.transfer_enabled = transfer_enabled
                        if action == "queue_recoverable_transfers":
                            getattr(app, action)()
                        else:
                            getattr(app, action)(task.task_id)
                        app.enqueue_transfer.assert_not_called()
                        app.finalize_converted_only.assert_not_called()
                        app.enqueue_conversion.assert_not_called()
                        app.finalize_convert_in_place.assert_called_once_with(task.task_id)
                        self.assertEqual(source.read_bytes(), b"original concert recording")

    def test_import_anyway_after_batch_release_queues_verified_copy_once(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "card" / "recording.mp4"
            local = Path(value) / "imports" / "recording.mp4"
            source.parent.mkdir()
            local.parent.mkdir()
            source.write_bytes(b"intentional duplicate")
            local.write_bytes(source.read_bytes())
            task = tasks.TaskState(
                "late-approval", source, source.name,
                local_input_path=local,
                stage=tasks.STAGE_DUPLICATE_REVIEW,
                duplicate=True,
                import_verified=True,
            )
            app = self.bare_app(task)
            app.import_condition = threading.Condition()
            app.import_job_batch = {task.task_id: "batch"}
            app.import_job_requires_enabled = {}
            app.import_enqueue_after_success = set()
            app.import_batches = {
                "batch": {
                    "pending": set(),
                    "imported": ["earlier-recording"],
                    "blocked": {task.task_id},
                    "conversion_released": True,
                    "cancelled": False,
                    "disk_batch": True,
                    "source_label": str(source.parent),
                    "source_id": "recording-disk",
                    "ready_notified": False,
                    "total": 2,
                },
            }
            app.queue_imported_batch_for_conversion = mock.Mock()

            app.import_duplicate_task(task.task_id)

            app.queue_imported_batch_for_conversion.assert_called_once_with([task.task_id])
            app.queue_import_batch.assert_not_called()
            app.requeue_duplicate_import.assert_not_called()
            self.assertNotIn(task.task_id, app.import_job_batch)
            self.assertEqual(source.read_bytes(), b"intentional duplicate")
            self.assertEqual(local.read_bytes(), b"intentional duplicate")

    def test_recovered_corrupt_import_cannot_start_conversion(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "original.mov"
            local = Path(value) / "copied.mov"
            source.write_bytes(b"full original concert")
            local.write_bytes(b"incomplete")
            task = tasks.TaskState(
                "interrupted-copy", source, source.name,
                local_input_path=local,
                original_checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
                import_verified=False,
            )
            app = self.bare_app(task)
            app.remember_original_checksum = mock.Mock()
            app.claim_task_in_shared_duplicate_history = mock.Mock()
            self.assertFalse(app.prepare_conversion_source(task, local, {}))
            self.assertEqual(task.stage, tasks.STAGE_ERROR)
            app.remember_original_checksum.assert_not_called()
            app.claim_task_in_shared_duplicate_history.assert_not_called()
            self.assertEqual(source.read_bytes(), b"full original concert")
            self.assertEqual(local.read_bytes(), b"incomplete")

    def test_unvalidated_recovered_output_blocks_transfer_and_cleanup(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "original.mov"
            output = Path(value) / "incomplete.mp4"
            source.write_bytes(b"full original concert")
            output.write_bytes(b"truncated converted video")
            task = tasks.TaskState(
                "interrupted-output", source, source.name,
                local_input_path=source,
                output_path=output,
                output_validated=False,
            )
            app = self.bare_app(task)
            app.get_config = lambda: {"ffmpeg_path": "ffmpeg.exe"}
            app.get_duration = mock.Mock(return_value=3600)
            app.validate_converted_output = mock.Mock(return_value="Converted duration differs from the source")
            app.reserve_safe_final_target = mock.Mock()
            app.delete_verified_working_file = mock.Mock()

            app.transfer_task(task.task_id)

            app.validate_converted_output.assert_called_once()
            app.reserve_safe_final_target.assert_not_called()
            app.delete_verified_working_file.assert_not_called()
            self.assertEqual(task.stage, tasks.STAGE_ERROR)
            self.assertFalse(task.output_validated)
            self.assertEqual(source.read_bytes(), b"full original concert")
            self.assertEqual(output.read_bytes(), b"truncated converted video")

    def test_recovery_preserves_stop_requests_until_previous_worker_finishes(self):
        for action in ("restart_or_resume_task", "retry_task", "resume_task", "route_resumed_task"):
            for owner in ("current_import_id", "current_conversion_id", "current_transfer_id", "copy_thread_active"):
                with self.subTest(action=action, owner=owner):
                    task = tasks.TaskState(
                        "stopping", Path("recording.mov"), "recording.mov",
                        stage=tasks.STAGE_SKIPPED,
                        skipped=True,
                        skip_archive_due=1.0,
                    )
                    old_import_stop = task.import_stop_event
                    old_transfer_stop = task.transfer_stop_event
                    old_import_stop.set()
                    old_transfer_stop.set()
                    app = self.bare_app(task)
                    if owner == "copy_thread_active":
                        task.copy_thread_active = True
                    else:
                        setattr(app, owner, task.task_id)

                    getattr(app, action)(task.task_id)

                    self.assertIs(task.import_stop_event, old_import_stop)
                    self.assertIs(task.transfer_stop_event, old_transfer_stop)
                    self.assertTrue(old_import_stop.is_set())
                    self.assertTrue(old_transfer_stop.is_set())
                    self.assertTrue(task.skipped)
                    self.assertIsNone(task.skip_archive_due)
                    app.enqueue_conversion.assert_not_called()
                    app.enqueue_transfer.assert_not_called()
                    app.queue_import_batch.assert_not_called()
                    app.requeue_duplicate_import.assert_not_called()
                    app.finalize_convert_in_place.assert_not_called()
                    app.finalize_converted_only.assert_not_called()
                    app.show_notification.assert_called_once()

    def test_undo_can_resume_normally_after_previous_worker_releases_task(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "imported.mov"
            source.write_bytes(b"verified concert")
            task = tasks.TaskState(
                "undo-after-stop", source, source.name,
                local_input_path=source,
                stage=tasks.STAGE_SKIPPED,
                skipped=True,
                import_verified=True,
            )
            app = self.bare_app(task)
            app.current_conversion_id = task.task_id
            previous_stop = task.import_stop_event
            previous_stop.set()
            app.resume_task(task.task_id)
            app.enqueue_conversion.assert_not_called()

            app.current_conversion_id = None
            app.resume_task(task.task_id)

            self.assertFalse(task.skipped)
            self.assertIsNot(task.import_stop_event, previous_stop)
            self.assertFalse(task.import_stop_event.is_set())
            app.enqueue_conversion.assert_called_once_with(task.task_id)
            self.assertEqual(source.read_bytes(), b"verified concert")


if __name__ == "__main__":
    unittest.main()
