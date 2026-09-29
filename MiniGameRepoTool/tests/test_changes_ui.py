from pathlib import Path

from PySide6.QtCore import Qt

from repo_tool.models import RepoSpec
from repo_tool.ui.changes_dialog import ChangesDialog
from test_ui import qapp, wait_job
from test_changes import workspace
from test_git_service import git, commit


def test_file_list_preview_and_confirmed_discard(qapp, workspace, tmp_path, monkeypatch):
    profile, root, service = workspace
    (root / "tracked.txt").write_text("changed content", encoding="utf-8")
    (root / "keep.txt").write_text("keep my work", encoding="utf-8")
    dialog = ChangesDialog(service.data_dir, profile, "root")
    dialog.show()
    qapp.processEvents()
    wait_job(dialog, qapp)
    assert set(dialog.items) == {"tracked.txt", "keep.txt"}
    assert not dialog.discard_button.isEnabled()
    dialog.table.setCurrentItem(dialog.items["tracked.txt"])
    dialog.show_diff()
    wait_job(dialog, qapp)
    assert "changed content" in dialog.preview.toPlainText()
    dialog.items["tracked.txt"].setCheckState(0, Qt.CheckState.Checked)
    assert dialog.discard_button.isEnabled()
    monkeypatch.setattr(dialog, "confirm_discard", lambda rows: False)
    dialog.discard_selected()
    assert dialog.worker is None
    assert (root / "tracked.txt").read_text() == "changed content"
    monkeypatch.setattr(dialog, "confirm_discard", lambda rows: True)
    dialog.discard_selected()
    wait_job(dialog, qapp)
    assert (root / "tracked.txt").read_text() == "base\n"
    assert (root / "keep.txt").read_text() == "keep my work"
    assert set(dialog.items) == {"keep.txt"}
    assert Path(dialog.last_backup).is_dir()
    assert dialog.backup_button.isEnabled()
    assert dialog.mutated
    dialog.close()


def test_submodule_row_cannot_be_checked_and_opens_own_files(qapp, workspace, tmp_path):
    profile, root, service = workspace
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    commit(remote)
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", str(remote), "child")
    git(root, "commit", "-m", "child")
    (root / "child/file.txt").write_text("edit", encoding="utf-8")
    profile.repositories.append(RepoSpec("child", "Child", "child", str(remote), branch="main"))
    dialog = ChangesDialog(service.data_dir, profile, "root")
    dialog.show()
    qapp.processEvents()
    wait_job(dialog, qapp)
    row = dialog.items["child"]
    assert not row.flags() & Qt.ItemFlag.ItemIsUserCheckable
    dialog.table.setCurrentItem(row)
    assert dialog.child_button.isEnabled()
    dialog.open_child()
    wait_job(dialog, qapp)
    assert dialog.repo_id == "child"
    assert "file.txt" in dialog.items
    assert "子模块" in dialog.hint.text()
    assert dialog.back_button.isEnabled()
    dialog.close()


def test_dialog_closed_before_initial_timer_does_not_start_a_worker(qapp, workspace):
    profile, root, service = workspace
    dialog = ChangesDialog(service.data_dir, profile, "root")
    dialog.reject()
    qapp.processEvents()
    started_after_close = dialog.worker is not None
    if dialog.worker:
        dialog.worker.cancel_requested.set()
        wait_job(dialog, qapp)
    assert not started_after_close
