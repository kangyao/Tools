from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from launch_profiles import (  # noqa: E402
    LaunchProfile, LauncherConfig, ProfileStore, pair_mismatches, profile_from_command,
)
from launcher_core import (  # noqa: E402
    ConfigStore, ConfigurationError, LauncherSettings, NetworkRole, format_command_preview,
)


def host(name: str = "本机 Host", **values: object) -> LaunchProfile:
    return LaunchProfile(name=name, role=NetworkRole.HOST, **values)  # type: ignore[arg-type]


def client(name: str = "本机 Client", **values: object) -> LaunchProfile:
    return LaunchProfile(name=name, role=NetworkRole.CLIENT, **values)  # type: ignore[arg-type]


class LaunchProfileTests(unittest.TestCase):
    def test_arguments_match_scripts_then_extras_then_debug_wait(self) -> None:
        profile = client(extra_arguments=("-client-only", "a b"), debug_wait=True)
        self.assertEqual(profile.settings().arguments, (
            "-MGFDevAccount", "2", "-MGFNetRole", "Client", "-MGFNetHost", "127.0.0.1", "-MGFNetPort", "7000",
            "-MGFNetRoomId", "1",
            "-client-only", "a b", "-script-debug-wait-client",
        ))
        self.assertEqual(host().settings().arguments, (
            "-MGFDevAccount", "1", "-MGFNetRole", "Host", "-MGFNetPort", "7000", "-MGFNetRoomId", "1",
        ))
        self.assertIn('"a b"', format_command_preview(profile.settings()))

    def test_summary_shows_key_parameters(self) -> None:
        self.assertEqual(host().summary(), "AICore_profile.exe · 端口 7000 · 房间 1 · 账号 1")
        self.assertEqual(client().summary(), "127.0.0.1:7000 · 房间 1 · 起始账号 2")
        self.assertIn("固定账号 5", client(dev_account=5, auto_dev_account=False).summary())
        self.assertIn("等待调试器", host(debug_wait=True).summary())

    def test_invalid_fields_are_rejected_with_field_names(self) -> None:
        cases = {
            "网络参数 -MGFNetPort": lambda: client(extra_arguments=("-MGFNetPort", "7001")),
            "等待 Lua 调试器": lambda: client(extra_arguments=("-script-debug-wait-client",)),
            "-lua-debug-port": lambda: host(extra_arguments=("-lua-debug-port", "1")),
            "开发账号序号": lambda: client(dev_account=-1),
            "-MGFNetUin 已移除": lambda: client(extra_arguments=("-MGFNetUin", "10001")),
            "网络参数 -MGFDevAccount": lambda: host(extra_arguments=("-MGFDevAccount", "3")),
            "配置名称": lambda: host(name="  "),
            "端口": lambda: host(port=70000),
            "房间号": lambda: host(room_id=0),
            "程序路径": lambda: host(executable=""),
        }
        for message, create in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(ConfigurationError, message):
                create()

    def test_command_round_trips_to_the_same_profile(self) -> None:
        profile = client(
            executable=r"C:\Program Files\Game\AICore.exe", host="10.0.0.8", port=7010,
            room_id=3, dev_account=5, extra_arguments=("-x", "a b"), debug_wait=True,
            show_console=False, auto_dev_account=False,
        )
        command = format_command_preview(profile.settings())
        self.assertEqual(profile_from_command(command, profile), profile)

    def test_edited_command_updates_fields_and_keeps_the_rest(self) -> None:
        base = host(show_console=False)
        edited = profile_from_command(
            r'"D:\Bin\App.exe" -MGFNetRole Client -MGFNetHost 10.0.0.9 -MGFNetPort 7100 '
            r'-MGFNetRoomId 4 -script-debug-wait-client -custom "x y"',
            base,
        )
        self.assertEqual(
            (edited.role, edited.executable, edited.host, edited.port, edited.room_id),
            (NetworkRole.CLIENT, r"D:\Bin\App.exe", "10.0.0.9", 7100, 4),
        )
        self.assertEqual(edited.dev_account, 2)  # 未写 -MGFDevAccount 时取 Client 默认值。
        self.assertEqual(edited.extra_arguments, ("-custom", "x y"))
        self.assertTrue(edited.debug_wait)
        self.assertEqual((edited.id, edited.name, edited.show_console), (base.id, base.name, False))

    def test_invalid_commands_are_explained(self) -> None:
        cases = {
            "程序路径": "-MGFNetRole Host",
            "-MGFNetRole Host 或": "App.exe -MGFNetPort 7000",
            "已移除": "App.exe -MGFNetRole Client -MGFNetUin 10001",
            "缺少值": "App.exe -MGFNetRole Host -MGFNetPort",
            "-lua-debug-port": "App.exe -MGFNetRole Host -lua-debug-port 3382",
        }
        for message, command in cases.items():
            with self.subTest(command=command), self.assertRaisesRegex(ConfigurationError, message):
                profile_from_command(command, host())

    def test_pair_mismatches_name_each_field(self) -> None:
        self.assertEqual(pair_mismatches(host(), client()), ())
        self.assertEqual(pair_mismatches(host(), client(host="localhost")), ())
        mismatches = pair_mismatches(host(), client(host="10.0.0.8", port=7010, room_id=2))
        self.assertEqual(len(mismatches), 3)
        self.assertIn("10.0.0.8", mismatches[0])
        self.assertIn("7010", mismatches[1])
        self.assertIn("房间 2", mismatches[2])


class LauncherConfigTests(unittest.TestCase):
    def test_default_selects_a_matching_host_and_client(self) -> None:
        config = LauncherConfig.default()
        host_profile, client_profile = config.selected(NetworkRole.HOST), config.selected(NetworkRole.CLIENT)
        self.assertEqual((host_profile.name, client_profile.name), ("默认 Host", "默认 Client"))
        self.assertEqual(pair_mismatches(host_profile, client_profile), ())
        self.assertFalse(host_profile.debug_wait)
        self.assertEqual(config.client_delay_seconds, 2.0)
        self.assertTrue(config.auto_tile_windows)

    def test_names_must_be_unique_ignoring_case(self) -> None:
        config = LauncherConfig((host("Local"),))
        with self.assertRaisesRegex(ConfigurationError, "已存在名为「Local」"):
            config.save_profile(client("local"))
        self.assertEqual(config.unique_name("LOCAL"), "LOCAL 2")

    def test_editing_one_profile_keeps_the_others(self) -> None:
        a, b = host("A"), host("B", port=7010)
        config = LauncherConfig((a, b), selected_host_id=a.id)
        edited = config.save_profile(replace(a, port=7020))
        self.assertEqual(edited.profile(a.id).port, 7020)
        self.assertEqual(edited.profile(b.id), b)
        self.assertEqual(edited.selected_host_id, a.id)

    def test_saving_new_profile_selects_it_only_when_role_has_no_selection(self) -> None:
        a = host("A")
        config = LauncherConfig((a,), selected_host_id=a.id)
        config = config.save_profile(host("B"))
        self.assertEqual(config.selected_host_id, a.id)
        remote = client("Remote")
        self.assertEqual(config.save_profile(remote).selected_client_id, remote.id)

    def test_role_change_and_delete_clear_the_stale_selection(self) -> None:
        a, c = host("A"), client("C")
        config = LauncherConfig((a, c), a.id, c.id)
        changed = config.save_profile(replace(a, role=NetworkRole.CLIENT))
        self.assertIsNone(changed.selected_host_id)
        self.assertEqual(changed.selected_client_id, c.id)
        removed = config.remove_profile(c.id)
        self.assertIsNone(removed.selected_client_id)
        self.assertEqual(removed.selected_host_id, a.id)

    def test_selection_must_match_role(self) -> None:
        c = client()
        with self.assertRaises(ConfigurationError):
            LauncherConfig((c,), selected_host_id=c.id)

    def test_invalid_delay_is_rejected(self) -> None:
        for delay in (-1, 61, float("nan"), float("inf"), "2", True):
            with self.subTest(delay=delay), self.assertRaises(ConfigurationError):
                LauncherConfig(client_delay_seconds=delay)  # type: ignore[arg-type]

    def test_invalid_auto_tile_setting_is_rejected(self) -> None:
        for value in (0, 1, "true", None):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                LauncherConfig(auto_tile_windows=value)  # type: ignore[arg-type]

    def test_activation_replaces_the_previous_profile_of_the_role(self) -> None:
        a, b, c = host("A"), host("B"), client("C")
        config = LauncherConfig((a, b, c), a.id, c.id)
        config = config.with_selection(NetworkRole.HOST, b.id)
        self.assertEqual((config.is_active(a), config.is_active(b), config.is_active(c)), (False, True, True))

    def test_invalid_client_count_is_rejected(self) -> None:
        for count in (0, 17, "2", True, 1.5):
            with self.subTest(count=count), self.assertRaises(ConfigurationError):
                LauncherConfig(client_count=count)  # type: ignore[arg-type]


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        self.store = ProfileStore(self.path)

    def test_missing_file_loads_defaults(self) -> None:
        self.assertEqual(len(self.store.load().profiles), 2)

    def test_named_profiles_and_selection_round_trip(self) -> None:
        profiles = (
            host(executable=r"C:\Host\AICore.exe", show_console=False, extra_arguments=("-h", "a b")),
            host("Debug Host", port=7010, debug_wait=True),
            client(),
            client("测试机 Client", host="10.0.0.8", dev_account=8, auto_dev_account=False),
        )
        config = LauncherConfig(
            profiles, profiles[1].id, profiles[3].id,
            client_delay_seconds=3.5, window_geometry="1100x911+100-30", client_count=4,
            auto_tile_windows=False,
        )
        self.store.save(config)
        self.assertEqual(self.store.load(), config)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 4)
        self.assertFalse(payload["auto_tile_windows"])

    def test_v2_host_migrates_to_default_host_and_client(self) -> None:
        ConfigStore(self.path).save(LauncherSettings(
            executable=r"C:\Custom\App.exe",
            arguments=("-MGFNetRole", "Host", "-MGFNetPort", "7008", "-MGFNetRoomId", "9", "-host-only",
                       "-script-debug-wait-client"),
            window_geometry="1100x850+10+20", show_console=False,
        ))
        config = self.store.load()
        host_profile, client_profile = config.selected(NetworkRole.HOST), config.selected(NetworkRole.CLIENT)
        self.assertEqual((host_profile.name, client_profile.name), ("默认 Host", "默认 Client"))
        self.assertEqual(host_profile.extra_arguments, ("-host-only",))
        self.assertEqual(client_profile.extra_arguments, ())
        self.assertEqual((client_profile.port, client_profile.room_id), (7008, 9))
        self.assertEqual((host_profile.dev_account, client_profile.dev_account), (1, 2))
        self.assertEqual(client_profile.executable, r"C:\Custom\App.exe")
        self.assertFalse(client_profile.show_console)
        self.assertTrue(host_profile.debug_wait and client_profile.debug_wait)
        self.assertEqual(config.window_geometry, "1100x850+10+20")

    def test_v2_remote_client_keeps_remote_target_and_client_only_options(self) -> None:
        ConfigStore(self.path).save(LauncherSettings(arguments=(
            "-MGFNetRole", "Client", "-MGFNetHost", "10.0.0.8",
            "-MGFNetUin", "10008", "-client-only",
        )))
        config = self.store.load()
        client_profile = config.selected(NetworkRole.CLIENT)
        self.assertEqual((client_profile.host, client_profile.dev_account), ("10.0.0.8", 2))
        self.assertEqual(client_profile.extra_arguments, ("-client-only",), "旧 -MGFNetUin 随迁移丢弃")
        self.assertEqual(config.selected(NetworkRole.HOST).extra_arguments, ())
        self.assertFalse(client_profile.debug_wait)

    def test_v1_gameplay_config_migrates_without_gameplay_flags(self) -> None:
        self.path.write_text(json.dumps({
            "version": 1, "executable": r"C:\MiniGame\Bin64\AIFramework_d.exe",
            "arguments": ["-MGFTopBattle", "-script-debug-wait-client"], "argument_enabled": [True, False],
            "network_role": "host", "network_port": 19120,
        }), encoding="utf-8")
        host_profile = self.store.load().selected(NetworkRole.HOST)
        self.assertEqual(host_profile.port, 7000)
        self.assertEqual(host_profile.extra_arguments, ())
        self.assertFalse(host_profile.debug_wait)
        self.assertTrue(host_profile.executable.endswith("AICore_profile.exe"))

    def test_v3_two_form_config_migrates(self) -> None:
        self.path.write_text(json.dumps({
            "version": 3,
            "host": {"executable": r"C:\Host.exe", "arguments": [
                "-MGFNetRole", "Host", "-MGFNetPort", "7005", "-MGFNetRoomId", "3", "-x"]},
            "client": {"executable": r"C:\Client.exe", "arguments": [
                "-MGFNetRole", "Client", "-MGFNetHost", "10.0.0.8", "-MGFNetPort", "7000",
                "-MGFNetUin", "10004", "-script-debug-wait-client"], "show_console": False},
            "client_follow_host": True, "client_auto_uin": False,
            "client_delay_seconds": 1.5, "window_geometry": "1100x911+1395+61",
        }), encoding="utf-8")
        config = self.store.load()
        host_profile, client_profile = config.selected(NetworkRole.HOST), config.selected(NetworkRole.CLIENT)
        self.assertEqual((host_profile.executable, host_profile.extra_arguments), (r"C:\Host.exe", ("-x",)))
        self.assertEqual(pair_mismatches(host_profile, client_profile), ())
        self.assertEqual((client_profile.dev_account, client_profile.auto_dev_account), (2, False))
        self.assertTrue(client_profile.debug_wait)
        self.assertFalse(client_profile.show_console)
        self.assertEqual((config.client_delay_seconds, config.window_geometry), (1.5, "1100x911+1395+61"))

    def test_v4_uin_fields_migrate_to_dev_account(self) -> None:
        self.path.write_text(json.dumps({"version": 4, "profiles": [
            {"id": "h", "name": "旧 Host", "role": "host", "uin": 10001, "auto_uin": True},
            {"id": "c", "name": "旧 Client", "role": "client", "uin": 10004, "auto_uin": False},
        ], "selected_host": "h", "selected_client": "c"}), encoding="utf-8")
        config = self.store.load()
        host_profile, client_profile = config.selected(NetworkRole.HOST), config.selected(NetworkRole.CLIENT)
        self.assertEqual((host_profile.dev_account, client_profile.dev_account), (1, 2))
        self.assertFalse(client_profile.auto_dev_account)
        self.assertTrue(config.auto_tile_windows)
        self.assertNotIn("-MGFNetUin", client_profile.settings().arguments)
        self.store.save(config)
        saved = json.loads(self.path.read_text(encoding="utf-8"))["profiles"][1]
        self.assertNotIn("uin", saved)
        self.assertEqual((saved["dev_account"], saved["auto_dev_account"]), (2, False))

    def test_invalid_v4_profile_names_the_profile(self) -> None:
        self.path.write_text(json.dumps({"version": 4, "profiles": [
            {"id": "a", "name": "坏配置", "role": "client", "dev_account": -1},
        ]}), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "「坏配置」.*开发账号序号"):
            self.store.load()
        self.path.write_text('{"version": 4, "profiles": {}}', encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            self.store.load()


if __name__ == "__main__":
    unittest.main()
