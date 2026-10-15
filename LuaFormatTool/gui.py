"""Tkinter 界面：后台列出文件，点击时检查，未修改的文件复用预览。"""

from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from diff_view import DiffRow, SideBySideDiff, aligned_rows
from formatter import (APP_DIR, CHOICES, DEFAULT_EXCLUDES, OPTION_LABELS, STATUS_LABELS, FileResult,
                       FormatError, FormatSettings, Formatter, apply_result, iter_lua_files)


FIELD_LABELS = {
    "syntax": "Lua 版本", "indent_type": "缩进类型", "indent_width": "每级缩进宽度",
    "column_width": "参考行宽", "line_endings": "换行符", "final_newline": "文件末尾换行", "quote_style": "字符串引号",
    "call_parentheses": "调用括号", "collapse_simple_statement": "合并简单语句",
    "space_after_function_names": "函数名后空格", "block_newline_gaps": "块首尾空行",
    "function_spacing": "函数之间空行",
}


def file_stamp(path: Path) -> tuple | None:
    try:
        info = path.stat(follow_symlinks=False)
        return (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino, info.st_mode)
    except OSError:
        return None


@dataclass
class FilePreview:
    result: FileResult
    signature: tuple
    stamp: tuple | None
    diff: str
    rows: list[DiffRow]

    @classmethod
    def build(cls, result: FileResult, signature: tuple, stamp: tuple | None) -> FilePreview:
        return cls(result, signature, stamp, result.error if result.status == "error" else result.diff(),
                   aligned_rows(result.source, result.source if result.status == "error" else result.preview))


class LuaFormatWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Lua 格式检查与格式化")
        self.root.geometry("1320x900")
        self.root.minsize(1000, 700)
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.files: dict[str, Path] = {}
        self.previews: dict[str, FilePreview] = {}
        self.list_signature: tuple | None = None
        self.preview_signature: tuple | None = None
        self.load_timer: str | None = None
        self.reload_pending = False
        self.preview_pending: str | None = None
        self.displayed_preview: FilePreview | None = None
        self.worker_summary = False
        self.path = tk.StringVar()
        self.stylua = tk.StringVar()
        self.encoding = tk.StringVar(value="auto")
        self.excludes = tk.StringVar(value=", ".join(DEFAULT_EXCLUDES))
        self.include_luau = tk.BooleanVar(value=False)
        self.backup = tk.BooleanVar(value=True)
        self.only_problems = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="选择 Lua 目录或文件，点击左侧文件查看修改差异；未修改的文件复用检查结果。")
        self.variables = {name: tk.StringVar(value=str(value)) for name, value in asdict(FormatSettings()).items()}
        self.config_panel: tk.Toplevel | None = None
        self.config_variables: dict[str, tk.StringVar] = {}
        self.config_options: dict[str, tk.Variable] = {}
        self.config_notice = tk.StringVar()
        self.rule_summary = tk.StringVar()
        self.load_local()
        self.update_rule_summary()
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_timer = self.root.after(80, self.poll)
        for variable in (self.path, self.excludes, self.include_luau):
            variable.trace_add("write", self.schedule_load)
        if self.path.get().strip():
            self.schedule_load()

    def button(self, parent, text, command, **kwargs):
        widget = ttk.Button(parent, text=text, command=command, **kwargs)
        self.action_buttons.append(widget)
        return widget

    def build_ui(self) -> None:
        self.action_buttons: list[ttk.Button] = []
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="文件路径").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(top, textvariable=self.path)
        entry.grid(row=0, column=1, sticky="ew", padx=6)
        entry.bind("<Return>", lambda event: self.load_files())
        self.button(top, "选择目录", self.choose_directory).grid(row=0, column=2, padx=3)
        self.button(top, "选择文件", self.choose_file).grid(row=0, column=3, padx=3)
        ttk.Label(top, text="StyLua 路径").grid(row=1, column=0, sticky="w", pady=6)
        entry = ttk.Entry(top, textvariable=self.stylua)
        entry.grid(row=1, column=1, sticky="ew", padx=6)
        entry.bind("<Return>", lambda event: self.settings_changed())
        entry.bind("<FocusOut>", lambda event: self.settings_changed())
        self.button(top, "选择 StyLua", self.choose_stylua).grid(row=1, column=2, padx=3)
        self.button(top, "安装 StyLua", self.install).grid(row=1, column=3, padx=3)
        ttk.Label(top, text="排除规则").grid(row=2, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.excludes).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Label(top, text="用逗号分隔，如 vendor, **/*.d.lua").grid(row=2, column=2, columnspan=2, sticky="w")
        top.columnconfigure(1, weight=1)

        ttk.Label(self.root, textvariable=self.rule_summary, padding=(15, 4), wraplength=1200).pack(fill="x")

        actions = ttk.Frame(self.root, padding=10)
        actions.pack(fill="x")
        self.button(actions, "格式配置", self.open_config_panel).pack(side="left", padx=3)
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop_work, state="disabled")
        self.stop_button.pack(side="left", padx=3)
        self.button(actions, "格式化选中文件", lambda: self.format_files(False)).pack(side="left", padx=3)
        self.button(actions, "格式化已检查问题", lambda: self.format_files(True)).pack(side="left", padx=3)
        self.button(actions, "导出已检查报告", self.export_report).pack(side="left", padx=3)
        ttk.Checkbutton(actions, text="隐藏已通过文件", variable=self.only_problems, command=self.render_results).pack(side="right")

        panes = ttk.Panedwindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=10)
        left = ttk.Frame(panes)
        self.tree = ttk.Treeview(left, columns=("status", "count"), selectmode="extended")
        self.tree.heading("#0", text="文件（可多选）")
        self.tree.heading("status", text="状态")
        self.tree.heading("count", text="问题")
        self.tree.column("#0", width=360, stretch=True)
        self.tree.column("status", width=85, stretch=False)
        self.tree.column("count", width=45, stretch=False)
        self.tree.tag_configure("changed", foreground="#a65a00")
        self.tree.tag_configure("error", foreground="#b02020")
        self.tree.tag_configure("ok", foreground="#247140")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.tree.bind("<ButtonRelease-1>", self.show_detail)
        self.tree.bind("<Return>", self.show_detail)
        panes.add(left, weight=2)

        right = ttk.Frame(panes)
        self.file_label = ttk.Label(right, text="点击左侧文件查看原因与修改预览", wraplength=650)
        self.file_label.pack(fill="x", pady=5)
        notebook = ttk.Notebook(right)
        self.notebook = notebook
        notebook.pack(fill="both", expand=True)
        self.details = {}
        for name in ("问题原因", "修改差异", "原始内容", "格式化预览"):
            page = ttk.Frame(notebook)
            notebook.add(page, text=name)
            if name == "修改差异":
                self.diff_page = page
                mode = tk.StringVar(value="split")
                toolbar = ttk.Frame(page)
                toolbar.pack(fill="x")
                content = ttk.Frame(page)
                content.pack(fill="both", expand=True)
                self.split_diff = SideBySideDiff(content)
                widget = ScrolledText(content, wrap="none", font=("Consolas", 10), state="disabled")
                def switch_diff():
                    widget = self.details["修改差异"]
                    widget.pack_forget()
                    self.split_diff.pack_forget()
                    (self.split_diff if mode.get() == "split" else widget).pack(fill="both", expand=True)
                for label, value in (("左右对比", "split"), ("单栏差异", "unified")):
                    ttk.Radiobutton(toolbar, text=label, variable=mode, value=value,
                                    command=switch_diff).pack(side="left", padx=5, pady=4)
                self.split_diff.pack(fill="both", expand=True)
            else:
                widget = ScrolledText(page, wrap="none", font=("Consolas", 10), state="disabled")
                widget.pack(fill="both", expand=True)
            self.details[name] = widget
        notebook.select(self.diff_page)
        self.details["修改差异"].tag_configure("removed", foreground="#b02020")
        self.details["修改差异"].tag_configure("added", foreground="#247140")
        panes.add(right, weight=3)
        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(self.root, textvariable=self.status, padding=10, wraplength=1200).pack(fill="x")

    def get_settings(self, variables: dict[str, tk.StringVar] | None = None) -> FormatSettings:
        data = {name: variable.get() for name, variable in (self.variables if variables is None else variables).items()}
        for name, labels in OPTION_LABELS.items():
            data[name] = {label: value for value, label in labels.items()}.get(data[name], data[name])
        for name in ("indent_width", "column_width"):
            try:
                data[name] = int(data[name])
            except ValueError as exc:
                raise FormatError(f"{FIELD_LABELS[name]}必须是整数") from exc
        return FormatSettings.from_mapping(data)

    def update_rule_summary(self) -> None:
        settings = self.get_settings()
        indent = f"{settings.indent_width} 个空格" if settings.indent_type == "Spaces" else f"Tab（宽度 {settings.indent_width}）"
        newline = {"Unix": "LF", "Windows": "CRLF", "Preserve": "换行符不处理"}[settings.line_endings]
        spacing = "函数间留空行" if settings.function_spacing == "Always" else "函数间距不处理"
        self.rule_summary.set(f"当前格式：{settings.syntax} · {indent} · 行宽 {settings.column_width} · {newline} · {settings.quote_style} · {spacing}")

    def open_config_panel(self) -> None:
        if self.busy:
            return
        if self.config_panel is not None:
            self.config_panel.lift()
            self.config_panel.focus_set()
            return
        panel = tk.Toplevel(self.root)
        self.config_panel = panel
        panel.title("格式配置")
        panel.transient(self.root)
        panel.resizable(False, False)
        self.config_variables = {name: tk.StringVar(master=panel, value=OPTION_LABELS.get(name, {}).get(variable.get(), variable.get()))
                                 for name, variable in self.variables.items()}
        self.config_options = {
            "encoding": tk.StringVar(master=panel, value=self.encoding.get()),
            "include_luau": tk.BooleanVar(master=panel, value=self.include_luau.get()),
            "backup": tk.BooleanVar(master=panel, value=self.backup.get()),
        }
        self.config_notice.set("点击应用保存配置；修改规则后，当前文件自动更新差异，其他文件在点击时检查。")
        body = ttk.Frame(panel, padding=15)
        body.pack(fill="both", expand=True)
        rules = ttk.LabelFrame(body, text="格式规则", padding=10)
        rules.pack(fill="x")
        for index, (name, variable) in enumerate(self.config_variables.items()):
            row, column = divmod(index, 2)
            frame = ttk.Frame(rules)
            frame.grid(row=row, column=column, sticky="ew", padx=8, pady=6)
            ttk.Label(frame, text=FIELD_LABELS[name]).pack(anchor="w")
            if name in CHOICES:
                values = [OPTION_LABELS.get(name, {}).get(value, value) for value in CHOICES[name]]
                ttk.Combobox(frame, textvariable=variable, values=values, state="readonly", width=30).pack(fill="x")
            else:
                ttk.Entry(frame, textvariable=variable, width=30).pack(fill="x")
        ttk.Label(body, text="“仅短条件提前返回”只合并 if ... then return end，超过参考行宽时保持多行。\n"
                            "“不处理”保留对应输入样式；其他合并模式也受参考行宽影响。\n"
                            "缩进、字符串引号、函数名后空格和参考行宽由 StyLua 统一处理，暂不支持单独关闭。",
                  wraplength=580).pack(fill="x", pady=(8, 0))
        options = ttk.LabelFrame(body, text="文件处理", padding=10)
        options.pack(fill="x", pady=(10, 0))
        ttk.Label(options, text="源文件编码").pack(side="left")
        ttk.Combobox(options, textvariable=self.config_options["encoding"],
                     values=("auto", "utf-8", "gb18030", "gbk"), state="readonly", width=10).pack(side="left", padx=6)
        ttk.Checkbutton(options, text="包括 .luau", variable=self.config_options["include_luau"]).pack(side="left", padx=8)
        ttk.Checkbutton(options, text="格式化时备份", variable=self.config_options["backup"]).pack(side="left", padx=8)
        ttk.Label(body, textvariable=self.config_notice, wraplength=580).pack(fill="x", pady=10)
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        ttk.Button(actions, text="导入规则", command=self.import_rules).pack(side="left", padx=3)
        ttk.Button(actions, text="导出规则", command=self.export_rules).pack(side="left", padx=3)
        ttk.Button(actions, text="取消", command=self.close_config_panel).pack(side="right", padx=3)
        ttk.Button(actions, text="应用", command=self.apply_config_panel).pack(side="right", padx=3)
        panel.protocol("WM_DELETE_WINDOW", self.close_config_panel)
        panel.bind("<Escape>", lambda event: self.close_config_panel())
        panel.update_idletasks()
        self.root.update_idletasks()
        width, height = panel.winfo_reqwidth(), panel.winfo_reqheight()
        x = max(0, self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2)
        y = max(0, self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2)
        panel.geometry(f"+{x}+{y}")
        panel.grab_set()
        panel.focus_set()

    def apply_config_panel(self) -> None:
        if self.config_panel is None or self.busy:
            return
        previous: dict[str, object] = {}
        try:
            settings = self.get_settings(self.config_variables)
            changed = settings != self.get_settings() or any(
                name != "backup" and variable.get() != getattr(self, name).get()
                for name, variable in self.config_options.items()
            )
            previous = {name: variable.get() for name, variable in self.variables.items()}
            previous.update({name: getattr(self, name).get() for name in self.config_options})
            for name, value in asdict(settings).items():
                self.variables[name].set(str(value))
            for name, variable in self.config_options.items():
                getattr(self, name).set(variable.get())
            self.save_local()
        except (OSError, ValueError, FormatError) as exc:
            for name, value in previous.items():
                (self.variables[name] if name in self.variables else getattr(self, name)).set(value)
            messagebox.showerror("应用配置失败", str(exc), parent=self.config_panel)
            return
        self.update_rule_summary()
        self.close_config_panel()
        self.status.set("格式配置已应用。" if changed else "格式配置已保存。")
        self.settings_changed()
        if self.path.get().strip() and self.list_signature != self.listing_signature():
            self.schedule_load()

    def close_config_panel(self) -> None:
        if self.config_panel is not None:
            self.config_panel.grab_release()
            self.config_panel.destroy()
            self.config_panel = None
            self.config_variables = {}
            self.config_options = {}

    @property
    def results(self) -> dict[str, FileResult]:
        return {key: preview.result for key, preview in self.previews.items() if key in self.files}

    def listing_signature(self) -> tuple:
        if not self.path.get().strip():
            raise FormatError("请选择目录或 Lua 文件")
        return (str(Path(self.path.get().strip()).absolute()), self.excludes.get(), self.include_luau.get())

    def format_signature(self) -> tuple:
        stylua = self.stylua.get().strip()
        return (self.get_settings(), stylua, self.encoding.get())

    def list_is_current(self) -> bool:
        try:
            return self.list_signature == self.listing_signature()
        except (ValueError, FormatError):
            return False

    def sync_settings(self) -> tuple:
        signature = self.format_signature()
        if signature != self.preview_signature:
            self.preview_signature = signature
            self.previews.clear()
            self.render_results()
            self.clear_detail()
        return signature

    def settings_changed(self) -> None:
        try:
            self.sync_settings()
            if self.tree.selection():
                self.show_detail()
        except (ValueError, FormatError) as exc:
            self.status.set(str(exc))

    def schedule_load(self, *args) -> None:
        if self.load_timer is not None:
            self.root.after_cancel(self.load_timer)
        self.load_timer = self.root.after(250, self.load_files)

    def stop_work(self) -> None:
        self.cancel.set()
        self.preview_pending = None
        self.reload_pending = False

    def choose_directory(self) -> None:
        value = filedialog.askdirectory(initialdir=self.initial_directory())
        if value:
            self.path.set(value)

    def initial_directory(self) -> str:
        path = Path(self.path.get()) if self.path.get() else APP_DIR
        return str(path if path.is_dir() else path.parent)

    def choose_file(self) -> None:
        value = filedialog.askopenfilename(initialdir=self.initial_directory(), filetypes=[("Lua 文件", "*.lua *.luau"), ("所有文件", "*.*")])
        if value:
            self.path.set(value)

    def choose_stylua(self) -> None:
        value = filedialog.askopenfilename(title="选择 StyLua 可执行文件")
        if value:
            self.stylua.set(value)
            self.settings_changed()

    def start_worker(self, title: str, work, *, kind: str = "check") -> None:
        if self.busy:
            return
        self.busy = True
        self.worker_kind = kind
        self.worker_summary = False
        self.cancel.clear()
        self.status.set(title)
        for button in self.action_buttons:
            button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.start()
        def worker():
            try:
                work()
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("done", None))
        threading.Thread(target=worker, daemon=True).start()

    def load_files(self) -> None:
        if self.load_timer is not None:
            self.root.after_cancel(self.load_timer)
            self.load_timer = None
        if self.busy:
            self.reload_pending = True
            return
        self.reload_pending = False
        try:
            signature = self.listing_signature()
            path, excludes, luau = signature
            self.sync_settings()
            self.save_local()
        except (OSError, ValueError, FormatError) as exc:
            self.files.clear()
            self.list_signature = None
            self.render_results()
            self.status.set(str(exc))
            return
        selected = self.tree.selection()
        self.files.clear()
        self.list_signature = signature
        self.render_results()
        self.clear_detail()
        def work():
            count = 0
            patterns = tuple(item.strip() for item in excludes.split(",") if item.strip())
            for file in iter_lua_files(Path(path), patterns, luau, self.cancel):
                if self.cancel.is_set():
                    break
                self.events.put(("file", (signature, file)))
                count += 1
            self.events.put(("listed", (signature, selected, count)))
        self.start_worker("正在列出 Lua 文件…", work, kind="list")

    def format_files(self, all_files: bool) -> None:
        if self.busy:
            return
        try:
            signature = self.sync_settings()
            if self.list_signature != self.listing_signature():
                raise FormatError("文件路径或排除规则已经改变，请等待文件列表更新")
            keys = list(self.files) if all_files else list(self.tree.selection())
            if not all_files and any(key not in self.previews for key in keys):
                raise FormatError("请先逐个点击选中文件查看差异，再执行格式化")
            targets = [self.previews[key].result for key in keys
                       if key in self.previews and self.previews[key].result.status == "changed"]
            if not targets:
                raise FormatError("没有已检查的格式问题文件，请先点击文件查看差异")
        except (ValueError, FormatError) as exc:
            messagebox.showinfo("格式化", str(exc))
            return
        backup = self.backup.get()
        def work():
            written = 0
            for result in targets:
                if self.cancel.is_set():
                    break
                try:
                    apply_result(result, backup)
                    written += 1
                    stamp = file_stamp(result.path)
                    try:
                        current = result.path.read_bytes() == result.formatted and stamp == file_stamp(result.path)
                    except OSError:
                        current = False
                    if not current:
                        self.events.put(("stale", str(result.path)))
                        continue
                    updated = FileResult(result.path, "ok", encoding=result.encoding,
                                         original=result.formatted, formatted=result.formatted,
                                         source=result.preview, preview=result.preview)
                    self.events.put(("preview", FilePreview.build(updated, signature, stamp)))
                except (OSError, FormatError) as exc:
                    self.events.put(("write_error", (str(result.path), str(exc))))
            self.events.put(("summary", f"已格式化 {written}/{len(targets)} 个已检查的问题文件。"))
        self.start_worker(f"正在格式化 {len(targets)} 个文件…", work, kind="format")

    def install(self) -> None:
        def work():
            from install_stylua import install
            self.events.put(("installed", str(install())))
        self.start_worker("正在下载并校验官方 StyLua 2.6.0…", work, kind="install")
        self.stop_button.configure(state="disabled")

    def poll(self) -> None:
        count = 0
        while count < 100:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            count += 1
            if kind == "file":
                signature, path = value
                if signature == self.list_signature and self.list_is_current():
                    self.files[str(path)] = path
                    self.render_row(str(path))
            elif kind == "listed":
                signature, selected, total = value
                if signature == self.list_signature and self.list_is_current():
                    if not self.tree.selection():
                        self.tree.selection_set([key for key in selected if self.tree.exists(key)])
                    self.status.set(f"{'已停止；' if self.cancel.is_set() else ''}已列出 {total} 个文件，点击文件查看修改差异。")
                    self.worker_summary = True
            elif kind == "preview":
                key = str(value.result.path)
                if value.signature == self.format_signature() and value.stamp == file_stamp(value.result.path):
                    self.previews[key] = value
                    if self.list_is_current() and key in self.files:
                        self.render_row(key)
                        if key == self.selected_key():
                            self.render_detail(value)
                        elif not self.tree.selection():
                            self.clear_detail()
                if self.preview_pending == key:
                    self.preview_pending = None
            elif kind == "stale":
                self.previews.pop(value, None)
                if value in self.files:
                    self.render_row(value)
                if self.preview_pending == value:
                    self.preview_pending = None
                if value == self.selected_key():
                    self.clear_detail()
                    self.file_label.configure(text=value)
                    self.set_text("问题原因", "文件预览已失效，请再次点击文件更新差异。")
                    self.status.set("文件已变化，请再次点击更新差异。")
                    self.worker_summary = True
            elif kind == "write_error":
                key, error = value
                if key in self.previews:
                    self.previews[key].result.error = "写入失败：" + error
                    self.render_row(key)
                    if key == self.selected_key():
                        self.render_detail(self.previews[key], force=True)
            elif kind == "error":
                self.preview_pending = None
                self.worker_summary = True
                self.status.set(f"操作失败：{value}")
                if self.worker_kind != "list":
                    messagebox.showerror("操作失败", value)
            elif kind == "installed":
                self.stylua.set(value)
                self.settings_changed()
                self.status.set(f"StyLua 已安装：{value}")
                try:
                    self.save_local()
                except (OSError, ValueError, FormatError) as exc:
                    messagebox.showerror("保存设置失败", f"StyLua 已安装成功，但设置未保存：{exc}")
            elif kind == "summary":
                self.status.set(value)
                self.worker_summary = True
            elif kind == "done":
                self.busy = False
                self.progress.stop()
                self.stop_button.configure(state="disabled")
                for button in self.action_buttons:
                    button.configure(state="normal")
                total = len(self.results)
                counts = {name: sum(result.status == name for result in self.results.values()) for name in STATUS_LABELS}
                errors = sum(bool(result.error) and result.status != "error" for result in self.results.values())
                if not self.worker_summary:
                    self.status.set(f"{'已停止；' if self.cancel.is_set() else ''}已检查 {total} 个文件；格式正确 {counts['ok']}；格式问题 {counts['changed']}；检查失败 {counts['error']}；写入失败 {errors}")
                if self.reload_pending:
                    self.load_files()
                elif self.preview_pending and not self.cancel.is_set() and self.load_timer is None:
                    self.preview_pending = None
                    self.show_detail()
                else:
                    self.preview_pending = None
        self.poll_timer = self.root.after(80, self.poll)

    def render_row(self, key: str) -> None:
        path = self.files[key]
        cached = self.previews.get(key)
        valid = cached and cached.signature == self.preview_signature and (
            cached.stamp == file_stamp(path) or (cached.result.error and cached.result.status != "error"))
        result = cached.result if valid else None
        visible = result is None or result.status != "ok" or not self.only_problems.get()
        exists = self.tree.exists(key)
        if not visible:
            if exists:
                self.tree.delete(key)
            return
        root = Path(self.list_signature[0]) if self.list_signature else path.parent
        try:
            label = str(path.relative_to(root)) if root != path else path.name
        except ValueError:
            label = str(path)
        status = "未检查" if result is None else "写入失败" if result.error and result.status != "error" else STATUS_LABELS[result.status]
        options = {"text": label, "values": (status, len(result.issues) if result else "—"),
                   "tags": (("error" if result.error else result.status) if result else "pending",)}
        if exists:
            self.tree.item(key, **options)
        else:
            self.tree.insert("", "end", iid=key, **options)

    def render_results(self) -> None:
        selected = self.tree.selection()
        for key in self.tree.get_children():
            self.tree.delete(key)
        for key in self.files:
            self.render_row(key)
        self.tree.selection_set([key for key in selected if self.tree.exists(key)])
        if not self.tree.selection():
            self.clear_detail()

    def set_text(self, name: str, text: str) -> None:
        widget = self.details[name]
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        if name == "修改差异":
            for number, line in enumerate(text.splitlines(), 1):
                if line.startswith(("+", "-")):
                    widget.tag_add("added" if line.startswith("+") else "removed", f"{number}.0", f"{number}.end")
        widget.configure(state="disabled")

    def clear_detail(self) -> None:
        self.displayed_preview = None
        self.file_label.configure(text="点击左侧文件查看原因与修改预览")
        self.split_diff.show()
        for name in self.details:
            self.set_text(name, "")

    def show_detail(self, event=None) -> None:
        key = self.selected_key()
        if key is None:
            self.preview_pending = None
            self.clear_detail()
            return
        if event is not None:
            self.notebook.select(self.diff_page)
        try:
            signature = self.sync_settings()
            if self.list_signature != self.listing_signature():
                self.schedule_load()
                return
        except (ValueError, FormatError) as exc:
            self.status.set(str(exc))
            return
        path = self.files[key]
        cached = self.previews.get(key)
        if cached and cached.signature == signature and cached.stamp == file_stamp(path):
            self.preview_pending = None
            self.render_detail(cached)
            self.status.set(f"使用缓存：{path.name}（文件和格式规则未修改）")
            return
        self.clear_detail()
        self.file_label.configure(text=str(path))
        self.set_text("问题原因", "正在检查当前文件并生成修改差异…")
        if self.busy:
            self.preview_pending = key
            return
        self.previews.pop(key, None)
        self.render_row(key)
        self.preview_pending = None
        settings, stylua, encoding = signature
        def work():
            before = file_stamp(path)
            if cached and cached.signature == signature and (cached.result.status != "error" or cached.result.original):
                try:
                    unchanged = path.read_bytes() == cached.result.original
                except OSError:
                    unchanged = False
                if unchanged and before == file_stamp(path):
                    cached.stamp = before
                    self.events.put(("preview", cached))
                    return
            with Formatter(settings, stylua or None, encoding) as formatter:
                result = formatter.check(path)
            after = file_stamp(path)
            if before == after:
                self.events.put(("preview", FilePreview.build(result, signature, after)))
            else:
                self.events.put(("stale", key))
        self.start_worker(f"正在检查 {path.name}…", work)

    def selected_key(self) -> str | None:
        selected = self.tree.selection()
        focused = self.tree.focus()
        return focused if focused in selected else selected[0] if selected else None

    def render_detail(self, cached: FilePreview, *, force: bool = False) -> None:
        if self.displayed_preview is cached and not force:
            return
        self.displayed_preview = cached
        result = cached.result
        self.file_label.configure(text=str(result.path))
        lines = [f"状态：{STATUS_LABELS[result.status]}", f"源文件编码：{result.encoding or '未识别'}"]
        if result.error:
            lines.extend(("", result.error))
        for issue in result.issues:
            lines.extend(("", f"第 {issue.line} 行 [{issue.rule}]", issue.message))
            if issue.before or issue.after:
                lines.extend(("原始：", issue.before, "目标：", issue.after))
        if not result.issues and not result.error:
            lines.append("文件符合当前格式规则。")
        self.set_text("问题原因", "\n".join(lines))
        self.set_text("修改差异", cached.diff)
        self.split_diff.show(result.source, result.preview, result.error if result.status == "error" else "", rows=cached.rows)
        self.set_text("原始内容", result.source)
        self.set_text("格式化预览", result.preview)

    def import_rules(self) -> None:
        filename = filedialog.askopenfilename(parent=self.config_panel or self.root,
                                             filetypes=[("LuaFormatTool / StyLua 配置", "*.toml"), ("所有文件", "*.*")])
        if not filename:
            return
        try:
            settings = FormatSettings.load(Path(filename))
            variables = self.config_variables if self.config_panel is not None else self.variables
            for name, value in asdict(settings).items():
                variables[name].set(OPTION_LABELS.get(name, {}).get(value, str(value))
                                    if self.config_panel is not None else str(value))
            if self.config_panel is not None:
                self.config_notice.set("已导入规则，点击应用保存配置。")
            else:
                self.save_local()
                self.update_rule_summary()
                self.settings_changed()
                self.status.set("已导入规则；当前文件更新差异，其他文件在点击时检查。")
        except (OSError, FormatError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.config_panel or self.root)

    def export_rules(self) -> None:
        try:
            variables = self.config_variables if self.config_panel is not None else self.variables
            content = self.get_settings(variables).to_toml()
            filename = filedialog.asksaveasfilename(parent=self.config_panel or self.root,
                                                  initialfile="stylua.toml", defaultextension=".toml", filetypes=[("LuaFormatTool / StyLua 配置", "*.toml")])
            if filename:
                if Path(filename).resolve() in set(self.files.values()):
                    raise FormatError("配置文件不能覆盖列表中的 Lua 源文件")
                Path(filename).write_text(content, encoding="utf-8", newline="\n")
                if self.config_panel is not None:
                    self.config_notice.set(f"已导出规则：{filename}")
                else:
                    self.save_local()
                    self.status.set(f"已导出规则：{filename}")
        except (OSError, FormatError) as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.config_panel or self.root)

    def export_report(self) -> None:
        self.sync_settings()
        if not self.list_is_current():
            messagebox.showinfo("导出报告", "请先选择目录或文件，并等待文件列表更新")
            return
        checked = {key: preview for key, preview in self.previews.items()
                   if key in self.files and preview.stamp == file_stamp(preview.result.path)}
        if not checked:
            messagebox.showinfo("导出报告", "请先点击文件查看差异；报告仅包含已经检查且未修改的文件")
            return
        filename = filedialog.asksaveasfilename(initialfile="lua-format-report.json", defaultextension=".json", filetypes=[("JSON 报告", "*.json")])
        if filename:
            try:
                if Path(filename).resolve() in set(self.files.values()):
                    raise FormatError("报告不能覆盖列表中的 Lua 源文件")
                data = {"root": self.list_signature[0] if self.list_signature else None,
                        "settings": asdict(self.preview_signature[0]), "scope": "checked_files_only",
                        "total_files": len(self.files), "checked_files": len(checked),
                        "unchecked": [key for key in self.files if key not in checked],
                        "files": [dict(preview.result.to_dict(), diff=preview.diff) for preview in checked.values()]}
                Path(filename).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self.status.set(f"已导出报告：{filename}")
            except (OSError, FormatError) as exc:
                messagebox.showerror("导出失败", str(exc))

    def load_local(self) -> None:
        try:
            data = json.loads((APP_DIR / "settings.local.json").read_text(encoding="utf-8"))
            settings = FormatSettings.from_mapping(data.get("settings", {}))
            for name, value in asdict(settings).items():
                self.variables[name].set(str(value))
            for name in ("path", "stylua", "encoding", "excludes", "include_luau", "backup"):
                if name in data:
                    getattr(self, name).set(data[name])
        except (OSError, ValueError, TypeError, AttributeError, FormatError):
            pass

    def save_local(self) -> None:
        data = {name: getattr(self, name).get() for name in ("path", "stylua", "encoding", "excludes", "include_luau", "backup")}
        data["settings"] = asdict(self.get_settings())
        destination = APP_DIR / "settings.local.json"
        temporary = destination.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self) -> None:
        if self.load_timer is not None:
            self.root.after_cancel(self.load_timer)
            self.load_timer = None
        self.reload_pending = False
        self.preview_pending = None
        if self.busy:
            self.cancel.set()
            self.status.set("正在停止任务，完成当前文件后即可关闭窗口。")
            return
        try:
            self.save_local()
        except (OSError, ValueError, FormatError):
            pass
        self.close_config_panel()
        self.root.after_cancel(self.poll_timer)
        self.root.destroy()


def run() -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    LuaFormatWindow(root)
    root.mainloop()
