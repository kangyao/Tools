"""AICore DS desktop console. Python standard library only."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from http.client import HTTPException
import json
import ntpath
import queue
import re
import shlex
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


HELP = """instances                     扫描本机服务
use pid <PID> / use port <端口>  选择实例（游戏 UDP 或管理 HTTP 端口）
status / players / worlds / maps 查看状态、玩家、World、地图模板
match <玩家ID> / cancel <玩家ID> 加入匹配 / 取消排队（四人自动开局）
matches / queue                查看比赛 / 排队玩家
return <玩家ID>                返回大厅；当前规则会结束该玩家所在比赛
info / caps / performance / memory 查看宿主信息与诊断
create <模板ID>                创建临时副本；模板含空格时用双引号
kick <玩家ID> [原因]           踢人，本局不能重入
request <请求ID>               恢复查询命令结果
stop                           正常停止选中的 DS
help                           显示帮助
超时不自动重发写命令；关闭本工具不会停止 DS。"""


class ManagerError(Exception):
    pass


class IdentityError(ManagerError):
    pass


class PendingError(ManagerError):
    pass


class ServiceError(ManagerError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ManagerError("管理服务返回重定向，已拒绝")


def unwrap(response):
    if not isinstance(response, dict) or response.get("ok") is not True:
        error = response.get("error", "服务响应无效") if isinstance(response, dict) else "服务响应无效"
        if isinstance(error, dict):
            error = f"{error.get('code', 'error')}: {error.get('message', '')}"
        raise ServiceError(str(error))
    return response.get("data", {})


def http_json(base, route, payload=None, timeout=3):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(base + route, data=data, headers={"Content-Type": "application/json; charset=utf-8"})
    # Local management must not go through machine HTTP proxy settings.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ManagerError("服务响应超过 4 MiB")
            return unwrap(json.loads(body.decode("utf-8")))
    except HTTPError as exc:
        try:
            body = exc.read(8192)
        except (OSError, TimeoutError) as error:
            raise ManagerError(f"读取 HTTP {exc.code} 错误响应超时") from error
        finally:
            exc.close()
        try:
            unwrap(json.loads(body.decode("utf-8")))
        except ManagerError as error:
            if "instance_mismatch" in str(error):
                raise IdentityError("实例已改变，请重新扫描并选择") from error
            raise
        except (ValueError, UnicodeError):
            pass
        raise ServiceError(f"HTTP {exc.code}: {body.decode('utf-8', errors='replace')}") from exc
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        raise ManagerError(f"无法连接 {base}：{exc}") from exc
    except (ValueError, UnicodeError) as exc:
        raise ManagerError("服务返回了无效 JSON") from exc


def net_endpoints():
    """Return local listening TCP/UDP port and owning PID from Windows netstat."""
    try:
        result = subprocess.run(["netstat", "-ano", "-n"], capture_output=True, timeout=12,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagerError(f"无法扫描本机端口：{exc}") from exc
    if result.returncode:
        raise ManagerError("netstat 扫描失败")
    tcp, udp = {}, {}
    for line in result.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] not in ("TCP", "UDP"):
            continue
        try:
            port, pid = int(parts[1].rsplit(":", 1)[1]), int(parts[-1])
        except ValueError:
            continue
        if not (1 <= port <= 65535 and pid > 0):
            continue
        # Listening TCP has a zero remote port, also on non-English Windows.
        if parts[0] == "TCP" and len(parts) == 5 and parts[2].rsplit(":", 1)[-1] == "0":
            tcp.setdefault(port, set()).add(pid)
        elif parts[0] == "UDP":
            udp.setdefault(port, set()).add(pid)
    return tcp, udp


class ProcessHandle:
    """Keep the original Windows process object, even if its PID is reused."""
    def __init__(self, pid):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not self.handle:
            raise IdentityError(f"无法验证进程 {pid}，进程已退出或没有访问权限")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.kernel.CloseHandle(self.handle)

    def stamp(self):
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
            raise IdentityError("无法读取进程启动身份")
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def wait(self, seconds):
        code = self.kernel.WaitForSingleObject(self.handle, int(seconds * 1000))
        if code == 258:
            raise PendingError("停服已受理，但目标进程尚未退出；没有强杀进程")
        if code != 0:
            raise ManagerError("等待目标进程退出失败")
        exit_code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(exit_code)):
            raise ManagerError("无法读取退出码")
        return exit_code.value


def process_stamp(pid):
    with ProcessHandle(pid) as process:
        return process.stamp()


@dataclass(frozen=True)
class Instance:
    pid: int
    http_port: int
    game_port: str
    role: str
    project: str
    instance_id: str
    base_url: str
    started: int
    commands: tuple[str, ...]
    udp_ports: tuple[int, ...] = ()


def project_key(path):
    return ntpath.normcase(ntpath.normpath(path))


def probe(port, owners, udp):
    for host in ("127.0.0.1", "[::1]"):
        base = f"http://{host}:{port}"
        try:
            info = http_json(base, "/api/aicore/v1/info", timeout=0.7)
            if not isinstance(info, dict):
                continue
            pid = info.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid not in owners:
                continue
            if not isinstance(info.get("instance_id"), str) or not info["instance_id"]:
                continue
            if not isinstance(info.get("project_path"), str) or not info["project_path"]:
                continue
            caps = http_json(base, "/api/aicore/v1/caps", timeout=0.7)
            if not isinstance(caps, dict) or caps.get("protocol") != "aicore/v1" or not isinstance(caps.get("commands", []), list):
                continue
            role = info.get("net_role", "Unknown")
            if role == "CloudServer":
                role = "Dedicated"
            try:
                admin = http_json(base, "/api/aicore/v1/admin", timeout=0.7)
                if isinstance(admin, dict):
                    role = admin.get("role", role)
            except ManagerError:
                pass
            started = process_stamp(pid)
            # Recheck after holding process identity to reject a restart during discovery.
            fresh = http_json(base, "/api/aicore/v1/info", timeout=0.7)
            if fresh.get("instance_id") != info["instance_id"] or fresh.get("pid") != pid:
                continue
            return Instance(pid, port, str(info.get("game_port", "")), role,
                            info["project_path"], info["instance_id"], base, started,
                            tuple(caps.get("commands", [])),
                            tuple(sorted(p for p, ids in udp.items() if pid in ids)))
        except (ManagerError, ValueError, AttributeError):
            continue
    return None


def scan_instances(port=None):
    tcp, udp = net_endpoints()
    if port is not None:
        owners = tcp.get(port, set()) | udp.get(port, set())
        tcp = {p: ids for p, ids in tcp.items() if ids & owners}
    with ThreadPoolExecutor(max_workers=16) as pool:
        found = list(pool.map(lambda item: probe(item[0], item[1], udp), sorted(tcp.items())))
    return sorted((item for item in found if item is not None), key=lambda item: (item.pid, item.http_port))


class Client:
    def __init__(self, instance, log=lambda text: None, timeout=20):
        self.instance = instance
        self.log = log
        self.timeout = timeout

    def validate(self):
        target = self.instance
        info = http_json(target.base_url, "/api/aicore/v1/info")
        if (not isinstance(info, dict) or info.get("pid") != target.pid or info.get("instance_id") != target.instance_id
                or project_key(str(info.get("project_path", ""))) != project_key(target.project)
                or process_stamp(target.pid) != target.started):
            raise IdentityError("实例身份已改变，请重新扫描并选择")
        return info

    def read(self, route):
        self.validate()
        result = http_json(self.instance.base_url, "/api/aicore/v1/" + route)
        if not isinstance(result, dict):
            raise ServiceError("管理接口返回的数据结构不受支持")
        if route == "admin":
            # Legacy serializers can emit {} for empty arrays, or [] for empty objects.
            for key in ("matchmaking", "travel"):
                value = result.get(key, {})
                if value == [] or value is None:
                    value = {}
                if not isinstance(value, dict):
                    raise ServiceError(f"管理快照中的 {key} 不是对象")
                result[key] = value
            for container, keys in ((result, ("players", "worlds", "maps")),
                                    (result.get("matchmaking", {}), ("matches", "queued_players")),
                                    (result.get("travel", {}), ("clients", "pawns"))):
                for key in keys:
                    value = container.get(key, [])
                    if value == {} or value is None:
                        value = []
                    if not isinstance(value, list):
                        raise ServiceError(f"管理快照中的 {key} 不是数组")
                    container[key] = value
        self.validate()
        return result

    def dedicated(self):
        admin = self.read("admin")
        if admin.get("role") != "Dedicated":
            raise ManagerError("该实例不是 DS，不能执行玩法管理命令")
        return admin

    def poll(self, request_id):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            result = self.read(f"commands?id={request_id}")
            if result.get("state") != "pending":
                return result
            time.sleep(0.15)
        raise PendingError(f"请求 {request_id} 已受理，结果尚未确定。输入 request {request_id} 继续查询；不要重发。")

    def write(self, operation, args):
        self.validate()
        caps = http_json(self.instance.base_url, "/api/aicore/v1/caps")
        if not isinstance(caps, dict) or not isinstance(caps.get("commands", []), list):
            raise ServiceError("服务能力目录无效")
        if operation not in caps.get("commands", []):
            raise ManagerError(f"当前服务不支持 {operation}")
        payload = {"instance_id": self.instance.instance_id, "operation": operation, "args": args}
        try:
            result = http_json(self.instance.base_url, "/api/aicore/v1/commands", payload)
        except (IdentityError, ServiceError):
            raise
        except ManagerError as exc:
            raise PendingError(f"提交未取得有效回执，执行结果未知，请先检查服务器状态，勿自动重发：{exc}") from exc
        if operation == "runtime.stop":
            return result
        request_id = str(result.get("request_id", ""))
        if not re.fullmatch(r"[1-9][0-9]*", request_id):
            raise PendingError("服务器未返回有效请求 ID；执行结果未知，不要重复提交")
        self.log(f"已受理，请求 ID={request_id}")
        return self.poll(request_id)

    def create(self, map_id):
        admin = self.dedicated()
        if map_id not in [item.get("map_id") for item in admin.get("maps", [])]:
            raise ManagerError("模板未注册，请先执行 maps")
        result = self.write("ds.world.create", {"map_id": map_id})
        if result.get("state") == "Running":
            return result
        world_id, generation = result.get("world_id"), result.get("generation")
        if world_id is None or generation is None:
            raise PendingError("创建请求已执行，但未返回 World 身份，不能确认就绪；请查看 worlds，勿重复创建")
        self.log(f"World {world_id} 已分配，等待加载完成…")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            worlds = self.dedicated().get("worlds", [])
            matches = [w for w in worlds if w.get("world_id") == world_id and w.get("generation") == generation]
            if not matches:
                raise ManagerError(f"World {world_id} 已失效或加载失败，请检查服务日志")
            world = matches[0]
            if world.get("state") == "Running":
                return world
            if world.get("state") != "Loading":
                raise ManagerError(f"World {world_id} 未就绪：{world.get('state')}")
            time.sleep(0.25)
        raise PendingError(f"World {world_id} 仍在加载，输入 worlds 检查；不要重复创建")

    def execute(self, line):
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise ManagerError(f"命令引号未闭合：{exc}") from exc
        if not parts:
            raise ManagerError("请输入命令")
        verb = parts[0].lower()
        if verb == "help" and len(parts) == 1:
            return HELP
        if verb in ("info", "caps", "performance", "memory") and len(parts) == 1:
            return self.read(verb)
        if verb in ("matches", "queue") and len(parts) == 1:
            matching = self.dedicated().get("matchmaking", {})
            if not matching.get("available"):
                raise ManagerError("当前 DS 未启用大厅匹配，请使用带匹配支持的宿主和框架")
            if verb == "matches":
                return matching.get("matches", [])
            return [{"player_id": player_id} for player_id in matching.get("queued_players", [])]
        if verb in ("match", "join", "cancel", "return") and len(parts) == 2:
            if not re.fullmatch(r"[1-9][0-9]*", parts[1]) or int(parts[1]) > 4294967295:
                raise ManagerError("玩家 ID 必须为 1..4294967295 的整数")
            self.dedicated()
            operation = {"match": "join", "join": "join", "cancel": "cancel", "return": "return"}[verb]
            result = self.write("ds.match." + operation, {"player_id": int(parts[1])})
            if verb == "return":
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    admin = self.dedicated()
                    player = next((p for p in admin.get("players", []) if p.get("player_id") == int(parts[1])), None)
                    lobby_id = result.get("lobby_world_id")
                    travel_state = next((p for p in admin.get("travel", {}).get("clients", [])
                                         if p.get("player_id") == int(parts[1])), None)
                    client_ready = travel_state is None or (travel_state.get("world_id") == lobby_id and travel_state.get("state") == "Active")
                    if player and lobby_id is not None and player.get("world_id") == lobby_id and not player.get("travel_state") and client_ready:
                        return {**result, "state": "InLobby", "world_id": lobby_id}
                    if player is None:
                        raise ManagerError("返回大厅期间玩家已离线")
                    time.sleep(0.2)
                raise PendingError("返回大厅已受理，迁移尚未完成；输入 players / matches 查看进度，不要重复提交")
            return result
        if verb in ("status", "players", "worlds", "maps") and len(parts) == 1:
            admin = self.read("admin")
            if verb != "status":
                return admin.get(verb, [])
            return {"pid": self.instance.pid, "http_port": self.instance.http_port,
                    "game_port": self.instance.game_port, "project": self.instance.project,
                    "role": admin.get("role"), "state": admin.get("state"),
                    "accepting_players": admin.get("accepting_players"),
                    "player_count": len(admin.get("players", [])), "world_count": len(admin.get("worlds", [])),
                    "matchmaking": admin.get("matchmaking", {}), "travel": admin.get("travel", {})}
        if verb == "request" and len(parts) == 2 and re.fullmatch(r"[1-9][0-9]*", parts[1]):
            return self.poll(parts[1])
        if verb == "create" and len(parts) == 2:
            return self.create(parts[1])
        if verb == "kick" and len(parts) >= 2:
            if not re.fullmatch(r"[1-9][0-9]*", parts[1]) or int(parts[1]) > 4294967295:
                raise ManagerError("玩家 ID 必须为 1..4294967295 的整数")
            reason = " ".join(parts[2:]) or "DS Manager"
            if len(reason.encode("utf-8")) > 256:
                raise ManagerError("踢人原因超过 256 UTF-8 字节")
            self.dedicated()
            return self.write("ds.kick", {"player_id": int(parts[1]), "reason": reason})
        if verb == "stop" and len(parts) == 1:
            if self.instance.role != "Dedicated":
                raise ManagerError("请先选择 DS 实例")
            with ProcessHandle(self.instance.pid) as process:
                if process.stamp() != self.instance.started:
                    raise IdentityError("PID 已被复用，请重新扫描")
                info = self.validate()
                if info.get("net_role") not in ("CloudServer", "Dedicated"):
                    self.dedicated()
                self.write("runtime.stop", {})
                self.log("停服已受理，等待原进程退出…")
                return {"pid": self.instance.pid, "state": "stopped", "exit_code": process.wait(self.timeout)}
        raise ManagerError("命令或参数不正确，输入 help 查看用法")


class ManagerWindow:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText
        self.root, self.tk, self.ttk = root, tk, ttk
        self.events = queue.Queue()
        self.busy = False
        self.instances = []
        self.selected = None
        self.history = []
        self.history_index = 0
        self.closed = False
        root.title("DS 管理工具")
        root.geometry("1120x780")
        root.minsize(840, 600)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        top = ttk.Frame(root, padding=12)
        top.pack(fill="x")
        self.scan_button = ttk.Button(top, text="扫描本机服务", command=self.scan)
        self.scan_button.pack(side="left")
        self.only_ds = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="仅显示 DS", variable=self.only_ds, command=self.render_instances).pack(side="left", padx=12)
        ttk.Label(top, text="指定端口：").pack(side="left")
        self.port = tk.StringVar()
        ttk.Entry(top, textvariable=self.port, width=8).pack(side="left")
        ttk.Button(top, text="查找", command=self.scan).pack(side="left", padx=5)
        self.status = tk.StringVar(value="准备扫描…")
        ttk.Label(top, textvariable=self.status).pack(side="right")
        frame = ttk.Frame(root, padding=(12, 0, 12, 8))
        frame.pack(fill="x")
        columns = ("pid", "http", "game", "role", "project")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=5, selectmode="browse")
        for name, title, width in zip(columns, ("PID", "管理 HTTP", "游戏 UDP", "角色", "工程"), (90, 95, 95, 110, 560)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=60, stretch=name == "project")
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self.choose)
        self.target = tk.StringVar(value="请选择上面的服务；关闭工具不会停服")
        ttk.Label(root, textvariable=self.target, padding=(12, 4)).pack(fill="x")
        buttons = ttk.Frame(root, padding=(12, 4))
        buttons.pack(fill="x")
        self.command_buttons = []
        for text, command in (("刷新状态", "status"), ("查看玩家", "players"), ("查看 World", "worlds"),
                              ("查看比赛", "matches"), ("匹配队列", "queue"), ("地图模板", "maps"), ("帮助", "help")):
            button = ttk.Button(buttons, text=text, command=lambda c=command: self.run(c))
            button.pack(side="left", padx=(0, 6))
            self.command_buttons.append(button)
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=8)
        console_frame = ttk.Frame(self.tabs)
        self.tabs.add(console_frame, text="命令与结果")
        self.output = ScrolledText(console_frame, wrap="word", font=("Consolas", 11), state="disabled", padx=10, pady=8)
        self.output.pack(fill="both", expand=True)
        self.tables = {}
        for key, title, cols in (
                ("players", "玩家", ("player_id", "world_id", "connection_id", "travel_state")),
                ("worlds", "World", ("world_id", "generation", "map_id", "state", "player_count", "actor_count", "tick_count")),
                ("matches", "比赛", ("id", "worldId", "mapId", "state", "players", "teams")),
                ("queue", "匹配队列", ("player_id",)),
                ("maps", "地图模板", ("map_id",))):
            page = ttk.Frame(self.tabs)
            self.tabs.add(page, text=title)
            table = ttk.Treeview(page, columns=cols, show="headings", selectmode="browse")
            for col in cols:
                table.heading(col, text=col)
                table.column(col, width=120, minwidth=75)
            scrollbar = ttk.Scrollbar(page, command=table.yview)
            table.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            table.pack(fill="both", expand=True)
            self.tables[key] = table
            if key == "players":
                actions = ttk.Frame(page)
                actions.pack(fill="x", pady=6)
                for text, verb in (("加入匹配", "match"), ("取消匹配", "cancel"), ("返回大厅", "return")):
                    ttk.Button(actions, text=text, command=lambda v=verb: self.match_selected(v)).pack(side="left", padx=4)
                ttk.Button(actions, text="踢出选中的玩家", command=self.kick_selected).pack(side="right", padx=4)
            if key == "maps":
                ttk.Button(page, text="用选中的模板创建副本", command=self.create_selected).pack(anchor="e", pady=6)
        entry_frame = ttk.Frame(root, padding=12)
        entry_frame.pack(fill="x")
        self.command = tk.StringVar()
        self.entry = ttk.Entry(entry_frame, textvariable=self.command, font=("Consolas", 12))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda event: self.submit())
        self.entry.bind("<Up>", lambda event: self.recall(-1))
        self.entry.bind("<Down>", lambda event: self.recall(1))
        self.execute_button = ttk.Button(entry_frame, text="执行", command=self.submit)
        self.execute_button.pack(side="left", padx=(8, 0))
        self.command_buttons.append(self.execute_button)
        ttk.Label(root, text="示例：match 900000001  |  cancel 900000001  |  matches  |  return 900000001  |  stop", padding=(12, 0, 12, 10)).pack(fill="x")
        self.append(HELP)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(80, self.drain_events)
        root.after(150, self.scan)

    def close(self):
        self.closed = True
        self.root.destroy()

    def append(self, message):
        self.output.configure(state="normal")
        self.output.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        if int(self.output.index("end-1c").split(".")[0]) > 5000:
            self.output.delete("1.0", "1001.0")
        self.output.see("end")
        self.output.configure(state="disabled")

    def launch(self, function, done):
        if self.busy:
            return
        self.busy = True
        self.status.set("执行中…")
        self.scan_button.configure(state="disabled")
        for button in self.command_buttons:
            button.configure(state="disabled")
        def worker():
            try:
                self.events.put(("done", done, function()))
            except Exception as exc:
                self.events.put(("error", None, exc))
        threading.Thread(target=worker, daemon=True).start()

    def drain_events(self):
        if self.closed:
            return
        while True:
            try:
                kind, done, result = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append(result)
                continue
            self.busy = False
            self.scan_button.configure(state="normal")
            for button in self.command_buttons:
                button.configure(state="normal")
            self.status.set("就绪" if kind == "done" else "操作失败")
            if kind == "done":
                done(result)
            else:
                self.append(f"错误：{result}")
                self.tabs.select(0)
                if isinstance(result, IdentityError):
                    self.selected = None
                    self.target.set("实例已失效，请重新扫描选择")
                    self.clear_tables()
        self.root.after(80, self.drain_events)

    def scan(self):
        if self.busy:
            return
        text = self.port.get().strip()
        if text and (not text.isdigit() or not 1 <= int(text) <= 65535):
            self.append("端口必须为 1..65535")
            return
        def done(items):
            self.instances = items
            if self.selected and self.selected not in items:
                self.selected = None
                self.clear_tables()
                self.target.set("原实例已离线，请重新选择")
            self.render_instances()
            count = len([i for i in items if i.role == "Dedicated"])
            self.status.set(f"发现 {count} 台 DS，{len(items)} 个服务")
            self.append(f"扫描完成：{count} 台 DS / {len(items)} 个可连接 AICore 服务")
        self.launch(lambda: scan_instances(int(text) if text else None), done)

    def clear_tables(self):
        for table in self.tables.values():
            table.delete(*table.get_children())

    def render_instances(self):
        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(self.instances):
            if self.only_ds.get() and item.role != "Dedicated":
                continue
            self.tree.insert("", "end", iid=str(index), values=(item.pid, item.http_port, item.game_port, item.role, item.project))
            if item == self.selected:
                self.tree.selection_set(str(index))

    def choose(self, event=None):
        if self.busy:
            # Keep the visible row aligned with the target captured by the worker.
            intended = str(self.instances.index(self.selected)) if self.selected in self.instances else None
            rows = self.tree.selection()
            if intended is not None and self.tree.exists(intended):
                if rows != (intended,):
                    self.tree.selection_set(intended)
            elif rows:
                self.tree.selection_remove(*rows)
            return
        rows = self.tree.selection()
        if not rows:
            return
        item = self.instances[int(rows[0])]
        if item == self.selected:
            return
        self.selected = item
        self.clear_tables()
        self.target.set(f"当前：PID {item.pid}  ·  UDP {item.game_port or '?'}  ·  HTTP {item.http_port}  ·  {item.role}  ·  {item.project}")
        self.run("status")

    def submit(self):
        line = self.command.get().strip()
        if not line or self.busy:
            return
        self.history.append(line)
        self.history_index = len(self.history)
        self.command.set("")
        self.run(line)

    def recall(self, step):
        self.history_index = max(0, min(len(self.history), self.history_index + step))
        self.command.set(self.history[self.history_index] if self.history_index < len(self.history) else "")
        return "break"

    def run(self, line):
        if self.busy:
            return
        if line.lower() == "help":
            self.append(HELP)
            self.tabs.select(0)
            return
        if line.lower() == "instances":
            self.scan()
            return
        match = re.fullmatch(r"use\s+(pid|port)\s+([1-9][0-9]*)", line, re.I)
        if match:
            kind, value = match[1].lower(), int(match[2])
            matches = [item for item in self.instances if (item.pid == value if kind == "pid" else item.http_port == value or value in item.udp_ports)]
            if len(matches) != 1:
                self.append("未找到唯一匹配实例，请先扫描或按 PID 选择")
                return
            self.only_ds.set(False if matches[0].role != "Dedicated" else self.only_ds.get())
            self.render_instances()
            self.tree.selection_set(str(self.instances.index(matches[0])))
            self.choose()
            return
        if self.selected is None:
            self.append("请先选择一个服务")
            return
        target = self.selected
        self.append(f"DS[{target.pid} HTTP:{target.http_port}]> {line}")
        client = Client(target, log=lambda text: self.events.put(("log", None, text)))
        def done(result):
            self.append(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
            key = line.strip().lower()
            if key in self.tables and isinstance(result, list):
                table = self.tables[key]
                table.delete(*table.get_children())
                for row in result:
                    values = [row.get(col, "") for col in table["columns"]]
                    values = [json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value for value in values]
                    table.insert("", "end", values=values)
                self.tabs.select(list(self.tables).index(key) + 1)
            else:
                self.tabs.select(0)
            if isinstance(result, dict) and result.get("state") == "stopped":
                self.selected = None
                self.clear_tables()
                self.target.set("目标 DS 已停止，请扫描选择其他实例")
                self.scan()
        self.launch(lambda: client.execute(line), done)

    def match_selected(self, verb):
        if self.busy:
            return
        rows = self.tables["players"].selection()
        if not rows:
            self.append("请先查看并选择玩家")
            return
        player_id = self.tables["players"].item(rows[0], "values")[0]
        self.run(f"{verb} {player_id}")

    def kick_selected(self):
        from tkinter import simpledialog
        if self.busy:
            return
        rows = self.tables["players"].selection()
        if not rows:
            self.append("请先查看并选择玩家")
            return
        player_id = self.tables["players"].item(rows[0], "values")[0]
        reason = simpledialog.askstring("踢出玩家", f"玩家 {player_id} 的踢出原因：", initialvalue="DS Manager", parent=self.root)
        if reason is not None:
            self.run(f"kick {player_id} {shlex.quote(reason)}")

    def create_selected(self):
        if self.busy:
            return
        rows = self.tables["maps"].selection()
        if not rows:
            self.append("请先查看并选择地图模板")
            return
        map_id = self.tables["maps"].item(rows[0], "values")[0]
        self.run("create " + shlex.quote(map_id))


def main():
    parser = argparse.ArgumentParser(description="AICore DS 管理工具，无参数启动桌面界面")
    parser.add_argument("--scan", action="store_true", help="扫描本机服务并输出 JSON")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--pid", type=int)
    selector.add_argument("--port", type=int, help="游戏 UDP 或管理 HTTP 端口")
    parser.add_argument("--command", help="执行一条管理命令，输出 JSON")
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("端口必须为 1..65535")
    if args.pid is not None and args.pid <= 0:
        parser.error("PID 必须为正整数")
    if args.command or args.scan:
        try:
            items = scan_instances(args.port)
            if args.pid is not None:
                items = [item for item in items if item.pid == args.pid]
            if args.command:
                if len(items) != 1:
                    raise ManagerError("请用 --pid 或 --port 选择唯一实例")
                result = Client(items[0]).execute(args.command)
            else:
                result = [asdict(item) for item in items]
            print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2))
            return 0
        except ManagerError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2 if isinstance(exc, PendingError) else 1
    import tkinter as tk
    root = tk.Tk()
    ManagerWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
