"""The answers to the first reads of PR 2 (#417 at 63c4b215): astra R1-R9, sol R1-R9, DeepSeek F1-F4.

Each test here failed on 63c4b215 and passes with the answer, except three kinds, marked: the coverage the reads
asked for (sol R8, astra R9), which passes on 63c4b215 and fails on the reviewer's mutant; and two guards
("guard:") that keep what the answer must not lose. Several are the reviewers' own checks, kept as they wrote
them or narrowed (said where). Every key and token is made up; no test reaches the network.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.loop import done, instructions as ins, permissions as perm
from recursion_core.loop.engine import Config, Engine, Session, UsageTotal
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import POSIX, Lines, private_dir, registry, workspace
from recursion_core.loop.tools import edit, read, shell, write
from recursion_core.loop.tools import registry as r
from recursion_core.loop.tools.workspace import SpillStore, ToolError, Workspace

LINUX = sys.platform.startswith("linux")
CORE = Path(__file__).resolve().parents[3]
BOUND_S = 30.0  # an outer bound for a call that must end by itself long before it; reaching it is the failure


def yes(call: b.ToolCall) -> perm.Decision:
    return perm.Decision(perm.APPROVED, dict(call.input))


def dispatch(ws, tools, name, args, approve=yes, cancel=None) -> r.Outcome:
    ctx = r.ToolContext(ws, cancel or b.CancelToken(), lambda s: s, approve=approve)
    return r.ToolRegistry(tools).dispatch(b.ToolCall("toolu_r1", name, args, ""), ctx)


def gone(pid: int) -> bool:
    """The process no longer runs (it does not exist, or it is a zombie waiting to be reaped)."""

    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return True
    return data[data.rfind(b")") + 2:].split()[0] == b"Z"


def wait_for(path: Path, bound: float = BOUND_S) -> None:
    pause = threading.Event()
    for _ in range(int(bound / 0.01)):
        if path.exists() and path.read_text().strip():
            return
        pause.wait(0.01)
    raise AssertionError(f"{path.name} never appeared")


def pids_in(case, *paths: Path) -> list[int]:
    pids = [int(p.read_text().split()[0]) for p in paths if p.exists() and p.read_text().strip()]

    def kill() -> None:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    case.addCleanup(kill)
    return pids


@unittest.skipUnless(POSIX and LINUX, "the shell's keeper and /proc are Linux's")
class ShellLifetime(unittest.TestCase):
    """sol R1, astra R3, DeepSeek F1."""

    def test_a_background_child_without_output_is_stopped_when_the_command_ends(self) -> None:
        # sol's case 1: the leader exits, nothing holds the output, the child was left running.
        ws = workspace(self)
        out = dispatch(ws, (shell.TOOL,), "shell", {"command": "sleep 60 </dev/null >/dev/null 2>&1 & echo $! > bg; "
                                                               "echo now"})
        self.assertEqual(out.status, r.OK, out.model_text)
        (pid,) = pids_in(self, ws.root / "bg")
        self.assertTrue(gone(pid), "a background child outlived its command")

    def test_output_written_after_the_command_ended_is_not_waited_for(self) -> None:
        # sol's case 3, narrowed: the background writer is stopped when the command ends, so its late line never
        # comes (the reviewer's test asked for timed_out; the call now ends at the command's end, before it).
        ws = workspace(self)
        ran = shell.run_command("(sleep 0.5; echo late) & echo $! > bg; echo now", str(ws.root), b.CancelToken(), 30)
        (pid,) = pids_in(self, ws.root / "bg")
        self.assertEqual(ran.exit_code, 0)
        self.assertIn("now", ran.output)
        self.assertNotIn("late", ran.output)
        self.assertTrue(gone(pid))

    def _held(self, ws, cancel, timeout_s):
        box: dict = {}

        def go() -> None:
            box["ran"] = shell.run_command("setsid sh -c 'echo $$ > held; sleep 60' & wait", str(ws.root), cancel,
                                           timeout_s)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t, box

    def test_a_detached_output_holder_ends_with_the_deadline(self) -> None:
        # DeepSeek F1, sol's case 2: a new session holds the output; the call ended only when it did.
        ws = workspace(self)
        t, box = self._held(ws, b.CancelToken(), 1)
        wait_for(ws.root / "held")
        (pid,) = pids_in(self, ws.root / "held")
        t.join(BOUND_S)
        self.assertFalse(t.is_alive(), "the call outlived its deadline while a detached process held the output")
        self.assertTrue(box["ran"].timed_out)
        self.assertTrue(gone(pid), "the detached holder survived the deadline")

    def test_a_detached_output_holder_ends_with_an_interrupt(self) -> None:
        ws = workspace(self)
        cancel = b.CancelToken()
        t, box = self._held(ws, cancel, 120)
        wait_for(ws.root / "held")
        (pid,) = pids_in(self, ws.root / "held")
        cancel.cancel()
        t.join(BOUND_S)
        self.assertFalse(t.is_alive(), "an interrupt did not end the call")
        self.assertTrue(box["ran"].cancelled)
        self.assertTrue(gone(pid), "the detached holder survived the interrupt")

    def test_a_double_forked_child_is_still_followed(self) -> None:
        # Out of the group and the session, and its parent gone: only a subreaper still has it.
        ws = workspace(self)
        ran = shell.run_command("setsid sh -c '(sleep 60 </dev/null >/dev/null 2>&1 & echo $! > orphan)' ; "
                                "while [ ! -s orphan ]; do sleep 0.01; done", str(ws.root), b.CancelToken(), 30)
        (pid,) = pids_in(self, ws.root / "orphan")
        self.assertEqual(ran.exit_code, 0, ran.output)
        self.assertTrue(gone(pid), "a double-forked child outlived its command")

    def test_a_command_cancelled_before_it_starts_is_not_started(self) -> None:
        # sol R2 at the runner: with the token cancelled, nothing is spawned at all.
        ws = workspace(self)
        cancel = b.CancelToken()
        cancel.cancel()
        with mock.patch.object(shell.subprocess, "Popen", side_effect=AssertionError("spawned")) as popen:
            ran = shell.run_command("touch ran", str(ws.root), cancel, 5)
        self.assertEqual(popen.call_count, 0)
        self.assertTrue(ran.cancelled)
        self.assertFalse((ws.root / "ran").exists())

    def test_an_exception_while_waiting_leaves_nothing_running(self) -> None:
        # astra R3: the wait raises; the command and its child are stopped on the way out. guard: on 63c4b215 the
        # processes also ended on this path (seen on both interpreters; why was not established).
        ws = workspace(self)
        real = subprocess.Popen

        class Boom(Exception):
            pass

        class Failing(real):
            raised = False

            def wait(self, timeout=None):
                if not Failing.raised and timeout is not None:
                    Failing.raised = True
                    wait_for(ws.root / "bg")
                    raise Boom()
                return super().wait(timeout)

        with mock.patch.object(shell.subprocess, "Popen", Failing), self.assertRaises(Boom):
            shell.run_command("echo $$ > leader; sleep 60 & echo $! > bg.tmp; mv bg.tmp bg; wait", str(ws.root),
                              b.CancelToken(), 60)
        pids = pids_in(self, ws.root / "leader", ws.root / "bg")
        self.assertEqual(len(pids), 2)
        self.assertEqual([p for p in pids if not gone(p)], [], "a process outlived the exception")

    def test_the_status_of_a_command_killed_by_a_signal_is_its_own(self) -> None:
        # guard: under its keeper a command's status is still its own (a signal stays a signal).
        ws = workspace(self)
        ran = shell.run_command("kill -9 $$", str(ws.root), b.CancelToken(), 30)
        self.assertEqual(ran.exit_code, -signal.SIGKILL)
        ran = shell.run_command("exit 7", str(ws.root), b.CancelToken(), 30)
        self.assertEqual(ran.exit_code, 7)


class ShellOutput(unittest.TestCase):
    def test_the_tail_keeps_exactly_four_mib(self) -> None:
        # sol R9: with 8 MiB and one byte, the tail had one byte too many.
        total = 8 * 1024 * 1024 + 1
        cap = shell._Capture(io.BytesIO(b"a" * total))
        cap.thread.join(BOUND_S)
        self.assertFalse(cap.thread.is_alive())
        self.assertEqual(cap.tail_size, shell.KEEP_TAIL)
        self.assertEqual(sum(len(c) for c in cap.tail), shell.KEEP_TAIL)
        self.assertEqual(len(cap.head) + cap.tail_size + cap.dropped, total)

    @unittest.skipUnless(POSIX, "a pipe the reader can wait on")
    def test_reading_stops_while_something_still_holds_the_pipe(self) -> None:
        # sol R1 / DeepSeek F1: nothing waits on a stream without a bound, whoever still holds it open.
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, write_end)
        stream = os.fdopen(read_end, "rb")
        self.addCleanup(stream.close)
        os.write(write_end, b"partial")
        cap = shell._Capture(stream)
        self.assertFalse(cap.wait(0.2), "the writer is still open: no end of output yet")
        cap.stop()
        self.assertFalse(cap.thread.is_alive(), "reading did not stop while the pipe was still held")
        self.assertEqual(cap.text(), "partial")

    def test_head_and_tail_bounds(self) -> None:
        # sol R8 (M10, M11): neither part may keep more than 4 MiB.
        cap = shell._Capture(io.BytesIO(b"a" * (13 * 1024 * 1024)))
        cap.thread.join(BOUND_S)
        self.assertEqual(len(cap.head), 4 * 1024 * 1024)
        self.assertLessEqual(cap.tail_size, 4 * 1024 * 1024)


@unittest.skipUnless(POSIX, "the shell tool runs bash or sh")
class ShellEnvironment(unittest.TestCase):
    """astra R2, sol R8 (M08, M09), DeepSeek F3's first half."""

    MADE_UP = {"SERVICE_TOKEN_FOR_TESTS": "made-up-token-0000", "OPENAI_API_KEY": "made-up-openai-0000",
               "ANTHROPIC_AUTH_TOKEN": "made-up-auth-0000", "ANTHROPIC_API_KEY": "made-up-anthropic-0000",
               "HTTPS_PROXY": "http://proxy.invalid:1"}

    def test_only_listed_names_reach_a_command_and_the_acceptance_check(self) -> None:
        ws = workspace(self)
        startup = ws.root / "startup.sh"
        startup.write_text("echo STARTUP-RAN\n")
        env = dict(self.MADE_UP, BASH_ENV=str(startup), ENV=str(startup), LC_ALL="C.UTF-8")
        with mock.patch.dict(os.environ, env):
            built = shell.child_env()
            ran = shell.run_command("env", str(ws.root), b.CancelToken(), 30)
            check = done.run_check(done.Acceptance("env"), str(ws.root), b.CancelToken())
        allowed = set(shell.ENV_PASSED) | set(shell.ENV_FIXED)
        self.assertEqual({k for k in built if not k.startswith("LC_")} - allowed, set())
        self.assertEqual(built["LC_ALL"], "C.UTF-8")
        for output in (ran.output, check.output):
            for value in self.MADE_UP.values():
                self.assertNotIn(value, output)
            self.assertNotIn("STARTUP-RAN", output)
            self.assertIn("PAGER=cat", output)


@unittest.skipUnless(POSIX and LINUX, "/proc is Linux's")
class StartEnvironment(unittest.TestCase):
    """DeepSeek F3: the key stayed readable in /proc/<loop pid>/environ after it was removed by name."""

    def test_the_key_is_not_in_the_process_start_environment(self) -> None:
        ws = workspace(self)
        home = private_dir(self, "loop-home-")
        script = ws.root.parent / f"{ws.root.name}-script.json"
        script.write_text(json.dumps([{"text": "ok"}]))
        self.addCleanup(script.unlink)
        key = "sk-ant-made-up-start-environment-key-0000"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(CORE), "ANTHROPIC_API_KEY": key}
        proc = subprocess.Popen([sys.executable, "-B", "-m", "recursion_core.loop", "--provider", "fake",
                                 "--fake-script", str(script), "--home", str(home), "--workspace", str(ws.root),
                                 "--no-project-instructions"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        first = json.loads(proc.stdout.readline())
        self.assertEqual(first["type"], "ready")
        with open(f"/proc/{proc.pid}/environ", "rb") as f:
            block = f.read()
        out, _ = proc.communicate(b'{"kind":"close"}\n', timeout=BOUND_S)
        self.assertNotIn(key.encode(), block)
        self.assertIn(b"ANTHROPIC_API_KEY=", block, "the name stays; its value is cleared")
        self.assertEqual(proc.returncode, 0)


class GateDeadline(unittest.TestCase):
    """astra R1: the deadline is a moment, kept where the answer is taken."""

    def test_an_answer_after_the_deadline_is_refused_before_the_waiter_wakes(self) -> None:
        lines = Lines()
        now = [100.0]
        gate = perm.PermissionGate(lines.writer, deadline_s=5, clock=lambda: now[0])
        box: dict = {}

        def on_line(doc: dict) -> None:
            if doc["type"] == "permission_request":
                now[0] = 105.0  # the moment passes; the waiting turn has not looked yet
                box["late"] = gate.answer(doc["request_id"], "apr-1", "approve", None)

        lines.buffer.on_line = on_line
        decision = gate.ask("turn-1", b.ToolCall("toolu_1", "write", {"path": "a", "content": "x"}), "/ws",
                            b.CancelToken())
        self.assertEqual(box["late"], "no_pending_request")
        self.assertEqual(decision.outcome, perm.EXPIRED)
        self.assertEqual(len(lines.of("permission_expired")), 1)

    def test_an_answer_before_the_deadline_is_taken(self) -> None:
        lines = Lines()
        now = [100.0]
        gate = perm.PermissionGate(lines.writer, deadline_s=5, clock=lambda: now[0])

        def on_line(doc: dict) -> None:
            if doc["type"] == "permission_request":
                now[0] = 104.9
                gate.answer(doc["request_id"], "apr-1", "approve", None)

        lines.buffer.on_line = on_line
        decision = gate.ask("turn-1", b.ToolCall("toolu_1", "write", {"path": "a", "content": "x"}), "/ws",
                            b.CancelToken())
        self.assertEqual(decision.outcome, perm.APPROVED)


class GateCancellation(unittest.TestCase):
    """sol R2: an interrupt during the wait is final."""

    def test_an_approval_after_the_interrupt_does_not_revive_the_call(self) -> None:
        # sol's test_cancelled_permission_cannot_be_revived_by_answer.
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, deadline_s=60)
        cancel = b.CancelToken()
        box: dict = {}

        def on_line(doc: dict) -> None:
            if doc["type"] == "permission_request":
                cancel.cancel()
                box["answer"] = gate.answer(doc["request_id"], "apr-1", "approve", None)

        lines.buffer.on_line = on_line
        decision = gate.ask("turn-1", b.ToolCall("toolu_1", "write", {"path": "a", "content": "x"}), "/ws", cancel)
        self.assertEqual(decision.outcome, perm.CANCELLED)
        self.assertEqual(box["answer"], "no_pending_request")

    def test_an_approval_that_arrives_with_the_interrupt_runs_nothing(self) -> None:
        # The registry looks at the turn again after the approval: a tool that does not check the token itself
        # (an inert custom one here) does not run.
        ws = workspace(self)
        cancel = b.CancelToken()
        ran = []
        spec = b.ToolSpec("inert", "records that it ran", {"type": "object", "properties": {},
                                                            "additionalProperties": False})
        tool = r.Tool(spec, lambda value, ctx: ran.append(1) or r.ToolOutput("ran"), True)

        def approve_and_interrupt(call: b.ToolCall) -> perm.Decision:
            cancel.cancel()
            return yes(call)

        out = dispatch(ws, (tool,), "inert", {}, approve_and_interrupt, cancel)
        self.assertEqual(out.status, r.CANCELLED)
        self.assertEqual(ran, [])

    @unittest.skipUnless(POSIX, "the shell tool runs bash or sh")
    def test_no_shell_is_spawned_after_an_interrupt_during_the_permission_wait(self) -> None:
        # sol's test_shell_is_not_spawned_after_cancel_during_permission: the spawn is made to run the child first.
        ws = workspace(self)
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, deadline_s=60)
        cancel = b.CancelToken()

        def on_line(doc: dict) -> None:
            if doc["type"] == "permission_request":
                cancel.cancel()
                gate.answer(doc["request_id"], "apr-1", "approve", None)

        lines.buffer.on_line = on_line
        real = shell.subprocess.Popen
        starts = []

        def child_first(*args, **kwargs):
            proc = real(*args, **kwargs)
            starts.append(proc.pid)
            proc.wait(timeout=BOUND_S)
            return proc

        ctx = r.ToolContext(ws, cancel, lambda s: s, approve=lambda call: gate.ask("turn-1", call, "/ws", cancel))
        with mock.patch.object(shell.subprocess, "Popen", child_first):
            out = r.ToolRegistry((shell.TOOL,)).dispatch(
                b.ToolCall("toolu_1", "shell", {"command": "echo ran > cancelled-ran"}, ""), ctx)
        self.assertEqual(out.status, r.CANCELLED)
        self.assertEqual(starts, [])
        self.assertFalse((ws.root / "cancelled-ran").exists())


class InputSnapshot(unittest.TestCase):
    """sol R5: one independent copy through validation, the request and the run."""

    def test_a_nested_input_changed_while_the_request_waits_does_not_run(self) -> None:
        # sol's test_approval_pending_nested_input_is_an_immutable_snapshot.
        ws = workspace(self)
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, deadline_s=60)
        args = {"paths": ["a"]}
        ran = []
        spec = b.ToolSpec("synthetic", "an inert tool with an array input", {
            "type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["paths"], "additionalProperties": False})
        tool = r.Tool(spec, lambda value, ctx: ran.append(list(value["paths"])) or r.ToolOutput("ok"), True)

        def on_line(doc: dict) -> None:
            if doc["type"] == "permission_request":
                self.assertEqual(doc["input"], {"paths": ["a"]})
                args["paths"][0] = "other"
                gate.answer(doc["request_id"], "apr-1", "approve", None)

        lines.buffer.on_line = on_line
        ctx = r.ToolContext(ws, b.CancelToken(), lambda s: s,
                            approve=lambda call: gate.ask("turn-1", call, "/ws", b.CancelToken()))
        out = r.ToolRegistry((tool,)).dispatch(b.ToolCall("toolu_1", "synthetic", args, ""), ctx)
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertEqual(ran, [["a"]])

    def test_the_gate_keeps_its_own_copy(self) -> None:
        # The gate called directly (no registry copy in front of it): a change to the caller's nested input
        # while the request waits is not what an approval without a bound input returns.
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, deadline_s=60)
        args = {"paths": ["a"]}

        def on_line(doc: dict) -> None:
            if doc["type"] == "permission_request":
                args["paths"][0] = "other"
                gate.answer(doc["request_id"], "apr-1", "approve", None)

        lines.buffer.on_line = on_line
        decision = gate.ask("turn-1", b.ToolCall("toolu_1", "synthetic", args, ""), "/ws", b.CancelToken())
        self.assertEqual(decision.outcome, perm.APPROVED)
        self.assertEqual(decision.input, {"paths": ["a"]})
        self.assertEqual(lines.of("permission_request")[0]["input"], {"paths": ["a"]})

    def test_what_is_asked_for_is_what_was_checked(self) -> None:
        # sol's test_approval_uses_validated_snapshot: a precheck that changes the caller's input.
        ws = workspace(self, {"a": "old"})
        dispatch(ws, (read.TOOL,), "read", {"path": "a"})
        args = {"path": "a", "content": "new"}

        def precheck(values, ctx):
            write.precheck(values, ctx)
            args["path"] = "other"

        tool = r.Tool(write.SPEC, write.run, True, precheck)
        ctx = r.ToolContext(ws, b.CancelToken(), lambda s: s, approve=yes)
        out = r.ToolRegistry((tool,)).dispatch(b.ToolCall("toolu_1", "write", args, ""), ctx)
        self.assertFalse((ws.root / "other").exists(), out.model_text)
        self.assertEqual((ws.root / "a").read_text(), "new")


class Files(unittest.TestCase):
    def test_a_read_file_deleted_while_the_request_waits_is_stale_and_not_recreated(self) -> None:
        # sol R3; a later write, asked for again, creates it.
        ws = workspace(self, {"a": "old"})
        dispatch(ws, (read.TOOL,), "read", {"path": "a"})

        def delete_then_approve(call: b.ToolCall) -> perm.Decision:
            (ws.root / "a").unlink()
            return yes(call)

        out = dispatch(ws, (write.TOOL,), "write", {"path": "a", "content": "new"}, delete_then_approve)
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("stale_snapshot", out.model_text)
        self.assertFalse((ws.root / "a").exists())
        again = dispatch(ws, (write.TOOL,), "write", {"path": "a", "content": "new"})
        self.assertEqual(again.status, r.OK, again.model_text)
        self.assertEqual((ws.root / "a").read_text(), "new")

    def test_saved_outputs_are_not_written_through_an_ordinary_path(self) -> None:
        # sol R4, astra R4: the engine home inside the workspace.
        root = private_dir(self, "loop-ws-")
        home = root / "engine-home"
        home.mkdir(mode=0o700)
        ws = Workspace(root, SpillStore(home))
        ref = ws.spill.put(b"saved output")
        target = ws.spill.path_for(ref)
        rel = str(target.relative_to(root))
        dispatch(ws, (read.TOOL,), "read", {"path": rel})
        tools = (write.TOOL, edit.TOOL)
        for name, args in (("write", {"path": rel, "content": "altered"}),
                           ("write", {"path": str(target), "content": "altered"}),
                           ("edit", {"path": rel, "old_string": "saved", "new_string": "altered"}),
                           ("write", {"path": "engine-home/spill/sha256/00/" + "0" * 64, "content": "planted"})):
            with self.subTest(name=name, path=args["path"]):
                out = dispatch(ws, tools, name, args)
                self.assertEqual(out.status, r.ERROR, out.model_text)
        self.assertEqual(target.read_bytes(), b"saved output")
        self.assertFalse((home / "spill" / "sha256" / "00").exists())

    def test_a_saved_output_is_not_planted_before_the_store_exists(self) -> None:
        # The spill folder is refused by its place even before anything was saved: a file planted under the name
        # of an output's digest would otherwise be taken as that output later.
        root = private_dir(self, "loop-ws-")
        home = root / "engine-home"
        home.mkdir(mode=0o700)
        ws = Workspace(root, SpillStore(home))
        out = dispatch(ws, (write.TOOL,), "write", {"path": "engine-home/spill/sha256/ab/" + "ab" * 32,
                                                   "content": "planted"})
        self.assertEqual(out.status, r.ERROR, out.model_text)
        self.assertFalse((home / "spill").exists())

    def test_git_in_any_case_is_not_written(self) -> None:
        # DeepSeek F2: on a case-insensitive drive .GIT is .git.
        ws = workspace(self)
        (ws.root / ".git").mkdir()
        for path in (".GIT/hooks/pre-commit", "sub/.Git/config"):
            with self.subTest(path=path):
                out = dispatch(ws, (write.TOOL,), "write", {"path": path, "content": "x"})
                self.assertEqual(out.status, r.ERROR, out.model_text)
        self.assertEqual(sorted(p.name for p in ws.root.iterdir()), [".git"])

    @unittest.skipUnless(POSIX, "needs symbolic links")
    def test_a_request_names_the_file_a_link_leads_to(self) -> None:
        # DeepSeek F4: the user approved link.txt and real.txt changed. The request now says so.
        ws = workspace(self, {"real.txt": "before\n"})
        os.symlink("real.txt", ws.root / "link.txt")
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, deadline_s=60)

        def on_line(doc: dict) -> None:
            if doc["type"] == "permission_request":
                gate.answer(doc["request_id"], "apr-1", "approve", None)

        lines.buffer.on_line = on_line
        provider = FakeProvider([
            {"tool_calls": [{"id": "toolu_1", "name": "read", "input": {"path": "link.txt"}}]},
            {"tool_calls": [{"id": "toolu_2", "name": "write", "input": {"path": "link.txt", "content": "after\n"}}]},
            {"tool_calls": [{"id": "toolu_3", "name": "write", "input": {"path": "plain.txt", "content": "x"}}]},
            {"text": "ok"}])
        session = Session(provider, r.ToolRegistry((read.TOOL, write.TOOL)), ws, lines.writer, lines.redactor,
                          Config(model="fake"), gate=gate)
        session.run_turn("turn-1", "go", b.CancelToken())
        first, second = lines.of("permission_request")
        self.assertEqual(first["input"]["path"], "link.txt")
        self.assertEqual(first["resolved_path"], "real.txt")
        self.assertNotIn("resolved_path", second)
        self.assertEqual((ws.root / "real.txt").read_text(), "after\n")


class EditAnchor(unittest.TestCase):
    def test_overlapping_occurrences_are_not_a_unique_anchor(self) -> None:
        # astra R8, astra's own check (planning only).
        ws = workspace(self, {"example.txt": "aaaa\n"})
        ctx = r.ToolContext(ws, b.CancelToken(), lambda text: text)
        r.ToolRegistry([read.TOOL]).dispatch(b.ToolCall("toolu_read", "read", {"path": "example.txt"}), ctx)
        with self.assertRaisesRegex(ToolError, "anchor_not_unique: old_string occurs 2 times"):
            edit.precheck({"path": "example.txt", "old_string": "aaa", "new_string": "b"}, ctx)
        out = dispatch(ws, (edit.TOOL,), "edit", {"path": "example.txt", "old_string": "aaa", "new_string": "b",
                                                 "replace_all": True})
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertEqual((ws.root / "example.txt").read_text(), "ba\n", "replace_all keeps str.replace's meaning")


class InstructionBudget(unittest.TestCase):
    """astra R7, sol R6."""

    def test_a_file_is_read_only_up_to_the_room_left_and_not_at_all_after(self) -> None:
        root = private_dir(self, "loop-repo-")
        (root / ".git").mkdir()
        (root / "AGENTS.md").write_bytes(b"x" * 100_000)
        (root / "CLAUDE.md").write_bytes(b"y" * 10)
        limits = []
        real = ins._read_at_most

        def counted(path, limit):
            limits.append(limit)
            return real(path, limit)

        original = Path.read_bytes

        def no_whole_read(path):
            if path.name in ins.NAMES:
                raise AssertionError("an instruction file was read whole")
            return original(path)

        with mock.patch.object(ins, "_read_at_most", counted), mock.patch.object(Path, "read_bytes", no_whole_read):
            text, found = ins.discover(root)
        self.assertEqual(limits, [ins.MAX_BYTES + 1, 0])
        self.assertEqual([(f.path, f.bytes, f.cut, f.skipped is not None) for f in found],
                         [("AGENTS.md", 100_000, True, False), ("CLAUDE.md", 10, False, True)])
        self.assertLessEqual(len(text.encode()), ins.MAX_BYTES + 200)


class Totals(unittest.TestCase):
    """astra R5, R6, R9; sol R7. Several are astra's own checks."""

    def session(self, steps, **config):
        lines = Lines()
        session = Session(FakeProvider(steps), registry(), workspace(self), lines.writer, lines.redactor,
                          Config(model="fake-model", **config))
        return session, lines

    def test_a_turn_interrupted_before_its_first_call_has_its_totals(self) -> None:
        session, lines = self.session([])
        cancel = b.CancelToken()
        cancel.cancel()
        session.run_turn("turn-1", "ordinary cancelled message", cancel)
        self.assertEqual([(x["type"], x.get("span")) for x in lines.all()],
                         [("usage", "turn"), ("usage", "session_total"), ("turn_ended", None)])
        turn = lines.of("usage")[0]
        self.assertEqual(turn["input_tokens"], {"value": 0, "quality": "known", "provenance": "observed"},
                         "no call made is a known 0, not the unknown of a call that measured nothing")

    def test_a_loop_error_still_writes_its_totals(self) -> None:
        def broken(request):
            raise RuntimeError("synthetic internal provider failure")

        session, lines = self.session([broken])
        engine = Engine(session, lines.writer, log=lambda text: None)
        engine.feed_line(json.dumps(dict(kind="user_message", turn_id="turn-1", delivery_id="dlv-1",
                                         text="hello")).encode())
        engine.input_ended()
        self.assertEqual(engine.serve(), 0)
        self.assertEqual([(x["type"], x.get("span")) for x in lines.all()],
                         [("input_accepted", None), ("usage", "call"), ("usage", "turn"),
                          ("usage", "session_total"), ("turn_ended", None), ("closed", None)])
        self.assertEqual(lines.of("turn_ended")[0]["outcome"], "failed")
        self.assertEqual(lines.of("usage")[1]["input_tokens"]["quality"], "unknown")

    def test_provenance_is_kept_and_a_mixture_takes_the_weakest(self) -> None:
        total = UsageTotal()
        total.add(b.Usage(cost_usd=b.Measurement(0.25, b.KNOWN, b.MODELLED),
                          input_tokens=b.Measurement(4, b.KNOWN, b.INFERRED)), "fake-model")
        self.assertEqual(total.to_wire()["cost_usd"], dict(value=0.25, quality="known", provenance="modelled"))
        self.assertEqual(total.to_wire()["input_tokens"]["provenance"], "inferred")
        total.add(b.Usage(cost_usd=b.Measurement(0.5, b.KNOWN, b.OBSERVED),
                          input_tokens=b.observed(1)), "fake-model")
        self.assertEqual(total.to_wire()["cost_usd"], dict(value=0.75, quality="known", provenance="modelled"))
        self.assertEqual(total.to_wire()["input_tokens"]["provenance"], "inferred")
        observed = UsageTotal()
        observed.add(b.Usage(input_tokens=b.observed(3)), "m")
        self.assertEqual(observed.to_wire()["input_tokens"]["provenance"], "observed")

    def test_the_session_total_runs_across_turns(self) -> None:
        # astra M02.
        session, lines = self.session([dict(text="one", usage={"input_tokens": 2}),
                                       dict(text="two", usage={"input_tokens": 3})])
        session.run_turn("turn-1", "first", b.CancelToken())
        session.run_turn("turn-2", "second", b.CancelToken())
        self.assertEqual([u["input_tokens"]["value"] for u in lines.of("usage") if u["span"] == "session_total"],
                         [2, 5])

    def test_a_total_names_its_one_model(self) -> None:
        # astra M03.
        total = UsageTotal()
        total.add(b.Usage(input_tokens=b.observed(7)), "fake-model")
        self.assertEqual(total.model, "fake-model")
        total.add(b.Usage(input_tokens=b.observed(2)), "another-fake-model")
        self.assertIsNone(total.model)

    def test_a_lower_bound_stays_a_lower_bound(self) -> None:
        # astra M04.
        total = UsageTotal()
        total.add(b.Usage(input_tokens=b.at_least(7)), "fake-model")
        self.assertEqual(total.to_wire()["input_tokens"], b.at_least(7).to_wire())


class Acceptance(unittest.TestCase):
    """astra M08; sol R8 (M12, M13)."""

    def test_the_feedback_keeps_an_8_kib_tail(self) -> None:
        shown = done.tail("x" * 12_000)
        self.assertLessEqual(len(shown.encode()), 8_192 + 100)
        self.assertTrue(shown.endswith("x" * 8_192))
        self.assertFalse(shown.endswith("x" * 8_193))

    @unittest.skipUnless(POSIX, "the acceptance check runs bash or sh")
    def test_the_round_limit_survives_tool_calls_between_checks(self) -> None:
        ws = workspace(self, {"a": "old"})
        script = []
        for n in range(4):
            script += [{"text": "Done"}, {"tool_calls": [{"id": f"toolu_{n}", "name": "read", "input": {"path": "a"}}]}]
        provider = FakeProvider(script)
        lines = Lines()
        session = Session(provider, r.ToolRegistry((read.TOOL,)), ws, lines.writer, lines.redactor,
                          Config(model="fake", acceptance=done.Acceptance("exit 1"), max_steps=9))
        self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "paused")
        self.assertEqual(len(lines.of("acceptance")), 3)
        self.assertEqual(len(provider.requests), 5)

    def test_project_instructions_are_scrubbed_before_the_provider_sees_them(self) -> None:
        ws = workspace(self)
        secret = "made-up-secret-0000-for-tests"
        lines = Lines((secret,))
        provider = FakeProvider([{"text": "ok"}])
        session = Session(provider, r.ToolRegistry((read.TOOL,)), ws, lines.writer, lines.redactor,
                          Config(model="fake", instructions="AGENTS.md says " + secret))
        session.run_turn("turn-1", "go", b.CancelToken())
        self.assertNotIn(secret, provider.requests[0].system)


if __name__ == "__main__":
    unittest.main()
