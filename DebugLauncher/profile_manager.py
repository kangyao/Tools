from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

from launcher_core import (
    ConfigurationError,
    DEFAULT_NETWORK_HOST,
    LauncherError,
    NetworkRole,
    format_argument_text,
    format_command_preview,
    parse_argument_text,
)
from launch_profiles import (
    LAUNCH_ROLES,
    ROLE_TITLES,
    LaunchProfile,
    LauncherConfig,
    new_profile_id,
    profile_from_command,
)


ROLE_BY_TITLE = {title: role for role, title in ROLE_TITLES.items()}
DEBUG_WAIT_HINT = (
    "勾选后 App 启动时会停住，直到 IDE 通过 Attach Lua 连接调试器后才继续运行。"
)


@dataclass
class _FormVars:
    name: tk.StringVar
    role: tk.StringVar
    executable: tk.StringVar
    host: tk.StringVar
    port: tk.StringVar
    room_id: tk.StringVar
    dev_account: tk.StringVar
    auto_dev_account: tk.BooleanVar
    extra_arguments: tk.StringVar
    show_console: tk.BooleanVar
    debug_wait: tk.BooleanVar

    @classmethod
    def create(cls, master: tk.Misc) -> _FormVars:
        return cls(
            tk.StringVar(master), tk.StringVar(master, value="Host"), tk.StringVar(master),
            tk.StringVar(master), tk.StringVar(master), tk.StringVar(master), tk.StringVar(master),
            tk.BooleanVar(master), tk.StringVar(master), tk.BooleanVar(master), tk.BooleanVar(master),
        )

    def all(self) -> tuple[tk.Variable, ...]:
        return tuple(vars(self).values())


class ProfileManagerWindow:
    """Edit saved launch profiles as drafts; only an explicit save commits them."""

    def __init__(
        self,
        master: tk.Misc,
        get_config: Callable[[], LauncherConfig],
        commit: Callable[[LauncherConfig], None],
        on_closed: Callable[[], None],
    ) -> None:
        self.get_config = get_config
        self.commit = commit
        self.on_closed = on_closed
        self.current_id: str | None = None
        self.previous_id: str | None = None
        self.baseline: tuple[object, ...] = ()
        self.loading = False
        self.list_ids: list[str] = []

        self.window = tk.Toplevel(master)
        self.window.title("配置管理")
        self.window.geometry("900x560")
        self.window.minsize(820, 520)
        self.window.transient(master)  # type: ignore[arg-type]
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.form = _FormVars.create(self.window)
        # 激活状态属于启动选择而非配置内容：已保存的配置勾选后立即生效，不计入草稿修改。
        self.active_var = tk.BooleanVar(self.window)
        self.preview_var = tk.StringVar(self.window)
        # 用户正在编辑命令时，表单变化不回写命令框，避免打断输入。
        self.preview_editing = False
        self.syncing_preview = False
        self.validation_var = tk.StringVar(self.window)
        self.draft_state_var = tk.StringVar(self.window)
        self._build_ui()
        for variable in self.form.all():
            variable.trace_add("write", lambda *_: self._on_form_changed())
        self.active_var.trace_add("write", lambda *_: self._on_active_toggled())
        self.preview_var.trace_add("write", lambda *_: self._on_command_edited())

        config = self.get_config()
        first = next(iter(self._ordered_profiles(config)), None)
        if first is not None:
            self._load_profile(first)
        else:
            self._load_draft(LaunchProfile(name="新 Host 配置", role=NetworkRole.HOST))

    # ----- layout -------------------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.window, padding=(16, 14, 16, 12))
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        left = ttk.Frame(outer)
        left.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 16))
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="已保存的配置").grid(row=0, column=0, sticky=tk.W, pady=(0, 6))
        self.profile_list = tk.Listbox(left, width=28, exportselection=False, activestyle="none")
        self.profile_list.grid(row=1, column=0, sticky=tk.NSEW)
        self.profile_list.bind("<<ListboxSelect>>", lambda _event: self._on_list_selected())
        list_buttons = ttk.Frame(left)
        list_buttons.grid(row=2, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Button(list_buttons, text="新建", width=6, command=self.new_profile).pack(side=tk.LEFT)
        self.copy_button = ttk.Button(list_buttons, text="复制", width=6, command=self.copy_profile)
        self.copy_button.pack(side=tk.LEFT, padx=6)
        ttk.Button(list_buttons, text="删除", width=6, command=self.delete_profile).pack(side=tk.LEFT)

        form = ttk.Frame(outer)
        form.grid(row=0, column=1, sticky=tk.NSEW)
        form.columnconfigure(3, weight=1)
        ttk.Label(form, textvariable=self.draft_state_var, foreground="#b45309").grid(
            row=0, column=0, columnspan=5, sticky=tk.W, pady=(0, 4),
        )
        f = self.form
        self._label(form, "配置名称", 1)
        ttk.Entry(form, textvariable=f.name).grid(row=1, column=1, columnspan=4, sticky=tk.EW, pady=3)
        self._label(form, "配置类型", 2)
        ttk.Combobox(
            form, textvariable=f.role, values=tuple(ROLE_TITLES[role] for role in LAUNCH_ROLES),
            state="readonly", width=12,
        ).grid(row=2, column=1, sticky=tk.W, pady=3)
        ttk.Checkbutton(
            form, text="激活（在启动器中使用，每种类型只激活一份）", variable=self.active_var,
        ).grid(row=2, column=2, columnspan=3, sticky=tk.W, padx=(16, 0), pady=3)
        self._label(form, "程序路径", 3)
        ttk.Entry(form, textvariable=f.executable).grid(row=3, column=1, columnspan=3, sticky=tk.EW, pady=3)
        ttk.Button(form, text="浏览", width=6, command=self._browse_executable).grid(
            row=3, column=4, padx=(6, 0), pady=3,
        )

        self.client_only: list[tk.Widget] = []
        host_label = self._label(form, "主机地址", 4)
        host_entry = ttk.Entry(form, textvariable=f.host)
        host_entry.grid(row=4, column=1, columnspan=4, sticky=tk.EW, pady=3)
        self.client_only += [host_label, host_entry]
        self._label(form, "端口", 5)
        ttk.Entry(form, textvariable=f.port, width=10).grid(row=5, column=1, sticky=tk.W, pady=3)
        ttk.Label(form, text="房间").grid(row=5, column=2, sticky=tk.W, padx=(16, 6))
        ttk.Entry(form, textvariable=f.room_id, width=10).grid(row=5, column=3, sticky=tk.W, pady=3)
        # 开发账号序号：AIGamePlay/.dev/account.json 的第 N 项，生成 -MGFDevAccount N；Host 与 Client 都需要登录。
        self._label(form, "开发账号序号", 6)
        ttk.Entry(form, textvariable=f.dev_account, width=10).grid(row=6, column=1, sticky=tk.W, pady=3)
        auto_dev_account = ttk.Checkbutton(
            form, text="多开时自动分配不同账号（本机每个进程须用不同账号）", variable=f.auto_dev_account,
        )
        auto_dev_account.grid(row=7, column=1, columnspan=4, sticky=tk.W, pady=3)
        self.client_only += [auto_dev_account]

        self._label(form, "额外参数", 8)
        ttk.Entry(form, textvariable=f.extra_arguments).grid(row=8, column=1, columnspan=4, sticky=tk.EW, pady=3)
        ttk.Label(form, text="可填写其他 App 参数，含空格的值使用双引号。", foreground="#6b7280").grid(
            row=9, column=1, columnspan=4, sticky=tk.W,
        )
        ttk.Checkbutton(form, text="显示控制台", variable=f.show_console).grid(
            row=10, column=1, columnspan=4, sticky=tk.W, pady=(8, 2),
        )
        ttk.Checkbutton(form, text="等待 Lua 调试器", variable=f.debug_wait).grid(
            row=11, column=1, columnspan=4, sticky=tk.W, pady=2,
        )
        ttk.Label(form, text=DEBUG_WAIT_HINT, foreground="#6b7280", wraplength=520).grid(
            row=12, column=1, columnspan=4, sticky=tk.W,
        )

        self._label(form, "启动命令", 13).grid_configure(pady=(12, 3))
        self.command_entry = ttk.Entry(form, textvariable=self.preview_var)
        self.command_entry.grid(row=13, column=1, columnspan=3, sticky=tk.EW, pady=(12, 3))
        self.command_entry.bind("<Return>", lambda _event: self._finish_command_edit())
        self.command_entry.bind("<FocusOut>", lambda _event: self._finish_command_edit())
        self.command_entry.bind("<Escape>", lambda _event: self._cancel_command_edit())
        ttk.Button(form, text="复制", width=6, command=self._copy_preview).grid(
            row=13, column=4, padx=(6, 0), pady=(12, 3),
        )
        ttk.Label(
            form, foreground="#6b7280", wraplength=560,
            text="可直接编辑，能解析的修改立即同步到表单；Esc 放弃无效的命令文字。\n"
                 "名称、控制台等不在命令中的设置不变；Lua 调试端口在启动时分配。",
        ).grid(row=14, column=1, columnspan=4, sticky=tk.W)
        ttk.Label(form, textvariable=self.validation_var, foreground="#b91c1c", wraplength=560).grid(
            row=15, column=0, columnspan=5, sticky=tk.W, pady=(8, 0),
        )

        actions = ttk.Frame(outer)
        actions.grid(row=1, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))
        self.save_button = ttk.Button(actions, text="保存", width=8, command=self.save)
        self.save_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="另存为", width=8, command=self.save_as).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="取消", width=8, command=self.cancel).pack(side=tk.LEFT)

    @staticmethod
    def _label(parent: ttk.Frame, text: str, row: int) -> ttk.Label:
        label = ttk.Label(parent, text=text)
        label.grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        return label

    # ----- form state ---------------------------------------------------------------------------

    @staticmethod
    def _ordered_profiles(config: LauncherConfig) -> tuple[LaunchProfile, ...]:
        return tuple(profile for role in LAUNCH_ROLES for profile in config.profiles_for(role))

    def _form_values(self) -> tuple[object, ...]:
        return tuple(variable.get() for variable in self.form.all())

    @property
    def is_dirty(self) -> bool:
        return self.current_id is None or self._form_values() != self.baseline

    def _fill_form(self, profile: LaunchProfile, active: bool) -> None:
        self.preview_editing = False
        self.loading = True
        try:
            self.active_var.set(active)
            f = self.form
            f.name.set(profile.name)
            f.role.set(ROLE_TITLES[profile.role])
            f.executable.set(profile.executable)
            f.host.set(profile.host)
            f.port.set(str(profile.port))
            f.room_id.set(str(profile.room_id))
            f.dev_account.set(str(profile.dev_account))
            f.auto_dev_account.set(profile.auto_dev_account)
            f.extra_arguments.set(format_argument_text(profile.extra_arguments))
            f.show_console.set(profile.show_console)
            f.debug_wait.set(profile.debug_wait)
        finally:
            self.loading = False
        self.baseline = self._form_values()
        self._on_form_changed()

    def _load_profile(self, profile: LaunchProfile) -> None:
        self.current_id = profile.id
        self.previous_id = profile.id
        self._fill_form(profile, self.get_config().is_active(profile))
        self._refresh_list()

    def _load_draft(self, draft: LaunchProfile) -> None:
        self.current_id = None
        # 该类型还没有激活的配置时，新配置默认激活。
        self._fill_form(draft, self.get_config().selected_id(draft.role) is None)
        self._refresh_list()

    def _refresh_list(self) -> None:
        config = self.get_config()
        self.list_ids = []
        self.profile_list.delete(0, tk.END)
        for profile in self._ordered_profiles(config):
            self.list_ids.append(profile.id)
            mark = "✓" if config.is_active(profile) else "　"
            self.profile_list.insert(tk.END, f"{mark} [{ROLE_TITLES[profile.role]}] {profile.name}")
        self.profile_list.selection_clear(0, tk.END)
        if self.current_id in self.list_ids:
            index = self.list_ids.index(self.current_id)
            self.profile_list.selection_set(index)
            self.profile_list.see(index)
        self.copy_button.configure(state=tk.NORMAL if self.current_id else tk.DISABLED)
        self._update_draft_state()

    def _update_draft_state(self) -> None:
        if self.current_id is None:
            self.draft_state_var.set("新配置，尚未保存")
        elif self.is_dirty:
            self.draft_state_var.set("有未保存的修改")
        else:
            self.draft_state_var.set("")

    def _on_form_changed(self) -> None:
        if self.loading:
            return
        is_client = ROLE_BY_TITLE.get(self.form.role.get()) is NetworkRole.CLIENT
        for widget in self.client_only:
            if is_client:
                widget.grid()
            else:
                widget.grid_remove()
        try:
            profile = self.collect()
        except LauncherError as exc:
            self._show_command("")
            self.validation_var.set(str(exc))
        else:
            self._show_command(format_command_preview(profile.settings()))
            self.validation_var.set("")
        self._update_draft_state()

    def _show_command(self, command: str) -> None:
        if self.preview_editing:
            return
        self.syncing_preview = True
        try:
            self.preview_var.set(command)
        finally:
            self.syncing_preview = False

    def _on_command_edited(self) -> None:
        """Apply a hand-edited command to the form as soon as it parses."""
        if self.syncing_preview or self.loading:
            return
        self.preview_editing = True
        try:
            base = self.collect()
        except LauncherError:
            base = LaunchProfile(
                name=self.form.name.get().strip() or "新配置",
                role=ROLE_BY_TITLE.get(self.form.role.get(), NetworkRole.HOST),
                show_console=bool(self.form.show_console.get()),
                auto_dev_account=bool(self.form.auto_dev_account.get()),
            )
        try:
            profile = profile_from_command(self.preview_var.get(), base)
        except LauncherError as exc:
            self.validation_var.set(f"启动命令无法解析：{exc}")
            return
        f = self.form
        f.role.set(ROLE_TITLES[profile.role])
        f.executable.set(profile.executable)
        if profile.role is NetworkRole.CLIENT:
            f.host.set(profile.host)
        f.port.set(str(profile.port))
        f.room_id.set(str(profile.room_id))
        f.dev_account.set(str(profile.dev_account))
        f.extra_arguments.set(format_argument_text(profile.extra_arguments))
        f.debug_wait.set(profile.debug_wait)

    def _finish_command_edit(self) -> None:
        """Leave command editing; a valid command is rewritten in canonical form."""
        if not self.preview_editing:
            return
        self.preview_editing = False
        if not self.validation_var.get().startswith("启动命令无法解析"):
            self._on_form_changed()

    def _cancel_command_edit(self) -> None:
        self.preview_editing = False
        self._on_form_changed()

    def _on_active_toggled(self) -> None:
        if self.loading:
            return
        desired = bool(self.active_var.get())
        if self.current_id is None:
            return  # 新配置在保存时按勾选状态激活。
        if not self.resolve_unsaved():
            self._set_active_var(not desired)
            return
        config = self.get_config()
        profile = config.profile(self.current_id)
        if profile is None:
            return
        if desired:
            config = config.with_selection(profile.role, profile.id)
        elif config.is_active(profile):
            config = config.with_selection(profile.role, None)
        if not self._commit(config):
            self._set_active_var(not desired)
            return
        self._load_profile(profile)

    def _set_active_var(self, value: bool) -> None:
        self.loading = True
        try:
            self.active_var.set(value)
        finally:
            self.loading = False

    def collect(self) -> LaunchProfile:
        f = self.form
        role = ROLE_BY_TITLE.get(f.role.get())
        if role is None:
            raise ConfigurationError("请选择配置类型。")

        def integer(variable: tk.StringVar, label: str) -> int:
            try:
                return int(variable.get().strip())
            except ValueError as exc:
                raise ConfigurationError(f"{label}必须是整数。") from exc

        is_client = role is NetworkRole.CLIENT
        # Host 不使用地址和自动分配开关，隐藏字段中的内容不参与校验。
        return LaunchProfile(
            id=self.current_id or new_profile_id(),
            name=f.name.get(), role=role, executable=f.executable.get(),
            host=f.host.get() if is_client else DEFAULT_NETWORK_HOST,
            port=integer(f.port, "端口"), room_id=integer(f.room_id, "房间号"),
            dev_account=integer(f.dev_account, "开发账号序号"),
            auto_dev_account=bool(f.auto_dev_account.get()) if is_client else True,
            extra_arguments=parse_argument_text(f.extra_arguments.get()),
            show_console=bool(f.show_console.get()), debug_wait=bool(f.debug_wait.get()),
        )

    # ----- actions ------------------------------------------------------------------------------

    def resolve_unsaved(self) -> bool:
        """Ask to save, discard or keep editing; False means keep editing."""
        if not self.is_dirty:
            return True
        name = self.form.name.get().strip() or "新配置"
        self.window.deiconify()
        self.window.lift()
        answer = messagebox.askyesnocancel(
            "未保存的修改",
            f"配置「{name}」有未保存的修改。\n\n是：保存修改\n否：放弃修改\n取消：继续编辑",
            parent=self.window,
        )
        if answer is None:
            return False
        return self.save() if answer else True

    def select_profile(self, profile_id: str) -> None:
        if profile_id == self.current_id or not self.resolve_unsaved():
            self._refresh_list()
            return
        profile = self.get_config().profile(profile_id)
        if profile is not None:
            self._load_profile(profile)

    def _on_list_selected(self) -> None:
        selection = self.profile_list.curselection()
        if selection:
            self.select_profile(self.list_ids[selection[0]])

    def new_profile(self) -> None:
        if not self.resolve_unsaved():
            return
        role = ROLE_BY_TITLE.get(self.form.role.get(), NetworkRole.HOST)
        name = self.get_config().unique_name(f"新 {ROLE_TITLES[role]} 配置")
        self._load_draft(LaunchProfile(name=name, role=role))

    def copy_profile(self) -> None:
        if self.current_id is None or not self.resolve_unsaved():
            return
        source = self.get_config().profile(self.current_id)
        if source is None:
            return
        name = self.get_config().unique_name(f"{source.name} 副本")
        self._load_draft(replace(source, id=new_profile_id(), name=name))

    def delete_profile(self) -> None:
        config = self.get_config()
        if self.current_id is None:
            self._discard_draft()
            return
        profile = config.profile(self.current_id)
        if profile is None:
            return
        if not messagebox.askyesno(
            "删除配置",
            f"删除配置「{profile.name}」？\n\n若该配置已激活，启动器中对应的一端会变为未激活；已启动的实例不受影响。",
            parent=self.window,
        ):
            return
        if not self._commit(config.remove_profile(profile.id)):
            return
        remaining = self._ordered_profiles(self.get_config())
        if remaining:
            self._load_profile(remaining[0])
        else:
            self._load_draft(LaunchProfile(name="新 Host 配置", role=NetworkRole.HOST))

    def save(self) -> bool:
        is_new, active = self.current_id is None, bool(self.active_var.get())
        try:
            profile = self.collect()
            config = self.get_config().save_profile(profile)
            if is_new and active:
                config = config.with_selection(profile.role, profile.id)
            elif is_new and config.is_active(profile):
                config = config.with_selection(profile.role, None)
        except LauncherError as exc:
            self._show_error("保存失败", exc)
            return False
        if not self._commit(config):
            return False
        self._load_profile(profile)
        return True

    def save_as(self) -> None:
        try:
            draft = self.collect()
        except LauncherError as exc:
            self._show_error("另存为失败", exc)
            return
        config = self.get_config()
        name = simpledialog.askstring(
            "另存为", "新配置名称：", parent=self.window,
            initialvalue=config.unique_name(f"{draft.name} 副本"),
        )
        if name is None:
            return
        try:
            profile = replace(draft, id=new_profile_id(), name=name)
            config = config.save_profile(profile)
        except LauncherError as exc:
            self._show_error("另存为失败", exc)
            return
        if self._commit(config):
            self._load_profile(profile)

    def cancel(self) -> None:
        if self.current_id is None:
            self._discard_draft()
            return
        profile = self.get_config().profile(self.current_id)
        if profile is not None:
            self._load_profile(profile)

    def close(self) -> bool:
        if not self.resolve_unsaved():
            return False
        self.window.destroy()
        self.on_closed()
        return True

    def _discard_draft(self) -> None:
        config = self.get_config()
        profile = config.profile(self.previous_id) or next(iter(self._ordered_profiles(config)), None)
        if profile is not None:
            self._load_profile(profile)
        else:
            self._load_draft(LaunchProfile(name="新 Host 配置", role=NetworkRole.HOST))

    def _commit(self, config: LauncherConfig) -> bool:
        try:
            self.commit(config)
        except LauncherError as exc:
            self._show_error("保存失败", exc)
            return False
        return True

    def _browse_executable(self) -> None:
        current = Path(self.form.executable.get().strip() or r"C:\MiniGame\Bin64")
        selected = filedialog.askopenfilename(
            parent=self.window, title="选择可执行文件",
            initialdir=str(current.parent if current.suffix else current),
            filetypes=(("可执行文件", "*.exe"), ("所有文件", "*.*")),
        )
        if selected:
            self.form.executable.set(selected)

    def _copy_preview(self) -> None:
        command = self.preview_var.get()
        if not command:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(command)

    def _show_error(self, title: str, error: Exception) -> None:
        self.validation_var.set(str(error))
        messagebox.showerror(title, str(error), parent=self.window)
