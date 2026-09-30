from pathlib import Path

import pytest
from PySide6.QtCore import QPoint

from repo_tool.models import ProfileDocument, RepoSpec
from repo_tool.profiles import ProfileStore
from repo_tool.ui import window as window_module
from repo_tool.ui.window import MainWindow
from test_git_service import make_profile
from test_ui import qapp, wait_job


def make_window(qapp, tmp_path):
    root = tmp_path / "工程"
    root.mkdir()
    (root / "child").mkdir()
    profile = make_profile(root, tmp_path / "remote")
    profile.repositories.append(RepoSpec("child", "Child", "child", str(tmp_path / "remote"), branch="main"))
    data_dir = tmp_path / "app"
    ProfileStore(data_dir).save(ProfileDocument(1, profile.id, [profile]))
    window = MainWindow(data_dir, auto_scan=False)
    window.show()
    qapp.processEvents()
    return window


def test_right_click_uses_clicked_repository_without_changing_checks(qapp, tmp_path, monkeypatch):
    window = make_window(qapp, tmp_path)
    calls = []
    def native_menu(path, owner, point):
        calls.append((Path(path), owner))
        assert not window.sync_button.isEnabled()
        return False
    monkeypatch.setattr(window_module, "show_repository_menu", native_menu, raising=False)
    checked = window.selected_ids()
    point = window.tree.visualItemRect(window.items["child"]).center()
    window.tree.customContextMenuRequested.emit(point)
    assert calls == [(tmp_path / "工程/child", window)]
    assert window.current_repo_id() == "child"
    assert window.selected_ids() == checked
    assert window.sync_button.isEnabled()
    window.close()


def test_native_command_refreshes_local_status(qapp, tmp_path, monkeypatch):
    window = make_window(qapp, tmp_path)
    def native_menu(path, owner, point):
        (Path(path) / "created-by-shell.txt").write_text("test", encoding="utf-8")
        return True
    monkeypatch.setattr(window_module, "show_repository_menu", native_menu, raising=False)
    point = window.tree.visualItemRect(window.items["child"]).center()
    window.tree.customContextMenuRequested.emit(point)
    wait_job(window, qapp)
    assert window.snapshots["child"].status == "occupied"
    window.close()


def test_right_click_empty_space_or_missing_directory_has_no_native_menu(qapp, tmp_path, monkeypatch):
    window = make_window(qapp, tmp_path)
    calls = []
    monkeypatch.setattr(window_module, "show_repository_menu", lambda *args: calls.append(args), raising=False)
    window.tree.customContextMenuRequested.emit(QPoint(10, window.tree.viewport().height() - 5))
    (tmp_path / "工程/child").rmdir()
    window.tree.customContextMenuRequested.emit(window.tree.visualItemRect(window.items["child"]).center())
    assert not calls
    assert "目录" in window.message.text()
    window.close()


@pytest.mark.parametrize("invoked", [True, False])
def test_close_during_native_menu_does_not_start_a_worker_after_close(qapp, tmp_path, monkeypatch, invoked):
    window = make_window(qapp, tmp_path)
    def native_menu(path, owner, point):
        assert owner.close() is False
        assert owner.isVisible()
        return invoked
    monkeypatch.setattr(window_module, "show_repository_menu", native_menu, raising=False)
    window.tree.customContextMenuRequested.emit(window.tree.visualItemRect(window.items["child"]).center())
    started_after_close = window.worker is not None
    if window.worker:
        wait_job(window, qapp)
    assert not started_after_close
    assert not window.isVisible()
