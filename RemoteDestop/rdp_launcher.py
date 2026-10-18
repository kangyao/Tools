# -*- coding: utf-8 -*-
"""
远程桌面(mstsc)启动器
- Tkinter 图形界面管理多台主机配置
- 支持命令行参数(新增/删除/列出/直接连接/导入导出)
- 配置以 JSON 持久化保存

用法示例:
  python rdp_launcher.py                          # 打开图形界面
  python rdp_launcher.py --list                   # 列出所有主机
  python rdp_launcher.py --connect 办公机          # 直接连接
  python rdp_launcher.py --add 办公机 --host 192.168.1.10 --user admin --fullscreen
  python rdp_launcher.py --add 服务器 --host 10.0.0.5 --port 3389 --admin --width 1600 --height 900
  python rdp_launcher.py --add 办公机 --host 192.168.1.10 --user admin --password 123456
  python rdp_launcher.py --connect 办公机          # 自动带上已保存的密码
  python rdp_launcher.py --remove 办公机
  python rdp_launcher.py --scan                   # 扫描系统里已有的远程桌面记录
  python rdp_launcher.py --import-system          # 把系统记录导入配置
  python rdp_launcher.py --export backup.json
  python rdp_launcher.py --import backup.json --config hosts.json
  python rdp_launcher.py --make-bat 办公机            # 生成单台主机的一键启动 bat
  python rdp_launcher.py --make-bat-all --out D:\\bat # 生成所有主机的菜单 bat
"""

import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys
import time

try:
    import winreg
except ImportError:  # 非 Windows 平台
    winreg = None

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

FIELDS = ('name', 'host', 'port', 'user', 'password', 'save_password',
          'fullscreen', 'width', 'height', 'admin', 'prompt', 'note')

DEFAULT_ENTRY = {
    'name': '',
    'host': '',
    'port': '',
    'user': '',
    'password': '',      # DPAPI 加密后的密文，形如 dpapi:base64
    'save_password': False,
    'fullscreen': False,
    'width': '',
    'height': '',
    'admin': False,
    'prompt': False,  # 默认不弹窗询问凭据，直接连接
    'note': '',
}

DPAPI_PREFIX = 'dpapi:'
CRYPTPROTECT_UI_FORBIDDEN = 0x01

SCRIPT_PATH = os.path.abspath(__file__)


# --------------------------------------------------------------------------- #
# 配置存取
# --------------------------------------------------------------------------- #
def default_config_path():
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'RdpLauncher', 'hosts.json')


def load_hosts(path):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
    except (ValueError, OSError):
        return []
    hosts = []
    for item in data if isinstance(data, list) else []:
        entry = dict(DEFAULT_ENTRY)
        entry.update({k: v for k, v in item.items() if k in FIELDS})
        hosts.append(entry)
    return hosts


def save_hosts(path, hosts):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fp:
        json.dump(hosts, fp, ensure_ascii=False, indent=2)


def find_host(hosts, name):
    for entry in hosts:
        if entry['name'] == name:
            return entry
    return None


def upsert_host(hosts, entry):
    old = find_host(hosts, entry['name'])
    if old is not None:
        old.update(entry)
    else:
        hosts.append(entry)
    return hosts


def target_of(entry):
    host = (entry.get('host') or '').strip()
    port = str(entry.get('port') or '').strip()
    return '%s:%s' % (host, port) if port else host


# --------------------------------------------------------------------------- #
# 从当前系统读取已有的远程桌面配置
# --------------------------------------------------------------------------- #
def _decode(raw):
    for encoding in ('utf-8', 'mbcs', 'latin-1'):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode('utf-8', 'replace')


def system_mru_hosts():
    """最近连接过的主机：HKCU\\...\\Terminal Server Client\\Default 的 MRU*"""
    if winreg is None:
        return []
    path = r'Software\Microsoft\Terminal Server Client\Default'
    hosts = []
    key = None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        index = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, index)
            except OSError:
                break
            if name.upper().startswith('MRU') and str(value).strip():
                hosts.append(str(value).strip())
            index += 1
    except OSError:
        pass
    finally:
        if key is not None:
            winreg.CloseKey(key)
    return hosts


def system_username_hints():
    """每台主机记住的用户名：HKCU\\...\\Servers\\<host> 的 UsernameHint"""
    if winreg is None:
        return {}
    path = r'Software\Microsoft\Terminal Server Client\Servers'
    hints = {}
    key = None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        index = 0
        while True:
            try:
                host = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            sub = None
            try:
                sub = winreg.OpenKey(key, host)
                hint, _ = winreg.QueryValueEx(sub, 'UsernameHint')
            except OSError:
                hint = None
            finally:
                if sub is not None:
                    winreg.CloseKey(sub)
            if hint:
                hints[host] = str(hint).strip()
    except OSError:
        pass
    finally:
        if key is not None:
            winreg.CloseKey(key)
    return hints


def system_saved_credentials():
    """凭据管理器里已保存的 TERMSRV/<host> 凭据 -> {host: user}"""
    try:
        raw = subprocess.run(['cmdkey', '/list'], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, check=False).stdout
    except (OSError, AttributeError):
        return {}
    result = {}
    current = None
    for line in _decode(raw).splitlines():
        line = line.strip()
        if 'target=' in line.lower():
            match = re.search(r'target\s*=\s*TERMSRV/(.+)$', line, re.I)
            current = match.group(1).strip() if match else None
            if current:
                result.setdefault(current, '')
            continue
        match = re.match(r'(?:user|用户)\s*:\s*(.+)$', line, re.I)
        if match and current:
            result[current] = match.group(1).strip()
    return result


def system_rdp_template():
    """解析 文档\\Default.rdp，取全屏/分辨率/用户名作为默认模板"""
    docs = os.path.join(os.path.expanduser('~'), 'Documents')
    path = os.path.join(docs, 'Default.rdp')
    if not os.path.isfile(path):
        return {}
    template = {}
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as fp:
            text = fp.read()
    except OSError:
        return template
    for line in text.splitlines():
        match = re.match(r'([^:]+):[sib]:\w*:(.+)$', line.strip()) or \
            re.match(r'([^:]+):[i]:(\d+)$', line.strip())
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        if key == 'full screen' and value == '1':
            template['fullscreen'] = True
        elif key == 'desktopwidth':
            template['width'] = value
        elif key == 'desktopheight':
            template['height'] = value
        elif key == 'username' and value:
            template['user'] = value
    return template


def rdp_file_address(path):
    """读取 .rdp 里的 full address:s:host[:port]；取不到说明只是设置模板，不是主机"""
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as fp:
            text = fp.read()
    except OSError:
        return ''
    match = re.search(r'^full address:s:(.+)$', text, re.I | re.M)
    return match.group(1).strip() if match else ''


def scan_system_hosts():
    """汇总系统里的远程桌面记录：MRU 顺序 + 用户名 + 已保存凭据 + rdp 模板"""
    template = system_rdp_template()
    hints = system_username_hints()
    creds = system_saved_credentials()

    ordered = []
    seen = set()

    def push(host):
        host = (host or '').strip()
        if not host or host.lower() in seen:
            return
        seen.add(host.lower())
        ordered.append(host)

    for host in system_mru_hosts():
        push(host)
    for host in list(hints.keys()) + list(creds.keys()):
        push(host)
    for path in sorted(glob.glob(os.path.join(os.path.expanduser('~'),
                                              'Documents', '*.rdp'))):
        address = rdp_file_address(path)
        if address:
            push(address)

    entries = []
    for host in ordered:
        entry = dict(DEFAULT_ENTRY)
        entry['name'] = host
        entry['host'] = host
        entry.update({k: v for k, v in template.items() if v})
        user = hints.get(host) or creds.get(host)
        if user:
            entry['user'] = user
        sources = []
        if host in hints:
            sources.append('历史记录')
        if host in creds:
            sources.append('已保存凭据')
            entry['prompt'] = False  # 凭据已存在，直接登录不弹窗
        entry['note'] = '系统导入' + ('(' + '+'.join(sources) + ')' if sources else '')
        entries.append(entry)
    return entries


def merge_entries(hosts, scanned):
    """把扫描结果并入现有配置：新增未知主机，已有主机只补空字段"""
    added = updated = 0
    for item in scanned:
        target = (item.get('host') or '').strip().lower()
        match = None
        for entry in hosts:
            if (entry.get('host') or '').strip().lower() == target or \
                    entry.get('name') == item.get('name'):
                match = entry
                break
        if match is None:
            hosts.append(dict(item))
            added += 1
            continue
        changed = False
        for key in ('user', 'note', 'width', 'height'):
            if not match.get(key) and item.get(key):
                match[key] = item[key]
                changed = True
        if item.get('prompt') is False and match.get('prompt'):
            match['prompt'] = False
            changed = True
        if changed:
            updated += 1
    return added, updated


# --------------------------------------------------------------------------- #
# 密码保存：使用 Windows DPAPI(CryptProtectData) 加密，仅当前用户/本机可解密
# --------------------------------------------------------------------------- #
def _dpapi_crypt(data, protect=True):
    """调用 CryptProtectData / CryptUnprotectData，失败返回 None"""
    if not data:
        return b''
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD),
                        ('pbData', ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

        blob_in = DATA_BLOB()
        blob_out = DATA_BLOB()
        buffer = ctypes.create_string_buffer(data, len(data))
        blob_in.cbData = len(data)
        blob_in.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))

        func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = func(ctypes.byref(blob_in), None, None, None, None,
                  CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def protect_password(plain):
    """明文 -> dpapi:base64；加密不可用时返回 None"""
    plain = (plain or '').strip()
    if not plain:
        return ''
    blob = _dpapi_crypt(plain.encode('utf-8'), True)
    if blob is None:
        return None
    return DPAPI_PREFIX + base64.b64encode(blob).decode('ascii')


def unprotect_password(stored):
    """dpapi:base64 -> 明文；非 dpapi 前缀按明文原样返回"""
    stored = (stored or '').strip()
    if not stored:
        return ''
    if not stored.startswith(DPAPI_PREFIX):
        return stored
    try:
        raw = base64.b64decode(stored[len(DPAPI_PREFIX):])
    except (ValueError, TypeError):
        return ''
    data = _dpapi_crypt(raw, False)
    if data is None:
        return ''
    return data.decode('utf-8', 'replace')


def resolve_password(entry):
    """取出配置里保存的密码，没有则返回 None"""
    plain = unprotect_password(entry.get('password'))
    return plain or None


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
def build_mstsc_args(entry):
    """根据配置生成 mstsc 命令行参数"""
    args = ['/v:' + target_of(entry)]
    if entry.get('fullscreen'):
        args.append('/f')
    else:
        width = str(entry.get('width') or '').strip()
        height = str(entry.get('height') or '').strip()
        if width and height:
            args.append('/w:' + width)
            args.append('/h:' + height)
    if entry.get('admin'):
        args.append('/admin')
    if entry.get('prompt'):
        args.append('/prompt')
    return args


def store_credential(entry, password):
    """用 cmdkey 预存凭据，使 mstsc 免交互登录"""
    host = (entry.get('host') or '').strip()
    user = (entry.get('user') or '').strip()
    if not (host and user and password):
        return False
    try:
        subprocess.run(
            ['cmdkey', '/generic:TERMSRV/' + host, '/user:' + user, '/pass:' + password],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def connect(entry, password=None):
    """启动 mstsc 连接指定主机"""
    if not (entry.get('host') or '').strip():
        raise ValueError('主机地址为空')
    entry = dict(entry)
    plain = password or resolve_password(entry)
    if plain:
        if store_credential(entry, plain):
            entry['prompt'] = False  # 已预存凭据，不再弹窗
    args = build_mstsc_args(entry)
    try:
        subprocess.Popen(['mstsc'] + args, shell=False)
    except OSError as exc:
        raise RuntimeError('启动 mstsc 失败: %s' % exc)
    return args


# --------------------------------------------------------------------------- #
# 生成一键启动 bat
# --------------------------------------------------------------------------- #
def desktop_dir():
    path = os.path.join(os.path.expanduser('~'), 'Desktop')
    return path if os.path.isdir(path) else os.path.expanduser('~')


def safe_filename(name):
    text = re.sub(r'[\\/:*?"<>|\r\n]+', '_', (name or '').strip())
    return text or 'remote'


def write_bat(path, text):
    """bat 一律写 GBK + CRLF，配合 bat 里的 chcp 936 才不会中文乱码"""
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, 'w', encoding='gbk', errors='replace', newline='\r\n') as fp:
        fp.write(text)
    return path


def _bat_connect_block(entry, label_prefix, index):
    """生成一段：优先用 python 启动器(可用已保存密码)，否则直接 mstsc"""
    name = entry['name'].replace('"', '')
    launch = 'python "%%LAUNCHER%%" --connect "%s" --config "%%CONFIG%%"' % name
    direct = 'start "" mstsc %s' % ' '.join(build_mstsc_args(entry))
    return [
        ':%s%s' % (label_prefix, index),
        'where python >nul 2>nul',
        'if errorlevel 1 goto D%s' % index,
        'if exist "%%LAUNCHER%%" (%s & goto :eof)' % launch,
        ':D%s' % index,
        direct,
        'goto :eof',
        '',
    ]


def generate_bat(entry, config_path, script_path):
    """单台主机的一键启动 bat"""
    name = entry['name'].replace('"', '')
    lines = [
        '@echo off',
        'chcp 936 >nul',
        'title 远程桌面 - %s' % name,
        'rem 由 rdp_launcher.py 自动生成 %s' % time.strftime('%Y-%m-%d %H:%M'),
        'set "LAUNCHER=%s"' % script_path,
        'set "CONFIG=%s"' % config_path,
        '',
    ]
    lines += _bat_connect_block(entry, 'H', 1)
    return '\r\n'.join(lines).rstrip() + '\r\n'


def generate_menu_bat(hosts, config_path, script_path):
    """所有主机的菜单式一键启动 bat"""
    lines = [
        '@echo off',
        'chcp 936 >nul',
        'title 远程桌面一键连接',
        'rem 由 rdp_launcher.py 自动生成 %s' % time.strftime('%Y-%m-%d %H:%M'),
        'set "LAUNCHER=%s"' % script_path,
        'set "CONFIG=%s"' % config_path,
        '',
        'echo ============ 远程桌面 ============',
    ]
    for index, entry in enumerate(hosts, 1):
        lines.append('echo [%d] %s   (%s)' % (index, entry['name'], target_of(entry)))
    lines += [
        'echo [0] 退出',
        'echo.',
        'set /p "SEL=请输入序号并回车: "',
        'if "%SEL%"=="0" goto :eof',
    ]
    for index in range(1, len(hosts) + 1):
        lines.append('if "%%SEL%%"=="%d" goto H%d' % (index, index))
    lines += ['echo 无效序号', 'pause', 'goto :eof', '']
    for index, entry in enumerate(hosts, 1):
        lines += _bat_connect_block(entry, 'H', index)
    return '\r\n'.join(lines).rstrip() + '\r\n'


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #
def run_gui(config_path):
    import tkinter as tk
    from tkinter import messagebox, ttk

    hosts = load_hosts(config_path)

    class App(object):
        def __init__(self, root):
            self.root = root
            self.root.title('远程桌面启动器')
            self.root.geometry('760x460')
            self.root.minsize(680, 420)

            self.vars = {}
            self.current = None
            self._build()

        # ---------- 界面搭建 ----------
        def _build(self):
            main = ttk.Frame(self.root, padding=8)
            main.pack(fill='both', expand=True)

            # 左侧列表
            left = ttk.Frame(main)
            left.pack(side='left', fill='y', padx=(0, 8))
            ttk.Label(left, text='主机列表').pack(anchor='w')
            self.listbox = tk.Listbox(left, width=26, height=20, activestyle='dotbox')
            self.listbox.pack(fill='y', expand=True)
            self.listbox.bind('<<ListboxSelect>>', self.on_select)
            self.listbox.bind('<Double-1>', lambda e: self.on_connect())

            # 右侧表单
            right = ttk.Frame(main)
            right.pack(side='left', fill='both', expand=True)
            form = ttk.Frame(right)
            form.pack(fill='x')

            rows = [
                ('名称', 'name', True), ('地址', 'host', True),
                ('端口', 'port', False), ('用户名', 'user', False),
                ('密码', 'password', False),
                ('宽度', 'width', False), ('高度', 'height', False),
                ('备注', 'note', False),
            ]
            for idx, (label, key, _wide) in enumerate(rows):
                ttk.Label(form, text=label + '：').grid(row=idx, column=0, sticky='e', pady=3)
                var = tk.StringVar()
                self.vars[key] = var
                widget = ttk.Entry(form, textvariable=var, width=38)
                if key == 'password':
                    widget.config(show='*')
                widget.grid(row=idx, column=1, sticky='we', pady=3)

            checks = [
                ('全屏 (/f)', 'fullscreen'), ('管理员 (/admin)', 'admin'),
                ('每次询问凭据 (/prompt)', 'prompt'), ('记住密码(DPAPI)', 'save_password'),
            ]
            check_frame = ttk.Frame(right)
            check_frame.pack(fill='x', pady=(6, 0))
            for idx, (label, key) in enumerate(checks):
                var = tk.BooleanVar()
                self.vars[key] = var
                ttk.Checkbutton(check_frame, text=label, variable=var).grid(
                    row=0, column=idx, sticky='w', padx=(0, 12))

            ttk.Label(right, text='提示：勾选“记住密码”后，密码会用 Windows DPAPI 加密写进本机配置'
                                  '（只有当前用户能解开，不存明文），连接时直接登录，不再询问；'
                                  '没有密码时使用系统凭据管理器里已保存的凭据。',
                      foreground='#666', wraplength=520, justify='left').pack(
                anchor='w', pady=(10, 0))

            # 按钮
            bar = ttk.Frame(self.root, padding=(8, 0, 8, 8))
            bar.pack(fill='x')
            for text, cmd in (('新建', self.on_new), ('保存', self.on_save),
                              ('删除', self.on_delete), ('连接', self.on_connect),
                              ('从系统导入', self.on_import_system),
                              ('生成bat', self.on_make_bat),
                              ('退出', self.root.destroy)):
                ttk.Button(bar, text=text, command=cmd).pack(side='left', padx=3)

            self.status = tk.StringVar(value='就绪')
            ttk.Label(self.root, textvariable=self.status, relief='sunken',
                      anchor='w', padding=(4, 2)).pack(fill='x', side='bottom')

            self.refresh()
            if hosts:
                self.listbox.selection_set(0)
                self.on_select()

        # ---------- 数据 <-> 表单 ----------
        def refresh(self, keep_selection=None):
            self.listbox.delete(0, 'end')
            for entry in hosts:
                label = entry['name'] or '(未命名)'
                target = target_of(entry)
                self.listbox.insert('end', '%s  [%s]' % (label, target) if target else label)
            if keep_selection is not None and 0 <= keep_selection < len(hosts):
                self.listbox.selection_set(keep_selection)

        def on_select(self, _event=None):
            sel = self.listbox.curselection()
            if not sel:
                return
            self.current = hosts[sel[0]]
            for key in FIELDS:
                value = self.current.get(key, DEFAULT_ENTRY.get(key))
                if key == 'password':
                    self.vars[key].set(unprotect_password(value) or '')
                    continue
                if isinstance(self.vars.get(key), tk.BooleanVar):
                    self.vars[key].set(bool(value))
                else:
                    self.vars[key].set('' if value is None else str(value))

        def collect(self):
            entry = dict(DEFAULT_ENTRY)
            for key in FIELDS:
                var = self.vars.get(key)
                entry[key] = var.get() if var is not None else DEFAULT_ENTRY[key]
            entry['name'] = entry['name'].strip()
            entry['host'] = entry['host'].strip()
            return entry

        # ---------- 操作 ----------
        def on_new(self, _event=None):
            self.current = None
            self.listbox.selection_clear(0, 'end')
            for key in FIELDS:
                default = DEFAULT_ENTRY[key]
                if isinstance(self.vars.get(key), tk.BooleanVar):
                    self.vars[key].set(bool(default))
                else:
                    self.vars[key].set('')
            self.status.set('新建配置：填写后点“保存”')

        def on_save(self, _event=None):
            entry = self.collect()
            if not entry['name']:
                messagebox.showwarning('缺少名称', '请先填写名称')
                return
            if not entry['host']:
                messagebox.showwarning('缺少地址', '请先填写主机地址')
                return
            plain = entry['password']
            if entry['save_password'] and plain:
                encrypted = protect_password(plain)
                if encrypted is None:
                    messagebox.showwarning('密码未保存',
                                           '系统加密接口不可用，密码不会写入配置')
                    entry['password'] = ''
                    entry['save_password'] = False
                else:
                    entry['password'] = encrypted
            else:
                entry['password'] = ''
                entry['save_password'] = False
            if self.current is not None and self.current['name'] != entry['name']:
                hosts.remove(self.current)
            upsert_host(hosts, entry)
            save_hosts(config_path, hosts)
            self.current = entry
            self.refresh(keep_selection=hosts.index(entry))
            self.status.set('已保存：%s' % entry['name'])

        def on_delete(self, _event=None):
            if self.current is None:
                return
            name = self.current['name']
            if not messagebox.askyesno('确认删除', '删除配置“%s”？' % name):
                return
            hosts.remove(self.current)
            save_hosts(config_path, hosts)
            self.on_new()
            self.refresh()
            self.status.set('已删除：%s' % name)

        def on_connect(self, _event=None):
            entry = self.collect()
            if not entry['host']:
                messagebox.showwarning('缺少地址', '请先填写主机地址')
                return
            password = entry['password'] or None  # 不再弹窗询问，直接用已保存凭据连接
            try:
                args = connect(entry, password)
            except Exception as exc:
                messagebox.showerror('连接失败', str(exc))
                return
            self.status.set('已启动 mstsc：%s' % ' '.join(args))

        def on_import_system(self, _event=None):
            scanned = scan_system_hosts()
            if not scanned:
                messagebox.showinfo('从系统导入', '当前系统里没有找到远程桌面记录')
                return
            added, updated = merge_entries(hosts, scanned)
            save_hosts(config_path, hosts)
            self.refresh()
            self.status.set('系统导入完成：发现 %d 条，新增 %d 条，补充 %d 条'
                            % (len(scanned), added, updated))
            messagebox.showinfo('从系统导入',
                                '发现 %d 条记录\n新增 %d 条，补充/更新 %d 条'
                                % (len(scanned), added, updated))

        def on_make_bat(self, _event=None):
            if not hosts:
                messagebox.showwarning('生成 bat', '还没有任何主机配置')
                return
            out_dir = desktop_dir()
            if messagebox.askyesno('生成一键启动 bat',
                                   '是：为所有主机生成一个菜单 bat\n'
                                   '否：只为当前选中的主机生成\n\n输出目录：%s' % out_dir):
                path = os.path.join(out_dir, '远程桌面菜单.bat')
                write_bat(path, generate_menu_bat(hosts, config_path, SCRIPT_PATH))
            else:
                entry = self.current or self.collect()
                saved = find_host(hosts, entry.get('name', ''))
                if saved is None:
                    messagebox.showwarning('生成 bat', '请先保存当前配置')
                    return
                path = os.path.join(out_dir, safe_filename(saved['name']) + '.bat')
                write_bat(path, generate_bat(saved, config_path, SCRIPT_PATH))
            self.status.set('已生成：%s' % path)
            messagebox.showinfo('生成完成', '已生成：\n%s' % path)

    root = tk.Tk()
    App(root)
    root.mainloop()


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        description='远程桌面(mstsc)启动器：多主机配置 + 图形界面',
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--config', default=None,
                        help='配置文件路径，默认 %%APPDATA%%\\RdpLauncher\\hosts.json')
    parser.add_argument('--gui', action='store_true', help='打开图形界面')

    parser.add_argument('--list', action='store_true', help='列出所有主机配置')
    parser.add_argument('--connect', metavar='NAME', help='按名称直接连接')
    parser.add_argument('--add', metavar='NAME', help='新增或更新主机配置')
    parser.add_argument('--remove', metavar='NAME', help='删除指定主机配置')
    parser.add_argument('--export', metavar='PATH', help='导出配置到 JSON 文件')
    parser.add_argument('--import', metavar='PATH', dest='import_path',
                        help='从 JSON 文件导入配置')
    parser.add_argument('--scan', action='store_true',
                        help='扫描当前系统里已有的远程桌面记录')
    parser.add_argument('--import-system', action='store_true', dest='import_system',
                        help='把系统里的远程桌面记录导入当前配置')
    parser.add_argument('--make-bat', metavar='NAME', dest='make_bat',
                        help='为指定主机生成一键启动 bat')
    parser.add_argument('--make-bat-all', action='store_true', dest='make_bat_all',
                        help='为所有主机生成菜单式一键启动 bat')
    parser.add_argument('--out', metavar='DIR', help='bat 输出目录，默认桌面')

    parser.add_argument('--host', help='主机地址(IP 或域名)')
    parser.add_argument('--port', help='端口，默认 3389')
    parser.add_argument('--user', help='用户名')
    parser.add_argument('--password',
                        help='密码：配合 --connect 临时使用；配合 --add 则用 DPAPI 加密保存')
    parser.add_argument('--width', help='窗口宽度')
    parser.add_argument('--height', help='窗口高度')
    parser.add_argument('--note', help='备注')

    parser.add_argument('--fullscreen', dest='fullscreen', action='store_true', default=None)
    parser.add_argument('--window', dest='fullscreen', action='store_false')
    parser.add_argument('--admin', dest='admin', action='store_true', default=None)
    parser.add_argument('--no-admin', dest='admin', action='store_false')
    parser.add_argument('--prompt', dest='prompt', action='store_true', default=None)
    parser.add_argument('--no-prompt', dest='prompt', action='store_false')
    return parser


def print_hosts(hosts):
    if not hosts:
        print('(暂无配置)')
        return
    print('%-20s %-28s %-10s %s' % ('名称', '地址', '用户名', '选项'))
    print('-' * 78)
    for entry in hosts:
        opts = []
        if entry['fullscreen']:
            opts.append('全屏')
        elif entry['width'] and entry['height']:
            opts.append('%sx%s' % (entry['width'], entry['height']))
        if entry['admin']:
            opts.append('管理员')
        if entry['prompt']:
            opts.append('询问凭据')
        if entry.get('save_password') and entry.get('password'):
            opts.append('已存密码')
        print('%-20s %-28s %-10s %s' % (entry['name'], target_of(entry),
                                        entry['user'] or '-', ','.join(opts) or '-'))


def main(argv=None):
    args = build_parser().parse_args(argv)
    config_path = args.config or default_config_path()
    hosts = load_hosts(config_path)

    if args.import_path:
        imported = load_hosts(args.import_path)
        for entry in imported:
            upsert_host(hosts, entry)
        save_hosts(config_path, hosts)
        print('已导入 %d 条配置 -> %s' % (len(imported), config_path))

    if args.add:
        entry = dict(DEFAULT_ENTRY)
        entry['name'] = args.add
        for key in ('host', 'port', 'user', 'width', 'height', 'note'):
            value = getattr(args, key)
            if value is not None:
                entry[key] = value
        for key in ('fullscreen', 'admin', 'prompt'):
            value = getattr(args, key)
            if value is not None:
                entry[key] = value
        if args.password is not None:
            encrypted = protect_password(args.password)
            if encrypted is None:
                print('警告：系统加密接口不可用，密码未保存')
            else:
                entry['password'] = encrypted
                entry['save_password'] = bool(encrypted)
        existing = find_host(hosts, args.add)
        if existing is not None:
            existing.update(entry)
        else:
            hosts.append(entry)
        save_hosts(config_path, hosts)
        print('已保存配置：%s -> %s' % (args.add, target_of(entry)))

    if args.remove:
        entry = find_host(hosts, args.remove)
        if entry is None:
            print('未找到配置：%s' % args.remove)
        else:
            hosts.remove(entry)
            save_hosts(config_path, hosts)
            print('已删除配置：%s' % args.remove)

    if args.scan:
        scanned = scan_system_hosts()
        print('系统记录（MRU 历史 / 用户名提示 / 凭据管理器）：')
        print_hosts(scanned)
        print('(选项里没有“询问凭据”= 凭据管理器已保存该主机密码，可免密登录)')

    if args.import_system:
        scanned = scan_system_hosts()
        added, updated = merge_entries(hosts, scanned)
        save_hosts(config_path, hosts)
        print('已导入系统记录：发现 %d 条，新增 %d 条，补充/更新 %d 条'
              % (len(scanned), added, updated))

    if args.make_bat:
        entry = find_host(hosts, args.make_bat)
        if entry is None:
            print('未找到配置：%s' % args.make_bat)
            return 1
        path = os.path.join(args.out or desktop_dir(),
                            safe_filename(entry['name']) + '.bat')
        write_bat(path, generate_bat(entry, config_path, SCRIPT_PATH))
        print('已生成：%s' % path)

    if args.make_bat_all:
        path = os.path.join(args.out or desktop_dir(), '远程桌面菜单.bat')
        write_bat(path, generate_menu_bat(hosts, config_path, SCRIPT_PATH))
        print('已生成：%s' % path)

    if args.list:
        print('配置文件：%s' % config_path)
        print_hosts(hosts)

    if args.connect:
        entry = find_host(hosts, args.connect)
        if entry is None:
            print('未找到配置：%s' % args.connect)
            return 1
        try:
            used = connect(entry, args.password)
        except Exception as exc:
            print('连接失败：%s' % exc)
            return 1
        print('已启动 mstsc %s' % ' '.join(used))
        return 0

    if args.export:
        save_hosts(args.export, hosts)
        print('已导出 %d 条配置 -> %s' % (len(hosts), args.export))

    acted = any([args.import_path, args.add, args.remove, args.list,
                 args.connect, args.export, args.scan, args.import_system,
                 args.make_bat, args.make_bat_all])
    if args.gui or not acted:
        run_gui(config_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
