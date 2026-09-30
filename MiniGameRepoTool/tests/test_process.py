import sys
import threading
import time

from repo_tool.process import ProcessRunner, redact
from repo_tool.storage import RunLock


def test_live_output_and_real_exit_code(tmp_path):
    lines = []
    runner = ProcessRunner(output=lines.append)
    result = runner.run(sys.executable, ["-u", "-c", "print('开始'); print('done'); raise SystemExit(7)"], tmp_path)
    assert result.returncode == 7
    assert "done" in result.output
    assert any("开始" in line for line in lines)


def test_timeout_and_cancel(tmp_path):
    runner = ProcessRunner()
    result = runner.run(sys.executable, ["-c", "import time; time.sleep(20)"], tmp_path, timeout=0.2)
    assert result.timed_out
    assert result.duration < 8
    cancel = threading.Event()
    cancel.set()
    runner = ProcessRunner(cancel=cancel)
    result = runner.run(sys.executable, ["-c", "print('should not start')"], tmp_path)
    assert result.cancelled
    assert "should not start" not in result.output


def test_credentials_are_redacted():
    assert "secret" not in redact("https://alice:secret@example.com/repo")
    assert "token=abc" not in redact("https://example.com/repo?token=abc")


def test_lock_is_released_and_second_writer_is_rejected(tmp_path):
    with RunLock(tmp_path):
        try:
            with RunLock(tmp_path):
                raise AssertionError("second writer was admitted")
        except RuntimeError as error:
            assert "正在" in str(error)
    with RunLock(tmp_path):
        pass


def test_run_journal_redacts_nested_snapshot_fields(tmp_path):
    from repo_tool.models import RepoResult, Snapshot
    from repo_tool.storage import RunJournal
    origin = "https://alice:private-value@example.invalid/repo"
    snapshot = Snapshot("root", "remote_mismatch", "block", "Mismatch",
                        origin=origin, changes=["https://token-value@example.invalid/r"])
    journal = RunJournal(tmp_path, "p", "sync")
    journal.result(RepoResult("root", "blocked", "Mismatch", snapshot))
    journal.finish()
    content = journal.path.read_text(encoding="utf-8")
    assert "private-value" not in content
    assert "token-value" not in content
    assert snapshot.origin == origin


def test_process_does_not_inherit_git_repository_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "unrelated-repository")
    monkeypatch.setenv("GIT_WORK_TREE", "unrelated-worktree")
    result = ProcessRunner().run(sys.executable, ["-c",
        "import os; print(os.environ.get('GIT_DIR')); print(os.environ.get('GIT_WORK_TREE'))"], tmp_path)
    assert result.output.splitlines() == ["None", "None"]


def test_per_process_environment_and_cancel_callback(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILD_TEST_VALUE", "original")
    cancel = threading.Event()
    callbacks = []
    def output(line):
        if line == "changed":
            cancel.set()
    result = ProcessRunner(output, cancel).run(sys.executable, ["-u", "-c",
        "import os, time; print(os.environ['BUILD_TEST_VALUE'], flush=True); time.sleep(20)"], tmp_path,
        environment={"BUILD_TEST_VALUE": "changed"}, on_terminate=lambda: callbacks.append("stopped"))
    assert result.cancelled
    assert callbacks == ["stopped"]
    import os
    assert os.environ["BUILD_TEST_VALUE"] == "original"


def test_cancel_stops_running_process_and_its_child(tmp_path):
    cancel = threading.Event()
    child_code = ("import time; from pathlib import Path; print('ARMED', flush=True); "
                  "time.sleep(2); Path('child-survived').write_text('bad')")
    parent_code = ("import subprocess, sys; "
                   f"subprocess.Popen([sys.executable, '-u', '-c', {child_code!r}]).wait()")
    runner = ProcessRunner(output=lambda line: cancel.set() if line == "ARMED" else None, cancel=cancel)
    result = runner.run(sys.executable, ["-u", "-c", parent_code], tmp_path, timeout=10)
    assert result.cancelled and not result.timed_out
    time.sleep(2.2)
    assert not (tmp_path / "child-survived").exists()
