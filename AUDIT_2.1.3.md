# MesterSync 2.1.3 review

## Version 2.2 follow-up

### Resize and navigation performance correction, 13 October

Profiling reproduced multi-second stalls after the rounded-control changes. Small translucent ttk image tiles made Windows redraws expensive, and stacked hidden pages continued receiving layout callbacks. Select also destroyed and recreated every visible card on resize. Rounded controls now use wide opaque assets with precomposited corners and avoid reapplying unchanged styles. Inactive pages are unmapped but cached. Select retains visible card widgets, coalesces resize callbacks, delays preview refitting until geometry settles, and suppresses preview work while hidden. Dashboard rows retain their widgets and apply deferred progress on return; completed History pages are reused until records change.

An isolated 1200×900 Windows Tk stress fixture used 60 queued tasks, 200 History records (25 initially rendered), and 300 Select candidates with cached ten-frame sample previews. The same five-navigation/six-resize profile fell from 36.3 seconds to 3.3 seconds, including deliberate event-pump pauses. Ordinary Select/Settings revisits used approximately 0.07–0.17 seconds of UI work, compared with 2.4–2.9 seconds before; six Select resizes used 0.18–0.39 seconds each, compared with 3.0–4.5 seconds. Initial History rendering still creates cards (about 0.65 seconds); a separate Dashboard run confirmed History reuse and no Select renders while hidden. These are local synthetic timings, not guarantees for other computers or media.

Regression checks verify card identity across resize and navigation, preserved selection/presets, idle hidden previews, reused History widgets, and deferred Dashboard progress without row replacement. All **229 tests passed with no skips**. The existing full suite continues to cover native controls, compact density, imports, conversion, transfer, and recovery. This is a source update; no executable or release was installed or published.

### Rounded controls and density correction

Moved the Log panel to the bottom of the main window and reduced the top control bar to 55 pixels in the isolated desktop layout. The default preset label and selector now share one row. Added scalable rounded ttk surfaces for buttons, rename/numeric entries, dropdowns, queue-number badges, and affix toggles; the status box and Dashboard rows also use rounded panels. Native control behavior is retained, including readonly fields, dropdown popups, disabled states, focus, and keyboard activation. Controls refresh their backdrop when a task changes color. Narrow arrow-free scrollbars have a rounded thumb, hover feedback, and a muted full-range state.

Compact mode previously left the large percentage and four-line estimate dominating row height. It now uses smaller controls and spacing, a two-line estimate alongside the progress bar, and a smaller percentage while retaining warnings, presets, and recovery actions. The same two sample Dashboard rows measured 276 pixels each in Comfortable and 182 pixels each in Compact (about 34% shorter). Select cards also tighten their controls and metadata spacing while retaining the thumbnail aspect ratio. Virtual row spacing was updated to match the new layouts.

Expanded GUI checks verify an actual reduction in whole-row height, bounded top-bar height, bottom placement of Log, native dropdown posting, native scrollbar press/drag handlers, and keyboard activation without also toggling Pause. The normal regression suite continues to exercise renaming, preset overrides, conversions, recovery, duplicates, settings, and History. Desktop captures verified both densities and the bottom log panel using temporary settings and sample images.

Final styling verification: **229 tests passed with no skips**; Git whitespace checks passed. The source remains version 2.2. No installed executable, release, or production recording was modified.

The mode switch now controls what happens when a new recording device is detected: Auto imports, while Select automatically opens the embedded review. Choosing Select alone keeps the normal Dashboard visible. Cancel dismisses review and releases candidate reservations without creating tasks, allowing a later reconnect to offer the recordings again. After a successful selection submission, the app returns to normal processing; unselected recordings stay on the device. Explicit manual imports and watched-folder processing retain their usual Dashboard behavior.

Review now scrolls continuously instead of using pages. Rows are virtualized to keep large device lists light, and selection and preset overrides survive scrolling and resizing. Card backgrounds, borders, names, and metadata toggle selection; thumbnails and dropdowns retain their own actions. Controls share one toolbar row, with horizontal scrolling on narrow displays. The two requested instruction lines and Active queue/Select videos navigation were removed.

Thumbnail boxes use Pillow's aspect-preserving, centered resize/crop at the actual widget dimensions, including enlargement. Pillow 12.3.0 is declared in runtime requirements and imported normally so the installer can collect it. Decoded source images and resized Tk images have bounded caches. Cancelled preview work stops between frame extractions, and late results carry session tokens so they cannot overwrite previews for a reconnected device. A mode switch while a drive-scan GUI event is pending follows the current mode without leaving invisible held candidates.

The app version, installer default, bundled help, README, and version-consistency test are now 2.2. Verification covers exact filled-thumbnail dimensions and pixels across small, large, and portrait boxes; card clicks and thumbnail exclusions; device-triggered opening, Cancel, continuous scrolling through 214 candidates, preserved selections and overrides, normal manual/watched-folder behavior, stale preview results, and pending scan mode changes. Window-specific captures at 800, 1000, and 1500 pixels confirm the responsive grid and single-line toolbar. This is a source update; no release or installed executable was changed.

Final 2.2 verification: **229 tests passed with no skips**, and Git whitespace checks passed. Added review-session guards also prevent a manually added batch or its delayed GUI event from importing after Cancel. The longer GUI fixture now cancels unrelated startup timers while retaining its checks that user interface actions do not start background work unexpectedly.

## Select-mode review, 13 October 2026

### Embedded review and interface follow-up

Select review now occupies the main Dashboard rather than a popup, with an Active queue link and a return action. A keyboard-accessible, centered Auto/Select switch replaces the separate radio row. Log moved below Compact and opens a full-width panel. Rounded native-Tk panels, a softer palette, consistent dark dropdowns, and responsive two/three-column cards improve both modes without adding runtime dependencies. Stage cards hide during selection to leave room for video previews and return with the processing queue.

Group presets apply only to checked candidates and preserve explicit per-video overrides. Each overridden card has a Use group action. Changing a preset updates its label without rebuilding the cards or losing focus. The group command reports how many videos changed and how many overrides were retained.

Additional UX fixes: discoveries no longer steal navigation focus; unavailable presets explain the problem inline; empty and paginated states have clear actions; navigation disables at page boundaries; Ctrl+A/Escape operate on candidates while queue shortcuts cannot affect hidden jobs. Window-specific captures at 1000 and 1500 pixels confirmed two/three columns and exposed a Notifications button overlapping the import action. Notifications now live in the header. The Auto view also exposed a clipped Prioritize import control at narrower widths; import actions now have their own row.

Added a saved Auto/Select setting and an explicit intake gate across recording drives, manual/drop imports, and the watched Importfolder. Candidates remain outside the runnable/persisted task queue until confirmation. Per-item presets are captured before task creation, preserving container naming and existing duplicate, copy, conversion, and transfer checks. Switching to Auto does not release candidates already held in this session. Missing sources remain in review for retry; duplicate-check failures become recoverable task errors.

Performance improvements in the new review: twelve visible cards per page, one background preview worker, a bounded image cache, one preset-directory listing per page, one default-preset read per incoming batch, batched watched-folder discovery notifications, and no shared-history synchronization for manual candidates before selection. The existing Dashboard virtualization, progressive History, bounded logs, and coalesced progress updates remain in place.

Verification includes the full regression suite covering import/conversion/transfer, duplicates and concurrency, recovery and retained media, renaming, diagnostics, updates/installer behavior, settings, notifications, and actual Tk widgets. Added selection-specific checks cover all intake paths, no pre-confirmation work, rescan/mode-switch protection, distinct per-video presets, missing sources, duplicate blocking, error recovery, pagination, selection counts, thumbnail display, and fullscreen. The GUI checks found and fixed a Windows Tk incompatibility between borderless popup windows and the fullscreen flag.

Final result: **223 tests passed with no skips**; Git whitespace checks passed. Tests ran with the bundled Python runtime and a temporary directory inside the workspace because the system Python launcher and default sandbox temporary directory were unavailable.

Limits: automated tests use isolated temporary settings and disposable media. They do not establish reliability for physical drive removal, live NAS outages, long-running GPU jobs, or installation on another computer. This change updates source only. Unconfirmed selections are session-only.

Suggested follow-ups, not implemented:

- Save review sessions, including checked items and per-file presets, across restarts.
- Add duration/resolution/frame-rate filters and capture-date sorting to the review grid.
- Add metadata-based preset rules (the requested manual group-preset feature is now implemented).
- Reuse review thumbnails in processing rows and investigate extracting the filmstrip with fewer FFmpeg launches; measure on long recordings and slow drives before changing seek behavior.

## Pre-commit verification, 16 September 2026

This follow-up used a fresh isolated Python environment containing only the declared runtime/build requirements. It found and corrected three issues:

- The one-video editor depended on undeclared Pillow. Its previews now use the existing Tk image support; the interface test also runs with Pillow explicitly unavailable and exercises the real drag-and-drop root.
- Stop all and stopping Conversion did not reliably cancel forced in-place work during its countdown or preparation. Cancellation now covers queued and active conversions, is checked before FFmpeg starts and after it exits, and survives saving/restoring a pending settings restart. An explicit resume resets cancellation. Stopping a pending in-place settings change no longer grants it permission to run while Conversion is stopped.
- Container duration alone could accept prematurely ended video when audio continued for the full recording. Where FFprobe exposes video-stream duration, it is now checked separately. A real MP4 regression with full-length audio and truncated video is rejected.

Verification: **214 tests passed with no skips** in the clean environment, including real CPU conversions, all advertised audio/container combinations, the Windows interface, duplicate/concurrency checks, recovery, checksum cleanup, and six new release regressions. A real FFmpeg cancellation test confirmed that the original remained byte-for-byte unchanged and only temporary output was removed. Syntax checks for 43 Python/spec files, Git whitespace checks, and dependency consistency checks passed.

The Windows executable was built successfully with PyInstaller. A hidden launch using empty temporary settings/folders reached its GUI event loop and stayed running, without a startup error. The test process was then terminated. A separate 1.5-second disposable recording converted successfully using the default NVIDIA HEVC preset on this computer; output video/audio/duration checks passed and source bytes were unchanged.

These results do not prove every hardware/failure combination. A full installer build/install/update on another PC, physical drive removal during writing, live NAS/SMB failure, and multi-hour GPU conversions were not exercised. Media validation checks reported streams and durations; it is not a full frame-by-frame decode or visual quality review. No production recordings or saved user settings were modified, and no commit, push, release or application installation was performed.

Reviewed 7 September 2026. Changes are in the source; no installed executable, real recording, or production NAS data was changed during this review.

## Main corrections

- **Repeated work:** closed additional races between folder scanning, file promotion, active jobs, and completion. In-place originals/results and rejected outputs kept for inspection now have persistent unchanged-file records separate from visible History. Clearing History or restarting does not make those unchanged files automatic inputs again. A different recording can reuse the same filename.
- **Duplicate approvals:** Retry, Resume, restart recovery, and conversion/transfer queues cannot bypass duplicate review. Only Import anyway supplies the override. Fixed a verified duplicate left waiting after its import batch had already released other files for conversion.
- **NAS duplicate history:** concurrent computers merge updates under the same per-record lock. Unreadable folders and missing initialized safety records cannot be mistaken for an empty, fully checked archive. Old checksum records remain usable.
- **Recovery:** persisted verification evidence distinguishes completed imports/conversions from interrupted files. A stopped in-place conversion cannot enter a finalizer that deletes its original. Recovery waits for an old operation to finish stopping. A rejected recovered output stays on disk, while explicit Retry can produce a new uniquely named conversion when the source is readable.
- **File preservation:** failed import/NAS copy verification leaves the files for inspection. On Windows, working-copy cleanup hashes and deletes using the same exclusively held file handle; a replacement between checking and deleting is blocked. Temporary files created by the app remain eligible for normal temporary-file cleanup.
- **Media checks:** completed/recovered outputs are checked before finalization or transfer. Missing source information, unreadable duration, missing video, and unexpectedly absent audio fail the check. Long-concert duration tolerance is capped at five seconds instead of growing to minutes.
- **Usability:** active drive notices remain in the notification center alongside the latest three ordinary messages. Dismissed messages cannot return through an untracked delayed callback. A manual in-place conversion can start immediately even when many normal queued conversions are stopped.
- **Input readiness:** same-size files still being written are no longer considered stable just because their size is unchanged. Camera metadata and unfinished `.mestersync_` media are excluded from every import route.

## Duplicate detection: what it can and cannot prove

Names are not proof of a duplicate. Size and cached beginning/middle/end samples cheaply rule out different originals. Possible exact matches are confirmed by hashing all bytes with SHA-256. Tests cover both different recordings sharing a filename and identical originals with different names.

Converted video normally has different bytes from its camera original. MesterSync therefore remembers the original checksum and its final destination locally and in the shared NAS ledger. It does not visually compare recordings, identify the same performance across cameras, or recognize independently re-encoded copies by their pictures. Previously converted NAS videos with no surviving original-checksum history cannot be reliably matched back to camera originals; incomplete archive coverage pauses unknown imports for review.

Import anyway overrides duplicate blocking, not no-overwrite naming, available disk space, permissions, or checksum/media safety checks.

## Verification

Safety-review result: **185 tests passed with no skips** on the local Windows run, including 58 additional regression checks. Python compilation and Git whitespace checks also passed.

The follow-up UI simplification adds on-demand batch rename, live filename previews, Details and More menus, operation-specific recovery labels, collapsed Advanced settings, optional field help, and neutral colours for non-deleting actions. The expanded Windows GUI test exercises the new controls, checks that rename still requires confirmation, and confirms hidden Advanced settings keep their values. Full diagnostic text remains available even when a row shows a short summary.

Follow-up UI verification on 8 September: **188 tests passed with no skips**, including the expanded Windows interface test. Compilation and whitespace checks passed.

The automated suite includes unit, failure-simulation, concurrency, and workflow tests. Disposable real recordings were generated and processed with the local FFmpeg/FFprobe installation. The real-media tests exercised import, conversion, transfer, cleanup, re-scanning, and in-place conversion with Transfer enabled. Original recording-disk bytes stayed unchanged.

The actual Tk interface was constructed in a hidden test window with temporary settings and background/network operations disabled. Dashboard, History, all five Settings categories, notification controls, and the Log panel were exercised without constructor or callback errors. This test needs Windows GUI-library access; restricted environments may skip it. Real-media tests skip when FFmpeg is unavailable.

### Follow-up controls and guidance, 8 September

- Restored short visible Settings explanations. Removed the selection-bar Convert beside original action, moved Details into More, restored coloured preset tags, and kept selection checkmarks.
- Added current-video and queue encoding estimates to the Conversion box. Queue estimates use measured speed for matching conversion settings and known video durations. Unknown work stays marked as estimating; paused/stopped work is identified. Estimates exclude importing and NAS transfer and are not guarantees.
- History shows the recorded output path in a selectable, read-only field.
- Make new preset guides users through picture size/frame rate, encoding/target bitrate, and review/name. Saving does not change the default preset or queued videos, and cannot overwrite an existing preset.
- The initial one-video editor changed resolution, frame rate and target bitrate before conversion started. The later follow-up below expands this to active conversions and audio/container settings. Stale editors and failed initial queue saves leave existing settings intact. Unsupported advanced filter combinations are rejected instead of silently rewritten.
- Guided scaling preserves aspect ratio with padding, and Mbps specifies a target rather than an exact constant bitrate; see the official [FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html) and [FFmpeg codecs](https://www.ffmpeg.org/ffmpeg-codecs.html) documentation.

Final follow-up verification: **202 tests passed with no skips**. Compilation and whitespace checks passed. Tests include cancelling the editor, duplicate preset names, per-video persistence and isolation, queue estimates, and a disposable real 4K/25 fps CPU conversion with a 30 Mbps target. The source test video remained unchanged. GPU encoding and full-length concert timings were not benchmarked.

### Progress, warnings and active settings follow-up

- Continue conversion replaces Finish conversion. Current-video ETA is left-aligned beside the Conversion box percentage with matching typography. Dashboard rows show a large percentage alongside estimated time and output size.
- Preset warning tags are clickable, Details lists the reasons, and notifications explain them once per video/settings combination per session. Worker-start notifications also cover rows outside the visible list. A target of 50 Mbps or higher triggers the existing large-file advisory; it is not proof of a failed conversion.
- One-video settings show available thumbnails and add MP4/MOV/MKV containers, AAC/MP3/AC-3 audio, 64–320 kbps audio rates and a 1–200 Mbps video target. MP3 explicitly uses stereo. Size is a target-rate estimate, not a promised size. The dialog explains restarting and preserves unrelated settings.
- Change settings is available beside an unlocked preset dropdown and in More even after conversion starts. A persisted pending request cancels the old operation, waits for worker ownership to end, remembers retained finished outputs, clears stale output state and queues new settings. Archive/cleanup cannot remove a task with a pending request. An already-popped old queue ticket claims its replacement too, preventing a second conversion. Missing originals or persistence errors leave work stopped with an explanation; Pause/Stop and duplicate review remain in force.
- Regression suite expanded to **208 tests**, including restart handover, save failures, pending-request persistence, archive cleanup guards, warning deduplication, real thumbnails and all nine advertised audio/container combinations on disposable media. Each real output was probed for its selected audio codec and frame rate; source bytes stayed unchanged. Format choices were checked against the official [FFmpeg formats](https://ffmpeg.org/ffmpeg-formats.html) and [codecs](https://ffmpeg.org/ffmpeg-codecs.html) references.

Run the suite with:

```powershell
py -m unittest discover -s tests -v
```

## Performance and remaining checks

The added media checks read container/stream information; they do not encode the concert again. Unchanged NAS checksum records avoid unnecessary writes, and repeated unchanged-file checks use cached samples. Full checksums still require reading the video and can take noticeable time on slow drives or networks. Short synthetic tests are not a benchmark for multi-hour concerts or a particular GPU.

The following were **not** exercised against production hardware: physical disk removal during writes, NAS power loss or intermittent SMB connectivity, long-concert encoding with the user's selected GPU preset, and installing/updating a newly built executable on another PC. Failure simulations help cover these paths but cannot guarantee all hardware conditions. Keep recording disks/backups until the resulting video has been checked.

No existing duplicate recordings were deleted or consolidated. The review prevents identified repetition paths; it does not retroactively prove which earlier files are duplicates.

## Delivering the update

The source and installer version are 2.1.3. Commit and push to `main` using GitHub Desktop when ready. The existing GitHub workflow runs tests and builds/publishes the installer if release `v2.1.3` does not already exist. This review did not push, publish, or install an update.
