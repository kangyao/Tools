import time

from repo_tool.models import InitOptions, ProfileDocument
from repo_tool.profiles import ProfileStore
from repo_tool.ui.dialogs import ProfilesDialog
from repo_tool.ui.window import MainWindow
from test_git_service import git, make_profile, remote  # noqa: F401  (fixture)
from test_ui import qapp, wait_job  # noqa: F401  (fixture)


def column(dialog, label):
    return next(i for i in range(dialog.table.columnCount()) if dialog.table.horizontalHeaderItem(i).text() == label)


def test_dialog_edits_init_options_and_scans_sources(tmp_path, qapp, remote):
    source = tmp_path / "源 工程"
    git(tmp_path, "clone", str(remote), str(source))
    profile = make_profile(tmp_path / "checkout", remote)
    dialog = ProfilesDialog(ProfileDocument(1, profile.id, [profile]), data_dir=tmp_path / "app")
    assert dialog.init_mode.currentData() == "network"

    dialog.init_mode.setCurrentIndex(dialog.init_mode.findData("auto"))
    dialog.init_fallback.setCurrentIndex(1)
    dialog.reuse_lfs.setChecked(False)
    dialog.sources_edit.setPlainText(f"{source}\n\n{tmp_path / 'other'}")
    dialog.table.item(0, column(dialog, "初始化来源（可选）")).setText(str(source))
    dialog.copy_profile()
    for saved in dialog.document.profiles:
        assert saved.init == InitOptions("auto", [str(source), str(tmp_path / "other")], True, False, False)
        assert saved.repo("root").init_source == str(source)

    dialog.scan_sources()
    deadline = time.monotonic() + 60
    while dialog.scan_thread is not None:
        qapp.processEvents()
        time.sleep(.01)
        assert time.monotonic() < deadline
    qapp.processEvents()
    assert f"Git 来源：{source.resolve()}" in dialog.scan_report
    assert "目录不存在" in dialog.scan_report
    dialog.close()


def test_main_window_shows_init_plan_and_syncs_with_local_source(tmp_path, qapp, remote):
    source = tmp_path / "source"
    git(tmp_path, "clone", str(remote), str(source))
    profile = make_profile(tmp_path / "checkout", remote)
    profile.init = InitOptions("auto", [str(source)])
    data = tmp_path / "app"
    ProfileStore(data).save(ProfileDocument(1, profile.id, [profile]))
    window = MainWindow(data, auto_scan=False)
    window.show()
    window.start_job("init-plan", ["root"])
    wait_job(window, qapp)
    assert "root" in window.init_plans
    assert f"Git 来源：{source.resolve()}" in window.plan.toPlainText()
    assert "初始化计划已生成" in window.message.text()
    assert not (tmp_path / "checkout").exists()

    window.start_job("sync", ["root"])
    wait_job(window, qapp)
    result = window.results["root"]
    assert result.outcome == "success", result.message
    assert result.message.startswith("初始化加速")
    assert any("初始化阶段 2/6" in line for _, line in window.log_entries)
    window.close()
