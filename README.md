# MesterSync 2.0.4

MesterSync is a Windows desktop tool for safely importing, renaming, converting, and transferring video files. It uses FFmpeg for conversion and FFprobe to verify completed media before local working files are removed.

## Main features

- One-at-a-time, checksum-verified imports from recording drives
- Automatic pickup and conversion of supported videos placed in Importfolder
- Duplicate memory that still works after NAS files are moved
- Per-file FFmpeg presets and safety warnings
- Compact and comfortable dashboard layouts, controlled from the sidebar
- Smooth inertial scrolling and a virtualized Dashboard for long queues
- Progressive History rendering so large histories do not block navigation
- Batched, bounded live logging that stays lightweight during long sessions
- Coalesced progress updates and faster background preview generation
- A collapsible Log box in the top control bar between the playback controls and preset selector
- Non-blocking in-app notifications for routine information, warnings, and errors
- Compact Settings categories for folders, drives, safety, and presets
- Smaller stage and Active Files headers that leave more room for video rows
- Ten-frame thumbnail scrubbing and a large preview
- Collision-safe naming across import, output, and NAS folders
- Verified NAS transfers and disk-ready-to-remove feedback
- Persistent queues, history, keyboard controls, and automatic settings saves
- Background folder, free-space, FFmpeg, FFprobe, and encoder diagnostics
- A Preset test section inside Settings that converts a 15-second middle sample and automatically removes prior test outputs after a successful replacement
- A normal per-user Windows installer, Start menu shortcut, uninstaller, and verified in-app updates from GitHub Releases

## Install MesterSync

Download `MesterSync-Setup-<version>.exe` from the latest GitHub Release and open it. The installer does not require administrator access. It installs MesterSync for the current Windows user in `%LOCALAPPDATA%\Programs\MesterSync` and opens the app when installation completes. Updates explicitly use the same per-user location even if an older copy was installed elsewhere.

The installed application checks for an update at most once per day without blocking startup. When a newer release is available, open **Settings → About & updates** and choose **Download and install**. MesterSync verifies the installer against GitHub's SHA-256 digest before allowing it to run. Active imports, conversions, transfers, and preset tests must be stopped before installation.

Settings and work data are stored separately in `%LOCALAPPDATA%\MesterSync`, so upgrading or uninstalling the program does not remove them. When upgrading from the portable/source layout, MesterSync copies existing data and presets into this location without changing or deleting the originals. Launching an update from the portable app supplies its current folder to the installer automatically.

## Run from source

- Windows 10 or 11
- Python 3.10 or newer, including Tkinter
- FFmpeg and FFprobe
- The included default HEVC preset uses NVIDIA NVENC; choose or create another preset on systems without a supported NVIDIA encoder

Install the drag-and-drop dependency from this folder:

```powershell
py -m pip install -r requirements.txt
```

If the Windows `py` launcher is unavailable, use `python -m pip install -r requirements.txt` instead.

Then open `app\MesterSync.vbs`. It starts the app without a console and creates a local `MesterSync.lnk` shortcut with the correct icon path.

The first-run wizard asks for FFmpeg, working folders, the optional NAS destination, and ignored drives.

## Folder layout

- `app`: source code, launcher, and artwork
- `presets`: default/shareable FFmpeg preset files copied into the user's writable preset folder
- `%LOCALAPPDATA%\MesterSync\data`: settings, queues, history, checksums, thumbnails, and preset-test samples
- `%LOCALAPPDATA%\MesterSync\presets`: writable user presets
- `packaging`: Windows executable and installer definitions

The legacy root `data` folder, generated installer output, user data, and generated shortcut are intentionally excluded from Git. Publishing the repository therefore does not include local paths, file history, thumbnails, or checksum records.

## Build the Windows installer

You do not need to build releases on your own computer. Every push to `main` runs the Windows tests and installer build on GitHub. The workflow automatically publishes a GitHub Release when the version in `app\version.py` does not already have one.

The files in `packaging` and `requirements-build.txt` are used by that GitHub workflow and should remain in the repository. Developers can also run `packaging\Build-Installer.ps1` directly for an optional local packaging test. It creates:

- `dist\installer\MesterSync-Setup-<version>.exe`
- `dist\installer\MesterSync-Setup-<version>.exe.sha256`

The build uses PyInstaller's folder mode so the installed app does not need to unpack itself into a temporary directory every time it starts.

## Publish an update

1. Change `APP_VERSION` in `app\version.py`.
2. Commit and push the finished changes to `main`.

The `Build Windows installer` GitHub workflow runs all tests, builds the executable and installer on Windows, creates the matching tag (for example `v2.0.4`), and publishes both files in a GitHub Release. If that version already exists, the release is left unchanged. Existing installations will find a newly published version during their next update check.

## File safety

MesterSync writes imports, conversions, and transfers to temporary files first. It verifies completed files and promotes them without replacing an existing destination. Final names are checked again immediately before each file is committed.

Matching filenames are not treated as proof that two recordings are duplicates. When a same-name and same-size candidate is found, MesterSync compares full-file checksums. Different recordings are imported separately and receive unique names such as `recording.mov`, `recording_1.mov`, and `recording_2.mov`. Final converted names are reserved across both the output and NAS folders so simultaneous jobs cannot select the same name.

Run the core safety checks with:

```powershell
py -m unittest discover -s tests -v
```
