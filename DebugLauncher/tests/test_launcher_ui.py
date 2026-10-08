from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import DEFAULT, patch

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import launcher  # noqa: E402
import profile_manager  # noqa: E402
from launch_profiles import LaunchProfile, LauncherConfig, ProfileStore  # noqa: E402
from launcher_core import (  # noqa: E402
    DEFAULT_EXECUTABLE, DebugPortAllocator, LauncherController, NetworkRole,
)
from test_launcher_core import FakeFactory  # noqa: E402
from window_layout import LayoutResult  # noqa: E402


HOST = NetworkRole.HOST
CLIENT = NetworkRole.CLIENT


class FakeWindowLayoutService:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], int, threading.Event]] = []
        self.block = False

    def arrange(self, targets, anchor_hwnd, cancel):
        targets = tuple(targets)
        self.calls.append((targets, anchor_hwnd, cancel))
        if self.block:
            cancel.wait(2)
        return LayoutResult(
            moved=tuple(target.instance_id for target in targets),
            cancelled=cancel.is_set(),
        )


@unittest.skipUnless(sys.platform == "win32", "Windows launcher UI")
class LauncherUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "settings.json"
        for name in ("_initialize_default_debug_service", "_start_debug_service"):
            patcher = patch.object(launcher.DebugLauncherApp, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(launcher, "CONFIG_PATH", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.open_app()
        self.error_patch = patch.object(launcher.DebugLauncherApp, "_show_error")
        self.show_error = self.error_patch.start()
        self.addCleanup(self.error_patch.stop)
        self.dialogs = patch.multiple(
            profile_manager.messagebox,
            showerror=DEFAULT, askyesno=DEFAULT, askyesnocancel=DEFAULT,
        )
        self.dialog_mocks = self.dialogs.start()
        self.addCleanup(self.dialogs.stop)

    def open_app(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.layout_service = FakeWindowLayoutService()
        self.app = launcher.DebugLauncherApp(self.root, self.layout_service)
        self.addCleanup(self.close_app, self.app)
        self.factory = FakeFactory()
        self.app.controller = LauncherController(self.factory, DebugPortAllocator(lambda _: True))
        self.app.client_delay_var.set("0")

    def close_app(self, app: launcher.DebugLauncherApp) -> None:
        if app.profile_manager is not None:  # 清理时不弹出未保存提示。
            app.profile_manager.window.destroy()
            app.profile_manager = None
        if not app.closing:
            app._on_close()

    def seed(self, *profiles: LaunchProfile) -> LauncherConfig:
        """Replace the saved configuration and reload it into the main window."""
        config = LauncherConfig.with_default_selection(profiles, client_delay_seconds=0)
        ProfileStore(self.config).save(config)
        self.app._load_settings()
        return config

    def wait_until_idle(self) -> None:
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy, "Launch operation did not complete")
        self.show_error.assert_not_called()

    def wait_for_layout(self, call_count: int) -> None:
        deadline = time.monotonic() + 3
        while len(self.layout_service.calls) < call_count and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(len(self.layout_service.calls), call_count)
        self.root.update()

    def activate(self, name: str, active: bool = True) -> None:
        manager = self.manager()
        profile = next(p for p in self.app.config.profiles if p.name == name)
        manager.select_profile(profile.id)
        manager.active_var.set(active)

    def manager(self) -> profile_manager.ProfileManagerWindow:
        self.app._open_profile_manager()
        assert self.app.profile_manager is not None
        return self.app.profile_manager

    def tree_values(self) -> list[tuple[str, ...]]:
        tree = self.app.instance_tree
        return [tuple(str(value) for value in tree.item(item, "values")) for item in tree.get_children()]

    # ----- main window --------------------------------------------------------------------------

    def test_main_window_shows_the_profile_activated_in_manager(self) -> None:
        self.seed(
            LaunchProfile(name="本机 Host", role=HOST),
            LaunchProfile(name="Debug Host", role=HOST, port=7010, debug_wait=True),
            LaunchProfile(name="本机 Client", role=CLIENT),
        )
        controls = self.app.role_controls
        self.assertEqual(controls[HOST].name.get(), "本机 Host")
        self.activate("Debug Host")
        self.assertEqual(controls[HOST].name.get(), "Debug Host")
        self.assertIn("端口 7010", controls[HOST].summary.get())
        self.assertIn("等待调试器", controls[HOST].summary.get())
        manager = self.app.profile_manager
        marks = [manager.profile_list.get(index)[0] for index in range(manager.profile_list.size())]
        self.assertEqual(marks, ["　", "✓", "✓"])
        self.app.client_count_var.set("3")
        self.close_app(self.app)
        self.open_app()
        self.assertEqual(self.app.role_controls[HOST].name.get(), "Debug Host")
        self.assertEqual(self.app.role_controls[CLIENT].name.get(), "本机 Client")
        self.assertEqual(self.app.client_count_var.get(), "3")
        self.assertEqual(str(self.app.start_pair_button.cget("text")), "一键启动 Host + 3 个 Client")

    def test_deactivating_disables_that_role_only(self) -> None:
        self.activate("默认 Client", False)
        self.assertEqual(self.app.role_controls[CLIENT].name.get(), "未激活")
        self.assertIsNone(self.app.store.load().selected_client_id)
        self.assertEqual(str(self.app.role_controls[CLIENT].start_button.cget("state")), tk.DISABLED)
        self.assertEqual(str(self.app.start_pair_button.cget("state")), tk.DISABLED)
        self.assertEqual(str(self.app.role_controls[HOST].start_button.cget("state")), tk.NORMAL)

    def test_client_count_launches_several_clients(self) -> None:
        self.app.client_count_var.set("3")
        self.assertEqual(str(self.app.role_controls[CLIENT].start_button.cget("text")), "启动 3 个 Client")
        self.app.start_pair_button.invoke()
        self.wait_until_idle()
        rows = self.tree_values()
        self.assertEqual([row[2] for row in rows], ["Host", "Client", "Client", "Client"])
        self.assertEqual([row[6] for row in rows], ["1", "2", "3", "4"])
        self.app.role_controls[CLIENT].start_button.invoke()
        self.wait_until_idle()
        self.assertEqual([row[6] for row in self.tree_values()][4:], ["5", "6", "7"])
        self.assertEqual(self.app.store.load().client_count, 3)

    def test_auto_tile_setting_is_persisted(self) -> None:
        self.assertTrue(self.app.auto_tile_windows_var.get())
        self.app.auto_tile_windows_button.invoke()
        self.assertFalse(self.app.auto_tile_windows_var.get())
        self.assertFalse(self.app.store.load().auto_tile_windows)
        self.close_app(self.app)
        self.open_app()
        self.assertFalse(self.app.auto_tile_windows_var.get())

    def test_single_host_requests_window_layout(self) -> None:
        self.app.role_controls[HOST].start_button.invoke()
        self.wait_until_idle()
        self.wait_for_layout(1)
        targets, anchor_hwnd, _cancel = self.layout_service.calls[0]
        self.assertEqual([(target.instance_id, target.pid) for target in targets], [(1, 1000)])
        self.assertGreater(anchor_hwnd, 0)

    def test_client_batch_requests_one_layout(self) -> None:
        self.app.client_count_var.set("3")
        self.app.role_controls[CLIENT].start_button.invoke()
        self.wait_until_idle()
        self.wait_for_layout(1)
        targets = self.layout_service.calls[0][0]
        self.assertEqual([target.instance_id for target in targets], [1, 2, 3])

    def test_pair_requests_one_layout_after_clients_finish(self) -> None:
        self.app.client_count_var.set("3")
        self.app.start_pair_button.invoke()
        self.wait_until_idle()
        self.wait_for_layout(1)
        targets = self.layout_service.calls[0][0]
        self.assertEqual([target.instance_id for target in targets], [1, 2, 3, 4])

    def test_layout_orders_host_before_older_client(self) -> None:
        self.app.role_controls[CLIENT].start_button.invoke()
        self.wait_until_idle()
        self.wait_for_layout(1)
        self.app.role_controls[HOST].start_button.invoke()
        self.wait_until_idle()
        self.wait_for_layout(2)
        targets = self.layout_service.calls[1][0]
        self.assertEqual([target.instance_id for target in targets], [2, 1])

    def test_restart_layout_uses_new_pid_and_keeps_instance_id(self) -> None:
        self.app.role_controls[HOST].start_button.invoke()
        self.wait_until_idle()
        self.wait_for_layout(1)
        item = self.app.instance_tree.get_children()[0]
        self.app.instance_tree.selection_set(item)
        self.root.update()
        self.app.restart_button.invoke()
        self.wait_until_idle()
        self.wait_for_layout(2)
        target = self.layout_service.calls[1][0][0]
        self.assertEqual((target.instance_id, target.pid), (1, 1001))

    def test_disabled_auto_tile_does_not_request_layout(self) -> None:
        self.app.auto_tile_windows_button.invoke()
        self.app.role_controls[HOST].start_button.invoke()
        self.wait_until_idle()
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(self.layout_service.calls, [])

    def test_stop_cancels_window_layout_wait(self) -> None:
        self.layout_service.block = True
        self.app.role_controls[HOST].start_button.invoke()
        self.wait_until_idle()
        self.wait_for_layout(1)
        cancel = self.layout_service.calls[0][2]
        item = self.app.instance_tree.get_children()[0]
        self.app.instance_tree.selection_set(item)
        self.root.update()
        self.app.stop_button.invoke()
        self.wait_until_idle()
        self.assertTrue(cancel.is_set())

    def test_fixed_account_client_cannot_start_several(self) -> None:
        self.seed(
            LaunchProfile(name="本机 Host", role=HOST),
            LaunchProfile(name="固定账号 Client", role=CLIENT, auto_dev_account=False),
        )
        self.app.client_count_var.set("2")
        self.app.start_pair_button.invoke()
        self.assertEqual(self.factory.created, [])
        self.assertIn("固定开发账号", str(self.show_error.call_args.args[1]))

    def test_missing_selection_disables_only_that_role(self) -> None:
        self.seed(LaunchProfile(name="本机 Host", role=HOST))
        controls = self.app.role_controls
        self.assertEqual(str(controls[HOST].start_button.cget("state")), tk.NORMAL)
        self.assertEqual(str(controls[CLIENT].start_button.cget("state")), tk.DISABLED)
        self.assertEqual(str(self.app.start_pair_button.cget("state")), tk.DISABLED)
        controls[HOST].start_button.invoke()
        self.wait_until_idle()
        self.assertEqual(self.factory.calls[0][1][3], "Host")

    def test_one_click_launches_pair_then_reuses_host_with_new_dev_account(self) -> None:
        self.app.start_pair_button.invoke()
        self.wait_until_idle()
        self.assertEqual([call[1][3] for call in self.factory.calls], ["Host", "Client"])
        self.app.start_pair_button.invoke()
        self.wait_until_idle()
        self.assertEqual(len(self.factory.created), 3)
        rows = self.tree_values()
        self.assertEqual([row[1] for row in rows], ["默认 Host", "默认 Client", "默认 Client"])
        self.assertEqual([row[6] for row in rows], ["1", "2", "3"])
        self.assertEqual(self.app.store.load().selected(CLIENT).dev_account, 2)

    def test_pair_mismatch_is_reported_without_launching(self) -> None:
        self.seed(
            LaunchProfile(name="本机 Host", role=HOST),
            LaunchProfile(name="测试机 Client", role=CLIENT, host="10.0.0.8", room_id=2),
        )
        self.app.start_pair_button.invoke()
        self.assertEqual(self.factory.created, [])
        message = str(self.show_error.call_args.args[1])
        self.assertIn("测试机 Client", message)
        self.assertIn("10.0.0.8", message)
        self.assertIn("房间 2", message)

    def test_client_failure_keeps_host_and_explains_it(self) -> None:
        self.seed(
            LaunchProfile(name="本机 Host", role=HOST),
            LaunchProfile(name="本机 Client", role=CLIENT, executable=r"C:\missing\Client.exe"),
        )
        normal = self.factory

        def fail_client(executable, arguments, show_console, environment):
            if "Client" in arguments:
                raise launcher.LauncherError("启动程序不存在")
            return normal(executable, arguments, show_console, environment)

        self.app.controller._process_factory = fail_client
        self.app.start_pair_button.invoke()
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
        message = str(self.show_error.call_args.args[1])
        self.assertIn("启动程序不存在", message)
        self.assertIn("本机 Host", message)
        self.assertEqual(len(self.tree_values()), 1)
        self.wait_for_layout(1)
        self.assertEqual(
            [target.instance_id for target in self.layout_service.calls[0][0]], [1],
        )

    def test_open_log_opens_the_instance_log_file(self) -> None:
        bin_dir = Path(self.directory.name) / "Bin64"
        bin_dir.mkdir()
        log = bin_dir / "AICoreApp_dev2.log"
        log.write_text("log", encoding="utf-8")
        self.seed(
            LaunchProfile(name="本机 Host", role=HOST, executable=str(bin_dir / "AICore.exe")),
            LaunchProfile(name="本机 Client", role=CLIENT, executable=str(bin_dir / "AICore.exe")),
        )
        self.assertEqual(str(self.app.log_button.cget("state")), tk.DISABLED)
        self.app.role_controls[CLIENT].start_button.invoke()
        self.wait_until_idle()
        self.app.role_controls[CLIENT].start_button.invoke()
        self.wait_until_idle()
        first, second = self.app.instance_tree.get_children()
        self.app.instance_tree.selection_set(first)
        self.root.update()
        with patch.object(launcher.os, "startfile", create=True) as startfile:
            self.app.log_button.invoke()
            startfile.assert_called_once_with(log)
            startfile.reset_mock()
            self.app.instance_tree.selection_set(second)
            self.root.update()
            with patch.object(launcher.messagebox, "askyesno", return_value=True) as ask:
                self.app.log_button.invoke()
            self.assertIn("AICoreApp_dev3.log", ask.call_args.args[1])
            startfile.assert_called_once_with(bin_dir)

    def test_double_click_on_instance_opens_its_log(self) -> None:
        self.app.role_controls[HOST].start_button.invoke()
        self.wait_until_idle()
        with patch.object(self.app, "_open_instance_log") as open_log, \
                patch.object(self.app.instance_tree, "identify_row", return_value="1"):
            self.app._on_instance_double_click(type("Event", (), {"y": 5})())
        open_log.assert_called_once_with()
        self.assertEqual(
            self.app._instance_log_path(1).name, "AICoreApp_dev1.log",
        )

    def test_view_command_shows_actual_dev_account_and_debug_port(self) -> None:
        self.app.role_controls[CLIENT].start_button.invoke()
        self.wait_until_idle()
        self.app.role_controls[CLIENT].start_button.invoke()
        self.wait_until_idle()
        second = self.app.instance_tree.get_children()[-1]
        command = self.app._instance_command(int(second))
        self.assertIn("-MGFDevAccount 3", command)
        self.assertNotIn("-MGFNetUin", command)
        self.assertIn("-lua-debug-port", command)

    def test_legacy_config_loads_as_default_profiles(self) -> None:
        self.config.write_text(json.dumps({
            "version": 1, "executable": DEFAULT_EXECUTABLE,
            "arguments": ["-MGFTopBattle", "-script-debug-wait-client"],
            "argument_enabled": [True, True], "network_role": "standalone", "network_port": 19120,
        }), encoding="utf-8")
        self.app._load_settings()
        host = self.app.config.selected(HOST)
        self.assertEqual((host.name, host.extra_arguments, host.debug_wait), ("默认 Host", (), True))
        self.assertEqual(self.app.role_controls[CLIENT].name.get(), "默认 Client")

    # ----- configuration manager ----------------------------------------------------------------

    def test_unsaved_edits_are_not_used_until_saved(self) -> None:
        manager = self.manager()
        manager.select_profile(self.app.config.selected_host_id)
        manager.form.port.set("7020")
        self.assertIn("-MGFNetPort 7020", manager.preview_var.get())
        self.app.role_controls[HOST].start_button.invoke()
        self.wait_until_idle()
        self.assertIn("7000", self.factory.calls[-1][1])
        self.assertTrue(manager.save())
        self.assertIn("端口 7020", self.app.role_controls[HOST].summary.get())
        self.assertEqual(self.app.store.load().selected(HOST).port, 7020)
        self.assertEqual(self.tree_values()[0][4], "监听 :7000")  # 运行实例保留启动快照。

    def test_cancel_restores_saved_profile(self) -> None:
        manager = self.manager()
        manager.select_profile(self.app.config.selected_client_id)
        manager.form.dev_account.set("9")
        self.assertTrue(manager.is_dirty)
        manager.cancel()
        self.assertEqual(manager.form.dev_account.get(), "2")
        self.assertFalse(manager.is_dirty)
        self.assertEqual(self.app.store.load().selected(CLIENT).dev_account, 2)

    def test_switching_with_unsaved_edits_asks_before_discarding(self) -> None:
        manager = self.manager()
        host_id, client_id = self.app.config.selected_host_id, self.app.config.selected_client_id
        manager.select_profile(host_id)
        manager.form.port.set("7030")
        ask = self.dialog_mocks["askyesnocancel"]
        ask.return_value = None  # 继续编辑
        manager.select_profile(client_id)
        self.assertEqual(manager.current_id, host_id)
        ask.return_value = False  # 放弃修改
        manager.select_profile(client_id)
        self.assertEqual(manager.current_id, client_id)
        self.assertEqual(self.app.config.selected(HOST).port, 7000)
        manager.select_profile(host_id)
        manager.form.port.set("7040")
        ask.return_value = True  # 保存
        manager.select_profile(client_id)
        self.assertEqual(self.app.config.selected(HOST).port, 7040)
        self.assertEqual(self.app.config.selected(CLIENT).port, 7000)

    def test_new_client_profile_is_activated_from_manager(self) -> None:
        manager = self.manager()
        manager.new_profile()
        self.assertIsNone(manager.current_id)
        manager.form.role.set("Client")
        manager.form.name.set("测试机 Client")
        manager.form.host.set("10.0.0.8")
        self.assertFalse(manager.active_var.get())  # Client 已有激活配置，新配置默认不激活。
        self.assertTrue(manager.save())
        self.assertEqual(self.app.role_controls[CLIENT].name.get(), "默认 Client")
        manager.active_var.set(True)
        self.assertEqual(self.app.role_controls[CLIENT].name.get(), "测试机 Client")
        self.assertIn("10.0.0.8:7000", self.app.role_controls[CLIENT].summary.get())

    def test_new_profile_can_be_activated_when_saved(self) -> None:
        manager = self.manager()
        manager.new_profile()
        manager.form.name.set("Debug Host")
        manager.active_var.set(True)
        self.assertIsNone(manager.current_id)
        self.assertEqual(self.app.role_controls[HOST].name.get(), "默认 Host")
        self.assertTrue(manager.save())
        self.assertEqual(self.app.role_controls[HOST].name.get(), "Debug Host")

    def test_copy_and_save_as_create_independent_profiles(self) -> None:
        manager = self.manager()
        host_id = self.app.config.selected_host_id
        manager.select_profile(host_id)
        manager.copy_profile()
        self.assertEqual(manager.form.name.get(), "默认 Host 副本")
        manager.form.port.set("7010")
        self.assertTrue(manager.save())
        manager.select_profile(host_id)
        manager.form.room_id.set("5")
        with patch.object(profile_manager.simpledialog, "askstring", return_value="房间 5 Host"):
            manager.save_as()
        config = self.app.config
        self.assertEqual(len(config.profiles_for(HOST)), 3)
        self.assertEqual(config.profile(host_id).room_id, 1)
        self.assertEqual(config.profile(host_id).port, 7000)
        self.assertEqual({p.name for p in config.profiles_for(HOST)}, {"默认 Host", "默认 Host 副本", "房间 5 Host"})

    def test_duplicate_name_is_rejected(self) -> None:
        manager = self.manager()
        manager.select_profile(self.app.config.selected_client_id)
        manager.form.name.set("默认 host")
        self.assertFalse(manager.save())
        self.dialog_mocks["showerror"].assert_called_once()
        self.assertEqual(self.app.store.load().selected(CLIENT).name, "默认 Client")

    def test_editing_command_updates_form_and_is_saved(self) -> None:
        manager = self.manager()
        manager.select_profile(self.app.config.selected_host_id)
        manager.preview_var.set(f'"{DEFAULT_EXECUTABLE}" -MGFNetRole Host -MGFNetPort 7200 -extra "a b"')
        self.assertEqual(manager.form.port.get(), "7200")
        self.assertEqual(manager.form.dev_account.get(), "1")
        self.assertEqual(manager.form.extra_arguments.get(), '-extra "a b"')
        self.assertIn("-MGFNetPort 7200", manager.preview_var.get())  # 编辑中不改写用户文字。
        manager._finish_command_edit()
        self.assertEqual(
            manager.preview_var.get(),
            f"{DEFAULT_EXECUTABLE} -MGFDevAccount 1 -MGFNetRole Host -MGFNetPort 7200 -MGFNetRoomId 1 -extra \"a b\"",
        )
        self.assertTrue(manager.is_dirty)
        self.assertTrue(manager.save())
        saved = self.app.store.load().selected(HOST)
        self.assertEqual((saved.port, saved.extra_arguments), (7200, ("-extra", "a b")))

    def test_invalid_command_keeps_form_and_text_until_escape(self) -> None:
        manager = self.manager()
        manager.select_profile(self.app.config.selected_client_id)
        manager.preview_var.set(f"{DEFAULT_EXECUTABLE} -MGFNetRole Client -MGFNetPort")
        self.assertIn("启动命令无法解析", manager.validation_var.get())
        self.assertEqual(manager.form.port.get(), "7000")
        manager._finish_command_edit()
        self.assertIn("-MGFNetPort", manager.preview_var.get())
        self.assertFalse(manager.preview_var.get().endswith("7000"))
        manager._cancel_command_edit()
        self.assertIn("-MGFNetPort 7000", manager.preview_var.get())
        self.assertEqual(manager.validation_var.get(), "")
        self.assertFalse(manager.is_dirty)

    def test_extra_network_argument_points_to_form_field(self) -> None:
        manager = self.manager()
        manager.form.extra_arguments.set("-MGFNetPort 7001")
        self.assertIn("对应的表单字段", manager.validation_var.get())
        self.assertEqual(manager.preview_var.get(), "")

    def test_delete_clears_selection_but_keeps_running_instance(self) -> None:
        self.app.role_controls[CLIENT].start_button.invoke()
        self.wait_until_idle()
        manager = self.manager()
        manager.select_profile(self.app.config.selected_client_id)
        self.dialog_mocks["askyesno"].return_value = True
        manager.delete_profile()
        self.assertIsNone(self.app.store.load().selected_client_id)
        self.assertEqual(self.app.role_controls[CLIENT].name.get(), "未激活")
        self.assertEqual(str(self.app.start_pair_button.cget("state")), tk.DISABLED)
        self.assertEqual(len(self.tree_values()), 1)
        self.assertEqual(self.tree_values()[0][1], "默认 Client")

    def test_client_fields_are_shown_only_for_client_profiles(self) -> None:
        manager = self.manager()
        manager.select_profile(self.app.config.selected_client_id)
        self.assertTrue(all(widget.winfo_manager() == "grid" for widget in manager.client_only))
        manager.select_profile(self.app.config.selected_host_id)
        self.assertTrue(all(widget.winfo_manager() == "" for widget in manager.client_only))

    def test_activating_with_unsaved_edits_asks_first(self) -> None:
        self.seed(
            LaunchProfile(name="本机 Host", role=HOST),
            LaunchProfile(name="Debug Host", role=HOST, port=7010),
            LaunchProfile(name="本机 Client", role=CLIENT),
        )
        manager = self.manager()
        debug_id = self.app.config.profiles[1].id
        manager.select_profile(debug_id)
        manager.form.port.set("7020")
        ask = self.dialog_mocks["askyesnocancel"]
        ask.return_value = None  # 继续编辑：不激活，保留草稿。
        manager.active_var.set(True)
        self.assertFalse(manager.active_var.get())
        self.assertEqual(self.app.role_controls[HOST].name.get(), "本机 Host")
        self.assertEqual(manager.form.port.get(), "7020")
        ask.return_value = False  # 放弃修改后激活。
        manager.active_var.set(True)
        self.assertEqual(self.app.role_controls[HOST].name.get(), "Debug Host")
        self.assertEqual(manager.form.port.get(), "7010")
        self.assertFalse(manager.is_dirty)

    def test_closing_with_unsaved_edits_can_be_cancelled(self) -> None:
        manager = self.manager()
        manager.form.port.set("7050")
        self.dialog_mocks["askyesnocancel"].return_value = None
        self.app._on_close()
        self.assertFalse(self.app.closing)
        self.assertIs(self.app.profile_manager, manager)
        self.dialog_mocks["askyesnocancel"].return_value = False
        self.app._on_close()
        self.assertTrue(self.app.closing)
        self.assertEqual(self.app.store.load().selected(HOST).port, 7000)


if __name__ == "__main__":
    unittest.main()
