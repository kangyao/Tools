from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from ..build import build_preview
from ..build_config import (BUILD_ACTIONS, BUILD_STEPS, CONFIGURATIONS, PLATFORMS, CompileTask,
                            GenerationTask, discover_generation_scripts, read_solution_configurations, read_solution_targets,
                            resolve_build_path, task_id, validate_build_options)
from ..build_tasks import compile_task_for_solution, split_arguments
from ..process import redact
from .worker import JobThread


BUILD_OUTCOMES = {"success": "完成", "failed": "失败", "skipped": "未执行", "cancelled": "已停止"}


class BuildDialog(QDialog):
    def __init__(self, data_dir: Path, profile, save_options, parent=None):
        super().__init__(parent)
        self.data_dir, self.profile = data_dir, deepcopy(profile)
        self.save_options = save_options
        self.worker = None
        self.results = {}
        self.validate_only = False
        self.loading = True
        self.generated_changed = False
        self.closing_requested = False
        self.log_path = self.build_directory = ""
        self.setWindowTitle(f"编译构建 · {profile.name}")
        self.resize(1120, 760)
        layout = QVBoxLayout(self)
        self.settings = QWidget()
        settings = QVBoxLayout(self.settings)
        settings.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.steps = {}
        for key, label in BUILD_STEPS.items():
            check = QCheckBox(label)
            check.setChecked(key in profile.build.steps)
            check.toggled.connect(self.refresh_preview)
            self.steps[key] = check
            top.addWidget(check)
        top.addStretch()
        advanced_button = QPushButton("高级设置")
        advanced_button.setCheckable(True)
        top.addWidget(advanced_button)
        settings.addLayout(top)
        self.parameter_tabs = QTabWidget()
        self.generation_table = self.task_table(["启用", "BAT", "参数", "输出目录（可留空）", "状态"])
        self.compile_table = self.task_table(["启用", "SLN", "编译目标", "配置", "平台", "动作", "状态"])
        self.add_task_page("生成任务", self.generation_table,
                           [("添加 BAT", self.add_generation_tasks), ("复制任务", self.copy_generation_tasks),
                            ("移除", lambda: self.remove_tasks(self.generation_table))])
        self.add_task_page("编译任务", self.compile_table,
                           [("添加 SLN", self.add_solutions), ("读取目标", self.load_targets),
                            ("移除", lambda: self.remove_tasks(self.compile_table))])
        self.generation_table.setColumnWidth(1, 240)
        self.compile_table.setColumnWidth(2, 170)
        for column in (2, 3):
            self.generation_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.compile_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.parameter_tabs.setCurrentIndex(0 if "generate" in profile.build.steps else 1)
        settings.addWidget(self.parameter_tabs, 1)
        self.advanced = QWidget()
        advanced = QFormLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        self.python = QLineEdit(profile.build.python_path)
        advanced.addRow("构建设置 Python", self.path_row(self.python, "Python (*.exe)"))
        self.install_file = QLineEdit(profile.build.setup_install_file)
        self.install_file.setPlaceholderText("构建设置使用的离线依赖包，可留空")
        advanced.addRow("离线依赖包", self.path_row(self.install_file, "ZIP (*.zip)"))
        self.pack_zip = QCheckBox("构建设置时下载并打包依赖")
        self.pack_zip.setChecked(profile.build.setup_pack_zip)
        advanced.addRow(self.pack_zip)
        self.skill_dir = QLineEdit(profile.build.ib_skill_dir)
        advanced.addRow("IB 技能目录", self.path_row(self.skill_dir))
        self.build_console = QLineEdit(profile.build.build_console_path)
        self.build_console.setPlaceholderText("留空自动查找 BuildConsole.exe")
        advanced.addRow("BuildConsole", self.path_row(self.build_console, "应用程序 (*.exe)"))
        self.environment = QPlainTextEdit("\n".join(f"{k}={v}" for k, v in profile.build.generation_environment.items()))
        self.environment.setPlaceholderText("生成 BAT 的环境变量，每行 名称=值，可留空")
        self.environment.setMaximumHeight(65)
        advanced.addRow("生成环境变量", self.environment)
        self.log_directory = QLineEdit(profile.build.log_directory)
        self.log_directory.setPlaceholderText("留空使用工具配置目录")
        advanced.addRow("日志目录", self.path_row(self.log_directory))
        limits = QHBoxLayout()
        self.timeout = QSpinBox()
        self.timeout.setRange(0, 1440)
        self.timeout.setSpecialValueText("不限时")
        self.timeout.setSuffix(" 分钟")
        self.timeout.setValue(profile.build.timeout_minutes)
        limits.addWidget(self.timeout)
        limits.addWidget(QLabel("编译不限时，可手动中断"))
        self.monitor = QCheckBox("打开 IB 监视器")
        self.monitor.setChecked(profile.build.open_monitor)
        limits.addWidget(self.monitor)
        limits.addStretch()
        advanced.addRow("设置 / 生成 / 检查超时", limits)
        self.advanced.hide()
        advanced_button.toggled.connect(self.advanced.setVisible)
        settings.addWidget(self.advanced)
        layout.addWidget(self.settings, 3)
        self.output_tabs = QTabWidget()
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(8000)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.output_tabs.addTab(self.preview, "执行计划")
        self.output_tabs.addTab(self.logs, "运行日志")
        layout.addWidget(self.output_tabs, 2)
        self.message = QLabel("勾选任务后执行；生成成功的 SLN 会自动加入编译列表。")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        self.save_button = self.button("保存", self.save)
        self.check_button = self.button("检查", lambda: self.start(True))
        self.run_button = self.button("执行所选步骤", lambda: self.start(False))
        self.run_button.setObjectName("primary")
        self.stop_button = self.button("停止后续任务", self.stop_after_step)
        self.cancel_button = self.button("中断当前任务", self.cancel_step)
        self.open_logs_button = self.button("日志目录", self.open_logs)
        for button in (self.save_button, self.check_button, self.run_button, self.stop_button,
                       self.cancel_button, self.open_logs_button):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self.button("关闭", self.close))
        layout.addLayout(actions)
        for task in profile.build.generation_tasks:
            self.append_generation(task)
        for task in profile.build.compile_tasks:
            self.append_compile(task)
        for edit in (self.python, self.install_file, self.skill_dir, self.build_console, self.log_directory):
            edit.textChanged.connect(self.refresh_preview)
        self.environment.textChanged.connect(self.refresh_preview)
        self.timeout.valueChanged.connect(self.refresh_preview)
        self.pack_zip.toggled.connect(self.refresh_preview)
        self.monitor.toggled.connect(self.refresh_preview)
        self.loading = False
        self.refresh_preview()
        self.update_controls()

    @staticmethod
    def button(label, action):
        button = QPushButton(label)
        button.clicked.connect(action)
        return button

    def task_table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().hide()
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(0, 45)
        table.setColumnWidth(len(headers) - 1, 100)
        table.cellChanged.connect(self.refresh_preview)
        return table

    def add_task_page(self, title, table, buttons):
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        for label, callback in buttons:
            bar.addWidget(self.button(label, callback))
        bar.addStretch()
        layout.addLayout(bar)
        layout.addWidget(table)
        self.parameter_tabs.addTab(page, title)

    def path_row(self, edit, file_filter=""):
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        def browse():
            value = (QFileDialog.getOpenFileName(self, "选择文件", self.profile.root, file_filter)[0] if file_filter
                     else QFileDialog.getExistingDirectory(self, "选择目录", self.profile.root))
            if value:
                edit.setText(value)
        row.addWidget(self.button("浏览", browse))
        return row

    def combo(self, values, selected, editable=False):
        combo = QComboBox()
        combo.setEditable(editable)
        combo.addItems(list(dict.fromkeys([*values, selected])))
        combo.setCurrentText(selected)
        combo.currentTextChanged.connect(self.refresh_preview)
        return combo

    @staticmethod
    def enable_item(task):
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if task.enabled else Qt.CheckState.Unchecked)
        item.setData(Qt.ItemDataRole.UserRole, task.id)
        return item

    @staticmethod
    def status_item():
        item = QTableWidgetItem("待执行")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        return item

    def append_generation(self, task):
        table = self.generation_table
        was_loading, self.loading = self.loading, True
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, self.enable_item(task))
        root = Path(self.profile.root)
        choices = [str(path.relative_to(root)).replace("\\", "/") for path in discover_generation_scripts(root)]
        combo = QComboBox()
        for value in dict.fromkeys([*choices, task.bat]):
            combo.addItem(Path(value).name, value)
        combo.setCurrentIndex(combo.findData(task.bat))
        combo.setToolTip(str(root / "SetupScript"))
        combo.currentTextChanged.connect(self.refresh_preview)
        table.setCellWidget(row, 1, combo)
        table.setItem(row, 2, QTableWidgetItem(subprocess.list2cmdline(task.arguments)))
        table.item(row, 2).setToolTip('BAT 参数，例如 --product AICore --configuration Profile；空格路径用双引号括起')
        table.setItem(row, 3, QTableWidgetItem(task.build_directory))
        table.item(row, 3).setToolTip("独立生成目录，自动转换为 --build-dir；相对路径基于当前工程根目录")
        table.setItem(row, 4, self.status_item())
        self.loading = was_loading
        self.refresh_preview()

    def append_compile(self, task):
        table = self.compile_table
        was_loading, self.loading = self.loading, True
        row = table.rowCount()
        table.insertRow(row)
        check = self.enable_item(task)
        check.setData(Qt.ItemDataRole.UserRole + 1, task.generation_id)
        table.setItem(row, 0, check)
        table.setItem(row, 1, QTableWidgetItem(task.solution))
        try:
            solution = resolve_build_path(Path(self.profile.root), task.solution)
            targets = read_solution_targets(solution)
            pairs = read_solution_configurations(solution)
        except (ValueError, OSError):
            targets, pairs = [], []
        table.setCellWidget(row, 2, self.combo(targets, task.target, True))
        table.setCellWidget(row, 3, self.combo([pair[0] for pair in pairs] or CONFIGURATIONS, task.configuration))
        table.setCellWidget(row, 4, self.combo([pair[1] for pair in pairs] or PLATFORMS, task.platform))
        table.setCellWidget(row, 5, self.combo(BUILD_ACTIONS, task.action))
        table.setItem(row, 6, self.status_item())
        self.loading = was_loading
        self.refresh_preview()

    def generation_tasks(self):
        table = self.generation_table
        return [GenerationTask(id=table.item(row, 0).data(Qt.ItemDataRole.UserRole),
                               enabled=table.item(row, 0).checkState() == Qt.CheckState.Checked,
                               bat=table.cellWidget(row, 1).currentData(),
                               arguments=split_arguments(table.item(row, 2).text()),
                               build_directory=table.item(row, 3).text().strip()) for row in range(table.rowCount())]

    def compile_tasks(self):
        table = self.compile_table
        return [CompileTask(id=table.item(row, 0).data(Qt.ItemDataRole.UserRole),
                            generation_id=table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1) or "",
                            enabled=table.item(row, 0).checkState() == Qt.CheckState.Checked,
                            solution=table.item(row, 1).text().strip(),
                            target=table.cellWidget(row, 2).currentText().strip(),
                            configuration=table.cellWidget(row, 3).currentText(),
                            platform=table.cellWidget(row, 4).currentText(),
                            action=table.cellWidget(row, 5).currentText()) for row in range(table.rowCount())]

    def add_generation_tasks(self):
        scripts = discover_generation_scripts(Path(self.profile.root))
        if not scripts:
            QMessageBox.warning(self, "没有生成入口", f"未在 {Path(self.profile.root) / 'SetupScript'} 找到生成 BAT")
            return
        picker = QDialog(self)
        picker.setWindowTitle("选择生成 BAT（可多选）")
        layout = QVBoxLayout(picker)
        listing = QListWidget()
        listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        listing.addItems([script.name for script in scripts])
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(picker.accept)
        buttons.rejected.connect(picker.reject)
        layout.addWidget(buttons)
        picker.resize(460, 320)
        if picker.exec() == QDialog.DialogCode.Accepted:
            for item in listing.selectedItems():
                name = item.text()
                args = ["--product", "MiniGame"] if name.lower() in {
                    "generate_projects.bat", "win_product_generateproject.bat"} else []
                self.append_generation(GenerationTask(bat="SetupScript/" + name, arguments=args))

    @staticmethod
    def selected_rows(table):
        return sorted({index.row() for index in table.selectionModel().selectedRows()})

    def copy_generation_tasks(self):
        try:
            tasks = self.generation_tasks()
            for row in self.selected_rows(self.generation_table):
                task = deepcopy(tasks[row])
                task.id = task_id()
                if task.build_directory:
                    task.build_directory += "-copy"
                self.append_generation(task)
        except ValueError as error:
            QMessageBox.warning(self, "无法复制任务", str(error))

    def remove_tasks(self, table):
        self.loading = True
        for row in reversed(self.selected_rows(table)):
            table.removeRow(row)
        self.loading = False
        self.refresh_preview()

    def add_solutions(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择 SLN（可多选）",
                                               str(Path(self.profile.root) / "Projects"), "解决方案 (*.sln)")
        for path in paths:
            try:
                self.append_compile(compile_task_for_solution(Path(path)))
            except (ValueError, OSError) as error:
                QMessageBox.warning(self, "读取 SLN 失败", str(error))

    def load_targets(self):
        try:
            tasks = self.compile_tasks()
            rows = self.selected_rows(self.compile_table) or list(range(len(tasks)))
            for row in rows:
                task = tasks[row]
                targets = read_solution_targets(resolve_build_path(Path(self.profile.root), task.solution))
                combo = self.compile_table.cellWidget(row, 2)
                current = task.target if task.target in targets else compile_task_for_solution(
                    resolve_build_path(Path(self.profile.root), task.solution)).target
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(targets)
                combo.setCurrentText(current)
                combo.blockSignals(False)
            self.refresh_preview()
            self.message.setText(f"已读取 {len(rows)} 个 SLN 的 C++ 目标")
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "读取目标失败", str(error))

    def options(self):
        environment = {}
        for line in self.environment.toPlainText().splitlines():
            if not line.strip():
                continue
            key, sep, value = line.partition("=")
            if not sep or key.strip().upper() in environment:
                raise ValueError("环境变量应为每行 名称=值，名称不能重复")
            environment[key.strip().upper()] = value.strip()
        options = deepcopy(self.profile.build)
        options.steps = [key for key, check in self.steps.items() if check.isChecked()]
        options.generation_tasks, options.compile_tasks = self.generation_tasks(), self.compile_tasks()
        options.python_path, options.setup_install_file = self.python.text().strip(), self.install_file.text().strip()
        options.setup_pack_zip, options.open_monitor = self.pack_zip.isChecked(), self.monitor.isChecked()
        options.ib_skill_dir, options.build_console_path = self.skill_dir.text().strip(), self.build_console.text().strip()
        options.generation_environment = environment
        options.log_directory, options.timeout_minutes = self.log_directory.text().strip(), self.timeout.value()
        validate_build_options(options)
        return options

    def refresh_preview(self, *_):
        if self.loading:
            return
        try:
            updated = deepcopy(self.profile)
            updated.build = self.options()
            self.preview.setPlainText(build_preview(updated))
        except ValueError as error:
            self.preview.setPlainText(str(error))

    def save(self, *_):
        try:
            options = self.options()
            if self.save_options(deepcopy(options)) is False:
                return False
            self.profile.build = options
            self.message.setText("构建任务已保存到当前方案")
            return True
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "构建任务未保存", str(error))
            return False

    def start(self, validate_only=False):
        if self.worker is not None or not self.save():
            return
        self.validate_only, self.generated_changed = validate_only, False
        self.results = {}
        self.logs.clear()
        self.log_path = self.build_directory = ""
        self.output_tabs.setCurrentIndex(1)
        for step, table in (("generate", self.generation_table), ("compile", self.compile_table)):
            for row in range(table.rowCount()):
                self.set_task_status(table, row, "待检查" if validate_only else "待执行")
                if step not in self.profile.build.steps or table.item(row, 0).checkState() != Qt.CheckState.Checked:
                    self.set_task_status(table, row, "未选择")
        self.worker = JobThread(self.data_dir, self.profile, "build-check" if validate_only else "build", [], parent=self)
        self.worker.event.connect(self.handle_event)
        self.worker.completed.connect(self.job_completed)
        self.worker.failed.connect(self.job_failed)
        self.worker.finished.connect(self.job_finished)
        self.message.setText("正在检查配置…" if validate_only else "正在执行构建任务…")
        self.update_controls()
        self.worker.start()

    def set_task_status(self, table, row, label, message="", failed=False):
        self.loading = True
        item = table.item(row, table.columnCount() - 1)
        item.setText(label)
        item.setToolTip(message)
        item.setForeground(QColor("#b45309" if failed else "#182536"))
        self.loading = False

    def task_row(self, step, identity):
        table = self.generation_table if step == "generate" else self.compile_table
        return table, next((row for row in range(table.rowCount())
                            if table.item(row, 0).data(Qt.ItemDataRole.UserRole) == identity), -1)

    def handle_event(self, kind, payload):
        if kind == "log":
            step, line = payload
            self.logs.appendPlainText(f"[{BUILD_STEPS.get(step, '构建')}] {redact(line)}")
        elif kind == "journal":
            self.log_path = payload
        elif kind == "build_directory":
            self.build_directory = payload
            self.update_controls()
        elif kind == "build_compile_tasks":
            self.loading = True
            self.compile_table.setRowCount(0)
            for task in deepcopy(payload):
                self.append_compile(task)
            self.loading = False
            self.generated_changed = True
            self.refresh_preview()
        elif kind == "build_task_started":
            step, identity = payload
            table, row = self.task_row(step, identity)
            if row >= 0:
                self.set_task_status(table, row, "检查中" if self.validate_only else "执行中")
        elif kind == "build_task_result":
            table, row = self.task_row(payload.step, payload.repo_id)
            if row >= 0:
                label = "检查通过" if self.validate_only and payload.outcome == "success" else BUILD_OUTCOMES[payload.outcome]
                self.set_task_status(table, row, label, payload.message, payload.outcome == "failed")
        elif kind == "build_started":
            self.message.setText(("正在检查：" if self.validate_only else "正在执行：") + BUILD_STEPS[payload])
        elif kind == "build_result":
            self.results[payload.repo_id] = payload
            if payload.repo_id in {"generate", "compile"} and not payload.tasks:
                table = self.generation_table if payload.repo_id == "generate" else self.compile_table
                for row in range(table.rowCount()):
                    if table.item(row, 0).checkState() == Qt.CheckState.Checked:
                        self.set_task_status(table, row, BUILD_OUTCOMES[payload.outcome], payload.message,
                                             payload.outcome == "failed")

    def job_completed(self, results):
        self.results = results
        if self.generated_changed:
            self.save()
        succeeded = sum(result.outcome == "success" for result in results.values())
        self.message.setText(f"{'配置检查' if self.validate_only else '构建'}结束：{succeeded}/{len(results)} 个步骤成功；详情见任务状态和日志。")

    def job_failed(self, message):
        if self.generated_changed:
            self.save()
        message = redact(message).strip()
        for table in (self.generation_table, self.compile_table):
            for row in range(table.rowCount()):
                status = table.item(row, table.columnCount() - 1).text()
                if status in {"待执行", "待检查"}:
                    self.set_task_status(table, row, "未执行", message)
                elif status in {"执行中", "检查中"}:
                    self.set_task_status(table, row, "失败", message, failed=True)
        self.logs.appendPlainText(message)
        self.message.setText(message if "\n" not in message else "构建任务未完成，详情见日志")

    def job_finished(self):
        old, self.worker = self.worker, None
        if old:
            old.deleteLater()
        self.update_controls()
        if self.closing_requested:
            self.close()

    def update_controls(self):
        busy = self.worker is not None
        self.settings.setEnabled(not busy)
        for button in (self.save_button, self.check_button, self.run_button):
            button.setEnabled(not busy)
        self.stop_button.setEnabled(busy and not self.worker.stop_requested.is_set())
        self.cancel_button.setEnabled(busy and not self.worker.cancel_requested.is_set())
        self.open_logs_button.setEnabled(bool(self.build_directory or self.log_path))

    def stop_after_step(self):
        if self.worker:
            self.worker.stop_requested.set()
            self.message.setText("当前任务结束后停止")
            self.update_controls()

    def cancel_step(self):
        if self.worker:
            self.worker.stop_requested.set()
            self.worker.cancel_requested.set()
            self.message.setText("正在中断当前任务并停止后续任务")
            self.update_controls()

    def open_logs(self):
        path = Path(self.build_directory) if self.build_directory else Path(self.log_path).parent
        if path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self.worker is None:
            event.accept()
            return
        if not self.closing_requested:
            answer = QMessageBox.question(self, "构建仍在执行", "中断当前任务并关闭？日志将保留。",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.closing_requested = True
                self.cancel_step()
        event.ignore()
