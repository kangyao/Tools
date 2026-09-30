"""Build a profile's repository list from the Git repositories already under a project root.

Read-only: lists directories and reads origin and the current branch of each repository.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from .git_ops import GitClient, remote_identity
from .models import Profile, RepoSpec
from .process import ProcessRunner

MAX_DEPTH = 3
# Large build or cache folders that never hold a configured repository.
SKIPPED_DIRECTORIES = {"projects", "bin", "bin64", "build", "buildlogs", "library", "node_modules",
                       "temp", "obj", "intermediate"}


@dataclass
class Discovery:
    repositories: list[RepoSpec]
    branch_groups: dict[str, str]
    required: list[str]
    found: int
    notes: list[str] = field(default_factory=list)


def find_repositories(root: Path, max_depth: int = MAX_DEPTH) -> list[Path]:
    """Directories under root (root included) that are Git worktree roots, nearest first."""
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        if any(entry.name.casefold() == ".git" for entry in entries):
            found.append(directory)
        if depth >= max_depth:
            return
        for entry in sorted(entries, key=lambda item: item.name.casefold()):
            name = entry.name.casefold()
            if name.startswith(".") or name in SKIPPED_DIRECTORIES:
                continue
            try:
                if not entry.is_dir(follow_symlinks=False) or entry.is_junction():
                    continue
            except OSError:
                continue
            walk(Path(entry.path), depth + 1)

    walk(root, 0)
    return found


def _stores_credentials(remote: str) -> bool:
    url = urlsplit(remote) if "://" in remote else None
    return bool(url and (url.password or (url.scheme.lower() in {"http", "https"} and url.username)
                         or re.search(r"(token|password|secret)=", url.query, re.I)))


def _unique_id(relative: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", relative).strip("-").lower() or "repo"
    candidate, index = base, 2
    while candidate in used:
        candidate, index = f"{base}-{index}", index + 1
    return candidate


def discover_profile(root: Path, template: Profile, git: GitClient | None = None) -> Discovery:
    """Map repositories found under root onto the template's repositories.

    A repository matching a template entry by remote identity (or, failing that, by relative
    path) keeps that entry's id, name, branch group and options; each branch group takes the
    branch most of its repositories are on. Other repositories get a fixed branch equal to their
    current branch. Template entries not found on disk stay listed but disabled.
    """
    git = git or GitClient(ProcessRunner())
    root = Path(root).resolve()
    by_identity = {remote_identity(r.remote): r for r in template.repositories}
    by_path = {Path(r.path).as_posix().casefold().strip("/") or ".": r for r in template.repositories}
    matched: dict[str, RepoSpec] = {}
    extras: list[RepoSpec] = []
    current: dict[str, str] = {}
    votes: dict[str, Counter] = {}
    notes: list[str] = []
    used = {r.id for r in template.repositories}
    found = find_repositories(root)
    for directory in found:
        relative = directory.relative_to(root).as_posix()
        relative = relative if relative not in {"", "."} else "."
        remote = git.run(["config", "--get", "remote.origin.url"], directory).output.strip()
        if not remote:
            notes.append(f"{relative}：没有 origin 远端，已跳过")
            continue
        if _stores_credentials(remote):
            notes.append(f"{relative}：origin 地址包含凭据，不能写入方案，已跳过")
            continue
        head = git.run(["symbolic-ref", "--short", "-q", "HEAD"], directory)
        branch = head.output.strip() if head.returncode == 0 else ""
        spec = by_identity.get(remote_identity(remote)) or by_path.get(relative.casefold())
        if spec is not None and spec.id not in matched:
            # The root repository is named after the project folder, which tells workspaces apart.
            name = root.name if relative == "." and root.name else spec.name
            repo = replace(spec, name=name, path=relative, remote=remote, enabled=True,
                           depends_on=list(spec.depends_on))
            if repo.branch_group:
                if branch:
                    votes.setdefault(repo.branch_group, Counter())[branch] += 1
                    current[repo.id] = branch
                else:
                    notes.append(f"{relative}：处于分离 HEAD，沿用分支组 {repo.branch_group}")
            elif branch:
                repo.branch = branch
            matched[spec.id] = repo
            continue
        if not branch:
            notes.append(f"{relative}：处于分离 HEAD，无法确定目标分支，已跳过")
            continue
        repo_id = _unique_id(relative, used)
        used.add(repo_id)
        name = Path(relative).name if relative != "." else root.name
        extras.append(RepoSpec(repo_id, name or repo_id, relative, remote, branch=branch))

    groups = dict(template.branch_groups)
    for group, counter in votes.items():
        groups[group] = counter.most_common(1)[0][0]
    for repo_id, branch in current.items():
        group = matched[repo_id].branch_group
        if branch != groups[group]:
            notes.append(f"{matched[repo_id].name}：当前在 {branch}，分支组 {group} 取 {groups[group]}，同步时会切换")

    repositories: list[RepoSpec] = []
    for spec in template.repositories:
        if spec.id in matched:
            repositories.append(matched[spec.id])
        else:
            repositories.append(replace(spec, enabled=False, depends_on=list(spec.depends_on)))
            notes.append(f"{spec.name}：目录中未找到（{spec.path}），保留但不启用")
    repositories.extend(extras)
    required = [repo.id for repo in repositories if repo.enabled]
    return Discovery(repositories, groups, required, len(matched) + len(extras), notes)
