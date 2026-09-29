import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from repo_tool.models import ProfileDocument, Snapshot, RepoResult
from repo_tool.profiles import ProfileStore
from repo_tool.ui.window import MainWindow
from repo_tool.ui.dialogs import ProfilesDialog
from test_git_service import make_profile, git, commit


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def wait_job(window, qapp, timeout=30):
    deadline = time.monotonic() + timeout
    while window.worker is not None:
        qapp.processEvents()
        time.sleep(0.01)
        assert time.monotonic() < deadline, "GUI task did not finish"
    qapp.processEvents()


def test_real_gui_clone_and_recheck(qapp, tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    commit(remote)
    p = make_profile(tmp_path / "工程 with spaces", remote)
    data = tmp_path / "app"
    ProfileStore(data).save(ProfileDocument(1, p.id, [p]))
    window = MainWindow(data, auto_scan=False)
    window.show()
    qapp.processEvents()
    assert window.tree.topLevelItemCount() == 1
    window.start_job("sync", ["root"])
    assert not window.sync_button.isEnabled()
    assert not window.items["root"].flags() & Qt.ItemFlag.ItemIsUserCheckable
    wait_job(window, qapp)
    assert window.results["root"].outcome == "success"
    assert window.snapshots["root"].ready
    assert window.sync_button.isEnabled()
    assert window.items["root"].flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert (tmp_path / "工程 with spaces" / ".git").exists()
    window.start_job("check", ["root"])
    wait_job(window, qapp)
    assert window.snapshots["root"].status == "up_to_date"
    window.close()


def test_profile_dialog_roundtrip_preserves_repo_fields(qapp, tmp_path):
    p = make_profile(tmp_path / "checkout", tmp_path / "remote")
    document = ProfileDocument(1, p.id, [p])
    dialog = ProfilesDialog(document)
    dialog.name_edit.setText("改名")
    dialog.save_current()
    assert dialog.document.profiles[0].name == "改名"
    assert document.profiles[0].name == "Local"
    dialog.copy_profile()
    assert len(dialog.document.profiles) == 2
    assert dialog.document.profiles[0].id != dialog.document.profiles[1].id
    dialog.close()


def test_fixed_branch_choice_refreshes_display_and_invalidates_results(qapp, tmp_path, monkeypatch):
    p = make_profile(tmp_path / "checkout", tmp_path / "remote")
    p.repo("root").branch_group = ""
    p.repo("root").branch = "main"
    data = tmp_path / "app"
    ProfileStore(data).save(ProfileDocument(1, p.id, [p]))
    window = MainWindow(data, auto_scan=False)
    window.snapshots["root"] = Snapshot("root", "up_to_date", "none", "Done")
    window.results["root"] = RepoResult("root", "success", "Done")
    window.pending_branches = ["main", "feature"]
    window.branch_choice_repo = "root"
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("feature", True))
    window.job_finished()
    assert window.profile.target(window.profile.repo("root")) == "feature"
    assert window.items["root"].text(2) == "feature"
    assert not window.snapshots and not window.results
    assert ProfileStore(data).load().profiles[0].repo("root").branch == "feature"
    window.close()


def test_invalid_origin_does_not_mutate_profile_and_details_are_redacted(qapp, tmp_path, monkeypatch):
    p = make_profile(tmp_path / "checkout", tmp_path / "remote")
    data = tmp_path / "app"
    ProfileStore(data).save(ProfileDocument(1, p.id, [p]))
    window = MainWindow(data, auto_scan=False)
    window.snapshots["root"] = Snapshot("root", "remote_mismatch", "block", "Mismatch",
                                       origin="https://user:private-value@example.invalid/repo")
    window.show_detail()
    assert "private-value" not in window.detail.toPlainText()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    window.adopt_origin()
    assert window.profile.repo("root").remote == p.repo("root").remote
    assert window.worker is None
    window.close()
