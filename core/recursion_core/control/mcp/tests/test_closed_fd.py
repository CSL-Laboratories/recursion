"""A server started with fd 2 not open must not hand that number to a control socket.

Diagnostics are written to fd 2 by number. If that descriptor is not open at startup, the next
socket the process opens takes it, and a line written while a call is in flight goes into the
daemon's connection. A Stop is then lost, or answered as delivered when the daemon read garbage.
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
import time
import unittest
from pathlib import Path

import recursion_core
from recursion_core.control.mcp.framing import encode
from recursion_core.control.mcp.tests.support import HANG_GUARD_S
from recursion_core.control_transport import UnixControlServer

CAN_SERVE = hasattr(socket, "AF_UNIX") and hasattr(socket, "SO_PEERCRED") and hasattr(os, "geteuid")
CORE = str(Path(recursion_core.__file__).resolve().parent.parent)
CLIENT = {"name": "scripted-test-client", "version": "0"}
# Close fd 2, then replace this process with the server. The descriptor stays not open across exec.
_LAUNCH = (
    "import os, sys\n"
    "os.close(2)\n"
    "os.execv(sys.executable, [sys.executable, '-B', '-m', 'recursion_core.control.mcp'])\n"
)


def _standard_targets(pid: int) -> dict[int, str]:
    found = {}
    for fd in (0, 1, 2):
        try:
            found[fd] = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError as exc:
            found[fd] = f"absent:{type(exc).__name__}"
    return found


def _buffered(socks: list[socket.socket]) -> bytes:
    """Bytes already waiting on the daemon's accepted sockets, left in place for the server to read."""

    extra = bytearray()
    for sock in socks:
        try:
            sock.setblocking(False)
            chunk = sock.recv(65536, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError, OSError):
            chunk = b""
        if chunk:
            extra.extend(chunk)
    return bytes(extra)


@unittest.skipUnless(CAN_SERVE, "the owner-only transport needs AF_UNIX, SO_PEERCRED and os.geteuid (Linux)")
class ClosedFdTest(unittest.TestCase):
    def test_a_stop_with_fd_2_not_open_is_pending_and_the_socket_is_not_fd_2(self) -> None:
        runtime = tempfile.mkdtemp(prefix="mcp-fd-")
        self.addCleanup(shutil.rmtree, runtime, True)
        seen: list[dict] = []
        held, noted = threading.Event(), threading.Event()
        trailing: list[bytes] = []

        def handle(message, _connection):
            seen.append(message)
            method = message.get("method")
            if method == "run.stop":
                held.set()
                self.assertTrue(noted.wait(HANG_GUARD_S), "the test never finished looking at the open socket")
                trailing.append(_buffered(list(server._conns)))
                run_id = (message.get("params") or {}).get("run_id")
                return {"ok": True, "result": {"code": "pending", "run_id": run_id, "state": "pending"}}
            return {"ok": True, "result": {"code": "ok", "runs": []}}

        server = UnixControlServer(os.path.join(runtime, "recursion"), handle)
        server.start()
        self.addCleanup(server.close)

        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = runtime
        env["PYTHONPATH"] = os.pathsep.join(p for p in (CORE, env.get("PYTHONPATH", "")) if p)
        proc = subprocess.Popen([sys.executable, "-B", "-c", _LAUNCH],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        guard = threading.Timer(HANG_GUARD_S, proc.kill)
        guard.daemon = True
        guard.start()

        def cleanup() -> None:
            guard.cancel()
            noted.set()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass

        self.addCleanup(cleanup)

        def request(request_id, method: str, params: dict | None = None) -> dict:
            message = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            proc.stdin.write(encode(message))
            proc.stdin.flush()
            line = proc.stdout.readline()
            self.assertTrue(line, "the server ended without answering")
            answer = json.loads(line)
            self.assertEqual(request_id, answer.get("id"), answer)
            return answer

        request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": CLIENT})
        proc.stdin.write(encode({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        proc.stdin.flush()

        answers: list[dict] = []

        def ask_stop() -> None:
            answers.append(request(2, "tools/call",
                                   {"name": "recursion_run_stop", "arguments": {"run_id": "run-1"}}))

        worker = threading.Thread(target=ask_stop)
        worker.start()
        self.assertTrue(held.wait(HANG_GUARD_S), "the daemon never saw the Stop")
        targets = _standard_targets(proc.pid)
        during = _buffered(list(server._conns))
        # A cancellation for an unknown id logs a line to fd 2 while the Stop's socket is still open.
        proc.stdin.write(encode({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                 "params": {"requestId": 999}}))
        proc.stdin.flush()
        noted.set()
        worker.join(HANG_GUARD_S)
        self.assertFalse(worker.is_alive(), "the Stop was not answered")

        for fd, target in targets.items():
            self.assertFalse(target.startswith("socket:"), (fd, targets))
        self.assertEqual("/dev/null", targets[2], targets)
        self.assertEqual(b"", during + b"".join(trailing))
        result = answers[0]["result"]
        self.assertEqual(("pending", False), (result["structuredContent"]["code"], result["isError"]))
        stops = [message for message in seen if message.get("method") == "run.stop"]
        self.assertEqual(1, len(stops), seen)
        self.assertEqual({"run_id": "run-1"}, stops[0].get("params"))

    def test_a_second_interrupt_a_millisecond_later_still_exits_130(self) -> None:
        # One SIGINT while serving ends at 130. A second one in the next few milliseconds used to kill the
        # process by the signal (status -2) during the interpreter's shutdown. Status stays 130, no traceback.
        runtime = tempfile.mkdtemp(prefix="mcp-sig-")
        self.addCleanup(shutil.rmtree, runtime, True)

        def handle(message, _connection):
            return {"ok": True, "result": {"code": "ok", "runs": []}}

        server = UnixControlServer(os.path.join(runtime, "recursion"), handle)
        server.start()
        self.addCleanup(server.close)
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = runtime
        env["PYTHONPATH"] = os.pathsep.join(p for p in (CORE, env.get("PYTHONPATH", "")) if p)
        proc = subprocess.Popen([sys.executable, "-B", "-m", "recursion_core.control.mcp"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        guard = threading.Timer(HANG_GUARD_S, proc.kill)
        guard.daemon = True
        guard.start()

        def cleanup() -> None:
            guard.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass

        self.addCleanup(cleanup)
        serving = proc.stderr.readline()
        self.assertIn(b"serving", serving, serving)
        proc.send_signal(signal.SIGINT)
        deadline = time.monotonic() + 0.005  # inside the old shutdown window (about 7 to 15 ms)
        while time.monotonic() < deadline and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            time.sleep(0.0005)
        err = proc.stderr.read()
        status = proc.wait(timeout=HANG_GUARD_S)
        self.assertEqual(130, status, err)
        self.assertNotIn(b"Traceback", err)


if __name__ == "__main__":
    unittest.main()
