import hashlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import duplicate_ledger
import duplicates


class DuplicateAuditTests(unittest.TestCase):
    def test_full_sync_keeps_paths_published_after_its_initial_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            nas = Path(folder)
            digest = "a" * 64
            original = str(nas / "original.mp4")
            concurrent = str(nas / "other-computer.mp4")
            local_new = str(nas / "this-computer.mp4")
            initial = {digest: {"original_size": 4, "final_path": original}}
            local = {digest: {"original_size": 4, "final_path": local_new}}
            self.assertEqual(duplicate_ledger.sync_shared_checksum_ledger(nas, initial).error, "")
            real_load = duplicate_ledger._load_shared_records

            def load_then_publish(*args, **kwargs):
                result = real_load(*args, **kwargs)
                published = duplicate_ledger.publish_shared_checksum_record(
                    nas, digest, {"original_size": 4, "final_path": concurrent}
                )
                self.assertEqual(published.error, "")
                return result

            with mock.patch.object(duplicate_ledger, "_load_shared_records", side_effect=load_then_publish):
                synced = duplicate_ledger.sync_shared_checksum_ledger(nas, local)
            final = duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            self.assertEqual(synced.error, "")
            self.assertEqual(set(final.records[digest]["final_paths"]), {original, concurrent, local_new})

    def test_unchanged_sync_does_not_write_locks_or_records(self):
        with tempfile.TemporaryDirectory() as folder:
            nas = Path(folder)
            local = {"a" * 64: {"original_size": 4, "final_path": str(nas / "concert.mp4")}}
            duplicate_ledger.sync_shared_checksum_ledger(nas, local)
            with mock.patch.object(duplicate_ledger, "_update_shared_record") as update:
                result = duplicate_ledger.sync_shared_checksum_ledger(nas, local)
            self.assertEqual(result.error, "")
            self.assertEqual(result.exported, 0)
            update.assert_not_called()

    def test_concurrent_claims_have_one_winner_and_keep_every_output_path(self):
        with tempfile.TemporaryDirectory() as folder:
            nas = Path(folder)
            self.assertEqual(duplicate_ledger.sync_shared_checksum_ledger(nas, {}).error, "")
            digest = "b" * 64
            gate = threading.Barrier(4)
            results = []

            def publish(index):
                gate.wait(timeout=5)
                results.append(duplicate_ledger.publish_shared_checksum_record(
                    nas, digest, {"final_path": str(nas / f"copy-{index}.mp4")}
                ))

            workers = [threading.Thread(target=publish, args=(index,)) for index in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
            self.assertEqual(len(results), 4)
            self.assertTrue(all(not result.error for result in results))
            self.assertEqual(sum(not result.already_present for result in results), 1)
            final = duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            self.assertEqual(len(final.records[digest]["final_paths"]), 4)

    def test_publish_does_not_recreate_missing_initialized_history(self):
        with tempfile.TemporaryDirectory() as folder:
            nas = Path(folder)
            duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            records = nas / duplicate_ledger.LEDGER_DIRNAME / duplicate_ledger.LEDGER_RECORDS_DIRNAME
            records.rmdir()
            result = duplicate_ledger.publish_shared_checksum_record(nas, "c" * 64, {})
            self.assertIn("missing or unavailable", result.error)
            self.assertFalse(records.exists())

    def test_legacy_checksum_paths_are_shared_and_detected_on_a_fresh_computer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nas = root / "nas"
            nas.mkdir()
            source = root / "renamed-original.mp4"
            source.write_bytes(b"camera original")
            converted = nas / "concert.mp4"
            converted.write_bytes(b"converted bytes")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            result = duplicate_ledger.sync_shared_checksum_ledger(nas, {digest: str(converted)})
            self.assertEqual(result.error, "")
            fresh = duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            reason = duplicates.exact_duplicate_reason(source, {}, fresh.records, threading.Event())
            self.assertIn("Checksum already imported", reason)

    def test_partial_shared_history_can_finish_initializing_without_local_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            nas = Path(folder)
            final = nas / "concert.mp4"
            final.write_bytes(b"converted bytes")
            published = duplicate_ledger.publish_shared_checksum_record(nas, "d" * 64, {"final_path": str(final)})
            self.assertFalse(published.initialized)
            result = duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            self.assertTrue(result.initialized)
            self.assertEqual(result.error, "")

    def test_unreadable_archive_directory_prevents_claiming_complete_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            nas = Path(folder)

            def inaccessible_walk(*args, onerror=None, **kwargs):
                if onerror:
                    onerror(PermissionError("concert folder is inaccessible"))
                return iter(())

            with mock.patch.object(duplicate_ledger.os, "walk", side_effect=inaccessible_walk):
                result = duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            self.assertFalse(result.initialized)
            self.assertIn("inaccessible", result.error)
            self.assertFalse((nas / duplicate_ledger.LEDGER_DIRNAME).exists())

    def test_unreadable_checksum_directory_is_not_treated_as_empty_history(self):
        with tempfile.TemporaryDirectory() as folder:
            nas = Path(folder)
            duplicate_ledger.sync_shared_checksum_ledger(nas, {})

            def inaccessible_walk(*args, onerror=None, **kwargs):
                if onerror:
                    onerror(PermissionError("checksum shard is inaccessible"))
                return iter(())

            with mock.patch.object(duplicate_ledger.os, "walk", side_effect=inaccessible_walk):
                result = duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            self.assertIn("inaccessible", result.error)

    def test_metadata_sidecar_does_not_block_empty_archive_initialization(self):
        with tempfile.TemporaryDirectory() as folder:
            nas = Path(folder)
            (nas / "._HyperDeck_0001.mp4").write_bytes(b"metadata")
            result = duplicate_ledger.sync_shared_checksum_ledger(nas, {})
            self.assertEqual(result.error, "")
            self.assertTrue(result.initialized)

    def test_replaced_file_cannot_return_an_old_full_hash(self):
        for cache in (None, {}):
            with self.subTest(cache=cache), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / "recording.mp4"
                source.write_bytes(b"original")
                real_hash = duplicates.sha256_file

                def hash_then_replace(path, *args, **kwargs):
                    digest = real_hash(path, *args, **kwargs)
                    old_stat = path.stat()
                    replacement = path.with_suffix(".replacement")
                    replacement.write_bytes(b"replaced")
                    os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
                    replacement.replace(path)
                    return digest

                with mock.patch.object(duplicates, "sha256_file", side_effect=hash_then_replace):
                    digest = duplicates.cached_sha256_file(source, cache, threading.Event())
                self.assertIsNone(digest)

    def test_source_changed_during_sample_filter_pauses_for_review(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "growing.mp4"
            source.write_bytes(b"original")
            database = {"e" * 64: {"original_size": 8, "quick_fingerprint": "a" * 64}}

            def sample_then_grow(*args):
                source.write_bytes(b"newly completed recording")
                return "b" * 64

            with mock.patch.object(duplicates, "cached_quick_fingerprint", side_effect=sample_then_grow):
                reason = duplicates.exact_duplicate_reason(source, {}, database, threading.Event())
            self.assertEqual(reason, duplicates.UNCERTAIN_DUPLICATE_REASON)


if __name__ == "__main__":
    unittest.main()
