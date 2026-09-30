import json
import os
import sys
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from repo_tool.build import BuildService, build_preview, generation_environment
from repo_tool.build_config import BuildOptions, read_solution_targets, validate_build_options
from repo_tool.models import Profile, ProfileDocument, RepoSpec
from repo_tool.profiles import ProfileStore, parse_document
from repo_tool.storage import RunLock


SOLUTION = 'Project("{GUID}") = "MiniGame", "MiniGame.vcxproj", "{ID}"\nEndProject\n'


@pytest.fixture
def build_profile(tmp_path):
    root = tmp_path / "工程 with spaces"
    setup = root / "Tools/Setup/SetupWin.py"
    setup.parent.mkdir(parents=True)
    setup.write_text("from pathlib import Path\nPath('setup-ran.txt').write_text('ok')\n", encoding="utf-8")
    generator = root / "Tools/buildtools/cmake_generate_projects.py"
    generator.parent.mkdir(parents=True)
    generator.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "assert Path.cwd().name == 'buildtools'\nassert sys.argv[2] == 'dev'\n"
        "assert os.environ['USE_LUA_JIT'] == '1'\n"
        "folder = Path('../../Projects') / sys.argv[1]\nfolder.mkdir(parents=True, exist_ok=True)\n"
        f"(folder / 'MiniGame.sln').write_text({SOLUTION!r})\n"
        "(folder / 'MiniGame.vcxproj').write_text('<Project />')\n", encoding="utf-8")
    preset = root / "Tools/buildtools/platformconfig/public/vs2019-win64-MiniGame.xml"
    preset.parent.mkdir(parents=True)
    preset.write_text("<preset />")
    scripts = tmp_path / "skill with spaces/scripts"
    scripts.mkdir(parents=True)
    (scripts / "invoke-mini-ib-build.ps1").write_text('''
param([string]$ProjectRoot, [string]$Target, [string]$Configuration, [string]$Platform,
      [string]$VisualStudioVersion, [string]$Action, [string]$LogPath, [string]$SessionFile,
      [string]$BuildConsolePath, [switch]$OpenMonitor, [switch]$ValidateOnly)
if ($ValidateOnly) { Write-Output 'VALIDATE_ONLY'; return }
$code = 0
$codeFile = Join-Path $ProjectRoot 'ib-exit-code.txt'
if (Test-Path -LiteralPath $codeFile) { $code = [int](Get-Content -LiteralPath $codeFile) }
@{ProjectRoot=$ProjectRoot; Target=$Target; ExitCode=$code; Status='completed'; StartedAt=[DateTime]::Now.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $SessionFile -Encoding UTF8
'BUILD_LOG' | Set-Content -LiteralPath $LogPath -Encoding UTF8
if (Test-Path -LiteralPath (Join-Path $ProjectRoot 'wait-for-cancel')) {
    Write-Output 'ARMED'
    Start-Sleep -Seconds 30
}
if ($code -ne 0) { throw "fixture IB failed $code" }
Write-Output 'BUILD_DONE'
''', encoding="utf-8")
    (scripts / "read-mini-ib-result.ps1").write_text("param([string]$LogPath)\nWrite-Output 'SUMMARY_READ'\n", encoding="utf-8")
    (scripts / "stop-mini-ib-build.ps1").write_text('''
[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$SessionFile, [switch]$IncludeWorkers)
$session = Get-Content -Raw -LiteralPath $SessionFile | ConvertFrom-Json
$SessionFile | Set-Content -LiteralPath (Join-Path $session.ProjectRoot 'stopped-owned-session.txt') -Encoding UTF8
''', encoding="utf-8")
    return Profile("build", "构建测试", str(root), {},
                   [RepoSpec("root", "Root", ".", "https://example.invalid/repo", branch="main")],
                   build=BuildOptions(python_path=sys.executable, ib_skill_dir=str(scripts.parent)))


def existing_solution(profile):
    solution = profile.build.solution(Path(profile.root))
    solution.parent.mkdir(parents=True, exist_ok=True)
    solution.write_text(SOLUTION, encoding="utf-8")
    (solution.parent / "MiniGame.vcxproj").write_text("<Project />")
    return solution


def test_build_options_legacy_defaults_and_roundtrip(build_profile, tmp_path):
    document = ProfileDocument(1, build_profile.id, [build_profile])
    legacy = asdict(document)
    del legacy["profiles"][0]["build"]
    restored = parse_document(legacy).profiles[0].build
    assert restored.steps == ["compile"]
    assert (restored.target, restored.configuration, restored.platform, restored.visual_studio_version) == (
        "MiniGame", "Debug", "x64", "2019")
    build_profile.build.steps = ["setup", "generate", "compile"]
    build_profile.build.compile_type = "参数 & literal"
    build_profile.build.open_monitor = True
    store = ProfileStore(tmp_path / "app")
    store.save(document)
    assert store.load().profiles[0].build == build_profile.build


@pytest.mark.parametrize("field,value", [("steps", []), ("steps", ["compile", "compile"]),
    ("configuration", "unsupported"), ("target", "MiniGame*"), ("target", "../target"),
    ("timeout_minutes", True), ("open_monitor", "false"),
    ("generation_environment", {"PYTHONHOME": "other"})])
def test_invalid_build_parameters_are_rejected(field, value):
    options = BuildOptions()
    setattr(options, field, value)
    with pytest.raises(ValueError):
        validate_build_options(options)


def test_environment_does_not_leak_or_modify_parent_process(build_profile, monkeypatch):
    monkeypatch.setenv("USE_RAINBOW_LIB", "1")
    build_profile.build.generation_environment = {"use_poco_build": "0"}
    env = generation_environment(build_profile)
    assert env["USE_RAINBOW_LIB"] == "0"
    assert env["USE_POCO_BUILD"] == "0"
    assert os.environ["USE_RAINBOW_LIB"] == "1"


def test_generation_uses_bat_working_directory_environment_and_output(build_profile, tmp_path):
    build_profile.build.steps = ["generate"]
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["generate"].outcome == "success"
    assert read_solution_targets(build_profile.build.solution(Path(build_profile.root))) == ["MiniGame"]
    assert not (Path(build_profile.root) / "setup-ran.txt").exists()


def test_generator_cmake_error_overrides_existing_stale_solution(build_profile, tmp_path):
    solution = existing_solution(build_profile)
    before = solution.read_bytes()
    generator = Path(build_profile.root) / "Tools/buildtools/cmake_generate_projects.py"
    generator.write_text("import os\nos.system('cmd /d /c exit 7')\n", encoding="utf-8")
    build_profile.build.steps = ["generate", "compile"]
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["generate"].outcome == "failed"
    assert result["generate"].exit_code == 7
    assert result["compile"].outcome == "skipped"
    assert solution.read_bytes() == before


def test_pipeline_setup_failure_stops_generation_and_compile(build_profile, tmp_path):
    setup = Path(build_profile.root) / "Tools/Setup/SetupWin.py"
    setup.write_text("raise SystemExit(9)\n")
    build_profile.build.steps = ["setup", "generate", "compile"]
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["setup"].exit_code == 9
    assert result["setup"].outcome == "failed"
    assert result["generate"].outcome == result["compile"].outcome == "skipped"
    assert not build_profile.build.solution(Path(build_profile.root)).exists()


def test_pipeline_uses_skill_entry_and_records_actual_ib_exit_code(build_profile, tmp_path):
    build_profile.build.steps = ["compile", "setup", "generate"]
    root = Path(build_profile.root)
    (root / "ib-exit-code.txt").write_text("12")
    events = []
    result = BuildService(tmp_path / "app", lambda kind, payload: events.append((kind, payload))).run(build_profile)
    assert list(result) == ["setup", "generate", "compile"]
    assert result["setup"].outcome == result["generate"].outcome == "success"
    assert result["compile"].outcome == "failed"
    assert result["compile"].exit_code == 12
    assert any(kind == "log" and "SUMMARY_READ" in value[1] for kind, value in events)
    request = json.loads((Path(result["compile"].session_file).parent / "invoke-mini-ib-build.ps1.request.json").read_text())
    assert request["Parameters"]["Target"] == "MiniGame"
    assert "AllowConcurrent" not in request["Parameters"]
    assert "MSBuild.exe" not in request["Script"]


def test_config_check_never_runs_setup_generation_or_compilation(build_profile, tmp_path):
    existing_solution(build_profile)
    build_profile.build.steps = ["setup", "generate", "compile"]
    result = BuildService(tmp_path / "app").run(build_profile, validate_only=True)
    assert all(item.outcome == "success" for item in result.values())
    assert not (Path(build_profile.root) / "setup-ran.txt").exists()
    assert not list((tmp_path / "app").rglob("incredibuild.session.json"))
    request = next((tmp_path / "app").rglob("invoke-mini-ib-build.ps1.request.json"))
    assert json.loads(request.read_text())["Parameters"]["ValidateOnly"] is True


def test_successful_ib_run_keeps_complete_logs_and_monitor_parameter(build_profile, tmp_path):
    existing_solution(build_profile)
    build_profile.build.open_monitor = True
    result = BuildService(tmp_path / "app").run(build_profile)["compile"]
    assert result.outcome == "success" and result.exit_code == 0
    folder = Path(result.session_file).parent
    assert "SUMMARY_READ" in (folder / "build.log").read_text(encoding="utf-8")
    assert Path(result.log_path).is_file()
    request = json.loads((folder / "invoke-mini-ib-build.ps1.request.json").read_text())
    assert request["Parameters"]["OpenMonitor"] is True
    assert "ValidateOnly" not in request["Parameters"]


def test_engine_library_download_failure_does_not_generate_solution(build_profile, tmp_path):
    root = Path(build_profile.root)
    (root / "Tools/Setup/PullEngine.py").write_text("raise SystemExit(8)\n")
    build_profile.build.steps = ["generate", "compile"]
    build_profile.build.generation_environment["USE_RAINBOW_LIB"] = "1"
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["generate"].exit_code == 8
    assert result["generate"].outcome == "failed"
    assert result["compile"].outcome == "skipped"
    assert not build_profile.build.solution(root).exists()


def test_real_skill_validate_only_resolves_fixture_without_executing_ib(build_profile, tmp_path):
    skill = Path("W:/git/skills/mini-compile-ib")
    if not skill.is_dir():
        pytest.skip("Local mini-compile-ib skill unavailable")
    existing_solution(build_profile)
    console = tmp_path / "BuildConsole.exe"
    console.write_bytes(b"This file must never execute")
    build_profile.build.ib_skill_dir = str(skill)
    build_profile.build.build_console_path = str(console)
    result = BuildService(tmp_path / "app").run(build_profile, validate_only=True)
    assert result["compile"].outcome == "success"
    assert "未执行编译" in result["compile"].message
    assert not list((tmp_path / "app").rglob("incredibuild.session.json"))


def test_cancel_calls_stop_helper_for_only_current_session(build_profile, tmp_path):
    existing_solution(build_profile)
    (Path(build_profile.root) / "wait-for-cancel").touch()
    cancel = threading.Event()
    def emit(kind, payload):
        if kind == "log" and payload[1] == "ARMED":
            cancel.set()
    result = BuildService(tmp_path / "app", emit, cancel=cancel).run(build_profile)
    assert result["compile"].outcome == "cancelled"
    marker = Path(build_profile.root) / "stopped-owned-session.txt"
    assert marker.read_text(encoding="utf-8-sig").strip() == result["compile"].session_file


def test_stop_after_current_step_and_shared_lock(build_profile, tmp_path):
    build_profile.build.steps = ["setup", "generate", "compile"]
    stop = threading.Event()
    def emit(kind, payload):
        if kind == "build_result" and payload.repo_id == "setup":
            stop.set()
    result = BuildService(tmp_path / "app", emit, stop=stop).run(build_profile)
    assert result["setup"].outcome == "success"
    assert result["generate"].outcome == result["compile"].outcome == "cancelled"
    with RunLock(tmp_path / "app"):
        with pytest.raises(RuntimeError, match="正在执行"):
            BuildService(tmp_path / "app").run(build_profile)


def test_compile_missing_solution_never_generates_it(build_profile, tmp_path):
    result = BuildService(tmp_path / "app").run(build_profile)
    assert result["compile"].outcome == "failed"
    assert "请先执行生成" in result["compile"].message
    assert not build_profile.build.solution(Path(build_profile.root)).exists()


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
    assert json.loads((root / "args.json").read_text()) == ["--workdir", str(root), "--install", str(package)]
    assert "Win_Setup.bat" in build_preview(build_profile)
