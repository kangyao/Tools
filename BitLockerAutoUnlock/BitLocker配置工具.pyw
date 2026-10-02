# -*- coding: utf-8 -*-
"""
BitLocker 开机自动解锁 —— 图形化配置工具

功能：
    * 配置盘符 + 解锁密码 / 48 位恢复密码（密码用 Windows DPAPI 按"本机"加密保存，不明文）
    * 查看、显示、删除已配置的盘符
    * 扫描本机 BitLocker 卷（需要管理员权限）
    * 安装 / 卸载开机自动解锁的计划任务、立即测试解锁、查看运行日志

双击本文件即可运行（.pyw，不弹黑窗）；也可以通过"配置界面.bat"启动。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

# ---------------------------------------------------------------- 基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
AUTOUNLOCK_PS1 = os.path.join(BASE_DIR, "AutoUnlock.ps1")
INSTALL_PS1 = os.path.join(BASE_DIR, "Install.ps1")
LOG_PATH = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                        "BitLockerAutoUnlock", "unlock.log")
POWERSHELL = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                          r"System32\WindowsPowerShell\v1.0\powershell.exe")
TASK_NAME = "BitLockerAutoUnlock"
CREATE_NO_WINDOW = 0x08000000


# --------------------------------------------------- DPAPI 加密（本机范围）
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB)]
_crypt32.CryptProtectData.restype = wt.BOOL
_crypt32.CryptUnprotectData.argtypes = _crypt32.CryptProtectData.argtypes
_crypt32.CryptUnprotectData.restype = wt.BOOL
_kernel32.LocalFree.argtypes = [wt.HLOCAL]
_kernel32.LocalFree.restype = wt.HLOCAL

CRYPTPROTECT_LOCAL_MACHINE = 0x04


def _make_blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    return blob


def dpapi_protect(data: bytes) -> bytes:
    """按"本机"范围加密，与 PowerShell 的 ProtectedData(LocalMachine) 完全兼容。"""
    src = _make_blob(data)
    out = DATA_BLOB()
    if not _crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None,
                                     CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        if out.pbData:
            _kernel32.LocalFree(out.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    src = _make_blob(data)
    out = DATA_BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0,
                                       ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        if out.pbData:
            _kernel32.LocalFree(out.pbData)


# ------------------------------------------------------------ 配置文件读写
def load_config(path):
    if not os.path.exists(path):
        return {"WaitTimeoutSeconds": 90, "Drives": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("配置文件格式不正确")
    except Exception as exc:
        messagebox.showwarning("配置文件有问题",
                               "读取失败：%s\n将按空配置继续，保存时会覆盖原文件。" % exc)
        return {"WaitTimeoutSeconds": 90, "Drives": []}
    if not isinstance(cfg.get("Drives"), list):
        cfg["Drives"] = []
    return cfg


def save_config(path, cfg):
    cfg["Drives"] = sorted(cfg["Drives"], key=lambda d: str(d.get("DriveLetter", "")).upper())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def entry_password(entry):
    """取出盘符条目中保存的密码明文；取不到返回 None。"""
    if entry.get("SecurePassword"):
        scope = str(entry.get("Scope", "Machine"))
        if scope != "Machine":
            return None
        try:
            return dpapi_unprotect(base64.b64decode(entry["SecurePassword"])).decode("utf-8")
        except Exception:
            return None
    if entry.get("Password"):
        return str(entry["Password"])
    return None


def run_ps(args, timeout=60):
    """运行 PowerShell 并捕获输出（不弹窗）。"""
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"] + args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=CREATE_NO_WINDOW)


def run_as_admin(ps_file, args=""):
    """用管理员权限运行 PowerShell 脚本（弹出 UAC）。"""
    params = '-NoProfile -ExecutionPolicy Bypass -File "%s" %s' % (ps_file, args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", POWERSHELL, params, None, 1)
    return rc > 32


# ------------------------------------------------------------------ 主界面
class App(tk.Tk):

    def __init__(self):
        tk.Tk.__init__(self)
        self.title("BitLocker 开机自动解锁 配置工具")
        self.geometry("760x640")
        self.minsize(700, 560)
        self.cfg = load_config(CONFIG_PATH)
        self._build_ui()
        self.refresh_table()
        self.log("配置文件：%s" % CONFIG_PATH)
        self.log("提示：密码使用 Windows DPAPI 按本机加密保存，只有这台电脑能解开。")

    # ------------------------------------------------------------ 界面搭建
    def _build_ui(self):
        self.option_add("*Font", "微软雅黑 10")
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        pad = {"padx": 8, "pady": 6}
        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # 标题
        ttk.Label(main, text="BitLocker 开机自动解锁 配置",
                  font=("微软雅黑", 13, "bold")).pack(anchor=tk.W, **pad)

        # ---- 已配置列表
        box = ttk.LabelFrame(main, text="已配置的盘符", padding=8)
        box.pack(fill=tk.BOTH, expand=True, **pad)

        cols = ("drive", "method", "store")
        self.table = ttk.Treeview(box, columns=cols, show="headings", height=6)
        self.table.heading("drive", text="盘符")
        self.table.heading("method", text="解锁方式")
        self.table.heading("store", text="密码存储")
        self.table.column("drive", width=80, anchor=tk.CENTER)
        self.table.column("method", width=140, anchor=tk.CENTER)
        self.table.column("store", width=420, anchor=tk.W)
        self.table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.table.bind("<<TreeviewSelect>>", self.on_select)

        sb = ttk.Scrollbar(box, orient=tk.VERTICAL, command=self.table.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.table.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(main)
        btns.pack(fill=tk.X, padx=8)
        ttk.Button(btns, text="显示密码", command=self.show_password).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="删除选中", command=self.delete_selected).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="刷新列表", command=self.refresh_table).pack(side=tk.LEFT, padx=3)

        # ---- 编辑区
        edit = ttk.LabelFrame(main, text="添加 / 修改盘符", padding=8)
        edit.pack(fill=tk.X, **pad)

        ttk.Label(edit, text="盘符：").grid(row=0, column=0, sticky=tk.W)
        self.drive_var = tk.StringVar(value="W")
        self.drive_combo = ttk.Combobox(edit, textvariable=self.drive_var, width=6,
                                        values=["%s:" % c for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"])
        self.drive_combo.grid(row=0, column=1, sticky=tk.W)
        ttk.Button(edit, text="扫描 BitLocker 卷", command=self.scan_volumes).grid(
            row=0, column=2, padx=10, sticky=tk.W)

        ttk.Label(edit, text="方式：").grid(row=0, column=3, sticky=tk.W, padx=(20, 0))
        self.method_var = tk.StringVar(value="Password")
        ttk.Radiobutton(edit, text="解锁密码", value="Password",
                        variable=self.method_var).grid(row=0, column=4, sticky=tk.W)
        ttk.Radiobutton(edit, text="恢复密码(48位)", value="RecoveryPassword",
                        variable=self.method_var).grid(row=0, column=5, sticky=tk.W)

        ttk.Label(edit, text="密钥 / 密码：").grid(row=1, column=0, sticky=tk.W, pady=6)
        self.pwd_var = tk.StringVar()
        self.pwd_entry = ttk.Entry(edit, textvariable=self.pwd_var, width=52, show="*")
        self.pwd_entry.grid(row=1, column=1, columnspan=4, sticky=tk.W, pady=6)
        self.pwd_entry.bind("<Return>", lambda e: self.add_or_update())

        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(edit, text="显示", variable=self.show_var,
                        command=self.toggle_show).grid(row=1, column=5, sticky=tk.W)

        ttk.Button(edit, text="添加 / 更新", command=self.add_or_update).grid(
            row=2, column=1, sticky=tk.W, pady=4)
        ttk.Label(edit, text="恢复密码形如：123456-123456-123456-123456-123456-123456-123456-123456",
                  foreground="#777777").grid(row=2, column=2, columnspan=4, sticky=tk.W)

        # ---- 其它设置
        opt = ttk.LabelFrame(main, text="其它设置", padding=8)
        opt.pack(fill=tk.X, **pad)
        ttk.Label(opt, text="开机后等待盘符出现的秒数：").pack(side=tk.LEFT)
        self.timeout_var = tk.IntVar(value=int(self.cfg.get("WaitTimeoutSeconds", 90) or 90))
        ttk.Spinbox(opt, from_=0, to=600, width=6, textvariable=self.timeout_var).pack(side=tk.LEFT)
        ttk.Button(opt, text="保存设置", command=self.save_settings).pack(side=tk.LEFT, padx=10)
        ttk.Button(opt, text="打开配置文件", command=self.open_config).pack(side=tk.LEFT, padx=4)

        # ---- 计划任务
        task = ttk.LabelFrame(main, text="开机自动执行（计划任务）", padding=8)
        task.pack(fill=tk.X, **pad)
        ttk.Button(task, text="安装开机任务", command=self.install_task).pack(side=tk.LEFT, padx=3)
        ttk.Button(task, text="卸载任务", command=self.uninstall_task).pack(side=tk.LEFT, padx=3)
        ttk.Button(task, text="立即测试解锁", command=self.test_unlock).pack(side=tk.LEFT, padx=3)
        ttk.Button(task, text="查看运行日志", command=self.show_log).pack(side=tk.LEFT, padx=3)
        self.task_state_var = tk.StringVar(value="")
        ttk.Label(task, textvariable=self.task_state_var, foreground="#0066cc").pack(side=tk.LEFT, padx=12)
        self.after(300, self.update_task_state)

        # ---- 状态 / 日志
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(main, textvariable=self.status_var, foreground="#006600").pack(anchor=tk.W, padx=8)

        logbox = ttk.Frame(main)
        logbox.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        self.log_text = tk.Text(logbox, height=10, wrap=tk.WORD, relief=tk.SUNKEN)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ls = ttk.Scrollbar(logbox, command=self.log_text.yview)
        ls.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=ls.set, state=tk.DISABLED)

    # ------------------------------------------------------------ 小工具
    def log(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def status(self, msg):
        self.status_var.set(msg)

    def toggle_show(self):
        self.pwd_entry.configure(show="" if self.show_var.get() else "*")

    def norm_letter(self, value):
        letter = "".join(ch for ch in str(value) if ch.isalpha()).upper()
        return letter[:1]

    # ------------------------------------------------------------ 列表操作
    def refresh_table(self):
        for item in self.table.get_children():
            self.table.delete(item)
        for entry in self.cfg["Drives"]:
            letter = self.norm_letter(entry.get("DriveLetter", ""))
            method = "恢复密码" if str(entry.get("Method", "Password")) == "RecoveryPassword" else "解锁密码"
            if entry.get("SecurePassword"):
                store = "已加密保存（DPAPI·本机，Scope=%s）" % entry.get("Scope", "Machine")
            elif entry.get("Password"):
                store = "明文保存（不安全）"
            else:
                store = "未设置密码"
            self.table.insert("", tk.END, values=(letter + ":", method, store), iid=letter)

    def on_select(self, event=None):
        sel = self.table.selection()
        if not sel:
            return
        letter = sel[0]
        entry = self.find_entry(letter)
        if not entry:
            return
        self.drive_var.set(letter)
        self.method_var.set(str(entry.get("Method", "Password")))
        self.status("已选中 %s 盘，可直接改密码后点『添加 / 更新』，或点『显示密码』。" % letter)

    def find_entry(self, letter):
        for entry in self.cfg["Drives"]:
            if self.norm_letter(entry.get("DriveLetter", "")) == letter:
                return entry
        return None

    # ------------------------------------------------------------ 增删改
    def add_or_update(self):
        letter = self.norm_letter(self.drive_var.get())
        pwd = self.pwd_var.get().strip()
        if not letter:
            messagebox.showwarning("缺少盘符", "请先选择或输入盘符，例如 W 或 W:")
            return
        if not pwd:
            messagebox.showwarning("缺少密码", "请输入该盘的解锁密码或恢复密钥。")
            return
        if self.method_var.get() == "RecoveryPassword":
            compact = pwd.replace(" ", "")
            if len(compact.replace("-", "")) != 48 or not compact.replace("-", "").isdigit():
                if not messagebox.askyesno("恢复密码看起来不对",
                                           "恢复密码通常是 48 位数字（8 组 6 位）。\n"
                                           "当前输入不像标准恢复密码，仍要保存吗？"):
                    return

        try:
            blob = base64.b64encode(dpapi_protect(pwd.encode("utf-8"))).decode("ascii")
        except Exception as exc:
            messagebox.showerror("加密失败", "无法用 DPAPI 加密密码：%s" % exc)
            return

        entry = self.find_entry(letter)
        if entry:
            entry.update({"DriveLetter": letter, "Method": self.method_var.get(),
                          "Scope": "Machine", "SecurePassword": blob})
            entry.pop("Password", None)
        else:
            self.cfg["Drives"].append({"DriveLetter": letter, "Method": self.method_var.get(),
                                       "Scope": "Machine", "SecurePassword": blob})
        save_config(CONFIG_PATH, self.cfg)
        self.refresh_table()
        self.pwd_var.set("")
        self.status("已保存 %s 盘的配置（密码已加密写入 config.json）。" % letter)
        self.log("保存成功：%s 盘，方式=%s" % (letter, self.method_var.get()))

    def delete_selected(self):
        sel = self.table.selection()
        if not sel:
            messagebox.showinfo("未选择", "请先在列表中选择要删除的盘符。")
            return
        letter = sel[0]
        if not messagebox.askyesno("确认删除", "确定删除 %s 盘的配置吗？" % letter):
            return
        self.cfg["Drives"] = [e for e in self.cfg["Drives"]
                              if self.norm_letter(e.get("DriveLetter", "")) != letter]
        save_config(CONFIG_PATH, self.cfg)
        self.refresh_table()
        self.status("已删除 %s 盘。" % letter)

    def show_password(self):
        sel = self.table.selection()
        if not sel:
            messagebox.showinfo("未选择", "请先在列表中选择一个盘符。")
            return
        entry = self.find_entry(sel[0])
        if not entry:
            return
        pwd = entry_password(entry)
        if pwd is None:
            messagebox.showinfo("无法读取",
                                "该条目的密码不是本机 DPAPI 加密格式（可能是明文或由其他用户加密），"
                                "请重新输入并点『添加 / 更新』。")
            return
        self.pwd_var.set(pwd)
        self.show_var.set(True)
        self.toggle_show()
        self.status("已取出 %s 盘的密码，请妥善保管屏幕。" % sel[0])

    def save_settings(self):
        try:
            self.cfg["WaitTimeoutSeconds"] = int(self.timeout_var.get())
        except Exception:
            messagebox.showwarning("无效数值", "等待秒数必须是整数。")
            return
        save_config(CONFIG_PATH, self.cfg)
        self.status("设置已保存。")

    def open_config(self):
        try:
            os.startfile(CONFIG_PATH)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    # ------------------------------------------------------------ 卷扫描
    def scan_volumes(self):
        self.status("正在扫描 BitLocker 卷...")
        self.update_idletasks()
        try:
            res = run_ps(["-Command",
                          "Get-BitLockerVolume -ErrorAction Stop | "
                          "Select-Object MountPoint,VolumeStatus,LockStatus,ProtectionStatus | "
                          "ConvertTo-Json"], timeout=60)
        except subprocess.TimeoutExpired:
            self.log("扫描超时。")
            return
        if res.returncode != 0 or not res.stdout.strip():
            self.log("扫描失败（需要管理员权限）：\n" + res.stdout.strip())
            self.status("扫描失败，请手动输入盘符，或以管理员身份运行本工具。")
            return
        try:
            data = json.loads(res.stdout)
            if isinstance(data, dict):
                data = [data]
        except Exception:
            self.log("无法解析扫描结果：\n" + res.stdout.strip())
            return

        letters = []
        for vol in data:
            mp = str(vol.get("MountPoint", "")).strip()
            letter = self.norm_letter(mp)
            if letter:
                letters.append(letter + ":")
            self.log("发现卷 %s  状态=%s  锁定=%s  保护=%s" %
                     (mp, vol.get("VolumeStatus"), vol.get("LockStatus"),
                      vol.get("ProtectionStatus")))
        if letters:
            self.drive_combo.configure(values=letters)
            locked = [l for l in letters]
            self.drive_var.set(locked[0])
            self.status("扫描完成，共 %d 个卷，已自动选中 %s。" % (len(letters), locked[0]))
        else:
            self.status("扫描完成，但没有找到带盘符的卷。")

    # ------------------------------------------------------------ 计划任务
    def update_task_state(self):
        try:
            res = run_ps(["-Command",
                          "(Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue).State" % TASK_NAME],
                         timeout=30)
            state = res.stdout.strip()
        except Exception:
            state = ""
        if state:
            self.task_state_var.set("计划任务状态：%s" % state)
        else:
            self.task_state_var.set("计划任务：未安装")

    def install_task(self):
        if not os.path.exists(INSTALL_PS1):
            messagebox.showerror("缺少脚本", "找不到 %s" % INSTALL_PS1)
            return
        if not self.cfg["Drives"]:
            if not messagebox.askyesno("尚未配置盘符",
                                       "config.json 里还没有任何盘符，确定仍要安装开机任务吗？"):
                return
        self.log("正在请求管理员权限安装计划任务（请在 UAC 窗口点『是』）...")
        if run_as_admin(INSTALL_PS1):
            self.status("安装命令已提交，请在弹出的窗口查看结果。")
            self.after(4000, self.update_task_state)
        else:
            self.log("提权被取消或失败。")

    def uninstall_task(self):
        if not os.path.exists(INSTALL_PS1):
            messagebox.showerror("缺少脚本", "找不到 %s" % INSTALL_PS1)
            return
        if not messagebox.askyesno("确认卸载", "确定移除开机自动解锁的计划任务吗？"):
            return
        if run_as_admin(INSTALL_PS1, "-Uninstall"):
            self.status("卸载命令已提交。")
            self.after(3000, self.update_task_state)
        else:
            self.log("提权被取消或失败。")

    def test_unlock(self):
        if not os.path.exists(AUTOUNLOCK_PS1):
            messagebox.showerror("缺少脚本", "找不到 %s" % AUTOUNLOCK_PS1)
            return
        self.log("正在以管理员身份立即执行一次解锁...")
        if run_as_admin(AUTOUNLOCK_PS1):
            self.status("已触发一次解锁，稍后查看日志。")
            self.after(9000, self.show_log)
        else:
            self.log("提权被取消或失败。")

    def show_log(self):
        if not os.path.exists(LOG_PATH):
            self.log("日志文件还不存在：%s" % LOG_PATH)
            return
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-40:]
        except Exception as exc:
            self.log("读取日志失败：%s" % exc)
            return
        self.log("---- %s 尾部日志 ----" % LOG_PATH)
        for line in lines:
            self.log(line.rstrip("\n"))


def main():
    if os.name != "nt":
        messagebox.showerror("不支持", "本工具只能在 Windows 上运行。")
        return
    try:
        App().mainloop()
    except Exception:
        with open(os.path.join(BASE_DIR, "gui_error.txt"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        messagebox.showerror("程序出错", "发生异常，详情已写入 gui_error.txt\n\n" + traceback.format_exc())


if __name__ == "__main__":
    main()
