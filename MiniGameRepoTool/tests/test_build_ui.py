from copy import deepcopy
from pathlib import Path

from repo_tool.models import ProfileDocument, Snapshot
from repo_tool.profiles import ProfileStore
from repo_tool.ui.build_dialog import BuildDialog
from repo_tool.ui.dialogs import ProfilesDialog
from repo_tool.ui.window import MainWindow
from test_build import build_profile, existing_solution
from test_ui import qapp, wait_job


def test_build_dialog_saves_parameters_and_profile_editor_preserves_them(qapp, build_profile, tmp_path):
    saved = []
    dialog = BuildDialog(tmp_path / "app", build_profile, lambda options: saved.append(options))
    dialog.steps["setup"].setChecked(True)
    dialog.configuration.setCurrentText("Release")
    dialog.action.setCurrentText("Rebuild")
    dialog.compile_type.setText("local-dev")
    dialog.timeout.setValue(90)
    assert dialog.save()
    assert saved[-1].steps == ["setup", "compile"]
    assert (saved[-1].configuration, saved[-1].action, saved[-1].timeout_minutes) == ("Release", "Rebuild", 90)
    build_profile.build = deepcopy(saved[-1])
    document = ProfileDocument(1, build_profile.id, [build_profile])
    editor = ProfilesDialog(document)
    editor.name_edit.setText("renamed")
    editor.save_current()
    assert editor.document.profiles[0].build == saved[-1]
    editor.copy_profile()
    assert editor.document.profiles[1].build == saved[-1]
    editor.close()
    dialog.close()


def test_build_dialog_executes_selected_step_and_reports_errors(qapp, build_profile, tmp_path):
    dialog = BuildDialog(tmp_path / "app", build_profile, lambda _: True)
    dialog.show()
    dialog.start(False)
    assert not dialog.settings.isEnabled()
    assert not dialog.run_button.isEnabled()
    wait_job(dialog, qapp, timeout=90)
    assert dialog.results["compile"].outcome == "failed"
    assert "请先执行生成" in dialog.status_table.item(2, 2).text()
    assert dialog.run_button.isEnabled()
    assert dialog.open_logs_button.isEnabled()
    assert not (Path(build_profile.root) / "setup-ran.txt").exists()
    dialog.close()


def test_build_dialog_reads_targets_and_checks_without_build(qapp, build_profile, tmp_path):
    existing_solution(build_profile)
    dialog = BuildDialog(tmp_path / "app", build_profile, lambda _: True)
    dialog.load_targets()
    assert dialog.target.findText("MiniGame") >= 0
    dialog.start(True)
    wait_job(dialog, qapp, timeout=90)
    assert dialog.status_table.item(2, 1).text() == "检查通过"
    assert not list((tmp_path / "app").rglob("incredibuild.session.json"))
    assert "ValidateOnly" not in dialog.preview.toPlainText()  # preview describes the real action
    dialog.close()


def test_main_build_entry_saves_without_invalidating_git_state(qapp, build_profile, tmp_path, monkeypatch):
    data = tmp_path / "app"
    ProfileStore(data).save(ProfileDocument(1, build_profile.id, [build_profile]))
    window = MainWindow(data, auto_scan=False)
    snapshot = Snapshot("root", "up_to_date", "none", "current")
    window.snapshots["root"] = snapshot
    def edit(dialog):
        dialog.configuration.setCurrentText("Release")
        assert dialog.save()
        return 0
    monkeypatch.setattr(BuildDialog, "exec", edit)
    window.setup_button.click()
    assert window.setup_button.text() == "编译构建"
    assert window.snapshots["root"] is snapshot
    assert ProfileStore(data).load().profiles[0].build.configuration == "Release"
    window.close()
