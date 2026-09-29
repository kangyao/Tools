from __future__ import annotations

import hashlib
import os
import shutil
import stat
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path, PureWindowsPath

from .git_ops import GitClient, Inspector
from .models import Profile, canonical, validate_profile
from .process import ProcessRunner, redact
from .profiles import atomic_json
from .storage import RunJournal, RunLock


@dataclass(frozen=True)
class FileChange:
    path: str
    index_status: str
    worktree_status: str
    kind: str
    original_path: str = ""
    reason: str = ""
    signature: str = ""

    @property
    def discardable(self) -> bool:
        return not self.reason and self.kind in {"tracked", "untracked"}

    @property
    def paths(self) -> list[str]:
        if self.original_path and "R" in self.index_status + self.worktree_status:
            return [self.original_path, self.path]
        return [self.path]


@dataclass
class ChangeSnapshot:
    repo_id: str
    root: str
    head: str
    files: list[FileChange]
    submodule: bool = False
    operation: str = ""


@dataclass
class DiscardResult:
    repo_id: str
    outcome: str
    message: str
    backup_dir: str


class ChangesService:
    def __init__(self, data_dir: Path, emit=None, cancel: threading.Event | None = None):
        self.data_dir = Path(data_dir)
        self.emit = emit or (lambda kind, payload: None)
        self.cancel = cancel or threading.Event()
        self.git = GitClient(ProcessRunner(cancel=self.cancel))
        self.inspector = Inspector(self.git)

    def _check_cancel(self):
        if self.cancel.is_set():
            raise ValueError("操作已取消")

    def _run(self, root: Path, args: list[str], complete: bool = True):
        self._check_cancel()
        result = self.git.run(["--no-optional-locks", *args], root)
        if result.returncode:
            raise ValueError(redact(result.output.strip()) or "Git 操作失败 / 已取消")
        if complete and len(result.output) >= 4_000_000:
            raise ValueError("Git 输出过大，无法完整校验；请使用外部 Git 工具处理")
        return result.output

    @staticmethod
    def _safe_path(root: Path, relative: str) -> Path:
        win = PureWindowsPath(relative)
        parts = relative.replace("\\", "/").split("/")
        if (not relative or win.drive or win.root or "\\" in relative
                or any(p in {"", ".", ".."} or p.casefold() == ".git" for p in parts)
                or "\0" in relative or "\ufffd" in relative):
            raise ValueError("路径无效或指向 Git 元数据")
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise ValueError("符号链接或目录联接不能在此丢弃")
            if current.exists():
                info = current.lstat()
                if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                    raise ValueError("重解析路径不能在此丢弃")
                if current.is_dir() and (current / ".git").exists():
                    raise ValueError("嵌套仓库不能按普通文件丢弃")
        resolved = current.resolve()
        if resolved == root or root not in resolved.parents:
            raise ValueError("路径越出仓库目录")
        if current.exists() and not current.is_file():
            raise ValueError("目录或特殊文件不能在此丢弃")
        return current

    def _fingerprint(self, path: Path) -> str:
        if not path.exists():
            return "absent"
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self._check_cancel()
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("读取期间文件发生变化，请刷新")
        return f"{before.st_size}:{before.st_mtime_ns}:{before.st_mode}:{digest.hexdigest()}"

    def _scan(self, profile: Profile, repo_id: str) -> ChangeSnapshot:
        validate_profile(profile)
        root = profile.directory(profile.repo(repo_id))
        if not self.inspector.is_own_repository(root):
            raise ValueError("此目录不是自身的有效 Git 工作树")
        head = self._run(root, ["rev-parse", "--verify", "HEAD"]).strip()
        git_dir = Path(self._run(root, ["rev-parse", "--absolute-git-dir"]).strip())
        operation = next((name for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                                            "rebase-merge", "rebase-apply")
                          if (git_dir / name).exists()), "")
        submodule = bool(self._run(root, ["rev-parse", "--show-superproject-working-tree"]).strip())
        output = self._run(root, ["status", "--porcelain=v2", "-z", "--untracked-files=all",
                                  "--ignore-submodules=none"])
        records = iter(output.split("\0"))
        files = []
        for record in records:
            if not record:
                continue
            original, reason, modes = "", "", []
            if record.startswith("? "):
                name, xy, kind = record[2:], "??", "untracked"
                if name.endswith("/"):
                    reason = "子仓库或目录不能整体丢弃；请进入子仓库查看具体文件"
            elif record.startswith(("1 ", "2 ")):
                fields = record.split(" ", 8 if record[0] == "1" else 9)
                name, xy = fields[-1], fields[1]
                modes = fields[3:6]
                kind = "submodule" if "160000" in modes or fields[2].startswith("S") else "tracked"
                if record[0] == "2":
                    original = next(records)
                if kind == "submodule":
                    reason = "子模块提交或内部文件变化；请进入子仓库查看文件"
                elif "120000" in modes:
                    reason = "涉及符号链接，请使用外部 Git 工具处理"
                elif xy == ".A" and fields[4] == "000000":
                    reason = "intent-to-add 状态，请先在外部 Git 工具中处理"
            elif record.startswith("u "):
                fields = record.split(" ", 10)
                name, xy, kind = fields[-1], fields[1], "conflict"
                reason = "存在合并冲突，请先完成或中止对应 Git 操作"
            else:
                raise ValueError("无法识别 Git 文件状态，请刷新")
            row = FileChange(name, xy[0], xy[1], kind, original, reason)
            digest = hashlib.sha256((record + "\0" + original).encode("utf-8"))
            if not reason:
                try:
                    for relative in row.paths:
                        digest.update(self._fingerprint(self._safe_path(root, relative)).encode("utf-8"))
                except (ValueError, OSError) as error:
                    row = replace(row, reason=str(error))
            if operation:
                row = replace(row, reason="存在未完成的 Git 操作：" + operation)
            files.append(replace(row, signature=digest.hexdigest()))
        if any(row.kind == "conflict" for row in files):
            operation = operation or "未解决的索引冲突"
            files = [replace(row, reason="存在未完成的 Git 操作：" + operation) for row in files]
        names = Counter(os.path.normcase(row.path) for row in files)
        present = {os.path.normcase(row.path) for row in files}
        for i, row in enumerate(files):
            if names[os.path.normcase(row.path)] > 1:
                files[i] = replace(row, reason="同一路径有多条变更记录，请使用外部 Git 工具处理")
            elif row.original_path and "R" in row.index_status + row.worktree_status:
                original = root / row.original_path
                if os.path.normcase(row.original_path) in present or original.exists() or original.is_symlink():
                    files[i] = replace(row, reason="重命名的原路径又有新内容，请先单独处理原路径")
        return ChangeSnapshot(repo_id, str(root), head, files, submodule, operation)

    def scan(self, profile: Profile, repo_id: str) -> ChangeSnapshot:
        with RunLock(self.data_dir):
            return self._scan(profile, repo_id)

    def diff(self, profile: Profile, repo_id: str, path: str) -> str:
        with RunLock(self.data_dir):
            snapshot = self._scan(profile, repo_id)
            row = next((r for r in snapshot.files if r.path == path), None)
            if row is None:
                return "该文件状态已变化，请刷新列表。"
            root = Path(snapshot.root)
            if row.kind == "submodule":
                return row.reason + "\n\n子模块提交位置不会通过文件 Discard 操作改变。"
            if row.kind == "untracked":
                try:
                    actual = self._safe_path(root, row.path)
                    with actual.open("rb") as stream:
                        data = stream.read(200_001)
                except (OSError, ValueError) as error:
                    return str(error)
                if b"\0" in data:
                    return f"未跟踪二进制文件，大小 {actual.stat().st_size:,} 字节。"
                return ("未跟踪文件内容：\n\n" + data[:200_000].decode("utf-8", errors="replace")
                        + ("\n\n…预览已截断，文件备份将保留完整字节。" if len(data) > 200_000 else ""))
            options = ["--no-ext-diff", "--no-textconv", "--no-color", "--no-relative"]
            paths = ["--", *row.paths]
            staged = self._run(root, ["--literal-pathspecs", "diff", "--cached", *options,
                                      snapshot.head, *paths], complete=False)
            working = self._run(root, ["--literal-pathspecs", "diff", *options, *paths], complete=False)
            return ("已暂存（HEAD → 暂存区）\n" + (staged[:100_000] or "无\n")
                    + "\n未暂存（暂存区 → 工作区）\n" + (working[:100_000] or "无\n")
                    + ("\n…预览已截断，备份不受预览长度限制。" if max(len(staged), len(working)) > 100_000 else ""))

    @staticmethod
    def _selection(snapshot: ChangeSnapshot, selected: list[str]) -> list[FileChange]:
        rows = {row.path: row for row in snapshot.files}
        if not selected or len(set(selected)) != len(selected) or any(key not in rows for key in selected):
            raise ValueError("所选文件无效或状态已变化，请刷新")
        result = [rows[key] for key in selected]
        for row in result:
            if not row.discardable:
                raise ValueError(f"不能丢弃 {row.path}：{row.reason}")
        return result

    def _recheck(self, profile, repo_id, approved, selected):
        fresh = self._scan(profile, repo_id)
        if (fresh.repo_id != approved.repo_id or canonical(Path(fresh.root)) != canonical(Path(approved.root))
                or fresh.head != approved.head):
            raise ValueError("仓库或 HEAD 已变化，请刷新后重新选择")
        current = self._selection(fresh, selected)
        previous = self._selection(approved, selected)
        if current != previous:
            raise ValueError("所选文件或暂存区发生变化，请刷新后重新确认")
        return fresh, current

    def _backup(self, root, head, paths, backup):
        files = []
        for relative in paths:
            source = self._safe_path(root, relative)
            fingerprint = self._fingerprint(source)
            item = {"path": relative, "exists": source.exists(), "fingerprint": fingerprint}
            if source.exists():
                target = backup / "worktree" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if self._fingerprint(source) != fingerprint or self._fingerprint(target) != fingerprint:
                    raise ValueError("备份期间文件发生变化，请刷新后重试")
                item["sha256"] = fingerprint.rsplit(":", 1)[-1]
            files.append(item)
        patch = backup / "staged.patch"
        # Git writes the patch itself: no text decoding, redaction or capture-size limit.
        batches, batch, size = [], [], 0
        for relative in paths:
            if size + len(relative) > 6000 and batch:
                batches.append(batch)
                batch, size = [], 0
            batch.append(relative)
            size += len(relative) + 3
        if batch:
            batches.append(batch)
        with patch.open("wb") as output:
            for i, batch in enumerate(batches):
                fragment = backup / f"staged-part-{i}.patch"
                self._run(root, ["--literal-pathspecs", "diff", "--cached", "--binary", "--full-index",
                                 "--no-ext-diff", "--no-textconv", "--no-color", "--no-relative",
                                 "--no-renames", "--src-prefix=a/", "--dst-prefix=b/",
                                 "--output=" + str(fragment), head, "--", *batch])
                with fragment.open("rb") as stream:
                    shutil.copyfileobj(stream, output)
            output.flush()
            os.fsync(output.fileno())
        if patch.stat().st_size:
            self._run(root, ["apply", "--check", "--cached", "--reverse", "--whitespace=nowarn", str(patch)])
        return files

    def discard(self, profile: Profile, repo_id: str, approved: ChangeSnapshot,
                selected: list[str]) -> DiscardResult:
        with RunLock(self.data_dir):
            fresh, rows = self._recheck(profile, repo_id, approved, selected)
            root = Path(fresh.root)
            backup = (self.data_dir / "discard-backups" /
                      (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])).resolve()
            if backup == root or root in backup.parents:
                raise ValueError("备份目录位于当前仓库内，请使用仓库之外的配置目录")
            paths = list(dict.fromkeys(path for row in rows for path in row.paths))
            for path in paths:
                self._safe_path(root, path)
            backup.mkdir(parents=True)
            manifest = {"repo_id": repo_id, "root": str(root), "head": fresh.head, "status": "backing_up",
                        "selected": [asdict(row) for row in rows], "files": []}
            journal = RunJournal(self.data_dir, profile.id, "discard")
            mutating = False
            try:
                manifest["files"] = self._backup(root, fresh.head, paths, backup)
                manifest["status"] = "ready"
                atomic_json(backup / "manifest.json", manifest)
                (backup / "RECOVERY.md").write_text(
                    "丢弃前备份\n\n仓库：" + str(root) + "\n原 HEAD：" + fresh.head
                    + "\n\nworktree/ 保存原工作区文件的完整字节；manifest.json 记录原先存在/缺失的路径。"
                      "\nstaged.patch 保存所选文件的暂存区改动。恢复前须先保留当前新改动，并确认仍在原 HEAD。"
                      "\n若 staged.patch 非空，先在原 HEAD 的干净暂存区应用该补丁（git apply --cached），再把 worktree/"
                      " 中的文件按相对路径复制回原仓库。原先缺失的路径见清单。"
                      "\n备份含原始文件内容，请按工程文件同等方式保管。\n", encoding="utf-8")
                self.emit("backup", str(backup))
                self._recheck(profile, repo_id, approved, selected)
                self._check_cancel()
                manifest["status"] = "applying"
                atomic_json(backup / "manifest.json", manifest)
                tracked = list(dict.fromkeys(path for row in rows if row.kind != "untracked" for path in row.paths))
                mutating = True
                if tracked:
                    pathspec = backup / "paths.nul"
                    pathspec.write_bytes(b"".join(path.encode("utf-8") + b"\0" for path in tracked))
                    self._run(root, ["--literal-pathspecs", "restore", "--source=" + fresh.head,
                                     "--staged", "--worktree", "--no-recurse-submodules",
                                     "--pathspec-from-file=" + str(pathspec), "--pathspec-file-nul"])
                for row in rows:
                    if row.kind == "untracked":
                        self._check_cancel()
                        target = self._safe_path(root, row.path)
                        saved = next(item for item in manifest["files"] if item["path"] == row.path)
                        if self._fingerprint(target) != saved["fingerprint"]:
                            raise ValueError("未跟踪文件在操作期间发生变化，已停止：" + row.path)
                        target.unlink()
                remaining = self._scan(profile, repo_id)
                if any(row.path in selected for row in remaining.files):
                    raise ValueError("操作后仍有相关变更，请刷新检查")
                manifest["status"] = "completed"
                atomic_json(backup / "manifest.json", manifest)
                result = DiscardResult(repo_id, "success", f"已丢弃 {len(rows)} 项改动；备份：{backup}", str(backup))
            except Exception as error:
                manifest["status"] = "partial" if mutating else "aborted"
                manifest["error"] = redact(str(error))
                try:
                    atomic_json(backup / "manifest.json", manifest)
                except OSError:
                    pass
                if not mutating:
                    journal.finish("interrupted")
                    raise
                result = DiscardResult(repo_id, "failed",
                                       f"操作未全部完成，请刷新检查。{redact(str(error))}\n备份：{backup}", str(backup))
            journal.log(repo_id, result.message)
            journal.result(result)
            journal.finish()
            return result
