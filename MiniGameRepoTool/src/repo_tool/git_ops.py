from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .models import Profile, RepoSpec, Snapshot, canonical
from .process import ProcessResult, ProcessRunner, redact


def normalized_remote(remote: str) -> str:
    remote = remote.rstrip("/")
    if remote.startswith("file://"):
        path = unquote(urlsplit(remote).path)
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        return canonical(Path(path))
    if Path(remote).is_absolute():
        return canonical(Path(remote))
    match = re.fullmatch(r"([^/@:]+@)?([^/:]+):(.+)", remote)
    if match and not re.match(r"^[A-Za-z]:", remote):
        return "ssh://" + (match[1] or "") + match[2].lower() + "/" + match[3].lstrip("/")
    return remote


class GitClient:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner
        self.program = shutil.which("git") or "git"

    def run(self, args: list[str], cwd: Path | None = None, network: bool = False,
            stream: bool = False) -> ProcessResult:
        return self.runner.run(self.program, ["-c", "core.quotepath=false", *args], cwd,
                               timeout=None if network else 60, stream=stream)

    def branches(self, remote: str) -> list[str]:
        result = self.run(["ls-remote", "--heads", remote], network=True)
        if result.returncode:
            raise RuntimeError(redact(result.output.strip()))
        return sorted(line.split("\trefs/heads/", 1)[1] for line in result.output.splitlines()
                      if "\trefs/heads/" in line)


def remote_failure(result: ProcessResult) -> tuple[str, str]:
    if result.cancelled:
        return "cancelled", "操作已中断，请重新检查仓库状态"
    if result.timed_out:
        return "remote_error", "远端检查超时"
    text = result.output.strip()
    lower = text.lower()
    if any(x in lower for x in ("authentication failed", "permission denied", "access denied",
                                "could not read username", "terminal prompts disabled")):
        return "auth_error", "认证未通过，请检查 SSH 密钥或 Git 凭据。\n" + redact(text[-1500:])
    if result.returncode == 2 or "couldn't find remote ref" in lower:
        return "branch_missing", "远端没有指定的目标分支"
    return "remote_error", redact(text[-1500:]) or "无法访问远端仓库"


class Inspector:
    def __init__(self, client: GitClient):
        self.git = client

    def is_own_repository(self, path: Path) -> bool:
        if not path.is_dir() or not (path / ".git").exists():
            return False
        result = self.git.run(["rev-parse", "--show-toplevel"], path)
        return result.returncode == 0 and canonical(Path(result.output.strip())) == canonical(path)

    def has_parent_gitlink(self, path: Path) -> bool:
        for parent in path.parents:
            if not (parent / ".git").exists() or not self.is_own_repository(parent):
                continue
            relative = path.relative_to(parent)
            prefixes = [Path(*relative.parts[:i]).as_posix() for i in range(1, len(relative.parts) + 1)]
            magic = ":(literal,icase)" if os.name == "nt" else ":(literal)"
            result = self.git.run(["ls-files", "--stage", "-z", "--",
                                   *(magic + prefix for prefix in prefixes)], parent)
            if result.returncode:
                raise ValueError("无法检查父仓库中的子模块记录：" + redact(result.output))
            for entry in result.output.split("\0"):
                if entry:
                    metadata, name = entry.split("\t", 1)
                    if metadata.startswith("160000 ") and canonical(parent / name) in {
                        canonical(parent / prefix) for prefix in prefixes
                    }:
                        return True
        return False

    def layout_conflict(self, path: Path, children: list[Path], revisions: list[str]) -> str:
        trees = {}
        for revision in dict.fromkeys(revisions):
            for child in children:
                relative = child.relative_to(path)
                tree = revision
                for depth, component in enumerate(relative.parts, 1):
                    prefix = Path(*relative.parts[:depth]).as_posix()
                    if tree not in trees:
                        result = self.git.run(["ls-tree", "-z", tree], path)
                        if result.returncode:
                            raise ValueError("无法检查目标目录布局：" + redact(result.output))
                        entries = {}
                        for entry in result.output.split("\0"):
                            if entry:
                                metadata, name = entry.split("\t", 1)
                                mode, _, oid = metadata.split(" ", 2)
                                entries[os.path.normcase(name)] = (mode, oid)
                        trees[tree] = entries
                    entry = trees[tree].get(os.path.normcase(component))
                    if entry is None:
                        break
                    mode, tree = entry
                    if depth == len(relative.parts) or mode != "040000":
                        return f"目标提交占用了子仓库路径或其上级路径 {prefix}，请先调整目录布局"
        return ""

    def inspect(self, profile: Profile, repo: RepoSpec, remote: bool = False) -> Snapshot:
        snap = Snapshot(repo.id, "unchecked", "check", "", target_branch=profile.target(repo),
                        checked_at=datetime.now().astimezone().isoformat(timespec="seconds"))

        def state(status: str, action: str, message: str) -> Snapshot:
            if self.git.runner.cancel.is_set():
                status, action, message = "cancelled", "error", "操作已中断，请重新检查"
            snap.status, snap.action, snap.message = status, action, redact(message)
            return snap

        path = profile.directory(repo)
        valid = self.git.run(["check-ref-format", "--branch", snap.target_branch])
        if valid.returncode:
            return state("error", "error", "Git 不可用或分支名称无效：" + valid.output)
        try:
            missing = not path.exists()
            empty = path.is_dir() and next(path.iterdir(), None) is None
        except OSError as error:
            return state("error", "error", str(error))
        if missing or empty:
            if self.has_parent_gitlink(path):
                return state("submodule", "block", "父仓库将此路径登记为 Git submodule，请通过父仓库管理")
            state("missing" if missing else "empty", "clone",
                  "验证目标分支后克隆" if missing else "现有目录为空，可以直接克隆")
            if remote:
                result = self.git.run(["ls-remote", "--exit-code", "--heads", repo.remote,
                                       "refs/heads/" + snap.target_branch], network=True)
                if result.returncode:
                    status, message = remote_failure(result)
                    return state(status, "error", message)
                snap.remote_checked = True
                snap.remote_oid = result.output.split()[0]
            return snap
        if not self.is_own_repository(path):
            return state("occupied", "block", "目录已有内容，但不是此目录自身的有效 Git 工作树；保留现有文件")
        result = self.git.run(["rev-parse", "--verify", "HEAD"], path)
        if result.returncode:
            return state("incomplete", "block", "仓库没有有效 HEAD，可能是初始化或克隆未完成")
        snap.current_branch = self.git.run(["symbolic-ref", "--quiet", "--short", "HEAD"], path).output.strip()
        if not snap.current_branch:
            return state("detached", "block", "仓库处于分离 HEAD 状态，请先保存或切换到本地分支")
        superproject = self.git.run(["rev-parse", "--show-superproject-working-tree"], path)
        if superproject.output.strip():
            return state("submodule", "block", "此目录是 Git submodule，需要按父仓库提交指针管理")
        origin = self.git.run(["remote", "get-url", "origin"], path)
        snap.origin = origin.output.strip() if not origin.returncode else ""
        if not snap.origin or normalized_remote(snap.origin) != normalized_remote(repo.remote):
            return state("remote_mismatch", "block", "origin 与配置不一致，请编辑配置或明确更换 origin")
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
            marker_path = self.git.run(["rev-parse", "--git-path", marker], path).output.strip()
            if marker_path and (path / marker_path).exists():
                return state("operation", "block", "存在未完成的 Git 操作：" + marker)
        status = self.git.run(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path)
        if status.returncode:
            return state("error", "error", status.output)
        children = [profile.directory(r) for r in profile.repositories
                    if r.id != repo.id and path in profile.directory(r).parents]
        parts = iter(status.output.split("\0"))
        for entry in parts:
            if not entry:
                continue
            code, name = entry[:2], entry[3:]
            if "R" in code or "C" in code:
                next(parts, None)
            entry_path = (path / name.rstrip("/")).resolve()
            if code == "??" and entry_path in children and self.is_own_repository(entry_path):
                continue
            snap.changes.append(f"{code} {name}")
        if snap.changes:
            return state("dirty", "block", f"有 {len(snap.changes)} 项工作区变更，请处理后重试")
        ref = "refs/remotes/origin/" + snap.target_branch
        if remote:
            result = self.git.run(["fetch", "--progress", "--no-tags", "origin",
                                   "+refs/heads/" + snap.target_branch + ":" + ref], path, network=True, stream=True)
            if result.returncode:
                kind, message = remote_failure(result)
                return state(kind, "error", message)
            snap.remote_checked = True
        revision = self.git.run(["rev-parse", "--verify", ref], path)
        if revision.returncode:
            return state("unchecked", "check", "没有目标分支的远端缓存，需要检查远端")
        snap.remote_oid = revision.output.strip()
        local = "refs/heads/" + snap.target_branch
        exists = self.git.run(["show-ref", "--verify", "--hash", local], path)
        targets = [snap.remote_oid] + ([exists.output.strip()] if exists.returncode == 0 else [])
        conflict = self.layout_conflict(path, children, targets)
        if conflict:
            return state("layout_conflict", "block", conflict)
        if exists.returncode:
            return state("switch", "switch", "创建目标本地跟踪分支并切换")
        counts = self.git.run(["rev-list", "--left-right", "--count", local + "..." + ref], path)
        if counts.returncode:
            return state("error", "error", counts.output)
        snap.ahead, snap.behind = map(int, counts.output.split())
        if snap.ahead and snap.behind:
            return state("diverged", "block", f"目标本地分支领先 {snap.ahead}、落后 {snap.behind} 个提交，请手动处理")
        if snap.current_branch != snap.target_branch:
            return state("switch", "switch", "切换到目标本地分支并检查快进更新")
        if snap.behind:
            return state("behind", "update", f"可以快进 {snap.behind} 个提交")
        if snap.ahead:
            return state("ahead", "none", f"本地领先 {snap.ahead} 个提交，保留本地提交")
        return state("up_to_date", "none", "目标分支与最近获取的远端提交一致")
