from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .acceleration import InitFailure, Initializer, InitPlan, SourceScanner
from .checks import ParallelChecks
from .force_update import ForceUpdate
from .git_ops import GitClient, Inspector
from .models import Profile, RepoResult, Snapshot, canonical, validate_profile
from .profiles import ProfileStore
from .process import ProcessResult, ProcessRunner, redact
from .setup import SetupRunner
from .storage import RunJournal, RunLock

# Checkout leaves LFS pointers; one batched `git lfs pull` then downloads them concurrently.
SKIP_SMUDGE = {"GIT_LFS_SKIP_SMUDGE": "1"}


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
        self.journal_lock = threading.Lock()
        self.lfs_available: bool | None = None

    def _log(self, line: str):
        line = redact(line)
        self._journal_log(self.current, line)
        self.emit("log", (self.current, line))

    def _journal_log(self, repo_id: str, line: str) -> None:
        journal = self.journal
        if journal:
            with self.journal_lock:
                journal.log(repo_id, line)

    def _prefetch(self, profile: Profile, selected: list[str]) -> dict[str, Snapshot]:
        """Fetch every selected repository concurrently before any worktree changes."""
        def emit(kind, payload):
            if kind == "log":
                self._journal_log(*payload)
            self.emit(kind, payload)
        return ParallelChecks(emit, self.stop, self.cancel).run(profile, selected, True)

    def _snapshot_for_sync(self, profile: Profile, repo_id: str, prefetched: Snapshot | None) -> Snapshot:
        if prefetched is not None and prefetched.action == "error":
            # A failed fetch must not fall back to stale remote-tracking refs.
            self.current = repo_id
            return prefetched
        if prefetched is not None and prefetched.runnable:
            # Refs are fresh; re-read local state since earlier repositories may have changed disk.
            snap = self._inspect(profile, repo_id, False)
            if snap.action != "check":
                snap.remote_checked = snap.remote_checked or prefetched.remote_checked
                return snap
        return self._inspect(profile, repo_id, True)

    def _with_managed_sources(self, profile: Profile) -> Profile:
        """In auto mode, also borrow from every other project this tool manages, after explicit sources."""
        if profile.init.mode != "auto":
            return profile
        store = ProfileStore(self.data_dir)
        if not store.path.is_file():
            return profile
        try:
            others = [p.root for p in store.read(store.path).profiles if p.id != profile.id]
        except ValueError:
            return profile
        seen = {canonical(Path(profile.root))}
        sources = []
        for source in [*profile.init.sources, *others]:
            key = canonical(Path(source))
            if key in seen or not Path(source).is_dir():
                continue
            seen.add(key)
            sources.append(source)
        added = [s for s in sources if s not in profile.init.sources]
        if not added:
            return profile
        self._log("自动复用仓库管理中的其他工程：" + "、".join(added))
        return replace(profile, init=replace(profile.init, sources=sources))

    def _has_lfs(self) -> bool:
        if self.lfs_available is None:
            self.lfs_available = self.git.run(["lfs", "version"]).returncode == 0
        return self.lfs_available

    def _pull_lfs(self, path: Path) -> tuple[ProcessResult | None, int]:
        """Batch-download LFS objects for HEAD; return a failed step or the unfilled count."""
        self._log("批量下载并检出 LFS 文件")
        result = self.git.run(["lfs", "pull"], path, network=True, stream=True)
        if result.returncode:
            return result, 0
        listing = self.git.run(["lfs", "ls-files", "--json"], path, unbounded=True)
        if listing.returncode:
            return listing, 0
        try:
            files = Initializer._lfs_files(listing.output)
        except InitFailure as failure:
            listing.returncode, listing.output = 1, failure.message
            return listing, 0
        return None, sum(1 for item in files if not item.get("checkout"))

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

    def init_plan(self, profile: Profile, selected: list[str]) -> dict[str, InitPlan]:
        """Read-only: local repository state plus usable sources. Nothing is fetched."""
        validate_profile(profile)
        if set(selected) - {repo.id for repo in profile.repositories}:
            raise ValueError("所选仓库不存在")
        plans: dict[str, InitPlan] = {}
        with RunLock(self.data_dir):
            scanner = SourceScanner(self.git)
            sourced = self._with_managed_sources(profile)
            for repo in profile.repositories:
                if repo.id not in selected:
                    continue
                if self.stop.is_set() or self.cancel.is_set():
                    break
                self.emit("started", repo.id)
                snap = self._inspect(profile, repo.id, False)
                plans[repo.id] = scanner.plan(sourced, repo, snap)
                self.emit("init_plan", plans[repo.id])
        return plans

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
        details, note = {}, ""
        lfs = self._has_lfs()
        skip_smudge = SKIP_SMUDGE if lfs else None
        lfs_failure: ProcessResult | None = None
        unfilled = 0
        if snap.action == "clone":
            # Git rejects a destination that acquired content after inspection.
            path.parent.mkdir(parents=True, exist_ok=True)
            if profile.init.mode != "network":
                init = Initializer(self.git, self._log, self.cancel).clone(self._with_managed_sources(profile), repo, snap)
                details = init.details
                if init.outcome in {"failed", "blocked", "cancelled"}:
                    return RepoResult(repo_id, init.outcome, init.message, snap, details)
                if init.outcome == "done":
                    after = self._inspect(profile, repo_id, False)
                    unfilled = details.get("lfs", {}).get("unfilled", 0)
                    if after.ready and not unfilled:
                        return RepoResult(repo_id, "success", init.message + "。" + after.message, after, details)
                    reason = f"仍有 {unfilled} 个 LFS 文件未填充" if unfilled else after.message
                    return RepoResult(repo_id, "blocked", init.message + "。完成前需要处理：" + reason,
                                      after, details)
                note = init.message
            command = ["clone", "--progress", "--single-branch", "--branch", snap.target_branch,
                       "--", repo.remote, str(path)]
            result = self.git.run(command, network=True, stream=True, environment=skip_smudge)
            if result.returncode == 0 and lfs:
                lfs_failure, unfilled = self._pull_lfs(path)
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
            result = self.git.run(args, path, stream=True, environment=skip_smudge)
            if result.returncode == 0:
                result = self.git.run(["merge", "--ff-only", "--no-overwrite-ignore", snap.remote_oid],
                                      path, stream=True, environment=skip_smudge)
            if result.returncode == 0:
                result = self.git.run(["branch", "--set-upstream-to=origin/" + snap.target_branch,
                                       snap.target_branch], path, stream=True)
            if result.returncode == 0 and lfs:
                lfs_failure, unfilled = self._pull_lfs(path)
        if lfs_failure is not None:
            note = (note + "。" if note else "") + "Git 数据已就位，LFS 下载失败，可在该仓库执行 git lfs pull 补齐"
            result = lfs_failure
        if result.returncode:
            message = "操作已中断，请重新检查" if result.cancelled else redact(result.output[-2000:])
            return RepoResult(repo_id, "cancelled" if result.cancelled else "failed",
                              (note + "。" if note else "") + (message or f"Git 退出码 {result.returncode}"),
                              snap, details)
        after = self._inspect(profile, repo_id, False)
        message = after.message if after.ready else "操作后需要重新处理：" + after.message
        if unfilled:
            message += f"；仍有 {unfilled} 个 LFS 文件未填充，请在该仓库执行 git lfs pull"
        return RepoResult(repo_id, "success" if after.ready and not unfilled else "blocked",
                          (note + "。" if note else "") + message, after, details)

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
                # Network is the bottleneck, so fetch everything first; worktrees still change one at a time.
                prefetched = self._prefetch(profile, selected)
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
                        snap = self._snapshot_for_sync(profile, key, prefetched.get(key))
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
