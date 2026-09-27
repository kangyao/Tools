from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
from enum import Enum
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
from typing import Callable, Mapping, Protocol, Sequence
import xml.etree.ElementTree as ET


DEFAULT_EXECUTABLE = r"C:\MiniGame\Bin64\AIFramework_d.exe"
DEFAULT_ARGUMENTS = ("-MGFTopBattle", "-script-debug-wait-client")
CONFIG_VERSION = 1
WINDOW_GEOMETRY_PATTERN = re.compile(r"^[1-9]\d*x[1-9]\d*[+-]\d+[+-]\d+$")
APP_CONFIGS_START_PATTERN = re.compile(
    r"^\s*Games\.AppConfigs\s*=\s*\{\s*(?:--.*)?$"
)
APP_CONFIGS_END_PATTERN = re.compile(r"^\s*\}\s*(?:--.*)?$")
APP_CONFIG_ARGUMENT_PATTERN = re.compile(
    r"^\s*\{\s*(?P<quote>[\"'])(?P<name>[^\"']+)(?P=quote)"
    r"\s*,\s*function\s*\(\s*\)"
)
MGF_ARGUMENT_PREFIX = "-MGF"
TOP_BATTLE_ARGUMENT = "-MGFTopBattle"
DEBUG_WAIT_ARGUMENT = "-script-debug-wait-client"
LUA_DEBUG_PORT_ARGUMENT = "-lua-debug-port"
MCP_CLIENT_NAME_ENVIRONMENT_VARIABLE = "MINIGAME_MCP_CLIENT_NAME"
DEFAULT_NETWORK_HOST = "127.0.0.1"
DEFAULT_NETWORK_PORT = 19120
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


def get_file_revision(path: Path) -> tuple[int, int] | None:
    """Return a cheap revision token that changes when a watched file is replaced."""

    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class LauncherError(RuntimeError):
    """Base error shown by the launcher UI."""


class ConfigurationError(LauncherError):
    """The local settings file is invalid."""


class NetworkRole(str, Enum):
    STANDALONE = "standalone"
    LOGIN = "login"
    HOST = "host"
    CLIENT = "client"


@dataclass(frozen=True)
class LauncherSettings:
    executable: str = DEFAULT_EXECUTABLE
    arguments: tuple[str, ...] = DEFAULT_ARGUMENTS
    show_console: bool = True
    window_geometry: str = ""
    argument_enabled: tuple[bool, ...] | None = None
    network_role: NetworkRole = NetworkRole.STANDALONE
    network_host: str = DEFAULT_NETWORK_HOST
    network_port: int = DEFAULT_NETWORK_PORT

    def __post_init__(self) -> None:
        try:
            role = NetworkRole(self.network_role)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "网络角色必须是 standalone、login、host 或 client。"
            ) from exc
        object.__setattr__(self, "network_role", role)
        if not isinstance(self.network_host, str) or not self.network_host.strip():
            raise ConfigurationError("网络主机地址必须是非空字符串。")
        if (
            isinstance(self.network_port, bool)
            or not isinstance(self.network_port, int)
            or not 1 <= self.network_port <= MAX_PORT
        ):
            raise ConfigurationError(f"网络端口必须是 1 到 {MAX_PORT} 之间的整数。")

        states = self.argument_enabled
        if states is None:
            object.__setattr__(
                self,
                "argument_enabled",
                tuple(True for _argument in self.arguments),
            )
            return
        if len(states) != len(self.arguments) or not all(
            isinstance(state, bool) for state in states
        ):
            raise ConfigurationError(
                "启动参数的启用状态必须是与参数列表等长的布尔值数组。"
            )

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


class ConfigStore:
    """Versioned, atomic JSON storage kept next to the tool."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> LauncherSettings:
        if not self.path.exists():
            return LauncherSettings()

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"无法读取配置文件：{exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigurationError("配置文件根节点必须是 JSON 对象。")
        if raw.get("version") != CONFIG_VERSION:
            raise ConfigurationError(
                f"不支持的配置版本：{raw.get('version')!r}，当前版本为 {CONFIG_VERSION}。"
            )

        executable = raw.get("executable")
        arguments = raw.get("arguments")
        argument_enabled = raw.get("argument_enabled")
        show_console = raw.get("show_console", True)
        window_geometry = raw.get("window_geometry", "")
        network_role = raw.get("network_role", NetworkRole.STANDALONE.value)
        network_host = raw.get("network_host", DEFAULT_NETWORK_HOST)
        network_port = raw.get("network_port", DEFAULT_NETWORK_PORT)
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

        return LauncherSettings(
            executable=executable,
            arguments=tuple(arguments),
            argument_enabled=tuple(argument_enabled),
            show_console=show_console,
            window_geometry=window_geometry,
            network_role=network_role,
            network_host=network_host,
            network_port=network_port,
        )

    def save(self, settings: LauncherSettings) -> None:
        payload = {
            "version": CONFIG_VERSION,
            "executable": settings.executable,
            "arguments": list(settings.arguments),
            "argument_enabled": list(settings.argument_enabled or ()),
            "show_console": settings.show_console,
            "window_geometry": settings.window_geometry,
            "network_role": settings.network_role.value,
            "network_host": settings.network_host,
            "network_port": settings.network_port,
        }
        if not is_valid_window_geometry(settings.window_geometry):
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


def try_discover_argument_suggestions(
    games_init_path: Path,
) -> tuple[str, ...] | None:
    """Read ordered game routes, returning None when the source is unavailable."""

    try:
        source = games_init_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None

    suggestions: list[str] = []
    in_app_configs = False
    for line in source.splitlines():
        if not in_app_configs:
            in_app_configs = APP_CONFIGS_START_PATTERN.fullmatch(line) is not None
            continue
        if APP_CONFIGS_END_PATTERN.fullmatch(line) is not None:
            break
        match = APP_CONFIG_ARGUMENT_PATTERN.match(line)
        if match is None:
            continue
        name = match.group("name").strip()
        if name:
            suggestions.append(name if name.startswith("-") else f"-{name}")

    suggestions.append(DEBUG_WAIT_ARGUMENT)
    return tuple(dict.fromkeys(suggestions))


def discover_argument_suggestions(games_init_path: Path) -> tuple[str, ...]:
    """Read game routes, falling back to defaults when init.lua is unavailable."""

    suggestions = try_discover_argument_suggestions(games_init_path)
    return suggestions if suggestions is not None else DEFAULT_ARGUMENTS


def is_mgf_argument(argument: str) -> bool:
    return argument.casefold().startswith(MGF_ARGUMENT_PREFIX.casefold())


def normalize_fixed_arguments(
    available_arguments: Sequence[str],
    saved_arguments: Sequence[str] = (),
    saved_enabled: Sequence[bool] | None = None,
    exclusive_arguments: Sequence[str] | None = None,
) -> tuple[tuple[str, ...], tuple[bool, ...]]:
    """Return every fixed argument while allowing one exclusive selection."""
    fixed_arguments = tuple(dict.fromkeys(available_arguments))
    states = tuple(saved_enabled) if saved_enabled is not None else tuple(
        True for _argument in saved_arguments
    )
    saved_states = {
        argument: enabled
        for argument, enabled in zip(saved_arguments, states)
    }
    exclusive_set = set(
        exclusive_arguments
        if exclusive_arguments is not None
        else (
            argument
            for argument in fixed_arguments
            if is_mgf_argument(argument)
        )
    )
    exclusive_options = tuple(
        argument for argument in fixed_arguments if argument in exclusive_set
    )
    selected_exclusive = next(
        (
            argument
            for argument, enabled in zip(saved_arguments, states)
            if enabled and argument in exclusive_set
        ),
        None,
    )
    if selected_exclusive is None:
        selected_exclusive = next(
            (
                argument
                for argument in DEFAULT_ARGUMENTS
                if argument in exclusive_set
            ),
            exclusive_options[0] if exclusive_options else None,
        )

    enabled_arguments = tuple(
        argument == selected_exclusive
        if argument in exclusive_set
        else saved_states.get(argument, argument in DEFAULT_ARGUMENTS)
        for argument in fixed_arguments
    )
    return fixed_arguments, enabled_arguments


def filter_argument_suggestions(
    value: str, suggestions: Sequence[str]
) -> tuple[str, ...]:
    query = value.strip()
    if not query:
        return tuple(suggestions)
    normalized = query if query.startswith("-") else f"-{query}"
    lowered = normalized.casefold()
    return tuple(
        suggestion
        for suggestion in suggestions
        if suggestion.casefold().startswith(lowered)
    )


def get_effective_network_role(settings: LauncherSettings) -> NetworkRole:
    if TOP_BATTLE_ARGUMENT not in settings.enabled_arguments:
        return NetworkRole.STANDALONE
    return settings.network_role


def build_process_environment_overrides(
    settings: LauncherSettings,
    debug_ports: DebugPortBundle | None = None,
) -> dict[str, str]:
    """Give every launched game a stable, selectable MCP identity."""

    role = get_effective_network_role(settings).value.upper()
    port_suffix = f"-{debug_ports.target_port}" if debug_ports is not None else ""
    return {
        MCP_CLIENT_NAME_ENVIRONMENT_VARIABLE: f"AIFramework-{role}{port_suffix}"
    }


@dataclass(frozen=True)
class DebugPortBundle:
    target_port: int
    bridge_port: int
    dap_port: int


def build_launch_arguments(
    settings: LauncherSettings,
    debug_ports: DebugPortBundle | None = None,
) -> tuple[str, ...]:
    """Build the single argv source used by previews and process launches."""

    arguments = list(settings.enabled_arguments)
    role = get_effective_network_role(settings)
    if role is NetworkRole.LOGIN:
        arguments.append("-TopBattleNetworkLogin")
    elif role is NetworkRole.HOST:
        arguments.extend(
            ("-TopBattleNetworkHost", "-TopBattleListenPort", str(settings.network_port))
        )
    elif role is NetworkRole.CLIENT:
        arguments.extend(
            (
                "-TopBattleNetworkClient",
                "-TopBattleHost",
                settings.network_host.strip(),
                "-TopBattlePort",
                str(settings.network_port),
            )
        )
    if debug_ports is not None:
        arguments.extend((LUA_DEBUG_PORT_ARGUMENT, str(debug_ports.target_port)))
    return tuple(arguments)


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

    def __init__(self, process_handle: int, job_handle: int, pid: int) -> None:
        self._kernel32 = _load_kernel32()
        self._process_handle = process_handle
        self._job_handle = job_handle
        self._pid = pid
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
            command_line = subprocess.list2cmdline(
                [str(executable_path), *list(arguments)]
            )
            command_buffer = ctypes.create_unicode_buffer(command_line)
            creation_flags = cls.CREATE_SUSPENDED | cls.CREATE_NEW_PROCESS_GROUP
            creation_flags |= (
                cls.CREATE_NEW_CONSOLE if show_console else cls.CREATE_NO_WINDOW
            )
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
                raise _windows_error("启动 AIFramework 失败")
            process_created = True

            if not kernel32.AssignProcessToJobObject(
                job_handle, process_info.hProcess
            ):
                raise _windows_error("将 AIFramework 加入 Job Object 失败")
            process_assigned = True

            resume_result = kernel32.ResumeThread(process_info.hThread)
            if resume_result == 0xFFFFFFFF:
                raise _windows_error("恢复 AIFramework 主线程失败")

            kernel32.CloseHandle(process_info.hThread)
            process_info.hThread = None
            return cls(
                process_handle=process_info.hProcess,
                job_handle=job_handle,
                pid=int(process_info.dwProcessId),
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
                raise _windows_error("查询 AIFramework 状态失败")

            exit_code = wintypes.DWORD()
            if not self._kernel32.GetExitCodeProcess(
                self._process_handle, ctypes.byref(exit_code)
            ):
                raise _windows_error("读取 AIFramework 退出码失败")
            self._exit_code = int(exit_code.value)
            return self._exit_code

    def terminate(self, exit_code: int = 1) -> None:
        with self._lock:
            if not self._job_handle or self.poll() is not None:
                return
            if not self._kernel32.TerminateJobObject(self._job_handle, exit_code):
                raise _windows_error("关闭 AIFramework 进程树失败")

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
            raise _windows_error("等待 AIFramework 退出失败")

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


ProcessFactory = Callable[
    [str, Sequence[str], bool, Mapping[str, str]],
    ManagedProcess,
]


@dataclass
class _ManagedInstance:
    instance_id: int
    settings: LauncherSettings
    debug_ports: DebugPortBundle
    arguments: tuple[str, ...]
    process: ManagedProcess


class LauncherController:
    """Thread-safe owner of every process tree launched by this GUI instance."""

    def __init__(
        self,
        process_factory: ProcessFactory | None = None,
        port_allocator: DebugPortAllocator | None = None,
    ) -> None:
        self._process_factory = process_factory or WindowsJobProcess.launch
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
        """Reserve a persistent debugger slot before an AIFramework process starts."""

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

    def start(self, settings: LauncherSettings) -> ControllerSnapshot:
        with self._lock:
            if self._closed:
                raise LauncherError("启动器正在关闭，不能再启动 AIFramework。")
            self._reap_finished_processes()
            self._validate_host_endpoint(settings)
            debug_ports = self._acquire_debug_slot()
            arguments = build_launch_arguments(settings, debug_ports)
            try:
                process = self._process_factory(
                    settings.executable,
                    arguments,
                    settings.show_console,
                    build_process_environment_overrides(settings, debug_ports),
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
            )
            self._instances[instance_id] = instance
            return self._snapshot(instance)

    def stop(self, instance_id: int, timeout_ms: int = 5000) -> ControllerSnapshot:
        with self._lock:
            self._reap_finished_processes()
            instance = self._instances.get(instance_id)
            if instance is None:
                raise LauncherError(f"实例 {instance_id} 不存在或已退出。")
            process = instance.process
            process.terminate()
            if not process.wait(timeout_ms):
                raise LauncherError(
                    f"等待 AIFramework（PID {process.pid}）退出超时。"
                )
            exit_code = process.poll()
            process.close()
            del self._instances[instance_id]
            self._release_debug_slot(instance.debug_ports)
            return ControllerSnapshot(
                instance_id,
                ControllerState.STOPPED,
                exit_code=exit_code,
                settings=instance.settings,
                debug_ports=instance.debug_ports,
                arguments=instance.arguments,
            )

    def restart(self, instance_id: int, timeout_ms: int = 5000) -> ControllerSnapshot:
        with self._lock:
            if self._closed:
                raise LauncherError("启动器正在关闭，不能重新启动 AIFramework。")
            self._reap_finished_processes()
            instance = self._instances.get(instance_id)
            if instance is None:
                raise LauncherError(f"实例 {instance_id} 不存在或已退出。")
            old_process = instance.process
            old_process.terminate()
            if not old_process.wait(timeout_ms):
                raise LauncherError(
                    f"等待 AIFramework（PID {old_process.pid}）退出超时。"
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
                )
            except Exception:
                del self._instances[instance_id]
                self._release_debug_slot(instance.debug_ports)
                raise
            instance.process = process
            return self._snapshot(instance)

    def stop_all(self, timeout_ms: int = 5000) -> tuple[ControllerSnapshot, ...]:
        with self._lock:
            instance_ids = tuple(self._instances)
        stopped: list[ControllerSnapshot] = []
        errors: list[str] = []
        for instance_id in instance_ids:
            try:
                stopped.append(self.stop(instance_id, timeout_ms))
            except LauncherError as exc:
                errors.append(str(exc))
        if errors:
            raise LauncherError("关闭全部实例失败：" + "；".join(errors))
        return tuple(stopped)

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

    def _validate_host_endpoint(self, settings: LauncherSettings) -> None:
        if get_effective_network_role(settings) is not NetworkRole.HOST:
            return
        for instance in self._instances.values():
            if (
                get_effective_network_role(instance.settings) is NetworkRole.HOST
                and instance.settings.network_port == settings.network_port
            ):
                raise LauncherError(
                    f"TopBattle Host 端口 {settings.network_port} 已被实例 "
                    f"{instance.instance_id} 使用。"
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
