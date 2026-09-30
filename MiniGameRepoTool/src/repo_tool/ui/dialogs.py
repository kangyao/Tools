from __future__ import annotations

import uuid
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..models import Profile, RepoSpec, SetupOptions, validate_profile
from ..profiles import ProfileStore, default_profile


class ProfilesDialog(QDialog):
    def __init__(self, document, parent=None, focus_repo: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("管理方案与仓库")
        self.resize(1220, 760)
        self.document = deepcopy(document)
        self.current_index = -1
        outer = QVBoxLayout(self)
        split = QSplitter()
        outer.addWidget(split)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.list = QListWidget()
        left_layout.addWidget(self.list)
        buttons = QHBoxLayout()
        for title, callback in [("新建", self.new_profile), ("复制", self.copy_profile), ("删除", self.delete_profile)]:
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, cb=callback: self.guard(cb))
            buttons.addWidget(button)
        left_layout.addLayout(buttons)
        split.addWidget(left)
        right = QWidget()
        layout = QVBoxLayout(right)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.root_edit = QLineEdit()
        root_row = QHBoxLayout()
        root_row.addWidget(self.root_edit)
        browse = QPushButton("选择目录")
        browse.clicked.connect(self.browse_root)
        root_row.addWidget(browse)
        self.groups_edit = QPlainTextEdit()
        self.groups_edit.setMaximumHeight(86)
        self.groups_edit.setPlaceholderText("game=miniw/release/...\nengine=Engine/Release_...")
        form.addRow("方案名称", self.name_edit)
        form.addRow("工程根目录", root_row)
        form.addRow("分支组（每行 名称=分支）", self.groups_edit)
        self.auto_setup = QCheckBox("所需仓库全部就绪后，自动准备构建环境")
        form.addRow(self.auto_setup)
        layout.addLayout(form)
        note = QLabel("分支组和固定分支二选一；依赖填写仓库 ID，以逗号分隔。未勾选环境必需项时，默认使用全部启用仓库。")
        note.setWordWrap(True)
        layout.addWidget(note)
        policy_note = QLabel("忽略修改提醒仅影响显示，文件与 Diff 仍可查看。强制更新在同步时丢弃未提交修改，不备份；保留本地提交，分支分叉时停止。")
        policy_note.setWordWrap(True)
        layout.addWidget(policy_note)
        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(["启用", "仓库 ID", "名称", "忽略修改提醒", "强制更新（不备份）",
                                             "相对目录", "远端地址", "分支组", "固定分支", "额外依赖", "环境必需"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, width in enumerate([45, 95, 125, 110, 150, 180, 320, 80, 180, 120, 75]):
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table)
        row_buttons = QHBoxLayout()
        add = QPushButton("添加仓库")
        add.clicked.connect(self.add_repo)
        remove = QPushButton("移除所选配置")
        remove.clicked.connect(self.remove_repo)
        row_buttons.addWidget(add)
        row_buttons.addWidget(remove)
        row_buttons.addStretch()
        import_button = QPushButton("导入方案文件")
        import_button.clicked.connect(self.import_file)
        export_button = QPushButton("导出全部方案")
        export_button.clicked.connect(self.export_file)
        row_buttons.addWidget(import_button)
        row_buttons.addWidget(export_button)
        layout.addLayout(row_buttons)
        split.addWidget(right)
        split.setSizes([190, 1000])
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        outer.addWidget(actions)
        self.list.currentRowChanged.connect(self.switch_profile)
        active = next((i for i, p in enumerate(self.document.profiles)
                       if p.id == self.document.active_profile_id), 0)
        self.refresh_list(active)
        if focus_repo:
            profile = self.document.profiles[active]
            for row, repo in enumerate(profile.repositories):
                if repo.id == focus_repo:
                    self.table.selectRow(row)
                    self.table.scrollToItem(self.table.item(row, 2))

    def guard(self, callback):
        try:
            callback()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "配置未保存", str(error))

    @staticmethod
    def check_item(checked: bool):
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        return item

    def refresh_list(self, selected: int):
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems([p.name for p in self.document.profiles])
        self.list.setCurrentRow(selected)
        self.list.blockSignals(False)
        self.load_profile(selected)

    def load_profile(self, index: int):
        self.current_index = index
        p = self.document.profiles[index]
        self.name_edit.setText(p.name)
        self.root_edit.setText(p.root)
        self.groups_edit.setPlainText("\n".join(f"{k}={v}" for k, v in p.branch_groups.items()))
        self.auto_setup.setChecked(p.setup.auto_run)
        self.table.setRowCount(0)
        for repo in p.repositories:
            self.insert_repo(repo, repo.id in p.setup.required_repositories)

    def insert_repo(self, repo: RepoSpec, required: bool = False):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, self.check_item(repo.enabled))
        values = [repo.id, repo.name, repo.path, repo.remote, repo.branch_group, repo.branch, ", ".join(repo.depends_on)]
        for col, value in zip((1, 2, 5, 6, 7, 8, 9), values):
            self.table.setItem(row, col, QTableWidgetItem(value))
        self.table.setItem(row, 10, self.check_item(required))
        self.table.setItem(row, 3, self.check_item(repo.ignore_changes))
        self.table.item(row, 3).setToolTip("不在主窗口强调本地修改；不改写 Git 状态，不自动丢弃文件。文件列表和 Diff 照常可用。")
        self.table.setItem(row, 4, self.check_item(repo.force_update))
        self.table.item(row, 4).setToolTip("同步时丢弃暂存、未暂存及普通未跟踪文件，不备份；保留本地提交，分叉时停止。被忽略文件和子仓库不清理。仅保存配置或检查状态不会丢弃文件。")

    def save_current(self):
        old = self.document.profiles[self.current_index]
        groups = {}
        for line in self.groups_edit.toPlainText().splitlines():
            if not line.strip():
                continue
            key, separator, value = line.partition("=")
            if not separator or not key.strip() or key.strip() in groups:
                raise ValueError("分支组格式应为 名称=分支，名称不能重复")
            groups[key.strip()] = value.strip()
        repositories, required = [], []
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, col).text().strip() for col in (1, 2, 5, 6, 7, 8, 9)]
            repo_id, name, relative, remote, group, branch, deps = cells
            repositories.append(RepoSpec(repo_id, name, relative, remote, group, branch,
                                         self.table.item(row, 0).checkState() == Qt.CheckState.Checked,
                                         [s.strip() for s in deps.split(",") if s.strip()],
                                         self.table.item(row, 3).checkState() == Qt.CheckState.Checked,
                                         self.table.item(row, 4).checkState() == Qt.CheckState.Checked))
            if self.table.item(row, 10).checkState() == Qt.CheckState.Checked:
                required.append(repo_id)
        updated = Profile(old.id, self.name_edit.text().strip(), self.root_edit.text().strip(),
                          groups, repositories, SetupOptions(self.auto_setup.isChecked(), required))
        validate_profile(updated)
        self.document.profiles[self.current_index] = updated
        self.list.item(self.current_index).setText(updated.name)

    def switch_profile(self, index: int):
        if index < 0 or index == self.current_index:
            return
        try:
            self.save_current()
            self.load_profile(index)
        except ValueError as error:
            self.list.blockSignals(True)
            self.list.setCurrentRow(self.current_index)
            self.list.blockSignals(False)
            QMessageBox.warning(self, "请先完成当前方案", str(error))

    def new_profile(self):
        self.save_current()
        p = default_profile()
        p.id = "profile-" + uuid.uuid4().hex[:8]
        p.name = "新方案"
        self.document.profiles.append(p)
        self.refresh_list(len(self.document.profiles) - 1)

    def copy_profile(self):
        self.save_current()
        p = deepcopy(self.document.profiles[self.current_index])
        p.id = "profile-" + uuid.uuid4().hex[:8]
        p.name += " 副本"
        self.document.profiles.append(p)
        self.refresh_list(len(self.document.profiles) - 1)

    def delete_profile(self):
        if len(self.document.profiles) == 1:
            raise ValueError("至少保留一套方案")
        self.document.profiles.pop(self.current_index)
        self.refresh_list(max(0, self.current_index - 1))

    def add_repo(self):
        key = "repo-" + uuid.uuid4().hex[:6]
        group = next(iter(self.document.profiles[self.current_index].branch_groups), "game")
        self.insert_repo(RepoSpec(key, "新仓库", key, "", group))
        self.table.selectRow(self.table.rowCount() - 1)
        self.table.scrollToBottom()

    def remove_repo(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def browse_root(self):
        chosen = QFileDialog.getExistingDirectory(self, "选择工程根目录", self.root_edit.text())
        if chosen:
            self.root_edit.setText(chosen)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入方案", "", "JSON (*.json *.bak)")
        if not path:
            return
        try:
            loaded = ProfileStore.read(Path(path))
            self.document = loaded
            active = next(i for i, p in enumerate(loaded.profiles) if p.id == loaded.active_profile_id)
            self.refresh_list(active)
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "导入失败", str(error))

    def export_file(self):
        def export():
            self.save_current()
            self.document.active_profile_id = self.document.profiles[self.current_index].id
            path, _ = QFileDialog.getSaveFileName(self, "导出全部方案", "profiles.json", "JSON (*.json)")
            if path:
                ProfileStore.export(self.document, Path(path))
        self.guard(export)

    def accept(self):
        try:
            self.save_current()
            self.document.active_profile_id = self.document.profiles[self.current_index].id
            super().accept()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "无法保存", str(error))
