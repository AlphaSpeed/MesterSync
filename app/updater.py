from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from storage import read_json_file, update_dir, write_json_file


LATEST_RELEASE_URL = "https://api.github.com/repos/AlphaSpeed/MesterSync/releases/latest"
UPDATE_STATE_FILENAME = "update_state.json"
MAX_INSTALLER_BYTES = 1024 * 1024 * 1024
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class UpdateError(RuntimeError):
    pass


class NoPublishedRelease(UpdateError):
    pass


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    asset_name: str
    download_url: str
    size: int
    sha256: str
    release_url: str
    notes: str = ""


def version_tuple(value: str) -> tuple[int, ...]:
    cleaned = str(value or "").strip().lower()
    if cleaned.startswith("v"):
        cleaned = cleaned[1:]
    match = re.fullmatch(r"(\d+(?:\.\d+){1,3})", cleaned)
    if not match:
        raise ValueError(f"Invalid version: {value}")
    parts = tuple(int(part) for part in match.group(1).split("."))
    return parts + (0,) * max(0, 3 - len(parts))


def is_newer_version(candidate: str, current: str) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def _validated_https_url(value: Any, allowed_hosts: set[str]) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise UpdateError("The release contained an unexpected download address.")
    return url


def release_from_payload(payload: Any) -> ReleaseInfo:
    if not isinstance(payload, dict):
        raise UpdateError("GitHub returned an invalid release response.")
    tag = str(payload.get("tag_name") or "").strip()
    try:
        version_tuple(tag)
    except ValueError as exc:
        raise UpdateError("The latest release has an invalid version tag.") from exc
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("The latest release does not contain an installer.")
    choices = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        lowered = name.lower()
        if lowered.startswith("mestersync-setup-") and lowered.endswith(".exe"):
            choices.append(asset)
    if not choices:
        raise UpdateError("The latest release does not contain MesterSync-Setup.exe.")
    asset = choices[0]
    asset_name = Path(str(asset.get("name") or "")).name
    if not asset_name or asset_name != str(asset.get("name") or ""):
        raise UpdateError("The installer filename in the release is unsafe.")
    try:
        size = int(asset.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise UpdateError("The installer size in the release is invalid.") from exc
    if size <= 0 or size > MAX_INSTALLER_BYTES:
        raise UpdateError("The installer size in the release is outside the allowed range.")
    digest = str(asset.get("digest") or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise UpdateError("The installer does not have a valid GitHub SHA-256 digest.")
    return ReleaseInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        asset_name=asset_name,
        download_url=_validated_https_url(asset.get("browser_download_url"), {"github.com"}),
        size=size,
        sha256=digest.split(":", 1)[1],
        release_url=_validated_https_url(payload.get("html_url"), {"github.com"}),
        notes=str(payload.get("body") or ""),
    )


def fetch_latest_release(timeout: float = 8.0) -> ReleaseInfo:
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "MesterSync-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NoPublishedRelease("No published MesterSync releases are available yet.") from exc
        raise UpdateError(f"Could not check GitHub for updates: HTTP Error {exc.code}: {exc.reason}") from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Could not check GitHub for updates: {exc}") from exc
    return release_from_payload(payload)


def update_state_path() -> Path:
    return update_dir() / UPDATE_STATE_FILENAME


def should_check_for_updates(now: Optional[float] = None) -> bool:
    state = read_json_file(update_state_path(), {})
    if not isinstance(state, dict):
        return True
    try:
        checked_at = float(state.get("checked_at") or 0)
    except (TypeError, ValueError):
        return True
    return (now if now is not None else time.time()) - checked_at >= CHECK_INTERVAL_SECONDS


def record_update_check(release: Optional[ReleaseInfo], now: Optional[float] = None) -> None:
    write_json_file(
        update_state_path(),
        {
            "checked_at": now if now is not None else time.time(),
            "latest_version": release.version if release else "",
            "latest_tag": release.tag if release else "",
        },
    )


def download_installer(
    release: ReleaseInfo,
    progress: Optional[Callable[[int, int], None]] = None,
    timeout: float = 30.0,
) -> Path:
    destination = update_dir() / release.asset_name
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(release.download_url, headers={"User-Agent": "MesterSync-Updater"})
    digest = hashlib.sha256()
    downloaded = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, partial.open("wb") as output:
            final_host = urlparse(response.geturl()).hostname
            if final_host not in ALLOWED_DOWNLOAD_HOSTS:
                raise UpdateError("The installer download redirected to an unexpected server.")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > release.size or downloaded > MAX_INSTALLER_BYTES:
                    raise UpdateError("The installer download was larger than expected.")
                output.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(downloaded, release.size)
            output.flush()
            os.fsync(output.fileno())
        if downloaded != release.size or digest.hexdigest().lower() != release.sha256.lower():
            raise UpdateError("The installer download failed its size or SHA-256 verification.")
        os.replace(partial, destination)
        return destination
    except Exception:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def verify_installer(installer: Path, release: ReleaseInfo) -> None:
    """Re-verify a cached installer immediately before execution."""
    try:
        if installer.stat().st_size != release.size:
            raise UpdateError("The downloaded installer size changed after verification.")
        digest = hashlib.sha256()
        with installer.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise UpdateError(f"The downloaded installer could not be verified: {exc}") from exc
    if digest.hexdigest().lower() != release.sha256.lower():
        raise UpdateError("The downloaded installer changed after verification.")


def installer_command(installer: Path, migration_root: Path) -> list[str]:
    return [
        str(installer),
        "/CURRENTUSER",
        "/SILENT",
        "/NORESTART",
        f"/MIGRATEFROM={migration_root}",
    ]


def start_installer(installer: Path, migration_root: Path) -> None:
    if os.name != "nt":
        raise UpdateError("Automatic installation is available only on Windows.")
    if not installer.is_file() or installer.suffix.lower() != ".exe":
        raise UpdateError("The downloaded installer is missing.")
    subprocess.Popen(installer_command(installer, migration_root), close_fds=True)
