"""只读左右差异视图：行对齐、行号和同步纵向滚动。"""

from __future__ import annotations

import difflib
import tkinter as tk
from dataclasses import dataclass
from itertools import zip_longest
from tkinter import ttk


@dataclass(frozen=True)
class DiffRow:
    old_number: int | None
    old_text: str
    new_number: int | None
    new_text: str
    changed: bool


def aligned_rows(source: str, preview: str) -> list[DiffRow]:
    """对齐共同内容，增删造成的空位不占用源文件行号。"""
    old_raw, new_raw = source.splitlines(keepends=True), preview.splitlines(keepends=True)
    old, new = source.splitlines(), preview.splitlines()
    rows = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, start, end, new_start, new_end in matcher.get_opcodes():
        for left, right in zip_longest(range(start, end), range(new_start, new_end)):
            old_text = old[left] if left is not None else ""
            new_text = new[right] if right is not None else ""
            changed = tag != "equal"
            if tag == "equal" and old_raw[left] != new_raw[right]:
                changed = True
                def ending(raw: str) -> str:
                    if raw.endswith("\r\n"):
                        return "CRLF"
                    if raw.endswith("\n"):
                        return "LF"
                    if raw.endswith("\r"):
                        return "CR"
                    return "无换行"
                old_text += f"  [{ending(old_raw[left])}]"
                new_text += f"  [{ending(new_raw[right])}]"
            rows.append(DiffRow(left + 1 if left is not None else None, old_text,
                                right + 1 if right is not None else None, new_text, changed))
    return rows


class SideBySideDiff(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.change_lines: list[int] = []
        self.current_change = -1
        self.syncing = False
        self.notice = tk.StringVar(value="选择文件查看左右差异。")
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=4)
        self.previous = ttk.Button(toolbar, text="上一处", command=lambda: self.jump(-1), state="disabled")
        self.previous.pack(side="left")
        self.next = ttk.Button(toolbar, text="下一处", command=lambda: self.jump(1), state="disabled")
        self.next.pack(side="left", padx=4)
        ttk.Label(toolbar, textvariable=self.notice).pack(side="left", padx=4)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.texts: list[tk.Text] = []
        self.gutters: list[tk.Text] = []
        for column, title in enumerate(("原始内容（红色为修改前 / 删除）", "格式化预览（绿色为修改后 / 新增）")):
            pane = ttk.Frame(body)
            pane.grid(row=0, column=column, sticky="nsew", padx=(0, 2))
            ttk.Label(pane, text=title).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
            options = dict(wrap="none", font=("Consolas", 10), state="disabled",
                           height=1, borderwidth=0, highlightthickness=0, padx=4, pady=2)
            gutter = tk.Text(pane, width=4, takefocus=False, background="#eeeeee", foreground="#777777", **options)
            gutter.grid(row=1, column=0, sticky="ns")
            text = tk.Text(pane, width=1, undo=False, **options)
            text.grid(row=1, column=1, sticky="nsew")
            horizontal = ttk.Scrollbar(pane, orient="horizontal", command=text.xview)
            horizontal.grid(row=2, column=1, sticky="ew")
            text.configure(xscrollcommand=horizontal.set,
                           yscrollcommand=lambda first, last, index=column: self.sync_view(index, first, last))
            for widget in (gutter, text):
                widget.tag_configure("removed", background="#fce0e0", foreground="#8b2020")
                widget.tag_configure("added", background="#dff0df", foreground="#205c2e")
                widget.tag_configure("gap", background="#e5e5e5")
                widget.tag_configure("current", underline=True)
            # 滚轮在行号区域也应滚动代码，而非单独移动行号。
            gutter.bind("<MouseWheel>", lambda event, target=text: self.wheel(event, target))
            for widget in (text, gutter):
                widget.bind("<Button-4>", lambda event: self.scroll("scroll", -3, "units") or "break")
                widget.bind("<Button-5>", lambda event: self.scroll("scroll", 3, "units") or "break")
            self.texts.append(text)
            self.gutters.append(gutter)
            pane.columnconfigure(1, weight=1)
            pane.rowconfigure(1, weight=1)
            body.columnconfigure(column, weight=1, uniform="diff")
        self.vertical = ttk.Scrollbar(body, orient="vertical", command=self.scroll)
        self.vertical.grid(row=0, column=2, sticky="ns")
        body.rowconfigure(0, weight=1)

    def sync_view(self, index, first, last) -> None:
        if self.syncing:
            return
        self.syncing = True
        try:
            self.vertical.set(first, last)
            for widget in (*self.texts, *self.gutters):
                if widget is not self.texts[index]:
                    widget.yview_moveto(first)
        finally:
            self.syncing = False

    def scroll(self, *args) -> None:
        for widget in (*self.texts, *self.gutters):
            widget.yview(*args)

    def wheel(self, event, target) -> str:
        # Windows 每格通常为 120；触控板的小幅 delta 也至少滚动一行。
        units = -int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
        target.yview_scroll(units * 3, "units")
        return "break"

    def show(self, source: str | None = None, preview: str = "", error: str = "",
             *, rows: list[DiffRow] | None = None) -> None:
        # 解析失败没有有效预览，不应把整个源文件标成删除。
        if rows is None:
            rows = aligned_rows(source or "", (source or "") if error else preview)
        self.change_lines = [index for index, row in enumerate(rows, 1)
                             if row.changed and (index == 1 or not rows[index - 2].changed)]
        self.current_change = -1
        self.syncing = True
        try:
            for side, (text, gutter) in enumerate(zip(self.texts, self.gutters)):
                for widget in (text, gutter):
                    widget.configure(state="normal")
                    widget.delete("1.0", "end")
                numbers, content = [], []
                for row in rows:
                    number = row.old_number if side == 0 else row.new_number
                    numbers.append(str(number) if number is not None else "")
                    content.append(row.old_text if side == 0 else row.new_text)
                gutter.configure(width=max(4, len(str(len(rows)))))
                gutter.insert("1.0", "\n".join(numbers))
                text.insert("1.0", "\n".join(content))
                for index, row in enumerate(rows, 1):
                    number = row.old_number if side == 0 else row.new_number
                    if row.changed:
                        tag = "gap" if number is None else ("removed" if side == 0 else "added")
                        for widget in (text, gutter):
                            widget.tag_add(tag, f"{index}.0", f"{index + 1}.0")
                for widget in (text, gutter):
                    widget.configure(state="disabled")
                    widget.yview_moveto(0)
                text.xview_moveto(0)
        finally:
            self.syncing = False
        state = "normal" if self.change_lines else "disabled"
        self.previous.configure(state=state)
        self.next.configure(state=state)
        self.notice.set("检查失败，无法生成格式化预览，请查看问题原因。" if error else
                        f"共 {len(self.change_lines)} 处差异" if self.change_lines else
                        "内容一致。" if source is not None else "选择文件查看左右差异。")
        if self.change_lines:
            self.jump(1)

    def jump(self, direction: int) -> None:
        if not self.change_lines:
            return
        self.current_change = (self.current_change + direction) % len(self.change_lines)
        line = self.change_lines[self.current_change]
        for widget in (*self.texts, *self.gutters):
            widget.tag_remove("current", "1.0", "end")
            widget.tag_add("current", f"{line}.0", f"{line + 1}.0")
            widget.yview(f"{max(1, line - 3)}.0")
        self.notice.set(f"第 {self.current_change + 1} / {len(self.change_lines)} 处差异")
