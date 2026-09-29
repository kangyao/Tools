import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from repo_tool.changes import ChangesService
from repo_tool.models import RepoSpec
from test_git_service import git, commit, make_profile


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "工作 区"
    root.mkdir()
    git(root, "init", "-b", "main")
    for name in ("tracked.txt", "keep.txt", "old name.txt", "deleted.txt", "[note].txt"):
        (root / name).write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial")
    return make_profile(root, tmp_path / "remote"), root, ChangesService(tmp_path / "app")


def test_list_and_diff_separate_index_worktree_and_untracked(workspace):
    profile, root, service = workspace
    (root / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(root, "add", "tracked.txt")
    (root / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (root / "中文 file.pdb").write_bytes(b"\0binary")
    snapshot = service.scan(profile, "root")
    changes = {c.path: c for c in snapshot.files}
    assert changes["tracked.txt"].index_status == "M"
    assert changes["tracked.txt"].worktree_status == "M"
    assert changes["中文 file.pdb"].kind == "untracked"
    preview = service.diff(profile, "root", "tracked.txt")
    assert "staged" in preview and "unstaged" in preview
    assert "二进制" in service.diff(profile, "root", "中文 file.pdb")


def test_discard_only_selected_files_and_backup_both_versions(workspace):
    profile, root, service = workspace
    head = git(root, "rev-parse", "HEAD")
    (root / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(root, "add", "tracked.txt")
    (root / "tracked.txt").write_text("working\n", encoding="utf-8")
    working_bytes = (root / "tracked.txt").read_bytes()
    (root / "keep.txt").write_text("keep my work\n", encoding="utf-8")
    (root / "extra.pdb").write_bytes(b"\0\xffuntracked")
    snapshot = service.scan(profile, "root")
    result = service.discard(profile, "root", snapshot, ["tracked.txt", "extra.pdb"])
    assert result.outcome == "success"
    assert (root / "tracked.txt").read_text() == "base\n"
    assert not (root / "extra.pdb").exists()
    assert (root / "keep.txt").read_text() == "keep my work\n"
    assert git(root, "rev-parse", "HEAD") == head
    backup = Path(result.backup_dir)
    assert (backup / "worktree/tracked.txt").read_bytes() == working_bytes
    assert (backup / "worktree/extra.pdb").read_bytes() == b"\0\xffuntracked"
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["head"] == head and manifest["status"] == "completed"
    git(root, "apply", "--cached", str(backup / "staged.patch"))
    assert git(root, "show", ":tracked.txt") == "staged"


def test_discard_rename_new_tracked_and_deleted_files(workspace):
    profile, root, service = workspace
    git(root, "mv", "old name.txt", "new name.txt")
    (root / "new staged.txt").write_text("new\n", encoding="utf-8")
    git(root, "add", "new staged.txt")
    (root / "deleted.txt").unlink()
    snapshot = service.scan(profile, "root")
    renamed = next(c for c in snapshot.files if c.path == "new name.txt")
    assert renamed.original_path == "old name.txt"
    result = service.discard(profile, "root", snapshot, ["new name.txt", "new staged.txt", "deleted.txt"])
    assert result.outcome == "success"
    assert (root / "old name.txt").read_text() == "base\n"
    assert not (root / "new name.txt").exists()
    assert not (root / "new staged.txt").exists()
    assert (root / "deleted.txt").read_text() == "base\n"
    assert git(root, "status", "--porcelain") == ""


def test_discard_treats_pathspec_metacharacters_literally(workspace):
    profile, root, service = workspace
    (root / "[note].txt").write_text("changed", encoding="utf-8")
    (root / "n.txt").write_text("keep", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    result = service.discard(profile, "root", snapshot, ["[note].txt"])
    assert result.outcome == "success"
    assert (root / "[note].txt").read_text() == "base\n"
    assert (root / "n.txt").read_text() == "keep"


@pytest.mark.parametrize("change", ["file", "index", "head"])
def test_stale_selection_is_rejected(workspace, change):
    profile, root, service = workspace
    (root / "tracked.txt").write_text("first edit", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    if change == "file":
        (root / "tracked.txt").write_text("new edit after preview", encoding="utf-8")
    elif change == "index":
        git(root, "add", "tracked.txt")
    else:
        commit(root, "another.txt", "later commit")
    before = (root / "tracked.txt").read_bytes()
    with pytest.raises(ValueError, match="变化|刷新"):
        service.discard(profile, "root", snapshot, ["tracked.txt"])
    assert (root / "tracked.txt").read_bytes() == before


def test_backup_failure_does_not_modify_files(workspace, monkeypatch):
    profile, root, service = workspace
    (root / "tracked.txt").write_text("keep this", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(shutil, "copy2", fail)
    with pytest.raises(OSError, match="disk full"):
        service.discard(profile, "root", snapshot, ["tracked.txt"])
    assert (root / "tracked.txt").read_text() == "keep this"


def test_submodule_pointer_is_protected_but_own_files_can_be_discarded(workspace, tmp_path):
    profile, root, service = workspace
    remote = tmp_path / "child-source"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    commit(remote)
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", str(remote), "child")
    git(root, "commit", "-m", "Submodule")
    child = root / "child"
    child_head = commit(child, "local.txt", "local commit")
    (child / "file.txt").write_text("child edit", encoding="utf-8")
    profile.repositories.append(RepoSpec("child", "Child", "child", str(remote), branch="main"))
    parent_snapshot = service.scan(profile, "root")
    row = next(c for c in parent_snapshot.files if c.path == "child")
    assert row.kind == "submodule" and not row.discardable
    with pytest.raises(ValueError):
        service.discard(profile, "root", parent_snapshot, ["child"])
    child_snapshot = service.scan(profile, "child")
    result = service.discard(profile, "child", child_snapshot, ["file.txt"])
    assert result.outcome == "success"
    assert (child / "file.txt").read_text() == "one"
    assert git(child, "rev-parse", "HEAD") == child_head
    assert "child" in git(root, "status", "--porcelain")


def test_untracked_nested_repository_and_outside_path_cannot_be_discarded(workspace):
    profile, root, service = workspace
    nested = root / "nested"
    nested.mkdir()
    git(nested, "init", "-b", "main")
    commit(nested)
    snapshot = service.scan(profile, "root")
    row = next(c for c in snapshot.files if c.path.rstrip("/") == "nested")
    assert not row.discardable
    with pytest.raises(ValueError):
        service.discard(profile, "root", snapshot, [row.path])
    with pytest.raises(ValueError):
        service.discard(profile, "root", snapshot, ["../outside"])
    assert (nested / ".git").is_dir()


def test_binary_staged_backup_is_complete_and_reapplicable(workspace):
    profile, root, service = workspace
    blob = os.urandom(3_200_000)
    (root / "large.bin").write_bytes(blob)
    git(root, "add", "large.bin")
    snapshot = service.scan(profile, "root")
    result = service.discard(profile, "root", snapshot, ["large.bin"])
    patch = Path(result.backup_dir) / "staged.patch"
    assert patch.stat().st_size > 4_000_000
    git(root, "apply", "--cached", str(patch))
    content = subprocess.run(["git", "-C", str(root), "show", ":large.bin"],
                             capture_output=True, check=True).stdout
    assert content == blob
    assert not (root / "large.bin").exists()


def test_renamed_source_recreated_cannot_be_overwritten_implicitly(workspace):
    profile, root, service = workspace
    git(root, "mv", "old name.txt", "new name.txt")
    (root / "old name.txt").write_text("new unrelated file", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    row = next(row for row in snapshot.files if row.path == "new name.txt")
    assert not row.discardable
    with pytest.raises(ValueError):
        service.discard(profile, "root", snapshot, ["new name.txt"])
    assert (root / "old name.txt").read_text() == "new unrelated file"
    assert (root / "new name.txt").exists()


def test_equal_length_edit_with_restored_mtime_is_still_stale(workspace):
    profile, root, service = workspace
    path = root / "tracked.txt"
    path.write_bytes(b"edit-A")
    snapshot = service.scan(profile, "root")
    info = path.stat()
    path.write_bytes(b"edit-B")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(ValueError, match="变化|刷新"):
        service.discard(profile, "root", snapshot, ["tracked.txt"])
    assert path.read_bytes() == b"edit-B"


def test_manifest_failure_leaves_index_and_worktree_untouched(workspace, monkeypatch):
    profile, root, service = workspace
    (root / "tracked.txt").write_text("staged", encoding="utf-8")
    git(root, "add", "tracked.txt")
    snapshot = service.scan(profile, "root")
    def fail(*args, **kwargs):
        raise OSError("manifest cannot be saved")
    monkeypatch.setattr("repo_tool.changes.atomic_json", fail)
    with pytest.raises(OSError, match="manifest"):
        service.discard(profile, "root", snapshot, ["tracked.txt"])
    assert git(root, "show", ":tracked.txt") == "staged"
    assert (root / "tracked.txt").read_text() == "staged"


def test_partial_failure_reports_backup_and_retains_unremoved_files(workspace, monkeypatch):
    profile, root, service = workspace
    (root / "tracked.txt").write_text("tracked edit", encoding="utf-8")
    extra = root / "untracked.txt"
    extra.write_text("untracked edit", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    original = Path.unlink
    def fail_selected(path, *args, **kwargs):
        if path == extra:
            raise PermissionError("file locked")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_selected)
    result = service.discard(profile, "root", snapshot, ["tracked.txt", "untracked.txt"])
    assert result.outcome == "failed"
    assert (root / "tracked.txt").read_text() == "base\n"
    assert extra.read_text() == "untracked edit"
    backup = Path(result.backup_dir)
    assert (backup / "worktree/tracked.txt").read_text() == "tracked edit"
    assert json.loads((backup / "manifest.json").read_text(encoding="utf-8"))["status"] == "partial"


def test_intent_to_add_is_not_claimed_recoverable(workspace):
    profile, root, service = workspace
    (root / "intent.txt").write_text("intent", encoding="utf-8")
    git(root, "add", "-N", "intent.txt")
    snapshot = service.scan(profile, "root")
    row = next(row for row in snapshot.files if row.path == "intent.txt")
    assert not row.discardable
    with pytest.raises(ValueError):
        service.discard(profile, "root", snapshot, ["intent.txt"])


def test_ignored_file_recreated_at_rename_source_is_protected(workspace):
    profile, root, service = workspace
    git(root, "mv", "old name.txt", "new name.txt")
    (root / ".git/info/exclude").write_text("old name.txt\n", encoding="utf-8")
    (root / "old name.txt").write_text("ignored but valuable", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    row = next(row for row in snapshot.files if row.path == "new name.txt")
    assert not row.discardable
    with pytest.raises(ValueError):
        service.discard(profile, "root", snapshot, ["new name.txt"])
    assert (root / "old name.txt").read_text() == "ignored but valuable"


def test_staged_deletion_is_restored_and_its_absence_is_backed_up(workspace):
    profile, root, service = workspace
    git(root, "rm", "deleted.txt")
    snapshot = service.scan(profile, "root")
    result = service.discard(profile, "root", snapshot, ["deleted.txt"])
    assert result.outcome == "success"
    assert (root / "deleted.txt").read_text() == "base\n"
    backup = Path(result.backup_dir)
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["exists"] is False
    git(root, "apply", "--cached", str(backup / "staged.patch"))
    assert "D  deleted.txt" in git(root, "status", "--porcelain")


def test_binary_patch_backup_failure_leaves_original_index_and_file(workspace, monkeypatch):
    profile, root, service = workspace
    (root / "tracked.txt").write_text("staged", encoding="utf-8")
    git(root, "add", "tracked.txt")
    (root / "tracked.txt").write_text("working", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    run = service._run
    def fail_patch(root, args, **kwargs):
        if any(arg.startswith("--output=") for arg in args):
            raise OSError("patch write failed")
        return run(root, args, **kwargs)
    monkeypatch.setattr(service, "_run", fail_patch)
    with pytest.raises(OSError, match="patch write failed"):
        service.discard(profile, "root", snapshot, ["tracked.txt"])
    assert git(root, "show", ":tracked.txt") == "staged"
    assert (root / "tracked.txt").read_text() == "working"


def test_edit_after_backup_aborts_before_restore(workspace):
    profile, root, service = workspace
    (root / "tracked.txt").write_text("before", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    def edit_after_backup(kind, payload):
        if kind == "backup":
            (root / "tracked.txt").write_text("new edit while backing up", encoding="utf-8")
    service.emit = edit_after_backup
    with pytest.raises(ValueError, match="变化|刷新"):
        service.discard(profile, "root", snapshot, ["tracked.txt"])
    assert (root / "tracked.txt").read_text() == "new edit while backing up"


def test_git_symlink_mode_cannot_be_discarded_as_regular_file(workspace):
    profile, root, service = workspace
    blob = git(root, "rev-parse", "HEAD:tracked.txt")
    git(root, "update-index", "--add", "--cacheinfo", "120000," + blob + ",link.txt")
    (root / "link.txt").write_text("regular file now", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    row = next(row for row in snapshot.files if row.path == "link.txt")
    assert not row.discardable
    assert "链接" in row.reason
    with pytest.raises(ValueError):
        service.discard(profile, "root", snapshot, ["link.txt"])


def test_merge_conflict_and_other_files_are_read_only_until_resolved(workspace):
    profile, root, service = workspace
    git(root, "checkout", "-b", "side")
    commit(root, "tracked.txt", "side")
    git(root, "checkout", "main")
    commit(root, "tracked.txt", "main")
    git(root, "merge", "side", check=False)
    (root / "keep.txt").write_text("keep during merge", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    assert snapshot.operation == "MERGE_HEAD"
    assert any(row.kind == "conflict" for row in snapshot.files)
    assert all(not row.discardable for row in snapshot.files)
    before = (root / "tracked.txt").read_bytes()
    with pytest.raises(ValueError):
        service.discard(profile, "root", snapshot, ["tracked.txt", "keep.txt"])
    assert (root / "tracked.txt").read_bytes() == before


def test_stash_conflict_blocks_discard_even_without_merge_marker(workspace):
    profile, root, service = workspace
    (root / "tracked.txt").write_text("stash version", encoding="utf-8")
    git(root, "stash", "push")
    commit(root, "tracked.txt", "committed version")
    git(root, "stash", "apply", check=False)
    assert not (root / ".git/MERGE_HEAD").exists()
    (root / "keep.txt").write_text("unrelated work", encoding="utf-8")
    snapshot = service.scan(profile, "root")
    assert snapshot.operation
    assert all(not row.discardable for row in snapshot.files)
    with pytest.raises(ValueError):
        service.discard(profile, "root", snapshot, ["keep.txt"])
    assert (root / "keep.txt").read_text() == "unrelated work"
