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


MATCH_SIZE = 4


def lobby_view(admin):
    """Project one authoritative snapshot into UI state; never infer ready from World alone."""
    matching = admin.get("matchmaking", {})
    matches = matching.get("matches", [])
    queued = set(matching.get("queued_players", []))
    memberships = {pid: match for match in matches for pid in match.get("players", [])}
    travel = {item.get("player_id"): item for item in admin.get("travel", {}).get("clients", [])}
    players = []
    for player in admin.get("players", []):
        pid = player["player_id"]
        match = memberships.get(pid)
        moving = bool(player.get("travel_state")) or travel.get(pid, {}).get("state") in ("Preparing", "Committing")
        failed = travel.get(pid, {}).get("state") == "Failed"
        in_lobby = matching.get("lobby_world_id") is not None and player.get("world_id") == matching["lobby_world_id"]
        can_join = bool(matching.get("available") and admin.get("accepting_players") and in_lobby
                        and not moving and not failed and pid not in queued and match is None)
        if failed:
            state = "切换失败"
        elif moving:
            state = "切换中"
        elif pid in queued:
            state = "排队中"
        elif match:
            state = {"Playing": "比赛中", "Returning": "返回中"}.get(match.get("state"), "准备比赛")
        else:
            state = "大厅空闲" if in_lobby else "其他 World"
        players.append({**player, "status": state, "match_id": match.get("id", "") if match else "",
                        "can_join": can_join, "can_cancel": pid in queued,
                        "can_return": bool(match and match.get("state") not in ("Returning", "Closing") and not moving)})
    return {"players": players, "matches": matches, "queued": queued, "available": bool(matching.get("available")),
            "needed": max(0, MATCH_SIZE - len(queued))}


def match_batch(client, verb, player_ids):
    """Execute a captured selection sequentially; stop on ambiguous results, never retry writes."""
    completed = []
    handled_matches = set()
    for pid in dict.fromkeys(player_ids):
        try:
            view = lobby_view(client.dedicated())
            player = next((p for p in view["players"] if p["player_id"] == pid), None)
            if verb == "return" and player and player["match_id"] in handled_matches:
                continue
            if not player or not player.get({"match": "can_join", "cancel": "can_cancel", "return": "can_return"}[verb]):
                completed.append({"player_id": pid, "state": "skipped", "reason": "玩家状态已变化"})
                continue
            result = client.execute(f"{verb} {pid}")
            completed.append({"player_id": pid, "state": "completed", "result": result})
            client.log(f"玩家 {pid}：{verb} 已完成")
            if verb == "return":
                handled_matches.add(player["match_id"])
        except IdentityError:
            raise
        except ManagerError as exc:
            completed.append({"player_id": pid, "state": "unknown" if isinstance(exc, PendingError) else "failed", "reason": str(exc)})
            break
    return completed


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
        self.timers = set()
        self.admin = None
        self.view = None
        self.refresh_error = False
        root.title("DS 管理工具")
        root.geometry("1180x860")
        root.minsize(940, 720)
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
        self.find_button = ttk.Button(top, text="查找", command=self.scan)
        self.find_button.pack(side="left", padx=5)
        self.status = tk.StringVar(value="准备扫描…")
        ttk.Label(top, textvariable=self.status).pack(side="right")
        frame = ttk.Frame(root, padding=(12, 0, 12, 8))
        frame.pack(fill="x")
        columns = ("pid", "http", "game", "role", "project")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=3, selectmode="browse")
        for name, title, width in zip(columns, ("PID", "管理 HTTP", "游戏 UDP", "角色", "工程"), (90, 95, 95, 110, 560)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=60, stretch=name == "project")
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self.choose)
        self.target = tk.StringVar(value="请选择上面的服务；关闭工具不会停服")
        ttk.Label(root, textvariable=self.target, padding=(12, 4)).pack(fill="x")
        buttons = ttk.Frame(root, padding=(12, 4))
        buttons.pack(fill="x")
        self.command_buttons = [self.find_button]
        ttk.Button(buttons, text="刷新大厅", command=self.refresh).pack(side="left", padx=(0, 6))
        self.auto_refresh = tk.BooleanVar(value=True)
        ttk.Checkbutton(buttons, text="每 3 秒自动刷新", variable=self.auto_refresh).pack(side="left", padx=(0, 18))
        for text, command in (("World 管理", "worlds"), ("地图模板", "maps"), ("帮助", "help")):
            button = ttk.Button(buttons, text=text, command=lambda c=command: self.run(c))
            button.pack(side="left", padx=(0, 6))
            self.command_buttons.append(button)
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=8)
        self.dashboard = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.dashboard, text="大厅与匹配")
        self.summary = tk.StringVar(value="选择 DS 后自动显示大厅玩家与比赛")
        ttk.Label(self.dashboard, textvariable=self.summary, font=("Microsoft YaHei UI", 12, "bold")).pack(fill="x", pady=(0, 5))
        self.hint = tk.StringVar(value="只有 1 台 DS 时自动连接；有多台时点击上面的服务。")
        ttk.Label(self.dashboard, textvariable=self.hint).pack(fill="x", pady=(0, 8))
        actions = ttk.Frame(self.dashboard)
        actions.pack(fill="x", pady=(0, 8))
        self.quick_button = ttk.Button(actions, text="一键凑齐一局", command=self.quick_match)
        self.quick_button.pack(side="left", padx=(0, 12))
        self.match_buttons = {}
        for title, verb in (("加入匹配", "match"), ("取消排队", "cancel"), ("返回大厅（整场）", "return")):
            button = ttk.Button(actions, text=title, command=lambda v=verb: self.match_selected(v))
            button.pack(side="left", padx=(0, 6))
            self.match_buttons[verb] = button
        self.selection_hint = tk.StringVar(value="支持 Ctrl / Shift 多选")
        ttk.Label(actions, textvariable=self.selection_hint).pack(side="right")
        self.feedback = tk.StringVar(value="操作结果会显示在这里；详细记录在“命令与日志”。")
        ttk.Label(self.dashboard, textvariable=self.feedback, wraplength=1000).pack(fill="x", pady=(0, 6))
        self.tables = {}
        self.pages = {}
        self.make_table(self.dashboard, "players", ("player_id", "status", "world_id", "match_id"),
                        ("玩家 ID", "当前状态", "World", "比赛 ID"), height=8, multiple=True)
        self.tables["players"].bind("<<TreeviewSelect>>", lambda event: self.update_actions())
        lower = ttk.Frame(self.dashboard)
        lower.pack(fill="x", pady=5)
        ttk.Label(lower, text="当前比赛 · 四人自动分两队，创建副本后切换客户端").pack(side="left")
        self.kick_button = ttk.Button(lower, text="踢出单个玩家…", command=self.kick_selected)
        self.kick_button.pack(side="right", padx=(6, 0))
        self.end_button = ttk.Button(lower, text="结束选中比赛并返回大厅", command=self.return_match)
        self.end_button.pack(side="right")
        self.make_table(self.dashboard, "matches", ("id", "state", "worldId", "mapId", "players"),
                        ("比赛 ID", "状态", "World", "地图", "玩家名单"), height=3)
        self.tables["matches"].bind("<<TreeviewSelect>>", lambda event: self.update_actions())
        console_frame = ttk.Frame(self.tabs)
        self.output = ScrolledText(console_frame, wrap="word", font=("Consolas", 11), state="disabled", padx=10, pady=8)
        for key, title, cols in (
                ("worlds", "World", ("world_id", "generation", "map_id", "state", "player_count", "actor_count", "tick_count")),
                ("maps", "地图模板", ("map_id",))):
            page = ttk.Frame(self.tabs)
            self.tabs.add(page, text=title)
            self.pages[key] = page
            self.make_table(page, key, cols, cols)
            if key == "maps":
                button = ttk.Button(page, text="用选中的模板创建副本", command=self.create_selected)
                button.pack(anchor="e", pady=6)
                self.command_buttons.append(button)
        self.console_page = console_frame
        self.tabs.add(console_frame, text="命令与日志")
        entry_frame = ttk.Frame(console_frame, padding=12)
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
        ttk.Label(console_frame, text="高级命令：players  |  matches  |  kick <玩家ID>  |  stop；匹配可直接在首页操作。", padding=(12, 0, 12, 10)).pack(fill="x")
        self.output.pack(fill="both", expand=True)
        self.append(HELP)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.schedule(80, self.drain_events)
        self.schedule(150, self.scan)
        self.schedule(3000, self.refresh_tick)
        self.update_actions()

    def make_table(self, parent, key, columns, titles, height=10, multiple=False):
        frame = self.ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        table = self.ttk.Treeview(frame, columns=columns, show="headings", height=height,
                                  selectmode="extended" if multiple else "browse")
        for col, title in zip(columns, titles):
            table.heading(col, text=title)
            table.column(col, width=140, minwidth=80, stretch=True)
        scrollbar = self.ttk.Scrollbar(frame, command=table.yview)
        table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        table.pack(fill="both", expand=True)
        table.tag_configure("排队中", foreground="#986200")
        table.tag_configure("比赛中", foreground="#176844")
        table.tag_configure("切换中", foreground="#2867ad")
        table.tag_configure("切换失败", foreground="#a42a2a")
        self.tables[key] = table

    def fill_table(self, key, rows, id_key):
        table = self.tables[key]
        selected = table.selection()
        focused = table.focus()
        scroll = table.yview()[0]
        table.delete(*table.get_children())
        for row in rows:
            values = [row.get(col, "") for col in table["columns"]]
            values = [json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value for value in values]
            table.insert("", "end", iid=str(row[id_key]), values=values, tags=(row.get("status", ""),))
        kept = [iid for iid in selected if table.exists(iid)]
        if kept:
            table.selection_set(kept)
        if table.exists(focused):
            table.focus(focused)
        table.yview_moveto(scroll)

    def apply_admin(self, admin):
        if self.refresh_error:
            self.feedback.set("连接已恢复，操作已重新启用。")
        elif self.feedback.get().startswith("正在加载大厅"):
            self.feedback.set("已连接。选择玩家加入匹配，或点击“一键凑齐一局”。")
        elif self.feedback.get().endswith("正在更新大厅状态。"):
            self.feedback.set(self.feedback.get().replace("正在更新大厅状态。", "已更新大厅状态。"))
        self.admin, self.view = admin, lobby_view(admin)
        self.refresh_error = False
        self.fill_table("players", self.view["players"], "player_id")
        self.fill_table("matches", self.view["matches"], "id")
        for key, id_key in (("worlds", "world_id"), ("maps", "map_id")):
            self.fill_table(key, admin.get(key, []), id_key)
        idle = sum(p["can_join"] for p in self.view["players"])
        queued = len(self.view["queued"])
        self.summary.set(f"在线 {len(self.view['players'])} 人   ·   大厅空闲 {idle} 人   ·   排队 {queued} 人   ·   比赛 {len(self.view['matches'])} 场")
        if admin.get("role") != "Dedicated":
            self.hint.set("当前是客户端实例，请在上方选择 Dedicated DS 进行匹配管理。")
        elif not self.view["available"]:
            self.hint.set("此 DS 未启用大厅匹配；请启动带匹配支持的默认大厅。")
        elif not admin.get("accepting_players"):
            self.hint.set("DS 正在停止接收玩家；加入匹配暂不可用。")
        else:
            needed = self.view["needed"]
            self.hint.set("排队人数已足够，等待服务器分配比赛；无需再加入玩家。" if needed == 0 else
                          f"四人自动开局：{'已有 ' + str(queued) + ' 人排队，' if queued else ''}还需 {needed} 人。"
                          + ("点击“一键凑齐一局”，或多选玩家加入匹配。" if idle >= needed else f"请再连接 {needed - idle} 个大厅客户端。"))
        failure = admin.get("matchmaking", {}).get("last_failure") or admin.get("travel", {}).get("last_error")
        if failure:
            self.hint.set(self.hint.get() + f"  服务器最近失败：{failure}")
        self.update_actions()

    def update_actions(self):
        available = bool(self.view and not self.busy and not self.refresh_error and self.selected
                         and self.selected.role == "Dedicated")
        selected = set(self.tables["players"].selection())
        players = [p for p in self.view["players"] if str(p["player_id"]) in selected] if self.view else []
        self.selection_hint.set(f"已选 {len(players)} 人 · Ctrl / Shift 多选")
        for verb, flag in (("match", "can_join"), ("cancel", "can_cancel"), ("return", "can_return")):
            enabled = available and any(p[flag] for p in players)
            self.match_buttons[verb].configure(state="normal" if enabled else "disabled")
        idle = sum(p["can_join"] for p in self.view["players"]) if self.view else 0
        needed = self.view["needed"] if self.view else MATCH_SIZE
        self.quick_button.configure(text=f"一键凑齐一局（补 {needed} 人）",
                                    state="normal" if available and needed > 0 and idle >= needed else "disabled")
        selected_matches = set(self.tables["matches"].selection())
        enabled = available and any(p["can_return"] and str(p["match_id"]) in selected_matches for p in self.view["players"])
        self.end_button.configure(state="normal" if enabled else "disabled")
        self.kick_button.configure(state="normal" if available and len(players) == 1 else "disabled")

    def refresh_tick(self):
        if self.closed:
            return
        if self.auto_refresh.get() and self.selected and not self.busy:
            self.refresh(automatic=True)
        self.schedule(3000, self.refresh_tick)

    def refresh(self, automatic=False):
        if self.busy or self.selected is None:
            return
        client = Client(self.selected)
        def done(admin):
            self.apply_admin(admin)
            self.status.set("更新于 " + time.strftime("%H:%M:%S"))
        def failed(error):
            self.refresh_error = True
            self.feedback.set(f"连接失败，操作已禁用：{error}。可点击“刷新大厅”或重新扫描。")
            if not automatic:
                self.append(f"刷新失败：{error}")
            self.update_actions()
        self.launch(lambda: client.read("admin"), done, failed)

    def close(self):
        self.closed = True
        for timer in self.timers:
            self.root.after_cancel(timer)
        self.timers.clear()
        self.root.destroy()

    def schedule(self, milliseconds, callback):
        def invoke():
            self.timers.discard(timer)
            if not self.closed:
                callback()
        timer = self.root.after(milliseconds, invoke)
        self.timers.add(timer)

    def append(self, message):
        self.output.configure(state="normal")
        self.output.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        if int(self.output.index("end-1c").split(".")[0]) > 5000:
            self.output.delete("1.0", "1001.0")
        self.output.see("end")
        self.output.configure(state="disabled")

    def launch(self, function, done, failed=None):
        if self.busy:
            return
        self.busy = True
        self.status.set("执行中…")
        self.scan_button.configure(state="disabled")
        for button in self.command_buttons:
            button.configure(state="disabled")
        self.update_actions()
        def worker():
            try:
                self.events.put(("done", done, function()))
            except Exception as exc:
                self.events.put(("error", failed, exc))
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
                self.refresh_error = True
                if done:
                    done(result)
                else:
                    self.append(f"错误：{result}")
                    self.feedback.set(f"操作失败：{result}")
                if isinstance(result, IdentityError):
                    self.selected = None
                    self.target.set("实例已失效，请重新扫描选择")
                    self.clear_tables()
            self.update_actions()
        self.schedule(80, self.drain_events)

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
            visible = [i for i in items if not self.only_ds.get() or i.role == "Dedicated"]
            if self.selected is None and len(visible) == 1:
                self.tree.selection_set(str(self.instances.index(visible[0])))
                self.choose()
            elif self.selected is not None:
                self.refresh()
            count = len([i for i in items if i.role == "Dedicated"])
            if self.selected is None:
                self.status.set(f"发现 {count} 台 DS，{len(items)} 个服务")
                self.hint.set("未发现 DS：先启动 StartGame.bat -Clients 4，再点击“扫描本机服务”。" if not visible
                              else "发现多个实例：点击上方需要管理的 DS。")
            self.append(f"扫描完成：{count} 台 DS / {len(items)} 个可连接 AICore 服务")
        self.launch(lambda: scan_instances(int(text) if text else None), done)

    def clear_tables(self):
        self.admin = self.view = None
        for table in self.tables.values():
            table.delete(*table.get_children())
        self.summary.set("尚未连接 DS")
        self.update_actions()

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
        self.feedback.set("正在加载大厅状态…")
        self.tabs.select(self.dashboard)
        self.refresh()

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
            self.tabs.select(self.console_page)
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
            self.feedback.set("请先在上方选择 DS 服务。")
            return
        target = self.selected
        self.append(f"DS[{target.pid} HTTP:{target.http_port}]> {line}")
        client = Client(target, log=lambda text: self.events.put(("log", None, text)))
        def done(result):
            self.append(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
            self.feedback.set(f"命令 {line} 已完成，详细结果见“命令与日志”。")
            key = line.strip().lower()
            if key in ("players", "matches", "queue", "worlds", "maps"):
                self.tabs.select(self.pages.get(key, self.dashboard))
            if isinstance(result, dict) and result.get("state") == "stopped":
                self.selected = None
                self.clear_tables()
                self.target.set("目标 DS 已停止，请扫描选择其他实例")
                self.scan()
            else:
                self.refresh()
        self.launch(lambda: client.execute(line), done)

    def match_selected(self, verb):
        if self.busy or not self.view or self.refresh_error:
            return
        rows = set(self.tables["players"].selection())
        flag = {"match": "can_join", "cancel": "can_cancel", "return": "can_return"}[verb]
        players = [p["player_id"] for p in self.view["players"] if str(p["player_id"]) in rows and p[flag]]
        if not players:
            self.feedback.set("请先选择符合操作状态的玩家；支持 Ctrl / Shift 多选。")
            return
        self.start_batch(verb, players)

    def quick_match(self):
        if self.busy or not self.view or self.refresh_error:
            return
        players = [p["player_id"] for p in self.view["players"] if p["can_join"]]
        needed = self.view["needed"]
        if needed <= 0:
            return
        if len(players) < needed:
            self.feedback.set(f"大厅人数不足，还需要连接 {needed - len(players)} 个客户端。")
            return
        self.start_batch("match", players[:needed])

    def return_match(self):
        if self.busy or not self.view or self.refresh_error:
            return
        matches = set(self.tables["matches"].selection())
        players = [p["player_id"] for p in self.view["players"] if str(p["match_id"]) in matches and p["can_return"]]
        if players:
            self.start_batch("return", players)

    def start_batch(self, verb, players):
        if self.busy or not self.selected or self.selected.role != "Dedicated":
            return
        title = {"match": "加入匹配", "cancel": "取消排队", "return": "返回大厅"}[verb]
        if verb == "return":
            from tkinter import messagebox
            matches = {p["match_id"] for p in self.view["players"] if p["player_id"] in players}
            count = sum(len(m["players"]) for m in self.view["matches"] if m["id"] in matches)
            if not messagebox.askokcancel("结束比赛并返回大厅", f"将结束 {len(matches)} 场比赛，{count} 名成员全部返回大厅。\n是否继续？", parent=self.root):
                return
            if self.busy:
                self.feedback.set("正在刷新，请稍后再点击返回大厅。")
                return
        client = Client(self.selected, log=lambda text: self.events.put(("log", None, text)))
        self.feedback.set(f"正在{title}：{len(players)} 名玩家…")
        self.append(f"DS[{self.selected.pid}] 批量{title}：{players}")
        def done(results):
            self.append(json.dumps(results, ensure_ascii=False, indent=2))
            completed = sum(r["state"] == "completed" for r in results)
            skipped = sum(r["state"] == "skipped" for r in results)
            error = next((r for r in results if r["state"] in ("failed", "unknown")), None)
            self.feedback.set(f"{title}：完成 {completed} 项，跳过 {skipped} 项。"
                              + (f"已停止后续操作：{error['reason']}" if error else "正在更新大厅状态。"))
            self.refresh()
        self.launch(lambda: match_batch(client, verb, players), done)

    def kick_selected(self):
        from tkinter import simpledialog
        if self.busy:
            return
        rows = self.tables["players"].selection()
        if len(rows) != 1:
            self.feedback.set("踢人请只选择一名玩家。")
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
