from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from pathlib import Path
from typing import Mapping
import uuid

from launcher_core import (
    ConfigStore,
    ConfigurationError,
    DEBUG_WAIT_ARGUMENT,
    DEFAULT_EXECUTABLE,
    DEFAULT_NETWORK_HOST,
    DEFAULT_NETWORK_PORT,
    DEFAULT_NETWORK_ROOM_ID,
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


PROFILE_CONFIG_VERSION = 4
LAUNCH_ROLES = (NetworkRole.HOST, NetworkRole.CLIENT)
ROLE_TITLES = {NetworkRole.HOST: "Host", NetworkRole.CLIENT: "Client"}
LOCAL_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))
DEFAULT_CLIENT_DELAY_SECONDS = 2.0


def new_profile_id() -> str:
    return uuid.uuid4().hex


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class LaunchProfile:
    """One named, complete launch setting for a Host or a Client App."""

    name: str
    role: NetworkRole
    executable: str = DEFAULT_EXECUTABLE
    host: str = DEFAULT_NETWORK_HOST
    port: int = DEFAULT_NETWORK_PORT
    room_id: int = DEFAULT_NETWORK_ROOM_ID
    # 开发账号序号（-MGFDevAccount）；0 表示取角色默认值（Host 1、Client 2）。
    dev_account: int = 0
    auto_dev_account: bool = True
    extra_arguments: tuple[str, ...] = ()
    show_console: bool = True
    debug_wait: bool = False
    id: str = field(default_factory=new_profile_id)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ConfigurationError("配置标识必须是非空字符串。")
        for name, label in (("name", "配置名称"), ("executable", "程序路径"), ("host", "主机地址")):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(f"{label}不能为空。")
            object.__setattr__(self, name, value.strip())
        if self.role not in LAUNCH_ROLES:
            raise ConfigurationError("配置类型必须是 Host 或 Client。")
        if not _is_integer(self.port) or not 1 <= self.port <= MAX_PORT:
            raise ConfigurationError(f"端口必须是 1 到 {MAX_PORT} 之间的整数。")
        if not _is_integer(self.room_id) or self.room_id <= 0:
            raise ConfigurationError("房间号必须是正整数。")
        if self.dev_account == 0 and _is_integer(self.dev_account):
            object.__setattr__(self, "dev_account", default_dev_account(self.role))
        if not _is_integer(self.dev_account) or self.dev_account <= 0:
            raise ConfigurationError("开发账号序号必须是正整数。")
        for name, label in (
            ("auto_dev_account", "自动分配开发账号"), ("show_console", "显示控制台"), ("debug_wait", "等待 Lua 调试器"),
        ):
            if not isinstance(getattr(self, name), bool):
                raise ConfigurationError(f"{label}必须是布尔值。")
        if not isinstance(self.extra_arguments, tuple) or not all(
            isinstance(argument, str) for argument in self.extra_arguments
        ):
            raise ConfigurationError("额外参数必须是字符串列表。")
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
        return NetworkOptions(self.role, self.host, self.port, self.room_id, self.dev_account)

    def settings(self) -> LauncherSettings:
        """Build the argv shared by previews and launches, in the .bat script order."""
        arguments = self.network.arguments() + self.extra_arguments
        if self.debug_wait:
            arguments += (DEBUG_WAIT_ARGUMENT,)
        return LauncherSettings(
            executable=self.executable, arguments=arguments, show_console=self.show_console,
        )

    def summary(self) -> str:
        if self.role is NetworkRole.HOST:
            parts = [
                Path(self.executable).name, f"端口 {self.port}", f"房间 {self.room_id}",
                f"账号 {self.dev_account}",
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
                f"{argument} 已移除（客机身份改取登录账号），请删除；用 -MGFDevAccount 选择账号。"
            )
    network = parse_network_arguments(arguments)
    if network.role not in LAUNCH_ROLES:
        raise ConfigurationError("命令中需要 -MGFNetRole Host 或 -MGFNetRole Client。")
    extras = apply_network_preset(arguments, NetworkRole.STANDALONE)
    return replace(
        base, role=network.role, executable=executable,
        host=network.host if network.role is NetworkRole.CLIENT else DEFAULT_NETWORK_HOST,
        port=network.port, room_id=network.room_id, dev_account=network.dev_account or 0,
        extra_arguments=tuple(argument for argument in extras if argument != DEBUG_WAIT_ARGUMENT),
        debug_wait=DEBUG_WAIT_ARGUMENT in extras,
    )


def pair_mismatches(host: LaunchProfile, client: LaunchProfile) -> tuple[str, ...]:
    """Fields that stop a one-click Client from joining the Host it launches."""
    mismatches = []
    if client.host.casefold() not in LOCAL_HOSTS:
        mismatches.append(f"主机地址 {client.host} 不是本机地址（127.0.0.1）")
    if client.port != host.port:
        mismatches.append(f"端口 {client.port} 与 Host 端口 {host.port} 不一致")
    if client.room_id != host.room_id:
        mismatches.append(f"房间 {client.room_id} 与 Host 房间 {host.room_id} 不一致")
    return tuple(mismatches)


def default_profiles() -> tuple[LaunchProfile, LaunchProfile]:
    return (
        LaunchProfile(name="默认 Host", role=NetworkRole.HOST),
        LaunchProfile(name="默认 Client", role=NetworkRole.CLIENT),
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

    @classmethod
    def default(cls) -> LauncherConfig:
        return cls.with_default_selection(default_profiles())

    @classmethod
    def with_default_selection(cls, profiles: tuple[LaunchProfile, ...], **values: object) -> LauncherConfig:
        host, client = (next((p.id for p in profiles if p.role is role), None) for role in LAUNCH_ROLES)
        return cls(profiles, host, client, **values)  # type: ignore[arg-type]

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

    def profile(self, profile_id: str | None) -> LaunchProfile | None:
        return next((profile for profile in self.profiles if profile.id == profile_id), None)

    def profiles_for(self, role: NetworkRole) -> tuple[LaunchProfile, ...]:
        return tuple(profile for profile in self.profiles if profile.role is role)

    def selected_id(self, role: NetworkRole) -> str | None:
        return self.selected_host_id if role is NetworkRole.HOST else self.selected_client_id

    def selected(self, role: NetworkRole) -> LaunchProfile | None:
        return self.profile(self.selected_id(role))

    def is_active(self, profile: LaunchProfile) -> bool:
        return self.selected_id(profile.role) == profile.id

    def with_selection(self, role: NetworkRole, profile_id: str | None) -> LauncherConfig:
        """Activate one profile for a role (replacing the previous one), or none."""
        field_name = "selected_host_id" if role is NetworkRole.HOST else "selected_client_id"
        return replace(self, **{field_name: profile_id})

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
        )

    def remove_profile(self, profile_id: str) -> LauncherConfig:
        return replace(
            self,
            profiles=tuple(profile for profile in self.profiles if profile.id != profile_id),
            selected_host_id=None if self.selected_host_id == profile_id else self.selected_host_id,
            selected_client_id=None if self.selected_client_id == profile_id else self.selected_client_id,
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
    client_source = network.role is NetworkRole.CLIENT
    return LaunchProfile(
        name=name, role=role, executable=settings.executable,
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
            return self._migrate_single(ConfigStore.parse_settings(raw))
        if version == 3:
            return self._migrate_pair(raw)
        if version != PROFILE_CONFIG_VERSION:
            raise ConfigurationError(f"不支持的配置版本：{version!r}。")
        profiles = raw.get("profiles")
        if not isinstance(profiles, list):
            raise ConfigurationError("配置项 profiles 必须是数组。")
        return LauncherConfig(
            profiles=tuple(self._parse_profile(item) for item in profiles),
            selected_host_id=raw.get("selected_host"),  # type: ignore[arg-type]
            selected_client_id=raw.get("selected_client"),  # type: ignore[arg-type]
            client_delay_seconds=raw.get("client_delay_seconds", DEFAULT_CLIENT_DELAY_SECONDS),  # type: ignore[arg-type]
            window_geometry=raw.get("window_geometry", ""),  # type: ignore[arg-type]
            client_count=raw.get("client_count", 1),  # type: ignore[arg-type]
        )

    @staticmethod
    def _parse_profile(raw: object) -> LaunchProfile:
        if not isinstance(raw, dict):
            raise ConfigurationError("每份启动配置必须是 JSON 对象。")
        name = raw.get("name")
        try:
            role = raw.get("role")
            if role not in ("host", "client"):
                raise ConfigurationError("配置类型必须是 host 或 client。")
            extras = raw.get("extra_arguments", [])
            if not isinstance(extras, list):
                raise ConfigurationError("额外参数必须是字符串列表。")
            return LaunchProfile(
                id=raw.get("id"), name=name, role=NetworkRole(role),  # type: ignore[arg-type]
                executable=raw.get("executable", DEFAULT_EXECUTABLE),  # type: ignore[arg-type]
                host=raw.get("host", DEFAULT_NETWORK_HOST),  # type: ignore[arg-type]
                port=raw.get("port", DEFAULT_NETWORK_PORT),  # type: ignore[arg-type]
                room_id=raw.get("room_id", DEFAULT_NETWORK_ROOM_ID),  # type: ignore[arg-type]
                # 旧存档的 uin 字段已无意义（客机身份改取登录账号），忽略；auto_uin 沿用为自动分配开关。
                dev_account=raw.get("dev_account", 0),  # type: ignore[arg-type]
                auto_dev_account=raw.get("auto_dev_account", raw.get("auto_uin", True)),  # type: ignore[arg-type]
                extra_arguments=tuple(extras),
                show_console=raw.get("show_console", True),  # type: ignore[arg-type]
                debug_wait=raw.get("debug_wait", False),  # type: ignore[arg-type]
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
                    "executable": profile.executable,
                    "host": profile.host,
                    "port": profile.port,
                    "room_id": profile.room_id,
                    "dev_account": profile.dev_account,
                    "auto_dev_account": profile.auto_dev_account,
                    "extra_arguments": list(profile.extra_arguments),
                    "show_console": profile.show_console,
                    "debug_wait": profile.debug_wait,
                }
                for profile in config.profiles
            ],
            "selected_host": config.selected_host_id,
            "selected_client": config.selected_client_id,
            "client_delay_seconds": config.client_delay_seconds,
            "client_count": config.client_count,
            "window_geometry": config.window_geometry,
        })
