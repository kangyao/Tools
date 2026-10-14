"""Local, PID-scoped AICore readiness and graceful shutdown."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import ntpath
import os
from http.client import HTTPException
from pathlib import Path
import socket
import tempfile
from typing import Callable, Sequence
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def argument_value(arguments: Sequence[str], name: str) -> str | None:
    for index, argument in enumerate(arguments):
        if argument.casefold() == name.casefold() and index + 1 < len(arguments):
            return arguments[index + 1]
    return None


def available_dev_account(start: int, used: set[int], arguments: Sequence[str], executable: str) -> int:
    """Use the same exclusive dev<N>.lock probe as Start-NetDebug.ps1."""
    project = argument_value(arguments, "-AICoreRoot")
    limit = start + 63
    if project:
        if not ntpath.isabs(project):
            project = ntpath.normpath(ntpath.join(str(Path(executable).resolve().parent), project))
        try:
            payload = json.loads((Path(project) / ".project/account.json").read_text(encoding="utf-8-sig"))
            accounts = payload.get("accounts") if isinstance(payload, dict) else None
            if not isinstance(accounts, list) or not accounts:
                raise ValueError("开发账号文件必须包含非空 accounts 数组。")
            limit = len(accounts)
        except FileNotFoundError:
            pass  # Let the App report its own missing-account configuration.
        except json.JSONDecodeError:
            raise ValueError("开发账号文件 JSON 无效。") from None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    directory = Path(tempfile.gettempdir()) / "miniw_devaccount"
    directory.mkdir(exist_ok=True)
    for index in range(start, limit + 1):
        if index in used:
            continue
        handle = kernel.CreateFileW(str(directory / f"dev{index}.lock"), 0xC0000000, 0, None, 4, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in (32, 33):
                continue
            raise OSError(error, "无法检查开发账号锁")
        kernel.CloseHandle(handle)
        return index
    raise ValueError(f"没有空闲开发账号（从 {start} 开始，账号总数 {limit}）。")


def owned_ports(pid: int, *, udp: bool = False) -> tuple[int, ...]:
    """Read IPv4 owner tables; AICore's management service binds 127.0.0.1."""
    if os.name != "nt":
        return ()
    api = ctypes.WinDLL("iphlpapi", use_last_error=True)
    function = api.GetExtendedUdpTable if udp else api.GetExtendedTcpTable
    function.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                         wintypes.ULONG, ctypes.c_int, wintypes.ULONG)
    function.restype = wintypes.DWORD
    size = wintypes.DWORD()
    table_class = 1 if udp else 3  # OWNER_PID UDP / OWNER_PID_LISTENER TCP
    function(None, ctypes.byref(size), False, 2, table_class, 0)
    if not size.value:
        return ()
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = function(buffer, ctypes.byref(size), False, 2, table_class, 0)
        if result == 122:  # ERROR_INSUFFICIENT_BUFFER: table changed
            continue
        if result:
            return ()
        words = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))
        width, port_index, owner_index = (3, 1, 2) if udp else (6, 2, 5)
        ports = set()
        for index in range(words[0]):
            row = 1 + index * width
            if words[row + owner_index] == pid:
                ports.add(socket.ntohs(words[row + port_index] & 0xFFFF))
        return tuple(sorted(ports))
    return ()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("AICore management redirect refused")


def _http(base: str, route: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(base + route, data=data, headers={"Content-Type": "application/json"})
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=1) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("AICore management response too large")
    result = json.loads(body)
    if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("data"), dict):
        raise ValueError("Invalid AICore management response")
    return result["data"]


def request_stop(pid: int, arguments: Sequence[str], alive: Callable[[], bool], *, working_directory: str | None = None) -> bool:
    """Return true only when this owned live instance accepts runtime.stop."""
    project = argument_value(arguments, "-AICoreRoot")
    if project and working_directory and not ntpath.isabs(project):
        project = ntpath.normpath(ntpath.join(working_directory, project))
    bases = []
    if project:
        for relative in (f".local/AICore/instances/instance-{pid}.json", ".project/AICore/instance.json"):
            try:
                receipt = json.loads((Path(project) / relative).read_text(encoding="utf-8-sig"))
                url = urlsplit(receipt["base_url"])
                if (receipt.get("pid") == pid and url.scheme == "http" and url.hostname == "127.0.0.1"
                        and url.port and url.path in ("", "/") and not url.username and not url.query and not url.fragment):
                    bases.append(f"http://127.0.0.1:{url.port}")
            except (OSError, ValueError, KeyError, TypeError):
                pass
    debug_port = argument_value(arguments, "-lua-debug-port")
    bases.extend(f"http://127.0.0.1:{port}" for port in owned_ports(pid) if str(port) != debug_port)
    for base in dict.fromkeys(bases):
        if not alive():
            return False
        try:
            info = _http(base, "/api/aicore/v1/info")
            if (info.get("pid") != pid or not info.get("instance_id")
                    or (project and ntpath.normcase(ntpath.normpath(str(info.get("project_path", ""))))
                        != ntpath.normcase(ntpath.normpath(project)))):
                continue
            fresh = _http(base, "/api/aicore/v1/info")
            if fresh.get("instance_id") != info["instance_id"] or fresh.get("pid") != pid or not alive():
                continue
            _http(base, "/api/aicore/v1/commands", {"instance_id": info["instance_id"], "operation": "runtime.stop"})
            return True
        except (OSError, ValueError, TypeError, HTTPException):
            continue
    return False
