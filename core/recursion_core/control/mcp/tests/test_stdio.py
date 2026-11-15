"""The real thing on one machine: ``python -m recursion_core.control.mcp`` as a child process over real stdio
pipes, talking to the real owner-only socket transport, behind which the real ``ControlAPI`` runs over the
shared fakes (``control/tests/fakes.py``). The socket's handler is the daemon's socket face in miniature
(``{"op": "control"}`` in, ``{"ok": true, "result": ...}`` out).

No MCP client product is run here: this is the server's half of the wire with a scripted client.
The hang guard kills a child that stops answering; no outcome depends on timing.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import recursion_core
from recursion_core.control.mcp.framing import encode
from recursion_core.control.mcp.tests.support import HANG_GUARD_S
from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.control.tests.fakes import FakeKillTree, make_api
from recursion_core.control_transport import UnixControlServer

CAN_SERVE = hasattr(socket, "AF_UNIX") and hasattr(socket, "SO_PEERCRED") and hasattr(os, "geteuid")
CORE = str(Path(recursion_core.__file__).resolve().parent.parent)
CLIENT = {"name": "scripted-test-client", "version": "0"}


def daemon_face(api, seen: list):
    def handle(message, conn):
        seen.append(message)
        if message.get("op") != "control":
            return {"ok": False, "error": {"code": "unknown_op"}}
        return {"ok": True, "result": api.call(message["method"], message.get("params"))}
    return handle


class Child:
    """The server process, with a line-at-a-time scripted client on its pipes."""

    def __init__(self, test: unittest.TestCase, runtime: str) -> None:
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = runtime
        env["PYTHONPATH"] = os.pathsep.join(p for p in (CORE, env.get("PYTHONPATH", "")) if p)
        self.proc = subprocess.Popen([sys.executable, "-B", "-m", "recursion_core.control.mcp"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self._guard = threading.Timer(HANG_GUARD_S, self.proc.kill)
        self._guard.daemon = True
        self._guard.start()
        test.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        self._guard.cancel()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def send(self, message: dict) -> None:
        self.proc.stdin.write(encode(message))
        self.proc.stdin.flush()

    def recv(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"the server ended without answering; stderr: {self.proc.stderr.read()!r}")
        return json.loads(line)

    def request(self, request_id, method: str, params: dict | None = None) -> dict:
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        answer = self.recv()
        assert answer.get("id") == request_id, answer
        return answer

    def tool(self, request_id, name: str, arguments: dict) -> dict:
        return self.request(request_id, "tools/call", {"name": name, "arguments": arguments})["result"]

    def end(self) -> tuple[int, bytes, list[dict]]:
        """Close stdin; return the exit status, anything more on stdout, and stderr's JSON lines."""

        self.proc.stdin.close()
        rest = self.proc.stdout.read()
        status = self.proc.wait()
        self._guard.cancel()
        return status, rest, [json.loads(line) for line in self.proc.stderr.read().splitlines()]


@unittest.skipUnless(CAN_SERVE, "the owner-only transport needs AF_UNIX, SO_PEERCRED and os.geteuid (Linux)")
class StdioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = tempfile.mkdtemp(prefix="mcp-run-")
        self.addCleanup(shutil.rmtree, self.runtime, True)
        self.seen: list[dict] = []

    def daemon(self, api, **kwargs) -> None:
        server = UnixControlServer(os.path.join(self.runtime, "recursion"), daemon_face(api, self.seen), **kwargs)
        server.start()
        self.addCleanup(server.close)

    def initialize(self, child: Child, request_id=1) -> dict:
        answer = child.request(request_id, "initialize",
                               {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": CLIENT})
        child.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return answer["result"]

    def test_start_poll_and_stop_a_fake_run_in_the_legacy_era(self) -> None:
        api, _, kill, starter = make_api(FakeKillTree([POPULATED, EMPTY]))
        self.daemon(api)
        child = Child(self, self.runtime)
        probe = child.request(0, "server/discover", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}})
        self.assertEqual({"code": -32601, "message": "Method not found: server/discover"}, probe["error"])
        self.assertEqual("2025-06-18", self.initialize(child)["protocolVersion"])
        names = [t["name"] for t in child.request(2, "tools/list")["result"]["tools"]]
        self.assertEqual(["recursion_run_list", "recursion_run_status", "recursion_run_start", "recursion_run_stop"], names)

        start = {"tenant": "t1", "engine": "codex", "prompt": "say hello", "client_key": "k-1"}
        started = child.tool(3, "recursion_run_start", start)
        self.assertEqual(("started", False, False), (started["structuredContent"]["code"],
                                                     started["structuredContent"]["duplicate"], started["isError"]))
        run_id = started["structuredContent"]["run"]["run_id"]
        again = child.tool(4, "recursion_run_start", start)  # the same key: one run, not two
        self.assertEqual((True, run_id), (again["structuredContent"]["duplicate"],
                                          again["structuredContent"]["run"]["run_id"]))
        self.assertEqual(1, len(starter.calls))

        status = child.tool(5, "recursion_run_status", {"run_id": run_id})
        self.assertEqual(("ok", "running"), (status["structuredContent"]["code"], status["structuredContent"]["run"]["state"]))

        pending = child.tool(6, "recursion_run_stop", {"run_id": run_id})  # a member lives
        self.assertEqual(("pending", True), (pending["structuredContent"]["code"], pending["isError"]))
        stopped = child.tool(7, "recursion_run_stop", {"run_id": run_id})  # the attested walk is empty
        self.assertEqual(("stopped", False), (stopped["structuredContent"]["code"], stopped["isError"]))
        self.assertEqual([f"/sys/fs/cgroup/fake/{run_id}"], kill.requests)
        self.assertEqual(2, len(kill.confirms))

        listed = child.tool(8, "recursion_run_list", {})
        self.assertEqual(["stopped"], [r["state"] for r in listed["structuredContent"]["runs"]])
        self.assertEqual("empty", listed["structuredContent"]["runs"][0]["stop_attestation"]["proof"])

        status_code, rest, events = child.end()
        self.assertEqual((0, b""), (status_code, rest))
        self.assertEqual({"event": "serving", "protocol": "2025-06-18"}, events[0])
        self.assertEqual({"event": "end_of_input", "stdout_broken": False}, events[-1])
        methods = [m["method"] for m in self.seen]
        self.assertEqual(["run.list", "run.start", "run.start", "run.status", "run.stop", "run.stop", "run.list"],
                         methods)  # the first is the socket check's probe
        self.assertEqual({"limit": 1}, self.seen[0]["params"])

    def test_closing_stdin_detaches_and_the_run_goes_on(self) -> None:
        api, _, kill, _ = make_api(FakeKillTree([EMPTY]))
        self.daemon(api)
        child = Child(self, self.runtime)
        self.initialize(child)
        started = child.tool(2, "recursion_run_start", {"tenant": "t1", "engine": "codex", "prompt": "p"})
        run_id = started["structuredContent"]["run"]["run_id"]
        self.assertEqual(0, child.end()[0])
        self.assertEqual([], kill.requests)
        self.assertEqual("running", api.call("run.status", {"run_id": run_id})["run"]["state"])
        self.assertNotIn("run.stop", [m["method"] for m in self.seen])

    def test_invalid_arguments_reach_nothing_over_the_real_socket(self) -> None:
        api, _, kill, starter = make_api()
        self.daemon(api)
        child = Child(self, self.runtime)
        self.initialize(child)
        refused = child.request(2, "tools/call", {"name": "recursion_run_stop", "arguments": {"run_id": ""}})
        self.assertEqual(-32602, refused["error"]["code"])
        refused = child.request(3, "tools/call", {"name": "recursion_run_start", "arguments": {"tenant": "t1"}})
        self.assertEqual(-32602, refused["error"]["code"])
        self.assertEqual(0, child.end()[0])
        self.assertEqual(["run.list"], [m["method"] for m in self.seen])  # the probe only
        self.assertEqual(([], []), (kill.requests, starter.calls))

    def test_no_socket_refuses_to_start(self) -> None:
        child = Child(self, self.runtime)  # nothing listens under this runtime folder
        status, rest, events = child.end()
        self.assertEqual((1, b""), (status, rest))
        self.assertEqual([{"code": "socket_check_failed", "reason": "socket_dir_missing",
                           "detail": os.path.join(self.runtime, "recursion")}], events)

    def test_a_daemon_that_does_not_admit_the_server_refuses_it_to_start(self) -> None:
        api, _, _, starter = make_api()
        self.daemon(api, admit=lambda cred: False)
        status, rest, events = Child(self, self.runtime).end()
        self.assertEqual((1, b""), (status, rest))
        self.assertEqual(("daemon_unreachable", "daemon:scope_refused"), (events[0]["reason"], events[0]["detail"]))
        self.assertEqual(([], []), (self.seen, starter.calls))

    def test_an_open_socket_folder_refuses_to_start(self) -> None:
        api, _, _, _ = make_api()
        self.daemon(api)
        os.chmod(os.path.join(self.runtime, "recursion"), 0o755)
        self.addCleanup(os.chmod, os.path.join(self.runtime, "recursion"), 0o700)
        status, rest, events = Child(self, self.runtime).end()
        self.assertEqual((1, b"", "socket_dir_mode"), (status, rest, events[0]["reason"]))
        self.assertEqual([], self.seen)


if __name__ == "__main__":
    unittest.main()
