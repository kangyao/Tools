from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path


BUILD_STEPS = {"setup": "构建设置", "generate": "生成 SLN 工程", "compile": "IncrediBuild 编译"}
CONFIGURATIONS = ("Debug", "Release", "Profile", "EditorDebug", "EditorRelease")
PLATFORMS = ("x64", "Win32")
VS_VERSIONS = ("2019", "2022")
BUILD_ACTIONS = ("Build", "Rebuild", "Clean")
DEFAULT_TARGET = "MiniGameApp"


def task_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class GenerationTask:
    bat: str = "SetupScript/Win_MiniGame_GenerateProject.bat"
    arguments: list[str] = field(default_factory=list)
    build_directory: str = ""
    enabled: bool = True
    id: str = field(default_factory=task_id)


@dataclass
class CompileTask:
    solution: str = ""
    target: str = DEFAULT_TARGET
    configuration: str = "Debug"
    platform: str = "x64"
    action: str = "Build"
    enabled: bool = True
    generation_id: str = ""
    id: str = field(default_factory=task_id)


def default_ib_skill_dir() -> str:
    candidate = Path(__file__).resolve().parents[4] / "mini-compile-ib"
    return str(candidate) if candidate.is_dir() else "W:/git/skills/mini-compile-ib"


def default_generation_environment() -> dict[str, str]:
    # Defaults from Tools/ProjectBat/VS2019_64-MiniGame.bat.
    return {"USE_LUA_JIT": "1", "USE_RAINBOW_LIB": "0", "USE_SANDBOXENGINE_DRIVER_LIB": "0",
            "USE_DEV_BUILD": "1", "USE_UNIVERSE_BUILD": "0", "USE_POCO_BUILD": "1",
            "ADVANCE_BETA_CLIENT_BUILD": "0", "ENABLE_MEMORY_LEAK_CHECK_LOG": "1"}


@dataclass
class BuildOptions:
    steps: list[str] = field(default_factory=lambda: ["compile"])
    python_path: str = "Tools/buildtools/Python39/python.exe"
    setup_install_file: str = ""
    setup_pack_zip: bool = False
    compile_type: str = ""
    generation_environment: dict[str, str] = field(default_factory=dict)
    # "MiniGame" in the generated SLN is only a solution folder; the game executable project is MiniGameApp.
    target: str = DEFAULT_TARGET
    configuration: str = "Debug"
    platform: str = "x64"
    visual_studio_version: str = "2019"
    action: str = "Build"
    ib_skill_dir: str = field(default_factory=default_ib_skill_dir)
    build_console_path: str = ""
    log_directory: str = ""
    open_monitor: bool = False
    timeout_minutes: int = 0
    generation_tasks: list[GenerationTask] = field(default_factory=list)
    compile_tasks: list[CompileTask] = field(default_factory=list)

    @property
    def preset(self) -> str:
        return f"vs{self.visual_studio_version}-{'win64' if self.platform == 'x64' else 'win32'}-MiniGame"

    def solution(self, root: Path) -> Path:
        # Kept for reading pre-task-list profiles; execution uses CompileTask.solution.
        return root / "Projects" / self.preset / "MiniGame.sln"


def parse_build_options(data: dict) -> BuildOptions:
    values = dict(data)
    generations = values.pop("generation_tasks", None)
    compilations = values.pop("compile_tasks", None)
    options = BuildOptions(**values)
    options.generation_tasks = ([GenerationTask(**task) for task in generations] if generations is not None else
                                [GenerationTask(arguments=["--vs-version", options.visual_studio_version,
                                                           "--architecture", "Win64" if options.platform == "x64" else "Win32",
                                                           "--configuration", options.configuration])] if data else [])
    options.compile_tasks = ([CompileTask(**task) for task in compilations] if compilations is not None else
                             [CompileTask(solution=f"Projects/{options.preset}/MiniGame.sln", target=options.target,
                                          configuration=options.configuration, platform=options.platform,
                                          action=options.action)] if data else [])
    if compilations is None and options.generation_tasks and options.compile_tasks:
        options.compile_tasks[0].generation_id = options.generation_tasks[0].id
    return options


def resolve_build_path(root: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def validate_build_options(options: BuildOptions) -> None:
    if not isinstance(options, BuildOptions):
        raise ValueError("编译构建配置格式无效")
    text_fields = ("python_path", "setup_install_file", "compile_type", "target", "configuration", "platform",
                   "visual_studio_version", "action", "ib_skill_dir", "build_console_path", "log_directory")
    if any(not isinstance(getattr(options, key), str) or any(c in getattr(options, key) for c in "\0\r\n")
           for key in text_fields):
        raise ValueError("构建路径和参数必须是单行文本")
    if (not isinstance(options.steps, list) or not options.steps
            or not all(isinstance(step, str) and step in BUILD_STEPS for step in options.steps)
            or len(set(options.steps)) != len(options.steps)):
        raise ValueError("至少选择一个构建步骤，且步骤不能重复")
    for value, allowed, name in [(options.configuration, CONFIGURATIONS, "配置"),
                                 (options.platform, PLATFORMS, "平台"),
                                 (options.visual_studio_version, VS_VERSIONS, "Visual Studio 版本"),
                                 (options.action, BUILD_ACTIONS, "编译动作")]:
        if value not in allowed:
            raise ValueError(f"不支持的{name}：{value}")
    if not options.target.strip() or not re.fullmatch(r"[\w .+\-]+", options.target) or options.target.startswith("-"):
        raise ValueError("编译目标应为 SLN 中的项目名称，不能包含路径或通配符")
    if not options.python_path.strip() or not options.ib_skill_dir.strip():
        raise ValueError("Python 路径和 IB 技能目录不能为空")
    if not isinstance(options.setup_pack_zip, bool) or not isinstance(options.open_monitor, bool):
        raise ValueError("打包依赖和打开 IB 监视器选项必须为布尔值")
    if options.setup_pack_zip and options.setup_install_file:
        raise ValueError("安装指定依赖包和打包依赖不能同时选择")
    if type(options.timeout_minutes) is not int or not 0 <= options.timeout_minutes <= 1440:
        raise ValueError("单个命令超时必须在 0 到 1440 分钟之间；0 表示不限时")
    environment = options.generation_environment
    if not isinstance(environment, dict):
        raise ValueError("生成工程的环境参数必须为键值表")
    reserved = {"pythonhome", "pythonpath", "python_exe", "project_root_dir", "mcomplietype"}
    names = set()
    for key, value in environment.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or key.casefold() in reserved or key.casefold() in names
                or not isinstance(value, str) or any(c in value for c in "\0\r\n")):
            raise ValueError(f"生成环境参数无效、重复或由工具管理：{key}")
        names.add(key.casefold())
    identities = set()
    for tasks, kind in ((options.generation_tasks, GenerationTask), (options.compile_tasks, CompileTask)):
        if not isinstance(tasks, list) or any(not isinstance(task, kind) for task in tasks):
            raise ValueError("构建任务必须是任务列表")
        for task in tasks:
            if (not isinstance(task.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task.id)
                    or task.id in identities or type(task.enabled) is not bool):
                raise ValueError("任务 ID 必须唯一，启用状态必须是布尔值")
            identities.add(task.id)
            if isinstance(task, GenerationTask):
                if (not isinstance(task.arguments, list)
                        or not all(isinstance(arg, str) for arg in task.arguments)):
                    raise ValueError("BAT 参数必须是字符串列表")
                for value in [task.bat, task.build_directory, *task.arguments]:
                    validate_bat_value(value)
                if task.enabled and (not task.bat.strip() or Path(task.bat).suffix.lower() != ".bat"):
                    raise ValueError("请选择 SetupScript 中的 BAT")
                if any(arg.lower() in {"--build-dir", "--dry-run", "--no-pause"} for arg in task.arguments):
                    raise ValueError("输出目录请填写在输出目录列；--dry-run 和 --no-pause 由工具管理")
            else:
                for value in (task.solution, task.target, task.configuration, task.platform, task.action,
                              task.generation_id):
                    if not isinstance(value, str) or any(c in value for c in "\0\r\n"):
                        raise ValueError("编译任务参数必须是单行文本")
                if task.enabled:
                    if not task.solution.strip() or Path(task.solution).suffix.lower() != ".sln":
                        raise ValueError("请为编译任务选择 SLN")
                    if not re.fullmatch(r"[\w .+\-]+", task.target) or task.target.startswith("-"):
                        raise ValueError("请选择 SLN 中的 C++ 编译目标")
                    if (task.configuration not in CONFIGURATIONS or task.platform not in PLATFORMS
                            or task.action not in BUILD_ACTIONS):
                        raise ValueError("编译任务的配置、平台或动作无效")


def validate_bat_value(value: str) -> None:
    # CALL inside the original BAT expands arguments again. Reject shell expansion and control characters.
    if not isinstance(value, str) or any(c in value for c in '\0\r\n"%!^&|<>'):
        raise ValueError("BAT 路径和参数不能包含控制字符、引号或 CMD 特殊字符（% ! ^ & | < >）")


def discover_generation_scripts(root: Path) -> list[Path]:
    folder = root / "SetupScript"
    if not folder.is_dir():
        return []
    return sorted((path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == ".bat"
                   and "generat" in path.stem.lower()), key=lambda path: path.name.lower())


def read_solution_configurations(solution: Path) -> list[tuple[str, str]]:
    text = solution.read_text(encoding="utf-8-sig", errors="replace")
    section = re.search(r"GlobalSection\(SolutionConfigurationPlatforms\)[^\n]*\n(.*?)EndGlobalSection",
                        text, re.S | re.I)
    return list(dict.fromkeys((config.strip(), platform.strip()) for config, platform in
                             re.findall(r"^\s*([^|=\r\n]+)\|([^=\r\n]+)\s*=", section[1], re.M))) if section else []


def read_solution_targets(solution: Path) -> list[str]:
    if not solution.is_file():
        raise ValueError(f"尚未生成解决方案：{solution}")
    text = solution.read_text(encoding="utf-8-sig", errors="replace")
    return sorted(set(re.findall(r'^Project\([^\r\n]+\)\s*=\s*"([^"]+)"\s*,\s*"[^"]+\.vcxproj"',
                                 text, re.MULTILINE | re.IGNORECASE)))


def require_solution_target(solution: Path, target: str) -> None:
    # The IB skill matches project names case-insensitively, so mirror that here.
    targets = read_solution_targets(solution)
    if target.casefold() in {name.casefold() for name in targets}:
        return
    hints = [name for name in targets if target.casefold() in name.casefold()][:5]
    raise ValueError(f"编译目标 {target} 不是 SLN 中的 C++ 工程（可能只是解决方案文件夹）；"
                     + (f"可选：{'、'.join(hints)}" if hints else "请点击“读取 SLN 目标”重新选择"))
