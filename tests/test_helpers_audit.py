"""Regression checks for validation and file cleanup discovered in the app audit."""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import file_utils
import importer
import media_validation
import safe_files


def media_info(duration="60", audio=True):
    streams = [{"codec_type": "video", "width": 1920, "height": 1080}]
    if audio:
        streams.append({"codec_type": "audio"})
    return {"format": {"duration": duration}, "streams": streams}


class HelperAuditTests(unittest.TestCase):
    def test_validation_never_accepts_nonfinite_or_missing_duration(self):
        for duration in ("NaN", "inf", "-inf", "N/A", "0"):
            with self.subTest(duration=duration):
                error = media_validation.validate_media_info(media_info(duration), media_info(), [], 60)
                self.assertIn("no readable duration", error)
        self.assertIn("Source duration", media_validation.validate_media_info(media_info(), media_info("N/A"), [], None))

    def test_source_probe_duration_checks_output_when_earlier_probe_failed(self):
        self.assertIsNone(media_validation.validate_media_info(media_info(), media_info(), [], None))
        error = media_validation.validate_media_info(media_info("20"), media_info(), [], float("nan"))
        self.assertIn("duration differs", error)

    def test_long_concert_cannot_lose_minutes_and_pass_validation(self):
        source_duration = 2 * 60 * 60
        error = media_validation.validate_media_info(media_info(str(source_duration - 120)), media_info(str(source_duration)), [], source_duration)
        self.assertIn("duration differs", error)
        self.assertIsNone(media_validation.validate_media_info(media_info(str(source_duration - 0.1)), media_info(str(source_duration)), [], source_duration))

    def test_failed_source_probe_keeps_original(self):
        with tempfile.TemporaryDirectory() as value:
            output = Path(value) / "converted.mp4"
            output.write_bytes(b"converted")
            with mock.patch.object(media_validation, "ffprobe_media_info", side_effect=[(media_info(), ""), (None, "Source unreadable")]):
                error = media_validation.validate_converted_output(output, Path(value) / "source.mov", Path("ffmpeg.exe"), [], 60)
            self.assertIn("original must be kept", error)
            self.assertIn("Source unreadable", error)

    def test_metadata_folder_file_is_rejected_even_when_added_directly(self):
        with tempfile.TemporaryDirectory() as value:
            metadata = Path(value) / ".BlamOS-Private-Items"
            metadata.mkdir()
            source = metadata / "clip.mp4"
            source.write_bytes(b"metadata")
            self.assertEqual(importer.collect_import_candidates([source], ["mp4"], lambda: False), [])

    def test_generated_conversion_files_are_excluded_from_every_import_route(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            original = folder / "recording.mp4"
            generated = [
                folder / ".mestersync_0123456789abcdef_recording.mp4",
                folder / ".MESTERSYNC_0123456789abcdef_recording.mp4",
                folder / ".mestersync_test_0123456789abcdef_recording.mp4",
            ]
            for path in [original, *generated]:
                path.write_bytes(b"media")
            self.assertEqual(importer.collect_import_candidates([folder], ["mp4"], lambda: False), [original])
            self.assertEqual(importer.collect_import_candidates(generated, ["mp4"], lambda: False), [])
            self.assertEqual(importer.scan_watchfolder_candidates(folder, ["mp4"]), [original])
            found, _ = importer.scan_drive_importable_videos(folder, {"import_extensions": ["mp4"]}, lambda: False, lambda *_: None)
            self.assertEqual(found, [original])

    def test_readiness_rejects_same_size_file_still_being_updated(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "recording.mp4"
            source.write_bytes(b"recording")
            initial = source.stat()
            def update_file(_interval):
                os.utime(source, ns=(initial.st_atime_ns, initial.st_mtime_ns + 2_000_000_000))
            with mock.patch.object(file_utils.time, "sleep", side_effect=update_file):
                self.assertFalse(file_utils.file_is_ready(source, stable_seconds=1))

    def test_stopping_while_paused_never_promotes_copy(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "original.mp4"
            target = Path(value) / "copy.mp4"
            source.write_bytes(b"original")
            stopped = False
            def stop_during_pause():
                nonlocal stopped
                stopped = True
            promote = mock.Mock()
            result = safe_files.copy_file_safely(source, target, 1024, lambda: stopped, stop_during_pause, lambda *_: None, promote, lambda _: None)
            self.assertFalse(result)
            promote.assert_not_called()
            self.assertFalse(target.exists())
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(list(Path(value).glob("*.part")), [])

    @unittest.skipUnless(os.name == "nt", "Windows handle-sharing safety")
    def test_cleanup_cannot_replace_verified_file_during_hash(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "working.mp4"
            source.write_bytes(b"original")
            expected = hashlib.sha256(b"original").hexdigest()
            real_sha256 = hashlib.sha256
            prevented = []
            def attempted_replacement():
                # Called after the cleanup handle is open, before hashing.
                for change in (lambda: source.write_bytes(b"different"), lambda: source.rename(source.with_suffix(".moved"))):
                    try:
                        change()
                    except PermissionError:
                        prevented.append(True)
                return real_sha256()
            with mock.patch.object(safe_files.hashlib, "sha256", side_effect=attempted_replacement):
                self.assertTrue(safe_files.delete_file_if_checksum_matches(source, expected, lambda _: None))
            self.assertEqual(len(prevented), 2)
            self.assertFalse(source.exists())

    @unittest.skipUnless(os.name == "nt", "Windows handle-sharing safety")
    def test_cleanup_keeps_file_open_elsewhere_and_does_not_retry_by_name(self):
        with tempfile.TemporaryDirectory() as value:
            source = Path(value) / "working.mp4"
            source.write_bytes(b"original")
            expected = hashlib.sha256(b"original").hexdigest()
            with source.open("rb"), mock.patch.object(safe_files, "force_delete") as delete:
                self.assertFalse(safe_files.delete_file_if_checksum_matches(source, expected, lambda _: None))
                delete.assert_not_called()
            self.assertEqual(source.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
