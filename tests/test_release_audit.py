"""Release checks using disposable files and controlled worker handovers."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_app_audit as audit_helpers
from test_app_audit import app_module
from tasks import TaskState, STAGE_QUEUED, STAGE_STOPPED
from storage import task_to_record, record_to_task
from media_validation import validate_converted_output


class ReleaseAuditTests(unittest.TestCase):
    def make_app(self, folder):
        app = audit_helpers.AppAuditTests.make_app(self, folder)
        app.cancel_import_queue = mock.Mock()
        source = folder / "imports" / "camera.mp4"
        source.write_bytes(b"original recording")
        task = TaskState("video", source, source.name, local_input_path=source,
                         stage=STAGE_QUEUED, convert_in_place=True)
        app.tasks[task.task_id] = task
        return app, task

    def test_stop_all_during_in_place_countdown_never_starts_ffmpeg(self):
        with tempfile.TemporaryDirectory() as value:
            app, task = self.make_app(Path(value))
            app.current_conversion_id = task.task_id
            with mock.patch.object(app_module.time, "sleep", side_effect=lambda _: app.stop_all()), \
                    mock.patch.object(app_module.subprocess, "Popen") as launch:
                app.convert_task(task.task_id)
            launch.assert_not_called()
            self.assertEqual(task.stage, STAGE_STOPPED)
            self.assertEqual(task.original_path.read_bytes(), b"original recording")

    def test_stop_all_removes_forced_queue_and_explicit_resume_works(self):
        with tempfile.TemporaryDirectory() as value:
            app, task = self.make_app(Path(value))
            app.enqueue_conversion(task.task_id, force=True)
            app.stop_all()
            self.assertFalse(app.conversion_queue)
            self.assertFalse(app.force_conversion_ids)
            self.assertTrue(task.conversion_stop_event.is_set())
            self.assertEqual(task.stage, STAGE_STOPPED)
            app.enqueue_conversion(task.task_id, force=True)
            self.assertEqual(list(app.conversion_queue), [task.task_id])
            self.assertFalse(task.conversion_stop_event.is_set())

    def test_stop_during_readiness_check_cannot_launch_ffmpeg(self):
        with tempfile.TemporaryDirectory() as value:
            app, task = self.make_app(Path(value))
            task.conversion_started = True
            task.import_verified = True
            app.current_conversion_id = task.task_id
            def ready(*_):
                app.stop_all()
                return True
            with mock.patch.object(app_module, "file_is_ready", side_effect=ready), \
                    mock.patch.object(app_module.subprocess, "Popen") as launch:
                app.convert_task(task.task_id)
            launch.assert_not_called()
            self.assertEqual(task.stage, STAGE_STOPPED)

    def test_stop_at_process_start_keeps_original_and_cleans_temporary_output(self):
        import subprocess
        if not Path(audit_helpers.FFMPEG).is_file():
            self.skipTest("FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as value:
            app, task = self.make_app(Path(value))
            task.original_path.unlink()
            audit_helpers.AppAuditTests.recording(self, task.original_path)
            original = task.original_path.read_bytes()
            task.conversion_started = True
            app.current_conversion_id = task.task_id
            launch = subprocess.Popen
            def start_then_stop(*args, **kwargs):
                process = launch(*args, **kwargs)
                app.stop_all()
                return process
            with mock.patch.object(app_module.subprocess, "Popen", side_effect=start_then_stop):
                app.convert_task(task.task_id)
            self.assertEqual(task.stage, STAGE_STOPPED)
            self.assertEqual(task.original_path.read_bytes(), original)
            self.assertEqual(list(task.original_path.parent.iterdir()), [task.original_path])
            self.assertFalse(app.archived)
            self.assertIsNone(app.current_process)

    def test_stopping_pending_in_place_restart_does_not_force_new_work(self):
        with tempfile.TemporaryDirectory() as value:
            app, task = self.make_app(Path(value))
            task.pending_conversion_args = ["-c:v", "libx264"]
            task.skipped = True
            app.stop_all()
            task = record_to_task(task_to_record(task))
            app.tasks[task.task_id] = task
            self.assertTrue(task.conversion_stop_event.is_set())
            app.finish_pending_conversion_changes()
            self.assertIsNone(task.pending_conversion_args)
            self.assertEqual(list(app.conversion_queue), [task.task_id])
            self.assertNotIn(task.task_id, app.force_conversion_ids)
            self.assertFalse(app.conversion_enabled)

    @unittest.skipUnless(Path(audit_helpers.FFMPEG).is_file(), "FFmpeg unavailable")
    def test_full_length_audio_cannot_hide_truncated_video(self):
        import subprocess
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source, output = folder / "source.mp4", folder / "output.mp4"
            audit_helpers.AppAuditTests.recording(self, source)
            result = subprocess.run([
                audit_helpers.FFMPEG, "-n", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=160x90:rate=25:duration=0.2", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000:duration=1.5", "-c:v", "mpeg4",
                "-c:a", "aac", str(output),
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            error = validate_converted_output(output, source, Path(audit_helpers.FFMPEG), [], 1.5)
            self.assertIsNotNone(error)
            self.assertIn("video stream duration", error)
