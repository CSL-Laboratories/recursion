"""The founder-attended tool's run mode (routes/tools/attended_codex.py) without a real Codex (lane DRIVER-CX).

The session loop is driven with the stand-in app-server of test_codex_driver; the local process wrapper is
driven with a tiny Python child that echoes lines reversed. No real codex, credential or network is used.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import AppServer, Launcher, agent, ask_command, done

TOOL = Path(__file__).resolve().parents[1] / "tools" / "attended_codex.py"


def tool():
    spec = importlib.util.spec_from_file_location("attended_codex_run_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AnsweringServer(AppServer):
    """The stand-in, plus: when the client answers server request ``rid``, emit ``after[rid]`` (the turn goes on)."""

    def __init__(self, after, **kw):
        super().__init__(**kw)
        self.after = after

    def send(self, line):
        super().send(line)
        msg = json.loads(line)
        if "method" not in msg and msg.get("id") in self.after:
            for note in self.after.pop(msg["id"]):
                self.emit(note(f"trn-{self.turns}"))


class SessionLoopTest(unittest.TestCase):
    def setUp(self):
        self.tool = tool()
        self.said: list[str] = []

    def run_loop(self, servers, commands):
        self.launcher = Launcher(*servers)
        console = self.tool.Console(self.said.append)
        driver = cx.CodexDriver(self.launcher, FakeClock(), approvals=console)
        return self.tool.session_loop(driver, iter(commands), self.said.append, console, read_s=0.0,
                                      env={"PATH": "/usr/bin"})

    def accepted(self, seen):
        return [(d["payload"]["delivery_id"], d["payload"]["turn_id"]) for d in seen["drafts"]
                if d["type"] == "input.accepted"]

    def test_two_turns_stop_and_resume(self):
        first = AppServer(script={1: [agent("three files"), done()], 2: [agent("3"), done()]})
        second = AppServer(script={1: [agent("still here"), done()]})
        seen = self.run_loop([first, second], ["list the files", "count them", "/stop", "/resume", "and now?",
                                               "/quit"])
        self.assertEqual(seen["threads"], ["thr-1", "thr-1"])
        self.assertEqual(seen["workers"], 2)
        self.assertEqual(self.accepted(seen), [("dlv-1", "turn-1"), ("dlv-2", "turn-2"), ("dlv-3", "turn-3")])
        self.assertTrue(first.stdin_closed)
        self.assertEqual([m.get("method") for m in second.sent][:3], ["initialize", "initialized", "thread/resume"])
        self.assertEqual(self.launcher.calls[0]["env"], {"PATH": "/usr/bin"})

    def test_deny_then_approve(self):
        server = AnsweringServer({70: [done()], 71: [done()]}, script={1: [ask_command(70, "touch a.txt", "it-1")],
                                                                       2: [ask_command(71, "touch a.txt", "it-2")]})
        self.run_loop([server], ["create a.txt", "n", "create a.txt again", "y", "/quit"])
        answers = {m["id"]: m["result"]["decision"] for m in server.sent if "method" not in m and "result" in m}
        self.assertEqual(answers, {70: "decline", 71: "accept"})
        self.assertTrue(any(s.startswith("APPROVAL apr-attended-1") and "touch a.txt" in s for s in self.said))

    def test_an_answer_to_an_approval_that_is_no_longer_waiting_is_refused_not_fatal(self):
        server = AppServer(script={1: [ask_command(70, "rm -rf x", "it-1")], 2: [done()]})
        seen = self.run_loop([server], ["delete x", "/interrupt", "n", "hello", "/quit"])
        self.assertTrue(any(s.startswith("refused:") for s in self.said))
        self.assertFalse([m for m in server.sent if m.get("id") == 70 and "method" not in m])
        self.assertIn(("dlv-2", "turn-2"), self.accepted(seen))
        self.assertIn("turn/interrupt", [m.get("method") for m in server.sent])

    def test_the_child_environment_keeps_no_api_key(self):
        parent = {"PATH": "/usr/bin", "HOME": "/home/l", "CODEX_HOME": "/home/l/.codex", "OPENAI_API_KEY": "FAKE-1",
                  "CODEX_API_KEY": "FAKE-2", "CODEX_ACCESS_TOKEN": "FAKE-3", "ANTHROPIC_API_KEY": "FAKE-4",
                  "Path": "C:\\Windows", "SECRET_THING": "FAKE-5"}
        env = self.tool.child_env(parent)
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/home/l", "CODEX_HOME": "/home/l/.codex",
                               "Path": "C:\\Windows"})


class LocalProcessTest(unittest.TestCase):
    def test_it_speaks_lines_both_ways_and_captures_them(self):
        tool_module = tool()
        out = Path(tempfile.mkdtemp(prefix="attended-"))
        self.addCleanup(shutil.rmtree, out, True)
        echo = "import sys\nfor line in sys.stdin:\n    print(line.strip()[::-1], flush=True)\n"
        proc = tool_module.LocalProcess([sys.executable, "-I", "-c", echo], env=None, cwd=str(out), out=out,
                                        tag="worker-1")
        self.addCleanup(proc.fence, "test_over")
        proc.send(json.dumps({"a": 1}))
        self.assertEqual(proc.read_line(60), "}1 :\"a\"{")
        proc.close_stdin()
        self.assertIsNone(proc.read_line(60))
        self.assertEqual((out / "worker-1.stdin.jsonl").read_text(encoding="utf-8"), '{"a": 1}\n')
        self.assertEqual((out / "worker-1.stdout.jsonl").read_text(encoding="utf-8"), '}1 :"a"{\n')


if __name__ == "__main__":
    unittest.main()
