from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from .build_config import BuildOptions, validate_build_options


@dataclass
class RepoSpec:
    id: str
    name: str
    path: str
    remote: str
    branch_group: str = ""
    branch: str = ""
    enabled: bool = True
    depends_on: list[str] = field(default_factory=list)
    ignore_changes: bool = False
    force_update: bool = False
    init_source: str = ""


@dataclass
class SetupOptions:
    auto_run: bool = False
    required_repositories: list[str] = field(default_factory=list)


INIT_MODES = {"auto": "自动复用本地工程", "specified": "指定源工程", "network": "纯网络下载"}


@dataclass
class InitOptions:
    # Without sources, auto finds no candidate and falls back to a network clone.
    mode: str = "auto"
    sources: list[str] = field(default_factory=list)
    reuse_git: bool = True
    reuse_lfs: bool = True
    fallback: bool = True


@dataclass
class Profile:
    id: str
    name: str
    root: str
    branch_groups: dict[str, str]
    repositories: list[RepoSpec]
    setup: SetupOptions = field(default_factory=SetupOptions)
    build: BuildOptions = field(default_factory=BuildOptions)
    init: InitOptions = field(default_factory=InitOptions)

    def repo(self, repo_id: str) -> RepoSpec:
        return next(r for r in self.repositories if r.id == repo_id)

    def target(self, repo: RepoSpec) -> str:
        return repo.branch or self.branch_groups.get(repo.branch_group, "")

    def directory(self, repo: RepoSpec) -> Path:
        return (Path(self.root) / repo.path).resolve()


@dataclass
class ProfileDocument:
    schema_version: int
    active_profile_id: str
    profiles: list[Profile]


@dataclass
class Snapshot:
    repo_id: str
    status: str
    action: str
    message: str
    current_branch: str = ""
    target_branch: str = ""
    ahead: int = 0
    behind: int = 0
    changes: list[str] = field(default_factory=list)
    origin: str = ""
    remote_oid: str = ""
    remote_checked: bool = False
    checked_at: str = ""

    @property
    def runnable(self) -> bool:
        return self.action in {"clone", "update", "switch", "none", "force_update"}

    @property
    def ready(self) -> bool:
        return self.status in {"up_to_date", "ahead"} and self.current_branch == self.target_branch


@dataclass
class RepoResult:
    repo_id: str
    outcome: str
    message: str
    snapshot: Snapshot | None = None
    details: dict = field(default_factory=dict)


STATUS_NAMES = {
    "missing": "未克隆", "empty": "空目录", "occupied": "目录被占用",
    "incomplete": "仓库不完整", "up_to_date": "已是最新", "ahead": "本地领先",
    "behind": "待快进更新", "switch": "待切换分支", "unchecked": "待远端检查",
    "dirty": "有本地修改", "diverged": "分支已分叉", "operation": "Git 操作未完成",
    "force_update": "待强制更新",
    "detached": "分离 HEAD", "remote_mismatch": "远端不一致", "submodule": "Git 子模块",
    "branch_missing": "目标分支不存在", "auth_error": "认证失败", "remote_error": "远端访问失败",
    "blocked": "待处理", "layout_conflict": "目录布局冲突", "cancelled": "已取消",
    "error": "检查失败", "running": "执行中",
}
ACTION_NAMES = {"clone": "克隆", "switch": "切换并更新", "update": "快进更新",
                "force_update": "强制更新（不备份）",
                "none": "无需更新", "check": "检查远端", "block": "待处理", "error": "失败"}
OUTCOME_NAMES = {"success": "完成", "failed": "失败", "blocked": "待处理", "cancelled": "未执行 / 取消"}


def canonical(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def dependencies(profile: Profile) -> dict[str, set[str]]:
    paths = {r.id: profile.directory(r) for r in profile.repositories}
    result = {r.id: set(r.depends_on) for r in profile.repositories}
    for repo in profile.repositories:
        parents = [other for other in profile.repositories
                   if other.id != repo.id and paths[other.id] in paths[repo.id].parents]
        if parents:
            nearest = max(parents, key=lambda r: len(paths[r.id].parts))
            result[repo.id].add(nearest.id)
    return result


def dependency_order(profile: Profile) -> list[str]:
    """Order tree rows using directory parents and legacy depends_on hints, not sync prerequisites."""
    deps = dependencies(profile)
    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(key: str) -> None:
        if key in ordered:
            return
        if key not in deps:
            raise ValueError(f"排序参考仓库不存在：{key}")
        if key in visiting:
            raise ValueError(f"仓库排序参考与目录层级形成循环：{key}")
        visiting.add(key)
        for candidate in profile.repositories:
            if candidate.id in deps[key]:
                visit(candidate.id)
        unknown = deps[key] - deps.keys()
        if unknown:
            raise ValueError(f"排序参考仓库不存在：{', '.join(sorted(unknown))}")
        visiting.remove(key)
        ordered.append(key)

    for repo in profile.repositories:
        visit(repo.id)
    return ordered


def validate_profile(profile: Profile) -> None:
    validate_build_options(profile.build)
    if not all(isinstance(value, str) for value in (profile.id, profile.name, profile.root)):
        raise ValueError("方案 ID、名称和根目录必须是文本")
    if not profile.id.strip() or not profile.name.strip():
        raise ValueError("方案 ID 和名称不能为空")
    if not Path(profile.root).is_absolute():
        raise ValueError("工程根目录必须是绝对路径")
    if not profile.repositories:
        raise ValueError("方案至少需要一个仓库")
    if (not isinstance(profile.branch_groups, dict)
            or not all(isinstance(k, str) and k.strip() and isinstance(v, str)
                       for k, v in profile.branch_groups.items())):
        raise ValueError("分支组格式无效")
    if (not isinstance(profile.setup.auto_run, bool)
            or not isinstance(profile.setup.required_repositories, list)
            or not all(isinstance(key, str) for key in profile.setup.required_repositories)):
        raise ValueError("环境准备的启用状态或必需仓库列表格式无效")
    ids: set[str] = set()
    paths: set[str] = set()
    root = Path(profile.root).resolve()
    for repo in profile.repositories:
        if not all(isinstance(value, str) for value in
                   (repo.id, repo.name, repo.path, repo.remote, repo.branch_group, repo.branch)):
            raise ValueError("仓库 ID、名称、路径、远端及分支必须是文本")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", repo.id) or repo.id in ids:
            raise ValueError(f"仓库 ID 无效或重复：{repo.id}")
        ids.add(repo.id)
        if not repo.name.strip() or not repo.remote.strip() or repo.remote.startswith("-"):
            raise ValueError(f"仓库名称或远端地址无效：{repo.id}")
        if any(c in repo.remote for c in "\r\n\0"):
            raise ValueError("远端地址不能包含控制字符")
        url = urlsplit(repo.remote) if "://" in repo.remote else None
        if url and (url.password or (url.scheme.lower() in {"http", "https"} and url.username)
                    or re.search(r"(token|password|secret)=", url.query, re.I)):
            raise ValueError("远端地址不能保存密码或令牌凭据")
        relative = PureWindowsPath(repo.path)
        if not repo.path or relative.drive or relative.is_absolute() or Path(repo.path).is_absolute():
            raise ValueError(f"仓库路径必须相对根目录：{repo.path}")
        if ".git" in [s.casefold() for s in relative.parts]:
            raise ValueError("仓库路径不能使用 .git 目录")
        dest = profile.directory(repo)
        if dest != root and root not in dest.parents:
            raise ValueError(f"仓库路径越出工程目录：{repo.path}")
        normalized = canonical(dest)
        if normalized in paths:
            raise ValueError(f"仓库目录重复：{repo.path}")
        paths.add(normalized)
        if bool(repo.branch) == bool(repo.branch_group):
            raise ValueError(f"{repo.name} 必须在分支组和固定分支中选择一个")
        target = profile.target(repo)
        if (not target or target.startswith("-") or target in {"HEAD", "@"}
                or re.search(r"[\s~^:?*\\\[\x00-\x1f]", target) or ".." in target
                or "@{" in target or target.endswith(("/", ".", ".lock"))
                or target.startswith("/") or "//" in target):
            raise ValueError(f"{repo.name} 的目标分支无效：{target}")
        if (not isinstance(repo.enabled, bool) or not isinstance(repo.depends_on, list)
                or not all(isinstance(x, str) for x in repo.depends_on)):
            raise ValueError(f"{repo.name} 的启用状态或排序参考格式无效")
        if not isinstance(repo.ignore_changes, bool) or not isinstance(repo.force_update, bool):
            raise ValueError(f"{repo.name} 的忽略修改提醒和强制更新选项必须为布尔值")
        if not isinstance(repo.init_source, str) or (repo.init_source and not _absolute_line(repo.init_source)):
            raise ValueError(f"{repo.name} 的初始化来源必须为空或绝对路径")
    if not set(profile.setup.required_repositories) <= ids:
        raise ValueError("环境准备引用了不存在的仓库")
    validate_init_options(profile)
    dependency_order(profile)


def _absolute_line(value: str) -> bool:
    return bool(value.strip()) and not any(c in value for c in "\r\n\0") and Path(value).is_absolute()


def validate_init_options(profile: Profile) -> None:
    init = profile.init
    if not isinstance(init, InitOptions) or init.mode not in INIT_MODES:
        raise ValueError("初始化方式无效")
    if not all(isinstance(value, bool) for value in (init.reuse_git, init.reuse_lfs, init.fallback)):
        raise ValueError("初始化加速的复用和回退选项必须为布尔值")
    if not isinstance(init.sources, list) or not all(isinstance(s, str) and _absolute_line(s) for s in init.sources):
        raise ValueError("本地工程来源必须是绝对路径，每行一个")
    if len({canonical(Path(s)) for s in init.sources}) != len(init.sources):
        raise ValueError("本地工程来源不能重复")
    if init.mode == "specified" and not init.sources and not any(r.init_source for r in profile.repositories):
        raise ValueError("指定源工程方式需要填写来源目录，或为仓库指定初始化来源")
