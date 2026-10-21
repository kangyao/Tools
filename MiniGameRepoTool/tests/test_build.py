import json
import os
import sys
import threading
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from repo_tool.build import BuildService, build_preview, generation_environment
from repo_tool.build_config import (BuildOptions, CompileTask, GenerationTask, discover_generation_scripts,
                                    read_solution_targets, validate_build_options)
from repo_tool.build_tasks import generated_compile_tasks, split_arguments
from repo_tool.models import Profile, ProfileDocument, RepoSpec
from repo_tool.profiles import ProfileStore, parse_document
from repo_tool.storage import RunLock


SOLUTION = ('Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "MiniGame", "MiniGame", "{FOLDER}"\nEndProject\n'
            'Project("{GUID}") = "MiniGameApp", "MiniGameApp.vcxproj", "{ID}"\nEndProject\n'
            'Global\nGlobalSection(SolutionConfigurationPlatforms) = preSolution\n'
            'Debug|x64 = Debug|x64\nRelease|x64 = Release|x64\nEndGlobalSection\nEndGlobal\n')


@pytest.fixture
def build_profile(tmp_path):
    root = tmp_path / "工程 with spaces"
    setup = root / "Tools/Setup/SetupWin.py"
    setup.parent.mkdir(parents=True)
    setup.write_text("from pathlib import Path\nPath('setup-ran.txt').write_text('ok')\n", encoding="utf-8")
    scripts = root / "SetupScript"
    scripts.mkdir()
    generator = scripts / "generate.py"
    generator.write_text('''
import argparse, json, os, sys
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--product', default='MiniGame')
p.add_argument('--build-dir')
p.add_argument('--count', type=int, default=1)
p.add_argument('--fail-code', type=int, default=0)
p.add_argument('--no-json', action='store_true')
p.add_argument('--no-pause', action='store_true')
p.add_argument('--label')
a = p.parse_args()
assert Path.cwd() == Path(__file__).resolve().parents[1]
assert os.environ['CI'] == '1' and a.no_pause
calls = Path('generation-calls.json')
history = json.loads(calls.read_text(encoding='utf-8')) if calls.exists() else []
history.append(vars(a))
calls.write_text(json.dumps(history), encoding='utf-8')
if a.fail_code:
    sys.exit(a.fail_code)
base = Path(a.build_dir) if a.build_dir else Path.cwd() / 'Projects' / a.product
results = []
for index in range(a.count):
    folder = base / str(index) if a.count > 1 else base
    folder.mkdir(parents=True, exist_ok=True)
    target = 'MiniGameApp' if a.product == 'MiniGame' else a.product
    solution = folder / (a.product + '.sln')
    template = %s
    solution.write_text(template.replace('MiniGameApp', target), encoding='utf-8')
    (folder / (target + '.vcxproj')).write_text('<Project />')
    metadata = dict(solution=str(solution.resolve()), buildDir=str(folder.resolve()),
                    primaryTarget=target, configuration='Debug', buildPlatform='x64')
    (folder / 'MINIW_PRODUCT.json').write_text(json.dumps(metadata), encoding='utf-8')
    results.append(metadata)
if not a.no_json:
    print(json.dumps(results))
''' % repr(SOLUTION), encoding="utf-8")
    for name in ("Win_MiniGame_GenerateProject.bat", "Win_AICore_GenerateProject.bat"):
        (scripts / name).write_text(
            '@echo off\n"' + sys.executable + '" "%~dp0generate.py" %*\nexit /b %ERRORLEVEL%\n',
            encoding="utf-8", newline="\r\n")
    (scripts / "Win_Setup.bat").write_text("@echo off\nexit /b 99\n")
    skill = tmp_path / "skill with spaces/scripts"
    skill.mkdir(parents=True)
    (skill / "invoke-mini-ib-build.ps1").write_text('''
param([string]$ProjectRoot, [string]$SolutionPath, [string]$Target, [string]$Configuration, [string]$Platform,
      [string]$Action, [string]$LogPath, [string]$SessionFile,
      [string]$BuildConsolePath, [switch]$OpenMonitor, [switch]$ValidateOnly)
if ($ValidateOnly) { Write-Output 'VALIDATE_ONLY'; return }
$code = 0
$codeFile = Join-Path $ProjectRoot 'ib-exit-code.txt'
if (Test-Path -LiteralPath $codeFile) { $code = [int](Get-Content -LiteralPath $codeFile) }
@{ProjectRoot=$ProjectRoot; Solution=$SolutionPath; Target=$Target; ExitCode=$code; Status='completed'; StartedAt=[DateTime]::Now.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $SessionFile -Encoding UTF8
'BUILD_LOG' | Set-Content -LiteralPath $LogPath -Encoding UTF8
if (Test-Path -LiteralPath (Join-Path $ProjectRoot 'wait-for-cancel')) {
    Write-Output 'ARMED'
    Start-Sleep -Seconds 30
}
if ($code -ne 0) { throw "fixture IB failed $code" }
Write-Output 'BUILD_DONE'
''', encoding="utf-8")
    (skill / "read-mini-ib-result.ps1").write_text("param([string]$LogPath)\nWrite-Output 'SUMMARY_READ'\n", encoding="utf-8")
    (skill / "stop-mini-ib-build.ps1").write_text('''
[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$SessionFile, [switch]$IncludeWorkers)
$session = Get-Content -Raw -LiteralPath $SessionFile | ConvertFrom-Json
$SessionFile | Set-Content -LiteralPath (Join-Path $session.ProjectRoot 'stopped-owned-session.txt') -Encoding UTF8
''', encoding="utf-8")
    return Profile("build", "构建测试", str(root), {},
                   [RepoSpec("root", "Root", ".", "https://example.invalid/repo", branch="main")],
                   build=BuildOptions(python_path=sys.executable, ib_skill_dir=str(skill.parent)))


def existing_solution(profile, name="MiniGame"):
    solution = Path(profile.root) / "Projects" / name / (name + ".sln")
    solution.parent.mkdir(parents=True, exist_ok=True)
    target = "MiniGameApp" if name == "MiniGame" else name
    solution.write_text(SOLUTION.replace("MiniGameApp", target), encoding="utf-8")
    (solution.parent / (target + ".vcxproj")).write_text("<Project />")
    profile.build.compile_tasks.append(CompileTask(solution=str(solution), target=target))
    return solution


def generation_task(profile, **kwargs):
    task = GenerationTask(**kwargs)
    profile.build.generation_tasks.append(task)
    return task


def test_build_options_legacy_migration_and_task_roundtrip(build_profile, tmp_path):
    payload = asdict(ProfileDocument(1, build_profile.id, [build_profile]))
    old = payload["profiles"][0]["build"]
    del old["generation_tasks"], old["compile_tasks"]
    old["target"], old["configuration"] = "MiniGameApp", "Release"
    migrated = parse_document(payload).profiles[0].build
    assert migrated.compile_tasks[0].solution == "Projects/vs2019-win64-MiniGame/MiniGame.sln"
    assert migrated.compile_tasks[0].configuration == "Release"
    assert migrated.generation_tasks[0].bat == "SetupScript/Win_MiniGame_GenerateProject.bat"
    generation_task(build_profile, arguments=["--label", "中文 and spaces"], build_directory="Projects/custom")
    existing_solution(build_profile)
    build_profile.build.open_monitor = True
    store = ProfileStore(tmp_path / "app")
    store.save(ProfileDocument(1, build_profile.id, [build_profile]))
    assert store.load().profiles[0].build == build_profile.build


@pytest.mark.parametrize("field,value", [("steps", []), ("steps", ["compile", "compile"]),
    ("configuration", "unsupported"), ("target", "../target"), ("timeout_minutes", True),
    ("open_monitor", "false"), ("generation_environment", {"PYTHONHOME": "other"}),
    ("generation_tasks", [{}]), ("compile_tasks", "bad")])
def test_invalid_build_parameters_are_rejected(field, value):
    options = BuildOptions()
    setattr(options, field, value)
    with pytest.raises(ValueError):
        validate_build_options(options)


@pytest.mark.parametrize("argument", ["& calc", "%PATH%", "bad\narg", 'bad"quote', "!secret!", "^escape", "|more"])
def test_generation_rejects_cmd_expansion_and_injection(argument):
    with pytest.raises(ValueError, match="CMD"):
        validate_build_options(BuildOptions(generation_tasks=[GenerationTask(arguments=[argument])]))


def test_discovery_and_windows_arguments(build_profile):
    assert [path.name for path in discover_generation_scripts(Path(build_profile.root))] == [
        "Win_AICore_GenerateProject.bat", "Win_MiniGame_GenerateProject.bat"]
    assert split_arguments('--label "中文 with spaces" --product AICore') == [
        "--label", "中文 with spaces", "--product", "AICore"]


def test_generation_environment_does_not_modify_parent_process(build_profile, monkeypatch):
    monkeypatch.setenv("USE_RAINBOW_LIB", "1")
    build_profile.build.generation_environment = {"use_poco_build": "0"}
    env = generation_environment(build_profile)
    assert env["USE_POCO_BUILD"] == "0" and os.environ["USE_RAINBOW_LIB"] == "1"
    assert "USE_RAINBOW_LIB" not in env  # BAT owns defaults.


def test_same_bat_multiple_tasks_generate_and_compile_multiple_solutions(build_profile, tmp_path):
    build_profile.build.steps = ["generate", "compile"]
    first = generation_task(build_profile, arguments=["--label", "中文 with spaces"], build_directory="Projects/first")
    generation_task(build_profile, arguments=["--product", "AICore"], build_directory="Projects/second")
    events = []
    result = BuildService(tmp_path / "app", lambda kind, payload: events.append((kind, payload))).run(build_profile)
    assert result["generate"].outcome == result["compile"].outcome == "success"
    assert len(result["generate"].tasks) == len(result["compile"].tasks) == 2
    tasks = build_profile.build.compile_tasks
    assert [task.target for task in tasks] == ["MiniGameApp", "AICore"]
    assert tasks[0].generation_id == first.id
    sessions = [Path(task.session_file) for task in result["compile"].tasks]
    assert sessions[0] != sessions[1] and all(path.is_file() for path in sessions)
    for task, session in zip(tasks, sessions):
        request = json.loads((session.parent / "invoke-mini-ib-build.ps1.request.json").read_text(encoding="utf-8"))
        assert request["Parameters"]["SolutionPath"] == task.solution
        assert request["Parameters"]["Target"] == task.target
        assert "AllowConcurrent" not in request["Parameters"]
    assert sum(kind == "build_task_started" for kind, _ in events) == 4
    calls = json.loads((Path(build_profile.root) / "generation-calls.json").read_text(encoding="utf-8"))
    assert calls[0]["label"] == "中文 with spaces"


def test_one_bat_can_publish_multiple_solutions(build_profile, tmp_path):
    build_profile.build.steps = ["generate"]
    generation_task(build_profile, arguments=["--count", "2"], build_directory="Projects/multi")
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["generate"].outcome == "success"
    assert len(build_profile.build.compile_tasks) == 2


def test_output_directory_discovers_results_without_console_metadata(build_profile, tmp_path):
    build_profile.build.steps = ["generate"]
    generation_task(build_profile, arguments=["--no-json"], build_directory="Projects/explicit")
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["generate"].outcome == "success"
    assert len(build_profile.build.compile_tasks) == 1


def test_regeneration_preserves_compile_choices_without_duplicate_rows(build_profile, tmp_path):
    build_profile.build.steps = ["generate"]
    generation_task(build_profile, build_directory="Projects/custom")
    BuildService(tmp_path / "app").run(build_profile)
    task = build_profile.build.compile_tasks[0]
    task.configuration, task.action, task.enabled = "Release", "Rebuild", False
    # Exported profiles may keep generated SLNs relative to the workspace.
    task.solution = str(Path(task.solution).relative_to(build_profile.root))
    identity = task.id
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["generate"].outcome == "success"
    assert len(build_profile.build.compile_tasks) == 1
    restored = build_profile.build.compile_tasks[0]
    assert (restored.id, restored.configuration, restored.action, restored.enabled) == (identity, "Release", "Rebuild", False)


def test_generation_failure_does_not_use_stale_solution_and_stops_queue(build_profile, tmp_path):
    existing_solution(build_profile)
    build_profile.build.steps = ["generate", "compile"]
    generation_task(build_profile, arguments=["--fail-code", "7"])
    generation_task(build_profile, arguments=["--product", "AICore"])
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["generate"].exit_code == 7
    assert [item.outcome for item in result["generate"].tasks] == ["failed", "skipped"]
    assert result["compile"].outcome == "skipped"
    assert len(json.loads((Path(build_profile.root) / "generation-calls.json").read_text(encoding="utf-8"))) == 1
    assert not list((tmp_path / "app").rglob("incredibuild.session.json"))


def test_duplicate_output_directories_are_rejected_before_generation(build_profile, tmp_path):
    build_profile.build.steps = ["generate"]
    generation_task(build_profile, build_directory="Projects/shared")
    generation_task(build_profile, build_directory="Projects/shared", arguments=["--product", "AICore"])
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["generate"].outcome == "failed" and "相同输出" in result["generate"].message
    assert not (Path(build_profile.root) / "generation-calls.json").exists()


def test_setup_failure_stops_generation_and_compile(build_profile, tmp_path):
    (Path(build_profile.root) / "Tools/Setup/SetupWin.py").write_text("raise SystemExit(9)\n")
    build_profile.build.steps = ["setup", "generate", "compile"]
    generation_task(build_profile)
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["setup"].exit_code == 9
    assert result["generate"].outcome == result["compile"].outcome == "skipped"


def test_multi_sln_configuration_and_actions_are_independent(build_profile, tmp_path):
    existing_solution(build_profile)
    existing_solution(build_profile, "AICore")
    build_profile.build.compile_tasks[1].configuration = "Release"
    build_profile.build.compile_tasks[1].action = "Rebuild"
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["compile"].outcome == "success"
    requests = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "app").rglob("invoke-mini-ib-build.ps1.request.json")]
    assert {(item["Parameters"]["Target"], item["Parameters"]["Configuration"], item["Parameters"]["Action"])
            for item in requests} == {("MiniGameApp", "Debug", "Build"), ("AICore", "Release", "Rebuild")}


def test_actual_ib_exit_code_stops_remaining_solutions(build_profile, tmp_path):
    existing_solution(build_profile)
    existing_solution(build_profile, "AICore")
    (Path(build_profile.root) / "ib-exit-code.txt").write_text("12")
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["compile"].exit_code == 12
    assert [item.outcome for item in result["compile"].tasks] == ["failed", "skipped"]
    assert len(list((tmp_path / "app").rglob("incredibuild.session.json"))) == 1


def test_config_check_never_runs_setup_generation_or_compilation(build_profile, tmp_path):
    existing_solution(build_profile)
    generation_task(build_profile)
    build_profile.build.steps = ["setup", "generate", "compile"]
    result = BuildService(tmp_path / "app").run(build_profile, validate_only=True)
    assert all(item.outcome == "success" for item in result.values())
    assert not (Path(build_profile.root) / "setup-ran.txt").exists()
    assert not (Path(build_profile.root) / "generation-calls.json").exists()
    assert not list((tmp_path / "app").rglob("incredibuild.session.json"))


def test_compile_rejects_solution_folder_and_unsupported_configuration(build_profile, tmp_path):
    existing_solution(build_profile)
    task = build_profile.build.compile_tasks[0]
    task.target = "MiniGame"
    result = BuildService(tmp_path / "app").run(build_profile)["compile"]
    assert result.outcome == "failed" and "MiniGame 不是 SLN 中的 C++ 工程" in result.message
    task.target, task.configuration = "MiniGameApp", "Profile"
    result = BuildService(tmp_path / "app").run(build_profile)["compile"]
    assert result.outcome == "failed" and "不支持 Profile|x64" in result.message
    assert not list((tmp_path / "app").rglob("invoke-mini-ib-build.ps1.request.json"))


def test_real_skill_accepts_explicit_solution_without_executing_ib(build_profile, tmp_path):
    skill = Path(__file__).resolve().parents[3] / "mini-compile-ib"
    if not skill.is_dir():
        pytest.skip("Local mini-compile-ib skill unavailable")
    solution = existing_solution(build_profile, "AICore")
    # A matching solution-folder name must not count as another C++ project.
    solution.write_text(solution.read_text(encoding="utf-8").replace('= "MiniGame", "MiniGame"', '= "AICore", "AICore"'))
    console = tmp_path / "BuildConsole.exe"
    console.write_bytes(b"This file must never execute")
    build_profile.build.ib_skill_dir, build_profile.build.build_console_path = str(skill), str(console)
    result = BuildService(tmp_path / "app").run(build_profile, validate_only=True)
    assert result["compile"].outcome == "success"
    assert not list((tmp_path / "app").rglob("incredibuild.session.json"))


@pytest.mark.parametrize("relative", [False, True])
def test_ib_entrypoint_uses_the_project_reference_in_each_solution(build_profile, tmp_path, relative):
    script = Path(__file__).resolve().parents[3] / "mini-compile-ib/scripts/invoke-mini-ib-build.ps1"
    if not script.is_file():
        pytest.skip("Local mini-compile-ib skill unavailable")
    solution = existing_solution(build_profile, "AICore")
    linked = solution.parent / "nested/project-file.vcxproj"
    linked.parent.mkdir()
    linked.write_text("<Project />")
    solution.write_text(solution.read_text(encoding="utf-8").replace("AICore.vcxproj", "nested/project-file.vcxproj"),
                        encoding="utf-8")
    # The unreferenced file must not make the target ambiguous.
    console = tmp_path / "BuildConsole.exe"
    console.write_bytes(b"never execute")
    selected = str(solution.relative_to(build_profile.root)) if relative else str(solution)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(script), "-ProjectRoot", build_profile.root, "-SolutionPath", selected,
                             "-Target", "AICore", "-BuildConsolePath", str(console), "-ValidateOnly"],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(Path(build_profile.root).rglob("*.session.json"))


def test_generation_entry_cannot_escape_setupscript(build_profile, tmp_path):
    build_profile.build.steps = ["generate"]
    generation_task(build_profile, bat="Tools/Setup/other.bat")
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["generate"].outcome == "failed" and "SetupScript" in result["generate"].message


def test_compile_ignores_command_timeout_while_check_keeps_it(build_profile, tmp_path, monkeypatch):
    from repo_tool.process import ProcessRunner
    existing_solution(build_profile)
    build_profile.build.timeout_minutes = 1
    seen = []
    original = ProcessRunner.run
    def run(self, program, arguments, *args, **kwargs):
        seen.append((Path(str(arguments[-1])).name, kwargs.get("timeout")))
        return original(self, program, arguments, *args, **kwargs)
    monkeypatch.setattr(ProcessRunner, "run", run)
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["compile"].outcome == "success"
    assert [timeout for name, timeout in seen if name == "invoke-mini-ib-build.ps1.request.json"] == [60, None]


def test_cancel_stops_only_current_session_and_skips_following_solution(build_profile, tmp_path):
    existing_solution(build_profile)
    existing_solution(build_profile, "AICore")
    (Path(build_profile.root) / "wait-for-cancel").touch()
    cancel = threading.Event()
    def emit(kind, payload):
        if kind == "log" and payload[1] == "ARMED":
            cancel.set()
    result = BuildService(tmp_path / "app", emit, cancel=cancel).run(build_profile)
    tasks = result["compile"].tasks
    assert [task.outcome for task in tasks] == ["cancelled", "cancelled"]
    marker = Path(build_profile.root) / "stopped-owned-session.txt"
    assert marker.read_text(encoding="utf-8-sig").strip() == tasks[0].session_file
    assert len(list((tmp_path / "app").rglob("incredibuild.session.json"))) == 1


def test_stop_after_current_task_and_shared_lock(build_profile, tmp_path):
    build_profile.build.steps = ["generate", "compile"]
    generation_task(build_profile)
    generation_task(build_profile, arguments=["--product", "AICore"])
    stop = threading.Event()
    def emit(kind, payload):
        if kind == "build_task_result":
            stop.set()
    result = BuildService(tmp_path / "app", emit, stop=stop).run(build_profile)
    assert [item.outcome for item in result["generate"].tasks] == ["success", "cancelled"]
    assert result["compile"].outcome == "cancelled"
    with RunLock(tmp_path / "app"):
        with pytest.raises(RuntimeError, match="正在执行"):
            BuildService(tmp_path / "app").run(build_profile)


def test_missing_solution_never_generates_it(build_profile, tmp_path):
    missing = Path(build_profile.root) / "Projects/absent/MiniGame.sln"
    build_profile.build.compile_tasks = [CompileTask(solution=str(missing))]
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["compile"].outcome == "failed" and "请先执行生成" in result["compile"].message
    assert not missing.exists()


def test_missing_generation_result_does_not_collect_unrelated_solutions(build_profile):
    existing_solution(build_profile)
    with pytest.raises(ValueError, match="未返回"):
        generated_compile_tasks(Path(build_profile.root), GenerationTask(), "nothing was generated")


def test_setup_parameters_keep_paths_as_single_arguments(build_profile, tmp_path):
    root = Path(build_profile.root)
    package = root / "离线包 with spaces.zip"
    package.touch()
    build_profile.build.steps = ["setup"]
    build_profile.build.setup_install_file = str(package)
    setup = root / "Tools/Setup/SetupWin.py"
    setup.write_text("import json, sys\nfrom pathlib import Path\nPath('args.json').write_text(json.dumps(sys.argv[1:]))\n")
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["setup"].outcome == "success"
    assert json.loads((root / "args.json").read_text(encoding="utf-8")) == ["--workdir", str(root), "--install", str(package)]
    assert "Win_Setup.bat" in build_preview(build_profile)
