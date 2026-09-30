from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from .checks import ParallelChecks
from .force_update import ForceUpdate
from .git_ops import GitClient, Inspector
from .models import Profile, RepoResult, Snapshot, validate_profile
from .process import ProcessRunner, redact
from .setup import SetupRunner
from .storage import RunJournal, RunLock


class RepoService:
    def __init__(self, data_dir: Path, emit: Callable | None = None,
                 stop: threading.Event | None = None, cancel: threading.Event | None = None):
        self.data_dir = Path(data_dir)
        self.emit = emit or (lambda kind, payload: None)
        self.stop = stop or threading.Event()
        self.cancel = cancel or threading.Event()
        self.current = ""
        self.journal: RunJournal | None = None
        self.runner = ProcessRunner(self._log, self.cancel)
        self.git = GitClient(self.runner)
        self.inspector = Inspector(self.git)

    def _log(self, line: str):
        line = redact(line)
        if self.journal:
            self.journal.log(self.current, line)
        self.emit("log", (self.current, line))

    def _inspect(self, profile: Profile, repo_id: str, remote: bool) -> Snapshot:
        self.current = repo_id
        try:
            snap = self.inspector.inspect(profile, profile.repo(repo_id), remote)
        except (OSError, ValueError) as error:
            snap = Snapshot(repo_id, "error", "error", str(error), target_branch=profile.target(profile.repo(repo_id)))
        self.emit("snapshot", snap)
        return snap

    def _record(self, result: RepoResult) -> None:
        self.current = result.repo_id
        self._log(result.message)
        if self.journal:
            self.journal.result(result)
        self.emit("result", result)

    def check(self, profile: Profile, selected: list[str], remote: bool = True) -> dict[str, Snapshot]:
        validate_profile(profile)
        if set(selected) - {repo.id for repo in profile.repositories}:
            raise ValueError("所选仓库不存在")
        with RunLock(self.data_dir):
            return ParallelChecks(self.emit, self.stop, self.cancel).run(profile, selected, remote)

    def branch_list(self, remote: str) -> list[str]:
        with RunLock(self.data_dir):
            return self.git.branches(remote)

    def _execute(self, profile: Profile, repo_id: str, snap: Snapshot, *, allow_force: bool = False) -> RepoResult:
        repo = profile.repo(repo_id)
        path = profile.directory(repo)
        if not snap.runnable:
            outcome = "blocked" if snap.action == "block" else "failed"
            if snap.status == "cancelled":
                outcome = "cancelled"
            return RepoResult(repo_id, outcome, snap.message, snap)
        if snap.action == "none":
            return RepoResult(repo_id, "success", snap.message, snap)
        if snap.action == "clone":
            # Git rejects a destination that acquired content after inspection.
            path.parent.mkdir(parents=True, exist_ok=True)
            command = ["clone", "--progress", "--single-branch", "--branch", snap.target_branch,
                       "--", repo.remote, str(path)]
            result = self.git.run(command, network=True, stream=True)
        else:
            # Recheck after fetch, immediately before changing the worktree.
            fresh = self._inspect(profile, repo_id, False)
            if not fresh.runnable:
                return RepoResult(repo_id, "blocked", fresh.message, fresh)
            if fresh.action == "force_update":
                if not allow_force:
                    return RepoResult(repo_id, "blocked", "该仓库未被选择，不执行强制更新", fresh)
                self._log("按仓库配置执行强制更新：丢弃未提交修改，不备份，保留本地提交")
                try:
                    count = ForceUpdate(self.data_dir, self.git).discard(profile, repo_id)
                except (OSError, ValueError) as error:
                    return RepoResult(repo_id, "cancelled" if self.cancel.is_set() else "blocked",
                                      "强制更新未完成，不生成备份；请重新检查：" + redact(str(error)), fresh)
                self._log(f"已丢弃 {count} 项未提交修改，未生成备份")
                fresh = self._inspect(profile, repo_id, False)
                if fresh.changes or not fresh.runnable:
                    return RepoResult(repo_id, "blocked", "丢弃后状态仍需处理：" + fresh.message, fresh)
                if fresh.action == "none":
                    return RepoResult(repo_id, "success", fresh.message, fresh)
            # A single-branch clone does not yet map other origin branches.
            # Register this exact mapping before asking Git to set up tracking.
            mapping = "+refs/heads/" + snap.target_branch + ":refs/remotes/origin/" + snap.target_branch
            mappings = self.git.run(["config", "--get-all", "remote.origin.fetch"], path).output.splitlines()
            if mapping not in mappings and "+refs/heads/*:refs/remotes/origin/*" not in mappings:
                configured = self.git.run(["config", "--add", "remote.origin.fetch", mapping], path)
                if configured.returncode:
                    return RepoResult(repo_id, "failed", configured.output, fresh)
            exists = self.git.run(["show-ref", "--verify", "--quiet", "refs/heads/" + snap.target_branch], path)
            args = (["switch", "--no-overwrite-ignore", snap.target_branch] if not exists.returncode else
                    ["switch", "--no-overwrite-ignore", "--track", "-c", snap.target_branch,
                     "origin/" + snap.target_branch])
            result = self.git.run(args, path, stream=True)
            if result.returncode == 0:
                result = self.git.run(["merge", "--ff-only", "--no-overwrite-ignore", snap.remote_oid],
                                      path, stream=True)
            if result.returncode == 0:
                result = self.git.run(["branch", "--set-upstream-to=origin/" + snap.target_branch,
                                       snap.target_branch], path, stream=True)
        if result.returncode:
            message = "操作已中断，请重新检查" if result.cancelled else redact(result.output[-2000:])
            return RepoResult(repo_id, "cancelled" if result.cancelled else "failed",
                              message or f"Git 退出码 {result.returncode}", snap)
        after = self._inspect(profile, repo_id, False)
        return RepoResult(repo_id, "success" if after.ready else "blocked",
                          after.message if after.ready else "操作后需要重新处理：" + after.message, after)

    def sync(self, profile: Profile, selected: list[str]) -> dict[str, RepoResult]:
        validate_profile(profile)
        unknown = set(selected) - {r.id for r in profile.repositories}
        if unknown:
            raise ValueError("所选仓库不存在")
        with RunLock(self.data_dir):
            self.journal = RunJournal(self.data_dir, profile.id, "sync")
            self.emit("journal", str(self.journal.log_path))
            results: dict[str, RepoResult] = {}
            try:
                chosen = set(selected)
                # Folder nesting is for display and path protection, never a prerequisite.
                for repo in profile.repositories:
                    key = repo.id
                    if key not in chosen:
                        continue
                    if self.stop.is_set() or self.cancel.is_set():
                        result = RepoResult(key, "cancelled", "队列已停止，未执行此仓库")
                    else:
                        self.current = key
                        self.emit("started", key)
                        snap = self._inspect(profile, key, True)
                        result = self._execute(profile, key, snap, allow_force=key in selected)
                    results[key] = result
                    self._record(result)
                if profile.setup.auto_run and not self.stop.is_set() and not self.cancel.is_set():
                    setup = self._prepare(profile, results)
                    results[setup.repo_id] = setup
                    self._record(setup)
                self.journal.finish()
            except BaseException:
                self.journal.finish("interrupted")
                raise
            finally:
                self.journal = None
            return results

    def _prepare(self, profile: Profile, previous: dict[str, RepoResult] | None = None) -> RepoResult:
        required = profile.setup.required_repositories or [r.id for r in profile.repositories if r.enabled]
        for repo_id in required:
            if previous and repo_id in previous and previous[repo_id].outcome != "success":
                return RepoResult("__setup__", "blocked", "必需仓库未同步完成：" + profile.repo(repo_id).name)
            if self.cancel.is_set() or self.stop.is_set():
                return RepoResult("__setup__", "cancelled", "环境准备已取消")
            snap = self._inspect(profile, repo_id, True)
            if not snap.ready:
                return RepoResult("__setup__", "blocked",
                                  "必需仓库未就绪：" + profile.repo(repo_id).name + "，" + snap.message)
        self.current = "__setup__"
        self.emit("started", "__setup__")
        self._log("开始准备构建环境")
        return SetupRunner(self.runner).prepare(profile)

    def setup(self, profile: Profile) -> dict[str, RepoResult]:
        validate_profile(profile)
        with RunLock(self.data_dir):
            self.journal = RunJournal(self.data_dir, profile.id, "setup")
            self.emit("journal", str(self.journal.log_path))
            try:
                result = self._prepare(profile)
                self._record(result)
                self.journal.finish()
                return {result.repo_id: result}
            except BaseException:
                self.journal.finish("interrupted")
                raise
            finally:
                self.journal = None
