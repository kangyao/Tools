import pytest

from repo_tool.changes import ChangesService
from repo_tool.models import RepoSpec
from repo_tool.service import RepoService
from test_git_service import commit, git, make_profile, remote


def checkout(tmp_path, remote):
    root = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(root))
    profile = make_profile(root, remote)
    profile.repo("root").force_update = True
    return root, profile, RepoService(tmp_path / "app")


def test_force_check_plans_without_discarding_files_or_index(tmp_path, remote):
    root, profile, service = checkout(tmp_path, remote)
    before = git(root, "rev-parse", "HEAD")
    (root / "file.txt").write_text("staged", encoding="utf-8")
    git(root, "add", "file.txt")
    (root / "file.txt").write_text("working", encoding="utf-8")
    commit(remote, "later.txt", "later")
    snapshot = service.check(profile, ["root"])["root"]
    assert snapshot.action == "force_update"
    assert snapshot.remote_checked and snapshot.changes
    assert git(root, "rev-parse", "HEAD") == before
    assert git(root, "show", ":file.txt") == "staged"
    assert (root / "file.txt").read_text(encoding="utf-8") == "working"
    assert not (tmp_path / "app/discard-backups").exists()


def test_force_sync_discards_staged_unstaged_and_untracked_without_backup(tmp_path, remote):
    commit(remote, ".gitignore", "cache.bin\n")
    root, profile, service = checkout(tmp_path, remote)
    (root / "file.txt").write_text("staged", encoding="utf-8")
    git(root, "add", "file.txt")
    (root / "file.txt").write_text("working", encoding="utf-8")
    (root / "暂存[1].txt").write_text("new staged", encoding="utf-8")
    git(root, "add", "暂存[1].txt")
    (root / "未跟踪.txt").write_text("untracked", encoding="utf-8")
    (root / "cache.bin").write_text("ignored", encoding="utf-8")
    expected = commit(remote, text="remote update")
    result = service.sync(profile, ["root"])["root"]
    assert result.outcome == "success", result.message
    assert git(root, "rev-parse", "HEAD") == expected
    assert (root / "file.txt").read_text(encoding="utf-8") == "remote update"
    assert not (root / "暂存[1].txt").exists()
    assert not (root / "未跟踪.txt").exists()
    assert (root / "cache.bin").read_text(encoding="utf-8") == "ignored"
    assert not git(root, "status", "--porcelain")
    assert not (tmp_path / "app/discard-backups").exists()


@pytest.mark.parametrize("diverged", [False, True])
def test_force_keeps_local_commits_and_does_not_discard_when_diverged(tmp_path, remote, diverged):
    root, profile, service = checkout(tmp_path, remote)
    local = commit(root, "local.txt", "local commit")
    (root / "file.txt").write_text("working", encoding="utf-8")
    if diverged:
        commit(remote, "remote.txt", "remote commit")
    result = service.sync(profile, ["root"])["root"]
    assert git(root, "rev-parse", "HEAD") == local
    assert (root / "local.txt").read_text(encoding="utf-8") == "local commit"
    assert result.outcome == ("blocked" if diverged else "success")
    assert (root / "file.txt").read_text(encoding="utf-8") == ("working" if diverged else "one")


def test_force_switch_keeps_previous_branch_commits(tmp_path, remote):
    git(remote, "switch", "-c", "feature")
    feature = commit(remote, "feature.txt", "feature")
    git(remote, "switch", "main")
    root, profile, service = checkout(tmp_path, remote)
    old_branch_head = commit(root, "local.txt", "local commit")
    (root / "file.txt").write_text("dirty", encoding="utf-8")
    profile.branch_groups["game"] = "feature"
    result = service.sync(profile, ["root"])["root"]
    assert result.outcome == "success", result.message
    assert git(root, "branch", "--show-current") == "feature"
    assert git(root, "rev-parse", "HEAD") == feature
    assert git(root, "rev-parse", "main") == old_branch_head


def test_force_missing_remote_branch_preserves_uncommitted_files(tmp_path, remote):
    root, profile, service = checkout(tmp_path, remote)
    profile.branch_groups["game"] = "missing"
    (root / "file.txt").write_text("keep", encoding="utf-8")
    result = service.sync(profile, ["root"])["root"]
    assert result.outcome != "success"
    assert (root / "file.txt").read_text(encoding="utf-8") == "keep"


def test_force_does_not_discard_before_worktree_branch_conflict(tmp_path, remote):
    git(remote, "branch", "feature")
    root, profile, service = checkout(tmp_path, remote)
    git(root, "worktree", "add", str(tmp_path / "linked"), "feature")
    profile.branch_groups["game"] = "feature"
    (root / "file.txt").write_text("keep", encoding="utf-8")
    result = service.sync(profile, ["root"])["root"]
    assert result.outcome != "success"
    assert (root / "file.txt").read_text(encoding="utf-8") == "keep"


def test_force_preserves_configured_nested_repository(tmp_path, remote):
    root, profile, service = checkout(tmp_path, remote)
    child = root / "Child"
    git(root, "clone", str(remote), str(child))
    profile.repositories.append(RepoSpec("child", "Child", "Child", str(remote), branch="main"))
    child_head = commit(child, "local.txt", "child commit")
    (child / "file.txt").write_text("child working", encoding="utf-8")
    (root / "file.txt").write_text("parent working", encoding="utf-8")
    expected = commit(remote, text="remote update")
    result = service.sync(profile, ["root"])["root"]
    assert result.outcome == "success", result.message
    assert git(root, "rev-parse", "HEAD") == expected
    assert git(child, "rev-parse", "HEAD") == child_head
    assert (child / "file.txt").read_text(encoding="utf-8") == "child working"


def test_force_layout_conflict_keeps_parent_changes_and_child(tmp_path, remote):
    root, profile, service = checkout(tmp_path, remote)
    git(root, "clone", str(remote), str(root / "Child"))
    profile.repositories.append(RepoSpec("child", "Child", "Child", str(remote), branch="main"))
    (root / "file.txt").write_text("keep", encoding="utf-8")
    (remote / "Child").mkdir()
    commit(remote, "Child/remote.txt", "collision")
    result = service.sync(profile, ["root"])["root"]
    assert result.snapshot.status == "layout_conflict"
    assert (root / "file.txt").read_text(encoding="utf-8") == "keep"
    assert (root / "Child/.git").exists()


def test_force_does_not_discard_unselected_dirty_parent(tmp_path, remote):
    root, profile, service = checkout(tmp_path, remote)
    git(root, "clone", str(remote), str(root / "Child"))
    profile.repositories.append(RepoSpec("child", "Child", "Child", str(remote), branch="main"))
    (root / "file.txt").write_text("keep", encoding="utf-8")
    result = service.sync(profile, ["child"])
    assert result["child"].outcome == "blocked"
    assert (root / "file.txt").read_text(encoding="utf-8") == "keep"


def test_force_rechecks_explicit_selection_when_parent_becomes_dirty_mid_sync(tmp_path, remote):
    git(remote, "branch", "feature")
    root, profile, service = checkout(tmp_path, remote)
    git(root, "clone", str(remote), str(root / "Child"))
    profile.repo("root").branch_group = ""
    profile.repo("root").branch = "feature"
    profile.repositories.append(RepoSpec("child", "Child", "Child", str(remote), branch="main"))
    def changed_after_remote_check(kind, payload):
        if kind == "snapshot" and payload.repo_id == "root" and payload.remote_checked:
            (root / "file.txt").write_text("new work after check", encoding="utf-8")
    service.emit = changed_after_remote_check
    result = service.sync(profile, ["child"])
    assert result["root"].outcome == "blocked"
    assert result["child"].outcome == "blocked"
    assert git(root, "branch", "--show-current") == "main"
    assert (root / "file.txt").read_text(encoding="utf-8") == "new work after check"


def test_ignore_reminder_alone_preserves_dirty_files_and_diff(tmp_path, remote):
    root, profile, service = checkout(tmp_path, remote)
    profile.repo("root").force_update = False
    profile.repo("root").ignore_changes = True
    (root / "file.txt").write_text("still visible in diff", encoding="utf-8")
    assert service.sync(profile, ["root"])["root"].outcome == "blocked"
    assert (root / "file.txt").read_text(encoding="utf-8") == "still visible in diff"
    changes = ChangesService(tmp_path / "app")
    assert changes.scan(profile, "root").files[0].path == "file.txt"
    assert "+still visible in diff" in changes.diff(profile, "root", "file.txt")


def test_force_rejects_unconfigured_nested_repo_before_discarding_any_file(tmp_path, remote):
    root, profile, service = checkout(tmp_path, remote)
    git(root, "clone", str(remote), str(root / "Unlisted"))
    (root / "file.txt").write_text("keep parent", encoding="utf-8")
    result = service.sync(profile, ["root"])["root"]
    assert result.outcome == "blocked"
    assert "Unlisted" in result.message
    assert (root / "file.txt").read_text(encoding="utf-8") == "keep parent"
    assert (root / "Unlisted/.git").exists()


def test_force_preserves_ignored_replacement_of_staged_deleted_file(tmp_path, remote):
    root, profile, service = checkout(tmp_path, remote)
    git(root, "rm", "--cached", "file.txt")
    (root / ".git/info/exclude").write_text("file.txt\n", encoding="utf-8")
    (root / "file.txt").write_text("valuable ignored replacement", encoding="utf-8")
    before = git(root, "status", "--porcelain", "--ignored")
    assert "D  file.txt" in before and "!! file.txt" in before
    result = service.sync(profile, ["root"])["root"]
    assert result.outcome == "blocked"
    assert (root / "file.txt").read_text(encoding="utf-8") == "valuable ignored replacement"
    assert git(root, "status", "--porcelain", "--ignored") == before


def test_force_rejects_stash_conflict_without_losing_unrelated_work(tmp_path, remote):
    root, profile, service = checkout(tmp_path, remote)
    (root / "file.txt").write_text("stashed", encoding="utf-8")
    git(root, "stash", "push")
    commit(root, text="committed")
    git(root, "stash", "apply", check=False)
    assert not (root / ".git/MERGE_HEAD").exists()
    (root / "unrelated.txt").write_text("keep", encoding="utf-8")
    before = git(root, "ls-files", "-u")
    assert before
    result = service.sync(profile, ["root"])["root"]
    assert result.outcome == "blocked"
    assert git(root, "ls-files", "-u") == before
    assert (root / "unrelated.txt").read_text(encoding="utf-8") == "keep"


def test_force_does_not_update_submodule_or_discard_its_work(tmp_path, remote):
    root, profile, service = checkout(tmp_path, remote)
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", str(remote), "Module")
    git(root, "commit", "-m", "Add module")
    child = root / "Module"
    child_head = git(child, "rev-parse", "HEAD")
    (child / "file.txt").write_text("keep child", encoding="utf-8")
    profile.repositories.append(RepoSpec("child", "Child", "Module", str(remote),
                                         branch="main", force_update=True))
    result = service.sync(profile, ["child"])["child"]
    assert result.outcome == "blocked"
    assert git(child, "rev-parse", "HEAD") == child_head
    assert (child / "file.txt").read_text(encoding="utf-8") == "keep child"
