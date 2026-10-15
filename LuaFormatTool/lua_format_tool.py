"""无参数启动 GUI；scan/format/init-config 子命令用于命令行。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from formatter import (CHOICES, DEFAULT_EXCLUDES, STATUS_LABELS, FileResult, FormatError,
                       FormatSettings, Formatter, apply_result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="递归检查 Lua 格式、解释差异并按自定义规则格式化；无参数打开图形界面")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("gui", help="打开图形界面")
    commands.add_parser("install", help="下载并校验官方 StyLua 2.6.0")
    config = commands.add_parser("init-config", help="生成可编辑的 LuaFormatTool 格式配置")
    config.add_argument("output", type=Path, nargs="?", default=Path("stylua.toml"))
    for command in ("scan", "format"):
        sub = commands.add_parser(command, help="只读扫描" if command == "scan" else "格式化（默认预览，--write 才写文件）")
        sub.add_argument("root", type=Path, help="Lua 文件或扫描目录")
        sub.add_argument("--config", type=Path, help="StyLua / LuaFormatTool TOML 配置文件；未提供时使用工具默认规则")
        sub.add_argument("--stylua", help="自定义 StyLua 可执行文件绝对路径")
        sub.add_argument("--encoding", choices=("auto", "utf-8", "gb18030", "gbk"), default="auto")
        sub.add_argument("--include-luau", action="store_true", help="同时扫描 .luau")
        sub.add_argument("--exclude", action="append", default=[], help="额外排除规则，可重复，如 vendor 或 **/*.d.lua")
        sub.add_argument("--diff", action="store_true", help="输出统一差异")
        sub.add_argument("--json", type=Path, help="导出 JSON 报告")
        for name, choices in CHOICES.items():
            sub.add_argument("--" + name.replace("_", "-"), choices=choices, default=None)
        sub.add_argument("--indent-width", type=int, default=None)
        sub.add_argument("--column-width", type=int, default=None)
        if command == "format":
            sub.add_argument("--write", action="store_true", help="应用预览，每个文件默认创建 .luaformat.bak 备份")
            sub.add_argument("--no-backup", action="store_true", help="写入时不创建备份")
    return parser


def settings_from_args(args: argparse.Namespace) -> FormatSettings:
    settings = FormatSettings.load(args.config) if args.config else FormatSettings()
    data = asdict(settings)
    for name in data:
        override = getattr(args, name, None)
        if override is not None:
            data[name] = override
    return FormatSettings.from_mapping(data)


def print_result(result: FileResult, show_diff: bool) -> None:
    print(f"[{STATUS_LABELS[result.status]}] {result.path}")
    if result.error:
        print(f"  {result.error}")
    for issue in result.issues:
        print(f"  第 {issue.line} 行 [{issue.rule}] {issue.message}")
    if show_diff and result.status == "changed":
        print(result.diff())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in (None, "gui"):
            from gui import run
            run()
            return 0
        if args.command == "install":
            from install_stylua import install
            print(f"已安装：{install()}")
            return 0
        if args.command == "init-config":
            with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(FormatSettings().to_toml())
            print(f"已创建配置：{args.output.resolve()}")
            return 0
        settings = settings_from_args(args)
        with Formatter(settings, args.stylua, args.encoding) as formatter:
            results = formatter.scan(args.root, DEFAULT_EXCLUDES + tuple(args.exclude), args.include_luau)
        protected = {result.path for result in results}
        protected.update(result.path.with_name(result.path.name + ".luaformat.bak") for result in results)
        if args.config:
            protected.add(args.config.resolve())
        if args.json and args.json.resolve() in protected:
            raise FormatError("JSON 报告不能覆盖源文件、备份文件或格式配置")
        for result in results:
            print_result(result, args.diff)
        applied: list[str] = []
        failures: list[dict] = []
        if args.command == "format" and args.write:
            for result in results:
                if result.status == "changed":
                    try:
                        backup = apply_result(result, backup=not args.no_backup)
                        applied.append(str(result.path))
                        print(f"已格式化：{result.path}" + (f"；备份：{backup}" if backup else ""))
                    except (OSError, FormatError) as exc:
                        failures.append({"path": str(result.path), "error": str(exc)})
                        print(f"写入失败：{exc}", file=sys.stderr)
        counts = {status: sum(result.status == status for result in results) for status in STATUS_LABELS}
        print(f"扫描 {len(results)} 个文件；格式正确 {counts['ok']}；格式问题 {counts['changed']}；检查失败 {counts['error']}；已写入 {len(applied)}")
        if args.command == "format" and not args.write:
            print("当前为预览模式；加 --write 应用格式化。")
        if args.json:
            report = {"root": str(args.root.resolve()), "settings": asdict(settings), "counts_before_format": counts,
                      "files": [result.to_dict(args.diff) for result in results],
                      "applied": applied, "write_errors": failures}
            args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if counts["error"] or failures:
            return 2
        if counts["changed"] and not applied:
            return 1
        return 0
    except (OSError, ValueError, FormatError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("操作已停止", file=sys.stderr)
        return 130


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    raise SystemExit(main())
