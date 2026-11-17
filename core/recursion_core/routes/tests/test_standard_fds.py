"""The attended console tool must not hand a shut standard descriptor to a pipe it opens.

``attended_codex.py`` is a console script under core/. Its first subprocess opens pipes.
With fd 0 or fd 2 not open, one of those pipes takes that number. The tool opens no control
connection; the check is that no socket exists and that fd 0, 1 and 2 are only the
inherited harness pipes or the null device.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

LINUX = sys.platform.startswith("linux") and os.path.isdir("/proc/self/fd")
GUARD_S = 15
TOOL = Path(__file__).resolve().parents[1] / "tools" / "attended_codex.py"


def _launcher(argv: list[str], shut_fd: int) -> str:
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


def _descendants(pid: int) -> list[int]:
    try:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text()
    except OSError:
        return []
    found = []
    for item in text.split():
        child = int(item)
        found.append(child)
        found.extend(_descendants(child))
    return found


def _sockets(pid: int) -> list[tuple[str, str]]:
    found = []
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return found
    for name in names:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{name}")
        except OSError:
            continue
        if target.startswith("socket:"):
            found.append((name, target))
    return found


@unittest.skipUnless(LINUX, "needs Linux /proc")
class AttendedToolStandardFdTest(unittest.TestCase):
    def _assert_standard(self, pid: int, shut_fd: int, inherited: set[str]) -> None:
        targets = _targets(pid)
        for fd, target in targets.items():
            self.assertFalse(target.startswith("socket:"), targets)
            if target.startswith("pipe:"):
                self.assertIn(target, inherited, targets)
            elif target.startswith("/"):
                self.assertEqual("/dev/null", target, targets)
            else:
                self.fail(repr(targets))
        self.assertEqual("/dev/null", targets[shut_fd], targets)
        self.assertEqual([], _sockets(pid))

    def test_attended_tool_with_fd_2_not_open_keeps_its_pipes_off_stdio(self) -> None:
        self._run(2)

    def test_attended_tool_with_fd_0_not_open_keeps_its_pipes_off_stdio(self) -> None:
        self._run(0)

    def _run(self, shut_fd: int) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="rc-attended-fd-", dir=os.environ.get("TMPDIR")))
        os.chmod(tmp, 0o700)
        fake = tmp / "codex"
        fake.write_text("#!/bin/sh\n[ \"$1\" = --version ] || exit 0\nexec sleep 30\n")
        fake.chmod(0o700)
        out = tmp / "out"
        out.mkdir()
        argv = [sys.executable, "-B", str(TOOL), "schema", "--out", str(out), "--codex", str(fake)]
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp),
            "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        proc = subprocess.Popen(
            [sys.executable, "-B", "-c", _launcher(argv, shut_fd)],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        inherited = _inherited_pipes(proc)

        def finish() -> None:
            if proc.poll() is None:
                for child in _descendants(proc.pid):
                    try:
                        os.kill(child, signal.SIGKILL)
                    except OSError:
                        pass
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            shutil.rmtree(tmp, True)

        self.addCleanup(finish)
        deadline = time.monotonic() + GUARD_S
        while time.monotonic() < deadline and proc.poll() is None and not _descendants(proc.pid):
            time.sleep(0.02)
        self.assertIsNone(proc.poll(), "the tool ended before it opened a pipe")
        self.assertTrue(_descendants(proc.pid), "the tool never started its child")
        self._assert_standard(proc.pid, shut_fd, inherited)


if __name__ == "__main__":
    unittest.main()
