"""Selection must never create runnable work before explicit confirmation."""
import tempfile
import queue
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

import test_app_audit
from test_core_safety import app_module
from file_utils import path_key
from tasks import STAGE_DUPLICATE_REVIEW, STAGE_ERROR


class ImportSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.app = test_app_audit.AppAuditTests().make_app(self.folder)
        self.app.selection_path_keys = set()
        self.app.get_config()["import_mode"] = "select"
        self.app.select_view = True
        self.app.queue_import_batch = mock.Mock(return_value="batch")
        self.app.queue_imported_batch_for_conversion = mock.Mock()
        self.app.duplicate_reason_for = mock.Mock(return_value="")

    def source(self, name="one.mp4", where="card"):
        path = self.folder / where / name
        path.write_bytes(b"test source unchanged")
        return path

    def test_manual_intake_waits_without_duplicate_io_or_task_creation(self):
        path = self.source()
        self.app.sync_shared_duplicate_history = mock.Mock()
        self.app.add_paths_to_import([path])
        self.assertEqual(self.app.tasks, {})
        self.app.queue_import_batch.assert_not_called()
        self.app.sync_shared_duplicate_history.assert_not_called()
        event, (paths, _) = self.app.gui_queue.get_nowait()
        self.assertEqual((event, paths), ("import_selection", [path]))
        self.assertEqual(path.read_bytes(), b"test source unchanged")

    def test_watchfolder_keeps_normal_flow_while_device_review_is_open(self):
        self.source(where="imports")
        self.assertEqual(self.app.scan_existing_input_as_detected(), 1)
        self.assertEqual(self.app.scan_existing_input_as_detected(), 0)
        self.app.get_config()["import_mode"] = "auto"
        self.assertEqual(self.app.scan_existing_input_as_detected(), 0)
        self.assertEqual(len(self.app.tasks), 1)
        self.app.duplicate_reason_for.assert_called_once()
        self.app.queue_imported_batch_for_conversion.assert_called_once()
        self.assertFalse(self.app.selection_path_keys)

    def test_select_mode_alone_does_not_intercept_manual_imports(self):
        self.app.select_view = False
        path = self.source()
        self.app.sync_shared_duplicate_history = mock.Mock()
        self.app.add_paths_to_import([path])
        self.app.queue_import_batch.assert_called_once()
        self.assertEqual(len(self.app.tasks), 1)
        self.assertFalse(self.app.selection_path_keys)

    def test_select_mode_alone_does_not_open_watchfolder_review(self):
        self.app.select_view = False
        self.source(where="imports")
        self.assertEqual(self.app.scan_existing_input_as_detected(), 1)
        self.app.queue_imported_batch_for_conversion.assert_called_once()
        self.assertFalse(self.app.selection_path_keys)

    def test_drive_intake_waits_without_delay_or_tasks(self):
        path = self.source()
        self.app.sync_shared_duplicate_history = mock.Mock()
        self.app.filter_new_drive_sources = mock.Mock(return_value=([path], 0, 0))
        with mock.patch.object(app_module, "scan_drive_importable_videos", return_value=([path], 1)), mock.patch.object(app_module.time, "sleep") as sleep:
            self.app.import_drive(path.parent, "disk-one")
        sleep.assert_not_called()
        self.assertEqual(self.app.tasks, {})
        self.app.queue_import_batch.assert_not_called()
        _, (_, context) = self.app.gui_queue.get_nowait()
        self.assertEqual(context["source_id"], "disk-one")

    def test_only_confirmed_items_use_their_own_presets(self):
        paths = [self.source(f"{i}.mp4") for i in range(3)]
        self.app.offer_import_selection(paths, source_label="Card")
        selected = [(path_key(path), path, {"source_label": "Card", "disk_batch": True, "source_id": "disk"},
                     f"Preset {i}", ["-c:v", "libx264", "-f", fmt])
                    for i, (path, fmt) in enumerate(zip(paths[:2], ("mov", "mkv")))]
        self.app.import_selected_candidates(selected)
        tasks = list(self.app.tasks.values())
        self.assertEqual([task.original_path for task in tasks], paths[:2])
        self.assertEqual([task.preset_name for task in tasks], ["Preset 0", "Preset 1"])
        self.assertEqual([task.output_ext for task in tasks], [".mov", ".mkv"])
        self.assertEqual(self.app.selection_path_keys, {path_key(paths[2])})
        self.assertEqual(self.app.queue_import_batch.call_args.kwargs, {"disk_batch": True, "source_id": "disk"})

    def test_selected_watchfolder_duplicate_remains_blocked(self):
        path = self.source(where="imports")
        self.app.duplicate_reason_for.return_value = "Already imported"
        self.app.import_selected_candidates([(path_key(path), path, {"imported": True}, "Custom", ["-f", "mp4"])])
        self.assertEqual(next(iter(self.app.tasks.values())).stage, STAGE_DUPLICATE_REVIEW)
        self.app.queue_imported_batch_for_conversion.assert_not_called()

    def test_missing_source_stays_available_for_retry(self):
        path = self.folder / "missing.mp4"
        self.app.selection_path_keys.add(path_key(path))
        self.app.import_selected_candidates([(path_key(path), path, {}, "Custom", ["-f", "mp4"])])
        self.assertEqual(self.app.tasks, {})
        self.assertIn(path_key(path), self.app.selection_path_keys)
        self.app.queue_import_batch.assert_not_called()

    def test_duplicate_check_failure_is_recoverable_error(self):
        path = self.source(where="imports")
        self.app.duplicate_reason_for.side_effect = OSError("NAS unavailable")
        self.app.import_selected_candidates([(path_key(path), path, {"imported": True}, "Custom", ["-f", "mp4"])])
        self.assertEqual(next(iter(self.app.tasks.values())).stage, STAGE_ERROR)
        self.app.queue_imported_batch_for_conversion.assert_not_called()

    def test_stop_before_submission_leaves_candidate_unprocessed(self):
        path = self.source()
        self.app.selection_path_keys.add(path_key(path))
        self.app.import_cancel_requested.set()
        self.app.import_selected_candidates([(path_key(path), path, {}, "Custom", ["-f", "mp4"])])
        self.assertEqual(self.app.tasks, {})
        self.assertIn(path_key(path), self.app.selection_path_keys)
        self.app.queue_import_batch.assert_not_called()

    def test_queue_failure_leaves_recoverable_task(self):
        path = self.source()
        self.app.queue_import_batch.side_effect = OSError("Queue unavailable")
        self.app.import_selected_candidates([(path_key(path), path, {}, "Custom", ["-f", "mp4"])])
        task = next(iter(self.app.tasks.values()))
        self.assertEqual(task.stage, STAGE_ERROR)
        self.assertIn("Queue unavailable", task.detail)

    def test_auto_switch_while_device_scan_event_is_pending_uses_auto_flow(self):
        path = self.source()
        self.app.get_config()["import_mode"] = "auto"
        self.app.import_selection = None
        self.app.selection_path_keys.add(path_key(path))
        context = {"disk_batch": True, "source_id": "disk-one", "source_label": "Card"}
        with mock.patch.object(app_module.threading, "Thread") as thread:
            self.app.receive_import_selection(([path], context))
        thread.return_value.start.assert_called_once()
        selected = thread.call_args.kwargs["args"][0]
        self.assertEqual(selected[0][1:3], (path, context))
        self.assertIsNone(self.app.import_selection)
        self.assertFalse(self.app.selection_path_keys)

    def test_cancelled_preview_result_cannot_replace_reconnected_device_preview(self):
        review = app_module.ImportSelection.__new__(app_module.ImportSelection)
        review.app = self.app
        review.app.cleanup_thumbnail_paths = mock.Mock()
        review.frame = mock.Mock()
        review.frame.winfo_exists.return_value = False
        review.tokens = {"same-path": "new-device-session"}
        review.previews = {"same-path": ["new.png"]}
        review.results = queue.Queue()
        review.results.put(("same-path", "cancelled-session", ["old.png"]))
        review.poll()
        self.assertEqual(review.previews["same-path"], ["new.png"])
        review.app.cleanup_thumbnail_paths.assert_called_once_with(["old.png"])

    def test_pending_added_files_do_not_import_after_review_is_cancelled(self):
        path = self.source()
        self.app.select_view = False
        self.app.import_selection = SimpleNamespace(session_id="after-cancel")
        self.app.sync_shared_duplicate_history = mock.Mock()
        self.app.add_paths_to_import([path], review_session="before-cancel")
        self.assertFalse(self.app.tasks)
        self.app.queue_import_batch.assert_not_called()
        self.app.sync_shared_duplicate_history.assert_not_called()

    def test_pending_review_gui_event_is_discarded_after_cancel(self):
        path = self.source()
        self.app.import_selection = SimpleNamespace(session_id="after-cancel")
        self.app.selection_path_keys.add(path_key(path))
        self.app.receive_import_selection(([path], {"review_session": "before-cancel"}))
        self.assertFalse(self.app.selection_path_keys)
        self.assertFalse(self.app.tasks)
