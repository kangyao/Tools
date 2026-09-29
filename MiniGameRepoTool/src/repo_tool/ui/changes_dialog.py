from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..changes import ChangeSnapshot, FileChange
from ..models import Profile, canonical
from .worker import JobThread


STATUS_LABELS = {".": "—", "?": "未跟踪", "M": "修改", "A": "新增", "D": "删除",
                 "R": "重命名", "C": "复制", "T": "类型变化", "U": "冲突"}
KIND_LABELS = {"tracked": "已跟踪", "untracked": "未跟踪", "submodule": "子模块", "conflict": "冲突"}


class ChangesDialog(QDialog):
    def __init__(self, data_dir: Path, profile: Profile, repo_id: str, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.profile = deepcopy(profile)
        self.repo_id = repo_id
        self.history: list[str] = []
        self.snapshot: ChangeSnapshot | None = None
        self.items: dict[str, QTreeWidgetItem] = {}
        self.worker: JobThread | None = None
        self.operation = ""
        self.last_backup = ""
        self.mutated = False
        self.closing_requested = False
        self.result_message = ""
        self.resize(1250, 820)
        self.setMinimumSize(880, 620)
        layout = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.title)
        self.hint = QLabel()
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        toolbar = QHBoxLayout()
        self.back_button = QPushButton("返回上级仓库")
        self.back_button.clicked.connect(self.go_back)
        self.refresh_button = QPushButton("刷新文件")
        self.refresh_button.clicked.connect(self.refresh)
        self.all_button = QPushButton("全选可丢弃文件")
        self.all_button.clicked.connect(lambda: self.check_all(True))
        self.none_button = QPushButton("取消勾选")
        self.none_button.clicked.connect(lambda: self.check_all(False))
        self.diff_button = QPushButton("查看 Diff")
        self.diff_button.clicked.connect(self.show_diff)
        self.child_button = QPushButton("进入子仓库")
        self.child_button.clicked.connect(self.open_child)
        for button in (self.back_button, self.refresh_button, self.all_button, self.none_button,
                       self.diff_button, self.child_button):
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTreeWidget()
        self.table.setColumnCount(5)
        self.table.setHeaderLabels(["勾选 / 文件路径", "类型", "已暂存", "未暂存", "处理说明"])
        self.table.setAlternatingRowColors(True)
        self.table.setUniformRowHeights(True)
        self.table.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((490, 85, 80, 80, 360)):
            self.table.setColumnWidth(column, width)
        self.table.itemChanged.connect(self.update_controls)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemDoubleClicked.connect(lambda *_: self.show_diff())
        splitter.addWidget(self.table)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview.setPlaceholderText("选中文件后点击“查看 Diff”，或双击文件。勾选框用于选择要丢弃的文件。")
        splitter.addWidget(self.preview)
        splitter.setSizes([360, 260])
        layout.addWidget(splitter, 1)
        footer = QHBoxLayout()
        self.count_label = QLabel()
        footer.addWidget(self.count_label)
        footer.addStretch()
        self.backup_button = QPushButton("打开上次备份")
        self.backup_button.clicked.connect(self.open_backup)
        footer.addWidget(self.backup_button)
        self.discard_button = QPushButton("备份并丢弃所选（Discard）")
        self.discard_button.clicked.connect(self.discard_selected)
        footer.addWidget(self.discard_button)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        status = QHBoxLayout()
        self.message = QLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        status.addWidget(self.message, 1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(120)
        self.progress.setTextVisible(False)
        status.addWidget(self.progress)
        layout.addLayout(status)
        self.update_title()
        self.update_controls()
        QTimer.singleShot(0, self.refresh)

    def update_title(self):
        repo = self.profile.repo(self.repo_id)
        self.setWindowTitle(f"文件改动 / Discard — {repo.name}")
        self.title.setText(f"{repo.name}  ·  {self.profile.directory(repo)}")
        self.hint.setText("勾选文件后可先备份再丢弃。已跟踪文件恢复到当前提交，包含其暂存和未暂存改动；未跟踪文件备份后删除。")

    def current_path(self) -> str:
        item = self.table.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else ""

    def selection_changed(self):
        self.preview.clear()
        self.message.setText("已选中：" + self.current_path() + "；双击或点击“查看 Diff”。")
        self.update_controls()

    def checked_rows(self) -> list[FileChange]:
        if self.snapshot is None:
            return []
        return [row for row in self.snapshot.files if row.discardable and row.path in self.items
                and self.items[row.path].checkState(0) == Qt.CheckState.Checked]

    def child_repo_id(self) -> str:
        if not self.snapshot or not self.current_path():
            return ""
        target = canonical(Path(self.snapshot.root) / self.current_path().rstrip("/"))
        return next((repo.id for repo in self.profile.repositories
                     if repo.id != self.repo_id and canonical(self.profile.directory(repo)) == target), "")

    def update_controls(self, *_):
        busy = self.worker is not None
        chosen = len(self.checked_rows())
        available = sum(row.discardable for row in self.snapshot.files) if self.snapshot else 0
        total = len(self.snapshot.files) if self.snapshot else 0
        self.count_label.setText(f"共 {total} 项 · 可丢弃 {available} 项 · 已勾选 {chosen} 项")
        self.table.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        self.back_button.setEnabled(not busy and bool(self.history))
        self.all_button.setEnabled(not busy and available > 0)
        self.none_button.setEnabled(not busy and chosen > 0)
        self.diff_button.setEnabled(not busy and bool(self.current_path()))
        self.child_button.setEnabled(not busy and bool(self.child_repo_id()))
        self.discard_button.setEnabled(not busy and chosen > 0)
        self.backup_button.setEnabled(bool(self.last_backup))
        self.progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.progress.setValue(0)

    def check_all(self, checked: bool):
        if self.worker or not self.snapshot:
            return
        self.table.blockSignals(True)
        for row in self.snapshot.files:
            if row.discardable:
                self.items[row.path].setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self.update_controls()

    def refresh(self):
        if self.worker is None and not self.closing_requested:
            self.start_job("changes")

    def load_snapshot(self, snapshot: ChangeSnapshot):
        self.snapshot = snapshot
        previous = self.current_path()
        self.table.blockSignals(True)
        self.table.clear()
        self.items = {}
        for row in snapshot.files:
            name = f"{row.original_path} → {row.path}" if row.original_path else row.path
            action = row.reason or ("备份后删除" if row.kind == "untracked" else "恢复到当前提交")
            item = QTreeWidgetItem([name, KIND_LABELS[row.kind],
                                   STATUS_LABELS.get(row.index_status, row.index_status),
                                   STATUS_LABELS.get(row.worktree_status, row.worktree_status), action])
            item.setData(0, Qt.ItemDataRole.UserRole, row.path)
            for column in range(5):
                item.setToolTip(column, item.text(column))
            if row.discardable:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setForeground(4, QColor("#b45309"))
            self.table.addTopLevelItem(item)
            self.items[row.path] = item
        if previous in self.items:
            self.table.setCurrentItem(self.items[previous])
        self.table.blockSignals(False)
        self.update_title()
        extra = f"当前提交：{snapshot.head[:12]}。"
        if snapshot.submodule:
            extra += " 当前是 Git 子模块；这里只丢弃内部文件改动，保留子模块提交位置。"
        if snapshot.operation:
            extra += " 存在未完成的 Git 操作，暂不可丢弃。"
        self.hint.setText(extra + "\n" + self.hint.text())
        self.preview.clear()
        self.message.setText(self.result_message or ("没有文件改动。" if not snapshot.files
                                                    else "双击文件查看 Diff；仅勾选的文件会被丢弃。"))

    def show_diff(self):
        if self.worker is None and self.current_path():
            self.start_job("file-diff", self.current_path())

    def confirm_discard(self, rows: list[FileChange]) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("确认丢弃所选文件改动")
        box.setText(f"备份并丢弃 {len(rows)} 项改动？")
        box.setInformativeText(
            f"仓库：{self.profile.repo(self.repo_id).name}\n"
            f"恢复到当前提交：{self.snapshot.head[:12]}\n\n"
            "已跟踪文件：丢弃选中文件的全部暂存和未暂存改动。\n"
            "未跟踪文件：先备份，再从工作区删除。\n"
            "未勾选的文件保持原样。展开“详细信息”可核对全部路径。\n\n"
            "备份完成并再次校验文件状态后才执行。")
        box.setDetailedText("\n".join(
            (f"{row.original_path} → {row.path}" if row.original_path else row.path)
            + ("  [删除未跟踪文件]" if row.kind == "untracked" else "  [恢复到当前提交]")
            for row in rows))
        confirm = box.addButton("备份并丢弃", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is confirm

    def discard_selected(self):
        if self.worker is not None:
            return
        rows = self.checked_rows()
        if rows and self.confirm_discard(rows):
            self.start_job("discard", {"snapshot": self.snapshot, "paths": [row.path for row in rows]})

    def navigate(self, repo_id: str):
        self.repo_id = repo_id
        self.snapshot = None
        self.items = {}
        self.table.clear()
        self.preview.clear()
        self.result_message = ""
        self.update_title()
        self.refresh()

    def open_child(self):
        if self.worker is None and (key := self.child_repo_id()):
            self.history.append(self.repo_id)
            self.navigate(key)

    def go_back(self):
        if self.worker is None and self.history:
            self.navigate(self.history.pop())

    def open_backup(self):
        if self.last_backup:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_backup))

    def start_job(self, operation: str, extra=None):
        self.operation = operation
        self.worker = JobThread(self.data_dir, self.profile, operation, [self.repo_id], extra, self)
        self.worker.event.connect(self.handle_event)
        self.worker.completed.connect(self.job_completed)
        self.worker.failed.connect(self.job_failed)
        self.worker.finished.connect(self.job_finished)
        self.message.setText({"changes": "正在读取文件改动…", "file-diff": "正在读取 Diff…",
                              "discard": "正在备份并丢弃所选文件…"}[operation])
        self.update_controls()
        self.worker.start()

    def handle_event(self, kind: str, payload):
        if kind == "backup":
            self.last_backup = payload
            self.update_controls()

    def job_completed(self, result):
        if self.operation == "changes":
            self.load_snapshot(result)
        elif self.operation == "file-diff":
            self.preview.setPlainText(result)
            self.message.setText("Diff 预览：" + self.current_path())
        else:
            self.mutated = True
            self.last_backup = result.backup_dir
            self.result_message = result.message
            self.message.setText(result.message)

    def job_failed(self, detail: str):
        self.result_message = "操作失败：" + detail.strip().splitlines()[-1]
        self.message.setText(self.result_message)
        self.preview.setPlainText(detail)
        if self.operation == "changes":
            self.snapshot = None
            self.items = {}
            self.table.clear()
        if self.operation == "discard":
            # A late logging failure must still trigger a fresh repository scan.
            self.mutated = True

    def job_finished(self):
        operation = self.operation
        old, self.worker = self.worker, None
        if old:
            old.deleteLater()
        if self.closing_requested:
            super().reject()
        elif operation == "discard":
            self.refresh()
        else:
            self.update_controls()

    def reject(self):
        self.closing_requested = True
        if self.worker is not None:
            if self.operation != "discard":
                self.worker.cancel_requested.set()
            self.message.setText("正在结束当前操作，完成后关闭窗口…")
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
            self.reject()
        else:
            self.closing_requested = True
            event.accept()
