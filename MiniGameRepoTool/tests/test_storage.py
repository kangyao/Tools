import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repo_tool.storage import RunLock, RunLockBusyError


def test_independent_projects_can_run_and_overlapping_roots_are_rejected(tmp_path):
    data = tmp_path / "app"
    root = tmp_path / "工程"
    with RunLock(data, root):
        with RunLock(data, tmp_path / "工程-copy"):
            pass
        for overlapping in (root, root / "Source", root.parent, root / "Source/.."):
            with pytest.raises(RunLockBusyError, match="工程目录正在被其他任务使用"):
                with RunLock(data, overlapping):
                    pytest.fail("overlapping project admitted")
        with pytest.raises(RunLockBusyError):
            with RunLock(data):
                pytest.fail("global operation admitted")
    with RunLock(data, root):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows path case normalization")
def test_project_aliases_share_the_same_lock(tmp_path):
    root = tmp_path / "Project"
    with RunLock(tmp_path / "app", root):
        with pytest.raises(RunLockBusyError):
            with RunLock(tmp_path / "app", str(root).swapcase()):
                pytest.fail("case alias admitted")


def test_project_lock_is_shared_across_processes_and_released_after_crash(tmp_path):
    data, root = tmp_path / "app", tmp_path / "工程"
    source = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import os, sys; sys.path.insert(0, sys.argv[1]); "
        "from repo_tool.storage import RunLock; "
        "lock = RunLock(sys.argv[2], sys.argv[3]); lock.__enter__(); "
        "print('ready', flush=True); sys.stdin.readline(); os._exit(0)"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(source), str(data), str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(RunLockBusyError):
            with RunLock(data, root / "Source"):
                pytest.fail("conflicting process admitted")
        with RunLock(data, tmp_path / "another-project"):
            pass
        process.communicate("exit\n", timeout=10)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    # Persistent registry/lock files must not be mistaken for an active process.
    with RunLock(data, root):
        pass


def test_corrupt_registry_does_not_disable_overlap_protection(tmp_path):
    registry = tmp_path / "project-locks/roots.json"
    registry.parent.mkdir()
    registry.write_text('{"roots": null}', encoding="utf-8")
    with pytest.raises(ValueError, match="无法读取工程任务锁目录"):
        with RunLock(tmp_path, tmp_path / "project"):
            pytest.fail("invalid registry ignored")
    registry.unlink()
    with RunLock(tmp_path, tmp_path / "project"):
        pass


def test_unexpected_os_lock_failure_is_not_reported_as_contention(tmp_path, monkeypatch):
    import repo_tool.storage as storage
    from unittest.mock import Mock

    if os.name == "nt":
        import msvcrt
        monkeypatch.setattr(msvcrt, "locking", Mock(side_effect=OSError(errno.EIO, "disk error")))
    else:
        import fcntl
        monkeypatch.setattr(fcntl, "flock", Mock(side_effect=OSError(errno.EIO, "disk error")))
    lock = storage._FileLock(tmp_path / "run.lock")
    with pytest.raises(OSError, match="disk error"):
        lock.__enter__()
    assert lock.stream is None
