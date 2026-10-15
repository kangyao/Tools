from __future__ import annotations

import sys
import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from formatter import APP_DIR, OPTION_LABELS, FileResult, FormatError, Formatter, resolve_stylua
from diff_view import aligned_rows
from gui import LuaFormatWindow


try:
    STYLUA = resolve_stylua()
except FormatError:
    STYLUA = None


@unittest.skipUnless(STYLUA, "请先安装 StyLua")
class GuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"没有 Tk 显示环境：{exc}")
        self.root.withdraw()
        self.callback_errors = []
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        with patch.object(LuaFormatWindow, "load_local"):
            self.window = LuaFormatWindow(self.root)
        self.save = patch.object(self.window, "save_local")
        self.save.start()
        self.dialog = patch("gui.messagebox.showerror")
        self.showerror = self.dialog.start()
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.window.path.set(str(self.directory))
        self.window.stylua.set(str(STYLUA))

    def tearDown(self):
        self.window.cancel.set()
        self.wait_idle()
        self.window.close()
        self.save.stop()
        self.dialog.stop()
        self.temp.cleanup()
        self.assertEqual(self.callback_errors, [])

    def wait_idle(self):
        deadline = time.monotonic() + 15
        self.root.update()
        while (self.window.busy or self.window.load_timer is not None or self.window.reload_pending or self.window.preview_pending) and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.window.busy, "GUI 后台任务未按时结束")
        self.assertIsNone(self.window.load_timer)

    def click_file(self, source, *, wait=True):
        self.window.tree.selection_set(str(source))
        self.window.tree.focus(str(source))
        self.window.show_detail()
        if wait:
            self.wait_idle()

    def load_and_check(self, source):
        self.window.load_files()
        self.wait_idle()
        self.click_file(source)

    def test_click_detail_format_and_filter(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"if true then\nprint(1)\nend\n")
        self.load_and_check(source)
        self.showerror.assert_not_called()
        key = str(source)
        self.assertEqual(self.window.results[key].status, "changed")
        self.assertTrue(self.window.tree.exists(key))
        self.window.tree.selection_set(key)
        self.window.show_detail()
        self.assertIn("INDENTATION", self.window.details["问题原因"].get("1.0", "end"))
        self.assertIn("+", self.window.details["修改差异"].get("1.0", "end"))
        self.assertIn("print(1)", self.window.split_diff.texts[0].get("1.0", "end"))
        self.assertIn("    print(1)", self.window.split_diff.texts[1].get("1.0", "end"))
        self.assertTrue(self.window.split_diff.change_lines)
        self.window.only_problems.set(True)
        self.window.format_files(False)
        self.wait_idle()
        self.assertEqual(self.window.results[key].status, "ok")
        self.assertTrue(source.with_name("test.lua.luaformat.bak").exists())
        self.assertFalse(self.window.tree.exists(key))
        self.assertEqual(self.window.split_diff.texts[0].get("1.0", "end-1c"), "")
        self.window.only_problems.set(False)
        self.window.render_results()
        self.assertTrue(self.window.tree.exists(key))

    def test_changing_rules_updates_only_current_file(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        self.load_and_check(source)
        other = self.directory / "other.lua"
        other.write_bytes(b"local y=2\n")
        self.window.open_config_panel()
        self.window.config_variables["indent_width"].set("2")
        self.assertEqual(self.window.get_settings().indent_width, 4)
        self.window.apply_config_panel()
        self.wait_idle()
        self.assertIsNone(self.window.config_panel)
        self.assertNotIn(str(other), self.window.results)
        self.assertEqual(self.window.previews[str(source)].signature[0].indent_width, 2)
        self.window.format_files(True)
        self.wait_idle()
        self.assertEqual(source.read_bytes(), b"local x = 1\n")

    def test_import_and_export_rules(self):
        source = self.directory / "custom.toml"
        source.write_text('indent_type = "Tabs"\nquote_style = "ForceSingle"', encoding="utf-8")
        self.window.open_config_panel()
        panel = self.window.config_panel
        self.window.open_config_panel()
        self.assertIs(self.window.config_panel, panel)
        with patch("gui.filedialog.askopenfilename", return_value=str(source)):
            self.window.import_rules()
        self.assertEqual(self.window.get_settings().indent_type, "Spaces")
        self.assertEqual(self.window.get_settings(self.window.config_variables).indent_type, "Tabs")
        output = self.directory / "exported.toml"
        with patch("gui.filedialog.asksaveasfilename", return_value=str(output)):
            self.window.export_rules()
        self.assertIn('quote_style = "ForceSingle"', output.read_text(encoding="utf-8"))
        self.window.close_config_panel()
        self.assertEqual(self.window.get_settings().indent_type, "Spaces")

    def test_preserve_options_have_readable_labels_and_survive_export_import(self):
        self.window.open_config_panel()
        for name, value in (("line_endings", "Preserve"), ("final_newline", "Preserve"),
                            ("collapse_simple_statement", "Input"), ("call_parentheses", "Input"),
                            ("block_newline_gaps", "Preserve"), ("function_spacing", "Preserve")):
            self.window.config_variables[name].set(OPTION_LABELS[name][value])
        expected = self.window.get_settings(self.window.config_variables)
        self.assertEqual(expected.line_endings, "Preserve")
        self.assertEqual(expected.collapse_simple_statement, "Input")
        output = self.directory / "preserve.toml"
        with patch("gui.filedialog.asksaveasfilename", return_value=str(output)):
            self.window.export_rules()
        self.window.apply_config_panel()
        self.assertEqual(self.window.get_settings(), expected)
        self.assertIn("换行符不处理", self.window.rule_summary.get())
        self.window.open_config_panel()
        self.assertEqual(self.window.config_variables["call_parentheses"].get(), OPTION_LABELS["call_parentheses"]["Input"])
        self.window.config_variables["line_endings"].set("Unix")
        with patch("gui.filedialog.askopenfilename", return_value=str(output)):
            self.window.import_rules()
        self.assertEqual(self.window.get_settings(self.window.config_variables), expected)
        self.window.close_config_panel()

    def test_write_failure_visible_and_original_protected(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        self.load_and_check(source)
        source.write_bytes(b"local newer = true\n")
        self.window.format_files(True)
        self.wait_idle()
        self.assertIn("已被修改", self.window.results[str(source)].error)
        self.assertEqual(source.read_bytes(), b"local newer = true\n")

    def test_function_spacing_is_enabled_for_existing_configs(self):
        self.assertEqual(self.window.get_settings().function_spacing, "Always")
        self.window.open_config_panel()
        self.assertEqual(self.window.config_variables["function_spacing"].get(), OPTION_LABELS["function_spacing"]["Always"])
        self.window.close_config_panel()

    def test_short_guard_return_mode_has_readable_label(self):
        self.assertEqual(self.window.get_settings().collapse_simple_statement, "GuardReturn")
        self.window.open_config_panel()
        self.assertEqual(self.window.config_variables["collapse_simple_statement"].get(), OPTION_LABELS["collapse_simple_statement"]["GuardReturn"])
        self.window.apply_config_panel()
        self.assertEqual(self.window.get_settings().collapse_simple_statement, "GuardReturn")

    def test_report_contains_only_checked_files_and_no_stale_rules(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        other = self.directory / "unchecked.lua"
        other.write_bytes(b"local y=2\n")
        self.load_and_check(source)
        report = self.directory / "report.json"
        with patch("gui.filedialog.asksaveasfilename", return_value=str(report)), patch.object(Formatter, "check", side_effect=AssertionError("报告不得检查文件")):
            self.window.export_report()
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["settings"]["indent_width"], 4)
        self.assertEqual(data["checked_files"], 1)
        self.assertEqual(data["unchecked"], [str(other)])
        self.window.variables["indent_width"].set("2")
        with patch("gui.messagebox.showinfo") as show:
            self.window.export_report()
        self.assertIn("请先点击文件", show.call_args.args[1])

    def test_listing_does_not_require_stylua_or_check_any_file(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        self.window.stylua.set(str(self.directory / "missing.exe"))
        with patch("gui.Formatter", side_effect=AssertionError("列文件不应创建格式化器")):
            self.window.load_files()
            self.wait_idle()
        self.assertEqual(self.window.results, {})
        self.assertEqual(self.window.tree.item(str(source), "values")[0], "未检查")
        self.assertNotIn("开始扫描", [button.cget("text") for button in self.window.action_buttons])
        self.showerror.assert_not_called()

    def test_clicks_reuse_checks_and_diff_until_file_content_changes(self):
        first = self.directory / "first.lua"
        second = self.directory / "second.lua"
        first.write_bytes(b"local x=1\n")
        second.write_bytes(b"local y=2\n")
        self.window.load_files()
        self.wait_idle()
        original = Formatter.check
        with patch.object(Formatter, "check", autospec=True, side_effect=original) as check, \
                patch.object(FileResult, "diff", autospec=True, side_effect=FileResult.diff) as diff, \
                patch("gui.aligned_rows", wraps=aligned_rows) as rows:
            self.click_file(first)
            self.click_file(second)
            self.click_file(first)
            self.click_file(first)
            self.assertEqual(check.call_count, 2)
            first.touch()
            self.click_file(first)
            self.assertEqual(check.call_count, 2, "仅时间戳变化时仍应复用相同内容")
            first.write_bytes(b"local updated=3\n")
            self.click_file(first)
            self.assertEqual(check.call_count, 3)
            self.assertEqual(diff.call_count, 3)
            self.assertEqual(rows.call_count, 3)
        self.assertIn("updated", self.window.details["原始内容"].get("1.0", "end"))

    def test_batch_format_does_not_check_or_write_unvisited_files(self):
        first = self.directory / "first.lua"
        second = self.directory / "second.lua"
        first.write_bytes(b"local x=1\n")
        second.write_bytes(b"local y=2\n")
        self.load_and_check(first)
        with patch.object(Formatter, "check", side_effect=AssertionError("写入已查看预览不应重新检查其他文件")):
            self.window.format_files(True)
            self.wait_idle()
        self.assertEqual(first.read_bytes(), b"local x = 1\n")
        self.assertEqual(second.read_bytes(), b"local y=2\n")
        self.assertNotIn(str(second), self.window.results)

    def test_rapid_clicks_check_latest_requested_file_and_keep_its_preview(self):
        paths = [self.directory / name for name in ("a.lua", "b.lua", "c.lua")]
        for index, path in enumerate(paths):
            path.write_bytes(f"local value={index}\n".encode())
        self.window.load_files()
        self.wait_idle()
        started, release = threading.Event(), threading.Event()
        original = Formatter.check
        def delayed(formatter, path):
            if path == paths[0]:
                started.set()
                release.wait(5)
            return original(formatter, path)
        with patch.object(Formatter, "check", autospec=True, side_effect=delayed) as check:
            try:
                self.click_file(paths[0], wait=False)
                self.assertTrue(started.wait(2))
                self.click_file(paths[1], wait=False)
                self.click_file(paths[2], wait=False)
            finally:
                release.set()
            self.wait_idle()
            self.assertEqual([call.args[1] for call in check.call_args_list], [paths[0], paths[2]])
        self.assertEqual(self.window.file_label.cget("text"), str(paths[2]))
        self.assertNotIn(str(paths[1]), self.window.results)

    def test_path_entry_automatically_loads_and_same_file_cache_survives_reload(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        # setUp 已填写路径；不调用显式加载方法也应自动列出文件。
        self.wait_idle()
        self.assertTrue(self.window.tree.exists(str(source)))
        self.assertEqual(self.window.results, {})
        original = Formatter.check
        with patch.object(Formatter, "check", autospec=True, side_effect=original) as check:
            self.click_file(source)
            self.window.load_files()
            self.wait_idle()
            self.click_file(source)
            self.assertEqual(check.call_count, 1)

    def test_switching_directory_during_check_does_not_display_old_result(self):
        source = self.directory / "a.lua"
        source.write_bytes(b"local x=1\n")
        other_directory = self.directory / "other"
        other_directory.mkdir()
        other = other_directory / "other.lua"
        other.write_bytes(b"local y=2\n")
        self.window.load_files()
        self.wait_idle()
        started, release = threading.Event(), threading.Event()
        original = Formatter.check
        def delayed(formatter, path):
            started.set()
            release.wait(5)
            return original(formatter, path)
        with patch.object(Formatter, "check", autospec=True, side_effect=delayed) as check:
            try:
                self.click_file(source, wait=False)
                self.assertTrue(started.wait(2))
                self.window.path.set(str(other_directory))
                self.window.load_files()
            finally:
                release.set()
            self.wait_idle()
            self.assertEqual(check.call_count, 1)
        self.assertEqual(list(self.window.files), [str(other)])
        self.assertEqual(self.window.results, {})
        self.assertEqual(self.window.details["修改差异"].get("1.0", "end-1c"), "")

    def test_file_modified_while_checking_does_not_cache_stale_preview(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        self.window.load_files()
        self.wait_idle()
        original = Formatter.check
        def modify_after_check(formatter, path):
            result = original(formatter, path)
            path.write_bytes(b"local changed=123\n")
            return result
        with patch.object(Formatter, "check", autospec=True, side_effect=modify_after_check) as check:
            self.click_file(source)
            self.assertEqual(check.call_count, 1)
        self.assertNotIn(str(source), self.window.results)
        self.assertIn("再次点击", self.window.details["问题原因"].get("1.0", "end"))

    def test_report_cannot_overwrite_unvisited_lua_file(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        other = self.directory / "unchecked.lua"
        other.write_bytes(b"local y=2\n")
        self.load_and_check(source)
        with patch("gui.filedialog.asksaveasfilename", return_value=str(other)):
            self.window.export_report()
        self.assertEqual(other.read_bytes(), b"local y=2\n")
        self.assertIn("不能覆盖", self.showerror.call_args.args[1])

    def test_click_while_listing_is_kept_after_listing_finishes(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        release = threading.Event()
        def slow_listing(*args):
            yield source
            release.wait(5)
        with patch("gui.iter_lua_files", side_effect=slow_listing):
            try:
                self.window.load_files()
                deadline = time.monotonic() + 2
                while not self.window.tree.exists(str(source)) and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(0.01)
                self.assertTrue(self.window.tree.exists(str(source)))
                self.click_file(source, wait=False)
            finally:
                release.set()
            self.wait_idle()
        self.assertEqual(self.window.results[str(source)].status, "changed")
        self.assertEqual(self.window.file_label.cget("text"), str(source))

    def test_nonexistent_typed_directory_does_not_open_error_dialog(self):
        self.window.path.set(str(self.directory / "not-created"))
        self.wait_idle()
        self.assertEqual(self.window.files, {})
        self.showerror.assert_not_called()
        self.assertIn("不存在", self.window.status.get())


if __name__ == "__main__":
    unittest.main()
