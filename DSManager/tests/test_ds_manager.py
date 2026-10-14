"""Wire-protocol tests use a private fake service; never mutate an existing DS."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ds_manager as ds


class State:
    instance_id = "private-test-instance"
    role = "Dedicated"
    project = "C:/DS Manager/Test"

    def __init__(self):
        self.posts = []
        self.pending = False
        self.reject = False
        self.missing_world = False
        self.result = {}
        self.world_reads = 0
        self.world = None
        self.stoppable = False
        self.matching_enabled = True
        self.queued = []
        self.matches = []
        self.player_world = 1
        self.return_pending = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, data, status=200, ok=True):
        encoded = json.dumps({"ok": ok, "data" if ok else "error": data}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        state = self.server.test_state
        route = self.path.rsplit("/", 1)[-1]
        if route == "info":
            return self.respond({"instance_id": state.instance_id, "pid": os.getpid(),
                                 "project_path": state.project, "net_role": state.role, "game_port": "7777"})
        if route == "caps":
            return self.respond({"protocol": "aicore/v1", "commands": ["ds.kick", "ds.world.create", "runtime.stop",
                                                                      "ds.match.join", "ds.match.cancel", "ds.match.return"]})
        if route == "admin":
            worlds = []
            if state.world and not state.missing_world:
                state.world_reads += 1
                worlds = [{**state.world, "state": "Loading" if state.world_reads < 2 else "Running"}]
            return self.respond({"role": state.role, "state": "Running", "accepting_players": True,
                                 "players": [{"player_id": 42, "world_id": state.player_world}], "worlds": worlds,
                                 "maps": [{"map_id": "Test Map"}], "matchmaking": {"available": state.matching_enabled,
                                    "queued_players": state.queued, "matches": state.matches, "lobby_world_id": 1}})
        if route.startswith("commands?id="):
            return self.respond({"state": "pending"} if state.pending else state.result, 202 if state.pending else 200)
        return self.respond("unknown_route", 404, False)

    def do_POST(self):
        state = self.server.test_state
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["instance_id"] != state.instance_id:
            return self.respond("instance_mismatch", 409, False)
        if state.reject:
            return self.respond({"code": "admin_busy", "message": "queue full"}, 503, False)
        state.posts.append(body)
        operation = body["operation"]
        if operation == "ds.world.create":
            state.world = {"world_id": 9, "generation": 2, "map_id": body["args"]["map_id"]}
            state.result = {**state.world, "state": "Loading"}
        elif operation == "ds.kick":
            state.result = {"state": "kicked", "player_id": body["args"]["player_id"]}
        elif operation.startswith("ds.match."):
            if operation == "ds.match.join":
                state.queued = [body["args"]["player_id"]]
            elif operation == "ds.match.cancel":
                state.queued = []
            elif not state.return_pending:
                state.player_world = 1
            state.result = {"player_id": body["args"]["player_id"], "lobby_world_id": 1,
                            "state": "Returning" if operation == "ds.match.return" else "completed"}
        elif operation == "runtime.stop" and state.stoppable:
            self.respond({"state": "accepted"}, 202)
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return
        return self.respond({"request_id": "7", "state": "accepted"}, 202)


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.test_state = State()
    return server


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = start_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.state = self.server.test_state = State()
        port = self.server.server_port
        self.instance = ds.Instance(os.getpid(), port, "7777", "Dedicated", self.state.project,
                                    self.state.instance_id, f"http://127.0.0.1:{port}", ds.process_stamp(os.getpid()),
                                    ("ds.kick", "ds.world.create", "runtime.stop"), (7777,))
        self.client = ds.Client(self.instance, timeout=0.6)

    def test_read_and_path_normalization(self):
        self.state.project = "c:\\DS Manager\\Test\\"
        self.assertEqual(self.client.execute("status")["player_count"], 1)

    def test_probe_checks_os_port_owner(self):
        self.assertIsNone(ds.probe(self.instance.http_port, {os.getpid() + 10000}, {}))
        found = ds.probe(self.instance.http_port, {os.getpid()}, {7777: {os.getpid()}})
        self.assertEqual(found.udp_ports, (7777,))

    def test_port_selection_uses_udp_owner(self):
        with patch.object(ds, "net_endpoints", return_value=({self.instance.http_port: {os.getpid()}}, {7777: {os.getpid()}})):
            self.assertEqual(ds.scan_instances(7777)[0].pid, os.getpid())
            self.assertEqual(ds.scan_instances(9999), [])

    def test_stale_identity_blocks_post(self):
        self.state.instance_id = "restarted"
        with self.assertRaises(ds.IdentityError):
            self.client.execute("kick 42 test")
        self.assertEqual(self.state.posts, [])

    def test_pid_reuse_blocks_post(self):
        with patch.object(ds, "process_stamp", return_value=self.instance.started + 1):
            with self.assertRaises(ds.IdentityError):
                self.client.execute("kick 42")
        self.assertEqual(self.state.posts, [])

    def test_client_cannot_write_gameplay(self):
        self.state.role = "Client"
        with self.assertRaises(ds.ManagerError):
            self.client.execute('create "Test Map"')
        self.assertEqual(self.state.posts, [])

    def test_utf8_kick_reason(self):
        result = self.client.execute('kick 42 "测试 原因"')
        self.assertEqual(result["state"], "kicked")
        self.assertEqual(self.state.posts[0]["args"]["reason"], "测试 原因")

    def test_invalid_commands_never_post(self):
        for command in ("kick 0", "kick 4294967296", "create", "create Missing", "stop extra", "request 0", "__import__('os')", 'create "unfinished'):
            with self.subTest(command=command), self.assertRaises(ds.ManagerError):
                self.client.execute(command)
        self.assertEqual(self.state.posts, [])

    def test_pending_timeout_does_not_resubmit(self):
        self.state.pending = True
        self.client.timeout = 0.2
        with self.assertRaisesRegex(ds.PendingError, "request 7"):
            self.client.execute("kick 42")
        self.assertEqual(len(self.state.posts), 1)
        self.state.pending = False
        self.assertEqual(self.client.execute("request 7")["state"], "kicked")
        self.assertEqual(len(self.state.posts), 1)

    def test_rejection_is_definite_failure(self):
        self.state.reject = True
        with self.assertRaises(ds.ServiceError):
            self.client.execute("kick 42")

    def test_create_waits_for_world_running(self):
        result = self.client.execute('create "Test Map"')
        self.assertEqual(result["state"], "Running")
        self.assertGreaterEqual(self.state.world_reads, 2)
        self.assertEqual(len(self.state.posts), 1)

    def test_failed_world_is_not_success(self):
        self.state.missing_world = True
        with self.assertRaisesRegex(ds.ManagerError, "加载失败"):
            self.client.execute('create "Test Map"')

    def test_error_body_timeout_is_contained(self):
        from urllib.error import HTTPError
        from unittest.mock import Mock
        error = HTTPError("http://localhost", 400, "Bad request", {}, Mock())
        error.read = Mock(side_effect=TimeoutError())
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(ds, "build_opener", return_value=opener), self.assertRaises(ds.ManagerError):
            ds.http_json("http://127.0.0.1", "/info")

    def test_non_http_listener_does_not_break_scan(self):
        from http.client import BadStatusLine
        from unittest.mock import Mock
        opener = Mock()
        opener.open.side_effect = BadStatusLine("not an HTTP service")
        with patch.object(ds, "build_opener", return_value=opener):
            self.assertIsNone(ds.probe(self.instance.http_port, {os.getpid()}, {}))

    def test_stop_waits_for_private_process_exit(self):
        child = subprocess.Popen([sys.executable, "-B", __file__, "--serve"], stdout=subprocess.PIPE,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            port = int(child.stdout.readline())
            instance = ds.probe(port, {child.pid}, {})
            self.assertIsNotNone(instance)
            result = ds.Client(instance, timeout=3).execute("stop")
            self.assertEqual(result["state"], "stopped")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(child.wait(timeout=3), 0)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
            child.stdout.close()

    def test_join_cancel_and_matches(self):
        self.client.execute("match 42")
        self.assertEqual(self.client.execute("queue"), [{"player_id": 42}])
        self.client.execute("cancel 42")
        self.assertEqual(self.client.execute("queue"), [])
        self.state.matches = [{"id": 9, "worldId": 3, "state": "Playing", "players": [42, 43, 44, 45]}]
        self.assertEqual(self.client.execute("matches")[0]["worldId"], 3)
        self.assertEqual([p["operation"] for p in self.state.posts], ["ds.match.join", "ds.match.cancel"])

    def test_return_checks_membership(self):
        self.state.player_world = 9
        self.assertEqual(self.client.execute("return 42")["state"], "InLobby")
        self.state.player_world = 9
        self.state.return_pending = True
        self.client.timeout = 0.1
        with self.assertRaises(ds.PendingError):
            self.client.execute("return 42")
        self.assertEqual(len(self.state.posts), 2, "timeout must not submit a second return command")

    def test_match_commands_reject_wrong_role_and_invalid_ids(self):
        self.state.role = "Client"
        for command in ("match 42", "cancel 42", "return 42", "match 0", "match 4294967296"):
            with self.subTest(command=command), self.assertRaises(ds.ManagerError):
                self.client.execute(command)
        self.assertEqual(self.state.posts, [])
        self.state.role = "Dedicated"
        self.state.matching_enabled = False
        with self.assertRaisesRegex(ds.ManagerError, "未启用"):
            self.client.execute("matches")

    def test_lua_empty_arrays_clear_match_and_queue_lists(self):
        self.state.matches = {}
        self.state.queued = {}
        self.assertEqual(self.client.execute("matches"), [])
        self.assertEqual(self.client.execute("queue"), [])


if __name__ == "__main__":
    if "--serve" in sys.argv:
        server = start_server()
        server.test_state.stoppable = True
        print(server.server_port, flush=True)
        server.serve_forever()
    else:
        unittest.main()
