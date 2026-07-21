MesterSync

Run without terminal:
- Double-click the MesterSync shortcut in the main folder.

Notes:
- Main folder layout: MesterSync shortcut, app, data, and presets.
- app contains the program files, launcher script, and icons.
- data contains config, history, pending tasks, and thumbnail cache.
- presets contains FFmpeg preset .txt files.
- If the shortcut icon is missing after copying to another computer, run app\MesterSync.vbs once. It recreates the main-folder shortcut with the local icon path.
- The app version is defined inside MesterSync.pyw as APP_VERSION and is shown in the window title and Settings.
- FFmpeg must exist at the path configured inside Settings.
- First-run setup explains the workflow and asks for importfolder, output folder, optional NAS folder, FFmpeg.exe, and ignored drives. You can run it again from Settings.
- Drag-and-drop requires tkinterdnd2, but Add files works without it.
- Settings Health check validates folders, free space, FFmpeg, FFprobe, and the selected video encoder in the background so the interface remains responsive.
- Video thumbnails are taken from the middle of the video. Hover left-to-right over a thumbnail to preview ten frames from the video.
- Click a thumbnail to view it larger; move left-to-right to preview the same ten frames, and click again anywhere to close it.
- Dragged or added files appear immediately in the Dashboard while duplicate checks run.
- Keyboard shortcuts: Delete skips/removes selected rows, Ctrl+A selects all rows, Up/Down moves selection, Shift+Up/Down extends selection, Alt+Up/Down changes the selected row's priority, Space pauses/resumes, Enter opens the selected file, R retries stopped/error rows, Ctrl+Up moves a queued row to the top.
- In a rename box, Enter saves and exits the box. Esc cancels the text change and exits the box.
- Pending dashboard work is restored from mestersync_tasks.json after restarting the app.
- Duplicate review rows appear when checksum or same-name checks find a likely duplicate; use Import anyway when that is intentional.
- Preset safety warnings appear in Settings when FFmpeg args look risky, such as missing audio handling, unsupported encoders, or large-output settings.
- Preset test has its own sidebar page and converts a 15-second sample from the middle of a selected video. Test files are stored under data\preset_tests and never replace or modify the selected source. A successful new test automatically deletes previous test videos.
- Use Compact: On above Show log in the Dashboard sidebar to fit more active files on screen. This preference saves automatically.
- Dashboard rows show the safe final name MesterSync would choose right now. Final output/NAS filenames are checked again at the moment files are finalized so videos cannot overwrite each other.
- Matching filenames alone do not mean two videos are duplicates. MesterSync confirms duplicates by checksum; different recordings with the same name are kept separately using numbered filenames.
- Imports run one at a time. Use the row Up/Down controls or Alt+Up/Down to change which waiting video goes next.
- A green Disk ready to remove notice appears after every video from a detected recording disk has been copied and checksum-verified. Conversion and NAS transfer may continue afterward.
- Settings save automatically after a short pause.
- Imported and NAS copies are checksum-verified before MesterSync removes any local working file. Converted media is checked with FFprobe for readable video, duration, and expected audio.
