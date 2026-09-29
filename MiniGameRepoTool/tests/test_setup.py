import sys
from pathlib import Path

import pytest

from repo_tool.models import Profile, RepoSpec
from repo_tool.process import ProcessRunner
from repo_tool.setup import SetupRunner


class InstalledPythonRunner(ProcessRunner):
    """Execute the fixture setup script with our test interpreter."""
    def run(self, program, arguments, cwd=None, **kwargs):
        assert Path(program).name == "python.exe"
        return super().run(sys.executable, arguments, cwd, **kwargs)


@pytest.mark.parametrize("exitcode", [0, 7])
def test_setup_executes_from_project_root_and_preserves_exitcode(tmp_path, exitcode):
    root = tmp_path / "工程 root"
    root.mkdir()
    (root / "SetupScript").mkdir()
    python = root / "Tools/buildtools/Python39/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    script = root / "Tools/Setup/SetupWin.py"
    script.parent.mkdir(parents=True)
    (script.parent / "Dependence.json").write_text("{}", encoding="utf-8")
    script.write_text(
        "from pathlib import Path\n"
        "assert (Path.cwd() / 'Tools/Setup/Dependence.json').is_file()\n"
        "Path('setup-ran.txt').write_text('ok')\n"
        f"raise SystemExit({exitcode})\n", encoding="utf-8")
    profile = Profile("p", "Test", str(root), {},
                      [RepoSpec("root", "Root", ".", "https://example.invalid/r", branch="main")])
    result = SetupRunner(InstalledPythonRunner()).prepare(profile)
    assert (root / "setup-ran.txt").read_text() == "ok"
    assert result.outcome == ("success" if exitcode == 0 else "failed")
    if exitcode:
        assert "7" in result.message


def test_missing_setup_script_is_actionable(tmp_path):
    profile = Profile("p", "Test", str(tmp_path), {}, [])
    result = SetupRunner(ProcessRunner()).prepare(profile)
    assert result.outcome == "blocked"
    assert "SetupWin.py" in result.message
