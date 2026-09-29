from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..models import ACTION_NAMES, OUTCOME_NAMES, STATUS_NAMES, dependencies, dependency_order, validate_profile
from ..process import redact
from ..profiles import ProfileStore
from .dialogs import ProfilesDialog
from .worker import JobThread


class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path, auto_scan: bool = True):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.store = ProfileStore(self.data_dir)
        self.document = self.store.load()
        self.profile = next(p for p in self.document.profiles if p.id == self.document.active_profile_id)
        self.worker: JobThread | None = None
        self.snapshots = {}
        self.results = {}
        self.items = {}
        self.log_entries: list[tuple[str, str]] = []
        self.log_path = ""
        self.branch_edits = {}
        self.closing_requested = False
        self.operation = ""
        self.branch_choice_repo = ""
        self.pending_branches = None
        self.setWindowTitle("MiniGame 仓库管理器")
        self.resize(1420, 900)
        self.setMinimumSize(1000, 650)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        header = QHBoxLayout()
        header.addWidget(QLabel("当前方案"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(180)
        header.addWidget(self.profile_combo)
        self.root_label = QLabel()
        self.root_label.setTextFormat(Qt.TextFormat.PlainText)
        self.root_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header.addWidget(self.root_label, 1)
        self.save_button = QPushButton("保存分支设置")
        self.save_button.clicked.connect(self.save_branch_edits)
        header.addWidget(self.save_button)
        self.manage_button = QPushButton("管理方案")
        self.manage_button.clicked.connect(self.manage_profiles)
        header.addWidget(self.manage_button)
        layout.addLayout(header)
        self.branch_group = QGroupBox("目标分支")
        self.branch_layout = QGridLayout(self.branch_group)
        layout.addWidget(self.branch_group)
        tools = QHBoxLayout()
        self.check_button = QPushButton("检查状态")
        self.check_button.clicked.connect(lambda: self.start_job("check", self.selected_ids()))
        self.sync_button = QPushButton("同步所选")
        self.sync_button.setObjectName("primary")
        self.sync_button.setDefault(True)
        self.sync_button.clicked.connect(lambda: self.start_job("sync", self.selected_ids()))
        self.retry_button = QPushButton("重试失败")
        self.retry_button.clicked.connect(self.retry)
        self.stop_button = QPushButton("停止队列")
        self.stop_button.clicked.connect(self.stop_queue)
        self.cancel_button = QPushButton("中断当前操作")
        self.cancel_button.clicked.connect(self.cancel_current)
        for button in [self.check_button, self.sync_button, self.retry_button, self.stop_button, self.cancel_button]:
            tools.addWidget(button)
        self.selection_label = QLabel()
        tools.addStretch()
        tools.addWidget(self.selection_label)
        layout.addLayout(tools)
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels(["仓库", "当前分支", "目标分支", "当前状态", "计划动作", "本次结果", "说明"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, width in enumerate([220, 155, 155, 110, 100, 100, 260]):
            self.tree.setColumnWidth(col, width)
        self.tree.itemSelectionChanged.connect(self.show_detail)
        self.tree.itemChanged.connect(self.selection_changed)
        main_splitter.addWidget(self.tree)
        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        self.detail_title = QLabel("仓库详情")
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        self.detail_title.setFont(font)
        detail_layout.addWidget(self.detail_title)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        detail_layout.addWidget(self.detail)
        detail_buttons = QGridLayout()
        self.single_button = QPushButton("同步此仓库")
        self.single_button.clicked.connect(lambda: self.start_job("sync", [self.current_repo_id()] if self.current_repo_id() else []))
        self.edit_button = QPushButton("编辑仓库")
        self.edit_button.clicked.connect(lambda: self.manage_profiles(self.current_repo_id()))
        self.branch_button = QPushButton("读取远端分支")
        self.branch_button.clicked.connect(self.fetch_branches)
        self.adopt_button = QPushButton("采用实际 origin")
        self.adopt_button.clicked.connect(self.adopt_origin)
        self.open_button = QPushButton("打开仓库目录")
        self.open_button.clicked.connect(self.open_repo)
        for index, button in enumerate([self.single_button, self.edit_button, self.branch_button, self.adopt_button, self.open_button]):
            detail_buttons.addWidget(button, index // 2, index % 2)
        detail_layout.addLayout(detail_buttons)
        main_splitter.addWidget(detail_widget)
        main_splitter.setSizes([1040, 350])
        self.vertical_splitter.addWidget(main_splitter)
        self.tabs = QTabWidget()
        self.plan = QPlainTextEdit()
        self.plan.setReadOnly(True)
        self.tabs.addTab(self.plan, "执行计划")
        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_toolbar = QHBoxLayout()
        self.log_filter = QComboBox()
        self.log_filter.addItems(["全部仓库", "选中仓库"])
        self.log_filter.currentIndexChanged.connect(self.refresh_logs)
        log_toolbar.addWidget(self.log_filter)
        copy_log = QPushButton("复制当前视图")
        copy_log.clicked.connect(lambda: QApplication.clipboard().setText(self.logs.toPlainText()))
        export_log = QPushButton("导出日志")
        export_log.clicked.connect(self.export_logs)
        open_log = QPushButton("打开运行记录目录")
        open_log.clicked.connect(self.open_history)
        for button in [copy_log, export_log, open_log]:
            log_toolbar.addWidget(button)
        log_toolbar.addStretch()
        log_layout.addLayout(log_toolbar)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(5000)
        log_layout.addWidget(self.logs)
        self.tabs.addTab(log_page, "运行日志")
        self.vertical_splitter.addWidget(self.tabs)
        self.vertical_splitter.setSizes([570, 230])
        layout.addWidget(self.vertical_splitter, 1)
        footer = QHBoxLayout()
        self.message = QLabel("准备就绪")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        footer.addWidget(self.message, 1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(140)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        footer.addWidget(self.progress)
        self.setup_button = QPushButton("准备构建环境")
        self.setup_button.clicked.connect(lambda: self.start_job("setup", []))
        footer.addWidget(self.setup_button)
        layout.addLayout(footer)
        self.profile_combo.currentIndexChanged.connect(self.change_profile)
        self.fill_profiles()
        self.load_profile()
        if auto_scan:
            QTimer.singleShot(0, lambda: self.start_job("local", [r.id for r in self.profile.repositories]))

    def fill_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for p in self.document.profiles:
            self.profile_combo.addItem(p.name, p.id)
        self.profile_combo.setCurrentIndex(self.profile_combo.findData(self.profile.id))
        self.profile_combo.blockSignals(False)

    def load_profile(self):
        self.root_label.setText(self.profile.root)
        while self.branch_layout.count():
            widget = self.branch_layout.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        self.branch_edits = {}
        for index, (name, branch) in enumerate(self.profile.branch_groups.items()):
            edit = QLineEdit(branch)
            edit.textEdited.connect(lambda _: self.message.setText("分支设置已修改，操作前将保存并重新检查"))
            self.branch_edits[name] = edit
            row, column = index // 2, (index % 2) * 2
            self.branch_layout.addWidget(QLabel(name), row, column)
            self.branch_layout.addWidget(edit, row, column + 1)
        self.snapshots = {}
        self.results = {}
        self.log_entries = []
        self.log_path = ""
        self.tree.blockSignals(True)
        self.tree.clear()
        self.items = {}
        deps = dependencies(self.profile)
        for key in dependency_order(self.profile):
            repo = self.profile.repo(key)
            path = self.profile.directory(repo)
            parents = [p for p in self.profile.repositories
                       if p.id != key and self.profile.directory(p) in path.parents]
            parent = max(parents, key=lambda p: len(self.profile.directory(p).parts)) if parents else None
            node = QTreeWidgetItem(["", "", self.profile.target(repo), "尚未检查", "", "", ""])
            node.setText(0, repo.name)
            node.setData(0, Qt.ItemDataRole.UserRole, key)
            node.setFlags(node.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            node.setCheckState(0, Qt.CheckState.Checked if repo.enabled else Qt.CheckState.Unchecked)
            node.setToolTip(2, self.profile.target(repo))
            if parent:
                self.items[parent.id].addChild(node)
            else:
                self.tree.addTopLevelItem(node)
            self.items[key] = node
        self.tree.expandAll()
        self.tree.blockSignals(False)
        if self.items:
            self.tree.setCurrentItem(next(iter(self.items.values())))
        self.refresh_plan()
        self.refresh_logs()
        self.update_controls()

    def selected_ids(self) -> list[str]:
        return [key for key, node in self.items.items() if node.checkState(0) == Qt.CheckState.Checked]

    def current_repo_id(self) -> str:
        node = self.tree.currentItem()
        return node.data(0, Qt.ItemDataRole.UserRole) if node else ""

    def selection_changed(self, node, column):
        if column == 0 and self.worker is None:
            self.refresh_plan()
            self.update_controls()

    def save_branch_edits(self) -> bool:
        if self.worker is not None:
            return False
        updated = deepcopy(self.profile)
        updated.branch_groups = {name: edit.text().strip() for name, edit in self.branch_edits.items()}
        return self.save_profile(updated)

    def save_profile(self, updated) -> bool:
        if self.worker is not None:
            return False
        try:
            validate_profile(updated)
            changed = updated != self.profile
            index = self.document.profiles.index(self.profile)
            new_document = deepcopy(self.document)
            new_document.profiles[index] = updated
            self.store.save(new_document)
            self.document = new_document
            self.profile = new_document.profiles[index]
            if changed:
                self.snapshots.clear()
                self.results.clear()
                for key, node in self.items.items():
                    node.setText(2, self.profile.target(self.profile.repo(key)))
                    node.setToolTip(2, node.text(2))
                    for column in [3, 4, 5, 6]:
                        node.setText(column, "待重新检查" if column == 3 else "")
                self.refresh_plan()
                self.show_detail()
            self.message.setText("方案设置已保存")
            return True
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "设置无效", str(error))
            return False

    def change_profile(self, index):
        if index < 0 or self.worker is not None:
            return
        key = self.profile_combo.itemData(index)
        if key == self.profile.id:
            return
        if not self.save_branch_edits():
            self.fill_profiles()
            return
        self.profile = next(p for p in self.document.profiles if p.id == key)
        self.document.active_profile_id = key
        try:
            self.store.save(self.document)
        except OSError as error:
            QMessageBox.warning(self, "保存失败", str(error))
        self.load_profile()
        self.start_job("local", [r.id for r in self.profile.repositories])

    def manage_profiles(self, focus_repo=None):
        if self.worker is not None or not self.save_branch_edits():
            return
        dialog = ProfilesDialog(self.document, self, focus_repo if isinstance(focus_repo, str) else None)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.store.save(dialog.document)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self.document = dialog.document
        self.profile = next(p for p in self.document.profiles if p.id == self.document.active_profile_id)
        self.fill_profiles()
        self.load_profile()
        self.start_job("local", [r.id for r in self.profile.repositories])

    def refresh_plan(self):
        chosen = set(self.selected_ids())
        lines = []
        deps = dependencies(self.profile)
        for key in dependency_order(self.profile):
            if key not in chosen:
                continue
            repo = self.profile.repo(key)
            snap = self.snapshots.get(key)
            operation = ACTION_NAMES.get(snap.action, "检查") if snap else "检查后决定"
            dependency = ", ".join(self.profile.repo(p).name for p in deps[key]) or "无"
            lines.append(f"{repo.name}  →  {operation}\n    目标：{self.profile.target(repo)}；前置：{dependency}")
        lines.append("\n缺失或未就绪的必要前置仓库会加入队列；已可用的未选父仓库不会被更新。")
        self.plan.setPlainText("\n".join(lines))
        self.selection_label.setText(f"已选 {len(chosen)} / {len(self.items)}")

    def show_detail(self):
        key = self.current_repo_id()
        if not key:
            return
        repo = self.profile.repo(key)
        snap = self.snapshots.get(key)
        self.detail_title.setText(repo.name)
        parts = [f"本地目录\n{self.profile.directory(repo)}", f"配置远端\n{repo.remote}",
                 f"目标分支\n{self.profile.target(repo)}"]
        if snap:
            parts.extend([f"当前分支\n{snap.current_branch or '尚未创建'}",
                          f"实际 origin\n{snap.origin or '未检测到'}",
                          f"状态\n{STATUS_NAMES.get(snap.status, snap.status)}：{snap.message}",
                          f"检测时间\n{snap.checked_at or '—'}（{'已获取远端' if snap.remote_checked else '本地 / 缓存'}）"])
            if snap.changes:
                parts.append("工作区变更\n" + "\n".join(snap.changes[:100]))
        if key in self.results:
            parts.append("本次结果\n" + self.results[key].message)
        self.detail.setPlainText(redact("\n\n".join(parts)))
        self.refresh_logs()
        self.update_controls()

    def update_controls(self):
        busy = self.worker is not None
        for button in [self.check_button, self.sync_button, self.manage_button, self.save_button,
                       self.single_button, self.edit_button, self.branch_button]:
            button.setEnabled(not busy)
        self.profile_combo.setEnabled(not busy)
        self.branch_group.setEnabled(not busy)
        self.tree.setEnabled(not busy or self.operation in {"sync", "check", "local", "setup"})
        self.tree.blockSignals(True)
        for node in self.items.values():
            flags = node.flags()
            node.setFlags(flags & ~Qt.ItemFlag.ItemIsUserCheckable if busy
                          else flags | Qt.ItemFlag.ItemIsUserCheckable)
        self.tree.blockSignals(False)
        self.sync_button.setEnabled(not busy and bool(self.selected_ids()))
        self.check_button.setEnabled(not busy and bool(self.selected_ids()))
        self.retry_button.setEnabled(not busy and any(r.outcome in {"failed", "blocked"} for r in self.results.values()))
        self.stop_button.setEnabled(busy and self.operation != "branches" and not self.worker.stop_requested.is_set())
        self.cancel_button.setEnabled(busy and not self.worker.cancel_requested.is_set())
        key = self.current_repo_id()
        snap = self.snapshots.get(key)
        self.adopt_button.setEnabled(not busy and snap is not None and snap.status == "remote_mismatch" and bool(snap.origin))
        # Setup performs its own complete prerequisite check in the worker.
        self.setup_button.setEnabled(not busy)

    def start_job(self, operation: str, selected: list[str], extra=None):
        if self.worker is not None:
            return
        if operation not in {"setup", "branches"} and not selected:
            return
        if not self.save_branch_edits():
            return
        self.operation = operation
        if operation in {"sync", "setup"}:
            if operation == "sync":
                for key in selected:
                    self.results.pop(key, None)
                    if key in self.items:
                        self.items[key].setText(5, "")
            self.log_entries = []
            self.log_path = ""
            self.tabs.setCurrentIndex(1)
        self.worker = JobThread(self.data_dir, self.profile, operation, selected, extra, self)
        self.worker.event.connect(self.handle_event)
        self.worker.completed.connect(self.job_completed)
        self.worker.failed.connect(self.job_failed)
        self.worker.finished.connect(self.job_finished)
        self.progress.setRange(0, 0)
        self.message.setText({"local": "读取本地仓库状态…", "check": "检查本地与远端状态…",
                              "sync": "正在同步仓库…", "branches": "读取远端分支…",
                              "setup": "检查所需仓库并准备环境…"}.get(operation, "执行中…"))
        self.update_controls()
        self.worker.start()

    def handle_event(self, kind: str, payload):
        if kind == "snapshot":
            self.snapshots[payload.repo_id] = payload
            node = self.items.get(payload.repo_id)
            if node:
                node.setText(1, payload.current_branch or "—")
                node.setToolTip(1, node.text(1))
                node.setText(3, STATUS_NAMES.get(payload.status, payload.status))
                color = ("#188153" if payload.ready else "#b45309" if payload.action == "block"
                         else "#b42318" if payload.action == "error" else "#2563eb")
                node.setForeground(3, QColor(color))
                node.setText(4, ACTION_NAMES.get(payload.action, payload.action))
                node.setText(6, payload.message.splitlines()[0] if payload.message else "")
                node.setToolTip(6, payload.message)
            self.refresh_plan()
            self.show_detail()
        elif kind == "result":
            self.results[payload.repo_id] = payload
            node = self.items.get(payload.repo_id)
            if node:
                node.setText(5, OUTCOME_NAMES[payload.outcome])
                node.setForeground(5, QColor({"success": "#188153", "failed": "#b42318",
                                              "blocked": "#b45309", "cancelled": "#697586"}[payload.outcome]))
                node.setToolTip(5, payload.message)
            self.show_detail()
        elif kind == "included":
            self.items[payload].setCheckState(0, Qt.CheckState.Checked)
            self.append_log(payload, "已加入必要的前置仓库")
            self.refresh_plan()
        elif kind == "started":
            name = self.profile.repo(payload).name if payload in self.items else "构建环境"
            self.message.setText("正在处理：" + name)
        elif kind == "log":
            self.append_log(*payload)
        elif kind == "journal":
            self.log_path = payload

    def append_log(self, repo_id: str, line: str):
        line = redact(line)
        self.log_entries.append((repo_id, line))
        if len(self.log_entries) > 5000:
            self.log_entries = self.log_entries[-5000:]
        if self.log_filter.currentIndex() == 0 or self.current_repo_id() == repo_id:
            self.logs.appendPlainText(f"[{repo_id or '系统'}] {line}")

    def refresh_logs(self):
        key = self.current_repo_id()
        entries = self.log_entries if self.log_filter.currentIndex() == 0 else [
            item for item in self.log_entries if item[0] == key]
        self.logs.setPlainText("\n".join(f"[{repo or '系统'}] {line}" for repo, line in entries))
        self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())

    def job_completed(self, result):
        if self.operation == "branches":
            self.pending_branches = result
            self.message.setText(f"已读取 {len(result)} 个远端分支")
        elif self.operation in {"sync", "setup"}:
            counts = {s: sum(r.outcome == s for r in result.values()) for s in OUTCOME_NAMES}
            self.message.setText("执行结束：" + " · ".join(f"{OUTCOME_NAMES[s]} {n}" for s, n in counts.items() if n))
        else:
            self.message.setText(f"检查完成：{len(result)} 个仓库")

    def job_failed(self, text):
        self.append_log("系统", text)
        self.tabs.setCurrentIndex(1)
        self.message.setText("任务失败，详情见运行日志")

    def job_finished(self):
        old = self.worker
        self.worker = None
        if old:
            old.deleteLater()
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.update_controls()
        if self.closing_requested:
            self.close()
            return
        if self.pending_branches is not None:
            branches, self.pending_branches = self.pending_branches, None
            if branches:
                value, accepted = QInputDialog.getItem(self, "选择目标分支", "修改此仓库使用的分支组或固定分支", branches, 0, True)
                if accepted and value:
                    repo = self.profile.repo(self.branch_choice_repo)
                    if repo.branch_group in self.branch_edits:
                        self.branch_edits[repo.branch_group].setText(value)
                        self.save_branch_edits()
                    else:
                        updated = deepcopy(self.profile)
                        updated.repo(repo.id).branch = value
                        self.save_profile(updated)

    def retry(self):
        ids = [key for key, result in self.results.items()
               if key in self.items and result.outcome in {"failed", "blocked"}]
        if ids:
            self.start_job("sync", ids)
        elif "__setup__" in self.results:
            self.start_job("setup", [])

    def stop_queue(self):
        if self.worker:
            self.worker.stop_requested.set()
            self.message.setText("将在当前仓库完成后停止队列")
            self.update_controls()

    def cancel_current(self):
        if self.worker:
            self.worker.stop_requested.set()
            self.worker.cancel_requested.set()
            self.message.setText("正在中断当前进程并停止队列，完成后可重新检查")
            self.update_controls()

    def fetch_branches(self):
        key = self.current_repo_id()
        if key:
            self.branch_choice_repo = key
            self.start_job("branches", [], self.profile.repo(key).remote)

    def adopt_origin(self):
        key = self.current_repo_id()
        snapshot = self.snapshots.get(key)
        if snapshot and snapshot.origin:
            updated = deepcopy(self.profile)
            updated.branch_groups = {name: edit.text().strip() for name, edit in self.branch_edits.items()}
            updated.repo(key).remote = snapshot.origin
            if self.save_profile(updated):
                self.start_job("local", [key])

    def open_repo(self):
        key = self.current_repo_id()
        if key:
            path = self.profile.directory(self.profile.repo(key))
            if path.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            else:
                self.message.setText("目录尚未创建")

    def open_history(self):
        folder = self.data_dir / "runs"
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def export_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", "repo-sync.log", "日志 (*.log)")
        if not path:
            return
        try:
            if self.log_filter.currentIndex() == 0 and self.log_path and Path(self.log_path).exists():
                content = Path(self.log_path).read_text(encoding="utf-8")
            else:
                content = self.logs.toPlainText()
            Path(path).write_text(redact(content), encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "导出失败", str(error))

    def closeEvent(self, event):
        if self.worker is None:
            event.accept()
            return
        if not self.closing_requested:
            answer = QMessageBox.question(self, "任务仍在执行", "中断当前任务并退出？已有文件和运行记录将保留。",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.closing_requested = True
                self.cancel_current()
        event.ignore()
