from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .git_ops import GitClient, Inspector
from .models import Profile, Snapshot, canonical
from .process import ProcessRunner, redact


class ParallelChecks:
    """Read independent repositories concurrently; publish each completed snapshot."""

    MAX_WORKERS = 8

    def __init__(self, emit, stop: threading.Event, cancel: threading.Event):
        self.emit = emit
        self.stop = stop
        self.cancel = cancel
        self.common_locks: dict[str, threading.Lock] = {}
        self.locks_guard = threading.Lock()

    def _read(self, profile: Profile, repo_id: str, remote: bool) -> Snapshot:
        repo = profile.repo(repo_id)
        # Each reader owns its runner and log callback. Never use RepoService.current.
        client = GitClient(ProcessRunner(
            lambda line: self.emit("log", (repo_id, redact(line))), self.cancel))
        inspector = Inspector(client)
        common_lock = None
        acquired = False
        try:
            path = profile.directory(repo)
            if remote and (path / ".git").exists():
                common = client.run(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
                if common.returncode:
                    raise ValueError("无法读取 Git 共享目录：" + redact(common.output.strip()))
                key = canonical(Path(common.output.strip()))
                with self.locks_guard:
                    common_lock = self.common_locks.setdefault(key, threading.Lock())
                # Linked worktrees share refs and FETCH_HEAD. Serialize their remote checks.
                while not self.cancel.is_set():
                    if common_lock.acquire(timeout=.05):
                        acquired = True
                        break
            if self.cancel.is_set():
                return Snapshot(repo_id, "cancelled", "error", "检查已中断",
                                target_branch=profile.target(repo))
            return inspector.inspect(profile, repo, remote)
        except (OSError, ValueError) as error:
            return Snapshot(repo_id, "cancelled" if self.cancel.is_set() else "error", "error",
                            redact(str(error)), target_branch=profile.target(repo))
        finally:
            if acquired:
                common_lock.release()

    def run(self, profile: Profile, selected: list[str], remote: bool) -> dict[str, Snapshot]:
        chosen = set(selected)
        ordered = [repo.id for repo in profile.repositories if repo.id in chosen]
        if not ordered or self.stop.is_set() or self.cancel.is_set():
            return {}
        remaining = iter(ordered)
        results = {}
        workers = min(self.MAX_WORKERS, len(ordered))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repo-check") as pool:
            pending = {}

            def fill():
                while len(pending) < workers and not (self.stop.is_set() or self.cancel.is_set()):
                    key = next(remaining, None)
                    if key is None:
                        break
                    self.emit("started", key)
                    pending[pool.submit(self._read, profile, key, remote)] = key

            def progress():
                self.emit("check_progress", {"completed": len(results), "total": len(ordered),
                                              "active": len(pending)})

            try:
                fill()
                progress()
                while pending:
                    done, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                    for future in list(pending):
                        if future in done:
                            key = pending.pop(future)
                            snapshot = future.result()
                            results[key] = snapshot
                            self.emit("snapshot", snapshot)
                    fill()
                    progress()
            except BaseException:
                self.cancel.set()
                for future in pending:
                    future.cancel()
                raise
        return {key: results[key] for key in ordered if key in results}
