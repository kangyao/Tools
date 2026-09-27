#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
from typing import Any, BinaryIO, Callable
from urllib.parse import unquote
from xml.etree import ElementTree


TOOL_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TOOL_DIR.parents[1]
SOURCE_DIR = TOOL_DIR.parents[2]
DEFAULT_IDEA_WORKSPACE = PROJECT_DIR / ".idea" / "workspace.xml"
DEFAULT_LUA_ROOT = PROJECT_DIR / "Scripts"
DEFAULT_ADAPTER = (
    SOURCE_DIR
    / "DevelopPythonTools"
    / "LuauDebugAdapter"
    / "out"
    / "debugAdapter.js"
)
DEFAULT_ADAPTER_BOOTSTRAP = TOOL_DIR / "dap_adapter_compat.js"
DEFAULT_NODE = Path(r"C:\Soft\nodejs\node.exe")
JETBRAINS_BREAKPOINT_TOGGLE_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*?"
    r"Toggle line breakpoint request received file: "
    r"XSourcePositionDto\(line=(?P<line>\d+),.*?"
    r"localVirtualFile=(?P<url>file://[^)]+)\).*?"
    r"Request details: hasBreakpoints=(?P<has_breakpoints>true|false)"
)

JsonObject = dict[str, Any]
Logger = Callable[[str], None]
ClientStateListener = Callable[[str | None], None]


def _format_peer(peer: object) -> str:
    if isinstance(peer, tuple) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return str(peer or "unknown")


def _prepare_process() -> None:
    """Apply the process setup that previously lived in the batch wrapper."""
    os.chdir(TOOL_DIR)
    if sys.platform == "win32":
        with suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(  # type: ignore[attr-defined]
                "MiniGamePlay Persistent Debug Server"
            )


class BridgeError(RuntimeError):
    """Persistent ScriptDebugger bridge failed."""


class TargetDisconnected(BridgeError):
    """The current AIFramework ScriptDebugger connection ended."""


@dataclass(frozen=True)
class PersistentDebugOptions:
    dap_host: str = "127.0.0.1"
    dap_port: int = 4711
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 3383
    target_host: str = "127.0.0.1"
    target_port: int = 3382
    runtime_id: str = "minigameplay-main-lua"
    language_id: str = "lua"
    idea_workspace: Path | None = DEFAULT_IDEA_WORKSPACE
    idea_log: Path | None = None
    lua_root: Path = DEFAULT_LUA_ROOT
    node: Path | None = None
    adapter: Path = DEFAULT_ADAPTER
    adapter_bootstrap: Path = DEFAULT_ADAPTER_BOOTSTRAP
    reuse_dap: bool = False


def _workspace_url_to_path(url: str, project_dir: Path) -> Path | None:
    project_prefix = "file://$PROJECT_DIR$/"
    if url.startswith(project_prefix):
        return project_dir / unquote(url[len(project_prefix) :])

    if url.startswith("file:///"):
        return Path(unquote(url[len("file:///") :]))
    if url.startswith("file://"):
        return Path(unquote(url[len("file://") :]))
    return None


def _jetbrains_project_dir(workspace_file: Path) -> Path:
    """Resolve ``$PROJECT_DIR$`` for regular and Rider workspace layouts."""

    idea_dir = workspace_file.parent
    nested_project_dir = idea_dir.parent
    if (
        idea_dir.name.casefold() == ".idea"
        and nested_project_dir.name.casefold().startswith(".idea.")
        and nested_project_dir.parent.name.casefold() == ".idea"
    ):
        return nested_project_dir.parent.parent.resolve()
    return idea_dir.parent.resolve()


def _load_pycharm_lua_breakpoints(
    workspace_file: Path,
    lua_root: Path,
) -> OrderedDict[str, tuple[int, ...]]:
    """Read PyCharm/EmmyLua line breakpoints as ScriptDebugger locations.

    JetBrains stores line numbers as zero-based values, while DAP and the Lua
    runtime use one-based lines. Only breakpoints inside the native ``Scripts``
    tree are imported; non-``lua-line`` breakpoints and unrelated Lua project
    breakpoints are deliberately ignored.
    """
    root = ElementTree.parse(workspace_file).getroot()
    project_dir = _jetbrains_project_dir(workspace_file)
    resolved_lua_root = lua_root.resolve()
    by_source: dict[str, set[int]] = {}

    for breakpoint in root.findall(
        ".//component[@name='XDebuggerManager']"
        "/breakpoint-manager/breakpoints/line-breakpoint"
    ):
        if breakpoint.attrib.get("type") != "lua-line":
            continue
        if breakpoint.attrib.get("enabled", "true").casefold() == "false":
            continue

        url_node = breakpoint.find("url")
        line_node = breakpoint.find("line")
        if url_node is None or line_node is None or not url_node.text:
            continue
        source_file = _workspace_url_to_path(url_node.text, project_dir)
        if source_file is None:
            continue

        try:
            source_path = source_file.resolve().relative_to(
                resolved_lua_root
            ).as_posix()
            line = int(line_node.text or "") + 1
        except (TypeError, ValueError):
            continue
        if line <= 0 or not source_file.is_file():
            continue
        by_source.setdefault(source_path, set()).add(line)

    return OrderedDict(
        (source_path, tuple(sorted(lines)))
        for source_path, lines in sorted(by_source.items())
    )


def _discover_idea_workspace(
    preferred: Path,
    lua_root: Path,
) -> Path:
    """Find the active JetBrains workspace that owns AIGamePlay breakpoints.

    PyCharm usually stores ``workspace.xml`` directly below the opened
    project's ``.idea`` directory. Rider stores it below a nested solution
    directory, while AIGamePlay itself is commonly attached from the sibling
    ``Projects`` tree. Prefer a workspace that currently contains applicable
    Lua breakpoints, then the most recently written candidate.
    """

    candidates: list[Path] = [preferred]
    resolved_lua_root = lua_root.resolve()
    for ancestor in (resolved_lua_root, *resolved_lua_root.parents):
        projects_dir = ancestor / "Projects"
        if not projects_dir.is_dir():
            continue
        candidates.extend(projects_dir.glob("*/.idea/workspace.xml"))
        candidates.extend(
            projects_dir.glob("*/.idea/.idea.*/.idea/workspace.xml")
        )

    unique_candidates: dict[str, Path] = {}
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).casefold()
        except OSError:
            key = str(candidate.absolute()).casefold()
        unique_candidates.setdefault(key, candidate)

    ranked: list[tuple[int, int, Path]] = []
    for candidate in unique_candidates.values():
        if not candidate.is_file():
            continue
        try:
            breakpoints = _load_pycharm_lua_breakpoints(
                candidate,
                resolved_lua_root,
            )
            breakpoint_count = sum(len(lines) for lines in breakpoints.values())
            modified_ns = candidate.stat().st_mtime_ns
        except (OSError, ElementTree.ParseError):
            continue
        ranked.append((breakpoint_count, modified_ns, candidate))

    if not ranked:
        return preferred
    return max(ranked, key=lambda item: (item[0] > 0, item[1]))[2]


def _discover_jetbrains_idea_log(lua_root: Path) -> Path | None:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    jetbrains_dir = Path(local_appdata) / "JetBrains"
    if not jetbrains_dir.is_dir():
        return None

    candidates: list[Path] = []
    for product_dir in jetbrains_dir.iterdir():
        product_name = product_dir.name.casefold()
        if not product_name.startswith(("rider", "pycharm", "webstorm")):
            continue
        idea_log = product_dir / "log" / "idea.log"
        if idea_log.is_file():
            candidates.append(idea_log)
    if not candidates:
        return None

    lua_root_marker = (
        str(lua_root.resolve()).replace("\\", "/").casefold().encode("utf-8")
    )

    def rank(candidate: Path) -> tuple[bool, int]:
        try:
            stat = candidate.stat()
            with candidate.open("rb") as stream:
                stream.seek(max(0, stat.st_size - 4 * 1024 * 1024))
                tail = stream.read().replace(b"\\", b"/").lower()
            return lua_root_marker in tail, stat.st_mtime_ns
        except OSError:
            return False, 0

    return max(candidates, key=rank)


def _parse_jetbrains_breakpoint_toggle(
    line: str,
    lua_root: Path,
) -> tuple[str, int, bool, float] | None:
    match = JETBRAINS_BREAKPOINT_TOGGLE_PATTERN.search(line)
    if match is None:
        return None
    try:
        event_time = datetime.strptime(
            match.group("timestamp"),
            "%Y-%m-%d %H:%M:%S,%f",
        ).timestamp()
        source_file = _workspace_url_to_path(
            match.group("url"),
            lua_root.parent,
        )
        if source_file is None:
            return None
        source_path = source_file.resolve().relative_to(
            lua_root.resolve()
        ).as_posix()
        line_number = int(match.group("line")) + 1
    except (OSError, ValueError):
        return None
    if line_number <= 0:
        return None

    # ``hasBreakpoints`` describes the state before the gutter click. A click
    # on an occupied line removes its breakpoint; a click on an empty line
    # adds one. Treat both EmmyLua and DAP variants as one runtime location.
    present = match.group("has_breakpoints") == "false"
    return source_path, line_number, present, event_time


class PersistentScriptDebugBridge:
    """Keep the adapter socket alive while AIFramework processes restart.

    The Luau debug adapter normally connects directly to AIFramework. Its socket
    close event becomes a DAP ``terminated`` event, so PyCharm/WebStorm drops
    the whole debug session whenever the executable exits. This bridge sits
    between them:

        LuauDebugAdapter -> bridge -> AIFramework ScriptDebugger

    The adapter-facing socket stays open. Breakpoint/configuration requests are
    cached while the target is offline and replayed before a replacement target
    is released from ``-script-debug-wait-client``.
    """

    _INTERNAL_REQUEST_ID_START = 1_000_000_000
    _CONTROL_COMMANDS = frozenset(
        {"continue", "next", "stepIn", "stepOut", "pause"}
    )

    def __init__(
        self,
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 3383,
        target_host: str = "127.0.0.1",
        target_port: int = 3382,
        runtime_id: str = "minigameplay-main-lua",
        language_id: str = "lua",
        idea_workspace: Path | None = None,
        idea_log: Path | None = None,
        lua_root: Path = DEFAULT_LUA_ROOT,
        breakpoint_refresh_interval: float = 0.5,
        reconnect_interval: float = 0.5,
        request_timeout: float = 5.0,
        logger: Logger | None = None,
        on_client_changed: ClientStateListener | None = None,
        on_target_changed: ClientStateListener | None = None,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.runtime_id = runtime_id
        self.language_id = language_id
        self.idea_workspace = idea_workspace
        self.idea_log = idea_log
        self.lua_root = lua_root
        self.breakpoint_refresh_interval = breakpoint_refresh_interval
        self.reconnect_interval = reconnect_interval
        self.request_timeout = request_timeout
        self.log = logger or print
        self.on_client_changed = on_client_changed
        self.on_target_changed = on_target_changed

        self.metadata: JsonObject = {
            "runtimeId": runtime_id,
            "languageId": language_id,
            "displayName": runtime_id,
            "runtimeAlive": False,
            "supportsSourceMap": True,
        }

        self._server: asyncio.AbstractServer | None = None
        self._connector_task: asyncio.Task[None] | None = None
        self._breakpoint_watcher_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

        self._frontend_reader: asyncio.StreamReader | None = None
        self._frontend_writer: asyncio.StreamWriter | None = None
        self._frontend_write_lock = asyncio.Lock()
        self._frontend_client: str | None = None

        self._target_reader: asyncio.StreamReader | None = None
        self._target_writer: asyncio.StreamWriter | None = None
        self._target_write_lock = asyncio.Lock()
        self._target_ready = False
        self._target_client: str | None = None
        self._target_stopped = False
        self._frontend_detaching = False

        self._breakpoints: OrderedDict[str, JsonObject] = OrderedDict()
        self._frontend_breakpoints_active = False
        self._pycharm_breakpoints_active = False
        self._pycharm_breakpoints: OrderedDict[str, tuple[int, ...]] = (
            OrderedDict()
        )
        self._idea_log_identity: tuple[int, int] | None = None
        self._idea_log_offset = 0
        self._idea_log_partial = b""
        self._idea_log_overrides: dict[
            tuple[str, int], tuple[float, bool]
        ] = {}
        # The engine keeps breakpoints after a TCP client disconnects.  Keep a
        # session-independent list so a later frontend can clear stale entries
        # left by an earlier IDE session.
        self._known_breakpoint_sources: set[str] = set()
        self._configuration_done = False
        self._state_version = 0

        self._next_internal_request_id = self._INTERNAL_REQUEST_ID_START
        self._internal_pending: dict[int, asyncio.Future[JsonObject]] = {}
        self._frontend_pending: dict[int, str] = {}

    @property
    def bound_port(self) -> int:
        if not self._server or not self._server.sockets:
            return self.listen_port
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def target_ready(self) -> bool:
        return self._target_ready

    @property
    def frontend_client(self) -> str | None:
        return self._frontend_client

    @property
    def target_client(self) -> str | None:
        return self._target_client

    def _set_frontend_client(self, client: str | None) -> None:
        if client == self._frontend_client:
            return
        self._frontend_client = client
        if self.on_client_changed is not None:
            self.on_client_changed(client)

    def _set_target_client(self, client: str | None) -> None:
        if client == self._target_client:
            return
        self._target_client = client
        if self.on_target_changed is not None:
            self.on_target_changed(client)

    async def start(self) -> None:
        self._refresh_pycharm_breakpoints()
        self._server = await asyncio.start_server(
            self._handle_frontend,
            self.listen_host,
            self.listen_port,
        )
        self._connector_task = asyncio.create_task(
            self._target_connector_loop(),
            name="script-debug-target-connector",
        )
        if self.idea_workspace is not None or self.idea_log is not None:
            self._breakpoint_watcher_task = asyncio.create_task(
                self._pycharm_breakpoint_watch_loop(),
                name="jetbrains-lua-breakpoint-watcher",
            )
        self.log(
            f"ScriptDebugger bridge listening on "
            f"{self.listen_host}:{self.bound_port}; target "
            f"{self.target_host}:{self.target_port}"
        )

    async def close(self) -> None:
        self._stopping.set()
        if self._breakpoint_watcher_task:
            self._breakpoint_watcher_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._breakpoint_watcher_task
            self._breakpoint_watcher_task = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        await self._close_target()
        await self._close_frontend()

        if self._connector_task:
            self._connector_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._connector_task
            self._connector_task = None

    def _refresh_pycharm_breakpoints(self) -> tuple[str, ...]:
        workspace_file = self.idea_workspace
        workspace_modified_at: float | None = None
        if workspace_file is None or not workspace_file.is_file():
            workspace_breakpoints: OrderedDict[str, tuple[int, ...]] = (
                OrderedDict()
            )
        else:
            try:
                workspace_breakpoints = _load_pycharm_lua_breakpoints(
                    workspace_file,
                    self.lua_root,
                )
                workspace_modified_at = workspace_file.stat().st_mtime
            except (OSError, ElementTree.ParseError) as exc:
                self.log(
                    f"[persistent-debug] Could not read JetBrains Lua "
                    f"breakpoints: {exc}"
                )
                return ()

        self._refresh_idea_log_breakpoint_overrides(workspace_modified_at)
        new_breakpoints = self._apply_idea_log_breakpoint_overrides(
            workspace_breakpoints,
            workspace_modified_at,
        )

        activated_pycharm_breakpoints = (
            bool(new_breakpoints) and not self._pycharm_breakpoints_active
        )
        if new_breakpoints:
            self._pycharm_breakpoints_active = True

        if (
            new_breakpoints == self._pycharm_breakpoints
            and not activated_pycharm_breakpoints
        ):
            return ()

        old_breakpoints = self._pycharm_breakpoints
        changed_source_set = {
            source_path
            for source_path in set(old_breakpoints) | set(new_breakpoints)
            if old_breakpoints.get(source_path) != new_breakpoints.get(source_path)
        }
        if activated_pycharm_breakpoints:
            changed_source_set.update(self._breakpoints)
            self.log(
                "[persistent-debug] JetBrains Lua breakpoints are now "
                "authoritative for this IDE session"
            )
        changed_sources = tuple(
            sorted(
                changed_source_set
            )
        )
        self._pycharm_breakpoints = new_breakpoints
        self._known_breakpoint_sources.update(changed_sources)
        self._state_version += 1
        count = sum(len(lines) for lines in new_breakpoints.values())
        self.log(
            f"[persistent-debug] Loaded {count} JetBrains Lua breakpoint(s) "
            f"from {len(new_breakpoints)} source file(s)"
        )
        return changed_sources

    def _refresh_idea_log_breakpoint_overrides(
        self,
        workspace_modified_at: float | None,
    ) -> None:
        idea_log = self.idea_log
        if idea_log is None or not idea_log.is_file():
            return
        try:
            stat = idea_log.stat()
            identity = (stat.st_dev, stat.st_ino)
            reset = (
                self._idea_log_identity != identity
                or stat.st_size < self._idea_log_offset
            )
            if reset:
                self._idea_log_identity = identity
                self._idea_log_offset = 0
                self._idea_log_partial = b""
                self._idea_log_overrides.clear()
                if workspace_modified_at is None:
                    self._idea_log_offset = stat.st_size
                    return

            with idea_log.open("rb") as stream:
                stream.seek(self._idea_log_offset)
                payload = self._idea_log_partial + stream.read()
                self._idea_log_offset = stream.tell()
        except OSError:
            return

        lines = payload.split(b"\n")
        self._idea_log_partial = lines.pop() if lines else b""
        for raw_line in lines:
            event = _parse_jetbrains_breakpoint_toggle(
                raw_line.decode("utf-8", errors="replace"),
                self.lua_root,
            )
            if event is None:
                continue
            source_path, line_number, present, event_time = event
            if (
                workspace_modified_at is not None
                and event_time <= workspace_modified_at
            ):
                continue
            self._idea_log_overrides[(source_path, line_number)] = (
                event_time,
                present,
            )

    def _apply_idea_log_breakpoint_overrides(
        self,
        workspace_breakpoints: OrderedDict[str, tuple[int, ...]],
        workspace_modified_at: float | None,
    ) -> OrderedDict[str, tuple[int, ...]]:
        by_source = {
            source_path: set(lines)
            for source_path, lines in workspace_breakpoints.items()
        }
        expired = []
        for key, (event_time, present) in self._idea_log_overrides.items():
            if (
                workspace_modified_at is not None
                and event_time <= workspace_modified_at
            ):
                expired.append(key)
                continue
            source_path, line_number = key
            lines = by_source.setdefault(source_path, set())
            if present:
                lines.add(line_number)
            else:
                lines.discard(line_number)
        for key in expired:
            self._idea_log_overrides.pop(key, None)
        return OrderedDict(
            (source_path, tuple(sorted(lines)))
            for source_path, lines in sorted(by_source.items())
        )

    async def _pycharm_breakpoint_watch_loop(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(self.breakpoint_refresh_interval)
            changed_sources = self._refresh_pycharm_breakpoints()
            if (
                not changed_sources
                or not self._target_ready
                or self._target_writer is None
            ):
                continue

            try:
                for source_path in changed_sources:
                    await self._send_internal_request(
                        "setBreakpoints",
                        self._merged_breakpoint_arguments(source_path),
                    )
                    self.log(
                        f"[persistent-debug] Applied JetBrains Lua "
                        f"breakpoints: {source_path} -> "
                        f"{list(self._merged_breakpoint_lines(source_path))}"
                    )
            except (BridgeError, ConnectionError, OSError, TimeoutError):
                # Target reconnect replay uses the latest workspace snapshot.
                continue

    def _frontend_breakpoint_lines(self, source_path: str) -> tuple[int, ...]:
        request = self._breakpoints.get(source_path)
        arguments = request.get("arguments", {}) if request else {}
        lines = arguments.get("lines", []) if isinstance(arguments, dict) else []
        return tuple(line for line in lines if isinstance(line, int))

    def _merged_breakpoint_lines(self, source_path: str) -> tuple[int, ...]:
        # EmmyLua's ordinary gutter click creates ``lua-line`` markers even
        # while LSP4IJ owns the active DAP session. Once a live workspace has
        # supplied such breakpoints, keep that complete workspace snapshot
        # authoritative so stale LSP4IJ ``dap-breakpoint`` markers cannot mask
        # or resurrect locations that are invisible in the editor.
        if self._pycharm_breakpoints_active:
            return self._pycharm_breakpoints.get(source_path, ())
        if self._frontend_breakpoints_active:
            return self._frontend_breakpoint_lines(source_path)
        return ()

    def _merged_breakpoint_arguments(self, source_path: str) -> JsonObject:
        request = self._breakpoints.get(source_path)
        cached = request.get("arguments", {}) if request else {}
        arguments = dict(cached) if isinstance(cached, dict) else {}
        arguments["sourcePath"] = source_path
        arguments["lines"] = list(self._merged_breakpoint_lines(source_path))
        return arguments

    def _breakpoint_snapshots(self) -> list[JsonObject]:
        sources = list(self._breakpoints)
        sources.extend(
            source_path
            for source_path in self._pycharm_breakpoints
            if source_path not in self._breakpoints
        )
        return [
            self._merged_breakpoint_arguments(source_path)
            for source_path in sources
            if source_path
        ]

    async def _handle_frontend(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        if self._frontend_writer is not None:
            self.log(f"Rejected second adapter connection from {peer}")
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return

        self._frontend_reader = reader
        self._frontend_writer = writer
        self._set_frontend_client(_format_peer(peer))
        self._frontend_detaching = False
        self._breakpoints.clear()
        self._frontend_breakpoints_active = False
        self._configuration_done = False
        self._state_version += 1
        self.log(f"Debug adapter connected from {peer}")

        try:
            while not self._stopping.is_set():
                line = await reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self.log(f"Ignored invalid adapter message: {exc}")
                    continue
                if isinstance(message, dict):
                    await self._handle_frontend_message(message)
        finally:
            if self._frontend_writer is writer:
                self.log("Debug adapter disconnected")
                self._frontend_reader = None
                self._frontend_writer = None
                self._set_frontend_client(None)
                self._breakpoints.clear()
                self._frontend_breakpoints_active = False
                self._configuration_done = False
                self._state_version += 1
                self._frontend_pending.clear()
                await self._detach_target()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _handle_frontend_message(self, message: JsonObject) -> None:
        if "id" not in message or "command" not in message:
            if self._target_ready:
                await self._write_target(message)
            return

        request_id = int(message["id"])
        command = str(message["command"])
        arguments = message.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}

        if command == "initialize":
            await self._send_frontend_response(
                request_id,
                success=True,
                body=dict(self.metadata),
            )
            return

        if command == "setBreakpoints":
            source_path = str(arguments.get("sourcePath", ""))
            activated_frontend_breakpoints = (
                not self._frontend_breakpoints_active
            )
            self._frontend_breakpoints_active = True
            self._breakpoints[source_path] = {
                "id": request_id,
                "command": command,
                "arguments": dict(arguments),
            }
            if source_path:
                self._known_breakpoint_sources.add(source_path)
            self._state_version += 1
            if (
                activated_frontend_breakpoints
                and not self._pycharm_breakpoints_active
            ):
                self.log(
                    "[persistent-debug] Frontend DAP breakpoints are now "
                    "authoritative for this IDE session"
                )
            self.log(
                f"[persistent-debug] DAP breakpoints updated: "
                f"{source_path} -> {arguments.get('lines', [])}"
            )

        elif command == "configurationDone":
            self._configuration_done = True
            self._state_version += 1

        elif command == "disconnect":
            await self._detach_target()
            await self._send_frontend_response(request_id, success=True, body={})
            return

        if self._target_ready and self._target_writer is not None:
            if (
                command == "setBreakpoints"
                and activated_frontend_breakpoints
                and not self._pycharm_breakpoints_active
            ):
                try:
                    for stale_source in sorted(
                        set(self._pycharm_breakpoints) - {source_path}
                    ):
                        await self._send_internal_request(
                            "setBreakpoints",
                            {"sourcePath": stale_source, "lines": []},
                        )
                except (BridgeError, ConnectionError, OSError, TimeoutError):
                    # A reconnect replays the authoritative DAP snapshot and
                    # clears every workspace-only source.
                    pass
            if command in {"continue", "next", "stepIn", "stepOut"}:
                self._target_stopped = False
            if command in self._CONTROL_COMMANDS:
                self.log(
                    f"[persistent-debug] Forwarding debugger control: {command}"
                )
            self._frontend_pending[request_id] = command
            try:
                forwarded_message = message
                if command == "setBreakpoints":
                    forwarded_message = {
                        **message,
                        "arguments": self._merged_breakpoint_arguments(
                            source_path
                        ),
                    }
                await self._write_target(forwarded_message)
            except (ConnectionError, OSError) as exc:
                self._frontend_pending.pop(request_id, None)
                await self._send_frontend_response(
                    request_id,
                    success=False,
                    message=f"AIFramework disconnected: {exc}",
                )
            return

        await self._send_offline_response(request_id, command, arguments)

    async def _send_offline_response(
        self,
        request_id: int,
        command: str,
        arguments: JsonObject,
    ) -> None:
        if command in self._CONTROL_COMMANDS:
            self.log(
                f"[persistent-debug] Ignored debugger control while "
                f"AIFramework is offline: {command}"
            )
        if command == "setBreakpoints":
            lines = arguments.get("lines")
            if not isinstance(lines, list):
                lines = []
            body = {
                "breakpoints": [
                    {"verified": True, "line": line}
                    for line in lines
                    if isinstance(line, int)
                ]
            }
        elif command == "scopes":
            body = {"scopes": []}
        elif command == "variables":
            body = {"variables": []}
        else:
            # Control requests are acknowledged while offline. The adapter does
            # not await several of these promises, so returning an error would
            # produce noisy unhandled rejections without helping the IDE.
            body = {}
        await self._send_frontend_response(request_id, success=True, body=body)

    async def _target_connector_loop(self) -> None:
        waiting_reported = False
        offline_reported = False
        while not self._stopping.is_set():
            if self._frontend_writer is None or self._frontend_detaching:
                waiting_reported = False
                offline_reported = False
                await asyncio.sleep(0.1)
                continue

            if not waiting_reported:
                self.log(
                    f"Waiting for AIFramework on "
                    f"{self.target_host}:{self.target_port}"
                )
                waiting_reported = True

            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.target_host, self.target_port),
                    timeout=max(1.0, self.reconnect_interval * 2),
                )
            except (TimeoutError, ConnectionError, OSError):
                await asyncio.sleep(self.reconnect_interval)
                continue

            if (
                self._frontend_writer is None
                or self._frontend_detaching
                or self._stopping.is_set()
            ):
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                continue

            self._target_reader = reader
            self._target_writer = writer
            self._target_ready = False
            self._target_stopped = False
            target_read_task = asyncio.create_task(
                self._read_target(reader),
                name="script-debug-target-reader",
            )

            try:
                await self._synchronize_target()
                self._target_ready = True
                self._set_target_client(
                    f"{self.target_host}:{self.target_port}"
                )
                waiting_reported = False
                offline_reported = False
                count = sum(
                    len(arguments.get("lines", []))
                    for arguments in self._breakpoint_snapshots()
                )
                text = (
                    f"[persistent-debug] AIFramework connected; "
                    f"restored {count} breakpoint(s).\n"
                )
                self.log(text.rstrip())
                await self._send_output(text)
                await target_read_task
            except asyncio.CancelledError:
                target_read_task.cancel()
                raise
            except (BridgeError, ConnectionError, OSError, TimeoutError) as exc:
                self.log(f"AIFramework connection ended: {exc}")
            finally:
                self._target_ready = False
                self._set_target_client(None)
                if not target_read_task.done():
                    target_read_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await target_read_task
                await self._finish_target_connection(writer)

            if (
                self._frontend_writer is not None
                and not self._frontend_detaching
                and not self._stopping.is_set()
            ):
                if not offline_reported:
                    await self._send_output(
                        "[persistent-debug] AIFramework offline; "
                        "waiting for restart.\n"
                    )
                    offline_reported = True
                await asyncio.sleep(self.reconnect_interval)

    async def _synchronize_target(self) -> None:
        self._refresh_pycharm_breakpoints()
        metadata = await self._send_internal_request("initialize", {})
        actual_runtime_id = str(metadata.get("runtimeId", ""))
        actual_language_id = str(metadata.get("languageId", ""))
        if actual_runtime_id != self.runtime_id:
            raise BridgeError(
                f"runtimeId mismatch: expected {self.runtime_id}, "
                f"actual {actual_runtime_id}"
            )
        if actual_language_id != self.language_id:
            raise BridgeError(
                f"languageId mismatch: expected {self.language_id}, "
                f"actual {actual_language_id}"
            )
        if metadata.get("runtimeAlive") is False:
            raise BridgeError(
                "AIFramework debugger backend is detached; restart "
                "AIFramework"
            )

        self.metadata = {
            **self.metadata,
            **metadata,
            "runtimeAlive": True,
        }

        # A breakpoint may be edited while a new target is being initialized.
        # Repeat the snapshot until no cached state changed during the replay.
        while True:
            version = self._state_version
            breakpoint_snapshots = self._breakpoint_snapshots()
            active_sources = {
                str(arguments.get("sourcePath", ""))
                for arguments in breakpoint_snapshots
                if arguments.get("sourcePath")
            }
            stale_sources = sorted(
                self._known_breakpoint_sources - active_sources
            )
            has_active_breakpoints = any(
                isinstance(arguments.get("lines"), list)
                and bool(arguments.get("lines"))
                for arguments in breakpoint_snapshots
            )
            configuration_done = self._configuration_done

            for source_path in stale_sources:
                await self._send_internal_request(
                    "setBreakpoints",
                    {"sourcePath": source_path, "lines": []},
                )
            for arguments in breakpoint_snapshots:
                await self._send_internal_request("setBreakpoints", arguments)
            if configuration_done:
                await self._send_internal_request("configurationDone", {})

            if version == self._state_version:
                if stale_sources and not has_active_breakpoints:
                    await self._send_internal_request("continue", {})
                    self.log(
                        "Cleared stale breakpoints and resumed AIFramework "
                        "for the new debugger session"
                    )
                return

    async def _read_target(self, reader: asyncio.StreamReader) -> None:
        while not self._stopping.is_set():
            line = await reader.readline()
            if not line:
                raise TargetDisconnected("target socket closed")
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.log(f"Ignored invalid target message: {exc}")
                continue
            if not isinstance(message, dict):
                continue

            if message.get("type") == "response":
                request_id = int(message.get("requestId", -1))
                future = self._internal_pending.pop(request_id, None)
                if future is not None:
                    if message.get("success") is False:
                        future.set_exception(
                            BridgeError(
                                str(message.get("message", "target request failed"))
                            )
                        )
                    else:
                        body = message.get("body")
                        future.set_result(body if isinstance(body, dict) else {})
                    continue

                command = self._frontend_pending.pop(request_id, None)
                if command is not None:
                    if command in self._CONTROL_COMMANDS:
                        success = message.get("success") is not False
                        result = "acknowledged" if success else "rejected"
                        self.log(
                            f"[persistent-debug] AIFramework {result} "
                            f"debugger control: {command}"
                        )
                    await self._write_frontend(message)
                continue

            if message.get("type") == "event":
                event = str(message.get("event", ""))
                if event == "stopped":
                    self._target_stopped = True
                    body = message.get("body")
                    if not isinstance(body, dict):
                        body = {}
                    reason = str(body.get("reason", "breakpoint"))
                    source = str(body.get("sourcePath", ""))
                    line = body.get("line", "?")
                    stack = body.get("stack")
                    valid_line = (
                        isinstance(line, int)
                        and not isinstance(line, bool)
                        and line > 0
                    )
                    if (
                        (not isinstance(stack, list) or not stack)
                        and source
                        and valid_line
                    ):
                        body = {
                            **body,
                            "stack": [
                                {
                                    "id": 0,
                                    "name": str(body.get("name", "<script>")),
                                    "sourcePath": source,
                                    "line": line,
                                    "column": 1,
                                }
                            ],
                        }
                        message = {**message, "body": body}
                        self.log(
                            "[persistent-debug] Restored the stopped source "
                            "frame from event location"
                        )
                    location = f"{source}:{line}" if source else str(line)
                    self.log(
                        f"[persistent-debug] AIFramework stopped "
                        f"({reason}) at {location}"
                    )
                elif event in {"continued", "disconnect"}:
                    self._target_stopped = False
                if event == "disconnect":
                    raise TargetDisconnected("target sent disconnect event")

            await self._write_frontend(message)

    async def _send_internal_request(
        self,
        command: str,
        arguments: JsonObject,
    ) -> JsonObject:
        if self._target_writer is None:
            raise TargetDisconnected("target is not connected")

        request_id = self._next_internal_request_id
        self._next_internal_request_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[JsonObject] = loop.create_future()
        self._internal_pending[request_id] = future
        try:
            await self._write_target(
                {
                    "id": request_id,
                    "command": command,
                    "arguments": arguments,
                }
            )
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        finally:
            self._internal_pending.pop(request_id, None)

    async def _send_frontend_response(
        self,
        request_id: int,
        *,
        success: bool,
        body: JsonObject | None = None,
        message: str | None = None,
    ) -> None:
        response: JsonObject = {
            "type": "response",
            "requestId": request_id,
            "success": success,
            "body": body or {},
        }
        if message:
            response["message"] = message
        await self._write_frontend(response)

    async def _send_output(self, text: str) -> None:
        await self._write_frontend(
            {
                "type": "event",
                "event": "output",
                "body": {"category": "console", "text": text},
            }
        )

    async def _write_frontend(self, message: JsonObject) -> None:
        writer = self._frontend_writer
        if writer is None or writer.is_closing():
            return
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._frontend_write_lock:
            writer.write(payload)
            await writer.drain()

    async def _write_target(self, message: JsonObject) -> None:
        writer = self._target_writer
        if writer is None or writer.is_closing():
            raise TargetDisconnected("target is not connected")
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._target_write_lock:
            writer.write(payload)
            await writer.drain()

    async def _finish_target_connection(
        self,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._target_writer is writer:
            self._target_writer = None
            self._target_reader = None
            self._target_stopped = False
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()

        error = TargetDisconnected("AIFramework disconnected")
        for future in tuple(self._internal_pending.values()):
            if not future.done():
                future.set_exception(error)
        self._internal_pending.clear()

        pending = tuple(self._frontend_pending.items())
        self._frontend_pending.clear()
        for request_id, _command in pending:
            await self._send_frontend_response(
                request_id,
                success=False,
                message=str(error),
            )

    async def _close_target(self) -> None:
        self._target_ready = False
        self._set_target_client(None)
        self._target_stopped = False
        writer = self._target_writer
        self._target_writer = None
        self._target_reader = None
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _detach_target(self) -> None:
        """Resume a paused target, then release only its TCP connection.

        The engine's ``disconnect`` command permanently destroys its debugger
        backend.  A persistent IDE session must therefore never send that
        command when the IDE temporarily detaches; closing the socket keeps
        the backend available for the next adapter connection.
        """
        if self._frontend_detaching:
            return
        self._frontend_detaching = True

        writer = self._target_writer
        if writer is None or writer.is_closing():
            await self._close_target()
            return

        resumed = False
        cleared_sources = 0
        for source_path in sorted(self._known_breakpoint_sources):
            try:
                await asyncio.wait_for(
                    self._send_internal_request(
                        "setBreakpoints",
                        {"sourcePath": source_path, "lines": []},
                    ),
                    timeout=1.0,
                )
                cleared_sources += 1
            except (BridgeError, ConnectionError, OSError, TimeoutError):
                break

        try:
            # This is deliberately unconditional.  The frontend can disappear
            # just before the target's ``stopped`` event reaches the bridge,
            # so _target_stopped alone cannot safely decide whether a resume
            # is required.  Continue is harmless when the runtime is running.
            await asyncio.wait_for(
                self._send_internal_request("continue", {}),
                timeout=1.0,
            )
            resumed = True
        except (BridgeError, ConnectionError, OSError, TimeoutError):
            pass

        await self._close_target()

        if cleared_sources:
            self.log(
                f"Cleared breakpoints for {cleared_sources} source file(s) "
                f"before debugger detach"
            )
        if resumed:
            self.log("Ensured AIFramework is running before debugger detach")

    async def _close_frontend(self) -> None:
        writer = self._frontend_writer
        self._frontend_writer = None
        self._frontend_reader = None
        self._set_frontend_client(None)
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


def _default_node_path() -> Path:
    if DEFAULT_NODE.is_file():
        return DEFAULT_NODE
    found = shutil.which("node")
    return Path(found) if found else DEFAULT_NODE


def _port_is_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


async def _pump_adapter_output(
    stream: BinaryIO,
    logger: Logger,
    on_dap_client_changed: ClientStateListener | None = None,
) -> None:
    """Drain adapter output so a pythonw parent never leaves invalid stdio."""

    client_connected = False
    try:
        while True:
            raw_line = await asyncio.to_thread(stream.readline)
            if not raw_line:
                return
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            logger(f"[DAP adapter] {line}")
            if "accepted connection from client" in line:
                if not client_connected and on_dap_client_changed is not None:
                    on_dap_client_changed("IDE")
                client_connected = True
            elif "client connection closed" in line:
                if client_connected and on_dap_client_changed is not None:
                    on_dap_client_changed(None)
                client_connected = False
    finally:
        if client_connected and on_dap_client_changed is not None:
            on_dap_client_changed(None)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Keep PyCharm/WebStorm's DAP session alive while AIFramework "
            "restarts, and replay cached breakpoints to each new process."
        )
    )
    parser.add_argument("--dap-host", default="127.0.0.1")
    parser.add_argument("--dap-port", type=int, default=4711)
    parser.add_argument("--bridge-host", default="127.0.0.1")
    parser.add_argument("--bridge-port", type=int, default=3383)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=3382)
    parser.add_argument("--runtime-id", default="minigameplay-main-lua")
    parser.add_argument("--language-id", default="lua")
    parser.add_argument(
        "--idea-workspace",
        type=Path,
        default=DEFAULT_IDEA_WORKSPACE,
        help="PyCharm workspace.xml used to synchronize EmmyLua breakpoints.",
    )
    parser.add_argument(
        "--idea-log",
        type=Path,
        help="JetBrains idea.log used for immediate breakpoint updates.",
    )
    parser.add_argument("--lua-root", type=Path, default=DEFAULT_LUA_ROOT)
    parser.add_argument("--node", type=Path, default=_default_node_path())
    parser.add_argument("--adapter", type=Path, default=DEFAULT_ADAPTER)
    parser.add_argument(
        "--adapter-bootstrap",
        type=Path,
        default=DEFAULT_ADAPTER_BOOTSTRAP,
    )
    parser.add_argument(
        "--reuse-dap",
        action="store_true",
        help="Do not launch Node when dap-port is already owned by another server.",
    )
    return parser.parse_args(argv)


def _options_from_args(args: argparse.Namespace) -> PersistentDebugOptions:
    return PersistentDebugOptions(
        dap_host=args.dap_host,
        dap_port=args.dap_port,
        bridge_host=args.bridge_host,
        bridge_port=args.bridge_port,
        target_host=args.target_host,
        target_port=args.target_port,
        runtime_id=args.runtime_id,
        language_id=args.language_id,
        idea_workspace=args.idea_workspace,
        idea_log=args.idea_log,
        lua_root=args.lua_root,
        node=args.node,
        adapter=args.adapter,
        adapter_bootstrap=args.adapter_bootstrap,
        reuse_dap=args.reuse_dap,
    )


async def _run(
    options: PersistentDebugOptions,
    *,
    stop_event: threading.Event | None = None,
    logger: Logger | None = None,
    on_ready: Callable[[int | None], None] | None = None,
    on_client_changed: ClientStateListener | None = None,
    on_target_changed: ClientStateListener | None = None,
    on_dap_client_changed: ClientStateListener | None = None,
) -> None:
    log = logger or (lambda message: print(message, flush=True))
    stop_requested = stop_event or threading.Event()

    idea_workspace = options.idea_workspace
    if idea_workspace == DEFAULT_IDEA_WORKSPACE:
        discovered_workspace = _discover_idea_workspace(
            idea_workspace,
            options.lua_root,
        )
        if discovered_workspace != idea_workspace:
            log(
                "[persistent-debug] Using JetBrains workspace for Lua "
                f"breakpoints: {discovered_workspace}"
            )
        idea_workspace = discovered_workspace

    idea_log = options.idea_log
    if idea_workspace is not None and idea_log is None:
        idea_log = _discover_jetbrains_idea_log(options.lua_root)
        if idea_log is not None:
            log(
                "[persistent-debug] Watching JetBrains breakpoint events: "
                f"{idea_log}"
            )

    bridge = PersistentScriptDebugBridge(
        listen_host=options.bridge_host,
        listen_port=options.bridge_port,
        target_host=options.target_host,
        target_port=options.target_port,
        runtime_id=options.runtime_id,
        language_id=options.language_id,
        idea_workspace=idea_workspace,
        idea_log=idea_log,
        lua_root=options.lua_root,
        logger=log,
        on_client_changed=on_client_changed,
        on_target_changed=on_target_changed,
    )
    await bridge.start()

    adapter_process: subprocess.Popen[bytes] | None = None
    adapter_output_task: asyncio.Task[None] | None = None
    try:
        dap_available = _port_is_available(options.dap_host, options.dap_port)
        if not dap_available and not options.reuse_dap:
            raise BridgeError(
                f"DAP port {options.dap_host}:{options.dap_port} is already in use. "
                "Stop the old WebStorm-launched debugAdapter, or pass --reuse-dap."
            )

        if dap_available:
            node_path = (options.node or _default_node_path()).resolve()
            adapter_path = options.adapter.resolve()
            adapter_bootstrap_path = options.adapter_bootstrap.resolve()
            if not node_path.is_file():
                raise BridgeError(f"Node.js was not found: {node_path}")
            if not adapter_path.is_file():
                raise BridgeError(f"Debug adapter was not found: {adapter_path}")
            if not adapter_bootstrap_path.is_file():
                raise BridgeError(
                    "Debug adapter compatibility bootstrap was not found: "
                    f"{adapter_bootstrap_path}"
                )

            adapter_process = subprocess.Popen(
                [
                    str(node_path),
                    str(adapter_bootstrap_path),
                    str(adapter_path),
                    f"--server={options.dap_port}",
                ],
                cwd=str(TOOL_DIR),
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if os.name == "nt"
                    else 0
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if adapter_process.stdout is not None:
                adapter_output_task = asyncio.create_task(
                    _pump_adapter_output(
                        adapter_process.stdout,
                        log,
                        on_dap_client_changed,
                    ),
                    name="dap-adapter-output",
                )
            log(
                f"DAP server started on {options.dap_host}:{options.dap_port}; "
                f"PID {adapter_process.pid}"
            )
        else:
            log(
                f"Reusing existing DAP server on "
                f"{options.dap_host}:{options.dap_port}"
            )

        await asyncio.sleep(0.1)
        if adapter_process is not None and adapter_process.poll() is not None:
            raise BridgeError(
                f"DAP server exited with code {adapter_process.returncode}"
            )
        if on_ready is not None:
            on_ready(adapter_process.pid if adapter_process is not None else None)
        log(
            "Ready. Start PyCharm/WebStorm's Lua Attach configuration and "
            "leave this window open."
        )
        while not stop_requested.is_set():
            await asyncio.sleep(0.5)
            if adapter_process is not None and adapter_process.poll() is not None:
                raise BridgeError(
                    f"DAP server exited with code {adapter_process.returncode}"
                )
    finally:
        await bridge.close()
        if adapter_process is not None and adapter_process.poll() is None:
            adapter_process.terminate()
            try:
                await asyncio.to_thread(adapter_process.wait, 3)
            except subprocess.TimeoutExpired:
                adapter_process.kill()
                await asyncio.to_thread(adapter_process.wait)
        if adapter_output_task is not None:
            try:
                await asyncio.wait_for(adapter_output_task, 1.0)
            except TimeoutError:
                adapter_output_task.cancel()
                with suppress(asyncio.CancelledError):
                    await adapter_output_task


def run_embedded_debug_server(
    stop_event: threading.Event,
    *,
    logger: Logger | None = None,
    on_ready: Callable[[int | None], None] | None = None,
    on_client_changed: ClientStateListener | None = None,
    on_target_changed: ClientStateListener | None = None,
    on_dap_client_changed: ClientStateListener | None = None,
    options: PersistentDebugOptions | None = None,
    restart_delay: float = 1.0,
    max_restart_delay: float = 10.0,
) -> None:
    """Supervise the persistent Lua debug service in the GUI worker thread."""
    log = logger or (lambda message: print(message, flush=True))
    service_options = options or PersistentDebugOptions(
        node=_default_node_path()
    )
    consecutive_failures = 0
    restart_count = 0

    while not stop_event.is_set():
        became_ready = False

        def supervised_on_ready(pid: int | None) -> None:
            nonlocal became_ready
            became_ready = True
            if on_ready is not None:
                on_ready(pid)

        failure: Exception | None = None
        try:
            asyncio.run(
                _run(
                    service_options,
                    stop_event=stop_event,
                    logger=log,
                    on_ready=supervised_on_ready,
                    on_client_changed=on_client_changed,
                    on_target_changed=on_target_changed,
                    on_dap_client_changed=on_dap_client_changed,
                )
            )
        except Exception as exc:  # Supervisor boundary: restart the service.
            failure = exc

        if stop_event.is_set():
            return
        if failure is None:
            failure = BridgeError("Lua debug service exited unexpectedly")

        restart_count += 1
        consecutive_failures = (
            1 if became_ready else consecutive_failures + 1
        )
        delay = min(
            max_restart_delay,
            restart_delay * (2 ** min(consecutive_failures - 1, 6)),
        )
        log(
            f"[persistent-debug] Lua debug service stopped: {failure}"
        )
        log(
            f"[persistent-debug] Restarting Lua debug service automatically "
            f"in {delay:g}s (restart #{restart_count})"
        )
        if stop_event.wait(delay):
            return


def main(argv: list[str] | None = None) -> int:
    _prepare_process()
    options = _options_from_args(_parse_args(argv))
    try:
        asyncio.run(_run(options))
    except KeyboardInterrupt:
        print("Persistent debug server stopped.")
        return 0
    except (BridgeError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
