from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


BUILD_STEPS = {"setup": "构建设置", "generate": "生成 SLN 工程", "compile": "IncrediBuild 编译"}
CONFIGURATIONS = ("Debug", "Release", "Profile", "EditorDebug", "EditorRelease")
PLATFORMS = ("x64", "Win32")
VS_VERSIONS = ("2019", "2022")
BUILD_ACTIONS = ("Build", "Rebuild", "Clean")


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
    generation_environment: dict[str, str] = field(default_factory=default_generation_environment)
    target: str = "MiniGame"
    configuration: str = "Debug"
    platform: str = "x64"
    visual_studio_version: str = "2019"
    action: str = "Build"
    ib_skill_dir: str = "W:/git/skills/mini-compile-ib"
    build_console_path: str = ""
    log_directory: str = ""
    open_monitor: bool = False
    timeout_minutes: int = 0

    @property
    def preset(self) -> str:
        return f"vs{self.visual_studio_version}-{'win64' if self.platform == 'x64' else 'win32'}-MiniGame"

    def solution(self, root: Path) -> Path:
        return root / "Projects" / self.preset / "MiniGame.sln"


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


def read_solution_targets(solution: Path) -> list[str]:
    if not solution.is_file():
        raise ValueError(f"尚未生成解决方案：{solution}")
    text = solution.read_text(encoding="utf-8-sig", errors="replace")
    return sorted(set(re.findall(r'^Project\([^\r\n]+\)\s*=\s*"([^"]+)"\s*,\s*"[^"]+\.vcxproj"',
                                 text, re.MULTILINE | re.IGNORECASE)))
