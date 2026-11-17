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
import signal
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
                try:
                    stream.close()
                except OSError:  # a line still buffered for a child that has ended
                    pass

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

    def test_a_stop_is_sent_while_eight_starts_hang_and_a_ninth_waits(self) -> None:
        release, stop_seen = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        changed = threading.Condition()
        entered: list[str] = []

        class Hanging:
            def call(self, method, params=None):
                with changed:
                    entered.append(method)
                    changed.notify_all()
                if method == "run.start":
                    release.wait(HANG_GUARD_S)
                    return {"code": "started", "run": {"run_id": "r"}}
                if method == "run.stop":
                    stop_seen.set()
                    return {"code": "pending", "run_id": params["run_id"], "state": "pending"}
                return {"code": "ok", "runs": []}

        self.daemon(Hanging())
        child = Child(self, self.runtime)
        self.initialize(child)
        start = {"tenant": "t1", "engine": "codex", "prompt": "p"}
        for n in range(1, 9):  # eight fill the default slots
            child.send({"jsonrpc": "2.0", "id": n, "method": "tools/call",
                        "params": {"name": "recursion_run_start", "arguments": start}})
        with changed:
            self.assertTrue(changed.wait_for(lambda: entered.count("run.start") == 8, HANG_GUARD_S))
        child.send({"jsonrpc": "2.0", "id": 9, "method": "tools/call",  # the ninth waits for a slot
                    "params": {"name": "recursion_run_start", "arguments": start}})
        child.send({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                    "params": {"name": "recursion_run_stop", "arguments": {"run_id": "r"}}})
        self.assertTrue(stop_seen.wait(HANG_GUARD_S), "the stop was not sent while the starts hung")
        with changed:
            self.assertEqual(8, entered.count("run.start"))
        self.assertEqual(10, child.recv()["id"])  # the stop's answer comes first: the starts still hang
        release.set()
        self.assertEqual(set(range(1, 10)), {child.recv()["id"] for _ in range(9)})
        self.assertEqual(0, child.end()[0])

    def test_with_the_default_limits_a_stop_is_sent_behind_a_busy_answer_while_stdout_is_full(self) -> None:
        # Eight large answers fill the pipe nobody reads and hold the eight ordinary slots; sixteen calls
        # wait; the next is refused Server busy; the stop behind it must still be read and sent.
        stop_seen = threading.Event()
        changed = threading.Condition()
        entered: list[str] = []

        class Padded:
            def call(self, method, params=None):
                with changed:
                    entered.append(method)
                    changed.notify_all()
                if method == "run.status":
                    return {"code": "ok", "run": {"run_id": "r"}, "padding": "x" * 40000}
                if method == "run.stop":
                    stop_seen.set()
                    return {"code": "pending", "run_id": params["run_id"], "state": "pending"}
                return {"code": "ok", "runs": []}

        def status(request_id) -> dict:
            return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": "recursion_run_status", "arguments": {"run_id": "r"}}}

        self.daemon(Padded())
        child = Child(self, self.runtime)
        self.initialize(child)
        for n in range(2, 10):
            child.send(status(n))
        with changed:
            self.assertTrue(changed.wait_for(lambda: entered.count("run.status") == 8, HANG_GUARD_S))
        for n in range(10, 27):  # sixteen wait, and the seventeenth (26) is refused
            child.send(status(n))
        child.send({"jsonrpc": "2.0", "id": 27, "method": "tools/call",
                    "params": {"name": "recursion_run_stop", "arguments": {"run_id": "r"}}})
        self.assertTrue(stop_seen.wait(HANG_GUARD_S), "the stop was not sent while stdout was full")
        with changed:
            self.assertEqual(8, entered.count("run.status"))  # the waiting calls were not sent meanwhile
        answers = {}
        for _ in range(26):  # reading stdout lets everything through
            answer = child.recv()
            answers[answer["id"]] = answer
        self.assertEqual(set(range(2, 28)), set(answers))
        self.assertEqual(-32000, answers[26]["error"]["code"])
        self.assertEqual("pending", answers[27]["result"]["structuredContent"]["code"])
        self.assertEqual(0, child.end()[0])
        with changed:
            self.assertEqual((24, 1), (entered.count("run.status"), entered.count("run.stop")))

    def test_with_stderr_full_the_reader_goes_on_and_an_interrupt_exits_130(self) -> None:
        # The client never reads stderr. Six thousand cancellation lines (about 300 KB) fill that pipe and
        # the diagnostics queue; the reader still reads all of them, a ping is answered and a stop is sent.
        # An interrupt then ends the process with 130 and no fatal error at the interpreter's end: the
        # diagnostics thread waits in a write to fd 2 and holds no lock of sys.stderr.
        stop_seen = threading.Event()

        class Stops:
            def call(self, method, params=None):
                if method == "run.stop":
                    stop_seen.set()
                    return {"code": "pending", "run_id": params["run_id"], "state": "pending"}
                return {"code": "ok", "runs": []}

        self.daemon(Stops())
        child = Child(self, self.runtime)
        self.initialize(child)
        note = encode({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 999}})
        child.proc.stdin.write(note * 6000)  # returns only once the reader has read nearly all of it
        child.proc.stdin.flush()
        self.assertEqual({}, child.request(2, "ping")["result"])
        stopped = child.tool(3, "recursion_run_stop", {"run_id": "r"})
        self.assertEqual("pending", stopped["structuredContent"]["code"])
        self.assertTrue(stop_seen.is_set())
        child.proc.send_signal(signal.SIGINT)
        status = child.proc.wait(timeout=HANG_GUARD_S)
        child._guard.cancel()
        stderr = child.proc.stderr.read().decode("ascii", "replace")
        self.assertEqual(130, status, stderr[-2000:])
        self.assertNotIn("Fatal Python error", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_an_interrupt_during_the_socket_probe_exits_130_with_no_traceback(self) -> None:
        # The daemon accepts the check's run.list and never answers, so the child waits in the check's read,
        # before it has read a byte of stdin or written the serving line. An interrupt there ends it with
        # status 130 and no traceback, as an interrupt while serving does (not by the signal: status -2).
        probe_seen, release = threading.Event(), threading.Event()

        class Silent:
            def call(self, method, params=None):
                probe_seen.set()
                release.wait(HANG_GUARD_S)
                return {"code": "ok", "runs": []}

        self.daemon(Silent())
        self.addCleanup(release.set)  # runs before the server is closed
        child = Child(self, self.runtime)
        self.assertTrue(probe_seen.wait(HANG_GUARD_S), "the probe never reached the daemon")
        child.proc.send_signal(signal.SIGINT)
        status = child.proc.wait(timeout=HANG_GUARD_S)
        child._guard.cancel()
        stdout = child.proc.stdout.read()
        stderr = child.proc.stderr.read().decode("ascii", "replace")
        self.assertEqual(130, status, stderr[-2000:])
        self.assertEqual(b"", stdout)
        self.assertNotIn("Traceback", stderr)

    def test_with_stderr_full_end_of_input_still_ends_the_process_with_status_0(self) -> None:
        # The diagnostics left in the queue get at most the drain budget (2 s), then the process ends.
        self.daemon(make_api()[0])
        child = Child(self, self.runtime)
        self.initialize(child)
        note = encode({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 999}})
        child.proc.stdin.write(note * 6000)
        child.proc.stdin.flush()
        self.assertEqual({}, child.request(2, "ping")["result"])
        child.proc.stdin.close()
        status = child.proc.wait(timeout=HANG_GUARD_S)  # stderr is never read
        child._guard.cancel()
        self.assertEqual(0, status)

    def test_an_argument_the_control_frame_cannot_carry_is_answered_not_delivered(self) -> None:
        # 1.2 MiB: under the 2 MiB line limit, over the control socket's 1 MiB frame. Nothing is written.
        self.daemon(make_api()[0])
        child = Child(self, self.runtime)
        self.initialize(child)
        answer = child.tool(2, "recursion_run_stop", {"run_id": "r" * 1_200_000})
        self.assertEqual({"code": "control_api_unavailable", "delivered": False, "detail": "request:frame_too_large"},
                         answer["structuredContent"])
        self.assertEqual(["run.list"], [m["method"] for m in self.seen])  # the check's probe only
        self.assertEqual(0, child.end()[0])

    def test_a_run_id_utf8_cannot_encode_is_answered_not_delivered(self) -> None:
        # A lone surrogate is legal JSON text ("\\ud800") and passes the schema; the control socket's UTF-8
        # frame cannot carry it, so nothing is written and the answer says so.
        self.daemon(make_api()[0])
        child = Child(self, self.runtime)
        self.initialize(child)
        line = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "recursion_run_stop", "arguments": {"run_id": chr(0xD800)}}})
        child.proc.stdin.write(line.encode("ascii") + b"\n")
        child.proc.stdin.flush()
        answer = child.recv()["result"]["structuredContent"]
        self.assertEqual({"code": "control_api_unavailable", "delivered": False,
                          "detail": "request:frame_unencodable:UnicodeEncodeError"}, answer)
        self.assertEqual(["run.list"], [m["method"] for m in self.seen])  # the check's probe only
        self.assertEqual(0, child.end()[0])

    def test_stdout_that_fails_ends_the_process_with_status_0_and_sends_nothing_more(self) -> None:
        release, status_seen = threading.Event(), threading.Event()
        seen: list[str] = []

        class Gated:
            def call(self, method, params=None):
                seen.append(method)
                if method == "run.status":
                    status_seen.set()
                    release.wait(HANG_GUARD_S)
                    return {"code": "ok", "run": {"run_id": "r"}}  # small: a buffered stdout would keep it
                return {"code": "started", "run": {"run_id": "r"}} if method == "run.start" else {"code": "ok", "runs": []}

        self.daemon(Gated())
        child = Child(self, self.runtime)
        self.initialize(child)
        child.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "recursion_run_status", "arguments": {"run_id": "r"}}})
        self.assertTrue(status_seen.wait(HANG_GUARD_S))
        child.proc.stdout.close()  # the client stops reading: the status answer's write fails
        release.set()
        status = child.proc.wait()  # ends with stdin still open
        child._guard.cancel()
        try:  # a start sent now finds nobody reading it
            child.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "recursion_run_start", "arguments": {"tenant": "t", "engine": "e", "prompt": "p"}}})
        except (BrokenPipeError, OSError):
            pass
        stderr = child.proc.stderr.read().decode()
        events = [json.loads(line) for line in stderr.splitlines()]  # every line is JSON: no traceback
        self.assertEqual(0, status, stderr)
        self.assertIn({"event": "output_failed", "stdout_broken": True}, events)
        self.assertNotIn("run.start", seen)

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


class ClosedStderrTest(unittest.TestCase):
    """The process's stderr lines go to fd 2 directly: a client that closes stderr before the first line is
    written leaves nothing buffered for the interpreter's final flush (which would exit 120)."""

    def run_with_stderr_closed(self, args: list[str], stdin: bytes) -> tuple[int, bytes]:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (CORE, env.get("PYTHONPATH", "")) if p)
        proc = subprocess.Popen([sys.executable, "-B", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env)
        proc.stderr.close()  # before the process writes anything there
        proc.stderr = None   # so communicate() does not read it (on Windows it would, from a thread)
        try:
            out, _ = proc.communicate(stdin, timeout=HANG_GUARD_S)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return proc.returncode, out

    def test_serving_with_stderr_closed_ends_with_status_0(self) -> None:
        code = "from recursion_core.control.mcp.__main__ import main; raise SystemExit(main([], check=lambda env: object()))"
        status, out = self.run_with_stderr_closed(["-c", code], encode({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        self.assertEqual((0, {"jsonrpc": "2.0", "id": 1, "result": {}}), (status, json.loads(out)))

    def test_a_usage_error_with_stderr_closed_still_exits_2(self) -> None:
        status, out = self.run_with_stderr_closed(["-m", "recursion_core.control.mcp", "--stdio"], b"")
        self.assertEqual((2, b""), (status, out))


if __name__ == "__main__":
    unittest.main()
