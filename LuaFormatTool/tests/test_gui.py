from __future__ import annotations

import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from formatter import APP_DIR, FormatError, resolve_stylua
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

    def wait_idle(self):
        deadline = time.monotonic() + 15
        while self.window.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.window.busy, "GUI 后台任务未按时结束")

    def test_scan_detail_format_and_filter(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"if true then\nprint(1)\nend\n")
        self.window.scan()
        self.wait_idle()
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
        self.window.format_files(False)
        self.wait_idle()
        self.assertEqual(self.window.results[key].status, "ok")
        self.assertTrue(source.with_name("test.lua.luaformat.bak").exists())
        self.assertFalse(self.window.tree.exists(key))
        self.assertEqual(self.window.split_diff.texts[0].get("1.0", "end-1c"), "")
        self.window.only_problems.set(False)
        self.window.render_results()
        self.assertTrue(self.window.tree.exists(key))

    def test_changing_rules_requires_rescan(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        self.window.scan()
        self.wait_idle()
        self.window.open_config_panel()
        self.window.config_variables["indent_width"].set("2")
        self.assertEqual(self.window.get_settings().indent_width, 4)
        self.window.apply_config_panel()
        self.assertIsNone(self.window.config_panel)
        with patch("gui.messagebox.showinfo") as show:
            self.window.format_files(True)
        self.assertIn("重新扫描", show.call_args.args[1])
        self.assertEqual(source.read_bytes(), b"local x=1\n")

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

    def test_write_failure_visible_and_original_protected(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        self.window.scan()
        self.wait_idle()
        source.write_bytes(b"local newer = true\n")
        self.window.format_files(True)
        self.wait_idle()
        self.assertIn("已被修改", self.window.results[str(source)].error)
        self.assertEqual(source.read_bytes(), b"local newer = true\n")

    def test_report_uses_scanned_rules_after_controls_change(self):
        source = self.directory / "test.lua"
        source.write_bytes(b"local x=1\n")
        self.window.scan()
        self.wait_idle()
        self.window.variables["indent_width"].set("2")
        report = self.directory / "report.json"
        with patch("gui.filedialog.asksaveasfilename", return_value=str(report)):
            self.window.export_report()
        import json
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["settings"]["indent_width"], 4)


if __name__ == "__main__":
    unittest.main()
