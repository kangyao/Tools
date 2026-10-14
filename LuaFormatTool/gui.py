"""Tkinter 界面，所有文件扫描和格式化均在后台线程执行。"""

from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from formatter import (APP_DIR, CHOICES, DEFAULT_EXCLUDES, STATUS_LABELS, FileResult,
                       FormatError, FormatSettings, Formatter, apply_result, resolve_stylua)


FIELD_LABELS = {
    "syntax": "Lua 版本", "indent_type": "缩进类型", "indent_width": "每级缩进宽度",
    "column_width": "参考行宽", "line_endings": "换行符", "quote_style": "字符串引号",
    "call_parentheses": "调用括号", "collapse_simple_statement": "合并简单语句",
    "space_after_function_names": "函数名后空格", "block_newline_gaps": "块首尾空行",
}


class LuaFormatWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Lua 格式检查与格式化")
        self.root.geometry("1320x900")
        self.root.minsize(1000, 700)
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.results: dict[str, FileResult] = {}
        self.scan_signature: tuple | None = None
        self.path = tk.StringVar()
        self.stylua = tk.StringVar()
        self.encoding = tk.StringVar(value="auto")
        self.excludes = tk.StringVar(value=", ".join(DEFAULT_EXCLUDES))
        self.include_luau = tk.BooleanVar(value=False)
        self.backup = tk.BooleanVar(value=True)
        self.only_problems = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="选择 Lua 目录或文件，然后点击“开始扫描”。扫描不会修改文件。")
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

    def button(self, parent, text, command, **kwargs):
        widget = ttk.Button(parent, text=text, command=command, **kwargs)
        self.action_buttons.append(widget)
        return widget

    def build_ui(self) -> None:
        self.action_buttons: list[ttk.Button] = []
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="扫描路径").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.path).grid(row=0, column=1, sticky="ew", padx=6)
        self.button(top, "选择目录", self.choose_directory).grid(row=0, column=2, padx=3)
        self.button(top, "选择文件", self.choose_file).grid(row=0, column=3, padx=3)
        ttk.Label(top, text="StyLua 路径").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(top, textvariable=self.stylua).grid(row=1, column=1, sticky="ew", padx=6)
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
        self.button(actions, "开始扫描", self.scan).pack(side="left", padx=3)
        self.stop_button = ttk.Button(actions, text="停止", command=self.cancel.set, state="disabled")
        self.stop_button.pack(side="left", padx=3)
        self.button(actions, "格式化选中文件", lambda: self.format_files(False)).pack(side="left", padx=3)
        self.button(actions, "格式化全部问题文件", lambda: self.format_files(True)).pack(side="left", padx=3)
        self.button(actions, "导出扫描报告", self.export_report).pack(side="left", padx=3)
        ttk.Checkbutton(actions, text="仅显示问题", variable=self.only_problems, command=self.render_results).pack(side="right")

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
        panes.add(left, weight=2)

        right = ttk.Frame(panes)
        self.file_label = ttk.Label(right, text="点击左侧文件查看原因与修改预览", wraplength=650)
        self.file_label.pack(fill="x", pady=5)
        notebook = ttk.Notebook(right)
        notebook.pack(fill="both", expand=True)
        self.details = {}
        for name in ("问题原因", "修改差异", "原始内容", "格式化预览"):
            widget = ScrolledText(notebook, wrap="none", font=("Consolas", 10), state="disabled")
            notebook.add(widget, text=name)
            self.details[name] = widget
        self.details["修改差异"].tag_configure("removed", foreground="#b02020")
        self.details["修改差异"].tag_configure("added", foreground="#247140")
        panes.add(right, weight=3)
        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(self.root, textvariable=self.status, padding=10, wraplength=1200).pack(fill="x")

    def get_settings(self, variables: dict[str, tk.StringVar] | None = None) -> FormatSettings:
        data = {name: variable.get() for name, variable in (self.variables if variables is None else variables).items()}
        for name in ("indent_width", "column_width"):
            try:
                data[name] = int(data[name])
            except ValueError as exc:
                raise FormatError(f"{FIELD_LABELS[name]}必须是整数") from exc
        return FormatSettings.from_mapping(data)

    def update_rule_summary(self) -> None:
        settings = self.get_settings()
        indent = f"{settings.indent_width} 个空格" if settings.indent_type == "Spaces" else f"Tab（宽度 {settings.indent_width}）"
        newline = "LF" if settings.line_endings == "Unix" else "CRLF"
        self.rule_summary.set(f"当前格式：{settings.syntax} · {indent} · 行宽 {settings.column_width} · {newline} · {settings.quote_style}")

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
        self.config_variables = {name: tk.StringVar(master=panel, value=variable.get())
                                 for name, variable in self.variables.items()}
        self.config_options = {
            "encoding": tk.StringVar(master=panel, value=self.encoding.get()),
            "include_luau": tk.BooleanVar(master=panel, value=self.include_luau.get()),
            "backup": tk.BooleanVar(master=panel, value=self.backup.get()),
        }
        self.config_notice.set("点击应用保存配置；修改格式规则后需要重新扫描。")
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
                ttk.Combobox(frame, textvariable=variable, values=CHOICES[name], state="readonly", width=30).pack(fill="x")
            else:
                ttk.Entry(frame, textvariable=variable, width=30).pack(fill="x")
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
        self.status.set("格式配置已应用，请重新扫描。" if changed else "格式配置已保存。")

    def close_config_panel(self) -> None:
        if self.config_panel is not None:
            self.config_panel.grab_release()
            self.config_panel.destroy()
            self.config_panel = None
            self.config_variables = {}
            self.config_options = {}

    def signature(self) -> tuple:
        if not self.path.get().strip():
            raise FormatError("请选择扫描目录或 Lua 文件")
        return (str(Path(self.path.get().strip()).absolute()), self.get_settings(),
                self.stylua.get().strip(), self.encoding.get(), self.excludes.get(), self.include_luau.get())

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

    def start_worker(self, title: str, work) -> None:
        if self.busy:
            return
        self.busy = True
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

    def scan(self) -> None:
        try:
            signature = self.signature()
            path, settings, stylua, encoding, excludes, luau = signature
            resolve_stylua(stylua or None)
            self.save_local()
        except (OSError, ValueError, FormatError) as exc:
            messagebox.showerror("无法扫描", str(exc))
            return
        self.results.clear()
        self.scan_signature = signature
        self.render_results()
        self.clear_detail()
        def work():
            with Formatter(settings, stylua or None, encoding) as formatter:
                formatter.scan(Path(path), tuple(item.strip() for item in excludes.split(",") if item.strip()),
                               luau, self.cancel, lambda result: self.events.put(("result", result)))
        self.start_worker("正在扫描，可点击停止…", work)

    def format_files(self, all_files: bool) -> None:
        try:
            if self.scan_signature != self.signature():
                raise FormatError("扫描路径、规则或编码已经改变，请重新扫描后格式化")
            keys = list(self.results) if all_files else list(self.tree.selection())
            targets = [self.results[key] for key in keys if self.results[key].status == "changed"]
            if not targets:
                raise FormatError("没有可格式化的文件，请先扫描并选中存在格式问题的文件")
        except (ValueError, FormatError) as exc:
            messagebox.showinfo("格式化", str(exc))
            return
        _, settings, stylua, encoding, _, _ = self.scan_signature
        backup = self.backup.get()
        def work():
            written = 0
            with Formatter(settings, stylua or None, encoding) as formatter:
                for result in targets:
                    if self.cancel.is_set():
                        break
                    try:
                        apply_result(result, backup)
                        written += 1
                        self.events.put(("result", formatter.check(result.path)))
                    except (OSError, FormatError) as exc:
                        # 保存预览与原始内容，失败原因留在该文件详情里。
                        self.events.put(("write_error", (str(result.path), str(exc))))
            self.events.put(("summary", f"已格式化 {written}/{len(targets)} 个文件；已成功写入的文件已重新检查。"))
        self.start_worker(f"正在格式化 {len(targets)} 个文件…", work)

    def install(self) -> None:
        def work():
            from install_stylua import install
            self.events.put(("installed", str(install())))
        self.start_worker("正在下载并校验官方 StyLua 2.6.0…", work)
        self.stop_button.configure(state="disabled")

    def poll(self) -> None:
        count = 0
        while count < 100:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            count += 1
            if kind == "result":
                self.results[str(value.path)] = value
                self.render_row(str(value.path))
                if str(value.path) in self.tree.selection():
                    self.show_detail()
                if self.busy:
                    self.status.set(f"已检查 {len(self.results)} 个文件：{value.path.name}")
            elif kind == "write_error":
                key, error = value
                self.results[key].error = "写入失败：" + error
                self.show_detail()
            elif kind == "error":
                self.scan_signature = None
                messagebox.showerror("操作失败", value)
            elif kind == "installed":
                self.stylua.set(value)
                self.status.set(f"StyLua 已安装：{value}")
                try:
                    self.save_local()
                except (OSError, ValueError, FormatError) as exc:
                    messagebox.showerror("保存设置失败", f"StyLua 已安装成功，但设置未保存：{exc}")
            elif kind == "summary":
                self.status.set(value)
            elif kind == "done":
                self.busy = False
                self.progress.stop()
                self.stop_button.configure(state="disabled")
                for button in self.action_buttons:
                    button.configure(state="normal")
                total = len(self.results)
                counts = {name: sum(result.status == name for result in self.results.values()) for name in STATUS_LABELS}
                errors = sum(bool(result.error) and result.status != "error" for result in self.results.values())
                if total:
                    self.status.set(f"{'已停止；' if self.cancel.is_set() else ''}已检查 {total} 个文件；格式正确 {counts['ok']}；格式问题 {counts['changed']}；检查失败 {counts['error']}；写入失败 {errors}")
                elif self.scan_signature:
                    self.status.set("扫描完成，没有找到符合条件的 Lua 文件。")
        self.poll_timer = self.root.after(80, self.poll)

    def render_row(self, key: str) -> None:
        result = self.results[key]
        visible = result.status != "ok" or not self.only_problems.get()
        exists = self.tree.exists(key)
        if not visible:
            if exists:
                self.tree.delete(key)
            return
        root = Path(self.scan_signature[0]) if self.scan_signature else result.path.parent
        try:
            label = str(result.path.relative_to(root)) if root != result.path else result.path.name
        except ValueError:
            label = str(result.path)
        options = {"text": label, "values": ("写入失败" if result.error and result.status != "error" else STATUS_LABELS[result.status], len(result.issues)),
                   "tags": ("error" if result.error else result.status,)}
        if exists:
            self.tree.item(key, **options)
        else:
            self.tree.insert("", "end", iid=key, **options)

    def render_results(self) -> None:
        selected = self.tree.selection()
        for key in self.tree.get_children():
            self.tree.delete(key)
        for key in self.results:
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
        self.file_label.configure(text="点击左侧文件查看原因与修改预览")
        for name in self.details:
            self.set_text(name, "")

    def show_detail(self, event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            self.clear_detail()
            return
        result = self.results[selected[0]]
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
        self.set_text("修改差异", result.diff())
        self.set_text("原始内容", result.source)
        self.set_text("格式化预览", result.preview)

    def import_rules(self) -> None:
        filename = filedialog.askopenfilename(parent=self.config_panel or self.root,
                                             filetypes=[("StyLua 配置", "*.toml"), ("所有文件", "*.*")])
        if not filename:
            return
        try:
            settings = FormatSettings.load(Path(filename))
            variables = self.config_variables if self.config_panel is not None else self.variables
            for name, value in asdict(settings).items():
                variables[name].set(str(value))
            if self.config_panel is not None:
                self.config_notice.set("已导入规则，点击应用保存配置。")
            else:
                self.save_local()
                self.update_rule_summary()
                self.status.set("已导入规则，请重新扫描。")
        except (OSError, FormatError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.config_panel or self.root)

    def export_rules(self) -> None:
        try:
            variables = self.config_variables if self.config_panel is not None else self.variables
            content = self.get_settings(variables).to_toml()
            filename = filedialog.asksaveasfilename(parent=self.config_panel or self.root,
                                                  initialfile="stylua.toml", defaultextension=".toml", filetypes=[("StyLua 配置", "*.toml")])
            if filename:
                if Path(filename).resolve() in {result.path for result in self.results.values()}:
                    raise FormatError("配置文件不能覆盖扫描中的 Lua 源文件")
                Path(filename).write_text(content, encoding="utf-8", newline="\n")
                if self.config_panel is not None:
                    self.config_notice.set(f"已导出规则：{filename}")
                else:
                    self.save_local()
                    self.status.set(f"已导出规则：{filename}")
        except (OSError, FormatError) as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.config_panel or self.root)

    def export_report(self) -> None:
        if not self.results:
            messagebox.showinfo("导出报告", "请先扫描 Lua 文件")
            return
        filename = filedialog.asksaveasfilename(initialfile="lua-format-report.json", defaultextension=".json", filetypes=[("JSON 报告", "*.json")])
        if filename:
            try:
                if Path(filename).resolve() in {result.path for result in self.results.values()}:
                    raise FormatError("报告不能覆盖扫描中的 Lua 源文件")
                data = {"root": self.scan_signature[0] if self.scan_signature else None,
                        "settings": asdict(self.scan_signature[1]) if self.scan_signature else None,
                        "files": [result.to_dict(True) for result in self.results.values()]}
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
