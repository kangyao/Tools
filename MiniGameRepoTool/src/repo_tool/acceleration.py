"""Initialization acceleration: seed a new clone from local Git objects and LFS files.

Sources are only read. The target is prepared in a run-owned temporary directory beside it,
fetched from the configured remote, detached from borrowed objects, verified and then moved.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .git_ops import GitClient, normalized_remote, remote_failure, remote_identity
from .models import INIT_MODES, STATUS_NAMES, Profile, RepoSpec, Snapshot, canonical
from .process import ProcessResult, redact

TEMP_PREFIX = ".repotool-init-"
STAGES = ("检查来源", "复用 Git 数据", "复用 LFS", "下载缺失数据", "检出", "验证完成")
OID = re.compile(r"[0-9a-f]{64}")


def lfs_object(storage: Path, oid: str) -> Path:
    return storage / "objects" / oid[:2] / oid[2:4] / oid


def byte_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


@dataclass
class SourceProbe:
    path: str
    reason: str = ""
    identities: set[str] = field(default_factory=set)
    common_dir: str = ""
    git_reason: str = ""
    lfs_dir: str = ""


@dataclass
class SourceCandidate:
    path: str
    requested: bool
    git_usable: bool
    lfs_usable: bool
    reference: str = ""
    lfs_dir: str = ""
    reason: str = ""


@dataclass
class InitPlan:
    repo_id: str
    name: str
    target: str
    branch: str
    status: str
    message: str
    creates: bool
    candidates: list[SourceCandidate] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)


class SourceScanner:
    """Read-only discovery of local repositories that can seed a new clone.

    Nothing is fetched, switched or configured in a source repository.
    """

    def __init__(self, git: GitClient):
        self.git = git
        self.cache: dict[str, SourceProbe] = {}

    def probe(self, path: Path) -> SourceProbe:
        key = canonical(path)
        if key not in self.cache:
            self.cache[key] = self._probe(path)
        return self.cache[key]

    def _probe(self, path: Path) -> SourceProbe:
        probe = SourceProbe(str(path))
        try:
            if not path.is_dir():
                probe.reason = "目录不存在"
                return probe
            if not (path / ".git").exists():
                probe.reason = "没有 .git，不是独立仓库"
                return probe
        except OSError as error:
            probe.reason = str(error)
            return probe
        result = self.git.run(["rev-parse", "--path-format=absolute", "--show-toplevel",
                               "--git-common-dir", "--is-shallow-repository"], path)
        lines = result.output.strip().splitlines()
        if result.returncode or len(lines) != 3:
            probe.reason = "无法读取仓库：" + redact(result.output.strip()[-300:])
            return probe
        top, common, shallow = lines
        # A broken .git lets Git walk up to an enclosing repository. Never borrow from that one.
        if canonical(Path(top)) != canonical(path):
            probe.reason = f"此目录的 .git 无效，Git 向上识别到了 {top}"
            return probe
        common_dir = Path(common)
        probe.common_dir = str(common_dir)
        config = self.git.run(["config", "--get-regexp",
                               r"^(remote\..*\.(url|promisor)|extensions\.partialclone|lfs\.storage)$"], path)
        settings = []
        for line in config.output.splitlines():
            key, _, value = line.partition(" ")
            settings.append((key.lower(), value.strip()))
        probe.identities = {remote_identity(value) for key, value in settings
                            if key.startswith("remote.") and key.endswith(".url")}
        if not probe.identities:
            probe.reason = "没有配置远端，无法确认仓库身份"
            return probe
        storage = next((value for key, value in reversed(settings) if key == "lfs.storage"), "")
        lfs = Path(storage) if storage and Path(storage).is_absolute() else common_dir / (storage or "lfs")
        if (lfs / "objects").is_dir():
            probe.lfs_dir = str(lfs)
        if shallow.strip() == "true":
            probe.git_reason = "浅克隆，对象不完整，第一版不作为 Git 来源"
        elif any(key == "extensions.partialclone" or (key.endswith(".promisor") and value == "true")
                 for key, value in settings):
            probe.git_reason = "部分克隆，对象不完整"
        elif not (common_dir / "objects").is_dir():
            probe.git_reason = "对象目录缺失"
        elif (common_dir / "gc.pid").exists():
            probe.git_reason = "对象库正在维护（存在 gc.pid）"
        return probe

    def candidates(self, profile: Profile, repo: RepoSpec) -> list[SourceCandidate]:
        init = profile.init
        wanted = remote_identity(repo.remote)
        target = canonical(profile.directory(repo))
        # (path, requested, primary): primary slots are reported even when unusable.
        listed: list[tuple[Path, bool, bool]] = []
        if repo.init_source:
            listed.append((Path(repo.init_source), True, True))
        # An explicit per-repository source is never replaced by another project in specified mode.
        if not (init.mode == "specified" and repo.init_source):
            roots = init.sources[:1] if init.mode == "specified" else init.sources
            for root in roots:
                listed.append((Path(root) / repo.path, False, True))
                # A project may keep this repository under another configured relative path.
                listed.extend((Path(root) / other.path, False, False)
                              for other in profile.repositories if other.id != repo.id)
        result: list[SourceCandidate] = []
        seen: set[str] = set()
        for path, requested, primary in listed:
            try:
                path = path.resolve()
            except OSError:
                continue
            key = canonical(path)
            if key in seen or key == target:
                continue
            seen.add(key)
            probe = self.probe(path)
            matched = wanted in probe.identities
            if not primary and (probe.reason or not matched):
                continue
            reason = probe.reason or ("" if matched else "远端与配置不同：" + "，".join(sorted(probe.identities)))
            git_ok = not reason and not probe.git_reason
            lfs_ok = not reason and bool(probe.lfs_dir)
            result.append(SourceCandidate(str(path), requested, git_ok, lfs_ok,
                                          probe.common_dir if git_ok else "", probe.lfs_dir if lfs_ok else "",
                                          reason or probe.git_reason))
        return result

    def plan(self, profile: Profile, repo: RepoSpec, snap: Snapshot) -> InitPlan:
        target = profile.directory(repo)
        leftovers = []
        try:
            leftovers = sorted(str(p) for p in target.parent.glob(TEMP_PREFIX + repo.id + "-*") if p.is_dir())
        except OSError:
            pass
        return InitPlan(repo.id, repo.name, str(target), profile.target(repo), snap.status, snap.message,
                        snap.action == "clone", self.candidates(profile, repo), leftovers)


def describe_plan(profile: Profile, plan: InitPlan) -> list[str]:
    init = profile.init
    lines = [f"{plan.name}  →  {STATUS_NAMES.get(plan.status, plan.status)}",
             f"    目标：{plan.target}；分支 {plan.branch}"]
    git = [c for c in plan.candidates if c.git_usable]
    lfs = [c for c in plan.candidates if c.lfs_usable]
    if not plan.creates:
        lines.append(f"    不新建：{plan.message}；沿用现有同步规则，不做初始化加速")
    else:
        lines.append(f"    初始化方式：{INIT_MODES[init.mode]}")
    lines.append("    Git 来源：" + (f"{git[0].path}（远端身份一致）" if git else "无可用来源"))
    if len(git) > 1:
        lines.append("    其他 Git 来源：" + "、".join(c.path for c in git[1:]))
    lines.append("    LFS 来源：" + ("、".join(c.lfs_dir for c in lfs) if lfs else "无本地 LFS 对象目录"))
    for candidate in plan.candidates:
        if candidate.reason:
            usable = "；仍可提供 LFS 对象" if candidate.lfs_usable else ""
            lines.append(f"    未用作 Git 来源：{candidate.path}：{candidate.reason}{usable}")
    if plan.creates:
        if init.mode == "network":
            lines.append("    执行：纯网络下载，使用原始远端与 LFS 配置")
        else:
            use_git = git and init.reuse_git
            use_lfs = lfs and init.reuse_lfs
            if use_git:
                lines.append("    执行：借用本地 Git 对象，从原始远端补齐目标分支，完成后解除借用")
            elif init.reuse_git and not init.fallback:
                lines.append("    执行：没有可用的本地 Git 来源，按配置停止该仓库")
            else:
                lines.append("    执行：Git 数据从原始远端下载")
            if use_lfs:
                lines.append("    LFS：按目标提交的对象清单复制并校验本地对象，缺失对象从原始 LFS 服务下载；数量在取得目标提交后统计")
            elif not use_git:
                lines.append("    LFS：无可复用对象，按原始配置下载")
            lines.append("    需要网络：是，从原始远端获取目标分支")
    for leftover in plan.leftovers:
        lines.append(f"    发现以前遗留的临时目录，可确认后手动删除：{leftover}")
    return lines


def init_summary(details: dict) -> str:
    parts = []
    if details.get("git_source"):
        parts.append(f"Git 来源 {details['git_source']}（已解除借用）")
    else:
        parts.append("Git 数据来自远端")
    lfs = details.get("lfs") or {}
    if lfs.get("needed"):
        parts.append(f"LFS 需要 {lfs['needed']} 个：已有 {lfs.get('present', 0)}、本地复制 {lfs.get('copied', 0)}"
                     f"（{byte_size(lfs.get('copied_bytes', 0))}）、下载 {lfs.get('downloaded', 0)}")
    if details.get("commit"):
        parts.append("目标提交 " + details["commit"][:12])
    return "初始化加速：" + "；".join(parts)


class InitFailure(Exception):
    def __init__(self, stage: str, message: str, final: bool = False, outcome: str = "failed"):
        super().__init__(message)
        self.stage, self.message, self.final, self.outcome = stage, message, final, outcome


@dataclass
class InitOutcome:
    # done: placed and verified; network: run the ordinary clone; failed/blocked/cancelled: stop this repo.
    outcome: str
    message: str
    details: dict


class Initializer:
    def __init__(self, git: GitClient, log: Callable[[str], None], cancel: threading.Event):
        self.git = git
        self.log = log
        self.cancel = cancel
        self.owned: set[str] = set()
        self.details: dict = {}
        self.current_stage = ""
        self.stage_started = 0.0

    def stage(self, name: str) -> None:
        self._close_stage()
        self.current_stage, self.stage_started = name, time.monotonic()
        self.log(f"初始化阶段 {STAGES.index(name) + 1}/{len(STAGES)}：{name}")

    def _close_stage(self) -> None:
        if self.current_stage:
            self.details["stages"][self.current_stage] = round(time.monotonic() - self.stage_started, 1)
            self.current_stage = ""

    def finish(self, outcome: str, message: str) -> InitOutcome:
        self._close_stage()
        return InitOutcome(outcome, redact(message), self.details)

    def clone(self, profile: Profile, repo: RepoSpec, snap: Snapshot) -> InitOutcome:
        init = profile.init
        target = profile.directory(repo)
        self.details = {"mode": init.mode, "stages": {}, "commit": "", "git_source": "", "lfs": {}}
        self.stage("检查来源")
        candidates = SourceScanner(self.git).candidates(profile, repo)
        if self.cancel.is_set():
            return self.finish("cancelled", "初始化已中断，未创建目标目录；请重新检查")
        self.details["candidates"] = [asdict(c) for c in candidates]
        for candidate in candidates:
            self.log(f"候选来源 {candidate.path}：" + (candidate.reason or "可复用 Git 对象")
                     + ("；有 LFS 对象目录" if candidate.lfs_usable else ""))
        git_refs = [c for c in candidates if c.git_usable] if init.reuse_git else []
        lfs_dirs = [Path(c.lfs_dir) for c in candidates if c.lfs_usable] if init.reuse_lfs else []
        if init.reuse_git and not git_refs and not init.fallback:
            return self.finish("blocked", "没有可用的本地 Git 来源，按配置停止该仓库；候选来源及原因见运行日志")
        if not git_refs and not lfs_dirs:
            self.log("没有可复用的本地数据，改用网络克隆")
            return self.finish("network", "没有可复用的本地数据，已使用网络克隆")
        if target.parent == target:
            return self.finish("network", "目标是磁盘根目录，无法准备同级临时目录，已使用网络克隆")
        temp = None
        try:
            try:
                temp = self._make_temp(target, repo.id)
                self._build(repo, snap.target_branch, temp, git_refs, lfs_dirs)
                self._place(temp, target)
                temp = None
            except OSError as error:
                raise InitFailure(self.current_stage or "验证完成", str(error), True) from error
        except InitFailure as failure:
            if temp is not None:
                self._remove_owned(temp)
                temp = None
            if self.cancel.is_set() or failure.outcome == "cancelled":
                return self.finish("cancelled", "初始化已中断，已清理本次临时目录；请重新检查")
            message = f"初始化加速在“{failure.stage}”阶段失败：{failure.message}"
            self.details["failure"] = {"stage": failure.stage, "message": redact(failure.message)}
            if failure.final or not init.fallback:
                return self.finish(failure.outcome, message)
            self.log(message + "；回退网络克隆")
            return self.finish("network", message + "；已回退网络克隆")
        finally:
            if temp is not None:
                self._remove_owned(temp)
        return self.finish("done", init_summary(self.details))

    def _make_temp(self, target: Path, repo_id: str) -> Path:
        # Beside the target, so the final move is a same-volume rename.
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.parent / f"{TEMP_PREFIX}{repo_id}-{uuid.uuid4().hex[:8]}"
            temp.mkdir()
        except OSError as error:
            raise InitFailure("检查来源", f"无法创建临时目录：{error}", True) from error
        self.owned.add(canonical(temp))
        self.details["temp_dir"] = str(temp)
        self.log("本次临时目录：" + str(temp))
        return temp

    def _check(self, result: ProcessResult, stage: str, final: bool = False) -> None:
        if result.returncode == 0:
            return
        if result.cancelled:
            raise InitFailure(stage, "操作已中断", True, "cancelled")
        kind, message = remote_failure(result)
        lower = result.output.lower()
        if kind == "branch_missing" and stage != "复用 Git 数据":
            # Exit code 2 means a missing ref only for clone/ls-remote, not for git-lfs.
            kind, message = "remote_error", redact(result.output.strip()[-1500:])
        final = (final or kind in {"auth_error", "branch_missing"} or "not found in upstream" in lower
                 or "no space left" in lower or "not enough space" in lower)
        raise InitFailure(stage, message or f"Git 退出码 {result.returncode}", final)

    def _git(self, cwd: Path, args: list[str], stage: str) -> str:
        result = self.git.run(args, cwd)
        self._check(result, stage, final=True)
        return result.output.strip()

    def _build(self, repo: RepoSpec, branch: str, temp: Path, git_refs: list[SourceCandidate],
               lfs_dirs: list[Path]) -> None:
        self.stage("复用 Git 数据")
        args = ["clone", "--progress", "--no-checkout", "--single-branch", "--branch", branch]
        for candidate in git_refs:
            args += ["--reference-if-able", candidate.reference]
        if git_refs:
            args.append("--dissociate")
            self.log("借用本地 Git 对象：" + "、".join(c.path for c in git_refs))
        else:
            self.log("没有可借用的 Git 对象，从原始远端下载 Git 数据")
        # No checkout yet: LFS objects are placed before anything could trigger a download.
        result = self.git.run([*args, "--", repo.remote, str(temp)], network=True, stream=True,
                              environment={"GIT_LFS_SKIP_SMUDGE": "1"})
        self._check(result, "复用 Git 数据")
        self.details["git_source"] = git_refs[0].path if git_refs else ""
        self._require_independent(temp, "复用 Git 数据")
        commit = self._git(temp, ["rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}"],
                           "复用 Git 数据")
        self.details["commit"] = commit
        self.log("目标提交：" + commit)
        lfs_active = self._reuse_lfs(temp, commit, lfs_dirs)
        self.stage("检出")
        result = self.git.run(["reset", "--hard", "--quiet", commit], temp, stream=True, unbounded=True)
        self._check(result, "检出", final=True)
        if lfs_active:
            # Fills pointers from local objects when no smudge filter is installed.
            result = self.git.run(["lfs", "checkout"], temp, stream=True, unbounded=True)
            self._check(result, "检出", final=True)
            listing = self.git.run(["lfs", "ls-files", "--json"], temp, unbounded=True)
            self._check(listing, "检出", final=True)
            unfilled = [item.get("name", "") for item in self._lfs_files(listing.output)
                        if not item.get("checkout")]
            self.details["lfs"]["unfilled"] = len(unfilled)
            if unfilled:
                self.log(f"仍有 {len(unfilled)} 个 LFS 文件为指针：" + "、".join(unfilled[:5]))
        self.stage("验证完成")
        current = self._git(temp, ["symbolic-ref", "--quiet", "--short", "HEAD"], "验证完成")
        head = self._git(temp, ["rev-parse", "--verify", "HEAD"], "验证完成")
        origin = self._git(temp, ["remote", "get-url", "origin"], "验证完成")
        if current != branch or head != commit:
            raise InitFailure("验证完成", f"检出结果与目标不一致：{current} {head[:12]}", True)
        if normalized_remote(origin) != normalized_remote(repo.remote):
            raise InitFailure("验证完成", "新仓库的 origin 与配置不一致", True)
        self._require_independent(temp, "验证完成")

    def _require_independent(self, repo: Path, stage: str) -> None:
        common = Path(self._git(repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"], stage))
        alternates = common / "objects" / "info" / "alternates"
        try:
            borrowed = alternates.is_file() and alternates.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as error:
            raise InitFailure(stage, f"无法确认对象独立性：{error}", True) from error
        if borrowed:
            raise InitFailure(stage, "新仓库仍依赖来源对象库（alternates）", True)

    @staticmethod
    def _lfs_files(output: str) -> list[dict]:
        start, end = output.find("{"), output.rfind("}")
        if start < 0 or end < start:
            return []
        try:
            return json.loads(output[start:end + 1]).get("files") or []
        except (ValueError, AttributeError):
            raise InitFailure("复用 LFS", "无法解析 LFS 文件清单", True) from None

    def _lfs_storage(self, repo: Path) -> Path:
        common = Path(self._git(repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"], "复用 LFS"))
        configured = self.git.run(["config", "--get", "lfs.storage"], repo)
        value = configured.output.strip() if configured.returncode == 0 else ""
        return Path(value) if value and Path(value).is_absolute() else common / (value or "lfs")

    def _reuse_lfs(self, temp: Path, commit: str, sources: list[Path]) -> bool:
        self.stage("复用 LFS")
        stats = {"needed": 0, "present": 0, "copied": 0, "copied_bytes": 0, "rejected": 0,
                 "downloaded": 0, "missing": 0}
        self.details["lfs"] = stats
        if self.git.run(["lfs", "version"]).returncode:
            self.log("未检测到 Git LFS，跳过 LFS 复用；检出时保留指针文件")
            stats["available"] = False
            return False
        listing = self.git.run(["lfs", "ls-files", "--json", commit], temp, unbounded=True)
        self._check(listing, "复用 LFS", final=True)
        wanted: dict[str, int] = {}
        for item in self._lfs_files(listing.output):
            oid, size = item.get("oid", ""), item.get("size")
            if not isinstance(oid, str) or not OID.fullmatch(oid) or type(size) is not int or size < 0:
                raise InitFailure("复用 LFS", "LFS 清单包含无效对象：" + str(item.get("name", "")), True)
            wanted[oid] = size
        stats["needed"] = len(wanted)
        if not wanted:
            self.log("目标提交没有 LFS 文件")
            return False
        storage = self._lfs_storage(temp)
        missing = {}
        for oid, size in wanted.items():
            try:
                present = lfs_object(storage, oid).stat().st_size == size
            except OSError:
                present = False
            if present:
                stats["present"] += 1
            else:
                missing[oid] = size
        if missing and sources:
            self._copy_lfs(missing, sources, storage, stats)
        self.log(f"LFS 对象：需要 {stats['needed']}，已有 {stats['present']}，本地复制 {stats['copied']}"
                 f"（{byte_size(stats['copied_bytes'])}），校验或读取失败 {stats['rejected']}，待下载 {len(missing)}")
        if missing:
            self.stage("下载缺失数据")
            result = self.git.run(["lfs", "fetch", "origin", commit], temp, network=True, stream=True)
            self._check(result, "下载缺失数据", final=True)
            still = [oid for oid, size in missing.items() if not lfs_object(storage, oid).is_file()]
            stats["downloaded"] = len(missing) - len(still)
            stats["missing"] = len(still)
            if still:
                self.log(f"下载后仍缺少 {len(still)} 个 LFS 对象，可能被 lfs.fetchexclude 等配置排除")
        return True

    def _copy_lfs(self, missing: dict[str, int], sources: list[Path], storage: Path, stats: dict) -> None:
        scratch = storage / "tmp"
        try:
            scratch.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise InitFailure("复用 LFS", f"无法创建 LFS 临时目录：{error}", True) from error
        reported = time.monotonic()
        total = len(missing)
        for index, (oid, size) in enumerate(list(missing.items()), 1):
            for source in sources:
                if self.cancel.is_set():
                    raise InitFailure("复用 LFS", "操作已中断", True, "cancelled")
                candidate = lfs_object(source, oid)
                try:
                    if candidate.stat().st_size != size:
                        stats["rejected"] += 1
                        continue
                except OSError:
                    continue
                if self._copy_verified(candidate, lfs_object(storage, oid), scratch, oid):
                    stats["copied"] += 1
                    stats["copied_bytes"] += size
                    del missing[oid]
                    break
                stats["rejected"] += 1
                self.log(f"LFS 对象校验或读取失败，已跳过：{candidate}")
            if time.monotonic() - reported > 3:
                reported = time.monotonic()
                self.log(f"LFS 本地复用进度：{index} / {total}，已复制 {byte_size(stats['copied_bytes'])}")

    def _copy_verified(self, source: Path, destination: Path, scratch: Path, oid: str) -> bool:
        part = scratch / f"{oid}.{uuid.uuid4().hex[:8]}.part"
        digest = hashlib.sha256()
        try:
            try:
                reader = source.open("rb")
            except OSError:
                return False
            with reader, part.open("wb") as writer:
                while chunk := reader.read(8 << 20):
                    if self.cancel.is_set():
                        raise InitFailure("复用 LFS", "操作已中断", True, "cancelled")
                    digest.update(chunk)
                    writer.write(chunk)
            if digest.hexdigest() != oid:
                return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part, destination)
            return True
        except OSError as error:
            raise InitFailure("复用 LFS", f"写入 LFS 对象失败：{error}", True) from error
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass

    def _place(self, temp: Path, target: Path) -> None:
        try:
            if target.exists():
                if not target.is_dir() or next(target.iterdir(), None) is not None:
                    raise InitFailure("验证完成", f"目标目录已被其他操作占用，未覆盖：{target}", True, "blocked")
                target.rmdir()
            for attempt in range(10):
                try:
                    os.rename(temp, target)
                    break
                except PermissionError:
                    # Indexers and virus scanners briefly hold new files on Windows.
                    if attempt == 9:
                        raise
                    time.sleep(.5)
        except OSError as error:
            raise InitFailure("验证完成", f"无法移动到目标目录 {target}：{error}", True, "blocked") from error
        self.owned.discard(canonical(temp))
        self.details["placed"] = True
        self.log("已就位：" + str(target))

    def _remove_owned(self, temp: Path) -> None:
        key = canonical(temp)
        if key not in self.owned or not temp.name.startswith(TEMP_PREFIX):
            self.log("拒绝删除非本次创建的目录：" + str(temp))
            return

        def writable(function, path, *_):
            os.chmod(path, stat.S_IWRITE)
            function(path)

        handler = {"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}
        try:
            shutil.rmtree(temp, **handler)
        except OSError as error:
            self.log(f"临时目录未能完全清理，请确认无程序占用后手动删除：{temp}（{error}）")
            return
        self.owned.discard(key)
        self.log("已清理本次临时目录")
