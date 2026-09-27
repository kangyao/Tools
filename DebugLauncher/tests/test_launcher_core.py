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
    DebugPortAllocator,
    DebugPortBundle,
    LauncherController,
    LauncherError,
    LauncherSettings,
    NetworkRole,
    WindowsJobProcess,
    build_launch_arguments,
    build_process_environment_overrides,
    discover_argument_suggestions,
    filter_argument_suggestions,
    format_argument_text,
    format_command_preview,
    get_file_revision,
    is_mgf_argument,
    load_attach_configuration,
    normalize_fixed_arguments,
    parse_argument_text,
    try_discover_argument_suggestions,
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
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            store = ConfigStore(Path(directory) / "settings.json")
            self.assertEqual(store.load(), LauncherSettings())

    def test_settings_round_trip(self) -> None:
        settings = LauncherSettings(
            executable=r"C:\Program Files\MiniGame\AIFramework_d.exe",
            arguments=("-MGFTopBattle", "value with spaces", "", 'a"b'),
            argument_enabled=(True, False, True, False),
            show_console=False,
            window_geometry="1024x768+120-40",
            network_role=NetworkRole.CLIENT,
            network_host="192.168.1.25",
            network_port=19121,
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "settings.json"
            store = ConfigStore(path)
            store.save(settings)
            self.assertEqual(store.load(), settings)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], 1)
            self.assertIsInstance(raw["arguments"], list)
            self.assertEqual(raw["argument_enabled"], [True, False, True, False])
            self.assertEqual(raw["window_geometry"], "1024x768+120-40")
            self.assertEqual(raw["network_role"], "client")
            self.assertEqual(raw["network_host"], "192.168.1.25")
            self.assertEqual(raw["network_port"], 19121)

    def test_login_settings_round_trip(self) -> None:
        settings = LauncherSettings(network_role=NetworkRole.LOGIN)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "settings.json"
            store = ConfigStore(path)
            store.save(settings)
            self.assertEqual(store.load(), settings)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["network_role"], "login")

    def test_legacy_settings_without_geometry_still_load(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "executable": r"C:\MiniGame\Bin64\AIFramework_d.exe",
                        "arguments": ["-MGFTopBattle"],
                        "show_console": True,
                    }
                ),
                encoding="utf-8",
            )
            loaded = ConfigStore(path).load()
            self.assertEqual(loaded.window_geometry, "")
            self.assertEqual(loaded.argument_enabled, (True,))
            self.assertEqual(loaded.network_role, NetworkRole.STANDALONE)
            self.assertEqual(loaded.network_host, "127.0.0.1")
            self.assertEqual(loaded.network_port, 19120)

    def test_save_window_geometry_preserves_other_settings(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "settings.json"
            store = ConfigStore(path)
            original = LauncherSettings(
                executable=r"C:\custom\AIFramework.exe",
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
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "settings.json"
            store = ConfigStore(path)
            with self.assertRaises(ConfigurationError):
                store.save(LauncherSettings(window_geometry="900x690"))

    def test_invalid_settings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"version": 1, "arguments": "bad"}', encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                ConfigStore(path).load()

    def test_mismatched_argument_enabled_list_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "executable": r"C:\MiniGame\Bin64\AIFramework_d.exe",
                        "arguments": ["-MGFTopBattle"],
                        "argument_enabled": [True, False],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                ConfigStore(path).load()


class ArgumentTests(unittest.TestCase):
    def test_discovers_ordered_game_arguments_from_app_configs(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            games_init_path = Path(directory) / "init.lua"
            games_init_path.write_text(
                "Games.AppConfigs = {\n"
                '    { "MGFDebugSandbox", function() return require("A") end },\n'
                "    -- { \"CommentedOut\", function() return require(\"B\") end },\n"
                "    { 'MiniguiUI', function() return require('C') end },\n"
                '    { "MGFTopBattle", function() return require("D") end },\n'
                "}\n"
                'if Engine.HasArgs("MGFAutoInput") then end\n',
                encoding="utf-8",
            )

            suggestions = discover_argument_suggestions(games_init_path)
            self.assertEqual(
                suggestions,
                (
                    "-MGFDebugSandbox",
                    "-MiniguiUI",
                    "-MGFTopBattle",
                    "-script-debug-wait-client",
                ),
            )

    def test_missing_games_init_falls_back_to_default_arguments(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            missing_path = Path(directory) / "missing.lua"
            suggestions = discover_argument_suggestions(missing_path)
            self.assertIsNone(try_discover_argument_suggestions(missing_path))
            self.assertEqual(
                suggestions,
                ("-MGFTopBattle", "-script-debug-wait-client"),
            )

    def test_file_revision_tracks_replace_and_removal(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "init.lua"
            self.assertIsNone(get_file_revision(path))

            path.write_text("first\n", encoding="utf-8")
            first_revision = get_file_revision(path)
            self.assertIsNotNone(first_revision)
            self.assertEqual(get_file_revision(path), first_revision)

            replacement = path.with_suffix(".tmp")
            replacement.write_text("second version\n", encoding="utf-8")
            replacement.replace(path)
            self.assertNotEqual(get_file_revision(path), first_revision)

            path.unlink()
            self.assertIsNone(get_file_revision(path))

    def test_filters_argument_completion_with_or_without_dash(self) -> None:
        suggestions = (
            "-MGFAutoInput",
            "-MGFTopBattle",
            "-script-debug-wait-client",
        )
        self.assertEqual(
            filter_argument_suggestions("-MGFT", suggestions),
            ("-MGFTopBattle",),
        )
        self.assertEqual(
            filter_argument_suggestions("MGFT", suggestions),
            ("-MGFTopBattle",),
        )
        self.assertEqual(filter_argument_suggestions("", suggestions), suggestions)

    def test_fixed_arguments_list_every_option_and_select_one_mgf(self) -> None:
        available = (
            "-MGFAutoInput",
            "-MGFTopBattle",
            "-script-debug-wait-client",
            "-TestScene",
        )
        arguments, enabled = normalize_fixed_arguments(
            available,
            (
                "-CustomArgument",
                "-MGFTopBattle",
                "-MGFAutoInput",
                "-TestScene",
            ),
            (True, True, True, True),
        )

        self.assertEqual(arguments, available)
        enabled_arguments = tuple(
            argument
            for argument, state in zip(arguments, enabled)
            if state
        )
        self.assertEqual(
            tuple(filter(is_mgf_argument, enabled_arguments)),
            ("-MGFTopBattle",),
        )
        self.assertIn("-script-debug-wait-client", enabled_arguments)
        self.assertIn("-TestScene", enabled_arguments)
        self.assertNotIn("-CustomArgument", arguments)

    def test_all_game_routes_are_exclusive_even_without_mgf_prefix(self) -> None:
        available = (
            "-MGFDebugSandbox",
            "-MiniguiUI",
            "-PhysicsSmoke",
            "-script-debug-wait-client",
        )
        arguments, enabled = normalize_fixed_arguments(
            available,
            ("-MiniguiUI", "-PhysicsSmoke", "-script-debug-wait-client"),
            (True, True, True),
            exclusive_arguments=available[:-1],
        )

        enabled_arguments = tuple(
            argument
            for argument, state in zip(arguments, enabled)
            if state
        )
        self.assertEqual(
            enabled_arguments,
            ("-MiniguiUI", "-script-debug-wait-client"),
        )

    def test_fixed_arguments_default_to_top_battle(self) -> None:
        arguments, enabled = normalize_fixed_arguments(
            (
                "-MGFAutoInput",
                "-MGFTopBattle",
                "-script-debug-wait-client",
            )
        )
        enabled_arguments = tuple(
            argument
            for argument, state in zip(arguments, enabled)
            if state
        )
        self.assertEqual(
            enabled_arguments,
            ("-MGFTopBattle", "-script-debug-wait-client"),
        )

    def test_disabled_arguments_are_omitted_from_command_preview(self) -> None:
        settings = LauncherSettings(
            executable=r"C:\MiniGame\Bin64\AIFramework_d.exe",
            arguments=("-MGFTopBattle", "-script-debug-wait-client"),
            argument_enabled=(False, True),
        )
        self.assertNotIn("-MGFTopBattle", format_command_preview(settings))
        self.assertIn("-script-debug-wait-client", format_command_preview(settings))

    def test_top_battle_host_arguments_are_exact(self) -> None:
        settings = LauncherSettings(
            network_role=NetworkRole.HOST,
            network_port=20001,
        )
        self.assertEqual(
            build_launch_arguments(settings),
            (
                "-MGFTopBattle",
                "-script-debug-wait-client",
                "-TopBattleNetworkHost",
                "-TopBattleListenPort",
                "20001",
            ),
        )

    def test_top_battle_client_arguments_are_exact(self) -> None:
        settings = LauncherSettings(
            network_role=NetworkRole.CLIENT,
            network_host="10.0.0.8",
            network_port=20002,
        )
        self.assertEqual(
            build_launch_arguments(settings),
            (
                "-MGFTopBattle",
                "-script-debug-wait-client",
                "-TopBattleNetworkClient",
                "-TopBattleHost",
                "10.0.0.8",
                "-TopBattlePort",
                "20002",
            ),
        )

    def test_top_battle_interactive_login_arguments_are_exact(self) -> None:
        settings = LauncherSettings(network_role=NetworkRole.LOGIN)
        self.assertEqual(
            build_launch_arguments(settings),
            (
                "-MGFTopBattle",
                "-script-debug-wait-client",
                "-TopBattleNetworkLogin",
            ),
        )

    def test_non_top_battle_forces_standalone_arguments(self) -> None:
        settings = LauncherSettings(
            arguments=("-MGFDebugSandbox",),
            network_role=NetworkRole.HOST,
        )
        self.assertEqual(build_launch_arguments(settings), ("-MGFDebugSandbox",))

    def test_preview_and_launch_share_argument_builder(self) -> None:
        settings = LauncherSettings(network_role=NetworkRole.HOST)
        ports = DebugPortBundle(3382, 3383, 4711)
        preview = format_command_preview(settings, ports)
        for argument in build_launch_arguments(settings, ports):
            self.assertIn(argument, preview)

    def test_windows_argument_round_trip(self) -> None:
        expected = (
            "-MGFTopBattle",
            "value with spaces",
            "",
            'embedded"quote',
            "trailing\\",
            "中文参数",
        )
        rendered = format_argument_text(expected)
        self.assertEqual(parse_argument_text(rendered), expected)

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
                "-MGFTopBattle",
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
            arguments=("-MGFTopBattle", "-script-debug-wait-client"),
            argument_enabled=(False, True),
        )
        self.controller.start(settings)
        self.assertEqual(
            self.factory.calls[0][1],
            ("-script-debug-wait-client", "-lua-debug-port", "3382"),
        )

    def test_restart_keeps_instance_original_settings_and_ports(self) -> None:
        settings = LauncherSettings(
            network_role=NetworkRole.CLIENT,
            network_host="10.0.0.2",
            network_port=20000,
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
        host = LauncherSettings(network_role=NetworkRole.HOST)
        self.controller.start(host)
        with self.assertRaises(LauncherError):
            self.controller.start(host)
        self.assertEqual(len(self.factory.created), 1)

    def test_same_port_is_allowed_for_clients(self) -> None:
        client = LauncherSettings(network_role=NetworkRole.CLIENT)
        self.controller.start(client)
        self.controller.start(client)
        self.assertEqual(len(self.factory.created), 2)

    def test_launched_games_receive_unique_selectable_mcp_names(self) -> None:
        client = LauncherSettings(network_role=NetworkRole.CLIENT)
        host = LauncherSettings(network_role=NetworkRole.HOST)
        client_ports = DebugPortBundle(3382, 3383, 4711)
        host_ports = DebugPortBundle(3384, 3385, 4712)

        self.assertEqual(
            build_process_environment_overrides(client, client_ports),
            {"MINIGAME_MCP_CLIENT_NAME": "AIFramework-CLIENT-3382"},
        )
        self.assertEqual(
            build_process_environment_overrides(host, host_ports),
            {"MINIGAME_MCP_CLIENT_NAME": "AIFramework-HOST-3384"},
        )

        self.controller.start(client)
        self.assertEqual(
            self.factory.calls[0][3],
            {"MINIGAME_MCP_CLIENT_NAME": "AIFramework-CLIENT-3382"},
        )

    def test_login_role_starts_without_direct_network_endpoint(self) -> None:
        login = LauncherSettings(network_role=NetworkRole.LOGIN)
        self.controller.start(login)
        self.controller.start(login)
        self.assertEqual(len(self.factory.created), 2)
        self.assertEqual(
            self.factory.calls[0][1],
            (
                "-MGFTopBattle",
                "-script-debug-wait-client",
                "-TopBattleNetworkLogin",
                "-lua-debug-port",
                "3382",
            ),
        )

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
