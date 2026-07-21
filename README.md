# MesterSync

MesterSync is a Windows desktop tool for safely importing, renaming, converting, and transferring video files. It uses FFmpeg for conversion and FFprobe to verify completed media before local working files are removed.

## Main features

- One-at-a-time, checksum-verified imports from recording drives
- Duplicate memory that still works after NAS files are moved
- Per-file FFmpeg presets and safety warnings
- Compact and comfortable dashboard layouts
- Ten-frame thumbnail scrubbing and a large preview
- Collision-safe naming across import, output, and NAS folders
- Verified NAS transfers and disk-ready-to-remove feedback
- Persistent queues, history, keyboard controls, and automatic settings saves
- A preset test that converts a 15-second sample from the middle of a selected video

## Requirements

- Windows 10 or 11
- Python 3.10 or newer, including Tkinter
- FFmpeg and FFprobe

Install the drag-and-drop dependency from this folder:

```powershell
py -m pip install -r requirements.txt
```

Then open `MesterSync\app\MesterSync.vbs`. It starts the app without a console and creates a local `MesterSync.lnk` shortcut with the correct icon path.

The first-run wizard asks for FFmpeg, working folders, the optional NAS destination, and ignored drives.

## Folder layout

- `MesterSync/app`: source code, launcher, and artwork
- `MesterSync/presets`: shareable FFmpeg preset files
- `MesterSync/data`: local settings, queues, history, checksums, thumbnails, and preset-test samples

`MesterSync/data` and the generated shortcut are intentionally excluded from Git. Publishing the repository therefore does not include local paths, file history, thumbnails, or checksum records.

## File safety

MesterSync writes imports, conversions, and transfers to temporary files first. It verifies completed files and promotes them without replacing an existing destination. Final names are checked again immediately before each file is committed.
