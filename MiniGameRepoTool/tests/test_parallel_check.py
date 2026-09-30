import sys
import threading
import time

import pytest

from repo_tool.git_ops import Inspector
from repo_tool.models import Profile, RepoSpec, Snapshot
from repo_tool.service import RepoService
from test_git_service import commit, git


def profile_with_repos(tmp_path, count):
    return Profile("parallel", "Parallel", str(tmp_path / "repos"), {}, [
        RepoSpec(f"r{i}", f"Repo {i}", f"repo{i}", str(tmp_path / "remote"), branch="main")
        for i in range(count)
    ])


def test_checks_overlap_with_bounded_workers_and_keep_log_ownership(tmp_path, monkeypatch):
    profile = profile_with_repos(tmp_path, 12)
    lock = threading.Lock()
    first_batch = threading.Event()
    active = peak = 0
    runners = []
    events = []

    def inspect(inspector, profile, repo, remote):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            runners.append(inspector.git.runner)
            if active == 8:
                first_batch.set()
        assert first_batch.wait(3), "仓库状态没有并行读取"
        inspector.git.runner.output("log for " + repo.id)
        time.sleep(.01)
        with lock:
            active -= 1
        return Snapshot(repo.id, "up_to_date", "none", repo.id)

    monkeypatch.setattr(Inspector, "inspect", inspect)
    service = RepoService(tmp_path / "app", emit=lambda kind, value: events.append((kind, value)))
    result = service.check(profile, [r.id for r in profile.repositories], remote=False)
    assert peak == 8
    assert len({id(runner) for runner in runners}) == 12
    assert len(result) == 12
    assert all(snapshot.repo_id == key for key, snapshot in result.items())
    assert all(line == "log for " + key for kind, (key, line) in
               [(kind, value) for kind, value in events if kind == "log"])
    assert len([1 for kind, _ in events if kind == "snapshot"]) == 12
    progress = [value for kind, value in events if kind == "check_progress"]
    assert progress[-1]["completed"] == progress[-1]["total"] == 12


def test_stop_finishes_inflight_checks_without_dispatching_next_batch(tmp_path, monkeypatch):
    profile = profile_with_repos(tmp_path, 12)
    barrier = threading.Barrier(8, timeout=3)
    stop = threading.Event()
    checked = []

    def inspect(inspector, profile, repo, remote):
        checked.append(repo.id)
        barrier.wait()
        return Snapshot(repo.id, "up_to_date", "none", "done")

    def emit(kind, value):
        if kind == "snapshot":
            stop.set()

    monkeypatch.setattr(Inspector, "inspect", inspect)
    result = RepoService(tmp_path / "app", emit, stop=stop).check(
        profile, [repo.id for repo in profile.repositories], remote=False)
    assert len(result) == 8
    assert set(checked) == {f"r{i}" for i in range(8)}


def test_cancel_interrupts_all_active_check_processes(tmp_path, monkeypatch):
    profile = profile_with_repos(tmp_path, 2)
    cancel = threading.Event()
    armed = set()
    lock = threading.Lock()

    def emit(kind, value):
        if kind == "log" and value[1] == "ARMED":
            with lock:
                armed.add(value[0])
                if len(armed) == 2:
                    cancel.set()

    def inspect(inspector, profile, repo, remote):
        process = inspector.git.runner.run(
            sys.executable, ["-u", "-c", "import time; print('ARMED', flush=True); time.sleep(20)"],
            timeout=5)
        return Snapshot(repo.id, "cancelled" if process.cancelled else "error", "error", process.output)

    monkeypatch.setattr(Inspector, "inspect", inspect)
    result = RepoService(tmp_path / "app", emit, cancel=cancel).check(profile, ["r0", "r1"], remote=False)
    assert armed == {"r0", "r1"}
    assert all(snapshot.status == "cancelled" for snapshot in result.values())


def test_one_failed_check_does_not_drop_other_repositories(tmp_path, monkeypatch):
    profile = profile_with_repos(tmp_path, 3)
    def inspect(inspector, profile, repo, remote):
        if repo.id == "r1":
            raise OSError("cannot read repository")
        return Snapshot(repo.id, "up_to_date", "none", repo.id)
    monkeypatch.setattr(Inspector, "inspect", inspect)
    result = RepoService(tmp_path / "app").check(profile, ["r0", "r1", "r2"], remote=False)
    assert result["r1"].status == "error"
    assert result["r0"].status == result["r2"].status == "up_to_date"


def test_worktrees_sharing_git_data_do_not_fetch_together(tmp_path, monkeypatch):
    root, linked = tmp_path / "main", tmp_path / "linked"
    root.mkdir()
    git(root, "init", "-b", "main")
    commit(root)
    git(root, "worktree", "add", "-b", "feature", str(linked))
    profile = Profile("p", "Worktrees", str(tmp_path), {}, [
        RepoSpec("main", "Main", "main", str(tmp_path / "remote"), branch="main"),
        RepoSpec("linked", "Linked", "linked", str(tmp_path / "remote"), branch="feature"),
    ])
    active = peak = 0
    lock = threading.Lock()
    def inspect(inspector, profile, repo, remote):
        nonlocal active, peak
        assert remote
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.3)
        with lock:
            active -= 1
        return Snapshot(repo.id, "up_to_date", "none", "done")
    monkeypatch.setattr(Inspector, "inspect", inspect)
    result = RepoService(tmp_path / "app").check(profile, ["main", "linked"], remote=True)
    assert len(result) == 2
    assert peak == 1


@pytest.mark.parametrize("flag", ["stop", "cancel"])
def test_check_stopped_before_start_does_no_work(tmp_path, monkeypatch, flag):
    profile = profile_with_repos(tmp_path, 2)
    event = threading.Event()
    event.set()
    def inspect(*args):
        pytest.fail("已停止的检查不应再读取仓库")
    monkeypatch.setattr(Inspector, "inspect", inspect)
    result = RepoService(tmp_path / "app", **{flag: event}).check(profile, ["r0", "r1"], remote=False)
    assert result == {}
