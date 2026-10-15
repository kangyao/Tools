"""Lua 格式检查核心：StyLua 解析、差异解释和带备份的写入。"""

from __future__ import annotations

import codecs
import difflib
import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable, Iterator


APP_DIR = Path(__file__).resolve().parent
DEFAULT_EXCLUDES = (".git", ".svn", ".venv", "node_modules", "__pycache__")
STATUS_LABELS = {"ok": "格式正确", "changed": "格式问题", "error": "检查失败"}
CHOICES = {
    "syntax": ("All", "Lua51", "Lua52", "Lua53", "Lua54", "LuaJIT", "Luau", "CfxLua"),
    "indent_type": ("Spaces", "Tabs"),
    "line_endings": ("Unix", "Windows", "Preserve"),
    "final_newline": ("Always", "Preserve"),
    "quote_style": ("AutoPreferDouble", "AutoPreferSingle", "ForceDouble", "ForceSingle"),
    "call_parentheses": ("Always", "NoSingleString", "NoSingleTable", "None", "Input"),
    "collapse_simple_statement": ("Never", "Input", "FunctionOnly", "ConditionalOnly", "Always"),
    "space_after_function_names": ("Never", "Definitions", "Calls", "Always"),
    "block_newline_gaps": ("Never", "Preserve"),
}

# GUI 使用清晰的中文说明，保存配置与命令行仍使用稳定的枚举值。
OPTION_LABELS = {
    "line_endings": {"Preserve": "不处理（保留原换行符）"},
    "final_newline": {"Always": "补齐末尾换行", "Preserve": "不处理（保留末尾换行状态）"},
    "call_parentheses": {"Input": "不处理（保留原括号）"},
    "collapse_simple_statement": {"Input": "不处理（保留原合并方式）"},
    "block_newline_gaps": {"Preserve": "不处理（保留原空行）"},
}


class FormatError(RuntimeError):
    pass


@dataclass(frozen=True)
class FormatSettings:
    syntax: str = "Lua54"
    indent_type: str = "Spaces"
    indent_width: int = 4
    column_width: int = 120
    line_endings: str = "Unix"
    final_newline: str = "Always"
    quote_style: str = "AutoPreferDouble"
    call_parentheses: str = "Always"
    collapse_simple_statement: str = "Never"
    space_after_function_names: str = "Never"
    block_newline_gaps: str = "Never"

    def validate(self) -> None:
        for name, choices in CHOICES.items():
            if getattr(self, name) not in choices:
                raise FormatError(f"无效的 {name}：{getattr(self, name)!r}；可选：{', '.join(choices)}")
        for name, low, high in (("indent_width", 1, 16), ("column_width", 20, 1000)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise FormatError(f"{name} 必须是 {low}～{high} 的整数")

    def to_toml(self, *, for_stylua: bool = False) -> str:
        self.validate()
        data = asdict(self)
        data.pop("final_newline")
        if self.line_endings == "Preserve":
            data["line_endings"] = "Unix"
        lines = ["# LuaFormatTool 格式规则"]
        lines.extend(f"{name} = {json.dumps(value)}" for name, value in data.items())
        lines.extend(("", "[sort_requires]", "enabled = false", ""))
        if not for_stylua and (self.line_endings == "Preserve" or self.final_newline == "Preserve"):
            lines.extend(("# 工具扩展配置；请通过 LuaFormatTool 使用本文件。", "[lua_format_tool]"))
            if self.line_endings == "Preserve":
                lines.append('line_endings = "Preserve"')
            if self.final_newline == "Preserve":
                lines.append('final_newline = "Preserve"')
            lines.append("")
        return "\n".join(lines)

    @classmethod
    def from_mapping(cls, data: dict) -> FormatSettings:
        known = {item.name for item in fields(cls)}
        unknown = set(data) - known - {"sort_requires", "lua_format_tool"}
        if unknown:
            raise FormatError(f"不支持的规则：{', '.join(sorted(unknown))}")
        sort = data.get("sort_requires", {})
        if not isinstance(sort, dict) or set(sort) - {"enabled"} or sort.get("enabled", False) is not False:
            raise FormatError("本工具不执行 require 排序，请设置 sort_requires.enabled = false")
        extensions = data.get("lua_format_tool", {})
        if not isinstance(extensions, dict) or set(extensions) - {"line_endings", "final_newline"}:
            raise FormatError("lua_format_tool 只支持 line_endings 和 final_newline")
        values = {name: value for name, value in data.items() if name in known}
        values.update(extensions)
        result = cls(**values)
        result.validate()
        return result

    @classmethod
    def load(cls, path: Path) -> FormatSettings:
        try:
            # 导入 StyLua 文件时，缺省值遵循 StyLua，而新建工具配置默认使用空格/Lua54。
            defaults = asdict(cls(syntax="All", indent_type="Tabs"))
            defaults.update(tomllib.loads(path.read_text(encoding="utf-8-sig")))
            return cls.from_mapping(defaults)
        except (OSError, ValueError, TypeError) as exc:
            raise FormatError(f"读取配置失败：{path}：{exc}") from exc


@dataclass
class Issue:
    line: int
    rule: str
    message: str
    before: str = ""
    after: str = ""


@dataclass
class FileResult:
    path: Path
    status: str
    issues: list[Issue] = field(default_factory=list)
    error: str = ""
    encoding: str = ""
    original: bytes = field(default=b"", repr=False)
    formatted: bytes = field(default=b"", repr=False)
    source: str = field(default="", repr=False)
    preview: str = field(default="", repr=False)

    def diff(self) -> str:
        chunks = difflib.unified_diff(
            self.source.splitlines(keepends=True), self.preview.splitlines(keepends=True),
            fromfile=str(self.path), tofile=f"{self.path} (格式化后)",
        )
        output = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                         for line in chunks)
        if self.status == "changed" and not output:
            return "仅换行符或文件末尾换行发生变化，请查看问题说明。"
        return output

    def to_dict(self, include_diff: bool = False) -> dict:
        data = {"path": str(self.path), "status": self.status, "encoding": self.encoding,
                "issues": [asdict(issue) for issue in self.issues], "error": self.error}
        if include_diff:
            data["diff"] = self.diff()
        return data


def resolve_stylua(explicit: str | None = None) -> Path:
    name = "stylua.exe" if os.name == "nt" else "stylua"
    candidates = [explicit] if explicit else [str(APP_DIR / "bin" / name), shutil.which("stylua")]
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path.resolve()
    raise FormatError("未找到 StyLua。请点击“安装 StyLua”，运行 python install_stylua.py，或指定可执行文件路径。")


def decode_source(raw: bytes, encoding: str = "auto") -> tuple[str, str, bytes]:
    for preamble, codec in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                            (codecs.BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(preamble):
            return raw[len(preamble):].decode(codec), codec, preamble
    candidates = ("utf-8", "gb18030") if encoding == "auto" else (encoding,)
    for codec in candidates:
        try:
            return raw.decode(codec), codec, b""
        except UnicodeDecodeError:
            continue
    raise FormatError(f"无法按 {encoding} 解码文件，请选择正确的源文件编码")


def explain_changes(source: str, preview: str, settings: FormatSettings) -> list[Issue]:
    """只解释实际变化的区域；布局/引号分类是差异推断，不执行 Lua 语义检查。"""
    issues: list[Issue] = []
    expected_eol = "\r\n" if settings.line_endings == "Windows" else "\n"
    eols = list(re.finditer(r"\r\n|\r|\n", source))
    wrong_eol = next((match for match in eols if match.group() != expected_eol), None)
    wrong_before = sum(match.group() != expected_eol for match in eols)
    wrong_after = sum(match.group() != expected_eol for match in re.finditer(r"\r\n|\r|\n", preview))
    if settings.line_endings != "Preserve" and wrong_eol and wrong_before > wrong_after:
        line = len(re.findall(r"\r\n|\r|\n", source[:wrong_eol.start()])) + 1
        issues.append(Issue(line, "LINE_ENDINGS", f"换行符与规则不同，目标为 {'CRLF' if expected_eol == chr(13) + chr(10) else 'LF'}"))
    if settings.final_newline != "Preserve" and source and not source.endswith(("\n", "\r")) and preview.endswith(("\n", "\r")):
        issues.append(Issue(len(source.splitlines()), "FINAL_NEWLINE", "文件末尾缺少换行"))
    before_lines, after_lines = source.splitlines(), preview.splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, start, end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        old, new = before_lines[start:end], after_lines[new_start:new_end]
        messages: list[tuple[str, str]] = []
        old_trailing = sum(len(line) - len(line.rstrip(" \t")) for line in old)
        new_trailing = sum(len(line) - len(line.rstrip(" \t")) for line in new)
        if old_trailing > new_trailing:
            messages.append(("TRAILING_WHITESPACE", "改动区域的行尾空格或 Tab 需要清理"))
        if len(old) == len(new):
            for left, right in zip(old, new):
                old_indent = left[:len(left) - len(left.lstrip(" \t"))]
                new_indent = right[:len(right) - len(right.lstrip(" \t"))]
                if left.strip() and right.strip() and old_indent != new_indent:
                    unit = f"{settings.indent_width} 个空格" if settings.indent_type == "Spaces" else "Tab"
                    messages.append(("INDENTATION", f"缩进与语法层级不符，当前规则每级使用 {unit}"))
                    break
        left, right = "\n".join(old), "\n".join(new)
        if re.sub(r"\s+", "", left) == re.sub(r"\s+", "", right) and left != right:
            messages.append(("SPACING", "根据差异判断，运算符、逗号或其他位置的空白/换行需要统一"))
        elif ("'" in left or '"' in left) and (left.count("'") != right.count("'") or left.count('"') != right.count('"')):
            messages.append(("QUOTES", f"根据差异判断，字符串引号或转义需要统一，当前规则为 {settings.quote_style}"))
        if len(old) != len(new):
            messages.append(("LAYOUT", f"语句、表或空行布局需要调整；参考行宽为 {settings.column_width}（并非硬性上限）"))
        if not messages:
            messages.append(("STYLE", "与当前格式规则不同，请对照修改预览查看括号、标点和语句布局"))
        for rule, message in dict.fromkeys(messages):
            issues.append(Issue(start + 1, rule, message, left, right))
    return issues


def preserve_newlines(source: str, preview: str, settings: FormatSettings) -> str:
    """保留原行的换行符；重排产生的新行沿用相邻源行的换行符。"""
    def split(text: str) -> list[tuple[str, str]]:
        # 只把 CR/LF 当成物理换行，避免拆开字符串中的 Unicode 分隔符。
        lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", text)
        return [(line.rstrip("\r\n"), line[len(line.rstrip("\r\n")):]) for line in lines if line]

    before, after = split(source), split(preview)
    if settings.line_endings == "Preserve":
        endings = [ending for _, ending in before if ending]
        fallback = max(dict.fromkeys(endings), key=endings.count) if endings else "\n"
        matcher = difflib.SequenceMatcher(a=[text for text, _ in before],
                                          b=[text for text, _ in after], autojunk=False)
        restored = []
        for tag, start, end, new_start, new_end in matcher.get_opcodes():
            for offset, index in enumerate(range(new_start, new_end)):
                if start < end:
                    original_ending = before[min(start + offset, end - 1)][1]
                else:
                    neighbor = start - 1 if start else start
                    original_ending = before[neighbor][1] if before else ""
                text, ending = after[index]
                restored.append(text + ((original_ending or fallback) if ending else ""))
        preview = "".join(restored)
    if settings.final_newline == "Preserve":
        old_ending = before[-1][1] if before else ""
        new_ending = re.search(r"(\r\n|\r|\n)$", preview)
        if old_ending and not new_ending:
            preview += old_ending if settings.line_endings == "Preserve" else ("\r\n" if settings.line_endings == "Windows" else "\n")
        elif not old_ending and new_ending:
            preview = preview[:new_ending.start()]
    return preview


def is_excluded(relative: Path, patterns: tuple[str, ...]) -> bool:
    value = relative.as_posix()
    for pattern in patterns:
        pattern = pattern.replace("\\", "/").strip().rstrip("/")
        if pattern and (fnmatch.fnmatchcase(value, pattern) or
                        (pattern.startswith("**/") and fnmatch.fnmatchcase(value, pattern[3:])) or
                        ("/" not in pattern and any(fnmatch.fnmatchcase(part, pattern) for part in relative.parts))):
            return True
    return False


def iter_lua_files(root: Path, excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
                   include_luau: bool = False, cancel: threading.Event | None = None) -> Iterator[Path]:
    if root.is_symlink() or root.is_junction():
        raise FormatError("扫描根路径不能是符号链接或目录联接")
    root = root.resolve()
    suffixes = {".lua", ".luau"} if include_luau else {".lua"}
    if root.is_file():
        if root.suffix.lower() not in suffixes:
            raise FormatError("请选择 .lua 文件或目录（.luau 需要启用 Luau 文件选项）")
        yield root
        return
    if not root.is_dir():
        raise FormatError(f"扫描路径不存在：{root}")
    def on_error(exc: OSError) -> None:
        raise FormatError(f"无法遍历目录：{exc}")
    for directory, folders, files in os.walk(root, followlinks=False, onerror=on_error):
        if cancel and cancel.is_set():
            return
        base = Path(directory)
        folders[:] = sorted(name for name in folders if not (base / name).is_symlink()
                            and not (base / name).is_junction()
                            and not is_excluded((base / name).relative_to(root), excludes))
        for name in sorted(files):
            path = base / name
            if path.suffix.lower() in suffixes and not path.is_symlink() and not is_excluded(path.relative_to(root), excludes):
                yield path


class Formatter:
    def __init__(self, settings: FormatSettings, stylua: str | None = None, encoding: str = "auto"):
        settings.validate()
        self.settings, self.encoding = settings, encoding
        self.executable = resolve_stylua(stylua)
        self._temp = tempfile.TemporaryDirectory(prefix="lua-format-")
        self.config = Path(self._temp.name) / "stylua.toml"
        self.config.write_text(settings.to_toml(for_stylua=True), encoding="utf-8", newline="\n")

    def __enter__(self) -> Formatter:
        return self

    def __exit__(self, *args) -> None:
        self._temp.cleanup()

    def check(self, path: Path) -> FileResult:
        original, source, codec = b"", "", ""
        try:
            if path.is_symlink():
                raise FormatError("跳过符号链接文件")
            original = path.read_bytes()
            source, codec, preamble = decode_source(original, self.encoding)
            if "\0" in source:
                raise FormatError("文件包含 NUL 字符，可能是二进制或编码选择错误")
            # 使用一致的换行进行 AST 校验，再恢复原有换行；否则 StyLua 的
            # --verify 会把长字符串中的 CRLF/LF 转换判成 AST 差异。
            stdin_source = source.replace("\r\n", "\n").replace("\r", "\n") if self.settings.line_endings == "Preserve" else source
            process = subprocess.run(
                [str(self.executable), "--verify", "--num-threads", "1", "--no-editorconfig", "--config-path", str(self.config),
                 "--stdin-filepath", str(path.resolve()), "-"],
                input=stdin_source.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if process.returncode != 0:
                detail = process.stderr.decode("utf-8", errors="replace").strip()
                raise FormatError(detail or f"StyLua 返回错误码 {process.returncode}")
            preview = process.stdout.decode("utf-8")
            if self.settings.line_endings == "Preserve" or self.settings.final_newline == "Preserve":
                preview = preserve_newlines(source, preview, self.settings)
            formatted = preamble + preview.encode(codec)
            changed = original != formatted
            return FileResult(path.resolve(), "changed" if changed else "ok",
                              explain_changes(source, preview, self.settings) if changed else [],
                              encoding=codec, original=original, formatted=formatted, source=source, preview=preview)
        except (OSError, UnicodeError, LookupError, FormatError, subprocess.SubprocessError) as exc:
            return FileResult(path.absolute(), "error", error=str(exc), encoding=codec,
                              original=original, source=source)

    def scan(self, root: Path, excludes: tuple[str, ...] = DEFAULT_EXCLUDES, include_luau: bool = False,
             cancel: threading.Event | None = None,
             on_result: Callable[[FileResult], None] | None = None) -> list[FileResult]:
        results: list[FileResult] = []
        for path in iter_lua_files(root, excludes, include_luau, cancel):
            if cancel and cancel.is_set():
                break
            result = self.check(path)
            results.append(result)
            if on_result:
                on_result(result)
        return results


def apply_result(result: FileResult, backup: bool = True) -> Path | None:
    """写入扫描时生成的预览；源文件改变或已有不同备份时拒绝覆盖。"""
    if result.status != "changed":
        raise FormatError("只能格式化已通过解析且存在格式问题的文件")
    path = result.path
    if path.is_symlink() or path.resolve() != path.absolute():
        raise FormatError(f"路径已变化或成为符号链接，请重新扫描：{path}")
    if path.read_bytes() != result.original:
        raise FormatError(f"文件在扫描后已被修改，请重新扫描：{path}")
    file_stat = path.stat()
    if not file_stat.st_mode & stat.S_IWUSR:
        raise FormatError(f"文件只读：{path}")
    backup_path = path.with_name(path.name + ".luaformat.bak") if backup else None
    if backup_path:
        try:
            with backup_path.open("xb") as stream:
                stream.write(result.original)
        except FileExistsError:
            if backup_path.is_symlink() or backup_path.read_bytes() != result.original:
                raise FormatError(f"已有不同内容的备份，请先移走备份：{backup_path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(result.formatted)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(file_stat.st_mode))
        if path.is_symlink() or path.resolve() != path.absolute() or path.read_bytes() != result.original:
            raise FormatError(f"写入前源文件发生变化，请重新扫描：{path}")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup_path
