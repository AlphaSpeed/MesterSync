# MesterSync 2.2

MesterSync is a Windows desktop tool for safely importing, renaming, converting, and transferring video files. It uses FFmpeg for conversion and FFprobe to verify completed media before local working files are removed.

## Auto and Select import modes

Use the **Auto / Select** switch in the center of the top bar. Both modes show the normal Dashboard until a recording drive is detected with Import/watching enabled. Auto starts importing automatically; Select opens a review of that drive's new videos inside the main view. Switching modes alone does not open review. No processing task for those device videos is created until **Import selected** is clicked.

Each video starts unchecked and has its own preset dropdown underneath. Scroll through a continuous grid with two or three columns. Only nearby rows render, keeping large devices responsive. Thumbnails smoothly fill their exact boxes at every size, with a centered crop that preserves aspect ratio. Hover to scrub ten frames or click for fullscreen (Escape/click closes it). Clicking the rest of a card toggles selection. Ctrl+A selects all candidates and Escape clears the selection. Presets are captured when you confirm, and normal duplicate checks and copy verification still apply.

Choose a **Group preset** and click **Apply to selected** to update checked videos. Choosing a per-video dropdown creates an **Individual preset** override, which later group changes preserve. **Use group** clears that override and adopts the last group preset applied to that video. Unchecked videos are unchanged.

**Cancel** dismisses review without importing its candidates; reconnect the device to review them again. Importing your selection returns to the normal Dashboard; unselected videos remain on the device. The toolbar stays on one line, with horizontal scrolling when needed on smaller windows. The mode is saved across restarts, while unconfirmed selections and preset choices are session-only.

Manual Add files and Importfolder scans keep their usual behavior from the Dashboard. Add files inside an open review adds candidates to that review. Scanning the Importfolder continues its normal conversion workflow. Previously queued jobs follow the existing Start/Pause/Stop controls. The Log button sits below Compact in the sidebar and opens a full-width panel at the bottom of the app. Notifications are in the header, clear of the import controls. Pillow is included in the declared runtime requirements for smooth thumbnail resizing and rounded control surfaces.

Compact reduces whole-row height with smaller controls, tighter spacing, and a two-line time/size estimate alongside the progress bar. It also tightens Select cards without changing their thumbnail aspect ratio. Comfortable restores larger controls and the expanded progress layout. Buttons, rename and number fields, preset dropdowns, queue numbers, and the status indicator share rounded corners. Scrollbars use a narrow rounded thumb with hover feedback and retain native dragging, track clicks, and mouse-wheel behavior.

Resizing reuses visible review cards and waits briefly for the window size to settle before refitting previews. Inactive pages remain cached without laying out or repainting behind the current view. Completed History pages are reused until their records change. Rounded controls use cached, opaque drawing assets to keep Windows repaints lightweight.

## Safety and usability review in 2.1.3

- Cleaner selection bar with on-demand batch rename and live before/after previews. Bulk rename still requires confirmation.
- File rows show coloured preset tags, selection checkmarks, a large percentage, remaining time and approximate output size. Warning tags open the explanation in Details and also produce a notification.
- Settings include short visible descriptions, with advanced controls collapsed by default. Non-deleting actions use neutral colours. Convert beside original… remains a file-picker action, not a selection-bar action.
- History displays the saved destination path. The Conversion box shows the current video's remaining time and a learned, approximate queue encoding estimate.
- Make new preset… guides you through resolution, frame rate, encoder, bitrate, and naming. One-video settings include previews, resolution/frame rate, 1–200 Mbps video bitrate, MP4/MOV/MKV format, AAC/MP3/AC-3 audio and 64–320 kbps audio bitrate. MP3 uses stereo. The size estimate is approximate and needs duration and target rates.
- Change settings… is beside the queued video's preset dropdown and remains available in More… after conversion starts. Applying changed settings stops that video's old operation before restarting it; finished files are kept and new output uses no-overwrite naming. The original must still be available. Pause/Stop controls are respected, and duplicate review cannot be bypassed. Restart requests survive closing the app.
- Stronger duplicate protection during concurrent NAS imports, interrupted jobs, and repeated folder scans. Resume and Retry cannot bypass **Import anyway**.
- Restart recovery distinguishes verified imports and completed conversions from unfinished files. In-place recovery always keeps the original and does not send it through normal source cleanup.
- In-place results and rejected outputs kept for inspection are remembered independently of History, so clearing History or restarting does not make unchanged files eligible for automatic conversion again. Different recordings reusing the same names are still detected.
- A rejected recovered output can be retried as a new, uniquely named conversion without deleting the rejected file. Recovery actions wait until an old operation has finished stopping.
- Checks for readable source/output video, expected audio, and duration before completion or transfer. Long recordings no longer receive a multi-minute duration tolerance; the maximum difference is five seconds.
- Windows working-file cleanup checks and removes the same exclusively held file, preventing a replacement between verification and deletion. Failed copy verification keeps both files for inspection.
- The notification center keeps active drive notices alongside the latest three ordinary messages. Dismissed messages cannot return through a delayed callback.

See [the review and test notes](AUDIT_2.1.3.md) for coverage and remaining real-world checks.

## Main features

- One-at-a-time, checksum-verified imports from recording drives
- Per-drive import memory, so reconnecting a recording disk queues only files that are new or changed
- Automatic pickup and conversion of supported videos placed in Importfolder
- Completed-file cleanup quarantine that prevents the Importfolder watcher from converting the same working copy again
- Shared original-checksum memory on the NAS, so different ingest computers recognize the same recording even after the converted file is renamed or moved
- Per-file FFmpeg presets and safety warnings
- Compact and comfortable dashboard layouts, controlled from the sidebar
- Smooth inertial scrolling and a virtualized Dashboard for long queues
- Progressive History rendering so large histories do not block navigation
- Batched, bounded live logging that stays lightweight during long sessions
- Coalesced progress updates and faster background preview generation
- A collapsible full-width Log panel at the bottom of the app, toggled below Compact in the sidebar
- Bottom-right, non-blocking notifications with compact X controls and a floating latest-three notification center
- Notification controls shifted clear of the scrollbar, with Dismiss all for transient messages
- Persistent disk-ready feedback that remains active until the corresponding recording drive is disconnected
- A contextual selection bar that always shows how many files an action will affect and clears after batch actions
- Safe rename scoping: inline fields rename one file only, while multi-file rename requires an explicit preview and confirmation
- A separate file-picker command for converting beside the original; selecting Dashboard rows does not change its scope
- An actionable empty Dashboard plus visible start/completion feedback for manual Importfolder scans
- Visible success, unsupported-file, and cancellation feedback for file-picker and drag-and-drop imports
- A durable Import anyway override that bypasses duplicate detection until the requested import succeeds
- Screen-aware main/setup windows, including a scrollable setup wizard for smaller laptop displays
- Safer idle controls and history changes: Pause is disabled while stopped, and failed History saves roll back cleanly
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

The `Build Windows installer` GitHub workflow runs all tests, builds the executable and installer on Windows, creates the matching tag (for example `v2.1`), and publishes both files in a GitHub Release. If that version already exists, the release is left unchanged. Existing installations will find a newly published version during their next update check.

## File safety

MesterSync writes imports, conversions, and transfers to temporary files first. It verifies completed files and promotes them without replacing an existing destination. Final names are checked again immediately before each file is committed.

Exact duplicate protection is always on. Matching filenames are not treated as proof that two recordings are duplicates. MesterSync uses file size plus cached samples from the beginning, middle, and end as a fast filter. A sample mismatch proves the recordings differ; a possible match is always confirmed with a SHA-256 checksum of every byte. The name is never part of the duplicate decision, so renamed identical recordings are still found while different camera recordings named `hyperdeck_02.mp4` remain separate.

After a disk file has been copied and checksum-verified, MesterSync also remembers that file's volume identity, relative path, metadata, and sampled content. A rescan or reinsertion skips the unchanged file before it reaches the queue, while a new or changed file at the same location still receives the normal exact-content check. A brief Windows drive-detection gap is not treated as an eject/reinsert, and an active task can never be reset to the import stage by a drive rescan.

When a NAS folder is configured, MesterSync stores small per-record checksum entries under `.mestersync` in that NAS folder. This lets another MesterSync computer recognize an original that was imported elsewhere without trying to compare the converted video to the camera original. Existing local history is backfilled when it can account for the existing NAS media. If the NAS or its shared history cannot be checked safely, an unknown import pauses for review instead of proceeding; **Import anyway** remains the explicit override. The independent per-record layout prevents two ingest computers from replacing each other's history.

Different recordings receive unique names such as `recording.mov`, `recording_1.mov`, and `recording_2.mov`. Final converted names are reserved across both the output and NAS folders so simultaneous jobs cannot select the same name. Choosing **Import anyway** still uses a new unique name and never replaces an existing file. Legacy records without samples continue to receive the full check. If a possible match cannot be read completely, or the checksum database cannot be saved, the import stops for review or in an error state and keeps the recording-disk file and any verified local copy. macOS `._` sidecar files and known camera/system metadata folders are always ignored even if old settings omit those exclusions.

Run the core safety checks with:

```powershell
py -m unittest discover -s tests -v
```
