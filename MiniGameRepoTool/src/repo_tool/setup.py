from pathlib import Path

from .build_config import resolve_build_path
from .models import Profile, RepoResult
from .process import ProcessRunner, redact


class SetupRunner:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner
        self.last_exit_code: int | None = None

    @staticmethod
    def arguments(profile: Profile) -> list[str]:
        options = profile.build
        root = Path(profile.root).resolve()
        args = ["-u", str(root / "Tools/Setup/SetupWin.py")]
        if options.setup_install_file or options.setup_pack_zip:
            args += ["--workdir", str(root)]
        if options.setup_install_file:
            args += ["--install", str(resolve_build_path(root, options.setup_install_file))]
        elif options.setup_pack_zip:
            args += ["--packzip", "True"]
        return args

    def prepare(self, profile: Profile) -> RepoResult:
        root = Path(profile.root).resolve()
        buildtools = root / "Tools" / "buildtools"
        python = resolve_build_path(root, profile.build.python_path)
        timeout = profile.build.timeout_minutes * 60 or None
        script = root / "Tools" / "Setup" / "SetupWin.py"
        if not script.is_file():
            return RepoResult("__setup__", "blocked", f"找不到环境准备脚本：{script}")
        if profile.build.setup_install_file and not resolve_build_path(root, profile.build.setup_install_file).is_file():
            return RepoResult("__setup__", "blocked", "指定的依赖安装文件不存在")
        if not python.is_file():
            if python != buildtools / "Python39" / "python.exe":
                return RepoResult("__setup__", "blocked", f"指定的 Python 不存在：{python}")
            archive, unzip = buildtools / "Python39.zip", root / "Tools" / "7z.exe"
            if not archive.is_file() or not unzip.is_file():
                return RepoResult("__setup__", "blocked", "缺少 Python39、Python39.zip 或 Tools/7z.exe")
            result = self.runner.run(str(unzip), ["x", str(archive), "-o" + str(buildtools), "-aoa"], root,
                                     timeout=timeout)
            self.last_exit_code = result.returncode
            if result.returncode or result.cancelled or result.timed_out:
                return RepoResult("__setup__", "cancelled" if result.cancelled else "failed",
                                  f"解压 Python39 失败（退出码 {result.returncode}）：{redact(result.output[-1200:])}")
        if not python.is_file():
            return RepoResult("__setup__", "failed", "解压后未找到 Python39/python.exe")
        # Win_Setup.bat starts from SetupScript then pushd .. before launching Python.
        result = self.runner.run(str(python), self.arguments(profile), root, timeout=timeout,
                                 environment={"PYTHONHOME": None, "PYTHONPATH": None})
        self.last_exit_code = result.returncode
        if result.cancelled:
            return RepoResult("__setup__", "cancelled", "环境准备已中断")
        if result.timed_out:
            return RepoResult("__setup__", "failed", "构建设置超时，已停止当前进程")
        return RepoResult("__setup__", "success" if result.returncode == 0 else "failed",
                          "构建环境准备完成" if result.returncode == 0 else
                          f"环境准备失败（退出码 {result.returncode}）：{redact(result.output[-1200:])}")
