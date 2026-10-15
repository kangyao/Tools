from __future__ import annotations

import sys
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diff_view import SideBySideDiff, aligned_rows


class AlignmentTests(unittest.TestCase):
    def test_insert_delete_and_replacement_preserve_following_line_numbers(self):
        source = "head\nremove\nanchor\nold\ntail\n"
        preview = "head\nanchor\nnew\nextra\ntail\n"
        rows = aligned_rows(source, preview)
        self.assertEqual([(row.old_number, row.new_number) for row in rows],
                         [(1, 1), (2, None), (3, 2), (4, 3), (None, 4), (5, 5)])
        self.assertEqual([row.old_text for row in rows if row.old_number], source.splitlines())
        self.assertEqual([row.new_text for row in rows if row.new_number], preview.splitlines())
        self.assertEqual([row.changed for row in rows], [False, True, False, True, True, False])

    def test_line_endings_and_missing_final_newline_are_visible(self):
        rows = aligned_rows("first\r\nsecond\nlast", "first\nsecond\nlast\n")
        self.assertEqual([row.changed for row in rows], [True, False, True])
        self.assertEqual((rows[0].old_text, rows[0].new_text), ("first  [CRLF]", "first  [LF]"))
        self.assertEqual((rows[-1].old_text, rows[-1].new_text), ("last  [无换行]", "last  [LF]"))

    def test_empty_files_blank_lines_and_repeated_content(self):
        self.assertEqual(aligned_rows("", ""), [])
        for source, preview in (("", "\n"), ("\n", ""), ("same\n" * 250, "same\n" * 251)):
            with self.subTest(source_lines=len(source.splitlines())):
                rows = aligned_rows(source, preview)
                self.assertEqual([row.old_number for row in rows if row.old_number],
                                 list(range(1, len(source.splitlines()) + 1)))
                self.assertEqual([row.new_number for row in rows if row.new_number],
                                 list(range(1, len(preview.splitlines()) + 1)))
                self.assertTrue(any(row.changed for row in rows))


class DiffWidgetTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"没有 Tk 显示环境：{exc}")
        self.root.geometry("900x400")
        self.view = SideBySideDiff(self.root)
        self.view.pack(fill="both", expand=True)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors, [], "Tk 回调不应产生异常")

    def assert_synchronized(self):
        self.root.update()
        positions = [widget.yview()[0] for widget in (*self.view.texts, *self.view.gutters)]
        for position in positions[1:]:
            self.assertAlmostEqual(position, positions[0], places=5)

    def test_alignment_tags_navigation_and_clear(self):
        self.view.show("head\nremove\nanchor\nold\ntail\n", "head\nanchor\nnew\nextra\ntail\n")
        self.root.update()
        self.assertEqual(self.view.gutters[0].get("1.0", "end-1c"), "1\n2\n3\n4\n\n5")
        self.assertEqual(self.view.gutters[1].get("1.0", "end-1c"), "1\n\n2\n3\n4\n5")
        self.assertIn("removed", self.view.texts[0].tag_names("2.0"))
        self.assertIn("gap", self.view.texts[1].tag_names("2.0"))
        self.assertIn("added", self.view.texts[1].tag_names("5.0"))
        self.assertEqual(self.view.notice.get(), "第 1 / 2 处差异")
        self.view.next.invoke()
        self.assertEqual(self.view.notice.get(), "第 2 / 2 处差异")
        self.view.next.invoke()
        self.assertEqual(self.view.notice.get(), "第 1 / 2 处差异")
        self.view.previous.invoke()
        self.assertEqual(self.view.notice.get(), "第 2 / 2 处差异")
        self.view.show()
        self.assertEqual(self.view.change_lines, [])
        self.assertTrue(self.view.next.instate(["disabled"]))
        for widget in (*self.view.texts, *self.view.gutters):
            self.assertEqual(widget.get("1.0", "end-1c"), "")
        self.view.show("", "")
        self.assertEqual(self.view.notice.get(), "内容一致。")

    def test_scroll_from_either_pane_scrollbar_and_resize(self):
        source = "\n".join(f"local value{i} = '中文'" for i in range(300))
        preview = source.replace("value100", "renamed100").replace("value200", "renamed200")
        self.view.show(source, preview)
        self.root.update()
        for text in self.view.texts:
            text.yview_moveto(0.5)
            self.assert_synchronized()
            text.event_generate("<MouseWheel>", delta=-120)
            self.assert_synchronized()
        self.view.scroll("moveto", 0.2)
        self.assert_synchronized()
        self.view.scroll("scroll", 1, "pages")
        self.assert_synchronized()
        self.view.gutters[0].event_generate("<MouseWheel>", delta=-120)
        self.assert_synchronized()
        self.root.geometry("1100x600")
        self.assert_synchronized()
        self.view.jump(1)
        self.assert_synchronized()
        for text in self.view.texts:
            self.assertEqual(str(text.cget("state")), "disabled")

    def test_parse_failure_does_not_report_every_line_as_deleted(self):
        self.view.show("local broken =\n", "", error="解析失败")
        self.assertFalse(self.view.change_lines)
        self.assertIn("检查失败", self.view.notice.get())
        self.assertFalse(self.view.texts[0].tag_ranges("removed"))


if __name__ == "__main__":
    unittest.main()
