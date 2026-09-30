from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .build_config import BUILD_STEPS, default_generation_environment, resolve_build_path, validate_build_options
from .models import Profile
from .process import ProcessRunner, redact
from .profiles import atomic_json
from .setup import SetupRunner
from .storage import RunJournal, RunLock


HELPERS = Path(__file__).parent / "build_helpers"


@dataclass
class BuildResult:
    repo_id: str
    outcome: str
    message: str
    exit_code: int | None = None
    duration: float = 0
    log_path: str = ""
    session_file: str = ""


def generation_environment(profile: Profile) -> dict[str, str | None]:
    root = Path(profile.root).resolve()
    python = resolve_build_path(root, profile.build.python_path)
    bundled = root / "Tools/buildtools/Python39/python.exe"
    return {**default_generation_environment(),
            **{key.upper(): value for key, value in profile.build.generation_environment.items()},
            "PROJECT_ROOT_DIR": str(root) + "\\",
            "mComplieType": profile.build.compile_type or None, "PYTHON_EXE": str(python),
            "PYTHONHOME": str(python.parent) if python == bundled else None,
            "PYTHONPATH": str(python.parent / "lib") if python == bundled else None}


def build_preview(profile: Profile) -> str:
    options = profile.build
    validate_build_options(options)
    root = Path(profile.root).resolve()
    python = str(resolve_build_path(root, options.python_path))
    commands = {
        "setup": subprocess.list2cmdline([python, *SetupRunner.arguments(profile)]),
        "generate": subprocess.list2cmdline([python, "-u", str(root / "Tools/buildtools/cmake_generate_projects.py"),
                                             options.preset, "dev"]),
        "compile": subprocess.list2cmdline(["powershell.exe", "-NoProfile", "-File",
                    str(resolve_build_path(root, options.ib_skill_dir) / "scripts/invoke-mini-ib-build.ps1"),
                    "-ProjectRoot", str(root), "-Target", options.target, "-Configuration", options.configuration,
                    "-Platform", options.platform, "-VisualStudioVersion", options.visual_studio_version,
                    "-Action", options.action]),
    }
    if options.build_console_path:
        commands["compile"] += " -BuildConsolePath " + subprocess.list2cmdline([
            str(resolve_build_path(root, options.build_console_path))])
    if options.open_monitor:
        commands["compile"] += " -OpenMonitor"
    lines = []
    for step, label in BUILD_STEPS.items():
        if step not in options.steps:
            continue
        cwd = root / "Tools/buildtools" if step == "generate" else root
        lines += [label, f"工作目录：{cwd}", commands[step]]
        if step == "setup":
            lines += ["对应 SetupScript/Win_Setup.bat；缺少内置 Python 时先解压 Python39.zip。"]
        if step == "generate":
            lines += ["对应 Tools/ProjectBat/VS2019_64-MiniGame.bat；不执行末尾 pause。",
                      "环境参数：" + "; ".join(f"{k}={v}" for k, v in generation_environment(profile).items() if v is not None),
                      f"mComplieType={options.compile_type}"]
            if generation_environment(profile).get("USE_RAINBOW_LIB") == "1":
                lines += ["先执行 Tools/Setup/PullEngine.py 获取引擎库，再生成工程。"]
        lines.append("")
    lines += [f"解决方案：{options.solution(root)}",
              "按上述顺序执行；任一步失败或中断即停止后续步骤。检查配置不会执行构建设置、生成或编译。"]
    return redact("\n".join(lines))


class BuildService:
    def __init__(self, data_dir: Path, emit=None, stop=None, cancel=None):
        self.data_dir = Path(data_dir)
        self.emit = emit or (lambda kind, payload: None)
        self.stop = stop or threading.Event()
        self.cancel = cancel or threading.Event()
        self.runner = ProcessRunner(self._log, self.cancel)
        self.journal = None
        self.current = ""
        self.run_dir = None

    def _log(self, line: str):
        line = redact(line)
        if self.journal:
            self.journal.log(self.current, line)
        if self.run_dir and self.run_dir.is_dir():
            with (self.run_dir / "build.log").open("a", encoding="utf-8") as stream:
                stream.write(f"[{self.current or 'build'}] {line}\n")
        self.emit("log", (self.current, line))

    @staticmethod
    def _require(path: Path, description: str):
        if not path.is_file():
            raise ValueError(f"找不到{description}：{path}")

    def _skill(self, profile: Profile, name: str, parameters: dict, *, cleanup=False, on_terminate=None,
               unbounded=False):
        root = Path(profile.root).resolve()
        script = resolve_build_path(root, profile.build.ib_skill_dir) / "scripts" / name
        self._require(script, "IB 技能脚本")
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise ValueError("找不到 Windows PowerShell：powershell.exe")
        request = self.run_dir / (name + ".request.json")
        atomic_json(request, {"Script": str(script), "Parameters": parameters})
        runner = ProcessRunner(self._log) if cleanup else self.runner
        if cleanup:
            timeout = 30
        elif unbounded:
            timeout = None
        else:
            timeout = profile.build.timeout_minutes * 60 or None
        return runner.run(powershell, ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                      str(HELPERS / "invoke_skill.ps1"), "-RequestFile", str(request)],
                          root, timeout=timeout, on_terminate=on_terminate,
                          environment={"PYTHONHOME": None, "PYTHONPATH": None})

    def _setup(self, profile: Profile, validate_only: bool) -> BuildResult:
        root = Path(profile.root).resolve()
        self._require(root / "Tools/Setup/SetupWin.py", "构建设置脚本")
        options = profile.build
        python = resolve_build_path(root, options.python_path)
        if options.setup_install_file:
            self._require(resolve_build_path(root, options.setup_install_file), "依赖安装文件")
        if not python.is_file():
            if python != root / "Tools/buildtools/Python39/python.exe":
                self._require(python, "Python")
            self._require(root / "Tools/7z.exe", "解压工具")
            self._require(root / "Tools/buildtools/Python39.zip", "内置 Python 压缩包")
        if validate_only:
            return BuildResult("setup", "success", "构建设置入口通过检查；未执行设置或解压")
        runner = SetupRunner(self.runner)
        result = runner.prepare(profile)
        return BuildResult("setup", result.outcome, result.message, runner.last_exit_code)

    def _generate(self, profile: Profile, validate_only: bool) -> BuildResult:
        root = Path(profile.root).resolve()
        options = profile.build
        python = resolve_build_path(root, options.python_path)
        script = root / "Tools/buildtools/cmake_generate_projects.py"
        preset = root / "Tools/buildtools/platformconfig/public" / (options.preset + ".xml")
        self._require(python, "Python（请先执行构建设置）")
        self._require(script, "工程生成脚本")
        self._require(preset, "生成预设")
        pull_engine = generation_environment(profile).get("USE_RAINBOW_LIB") == "1"
        if pull_engine:
            self._require(root / "Tools/Setup/PullEngine.py", "引擎库下载脚本")
        if validate_only:
            return BuildResult("generate", "success", f"生成入口和预设 {options.preset} 通过检查；未生成工程")
        timeout = options.timeout_minutes * 60 or None
        environment = generation_environment(profile)
        if pull_engine:
            result = self.runner.run(str(python), ["-u", str(root / "Tools/Setup/PullEngine.py")], root,
                                     timeout=timeout, environment=environment)
            if result.returncode or result.cancelled or result.timed_out:
                return self._process_result("generate", result, "获取引擎库")
        result = self.runner.run(str(python), ["-u", str(HELPERS / "run_generation.py"), str(script), options.preset],
                                 root / "Tools/buildtools", timeout=timeout, environment=environment)
        outcome = self._process_result("generate", result, "生成 SLN 工程")
        if outcome.outcome == "success":
            if not options.solution(root).is_file():
                outcome.outcome = "failed"
                outcome.message = f"生成脚本已退出，但没有找到预期解决方案：{options.solution(root)}"
            else:
                outcome.message = f"工程生成完成：{options.solution(root)}"
        return outcome

    @staticmethod
    def _process_result(step, process, label):
        if process.cancelled:
            return BuildResult(step, "cancelled", label + "已中断", process.returncode)
        if process.timed_out:
            return BuildResult(step, "failed", label + "超时，已请求停止当前会话", process.returncode)
        return BuildResult(step, "success" if process.returncode == 0 else "failed",
                           f"{label}{'完成' if process.returncode == 0 else '失败'}（退出码 {process.returncode}）",
                           process.returncode)

    def _compile(self, profile: Profile, validate_only: bool) -> BuildResult:
        root = Path(profile.root).resolve()
        options = profile.build
        self._require(options.solution(root), "已有 SLN 工程（请先执行生成 SLN 工程）")
        skill = resolve_build_path(root, options.ib_skill_dir) / "scripts"
        for name in ("invoke-mini-ib-build.ps1", "stop-mini-ib-build.ps1", "read-mini-ib-result.ps1"):
            self._require(skill / name, "IB 技能脚本")
        log = self.run_dir / "incredibuild.log"
        session = self.run_dir / "incredibuild.session.json"
        parameters = {"ProjectRoot": str(root), "Target": options.target, "Configuration": options.configuration,
                      "Platform": options.platform, "VisualStudioVersion": options.visual_studio_version,
                      "Action": options.action, "LogPath": str(log), "SessionFile": str(session)}
        if options.build_console_path:
            parameters["BuildConsolePath"] = str(resolve_build_path(root, options.build_console_path))
        if options.open_monitor:
            parameters["OpenMonitor"] = True
        checked = self._skill(profile, "invoke-mini-ib-build.ps1", {**parameters, "ValidateOnly": True})
        if checked.returncode or checked.cancelled or checked.timed_out or validate_only:
            result = self._process_result("compile", checked, "IB 配置检查")
            if result.outcome == "success":
                result.message = "IB 解决方案、目标和入口通过检查；未执行编译"
            return result

        def stop_owned_session():
            # Only the unique session file from this invocation is ever passed to the skill.
            if session.is_file():
                self._log("请求停止本次 IB 会话：" + str(session))
                stopped = self._skill(profile, "stop-mini-ib-build.ps1",
                                      {"SessionFile": str(session), "IncludeWorkers": True, "Confirm": False}, cleanup=True)
                if stopped.returncode:
                    raise RuntimeError("IB 会话停止脚本失败，请查看本次会话日志")

        # A full build routinely outlasts any per-command limit; only cancel stops it.
        process = self._skill(profile, "invoke-mini-ib-build.ps1", parameters, on_terminate=stop_owned_session,
                              unbounded=True)
        result = self._process_result("compile", process, "IB 编译")
        result.log_path, result.session_file = str(log), str(session)
        if not process.cancelled and not process.timed_out:
            if session.is_file():
                recorded = json.loads(session.read_text(encoding="utf-8-sig"))
                code = recorded.get("ExitCode")
                result.exit_code = code if type(code) is int else None
                result.outcome = "success" if type(code) is int and code == 0 and process.returncode == 0 else "failed"
                result.message = f"IB {options.target} {options.configuration}|{options.platform} {options.action}：退出码 {code}"
            else:
                result.outcome = "failed"
                result.message = "IB 未产生本次会话记录，未确认编译成功；详情见日志"
            if log.is_file():
                summary = self._skill(profile, "read-mini-ib-result.ps1", {"LogPath": str(log)}, cleanup=True)
                if summary.returncode:
                    self._log("IB 日志摘要读取失败；编译结果仍以本次 IB 退出码为准")
        return result

    def run(self, profile: Profile, *, validate_only=False) -> dict[str, BuildResult]:
        validate_build_options(profile.build)
        root = Path(profile.root).resolve()
        with RunLock(self.data_dir):
            self.journal = RunJournal(self.data_dir, profile.id, "build-check" if validate_only else "build")
            self.emit("journal", str(self.journal.log_path))
            log_root = (resolve_build_path(root, profile.build.log_directory) if profile.build.log_directory
                        else self.data_dir / "builds")
            self.run_dir = log_root / self.journal.path.stem
            results = {}
            failed = False
            try:
                if not root.is_dir():
                    raise ValueError(f"工程根目录不存在：{root}")
                self.run_dir.mkdir(parents=True, exist_ok=False)
                self.emit("build_directory", str(self.run_dir))
                self._log(build_preview(profile))
                for step in BUILD_STEPS:
                    if step not in profile.build.steps:
                        continue
                    self.current = step
                    started = time.monotonic()
                    if self.stop.is_set() or self.cancel.is_set():
                        result = BuildResult(step, "cancelled", "已停止，未执行此步骤")
                    elif failed and not validate_only:
                        result = BuildResult(step, "skipped", "前一步未成功，未执行此步骤")
                    else:
                        self.emit("build_started", step)
                        try:
                            if not root.is_dir():
                                raise ValueError(f"工程根目录不存在：{root}")
                            result = getattr(self, "_" + step)(profile, validate_only)
                        except (OSError, ValueError, RuntimeError) as error:
                            result = BuildResult(step, "failed", redact(str(error)))
                    result.duration = round(time.monotonic() - started, 2)
                    if not result.log_path:
                        result.log_path = str(self.run_dir / "build.log")
                    results[step] = result
                    failed |= result.outcome != "success"
                    self._log(result.message)
                    self.journal.result(result)
                    self.emit("build_result", result)
                self.journal.finish("failed" if failed else "finished")
                return results
            except BaseException:
                self.journal.finish("interrupted")
                raise
            finally:
                self.journal = None
