"""The founder-attended tool's run mode (routes/tools/attended_codex.py) without a real Codex (lane DRIVER-CX).

The session loop is driven with the stand-in app-server of test_codex_driver; the local process wrapper is
driven with a tiny Python child that echoes lines reversed. No real codex, credential or network is used.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests import test_codex_driver as standin
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import Launcher, Raw, agent, ask_command, done

TOOL = Path(__file__).resolve().parents[1] / "tools" / "attended_codex.py"


class AppServer(standin.AppServer):
    """The driver tests' stand-in, with a bound: a loop that keeps reading a worker with nothing to say fails
    here instead of hanging."""

    EMPTY_READS = 500

    def read_line(self, timeout_s: float):
        if not self.out:
            self.empty_reads = getattr(self, "empty_reads", 0) + 1
            if self.empty_reads > self.EMPTY_READS:
                raise AssertionError(f"the loop read an empty worker {self.EMPTY_READS} times")
        return super().read_line(timeout_s)


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


class LateTyping:
    """Commands a person types only once the worker's output so far has been read (``ready()`` is the hook
    ``session_loop`` asks during a turn, as for the keyboard)."""

    def __init__(self, server, commands):
        self.server, self.commands = server, list(commands)

    def __iter__(self):
        return self

    def __next__(self):
        if not self.commands:
            raise StopIteration
        return self.commands.pop(0)

    def ready(self):
        return not self.server.out


class SwitchingLateTyping(LateTyping):
    """LateTyping across a /stop and /resume: ready when every worker's output so far has been read."""

    def __init__(self, servers, commands):
        super().__init__(servers[0], commands)
        self.servers = servers

    def ready(self):
        return all(not s.out for s in self.servers)


class SessionLoopTest(unittest.TestCase):
    def setUp(self):
        self.tool = tool()
        self.said: list[str] = []

    def run_loop(self, servers, commands):
        self.launcher = Launcher(*servers)
        console = self.tool.Console(self.said.append)
        driver = cx.CodexDriver(self.launcher, FakeClock(), approvals=console)
        source = commands if hasattr(commands, "ready") else iter(commands)
        return self.tool.session_loop(driver, source, self.said.append, console, read_s=0.0,
                                      env={"PATH": "/usr/bin"})

    def accepted(self, seen):
        return [(d["payload"]["delivery_id"], d["payload"]["turn_id"]) for d in seen["drafts"]
                if d["type"] == "input.accepted"]

    def test_two_turns_stop_and_resume(self):
        first = AppServer(script={1: [agent("three files"), done()], 2: [agent("3"), done()]})
        second = AppServer(script={1: [agent("still here"), done()]})
        # The person types each command once the worker's output so far has been read (round 2 of #402: a
        # command typed during a turn is read at once, and /stop would act before the second message).
        seen = self.run_loop([first, second], SwitchingLateTyping([first, second], [
            "list the files", "count them", "/stop", "/resume", "and now?", "/quit"]))
        self.assertEqual(seen["threads"], ["thr-1", "thr-1"])
        self.assertEqual(seen["workers"], 2)
        self.assertEqual(self.accepted(seen), [("dlv-1", "turn-1"), ("dlv-2", "turn-2"), ("dlv-3", "turn-3")])
        self.assertTrue(first.stdin_closed)
        self.assertEqual([m.get("method") for m in second.sent][:3], ["initialize", "initialized", "thread/resume"])
        self.assertEqual(self.launcher.calls[0]["env"], {"PATH": "/usr/bin"})

    def test_deny_then_approve(self):
        server = AnsweringServer({70: [done()], 71: [done()]}, script={1: [ask_command(70, "touch a.txt", "it-1")],
                                                                       2: [ask_command(71, "touch a.txt", "it-2")]})
        self.run_loop([server], LateTyping(server, ["create a.txt", "n", "create a.txt again", "y", "/quit"]))
        answers = {m["id"]: m["result"]["decision"] for m in server.sent if "method" not in m and "result" in m}
        self.assertEqual(answers, {70: "decline", 71: "accept"})
        self.assertTrue(any(s.startswith("APPROVAL apr-attended-1") and "touch a.txt" in s for s in self.said))

    def test_an_answer_to_an_approval_that_is_no_longer_waiting_is_refused_not_fatal(self):
        server = AppServer(script={1: [ask_command(70, "rm -rf x", "it-1")], 2: [done()]})
        # The person answers only after the interrupt has ended the turn, so the approval is gone by then.
        seen = self.run_loop([server], LateTyping(server, ["delete x", "/interrupt", "n", "hello", "/quit"]))
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


class AttendedReviewTest(unittest.TestCase):
    """The two reads of #402: GPT-6 Astra R6 and R7, DeepSeek F2 and F5."""

    def setUp(self):
        self.tool = tool()
        self.said: list[str] = []

    def loop(self, server_or_servers, commands, launcher=None):
        servers = server_or_servers if isinstance(server_or_servers, list) else [server_or_servers]
        console = self.tool.Console(self.said.append)
        driver = cx.CodexDriver(launcher or Launcher(*servers), FakeClock(), approvals=console)
        return self.tool.session_loop(driver, commands, self.said.append, console, read_s=0.0, env={})

    def test_interrupt_is_read_while_a_turn_runs(self):
        consumed = []

        class ReadBudget(Exception):
            pass

        class BudgetServer(AppServer):
            reads_empty = 0

            def read_line(self, timeout_s):
                if not self.out:
                    self.reads_empty += 1
                    if self.reads_empty == 4:
                        raise ReadBudget()
                return super().read_line(timeout_s)

        def commands():
            for command in ("hello", "/interrupt", "/quit"):
                consumed.append(command)
                yield command
        server = BudgetServer()
        try:
            self.loop(server, commands())
        except ReadBudget:
            pass
        self.assertIn("/interrupt", consumed)  # four read opportunities were enough: no wall-clock sleep

    def test_an_interrupt_typed_during_a_turn_reaches_the_engine_and_ends_it(self):
        class Budget(AppServer):  # its turn never ends by itself; a loop that never acts fails, not hangs
            reads_empty = 0

            def read_line(self, timeout_s):
                if not self.out:
                    self.reads_empty += 1
                    if self.reads_empty > 50:
                        raise AssertionError("the interrupt was never acted on")
                return super().read_line(timeout_s)
        server = Budget()
        seen = self.loop(server, LateTyping(server, ["hello", "/interrupt", "/quit"]))
        self.assertIn("turn/interrupt", [m.get("method") for m in server.sent])
        self.assertIn({"code": "interrupted", "detail": "the turn was interrupted"},
                      [d["payload"] for d in seen["drafts"] if d["type"] == "result"])

    def test_a_message_typed_during_a_turn_waits_for_its_end(self):
        server = AppServer(script={1: [agent("a"), done()], 2: [done()]})
        seen = self.loop(server, iter(["first", "second"]))  # the source ends with a message still waiting
        accepted = [(d["payload"]["delivery_id"], d["payload"]["how"]) for d in seen["drafts"]
                    if d["type"] == "input.accepted"]
        self.assertEqual(accepted, [("dlv-1", "new_turn"), ("dlv-2", "new_turn")])

    def test_a_waiting_message_does_not_hide_a_later_interrupt_or_quit(self):
        # Round 2 of #402 (GPT-6 Astra 3, GPT-6.1 Sol 2, DeepSeek R2-6): the turn never ends by itself.
        for later, expect in (("/interrupt", "turn/interrupt"), ("/quit", None)):
            with self.subTest(later=later):
                server = AppServer(script={2: [done()]})  # turn 1 never ends by itself; the queued one does
                consumed = []

                def commands(later=later):
                    for command in ("hello", "queued", later):
                        consumed.append(command)
                        yield command
                self.loop(server, LateTyping(server, commands()))
                self.assertEqual(consumed, ["hello", "queued", later])
                if expect:
                    self.assertIn(expect, [m.get("method") for m in server.sent])
                else:
                    self.assertTrue(server.stdin_closed)  # /quit was read and ended the worker's input

    def test_resume_waits_for_the_stopped_workers_turn_to_end(self):
        # The stopped worker ends its turn only after its input has ended; /resume typed meanwhile waits, so
        # the old worker's last drafts are kept before the new worker starts.
        class EndsOnInputEnd(AppServer):
            def close_stdin(self):
                super().close_stdin()
                self.emit({"method": "turn/completed", "params": {"threadId": "thr-1",
                                                                  "turn": {"id": "trn-1", "items": [],
                                                                           "status": "interrupted"}}})
        first, second = EndsOnInputEnd(), AppServer()
        seen = self.loop([first, second], iter(["hello", "/stop", "/resume"]),  # (a /quit would act at once)
                         launcher=Launcher(first, second))
        self.assertEqual(seen["workers"], 2)
        results = [d["payload"]["code"] for d in seen["drafts"] if d["type"] == "result"]
        self.assertEqual(results, ["interrupted"])
        started = self.said.index("worker 2 on thread thr-1")
        self.assertTrue(any(s.startswith("result:") for s in self.said[:started]))

    def test_resume_is_refused_while_the_worker_runs(self):
        first, second = AppServer(), AppServer()
        launch = Launcher(first, second)
        seen = self.loop([first, second], iter(["/resume", "/quit"]), launcher=launch)
        self.assertEqual(seen["workers"], 1)
        self.assertTrue(first.stdin_closed)
        self.assertTrue(any(s.startswith("refused: /stop the worker first") for s in self.said))

    def test_a_malformed_line_ends_the_run_with_a_message(self):
        server = AppServer(script={1: [lambda tid: Raw("not json at all")]})
        self.loop(server, iter(["hello", "/quit"]))
        self.assertTrue(any(s.startswith("the worker's output failed: protocol_error") for s in self.said))

    def test_a_dead_input_is_a_refusal_not_a_crash(self):
        class Dying(AppServer):
            def send(self, line):
                if json.loads(line).get("method") == "turn/start":
                    raise BrokenPipeError(32, "Broken pipe")
                super().send(line)
        self.loop(Dying(), iter(["hello", "/quit"]))
        self.assertTrue(any(s.startswith("refused: engine_error: engine io: BrokenPipeError") for s in self.said))

    def test_an_exception_still_ends_every_worker_and_closes_the_drafts(self):
        module = self.tool
        fenced = []

        class DummyProcess:
            def __init__(self, *args, **kwargs):
                self.proc = type("Child", (), {"pid": 12345})()

            def fence(self, reason):
                fenced.append(reason)
                if len(fenced) == 1:
                    raise OSError("the first worker could not be ended")

        def interrupted(driver, *args, **kwargs):
            driver._launcher(["synthetic-child"], env={})
            driver._launcher(["synthetic-child"], env={})
            kwargs["on_draft"]({"type": "result", "payload": {"code": None, "detail": ""}})
            raise KeyboardInterrupt()
        scratch = Path(tempfile.mkdtemp(prefix="attended-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        args = argparse.Namespace(out=str(scratch), workdir=str(scratch), codex="synthetic-codex", model=None)
        with mock.patch.object(module, "_run", return_value="synthetic 0"), \
                mock.patch.object(module, "LocalProcess", DummyProcess), \
                mock.patch.object(module, "session_loop", side_effect=interrupted), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                module.run_mode(args)
        self.assertEqual(fenced, ["attended_run_over", "attended_run_over"])
        self.assertEqual((scratch / "drafts.jsonl").read_text(encoding="utf-8"),
                         '{"payload": {"code": null, "detail": ""}, "type": "result"}\n')


class LocalProcessTest(unittest.TestCase):
    def test_fence_on_a_dead_child_still_closes_the_captures(self):
        tool_module = tool()
        out = Path(tempfile.mkdtemp(prefix="attended-"))
        self.addCleanup(shutil.rmtree, out, True)
        proc = tool_module.LocalProcess([sys.executable, "-I", "-c", "pass"], env=None, cwd=str(out), out=out,
                                        tag="worker-1")
        self.assertIsNone(proc.read_line(60))  # the child has ended
        proc.proc.wait(60)
        proc.proc.stdin.close()

        class DeadPipe:  # a pipe whose close flushes into a dead reader (how it fails depends on buffering)
            closed = False

            def close(self):
                raise BrokenPipeError(32, "Broken pipe")
        proc.proc.stdin = DeadPipe()
        proc.fence("test_over")  # no BrokenPipeError
        self.assertTrue(proc._stdin.closed and proc._stderr.closed)

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
