"""Answers still open on draft #465 after the grading-interrupt reply.

A second interruption during the reap must still reap the engine and must leave a note.
An error from session cleanup must not replace the interruption. A live stranger whose
pid is a session id must not be signalled. An interruption before the engine is tracked
must still end that process. A huge integer cost must not drop a success line.
"""
from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import arms

SAFE_ENV = {key: os.environ[key] for key in ("PATH", "TMPDIR", "TEMP", "TMP", "XDG_RUNTIME_DIR") if key in os.environ}


def _end(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.kill(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class SecondReapTests(unittest.TestCase):
    def test_a_second_interrupt_during_the_reap_still_reaps_the_engine(self):
        made = []
        original = subprocess.Popen
        calls = {"n": 0}

        def launch(*args, **kwargs):
            made.append(original(*args, **kwargs))
            return made[-1]

        def interrupted_wait(process, timeout=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise KeyboardInterrupt("synthetic")
            return original.wait(process, timeout=timeout)

        folder = Path(os.environ.get("TMPDIR", "/tmp"))
        try:
            with patch.object(arms.subprocess, "Popen", side_effect=launch), \
                    patch.object(original, "wait", interrupted_wait):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    arms.run_process([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                     cwd=folder, env=SAFE_ENV, stdin=None, timeout_s=5)
            child = made[0]
            notes = " ".join(getattr(caught.exception, "__notes__", []))
            self.assertIsNotNone(child.returncode, "second interruption left the engine unreaped")
            self.assertIn("second interruption", notes)
        finally:
            for process in made:
                _end(process)


class CleanupErrorTests(unittest.TestCase):
    def test_a_cleanup_error_does_not_replace_the_interruption(self):
        class Process:
            pid = 515151

            def __init__(self):
                self.returncode = None
                self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

            def wait(self, timeout=None):
                if not getattr(self, "seen", False):
                    self.seen = True
                    raise KeyboardInterrupt("cancel")
                self.returncode = -9
                return -9

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -9

        for kind, exc in (("os", OSError("synthetic /proc failure")),
                          ("runtime", RuntimeError("refusing to signal the runner session"))):
            with self.subTest(kind=kind):
                with patch.object(arms.subprocess, "Popen", return_value=Process()), \
                        patch.object(arms, "_kill_session", side_effect=exc):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        arms.run_process(["synthetic"], cwd=Path(os.environ.get("TMPDIR", "/tmp")),
                                         env={}, stdin=None, timeout_s=1)
                notes = " ".join(getattr(caught.exception, "__notes__", []))
                self.assertIn(str(exc), notes)


class ReusedSessionTests(unittest.TestCase):
    def _sleeper(self) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(30)"],
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def test_a_bare_session_id_does_not_signal_a_live_stranger(self):
        proc = self._sleeper()
        try:
            arms._kill_session(proc.pid)
            self.assertIsNone(proc.poll(), "a bare session id killed a stranger")
        finally:
            _end(proc)

    def test_a_pidfd_from_start_still_signals_that_leader(self):
        proc = self._sleeper()
        fd = None
        try:
            fd = os.pidfd_open(proc.pid)
            proc._csl_pidfd = fd
            arms._kill_session(proc.pid, leader=proc)
            self.assertIsNotNone(proc.wait(timeout=2), "the leader the pidfd names was left alive")
        finally:
            if isinstance(fd, int):
                os.close(fd)
                proc._csl_pidfd = None
            _end(proc)


class UntrackedEngineTests(unittest.TestCase):
    def test_an_interrupt_before_track_ends_the_engine(self):
        made = []
        original = subprocess.Popen

        def launch(*args, **kwargs):
            made.append(original(*args, **kwargs))
            return made[-1]

        def boom(process, cancel=True):
            raise KeyboardInterrupt("before track")

        folder = Path(os.environ.get("TMPDIR", "/tmp"))
        try:
            with patch.object(arms.subprocess, "Popen", side_effect=launch), \
                    patch.object(arms, "_track", side_effect=boom):
                with self.assertRaises(KeyboardInterrupt):
                    arms.run_process([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                     cwd=folder, env=SAFE_ENV, stdin=None, timeout_s=5)
            child = made[0]
            self.assertIsNotNone(child.poll(), "the engine lived past an interrupt before it was tracked")
            self.assertNotIn(child, arms._live)
        finally:
            for process in made:
                _end(process)


class HugeCostTests(unittest.TestCase):
    def test_a_huge_integer_cost_keeps_the_success_and_drops_the_cost(self):
        for digits in (401, 4301):
            with self.subTest(digits=digits):
                line = (b'{"type":"result","subtype":"success","is_error":false,"total_cost_usd":1'
                        + b"0" * (digits - 1) + b"}")
                parsed = arms.parse_stream_json(line, cost_basis="engine")
                self.assertTrue(parsed["claimed_done"])
                self.assertTrue(parsed["has_result"])
                self.assertIsNone(parsed["usage"]["cost"])


if __name__ == "__main__":
    unittest.main()
