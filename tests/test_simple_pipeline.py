import os
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from test_app_audit import FFMPEG, app_module
import storage


class SimplePipelineTests(unittest.TestCase):
    @unittest.skipUnless(Path(FFMPEG).is_file(), "FFmpeg unavailable")
    def test_saved_simple_mode_imports_four_selected_videos_and_transfers_each_once(self):
        try:
            root = app_module.tk.Tk()
        except app_module.tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        self.addCleanup(root.destroy)
        with tempfile.TemporaryDirectory() as value, ExitStack() as patches:
            folder = Path(value)
            card = folder / "card"
            card.mkdir()
            originals = {}
            for color in ("red", "blue", "green", "yellow"):
                path = card / f"{color}.mp4"
                subprocess.run([FFMPEG, "-n", "-v", "error", "-f", "lavfi", "-i",
                                f"color={color}:s=96x64:d=1", "-c:v", "mpeg4", str(path)],
                               capture_output=True, check=True, timeout=30)
                originals[path] = path.read_bytes()

            patches.enter_context(mock.patch.dict(os.environ, {"MESTERSYNC_DATA_DIR": str(folder / "data")}))
            patches.enter_context(mock.patch.object(storage, "_legacy_storage_roots", return_value=[]))
            patches.enter_context(mock.patch.object(storage, "_storage_setup_complete", False))
            cfg = dict(app_module.DEFAULT_CONFIG, ffmpeg_path=FFMPEG, ffmpeg_args=["-c:v", "mpeg4"],
                       simple_dashboard=True, import_priority_enabled=False, setup_complete=True,
                       input_folder=str(folder / "imports"), output_folder=str(folder / "output"),
                       nas_folder=str(folder / "nas"), file_stable_seconds=1, scan_interval_seconds=1)
            storage.write_json_file(storage.config_path(), cfg)
            # Only replace physical drive discovery and unrelated preview workers.
            patches.enter_context(mock.patch.object(app_module, "list_candidate_drives",
                                                    return_value=[(str(folder), "test-volume")]))
            for name in ("ensure_thumbnail_worker_running", "ensure_thumbnail_display_worker_running",
                         "ensure_checksum_worker_running"):
                patches.enter_context(mock.patch.object(app_module.MesterSyncApp, name))
            existing_threads = set(threading.enumerate())
            app = app_module.MesterSyncApp(root)
            def stop_workers():
                app.shutdown_event.set()
                for thread in set(threading.enumerate()) - existing_threads:
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive(), thread.name)
                for timer in root.tk.call("after", "info"):
                    root.after_cancel(timer)
            patches.callback(stop_workers)
            self.assertTrue(app.import_enabled and app.conversion_enabled and app.transfer_enabled)
            self.assertTrue(app.import_priority_enabled)
            locked_copy = folder / "imports" / "red.mp4"
            delete_verified = app.delete_verified_working_file
            def simulate_locked_copy(path, checksum):
                return False if path == locked_copy else delete_verified(path, checksum)
            patches.enter_context(mock.patch.object(app, "delete_verified_working_file", side_effect=simulate_locked_copy))

            logs = []
            reviews = 0
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    kind, payload = app.gui_queue.get(timeout=0.1)
                except queue.Empty:
                    kind, payload = "", None
                if kind == "import_selection":
                    reviews += 1
                    paths, context = payload
                    self.assertEqual(set(paths), set(originals))
                    name, args = app.default_task_preset_snapshot()
                    app.import_selected_candidates([(app_module.path_key(p), p, context, name, args) for p in paths])
                elif kind == "log":
                    logs.append(payload)
                with app.task_lock:
                    errors = [t.detail for t in app.tasks.values()
                              if t.stage in (app_module.STAGE_ERROR, app_module.STAGE_DUPLICATE_REVIEW)]
                    self.assertFalse(errors, (errors, logs))
                if len(app.history_records) == 4 and app.current_transfer_id is None:
                    break
            self.assertEqual(reviews, 1)
            self.assertEqual([r["status"] for r in storage.load_history_records()], ["transferred"] * 4, logs)
            self.assertEqual(set(p.name for p in (folder / "nas").glob("*.mp4")), {p.name for p in originals})
            self.assertEqual(list((folder / "imports").glob("*.mp4")), [locked_copy])
            self.assertEqual(list((folder / "output").glob("*.mp4")), [])
            self.assertEqual({p: p.read_bytes() for p in originals}, originals)
            first_transfer = next(i for i, msg in enumerate(logs) if msg.startswith("Starting NAS transfer"))
            last_conversion = max(i for i, msg in enumerate(logs) if msg.startswith("Starting conversion"))
            self.assertLess(first_transfer, last_conversion)
            self.assertEqual(storage.load_task_records(), [])
            app.retained_watchfolder_files = storage.load_retained_media_records()
            self.assertNotIn(app_module.path_key(locked_copy), app.cleanup_path_keys)
            self.assertEqual(app.scan_existing_input_as_detected(), 0)
            app.load_checksum_database()
            sources, _ = app_module.scan_drive_importable_videos(folder, app.get_config(), lambda: False, lambda *_: None)
            self.assertEqual(set(sources), set(originals))
            self.assertEqual(app.filter_new_drive_sources(folder, "test-volume", sources), ([], 0, 4))
