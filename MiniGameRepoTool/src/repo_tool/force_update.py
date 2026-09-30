from __future__ import annotations

import tempfile
from pathlib import Path

from .changes import ChangesService
from .git_ops import GitClient, Inspector
from .models import Profile, canonical


class ForceUpdate:
    """Discard ordinary uncommitted files, under RepoService's existing run lock."""

    def __init__(self, data_dir: Path, git: GitClient):
        self.changes = ChangesService(data_dir, cancel=git.runner.cancel)
        self.changes.git = git
        self.changes.inspector = Inspector(git)

    def _protect_ignored_files(self, root: Path, paths: list[str]) -> None:
        # A staged deletion can coexist with an ignored replacement at the same path.
        # check-ignore consults the current index, so ordinary tracked files stay eligible.
        batches, batch, size = [], [], 0
        for path in paths:
            if not self.changes._safe_path(root, path).exists():
                continue
            if batch and size + len(path) > 6000:
                batches.append(batch)
                batch, size = [], 0
            batch.append(path)
            size += len(path) + 3
        if batch:
            batches.append(batch)
        for batch in batches:
            self.changes._check_cancel()
            result = self.changes.git.run(["check-ignore", "--", *batch], root)
            if result.returncode == 0:
                raise ValueError("恢复路径被 Git 忽略的文件占用，未丢弃文件：" + result.output.strip())
            if result.returncode != 1:
                raise ValueError("无法核对被忽略文件，未丢弃文件：" + result.output.strip())

    def discard(self, profile: Profile, repo_id: str) -> int:
        changes = self.changes
        repo = profile.repo(repo_id)
        if not repo.force_update:
            raise ValueError("该仓库未启用强制更新")
        root = profile.directory(repo)
        # Refuse a known switch failure before throwing away any working files.
        worktrees = changes._run(root, ["worktree", "list", "--porcelain", "-z"])
        for record in worktrees.split("\0\0"):
            fields = dict(field.split(" ", 1) for field in record.split("\0") if " " in field)
            if (fields.get("branch") == "refs/heads/" + profile.target(repo)
                    and canonical(Path(fields["worktree"])) != canonical(root)):
                raise ValueError("目标分支正被其他 worktree 使用，请先处理；未丢弃文件")
        approved = changes._scan(profile, repo_id)
        if approved.operation or approved.submodule:
            raise ValueError("未完成的 Git 操作或子模块不能在此强制更新")
        children = {canonical(profile.directory(child)) for child in profile.repositories
                    if child.id != repo_id and root in profile.directory(child).parents}
        rows = []
        for row in approved.files:
            child = root / row.path.rstrip("/")
            # A configured child repository: untracked, or its commit drifting from our gitlink.
            if ((row.kind == "untracked" or (row.kind == "submodule" and row.index_status == "."))
                    and canonical(child) in children and changes.inspector.is_own_repository(child)):
                continue
            if not row.discardable:
                raise ValueError(f"无法强制更新 {row.path}：{row.reason}；未丢弃文件")
            rows.append(row)
        if not rows:
            return 0
        for row in rows:
            for path in row.paths:
                changes._safe_path(root, path)
        untracked = {row.path: changes._fingerprint(changes._safe_path(root, row.path))
                     for row in rows if row.kind == "untracked"}
        tracked = list(dict.fromkeys(path for row in rows if row.kind == "tracked" for path in row.paths))
        if changes._scan(profile, repo_id) != approved:
            raise ValueError("仓库文件或 HEAD 已变化，请重新检查；未丢弃文件")
        self._protect_ignored_files(root, tracked)
        changes._check_cancel()
        if tracked:
            # Only a path list is written; no backup, stash, reference or patch is created.
            with tempfile.TemporaryDirectory(prefix="repo-tool-force-") as temporary:
                pathspec = Path(temporary) / "paths.nul"
                pathspec.write_bytes(b"".join(path.encode("utf-8") + b"\0" for path in tracked))
                changes._run(root, ["--literal-pathspecs", "restore", "--source=" + approved.head,
                                   "--staged", "--worktree", "--no-recurse-submodules",
                                   "--pathspec-from-file=" + str(pathspec), "--pathspec-file-nul"])
        for path, fingerprint in untracked.items():
            changes._check_cancel()
            target = changes._safe_path(root, path)
            if changes._fingerprint(target) != fingerprint:
                raise ValueError("未跟踪文件在操作期间变化，已停止：" + path)
            target.unlink()
        return len(rows)
