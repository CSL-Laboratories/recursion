# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""The shared ACP client over a real OS pipe: the fake agent runs as its own process (``fake_acp_agent.main``).

These cases add what the in-process fake cannot show: lines split and buffered by a real pipe, the end of a
child's output when it exits, and its exit on the end of its input. Time is real here, but nothing waits on
it: each case synchronises on the agent's answers, and the deadline only turns a hang into a failure.

Run: PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.antigravity.test_antigravity_acp_pipe
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from recursion_core.routes import contract as c
from recursion_core.routes.engines.fixtures.antigravity import acp
from recursion_core.routes.launch import ReadTimeout

CORE = Path(__file__).resolve().parents[5]
MODULE = "recursion_core.routes.engines.fixtures.antigravity.fake_acp_agent"
CWD = "/var/tmp/workspace"
WATCHDOG_S = 60.0   # only a hang reaches it


class PipeProcess:
    """An ``EngineProcess`` over a child's stdin and stdout, for these tests only."""

    def __init__(self, scenario: dict) -> None:
        env = dict(os.environ, PYTHONPATH=str(CORE))
        self.proc = subprocess.Popen([sys.executable, "-B", "-m", MODULE, json.dumps(scenario)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, text=True, encoding="utf-8", bufsize=1)
        self._lines: queue.Queue = queue.Queue()
        self._ended = False
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line.rstrip("\n"))
        self._lines.put(None)

    def send(self, line: str) -> None:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def close_stdin(self) -> None:
        self.proc.stdin.close()

    def read_line(self, timeout_s: float):
        if self._ended:
            return None
        try:
            line = self._lines.get(timeout=min(timeout_s, WATCHDOG_S))
        except queue.Empty:
            raise ReadTimeout() from None
        if line is None:
            self._ended = True
        return line

    def fence(self, reason: str) -> None:
        self.proc.kill()

    def finish(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(WATCHDOG_S)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream and not stream.closed:
                stream.close()


class PipeTest(unittest.TestCase):
    def start(self, scripts, **scenario) -> tuple[PipeProcess, acp.AcpSession, float]:
        process = PipeProcess({"scripts": scripts, **scenario})
        self.addCleanup(process.finish)
        deadline = time.monotonic() + WATCHDOG_S
        session = acp.AcpSession(process, time.monotonic, **scenario.pop("session", {}))
        return process, session, deadline

    def test_a_turn_over_a_real_pipe(self) -> None:
        process, session, deadline = self.start([[["chunk", "over "], ["chunk", "a pipe"], ["end", "end_turn"]]])
        self.assertEqual(session.open("gemini-api-key", CWD, deadline), "fake-session-1")
        out = session.prompt("hello", deadline)
        self.assertIsNone(out.code)
        self.assertEqual(out.text, "over a pipe")
        session.close()
        self.assertEqual(process.proc.wait(WATCHDOG_S), 0)   # the agent exits on the end of its input

    def test_a_permission_ask_answered_over_a_pipe(self) -> None:
        tool_call = {"toolCallId": "call-1", "title": "Run ls", "kind": "execute", "status": "pending"}
        options = [{"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                   {"optionId": "no", "name": "Deny", "kind": "reject_once"}]
        holder = {}

        def on_ask(ask):
            holder["answered"] = holder["s"].answer_permission(ask.token, acp.APPROVE)

        process = PipeProcess({"scripts": [[["ask", tool_call, options], ["chunk", "ran"], ["end", "end_turn"]]]})
        self.addCleanup(process.finish)
        deadline = time.monotonic() + WATCHDOG_S
        holder["s"] = session = acp.AcpSession(process, time.monotonic, on_ask=on_ask)
        session.open("gemini-api-key", CWD, deadline)
        out = session.prompt("x", deadline)
        self.assertTrue(holder["answered"])
        self.assertEqual(out.text, "ran")

    def test_a_cancel_over_a_pipe_is_confirmed_by_the_agent(self) -> None:
        holder = {}
        process = PipeProcess({"scripts": [[["chunk", "working"], ["wait_cancel"], ["end", "cancelled"]]]})
        self.addCleanup(process.finish)
        deadline = time.monotonic() + WATCHDOG_S
        holder["s"] = session = acp.AcpSession(process, time.monotonic, on_update=lambda _p: holder["s"].cancel())
        session.open("gemini-api-key", CWD, deadline)
        out = session.prompt("x", deadline)
        self.assertEqual(out.code, acp.CANCELLED)
        self.assertTrue(out.cancel_confirmed)

    def test_the_agent_exiting_mid_turn_is_engine_exited(self) -> None:
        process, session, deadline = self.start([[["chunk", "half"], ["die"]]])
        session.open("gemini-api-key", CWD, deadline)
        out = session.prompt("x", deadline)
        self.assertEqual(out.code, c.ENGINE_EXITED)
        self.assertEqual(process.proc.wait(WATCHDOG_S), 0)

    def test_a_method_the_agent_does_not_offer_never_reaches_it(self) -> None:
        process, session, deadline = self.start([], methods=["gemini-api-key"])
        with self.assertRaises(acp.AcpRefused):
            session.open("agent-platform", CWD, deadline)
        session.close()
        self.assertEqual(process.proc.wait(WATCHDOG_S), 0)


if __name__ == "__main__":
    unittest.main()
