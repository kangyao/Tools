"""A parent may record a configured child path as a gitlink (MiniGame Source/Core/RainbowEngine).

The child is still cloned and updated on its own branch; the parent's gitlink is left alone.
"""
from repo_tool.models import RepoSpec
from repo_tool.service import RepoService
from test_git_service import commit, git, make_profile


def gitlink(repo, path, oid):
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},{path}")
    git(repo, "commit", "-m", "gitlink " + oid[:7])


def workspace(tmp_path, parent_force=False):
    child_remote = tmp_path / "child-remote"
    child_remote.mkdir()
    git(child_remote, "init", "-b", "main")
    pinned = commit(child_remote, "engine.txt", "pinned")
    git(child_remote, "switch", "-c", "release")
    commit(child_remote, "engine.txt", "release-one")
    parent_remote = tmp_path / "parent-remote"
    parent_remote.mkdir()
    git(parent_remote, "init", "-b", "main")
    commit(parent_remote, "file.txt", "parent")
    # No .gitmodules: an orphan gitlink, as on sandboxgame/ugc-1029.
    gitlink(parent_remote, "Core/Engine", pinned)
    root = tmp_path / "checkout"
    profile = make_profile(root, parent_remote)
    profile.repo("root").force_update = parent_force
    profile.repositories.append(RepoSpec("engine", "Engine", "Core/Engine", str(child_remote), branch="release"))
    return root, profile, child_remote, parent_remote, pinned


def test_gitlink_child_is_cloned_and_updated_on_its_branch(tmp_path):
    root, profile, child_remote, parent_remote, pinned = workspace(tmp_path)
    service = RepoService(tmp_path / "app")
    results = service.sync(profile, ["root", "engine"])
    assert results["root"].outcome == "success", results["root"].message
    assert results["engine"].outcome == "success", results["engine"].message
    engine = root / "Core" / "Engine"
    assert git(engine, "symbolic-ref", "--short", "HEAD") == "release"
    assert (engine / "engine.txt").read_text(encoding="utf-8") == "release-one"

    # Neither repository reports the other's state as a problem.
    checks = service.check(profile, ["root", "engine"])
    assert checks["root"].status == "up_to_date" and checks["root"].changes == []
    assert checks["engine"].status == "up_to_date", checks["engine"].message

    commit(child_remote, "engine.txt", "release-two")
    git(parent_remote, "switch", "--detach")
    git(parent_remote, "switch", "main")
    commit(parent_remote, "file.txt", "parent-two")
    gitlink(parent_remote, "Core/Engine", git(child_remote, "rev-parse", "release"))
    results = service.sync(profile, ["root", "engine"])
    assert results["root"].outcome == "success", results["root"].message
    assert results["engine"].outcome == "success", results["engine"].message
    assert git(root, "rev-parse", "HEAD") == git(parent_remote, "rev-parse", "main")
    assert (engine / "engine.txt").read_text(encoding="utf-8") == "release-two"
    assert git(root, "ls-files", "--stage", "Core/Engine").split()[1] == git(child_remote, "rev-parse", "release")


def test_force_update_parent_keeps_gitlink_child(tmp_path):
    root, profile, _, _, pinned = workspace(tmp_path, parent_force=True)
    service = RepoService(tmp_path / "app")
    service.sync(profile, ["root", "engine"])
    engine = root / "Core" / "Engine"
    (engine / "local.txt").write_text("child work", encoding="utf-8")
    (root / "file.txt").write_text("discard me", encoding="utf-8")
    result = service.sync(profile, ["root"])["root"]
    assert result.outcome == "success", result.message
    assert (root / "file.txt").read_text(encoding="utf-8") == "parent"
    assert (engine / "local.txt").read_text(encoding="utf-8") == "child work"
    assert git(engine, "symbolic-ref", "--short", "HEAD") == "release"
