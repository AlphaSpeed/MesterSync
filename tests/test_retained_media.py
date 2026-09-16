import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import history_utils
import storage
from file_utils import path_key


class RetainedMediaTests(unittest.TestCase):
    def test_unchanged_retained_video_is_recognized_after_restart(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(storage, "data_dir", return_value=Path(folder)):
            source = Path(folder) / "converted.mp4"
            source.write_bytes(b"finished video")
            record = history_utils.retained_media_record(source)
            self.assertIsNotNone(record)
            mapping = {path_key(source): record}
            storage.save_retained_media_records(mapping)
            restored = storage.load_retained_media_records()
            self.assertEqual(restored, mapping)
            self.assertTrue(history_utils.retained_media_is_unchanged(source, restored[path_key(source)], {}, threading.Event()))

    def test_replaced_recording_with_same_name_and_timestamp_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "HyperDeck_0001.mp4"
            source.write_bytes(b"first movie")
            record = history_utils.retained_media_record(source)
            cache = {}
            self.assertTrue(history_utils.retained_media_is_unchanged(source, record, cache, threading.Event()))
            before = source.stat()
            replacement = Path(folder) / "replacement.mp4"
            replacement.write_bytes(b"other movie")
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(source)
            self.assertFalse(history_utils.retained_media_is_unchanged(source, record, cache, threading.Event()))

    def test_retained_sample_must_match_not_just_file_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "converted.mp4"
            source.write_bytes(b"first movie")
            record = history_utils.retained_media_record(source)
            record["quick_fingerprint"] = "a" * 64
            self.assertFalse(history_utils.retained_media_is_unchanged(source, record, {}, threading.Event()))

    def test_file_changed_during_retained_check_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "converted.mp4"
            source.write_bytes(b"movie")
            record = history_utils.retained_media_record(source)

            def fingerprint_then_change(*args):
                source.write_bytes(b"new longer movie")
                return record["quick_fingerprint"]

            with mock.patch.object(history_utils, "cached_quick_fingerprint", side_effect=fingerprint_then_change):
                self.assertFalse(history_utils.retained_media_is_unchanged(source, record, {}, threading.Event()))

    def test_unreadable_retained_file_does_not_produce_a_partial_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "converted.mp4"
            source.write_bytes(b"movie")
            with mock.patch.object(history_utils, "quick_fingerprint", return_value=None):
                self.assertIsNone(history_utils.retained_media_record(source))

    def test_history_migration_uses_latest_record_and_tolerates_invalid_items(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "converted.mp4"
            source.write_bytes(b"movie")
            latest = history_utils.retained_media_record(source)
            older = dict(latest, quick_fingerprint="a" * 64)
            records = [
                {"retained_media": [latest]},
                {"retained_media": [older]},
                {"retained_media": [None, {"path": 42}, {"path": ""}]},
                None,
            ]
            restored = history_utils.retained_media_from_history(records)
            self.assertEqual(restored, {path_key(source): latest})

    def test_clearing_history_does_not_erase_retained_video_protection(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(storage, "data_dir", return_value=Path(folder)):
            source = Path(folder) / "converted.mp4"
            source.write_bytes(b"movie")
            record = history_utils.retained_media_record(source)
            history = [{"status": "converted_in_place", "retained_media": [record]}]
            storage.save_history_records(history)
            storage.save_retained_media_records(history_utils.retained_media_from_history(history))
            storage.save_history_records([])
            self.assertEqual(storage.load_history_records(), [])
            retained = storage.load_retained_media_records()
            self.assertIn(path_key(source), retained)
            self.assertTrue(history_utils.retained_media_is_unchanged(source, retained[path_key(source)], {}, threading.Event()))

    def test_retained_state_recovers_from_backup(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(storage, "data_dir", return_value=Path(folder)):
            source = Path(folder) / "converted.mp4"
            source.write_bytes(b"movie")
            records = {path_key(source): history_utils.retained_media_record(source)}
            storage.save_retained_media_records(records)
            storage.save_retained_media_records(records)
            storage.retained_media_path().write_text("{broken", encoding="utf-8")
            self.assertEqual(storage.load_retained_media_records(), records)

    def test_existing_unrecoverable_state_is_not_treated_as_a_first_run(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(storage, "data_dir", return_value=Path(folder)):
            self.assertEqual(storage.load_retained_media_records(), {})
            storage.retained_media_path().write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "could not be read"):
                storage.load_retained_media_records()
            storage.retained_media_path().write_text('{"bad": {"path": "video.mp4"}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid file fingerprint"):
                storage.load_retained_media_records()

    def test_retained_save_failure_preserves_previous_records_and_is_reported(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(storage, "data_dir", return_value=Path(folder)):
            source = Path(folder) / "converted.mp4"
            source.write_bytes(b"movie")
            records = {path_key(source): history_utils.retained_media_record(source)}
            storage.save_retained_media_records(records)
            original_replace = storage.os.replace

            def fail_primary_write(source_path, destination):
                if Path(destination) == storage.retained_media_path():
                    raise PermissionError("safety file is locked")
                return original_replace(source_path, destination)

            with mock.patch.object(storage.os, "replace", side_effect=fail_primary_write):
                with self.assertRaises(PermissionError):
                    storage.save_retained_media_records({})
            self.assertEqual(storage.load_retained_media_records(), records)


if __name__ == "__main__":
    unittest.main()
