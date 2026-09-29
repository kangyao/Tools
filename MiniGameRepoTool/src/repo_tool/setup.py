from pathlib import Path

from .models import Profile, RepoResult
from .process import ProcessRunner, redact


class SetupRunner:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    def prepare(self, profile: Profile) -> RepoResult:
        root = Path(profile.root).resolve()
        buildtools = root / "Tools" / "buildtools"
        python = buildtools / "Python39" / "python.exe"
        script = root / "Tools" / "Setup" / "SetupWin.py"
        if not script.is_file():
            return RepoResult("__setup__", "blocked", f"找不到环境准备脚本：{script}")
        if not python.is_file():
            archive, unzip = buildtools / "Python39.zip", root / "Tools" / "7z.exe"
            if not archive.is_file() or not unzip.is_file():
                return RepoResult("__setup__", "blocked", "缺少 Python39、Python39.zip 或 Tools/7z.exe")
            result = self.runner.run(str(unzip), ["x", str(archive), "-o" + str(buildtools), "-aoa"], root)
            if result.returncode:
                return RepoResult("__setup__", "cancelled" if result.cancelled else "failed",
                                  f"解压 Python39 失败（退出码 {result.returncode}）：{redact(result.output[-1200:])}")
        if not python.is_file():
            return RepoResult("__setup__", "failed", "解压后未找到 Python39/python.exe")
        # Win_Setup.bat starts from SetupScript then pushd .. before launching Python.
        result = self.runner.run(str(python), ["-u", str(script)], root)
        if result.cancelled:
            return RepoResult("__setup__", "cancelled", "环境准备已中断")
        return RepoResult("__setup__", "success" if result.returncode == 0 else "failed",
                          "构建环境准备完成" if result.returncode == 0 else
                          f"环境准备失败（退出码 {result.returncode}）：{redact(result.output[-1200:])}")
