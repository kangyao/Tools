from __future__ import annotations

import errno
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import canonical
from .process import redact
from .profiles import atomic_json


def sanitized(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: sanitized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return value


class RunLockBusyError(RuntimeError):
    """Another task holds a conflicting run lock."""


class _FileLock:
    def __init__(self, path: Path, *, wait: float = 0):
        self.path = path
        self.wait = wait
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            deadline = time.monotonic() + self.wait
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise RunLockBusyError(
                            "另一个工具实例正在执行任务，请等待它完成，或在该窗口停止任务后重试。"
                        ) from error
                    time.sleep(0.01)
        except BaseException:
            self.stream.close()
            self.stream = None
            raise
        return self

    def __exit__(self, *args):
        if self.stream:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None


class RunLock:
    """Serialize overlapping project roots while admitting independent projects.

    The legacy run.lock is held only while checking/registering project locks.
    Without a root it remains an exclusive lock for the whole data directory.
    OS locks, rather than registry entries, determine whether a task is active.
    """

    def __init__(self, directory: Path, root: str | Path | None = None):
        self.directory = Path(directory)
        self.root = canonical(Path(root)) if root is not None else None
        self.lock = None

    def _project_lock(self, root: str) -> _FileLock:
        key = hashlib.sha256(root.encode("utf-8")).hexdigest()
        return _FileLock(self.directory / "project-locks" / (key + ".lock"))

    def __enter__(self):
        guard = _FileLock(self.directory / "run.lock", wait=1)
        guard.__enter__()
        try:
            registry = self.directory / "project-locks" / "roots.json"
            roots = []
            if registry.exists():
                try:
                    roots = json.loads(registry.read_text(encoding="utf-8"))["roots"]
                    if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
                        raise ValueError("invalid project roots")
                except (ValueError, KeyError, TypeError) as error:
                    raise ValueError(f"无法读取工程任务锁目录：{registry}") from error
            for other in roots:
                if (self.root is None or self.root == other or Path(self.root) in Path(other).parents
                        or Path(other) in Path(self.root).parents):
                    try:
                        with self._project_lock(other):
                            pass
                    except RunLockBusyError as error:
                        raise RunLockBusyError(
                            f"工程目录正在被其他任务使用：{other}。请等待任务完成，或在该窗口停止任务后重试。"
                        ) from error
            if self.root is None:
                self.lock = guard
            else:
                project_lock = self._project_lock(self.root)
                project_lock.__enter__()
                try:
                    if self.root not in roots:
                        atomic_json(registry, {"roots": [*roots, self.root]})
                except BaseException:
                    project_lock.__exit__()
                    raise
                self.lock = project_lock
            return self
        finally:
            if self.lock is not guard:
                guard.__exit__()

    def __exit__(self, *args):
        if self.lock:
            try:
                self.lock.__exit__(*args)
            finally:
                self.lock = None


class RunJournal:
    def __init__(self, directory: Path, profile_id: str, operation: str):
        key = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.directory = Path(directory) / "runs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / (key + ".json")
        self.log_path = self.directory / (key + ".log")
        self.data = {"profile_id": profile_id, "operation": operation, "status": "running",
                     "started_at": datetime.now().astimezone().isoformat(), "results": {}}
        atomic_json(self.path, self.data)

    def log(self, repo_id: str, line: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now():%H:%M:%S} [{repo_id}] {redact(line)}\n")

    def result(self, result) -> None:
        self.data["results"][result.repo_id] = sanitized(asdict(result))
        atomic_json(self.path, self.data)

    def finish(self, status: str = "finished") -> None:
        self.data["status"] = status
        self.data["finished_at"] = datetime.now().astimezone().isoformat()
        atomic_json(self.path, self.data)
