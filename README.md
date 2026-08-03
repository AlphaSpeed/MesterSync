# MesterSync 1.9

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

## Requirements

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
- `presets`: shareable FFmpeg preset files
- `data`: local settings, queues, history, checksums, thumbnails, and preset-test samples

`data` and the generated shortcut are intentionally excluded from Git. Publishing the repository therefore does not include local paths, file history, thumbnails, or checksum records.

## File safety

MesterSync writes imports, conversions, and transfers to temporary files first. It verifies completed files and promotes them without replacing an existing destination. Final names are checked again immediately before each file is committed.

Matching filenames are not treated as proof that two recordings are duplicates. When a same-name and same-size candidate is found, MesterSync compares full-file checksums. Different recordings are imported separately and receive unique names such as `recording.mov`, `recording_1.mov`, and `recording_2.mov`. Final converted names are reserved across both the output and NAS folders so simultaneous jobs cannot select the same name.

Run the core safety checks with:

```powershell
py -m unittest discover -s tests -v
```
