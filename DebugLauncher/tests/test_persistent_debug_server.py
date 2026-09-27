from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from persistent_debug_server import (  # noqa: E402
    BridgeError,
    PersistentDebugOptions,
    PersistentScriptDebugBridge,
    _discover_idea_workspace,
    _load_pycharm_lua_breakpoints,
    _parse_jetbrains_breakpoint_toggle,
    _pump_adapter_output,
    _run,
    run_embedded_debug_server,
)


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeScriptDebugger:
    def __init__(self, port: int) -> None:
        self.port = port
        self.server: asyncio.AbstractServer | None = None
        self.sessions: list[list[dict[str, object]]] = []
        self.writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle_client,
            "127.0.0.1",
            self.port,
        )

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        await self.disconnect_clients()

    async def disconnect_clients(self) -> None:
        writers = tuple(self.writers)
        self.writers.clear()
        for writer in writers:
            writer.close()
        for writer in writers:
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def send_event(
        self,
        event: str,
        body: dict[str, object] | None = None,
    ) -> None:
        message = {
            "type": "event",
            "event": event,
            "body": body or {},
        }
        payload = (json.dumps(message) + "\n").encode("utf-8")
        for writer in tuple(self.writers):
            writer.write(payload)
            await writer.drain()

    async def wait_for_commands(
        self,
        session_index: int,
        count: int,
        timeout: float = 3.0,
    ) -> list[dict[str, object]]:
        async def wait() -> list[dict[str, object]]:
            while True:
                if (
                    len(self.sessions) > session_index
                    and len(self.sessions[session_index]) >= count
                ):
                    return self.sessions[session_index]
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(wait(), timeout)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        messages: list[dict[str, object]] = []
        self.sessions.append(messages)
        self.writers.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                message = json.loads(line.decode("utf-8"))
                messages.append(message)
                command = str(message.get("command", ""))
                if command == "initialize":
                    body: dict[str, object] = {
                        "runtimeId": "minigameplay-main-lua",
                        "languageId": "lua",
                        "displayName": "Fake AIFramework",
                        "runtimeAlive": True,
                        "supportsSourceMap": True,
                    }
                elif command == "setBreakpoints":
                    arguments = message.get("arguments", {})
                    lines = (
                        arguments.get("lines", [])
                        if isinstance(arguments, dict)
                        else []
                    )
                    body = {
                        "breakpoints": [
                            {"verified": True, "line": line} for line in lines
                        ]
                    }
                else:
                    body = {}
                response = {
                    "type": "response",
                    "requestId": message["id"],
                    "success": True,
                    "body": body,
                }
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            self.writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


class PyCharmLuaBreakpointTests(unittest.TestCase):
    def test_loads_enabled_lua_breakpoints_under_scripts(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            project_dir = Path(directory)
            lua_root = project_dir / "Scripts"
            nested = lua_root / "Games" / "TopBattle"
            nested.mkdir(parents=True)
            (lua_root / "main.lua").write_text("return true\n", encoding="utf-8")
            (nested / "TopBattleGame.lua").write_text(
                "return true\n",
                encoding="utf-8",
            )
            unrelated = project_dir / "LuaScript" / "physics"
            unrelated.mkdir(parents=True)
            (unrelated / "init.lua").write_text("return true\n", encoding="utf-8")

            idea_dir = project_dir / ".idea"
            idea_dir.mkdir()
            workspace_file = idea_dir / "workspace.xml"
            workspace_file.write_text(
                """<project>
  <component name="XDebuggerManager">
    <breakpoint-manager><breakpoints>
      <line-breakpoint type="lua-line">
        <url>file://$PROJECT_DIR$/Scripts/main.lua</url><line>30</line>
      </line-breakpoint>
      <line-breakpoint enabled="true" type="lua-line">
        <url>file://$PROJECT_DIR$/Scripts/Games/TopBattle/TopBattleGame.lua</url><line>41</line>
      </line-breakpoint>
      <line-breakpoint enabled="false" type="lua-line">
        <url>file://$PROJECT_DIR$/Scripts/main.lua</url><line>99</line>
      </line-breakpoint>
      <line-breakpoint type="lua-line">
        <url>file://$PROJECT_DIR$/LuaScript/physics/init.lua</url><line>5</line>
      </line-breakpoint>
      <line-breakpoint type="dap-breakpoint">
        <url>file://$PROJECT_DIR$/Scripts/main.lua</url><line>10</line>
      </line-breakpoint>
    </breakpoints></breakpoint-manager>
  </component>
</project>
""",
                encoding="utf-8",
            )

            self.assertEqual(
                _load_pycharm_lua_breakpoints(workspace_file, lua_root),
                {
                    "Games/TopBattle/TopBattleGame.lua": (42,),
                    "main.lua": (31,),
                },
            )

    def test_loads_breakpoints_from_nested_rider_workspace(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            mini_root = Path(directory)
            lua_root = mini_root / "Source" / "AIGamePlay" / "Scripts"
            source_file = lua_root / "Games" / "init.lua"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("return true\n", encoding="utf-8")

            workspace_file = (
                mini_root
                / "Projects"
                / "MiniGame"
                / ".idea"
                / ".idea.MiniGame"
                / ".idea"
                / "workspace.xml"
            )
            workspace_file.parent.mkdir(parents=True)
            workspace_file.write_text(
                """<project>
  <component name="XDebuggerManager">
    <breakpoint-manager><breakpoints>
      <line-breakpoint type="lua-line">
        <url>file://$PROJECT_DIR$/../../Source/AIGamePlay/Scripts/Games/init.lua</url><line>52</line>
      </line-breakpoint>
    </breakpoints></breakpoint-manager>
  </component>
</project>
""",
                encoding="utf-8",
            )

            self.assertEqual(
                _load_pycharm_lua_breakpoints(workspace_file, lua_root),
                {"Games/init.lua": (53,)},
            )
            self.assertEqual(
                _discover_idea_workspace(
                    lua_root.parent / ".idea" / "workspace.xml",
                    lua_root,
                ).resolve(),
                workspace_file.resolve(),
            )

    def test_rider_toggle_log_clears_stale_workspace_breakpoint(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            project_dir = Path(directory)
            lua_root = project_dir / "Scripts"
            source_file = lua_root / "Games" / "init.lua"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("\n" * 70, encoding="utf-8")

            workspace_file = project_dir / ".idea" / "workspace.xml"
            workspace_file.parent.mkdir()
            workspace_file.write_text(
                """<project>
  <component name="XDebuggerManager">
    <breakpoint-manager><breakpoints>
      <line-breakpoint type="lua-line">
        <url>file://$PROJECT_DIR$/Scripts/Games/init.lua</url><line>64</line>
      </line-breakpoint>
    </breakpoints></breakpoint-manager>
  </component>
</project>
""",
                encoding="utf-8",
            )
            toggle_line = (
                "2099-01-01 10:00:00,000 [1] INFO - breakpoint - "
                "Toggle line breakpoint request received file: "
                "XSourcePositionDto(line=64, offset=1, "
                f"fileId=VirtualFileId(localVirtualFile=file://{source_file.as_posix()}), "
                "textRangeDto=null), line: 64Request details: "
                "hasBreakpoints=true, isTemporary=false"
            )
            idea_log = project_dir / "idea.log"
            idea_log.write_text("", encoding="utf-8")

            event = _parse_jetbrains_breakpoint_toggle(toggle_line, lua_root)
            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(event[:3], ("Games/init.lua", 65, False))
            bridge = PersistentScriptDebugBridge(
                idea_workspace=workspace_file,
                idea_log=idea_log,
                lua_root=lua_root,
                logger=lambda _message: None,
            )

            self.assertEqual(
                bridge._refresh_pycharm_breakpoints(),
                ("Games/init.lua",),
            )
            self.assertEqual(
                bridge._pycharm_breakpoints,
                {"Games/init.lua": (65,)},
            )

            idea_log.write_text(toggle_line + "\n", encoding="utf-8")
            self.assertEqual(
                bridge._refresh_pycharm_breakpoints(),
                ("Games/init.lua",),
            )
            self.assertEqual(
                bridge._pycharm_breakpoints,
                {"Games/init.lua": ()},
            )
            self.assertTrue(bridge._pycharm_breakpoints_active)
            self.assertEqual(
                bridge._breakpoint_snapshots(),
                [{"sourcePath": "Games/init.lua", "lines": []}],
            )

            replay_bridge = PersistentScriptDebugBridge(
                idea_workspace=workspace_file,
                idea_log=idea_log,
                lua_root=lua_root,
                logger=lambda _message: None,
            )
            replay_bridge._refresh_pycharm_breakpoints()
            self.assertEqual(
                replay_bridge._pycharm_breakpoints,
                {"Games/init.lua": ()},
            )


class PersistentScriptDebugBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.target_port = _unused_tcp_port()
        self.logs: list[str] = []
        self.client_states: list[str | None] = []
        self.target_states: list[str | None] = []
        self.bridge = PersistentScriptDebugBridge(
            listen_port=0,
            target_port=self.target_port,
            reconnect_interval=0.05,
            request_timeout=1.0,
            logger=self.logs.append,
            on_client_changed=self.client_states.append,
            on_target_changed=self.target_states.append,
        )
        await self.bridge.start()
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1",
            self.bridge.bound_port,
        )
        self.engine = FakeScriptDebugger(self.target_port)

    async def test_reports_frontend_client_connect_and_disconnect(self) -> None:
        async def wait_for_client_state(expected: str | None) -> None:
            while not self.client_states or self.client_states[-1] != expected:
                await asyncio.sleep(0.01)

        async def wait_for_connected_client() -> None:
            while self.bridge.frontend_client is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_connected_client(), 3.0)
        self.assertIsNotNone(self.bridge.frontend_client)
        assert self.bridge.frontend_client is not None
        self.assertEqual(self.client_states[-1], self.bridge.frontend_client)
        self.assertTrue(self.bridge.frontend_client.startswith("127.0.0.1:"))

        self.writer.close()
        await self.writer.wait_closed()
        await asyncio.wait_for(wait_for_client_state(None), 3.0)
        self.assertIsNone(self.bridge.frontend_client)

    async def asyncTearDown(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        await self.engine.close()
        await self.bridge.close()

    async def test_target_restart_keeps_frontend_and_replays_breakpoints(self) -> None:
        initialize = await self._request(1, "initialize")
        self.assertTrue(initialize["success"])
        self.assertEqual(
            initialize["body"]["runtimeId"],
            "minigameplay-main-lua",
        )

        breakpoint_args = {
            "sourcePath": "Games/TopBattle/TopBattleTop.lua",
            "lines": [12, 34],
        }
        set_breakpoints = await self._request(
            2,
            "setBreakpoints",
            breakpoint_args,
        )
        self.assertTrue(set_breakpoints["success"])
        self.assertEqual(len(set_breakpoints["body"]["breakpoints"]), 2)
        self.assertTrue((await self._request(3, "configurationDone"))["success"])

        await self.engine.start()
        first_session = await self.engine.wait_for_commands(0, 3)
        self.assertEqual(
            [message["command"] for message in first_session[:3]],
            ["initialize", "setBreakpoints", "configurationDone"],
        )
        self.assertEqual(first_session[1]["arguments"], breakpoint_args)
        await self._wait_until_target_ready()
        self.assertEqual(
            self.target_states[-1],
            f"127.0.0.1:{self.target_port}",
        )

        continued = await self._request(4, "continue")
        self.assertTrue(continued["success"])
        await self.engine.wait_for_commands(0, 4)

        await self.engine.disconnect_clients()
        await self._wait_for_target_state(None)
        offline_events = await self._wait_for_output("offline")
        self.assertNotIn("disconnect", [event.get("event") for event in offline_events])
        self.assertFalse(self.reader.at_eof())

        second_session = await self.engine.wait_for_commands(1, 3)
        self.assertEqual(
            [message["command"] for message in second_session[:3]],
            ["initialize", "setBreakpoints", "configurationDone"],
        )
        self.assertEqual(second_session[1]["arguments"], breakpoint_args)
        await self._wait_until_target_ready()
        self.assertEqual(
            self.target_states[-1],
            f"127.0.0.1:{self.target_port}",
        )

        continued_after_restart = await self._request(5, "continue")
        self.assertTrue(continued_after_restart["success"])
        await self.engine.wait_for_commands(1, 4)

    async def test_replays_pycharm_lua_breakpoints_without_dap_request(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            project_dir = Path(directory)
            lua_root = project_dir / "Scripts"
            lua_root.mkdir()
            (lua_root / "main.lua").write_text(
                "return true\n",
                encoding="utf-8",
            )
            idea_dir = project_dir / ".idea"
            idea_dir.mkdir()
            workspace_file = idea_dir / "workspace.xml"
            workspace_file.write_text(
                """<project>
  <component name="XDebuggerManager">
    <breakpoint-manager><breakpoints>
      <line-breakpoint type="lua-line">
        <url>file://$PROJECT_DIR$/Scripts/main.lua</url><line>30</line>
      </line-breakpoint>
    </breakpoints></breakpoint-manager>
  </component>
</project>
""",
                encoding="utf-8",
            )

            self.bridge.idea_workspace = workspace_file
            self.bridge.lua_root = lua_root
            self.bridge.breakpoint_refresh_interval = 0.02
            self.bridge._refresh_pycharm_breakpoints()
            self.bridge._breakpoint_watcher_task = asyncio.create_task(
                self.bridge._pycharm_breakpoint_watch_loop()
            )

            self.assertTrue((await self._request(20, "initialize"))["success"])
            self.assertTrue(
                (await self._request(21, "configurationDone"))["success"]
            )
            await self.engine.start()

            session = await self.engine.wait_for_commands(0, 3)
            self.assertEqual(
                [message["command"] for message in session[:3]],
                ["initialize", "setBreakpoints", "configurationDone"],
            )
            self.assertEqual(
                session[1]["arguments"],
                {"sourcePath": "main.lua", "lines": [31]},
            )
            self.assertIn(
                "[persistent-debug] Loaded 1 JetBrains Lua breakpoint(s) "
                "from 1 source file(s)",
                self.logs,
            )
            await self._wait_until_target_ready()

            workspace_file.write_text(
                workspace_file.read_text(encoding="utf-8").replace(
                    "<line>30</line>",
                    "<line>40</line>",
                ),
                encoding="utf-8",
            )
            updated_session = await self.engine.wait_for_commands(0, 4)
            self.assertEqual(updated_session[3]["command"], "setBreakpoints")
            self.assertEqual(
                updated_session[3]["arguments"],
                {"sourcePath": "main.lua", "lines": [41]},
            )

            async def wait_for_applied_log() -> None:
                expected = (
                    "[persistent-debug] Applied JetBrains Lua breakpoints: "
                    "main.lua -> [41]"
                )
                while expected not in self.logs:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_applied_log(), 3.0)
            self.assertIn(
                "[persistent-debug] Applied JetBrains Lua breakpoints: "
                "main.lua -> [41]",
                self.logs,
            )

            watcher = self.bridge._breakpoint_watcher_task
            assert watcher is not None
            watcher.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await watcher
            self.bridge._breakpoint_watcher_task = None

    async def test_workspace_lua_breakpoints_override_frontend_dap_locations(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            project_dir = Path(directory)
            lua_root = project_dir / "Scripts"
            lua_root.mkdir()
            (lua_root / "main.lua").write_text(
                "return true\n",
                encoding="utf-8",
            )
            idea_dir = project_dir / ".idea"
            idea_dir.mkdir()
            workspace_file = idea_dir / "workspace.xml"
            workspace_file.write_text(
                """<project>
  <component name="XDebuggerManager">
    <breakpoint-manager><breakpoints>
      <line-breakpoint type="lua-line">
        <url>file://$PROJECT_DIR$/Scripts/main.lua</url><line>30</line>
      </line-breakpoint>
    </breakpoints></breakpoint-manager>
  </component>
</project>
""",
                encoding="utf-8",
            )

            self.bridge.idea_workspace = workspace_file
            self.bridge.lua_root = lua_root
            self.bridge._refresh_pycharm_breakpoints()

            self.assertTrue((await self._request(30, "initialize"))["success"])
            self.assertTrue(
                (
                    await self._request(
                        31,
                        "setBreakpoints",
                        {"sourcePath": "main.lua", "lines": [7, 99]},
                    )
                )["success"]
            )
            self.assertTrue(
                (await self._request(32, "configurationDone"))["success"]
            )
            await self.engine.start()

            session = await self.engine.wait_for_commands(0, 3)
            self.assertEqual(
                [message["command"] for message in session[:3]],
                ["initialize", "setBreakpoints", "configurationDone"],
            )
            self.assertEqual(
                session[1]["arguments"],
                {"sourcePath": "main.lua", "lines": [31]},
            )
            await self._wait_until_target_ready()
            self.assertIn(
                "[persistent-debug] JetBrains Lua breakpoints are now "
                "authoritative for this IDE session",
                self.logs,
            )

    async def test_empty_stopped_stack_restores_source_frame(self) -> None:
        self.assertTrue((await self._request(40, "initialize"))["success"])
        await self.engine.start()
        await self.engine.wait_for_commands(0, 1)
        await self._wait_until_target_ready()

        await self.engine.send_event(
            "stopped",
            {
                "reason": "breakpoint",
                "sourcePath": "Framework/Game/Game.lua",
                "line": 74,
                "stack": [],
            },
        )

        while True:
            line = await asyncio.wait_for(self.reader.readline(), 3.0)
            self.assertTrue(line, "bridge closed while waiting for stopped event")
            message = json.loads(line.decode("utf-8"))
            if message.get("event") == "stopped":
                break

        self.assertEqual(
            message["body"]["stack"],
            [
                {
                    "id": 0,
                    "name": "<script>",
                    "sourcePath": "Framework/Game/Game.lua",
                    "line": 74,
                    "column": 1,
                }
            ],
        )
        self.assertIn(
            "[persistent-debug] Restored the stopped source frame from "
            "event location",
            self.logs,
        )

    async def test_debugger_detach_resumes_a_stopped_target(self) -> None:
        self.assertTrue((await self._request(10, "initialize"))["success"])
        breakpoint_args = {
            "sourcePath": "Games/TopBattle/TopBattlePlayerController.lua",
            "lines": [13],
        }
        self.assertTrue(
            (
                await self._request(
                    11,
                    "setBreakpoints",
                    breakpoint_args,
                )
            )["success"]
        )
        await self.engine.start()
        await self.engine.wait_for_commands(0, 2)
        await self._wait_until_target_ready()

        await self.engine.send_event(
            "stopped",
            {"reason": "breakpoint", "sourcePath": "main.lua", "line": 12},
        )
        self.assertTrue((await self._request(12, "disconnect"))["success"])

        session = await self.engine.wait_for_commands(0, 4)
        commands = [message["command"] for message in session]
        self.assertIn("continue", commands)
        self.assertNotIn("disconnect", commands)
        self.assertEqual(session[-2]["command"], "setBreakpoints")
        self.assertEqual(
            session[-2]["arguments"],
            {
                "sourcePath": breakpoint_args["sourcePath"],
                "lines": [],
            },
        )
        self.assertEqual(session[-1]["command"], "continue")
        self.assertIn(
            "Cleared breakpoints for 1 source file(s) before debugger detach",
            self.logs,
        )
        self.assertIn(
            "Ensured AIFramework is running before debugger detach",
            self.logs,
        )

        self.writer.close()
        await self.writer.wait_closed()

        async def wait_for_frontend_release() -> None:
            while self.bridge._frontend_writer is not None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_frontend_release(), 3.0)
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1",
            self.bridge.bound_port,
        )
        self.assertTrue((await self._request(13, "initialize"))["success"])
        second_session = await self.engine.wait_for_commands(1, 3)
        self.assertEqual(second_session[0]["command"], "initialize")
        self.assertEqual(second_session[1]["command"], "setBreakpoints")
        self.assertEqual(second_session[1]["arguments"]["lines"], [])
        self.assertEqual(second_session[2]["command"], "continue")
        await self._wait_until_target_ready()
        self.assertIn(
            "Cleared stale breakpoints and resumed AIFramework for the new "
            "debugger session",
            self.logs,
        )

    async def _request(
        self,
        request_id: int,
        command: str,
        arguments: dict[str, object] | None = None,
    ) -> dict[str, object]:
        request = {
            "id": request_id,
            "command": command,
            "arguments": arguments or {},
        }
        self.writer.write((json.dumps(request) + "\n").encode("utf-8"))
        await self.writer.drain()
        while True:
            line = await asyncio.wait_for(self.reader.readline(), 3.0)
            self.assertTrue(line, "bridge closed the adapter-facing socket")
            message = json.loads(line.decode("utf-8"))
            if (
                message.get("type") == "response"
                and message.get("requestId") == request_id
            ):
                return message

    async def _wait_for_output(self, needle: str) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        while True:
            line = await asyncio.wait_for(self.reader.readline(), 3.0)
            self.assertTrue(line, "bridge closed while waiting for output")
            message = json.loads(line.decode("utf-8"))
            events.append(message)
            body = message.get("body", {})
            text = str(body.get("text", "")) if isinstance(body, dict) else ""
            if needle in text:
                return events

    async def _wait_until_target_ready(self) -> None:
        async def wait() -> None:
            while not self.bridge.target_ready:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 3.0)

    async def _wait_for_target_state(self, expected: str | None) -> None:
        async def wait() -> None:
            while not self.target_states or self.target_states[-1] != expected:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 3.0)


class EmbeddedDebugServerTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    async def test_adapter_bootstrap_emits_initialized_after_attach(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module_dir = root / "node_modules" / "vscode-debugadapter"
            module_dir.mkdir(parents=True)
            (module_dir / "index.js").write_text(
                """
class DebugSession {
  sendResponse(response) { console.log(`response:${response.command}`); }
  sendEvent(event) { console.log(`event:${event.event}`); }
  static run(Session) {
    const session = new Session();
    session.initializeRequest({ command: 'initialize' }, {});
    Promise.resolve(session.attachRequest(
      { command: 'attach', success: true }, {}
    )).catch((error) => { console.error(error); process.exitCode = 1; });
  }
}
class InitializedEvent { constructor() { this.event = 'initialized'; } }
module.exports = { DebugSession, InitializedEvent };
""".strip(),
                encoding="utf-8",
            )
            adapter = root / "fakeAdapter.js"
            adapter.write_text(
                """
const { DebugSession, InitializedEvent } = require('vscode-debugadapter');
class FakeSession extends DebugSession {
  initializeRequest(response) {
    this.sendResponse(response);
    this.sendEvent(new InitializedEvent());
  }
  async attachRequest(response) { this.sendResponse(response); }
}
FakeSession.run(FakeSession);
""".strip(),
                encoding="utf-8",
            )

            completed = await asyncio.to_thread(
                subprocess.run,
                [
                    shutil.which("node"),
                    str(PersistentDebugOptions().adapter_bootstrap),
                    str(adapter),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "response:initialize",
                "response:attach",
                "event:initialized",
            ],
        )

    async def test_adapter_output_reports_dap_client_lifecycle(self) -> None:
        logs: list[str] = []
        states: list[str | None] = []
        stream = io.BytesIO(
            b"waiting for debug protocol on port 4711\n"
            b">> accepted connection from client\n"
            b">> client connection closed\n"
        )

        await _pump_adapter_output(stream, logs.append, states.append)

        self.assertEqual(
            logs,
            [
                "[DAP adapter] waiting for debug protocol on port 4711",
                "[DAP adapter] >> accepted connection from client",
                "[DAP adapter] >> client connection closed",
            ],
        )
        self.assertEqual(states, ["IDE", None])

    async def test_stop_event_closes_bridge_and_adapter_process(self) -> None:
        stop_event = threading.Event()
        ready_pids: list[int | None] = []

        class FakeBridge:
            instance: "FakeBridge | None" = None

            def __init__(self, **_kwargs: object) -> None:
                self.started = False
                self.closed = False
                FakeBridge.instance = self

            async def start(self) -> None:
                self.started = True

            async def close(self) -> None:
                self.closed = True

        class FakeProcess:
            pid = 2468
            stdout = None

            def __init__(self) -> None:
                self.returncode: int | None = None
                self.terminated = False

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = 0

            def wait(self, _timeout: float | None = None) -> int:
                return self.returncode or 0

            def kill(self) -> None:
                self.returncode = -1

        fake_process = FakeProcess()
        existing_file = Path(__file__)
        options = PersistentDebugOptions(
            dap_port=_unused_tcp_port(),
            bridge_port=_unused_tcp_port(),
            idea_workspace=None,
            node=existing_file,
            adapter=existing_file,
        )

        def on_ready(pid: int | None) -> None:
            ready_pids.append(pid)
            stop_event.set()

        with (
            patch(
                "persistent_debug_server.PersistentScriptDebugBridge",
                FakeBridge,
            ),
            patch(
                "persistent_debug_server.subprocess.Popen",
                return_value=fake_process,
            ) as subprocess_popen,
        ):
            await _run(options, stop_event=stop_event, on_ready=on_ready)

        self.assertEqual(ready_pids, [2468])
        self.assertIsNotNone(FakeBridge.instance)
        self.assertTrue(FakeBridge.instance.started)
        self.assertTrue(FakeBridge.instance.closed)
        self.assertTrue(fake_process.terminated)
        popen = subprocess_popen.call_args
        self.assertEqual(
            popen.args[0],
            [
                str(existing_file.resolve()),
                str(options.adapter_bootstrap.resolve()),
                str(existing_file.resolve()),
                f"--server={options.dap_port}",
            ],
        )
        self.assertEqual(popen.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(popen.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(popen.kwargs["stderr"], subprocess.STDOUT)


class DebugServiceSupervisorTests(unittest.TestCase):
    def test_restarts_service_after_unexpected_failure(self) -> None:
        stop_event = threading.Event()
        logs: list[str] = []
        ready_pids: list[int | None] = []
        calls = 0

        async def fake_run(
            _options: PersistentDebugOptions,
            *,
            stop_event: threading.Event,
            logger: object,
            on_ready: object,
            on_client_changed: object,
            on_target_changed: object,
            on_dap_client_changed: object,
        ) -> None:
            nonlocal calls
            del logger, on_client_changed, on_target_changed, on_dap_client_changed
            calls += 1
            assert callable(on_ready)
            on_ready(2400 + calls)
            if calls == 1:
                raise BridgeError("DAP adapter crashed")
            stop_event.set()

        with patch("persistent_debug_server._run", new=fake_run):
            run_embedded_debug_server(
                stop_event,
                logger=logs.append,
                on_ready=ready_pids.append,
                restart_delay=0.001,
                max_restart_delay=0.001,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(ready_pids, [2401, 2402])
        self.assertIn(
            "[persistent-debug] Lua debug service stopped: "
            "DAP adapter crashed",
            logs,
        )
        self.assertIn(
            "[persistent-debug] Restarting Lua debug service automatically "
            "in 0.001s (restart #1)",
            logs,
        )


if __name__ == "__main__":
    unittest.main()
