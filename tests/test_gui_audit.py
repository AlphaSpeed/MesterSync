"""Exercise actual Tk widgets using an invisible, fully isolated app instance."""

import copy
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import traceback
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

try:
    import tkinter as tk
except ImportError:
    tk = None

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))


@unittest.skipIf(tk is None, "Tkinter is unavailable")
class GuiAuditTests(unittest.TestCase):
    def test_hidden_app_views_settings_and_notifications(self):
        loader = importlib.machinery.SourceFileLoader("mestersync_gui_audit", str(APP_DIR / "MesterSync.pyw"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        app_module = importlib.util.module_from_spec(spec)
        loader.exec_module(app_module)
        import storage

        try:
            root = app_module.TkinterDnD.Tk() if app_module.TkinterDnD is not None else tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk cannot create a test window: {exc}")
        root.withdraw()
        callback_errors = []
        root.report_callback_exception = lambda *error: callback_errors.append("".join(traceback.format_exception(*error)))

        try:
            with tempfile.TemporaryDirectory() as value, ExitStack() as patches:
                folder = Path(value)
                config = copy.deepcopy(app_module.DEFAULT_CONFIG)
                for key, name in (("input_folder", "imports"), ("output_folder", "output"), ("nas_folder", "nas"), ("preset_folder", "presets")):
                    path = folder / name
                    path.mkdir()
                    config[key] = str(path)
                ffmpeg = folder / "ffmpeg.exe"
                ffmpeg.write_bytes(b"GUI test placeholder; never executed")
                config.update(ffmpeg_path=str(ffmpeg), checksum_database_path=str(folder / "checksums.json"), setup_complete=True)
                patches.enter_context(mock.patch.dict(os.environ, {"MESTERSYNC_DATA_DIR": str(folder / "data"), "MESTERSYNC_MIGRATE_FROM": ""}))
                patches.enter_context(mock.patch.object(storage, "_legacy_storage_roots", return_value=[]))
                patches.enter_context(mock.patch.object(storage, "_storage_setup_complete", False))
                patches.enter_context(mock.patch.object(app_module, "load_config_data", return_value=config))
                patches.enter_context(mock.patch.object(app_module, "load_history_records", return_value=[]))
                patches.enter_context(mock.patch.object(app_module, "load_task_records", return_value=[]))
                patches.enter_context(mock.patch.object(app_module, "load_checksum_records", return_value={}))
                patches.enter_context(mock.patch.object(app_module, "list_windows_drives", return_value=["C:"]))
                patches.enter_context(mock.patch.object(root, "bell"))
                for method in ("ensure_thumbnail_worker_running", "ensure_thumbnail_display_worker_running", "ensure_checksum_worker_running", "check_for_updates", "cleanup_thumbnail_cache", "set_sleep_prevention", "tick"):
                    patches.enter_context(mock.patch.object(app_module.MesterSyncApp, method))
                forbidden_calls = []
                for owner, method in ((app_module.threading.Thread, "start"), (app_module.subprocess, "run"), (app_module.subprocess, "Popen"), (app_module, "fetch_latest_release"), (app_module, "list_candidate_drives"), (app_module.MesterSyncApp, "run_setup_wizard")):
                    forbidden_calls.append(patches.enter_context(mock.patch.object(owner, method, side_effect=AssertionError(f"GUI smoke test must not invoke {method}"))))

                app = app_module.MesterSyncApp(root)
                # This fixture drives updates explicitly; startup cleanup/check
                # timers must not launch unrelated work during longer UI checks.
                for after_id in root.tk.call("after", "info"):
                    root.after_cancel(after_id)
                self.assertEqual(root.state(), "withdrawn")
                self.assertEqual(app.current_tab, "dashboard")
                self.assertFalse(app.settings_built)
                self.assertEqual(app.tasks, {})
                self.assertFalse(app.started)
                self.assertFalse(app.stage_cards["import"]["cancel_button"].winfo_manager())
                root.tk.call("ttk::combobox::Post", app.top_preset_combo._w)
                root.update_idletasks()
                popdown = root.tk.call("ttk::combobox::PopdownWindow", app.top_preset_combo._w)
                self.assertTrue(root.tk.call("winfo", "ismapped", popdown))
                root.tk.call("ttk::combobox::Unpost", app.top_preset_combo._w)

                # Mode choice alone leaves the dashboard visible. A detected
                # device opens review, without creating any processing work.
                app.mode_switch.choose("select")
                self.assertFalse(app.select_view)
                self.assertIsNone(app.import_selection)
                app.import_selection = app_module.ImportSelection(app)
                review = app.import_selection
                self.assertIs(review.frame, app.selection_frame)
                self.assertEqual(review.frame.winfo_toplevel(), root)
                self.assertEqual(app.mode_switch.master.grid_size()[0], 3)
                self.assertEqual(app.log_toggle.master, app.compact_dashboard_btn.master)
                self.assertIs(app.notification_history_button.master, app.header_frame)
                preview = folder / "selection_preview.png"
                preview_image = tk.PhotoImage(width=444, height=250)
                preview_image.put("#4588bb", to=(0, 0, 444, 250))
                preview_image.write(str(preview), format="png")
                paths = [folder / f"review_{i}.mp4" for i in range(14)]
                for path in paths:
                    review.previews[app_module.path_key(path)] = [str(preview)] * 10
                review.add((paths, {"source_label": "Test card", "disk_batch": True, "source_id": "test-device"}))
                root.update_idletasks()
                self.assertTrue(app.select_view)
                self.assertLessEqual(len(review.labels), len(paths))
                self.assertEqual(review.feedback.get(), "")
                self.assertEqual(review.add_button.master, review.group_combo.master)
                self.assertEqual(str(review.submit.cget("state")), "disabled")
                review.select_all(True)
                self.assertEqual(review.submit.cget("text"), "Import selected (14)")
                first_key = app_module.path_key(paths[0])
                second_key = app_module.path_key(paths[1])
                review.items[first_key][3].set("Individual")
                review.mark_override(first_key)
                review.items[second_key][2].set(False)
                review.group_preset.set("Group A")
                review.apply_group_preset()
                self.assertEqual(review.items[first_key][3].get(), "Individual")
                self.assertEqual(review.items[second_key][3].get(), "Current settings")
                self.assertEqual(review.items[app_module.path_key(paths[2])][3].get(), "Group A")
                review.group_preset.set("Group B")
                review.apply_group_preset()
                self.assertEqual(review.items[first_key][3].get(), "Individual")
                review.use_group(first_key)
                self.assertEqual(review.items[first_key][3].get(), "Group B")
                self.assertNotIn(first_key, review.override_keys)
                from types import SimpleNamespace
                review.resize(SimpleNamespace(width=1050))
                review.render()
                self.assertEqual(review.columns, 3)
                review.resize(SimpleNamespace(width=700))
                review.render()
                self.assertEqual(review.columns, 2)
                review.canvas.yview_moveto(1)
                root.update_idletasks()
                review.render(force=False)
                self.assertIn(app_module.path_key(paths[-1]), review.labels)
                review.select_all(False)
                self.assertEqual(str(review.submit.cget("state")), "disabled")
                with mock.patch.object(app, "selected_editing_text", return_value=False):
                    app.dispatch_keyboard_shortcut(None, app.select_all_tasks)
                    self.assertTrue(all(item[2].get() for item in review.items.values()))
                    app.dispatch_keyboard_shortcut(None, app.clear_task_selection)
                    self.assertFalse(any(item[2].get() for item in review.items.values()))
                    with mock.patch.object(app, "toggle_pause") as pause:
                        app.dispatch_keyboard_shortcut(None, app.toggle_pause_shortcut)
                        pause.assert_not_called()
                key = app_module.path_key(paths[-1])
                review.toggle_card(key)
                self.assertTrue(review.items[key][2].get())
                review.cards[key].inner.event_generate("<Button-1>")
                self.assertFalse(review.items[key][2].get())
                review.display(key, 9)
                self.assertTrue(review.labels[key].cget("image"))
                image_name = review.labels[key].cget("image")
                self.assertEqual(root.tk.call("image", "width", image_name), review.labels[key].winfo_width())
                self.assertEqual(root.tk.call("image", "height", image_name), review.labels[key].winfo_height())
                test_label = tk.Label(root)
                for width, height in ((137, 77), (720, 405), (480, 640), (1920, 1080)):
                    self.assertTrue(review.cache.configure_cover(test_label, str(preview), width, height))
                    self.assertEqual(root.tk.call("image", "width", test_label.cget("image")), width)
                    self.assertEqual(root.tk.call("image", "height", test_label.cget("image")), height)
                    self.assertEqual(root.tk.call(str(test_label.image), "get", width-1, height-1), (69, 136, 187))
                test_label.destroy()
                review.fullscreen(key)
                self.assertIsNotNone(review.popup.popup)
                self.assertFalse(review.items[key][2].get())
                review.popup.close()
                more_paths = [folder / f"large_review_{i}.mp4" for i in range(200)]
                for path in more_paths:
                    review.previews[app_module.path_key(path)] = [str(preview)] * 10
                review.add((more_paths, {"source_label": "Second card", "disk_batch": True}))
                root.update_idletasks()
                self.assertEqual(len(review.items), 214)
                self.assertLess(len(review.cards), 50)
                review.select_all(True)
                review.canvas.yview_moveto(1)
                root.update_idletasks()
                review.render(force=False)
                self.assertIn(app_module.path_key(more_paths[-1]), review.cards)
                self.assertTrue(all(item[2].get() for item in review.items.values()))
                review.canvas.yview_moveto(0)
                root.deiconify()
                root.update()
                # Resizing reuses visible cards and keeps the selection model.
                review.render()
                original_cards = dict(review.cards)
                root.geometry(f"{root.winfo_width()+20}x{root.winfo_height()}")
                root.update_idletasks()
                review.render()
                root.update_idletasks()
                retained = set(original_cards) & set(review.cards)
                self.assertTrue(retained)
                self.assertTrue(all(review.cards[k] is original_cards[k] for k in retained))
                self.assertTrue(all(item[2].get() for item in review.items.values()))
                self.assertEqual(review.items[first_key][3].get(), "Group B")
                # Hidden pages remain cached and do not resize preview images.
                app.show_tab("history")
                root.update_idletasks()
                self.assertEqual(review.frame.winfo_manager(), "")
                self.assertEqual(app.dashboard_frame.winfo_manager(), "")
                hidden_cards = dict(review.cards)
                with mock.patch.object(review.cache, "configure_cover") as cover:
                    root.geometry(f"{root.winfo_width()+10}x{root.winfo_height()}")
                    root.update_idletasks()
                    review.render()
                    review.refresh_preview(next(iter(hidden_cards)))
                    cover.assert_not_called()
                self.assertEqual(review.cards, hidden_cards)
                app.show_tab("dashboard")
                root.update_idletasks()
                review.render()
                self.assertTrue(retained & set(review.cards))
                self.assertTrue(all(review.cards[k] is original_cards[k] for k in retained & set(review.cards)))
                self.assertTrue(all(item[2].get() for item in review.items.values()))
                scroll = next(w for w in review.canvas.master.winfo_children() if isinstance(w, app_module.ttk.Scrollbar))
                x = max(1, scroll.winfo_width()//2)
                thumb_y = [y for y in range(scroll.winfo_height()) if str(scroll.identify(x, y)).endswith("thumb")]
                self.assertTrue(thumb_y, f"Scrollbar {(scroll.winfo_width(), scroll.winfo_height(), scroll.get())}: {set(str(scroll.identify(x, y)) for y in range(scroll.winfo_height()))}")
                y = thumb_y[len(thumb_y)//2]
                before_scroll = review.canvas.yview()[0]
                self.assertIn("ttk::scrollbar::Press", root.bind_class("TScrollbar", "<ButtonPress-1>"))
                # Exercise the actual Tk drag handlers without relying on the
                # foreground application's delivery of synthetic mouse events.
                root.tk.call("ttk::scrollbar::Press", scroll._w, x, y)
                root.tk.call("ttk::scrollbar::Drag", scroll._w, x, y+70)
                root.tk.call("ttk::scrollbar::Release", scroll._w, x, y+70)
                self.assertGreater(review.canvas.yview()[0], before_scroll)
                root.withdraw()
                app.selection_path_keys.update(review.items)
                review.cancel_button.invoke()
                self.assertFalse(app.select_view)
                self.assertFalse(review.items)
                self.assertFalse(app.selection_path_keys)
                app.show_import_selection()
                self.assertFalse(app.select_view)
                app.mode_switch.choose("auto")
                self.assertEqual(app.get_config()["import_mode"], "auto")
                self.assertEqual(app.tasks, {})

                app.tab_buttons["history"].invoke()
                root.update_idletasks()
                self.assertEqual(app.current_tab, "history")
                self.assertTrue(app.history_widgets)

                app.tab_buttons["settings"].invoke()
                # Run the actual lazy builder without waiting for its timer.
                app.finish_lazy_settings_build()
                root.update_idletasks()
                self.assertTrue(app.settings_built, "Settings builder caught an error: " + "\n".join(app.log_entries))
                self.assertFalse(app.settings_building)
                history_widgets = tuple(app.history_widgets)
                app.show_tab("history")
                root.update_idletasks()
                self.assertEqual(tuple(app.history_widgets), history_widgets)
                app.show_tab("settings")
                for key, button in app.settings_category_buttons.items():
                    button.invoke()
                    root.update_idletasks()
                    self.assertEqual(app.settings_categories[key].winfo_manager(), "pack")
                app.set_config_from_ui()
                self.assertEqual(app.get_config()["input_folder"], config["input_folder"])
                self.assertEqual(app.get_config()["ffmpeg_args"], app_module.DEFAULT_FFMPEG_ARGS)

                app.show_disk_ready({"drive_id": "test-disk", "root": str(folder / "imports"), "text": "Test disk is ready to remove"})
                for index in range(4):
                    app.show_notification(f"Test notice {index}", "info")
                app.notification_history_button.invoke()
                root.update_idletasks()
                self.assertTrue(app.notification_history_open)
                self.assertFalse(app.notification_active)
                self.assertIn("test-disk", app.persistent_notifications)
                app.notification_dismiss_all_button.invoke()
                self.assertFalse(app.notification_queue)
                self.assertIn("test-disk", app.persistent_notifications)
                app.notification_history_button.invoke()
                app.hide_disk_ready("test-disk")
                self.assertFalse(app.persistent_notifications)

                app.tab_buttons["dashboard"].invoke()
                # Real widgets for selection, on-demand rename, compact rows,
                # action menus and technical details, with no media work.
                patches.enter_context(mock.patch.object(app, "apply_thumbnail"))
                first = app_module.TaskState("first", folder / "one.mp4", "one.mp4", rename_base="one", local_input_path=folder / "one.mp4", stage=app_module.STAGE_QUEUED)
                second = app_module.TaskState("second", folder / "two.mp4", "two.mp4", rename_base="two", stage=app_module.STAGE_QUEUED)
                app.tasks = {first.task_id: first, second.task_id: second}
                app.create_task_row(first)
                app.create_task_row(second)
                first_row = app.row_widgets[first.task_id]["outer"]
                app.show_tab("settings")
                first.progress = 37
                app.render_task(first.task_id)
                self.assertIs(app.row_widgets[first.task_id]["outer"], first_row)
                self.assertIn(first.task_id, app.deferred_dashboard_task_ids)
                app.show_tab("dashboard")
                root.update_idletasks()
                self.assertIs(app.row_widgets[first.task_id]["outer"], first_row)
                self.assertEqual(app.row_widgets[first.task_id]["progress_text"].cget("text"), "37%")
                first_row.destroy()
                app.refresh_dashboard_rows()
                packed_rows = [w for w in app.dashboard_inner.pack_slaves()
                               if w in {app.row_widgets[first.task_id]["outer"], app.row_widgets[second.task_id]["outer"]}]
                self.assertEqual(packed_rows, [app.row_widgets[first.task_id]["outer"], app.row_widgets[second.task_id]["outer"]])
                app.compact_dashboard = False
                app.apply_dashboard_density()
                root.update_idletasks()
                comfortable_height = app.row_widgets[first.task_id]["outer"].winfo_reqheight()
                app.compact_dashboard = True
                app.apply_dashboard_density()
                root.update_idletasks()
                compact_height = app.row_widgets[first.task_id]["outer"].winfo_reqheight()
                self.assertLess(compact_height, comfortable_height-35)
                self.assertLessEqual(compact_height, app.dashboard_row_extent())
                self.assertLessEqual(app.top_control_bar.winfo_reqheight(), round(64*root.winfo_fpixels("1i")/96))
                from types import SimpleNamespace
                with mock.patch.object(app, "toggle_pause") as pause:
                    app.dispatch_keyboard_shortcut(SimpleNamespace(widget=app.start_btn), app.toggle_pause_shortcut)
                    pause.assert_not_called()
                # State changes must recolour the whole passive surface even
                # when the progress percentage has not changed.
                surface_keys = ("card", "right", "top", "progress_wrap", "progress_text", "estimate_text", "preset_holder", "preset_label")
                for stage, background in ((app_module.STAGE_CONVERTING, app.DARK_GREEN),
                                          (app_module.STAGE_CONVERTING_IN_PLACE, app.DARK_GREEN),
                                          (app_module.STAGE_ERROR, "#3a1018"),
                                          (app_module.STAGE_QUEUED, app.CARD)):
                    first.stage = stage
                    app.render_task(first.task_id)
                    for key in surface_keys:
                        self.assertEqual(app.row_widgets[first.task_id][key].cget("bg"), background, (stage, key))
                app.selected_ids = [first.task_id, second.task_id]
                app.update_batch_box_visibility()
                self.assertEqual(app.selection_summary_var.get(), "2 selected")
                self.assertFalse(hasattr(app, "selection_convert_btn"))
                self.assertFalse(app.batch_rename_row.winfo_manager())
                app.selection_rename_btn.invoke()
                self.assertEqual(app.batch_rename_row.winfo_manager(), "pack")
                app.batch_name_var.set("concert")
                self.assertIn("one.mp4 → concert_01.mp4", app.batch_hint_var.get())
                self.assertIn("two.mp4 → concert_02.mp4", app.batch_hint_var.get())
                app.batch_apply_btn.invoke()
                self.assertEqual(first.rename_base, "one")
                app.update_batch_box_visibility()
                self.assertEqual(app.batch_apply_btn.cget("text"), "Confirm rename 2 files")
                app.batch_name_var.set("show")
                self.assertIsNone(app.batch_rename_confirmation)
                app.batch_apply_btn.invoke()
                app.batch_apply_btn.invoke()
                self.assertEqual(first.rename_base, "show_01")
                self.assertEqual(second.rename_base, "show_02")
                self.assertFalse(app.selected_ids)
                app.selected_ids = [first.task_id, second.task_id]
                app.update_batch_box_visibility()
                self.assertFalse(app.batch_rename_row.winfo_manager())

                first.stage = app_module.STAGE_ERROR
                first.detail = "FFmpeg failed: full technical error"
                first.error_log = "Original diagnostic text"
                app.render_task(first.task_id)
                row = app.row_widgets[first.task_id]
                self.assertEqual(row["retry_btn"].cget("text"), "Retry conversion")
                self.assertEqual(row["detail"].cget("text"), "Conversion failed. See Details.")
                self.assertFalse(row["times"].winfo_manager())
                self.assertEqual(row["preset_badges"].winfo_manager(), "pack")
                self.assertGreater(len(row["preset_badges"].winfo_children()), 0)
                self.assertFalse("details_btn" in row)
                self.assertFalse(row["copy_error_btn"].winfo_manager())
                self.assertEqual(row["retry_btn"].winfo_manager(), "pack")
                with mock.patch.object(tk.Menu, "tk_popup"), mock.patch.object(tk.Menu, "grab_release"):
                    app.show_task_menu(first.task_id)
                menu = row["context_menu"]
                self.assertTrue(menu.winfo_exists())
                self.assertEqual(menu.entrycget(0, "label"), "Details")
                self.assertEqual(menu.entrycget(1, "label"), "Change conversion settings…")
                self.assertEqual(menu.entrycget(3, "state"), "disabled")
                self.assertEqual(menu.entrycget(menu.index("end"), "label"), "Copy error")
                app.queued_conversion_ids.add(first.task_id)
                with mock.patch.object(tk.Menu, "tk_popup"), mock.patch.object(tk.Menu, "grab_release"):
                    app.show_task_menu(first.task_id)
                self.assertEqual(row["context_menu"].entrycget(3, "state"), "normal")
                with mock.patch.object(app, "move_to_top") as move:
                    row["context_menu"].invoke(3)
                    move.assert_called_once_with(first.task_id)

                original_toplevel = tk.Toplevel
                def hidden_toplevel(*args, **kwargs):
                    window = original_toplevel(*args, **kwargs)
                    window.withdraw()
                    return window
                with mock.patch.object(tk, "Toplevel", side_effect=hidden_toplevel):
                    details = app.show_task_details(first.task_id)
                text_widget = next(child for child in details.winfo_children() if isinstance(child, tk.Frame))
                contents = next(child for child in text_widget.winfo_children() if isinstance(child, tk.Text)).get("1.0", "end")
                self.assertIn("Original diagnostic text", contents)
                self.assertIn("Imported:", contents)
                details.destroy()
                for advanced in (app.safety_advanced, app.preset_advanced):
                    self.assertFalse(advanced.winfo_manager())
                    advanced.toggle_button.invoke()
                    self.assertEqual(advanced.winfo_manager(), "pack")
                    advanced.toggle_button.invoke()
                    self.assertFalse(advanced.winfo_manager())
                app.set_config_from_ui()
                self.assertEqual(app.get_config()["ffmpeg_args"], app_module.DEFAULT_FFMPEG_ARGS)
                for key in ("copy_chunk_mb", "scan_interval_seconds", "file_stable_seconds", "auto_import_delay_seconds", "low_disk_warning_gb"):
                    if key in config:
                        self.assertEqual(app.get_config()[key], config[key])
                app.selected_ids = [first.task_id]
                app.update_batch_box_visibility()
                self.assertFalse(app.batch_rename_row.winfo_manager())
                self.assertFalse(app.selection_rename_btn.winfo_manager())
                help_parent = tk.Frame(root, bg=app.CARD)
                app.setting_title(help_parent, "Test setting", "Optional explanation", app.CARD)
                heading = help_parent.winfo_children()[0]
                title_row, help_text = heading.winfo_children()
                self.assertEqual(help_text.winfo_manager(), "pack")
                help_parent.destroy()
                # Guided preset creation is independent of the default preset.
                previous_default = app.get_config()["default_preset_name"]
                editor = app.make_new_preset()
                editor.withdraw()
                editor.resolution.set("4K UHD (3840 × 2160)")
                editor.fps.set("25")
                editor.next_button.invoke()
                self.assertEqual(editor.step, 1)
                editor.encoder.set("H.264 · CPU")
                editor.bitrate.set("30")
                editor.next_button.invoke()
                self.assertEqual(editor.step, 2)
                editor.name.set("Stage 4K")
                editor.next_button.invoke()
                self.assertFalse(editor.winfo_exists())
                preset_file = Path(config["preset_folder"]) / "Stage 4K.txt"
                self.assertTrue(preset_file.is_file())
                self.assertEqual(app.get_config()["default_preset_name"], previous_default)
                self.assertIn("Stage 4K", app.preset_menu_values())
                original_preset = preset_file.read_bytes()
                duplicate_editor = app.make_new_preset()
                duplicate_editor.withdraw()
                duplicate_editor.step = 2
                duplicate_editor.draw_step()
                duplicate_editor.name.set("Stage 4K")
                duplicate_editor.next_button.invoke()
                self.assertTrue(duplicate_editor.winfo_exists())
                self.assertIn("already exists", duplicate_editor.error.get())
                self.assertEqual(preset_file.read_bytes(), original_preset)
                duplicate_editor.destroy()

                first.stage = app_module.STAGE_QUEUED
                first.ffmpeg_args = list(app_module.DEFAULT_FFMPEG_ARGS)
                second_args = second.ffmpeg_args
                cancelled_editor = app.edit_video_settings(first.task_id)
                cancelled_editor.withdraw()
                cancelled_editor.bitrate.set("50")
                cancelled_editor.destroy()
                self.assertEqual(first.ffmpeg_args, app_module.DEFAULT_FFMPEG_ARGS)
                preview_path = folder / "editor_preview.png"
                preview = tk.PhotoImage(master=root, width=320, height=180)
                preview.put("navy", to=(0, 0, 320, 180))
                preview.write(str(preview_path), format="png")
                first.thumbnail_path = str(preview_path)
                one_video = app.edit_video_settings(first.task_id)
                one_video.withdraw()
                self.assertEqual(len(one_video.preview_images), 1)
                self.assertIn("restarts", " ".join(str(widget.cget("text")) for widget in one_video.body.winfo_children() if isinstance(widget, tk.Label)))
                one_video.resolution.set("4K UHD (3840 × 2160)")
                one_video.fps.set("25")
                one_video.bitrate.set("30")
                one_video.container.set("MOV (.mov)")
                one_video.audio.set("AAC")
                one_video.audio_kbps.set("256")
                one_video.next_button.invoke()
                self.assertFalse(one_video.winfo_exists())
                self.assertEqual(first.preset_name, "Custom · this video")
                self.assertEqual(first.output_ext, ".mov")
                root.update_idletasks()
                self.assertLessEqual(app.row_widgets[first.task_id]["outer"].winfo_reqheight(), app.dashboard_row_extent())
                self.assertEqual(second.ffmpeg_args, second_args)
                self.assertEqual(app.get_config()["ffmpeg_args"], app_module.DEFAULT_FFMPEG_ARGS)
                first.source_duration_seconds = 240
                first.eta_seconds = 120
                app.render_task(first.task_id)
                self.assertIn("Estimated size", app.row_widgets[first.task_id]["estimate_text"].cget("text"))
                self.assertIn("~", app.row_widgets[first.task_id]["estimate_text"].cget("text"))
                self.assertEqual(app.row_widgets[first.task_id]["progress_text"].cget("text"), f"{first.progress}%")
                warning_tags = [widget for widget in app.row_widgets[first.task_id]["preset_badges"].winfo_children() if "WARNING" in widget.cget("text")]
                self.assertTrue(warning_tags)
                self.assertTrue(warning_tags[0].bind("<Button-1>"))
                first.conversion_started = True
                active_editor = app.edit_video_settings(first.task_id)
                self.assertIsNotNone(active_editor)
                active_editor.destroy()
                first.conversion_started = False
                second.source_duration_seconds = 300
                app.conversion_enabled = True
                app.conversion_rates = {tuple(first.ffmpeg_args): 2, tuple(app_module.DEFAULT_FFMPEG_ARGS): 2}
                app.refresh_conversion_queue_estimate()
                self.assertIn("encoding", app.conversion_queue_eta_var.get())
                app.update_stage_card("conversion", "Converting one.mp4", 20, 120)
                self.assertIn("This video:", app.stage_cards["conversion"]["eta"].get())
                app.conversion_enabled = False

                destination_path = str(folder / "nas" / "finished.mp4")
                app.build_history_record_card({"status": "transferred", "display_name": "one.mp4",
                                               "final_name": "finished.mp4", "path": destination_path,
                                               "folder": str(folder / "nas")}, 0, app.history_render_generation)
                def descendants(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from descendants(child)
                destination_widgets = [widget for widget in descendants(app.history_widgets[-1]) if isinstance(widget, (tk.Entry, app_module.ttk.Entry))]
                self.assertTrue(any(widget.get() == destination_path and widget.cget("state") == "readonly" for widget in destination_widgets))
                app.toggle_log()
                root.update_idletasks()
                self.assertTrue(app.log_visible)
                self.assertEqual(app.bottom_log_outer.pack_info()["side"], "bottom")
                app.toggle_log()
                app.clear_task_selection()
                root.update()
                self.assertEqual(root.state(), "withdrawn")
                self.assertEqual(callback_errors, [])
                self.assertFalse(any("Settings page build warning" in line or "GUI event error" in line for line in app.log_entries))
                for forbidden in forbidden_calls:
                    forbidden.assert_not_called()
                app.shutdown_event.set()
        finally:
            for after_id in root.tk.call("after", "info"):
                root.after_cancel(after_id)
            root.destroy()


if __name__ == "__main__":
    unittest.main()
