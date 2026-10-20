from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from pathlib import Path
from typing import Mapping
import uuid

from aicore_control import argument_value, resolve_account_file

from launcher_core import (
    ConfigStore,
    ConfigurationError,
    DEBUG_WAIT_ARGUMENT,
    DEFAULT_EXECUTABLE,
    DEFAULT_PROJECT_ROOT,
    DEFAULT_NETWORK_HOST,
    DEFAULT_NETWORK_PORT,
    DEFAULT_NETWORK_ROOM_ID,
    DEV_ACCOUNT_ARGUMENT,
    LEGACY_NETWORK_ARGUMENTS,
    LUA_DEBUG_PORT_ARGUMENT,
    LauncherSettings,
    MAX_CLIENT_COUNT,
    MAX_PORT,
    NETWORK_ARGUMENTS,
    NetworkOptions,
    NetworkRole,
    apply_network_preset,
    default_dev_account,
    is_valid_window_geometry,
    parse_argument_text,
    parse_network_arguments,
)


PROFILE_CONFIG_VERSION = 7
LAUNCH_ROLES = (NetworkRole.HOST, NetworkRole.CLIENT, NetworkRole.STANDALONE)
ROLE_TITLES = {NetworkRole.HOST: "Host", NetworkRole.CLIENT: "Client", NetworkRole.STANDALONE: "单机"}
SELECTION_FIELDS = {
    NetworkRole.HOST: "selected_host_id",
    NetworkRole.CLIENT: "selected_client_id",
    NetworkRole.STANDALONE: "selected_standalone_id",
}
LOCAL_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))
DEFAULT_CLIENT_DELAY_SECONDS = 2.0
INITIAL_CHARACTER_ARGUMENT = "-MGFInitialCharacter"
DEFAULT_INITIAL_CHARACTER = "avatar_default"
TEST_ARGUMENT_FIELDS = {
    "-aicoretestmode": "test_mode",
    "-aicorepeerticket": "peer_ticket",
    "-aicoretestserviceidentity": "service_identity",
}


def has_initial_character(arguments: tuple[str, ...]) -> bool:
    return any(argument.partition("=")[0].casefold() == INITIAL_CHARACTER_ARGUMENT.casefold()
               for argument in arguments)


def new_profile_id() -> str:
    return uuid.uuid4().hex


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _split_project_arguments(arguments: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    project = None
    extras = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        key, separator, value = argument.partition("=")
        if key.casefold() != "-aicoreroot":
            extras.append(argument)
        else:
            if project is not None:
                raise ConfigurationError("目标工程参数 -AICoreRoot 不能重复。")
            if not separator:
                index += 1
                if index >= len(arguments) or arguments[index].startswith("-"):
                    raise ConfigurationError("目标工程参数 -AICoreRoot 缺少值。")
                value = arguments[index]
            if not value.strip():
                raise ConfigurationError("目标工程参数 -AICoreRoot 缺少值。")
            project = value.strip()
        index += 1
    return project, tuple(extras)


@dataclass(frozen=True)
class LaunchProfile:
    """One named launch setting for a standalone, Host or Client App."""

    name: str
    role: NetworkRole
    executable: str = DEFAULT_EXECUTABLE
    host: str = DEFAULT_NETWORK_HOST
    port: int = DEFAULT_NETWORK_PORT
    room_id: int = DEFAULT_NETWORK_ROOM_ID
    # 账号序号（-MGFTestAccount）；0 表示取角色默认值（Host 1、Client 2）。
    dev_account: int = 0
    auto_dev_account: bool = True
    extra_arguments: tuple[str, ...] = ()
    show_console: bool = True
    debug_wait: bool = False
    id: str = field(default_factory=new_profile_id)
    server_mode: str = "CloudServer"
    project_root: str = DEFAULT_PROJECT_ROOT
    skip_character_selection: bool = True
    test_mode: bool = True
    peer_ticket: str = ""
    service_identity: str = "debug-launcher"

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ConfigurationError("配置标识必须是非空字符串。")
        for name, label in (("name", "配置名称"), ("executable", "程序路径"), ("host", "主机地址")):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(f"{label}不能为空。")
            object.__setattr__(self, name, value.strip())
        if self.server_mode not in ("CloudServer", "Host"):
            raise ConfigurationError("服务端模式必须是 CloudServer 或 Host。")
        if self.role not in LAUNCH_ROLES:
            raise ConfigurationError("配置类型必须是单机、Host 或 Client。")
        if not _is_integer(self.port) or not 1 <= self.port <= MAX_PORT:
            raise ConfigurationError(f"端口必须是 1 到 {MAX_PORT} 之间的整数。")
        if not _is_integer(self.room_id) or self.room_id <= 0:
            raise ConfigurationError("房间号必须是正整数。")
        if self.dev_account == 0 and _is_integer(self.dev_account):
            object.__setattr__(self, "dev_account", default_dev_account(self.role))
        if not _is_integer(self.dev_account) or self.dev_account < 0 or (
            self.role is not NetworkRole.STANDALONE and self.dev_account == 0
        ):
            raise ConfigurationError("开发账号序号必须是正整数。")
        for name, label in (
            ("auto_dev_account", "自动分配开发账号"), ("show_console", "显示控制台"), ("debug_wait", "等待 Lua 调试器"),
            ("skip_character_selection", "跳过角色选择"),
            ("test_mode", "本地联调测试模式"),
        ):
            if not isinstance(getattr(self, name), bool):
                raise ConfigurationError(f"{label}必须是布尔值。")
        if not isinstance(self.extra_arguments, tuple) or not all(
            isinstance(argument, str) for argument in self.extra_arguments
        ):
            raise ConfigurationError("额外参数必须是字符串列表。")
        if not isinstance(self.project_root, str):
            raise ConfigurationError("目标工程必须是目录路径。")
        project, extras = _split_project_arguments(self.extra_arguments)
        object.__setattr__(self, "project_root", self.project_root.strip() if project is None else project)
        object.__setattr__(self, "extra_arguments", extras)
        remaining = []
        index = 0
        while index < len(extras):
            argument = extras[index]
            key, separator, value = argument.partition("=")
            field_name = TEST_ARGUMENT_FIELDS.get(key.casefold())
            if field_name is None:
                remaining.append(argument)
            elif field_name == "test_mode":
                if separator and value != "true":
                    raise ConfigurationError("-AICoreTestMode 仅支持 true 或无值。")
                if not separator and extras[index + 1:index + 2] == ("true",):
                    index += 1
                object.__setattr__(self, field_name, True)
            else:
                if not separator:
                    index += 1
                    if index >= len(extras) or extras[index].startswith("-"):
                        raise ConfigurationError(f"{key} 缺少值。")
                    value = extras[index]
                object.__setattr__(self, field_name, value)
            index += 1
        object.__setattr__(self, "extra_arguments", tuple(remaining))
        for field_name, label, limit in (("peer_ticket", "联机票据", 32), ("service_identity", "服务身份", 128)):
            value = getattr(self, field_name)
            if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
                raise ConfigurationError(f"{label}必须是最多 {limit} 字节的字符串。")
        if self.network.is_dedicated and self.test_mode and not self.service_identity:
            raise ConfigurationError("本地联调的 CloudServer 需要服务身份。")
        for argument in self.extra_arguments:
            if argument in NETWORK_ARGUMENTS:
                raise ConfigurationError(
                    f"额外参数中包含网络参数 {argument}，请在对应的表单字段中修改。"
                )
            if argument in LEGACY_NETWORK_ARGUMENTS:
                raise ConfigurationError(
                    f"{argument} 已移除（客机身份改取登录账号），请删除；用“开发账号序号”选择账号。"
                )
        if DEBUG_WAIT_ARGUMENT in self.extra_arguments:
            raise ConfigurationError(
                f"请使用“等待 Lua 调试器”选项，不要在额外参数中填写 {DEBUG_WAIT_ARGUMENT}。"
            )
        if LUA_DEBUG_PORT_ARGUMENT in self.extra_arguments:
            raise ConfigurationError(f"{LUA_DEBUG_PORT_ARGUMENT} 由启动器自动分配，请从额外参数中删除。")

    @property
    def network(self) -> NetworkOptions:
        return NetworkOptions(self.role, self.host, self.port, self.room_id, self.dev_account, self.server_mode)

    def settings(self) -> LauncherSettings:
        """Build the argv shared by previews and launches, in the .bat script order."""
        arguments = self.network.arguments()
        if self.role is NetworkRole.STANDALONE:
            if self.dev_account:
                arguments += (DEV_ACCOUNT_ARGUMENT, str(self.dev_account))
        else:
            if self.test_mode:
                arguments += ("-AICoreTestMode",)
                if self.network.is_dedicated:
                    arguments += ("-AICoreTestServiceIdentity", self.service_identity)
            if self.peer_ticket:
                arguments += ("-AICorePeerTicket", self.peer_ticket)
        arguments += self.extra_arguments
        if self.project_root:
            arguments += ("-AICoreRoot", self.project_root)
        if self.skip_character_selection and not has_initial_character(arguments):
            arguments += (INITIAL_CHARACTER_ARGUMENT, DEFAULT_INITIAL_CHARACTER)
        if self.network.is_dedicated and not self.show_console and "-AICoreNoConsole" not in arguments:
            arguments += ("-AICoreNoConsole",)
        if self.debug_wait:
            arguments += (DEBUG_WAIT_ARGUMENT,)
        return LauncherSettings(
            executable=self.executable, arguments=arguments, show_console=self.show_console,
        )

    def summary(self) -> str:
        if self.role is NetworkRole.STANDALONE:
            parts = [Path(self.executable).name, "单机运行", "无需服务端"]
            if self.dev_account:
                parts.append(f"账号 {self.dev_account}")
        elif self.role is NetworkRole.HOST:
            parts = [
                Path(self.executable).name, f"端口 {self.port}", f"房间 {self.room_id}",
                "DS · mock 账号" if self.network.is_dedicated else f"账号 {self.dev_account}",
            ]
        else:
            account = f"起始账号 {self.dev_account}" if self.auto_dev_account else f"固定账号 {self.dev_account}"
            parts = [f"{self.host}:{self.port}", f"房间 {self.room_id}", account]
        if self.debug_wait:
            parts.append("等待调试器")
        return " · ".join(parts)


def profile_from_command(command: str, base: LaunchProfile) -> LaunchProfile:
    """Read an edited command line back into a profile.

    Settings the command cannot express (name, console window, automatic dev
    accounts) are kept from ``base``.
    """
    argv = parse_argument_text(command)
    if not argv or argv[0].startswith("-"):
        raise ConfigurationError("命令的第一项必须是程序路径。")
    executable, arguments = argv[0], argv[1:]
    for argument in arguments:
        if argument in LEGACY_NETWORK_ARGUMENTS:
            raise ConfigurationError(
                f"{argument} 已移除（客机身份改取登录账号），请删除；用 -MGFTestAccount 选择账号。"
            )
    network = parse_network_arguments(arguments)
    if network.role is NetworkRole.STANDALONE and any(
        argument in NETWORK_ARGUMENTS and argument not in ("-MGFNetRole", "-MGFDevAccount", DEV_ACCOUNT_ARGUMENT) for argument in arguments
    ):
        raise ConfigurationError("单机命令不能包含联机地址、端口或房间参数。")
    extras = apply_network_preset(arguments, NetworkRole.STANDALONE)
    project, extras = _split_project_arguments(extras)
    if "-MGFTestAccountFile" in extras and argument_value(base.extra_arguments, "-MGFTestAccountFile") is None:
        index = extras.index("-MGFTestAccountFile")
        default_file = resolve_account_file(("-AICoreRoot", project) if project else (), executable)
        if extras[index + 1:index + 2] == (str(default_file),):
            extras = extras[:index] + extras[index + 2:]
    skip_selection = base.skip_character_selection if has_initial_character(extras) else False
    # Keep explicit custom characters in extras; the checkbox supplies only the default Avatar.
    if not has_initial_character(base.extra_arguments):
        for index, argument in enumerate(extras):
            if argument == INITIAL_CHARACTER_ARGUMENT and extras[index + 1:index + 2] == (DEFAULT_INITIAL_CHARACTER,):
                extras = extras[:index] + extras[index + 2:]
                skip_selection = True
                break
    return replace(
        base, role=network.role, executable=executable,
        server_mode=base.server_mode if network.role is NetworkRole.STANDALONE else network.server_mode,
        host=network.host if network.role is NetworkRole.CLIENT else DEFAULT_NETWORK_HOST,
        port=network.port, room_id=network.room_id, dev_account=network.dev_account or 0,
        project_root=project or "",
        extra_arguments=tuple(argument for argument in extras if argument != DEBUG_WAIT_ARGUMENT),
        debug_wait=DEBUG_WAIT_ARGUMENT in extras,
        skip_character_selection=skip_selection,
        test_mode=base.test_mode if network.role is NetworkRole.STANDALONE else any(
            argument.partition("=")[0].casefold() == "-aicoretestmode" for argument in arguments
        ),
        peer_ticket="",
    )


def pair_mismatches(host: LaunchProfile, client: LaunchProfile) -> tuple[str, ...]:
    """Fields that stop a one-click Client from joining the Host it launches."""
    mismatches = []
    if client.host.casefold() not in LOCAL_HOSTS:
        mismatches.append(f"主机地址 {client.host} 不是本机地址（127.0.0.1）")
    if client.server_mode != host.server_mode:
        mismatches.append(f"服务端模式 {client.server_mode} 与 Host 的 {host.server_mode} 不一致")
    if client.port != host.port:
        mismatches.append(f"端口 {client.port} 与 Host 端口 {host.port} 不一致")
    if client.room_id != host.room_id:
        mismatches.append(f"房间 {client.room_id} 与 Host 房间 {host.room_id} 不一致")
    if client.test_mode != host.test_mode:
        mismatches.append("Client 与 Host 的本地联调测试模式不一致")
    host_ticket = host.peer_ticket or ("aicore-test" if host.test_mode else "")
    client_ticket = client.peer_ticket or ("aicore-test" if client.test_mode else "")
    if client_ticket != host_ticket:
        mismatches.append("Client 与 Host 的联机票据不一致")
    return tuple(mismatches)


def default_profiles() -> tuple[LaunchProfile, ...]:
    return (
        LaunchProfile(name="默认 Host", role=NetworkRole.HOST),
        LaunchProfile(name="默认 Client", role=NetworkRole.CLIENT),
        LaunchProfile(name="默认单机", role=NetworkRole.STANDALONE),
    )


@dataclass(frozen=True)
class LauncherConfig:
    """Saved profiles plus the one active profile per role that the main window launches."""

    profiles: tuple[LaunchProfile, ...] = ()
    selected_host_id: str | None = None
    selected_client_id: str | None = None
    client_delay_seconds: float = DEFAULT_CLIENT_DELAY_SECONDS
    window_geometry: str = ""
    client_count: int = 1
    auto_tile_windows: bool = True
    selected_standalone_id: str | None = None

    @classmethod
    def default(cls) -> LauncherConfig:
        return cls.with_default_selection(default_profiles())

    @classmethod
    def with_default_selection(cls, profiles: tuple[LaunchProfile, ...], **values: object) -> LauncherConfig:
        selection = {
            SELECTION_FIELDS[role]: next((p.id for p in profiles if p.role is role), None)
            for role in LAUNCH_ROLES
        }
        return cls(profiles=profiles, **selection, **values)  # type: ignore[arg-type]

    def __post_init__(self) -> None:
        if not isinstance(self.profiles, tuple) or not all(
            isinstance(profile, LaunchProfile) for profile in self.profiles
        ):
            raise ConfigurationError("配置集合必须由启动配置组成。")
        ids: set[str] = set()
        names: dict[str, str] = {}
        for profile in self.profiles:
            if profile.id in ids:
                raise ConfigurationError(f"配置标识重复：{profile.id}。")
            ids.add(profile.id)
            key = profile.name.casefold()
            if key in names:
                raise ConfigurationError(f"已存在名为「{names[key]}」的配置，请使用其他名称。")
            names[key] = profile.name
        for role in LAUNCH_ROLES:
            selected_id = self.selected_id(role)
            if selected_id is None:
                continue
            profile = self.profile(selected_id)
            if profile is None or profile.role is not role:
                raise ConfigurationError(f"激活的 {ROLE_TITLES[role]} 配置不存在或类型不符。")
        delay = self.client_delay_seconds
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) or not (
            math.isfinite(delay) and 0 <= delay <= 60
        ):
            raise ConfigurationError("Client 启动间隔必须在 0 到 60 秒之间。")
        if not _is_integer(self.client_count) or not 1 <= self.client_count <= MAX_CLIENT_COUNT:
            raise ConfigurationError(f"Client 数量必须是 1 到 {MAX_CLIENT_COUNT} 之间的整数。")
        if not isinstance(self.window_geometry, str) or not is_valid_window_geometry(self.window_geometry):
            raise ConfigurationError("窗口位置格式无效。")
        if not isinstance(self.auto_tile_windows, bool):
            raise ConfigurationError("自动平铺窗口必须是布尔值。")

    def profile(self, profile_id: str | None) -> LaunchProfile | None:
        return next((profile for profile in self.profiles if profile.id == profile_id), None)

    def profiles_for(self, role: NetworkRole) -> tuple[LaunchProfile, ...]:
        return tuple(profile for profile in self.profiles if profile.role is role)

    def selected_id(self, role: NetworkRole) -> str | None:
        return getattr(self, SELECTION_FIELDS[role])

    def selected(self, role: NetworkRole) -> LaunchProfile | None:
        return self.profile(self.selected_id(role))

    def is_active(self, profile: LaunchProfile) -> bool:
        return self.selected_id(profile.role) == profile.id

    def with_selection(self, role: NetworkRole, profile_id: str | None) -> LauncherConfig:
        """Activate one profile for a role (replacing the previous one), or none."""
        return replace(self, **{SELECTION_FIELDS[role]: profile_id})

    def save_profile(self, profile: LaunchProfile) -> LauncherConfig:
        """Insert or replace one profile; a role change deactivates it for the old role.

        The first profile of a role is activated automatically so a fresh setup can launch.
        """
        existing = self.profile(profile.id)
        if existing is None:
            profiles = self.profiles + (profile,)
        else:
            profiles = tuple(profile if item.id == profile.id else item for item in self.profiles)
        selection = {role: self.selected_id(role) for role in LAUNCH_ROLES}
        for role, selected_id in selection.items():
            if selected_id == profile.id and profile.role is not role:
                selection[role] = None
        if selection[profile.role] is None:
            selection[profile.role] = profile.id
        return replace(
            self, profiles=profiles,
            selected_host_id=selection[NetworkRole.HOST],
            selected_client_id=selection[NetworkRole.CLIENT],
            selected_standalone_id=selection[NetworkRole.STANDALONE],
        )

    def remove_profile(self, profile_id: str) -> LauncherConfig:
        return replace(
            self,
            profiles=tuple(profile for profile in self.profiles if profile.id != profile_id),
            selected_host_id=None if self.selected_host_id == profile_id else self.selected_host_id,
            selected_client_id=None if self.selected_client_id == profile_id else self.selected_client_id,
            selected_standalone_id=None if self.selected_standalone_id == profile_id else self.selected_standalone_id,
        )

    def unique_name(self, base: str, exclude_id: str | None = None) -> str:
        taken = {profile.name.casefold() for profile in self.profiles if profile.id != exclude_id}
        name, index = base, 2
        while name.casefold() in taken:
            name, index = f"{base} {index}", index + 1
        return name


def _profile_from_settings(
    name: str, role: NetworkRole, settings: LauncherSettings, *,
    network: NetworkOptions | None = None, own_extras: bool = True, auto_dev_account: bool = True,
) -> LaunchProfile:
    network = network or settings.network
    extras = apply_network_preset(settings.enabled_arguments, NetworkRole.STANDALONE)
    project, extras = _split_project_arguments(extras)
    client_source = network.role is NetworkRole.CLIENT
    return LaunchProfile(
        name=name, role=role, executable=settings.executable,
        project_root=project if project is not None else (
            DEFAULT_PROJECT_ROOT if Path(settings.executable) == Path(DEFAULT_EXECUTABLE) else ""
        ),
        host=network.host if client_source else DEFAULT_NETWORK_HOST,
        port=network.port, room_id=network.room_id,
        dev_account=network.dev_account if network.role is role else default_dev_account(role),
        auto_dev_account=auto_dev_account,
        extra_arguments=tuple(a for a in extras if a != DEBUG_WAIT_ARGUMENT) if own_extras else (),
        show_console=settings.show_console,
        debug_wait=DEBUG_WAIT_ARGUMENT in extras,
    )


class ProfileStore:
    """Store the named profile collection, migrating earlier single-form files."""

    def __init__(self, path: Path) -> None:
        self.storage = ConfigStore(path)

    def load(self) -> LauncherConfig:
        raw = self.storage.read_payload()
        if raw is None:
            return LauncherConfig.default()
        version = raw.get("version")
        if version in (1, 2):
            return self._add_standalone(self._migrate_single(ConfigStore.parse_settings(raw)))
        if version == 3:
            return self._add_standalone(self._migrate_pair(raw))
        if version not in (4, 5, 6, PROFILE_CONFIG_VERSION):
            raise ConfigurationError(f"不支持的配置版本：{version!r}。")
        profiles = raw.get("profiles")
        if not isinstance(profiles, list):
            raise ConfigurationError("配置项 profiles 必须是数组。")
        config = LauncherConfig(
            profiles=tuple(self._parse_profile(item) for item in profiles),
            selected_host_id=raw.get("selected_host"),  # type: ignore[arg-type]
            selected_client_id=raw.get("selected_client"),  # type: ignore[arg-type]
            client_delay_seconds=raw.get("client_delay_seconds", DEFAULT_CLIENT_DELAY_SECONDS),  # type: ignore[arg-type]
            window_geometry=raw.get("window_geometry", ""),  # type: ignore[arg-type]
            client_count=raw.get("client_count", 1),  # type: ignore[arg-type]
            auto_tile_windows=raw.get("auto_tile_windows", True),  # type: ignore[arg-type]
            selected_standalone_id=raw.get("selected_standalone"),  # type: ignore[arg-type]
        )
        return self._add_standalone(config) if version < PROFILE_CONFIG_VERSION else config

    @staticmethod
    def _add_standalone(config: LauncherConfig) -> LauncherConfig:
        """Give old installations an independent single-player entry without altering network profiles."""
        if config.profiles_for(NetworkRole.STANDALONE):
            return config
        source = config.selected(NetworkRole.CLIENT) or config.selected(NetworkRole.HOST)
        if source is None:
            source = next(iter(config.profiles), None)
        profile = LaunchProfile(
            name=config.unique_name("默认单机"), role=NetworkRole.STANDALONE,
            executable=source.executable if source else DEFAULT_EXECUTABLE,
            project_root=source.project_root if source else DEFAULT_PROJECT_ROOT,
            show_console=source.show_console if source else True,
            skip_character_selection=source.skip_character_selection if source else True,
        )
        return config.save_profile(profile)

    @staticmethod
    def _parse_profile(raw: object) -> LaunchProfile:
        if not isinstance(raw, dict):
            raise ConfigurationError("每份启动配置必须是 JSON 对象。")
        name = raw.get("name")
        try:
            role = raw.get("role")
            if role not in tuple(item.value for item in LAUNCH_ROLES):
                raise ConfigurationError("配置类型必须是 standalone、host 或 client。")
            extras = raw.get("extra_arguments", [])
            if not isinstance(extras, list):
                raise ConfigurationError("额外参数必须是字符串列表。")
            executable = raw.get("executable", DEFAULT_EXECUTABLE)
            default_project = DEFAULT_PROJECT_ROOT if (
                isinstance(executable, str) and Path(executable) == Path(DEFAULT_EXECUTABLE)
            ) else ""
            return LaunchProfile(
                id=raw.get("id"), name=name, role=NetworkRole(role),  # type: ignore[arg-type]
                executable=executable,  # type: ignore[arg-type]
                project_root=raw.get("project_root", default_project),  # type: ignore[arg-type]
                host=raw.get("host", DEFAULT_NETWORK_HOST),  # type: ignore[arg-type]
                port=raw.get("port", DEFAULT_NETWORK_PORT),  # type: ignore[arg-type]
                room_id=raw.get("room_id", DEFAULT_NETWORK_ROOM_ID),  # type: ignore[arg-type]
                # 旧存档的 uin 字段已无意义（客机身份改取登录账号），忽略；auto_uin 沿用为自动分配开关。
                dev_account=raw.get("dev_account", 0),  # type: ignore[arg-type]
                auto_dev_account=raw.get("auto_dev_account", raw.get("auto_uin", True)),  # type: ignore[arg-type]
                extra_arguments=tuple(extras),
                show_console=raw.get("show_console", True),  # type: ignore[arg-type]
                server_mode=raw.get("server_mode", "CloudServer"),
                debug_wait=raw.get("debug_wait", False),  # type: ignore[arg-type]
                skip_character_selection=raw.get("skip_character_selection", True),  # type: ignore[arg-type]
                test_mode=raw.get("test_mode", True),  # type: ignore[arg-type]
                peer_ticket=raw.get("peer_ticket", ""),  # type: ignore[arg-type]
                service_identity=raw.get("service_identity", "debug-launcher"),  # type: ignore[arg-type]
            )
        except ConfigurationError as exc:
            label = f"「{name}」" if isinstance(name, str) and name.strip() else ""
            raise ConfigurationError(f"启动配置{label}无效：{exc}") from exc

    @staticmethod
    def _migrate_single(settings: LauncherSettings) -> LauncherConfig:
        """Version 1/2 kept one form; split it into a default Host and Client."""
        network = settings.network

        def profile(role: NetworkRole) -> LaunchProfile:
            return _profile_from_settings(
                f"默认 {ROLE_TITLES[role]}", role, settings,
                own_extras=network.role in (role, NetworkRole.STANDALONE),
            )

        return LauncherConfig.with_default_selection(
            (profile(NetworkRole.HOST), profile(NetworkRole.CLIENT)),
            window_geometry=settings.window_geometry,
        )

    @staticmethod
    def _migrate_pair(raw: Mapping[str, object]) -> LauncherConfig:
        """Version 3 kept exactly one Host form and one Client form."""

        def settings(name: str) -> LauncherSettings:
            payload = raw.get(name)
            if not isinstance(payload, dict):
                raise ConfigurationError(f"配置项 {name} 必须是 JSON 对象。")
            return ConfigStore.parse_settings({**payload, "version": 2, "window_geometry": ""})

        host_settings, client_settings = settings("host"), settings("client")
        client_network = client_settings.network
        if raw.get("client_follow_host", True) is True:
            host_network = host_settings.network
            client_network = replace(
                client_network, host=DEFAULT_NETWORK_HOST,
                port=host_network.port, room_id=host_network.room_id,
            )
        auto_dev_account = raw.get("client_auto_uin", True)
        return LauncherConfig.with_default_selection(
            (
                _profile_from_settings("默认 Host", NetworkRole.HOST, host_settings),
                _profile_from_settings(
                    "默认 Client", NetworkRole.CLIENT, client_settings,
                    network=client_network, auto_dev_account=auto_dev_account,  # type: ignore[arg-type]
                ),
            ),
            client_delay_seconds=raw.get("client_delay_seconds", DEFAULT_CLIENT_DELAY_SECONDS),
            window_geometry=raw.get("window_geometry", ""),
        )

    def save(self, config: LauncherConfig) -> None:
        self.storage.write_payload({
            "version": PROFILE_CONFIG_VERSION,
            "profiles": [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "role": profile.role.value,
                    "server_mode": profile.server_mode,
                    "executable": profile.executable,
                    "project_root": profile.project_root,
                    "host": profile.host,
                    "port": profile.port,
                    "room_id": profile.room_id,
                    "dev_account": profile.dev_account,
                    "auto_dev_account": profile.auto_dev_account,
                    "extra_arguments": list(profile.extra_arguments),
                    "show_console": profile.show_console,
                    "debug_wait": profile.debug_wait,
                    "skip_character_selection": profile.skip_character_selection,
                    "test_mode": profile.test_mode,
                    "peer_ticket": profile.peer_ticket,
                    "service_identity": profile.service_identity,
                }
                for profile in config.profiles
            ],
            "selected_host": config.selected_host_id,
            "selected_client": config.selected_client_id,
            "selected_standalone": config.selected_standalone_id,
            "client_delay_seconds": config.client_delay_seconds,
            "client_count": config.client_count,
            "window_geometry": config.window_geometry,
            "auto_tile_windows": config.auto_tile_windows,
        })
