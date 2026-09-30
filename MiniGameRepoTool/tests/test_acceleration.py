import os
import shutil
import stat
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from repo_tool.acceleration import TEMP_PREFIX, Initializer, describe_plan
from repo_tool.git_ops import remote_identity
from repo_tool.models import InitOptions, ProfileDocument, RepoSpec, validate_profile
from repo_tool.profiles import ProfileStore, default_profile, parse_document
from repo_tool.service import RepoService
from test_git_service import commit, git, make_profile, remote  # noqa: F401  (fixture)

HAS_LFS = subprocess.run(["git", "lfs", "version"], capture_output=True).returncode == 0
needs_lfs = pytest.mark.skipif(not HAS_LFS, reason="Git LFS is not installed")


def service(tmp_path):
    return RepoService(tmp_path / "app")


def remove_tree(path):
    def writable(function, target, *_):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def accelerated(root, remote, sources, mode="auto", **options):
    profile = make_profile(root, remote)
    profile.init = InitOptions(mode, [str(s) for s in sources], **options)
    return profile


def temp_dirs(parent):
    return [p for p in Path(parent).iterdir() if p.name.startswith(TEMP_PREFIX)] if Path(parent).exists() else []


@pytest.fixture
def branched_remote(tmp_path):
    path = tmp_path / "remote"
    path.mkdir()
    git(path, "init", "-b", "main")
    commit(path, "file.txt", "one")
    git(path, "switch", "-c", "feature")
    commit(path, "feature.txt", "feature-only")
    git(path, "switch", "main")
    commit(path, "file.txt", "main-two")
    return path


@pytest.mark.parametrize(("left", "right", "same"), [
    ("git@host.example:group/Repo.git", "ssh://git@host.example/group/repo", True),
    ("git@Host.example:group/repo.git", "https://host.example/group/repo.git/", True),
    ("git@host.example:group/repo.git", "git@host.example:group/other.git", False),
    ("git@host.example:group/repo.git", "git@mirror.example:group/repo.git", False),
])
def test_remote_identity_merges_only_matching_host_and_path(left, right, same):
    assert (remote_identity(left) == remote_identity(right)) is same


def test_old_profiles_default_to_auto_and_new_fields_round_trip(tmp_path):
    profile = make_profile(tmp_path / "checkout", tmp_path / "remote")
    payload = asdict(ProfileDocument(1, profile.id, [profile]))
    payload["profiles"][0].pop("init")
    payload["profiles"][0]["repositories"][0].pop("init_source")
    loaded = parse_document(payload).profiles[0]
    assert loaded.init == InitOptions()
    assert loaded.init.mode == "auto"
    assert loaded.init.sources == []
    assert loaded.repo("root").init_source == ""
    assert default_profile().init.mode == "auto"

    loaded.init = InitOptions("specified", [str(tmp_path / "源 工程")], reuse_lfs=False, fallback=False)
    loaded.repo("root").init_source = str(tmp_path / "指定")
    store = ProfileStore(tmp_path / "app")
    store.save(ProfileDocument(1, loaded.id, [loaded]))
    exported = tmp_path / "export.json"
    store.export(store.load(), exported)
    again = store.read(exported).profiles[0]
    assert again.init == loaded.init
    assert again.repo("root").init_source == str(tmp_path / "指定")


@pytest.mark.parametrize(("change", "message"), [
    (lambda p: setattr(p.init, "mode", "copy"), "初始化方式"),
    (lambda p: setattr(p.init, "sources", ["relative/path"]), "绝对路径"),
    (lambda p: setattr(p.init, "sources", ["C:/A", "c:/a"]), "重复"),
    (lambda p: setattr(p.init, "reuse_lfs", "yes"), "布尔"),
    (lambda p: setattr(p.init, "mode", "specified"), "指定源工程"),
    (lambda p: setattr(p.repo("root"), "init_source", "relative"), "初始化来源"),
])
def test_invalid_init_options_are_rejected(tmp_path, change, message):
    profile = make_profile(tmp_path / "checkout", tmp_path / "remote")
    change(profile)
    with pytest.raises(ValueError, match=message):
        validate_profile(profile)


def test_git_objects_are_reused_across_branches_and_dissociated(tmp_path, branched_remote):
    source_root = tmp_path / "源 工程"
    git(tmp_path, "clone", "--branch", "feature", str(branched_remote), str(source_root))
    commit(source_root, "local.txt", "local-only-commit")
    (source_root / "feature.txt").write_text("uncommitted edit", encoding="utf-8")
    (source_root / "untracked.txt").write_text("untracked", encoding="utf-8")
    source_head = git(source_root, "rev-parse", "HEAD")
    root = tmp_path / "新 工程"
    profile = accelerated(root, branched_remote, [source_root])

    result = service(tmp_path).sync(profile, ["root"])["root"]

    assert result.outcome == "success", result.message
    assert result.details["git_source"] == str(source_root.resolve())
    assert result.details["commit"] == git(branched_remote, "rev-parse", "main")
    assert set(result.details["stages"]) >= {"检查来源", "复用 Git 数据", "检出", "验证完成"}
    assert git(root, "rev-parse", "HEAD") == git(branched_remote, "rev-parse", "main")
    assert git(root, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/main"
    assert (root / "file.txt").read_text(encoding="utf-8") == "main-two"
    assert not (root / "local.txt").exists() and not (root / "untracked.txt").exists()
    assert not (root / ".git" / "objects" / "info" / "alternates").exists()
    assert git(root, "status", "--porcelain") == ""
    # The source is only read.
    assert git(source_root, "rev-parse", "HEAD") == source_head
    assert (source_root / "feature.txt").read_text(encoding="utf-8") == "uncommitted edit"
    assert git(source_root, "symbolic-ref", "--short", "HEAD") == "feature"
    assert temp_dirs(tmp_path) == []

    remove_tree(source_root)
    git(root, "fsck", "--connectivity-only")
    assert git(root, "log", "--format=%s") == "main-two\none"


def test_invalid_sources_are_reported_and_network_clone_is_used(tmp_path, remote):
    projects = tmp_path / "projects"
    shallow = projects / "Shallow"
    git(tmp_path, "clone", "--depth", "1", remote.as_uri(), str(shallow))
    # The MiniGame\AssetRuntime case: a .git with only objects inside an enclosing repository.
    broken = projects / "Broken"
    git(tmp_path, "clone", str(remote), str(broken))
    nested = broken / "Child"
    (nested / ".git" / "objects").mkdir(parents=True)
    other = tmp_path / "other-remote"
    other.mkdir()
    git(other, "init", "-b", "main")
    commit(other, "other.txt", "other")
    mismatch = projects / "Mismatch"
    git(tmp_path, "clone", str(other), str(mismatch))
    root = tmp_path / "checkout"
    profile = accelerated(root, remote, [shallow, nested, mismatch, projects / "Missing"])

    plans = service(tmp_path).init_plan(profile, ["root"])
    text = "\n".join(describe_plan(profile, plans["root"]))
    assert "浅克隆" in text and "Git 向上识别到了" in text and "远端与配置不同" in text
    assert "目录不存在" in text and "Git 来源：无可用来源" in text

    result = service(tmp_path).sync(profile, ["root"])["root"]
    assert result.outcome == "success", result.message
    assert "没有可复用的本地数据" in result.message
    assert git(root, "rev-parse", "HEAD") == git(remote, "rev-parse", "HEAD")
    reasons = {Path(c["path"]).name: c["reason"] for c in result.details["candidates"]}
    assert "浅克隆" in reasons["Shallow"]


def test_no_source_with_stop_policy_leaves_target_untouched(tmp_path, remote):
    root = tmp_path / "checkout"
    profile = accelerated(root, remote, [tmp_path / "Missing"], fallback=False)
    result = service(tmp_path).sync(profile, ["root"])["root"]
    assert result.outcome == "blocked"
    assert "按配置停止" in result.message
    assert not root.exists()


def test_specified_mode_does_not_fall_through_to_other_projects(tmp_path, remote):
    good = tmp_path / "good"
    git(tmp_path, "clone", str(remote), str(good))
    root = tmp_path / "checkout"
    profile = accelerated(root, remote, [tmp_path / "missing", good], mode="specified", fallback=False)
    result = service(tmp_path).sync(profile, ["root"])["root"]
    assert result.outcome == "blocked"
    assert [Path(c["path"]).name for c in result.details["candidates"]] == ["missing"]

    profile.init.sources = [str(good)]
    profile.repo("root").init_source = str(tmp_path / "missing")
    result = service(tmp_path).sync(profile, ["root"])["root"]
    assert result.outcome == "blocked"
    assert [c["requested"] for c in result.details["candidates"]] == [True]

    profile.repo("root").init_source = str(good)
    result = service(tmp_path).sync(profile, ["root"])["root"]
    assert result.outcome == "success", result.message
    assert result.details["git_source"] == str(good.resolve())


def test_source_found_under_other_configured_path_by_remote_identity(tmp_path, remote):
    projects = tmp_path / "projects"
    git(tmp_path, "clone", str(remote), str(projects / "Elsewhere"))
    root = tmp_path / "checkout"
    profile = accelerated(root, remote, [projects])
    profile.repositories.append(RepoSpec("other", "Other", "Elsewhere", str(tmp_path / "x"), "game"))
    result = service(tmp_path).sync(profile, ["root"])["root"]
    assert result.outcome == "success", result.message
    assert result.details["git_source"] == str((projects / "Elsewhere").resolve())


def test_failed_remote_branch_is_reported_without_fallback_and_temp_is_removed(tmp_path, remote):
    source = tmp_path / "source"
    git(tmp_path, "clone", str(remote), str(source))
    root = tmp_path / "checkout"
    profile = accelerated(root, remote, [source])
    snap = service(tmp_path).inspector.inspect(profile, profile.repo("root"))
    snap.target_branch = "missing-branch"
    worker = service(tmp_path)
    outcome = Initializer(worker.git, lambda line: None, worker.cancel).clone(profile, profile.repo("root"), snap)
    assert outcome.outcome == "failed"
    assert outcome.details["failure"]["stage"] == "复用 Git 数据"
    assert temp_dirs(tmp_path) == []
    assert not root.exists()


def test_target_occupied_before_placement_is_not_overwritten(tmp_path, remote, monkeypatch):
    source = tmp_path / "source"
    git(tmp_path, "clone", str(remote), str(source))
    root = tmp_path / "checkout"
    profile = accelerated(root, remote, [source])
    original = Initializer._place

    def occupy(self, temp, target):
        target.mkdir()
        (target / "user.txt").write_text("keep", encoding="utf-8")
        return original(self, temp, target)

    monkeypatch.setattr(Initializer, "_place", occupy)
    result = service(tmp_path).sync(profile, ["root"])["root"]
    assert result.outcome == "blocked"
    assert "已被其他操作占用" in result.message
    assert (root / "user.txt").read_text(encoding="utf-8") == "keep"
    assert sorted(p.name for p in root.iterdir()) == ["user.txt"]
    assert temp_dirs(tmp_path) == []


def test_cancel_before_clone_keeps_no_partial_result(tmp_path, remote):
    source = tmp_path / "source"
    git(tmp_path, "clone", str(remote), str(source))
    root = tmp_path / "checkout"
    profile = accelerated(root, remote, [source])
    worker = service(tmp_path)
    snap = worker.inspector.inspect(profile, profile.repo("root"))
    worker.cancel.set()
    outcome = Initializer(worker.git, lambda line: None, worker.cancel).clone(profile, profile.repo("root"), snap)
    assert outcome.outcome == "cancelled"
    assert not root.exists() and temp_dirs(tmp_path) == []


def test_nested_child_is_initialized_even_when_parent_fails(tmp_path, remote):
    child_remote = tmp_path / "child-remote"
    child_remote.mkdir()
    git(child_remote, "init", "-b", "main")
    commit(child_remote, "child.txt", "child")
    source = tmp_path / "source"
    git(tmp_path, "clone", str(child_remote), str(source / "Child"))
    root = tmp_path / "checkout"
    profile = accelerated(root, tmp_path / "no-such-remote", [source])
    profile.repositories.append(RepoSpec("child", "Child", "Child", str(child_remote), "game"))
    results = service(tmp_path).sync(profile, ["root", "child"])
    assert results["root"].outcome == "failed"
    assert results["child"].outcome == "success", results["child"].message
    assert results["child"].details["git_source"] == str((source / "Child").resolve())


def make_lfs_remote(tmp_path):
    remote = tmp_path / "lfs-remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    git(remote, "lfs", "install", "--local")
    git(remote, "lfs", "track", "*.bin")
    (remote / "a.bin").write_bytes(os.urandom(4096))
    (remote / "b.bin").write_bytes(os.urandom(8192))
    (remote / "text.txt").write_text("plain", encoding="utf-8")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "lfs")
    return remote


def lfs_clone(tmp_path, remote, target, *options):
    # Git 2.45.1 rejects the LFS post-checkout hook during clone; check out afterwards.
    git(tmp_path, "clone", "--no-checkout", *options, str(remote), str(target))
    git(target, "reset", "--hard", "--quiet")


def lfs_oid(repo, name):
    return git(repo, "lfs", "ls-files", "--long", "--include", name).split()[0]


@needs_lfs
def test_lfs_objects_are_copied_and_verified_without_download(tmp_path):
    remote = make_lfs_remote(tmp_path)
    source = tmp_path / "source"
    lfs_clone(tmp_path, remote, source)
    expected = {name: (remote / name).read_bytes() for name in ("a.bin", "b.bin")}
    # Nothing can be downloaded any more: success proves the local objects were used.
    remove_tree(remote / ".git" / "lfs" / "objects")
    root = tmp_path / "checkout"
    profile = accelerated(root, remote, [source])

    result = service(tmp_path).sync(profile, ["root"])["root"]

    assert result.outcome == "success", result.message
    lfs = result.details["lfs"]
    assert (lfs["needed"], lfs["copied"], lfs["downloaded"], lfs["unfilled"]) == (2, 2, 0, 0)
    assert lfs["copied_bytes"] == 4096 + 8192
    for name, data in expected.items():
        assert (root / name).read_bytes() == data
    assert "本地复制 2" in result.message


@needs_lfs
def test_corrupt_local_lfs_object_is_rejected_and_downloaded(tmp_path):
    remote = make_lfs_remote(tmp_path)
    source = tmp_path / "source"
    lfs_clone(tmp_path, remote, source)
    full = lfs_oid(source, "a.bin")
    assert len(full) == 64
    cached = source / ".git" / "lfs" / "objects" / full[:2] / full[2:4] / full
    size = cached.stat().st_size
    # git-lfs hardlinks objects from a file remote; replace the link instead of writing through it.
    os.chmod(cached, stat.S_IWRITE)
    cached.unlink()
    cached.write_bytes(b"x" * size)
    root = tmp_path / "checkout"
    profile = accelerated(root, remote, [source], reuse_git=False)

    result = service(tmp_path).sync(profile, ["root"])["root"]

    assert result.outcome == "success", result.message
    lfs = result.details["lfs"]
    assert (lfs["copied"], lfs["rejected"], lfs["downloaded"]) == (1, 1, 1)
    assert result.details["git_source"] == ""
    assert (root / "a.bin").read_bytes() == (remote / "a.bin").read_bytes()
    assert cached.read_bytes() == b"x" * cached.stat().st_size  # the source cache is left as found


@needs_lfs
def test_shallow_source_still_provides_lfs_objects(tmp_path):
    remote = make_lfs_remote(tmp_path)
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "--no-checkout", "--depth", "1", remote.as_uri(), str(shallow))
    git(shallow, "lfs", "fetch", "origin", "main")
    remove_tree(remote / ".git" / "lfs" / "objects")
    root = tmp_path / "checkout"
    result = service(tmp_path).sync(accelerated(root, remote, [shallow]), ["root"])["root"]
    assert result.outcome == "success", result.message
    assert result.details["git_source"] == ""
    assert result.details["lfs"]["copied"] == 2
