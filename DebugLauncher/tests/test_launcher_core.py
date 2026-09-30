from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

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


class ConfigStoreTests(unittest.TestCase):
    def test_missing_file_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "settings.json")
            self.assertEqual(store.load(), LauncherSettings())
            self.assertEqual(store.load().executable, DEFAULT_EXECUTABLE)

    def test_settings_round_trip(self) -> None:
        arguments = (
            "-MGFNetRole", "Client", "-MGFNetHost", "192.168.1.25",
            "-MGFNetPort", "7001", "-MGFNetRoomId", "3", "-MGFNetUin", "10002",
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
                NetworkRole.CLIENT, "192.168.1.25", 7001, 3, 10002,
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
                    self.assertEqual(loaded.network.uin, 10001)

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
        expected = ("-MGFNetRole", "Host", "-MGFNetPort", "7000", "-MGFNetRoomId", "1")
        arguments = apply_network_preset((), NetworkRole.HOST)
        self.assertEqual(arguments, expected)
        settings = LauncherSettings(arguments=arguments)
        self.assertEqual(build_launch_arguments(settings), expected)
        self.assertEqual(settings.network.role, NetworkRole.HOST)

    def test_client_preset_matches_start_aicore_client_bat(self) -> None:
        expected = (
            "-MGFNetRole", "Client", "-MGFNetHost", "127.0.0.1",
            "-MGFNetPort", "7000", "-MGFNetRoomId", "1", "-MGFNetUin", "10001",
        )
        arguments = apply_network_preset((), NetworkRole.CLIENT)
        self.assertEqual(arguments, expected)
        settings = LauncherSettings(arguments=arguments)
        self.assertEqual(build_launch_arguments(settings), expected)
        self.assertEqual(settings.network, NetworkOptions(role=NetworkRole.CLIENT))

    def test_switching_presets_preserves_custom_options_without_stale_network_flags(self) -> None:
        custom = ("-App", "value with spaces", "-script-debug-wait-client")
        client = NetworkOptions(NetworkRole.CLIENT, "10.0.0.8", 7002, 5, 10008).arguments()
        host = apply_network_preset(custom + client, NetworkRole.HOST)
        self.assertEqual(host, (
            "-MGFNetRole", "Host", "-MGFNetPort", "7000", "-MGFNetRoomId", "1", *custom,
        ))
        self.assertEqual(apply_network_preset(host, NetworkRole.STANDALONE), custom)

    def test_custom_app_arguments_are_preserved_without_gameplay_selection(self) -> None:
        arguments = ("-App", "My App", "-UserData", r"C:\User Data\profile", "", "中文参数")
        settings = LauncherSettings(arguments=arguments)
        self.assertEqual(build_launch_arguments(settings), arguments)
        self.assertEqual(settings.network.role, NetworkRole.STANDALONE)
        self.assertNotIn("-MGFTopBattle", LauncherSettings().arguments)

    def test_edited_network_values_drive_metadata_and_launch(self) -> None:
        arguments = parse_argument_text(
            '-MGFNetRole Client -MGFNetHost 10.0.0.8 -MGFNetPort 7002 '
            '-MGFNetRoomId 12 -MGFNetUin 20002 -App "Custom App"'
        )
        settings = LauncherSettings(arguments=arguments)
        self.assertEqual(settings.network, NetworkOptions(NetworkRole.CLIENT, "10.0.0.8", 7002, 12, 20002))
        self.assertEqual(build_launch_arguments(settings), arguments)

    def test_invalid_network_values_are_rejected(self) -> None:
        invalid = (
            ("-MGFNetRole",), ("-MGFNetRole", "invalid"),
            ("-MGFNetPort", "0"), ("-MGFNetPort", "65536"),
            ("-MGFNetPort", "abc"), ("-MGFNetPort", "-MGFNetRoomId", "1"),
            ("-MGFNetHost", ""), ("-MGFNetRoomId", "0"),
            ("-MGFNetRole", "Client", "-MGFNetUin", "1"),
            ("-MGFNetUin", "-2"), ("-MGFNetUin", "abc"),
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

    def __call__(
        self,
        executable: str,
        arguments: tuple[str, ...],
        show_console: bool,
        environment_overrides: dict[str, str],
    ) -> FakeProcess:
        self.calls.append(
            (executable, tuple(arguments), show_console, environment_overrides)
        )
        process = FakeProcess(1000 + len(self.created))
        self.created.append(process)
        return process


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
            arguments=NetworkOptions(role=NetworkRole.CLIENT, uin=10002).arguments(),
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

    def test_duplicate_client_uin_in_same_room_is_rejected(self) -> None:
        client = LauncherSettings(arguments=NetworkOptions(role=NetworkRole.CLIENT).arguments())
        started = self.controller.start(client)
        with self.assertRaisesRegex(LauncherError, "UIN 10001"):
            self.controller.start(client)
        self.assertEqual(len(self.factory.created), 1)
        self.controller.stop(started.instance_id)
        self.controller.start(client)
        self.assertEqual(len(self.factory.created), 2)

    def test_stop_all(self) -> None:
        self.controller.start(self.settings)
        self.controller.start(self.settings)
        stopped = self.controller.stop_all()
        self.assertEqual(len(stopped), 2)
        self.assertEqual(self.controller.snapshots(), ())
        self.assertTrue(all(process.closed for process in self.factory.created))

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


if __name__ == "__main__":
    unittest.main()
