from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass, replace
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from launcher_core import (
    ControllerSnapshot,
    DEBUG_WAIT_ARGUMENT,
    DEFAULT_SCRIPT_DEBUG_PORT,
    DebugPortBundle,
    ClientLaunchResult,
    LauncherController,
    LauncherError,
    MAX_CLIENT_COUNT,
    LauncherSettings,
    NetworkRole,
    PairLaunchResult,
    is_valid_window_geometry,
    load_attach_configuration,
    validate_client_count,
)
from launch_profiles import (
    LAUNCH_ROLES,
    ROLE_TITLES,
    LauncherConfig,
    ProfileStore,
    pair_mismatches,
)
from persistent_debug_server import PersistentDebugOptions, run_embedded_debug_server
from profile_manager import ProfileManagerWindow


TOOL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_DIR / "settings.local.json"
RUN_CONFIGURATION_PATH = TOOL_DIR.parents[1] / ".run" / "Lua.run.xml"


@dataclass
class _DebugService:
    stop_event: threading.Event
    thread: threading.Thread


@dataclass
class _RoleControls:
    name: tk.StringVar
    summary: tk.StringVar
    start_button: ttk.Button


class DebugLauncherApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.store = ProfileStore(CONFIG_PATH)
        self.config = LauncherConfig.default()
        self.controller = LauncherController()
        self.attach_configuration = load_attach_configuration(
            RUN_CONFIGURATION_PATH
        )
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.busy = False
        self.closing = False
        self.snapshots_by_id: dict[int, ControllerSnapshot] = {}
        self.debug_services: dict[int, _DebugService] = {}
        self.debug_states: dict[int, str] = {}
        self.debug_attach_clients: dict[int, str] = {}
        self.debug_runtime_clients: dict[int, str] = {}
        self.debug_component_missing_reported = False
        self.default_debug_port: int | None = None
        self.status_after_id: str | None = None
        self.normal_window_geometry = ""
        self.launch_cancel = threading.Event()
        self.profile_manager: ProfileManagerWindow | None = None
        self.role_controls: dict[NetworkRole, _RoleControls] = {}
        self.client_delay_var = tk.StringVar(value="2")
        self.client_count_var = tk.StringVar(value="1")
        self.status_var = tk.StringVar(value="运行实例：0")
        self.debug_attach_status_var = tk.StringVar(
            value=f"Attach {self.attach_configuration.name}：启动中…"
        )
        self.debug_runtime_status_var = tk.StringVar(
            value=f"{self.attach_configuration.session_name}：未连接"
        )
        self.debug_service_status_var = tk.StringVar(value="")

        self._configure_window()
        self._build_ui()
        self._load_settings()
        self._refresh_status()
        self._drain_events()
        self.root.after_idle(self._initialize_default_debug_service)

    def _configure_window(self) -> None:
        self.root.title("Host / Client 启动器")
        self.root.geometry("1100x820")
        self.root.minsize(960, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._remember_window_geometry, add="+")

        style = ttk.Style(self.root)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Summary.TLabel", foreground="#4b5563")
        style.configure("Profile.TLabel", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(22, 16, 22, 16))
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        launch_frame = ttk.Frame(outer)
        launch_frame.grid(row=0, column=0, sticky=tk.EW)
        launch_frame.columnconfigure(1, weight=1)
        ttk.Label(launch_frame, text="Host / Client 启动器", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 10),
        )
        ttk.Button(launch_frame, text="配置管理", command=self._open_profile_manager).grid(
            row=0, column=3, sticky=tk.E, pady=(0, 10),
        )
        client_count = ttk.Frame(launch_frame)
        client_count.grid(row=3, column=2, sticky=tk.E, padx=8)
        ttk.Label(client_count, text="数量").pack(side=tk.LEFT, padx=(0, 5))
        ttk.Spinbox(
            client_count, textvariable=self.client_count_var,
            from_=1, to=MAX_CLIENT_COUNT, increment=1, width=4,
        ).pack(side=tk.LEFT)
        self._build_role_row(launch_frame, NetworkRole.HOST, 1)
        self._build_role_row(launch_frame, NetworkRole.CLIENT, 3)

        pair_bar = ttk.Frame(launch_frame)
        pair_bar.grid(row=5, column=0, columnspan=4, sticky=tk.EW, pady=(14, 0))
        self.start_pair_button = ttk.Button(
            pair_bar, text="一键启动 Host + Client", style="Primary.TButton",
            command=self._start_pair, padding=(12, 6),
        )
        self.start_pair_button.pack(side=tk.LEFT)
        ttk.Label(pair_bar, text="Host 启动后").pack(side=tk.LEFT, padx=(18, 5))
        ttk.Spinbox(
            pair_bar, textvariable=self.client_delay_var,
            from_=0, to=60, increment=0.5, width=5,
        ).pack(side=tk.LEFT)
        ttk.Label(pair_bar, text="秒启动 Client").pack(side=tk.LEFT, padx=(5, 0))
        self.stop_all_button = ttk.Button(
            pair_bar, text="关闭全部", command=self._stop_all, width=12
        )
        self.stop_all_button.pack(side=tk.RIGHT)

        instance_frame = ttk.LabelFrame(
            outer, text="运行实例", style="Section.TLabelframe", padding=(10, 8)
        )
        instance_frame.grid(row=1, column=0, sticky=tk.EW, pady=(14, 0))
        instance_frame.columnconfigure(0, weight=1)
        columns = (
            "id", "name", "role", "pid", "endpoint", "room", "account", "state",
            "target", "dap", "attach", "runtime", "debug",
        )
        self.instance_tree = ttk.Treeview(
            instance_frame, columns=columns, show="headings", height=5, selectmode="browse"
        )
        headings = {
            "id": ("ID", 45),
            "name": ("配置名称", 140),
            "role": ("角色", 60),
            "pid": ("PID", 70),
            "endpoint": ("网络端点", 150),
            "room": ("房间", 55),
            "account": ("开发账号", 70),
            "state": ("状态", 70),
            "target": ("ScriptDebugger", 110),
            "dap": ("DAP", 60),
            "attach": (f"Attach {self.attach_configuration.name}", 150),
            "runtime": (self.attach_configuration.session_name, 150),
            "debug": ("调试服务", 150),
        }
        for column, (text, width) in headings.items():
            self.instance_tree.heading(column, text=text)
            self.instance_tree.column(column, width=width, anchor=tk.CENTER, stretch=False)
        self.instance_tree.grid(row=0, column=0, sticky=tk.EW)
        self.instance_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_buttons())
        instance_y_scrollbar = ttk.Scrollbar(
            instance_frame, orient=tk.VERTICAL, command=self.instance_tree.yview,
        )
        instance_y_scrollbar.grid(row=0, column=1, sticky=tk.NS)
        instance_x_scrollbar = ttk.Scrollbar(
            instance_frame, orient=tk.HORIZONTAL, command=self.instance_tree.xview,
        )
        instance_x_scrollbar.grid(row=1, column=0, sticky=tk.EW)
        self.instance_tree.configure(
            xscrollcommand=instance_x_scrollbar.set,
            yscrollcommand=instance_y_scrollbar.set,
        )

        action_frame = ttk.Frame(outer, padding=(0, 12, 0, 12))
        action_frame.grid(row=2, column=0, sticky=tk.EW)
        action_frame.columnconfigure(4, weight=1)

        self.stop_button = ttk.Button(
            action_frame, text="关闭选中实例", command=self._stop, width=12
        )
        self.stop_button.grid(row=0, column=0, padx=(0, 8))
        self.restart_button = ttk.Button(
            action_frame, text="重启选中实例", command=self._restart, width=12
        )
        self.restart_button.grid(row=0, column=1, padx=8)
        self.command_button = ttk.Button(
            action_frame, text="查看启动命令", command=self._show_instance_command, width=12
        )
        self.command_button.grid(row=0, column=2, padx=8)

        status_box = ttk.Frame(action_frame)
        status_box.grid(row=0, column=5, sticky=tk.E)
        self.status_dot = tk.Label(
            status_box,
            text="●",
            foreground="#9aa0a6",
            font=("Segoe UI Symbol", 13),
        )
        self.status_dot.pack(side=tk.LEFT)
        ttk.Label(status_box, textvariable=self.status_var).pack(side=tk.LEFT, padx=(5, 0))

        debug_status_box = ttk.Frame(action_frame)
        debug_status_box.grid(row=1, column=0, columnspan=6, sticky=tk.EW, pady=(8, 0))
        self.debug_status_images = {
            "connected": self._create_status_image("#16a34a"),
            "waiting": self._create_status_image("#d97706"),
            "disconnected": self._create_status_image("#9aa0a6"),
            "error": self._create_status_image("#dc2626"),
        }
        self.debug_attach_status_image = ttk.Label(
            debug_status_box,
            image=self.debug_status_images["waiting"],
        )
        self.debug_attach_status_image.pack(side=tk.LEFT)
        ttk.Label(
            debug_status_box,
            textvariable=self.debug_attach_status_var,
        ).pack(side=tk.LEFT, padx=(5, 14))
        self.debug_runtime_status_image = ttk.Label(
            debug_status_box,
            image=self.debug_status_images["disconnected"],
        )
        self.debug_runtime_status_image.pack(side=tk.LEFT)
        ttk.Label(
            debug_status_box,
            textvariable=self.debug_runtime_status_var,
        ).pack(side=tk.LEFT, padx=(5, 14))
        ttk.Label(
            debug_status_box,
            textvariable=self.debug_service_status_var,
        ).pack(side=tk.LEFT)
        self._build_log_ui(outer)
        self.client_count_var.trace_add("write", lambda *_: self._update_launch_labels())
        self._update_launch_labels()

    def _build_role_row(self, parent: ttk.Frame, role: NetworkRole, row: int) -> None:
        """Show the active profile of a role; choosing profiles happens in 配置管理."""
        title = ROLE_TITLES[role]
        name = tk.StringVar()
        summary = tk.StringVar()
        ttk.Label(parent, text=title, style="Profile.TLabel").grid(
            row=row, column=0, sticky=tk.W, padx=(0, 16),
        )
        ttk.Label(parent, textvariable=name, style="Profile.TLabel").grid(row=row, column=1, sticky=tk.W)
        start_button = ttk.Button(
            parent, text=f"启动 {title}", style="Primary.TButton", width=16,
            command=lambda: self._start_role(role),
        )
        start_button.grid(row=row, column=3, sticky=tk.E)
        ttk.Label(parent, textvariable=summary, style="Summary.TLabel").grid(
            row=row + 1, column=1, columnspan=3, sticky=tk.W, pady=(3, 10),
        )
        self.role_controls[role] = _RoleControls(name, summary, start_button)

    def _create_status_image(self, color: str) -> tk.PhotoImage:
        image = tk.PhotoImage(master=self.root, width=14, height=14)
        center = 6.5
        radius_squared = 5.5 * 5.5
        for y in range(14):
            for x in range(14):
                if (x - center) ** 2 + (y - center) ** 2 <= radius_squared:
                    image.put(color, (x, y))
        return image

    @staticmethod
    def _set_status_image(label: ttk.Label, image: tk.PhotoImage) -> None:
        label.configure(image=image)

    def _build_log_ui(self, outer: ttk.Frame) -> None:
        log_frame = ttk.LabelFrame(
            outer, text="运行日志", style="Section.TLabelframe", padding=(10, 8)
        )
        log_frame.grid(row=3, column=0, sticky=tk.NSEW)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame,
            height=10,
            wrap=tk.WORD,
            state=tk.DISABLED,
            background="#111827",
            foreground="#e5e7eb",
            insertbackground="#e5e7eb",
            relief=tk.FLAT,
            padx=10,
            pady=8,
            font=("Cascadia Mono", 9),
        )
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar = ttk.Scrollbar(
            log_frame, orient=tk.VERTICAL, command=self.log_text.yview
        )
        scrollbar.grid(row=0, column=1, sticky=tk.NS)
        self.log_text.configure(yscrollcommand=scrollbar.set)

    # ----- saved configuration ------------------------------------------------------------------

    def _load_settings(self) -> None:
        try:
            self.config = self.store.load()
        except LauncherError as exc:
            self.config = LauncherConfig.default()
            self._append_log(f"配置读取失败，已显示默认配置：{exc}", error=True)
            self.root.after(50, lambda error=exc: messagebox.showwarning(
                "配置文件无效", str(error), parent=self.root,
            ))
        self.client_delay_var.set(f"{self.config.client_delay_seconds:g}")
        self.client_count_var.set(str(self.config.client_count))
        if self.config.window_geometry:
            self.normal_window_geometry = self.config.window_geometry
            self.root.geometry(self.config.window_geometry)
        self._refresh_profile_choices()
        self._append_log(f"已加载 {len(self.config.profiles)} 份启动配置。")

    def _save_config(self, config: LauncherConfig) -> None:
        """Persist profiles and selection together with the current window state."""
        try:
            delay = float(self.client_delay_var.get())
            config = replace(config, client_delay_seconds=delay)
        except (ValueError, LauncherError):
            pass  # An unfinished delay edit must not block saving profiles.
        count = self._client_count()
        if count is not None:
            config = replace(config, client_count=count)
        config = replace(config, window_geometry=self._current_window_geometry())
        self.store.save(config)
        self.config = config

    def _commit_profiles(self, config: LauncherConfig) -> None:
        self._save_config(config)
        self._refresh_profile_choices()

    def _refresh_profile_choices(self) -> None:
        for role, controls in self.role_controls.items():
            active = self.config.selected(role)
            controls.name.set(active.name if active else "未激活")
            controls.summary.set(
                active.summary() if active
                else f"请在“配置管理”中勾选一份 {ROLE_TITLES[role]} 配置的“激活”。"
            )
        self._update_buttons()

    def _client_count(self) -> int | None:
        try:
            count = int(self.client_count_var.get())
            validate_client_count(count, True)
        except (ValueError, LauncherError):
            return None
        return count

    def _validated_client_count(self, auto_dev_account: bool) -> int:
        try:
            count = int(self.client_count_var.get())
        except ValueError:
            count = 0
        validate_client_count(count, auto_dev_account)
        return count

    def _update_launch_labels(self) -> None:
        count = self._client_count()
        clients = "Client" if count in (None, 1) else f"{count} 个 Client"
        self.role_controls[NetworkRole.CLIENT].start_button.configure(text=f"启动 {clients}")
        self.start_pair_button.configure(text=f"一键启动 Host + {clients}")

    def _open_profile_manager(self) -> None:
        if self.profile_manager is None:
            self.profile_manager = ProfileManagerWindow(
                self.root, lambda: self.config, self._commit_profiles, self._on_profile_manager_closed,
            )
        else:
            self.profile_manager.window.deiconify()
            self.profile_manager.window.lift()

    def _on_profile_manager_closed(self) -> None:
        self.profile_manager = None

    # ----- launching ----------------------------------------------------------------------------

    def _start_role(self, role: NetworkRole) -> None:
        if self.busy or self.closing:
            return
        profile = self.config.selected(role)
        if profile is None:
            return
        try:
            settings = profile.settings()
            count = 1
            if role is NetworkRole.CLIENT:
                count = self._validated_client_count(profile.auto_dev_account)
                self._save_config(replace(self.config, client_count=count))
        except LauncherError as exc:
            self._show_error("启动失败", exc)
            return
        if role is NetworkRole.HOST:
            self._run_async(
                "正在启动 Host…", lambda: self.controller.ensure_host(settings, profile.name),
                lambda result: self._on_host_started(*result),
            )
        else:
            self.launch_cancel.clear()
            self._run_async(
                f"正在启动 {count} 个 Client…",
                lambda: self.controller.start_clients(
                    settings, count, auto_dev_account=profile.auto_dev_account,
                    label=profile.name, cancel=self.launch_cancel,
                ),
                self._on_clients_started,
            )

    def _start_pair(self) -> None:
        if self.busy or self.closing:
            return
        host, client = self.config.selected(NetworkRole.HOST), self.config.selected(NetworkRole.CLIENT)
        if host is None or client is None:
            return
        mismatches = pair_mismatches(host, client)
        if mismatches:
            self._show_error("Client 与 Host 不匹配", LauncherError(
                f"Client 配置「{client.name}」无法连接 Host 配置「{host.name}」：\n- "
                + "\n- ".join(mismatches)
                + "\n\n请在“配置管理”中修改对应字段，或激活其他配置。"
            ))
            return
        try:
            count = self._validated_client_count(client.auto_dev_account)
            delay = float(self.client_delay_var.get())
            self._save_config(replace(self.config, client_delay_seconds=delay, client_count=count))
            host_settings, client_settings = host.settings(), client.settings()
        except ValueError:
            self._show_error("启动失败", LauncherError("Client 启动间隔必须是 0 到 60 之间的数字。"))
            return
        except LauncherError as exc:
            self._show_error("启动失败", exc)
            return
        self.launch_cancel.clear()
        self._run_async(
            f"正在启动 Host + {count} 个 Client…",
            lambda: self.controller.start_pair(
                host_settings, client_settings, delay_seconds=delay,
                auto_dev_account=client.auto_dev_account, client_count=count,
                cancel=self.launch_cancel,
                on_host_started=lambda snapshot, reused: self.events.put(("pair_host", (snapshot, reused))),
                host_label=host.name, client_label=client.name,
            ),
            self._on_pair_started,
        )

    def _on_host_started(self, snapshot: ControllerSnapshot, reused: bool) -> None:
        if reused:
            self.snapshots_by_id[snapshot.instance_id] = snapshot
            self._append_log(
                f"[{snapshot.display_name}] 复用已运行的相同配置 Host，PID {snapshot.pid}。"
            )
        else:
            self._on_instance_started(snapshot)
        self._render_instances()
        self.instance_tree.selection_set(str(snapshot.instance_id))

    def _on_client_started(self, result: object) -> None:
        if not isinstance(result, ControllerSnapshot):
            return
        self._on_instance_started(result)
        self._render_instances()
        self.instance_tree.selection_set(str(result.instance_id))

    def _on_clients_started(self, result: object) -> None:
        if not isinstance(result, ClientLaunchResult):
            return
        for client in result.clients:
            self._on_client_started(client)
        if result.error and not self.closing:
            self._show_error("部分 Client 未启动", LauncherError(
                f"已启动 {len(result.clients)} / {result.requested} 个 Client。\n{result.error}"
            ))

    def _on_pair_started(self, result: object) -> None:
        if not isinstance(result, PairLaunchResult):
            return
        for client in result.clients:
            self._on_client_started(client)
        if result.error and not self.closing:
            self._show_error("Client 未全部启动", LauncherError(
                f"已启动 {len(result.clients)} / {result.requested_clients} 个 Client。\n{result.error}"
                f"\n\nHost（{result.host.display_name}）仍由启动器管理，"
                "可单独重试“启动 Client”，或在实例列表中关闭 Host。"
            ))

    def _instance_command(self, instance_id: int | None) -> str | None:
        """The exact command line of a running instance, including its debug port."""
        snapshot = self.snapshots_by_id.get(instance_id) if instance_id is not None else None
        if snapshot is None or snapshot.settings is None:
            return None
        return subprocess.list2cmdline([snapshot.settings.executable, *snapshot.arguments])

    def _show_instance_command(self) -> None:
        instance_id = self._selected_instance_id()
        command = self._instance_command(instance_id)
        if instance_id is None or command is None:
            return
        snapshot = self.snapshots_by_id[instance_id]
        dialog = tk.Toplevel(self.root)
        dialog.title(f"启动命令 - {snapshot.display_name}")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = tk.Text(frame, width=100, height=5, wrap=tk.WORD, font=("Cascadia Mono", 9))
        text.insert("1.0", command)
        text.configure(state=tk.DISABLED)
        text.grid(row=0, column=0, columnspan=2, sticky=tk.NSEW)

        def copy() -> None:
            dialog.clipboard_clear()
            dialog.clipboard_append(command)
            self._append_log(f"[{snapshot.display_name}] 启动命令已复制。")

        ttk.Button(frame, text="复制", command=copy).grid(row=1, column=0, sticky=tk.E, pady=(10, 0))
        ttk.Button(frame, text="关闭", command=dialog.destroy).grid(
            row=1, column=1, sticky=tk.E, padx=(8, 0), pady=(10, 0),
        )

    def _stop(self) -> None:
        instance_id = self._selected_instance_id()
        if instance_id is None:
            return
        self._run_async(
            "正在关闭…",
            lambda: self._stop_instance(instance_id),
            self._on_instance_stopped,
        )

    def _restart(self) -> None:
        instance_id = self._selected_instance_id()
        if instance_id is None:
            return
        self._run_async(
            "正在重新启动…",
            lambda: self.controller.restart(instance_id),
            self._on_instance_restarted,
        )

    def _stop_all(self) -> None:
        if not self.snapshots_by_id:
            return
        self._run_async(
            "正在关闭全部…",
            self._stop_all_instances,
            self._on_all_stopped,
        )

    def _stop_instance(self, instance_id: int) -> ControllerSnapshot:
        return self.controller.stop(instance_id)

    def _stop_all_instances(self) -> tuple[ControllerSnapshot, ...]:
        return self.controller.stop_all()

    def _run_async(
        self,
        status_text: str,
        operation: Callable[[], object],
        on_success: Callable[[object], None],
    ) -> None:
        if self.busy or self.closing:
            return
        self.busy = True
        self.status_var.set(status_text)
        self.status_dot.configure(foreground="#d97706")
        self._update_buttons()

        def worker() -> None:
            try:
                result = operation()
            except Exception as exc:  # UI boundary: convert all worker failures to messages.
                self.events.put(("error", exc))
            else:
                self.events.put(("success", (result, on_success)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_instance_started(self, result: object) -> None:
        snapshot = result
        if not isinstance(snapshot, ControllerSnapshot):
            return
        self.snapshots_by_id[snapshot.instance_id] = snapshot
        if snapshot.debug_ports is not None:
            self._start_debug_service(snapshot.debug_ports)
        self._append_log(
            f"[{snapshot.display_name}] App 已启动，PID {snapshot.pid}。"
        )
        if snapshot.settings is not None and DEBUG_WAIT_ARGUMENT in snapshot.arguments:
            self._append_log(
                f"[{snapshot.display_name}] 已启用“等待 Lua 调试器”：App 会停在启动阶段，"
                f"直到 IDE 通过 Attach {self.attach_configuration.name} 连接后才继续运行。"
            )

    def _on_instance_stopped(self, result: object) -> None:
        snapshot = result
        if not isinstance(snapshot, ControllerSnapshot):
            return
        self.snapshots_by_id.pop(snapshot.instance_id, None)
        self._append_log(
            f"[实例 {snapshot.instance_id}] App 已关闭。"
            + (
                f"退出码 {snapshot.exit_code}。"
                if snapshot.exit_code is not None
                else ""
            )
        )

    def _on_instance_restarted(self, result: object) -> None:
        snapshot = result
        if not isinstance(snapshot, ControllerSnapshot):
            return
        self.snapshots_by_id[snapshot.instance_id] = snapshot
        if snapshot.debug_ports is not None:
            self._start_debug_service(snapshot.debug_ports)
        self._append_log(
            f"[实例 {snapshot.instance_id}] 已按原始配置重新启动，PID {snapshot.pid}。"
        )

    def _on_all_stopped(self, result: object) -> None:
        snapshots = result if isinstance(result, tuple) else ()
        for snapshot in snapshots:
            if isinstance(snapshot, ControllerSnapshot):
                self._append_log(f"[实例 {snapshot.instance_id}] App 已关闭。")
        self.snapshots_by_id.clear()

    def _initialize_default_debug_service(self) -> None:
        if self.closing:
            return
        preferred = DebugPortBundle(
            target_port=DEFAULT_SCRIPT_DEBUG_PORT,
            bridge_port=self.attach_configuration.bridge_port,
            dap_port=self.attach_configuration.dap_port,
        )
        try:
            ports = self.controller.reserve_debug_slot(
                preferred,
                allow_active_target=True,
            )
        except LauncherError as exc:
            self.default_debug_port = self.attach_configuration.dap_port
            self.debug_states[self.attach_configuration.dap_port] = "调试失败"
            self._append_log(f"默认 Attach 服务启动失败：{exc}", error=True)
            self._update_debug_status()
            return
        self.default_debug_port = ports.dap_port
        self._start_debug_service(ports)

    def _start_debug_service(self, ports: DebugPortBundle) -> None:
        if self.closing:
            return
        debug_port = ports.dap_port
        current = self.debug_services.get(debug_port)
        if current is not None and current.thread.is_alive():
            return

        stop_event = threading.Event()
        options = PersistentDebugOptions(
            dap_port=ports.dap_port,
            bridge_port=ports.bridge_port,
            target_port=ports.target_port,
        )
        if not options.adapter.is_file():
            if not self.debug_component_missing_reported:
                self.debug_component_missing_reported = True
                self._append_log(
                    f"Lua 调试组件不存在：{options.adapter}。"
                    "App 仍可正常启动和运行；如需调试，请恢复该组件。", error=True,
                )
            self.debug_states[debug_port] = "调试不可用"
            self._update_debug_status()
            return
        self.debug_states[debug_port] = "调试启动中"
        self.debug_attach_clients[debug_port] = "未连接"
        self.debug_runtime_clients[debug_port] = "未连接"
        self._update_debug_status()

        def logger(message: str) -> None:
            self.events.put(("debug_log", (debug_port, message)))

        def on_ready(pid: int | None) -> None:
            self.events.put(("debug_ready", (debug_port, pid)))

        def on_client_changed(client: str | None) -> None:
            self.events.put(("debug_attach", (debug_port, client)))

        def on_target_changed(client: str | None) -> None:
            self.events.put(("debug_target", (debug_port, client)))

        def worker() -> None:
            try:
                run_embedded_debug_server(
                    stop_event,
                    logger=logger,
                    on_ready=on_ready,
                    on_dap_client_changed=on_client_changed,
                    on_target_changed=on_target_changed,
                    options=options,
                )
            except Exception as exc:  # Service boundary: report in the GUI.
                self.events.put(("debug_error", (debug_port, exc)))
            else:
                self.events.put(("debug_stopped", debug_port))

        thread = threading.Thread(
            target=worker,
            name=f"persistent-debug-server-{debug_port}",
            daemon=True,
        )
        self.debug_services[debug_port] = _DebugService(stop_event, thread)
        thread.start()

    def _stop_debug_service(self, debug_port: int, timeout: float = 6.0) -> None:
        service = self.debug_services.get(debug_port)
        if service is None:
            return
        service.stop_event.set()
        service.thread.join(timeout)
        if service.thread.is_alive():
            raise LauncherError(
                f"DAP {debug_port} 的常驻调试服务未能在规定时间内停止。"
            )

    def _drain_events(self) -> None:
        if self.closing:
            return
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "pair_host":
                snapshot, reused = payload
                self._on_host_started(snapshot, reused)
                self.status_var.set("Host 已启动，正在准备 Client…")
                continue
            if kind == "debug_log":
                debug_port, message = payload  # type: ignore[misc]
                self._append_log(f"[DAP {debug_port}] [常驻调试] {message}")
                continue
            if kind == "debug_ready":
                debug_port, pid = payload  # type: ignore[misc]
                pid_text = f" / DAP PID {pid}" if isinstance(pid, int) else ""
                self.debug_states[debug_port] = f"调试运行中{pid_text}"
                self._render_instances()
                self._update_debug_status()
                continue
            if kind == "debug_error":
                debug_port, error = payload  # type: ignore[misc]
                self.debug_states[debug_port] = "调试失败"
                self._append_log(
                    f"[DAP {debug_port}] 常驻调试服务失败：{error}", error=True
                )
                self._render_instances()
                self._update_debug_status()
                continue
            if kind == "debug_attach":
                debug_port, client = payload  # type: ignore[misc]
                self.debug_attach_clients[debug_port] = (
                    str(client) if client else "未连接"
                )
                self._render_instances()
                self._update_debug_status()
                continue
            if kind == "debug_target":
                debug_port, client = payload  # type: ignore[misc]
                self.debug_runtime_clients[debug_port] = (
                    str(client) if client else "未连接"
                )
                self._render_instances()
                self._update_debug_status()
                continue
            if kind == "debug_stopped":
                debug_port = int(payload)
                self.debug_states[debug_port] = "调试已停止"
                self._render_instances()
                self._update_debug_status()
                continue
            self.busy = False
            if kind == "error":
                error = payload if isinstance(payload, Exception) else LauncherError(str(payload))
                self._show_error("操作失败", error)
            else:
                result, callback = payload  # type: ignore[misc]
                callback(result)
            self._refresh_status(force=True)
            self._update_buttons()
        self.root.after(100, self._drain_events)

    def _update_debug_status(self) -> None:
        debug_port = self.default_debug_port
        if debug_port is None:
            self._set_status_image(
                self.debug_attach_status_image,
                self.debug_status_images["waiting"],
            )
            self._set_status_image(
                self.debug_runtime_status_image,
                self.debug_status_images["disconnected"],
            )
            self.debug_attach_status_var.set(
                f"Attach {self.attach_configuration.name}：启动中…"
            )
            self.debug_runtime_status_var.set(
                f"{self.attach_configuration.session_name}：未连接"
            )
            self.debug_service_status_var.set("")
            return
        state = self.debug_states.get(debug_port, "调试启动中")
        attach_client = self.debug_attach_clients.get(debug_port, "未连接")
        runtime_client = self.debug_runtime_clients.get(debug_port, "未连接")
        attach_connected = attach_client != "未连接"
        runtime_connected = runtime_client != "未连接"
        service_failed = state in ("调试失败", "调试不可用")
        attach_image = (
            "connected"
            if attach_connected
            else "error" if service_failed else "disconnected"
        )
        runtime_image = (
            "connected"
            if runtime_connected
            else "error" if service_failed else "disconnected"
        )
        self._set_status_image(
            self.debug_attach_status_image,
            self.debug_status_images[attach_image],
        )
        self._set_status_image(
            self.debug_runtime_status_image,
            self.debug_status_images[runtime_image],
        )
        attach_state = (
            f"已连接（{attach_client}）" if attach_connected else "未连接"
        )
        runtime_state = (
            f"已连接（{runtime_client}）" if runtime_connected else "未连接"
        )
        self.debug_attach_status_var.set(
            f"Attach {self.attach_configuration.name}：{attach_state}"
        )
        self.debug_runtime_status_var.set(
            f"{self.attach_configuration.session_name}：{runtime_state}"
        )
        self.debug_service_status_var.set(f"DAP {debug_port}；服务 {state}")

    def _refresh_status(self, force: bool = False) -> None:
        if self.closing:
            return
        if force and self.status_after_id is not None:
            self.root.after_cancel(self.status_after_id)
            self.status_after_id = None
        elif not force:
            self.status_after_id = None
        if self.busy and not force:
            self.status_after_id = self.root.after(500, self._refresh_status)
            return
        try:
            snapshots = self.controller.snapshots()
        except LauncherError as exc:
            self.status_var.set("状态未知")
            self.status_dot.configure(foreground="#dc2626")
            self._append_log(f"状态检查失败：{exc}", error=True)
            self.status_after_id = self.root.after(1000, self._refresh_status)
            return

        current = {snapshot.instance_id: snapshot for snapshot in snapshots}
        finished_ids = self.snapshots_by_id.keys() - current.keys()
        for instance_id in finished_ids:
            previous = self.snapshots_by_id[instance_id]
            self._append_log(
                f"[实例 {instance_id}] App（PID {previous.pid}）已自然退出。"
            )
        self.snapshots_by_id = current
        if snapshots:
            self.status_dot.configure(foreground="#16a34a")
        else:
            self.status_dot.configure(foreground="#9aa0a6")
        self.status_var.set(f"运行实例：{len(snapshots)}")
        self._render_instances()
        self._update_buttons()
        self.status_after_id = self.root.after(500, self._refresh_status)

    def _update_buttons(self) -> None:
        selected = self._selected_instance_id() is not None
        idle = not self.busy and not self.closing
        for role, controls in self.role_controls.items():
            has_profile = self.config.selected(role) is not None
            controls.start_button.configure(state=tk.NORMAL if idle and has_profile else tk.DISABLED)
        both_selected = all(self.config.selected(role) is not None for role in LAUNCH_ROLES)
        self.start_pair_button.configure(state=tk.NORMAL if idle and both_selected else tk.DISABLED)
        instance_state = tk.NORMAL if idle and selected else tk.DISABLED
        self.stop_button.configure(state=instance_state)
        self.restart_button.configure(state=instance_state)
        self.command_button.configure(state=tk.NORMAL if selected else tk.DISABLED)
        self.stop_all_button.configure(
            state=tk.NORMAL if idle and self.snapshots_by_id else tk.DISABLED
        )

    def _selected_instance_id(self) -> int | None:
        selection = self.instance_tree.selection()
        return int(selection[0]) if selection else None

    def _render_instances(self) -> None:
        selected = self._selected_instance_id()
        for item in self.instance_tree.get_children():
            self.instance_tree.delete(item)
        role_labels = {
            NetworkRole.STANDALONE: "单机",
            NetworkRole.HOST: "Host",
            NetworkRole.CLIENT: "Client",
        }
        for instance_id, snapshot in self.snapshots_by_id.items():
            settings = snapshot.settings or LauncherSettings()
            network = settings.network
            role = network.role
            if role is NetworkRole.HOST:
                endpoint, room, account = f"监听 :{network.port}", network.room_id, network.dev_account or "—"
            elif role is NetworkRole.CLIENT:
                endpoint, room, account = f"{network.host}:{network.port}", network.room_id, network.dev_account or "—"
            else:
                endpoint, room, account = "—", "—", "—"
            ports = snapshot.debug_ports
            debug_port = ports.dap_port if ports else None
            attach_client = self.debug_attach_clients.get(debug_port, "未连接")
            runtime_client = self.debug_runtime_clients.get(debug_port, "未连接")
            attach_state = (
                f"已连接 {attach_client}" if attach_client != "未连接" else "未连接"
            )
            runtime_state = (
                f"已连接 {runtime_client}" if runtime_client != "未连接" else "未连接"
            )
            self.instance_tree.insert(
                "",
                tk.END,
                iid=str(instance_id),
                values=(
                    instance_id,
                    snapshot.label or "—",
                    role_labels[role],
                    snapshot.pid or "—",
                    endpoint,
                    room,
                    account,
                    "运行中",
                    ports.target_port if ports else "—",
                    ports.dap_port if ports else "—",
                    attach_state if ports else "—",
                    runtime_state if ports else "—",
                    self.debug_states.get(debug_port, "—") if ports else "—",
                ),
            )
        if selected is not None and selected in self.snapshots_by_id:
            self.instance_tree.selection_set(str(selected))

    def _remember_window_geometry(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is not self.root or self.root.state() != "normal":
            return
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        if width < 100 or height < 100:
            return
        geometry = (
            f"{width}x{height}"
            f"{self.root.winfo_x():+d}{self.root.winfo_y():+d}"
        )
        if is_valid_window_geometry(geometry):
            self.normal_window_geometry = geometry

    def _current_window_geometry(self) -> str:
        if self.normal_window_geometry:
            return self.normal_window_geometry
        self.root.update_idletasks()
        geometry = (
            f"{self.root.winfo_width()}x{self.root.winfo_height()}"
            f"{self.root.winfo_x():+d}{self.root.winfo_y():+d}"
        )
        return geometry if is_valid_window_geometry(geometry) else ""

    def _append_log(self, message: str, error: bool = False) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = "ERROR" if error else "INFO "
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{timestamp}] [{prefix}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _show_error(self, title: str, error: Exception) -> None:
        self._append_log(str(error), error=True)
        messagebox.showerror(title, str(error), parent=self.root)

    def _on_close(self) -> None:
        if self.closing:
            return
        if self.profile_manager is not None and not self.profile_manager.close():
            return
        self.closing = True
        self.launch_cancel.set()
        for callback_id in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(callback_id)
        close_errors: list[str] = []
        try:
            self._save_config(self.config)
        except LauncherError as exc:
            close_errors.append(str(exc))

        try:
            self.controller.shutdown()
        except LauncherError as exc:
            close_errors.append(str(exc))
        for instance_id in tuple(self.debug_services):
            try:
                self._stop_debug_service(instance_id)
            except LauncherError as exc:
                close_errors.append(str(exc))
        if close_errors:
            self._append_log("关闭时发生错误：" + "；".join(close_errors), error=True)
        self.root.destroy()


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes = __import__("ctypes")
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def main() -> None:
    _enable_dpi_awareness()
    root = tk.Tk()
    DebugLauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
