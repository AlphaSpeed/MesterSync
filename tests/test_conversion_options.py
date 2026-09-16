import copy
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_core_safety import app_module
from config_defaults import DEFAULT_FFMPEG_ARGS
from conversion_options import (KEEP, apply_video_options, apply_media_options, estimated_output_bytes, new_preset_options, save_new_preset)
from file_utils import no_window_flags
from presets import arg_value, preset_badges
from queue_eta import queue_estimate
from storage import task_to_record, record_to_task
from tasks import TaskState, STAGE_QUEUED, STAGE_CONVERTING, STAGE_ERROR

FFMPEG = os.environ.get("MESTERSYNC_TEST_FFMPEG") or shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"


class ConversionOptionTests(unittest.TestCase):
    def test_media_formats_audio_and_size(self):
        for container, extension in [("MP4 (.mp4)", ".mp4"), ("MOV (.mov)", ".mov"), ("MKV (.mkv)", ".mkv")]:
            args = apply_media_options(apply_video_options(DEFAULT_FFMPEG_ARGS, bitrate="200"), container, "MP3", "320")
            self.assertEqual(app_module.detect_output_extension(args), extension)
            self.assertEqual(arg_value(args, "-c:a"), "libmp3lame")
            self.assertEqual(arg_value(args, "-b:a"), "320k")
            self.assertEqual(estimated_output_bytes(args, 60), 1502400000)
        with self.assertRaises(ValueError):
            apply_video_options(DEFAULT_FFMPEG_ARGS, bitrate="201")
        with self.assertRaises(ValueError):
            apply_media_options(DEFAULT_FFMPEG_ARGS, audio_kbps="999")
        self.assertIsNone(estimated_output_bytes(DEFAULT_FFMPEG_ARGS, None))

    def prepare_restart_app(self, folder):
        from collections import deque
        app, task, second = self.custom_app()
        task.original_path = folder / "original.mp4"
        task.original_path.write_bytes(b"original untouched")
        app.current_transfer_id = app.current_import_id = None
        app.current_process_lock = threading.Lock()
        app.current_process = mock.Mock()
        app.current_process.poll.return_value = None
        app.queue_condition, app.transfer_condition = threading.Condition(), threading.Condition()
        app.conversion_queue, app.transfer_queue = deque(), deque()
        app.queued_conversion_ids, app.queued_transfer_ids = set(), set()
        app.remember_retained_media = mock.Mock()
        app.ensure_workers_running = mock.Mock()
        app.enqueue_conversion = mock.Mock()
        return app, task, second

    def test_active_edit_waits_for_worker_then_restarts_and_preserves_finished_files(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            app, task, second = self.prepare_restart_app(folder)
            app.current_conversion_id = task.task_id
            task.conversion_started = True
            task.output_path = folder / "finished.mp4"
            task.output_path.write_bytes(b"earlier result")
            args = apply_video_options(DEFAULT_FFMPEG_ARGS, bitrate="50")
            app.apply_video_settings(task.task_id, list(DEFAULT_FFMPEG_ARGS), args)
            self.assertEqual(task.ffmpeg_args, DEFAULT_FFMPEG_ARGS)
            self.assertTrue(task.skipped)
            app.current_process.terminate.assert_called_once()
            self.assertEqual(record_to_task(task_to_record(task)).pending_conversion_args, args)
            app.finish_pending_conversion_changes()
            app.enqueue_conversion.assert_not_called()
            app.current_conversion_id = None
            app.finish_pending_conversion_changes()
            self.assertEqual(task.ffmpeg_args, args)
            self.assertFalse(task.skipped)
            self.assertIsNone(task.output_path)
            self.assertIsNone(task.pending_conversion_args)
            self.assertEqual(task.original_path.read_bytes(), b"original untouched")
            self.assertEqual((folder / "finished.mp4").read_bytes(), b"earlier result")
            self.assertEqual(second.ffmpeg_args, DEFAULT_FFMPEG_ARGS)
            app.enqueue_conversion.assert_called_once_with(task.task_id, force=False)

    def test_restart_handles_unfinished_output_and_blocks_archive_cleanup(self):
        with tempfile.TemporaryDirectory() as value:
            app, task, _ = self.prepare_restart_app(Path(value))
            task.output_path = Path(value) / "reserved_but_not_written.mp4"
            task.conversion_started = True
            app.apply_video_settings(task.task_id, list(DEFAULT_FFMPEG_ARGS), apply_video_options(DEFAULT_FFMPEG_ARGS, bitrate="30"))
            with self.assertRaises(RuntimeError):
                app.archive_task(task.task_id, "converted_only")
            self.assertIn(task.task_id, app.tasks)
            app.finish_pending_conversion_changes()
            app.enqueue_conversion.assert_called_once()
            self.assertIsNone(task.pending_conversion_args)

    def test_active_edit_failed_save_does_not_stop_process(self):
        with tempfile.TemporaryDirectory() as value:
            app, task, _ = self.prepare_restart_app(Path(value))
            app.current_conversion_id = task.task_id
            app.save_pending_tasks.side_effect = OSError("disk full")
            with self.assertRaises(OSError):
                app.apply_video_settings(task.task_id, list(DEFAULT_FFMPEG_ARGS), apply_video_options(DEFAULT_FFMPEG_ARGS, bitrate="30"))
            self.assertFalse(task.skipped)
            self.assertIsNone(task.pending_conversion_args)
            app.current_process.terminate.assert_not_called()

    def test_restart_replacement_ticket_is_claimed_once(self):
        with tempfile.TemporaryDirectory() as value:
            app, task, _ = self.prepare_restart_app(Path(value))
            app.force_conversion_ids = set()
            app.shutdown_event, app.pause_event = threading.Event(), threading.Event()
            app.emit_status = mock.Mock()
            app.conversion_queue.append(task.task_id)
            app.queued_conversion_ids.add(task.task_id)
            def convert(tid):
                self.assertEqual(tid, task.task_id)
                self.assertNotIn(tid, app.conversion_queue)
                self.assertNotIn(tid, app.queued_conversion_ids)
                app.shutdown_event.set()
            app.convert_task = mock.Mock(side_effect=convert)
            with mock.patch.object(app_module, "wait_for_conversion_task", return_value=(task.task_id, False)):
                app.conversion_worker()
            app.convert_task.assert_called_once()

    def test_warning_notification_explains_high_bitrate_and_is_not_repeated(self):
        app, task, _ = self.custom_app()
        task.ffmpeg_args = apply_video_options(DEFAULT_FFMPEG_ARGS, bitrate="50")
        app.notify_task_preset_warnings(task.task_id)
        app.notify_task_preset_warnings(task.task_id)
        app.show_notification.assert_called_once()
        message = app.show_notification.call_args.args[0]
        self.assertIn("large files", message)
        self.assertIn("Conversion can continue", message)

    def test_4k_25fps_30mbps_replaces_conflicting_video_options_only(self):
        before = list(DEFAULT_FFMPEG_ARGS)
        args = apply_video_options(before, "4K UHD (3840 × 2160)", "25", "30")
        self.assertEqual(before, DEFAULT_FFMPEG_ARGS)
        self.assertEqual(arg_value(args, "-b:v"), "30M")
        self.assertEqual(arg_value(args, "-r"), "25")
        self.assertNotIn("-cq:v", args)
        self.assertNotIn("1920:1080", arg_value(args, "-vf"))
        self.assertIn("scale=3840:2160", arg_value(args, "-vf"))
        for option in ("-c:v", "-c:a", "-b:a", "-ac", "-ar", "-map_metadata", "-f"):
            self.assertEqual(arg_value(args, option), arg_value(before, option))
        self.assertIn("0:a:0?", args)
        self.assertNotIn("-y", args)
        self.assertTrue(any("4K · 25 fps · 30 Mbps" == label for label, _ in preset_badges("Custom", args)))

    def test_keep_current_is_an_exact_copy(self):
        args = apply_video_options(DEFAULT_FFMPEG_ARGS)
        self.assertEqual(args, DEFAULT_FFMPEG_ARGS)
        self.assertIsNot(args, DEFAULT_FFMPEG_ARGS)

    def test_original_size_and_rate_remove_previous_limits(self):
        base = [*DEFAULT_FFMPEG_ARGS, "-r", "50"]
        args = apply_video_options(base, "Original size", "Original frame rate", KEEP)
        self.assertNotIn("1920:1080", arg_value(args, "-vf"))
        self.assertNotIn("-r", args)
        self.assertEqual(arg_value(args, "-cq:v"), "22")

    def test_invalid_values_and_advanced_filters_are_rejected(self):
        for bitrate in ("0", "nan", "inf", "-30", "501", "30M", "-y"):
            with self.subTest(bitrate=bitrate), self.assertRaises(ValueError):
                apply_video_options(DEFAULT_FFMPEG_ARGS, bitrate=bitrate)
        with self.assertRaises(ValueError):
            apply_video_options(DEFAULT_FFMPEG_ARGS, fps="999")
        with self.assertRaises(ValueError):
            apply_video_options([*DEFAULT_FFMPEG_ARGS, "-filter_complex", "[0:v]null[v]"], fps="25")
        base = ["-c:v", "libx264", "-vf", "scale=1920:1080,crop=1280:720"]
        with self.assertRaises(ValueError):
            apply_video_options(base, "4K UHD (3840 × 2160)")

    def test_guided_cpu_encoder_has_no_nvenc_specific_options(self):
        args = new_preset_options("H.264 · CPU", "1080p (1920 × 1080)", "25", "30")
        self.assertEqual(arg_value(args, "-c:v"), "libx264")
        self.assertEqual(arg_value(args, "-preset"), "fast")
        self.assertNotIn("-rc", args)
        self.assertNotIn("-cq:v", args)
        self.assertIn("0:a:0?", args)

    def test_new_preset_does_not_overwrite_and_cannot_escape_folder(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            args = new_preset_options("H.264 · CPU", "Original size", "25", "30")
            path = save_new_preset(folder, "Concert", args)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                save_new_preset(folder, "Concert", ["different"])
            self.assertEqual(path.read_bytes(), original)
            for name in ("../outside", "CON", "", "bad\nname", "Current settings"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    save_new_preset(folder, name, args)
            self.assertEqual(list(folder.iterdir()), [path])

    def custom_app(self):
        app = app_module.MesterSyncApp.__new__(app_module.MesterSyncApp)
        app.task_lock = threading.RLock()
        app.current_conversion_id = None
        app.get_config = lambda: {"ffmpeg_args": DEFAULT_FFMPEG_ARGS}
        app.save_pending_tasks = mock.Mock()
        app.emit_task = mock.Mock()
        app.show_notification = mock.Mock()
        first = TaskState("one", Path("one.mp4"), "one.mp4", ffmpeg_args=list(DEFAULT_FFMPEG_ARGS), stage=STAGE_QUEUED)
        second = TaskState("two", Path("two.mp4"), "two.mp4", ffmpeg_args=list(DEFAULT_FFMPEG_ARGS), stage=STAGE_QUEUED)
        app.tasks = {"one": first, "two": second}
        return app, first, second

    def test_one_video_override_is_persisted_without_changing_other_jobs(self):
        app, first, second = self.custom_app()
        args = apply_video_options(DEFAULT_FFMPEG_ARGS, "4K UHD (3840 × 2160)", "25", "30")
        app.apply_video_settings("one", list(DEFAULT_FFMPEG_ARGS), args)
        self.assertEqual(first.ffmpeg_args, args)
        self.assertEqual(second.ffmpeg_args, DEFAULT_FFMPEG_ARGS)
        self.assertEqual(app.get_config()["ffmpeg_args"], DEFAULT_FFMPEG_ARGS)
        restored = record_to_task(task_to_record(first))
        self.assertEqual(restored.ffmpeg_args, args)
        self.assertEqual(restored.preset_name, "Custom · this video")
        app.save_pending_tasks.assert_called_once_with(force=True)

    def test_started_or_changed_job_rejects_stale_editor(self):
        app, first, _ = self.custom_app()
        args = apply_video_options(DEFAULT_FFMPEG_ARGS, bitrate="30")
        app.current_conversion_id = "one"
        with self.assertRaises(ValueError):
            app.apply_video_settings("one", list(DEFAULT_FFMPEG_ARGS), args)
        self.assertEqual(first.ffmpeg_args, DEFAULT_FFMPEG_ARGS)
        app.current_conversion_id = None
        first.ffmpeg_args = [*DEFAULT_FFMPEG_ARGS, "-r", "50"]
        with self.assertRaises(ValueError):
            app.apply_video_settings("one", list(DEFAULT_FFMPEG_ARGS), args)

    def test_failed_override_save_restores_previous_settings(self):
        app, first, _ = self.custom_app()
        before = task_to_record(first)
        app.save_pending_tasks.side_effect = OSError("disk unavailable")
        with self.assertRaises(OSError):
            app.apply_video_settings("one", list(DEFAULT_FFMPEG_ARGS), apply_video_options(DEFAULT_FFMPEG_ARGS, bitrate="30"))
        self.assertEqual(task_to_record(first), before)

    @unittest.skipUnless(Path(FFMPEG).is_file(), "FFmpeg unavailable")
    def test_real_guided_4k_output_has_25fps_audio_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source, target = folder / "source.mp4", folder / "output.mp4"
            def run(args):
                result = subprocess.run(args, capture_output=True, text=True, timeout=90, creationflags=no_window_flags())
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout
            run([FFMPEG, "-n", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.4", "-c:v", "mpeg4", "-c:a", "aac", str(source)])
            original = source.read_bytes()
            args = new_preset_options("H.264 · CPU", "4K UHD (3840 × 2160)", "25", "30")
            run([FFMPEG, "-n", "-v", "error", "-i", str(source), *args, str(target)])
            info = json.loads(run([str(Path(FFMPEG).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")), "-v", "error", "-show_streams", "-of", "json", str(target)]))
            video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (3840, 2160))
            self.assertEqual(video["avg_frame_rate"], "25/1")
            self.assertTrue(any(stream["codec_type"] == "audio" for stream in info["streams"]))
            self.assertEqual(source.read_bytes(), original)
            # Exercise every advertised container/audio combination on real media.
            for container, ext in [("MP4 (.mp4)", ".mp4"), ("MOV (.mov)", ".mov"), ("MKV (.mkv)", ".mkv")]:
                for audio, codec in [("AAC", "aac"), ("MP3", "mp3"), ("AC-3", "ac3")]:
                    with self.subTest(container=container, audio=audio):
                        args = apply_media_options(new_preset_options("H.264 · CPU", "Original size", "50", "4"), container, audio, "192")
                        output = folder / (audio + ext)
                        run([FFMPEG, "-n", "-v", "error", "-i", str(source), *args, str(output)])
                        info = json.loads(run([str(Path(FFMPEG).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")), "-v", "error", "-show_streams", "-of", "json", str(output)]))
                        self.assertEqual(next(s for s in info["streams"] if s["codec_type"] == "audio")["codec_name"], codec)
                        self.assertEqual(next(s for s in info["streams"] if s["codec_type"] == "video")["avg_frame_rate"], "50/1")
                        self.assertEqual(source.read_bytes(), original)


class QueueEstimateTests(unittest.TestCase):
    def test_speed_learning_and_duration_survive_task_serialization(self):
        app = app_module.MesterSyncApp.__new__(app_module.MesterSyncApp)
        app.task_lock = threading.RLock()
        app.conversion_rates = {}
        args = ["-c:v", "libx264"]
        app.remember_conversion_speed(args, 2, 1)
        self.assertFalse(app.conversion_rates)
        app.remember_conversion_speed(args, 120, 60)
        self.assertEqual(app.conversion_rates[tuple(args)], 2)
        task = TaskState("one", Path("one.mp4"), "one.mp4", source_duration_seconds=3600)
        self.assertEqual(record_to_task(task_to_record(task)).source_duration_seconds, 3600)

    def test_forced_conversion_does_not_count_stopped_jobs_as_running(self):
        forced = TaskState("one", Path("one.mp4"), "one.mp4", stage=STAGE_CONVERTING,
                           convert_in_place=True, eta_seconds=60)
        stopped = TaskState("two", Path("two.mp4"), "two.mp4", stage=STAGE_QUEUED, source_duration_seconds=3600)
        text = queue_estimate([forced, stopped], "one", {}, [], enabled=False)
        self.assertIn("1m 00s encoding", text)
        self.assertIn("1 stopped", text)

    def test_estimate_uses_matching_settings_and_active_remaining_time(self):
        args = ["-c:v", "libx264"]
        active = TaskState("active", Path("a.mp4"), "a.mp4", stage=STAGE_CONVERTING, eta_seconds=60, ffmpeg_args=args)
        waiting = TaskState("next", Path("b.mp4"), "b.mp4", stage=STAGE_QUEUED, source_duration_seconds=240, ffmpeg_args=args)
        self.assertEqual(queue_estimate([active, waiting], "active", {tuple(args): 2}, []), "Queue: ~3m 00s encoding")
        waiting.ffmpeg_args = ["-c:v", "libx265"]
        self.assertIn("1 estimating", queue_estimate([active, waiting], "active", {tuple(args): 2}, []))

    def test_unknown_durations_pauses_and_exclusions_are_honest(self):
        task = TaskState("one", Path("a.mp4"), "a.mp4", stage=STAGE_QUEUED)
        self.assertIn("estimating", queue_estimate([task], None, {}, []))
        self.assertIn("paused", queue_estimate([task], None, {}, [], paused=True))
        self.assertIn("stopped", queue_estimate([task], None, {}, [], enabled=False))
        task.duplicate = True
        self.assertEqual(queue_estimate([task], None, {}, []), "Queue: no conversions waiting")
        task.duplicate = False
        task.stage = STAGE_ERROR
        self.assertEqual(queue_estimate([task], None, {}, []), "Queue: no conversions waiting")
