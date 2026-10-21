from __future__ import annotations

import ctypes
import json
import os
import re
import shlex
from pathlib import Path

from .build_config import (DEFAULT_TARGET, CompileTask, GenerationTask, read_solution_configurations,
                           read_solution_targets, resolve_build_path, validate_bat_value)


def split_arguments(text: str) -> list[str]:
    """Parse an argument list, never a CMD command. Quoted Windows paths stay intact."""
    if not text.strip():
        return []
    if os.name != "nt":
        return shlex.split(text)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW("task.exe " + text, ctypes.byref(count))
    if not argv:
        raise ValueError("无法解析 BAT 参数")
    try:
        return [argv[index] for index in range(1, count.value)]
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def generation_command(root: Path, task: GenerationTask, folder: Path) -> tuple[list[str], dict[str, str]]:
    bat = resolve_build_path(root, task.bat)
    if not bat.is_relative_to((root / "SetupScript").resolve()) or bat.suffix.lower() != ".bat":
        raise ValueError("生成入口必须是当前工程 SetupScript 目录中的 BAT")
    if not bat.is_file():
        raise ValueError(f"找不到生成 BAT：{bat}")
    arguments = [*task.arguments, "--no-pause"]
    if task.build_directory:
        arguments += ["--build-dir", str(resolve_build_path(root, task.build_directory))]
    for value in [str(bat), str(root), *arguments]:
        validate_bat_value(value)
    # Use a fixed, ASCII wrapper and quoted environment substitutions. No user text becomes CMD source.
    # Tail-calling the BAT preserves its exit code without CALL's extra expansion in this wrapper.
    wrapper = ('@echo off\nsetlocal DisableDelayedExpansion\ncd /d "%REPO_TOOL_WORKSPACE%"\n'
               'if errorlevel 1 exit /b 1\n"%REPO_TOOL_BAT%" '
               + " ".join(f'"%REPO_TOOL_ARG_{index}%"' for index in range(len(arguments))) + "\n")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "generate.bat").write_text(wrapper, encoding="ascii", newline="\r\n")
    environment = {"REPO_TOOL_WORKSPACE": str(root), "REPO_TOOL_BAT": str(bat), "CI": "1"}
    environment.update({f"REPO_TOOL_ARG_{index}": value for index, value in enumerate(arguments)})
    return ["/d", "/c", "generate.bat"], environment


def read_product_metadata(solution: Path) -> dict:
    path = solution.parent / "MINIW_PRODUCT.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"工程元数据格式无效：{path}")
    return value


def compile_task_for_solution(solution: Path, *, metadata: dict | None = None, generation_id="") -> CompileTask:
    metadata = read_product_metadata(solution) if metadata is None else metadata
    targets = read_solution_targets(solution)
    target = metadata.get("primaryTarget", "")
    if target not in targets:
        target = (DEFAULT_TARGET if DEFAULT_TARGET in targets else solution.stem if solution.stem in targets
                  else targets[0] if len(targets) == 1 else "")
    pairs = read_solution_configurations(solution)
    configuration = metadata.get("configuration", "Debug")
    platform = metadata.get("buildPlatform", "x64")
    if pairs and (configuration, platform) not in pairs:
        configuration, platform = pairs[0]
    return CompileTask(solution=str(solution.resolve()), target=target, configuration=configuration,
                       platform=platform, generation_id=generation_id, enabled=bool(target))


def generated_compile_tasks(root: Path, task: GenerationTask, output: str) -> list[CompileTask]:
    candidates: list[dict] = []

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            candidates.append({"solution": value})
        elif isinstance(value, dict):
            if isinstance(value.get("solution"), str):
                candidates.append(value)
            elif isinstance(value.get("solutions"), list):
                collect(value["solutions"])

    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^[ \t]*(?=[\[{])", output):
        try:
            value, _ = decoder.raw_decode(output[match.end():])
            collect(value)
        except json.JSONDecodeError:
            continue
    if not candidates and task.build_directory:
        folder = resolve_build_path(root, task.build_directory)
        metadata_files = sorted(folder.rglob("MINIW_PRODUCT.json")) if folder.is_dir() else []
        for path in metadata_files:
            collect(json.loads(path.read_text(encoding="utf-8-sig")))
        if not candidates:
            collect([str(path) for path in sorted(folder.rglob("*.sln"))] if folder.is_dir() else [])
    tasks = {}
    for metadata in candidates:
        solution = resolve_build_path(root, metadata["solution"])
        if solution.suffix.lower() != ".sln" or not solution.is_file():
            raise ValueError(f"生成 BAT 已退出，但没有找到返回的 SLN：{solution}")
        # Prefer the published metadata over a partial console object.
        published = read_product_metadata(solution)
        if published:
            if resolve_build_path(root, published.get("solution", "")) != solution:
                raise ValueError(f"生成元数据与 SLN 不匹配：{solution}")
            metadata = published
        tasks[str(solution).casefold()] = compile_task_for_solution(solution, metadata=metadata,
                                                                  generation_id=task.id)
    if not tasks:
        raise ValueError("生成 BAT 未返回 SLN 元数据；请在任务的输出目录列指定本次生成目录")
    return list(tasks.values())


def merge_generated_tasks(root: Path, existing: list[CompileTask], generation_id: str,
                          generated: list[CompileTask]) -> list[CompileTask]:
    old = {str(resolve_build_path(root, task.solution)).casefold(): task for task in existing
           if task.generation_id == generation_id}
    for task in generated:
        previous = old.get(str(Path(task.solution).resolve()).casefold())
        if previous:
            task.id, task.enabled, task.action = previous.id, previous.enabled, previous.action
            task.target, task.configuration, task.platform = previous.target, previous.configuration, previous.platform
    merged = []
    inserted = False
    for task in existing:
        if task.generation_id == generation_id:
            if not inserted:
                merged.extend(generated)
                inserted = True
        else:
            merged.append(task)
    return merged if inserted else merged + generated
