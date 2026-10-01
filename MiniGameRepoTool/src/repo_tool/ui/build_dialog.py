from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from ..build import build_preview
from ..build_config import (
    BUILD_ACTIONS, BUILD_STEPS, CONFIGURATIONS, DEFAULT_TARGET, PLATFORMS, VS_VERSIONS, BuildOptions,
    read_solution_targets, validate_build_options,
)
from ..process import redact
from .worker import JobThread


BUILD_OUTCOMES = {"success": "完成", "failed": "失败", "blocked": "待处理",
                  "cancelled": "已停止", "skipped": "未执行"}


class BuildDialog(QDialog):
    def __init__(self, data_dir: Path, profile, save_options, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.profile = deepcopy(profile)
        self.save_options = save_options
        self.worker = None
        self.closing_requested = False
        self.results = {}
        self.log_path = ""
        self.build_directory = ""
        self.validate_only = False
        self.setWindowTitle("编译构建 — " + profile.name)
        self.resize(1180, 900)
        self.setMinimumSize(920, 720)
        layout = QVBoxLayout(self)
        root = QLabel(f"当前方案：{profile.name}     工程根目录：{profile.root}")
        root.setTextFormat(Qt.TextFormat.PlainText)
        root.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.setWordWrap(True)
        layout.addWidget(root)

        self.settings = QWidget()
        settings_layout = QVBoxLayout(self.settings)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        steps_row = QHBoxLayout()
        steps_row.addWidget(QLabel("执行步骤"))
        self.steps = {}
        for number, (key, name) in enumerate(BUILD_STEPS.items(), 1):
            check = QCheckBox(f"{number}. {name}")
            check.setChecked(key in profile.build.steps)
            self.steps[key] = check
            steps_row.addWidget(check)
        steps_row.addStretch()
        settings_layout.addLayout(steps_row)
        note = QLabel("可单选任一步；多选时按 1 → 2 → 3 执行，失败即停止后续步骤。默认只编译，参数随当前方案保存。")
        note.setWordWrap(True)
        settings_layout.addWidget(note)

        common = QHBoxLayout()
        self.vs_version = self.combo(VS_VERSIONS, profile.build.visual_studio_version)
        self.platform = self.combo(PLATFORMS, profile.build.platform)
        self.python = QLineEdit(profile.build.python_path)
        common.addWidget(QLabel("Visual Studio"))
        common.addWidget(self.vs_version)
        common.addWidget(QLabel("平台"))
        common.addWidget(self.platform)
        common.addWidget(QLabel("Python"))
        common.addWidget(self.python, 1)
        choose_python = QPushButton("选择 Python")
        choose_python.clicked.connect(lambda: self.browse_file(self.python, "选择构建用 Python", "Python (python.exe);;全部文件 (*)"))
        common.addWidget(choose_python)
        settings_layout.addLayout(common)

        self.parameter_tabs = QTabWidget()
        setup_form = self.form_tab("构建设置")
        setup_note = QLabel("对应 SetupScript/Win_Setup.bat：解压所需的内置 Python，执行 Tools/Setup/SetupWin.py。")
        setup_note.setWordWrap(True)
        setup_form.addRow(setup_note)
        self.install_file = QLineEdit(profile.build.setup_install_file)
        self.install_file.setPlaceholderText("留空：按 Dependence.json 下载和设置依赖")
        setup_form.addRow("离线依赖包", self.file_row(self.install_file, "选择依赖安装包", "ZIP (*.zip);;全部文件 (*)"))
        self.pack_zip = QCheckBox("下载依赖并打包为 WinSetup.zip")
        self.pack_zip.setChecked(profile.build.setup_pack_zip)
        setup_form.addRow(self.pack_zip)

        generate_form = self.form_tab("生成 SLN 工程")
        generator_note = QLabel("沿用 VS2019_64-MiniGame.bat 的 dev 生成方式；预设随上方 VS 版本和平台切换。环境参数每行一个“名称=值”。")
        generator_note.setWordWrap(True)
        generate_form.addRow(generator_note)
        self.compile_type = QLineEdit(profile.build.compile_type)
        self.compile_type.setPlaceholderText("对应批处理的第一个参数 mComplieType，可留空")
        generate_form.addRow("生成参数", self.compile_type)
        self.environment = QPlainTextEdit("\n".join(f"{k}={v}" for k, v in profile.build.generation_environment.items()))
        self.environment.setMinimumHeight(140)
        generate_form.addRow("环境参数", self.environment)

        compile_form = self.form_tab("IncrediBuild 编译")
        target_row = QHBoxLayout()
        self.target = self.combo([profile.build.target], profile.build.target)
        self.target.setEditable(True)
        target_row.addWidget(self.target, 1)
        read_targets = QPushButton("读取 SLN 目标")
        read_targets.clicked.connect(self.load_targets)
        target_row.addWidget(read_targets)
        compile_form.addRow("编译目标", target_row)
        config_row = QHBoxLayout()
        self.configuration = self.combo(CONFIGURATIONS, profile.build.configuration)
        self.action = self.combo(BUILD_ACTIONS, profile.build.action)
        config_row.addWidget(self.configuration)
        config_row.addWidget(QLabel("动作"))
        config_row.addWidget(self.action)
        self.monitor = QCheckBox("打开 IB 监视器")
        self.monitor.setToolTip("另开 IncrediBuild 自带的监视窗口；不勾选时编译日志照样实时显示在下方“运行日志”")
        self.monitor.setChecked(profile.build.open_monitor)
        config_row.addWidget(self.monitor)
        config_row.addStretch()
        compile_form.addRow("构建配置", config_row)
        self.skill_dir = QLineEdit(profile.build.ib_skill_dir)
        compile_form.addRow("mini-compile-ib 技能目录", self.directory_row(self.skill_dir))
        self.build_console = QLineEdit(profile.build.build_console_path)
        self.build_console.setPlaceholderText("留空：由技能脚本自动查找 BuildConsole.exe")
        compile_form.addRow("BuildConsole", self.file_row(self.build_console, "选择 BuildConsole.exe", "应用程序 (*.exe)"))
        ib_note = QLabel("Build 增量编译；Rebuild 重新编译；Clean 清理指定目标。编译调用技能脚本，使用已有 SLN，不自动生成工程。"
                         "编译不限时，只能手动中断；编译输出实时显示在下方“运行日志”，并保存到本次构建目录。")
        ib_note.setWordWrap(True)
        compile_form.addRow(ib_note)
        self.parameter_tabs.setCurrentIndex(2)
        self.parameter_tabs.setMinimumHeight(255)
        settings_layout.addWidget(self.parameter_tabs, 1)
        advanced = QHBoxLayout()
        self.timeout = QSpinBox()
        self.timeout.setRange(0, 1440)
        self.timeout.setSpecialValueText("不限时")
        self.timeout.setSuffix(" 分钟")
        self.timeout.setValue(profile.build.timeout_minutes)
        self.timeout.setToolTip("作用于构建设置、生成 SLN 工程和各项检查；IncrediBuild 编译不受此限制")
        advanced.addWidget(QLabel("单个命令超时（不含编译）"))
        advanced.addWidget(self.timeout)
        advanced.addWidget(QLabel("构建日志目录"))
        self.log_directory = QLineEdit(profile.build.log_directory)
        self.log_directory.setPlaceholderText("留空：工具配置目录/builds；每次构建使用独立子目录")
        advanced.addLayout(self.directory_row(self.log_directory), 1)
        settings_layout.addLayout(advanced)
        layout.addWidget(self.settings, 1)

        self.status_table = QTableWidget(3, 3)
        self.status_table.setHorizontalHeaderLabels(["步骤", "状态", "说明"])
        self.status_table.verticalHeader().hide()
        self.status_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.status_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.status_table.setColumnWidth(0, 180)
        self.status_table.setColumnWidth(1, 95)
        self.status_table.setFixedHeight(131)
        for row, label in enumerate(BUILD_STEPS.values()):
            for column, text in enumerate([label, "待执行", ""]):
                self.status_table.setItem(row, column, QTableWidgetItem(text))
        layout.addWidget(self.status_table)
        self.output_tabs = QTabWidget()
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(8000)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.output_tabs.addTab(self.preview, "执行计划")
        self.output_tabs.addTab(self.logs, "运行日志")
        layout.addWidget(self.output_tabs, 1)
        self.message = QLabel("准备就绪；检查配置仅校验入口和 IB 参数，不执行构建。")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        self.save_button = QPushButton("保存参数")
        self.save_button.clicked.connect(self.save)
        self.check_button = QPushButton("检查配置")
        self.check_button.clicked.connect(lambda: self.start(True))
        self.run_button = QPushButton("执行所选步骤")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(lambda: self.start(False))
        self.stop_button = QPushButton("停止后续步骤")
        self.stop_button.clicked.connect(self.stop_after_step)
        self.cancel_button = QPushButton("中断当前步骤")
        self.cancel_button.clicked.connect(self.cancel_step)
        self.open_logs_button = QPushButton("打开日志目录")
        self.open_logs_button.clicked.connect(self.open_logs)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        for button in [self.save_button, self.check_button, self.run_button, self.stop_button,
                       self.cancel_button, self.open_logs_button, self.close_button]:
            actions.addWidget(button)
        layout.addLayout(actions)
        for widget in [self.python, self.install_file, self.compile_type, self.skill_dir, self.build_console, self.log_directory]:
            widget.textChanged.connect(self.refresh_preview)
        for combo in [self.vs_version, self.platform, self.target, self.configuration, self.action]:
            combo.currentTextChanged.connect(self.refresh_preview)
        for check in [*self.steps.values(), self.pack_zip, self.monitor]:
            check.toggled.connect(self.refresh_preview)
        self.environment.textChanged.connect(self.refresh_preview)
        self.timeout.valueChanged.connect(self.refresh_preview)
        self.refresh_preview()
        self.update_controls()

    @staticmethod
    def combo(values, selected):
        combo = QComboBox()
        combo.addItems(values)
        combo.setCurrentText(selected)
        return combo

    def form_tab(self, title):
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.parameter_tabs.addTab(scroll, title)
        return form

    def file_row(self, edit, title, file_filter):
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        button = QPushButton("浏览")
        button.clicked.connect(lambda: self.browse_file(edit, title, file_filter))
        row.addWidget(button)
        return row

    def directory_row(self, edit):
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        button = QPushButton("浏览")
        button.clicked.connect(lambda: self.browse_directory(edit))
        row.addWidget(button)
        return row

    def browse_file(self, edit, title, file_filter):
        value, _ = QFileDialog.getOpenFileName(self, title, self.profile.root, file_filter)
        if value:
            edit.setText(value)

    def browse_directory(self, edit):
        value = QFileDialog.getExistingDirectory(self, "选择目录", self.profile.root)
        if value:
            edit.setText(value)

    def options(self) -> BuildOptions:
        environment = {}
        for line in self.environment.toPlainText().splitlines():
            if not line.strip():
                continue
            key, sep, value = line.partition("=")
            if not sep or key.strip() in environment:
                raise ValueError("生成环境参数应为每行 名称=值，名称不能重复")
            environment[key.strip()] = value.strip()
        options = BuildOptions(
            steps=[key for key, check in self.steps.items() if check.isChecked()],
            python_path=self.python.text().strip(), setup_install_file=self.install_file.text().strip(),
            setup_pack_zip=self.pack_zip.isChecked(), compile_type=self.compile_type.text(),
            generation_environment=environment, target=self.target.currentText().strip(),
            configuration=self.configuration.currentText(), platform=self.platform.currentText(),
            visual_studio_version=self.vs_version.currentText(), action=self.action.currentText(),
            ib_skill_dir=self.skill_dir.text().strip(), build_console_path=self.build_console.text().strip(),
            log_directory=self.log_directory.text().strip(), open_monitor=self.monitor.isChecked(),
            timeout_minutes=self.timeout.value())
        validate_build_options(options)
        return options

    def refresh_preview(self):
        try:
            updated = deepcopy(self.profile)
            updated.build = self.options()
            self.preview.setPlainText(build_preview(updated))
        except ValueError as error:
            self.preview.setPlainText(str(error))
        if self.worker is None:
            self.results = {}
            for row, key in enumerate(BUILD_STEPS):
                self.status_table.item(row, 1).setText("待执行" if self.steps[key].isChecked() else "未选择")
                self.status_table.item(row, 1).setForeground(QColor("#182536"))
                self.status_table.item(row, 2).setText("")

    def save(self) -> bool:
        try:
            options = self.options()
            if self.save_options(deepcopy(options)) is False:
                return False
            self.profile.build = options
            self.message.setText("构建参数已保存到当前方案")
            return True
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "构建参数未保存", str(error))
            return False

    def load_targets(self):
        try:
            options = self.options()
            targets = read_solution_targets(options.solution(Path(self.profile.root)))
            if not targets:
                raise ValueError("SLN 中没有找到 C++ 编译目标")
            current = self.target.currentText().strip()
            message = f"已读取 {len(targets)} 个编译目标"
            if current.casefold() not in {name.casefold() for name in targets} and DEFAULT_TARGET in targets:
                message += f"；{current or '当前目标'} 不是 C++ 工程，已切换为 {DEFAULT_TARGET}"
                current = DEFAULT_TARGET
            self.target.blockSignals(True)
            self.target.clear()
            self.target.addItems(targets)
            self.target.setCurrentText(current)
            self.target.blockSignals(False)
            self.refresh_preview()
            self.message.setText(message)
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "读取目标失败", str(error))

    def start(self, validate_only=False):
        if self.worker is not None or not self.save():
            return
        self.validate_only = validate_only
        self.results = {}
        self.logs.clear()
        self.log_path = self.build_directory = ""
        self.output_tabs.setCurrentIndex(1)
        for row, key in enumerate(BUILD_STEPS):
            self.status_table.item(row, 1).setText("待检查" if validate_only else "待执行")
            if key not in self.profile.build.steps:
                self.status_table.item(row, 1).setText("未选择")
            self.status_table.item(row, 1).setForeground(QColor("#182536"))
            self.status_table.item(row, 2).setText("")
        self.worker = JobThread(self.data_dir, self.profile, "build-check" if validate_only else "build", [], parent=self)
        self.worker.event.connect(self.handle_event)
        self.worker.completed.connect(self.job_completed)
        self.worker.failed.connect(self.job_failed)
        self.worker.finished.connect(self.job_finished)
        self.message.setText("正在检查构建配置…" if validate_only else "正在执行构建步骤…")
        self.update_controls()
        self.worker.start()

    def handle_event(self, kind, payload):
        if kind == "log":
            step, line = payload
            self.logs.appendPlainText(f"[{BUILD_STEPS.get(step, '构建')}] {redact(line)}")
        elif kind == "journal":
            self.log_path = payload
        elif kind == "build_directory":
            self.build_directory = payload
            self.update_controls()
        elif kind == "build_started":
            row = list(BUILD_STEPS).index(payload)
            self.status_table.item(row, 1).setText("检查中" if self.validate_only else "执行中")
            self.message.setText(("正在检查：" if self.validate_only else "正在执行：") + BUILD_STEPS[payload])
        elif kind == "build_result":
            self.results[payload.repo_id] = payload
            row = list(BUILD_STEPS).index(payload.repo_id)
            label = "检查通过" if self.validate_only and payload.outcome == "success" else BUILD_OUTCOMES[payload.outcome]
            self.status_table.item(row, 1).setText(label)
            self.status_table.item(row, 1).setForeground(QColor("#188153" if payload.outcome == "success" else "#b45309"))
            self.status_table.item(row, 2).setText(payload.message)
            self.status_table.item(row, 2).setToolTip(payload.message)

    def job_completed(self, results):
        self.results = results
        succeeded = sum(result.outcome == "success" for result in results.values())
        self.message.setText(f"{'配置检查' if self.validate_only else '构建执行'}结束：{succeeded} / {len(results)} 项成功；详情见各步骤和日志。")

    def job_failed(self, message):
        self.logs.appendPlainText(redact(message))
        self.message.setText("构建任务未完成，详情见日志")

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
        for button in [self.save_button, self.check_button, self.run_button]:
            button.setEnabled(not busy)
        self.stop_button.setEnabled(busy and not self.worker.stop_requested.is_set())
        self.cancel_button.setEnabled(busy and not self.worker.cancel_requested.is_set())
        self.open_logs_button.setEnabled(bool(self.build_directory or self.log_path))

    def stop_after_step(self):
        if self.worker:
            self.worker.stop_requested.set()
            self.message.setText("当前步骤结束后停止，不执行后续步骤")
            self.update_controls()

    def cancel_step(self):
        if self.worker:
            self.worker.stop_requested.set()
            self.worker.cancel_requested.set()
            self.message.setText("正在中断当前步骤并停止后续步骤；IB 使用本次会话文件停止")
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
            answer = QMessageBox.question(self, "构建仍在执行", "中断当前步骤并关闭？本次构建日志将保留。",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.closing_requested = True
                self.cancel_step()
        event.ignore()
