from dataclasses import asdict

import pytest
from PySide6.QtCore import Qt

from repo_tool.models import ProfileDocument, RepoSpec, Snapshot
from repo_tool.profiles import ProfileStore, parse_document
from repo_tool.ui.dialogs import ProfilesDialog
from repo_tool.ui.window import MainWindow
from test_git_service import commit, git, make_profile, remote
from test_ui import qapp, wait_job


def test_old_config_defaults_and_new_options_survive_save_export_and_copy(tmp_path, qapp):
    profile = make_profile(tmp_path / "checkout", tmp_path / "remote")
    payload = asdict(ProfileDocument(1, profile.id, [profile]))
    repo = payload["profiles"][0]["repositories"][0]
    repo.pop("ignore_changes", None)
    repo.pop("force_update", None)
    document = parse_document(payload)
    assert document.profiles[0].repo("root").ignore_changes is False
    assert document.profiles[0].repo("root").force_update is False

    dialog = ProfilesDialog(document)
    headers = {dialog.table.horizontalHeaderItem(i).text(): i for i in range(dialog.table.columnCount())}
    for label in ("忽略修改提醒", "强制更新（不备份）"):
        dialog.table.item(0, headers[label]).setCheckState(Qt.CheckState.Checked)
    dialog.copy_profile()
    store = ProfileStore(tmp_path / "app")
    store.save(dialog.document)
    exported = tmp_path / "export.json"
    store.export(store.load(), exported)
    loaded = store.read(exported)
    for saved in loaded.profiles:
        assert saved.repo("root").ignore_changes is True
        assert saved.repo("root").force_update is True
        assert saved.repo("root").name == "Root"
        assert saved.repo("root").path == "."
        assert saved.repo("root").remote == str(tmp_path / "remote")
        assert saved.repo("root").branch_group == "game"
        assert saved.repo("root").branch == ""
        assert saved.setup.required_repositories == ["root"]
    assert not document.profiles[0].repo("root").force_update
    dialog.close()


@pytest.mark.parametrize("field", ["ignore_changes", "force_update"])
def test_repo_options_reject_string_booleans(tmp_path, field):
    profile = make_profile(tmp_path / "checkout", tmp_path / "remote")
    payload = asdict(ProfileDocument(1, profile.id, [profile]))
    payload["profiles"][0]["repositories"][0][field] = "false"
    with pytest.raises(ValueError):
        parse_document(payload)


def test_ignored_reminder_does_not_hide_real_changes_or_other_errors(qapp, tmp_path):
    profile = make_profile(tmp_path / "checkout", tmp_path / "remote")
    profile.repo("root").ignore_changes = True
    data = tmp_path / "app"
    ProfileStore(data).save(ProfileDocument(1, profile.id, [profile]))
    window = MainWindow(data, auto_scan=False)
    window.handle_event("snapshot", Snapshot("root", "dirty", "block", "工作区需要处理", changes=[" M file.txt"]))
    assert window.items["root"].text(3) == "修改提醒已忽略"
    assert window.snapshots["root"].status == "dirty"
    assert window.changes_button.isEnabled()
    assert "file.txt" not in window.detail.toPlainText()
    window.handle_event("snapshot", Snapshot("root", "remote_mismatch", "block", "origin 与配置不同"))
    assert window.items["root"].text(3) == "远端不一致"
    assert "origin 与配置不同" in window.detail.toPlainText()
    window.close()


def test_child_sync_and_retry_do_not_select_or_modify_parent(qapp, tmp_path, remote):
    root = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(root))
    git(root, "clone", str(remote), str(root / "Child"))
    profile = make_profile(root, remote)
    profile.repo("root").force_update = True
    profile.repositories.append(RepoSpec("child", "Child", "Child", str(remote), branch="main"))
    data = tmp_path / "app"
    ProfileStore(data).save(ProfileDocument(1, profile.id, [profile]))
    (root / "file.txt").write_text("keep parent", encoding="utf-8")
    (root / "Child/file.txt").write_text("child working", encoding="utf-8")
    updated = commit(remote, "new.txt", "remote update")
    window = MainWindow(data, auto_scan=False)
    window.items["root"].setCheckState(0, Qt.CheckState.Unchecked)
    window.start_job("sync", ["child"])
    wait_job(window, qapp, timeout=120)
    assert window.selected_ids() == ["child"]
    assert set(window.results) == {"child"}
    assert window.results["child"].outcome == "blocked"
    (root / "Child/file.txt").write_text("one", encoding="utf-8")
    window.retry()
    wait_job(window, qapp, timeout=120)
    assert (root / "file.txt").read_text(encoding="utf-8") == "keep parent"
    assert set(window.results) == {"child"}
    assert window.results["child"].outcome == "success"
    assert git(root / "Child", "rev-parse", "HEAD") == updated
    window.close()
