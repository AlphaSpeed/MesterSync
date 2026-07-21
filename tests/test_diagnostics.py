import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import diagnostics


class DiagnosticsTests(unittest.TestCase):
    def test_encoder_parser_uses_ffmpeg_encoder_rows(self):
        text = """
 Encoders:
 V..... libx264              H.264 encoder
 V....D hevc_nvenc           NVIDIA HEVC encoder
 A..... aac                  AAC encoder
 """
        self.assertEqual(diagnostics.parse_video_encoders(text), {"libx264", "hevc_nvenc", "aac"})

    def test_missing_required_folders_are_reported_without_creating_them(self):
        lines = diagnostics.check_folder_health({"input_folder": "", "output_folder": "", "nas_folder": ""})
        self.assertIn("ERROR Importfolder is not configured.", lines)
        self.assertIn("ERROR Output folder is not configured.", lines)
        self.assertIn("INFO NAS folder is optional and not configured.", lines)

    def test_ffmpeg_health_reports_configured_encoder_and_ffprobe(self):
        with tempfile.TemporaryDirectory() as value:
            folder = Path(value)
            ffmpeg = folder / "ffmpeg.exe"
            ffprobe = folder / "ffprobe.exe"
            ffmpeg.write_bytes(b"placeholder")
            ffprobe.write_bytes(b"placeholder")
            completed = mock.Mock(returncode=0, stdout=" V..... libx264 encoder\n", stderr="")
            cfg = {"ffmpeg_path": str(ffmpeg), "ffmpeg_args": ["-c:v", "libx264"]}

            with mock.patch.object(diagnostics.subprocess, "run", return_value=completed):
                lines, encoders = diagnostics.check_ffmpeg_health(cfg)

            self.assertIn(f"OK FFprobe found: {ffprobe}", lines)
            self.assertIn("OK video encoder available: libx264", lines)
            self.assertEqual(encoders, {"libx264"})

    def test_failed_encoder_query_returns_warning_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as value:
            ffmpeg = Path(value) / "ffmpeg.exe"
            ffmpeg.write_bytes(b"placeholder")
            with mock.patch.object(diagnostics, "find_ffprobe", return_value=None), mock.patch.object(
                diagnostics.subprocess,
                "run",
                side_effect=TimeoutError("timed out"),
            ):
                lines, encoders = diagnostics.check_ffmpeg_health({"ffmpeg_path": str(ffmpeg), "ffmpeg_args": []})

            self.assertIsNone(encoders)
            self.assertTrue(any("FFprobe was not found" in line for line in lines))
            self.assertTrue(any("could not query FFmpeg encoders" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
