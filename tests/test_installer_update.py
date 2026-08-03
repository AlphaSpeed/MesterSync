import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
os.environ.setdefault("MESTERSYNC_DATA_DIR", str(Path(tempfile.gettempdir()) / f"MesterSyncTests-{os.getpid()}"))
sys.path.insert(0, str(APP_DIR))

import storage
import updater


class FakeDownloadResponse:
    def __init__(self, payload: bytes):
        self.stream = io.BytesIO(payload)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.stream.read(size)

    def geturl(self):
        return "https://release-assets.githubusercontent.com/release/MesterSync-Setup-2.0.exe"


class InstallerUpdateTests(unittest.TestCase):
    def test_versions_are_compared_numerically(self):
        self.assertTrue(updater.is_newer_version("1.10", "1.9"))
        self.assertTrue(updater.is_newer_version("v2.0.0", "1.99.9"))
        self.assertFalse(updater.is_newer_version("1.9.0", "1.9"))
        with self.assertRaises(ValueError):
            updater.version_tuple("1.9-beta")

    def test_release_requires_a_github_sha256_digest(self):
        payload = {
            "tag_name": "v2.0",
            "html_url": "https://github.com/AlphaSpeed/MesterSync/releases/tag/v2.0",
            "assets": [{
                "name": "MesterSync-Setup-2.0.exe",
                "browser_download_url": "https://github.com/AlphaSpeed/MesterSync/releases/download/v2.0/MesterSync-Setup-2.0.exe",
                "size": 100,
                "digest": "",
            }],
        }
        with self.assertRaises(updater.UpdateError):
            updater.release_from_payload(payload)

    def test_missing_latest_release_is_a_normal_no_release_state(self):
        missing = updater.urllib.error.HTTPError(
            updater.LATEST_RELEASE_URL,
            404,
            "Not Found",
            {},
            None,
        )
        with mock.patch.object(updater.urllib.request, "urlopen", side_effect=missing):
            with self.assertRaises(updater.NoPublishedRelease):
                updater.fetch_latest_release()

    def test_verified_installer_download_is_promoted_atomically(self):
        payload = b"verified installer payload"
        digest = __import__("hashlib").sha256(payload).hexdigest()
        release = updater.ReleaseInfo(
            version="2.0",
            tag="v2.0",
            asset_name="MesterSync-Setup-2.0.exe",
            download_url="https://github.com/AlphaSpeed/MesterSync/releases/download/v2.0/MesterSync-Setup-2.0.exe",
            size=len(payload),
            sha256=digest,
            release_url="https://github.com/AlphaSpeed/MesterSync/releases/tag/v2.0",
        )
        with tempfile.TemporaryDirectory() as value:
            destination = Path(value)
            with mock.patch.object(updater, "update_dir", return_value=destination), mock.patch.object(
                updater.urllib.request, "urlopen", return_value=FakeDownloadResponse(payload)
            ):
                installer = updater.download_installer(release)
            self.assertEqual(installer.read_bytes(), payload)
            self.assertFalse((destination / "MesterSync-Setup-2.0.exe.part").exists())
            updater.verify_installer(installer, release)
            installer.write_bytes(b"tampered after download")
            with self.assertRaises(updater.UpdateError):
                updater.verify_installer(installer, release)

    def test_bad_download_digest_is_deleted(self):
        payload = b"tampered"
        release = updater.ReleaseInfo(
            version="2.0",
            tag="v2.0",
            asset_name="MesterSync-Setup-2.0.exe",
            download_url="https://github.com/AlphaSpeed/MesterSync/releases/download/v2.0/MesterSync-Setup-2.0.exe",
            size=len(payload),
            sha256="0" * 64,
            release_url="https://github.com/AlphaSpeed/MesterSync/releases/tag/v2.0",
        )
        with tempfile.TemporaryDirectory() as value:
            destination = Path(value)
            with mock.patch.object(updater, "update_dir", return_value=destination), mock.patch.object(
                updater.urllib.request, "urlopen", return_value=FakeDownloadResponse(payload)
            ):
                with self.assertRaises(updater.UpdateError):
                    updater.download_installer(release)
            self.assertEqual(list(destination.iterdir()), [])

    def test_portable_data_is_copied_without_overwriting_user_data(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            legacy = root / "portable"
            destination = root / "user"
            (legacy / "data").mkdir(parents=True)
            (legacy / "presets").mkdir()
            (legacy / "app" / "ffmpeg_presets").mkdir(parents=True)
            (legacy / "data" / storage.CONFIG_FILENAME).write_text('{"source":"portable"}', encoding="utf-8")
            (legacy / "app" / storage.HISTORY_FILENAME).write_text('[{"status":"skipped"}]', encoding="utf-8")
            (legacy / "presets" / "Portable.txt").write_text("-c:v libx264", encoding="utf-8")
            (legacy / "app" / "ffmpeg_presets" / "Legacy.txt").write_text("-c:v libx265", encoding="utf-8")
            (destination / "data").mkdir(parents=True)
            (destination / "data" / storage.CONFIG_FILENAME).write_text('{"source":"installed"}', encoding="utf-8")
            old_ready = storage._storage_setup_complete
            try:
                storage._storage_setup_complete = False
                with mock.patch.dict(os.environ, {"MESTERSYNC_DATA_DIR": str(destination)}), mock.patch.object(
                    storage, "_legacy_storage_roots", return_value=[legacy]
                ):
                    storage.ensure_user_storage()
                self.assertIn("installed", (destination / "data" / storage.CONFIG_FILENAME).read_text(encoding="utf-8"))
                self.assertTrue((destination / "data" / storage.HISTORY_FILENAME).exists())
                self.assertTrue((destination / "presets" / "Portable.txt").exists())
                self.assertTrue((destination / "presets" / "Legacy.txt").exists())
                self.assertTrue((legacy / "data" / storage.CONFIG_FILENAME).exists())
            finally:
                storage._storage_setup_complete = old_ready

    def test_installer_command_requests_a_current_user_upgrade(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\Video\AppData\Local"}):
            command = updater.installer_command(Path("MesterSync-Setup-2.0.exe"), Path("C:/Portable/MesterSync"))
        self.assertIn("/CURRENTUSER", command)
        self.assertIn("/SILENT", command)
        self.assertIn("/CLOSEAPPLICATIONS", command)
        self.assertIn(r"/DIR=C:\Users\Video\AppData\Local\Programs\MesterSync", command)
        self.assertTrue(any(item.startswith("/MIGRATEFROM=") for item in command))

    def test_installer_definition_never_reuses_a_protected_install_location(self):
        script = (ROOT / "packaging" / "MesterSync.iss").read_text(encoding="utf-8-sig")
        self.assertIn(r"DefaultDirName={localappdata}\Programs\MesterSync", script)
        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("UsePreviousAppDir=no", script)


if __name__ == "__main__":
    unittest.main()
