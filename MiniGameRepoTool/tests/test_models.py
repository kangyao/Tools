import json
from dataclasses import replace

import pytest

from repo_tool.models import Profile, RepoSpec, SetupOptions, validate_profile, dependency_order
from repo_tool.profiles import ProfileStore, default_profile


def profile(tmp_path):
    return Profile("p", "测试", str(tmp_path), {"game": "main"}, [
        RepoSpec("root", "Root", ".", "https://example.invalid/root.git", "game"),
        RepoSpec("child", "Child", "AssetRuntime", "https://example.invalid/child.git", "game"),
    ])


def test_empty_child_order_and_default_profiles(tmp_path):
    p = profile(tmp_path)
    validate_profile(p)
    assert dependency_order(p) == ["root", "child"]
    assert len(default_profile().repositories) == 8
    assert default_profile().repositories[-1].path == "Source/Editor"


@pytest.mark.parametrize("bad_path", ["../escape", "C:/elsewhere", ".git/data"])
def test_rejects_paths_outside_workspace(tmp_path, bad_path):
    p = profile(tmp_path)
    p.repositories[1].path = bad_path
    with pytest.raises(ValueError):
        validate_profile(p)


def test_rejects_duplicate_paths_cycle_unknown_group_and_credentials(tmp_path):
    p = profile(tmp_path)
    p.repositories[0].depends_on = ["child"]
    with pytest.raises(ValueError, match="排序参考.*循环"):
        validate_profile(p)
    p.repositories[0].depends_on = []
    p.repositories[1].path = "."
    with pytest.raises(ValueError, match="重复"):
        validate_profile(p)
    p.repositories[1].path = "child"
    p.repositories[1].branch_group = "unknown"
    with pytest.raises(ValueError):
        validate_profile(p)
    p.repositories[1].branch_group = "game"
    p.repositories[1].remote = "https://user:secret@example.invalid/r.git"
    with pytest.raises(ValueError, match="凭据"):
        validate_profile(p)


def test_atomic_store_backup_and_corruption_does_not_overwrite(tmp_path):
    store = ProfileStore(tmp_path / "config")
    document = store.load()
    document.profiles = [profile(tmp_path / "checkout")]
    document.active_profile_id = "p"
    store.save(document)
    assert store.load().profiles[0].name == "测试"
    assert store.path.with_suffix(".json.bak").exists()
    store.path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ValueError):
        store.load()
    assert store.path.read_text(encoding="utf-8") == "{ broken"


def test_rejects_http_userinfo_and_nonboolean_setup(tmp_path):
    p = profile(tmp_path)
    p.repositories[0].remote = "https://private-token@example.invalid/repo"
    with pytest.raises(ValueError, match="凭据"):
        validate_profile(p)
    p.repositories[0].remote = "git@example.invalid:repo.git"
    p.setup.auto_run = "false"
    with pytest.raises(ValueError, match="环境准备"):
        validate_profile(p)
