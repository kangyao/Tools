from __future__ import annotations

import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from typing import Callable, Mapping, Protocol, Sequence
import xml.etree.ElementTree as ET


from aicore_control import argument_value, available_dev_account, owned_ports, request_stop
from window_layout import Rect


DEFAULT_PROJECT_ROOT = r"C:\MiniGame\Source\AIGamePlay"
DEFAULT_EXECUTABLE = r"C:\MiniGame\Bin64\AICore_profile.exe"
DEFAULT_ARGUMENTS = ("-script-debug-wait-client",)
CONFIG_VERSION = 2
WINDOW_GEOMETRY_PATTERN = re.compile(
    r"^(?P<width>[1-9]\d*)x(?P<height>[1-9]\d*)"
    r"(?P<left>[+-]\d+)(?P<top>[+-]\d+)$"
)
DEBUG_WAIT_ARGUMENT = "-script-debug-wait-client"
LUA_DEBUG_PORT_ARGUMENT = "-lua-debug-port"
MCP_CLIENT_NAME_ENVIRONMENT_VARIABLE = "MINIGAME_MCP_CLIENT_NAME"
DEFAULT_NETWORK_HOST = "127.0.0.1"
DEFAULT_NETWORK_PORT = 7000
DEFAULT_NETWORK_ROOM_ID = 1
DEV_ACCOUNT_ARGUMENT = "-MGFDevAccount"
# 开发账号序号（AIGamePlay/.project/account.json 的数组位置，1 起）：Host 默认 1，Client 默认 2。
# AICore 按账号加锁，同一台机器上每个进程必须用不同的账号。
DEFAULT_DEV_ACCOUNTS = {"host": 1, "client": 2}
NETWORK_ARGUMENTS = (
    DEV_ACCOUNT_ARGUMENT, "-MGFNetRole", "-MGFNetHost", "-MGFNetPort", "-MGFNetRoomId", "-MGFNetServer",
)
# 已移除的旧参数：客机身份改取登录账号，AICore 收到会判 NetLaunchInvalid: uin；读取旧参数时丢弃。
LEGACY_NETWORK_ARGUMENTS = ("-MGFNetUin",)
DEFAULT_SCRIPT_DEBUG_PORT = 3382
DEFAULT_BRIDGE_PORT = 3383
DEFAULT_DAP_PORT = 4711
MAX_PORT = 65535


@dataclass(frozen=True)
class AttachConfiguration:
    name: str = "Lua"
    session_name: str = "MiniGamePlay lua"
    dap_port: int = DEFAULT_DAP_PORT
    bridge_port: int = DEFAULT_BRIDGE_PORT

    @property
    def display_name(self) -> str:
        if self.session_name and self.session_name != self.name:
            return f"{self.name} / {self.session_name}"
        return self.name


def load_attach_configuration(path: Path) -> AttachConfiguration:
    """Load the IDE Attach identity and endpoints from a JetBrains run config."""

    fallback = AttachConfiguration()
    try:
        root = ET.parse(path).getroot()
        configuration = (
            root if root.tag == "configuration" else root.find("configuration")
        )
        if configuration is None:
            return fallback
        options = {
            option.get("name", ""): option.get("value", "")
            for option in configuration.findall("option")
        }
        attach = json.loads(options.get("attachConfiguration", "{}"))
        if not isinstance(attach, dict):
            attach = {}

        name = configuration.get("name", "").strip() or fallback.name
        session_name_value = attach.get("name")
        session_name = (
            session_name_value.strip()
            if isinstance(session_name_value, str) and session_name_value.strip()
            else name
        )

        def parse_port(value: object, default: int) -> int:
            try:
                port = int(value)
            except (TypeError, ValueError):
                return default
            return port if 1 <= port <= MAX_PORT else default

        return AttachConfiguration(
            name=name,
            session_name=session_name,
            dap_port=parse_port(options.get("attachPort"), fallback.dap_port),
            bridge_port=parse_port(attach.get("port"), fallback.bridge_port),
        )
    except (OSError, ET.ParseError, json.JSONDecodeError):
        return fallback


class LauncherError(RuntimeError):
    """Base error shown by the launcher UI."""


class ConfigurationError(LauncherError):
    """The local settings file is invalid."""


class NetworkRole(str, Enum):
    STANDALONE = "standalone"
    HOST = "host"
    CLIENT = "client"


def default_dev_account(role: NetworkRole) -> int:
    """Default dev account index for a role; 0 means the role passes no -MGFDevAccount."""
    return DEFAULT_DEV_ACCOUNTS.get(role.value, 0)


@dataclass(frozen=True)
class NetworkOptions:
    role: NetworkRole = NetworkRole.STANDALONE
    host: str = DEFAULT_NETWORK_HOST
    port: int = DEFAULT_NETWORK_PORT
    room_id: int = DEFAULT_NETWORK_ROOM_ID
    # None 表示取角色默认值（Host 1、Client 2、单机不传）。
    dev_account: int | None = None
    server_mode: str = "Host"

    def __post_init__(self) -> None:
        if self.server_mode not in ("Host", "CloudServer"):
            raise ConfigurationError("服务端模式必须是 CloudServer 或 Host。")
        if self.is_dedicated:
            object.__setattr__(self, "dev_account", 0)
        elif self.dev_account is None:
            object.__setattr__(self, "dev_account", default_dev_account(self.role))

    @property
    def is_dedicated(self) -> bool:
        return self.role is NetworkRole.HOST and self.server_mode == "CloudServer"

    @property
    def native_role(self) -> str:
        return "CloudServer" if self.is_dedicated else self.role.value.title()

    def arguments(self) -> tuple[str, ...]:
        """Match AIGamePlay Tools/Start-NetDebug.ps1 argument order (dev account first)."""
        if self.role is NetworkRole.STANDALONE:
            return ()
        arguments: list[str] = []
        if self.dev_account:
            arguments.extend((DEV_ACCOUNT_ARGUMENT, str(self.dev_account)))
        arguments.extend(("-MGFNetRole", self.native_role))
        if self.role is NetworkRole.CLIENT:
            arguments.extend(("-MGFNetHost", self.host))
        arguments.extend((
            "-MGFNetPort", str(self.port), "-MGFNetRoomId", str(self.room_id),
        ))
        if self.role is NetworkRole.CLIENT and self.server_mode == "CloudServer":
            arguments.extend(("-MGFNetServer", "CloudServer"))
        return tuple(arguments)


def parse_network_arguments(arguments: Sequence[str]) -> NetworkOptions:
    """Read network metadata from the same argv that is passed to the App."""
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument in LEGACY_NETWORK_ARGUMENTS:
            if index < len(arguments) and not arguments[index].startswith("-"):
                index += 1
            continue
        if argument not in NETWORK_ARGUMENTS:
            continue
        if argument in values:
            raise ConfigurationError(f"网络参数 {argument} 不能重复。")
        if index == len(arguments) or arguments[index].startswith("-"):
            raise ConfigurationError(f"网络参数 {argument} 缺少值。")
        values[argument] = arguments[index]
        index += 1

    try:
        native_role = values.get("-MGFNetRole", "standalone").casefold()
        role = NetworkRole.HOST if native_role == "cloudserver" else NetworkRole(native_role)
        server_mode = "CloudServer" if native_role == "cloudserver" else values.get("-MGFNetServer", "Host")
    except ValueError as exc:
        raise ConfigurationError("-MGFNetRole 必须是 CloudServer、Host、Client 或 Standalone。") from exc
    host = values.get("-MGFNetHost", DEFAULT_NETWORK_HOST).strip()
    if not host:
        raise ConfigurationError("-MGFNetHost 必须是非空地址。")

    def positive_integer(argument: str, default: int) -> int:
        try:
            value = int(values.get(argument, str(default)))
        except ValueError as exc:
            raise ConfigurationError(f"{argument} 必须是正整数。") from exc
        if value <= 0:
            raise ConfigurationError(f"{argument} 必须是正整数。")
        return value

    port = positive_integer("-MGFNetPort", DEFAULT_NETWORK_PORT)
    if port > MAX_PORT:
        raise ConfigurationError(f"-MGFNetPort 必须是 1 到 {MAX_PORT} 之间的整数。")
    room_id = positive_integer("-MGFNetRoomId", DEFAULT_NETWORK_ROOM_ID)
    if DEV_ACCOUNT_ARGUMENT in values:
        dev_account = positive_integer(DEV_ACCOUNT_ARGUMENT, 1)
    else:
        dev_account = default_dev_account(role)
    return NetworkOptions(role, host, port, room_id, dev_account, server_mode)


def apply_network_preset(
    arguments: Sequence[str], role: NetworkRole,
) -> tuple[str, ...]:
    """Replace network flags with a script preset while preserving App options."""
    remaining: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument in NETWORK_ARGUMENTS or argument in LEGACY_NETWORK_ARGUMENTS:
            if index < len(arguments) and not arguments[index].startswith("-"):
                index += 1
        else:
            remaining.append(argument)
    return NetworkOptions(role=role).arguments() + tuple(remaining)


@dataclass(frozen=True)
class LauncherSettings:
    executable: str = DEFAULT_EXECUTABLE
    arguments: tuple[str, ...] = DEFAULT_ARGUMENTS
    show_console: bool = True
    window_geometry: str = ""
    argument_enabled: tuple[bool, ...] | None = None
    initial_window_rect: Rect | None = None
    network: NetworkOptions = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.initial_window_rect is not None and min(self.initial_window_rect.width, self.initial_window_rect.height) <= 0:
            raise ConfigurationError("App 初始窗口尺寸必须为正数。")
        states = self.argument_enabled
        if states is None:
            object.__setattr__(
                self,
                "argument_enabled",
                tuple(True for _argument in self.arguments),
            )
        elif len(states) != len(self.arguments) or not all(
            isinstance(state, bool) for state in states
        ):
            raise ConfigurationError(
                "启动参数的启用状态必须是与参数列表等长的布尔值数组。"
            )
        if LUA_DEBUG_PORT_ARGUMENT in self.enabled_arguments:
            raise ConfigurationError("-lua-debug-port 由启动器自动分配，请从 App 参数中删除。")
        object.__setattr__(self, "network", parse_network_arguments(self.enabled_arguments))

    @property
    def enabled_arguments(self) -> tuple[str, ...]:
        states = self.argument_enabled
        if states is None:  # __post_init__ 已规范化；保留防御式回退。
            return self.arguments
        return tuple(
            argument
            for argument, enabled in zip(self.arguments, states)
            if enabled
        )


def is_valid_window_geometry(value: str) -> bool:
    return not value or WINDOW_GEOMETRY_PATTERN.fullmatch(value) is not None


def center_window_geometry_on_screen(
    geometry: str, screen_width: int, screen_height: int,
) -> str:
    """Keep a saved window size while centering it inside one screen."""

    match = WINDOW_GEOMETRY_PATTERN.fullmatch(geometry)
    if match is None:
        raise ValueError("窗口几何必须是“宽x高+X+Y”格式。")
    if screen_width <= 0 or screen_height <= 0:
        raise ValueError("屏幕尺寸必须是正整数。")

    width = min(int(match.group("width")), screen_width)
    height = min(int(match.group("height")), screen_height)
    left = max(0, (screen_width - width) // 2)
    top = max(0, (screen_height - height) // 2)
    return f"{width}x{height}+{left}+{top}"


class ConfigStore:
    """Versioned, atomic JSON storage kept next to the tool."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> LauncherSettings:
        raw = self.read_payload()
        return LauncherSettings() if raw is None else self.parse_settings(raw)

    def read_payload(self) -> dict[str, object] | None:
        if not self.path.exists():
            return None

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"无法读取配置文件：{exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigurationError("配置文件根节点必须是 JSON 对象。")
        return raw

    @staticmethod
    def parse_settings(raw: dict[str, object]) -> LauncherSettings:
        if raw.get("version") not in (1, CONFIG_VERSION):
            raise ConfigurationError(
                f"不支持的配置版本：{raw.get('version')!r}，当前版本为 {CONFIG_VERSION}。"
            )

        executable = raw.get("executable")
        arguments = raw.get("arguments")
        argument_enabled = raw.get("argument_enabled")
        show_console = raw.get("show_console", True)
        window_geometry = raw.get("window_geometry", "")
        if not isinstance(executable, str) or not executable.strip():
            raise ConfigurationError("配置项 executable 必须是非空字符串。")
        if not isinstance(arguments, list) or not all(
            isinstance(item, str) for item in arguments
        ):
            raise ConfigurationError("配置项 arguments 必须是字符串数组。")
        if "argument_enabled" not in raw:
            argument_enabled = [True for _argument in arguments]
        if not isinstance(argument_enabled, list) or not all(
            isinstance(item, bool) for item in argument_enabled
        ):
            raise ConfigurationError("配置项 argument_enabled 必须是布尔值数组。")
        if len(argument_enabled) != len(arguments):
            raise ConfigurationError(
                "配置项 argument_enabled 必须与 arguments 等长。"
            )
        if not isinstance(show_console, bool):
            raise ConfigurationError("配置项 show_console 必须是布尔值。")
        if not isinstance(window_geometry, str) or not is_valid_window_geometry(
            window_geometry
        ):
            raise ConfigurationError(
                "配置项 window_geometry 必须是“宽x高+X+Y”格式的字符串。"
            )

        if raw["version"] == 1:
            # Version 1 stored a fixed gameplay radio list. Only the debugger
            # option survives; network presets now use the App-level protocol.
            debug_enabled = any(
                argument == DEBUG_WAIT_ARGUMENT and enabled
                for argument, enabled in zip(arguments, argument_enabled)
            )
            role = raw.get("network_role", "standalone")
            if role not in ("standalone", "login", "host", "client"):
                raise ConfigurationError(f"旧配置的网络角色无效：{role!r}。")
            network_arguments: tuple[str, ...] = ()
            if role in ("host", "client"):
                port = raw.get("network_port", DEFAULT_NETWORK_PORT)
                host = raw.get("network_host", DEFAULT_NETWORK_HOST)
                if isinstance(port, bool) or not isinstance(port, int):
                    raise ConfigurationError("旧配置的网络端口必须是整数。")
                if not isinstance(host, str) or not host.strip():
                    raise ConfigurationError("旧配置的网络主机地址必须是非空字符串。")
                if port == 19120:
                    port = DEFAULT_NETWORK_PORT
                network_arguments = NetworkOptions(
                    role=NetworkRole(role),
                    host=host,
                    port=port,
                ).arguments()
            arguments = [*network_arguments, DEBUG_WAIT_ARGUMENT]
            argument_enabled = [True] * len(network_arguments) + [debug_enabled]
            if executable.replace("/", "\\").casefold() == (
                r"C:\MiniGame\Bin64\AIFramework_d.exe".casefold()
            ):
                executable = DEFAULT_EXECUTABLE

        return LauncherSettings(
            executable=executable,
            arguments=tuple(arguments),
            argument_enabled=tuple(argument_enabled),
            show_console=show_console,
            window_geometry=window_geometry,
        )

    def save(self, settings: LauncherSettings) -> None:
        self.write_payload(self.settings_payload(settings))

    @staticmethod
    def settings_payload(settings: LauncherSettings) -> dict[str, object]:
        return {
            "version": CONFIG_VERSION,
            "executable": settings.executable,
            "arguments": list(settings.arguments),
            "argument_enabled": list(settings.argument_enabled or ()),
            "show_console": settings.show_console,
            "window_geometry": settings.window_geometry,
        }

    def write_payload(self, payload: dict[str, object]) -> None:
        geometry = payload.get("window_geometry", "")
        if not isinstance(geometry, str) or not is_valid_window_geometry(geometry):
            raise ConfigurationError(
                "窗口位置格式无效，必须是“宽x高+X+Y”。"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise ConfigurationError(f"无法保存配置文件：{exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def save_window_geometry(self, geometry: str) -> None:
        if not is_valid_window_geometry(geometry):
            raise ConfigurationError(
                "窗口位置格式无效，必须是“宽x高+X+Y”。"
            )
        self.save(replace(self.load(), window_geometry=geometry))


def parse_argument_text(text: str) -> tuple[str, ...]:
    """Parse one Windows command-line fragment into argv without invoking a shell."""

    if not text.strip():
        return ()
    if os.name != "nt":
        raise LauncherError("该启动器仅支持 Windows。")

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    argc = ctypes.c_int()
    # A stable dummy argv[0] makes the user-entered text an argument-only fragment.
    argv = shell32.CommandLineToArgvW(f"launcher.exe {text}", ctypes.byref(argc))
    if not argv:
        error = ctypes.get_last_error()
        raise LauncherError(f"启动参数解析失败（Windows 错误 {error}）。")
    try:
        return tuple(argv[index] for index in range(1, argc.value))
    finally:
        kernel32.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))


def format_argument_text(arguments: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(arguments)) if arguments else ""


def build_process_environment_overrides(
    settings: LauncherSettings,
    debug_ports: DebugPortBundle | None = None,
) -> dict[str, str]:
    """Give every launched game a stable, selectable MCP identity."""

    role = settings.network.role.value.upper()
    port_suffix = f"-{debug_ports.target_port}" if debug_ports is not None else ""
    app_name = Path(settings.executable).stem
    return {
        MCP_CLIENT_NAME_ENVIRONMENT_VARIABLE: f"{app_name}-{role}{port_suffix}"
    }


@dataclass(frozen=True)
class DebugPortBundle:
    target_port: int
    bridge_port: int
    dap_port: int


_MAX_DEV_ACCOUNT_DIGITS = 9


def resolve_app_log_path(
    executable: str, arguments: Sequence[str], pid: int | None,
) -> Path:
    """Return the log file an AICore App writes next to its executable.

    Mirrors GetLogFileName / ResolveDevAccountLogTag in AiFrameworkApp/WinApp/WinGameStart.cpp:
    -AICoreLogFile wins, then -AICoreNetRole Dedicated / Client, then -MGFDevAccount, else AICoreApp.log.
    """

    directory = Path(os.path.expandvars(executable)).expanduser().parent
    process = str(pid) if pid is not None else "<pid>"

    def value(name: str) -> str:
        for index, argument in enumerate(arguments[:-1]):
            if argument == name and not arguments[index + 1].startswith("-"):
                return arguments[index + 1]
        return ""

    if any("MGFAvatarTerrainDemo" in argument for argument in arguments):
        return directory / "AICoreAvatarTerrainDemo.log"
    explicit = value("-AICoreLogFile")
    if explicit and not any(separator in explicit for separator in "\\/:"):
        return directory / explicit
    role = value("-AICoreNetRole")
    if role == "Dedicated":
        return directory / "AICoreApp-server.log"
    if role == "Client":
        return directory / f"AICoreApp-client-{process}.log"
    for index, argument in enumerate(arguments):
        if argument.casefold() != DEV_ACCOUNT_ARGUMENT.casefold():
            continue
        account = arguments[index + 1] if index + 1 < len(arguments) else ""
        if account.isdigit() and account.isascii() and not account.startswith("0") and (
            len(account) <= _MAX_DEV_ACCOUNT_DIGITS
        ):
            return directory / f"AICoreApp_dev{account}.log"
        return directory / f"AICoreApp_dev-{process}.log"
    return directory / "AICoreApp.log"


def build_launch_arguments(
    settings: LauncherSettings,
    debug_ports: DebugPortBundle | None = None,
) -> tuple[str, ...]:
    """Build the single argv source used by previews and process launches."""

    arguments = list(settings.enabled_arguments)
    if Path(settings.executable).stem.casefold() in ("aicore", "aicore_profile", "aicore_d"):
        _append_sdk_framework_root(arguments, settings.executable)
    if debug_ports is not None:
        arguments.extend((LUA_DEBUG_PORT_ARGUMENT, str(debug_ports.target_port)))
    return tuple(arguments)



def _append_sdk_framework_root(arguments: list[str], executable: str) -> None:
    """Resolve schema-2 SDKs for hosts that still read frameworkRoot only."""
    if argument_value(arguments, "-AICoreFrameworkRoot") is not None:
        return
    project_argument = argument_value(arguments, "-AICoreRoot")
    if project_argument is None:
        return
    project = Path(os.path.expandvars(project_argument)).expanduser()
    if not project.is_absolute():
        project = Path(executable).resolve().parent / project
    project = project.resolve()
    config_file = project / ".project/dev-runtime.json"
    try:
        config = json.loads(config_file.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigurationError(f"无法读取工程运行配置 {config_file}：{exc}") from exc
    if not isinstance(config, dict) or config.get("schemaVersion") != 2:
        return
    sdk_root = config.get("sdkRoot")
    if not isinstance(sdk_root, str) or not sdk_root.strip():
        raise ConfigurationError(f"工程运行配置缺少 sdkRoot：{config_file}")
    framework = (project / sdk_root / "AICoreScripts").resolve()
    if not (framework / "main.lua").is_file():
        raise ConfigurationError(f"所选 SDK 缺少框架入口：{framework / 'main.lua'}")
    if (project / "AICoreScripts").is_dir():
        raise ConfigurationError(
            f"工程仍有旧目录 {project / 'AICoreScripts'}，宿主会优先加载它；"
            "请清理迁移残留后使用所选 SDK。"
        )
    arguments.extend(("-AICoreFrameworkRoot", str(framework)))


def format_command_preview(
    settings: LauncherSettings,
    debug_ports: DebugPortBundle | None = None,
) -> str:
    return subprocess.list2cmdline(
        [settings.executable, *build_launch_arguments(settings, debug_ports)]
    )


def _is_tcp_port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            if os.name == "nt":
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            candidate.bind((host, port))
    except OSError:
        return False
    return True



def _check_server_udp_port(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as candidate:
            if os.name == "nt":
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            candidate.bind(("0.0.0.0", port))
    except OSError as exc:
        raise LauncherError(
            f"无法监听 UDP {port}（{exc}）；请关闭占用端口的服务端，"
            "或在 Host、Client 配置中选择相同的空闲端口。"
        ) from exc


class DebugPortAllocator:
    """Reserve reusable per-instance ScriptDebugger, bridge, and DAP ports."""

    def __init__(
        self,
        port_available: Callable[[int], bool] | None = None,
        *,
        host: str = DEFAULT_NETWORK_HOST,
    ) -> None:
        self._port_available = port_available or (
            lambda port: _is_tcp_port_available(host, port)
        )
        self._reserved: set[int] = set()
        self._lock = threading.RLock()

    def allocate(self) -> DebugPortBundle:
        with self._lock:
            index = 0
            while True:
                bundle = DebugPortBundle(
                    target_port=DEFAULT_SCRIPT_DEBUG_PORT + index * 2,
                    bridge_port=DEFAULT_BRIDGE_PORT + index * 2,
                    dap_port=DEFAULT_DAP_PORT + index,
                )
                ports = (bundle.target_port, bundle.bridge_port, bundle.dap_port)
                if max(ports) > MAX_PORT:
                    raise LauncherError("没有可用的 Lua 调试端口组。")
                if all(
                    port not in self._reserved and self._port_available(port)
                    for port in ports
                ):
                    self._reserved.update(ports)
                    return bundle
                index += 1

    def reserve(
        self,
        bundle: DebugPortBundle,
        *,
        allow_active_target: bool = False,
    ) -> bool:
        """Reserve a bundle and report whether its target port is available."""

        with self._lock:
            ports = (bundle.target_port, bundle.bridge_port, bundle.dap_port)
            if any(port in self._reserved for port in ports):
                raise LauncherError(
                    "Lua 调试端口组已被当前启动器保留："
                    + ", ".join(str(port) for port in ports)
                )
            availability = {
                port: self._port_available(port)
                for port in ports
            }
            required_ports = (
                (bundle.bridge_port, bundle.dap_port)
                if allow_active_target
                else ports
            )
            unavailable = [
                port for port in required_ports if not availability[port]
            ]
            if unavailable:
                raise LauncherError(
                    "Lua 调试端口不可用："
                    + ", ".join(str(port) for port in unavailable)
                )
            self._reserved.update(ports)
            return availability[bundle.target_port]

    def release(self, bundle: DebugPortBundle) -> None:
        with self._lock:
            self._reserved.difference_update(
                (bundle.target_port, bundle.bridge_port, bundle.dap_port)
            )


class ManagedProcess(Protocol):
    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self, exit_code: int = 1) -> None: ...

    def wait(self, timeout_ms: int) -> bool: ...

    def close(self) -> None: ...


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJobProcess:
    """A process tree owned by an anonymous Windows Job Object."""

    CREATE_SUSPENDED = 0x00000004
    CREATE_NEW_CONSOLE = 0x00000010
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    STILL_ACTIVE = 259

    def __init__(self, process_handle: int, job_handle: int, pid: int, executable: str = "", arguments: Sequence[str] = ()) -> None:
        self._kernel32 = _load_kernel32()
        self._process_handle = process_handle
        self._job_handle = job_handle
        self._pid = pid
        self._executable = executable
        self._arguments = tuple(arguments)
        self._exit_code: int | None = None
        self._lock = threading.RLock()

    @property
    def pid(self) -> int:
        return self._pid

    @classmethod
    def launch(
        cls,
        executable: str,
        arguments: Sequence[str],
        show_console: bool = True,
        environment_overrides: Mapping[str, str] | None = None,
        initial_window_rect: Rect | None = None,
    ) -> WindowsJobProcess:
        if os.name != "nt":
            raise LauncherError("该启动器仅支持 Windows。")

        executable_path = Path(os.path.expandvars(executable)).expanduser()
        try:
            executable_path = executable_path.resolve(strict=True)
        except OSError as exc:
            raise LauncherError(f"启动程序不存在：{executable_path}") from exc
        if not executable_path.is_file():
            raise LauncherError(f"启动程序不是文件：{executable_path}")

        network = parse_network_arguments(arguments)
        if network.role is NetworkRole.HOST:
            _check_server_udp_port(network.port)
        kernel32 = _load_kernel32()
        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            raise _windows_error("创建 Job Object 失败")

        process_info = _PROCESS_INFORMATION()
        process_created = False
        process_assigned = False
        try:
            limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = cls.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job_handle,
                cls.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise _windows_error("设置 Job Object 失败")

            startup = _STARTUPINFOW()
            startup.cb = ctypes.sizeof(startup)
            if initial_window_rect is not None:
                startup.dwFlags |= 0x0004 | 0x0002  # STARTF_USEPOSITION | STARTF_USESIZE
                startup.dwX, startup.dwY = initial_window_rect.left, initial_window_rect.top
                startup.dwXSize, startup.dwYSize = initial_window_rect.width, initial_window_rect.height
            dedicated_aicore = (executable_path.stem.casefold() in ("aicore", "aicore_profile", "aicore_d")
                                and parse_network_arguments(arguments).is_dedicated)
            if dedicated_aicore and not show_console:
                startup.dwFlags |= 1  # STARTF_USESHOWWINDOW, as Start-Process -WindowStyle Hidden
                startup.wShowWindow = 0
            command_line = subprocess.list2cmdline(
                [str(executable_path), *list(arguments)]
            )
            command_buffer = ctypes.create_unicode_buffer(command_line)
            creation_flags = cls.CREATE_SUSPENDED | cls.CREATE_NEW_PROCESS_GROUP
            # AICore DS allocates and initializes its own log console / Ctrl+C handler.
            # Precreating a console makes its AllocConsole fail and skips that setup.
            if not (dedicated_aicore and show_console):
                creation_flags |= cls.CREATE_NEW_CONSOLE if show_console else cls.CREATE_NO_WINDOW
            environment_pointer = None
            environment_buffer = None
            if environment_overrides:
                environment = dict(os.environ)
                environment.update(environment_overrides)
                environment_text = "\0".join(
                    f"{key}={value}"
                    for key, value in sorted(
                        environment.items(), key=lambda item: item[0].casefold()
                    )
                ) + "\0"
                environment_buffer = ctypes.create_unicode_buffer(environment_text)
                environment_pointer = ctypes.cast(
                    environment_buffer,
                    wintypes.LPWSTR,
                )
                creation_flags |= cls.CREATE_UNICODE_ENVIRONMENT
            if not kernel32.CreateProcessW(
                str(executable_path),
                command_buffer,
                None,
                None,
                False,
                creation_flags,
                environment_pointer,
                str(executable_path.parent),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            ):
                raise _windows_error("启动 App 失败")
            process_created = True

            if not kernel32.AssignProcessToJobObject(
                job_handle, process_info.hProcess
            ):
                raise _windows_error("将 App 加入 Job Object 失败")
            process_assigned = True

            resume_result = kernel32.ResumeThread(process_info.hThread)
            if resume_result == 0xFFFFFFFF:
                raise _windows_error("恢复 App 主线程失败")

            kernel32.CloseHandle(process_info.hThread)
            process_info.hThread = None
            return cls(
                process_handle=process_info.hProcess,
                job_handle=job_handle,
                pid=int(process_info.dwProcessId),
                executable=str(executable_path), arguments=arguments,
            )
        except Exception as original_error:
            cleanup_errors: list[str] = []
            if process_created and process_info.hProcess:
                if process_assigned:
                    terminated = kernel32.TerminateJobObject(job_handle, 1)
                    cleanup_action = "终止启动失败的 Job Object"
                else:
                    terminated = kernel32.TerminateProcess(process_info.hProcess, 1)
                    cleanup_action = "终止尚未纳管的挂起进程"
                if not terminated:
                    cleanup_errors.append(str(_windows_error(cleanup_action)))
                wait_result = kernel32.WaitForSingleObject(process_info.hProcess, 5000)
                if wait_result != cls.WAIT_OBJECT_0:
                    cleanup_errors.append(
                        f"等待启动失败的进程退出失败（WaitForSingleObject={wait_result}）"
                    )
            if process_info.hThread:
                kernel32.CloseHandle(process_info.hThread)
            if process_info.hProcess:
                kernel32.CloseHandle(process_info.hProcess)
            kernel32.CloseHandle(job_handle)
            if cleanup_errors:
                raise LauncherError(
                    f"{original_error}；安全清理同时失败：{'；'.join(cleanup_errors)}"
                ) from original_error
            raise

    def poll(self) -> int | None:
        with self._lock:
            if not self._process_handle:
                return self._exit_code
            wait_result = self._kernel32.WaitForSingleObject(self._process_handle, 0)
            if wait_result == self.WAIT_TIMEOUT:
                return None
            if wait_result != self.WAIT_OBJECT_0:
                raise _windows_error("查询 App 状态失败")

            exit_code = wintypes.DWORD()
            if not self._kernel32.GetExitCodeProcess(
                self._process_handle, ctypes.byref(exit_code)
            ):
                raise _windows_error("读取 App 退出码失败")
            self._exit_code = int(exit_code.value)
            return self._exit_code

    def terminate(self, exit_code: int = 1) -> None:
        with self._lock:
            if not self._job_handle or self.poll() is not None:
                return
            if (Path(self._executable).stem.casefold() in ("aicore", "aicore_profile", "aicore_d")
                    and request_stop(self.pid, self._arguments, lambda: self.poll() is None,
                                     working_directory=str(Path(self._executable).parent))
                    and self.wait(15000)):
                return
            if not self._kernel32.TerminateJobObject(self._job_handle, exit_code):
                raise _windows_error("关闭 App 进程树失败")

    def wait(self, timeout_ms: int) -> bool:
        with self._lock:
            if not self._process_handle:
                return True
            wait_result = self._kernel32.WaitForSingleObject(
                self._process_handle, max(0, int(timeout_ms))
            )
            if wait_result == self.WAIT_OBJECT_0:
                self.poll()
                return True
            if wait_result == self.WAIT_TIMEOUT:
                return False
            raise _windows_error("等待 App 退出失败")

    def wait_network_ready(self, port: int, cancel: threading.Event, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.poll() is not None:
                raise LauncherError(f"服务端（PID {self.pid}）已退出，查看实例日志。")
            if cancel.is_set():
                raise LauncherError("已取消后续 Client 启动。")
            if port in owned_ports(self.pid, udp=True):
                return
            cancel.wait(0.2)
        raise LauncherError(f"服务端（PID {self.pid}）未在 {timeout:g} 秒内监听 UDP {port}。")

    def close(self) -> None:
        with self._lock:
            if self._job_handle:
                # KILL_ON_JOB_CLOSE guarantees that descendants cannot outlive this owner.
                self._kernel32.CloseHandle(self._job_handle)
                self._job_handle = 0
            if self._process_handle:
                self._kernel32.CloseHandle(self._process_handle)
                self._process_handle = 0

    def __enter__(self) -> WindowsJobProcess:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _load_kernel32() -> ctypes.WinDLL:
    if os.name != "nt":
        raise LauncherError("该启动器仅支持 Windows。")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _windows_error(prefix: str) -> LauncherError:
    code = ctypes.get_last_error()
    detail = ctypes.FormatError(code).strip() if code else "未知错误"
    return LauncherError(f"{prefix}（Windows 错误 {code}：{detail}）")


class ControllerState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"


@dataclass(frozen=True)
class ControllerSnapshot:
    instance_id: int
    state: ControllerState
    pid: int | None = None
    exit_code: int | None = None
    settings: LauncherSettings | None = None
    debug_ports: DebugPortBundle | None = None
    arguments: tuple[str, ...] = ()
    label: str = ""

    @property
    def display_name(self) -> str:
        return f"实例 {self.instance_id}" + (f"「{self.label}」" if self.label else "")


@dataclass(frozen=True)
class InstanceBatchResult:
    snapshots: tuple[ControllerSnapshot, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClientLaunchResult:
    """Clients started by one batch; a failure stops the batch but keeps earlier Clients."""

    clients: tuple[ControllerSnapshot, ...] = ()
    requested: int = 1
    error: str = ""


@dataclass(frozen=True)
class PairLaunchResult:
    host: ControllerSnapshot
    clients: tuple[ControllerSnapshot, ...] = ()
    host_reused: bool = False
    error: str = ""
    requested_clients: int = 1

    @property
    def client(self) -> ControllerSnapshot | None:
        return self.clients[0] if self.clients else None


ProcessFactory = Callable[
    [str, Sequence[str], bool, Mapping[str, str], Rect | None],
    ManagedProcess,
]


@dataclass
class _ManagedInstance:
    instance_id: int
    settings: LauncherSettings
    debug_ports: DebugPortBundle
    arguments: tuple[str, ...]
    process: ManagedProcess
    label: str = ""
    had_clients: bool = False


MAX_CLIENT_COUNT = 16


def validate_client_count(count: int, auto_dev_account: bool) -> None:
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_CLIENT_COUNT:
        raise ConfigurationError(f"Client 数量必须是 1 到 {MAX_CLIENT_COUNT} 之间的整数。")
    if count > 1 and not auto_dev_account:
        raise ConfigurationError(
            "该 Client 配置使用固定开发账号，不能一次启动多个；"
            "请在配置管理中启用“多开时自动分配不同账号”，或把数量设为 1。"
        )


class LauncherController:
    """Thread-safe owner of every process tree launched by this GUI instance."""

    def __init__(
        self,
        process_factory: ProcessFactory | None = None,
        port_allocator: DebugPortAllocator | None = None,
    ) -> None:
        self._process_factory = process_factory or WindowsJobProcess.launch
        self._native_process_factory = process_factory is None
        self._port_allocator = port_allocator or DebugPortAllocator()
        self._instances: dict[int, _ManagedInstance] = {}
        self._debug_slots: list[DebugPortBundle] = []
        self._idle_debug_slots: list[DebugPortBundle] = []
        self._next_instance_id = 1
        self._closed = False
        self._lock = threading.RLock()

    def snapshots(self) -> tuple[ControllerSnapshot, ...]:
        with self._lock:
            self._reap_finished_processes()
            return tuple(
                self._snapshot(instance)
                for instance in self._instances.values()
            )

    def snapshot(self, instance_id: int | None = None) -> ControllerSnapshot:
        """Return one instance; the optional form preserves legacy single-instance use."""

        with self._lock:
            self._reap_finished_processes()
            if instance_id is None:
                if not self._instances:
                    return ControllerSnapshot(0, ControllerState.STOPPED)
                if len(self._instances) != 1:
                    raise LauncherError("存在多个实例，请指定 instance_id。")
                instance = next(iter(self._instances.values()))
            else:
                instance = self._instances.get(instance_id)
                if instance is None:
                    raise LauncherError(f"实例 {instance_id} 不存在或已退出。")
            return self._snapshot(instance)

    def reserve_debug_slot(
        self,
        preferred: DebugPortBundle | None = None,
        *,
        allow_active_target: bool = False,
    ) -> DebugPortBundle:
        """Reserve a persistent debugger slot before an App process starts."""

        with self._lock:
            if self._closed:
                raise LauncherError("启动器正在关闭，不能创建调试服务。")
            if preferred is not None:
                if preferred in self._debug_slots:
                    return preferred
                target_available = self._port_allocator.reserve(
                    preferred,
                    allow_active_target=allow_active_target,
                )
                debug_ports = preferred
            else:
                debug_ports = self._port_allocator.allocate()
                target_available = True
            self._debug_slots.append(debug_ports)
            if target_available:
                self._idle_debug_slots.append(debug_ports)
            return debug_ports

    def start(self, settings: LauncherSettings, label: str = "") -> ControllerSnapshot:
        with self._lock:
            if self._closed:
                raise LauncherError("启动器正在关闭，不能再启动 App。")
            self._reap_finished_processes()
            self._validate_network_identity(settings)
            debug_ports = self._acquire_debug_slot()
            arguments = build_launch_arguments(settings, debug_ports)
            try:
                process = self._process_factory(
                    settings.executable,
                    arguments,
                    settings.show_console,
                    build_process_environment_overrides(settings, debug_ports),
                    settings.initial_window_rect,
                )
            except Exception:
                self._release_debug_slot(debug_ports)
                raise
            instance_id = self._next_instance_id
            self._next_instance_id += 1
            instance = _ManagedInstance(
                instance_id,
                settings,
                debug_ports,
                arguments,
                process,
                label,
            )
            self._instances[instance_id] = instance
            if settings.network.role is NetworkRole.CLIENT:
                for server in self._instances.values():
                    if self._client_matches_server(instance, server):
                        server.had_clients = True
            return self._snapshot(instance)

    def ensure_host(
        self, settings: LauncherSettings, label: str = "",
    ) -> tuple[ControllerSnapshot, bool]:
        """Reuse only a managed Host with the same executable and arguments."""
        if settings.network.role is not NetworkRole.HOST:
            raise ConfigurationError("Host 配置的网络角色必须是 Host。")
        with self._lock:
            if self._closed:
                raise LauncherError("启动器正在关闭，不能再启动 Host。")
            self._reap_finished_processes()
            for instance in self._instances.values():
                existing = instance.settings
                if (
                    existing.network.role is NetworkRole.HOST
                    and existing.network.port == settings.network.port
                ):
                    same_executable = os.path.normcase(os.path.normpath(existing.executable)) == (
                        os.path.normcase(os.path.normpath(settings.executable))
                    )
                    if same_executable and existing.enabled_arguments == settings.enabled_arguments:
                        return self._snapshot(instance), True
                    raise LauncherError(
                        f"端口 {settings.network.port} 上已有参数不同的 Host"
                        f"（{self._snapshot(instance).display_name}），"
                        "请先关闭该实例，或选择使用其他端口的配置。"
                    )
            return self.start(settings, label), False

    def start_client(
        self, settings: LauncherSettings, *, auto_dev_account: bool = True, label: str = "",
    ) -> ControllerSnapshot:
        if settings.network.role is not NetworkRole.CLIENT:
            raise ConfigurationError("Client 配置的网络角色必须是 Client。")
        with self._lock:
            self._reap_finished_processes()
            if auto_dev_account:
                network = settings.network
                # 开发账号锁是本机全局的：Host 与所有 Client（不论房间）占用的序号都要跳过。
                used = {
                    instance.settings.network.dev_account
                    for instance in self._instances.values()
                    if instance.settings.network.dev_account
                }
                dev_account = network.dev_account or default_dev_account(NetworkRole.CLIENT)
                if self._native_process_factory and Path(settings.executable).stem.casefold() in ("aicore", "aicore_profile", "aicore_d"):
                    try:
                        dev_account = available_dev_account(dev_account, used, settings.enabled_arguments, settings.executable)
                    except (OSError, ValueError) as exc:
                        raise LauncherError(str(exc)) from None
                else:
                    while dev_account in used:
                        dev_account += 1
                if dev_account != network.dev_account:
                    arguments = replace(network, dev_account=dev_account).arguments() + apply_network_preset(
                        settings.enabled_arguments, NetworkRole.STANDALONE,
                    )
                    settings = replace(settings, arguments=arguments, argument_enabled=None)
            return self.start(settings, label)

    def start_clients(
        self,
        settings: LauncherSettings,
        count: int = 1,
        *,
        auto_dev_account: bool = True,
        label: str = "",
        cancel: threading.Event | None = None,
        window_rects: Sequence[Rect] | None = None,
        on_client_started: Callable[[ControllerSnapshot], None] | None = None,
    ) -> ClientLaunchResult:
        """Start several Clients from one configuration, each with its own dev account."""
        validate_client_count(count, auto_dev_account)
        if window_rects is not None and len(window_rects) != count:
            raise ConfigurationError("初始窗口位置数量必须与 Client 数量一致。")
        clients: list[ControllerSnapshot] = []
        try:
            for _index in range(count):
                if cancel is not None and cancel.is_set():
                    raise LauncherError("已取消后续 Client 启动。")
                launch_settings = replace(settings, initial_window_rect=window_rects[_index]) if window_rects is not None else settings
                snapshot = self.start_client(launch_settings, auto_dev_account=auto_dev_account, label=label)
                clients.append(snapshot)
                if on_client_started is not None:
                    on_client_started(snapshot)
        except Exception as exc:
            if not clients:
                raise
            return ClientLaunchResult(tuple(clients), count, str(exc))
        return ClientLaunchResult(tuple(clients), count)

    def start_pair(
        self,
        host: LauncherSettings,
        client: LauncherSettings,
        *,
        delay_seconds: float = 2.0,
        auto_dev_account: bool = True,
        client_count: int = 1,
        cancel: threading.Event | None = None,
        on_host_started: Callable[[ControllerSnapshot, bool], None] | None = None,
        host_label: str = "",
        client_label: str = "",
        client_window_rects: Sequence[Rect] | None = None,
        on_client_started: Callable[[ControllerSnapshot], None] | None = None,
    ) -> PairLaunchResult:
        """Start Host first, then the Clients; keep the Host managed if a Client fails."""
        if client.network.role is not NetworkRole.CLIENT:
            raise ConfigurationError("Client 配置的网络角色必须是 Client。")
        if not 0 <= delay_seconds <= 60:
            raise ConfigurationError("Client 启动间隔必须在 0 到 60 秒之间。")
        validate_client_count(client_count, auto_dev_account)
        if client_window_rects is not None and len(client_window_rects) != client_count:
            raise ConfigurationError("初始窗口位置数量必须与 Client 数量一致。")
        cancel = cancel or threading.Event()
        if cancel.is_set():
            raise LauncherError("已取消启动。")
        snapshot, reused = self.ensure_host(host, host_label)
        if on_host_started is not None:
            on_host_started(snapshot, reused)
        try:
            if cancel.wait(0 if reused else delay_seconds):
                raise LauncherError("已取消后续 Client 启动。")
            self.snapshot(snapshot.instance_id)  # Host may have exited during the delay.
            with self._lock:
                server_process = self._instances[snapshot.instance_id].process
            wait_ready = getattr(server_process, "wait_network_ready", None)
            if wait_ready is not None:
                wait_ready(host.network.port, cancel)
            launched = self.start_clients(
                client, client_count, auto_dev_account=auto_dev_account,
                label=client_label, cancel=cancel,
                window_rects=client_window_rects, on_client_started=on_client_started,
            )
        except Exception as exc:
            return PairLaunchResult(
                snapshot, host_reused=reused, error=str(exc), requested_clients=client_count,
            )
        return PairLaunchResult(snapshot, launched.clients, reused, launched.error, client_count)

    def stop(self, instance_id: int, timeout_ms: int = 5000) -> ControllerSnapshot:
        with self._lock:
            self._reap_finished_processes()
            instance = self._instances.get(instance_id)
            if instance is None:
                raise LauncherError(f"实例 {instance_id} 不存在或已退出。")
            stopped = self._stop_process(instance, timeout_ms)
            del self._instances[instance_id]
            self._release_debug_slot(instance.debug_ports)
            return stopped

    @staticmethod
    def _stop_process(instance: _ManagedInstance, timeout_ms: int) -> ControllerSnapshot:
        process = instance.process
        process.terminate()
        if not process.wait(timeout_ms):
            raise LauncherError(f"等待 App（PID {process.pid}）退出超时。")
        exit_code = process.poll()
        process.close()
        return ControllerSnapshot(
            instance.instance_id,
            ControllerState.STOPPED,
            exit_code=exit_code,
            settings=instance.settings,
            debug_ports=instance.debug_ports,
            arguments=instance.arguments,
            label=instance.label,
        )

    def stop_many(self, instance_ids: Sequence[int], timeout_ms: int = 5000) -> InstanceBatchResult:
        """Issue independent graceful stops concurrently, then collect all outcomes."""
        instance_ids = tuple(dict.fromkeys(instance_ids))
        if not instance_ids:
            return InstanceBatchResult()
        with self._lock:
            self._reap_finished_processes()
            stopped: list[ControllerSnapshot] = []
            errors: list[str] = []
            # Retain ownership while workers wait on separate process handles. Workers must
            # not call stop(): its controller lock would serialize the HTTP/exit waits again.
            with ThreadPoolExecutor(max_workers=len(instance_ids), thread_name_prefix="app-stop") as executor:
                futures = {
                    instance_id: executor.submit(self._stop_process, self._instances[instance_id], timeout_ms)
                    for instance_id in instance_ids if instance_id in self._instances
                }
                for instance_id in instance_ids:
                    future = futures.get(instance_id)
                    if future is None:
                        errors.append(f"实例 {instance_id} 不存在或已退出。")
                        continue
                    try:
                        stopped.append(future.result())
                    except Exception as exc:
                        errors.append(f"实例 {instance_id}：{exc}")
                    else:
                        instance = self._instances.pop(instance_id)
                        self._release_debug_slot(instance.debug_ports)
            return InstanceBatchResult(tuple(stopped), tuple(errors))

    def restart(self, instance_id: int, timeout_ms: int = 5000) -> ControllerSnapshot:
        with self._lock:
            if self._closed:
                raise LauncherError("启动器正在关闭，不能重新启动 App。")
            self._reap_finished_processes()
            instance = self._instances.get(instance_id)
            if instance is None:
                raise LauncherError(f"实例 {instance_id} 不存在或已退出。")
            old_process = instance.process
            old_process.terminate()
            if not old_process.wait(timeout_ms):
                raise LauncherError(
                    f"等待 App（PID {old_process.pid}）退出超时。"
                )
            old_process.close()
            try:
                process = self._process_factory(
                    instance.settings.executable,
                    instance.arguments,
                    instance.settings.show_console,
                    build_process_environment_overrides(
                        instance.settings,
                        instance.debug_ports,
                    ),
                    instance.settings.initial_window_rect,
                )
            except Exception:
                del self._instances[instance_id]
                self._release_debug_slot(instance.debug_ports)
                raise
            instance.process = process
            return self._snapshot(instance)

    def stop_all(self, timeout_ms: int = 5000) -> tuple[ControllerSnapshot, ...]:
        with self._lock:
            self._reap_finished_processes()
            result = self.stop_many(tuple(self._instances), timeout_ms)
        if result.errors:
            raise LauncherError("关闭全部实例失败：" + "；".join(result.errors))
        return result.snapshots

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            instances = tuple(self._instances.values())
            self._instances.clear()
        for instance in instances:
            try:
                instance.process.terminate()
                instance.process.wait(3000)
            finally:
                instance.process.close()
        for debug_ports in self._debug_slots:
            self._port_allocator.release(debug_ports)
        self._debug_slots.clear()
        self._idle_debug_slots.clear()

    @staticmethod
    def _client_matches_server(client: _ManagedInstance, server: _ManagedInstance) -> bool:
        source, target = client.settings.network, server.settings.network
        return (target.is_dedicated and source.role is NetworkRole.CLIENT
                and source.server_mode == "CloudServer" and source.host.casefold() in ("127.0.0.1", "localhost", "::1")
                and source.port == target.port and source.room_id == target.room_id)

    def empty_dedicated_host_ids(self) -> tuple[int, ...]:
        """The UI closes these in a worker, after the last matching client exits."""
        with self._lock:
            self._reap_finished_processes()
            return tuple(server.instance_id for server in self._instances.values()
                         if server.settings.network.is_dedicated and server.had_clients
                         and not any(self._client_matches_server(client, server) for client in self._instances.values()))

    def _validate_network_identity(self, settings: LauncherSettings) -> None:
        network = settings.network
        for instance in self._instances.values():
            existing = instance.settings.network
            if (
                network.role is NetworkRole.HOST
                and existing.role is NetworkRole.HOST
                and existing.port == network.port
            ):
                raise LauncherError(
                    f"Host 端口 {network.port} 已被实例 "
                    f"{instance.instance_id} 使用。"
                )
            if network.dev_account and existing.dev_account == network.dev_account:
                raise LauncherError(
                    f"开发账号 {network.dev_account} 已被{self._snapshot(instance).display_name}"
                    "占用（同一台机器每个进程须用不同账号），请修改开发账号序号或启用自动分配后再启动。"
                )

    def _acquire_debug_slot(self) -> DebugPortBundle:
        if self._idle_debug_slots:
            return self._idle_debug_slots.pop(0)
        debug_ports = self._port_allocator.allocate()
        self._debug_slots.append(debug_ports)
        return debug_ports

    def _release_debug_slot(self, debug_ports: DebugPortBundle) -> None:
        if (
            debug_ports in self._debug_slots
            and debug_ports not in self._idle_debug_slots
        ):
            self._idle_debug_slots.append(debug_ports)
            self._idle_debug_slots.sort(key=lambda bundle: bundle.dap_port)

    @staticmethod
    def _snapshot(instance: _ManagedInstance) -> ControllerSnapshot:
        return ControllerSnapshot(
            instance.instance_id,
            ControllerState.RUNNING,
            pid=instance.process.pid,
            settings=instance.settings,
            debug_ports=instance.debug_ports,
            arguments=instance.arguments,
            label=instance.label,
        )

    def _reap_finished_processes(self) -> None:
        finished: list[int] = []
        for instance_id, instance in self._instances.items():
            if instance.process.poll() is not None:
                instance.process.close()
                self._release_debug_slot(instance.debug_ports)
                finished.append(instance_id)
        for instance_id in finished:
            del self._instances[instance_id]
