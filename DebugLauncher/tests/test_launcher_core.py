from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from window_layout import Rect  # noqa: E402
from launcher_core import (  # noqa: E402
    AttachConfiguration,
    ConfigStore,
    ConfigurationError,
    ControllerState,
    DEFAULT_EXECUTABLE,
    DebugPortAllocator,
    DebugPortBundle,
    LauncherController,
    LauncherError,
    LauncherSettings,
    NetworkOptions,
    NetworkRole,
    WindowsJobProcess,
    apply_network_preset,
    build_launch_arguments,
    build_process_environment_overrides,
    center_window_geometry_on_screen,
    format_argument_text,
    format_command_preview,
    load_attach_configuration,
    parse_argument_text,
)


class AttachConfigurationTests(unittest.TestCase):
    def test_loads_attach_name_and_ports_from_run_configuration(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "Lua.run.xml"
            path.write_text(
                """<component name="ProjectRunConfigurationManager">
  <configuration name="Lua Attach">
    <option name="attachConfiguration" value="{&quot;name&quot;:&quot;Gameplay Lua&quot;,&quot;port&quot;:3393}" />
    <option name="attachPort" value="4721" />
  </configuration>
</component>
""",
                encoding="utf-8",
            )

            loaded = load_attach_configuration(path)

            self.assertEqual(loaded.name, "Lua Attach")
            self.assertEqual(loaded.session_name, "Gameplay Lua")
            self.assertEqual(loaded.display_name, "Lua Attach / Gameplay Lua")
            self.assertEqual(loaded.dap_port, 4721)
            self.assertEqual(loaded.bridge_port, 3393)

    def test_missing_or_invalid_configuration_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            missing = Path(directory) / "missing.xml"
            invalid = Path(directory) / "invalid.xml"
            invalid.write_text("<component>", encoding="utf-8")

            self.assertEqual(load_attach_configuration(missing), AttachConfiguration())
            self.assertEqual(load_attach_configuration(invalid), AttachConfiguration())


class WindowGeometryTests(unittest.TestCase):
    def test_centers_saved_size_on_primary_screen(self) -> None:
        self.assertEqual(
            center_window_geometry_on_screen("1100x820-1920+40", 1920, 1080),
            "1100x820+410+130",
        )

    def test_clamps_oversized_window_to_primary_screen(self) -> None:
        self.assertEqual(
            center_window_geometry_on_screen("2560x1440+200+100", 1920, 1080),
            "1920x1080+0+0",
        )

    def test_rejects_invalid_geometry_or_screen_size(self) -> None:
        with self.assertRaises(ValueError):
            center_window_geometry_on_screen("1100x820", 1920, 1080)
        with self.assertRaises(ValueError):
            center_window_geometry_on_screen("1100x820+0+0", 0, 1080)


class ConfigStoreTests(unittest.TestCase):
    def test_missing_file_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "settings.json")
            self.assertEqual(store.load(), LauncherSettings())
            self.assertEqual(store.load().executable, DEFAULT_EXECUTABLE)

    def test_settings_round_trip(self) -> None:
        arguments = (
            "-MGFDevAccount", "5", "-MGFNetRole", "Client", "-MGFNetHost", "192.168.1.25",
            "-MGFNetPort", "7001", "-MGFNetRoomId", "3",
            "-Custom", "value with spaces", "", 'a"b', "-script-debug-wait-client",
        )
        settings = LauncherSettings(
            executable=r"C:\Program Files\MiniGame\AICore_profile.exe",
            arguments=arguments,
            argument_enabled=(*([True] * (len(arguments) - 1)), False),
            show_console=False,
            window_geometry="1024x768+120-40",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = ConfigStore(path)
            store.save(settings)
            self.assertEqual(store.load(), settings)
            self.assertEqual(store.load().network, NetworkOptions(
                NetworkRole.CLIENT, "192.168.1.25", 7001, 3, 5,
            ))
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], 2)
            self.assertEqual(raw["arguments"], list(arguments))
            self.assertNotIn("network_role", raw)

    def test_legacy_settings_drop_gameplay_and_keep_window_and_debug_option(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({
                "version": 1,
                "executable": r"C:\MiniGame\Bin64\AIFramework_d.exe",
                "arguments": ["-MGFTopBattle", "-OtherGame", "-script-debug-wait-client"],
                "argument_enabled": [True, False, True],
                "show_console": False,
                "window_geometry": "1100x850+120-40",
                "network_role": "login",
                "network_port": 19120,
            }), encoding="utf-8")
            store = ConfigStore(path)
            loaded = store.load()
            self.assertEqual(loaded.executable, DEFAULT_EXECUTABLE)
            self.assertEqual(loaded.enabled_arguments, ("-script-debug-wait-client",))
            self.assertEqual(loaded.network.role, NetworkRole.STANDALONE)
            self.assertEqual(loaded.window_geometry, "1100x850+120-40")
            self.assertFalse(loaded.show_console)
            store.save(loaded)
            self.assertEqual(store.load(), loaded)

    def test_legacy_network_roles_migrate_to_new_app_arguments(self) -> None:
        for role, port in (("host", 19120), ("client", 20002)):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                path.write_text(json.dumps({
                    "version": 1,
                    "executable": r"C:\custom\AICore_d.exe",
                    "arguments": ["-MGFTopBattle", "-script-debug-wait-client"],
                    "argument_enabled": [True, False],
                    "network_role": role,
                    "network_host": "10.0.0.8",
                    "network_port": port,
                }), encoding="utf-8")
                loaded = ConfigStore(path).load()
                self.assertEqual(loaded.executable, r"C:\custom\AICore_d.exe")
                self.assertEqual(loaded.network.role, NetworkRole(role))
                self.assertEqual(loaded.network.port, 7000 if role == "host" else 20002)
                self.assertEqual(loaded.network.room_id, 1)
                self.assertNotIn("-script-debug-wait-client", loaded.enabled_arguments)
                self.assertNotIn("-MGFTopBattle", loaded.arguments)
                if role == "client":
                    self.assertEqual(loaded.network.host, "10.0.0.8")
                    self.assertEqual(loaded.network.dev_account, 2)

    def test_legacy_settings_without_optional_fields_still_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({
                "version": 1,
                "executable": DEFAULT_EXECUTABLE,
                "arguments": ["-MGFTopBattle"],
            }), encoding="utf-8")
            loaded = ConfigStore(path).load()
            self.assertEqual(loaded.window_geometry, "")
            self.assertEqual(loaded.enabled_arguments, ())
            self.assertEqual(loaded.network, NetworkOptions())

    def test_save_window_geometry_preserves_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "settings.json")
            original = LauncherSettings(
                executable=r"C:\custom\App.exe",
                arguments=("-CustomMode",),
                argument_enabled=(False,),
                show_console=False,
            )
            store.save(original)
            store.save_window_geometry("900x690-1500+80")
            loaded = store.load()
            self.assertEqual(loaded.executable, original.executable)
            self.assertEqual(loaded.arguments, original.arguments)
            self.assertEqual(loaded.argument_enabled, original.argument_enabled)
            self.assertEqual(loaded.show_console, original.show_console)
            self.assertEqual(loaded.window_geometry, "900x690-1500+80")

    def test_invalid_window_geometry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "settings.json")
            with self.assertRaises(ConfigurationError):
                store.save(LauncherSettings(window_geometry="900x690"))

    def test_invalid_settings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"version": 2, "arguments": "bad"}', encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                ConfigStore(path).load()

    def test_mismatched_argument_enabled_list_is_rejected(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                path.write_text(json.dumps({
                    "version": version,
                    "executable": DEFAULT_EXECUTABLE,
                    "arguments": ["-Custom"],
                    "argument_enabled": [True, False],
                }), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    ConfigStore(path).load()


class ArgumentTests(unittest.TestCase):
    def test_host_preset_matches_start_aicore_host_bat(self) -> None:
        expected = ("-MGFDevAccount", "1", "-MGFNetRole", "Host", "-MGFNetPort", "7000", "-MGFNetRoomId", "1")
        arguments = apply_network_preset((), NetworkRole.HOST)
        self.assertEqual(arguments, expected)
        settings = LauncherSettings(arguments=arguments)
        self.assertEqual(build_launch_arguments(settings), expected)
        self.assertEqual(settings.network.role, NetworkRole.HOST)

    def test_client_preset_matches_start_aicore_client_bat(self) -> None:
        expected = (
            "-MGFDevAccount", "2", "-MGFNetRole", "Client", "-MGFNetHost", "127.0.0.1",
            "-MGFNetPort", "7000", "-MGFNetRoomId", "1",
        )
        arguments = apply_network_preset((), NetworkRole.CLIENT)
        self.assertEqual(arguments, expected)
        settings = LauncherSettings(arguments=arguments)
        self.assertEqual(build_launch_arguments(settings), expected)
        self.assertEqual(settings.network, NetworkOptions(role=NetworkRole.CLIENT))

    def test_switching_presets_preserves_custom_options_without_stale_network_flags(self) -> None:
        custom = ("-App", "value with spaces", "-script-debug-wait-client")
        client = NetworkOptions(NetworkRole.CLIENT, "10.0.0.8", 7002, 5, 8).arguments()
        host = apply_network_preset(custom + client + ("-MGFNetUin", "10008"), NetworkRole.HOST)
        self.assertEqual(host, (
            "-MGFDevAccount", "1", "-MGFNetRole", "Host", "-MGFNetPort", "7000", "-MGFNetRoomId", "1", *custom,
        ), "切换预设同时清掉已移除的 -MGFNetUin")
        self.assertEqual(apply_network_preset(host, NetworkRole.STANDALONE), custom)

    def test_custom_app_arguments_are_preserved_without_gameplay_selection(self) -> None:
        arguments = ("-App", "My App", "-UserData", r"C:\User Data\profile", "", "中文参数")
        settings = LauncherSettings(arguments=arguments)
        self.assertEqual(build_launch_arguments(settings), arguments)
        self.assertEqual(settings.network.role, NetworkRole.STANDALONE)
        self.assertNotIn("-MGFTopBattle", LauncherSettings().arguments)

    def test_edited_network_values_drive_metadata_and_launch(self) -> None:
        arguments = parse_argument_text(
            '-MGFDevAccount 7 -MGFNetRole Client -MGFNetHost 10.0.0.8 -MGFNetPort 7002 '
            '-MGFNetRoomId 12 -App "Custom App"'
        )
        settings = LauncherSettings(arguments=arguments)
        self.assertEqual(settings.network, NetworkOptions(NetworkRole.CLIENT, "10.0.0.8", 7002, 12, 7))
        self.assertEqual(build_launch_arguments(settings), arguments)

    def test_invalid_network_values_are_rejected(self) -> None:
        invalid = (
            ("-MGFNetRole",), ("-MGFNetRole", "invalid"),
            ("-MGFNetPort", "0"), ("-MGFNetPort", "65536"),
            ("-MGFNetPort", "abc"), ("-MGFNetPort", "-MGFNetRoomId", "1"),
            ("-MGFNetHost", ""), ("-MGFNetRoomId", "0"),
            ("-MGFDevAccount", "0"), ("-MGFDevAccount", "abc"), ("-MGFDevAccount",),
            ("-MGFNetPort", "7000", "-MGFNetPort", "7001"),
            ("-lua-debug-port", "3382"),
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ConfigurationError):
                LauncherSettings(arguments=arguments)

    def test_disabled_arguments_are_omitted_from_command_preview(self) -> None:
        settings = LauncherSettings(
            arguments=("-Custom", "-script-debug-wait-client"),
            argument_enabled=(False, True),
        )
        self.assertNotIn("-Custom", format_command_preview(settings))
        self.assertIn("-script-debug-wait-client", format_command_preview(settings))

    def test_preview_and_launch_share_argument_builder(self) -> None:
        settings = LauncherSettings(arguments=("-MGFNetRole", "Host", "-App", "App with spaces"))
        ports = DebugPortBundle(3382, 3383, 4711)
        preview = format_command_preview(settings, ports)
        self.assertEqual(parse_argument_text(preview), (settings.executable, *build_launch_arguments(settings, ports)))

    def test_windows_argument_round_trip(self) -> None:
        expected = ("-App", "value with spaces", "", 'embedded"quote', "trailing\\", "中文参数", "&", "%PATH%")
        self.assertEqual(parse_argument_text(format_argument_text(expected)), expected)

    def test_blank_text_is_empty_argument_list(self) -> None:
        self.assertEqual(parse_argument_text("   "), ())


class FakeProcess:
    def __init__(self, pid: int, wait_result: bool = True) -> None:
        self._pid = pid
        self.exit_code: int | None = None
        self.wait_result = wait_result
        self.terminated = False
        self.closed = False

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self, exit_code: int = 1) -> None:
        self.terminated = True
        if self.wait_result:
            self.exit_code = exit_code

    def wait(self, timeout_ms: int) -> bool:
        del timeout_ms
        return self.wait_result

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self) -> None:
        self.created: list[FakeProcess] = []
        self.calls: list[tuple[str, tuple[str, ...], bool, dict[str, str]]] = []
        self.window_rects: list[Rect | None] = []

    def __call__(
        self,
        executable: str,
        arguments: tuple[str, ...],
        show_console: bool,
        environment_overrides: dict[str, str],
        initial_window_rect: Rect | None = None,
    ) -> FakeProcess:
        self.window_rects.append(initial_window_rect)
        self.calls.append(
            (executable, tuple(arguments), show_console, environment_overrides)
        )
        process = FakeProcess(1000 + len(self.created))
        self.created.append(process)
        return process



class SdkLaunchArgumentsTests(unittest.TestCase):
    def configure(self, root: Path, sdk_root: str = "SDK") -> LauncherSettings:
        (root / ".project").mkdir(exist_ok=True)
        (root / ".project/dev-runtime.json").write_text(
            json.dumps({"schemaVersion": 2, "sdkRoot": sdk_root}), encoding="utf-8",
        )
        framework = root / sdk_root / "AICoreScripts"
        framework.mkdir(parents=True, exist_ok=True)
        (framework / "main.lua").write_text("return {}", encoding="utf-8")
        return LauncherSettings(executable=str(root / "bin/AICore_profile.exe"),
                                arguments=("-AICoreRoot", str(root)))

    def test_selected_sdk_is_resolved_for_preview_and_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for sdk_root in ("SDK", str(root / "other SDK")):
                with self.subTest(sdk_root=sdk_root):
                    settings = self.configure(root, sdk_root)
                    arguments = build_launch_arguments(settings)
                    expected = str((root / sdk_root / "AICoreScripts").resolve())
                    self.assertEqual(arguments[-2:], ("-AICoreFrameworkRoot", expected))
                    self.assertEqual(parse_argument_text(format_command_preview(settings))[1:], arguments)

    def test_explicit_framework_and_other_apps_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.configure(Path(directory))
            explicit = LauncherSettings(executable=settings.executable,
                arguments=settings.arguments + ("-AICoreFrameworkRoot", "custom/framework"))
            self.assertEqual(build_launch_arguments(explicit), explicit.arguments)
            other_app = LauncherSettings(executable="CustomApp.exe", arguments=settings.arguments)
            self.assertEqual(build_launch_arguments(other_app), other_app.arguments)

    def test_missing_entry_and_old_directory_fail_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.configure(root)
            (root / "AICoreScripts").mkdir()
            with self.assertRaisesRegex(ConfigurationError, "旧目录"):
                build_launch_arguments(settings)
            (root / "AICoreScripts").rmdir()
            (root / "SDK/AICoreScripts/main.lua").unlink()
            with self.assertRaisesRegex(ConfigurationError, "缺少框架入口"):
                build_launch_arguments(settings)

    def test_legacy_config_is_unchanged_and_invalid_json_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.configure(root)
            config = root / ".project/dev-runtime.json"
            config.write_text('{"schemaVersion":1,"frameworkRoot":"framework"}', encoding="utf-8")
            self.assertEqual(build_launch_arguments(settings), settings.arguments)
            config.write_text("invalid JSON", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "无法读取工程运行配置"):
                build_launch_arguments(settings)

    def test_busy_udp_port_does_not_create_an_app(self) -> None:
        import socket
        from launcher_core import _check_server_udp_port
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as owner:
            owner.bind(("0.0.0.0", 0))
            with self.assertRaisesRegex(LauncherError, "无法监听 UDP"):
                _check_server_udp_port(owner.getsockname()[1])


class DebugPortAllocatorTests(unittest.TestCase):
    def test_allocates_incrementing_bundles_and_reuses_released_bundle(self) -> None:
        allocator = DebugPortAllocator(lambda _port: True)
        first = allocator.allocate()
        second = allocator.allocate()
        self.assertEqual(first, DebugPortBundle(3382, 3383, 4711))
        self.assertEqual(second, DebugPortBundle(3384, 3385, 4712))
        allocator.release(first)
        self.assertEqual(allocator.allocate(), first)

    def test_skips_bundle_when_any_port_is_unavailable(self) -> None:
        allocator = DebugPortAllocator(lambda port: port != 3383)
        self.assertEqual(
            allocator.allocate(),
            DebugPortBundle(3384, 3385, 4712),
        )

    def test_reserves_an_explicit_bundle(self) -> None:
        allocator = DebugPortAllocator(lambda _port: True)
        preferred = DebugPortBundle(3382, 3383, 4711)
        allocator.reserve(preferred)
        self.assertEqual(
            allocator.allocate(),
            DebugPortBundle(3384, 3385, 4712),
        )

    def test_rejects_an_unavailable_explicit_bundle(self) -> None:
        allocator = DebugPortAllocator(lambda port: port != 4711)
        with self.assertRaisesRegex(LauncherError, "4711"):
            allocator.reserve(DebugPortBundle(3382, 3383, 4711))

    def test_allows_an_active_target_for_a_persistent_attach_slot(self) -> None:
        allocator = DebugPortAllocator(lambda port: port != 3382)
        preferred = DebugPortBundle(3382, 3383, 4711)

        self.assertFalse(
            allocator.reserve(preferred, allow_active_target=True)
        )
        self.assertEqual(
            allocator.allocate(),
            DebugPortBundle(3384, 3385, 4712),
        )

    def test_active_target_does_not_allow_an_occupied_dap_port(self) -> None:
        allocator = DebugPortAllocator(lambda port: port not in (3382, 4711))
        with self.assertRaisesRegex(LauncherError, "4711"):
            allocator.reserve(
                DebugPortBundle(3382, 3383, 4711),
                allow_active_target=True,
            )


class AppLogPathTests(unittest.TestCase):
    EXE = r"C:\MiniGame\Bin64\AICore_profile.exe"

    def name(self, *arguments: str, pid: int | None = 42) -> str:
        from launcher_core import resolve_app_log_path
        path = resolve_app_log_path(self.EXE, arguments, pid)
        self.assertEqual(path.parent, Path(r"C:\MiniGame\Bin64"))
        return path.name

    def test_names_follow_aicore_win_game_start_rules(self) -> None:
        cases = {
            "AICoreApp.log": (),
            "AICoreApp_dev2.log": ("-MGFDevAccount", "2", "-MGFNetRole", "Client"),
            "AICoreApp_dev1.log": ("-mgfdevaccount", "1"),
            "AICoreApp_dev-42.log": ("-MGFDevAccount", "01"),
            "AICoreApp-server.log": ("-AICoreNetRole", "Dedicated", "-MGFDevAccount", "3"),
            "AICoreApp-client-42.log": ("-AICoreNetRole", "Client", "-MGFDevAccount", "3"),
            "mine.log": ("-AICoreLogFile", "mine.log", "-AICoreNetRole", "Dedicated"),
            "AICoreAvatarTerrainDemo.log": ("-MGFAvatarTerrainDemo", "-AICoreLogFile", "x.log"),
        }
        for expected, arguments in cases.items():
            with self.subTest(arguments=arguments):
                self.assertEqual(self.name(*arguments), expected)

    def test_log_file_with_a_path_is_ignored_like_the_app(self) -> None:
        self.assertEqual(self.name("-AICoreLogFile", r"D:\x.log", "-MGFDevAccount", "4"), "AICoreApp_dev4.log")

    def test_dev_account_without_value_uses_pid(self) -> None:
        self.assertEqual(self.name("-MGFDevAccount"), "AICoreApp_dev-42.log")
        self.assertEqual(self.name("-MGFDevAccount", "x", pid=None), "AICoreApp_dev-<pid>.log")


class LauncherControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = FakeFactory()
        self.allocator = DebugPortAllocator(lambda _port: True)
        self.controller = LauncherController(self.factory, self.allocator)
        self.settings = LauncherSettings()

    def tearDown(self) -> None:
        self.controller.shutdown()

    def test_start_multiple_instances_and_stop_one(self) -> None:
        first = self.controller.start(self.settings)
        second = self.controller.start(self.settings)
        self.assertEqual((first.instance_id, second.instance_id), (1, 2))
        self.assertEqual(first.debug_ports, DebugPortBundle(3382, 3383, 4711))
        self.assertEqual(second.debug_ports, DebugPortBundle(3384, 3385, 4712))
        self.assertEqual(len(self.controller.snapshots()), 2)
        self.assertEqual(
            self.factory.calls[0][1],
            (
                "-script-debug-wait-client",
                "-lua-debug-port",
                "3382",
            ),
        )

        stopped = self.controller.stop(first.instance_id)
        self.assertEqual(stopped.state, ControllerState.STOPPED)
        self.assertEqual(
            tuple(item.instance_id for item in self.controller.snapshots()),
            (second.instance_id,),
        )
        self.assertTrue(self.factory.created[0].terminated)
        self.assertTrue(self.factory.created[0].closed)

    def test_reserved_debug_slot_is_used_by_the_first_instance(self) -> None:
        preferred = DebugPortBundle(3382, 3383, 4711)
        self.assertEqual(self.controller.reserve_debug_slot(preferred), preferred)
        started = self.controller.start(self.settings)
        self.assertEqual(started.debug_ports, preferred)

        self.controller.stop(started.instance_id)
        replacement = self.controller.start(self.settings)
        self.assertEqual(replacement.debug_ports, preferred)

    def test_active_external_target_is_not_assigned_to_a_new_instance(self) -> None:
        allocator = DebugPortAllocator(lambda port: port != 3382)
        controller = LauncherController(self.factory, allocator)
        preferred = DebugPortBundle(3382, 3383, 4711)
        try:
            controller.reserve_debug_slot(
                preferred,
                allow_active_target=True,
            )

            started = controller.start(self.settings)

            self.assertEqual(
                started.debug_ports,
                DebugPortBundle(3384, 3385, 4712),
            )
        finally:
            controller.shutdown()

    def test_lowest_idle_debug_slot_is_reused_first(self) -> None:
        preferred = DebugPortBundle(3382, 3383, 4711)
        self.controller.reserve_debug_slot(preferred)
        first = self.controller.start(self.settings)
        second = self.controller.start(self.settings)

        self.controller.stop(second.instance_id)
        self.controller.stop(first.instance_id)
        replacement = self.controller.start(self.settings)
        self.assertEqual(replacement.debug_ports, preferred)

    def test_start_passes_only_checked_arguments_plus_debug_port(self) -> None:
        settings = LauncherSettings(
            arguments=("-Custom", "-script-debug-wait-client"),
            argument_enabled=(False, True),
        )
        self.controller.start(settings)
        self.assertEqual(
            self.factory.calls[0][1],
            ("-script-debug-wait-client", "-lua-debug-port", "3382"),
        )

    def test_restart_keeps_instance_original_settings_and_ports(self) -> None:
        settings = LauncherSettings(
            arguments=NetworkOptions(NetworkRole.CLIENT, "10.0.0.2", 20000).arguments(),
        )
        started = self.controller.start(settings)
        restarted = self.controller.restart(started.instance_id)
        self.assertEqual(restarted.instance_id, started.instance_id)
        self.assertEqual(restarted.debug_ports, started.debug_ports)
        self.assertEqual(restarted.settings, settings)
        self.assertEqual(self.factory.calls[1], self.factory.calls[0])
        self.assertTrue(self.factory.created[0].terminated)
        self.assertTrue(self.factory.created[0].closed)

    def test_natural_exit_is_reaped_and_ports_are_reused(self) -> None:
        started = self.controller.start(self.settings)
        self.factory.created[0].exit_code = 7
        self.assertEqual(self.controller.snapshots(), ())
        self.assertTrue(self.factory.created[0].closed)
        replacement = self.controller.start(self.settings)
        self.assertNotEqual(replacement.instance_id, started.instance_id)
        self.assertEqual(replacement.debug_ports, started.debug_ports)

    def test_duplicate_host_port_is_rejected(self) -> None:
        host = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments())
        self.controller.start(host)
        with self.assertRaises(LauncherError):
            self.controller.start(host)
        self.assertEqual(len(self.factory.created), 1)

    def test_same_port_is_allowed_for_clients(self) -> None:
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        self.controller.start(client)
        self.controller.start(LauncherSettings(
            arguments=NetworkOptions(role=NetworkRole.CLIENT, dev_account=3).arguments(),
        ))
        self.assertEqual(len(self.factory.created), 2)

    def test_launched_games_receive_unique_selectable_mcp_names(self) -> None:
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        host = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments())
        client_ports = DebugPortBundle(3382, 3383, 4711)
        host_ports = DebugPortBundle(3384, 3385, 4712)

        self.assertEqual(
            build_process_environment_overrides(client, client_ports),
            {"MINIGAME_MCP_CLIENT_NAME": "AICore_profile-CLIENT-3382"},
        )
        self.assertEqual(
            build_process_environment_overrides(host, host_ports),
            {"MINIGAME_MCP_CLIENT_NAME": "AICore_profile-HOST-3384"},
        )

        self.controller.start(client)
        self.assertEqual(
            self.factory.calls[0][3],
            {"MINIGAME_MCP_CLIENT_NAME": "AICore_profile-CLIENT-3382"},
        )

    def test_duplicate_dev_account_is_rejected(self) -> None:
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        started = self.controller.start(client)
        with self.assertRaisesRegex(LauncherError, "开发账号 2"):
            self.controller.start(client)
        self.assertEqual(len(self.factory.created), 1)
        self.controller.stop(started.instance_id)
        self.controller.start(client)
        self.assertEqual(len(self.factory.created), 2)

    def test_pair_starts_host_before_client_and_uses_separate_debug_ports(self) -> None:
        host = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments())
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        observed = []
        result = self.controller.start_pair(
            host, client, delay_seconds=0,
            on_host_started=lambda snapshot, reused: observed.append((snapshot.instance_id, reused, len(self.factory.created))),
        )
        self.assertEqual(observed, [(1, False, 1)])
        self.assertEqual(result.client.instance_id, 2)
        self.assertNotEqual(result.host.debug_ports, result.client.debug_ports)
        self.assertEqual(self.factory.calls[0][1][:4], ("-MGFDevAccount", "1", "-MGFNetRole", "Host"))
        self.assertEqual(self.factory.calls[1][1][:4], ("-MGFDevAccount", "2", "-MGFNetRole", "Client"))

    def test_pair_reuses_managed_host_and_allocates_another_client_dev_account(self) -> None:
        host = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments())
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        first = self.controller.start_pair(host, client, delay_seconds=0)
        second = self.controller.start_pair(host, client, delay_seconds=0)
        self.assertTrue(second.host_reused)
        self.assertEqual(first.host.instance_id, second.host.instance_id)
        self.assertEqual(second.client.settings.network.dev_account, 3)
        self.assertEqual(len(self.factory.created), 3)

    def test_pair_passes_each_initial_rectangle_before_reporting_its_client(self) -> None:
        host_rect = Rect(0, 0, 640, 360)
        client_rects = (Rect(640, 0, 1280, 360), Rect(0, 360, 640, 720))
        host = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments(),
                                initial_window_rect=host_rect)
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        observed = []
        result = self.controller.start_pair(
            host, client, delay_seconds=0, client_count=2, client_window_rects=client_rects,
            on_client_started=lambda snapshot: observed.append((snapshot.settings.initial_window_rect, len(self.factory.created))),
        )
        self.assertEqual(self.factory.window_rects, [host_rect, *client_rects])
        self.assertEqual(observed, [(client_rects[0], 2), (client_rects[1], 3)])
        self.controller.restart(result.clients[1].instance_id)
        self.assertEqual(self.factory.window_rects[-1], client_rects[1])

    def test_changed_host_configuration_is_not_silently_reused(self) -> None:
        original = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments())
        changed = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST, room_id=2).arguments())
        self.controller.ensure_host(original)
        with self.assertRaisesRegex(LauncherError, "参数不同"):
            self.controller.ensure_host(changed)
        self.assertEqual(len(self.factory.created), 1)

    def test_client_failure_keeps_host_owned_and_reports_partial_result(self) -> None:
        normal_factory = self.controller._process_factory
        def fail_client(executable, arguments, show_console, environment, initial_window_rect=None):
            if "Client" in arguments:
                raise LauncherError("Client executable missing")
            return normal_factory(executable, arguments, show_console, environment, initial_window_rect)
        self.controller._process_factory = fail_client
        result = self.controller.start_pair(
            LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments()),
            LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments()),
            delay_seconds=0,
        )
        self.assertIsNone(result.client)
        self.assertIn("Client executable missing", result.error)
        self.assertEqual(len(self.controller.snapshots()), 1)
        self.controller.stop(result.host.instance_id)
        self.assertTrue(self.factory.created[0].terminated)

    def test_host_failure_prevents_client_launch(self) -> None:
        def fail(*args):
            raise LauncherError("Host executable missing")
        self.controller._process_factory = fail
        with self.assertRaisesRegex(LauncherError, "Host executable missing"):
            self.controller.start_pair(
                LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments()),
                LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments()),
                delay_seconds=0,
            )
        self.assertEqual(self.controller.snapshots(), ())

    def test_cancelling_pair_after_host_start_prevents_client(self) -> None:
        cancel = threading.Event()
        result = self.controller.start_pair(
            LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments()),
            LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments()),
            delay_seconds=5, cancel=cancel,
            on_host_started=lambda *_: cancel.set(),
        )
        self.assertIsNone(result.client)
        self.assertIn("取消", result.error)
        self.assertEqual(len(self.factory.created), 1)

    def test_client_can_disable_auto_dev_account_and_report_duplicate(self) -> None:
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        self.controller.start_client(client, auto_dev_account=False)
        with self.assertRaisesRegex(LauncherError, "开发账号 2"):
            self.controller.start_client(client, auto_dev_account=False)

    def test_auto_dev_account_skips_accounts_used_by_host(self) -> None:
        self.controller.start(LauncherSettings(
            arguments=NetworkOptions(role=NetworkRole.HOST, dev_account=2).arguments(),
        ))
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        started = self.controller.start_client(client)
        self.assertEqual(started.settings.network.dev_account, 3, "本机账号锁全局：跳过 Host 占用的 2")

    def test_profile_label_follows_instance_through_restart_and_conflicts(self) -> None:
        host = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments())
        snapshot, _ = self.controller.ensure_host(host, "本机 Host")
        self.assertEqual(snapshot.label, "本机 Host")
        self.assertEqual(self.controller.restart(snapshot.instance_id).label, "本机 Host")
        changed = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST, room_id=2).arguments())
        with self.assertRaisesRegex(LauncherError, "实例 1「本机 Host」"):
            self.controller.ensure_host(changed, "房间 2 Host")
        result = self.controller.start_pair(
            host, LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments()),
            delay_seconds=0, host_label="另一个名称", client_label="本机 Client",
        )
        self.assertEqual((result.host.label, result.client.label), ("本机 Host", "本机 Client"))
        self.assertEqual(self.controller.stop(result.client.instance_id).label, "本机 Client")

    def test_start_clients_gives_each_client_its_own_dev_account(self) -> None:
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        result = self.controller.start_clients(client, 3, label="本机 Client")
        self.assertEqual([c.settings.network.dev_account for c in result.clients], [2, 3, 4])
        self.assertEqual({c.label for c in result.clients}, {"本机 Client"})
        self.assertEqual(result.error, "")

    def test_fixed_dev_account_cannot_start_several_clients(self) -> None:
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        with self.assertRaisesRegex(ConfigurationError, "固定开发账号"):
            self.controller.start_clients(client, 2, auto_dev_account=False)
        with self.assertRaisesRegex(ConfigurationError, "固定开发账号"):
            self.controller.start_pair(
                LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments()),
                client, delay_seconds=0, auto_dev_account=False, client_count=2,
            )
        self.assertEqual(self.factory.created, [])

    def test_client_batch_failure_keeps_started_clients(self) -> None:
        normal_factory = self.controller._process_factory

        def fail_third(executable, arguments, show_console, environment, initial_window_rect=None):
            if len(self.factory.created) == 2:
                raise LauncherError("third client failed")
            return normal_factory(executable, arguments, show_console, environment, initial_window_rect)

        self.controller._process_factory = fail_third
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        result = self.controller.start_clients(client, 3)
        self.assertEqual((len(result.clients), result.requested), (2, 3))
        self.assertIn("third client failed", result.error)
        self.assertEqual(len(self.controller.snapshots()), 2)

    def test_pair_starts_the_requested_number_of_clients(self) -> None:
        result = self.controller.start_pair(
            LauncherSettings(arguments=NetworkOptions(role=NetworkRole.HOST).arguments()),
            LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments()),
            delay_seconds=0, client_count=2,
        )
        self.assertEqual(len(result.clients), 2)
        self.assertEqual(result.client, result.clients[0])
        self.assertEqual(len(self.factory.created), 3)

    def test_stop_all(self) -> None:
        self.controller.start(self.settings)
        self.controller.start(self.settings)
        stopped = self.controller.stop_all()
        self.assertEqual(len(stopped), 2)
        self.assertEqual(self.controller.snapshots(), ())
        self.assertTrue(all(process.closed for process in self.factory.created))

    def test_stop_all_sends_every_request_before_any_process_finishes(self) -> None:
        for _ in range(3):
            self.controller.start(self.settings)
        entered = {process.pid: threading.Event() for process in self.factory.created}
        release = threading.Event()
        results = []
        errors = []

        for process in self.factory.created:
            def terminate(exit_code=1, process=process):
                entered[process.pid].set()
                if not release.wait(5):
                    raise LauncherError("测试等待释放退出超时")
                FakeProcess.terminate(process, exit_code)
            process.terminate = terminate

        def stop_all():
            try:
                results.append(self.controller.stop_all())
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=stop_all, daemon=True)
        worker.start()
        try:
            for requested in entered.values():
                self.assertTrue(requested.wait(1), "其他实例的关闭请求被前一个退出等待阻塞")
            self.assertFalse(any(process.closed for process in self.factory.created))
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([snapshot.instance_id for snapshot in results[0]], [1, 2, 3])
        self.assertEqual(self.controller.snapshots(), ())

    def test_stop_many_preserves_failed_and_unselected_instances(self) -> None:
        for _ in range(4):
            self.controller.start(self.settings)
        self.factory.created[0].wait_result = False
        result = self.controller.stop_many((3, 1, 2, 2, 999), timeout_ms=1)
        self.assertEqual([snapshot.instance_id for snapshot in result.snapshots], [3, 2])
        self.assertEqual(len(result.errors), 2)
        self.assertIn("实例 1", result.errors[0])
        self.assertIn("实例 999", result.errors[1])
        self.assertEqual([snapshot.instance_id for snapshot in self.controller.snapshots()], [1, 4])
        self.assertFalse(self.factory.created[0].closed)
        self.assertFalse(self.factory.created[3].terminated)

    def test_stop_timeout_keeps_process_owned(self) -> None:
        process = FakeProcess(2000, wait_result=False)
        controller = LauncherController(
            lambda *_: process,
            DebugPortAllocator(lambda _port: True),
        )
        started = controller.start(self.settings)
        with self.assertRaises(LauncherError):
            controller.stop(started.instance_id, timeout_ms=1)
        self.assertEqual(
            controller.snapshot(started.instance_id).state,
            ControllerState.RUNNING,
        )
        process.wait_result = True
        controller.shutdown()
        self.assertTrue(process.closed)

    def test_shutdown_closes_every_instance_and_rejects_late_start(self) -> None:
        first = self.controller.start(self.settings)
        self.controller.start(self.settings)
        self.controller.shutdown()
        self.assertTrue(all(process.closed for process in self.factory.created))
        with self.assertRaises(LauncherError):
            self.controller.start(self.settings)
        with self.assertRaises(LauncherError):
            self.controller.restart(first.instance_id)


@unittest.skipUnless(sys.platform == "win32", "Windows Job Object integration test")
class WindowsJobProcessTests(unittest.TestCase):
    def test_initial_placement_moves_a_real_hidden_window_and_corrects_its_show_position(self) -> None:
        from window_layout import LayoutTarget, Win32WindowBackend, WindowLayoutService

        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            show = Path(directory) / "show"
            code = """
import pathlib, sys, tkinter as tk
ready, show = map(pathlib.Path, sys.argv[1:])
root = tk.Tk()
root.withdraw()
root.title("DebugLauncher initial placement test")
root.geometry("800x600+200+200")
root.update_idletasks()
ready.touch()
def check():
    if show.exists():
        root.deiconify()
    else:
        root.after(20, check)
root.after(20, check)
root.after(8000, root.destroy)
root.mainloop()
"""
            process = WindowsJobProcess.launch(sys.executable, ("-I", "-c", code, str(ready), str(show)), show_console=False)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())

                class RecordingBackend(Win32WindowBackend):
                    visible_on_place = []

                    def place_window(self, hwnd, rect):
                        self.visible_on_place.append(bool(self.user32.IsWindowVisible(hwnd)))
                        super().place_window(hwnd, rect)
                        show.touch()

                backend = RecordingBackend()
                area = backend.primary_work_area()
                rect = Rect(area.left, area.top, area.left + 640, area.top + 400)
                result = WindowLayoutService(backend).place_on_start(
                    LayoutTarget(1, process.pid), rect, threading.Event(), timeout_seconds=4,
                )
                self.assertEqual(result.moved, (1,))
                self.assertFalse(backend.visible_on_place[0], "首次定位应在窗口显示前")
                windows = backend.enumerate_windows({process.pid})
                self.assertTrue(any(window.visible and window.rect == rect for window in windows))
            finally:
                process.close()

    def test_initial_rectangle_reaches_native_startup_info(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "startup.json"
            process = WindowsJobProcess.launch(
                sys.executable,
                ("-I", "-c",
                 "import ctypes,json,pathlib,sys; sys.path.insert(0,sys.argv[1]); "
                 "from launcher_core import _STARTUPINFOW; s=_STARTUPINFOW(); "
                 "ctypes.windll.kernel32.GetStartupInfoW(ctypes.byref(s)); "
                 "pathlib.Path(sys.argv[2]).write_text(json.dumps([s.dwFlags & 6, ctypes.c_int(s.dwX).value, "
                 "ctypes.c_int(s.dwY).value, s.dwXSize, s.dwYSize]))",
                 str(TOOL_DIR), str(output)),
                show_console=False, initial_window_rect=Rect(-900, 20, -100, 620),
            )
            try:
                self.assertTrue(process.wait(10000))
                self.assertEqual(process.poll(), 0)
                self.assertEqual(json.loads(output.read_text()), [6, -900, 20, 800, 600])
            finally:
                process.close()

    def test_app_arguments_reach_windows_process_without_shell_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "received arguments.json"
            expected = ["value with spaces", "中文参数", 'embedded"quote', "", "&", "%PATH%", "trailing\\"]
            arguments = (
                "-I", "-c",
                "import json,pathlib,sys; "
                "pathlib.Path(sys.argv[1]).write_text("
                "json.dumps(sys.argv[2:],ensure_ascii=False),encoding='utf-8')",
                str(output), *expected,
            )
            settings = LauncherSettings(
                executable=sys.executable,
                arguments=parse_argument_text(format_argument_text(arguments)),
                show_console=False,
            )
            process = WindowsJobProcess.launch(
                settings.executable, build_launch_arguments(settings), show_console=False,
            )
            try:
                self.assertTrue(process.wait(10000))
                self.assertEqual(process.poll(), 0)
                self.assertEqual(json.loads(output.read_text(encoding="utf-8")), expected)
            finally:
                process.close()

    def test_launch_applies_environment_overrides(self) -> None:
        process = WindowsJobProcess.launch(
            sys.executable,
            (
                "-I",
                "-c",
                "import os,sys; sys.exit(0 if "
                "os.environ.get('MINIGAME_MCP_AUTOCONNECT') == '0' else 9)",
            ),
            show_console=False,
            environment_overrides={"MINIGAME_MCP_AUTOCONNECT": "0"},
        )
        try:
            self.assertTrue(process.wait(3000))
            self.assertEqual(process.poll(), 0)
        finally:
            process.close()

    def test_job_can_terminate_managed_process(self) -> None:
        process = WindowsJobProcess.launch(
            sys.executable,
            ("-I", "-c", "import time; time.sleep(30)"),
            show_console=False,
        )
        try:
            self.assertIsNone(process.poll())
            process.terminate(exit_code=23)
            self.assertTrue(process.wait(3000))
            self.assertEqual(process.poll(), 23)
        finally:
            process.close()


class AICoreGracefulStopTests(unittest.TestCase):
    def info(self, **values):
        return dict(pid=1234, instance_id="session-1", project_path="C:/test-project", **values)

    def test_verified_owned_instance_gets_one_stop_request(self) -> None:
        from aicore_control import request_stop
        info = self.info()
        for project in ("C:/test-project", "../test-project"):
            with self.subTest(project=project), patch("aicore_control.owned_ports", return_value=(9050,)), patch(
                "aicore_control._http", side_effect=[info, info, {"state": "accepted"}]
            ) as http:
                self.assertTrue(request_stop(1234, ("-AICoreRoot", project), lambda: True, working_directory="C:/Bin64"))
                self.assertEqual(http.call_args.args[2], {"instance_id": "session-1", "operation": "runtime.stop"})

    def test_other_pid_or_project_cannot_receive_stop(self) -> None:
        from aicore_control import request_stop
        for change in ({"pid": 9876}, {"project_path": "D:/other-project"}):
            info = {**self.info(), **change}
            with self.subTest(change=change), patch("aicore_control.owned_ports", return_value=(9050,)), patch(
                "aicore_control._http", return_value=info
            ) as http:
                self.assertFalse(request_stop(1234, ("-AICoreRoot", "C:/test-project"), lambda: True))
                self.assertEqual(http.call_count, 1)

    def test_instance_replaced_during_discovery_cannot_receive_stop(self) -> None:
        from aicore_control import request_stop
        with patch("aicore_control.owned_ports", return_value=(9050,)), patch(
            "aicore_control._http", side_effect=[self.info(), {**self.info(), "instance_id": "session-2"}]
        ) as http:
            self.assertFalse(request_stop(1234, ("-AICoreRoot", "C:/test-project"), lambda: True))
            self.assertEqual(http.call_count, 2)

    def test_original_process_exit_cancels_stop_before_mutation(self) -> None:
        from aicore_control import request_stop
        with patch("aicore_control.owned_ports", return_value=(9050,)), patch(
            "aicore_control._http", side_effect=[self.info(), self.info()]
        ) as http:
            self.assertFalse(request_stop(1234, ("-AICoreRoot", "C:/test-project"), iter((True, False)).__next__))
            self.assertEqual(http.call_count, 2)


@unittest.skipUnless(sys.platform == "win32", "Windows exclusive dev account locks")
class GlobalDevAccountTests(unittest.TestCase):
    def test_external_lock_and_account_limit_match_start_game(self) -> None:
        import ctypes
        from ctypes import wintypes
        from aicore_control import available_dev_account
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            (project / ".project").mkdir(parents=True)
            (project / ".project/account.json").write_text(json.dumps({"accounts": [{}, {}, {}]}), encoding="utf-8")
            lock_directory = Path(directory) / "miniw_devaccount"
            lock_directory.mkdir()
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel.CreateFileW(str(lock_directory / "dev2.lock"), 0xC0000000, 0, None, 4, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            try:
                with patch("aicore_control.tempfile.gettempdir", return_value=directory):
                    args = ("-AICoreRoot", str(project))
                    self.assertEqual(available_dev_account(2, set(), args, DEFAULT_EXECUTABLE), 3)
                    with self.assertRaisesRegex(ValueError, "没有空闲开发账号"):
                        available_dev_account(2, {3}, args, DEFAULT_EXECUTABLE)
            finally:
                kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
