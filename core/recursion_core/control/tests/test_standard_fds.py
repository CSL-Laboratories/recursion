"""Stop, the TUI and the Control MCP server must not hand a shut standard descriptor to a socket.

A process started with fd 0 or fd 2 not open gives that number to its next socket. These
entries open the daemon's control connection. The socket must not be fd 0, 1 or 2, and the
connection must not carry bytes that are not the request frame.
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
from recursion_core.control_transport import UnixControlServer

CAN_SERVE = hasattr(os, "geteuid") and hasattr(__import__("socket"), "AF_UNIX") and os.path.isdir("/proc/self/fd")
CORE = str(Path(recursion_core.__file__).resolve().parent.parent)
GUARD_S = 15


def _launcher(argv: list[str], shut_fd: int) -> str:
    """Shut one standard descriptor, then replace this process with the entry."""

    return "import os, sys\n" f"os.close({int(shut_fd)})\n" f"os.execv(sys.executable, {list(argv)!r})\n"


def _targets(pid: int) -> dict[int, str]:
    found = {}
    for fd in (0, 1, 2):
        try:
            found[fd] = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError as exc:
            found[fd] = f"absent:{type(exc).__name__}"
    return found


def _inherited_pipes(proc: subprocess.Popen) -> set[str]:
    found = set()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            found.add(os.readlink(f"/proc/self/fd/{stream.fileno()}"))
        except OSError:
            pass
    return found


def _peek(server: UnixControlServer) -> bytes:
    """Bytes waiting on the daemon's accepted sockets, left in place."""

    extra = bytearray()
    for sock in list(server._conns):
        try:
            sock.setblocking(False)
            chunk = sock.recv(65536, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError, OSError):
            chunk = b""
        if chunk:
            extra.extend(chunk)
    return bytes(extra)


@unittest.skipUnless(CAN_SERVE, "needs Linux, AF_UNIX and /proc")
class StandardFdEntryTest(unittest.TestCase):
    def _assert_standard(self, pid: int, shut_fd: int, inherited: set[str]) -> dict[int, str]:
        targets = _targets(pid)
        for fd, target in targets.items():
            self.assertFalse(target.startswith("socket:"), targets)
            self.assertFalse(target.startswith("anon_inode:"), targets)
            if target.startswith("pipe:"):
                self.assertIn(target, inherited, targets)
            elif target.startswith("/"):
                self.assertEqual("/dev/null", target, targets)
            else:
                self.fail(targets)
        self.assertEqual("/dev/null", targets[shut_fd], targets)
        return targets

    def _start(self, argv: list[str], shut_fd: int, hold_method: str, result: dict):
        runtime = tempfile.mkdtemp(prefix="rc-stdio-")
        os.chmod(runtime, 0o700)
        seen: list[dict] = []
        held, release = threading.Event(), threading.Event()

        def handle(message, _connection):
            seen.append(message)
            if message.get("method") == hold_method:
                held.set()
                self.assertTrue(release.wait(GUARD_S), "the test never finished looking at the open socket")
            return {"ok": True, "result": result}

        server = UnixControlServer(os.path.join(runtime, "recursion"), handle)
        server.start()
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": runtime,
            "XDG_RUNTIME_DIR": runtime,
            "XDG_CONFIG_HOME": os.path.join(runtime, "config"),
            "PYTHONPATH": CORE,
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        proc = subprocess.Popen(
            [sys.executable, "-B", "-c", _launcher(argv, shut_fd)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        inherited = _inherited_pipes(proc)

        def finish() -> None:
            release.set()
            if proc.poll() is None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            server.close()
            shutil.rmtree(runtime, True)

        self.addCleanup(finish)
        return proc, server, seen, held, release, inherited

    def _run(self, argv: list[str], shut_fd: int, hold_method: str, result: dict, expect: int) -> bytes:
        proc, server, seen, held, release, inherited = self._start(argv, shut_fd, hold_method, result)
        self.assertTrue(held.wait(GUARD_S), "the control connection never saw the call")
        self._assert_standard(proc.pid, shut_fd, inherited)
        self.assertEqual(b"", _peek(server))
        matched = [message for message in seen if message.get("method") == hold_method]
        self.assertEqual(1, len(matched), seen)
        release.set()
        try:
            out, err = proc.communicate(timeout=GUARD_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate(timeout=5)
            self.fail(f"the entry did not end: out={out!r} err={err!r}")
        self.assertEqual(expect, proc.returncode, (out, err))
        self.assertNotIn(b"Traceback", err)
        return out

    def test_stop_with_fd_2_not_open_keeps_our_socket_off_stdio(self) -> None:
        out = self._run(
            [sys.executable, "-B", "-m", "recursion_core.control", "stop", "run-1"],
            2, "run.stop",
            {"code": "pending", "run_id": "run-1", "state": "pending"},
            3,
        )
        self.assertEqual({"code": "pending", "run_id": "run-1", "state": "pending"}, json.loads(out))

    def test_stop_with_fd_0_not_open_keeps_our_socket_off_stdio(self) -> None:
        out = self._run(
            [sys.executable, "-B", "-m", "recursion_core.control", "stop", "run-1"],
            0, "run.stop",
            {"code": "pending", "run_id": "run-1", "state": "pending"},
            3,
        )
        self.assertEqual({"code": "pending", "run_id": "run-1", "state": "pending"}, json.loads(out))

    def test_tui_with_fd_2_not_open_keeps_our_socket_off_stdio(self) -> None:
        self._run(
            [sys.executable, "-B", "-m", "recursion_core.control.tui"],
            2, "run.list", {"code": "ok", "runs": []}, 0,
        )

    def test_tui_with_fd_0_not_open_keeps_our_socket_off_stdio(self) -> None:
        self._run(
            [sys.executable, "-B", "-m", "recursion_core.control.tui"],
            0, "run.list", {"code": "ok", "runs": []}, 0,
        )

    def test_mcp_with_fd_0_not_open_keeps_our_socket_off_stdio(self) -> None:
        # The same helper the MCP entry already calls fills fd 0 as well as fd 2.
        # The socket check's run.list is the first control connection.
        self._run(
            [sys.executable, "-B", "-m", "recursion_core.control.mcp"],
            0, "run.list", {"code": "ok", "runs": []}, 0,
        )


if __name__ == "__main__":
    unittest.main()
