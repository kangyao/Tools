from copy import deepcopy
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QTableWidgetItem

from repo_tool.build_config import CompileTask, GenerationTask
from repo_tool.models import ProfileDocument, Snapshot
from repo_tool.profiles import ProfileStore
from repo_tool.storage import RunLock
from repo_tool.ui.build_dialog import BuildDialog
from repo_tool.ui.dialogs import ProfilesDialog
from repo_tool.ui.window import MainWindow
from test_build import build_profile, existing_solution, generation_task
from test_ui import qapp, wait_job


@pytest.mark.parametrize("validate_only", [False, True])
def test_busy_project_marks_tasks_unstarted_and_allows_retry(qapp, build_profile, tmp_path, validate_only):
    build_profile.build.steps = ["generate", "compile"]
    existing_solution(build_profile)
    generation_task(build_profile)
    data = tmp_path / "app"
    dialog = BuildDialog(data, build_profile, lambda _: True)
    try:
        with RunLock(data, build_profile.root):
            dialog.start(validate_only)
            wait_job(dialog, qapp)
        message = dialog.logs.toPlainText()
        assert "工程目录正在被其他任务使用" in message
        assert "Traceback" not in message and "PermissionError" not in message
        assert dialog.message.text() == message
        assert dialog.generation_table.item(0, 4).text() == "未执行"
        assert dialog.compile_table.item(0, 6).text() == "未执行"
        assert dialog.generation_table.item(0, 4).toolTip() == message
        assert dialog.run_button.isEnabled() and dialog.check_button.isEnabled()
        assert not (Path(build_profile.root) / "generation-calls.json").exists()
        assert not (data / "runs").exists()
        # Another project's task must not prevent this generation/check.
        dialog.steps["compile"].setChecked(False)
        with RunLock(data, tmp_path / "other-project"):
            dialog.start(validate_only)
            wait_job(dialog, qapp, timeout=90)
        assert dialog.results["generate"].outcome == "success"
    finally:
        dialog.close()


def test_task_lists_save_and_profile_editor_preserves_them(qapp, build_profile, tmp_path):
    existing_solution(build_profile)
    generation_task(build_profile, arguments=["--label", "中文 with spaces"], build_directory="Projects/custom")
    saved = []
    dialog = BuildDialog(tmp_path / "app", build_profile, lambda options: saved.append(options))
    assert dialog.advanced.isHidden()
    assert dialog.parameter_tabs.count() == 2
    dialog.steps["setup"].setChecked(True)
    dialog.compile_table.cellWidget(0, 3).setCurrentText("Release")
    dialog.compile_table.cellWidget(0, 5).setCurrentText("Rebuild")
    dialog.timeout.setValue(90)
    dialog.generation_table.selectRow(0)
    dialog.copy_generation_tasks()
    assert dialog.generation_table.rowCount() == 2
    assert dialog.save()
    options = saved[-1]
    assert options.steps == ["setup", "compile"]
    assert (options.compile_tasks[0].configuration, options.compile_tasks[0].action) == ("Release", "Rebuild")
    assert options.generation_tasks[1].build_directory == "Projects/custom-copy"
    assert options.generation_tasks[0].id != options.generation_tasks[1].id
    assert options.generation_tasks[1].arguments == ["--label", "中文 with spaces"]
    build_profile.build = deepcopy(options)
    editor = ProfilesDialog(ProfileDocument(1, build_profile.id, [build_profile]))
    editor.name_edit.setText("renamed")
    editor.save_current()
    editor.copy_profile()
    assert editor.document.profiles[0].build == editor.document.profiles[1].build == options
    editor.close()
    dialog.close()


def test_dialog_reports_missing_solution_and_unlocks_controls(qapp, build_profile, tmp_path):
    missing = str(Path(build_profile.root) / "Projects/missing/MiniGame.sln")
    build_profile.build.compile_tasks = [CompileTask(solution=missing)]
    dialog = BuildDialog(tmp_path / "app", build_profile, lambda _: True)
    dialog.show()
    dialog.start(False)
    assert not dialog.settings.isEnabled() and not dialog.run_button.isEnabled()
    wait_job(dialog, qapp, timeout=90)
    assert dialog.results["compile"].outcome == "failed"
    assert "请先执行生成" in dialog.compile_table.item(0, 6).toolTip()
    assert dialog.compile_table.item(0, 6).text() == "失败"
    assert dialog.run_button.isEnabled() and dialog.open_logs_button.isEnabled()
    assert not (Path(build_profile.root) / "setup-ran.txt").exists()
    dialog.close()


def test_read_targets_fixes_legacy_solution_folder_and_check_does_not_build(qapp, build_profile, tmp_path):
    existing_solution(build_profile)
    build_profile.build.compile_tasks[0].target = "MiniGame"
    dialog = BuildDialog(tmp_path / "app", build_profile, lambda _: True)
    dialog.load_targets()
    assert dialog.compile_table.cellWidget(0, 2).currentText() == "MiniGameApp"
    dialog.start(True)
    wait_job(dialog, qapp, timeout=90)
    assert dialog.compile_table.item(0, 6).text() == "检查通过"
    assert not list((tmp_path / "app").rglob("incredibuild.session.json"))
    dialog.close()


def test_generation_adds_multi_sln_rows_compiles_and_persists_results(qapp, build_profile, tmp_path):
    build_profile.build.steps = ["generate", "compile"]
    generation_task(build_profile, arguments=["--count", "2"], build_directory="Projects/multi")
    store = ProfileStore(tmp_path / "config")
    def save(options):
        profile = deepcopy(build_profile)
        profile.build = options
        store.save(ProfileDocument(1, profile.id, [profile]))
    dialog = BuildDialog(tmp_path / "app", build_profile, save)
    dialog.start()
    wait_job(dialog, qapp, timeout=90)
    assert dialog.results["generate"].outcome == dialog.results["compile"].outcome == "success"
    assert dialog.generation_table.item(0, 4).text() == "完成"
    assert dialog.compile_table.rowCount() == 2
    assert all(dialog.compile_table.item(row, 6).text() == "完成" for row in range(2))
    restored = store.load().profiles[0]
    assert len(restored.build.compile_tasks) == 2
    reopened = BuildDialog(tmp_path / "app", restored, lambda _: True)
    assert reopened.options().compile_tasks == restored.build.compile_tasks
    reopened.close()
    dialog.close()


def test_partial_generation_failure_preserves_completed_sln_rows(qapp, build_profile, tmp_path):
    build_profile.build.steps = ["generate", "compile"]
    generation_task(build_profile, build_directory="Projects/first")
    generation_task(build_profile, arguments=["--fail-code", "7"], build_directory="Projects/second")
    saved = []
    dialog = BuildDialog(tmp_path / "app", build_profile, lambda options: saved.append(options))
    dialog.start()
    wait_job(dialog, qapp, timeout=90)
    assert [dialog.generation_table.item(row, 4).text() for row in range(2)] == ["完成", "失败"]
    assert dialog.compile_table.rowCount() == 1 and dialog.compile_table.item(0, 6).text() == "未执行"
    assert len(saved[-1].compile_tasks) == 1
    dialog.close()


def test_multi_select_sln_import_and_disabled_rows(qapp, build_profile, tmp_path, monkeypatch):
    first = existing_solution(build_profile)
    second = existing_solution(build_profile, "AICore")
    build_profile.build.compile_tasks.clear()
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *args: ([str(first), str(second)], ""))
    dialog = BuildDialog(tmp_path / "app", build_profile, lambda _: True)
    dialog.add_solutions()
    assert dialog.compile_table.rowCount() == 2
    dialog.compile_table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    dialog.start()
    wait_job(dialog, qapp, timeout=90)
    assert dialog.compile_table.item(0, 6).text() == "完成"
    assert dialog.compile_table.item(1, 6).text() == "未选择"
    assert len(dialog.results["compile"].tasks) == 1
    dialog.compile_table.selectRow(1)
    dialog.remove_tasks(dialog.compile_table)
    assert len(dialog.options().compile_tasks) == 1
    dialog.close()


def test_main_build_entry_saves_tasks_without_invalidating_git_state(qapp, build_profile, tmp_path, monkeypatch):
    existing_solution(build_profile)
    data = tmp_path / "app"
    ProfileStore(data).save(ProfileDocument(1, build_profile.id, [build_profile]))
    window = MainWindow(data, auto_scan=False)
    snapshot = Snapshot("root", "up_to_date", "none", "current")
    window.snapshots["root"] = snapshot
    def edit(dialog):
        dialog.compile_table.cellWidget(0, 3).setCurrentText("Release")
        assert dialog.save()
        return 0
    monkeypatch.setattr(BuildDialog, "exec", edit)
    window.setup_button.click()
    assert window.setup_button.text() == "编译构建"
    assert window.snapshots["root"] is snapshot
    assert ProfileStore(data).load().profiles[0].build.compile_tasks[0].configuration == "Release"
    window.close()
