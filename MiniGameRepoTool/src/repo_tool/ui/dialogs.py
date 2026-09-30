from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..acceleration import describe_plan
from ..discovery import discover_profile, find_repositories
from ..models import INIT_MODES, InitOptions, Profile, RepoSpec, SetupOptions, validate_profile
from ..profiles import ProfileStore, default_data_dir, default_profile
from .worker import JobThread


class ProfilesDialog(QDialog):
    def __init__(self, document, parent=None, focus_repo: str | None = None, data_dir: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle("管理方案与仓库")
        self.resize(1260, 880)
        self.document = deepcopy(document)
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.scan_thread: JobThread | None = None
        self.scan_report = ""
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
        discover = QPushButton("从目录识别")
        discover.setToolTip("扫描工程根目录下已有的 Git 仓库，按远端与当前分支生成仓库列表和分支组；只读，不访问远端")
        discover.clicked.connect(lambda: self.guard(self.discover_from_root))
        root_row.addWidget(discover)
        self.groups_edit = QPlainTextEdit()
        self.groups_edit.setMaximumHeight(86)
        self.groups_edit.setPlaceholderText("game=miniw/release/...\nengine=Engine/Release_...")
        form.addRow("方案名称", self.name_edit)
        form.addRow("工程根目录", root_row)
        form.addRow("分支组（每行 名称=分支）", self.groups_edit)
        self.auto_setup = QCheckBox("所需仓库全部就绪后，自动准备构建环境")
        form.addRow(self.auto_setup)
        layout.addLayout(form)
        init_box = QGroupBox("初始化加速（仅用于新建仓库）")
        grid = QGridLayout(init_box)
        self.init_mode = QComboBox()
        for key, text in INIT_MODES.items():
            self.init_mode.addItem(text, key)
        self.init_fallback = QComboBox()
        self.init_fallback.addItem("回退远端下载", True)
        self.init_fallback.addItem("停止该仓库", False)
        self.reuse_git = QCheckBox("复用 Git 数据")
        self.reuse_lfs = QCheckBox("复用 LFS 大文件")
        self.sources_edit = QPlainTextEdit()
        self.sources_edit.setMaximumHeight(70)
        self.sources_edit.setPlaceholderText("D:/MiniGame\nD:/AIMiniGame")
        add_source = QPushButton("添加来源")
        add_source.clicked.connect(self.browse_source)
        self.scan_button = QPushButton("扫描本地工程")
        self.scan_button.setToolTip("只读检查各来源目录能为本方案哪些仓库提供 Git/LFS 数据；不访问远端")
        self.scan_button.clicked.connect(lambda: self.guard(self.scan_sources))
        grid.addWidget(QLabel("初始化方式"), 0, 0)
        grid.addWidget(self.init_mode, 0, 1)
        grid.addWidget(QLabel("无法复用时"), 0, 2)
        grid.addWidget(self.init_fallback, 0, 3)
        grid.addWidget(self.reuse_git, 0, 4)
        grid.addWidget(self.reuse_lfs, 0, 5)
        grid.addWidget(QLabel("本地工程来源\n（按优先级，每行一个）"), 1, 0)
        grid.addWidget(self.sources_edit, 1, 1, 1, 5)
        source_buttons = QVBoxLayout()
        source_buttons.addWidget(add_source)
        source_buttons.addWidget(self.scan_button)
        grid.addLayout(source_buttons, 1, 6)
        init_note = QLabel("自动复用按来源顺序为每个仓库选择远端一致、对象完整的本地仓库；指定源工程只使用第一个来源目录，"
                           "或仓库行中填写的“初始化来源”，无效时按“无法复用时”处理。复用 Git 数据后仍从原始远端获取目标分支，"
                           "LFS 对象按哈希复制并校验。已有仓库沿用现有同步规则。")
        init_note.setWordWrap(True)
        grid.addWidget(init_note, 2, 0, 1, 7)
        layout.addWidget(init_box)
        note = QLabel("分支组和固定分支二选一；所选仓库独立同步。排序参考填写仓库 ID，以逗号分隔，仅影响列表展示。未勾选环境必需项时，默认使用全部启用仓库。")
        note.setWordWrap(True)
        layout.addWidget(note)
        policy_note = QLabel("忽略修改提醒仅影响显示，文件与 Diff 仍可查看。强制更新在同步时丢弃未提交修改，不备份；保留本地提交，分支分叉时停止。")
        policy_note.setWordWrap(True)
        layout.addWidget(policy_note)
        self.table = QTableWidget(0, 12)
        self.table.setHorizontalHeaderLabels(["启用", "仓库 ID", "名称", "忽略修改提醒", "强制更新（不备份）",
                                             "相对目录", "远端地址", "分支组", "固定分支", "排序参考", "环境必需",
                                             "初始化来源（可选）"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, width in enumerate([45, 95, 125, 110, 150, 180, 320, 80, 180, 120, 75, 220]):
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table, 1)
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
        self.init_mode.setCurrentIndex(self.init_mode.findData(p.init.mode))
        self.init_fallback.setCurrentIndex(0 if p.init.fallback else 1)
        self.reuse_git.setChecked(p.init.reuse_git)
        self.reuse_lfs.setChecked(p.init.reuse_lfs)
        self.sources_edit.setPlainText("\n".join(p.init.sources))
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
        self.table.setItem(row, 11, QTableWidgetItem(repo.init_source))
        self.table.item(row, 11).setToolTip("新建此仓库时优先使用的本地源仓库目录（绝对路径），例如 D:/AIMiniGame/AssetRuntime；留空按方案来源自动选择。")

    def save_current(self):
        updated = self.collect_current()
        validate_profile(updated)
        self.document.profiles[self.current_index] = updated
        self.list.item(self.current_index).setText(updated.name)

    def collect_current(self) -> Profile:
        """Read the form into a profile without validating it."""
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
                                         self.table.item(row, 4).checkState() == Qt.CheckState.Checked,
                                         self.table.item(row, 11).text().strip()))
            if self.table.item(row, 10).checkState() == Qt.CheckState.Checked:
                required.append(repo_id)
        init = InitOptions(self.init_mode.currentData(),
                           [line.strip() for line in self.sources_edit.toPlainText().splitlines() if line.strip()],
                           self.reuse_git.isChecked(), self.reuse_lfs.isChecked(), self.init_fallback.currentData())
        return Profile(old.id, self.name_edit.text().strip(), self.root_edit.text().strip(),
                       groups, repositories, SetupOptions(self.auto_setup.isChecked(), required),
                       deepcopy(old.build), init)

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
            if find_repositories(Path(chosen)):
                self.guard(self.discover_from_root)

    def discover_from_root(self):
        root = Path(self.root_edit.text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("请先填写已存在的工程根目录（绝对路径）")
        # Recognition is for incomplete profiles too, so read the form without validating it.
        profile = self.collect_current()
        template = profile
        if not profile.repositories:
            builtin = default_profile()
            template = replace(profile, repositories=builtin.repositories,
                               branch_groups={**builtin.branch_groups, **profile.branch_groups})
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = discover_profile(root, template)
        finally:
            QApplication.restoreOverrideCursor()
        if not result.found:
            QMessageBox.information(self, "从目录识别", f"{root} 下没有找到带 origin 的 Git 仓库，方案未改变。"
                                    + ("\n\n" + "\n".join(result.notes) if result.notes else ""))
            return
        enabled = [r for r in result.repositories if r.enabled]
        lines = [f"识别到 {result.found} 个仓库："]
        lines += [f"  {r.name}  ({r.path})  → {r.branch or r.branch_group + '=' + result.branch_groups[r.branch_group]}"
                  for r in enabled]
        lines += ["", "分支组：" + "；".join(f"{k}={v}" for k, v in result.branch_groups.items())]
        if result.notes:
            lines += ["", "说明："] + ["  " + note for note in result.notes]
        lines += ["", "用识别结果替换当前方案的仓库列表、分支组和环境必需项？其他设置保持不变。"]
        answer = QMessageBox.question(self, "从目录识别", "\n".join(lines))
        if answer != QMessageBox.StandardButton.Yes:
            return
        updated = replace(profile, branch_groups=result.branch_groups, repositories=result.repositories,
                          setup=SetupOptions(profile.setup.auto_run, result.required))
        validate_profile(updated)
        self.document.profiles[self.current_index] = updated
        self.list.item(self.current_index).setText(updated.name)
        self.load_profile(self.current_index)

    def browse_source(self):
        chosen = QFileDialog.getExistingDirectory(self, "添加本地工程来源", "")
        if chosen:
            text = self.sources_edit.toPlainText().rstrip()
            self.sources_edit.setPlainText((text + "\n" if text else "") + chosen)

    def scan_sources(self):
        if self.scan_thread is not None:
            return
        self.save_current()
        profile = deepcopy(self.document.profiles[self.current_index])
        thread = JobThread(self.data_dir, profile, "init-plan", [r.id for r in profile.repositories], parent=self)
        thread.completed.connect(lambda plans: self.show_scan(profile, plans))
        thread.failed.connect(lambda text: QMessageBox.warning(self, "扫描失败", text.strip().splitlines()[-1]))
        thread.finished.connect(self.scan_finished)
        self.scan_thread = thread
        self.scan_button.setEnabled(False)
        self.scan_button.setText("扫描中…")
        thread.start()

    def scan_finished(self):
        thread, self.scan_thread = self.scan_thread, None
        if thread:
            thread.deleteLater()
        self.scan_button.setEnabled(True)
        self.scan_button.setText("扫描本地工程")

    def show_scan(self, profile, plans):
        lines = [f"方案：{profile.name}；初始化方式：{INIT_MODES[profile.init.mode]}；只读扫描，未访问远端", ""]
        for plan in plans.values():
            lines.extend(describe_plan(profile, plan))
            lines.append("")
        self.scan_report = "\n".join(lines)
        report = QDialog(self)
        report.setWindowTitle("本地工程扫描结果")
        report.resize(960, 620)
        box = QVBoxLayout(report)
        text = QPlainTextEdit(self.scan_report)
        text.setReadOnly(True)
        box.addWidget(text)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(report.reject)
        box.addWidget(close)
        report.open()

    def done(self, result):
        # Never leave a scan thread running behind a closed dialog.
        if self.scan_thread is not None:
            self.scan_thread.stop_requested.set()
            self.scan_thread.cancel_requested.set()
            self.scan_thread.wait()
        super().done(result)

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
