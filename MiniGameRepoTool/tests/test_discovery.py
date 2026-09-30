from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from repo_tool.discovery import discover_profile, find_repositories
from repo_tool.models import Profile, ProfileDocument, RepoSpec, SetupOptions, validate_profile
from repo_tool.profiles import default_profile
from repo_tool.ui.dialogs import ProfilesDialog
from test_git_service import commit, git
from test_ui import qapp  # noqa: F401  (fixture)


def make_remote(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    commit(path)
    return path


def test_discovery_maps_existing_repositories_onto_template(tmp_path):
    main_remote = make_remote(tmp_path / "remotes/main")
    child_remote = make_remote(tmp_path / "remotes/child")
    extra_remote = make_remote(tmp_path / "remotes/extra")
    git(child_remote, "branch", "feature")
    root = tmp_path / "工程"
    git(tmp_path, "clone", str(main_remote), str(root))
    git(tmp_path, "clone", "-b", "feature", str(child_remote), str(root / "Assets"))
    git(tmp_path, "clone", str(extra_remote), str(root / "Tools/Extra"))
    (root / "NoOrigin").mkdir()
    git(root / "NoOrigin", "init", "-b", "main")
    (root / "Projects/Generated").mkdir(parents=True)
    git(root / "Projects/Generated", "init", "-b", "main")

    template = Profile("p", "新方案", str(root), {"game": "old", "engine": "eng"}, [
        RepoSpec("main", "Main", ".", str(main_remote), "game"),
        RepoSpec("child", "Child", "Content", str(child_remote), "game", ignore_changes=True),
        RepoSpec("engine", "Engine", "Engine", "git@example.invalid:group/engine.git", "engine"),
    ], SetupOptions(False, ["main", "child", "engine"]))

    found = find_repositories(root)
    assert root / "Projects/Generated" not in found
    result = discover_profile(root, template)

    repos = {r.id: r for r in result.repositories}
    assert list(repos) == ["main", "child", "engine", "tools-extra"]
    assert (repos["main"].path, repos["main"].enabled) == (".", True)
    # Matched by remote identity: the path follows the disk, the options follow the template.
    assert (repos["child"].path, repos["child"].branch_group, repos["child"].ignore_changes) == ("Assets", "game", True)
    assert repos["engine"].enabled is False
    assert (repos["tools-extra"].path, repos["tools-extra"].branch, repos["tools-extra"].branch_group) == \
        ("Tools/Extra", "main", "")
    assert result.branch_groups == {"game": "main", "engine": "eng"}
    assert result.required == ["main", "child", "tools-extra"]
    assert result.found == 3
    assert any("NoOrigin" in note for note in result.notes)
    assert any("Child" in note and "feature" in note for note in result.notes)
    assert any("Engine" in note and "未找到" in note for note in result.notes)

    updated = replace(template, branch_groups=result.branch_groups, repositories=result.repositories,
                      setup=SetupOptions(False, result.required))
    validate_profile(updated)


def test_dialog_recognizes_profile_whose_repository_table_is_empty(tmp_path, qapp, monkeypatch):
    remote = make_remote(tmp_path / "remotes/extra")
    root = tmp_path / "工程"
    root.mkdir()
    git(tmp_path, "clone", str(remote), str(root / "Tools/Extra"))
    empty = Profile("p", "空方案", str(root), {}, [])
    dialog = ProfilesDialog(ProfileDocument(1, "p", [empty]), data_dir=tmp_path / "app")
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError(args)))
    dialog.guard(dialog.discover_from_root)
    saved = dialog.document.profiles[0]
    builtin = [r.id for r in default_profile().repositories]
    assert [r.id for r in saved.repositories] == builtin + ["tools-extra"]
    assert [r.id for r in saved.repositories if r.enabled] == ["tools-extra"]
    assert saved.branch_groups == default_profile().branch_groups
    assert dialog.table.rowCount() == len(builtin) + 1


def test_discovery_of_directory_without_repositories(tmp_path):
    template = Profile("p", "新方案", str(tmp_path), {"game": "main"}, [
        RepoSpec("main", "Main", ".", "git@example.invalid:group/main.git", "game")])
    result = discover_profile(tmp_path, template)
    assert result.found == 0
    assert [r.enabled for r in result.repositories] == [False]
