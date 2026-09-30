from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from launcher_core import (
    ConfigStore,
    ConfigurationError,
    ControllerSnapshot,
    DEBUG_WAIT_ARGUMENT,
    DEFAULT_SCRIPT_DEBUG_PORT,
    DebugPortBundle,
    LauncherController,
    LauncherError,
    LauncherSettings,
    NetworkRole,
    apply_network_preset,
    format_argument_text,
    format_command_preview,
    is_valid_window_geometry,
    load_attach_configuration,
    parse_argument_text,
)
from persistent_debug_server import PersistentDebugOptions, run_embedded_debug_server


TOOL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_DIR / "settings.local.json"
RUN_CONFIGURATION_PATH = TOOL_DIR.parents[1] / ".run" / "Lua.run.xml"


@dataclass
class _DebugService:
    stop_event: threading.Event
    thread: threading.Thread


class DebugLauncherApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.store = ConfigStore(CONFIG_PATH)
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
        self.default_debug_port: int | None = None
        self.status_after_id: str | None = None
        self.normal_window_geometry = ""

        self.executable_var = tk.StringVar()
        self.argument_text_var = tk.StringVar()
        self.debug_wait_var = tk.BooleanVar(value=True)
        self.show_console_var = tk.BooleanVar(value=True)
        self.preview_var = tk.StringVar()
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
        self._refresh_preview()
        self._refresh_status()
        self._drain_events()
        self.root.after_idle(self._initialize_default_debug_service)

    def _configure_window(self) -> None:
        self.root.title("App 调试启动器")
        self.root.geometry("1100x850")
        self.root.minsize(900, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._remember_window_geometry, add="+")

        style = ttk.Style(self.root)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(22, 18, 22, 16))
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        config_frame = ttk.Frame(outer, padding=14)
        config_frame.grid(row=0, column=0, sticky=tk.EW)
        config_frame.columnconfigure(1, weight=1)

        ttk.Label(config_frame, text="程序").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 10), pady=(0, 10)
        )
        executable_entry = ttk.Entry(
            config_frame, textvariable=self.executable_var
        )
        executable_entry.grid(row=0, column=1, sticky=tk.EW, pady=(0, 10))
        ttk.Button(config_frame, text="浏览…", command=self._browse_executable).grid(
            row=0, column=2, padx=(8, 0), pady=(0, 10)
        )

        argument_frame = ttk.LabelFrame(
            config_frame,
            text="App 启动参数",
            padding=(10, 10),
        )
        argument_frame.grid(
            row=1,
            column=0,
            columnspan=3,
            sticky=tk.EW,
            pady=(0, 8),
        )
        argument_frame.columnconfigure(0, weight=1)
        self.arguments_entry = ttk.Entry(
            argument_frame, textvariable=self.argument_text_var,
        )
        self.arguments_entry.grid(row=0, column=0, sticky=tk.EW)
        argument_scrollbar = ttk.Scrollbar(
            argument_frame, orient=tk.HORIZONTAL, command=self.arguments_entry.xview,
        )
        argument_scrollbar.grid(row=1, column=0, sticky=tk.EW)
        self.arguments_entry.configure(xscrollcommand=argument_scrollbar.set)
        presets = ttk.Frame(argument_frame)
        presets.grid(row=2, column=0, sticky=tk.W, pady=(8, 0))
        for label, role in (
            ("填入 Host 参数", NetworkRole.HOST),
            ("填入 Client 参数", NetworkRole.CLIENT),
            ("清除网络参数", NetworkRole.STANDALONE),
        ):
            ttk.Button(
                presets, text=label,
                command=lambda selected=role: self._apply_network_preset(selected),
            ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(
            argument_frame,
            text='直接输入参数（不含 exe）；含空格的值请加双引号。预设默认端口 7000、房间 1。',
        ).grid(row=3, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Label(
            argument_frame,
            text="Client 默认地址 127.0.0.1、UIN 10001；多开时每个 Client 使用不同 UIN，且不能为 1。",
        ).grid(row=4, column=0, sticky=tk.W, pady=(4, 0))
        ttk.Checkbutton(
            config_frame,
            text=f"等待 Lua 调试器连接（{DEBUG_WAIT_ARGUMENT}）",
            variable=self.debug_wait_var,
            command=self._refresh_preview,
        ).grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))

        ttk.Label(config_frame, text="命令预览").grid(
            row=3, column=0, sticky=tk.W, padx=(0, 10)
        )
        preview_frame = ttk.Frame(config_frame)
        preview_frame.grid(row=3, column=1, columnspan=2, sticky=tk.EW)
        preview_frame.columnconfigure(0, weight=1)
        self.preview_entry = ttk.Entry(
            preview_frame,
            textvariable=self.preview_var,
            state="readonly",
        )
        self.preview_entry.grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(
            preview_frame,
            text="复制",
            command=self._copy_preview,
            width=8,
        ).grid(row=0, column=1, padx=(8, 0))

        options = ttk.Frame(config_frame)
        options.grid(row=4, column=1, columnspan=2, sticky=tk.W, pady=(10, 0))
        ttk.Checkbutton(
            options,
            text="显示 App 控制台窗口",
            variable=self.show_console_var,
        ).pack(side=tk.LEFT)
        ttk.Button(options, text="保存配置", command=self._save_only).pack(
            side=tk.LEFT, padx=(16, 0)
        )

        instance_frame = ttk.LabelFrame(
            outer, text="运行实例", style="Section.TLabelframe", padding=(10, 8)
        )
        instance_frame.grid(row=1, column=0, sticky=tk.EW, pady=(12, 0))
        instance_frame.columnconfigure(0, weight=1)
        columns = (
            "id",
            "role",
            "pid",
            "endpoint",
            "target",
            "dap",
            "attach",
            "runtime",
            "status",
        )
        self.instance_tree = ttk.Treeview(
            instance_frame, columns=columns, show="headings", height=5, selectmode="browse"
        )
        headings = {
            "id": ("ID", 55),
            "role": ("角色", 80),
            "pid": ("PID", 80),
            "endpoint": ("网络端点", 210),
            "target": ("ScriptDebugger", 120),
            "dap": ("DAP", 80),
            "attach": (f"Attach {self.attach_configuration.name}", 170),
            "runtime": (self.attach_configuration.session_name, 170),
            "status": ("状态", 110),
        }
        for column, (text, width) in headings.items():
            self.instance_tree.heading(column, text=text)
            self.instance_tree.column(column, width=width, anchor=tk.CENTER)
        self.instance_tree.grid(row=0, column=0, sticky=tk.EW)
        self.instance_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_buttons())

        action_frame = ttk.Frame(outer, padding=(0, 12, 0, 12))
        action_frame.grid(row=2, column=0, sticky=tk.EW)
        action_frame.columnconfigure(4, weight=1)

        self.start_button = ttk.Button(
            action_frame,
            text="启动新实例",
            style="Primary.TButton",
            command=self._start,
            width=12,
        )
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = ttk.Button(
            action_frame, text="关闭", command=self._stop, width=12
        )
        self.stop_button.grid(row=0, column=1, padx=8)
        self.restart_button = ttk.Button(
            action_frame, text="重新启动", command=self._restart, width=12
        )
        self.restart_button.grid(row=0, column=2, padx=8)
        self.stop_all_button = ttk.Button(
            action_frame, text="关闭全部", command=self._stop_all, width=12
        )
        self.stop_all_button.grid(row=0, column=3, padx=8)

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

        self.executable_var.trace_add("write", lambda *_: self._refresh_preview())
        self.argument_text_var.trace_add("write", lambda *_: self._refresh_preview())

    def _load_settings(self) -> None:
        try:
            settings = self.store.load()
        except ConfigurationError as exc:
            settings = LauncherSettings()
            self._append_log(f"配置读取失败，已显示默认值：{exc}", error=True)
            self.root.after(
                50,
                lambda error=exc: messagebox.showwarning(
                    "配置文件无效",
                    f"{error}\n\n当前显示默认配置；点击“保存配置”可重新生成本地配置。",
                    parent=self.root,
                ),
            )
        self.executable_var.set(settings.executable)
        self.debug_wait_var.set(DEBUG_WAIT_ARGUMENT in settings.enabled_arguments)
        self.argument_text_var.set(format_argument_text(tuple(
            argument for argument in settings.enabled_arguments
            if argument != DEBUG_WAIT_ARGUMENT
        )))
        self.show_console_var.set(settings.show_console)
        if settings.window_geometry:
            self.normal_window_geometry = settings.window_geometry
            self.root.geometry(settings.window_geometry)
        self._append_log("启动器已就绪。")

    def _collect_settings(self) -> LauncherSettings:
        executable = os.path.expandvars(self.executable_var.get().strip())
        if not executable:
            raise LauncherError("请选择 App 可执行文件。")
        arguments = parse_argument_text(self.argument_text_var.get())
        if self.debug_wait_var.get() and DEBUG_WAIT_ARGUMENT not in arguments:
            arguments += (DEBUG_WAIT_ARGUMENT,)
        return LauncherSettings(
            executable=executable,
            arguments=arguments,
            show_console=bool(self.show_console_var.get()),
            window_geometry=self._current_window_geometry(),
        )

    def _save_settings(self) -> LauncherSettings:
        settings = self._collect_settings()
        self.store.save(settings)
        return settings

    def _save_only(self) -> None:
        try:
            settings = self._save_settings()
        except LauncherError as exc:
            self._show_error("保存配置失败", exc)
            return
        self._append_log(f"已保存配置：{format_command_preview(settings)}")

    def _copy_preview(self) -> None:
        command = self.preview_var.get()
        if not command:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(command)
            self.root.update_idletasks()
        except tk.TclError as exc:
            self._show_error("复制命令失败", exc)
            return
        self._append_log("命令预览已复制到剪贴板。")

    def _start(self) -> None:
        try:
            settings = self._save_settings()
        except LauncherError as exc:
            self._show_error("启动失败", exc)
            return
        self._run_async(
            "正在启动…",
            lambda: self.controller.start(settings),
            self._on_instance_started,
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
            f"[实例 {snapshot.instance_id}] App 已启动，PID {snapshot.pid}。"
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
        service_failed = state == "调试失败"
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
        self.start_button.configure(state=tk.DISABLED if self.busy else tk.NORMAL)
        self.stop_button.configure(
            state=tk.NORMAL if not self.busy and selected else tk.DISABLED
        )
        self.restart_button.configure(
            state=tk.NORMAL if not self.busy and selected else tk.DISABLED
        )
        self.stop_all_button.configure(
            state=tk.NORMAL if not self.busy and self.snapshots_by_id else tk.DISABLED
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
            NetworkRole.HOST: "主机",
            NetworkRole.CLIENT: "客机",
        }
        for instance_id, snapshot in self.snapshots_by_id.items():
            settings = snapshot.settings or LauncherSettings()
            network = settings.network
            role = network.role
            if role is NetworkRole.HOST:
                endpoint = f"监听 :{network.port} / 房间 {network.room_id}"
            elif role is NetworkRole.CLIENT:
                endpoint = (
                    f"{network.host}:{network.port} / 房间 {network.room_id}"
                    f" / UIN {network.uin}"
                )
            else:
                endpoint = "—"
            ports = snapshot.debug_ports
            debug_port = ports.dap_port if ports else None
            attach_client = self.debug_attach_clients.get(
                debug_port, "未连接"
            )
            runtime_client = self.debug_runtime_clients.get(
                debug_port, "未连接"
            )
            attach_state = (
                f"已连接 {attach_client}"
                if attach_client != "未连接"
                else "未连接"
            )
            runtime_state = (
                f"已连接 {runtime_client}"
                if runtime_client != "未连接"
                else "未连接"
            )
            self.instance_tree.insert(
                "",
                tk.END,
                iid=str(instance_id),
                values=(
                    instance_id,
                    role_labels[role],
                    snapshot.pid or "—",
                    endpoint,
                    ports.target_port if ports else "—",
                    ports.dap_port if ports else "—",
                    attach_state if ports else "—",
                    runtime_state if ports else "—",
                    self.debug_states.get(debug_port, "运行中"),
                ),
            )
        if selected is not None and selected in self.snapshots_by_id:
            self.instance_tree.selection_set(str(selected))

    def _apply_network_preset(self, role: NetworkRole) -> None:
        try:
            arguments = parse_argument_text(self.argument_text_var.get())
            self.argument_text_var.set(format_argument_text(
                apply_network_preset(arguments, role)
            ))
        except LauncherError as exc:
            self._show_error("填入参数失败", exc)
            return
        self.arguments_entry.xview_moveto(0)
        self.arguments_entry.focus_set()

    def _refresh_preview(self) -> None:
        try:
            settings = self._collect_settings()
        except LauncherError as exc:
            self.preview_var.set(f"参数无效：{exc}")
            return
        self.preview_var.set(format_command_preview(settings))

    def _browse_executable(self) -> None:
        current = Path(self.executable_var.get().strip() or r"C:\MiniGame\Bin64")
        initial = current.parent if current.suffix else current
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择 App 可执行文件",
            initialdir=str(initial),
            filetypes=(("可执行文件", "*.exe"), ("所有文件", "*.*")),
        )
        if selected:
            self.executable_var.set(selected)

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
        self.closing = True
        close_errors: list[str] = []
        try:
            self.store.save_window_geometry(self._current_window_geometry())
        except ConfigurationError as exc:
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
