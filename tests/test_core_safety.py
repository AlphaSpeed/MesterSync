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


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import duplicates
import file_utils
import preset_testing
import rename_utils
import storage
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

    def test_copy_never_overwrites_and_rejects_source_changes(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source = folder / "source.bin"
            source.write_bytes(os.urandom(2 * 1024 * 1024))
            app = self.bare_app()

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


if __name__ == "__main__":
    unittest.main()
