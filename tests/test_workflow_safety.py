import hashlib
import os
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
import output_names
import safe_files
import storage
from tasks import STAGE_STOPPED, STAGE_TRANSFERRING, TaskState


class WorkflowSafetyTests(unittest.TestCase):
    def test_different_recordings_with_same_filename_remain_separate(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            card_a = folder / "card_a"
            card_b = folder / "card_b"
            import_folder = folder / "imports"
            card_a.mkdir()
            card_b.mkdir()
            import_folder.mkdir()
            first_source = card_a / "recording.mov"
            second_source = card_b / "recording.mov"
            first_source.write_bytes(b"A" * 4096)
            second_source.write_bytes(b"B" * 4096)

            first_digest = hashlib.sha256(first_source.read_bytes()).hexdigest()
            database = {
                first_digest: {
                    "original_name": first_source.name,
                    "original_size": first_source.stat().st_size,
                    "final_path": "remembered first recording",
                }
            }
            reason = duplicates.exact_duplicate_reason(
                second_source,
                {"check_existing_in_input": False},
                database,
                threading.Event(),
            )
            self.assertEqual(reason, "")

            lock = threading.RLock()
            reserved = set()
            first_target = output_names.reserve_unique_path(import_folder / first_source.name, reserved, lock)
            second_target = output_names.reserve_unique_path(import_folder / second_source.name, reserved, lock)
            self.assertEqual(first_target.name, "recording.mov")
            self.assertEqual(second_target.name, "recording_1.mov")
            self.assertNotEqual(first_target, second_target)

    def test_final_names_are_unique_across_output_nas_and_active_jobs(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            output = folder / "output"
            nas = folder / "nas"
            output.mkdir()
            nas.mkdir()
            existing = [
                (str(output / "recording.mp4"), "recording.mp4"),
                (str(nas / "recording_1.mp4"), "recording_1.mp4"),
            ]
            lock = threading.RLock()
            output_reserved = set()
            nas_reserved = set()

            first = output_names.reserve_final_target(
                output,
                "recording",
                "recording.mov",
                ".mp4",
                False,
                existing,
                output_reserved,
                nas_reserved,
                output_reserved,
                lock,
            )
            second = output_names.reserve_final_target(
                nas,
                "recording",
                "recording.mov",
                ".mp4",
                False,
                existing,
                output_reserved,
                nas_reserved,
                nas_reserved,
                lock,
            )
            self.assertEqual(first.name, "recording_2.mp4")
            self.assertEqual(second.name, "recording_3.mp4")

    def test_concurrent_final_name_reservations_do_not_collide(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            output = folder / "output"
            nas = folder / "nas"
            output.mkdir()
            nas.mkdir()
            lock = threading.RLock()
            result_lock = threading.Lock()
            output_reserved = set()
            nas_reserved = set()
            results = []
            start = threading.Barrier(12)

            def reserve(index):
                start.wait()
                target_set = output_reserved if index % 2 == 0 else nas_reserved
                target_folder = output if index % 2 == 0 else nas
                target = output_names.reserve_final_target(
                    target_folder,
                    "recording",
                    "recording.mov",
                    ".mp4",
                    False,
                    [],
                    output_reserved,
                    nas_reserved,
                    target_set,
                    lock,
                )
                with result_lock:
                    results.append(target.name.lower())

            threads = [threading.Thread(target=reserve, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(results), 12)
            self.assertEqual(len(set(results)), 12)

    def test_cancelled_copy_removes_only_temporary_output(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source = folder / "source.mov"
            destination = folder / "destination.mov"
            original = os.urandom(3 * 1024 * 1024)
            source.write_bytes(original)
            stop_checks = 0

            def should_stop():
                nonlocal stop_checks
                stop_checks += 1
                return stop_checks >= 3

            result = safe_files.copy_file_safely(
                source,
                destination,
                256 * 1024,
                should_stop,
                lambda: None,
                lambda *_args: None,
                lambda temp, final: safe_files.promote_temp_no_overwrite(temp, final, lambda _message: None),
                lambda _message: None,
            )
            self.assertFalse(result)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(destination.exists())
            self.assertEqual(list(folder.glob("*.part")), [])

    def test_failed_final_promotion_keeps_source_and_foreign_destination(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source = folder / "converted.mp4"
            destination = folder / "nas" / "converted.mp4"
            destination.parent.mkdir()
            source.write_bytes(b"verified local conversion")

            def interrupted_promotion(temp, final):
                final.write_bytes(b"unrelated file created during transfer")
                return False

            result = safe_files.copy_file_safely(
                source,
                destination,
                1024,
                lambda: False,
                lambda: None,
                lambda *_args: None,
                interrupted_promotion,
                lambda _message: None,
            )
            self.assertFalse(result)
            self.assertEqual(source.read_bytes(), b"verified local conversion")
            self.assertEqual(destination.read_bytes(), b"unrelated file created during transfer")
            self.assertEqual(list(destination.parent.glob("*.part")), [])

    def test_destination_write_failure_keeps_local_source(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            source = folder / "converted.mp4"
            destination = folder / "nas" / "converted.mp4"
            destination.parent.mkdir()
            original = os.urandom(1024 * 1024)
            source.write_bytes(original)

            with mock.patch.object(safe_files.os, "fsync", side_effect=OSError("NAS disconnected")):
                result = safe_files.copy_file_safely(
                    source,
                    destination,
                    128 * 1024,
                    lambda: False,
                    lambda: None,
                    lambda *_args: None,
                    lambda temp, final: safe_files.promote_temp_no_overwrite(temp, final, lambda _message: None),
                    lambda _message: None,
                )

            self.assertFalse(result)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.glob("*.part")), [])

    def test_interrupted_task_restores_as_stopped_with_media_paths_intact(self):
        task = TaskState(
            task_id="restart-safety",
            original_path=Path("D:/CARD/recording.mov"),
            display_name="recording.mov",
            local_input_path=Path("C:/Imports/recording.mov"),
            output_path=Path("C:/Output/recording.mp4"),
            stage=STAGE_TRANSFERRING,
            progress=63,
            detail="Transferring",
            original_checksum="a" * 64,
        )
        restored = storage.record_to_task(storage.task_to_record(task))
        self.assertIsNotNone(restored)
        self.assertEqual(restored.stage, STAGE_STOPPED)
        self.assertEqual(restored.progress, 0)
        self.assertEqual(restored.original_path, task.original_path)
        self.assertEqual(restored.local_input_path, task.local_input_path)
        self.assertEqual(restored.output_path, task.output_path)
        self.assertEqual(restored.original_checksum, task.original_checksum)
        self.assertFalse(restored.import_stop_event.is_set())
        self.assertFalse(restored.transfer_stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
