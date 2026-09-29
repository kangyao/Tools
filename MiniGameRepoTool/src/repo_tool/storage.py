from __future__ import annotations

import os
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

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


class RunLock:
    def __init__(self, directory: Path):
        self.path = Path(directory) / "run.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            self.stream = None
            raise RuntimeError("另一个工具实例正在执行任务，请等待它完成") from error
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


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
