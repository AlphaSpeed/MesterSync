import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from tasks import TaskState, STAGE_ERROR, STAGE_STOPPED, STAGE_CONVERTING, STAGE_DUPLICATE_REVIEW
from ui_copy import recovery_label, row_status_text


class UiCopyTests(unittest.TestCase):
    def test_recovery_labels_describe_next_operation(self):
        task = TaskState("id", Path("recording.mp4"), "recording.mp4", stage=STAGE_STOPPED)
        self.assertEqual(recovery_label(task, True), "Resume import")
        task.local_input_path = Path("local.mp4")
        self.assertEqual(recovery_label(task, True), "Resume conversion")
        task.stage = STAGE_ERROR
        self.assertEqual(recovery_label(task, True), "Retry conversion")
        task.output_path = Path("output.mp4")
        self.assertEqual(recovery_label(task, True), "Retry transfer")
        self.assertEqual(recovery_label(task, False), "Continue conversion")
        task.convert_in_place = True
        self.assertEqual(recovery_label(task, True), "Continue conversion")

    def test_normal_telemetry_is_hidden_but_warnings_remain(self):
        task = TaskState("id", Path("recording.mp4"), "recording.mp4", stage=STAGE_CONVERTING)
        task.detail = "Elapsed: 10m | Preset: HEVC | FPS: 30 | Speed: 1.2x"
        self.assertEqual(row_status_text(task), "")
        for text in ("Low disk warning", "Waiting for source file to be stable", "Verifying NAS copy"):
            task.detail = text
            self.assertEqual(row_status_text(task), text)

    def test_error_summary_does_not_destroy_full_diagnostics(self):
        task = TaskState("id", Path("recording.mp4"), "recording.mp4", stage=STAGE_ERROR)
        task.detail = "FFmpeg failed: " + "diagnostic " * 100
        original = task.detail
        self.assertEqual(row_status_text(task), "Conversion failed. See Details.")
        self.assertEqual(task.detail, original)
        task.stage = STAGE_DUPLICATE_REVIEW
        task.detail = "Already imported: " + "a" * 200
        self.assertLessEqual(len(row_status_text(task)), 180)
        self.assertTrue(row_status_text(task).startswith("Already imported:"))
