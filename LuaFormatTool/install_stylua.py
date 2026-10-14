"""下载固定版本的官方 StyLua，校验 SHA-256 后安装到本工具 bin 目录。"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from formatter import APP_DIR, FormatError


VERSION = "2.6.0"
# 来自官方 GitHub v2.6.0 release asset 的 sha256 digest。
ASSETS = {
    ("Windows", "x86_64"): ("stylua-windows-x86_64.zip", "fd13b4014e3b779c244bbc03f2c282e357f15bc526de6a6fa5ecbc447538313c"),
    ("Linux", "x86_64"): ("stylua-linux-x86_64.zip", "a485f72bbfa22e4642e77a84b5edc2611a5cab7477b34e2e709dd8f78e919895"),
    ("Linux", "aarch64"): ("stylua-linux-aarch64.zip", "8e67539e5fdf7a81cd03cf592b0df18d3bb96ee17a5ea5d9bf67b86387b903b0"),
    ("Darwin", "x86_64"): ("stylua-macos-x86_64.zip", "90779aa873837b2dff8eb3361bd27950303914c24265a59cae0a0d0629d71585"),
    ("Darwin", "aarch64"): ("stylua-macos-aarch64.zip", "dab9b66d340ca5ffc8225211f1dcf89b84783e09b8cbec8c518497eba81f3c42"),
}


def install(destination: Path | None = None) -> Path:
    system = platform.system()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    # Windows ARM64 可以通过系统仿真运行官方 x64 发行包。
    if system == "Windows" and machine == "aarch64":
        machine = "x86_64"
    if (system, machine) not in ASSETS:
        raise FormatError(f"不支持自动安装的平台：{system}/{machine}，请手动指定 StyLua")
    asset, digest = ASSETS[(system, machine)]
    url = f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{VERSION}/{asset}"
    request = urllib.request.Request(url, headers={"User-Agent": "LuaFormatTool"})
    with urllib.request.urlopen(request, timeout=60) as response:
        archive = response.read(40 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != digest:
        raise FormatError("StyLua 下载包 SHA-256 校验失败，未安装")
    name = "stylua.exe" if system == "Windows" else "stylua"
    destination = destination or APP_DIR / "bin" / name
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        binaries = [entry for entry in package.infolist() if Path(entry.filename).name == name]
        if len(binaries) != 1:
            raise FormatError("下载包内未找到唯一的 StyLua 可执行文件")
        binary = package.read(binaries[0])
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(binary)
        os.chmod(temporary, 0o755)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


if __name__ == "__main__":
    try:
        print(f"已安装 StyLua {VERSION}：{install()}")
    except (OSError, ValueError, zipfile.BadZipFile, FormatError) as exc:
        raise SystemExit(f"安装失败：{exc}")
