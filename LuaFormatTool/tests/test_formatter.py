from __future__ import annotations

import codecs
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from formatter import (FormatError, FormatSettings, Formatter, apply_result,
                       decode_source, explain_changes, iter_lua_files, resolve_stylua)
from install_stylua import install
from lua_format_tool import main


class SettingsTests(unittest.TestCase):
    def test_round_trip_and_stylua_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "stylua.toml"
            settings = replace(FormatSettings(), indent_type="Tabs", quote_style="ForceSingle", line_endings="Windows")
            config.write_text(settings.to_toml(), encoding="utf-8")
            self.assertEqual(FormatSettings.load(config), settings)
            config.write_text("column_width = 80", encoding="utf-8")
            imported = FormatSettings.load(config)
            self.assertEqual(imported.indent_type, "Tabs")
            self.assertEqual(imported.syntax, "All")
            self.assertEqual(imported.column_width, 80)

    def test_reject_invalid_and_unsafe_rules(self):
        for data in ({"indent_width": True}, {"indent_width": 0}, {"column_width": 5},
                     {"syntax": "lua"}, {"unknown": 1}, {"sort_requires": {"enabled": True}}):
            with self.subTest(data=data), self.assertRaises(FormatError):
                FormatSettings.from_mapping(data)

    def test_decode_bom_and_chinese(self):
        text = 'local name = "中文"\r\n'
        for codec, bom in (("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE),
                           ("utf-16-be", codecs.BOM_UTF16_BE), ("gb18030", b"")):
            with self.subTest(codec=codec):
                decoded, detected, preamble = decode_source(bom + text.encode(codec))
                self.assertEqual((decoded, detected, preamble), (text, codec, bom))

    def test_reasons_only_for_formatter_changes(self):
        settings = FormatSettings()
        reasons = explain_changes("if true then\n\tprint(1)   \nend", "if true then\n    print(1)\nend\n", settings)
        self.assertIn("FINAL_NEWLINE", {reason.rule for reason in reasons})
        self.assertIn("TRAILING_WHITESPACE", {reason.rule for reason in reasons})
        self.assertTrue(all(reason.line >= 1 for reason in reasons))


class TraversalTests(unittest.TestCase):
    def test_recursive_case_insensitive_suffix_excludes_and_luau(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("main.lua", "UPPER.LUA", "types.d.lua", "nested/test.lua", ".git/test.lua",
                             "node_modules/test.lua", "vendor/test.lua", "demo.luau", "readme.md"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            files = list(iter_lua_files(root, (".git", "node_modules", "vendor", "**/*.d.lua")))
            self.assertEqual({path.relative_to(root).as_posix() for path in files}, {"main.lua", "UPPER.LUA", "nested/test.lua"})
            self.assertIn(root / "demo.luau", list(iter_lua_files(root, include_luau=True)))

    def test_missing_or_non_lua_root_is_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "test.md"
            file.touch()
            for target in (root / "missing", file):
                with self.subTest(target=target), self.assertRaises(FormatError):
                    list(iter_lua_files(target))

    def test_cancelled_scan_stops_before_first_file(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "test.lua").touch()
            cancel = threading.Event()
            cancel.set()
            self.assertEqual(list(iter_lua_files(Path(directory), cancel=cancel)), [])

    def test_symlink_files_and_directories_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real.lua"
            real.write_text("local x=1")
            try:
                (root / "link.lua").symlink_to(real)
                (root / "loop").symlink_to(root, target_is_directory=True)
            except OSError:
                self.skipTest("当前系统不允许创建符号链接")
            self.assertEqual(list(iter_lua_files(root)), [real])


class InstallerTests(unittest.TestCase):
    def test_invalid_checksum_does_not_install(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "stylua.exe"
            fake_response = io.BytesIO(b"invalid archive")
            with patch("install_stylua.urllib.request.urlopen", return_value=fake_response), self.assertRaisesRegex(FormatError, "SHA-256"):
                install(destination)
            self.assertFalse(destination.exists())


try:
    STYLUA = resolve_stylua()
except FormatError:
    STYLUA = None


@unittest.skipUnless(STYLUA, "请先运行 python install_stylua.py 安装真实格式化器")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.formatter = Formatter(FormatSettings(), str(STYLUA))

    def tearDown(self):
        self.formatter.__exit__()
        self.temp.cleanup()

    def write(self, name="test.lua", text="local x=1\n"):
        path = self.root / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def test_scan_preview_apply_backup_and_idempotence(self):
        path = self.write(text="local x={a=1,b=2}\nif x then\nprint('中文')\nend")
        original = path.read_bytes()
        result = self.formatter.check(path)
        self.assertEqual(result.status, "changed", result.error)
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("中文", result.preview)
        self.assertIn("    print", result.preview)
        self.assertTrue(result.issues)
        self.assertIn("--- ", result.diff())
        self.assertIn("\\ No newline at end of file", result.diff())
        backup = apply_result(result)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(path.read_bytes(), result.formatted)
        self.assertEqual(self.formatter.check(path).status, "ok")

    def test_syntax_error_never_written(self):
        path = self.write(text="local x = {\n")
        original = path.read_bytes()
        result = self.formatter.check(path)
        self.assertEqual(result.status, "error")
        self.assertTrue(result.error)
        self.assertEqual(result.source, "local x = {\n")
        self.assertEqual(result.encoding, "utf-8")
        with self.assertRaises(FormatError):
            apply_result(result)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(path.with_name(path.name + ".luaformat.bak").exists())

    def test_editor_changes_and_existing_backup_protected(self):
        path = self.write()
        result = self.formatter.check(path)
        path.write_bytes(b"local modified = true\n")
        with self.assertRaisesRegex(FormatError, "已被修改"):
            apply_result(result)
        self.assertEqual(path.read_bytes(), b"local modified = true\n")
        path.write_bytes(result.original)
        backup = path.with_name(path.name + ".luaformat.bak")
        backup.write_bytes(b"previous backup")
        with self.assertRaisesRegex(FormatError, "已有不同内容"):
            apply_result(result)
        self.assertEqual(backup.read_bytes(), b"previous backup")
        self.assertEqual(path.read_bytes(), result.original)

    def test_preserve_encoding_and_bom(self):
        for codec, bom in (("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE),
                           ("utf-16-be", codecs.BOM_UTF16_BE), ("gb18030", b"")):
            with self.subTest(codec=codec):
                path = self.root / f"{codec}.lua"
                raw = bom + "local text='中文'\n".encode(codec)
                path.write_bytes(raw)
                result = self.formatter.check(path)
                self.assertEqual(result.status, "changed", result.error)
                apply_result(result, backup=False)
                self.assertEqual(path.read_bytes(), bom + result.preview.encode(codec))
                self.assertEqual(self.formatter.check(path).status, "ok")

    def test_custom_rules_crlf_tabs_quotes_and_lua_version(self):
        path = self.write(text='if true then\nprint("hello")\nend\n')
        settings = replace(FormatSettings(), indent_type="Tabs", quote_style="ForceSingle", line_endings="Windows", syntax="Lua51")
        with Formatter(settings, str(STYLUA)) as formatter:
            result = formatter.check(path)
            self.assertEqual(result.status, "changed", result.error)
            self.assertIn(b"\tprint('hello')\r\n", result.formatted)
            apply_result(result, backup=False)
            self.assertEqual(formatter.check(path).status, "ok")
            path.write_text("local x <const> = 1\n")
            self.assertEqual(formatter.check(path).status, "error")

    def test_long_strings_comments_and_ignore_preserve_contents(self):
        path = self.write(text="-- 注释中的 x=1 不应当变成代码\nlocal text=[=[a  b   \n c\t d]=]\n-- stylua: ignore\nlocal   skip=1\nlocal x=2\n")
        result = self.formatter.check(path)
        self.assertEqual(result.status, "changed", result.error)
        self.assertIn("[=[a  b   \n c\t d]=]", result.preview)
        self.assertIn("local   skip=1", result.preview)
        self.assertIn("-- 注释中的 x=1 不应当变成代码", result.preview)
        self.assertNotIn("TRAILING_WHITESPACE", {issue.rule for issue in result.issues})
        apply_result(result, backup=False)
        self.assertEqual(self.formatter.check(path).status, "ok")

    def test_project_config_cannot_override_explicit_settings(self):
        (self.root / "stylua.toml").write_text('indent_type = "Tabs"\nquote_style = "ForceSingle"')
        (self.root / ".editorconfig").write_text("root = true\n[*]\nindent_style = tab\n")
        path = self.write(text="if true then\nprint('hello')\nend\n")
        result = self.formatter.check(path)
        self.assertIn('    print("hello")', result.preview)

    def test_readonly_file_not_overwritten(self):
        path = self.write()
        result = self.formatter.check(path)
        path.chmod(stat.S_IREAD)
        try:
            with self.assertRaisesRegex(FormatError, "只读"):
                apply_result(result)
        finally:
            path.chmod(stat.S_IREAD | stat.S_IWRITE)
        self.assertEqual(path.read_bytes(), result.original)

    def test_cli_dry_run_write_report_exit_codes_and_source_protection(self):
        path = self.write()
        report = self.root / "report.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["scan", str(path), "--json", str(report), "--diff"]), 1)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["files"][0]["status"], "changed")
            self.assertIn("diff", data["files"][0])
            self.assertEqual(main(["format", str(path)]), 1)
            self.assertEqual(path.read_bytes(), b"local x=1\n")
            self.assertEqual(main(["format", str(path), "--write", "--json", str(path)]), 2)
            self.assertEqual(path.read_bytes(), b"local x=1\n")
            self.assertEqual(main(["format", str(path), "--write"]), 0)
            self.assertEqual(main(["scan", str(path)]), 0)
            path.write_bytes(b"function\n")
            self.assertEqual(main(["scan", str(path)]), 2)

    def test_luau_and_lua54_features(self):
        path = self.write(text="local x <const> =1\n::again::\n")
        self.assertEqual(self.formatter.check(path).status, "changed")
        path = self.write(name="typed.luau", text="local x:number=1\n")
        with Formatter(replace(FormatSettings(), syntax="Luau"), str(STYLUA)) as formatter:
            results = formatter.scan(self.root, include_luau=True)
            typed = next(result for result in results if result.path == path)
            self.assertEqual(typed.status, "changed", typed.error)


if __name__ == "__main__":
    unittest.main()
