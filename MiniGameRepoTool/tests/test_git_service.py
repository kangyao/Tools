import subprocess
import threading
from pathlib import Path

import pytest

from repo_tool.models import Profile, RepoSpec, SetupOptions
from repo_tool.service import RepoService


def git(path, *args, check=True):
    p = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
                        "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check:
        assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def commit(path, name="file.txt", text="one"):
    (path / name).write_text(text, encoding="utf-8")
    git(path, "add", name)
    git(path, "commit", "-m", text)
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote"
    path.mkdir()
    git(path, "init", "-b", "main")
    commit(path)
    return path


def make_profile(root, remote):
    return Profile("p", "Local", str(root), {"game": "main"}, [
        RepoSpec("root", "Root", ".", str(remote), "game"),
    ], SetupOptions(False, ["root"]))


@pytest.mark.parametrize("target", ["remote", "local"])
@pytest.mark.parametrize("child_path", ["Engine/Child", "ENGINE/Child"])
def test_parent_cannot_replace_ignored_child_ancestor_with_file(tmp_path, remote, target, child_path):
    commit(remote, ".gitignore", "Engine/\n")
    git(remote, "branch", "feature")
    root = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(root))
    p = make_profile(root, remote)
    p.repositories.append(RepoSpec("child", "Child", child_path, str(remote), branch="main"))
    if target == "remote":
        commit(remote, "Engine", "tracked-file")
    else:
        git(root, "switch", "-c", "feature", "origin/feature")
        commit(root, "Engine", "local-target-file")
        git(root, "switch", "main")
        p.branch_groups["game"] = "feature"
    child = root / "Engine" / "Child"
    git(tmp_path, "clone", str(remote), str(child))
    child_head = commit(child, "local.txt", "local-child-work")
    before = git(root, "rev-parse", "HEAD")
    result = service(tmp_path).sync(p, ["root"])["root"]
    assert result.outcome == "blocked"
    assert result.snapshot.status == "layout_conflict"
    assert git(root, "rev-parse", "HEAD") == before
    assert child.is_dir()
    assert git(child, "rev-parse", "HEAD") == child_head
    assert (child / "local.txt").read_text(encoding="utf-8") == "local-child-work"


@pytest.mark.parametrize("branch", ["main", "feature"])
def test_sync_preserves_ignored_user_files_when_target_starts_tracking_them(tmp_path, remote, branch):
    commit(remote, ".gitignore", "cache.bin\n")
    root = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(root))
    before = git(root, "rev-parse", "HEAD")
    (root / "cache.bin").write_text("user-data", encoding="utf-8")
    if branch != "main":
        git(remote, "switch", "-c", branch)
    (remote / "cache.bin").write_text("remote-data", encoding="utf-8")
    git(remote, "add", "-f", "cache.bin")
    git(remote, "commit", "-m", "Track previously ignored file")
    p = make_profile(root, remote)
    p.branch_groups["game"] = branch
    result = service(tmp_path).sync(p, ["root"])["root"]
    assert result.outcome != "success"
    assert (root / "cache.bin").read_text(encoding="utf-8") == "user-data"
    assert git(root, "rev-parse", "HEAD") == before


@pytest.mark.parametrize("create_directory", [False, True])
@pytest.mark.parametrize("configured_path", ["module", "MODULE", "MODULE/subdir"])
def test_empty_uninitialized_submodule_is_blocked_before_clone(tmp_path, remote, create_directory, configured_path):
    old = git(remote, "rev-parse", "HEAD")
    commit(remote, "later.txt", "later")
    parent = tmp_path / "parent"
    parent.mkdir()
    git(parent, "init", "-b", "main")
    commit(parent)
    git(parent, "update-index", "--add", "--cacheinfo", f"160000,{old},module")
    git(parent, "commit", "-m", "Register gitlink")
    module = parent / "module"
    if create_directory:
        module.mkdir()
    before_status = git(parent, "status", "--porcelain")
    profile = make_profile(parent / configured_path, remote)
    result = service(tmp_path).sync(profile, ["root"])["root"]
    assert result.outcome == "blocked"
    assert result.snapshot.status == "submodule"
    assert not module.exists() or list(module.iterdir()) == []
    assert git(parent, "status", "--porcelain") == before_status


def test_auto_setup_requires_unselected_required_repositories(tmp_path, remote):
    p = make_profile(tmp_path / "checkout", remote)
    p.repositories.append(RepoSpec("required", "Required", "required", str(remote), "game"))
    p.setup = SetupOptions(True, ["root", "required"])
    result = service(tmp_path).sync(p, ["root"])
    assert result["root"].outcome == "success"
    assert result["__setup__"].outcome == "blocked"
    assert "Required" in result["__setup__"].message
    assert not (Path(p.root) / "required").exists()


def service(tmp_path, **kwargs):
    return RepoService(tmp_path / "app", **kwargs)


def test_clone_empty_directory_nested_in_parent_is_own_repo(tmp_path, remote):
    parent = tmp_path / "parent"
    parent.mkdir()
    git(parent, "init", "-b", "main")
    parent_head = commit(parent)
    target = parent / "AssetRuntime"
    target.mkdir()
    p = make_profile(target, remote)
    svc = service(tmp_path)
    assert svc.check(p, ["root"], remote=False)["root"].status == "empty"
    result = svc.sync(p, ["root"])
    assert result["root"].outcome == "success"
    assert Path(git(target, "rev-parse", "--show-toplevel")).resolve() == target.resolve()
    assert git(parent, "rev-parse", "HEAD") == parent_head


def test_populated_nonrepo_is_preserved_and_branch_missing_is_distinct(tmp_path, remote):
    target = tmp_path / "工作 区"
    target.mkdir()
    (target / ".keep").write_text("keep", encoding="utf-8")
    p = make_profile(target, remote)
    result = service(tmp_path).sync(p, ["root"])
    assert result["root"].outcome == "blocked"
    assert (target / ".keep").read_text() == "keep"
    p.root = str(tmp_path / "missing")
    p.branch_groups["game"] = "no-such-branch"
    snapshot = service(tmp_path).check(p, ["root"])["root"]
    assert snapshot.status == "branch_missing"
    assert not Path(p.root).exists()


def test_fast_forward_dirty_ahead_and_divergence(tmp_path, remote):
    target = tmp_path / "checkout"
    p = make_profile(target, remote)
    svc = service(tmp_path)
    assert svc.sync(p, ["root"])["root"].outcome == "success"
    latest = commit(remote, text="two")
    assert svc.sync(p, ["root"])["root"].outcome == "success"
    assert git(target, "rev-parse", "HEAD") == latest
    (target / "file.txt").write_text("dirty")
    assert svc.sync(p, ["root"])["root"].outcome == "blocked"
    local_head = commit(target, text="local")
    assert svc.check(p, ["root"])["root"].status == "ahead"
    assert svc.sync(p, ["root"])["root"].outcome == "success"
    assert git(target, "rev-parse", "HEAD") == local_head
    commit(remote, "remote.txt", "remote")
    assert svc.check(p, ["root"])["root"].status == "diverged"
    assert svc.sync(p, ["root"])["root"].outcome == "blocked"
    assert git(target, "rev-parse", "HEAD") == local_head


def test_switch_compares_target_branch_not_current(tmp_path, remote):
    git(remote, "switch", "-c", "feature")
    feature_head = commit(remote, "feature.txt", "feature")
    git(remote, "switch", "main")
    p = make_profile(tmp_path / "checkout", remote)
    svc = service(tmp_path)
    svc.sync(p, ["root"])
    git(Path(p.root), "switch", "-c", "local-only")
    commit(Path(p.root), "local.txt", "local")
    p.branch_groups["game"] = "feature"
    assert svc.sync(p, ["root"])["root"].outcome == "success"
    assert git(Path(p.root), "branch", "--show-current") == "feature"
    assert git(Path(p.root), "rev-parse", "HEAD") == feature_head
    assert "local-only" in git(Path(p.root), "branch", "--list")


def test_parent_dependency_autoinclusion_and_untracked_nested_repo(tmp_path, remote):
    p = make_profile(tmp_path / "checkout", remote)
    p.repositories.append(RepoSpec("child", "Child", "Assets", str(remote), "game"))
    svc = service(tmp_path)
    result = svc.sync(p, ["child"])
    assert list(result) == ["root", "child"]
    assert all(r.outcome == "success" for r in result.values())
    assert svc.check(p, ["root"], remote=False)["root"].status == "up_to_date"
    (Path(p.root) / "own-untracked.txt").write_text("keep")
    assert svc.check(p, ["root"], remote=False)["root"].status == "dirty"


def test_parent_failure_blocks_child_and_keeps_independent_repo(tmp_path, remote):
    root = tmp_path / "checkout"
    root.mkdir()
    p = Profile("p", "Local", str(root), {"game":"main"}, [
        RepoSpec("a", "A", "A", str(remote), branch="missing"),
        RepoSpec("child", "Child", "A/child", str(remote), "game"),
        RepoSpec("b", "B", "B", str(remote), "game"),
    ])
    result = service(tmp_path).sync(p, ["a", "child", "b"])
    assert result["a"].outcome == "failed"
    assert result["child"].outcome == "blocked"
    assert result["b"].outcome == "success"
    assert not (root / "A" / "child").exists()


def test_stop_queue_finishes_current_then_stops(tmp_path, remote):
    stop = threading.Event()
    p = Profile("p","Stop",str(tmp_path / "checkout"),{"game":"main"},[
        RepoSpec("a","A","A",str(remote),"game"), RepoSpec("b","B","B",str(remote),"game")])
    def emit(kind, payload):
        if kind == "result" and payload.repo_id == "a":
            stop.set()
    result = service(tmp_path, stop=stop, emit=emit).sync(p, ["a","b"])
    assert result["a"].outcome == "success"
    assert result["b"].outcome == "cancelled"
    assert not (Path(p.root)/"B").exists()


def test_target_layout_cannot_overwrite_configured_nested_repo(tmp_path, remote):
    p = make_profile(tmp_path / "checkout", remote)
    p.repositories.append(RepoSpec("child","Child","Assets",str(remote),"game"))
    svc = service(tmp_path)
    svc.sync(p, ["root","child"])
    (remote / "Assets").mkdir()
    commit(remote, "Assets/file.txt", "collision")
    root_head = git(Path(p.root), "rev-parse","HEAD")
    result = svc.sync(p, ["root"])
    assert result["root"].outcome == "blocked"
    assert git(Path(p.root),"rev-parse","HEAD") == root_head


def test_valid_worktree_gitfile_and_incomplete_repository(tmp_path, remote):
    p = make_profile(tmp_path / "checkout", remote)
    svc = service(tmp_path)
    svc.sync(p, ["root"])
    worktree = tmp_path / "linked"
    git(Path(p.root), "worktree", "add", "-b", "linked", str(worktree))
    p.root = str(worktree)
    p.branch_groups["game"] = "linked"
    assert svc.check(p, ["root"], remote=False)["root"].status == "unchecked"
    empty = tmp_path / "incomplete"
    empty.mkdir()
    git(empty,"init","-b","main")
    p.root = str(empty)
    assert svc.check(p, ["root"], remote=False)["root"].status == "incomplete"


def test_child_sync_does_not_update_existing_unselected_parent(tmp_path, remote):
    p = make_profile(tmp_path / "checkout", remote)
    p.repositories.append(RepoSpec("child", "Child", "Assets", str(remote), "game"))
    svc = service(tmp_path)
    svc.sync(p, ["root", "child"])
    parent_head = git(Path(p.root), "rev-parse", "HEAD")
    commit(remote, text="new version")
    git(Path(p.root), "fetch", "origin")
    result = svc.sync(p, ["child"])
    assert list(result) == ["child"]
    assert git(Path(p.root), "rev-parse", "HEAD") == parent_head
