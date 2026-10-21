from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .build_config import (BUILD_STEPS, CompileTask, GenerationTask, read_solution_configurations,
                           require_solution_target, resolve_build_path, validate_build_options)
from .build_tasks import generated_compile_tasks, generation_command, merge_generated_tasks
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
    step: str = ""
    tasks: list[BuildResult] = field(default_factory=list)


def generation_environment(profile: Profile) -> dict[str, str | None]:
    root = Path(profile.root).resolve()
    python = resolve_build_path(root, profile.build.python_path)
    bundled = root / "Tools/buildtools/Python39/python.exe"
    return {**{key.upper(): value for key, value in profile.build.generation_environment.items()},
            "PROJECT_ROOT_DIR": str(root) + "\\",
            "mComplieType": profile.build.compile_type or None, "PYTHON_EXE": str(python),
            "PYTHONHOME": str(python.parent) if python == bundled else None,
            "PYTHONPATH": str(python.parent / "lib") if python == bundled else None}


def build_preview(profile: Profile) -> str:
    options = profile.build
    validate_build_options(options)
    root = Path(profile.root).resolve()
    python = str(resolve_build_path(root, options.python_path))
    lines = []
    for step, label in BUILD_STEPS.items():
        if step not in options.steps:
            continue
        lines += [label]
        if step == "setup":
            lines += [subprocess.list2cmdline([python, *SetupRunner.arguments(profile)]),
                      "对应 SetupScript/Win_Setup.bat；缺少内置 Python 时先解压 Python39.zip。"]
        if step == "generate":
            for task in options.generation_tasks:
                if task.enabled:
                    arguments = [str(resolve_build_path(root, task.bat)), *task.arguments, "--no-pause"]
                    if task.build_directory:
                        arguments += ["--build-dir", str(resolve_build_path(root, task.build_directory))]
                    lines += [subprocess.list2cmdline(arguments)]
            lines += ["成功后将本次生成的 SLN 加入编译列表。"]
        if step == "compile":
            lines += [f"{task.solution} → {task.target} {task.configuration}|{task.platform} {task.action}"
                      for task in options.compile_tasks if task.enabled]
            if "generate" in options.steps:
                lines += ["包含本次生成后加入的编译任务。"]
        lines.append("")
    lines += ["按设置 → 生成任务 → 编译任务顺序执行，失败或停止后不再启动后续任务。",
              "检查配置不会执行构建设置、生成或编译；编译使用独立的 IB 会话和日志。"]
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
        self.task_dir = None

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
        request = (self.task_dir or self.run_dir) / (name + ".request.json")
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

    def _run_tasks(self, profile: Profile, step: str, tasks: list, operation, validate_only: bool) -> BuildResult:
        selected = [task for task in tasks if task.enabled]
        if not selected:
            raise ValueError("请先添加并勾选生成 BAT 任务" if step == "generate" else
                             "请先添加并勾选 SLN 编译任务，或先执行生成")
        results = []
        failed = False
        for index, task in enumerate(selected, 1):
            self.task_dir = self.run_dir / f"{step}-{index:02d}-{task.id}"
            self.task_dir.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            if self.stop.is_set() or self.cancel.is_set():
                result = BuildResult(task.id, "cancelled", "已停止，未执行此任务")
            elif failed and not validate_only:
                result = BuildResult(task.id, "skipped", "前一任务未成功，未执行此任务")
            else:
                self.emit("build_task_started", (step, task.id))
                label = task.bat if step == "generate" else task.solution
                self._log(f"[{index}/{len(selected)}] {label}")
                try:
                    result = operation(profile, task, validate_only)
                except (OSError, ValueError, RuntimeError) as error:
                    result = BuildResult(task.id, "failed", redact(str(error)))
            result.repo_id, result.step = task.id, step
            result.duration = round(time.monotonic() - started, 2)
            result.log_path = result.log_path or str(self.run_dir / "build.log")
            failed |= result.outcome != "success"
            results.append(result)
            self._log(result.message)
            self.emit("build_task_result", result)
        self.task_dir = None
        unsuccessful = next((result for result in results if result.outcome != "success"), None)
        last = unsuccessful or results[-1]
        return BuildResult(step, unsuccessful.outcome if unsuccessful else "success",
                           f"{sum(item.outcome == 'success' for item in results)}/{len(results)} 项成功；{last.message}",
                           last.exit_code, log_path=last.log_path, session_file=last.session_file, tasks=results)

    def _generate(self, profile: Profile, validate_only: bool) -> BuildResult:
        root = Path(profile.root).resolve()
        destinations = set()
        for task in profile.build.generation_tasks:
            if not task.enabled:
                continue
            key = ("directory", str(resolve_build_path(root, task.build_directory)).casefold()) if task.build_directory else (
                task.bat.casefold(), tuple(task.arguments))
            if key in destinations:
                raise ValueError("多条生成任务指向相同输出；请为复制的任务设置不同参数或独立输出目录")
            destinations.add(key)
        return self._run_tasks(profile, "generate", profile.build.generation_tasks, self._generate_task, validate_only)

    def _generate_task(self, profile: Profile, task: GenerationTask, validate_only: bool) -> BuildResult:
        root = Path(profile.root).resolve()
        options = profile.build
        arguments, environment = generation_command(root, task, self.task_dir)
        if validate_only:
            return BuildResult(task.id, "success", f"{Path(task.bat).name} 入口和参数通过检查；未生成工程")
        command = shutil.which("cmd.exe")
        if not command:
            raise ValueError("找不到 Windows 命令解释器 cmd.exe")
        result = self.runner.run(command, arguments, self.task_dir, timeout=options.timeout_minutes * 60 or None,
                                 environment={**generation_environment(profile), **environment})
        outcome = self._process_result(task.id, result, Path(task.bat).name)
        if outcome.outcome == "success":
            generated = generated_compile_tasks(root, task, result.output)
            options.compile_tasks = merge_generated_tasks(root, options.compile_tasks, task.id, generated)
            self.emit("build_compile_tasks", options.compile_tasks)
            outcome.message = f"{Path(task.bat).name} 完成，已加入 {len(generated)} 个 SLN 编译任务"
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
        return self._run_tasks(profile, "compile", profile.build.compile_tasks, self._compile_task, validate_only)

    def _compile_task(self, profile: Profile, task: CompileTask, validate_only: bool) -> BuildResult:
        root = Path(profile.root).resolve()
        options = profile.build
        solution = resolve_build_path(root, task.solution)
        self._require(solution, "已有 SLN 工程（请先执行生成 SLN 工程）")
        require_solution_target(solution, task.target)
        pairs = read_solution_configurations(solution)
        if pairs and (task.configuration, task.platform) not in pairs:
            raise ValueError(f"{solution.name} 不支持 {task.configuration}|{task.platform}")
        skill = resolve_build_path(root, options.ib_skill_dir) / "scripts"
        for name in ("invoke-mini-ib-build.ps1", "stop-mini-ib-build.ps1", "read-mini-ib-result.ps1"):
            self._require(skill / name, "IB 技能脚本")
        log = self.task_dir / "incredibuild.log"
        session = self.task_dir / "incredibuild.session.json"
        parameters = {"ProjectRoot": str(root), "SolutionPath": str(solution), "Target": task.target,
                      "Configuration": task.configuration, "Platform": task.platform,
                      "Action": task.action, "LogPath": str(log), "SessionFile": str(session)}
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
                result.message = f"IB {solution.name} / {task.target} {task.configuration}|{task.platform} {task.action}：退出码 {code}"
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
        with RunLock(self.data_dir, profile.root):
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
