"""The answers to the second reads of PR 2 (#417 at e7324262): astra C1-C5, sol 1-2, DeepSeek N1-N5.

Each test here failed on e7324262 and passes with the answer, except the coverage the reads asked for (astra C4,
sol 2, DeepSeek N1 and N5), marked "coverage:": those pass on e7324262 and fail on the reviewer's one-rule
mutant, promptly; and the guards ("guard"), which keep what the answer must not lose. Several are the reviewers' own checks, kept as they wrote them or narrowed (said where). No
test here starts a process; every key and token is made up.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from recursion_core.loop import __main__ as cli
from recursion_core.loop import instructions as ins
from recursion_core.loop import permissions as perm
from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.support import POSIX, Lines, private_dir, workspace
from recursion_core.loop.tools import edit, files, read, reaper, shell, write
from recursion_core.loop.tools import registry as r
from recursion_core.loop.tools.workspace import ToolError

LINUX = sys.platform.startswith("linux")
INERT = b.ToolSpec("inert", "records only", {"type": "object", "properties": {}, "additionalProperties": False})


def approve_all(call: b.ToolCall) -> perm.Decision:
    return perm.Decision(perm.APPROVED, dict(call.input))


class PollingFailure(unittest.TestCase):
    """astra C1 (its polling_failure.py): a pipe the selector cannot wait on must not make the final close wait for
    a writer the loop could not stop. The only writer closes after 0.6 s whatever happens; no process starts."""

    def run_case(self, unavailable: bool) -> float:
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")
        release = threading.Timer(0.6, os.close, args=(write_fd,))
        process = mock.Mock(pid=987654, stdout=stream, returncode=0)
        process.wait.return_value = 0
        process.poll.return_value = 0
        failing = mock.patch.object(shell.selectors, "DefaultSelector",
                                    side_effect=OSError(errno.EMFILE, "synthetic selector exhaustion"))
        release.start()
        started = time.monotonic()
        try:
            with mock.patch.object(shell.subprocess, "Popen", return_value=process), \
                    mock.patch.object(shell, "_kill_group"), \
                    mock.patch.object(shell, "DRAIN_GRACE_S", 0.02), mock.patch.object(shell, "POLL_S", 0.01):
                if unavailable:
                    with failing:
                        shell.run_command("unused inert command", str(workspace(self).root), b.CancelToken(), 0.05)
                else:
                    shell.run_command("unused inert command", str(workspace(self).root), b.CancelToken(), 0.05)
            elapsed = time.monotonic() - started
        finally:
            release.join(2)
            stream.close()
        return elapsed

    def test_guard_working_selector_does_not_wait_for_the_writer(self) -> None:
        self.assertLess(self.run_case(False), 0.3)

    def test_selector_failure_does_not_make_the_final_close_wait_for_the_writer(self) -> None:
        elapsed = self.run_case(True)
        self.assertLess(elapsed, 0.3, f"the final close waited {elapsed:.3f} s for the unrelated pipe writer")

    def test_a_pipe_that_can_be_neither_waited_on_nor_polled_is_refused(self) -> None:
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")
        try:
            with mock.patch.object(shell.selectors, "DefaultSelector", side_effect=OSError(errno.EMFILE, "synthetic")), \
                    mock.patch.object(shell.os, "set_blocking", side_effect=OSError(errno.EINVAL, "synthetic")):
                with self.assertRaises(OSError):
                    shell._Capture(stream)
        finally:
            os.close(write_fd)
            stream.close()


class KeeperStart(unittest.TestCase):
    """astra C2 (its StartupFailureChecks): a keeper that is not the subreaper starts no command. fork is
    intercepted and _exit raises, so nothing is started or ended."""

    def failed_setup(self, loader) -> list:
        class ForkIntercept(BaseException):
            pass

        said = []
        # create=True: os.fork does not exist on Windows (nor in the simulated-Windows run); the check holds there too
        with mock.patch.object(reaper.ctypes, "CDLL", loader), \
                mock.patch.object(reaper.signal, "signal"), \
                mock.patch.object(reaper.os, "fork", side_effect=ForkIntercept, create=True) as fork, \
                mock.patch.object(reaper.os, "write", side_effect=lambda fd, data: said.append((fd, data))), \
                mock.patch.object(reaper.os, "_exit", side_effect=SystemExit) as end:
            try:
                reaper.main()
            except (ForkIntercept, SystemExit):
                pass
            self.assertFalse(fork.called, "the command was started without the subreaper")
            end.assert_called_once_with(reaper.REFUSED)
        return said

    def test_an_error_return_refuses_the_command(self) -> None:
        said = self.failed_setup(mock.Mock(return_value=SimpleNamespace(prctl=mock.Mock(return_value=-1))))
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0][0], 2)
        self.assertIn(b"subreaper", said[0][1])

    def test_a_loader_failure_refuses_the_command(self) -> None:
        self.failed_setup(mock.Mock(side_effect=OSError(errno.EPERM, "synthetic prctl unavailable")))

    def test_a_setting_that_does_not_read_back_refuses_the_command(self) -> None:
        # The set call reports success, the read-back says it is not set.
        self.failed_setup(mock.Mock(return_value=SimpleNamespace(prctl=mock.Mock(return_value=0))))


@unittest.skipUnless(LINUX, "the start environment is cleared on Linux only")
class StartEnvironment(unittest.TestCase):
    """astra C3, DeepSeek N4: a start environment that cannot be read is not reported as cleared. The read is
    mocked; the real process environment is never read here."""

    def test_permission_error_is_not_success(self) -> None:
        with mock.patch("builtins.open", side_effect=PermissionError(errno.EACCES, "synthetic denial")):
            self.assertFalse(cli.clear_start_environment())

    def test_io_error_is_not_success(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError(errno.EIO, "synthetic I/O failure")):
            self.assertFalse(cli.clear_start_environment())

    def test_guard_no_proc_at_all_is_nothing_to_clear(self) -> None:
        with mock.patch.object(cli.os.path, "exists", return_value=False), \
                mock.patch("builtins.open", side_effect=AssertionError("nothing is read without /proc")):
            self.assertTrue(cli.clear_start_environment())


class GateCancel(unittest.TestCase):
    """astra C5, DeepSeek N3; coverage for DeepSeek N1 (M05) and M02, sol 2 (approve-then-cancel)."""

    def test_cancel_during_callback_registration_draws_no_request(self) -> None:
        # astra's LaterStandard check, as written.
        lines, token = Lines(), b.CancelToken()
        gate = perm.PermissionGate(lines.writer, deadline_s=5)
        register = token.on_cancel

        def cancel_then_register(callback):
            token.cancel()
            return register(callback)

        with mock.patch.object(token, "on_cancel", side_effect=cancel_then_register):
            decision = gate.ask("turn-1", b.ToolCall("c1", "inert", {}), "/ws", token)
        self.assertEqual(decision.outcome, perm.CANCELLED)
        self.assertEqual(gate.waiting(), ())
        self.assertEqual(lines.of("permission_request"), [])

    def test_coverage_an_already_cancelled_turn_draws_no_request(self) -> None:
        lines, token = Lines(), b.CancelToken()
        token.cancel()
        gate = perm.PermissionGate(lines.writer, deadline_s=5)
        self.assertEqual(gate.ask("turn-1", b.ToolCall("c1", "inert", {}), "/ws", token).outcome, perm.CANCELLED)
        self.assertEqual(lines.of("permission_request"), [])
        self.assertEqual(gate.waiting(), ())

    def test_coverage_an_answer_then_a_cancel_is_cancelled(self) -> None:
        # DeepSeek's m05: the approval is taken first, the cancel comes before ask returns. The gate itself
        # answers cancelled (not only the registry's later check).
        lines, token = Lines(), b.CancelToken()
        gate = perm.PermissionGate(lines.writer, deadline_s=5)
        taken = []

        def respond(doc):
            if doc["type"] == "permission_request":
                taken.append(gate.answer(doc["request_id"], "apr-1", "approve", None))
                token.cancel()

        lines.buffer.on_line = respond
        decision = gate.ask("turn-1", b.ToolCall("c1", "inert", {}), "/ws", token)
        self.assertEqual(taken, [None], "the approval was taken")
        self.assertEqual(decision.outcome, perm.CANCELLED)
        self.assertFalse(decision.approved)
        self.assertEqual(gate.waiting(), ())


class Facts(unittest.TestCase):
    """DeepSeek N2: a precheck's facts are checked before anything is asked."""

    def dispatch_with(self, facts):
        lines, ws, token = Lines(), workspace(self), b.CancelToken()
        gate = perm.PermissionGate(lines.writer, deadline_s=5)
        ran = []
        tool = r.Tool(INERT, lambda args, ctx: ran.append(1) or r.ToolOutput("ran"), True, lambda args, ctx: facts)
        ctx = r.ToolContext(ws, token, str)
        ctx.approve = lambda call: gate.ask("turn-1", call, "/ws", token, facts=ctx.request_facts)
        out = r.ToolRegistry((tool,)).dispatch(b.ToolCall("c1", "inert", {}), ctx)
        return out, ran, gate, lines

    def test_a_fact_the_request_cannot_show_is_refused_before_asking(self) -> None:
        # astra's test_invalid_extension_facts_fail_closed_and_release_pending_state, plus the reason in the text.
        for facts in ({"input": {}}, {"tool": "inert"}, {"profile": "full_access"}, {"custom": object()},
                      {"resolved_path": object()}, {"resolved_path": float("nan")}):
            with self.subTest(facts=tuple(facts)):
                out, ran, gate, lines = self.dispatch_with(facts)
                self.assertEqual(out.status, r.ERROR)
                self.assertNotIn("TypeError", out.model_text)
                self.assertIn("could not be asked for", out.model_text)
                self.assertEqual(ran, [])
                self.assertEqual(gate.waiting(), ())
                self.assertEqual(lines.of("permission_request"), [])

    def test_the_gate_refuses_such_facts_itself(self) -> None:
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, deadline_s=5)
        for facts in ({"input": {}}, {"tool": "x"}, {"resolved_path": object()}):
            with self.subTest(facts=tuple(facts)):
                with self.assertRaises(ValueError):
                    gate.ask("turn-1", b.ToolCall("c1", "inert", {}), "/ws", b.CancelToken(), facts=facts)
        self.assertEqual(lines.of("permission_request"), [])
        self.assertEqual(gate.waiting(), ())

    def test_guard_a_documented_fact_is_still_shown(self) -> None:
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, profile=perm.FULL_ACCESS)
        self.assertTrue(gate.ask("turn-1", b.ToolCall("c1", "inert", {}), "/ws", b.CancelToken(),
                                 facts={"resolved_path": "a.txt"}).approved)
        self.assertEqual(lines.of("permission_request")[0]["resolved_path"], "a.txt")


class ApprovedTarget(unittest.TestCase):
    """sol 1: what was approved is what changes. A target whose resolution changed after the request is not
    changed."""

    def test_resolved_request_facts_are_checked_again_before_the_run(self) -> None:
        # sol's fresh_contracts test, as written (an inert tool, no effect).
        ws = workspace(self)
        current = ["first.txt"]
        ran = []

        def check(args, ctx):
            return {"resolved_path": current[0]}

        def approve(call):
            self.assertEqual(ctx.request_facts, {"resolved_path": "first.txt"})
            current[0] = "second.txt"
            return perm.Decision(perm.APPROVED, call.input)

        tool = r.Tool(INERT, lambda args, ctx: ran.append(current[0]) or r.ToolOutput("ok"), True, check)
        ctx = r.ToolContext(ws, b.CancelToken(), lambda s: s, approve=approve)
        result = r.ToolRegistry((tool,)).dispatch(b.ToolCall("toolu_1", "inert", {}), ctx)
        self.assertIn(result.status, (r.ERROR, r.DENIED))
        self.assertEqual(ran, [], "the resolved target differs from the fact shown for approval")
        self.assertIs(ctx.approved_facts, r.UNBOUND)

    def test_guard_unchanged_facts_run_and_the_binding_ends_with_the_run(self) -> None:
        ws = workspace(self)
        seen = []
        tool = r.Tool(INERT, lambda args, ctx: seen.append(ctx.approved_facts) or r.ToolOutput("ok"), True,
                      lambda args, ctx: {"resolved_path": "same.txt"})
        ctx = r.ToolContext(ws, b.CancelToken(), lambda s: s, approve=approve_all)
        self.assertEqual(r.ToolRegistry((tool,)).dispatch(b.ToolCall("toolu_1", "inert", {}), ctx).status, r.OK)
        self.assertEqual(seen, [{"resolved_path": "same.txt"}])
        self.assertIs(ctx.approved_facts, r.UNBOUND)

    @unittest.skipUnless(POSIX, "links")
    def test_a_link_repointed_during_the_approval_changes_nothing(self) -> None:
        for tool, args in ((write.TOOL, {"path": "alias.txt", "content": "new"}),
                           (edit.TOOL, {"path": "alias.txt", "old_string": "old", "new_string": "new"})):
            with self.subTest(tool=tool.spec.name):
                ws = workspace(self, {"first.txt": "old", "second.txt": "old"})
                ctx = r.ToolContext(ws, b.CancelToken(), lambda s: s)
                reg = r.ToolRegistry((read.TOOL,))
                for n, path in enumerate(("first.txt", "second.txt")):
                    reg.dispatch(b.ToolCall(f"toolu_{n}", "read", {"path": path}), ctx)
                alias = ws.root / "alias.txt"
                alias.symlink_to("first.txt")
                shown = []

                def approve(call):
                    shown.append(dict(ctx.request_facts or {}))
                    alias.unlink()
                    alias.symlink_to("second.txt")
                    return approve_all(call)

                ctx.approve = approve
                out = r.ToolRegistry((tool,)).dispatch(b.ToolCall("toolu_w", tool.spec.name, args), ctx)
                self.assertEqual(shown, [{"resolved_path": "first.txt"}])
                self.assertEqual(out.status, r.ERROR)
                self.assertEqual((ws.root / "first.txt").read_text(), "old")
                self.assertEqual((ws.root / "second.txt").read_text(), "old")

    @unittest.skipUnless(POSIX, "links")
    def test_the_run_itself_refuses_a_target_other_than_the_approved_one(self) -> None:
        # The registry checks again before the run; the run checks once more where it resolves the path.
        for run, args in ((write.run, {"path": "alias.txt", "content": "new"}),
                          (edit.run, {"path": "alias.txt", "old_string": "old", "new_string": "new"})):
            with self.subTest(run=run.__module__):
                ws = workspace(self, {"first.txt": "old", "second.txt": "old"})
                ctx = r.ToolContext(ws, b.CancelToken(), lambda s: s)
                reg = r.ToolRegistry((read.TOOL,))
                for n, path in enumerate(("first.txt", "second.txt")):
                    reg.dispatch(b.ToolCall(f"toolu_{n}", "read", {"path": path}), ctx)
                (ws.root / "alias.txt").symlink_to("second.txt")
                ctx.approved_facts = {"resolved_path": "first.txt"}
                with self.assertRaisesRegex(ToolError, "no longer what the permission request showed"):
                    run(args, ctx)
                ctx.approved_facts = None  # approved as the file itself: a path that now leads elsewhere is refused
                with self.assertRaises(ToolError):
                    run(args, ctx)
                self.assertEqual((ws.root / "second.txt").read_text(), "old")


class Coverage(unittest.TestCase):
    """coverage: astra C4, sol 2, DeepSeek N5 (M12)."""

    def test_coverage_the_instruction_reader_passes_a_real_read_size(self) -> None:
        # astra's test_instruction_reader_passes_a_real_read_size (its M01; sol's mutant-instruction_read).
        root = private_dir(self)
        (root / ".git").mkdir()
        (root / "AGENTS.md").write_bytes(b"x" * 100_000)
        (root / "CLAUDE.md").write_bytes(b"y" * 30)
        calls = []
        real_open = open
        case = self

        class CheckedReader:
            def __init__(self, name, *args, **kwargs):
                self.file = real_open(name, *args, **kwargs)

            def __enter__(self):
                self.file.__enter__()
                return self

            def __exit__(self, *args):
                return self.file.__exit__(*args)

            def fileno(self):
                return self.file.fileno()

            def read(self, count=-1):
                calls.append(count)
                case.assertGreaterEqual(count, 0, "an instruction file's read has no bound")
                case.assertLessEqual(count, ins.MAX_BYTES + 1)
                return self.file.read(count)

        with mock.patch.object(ins, "open", CheckedReader, create=True):
            text, found = ins.discover(root)
        self.assertEqual(calls, [ins.MAX_BYTES + 1])
        self.assertEqual(found[0].bytes, 100_000)
        self.assertIsNotNone(found[1].skipped)
        self.assertLessEqual(len(text.encode()), ins.MAX_BYTES + 200)

    def test_coverage_a_precheck_gets_its_own_copy(self) -> None:
        # sol's test_precheck_does_not_mutate_execution_snapshot and astra's nested form, together.
        ws = workspace(self)
        spec = b.ToolSpec("inert", "records only", {"type": "object", "properties": {
            "values": {"type": "array", "items": {"type": "string"}}}, "required": ["values"]})
        seen = []

        def precheck(args, ctx):
            args["values"][0] = "precheck changed"
            args["values"].append("precheck added")

        def approve(call):
            seen.append(perm.snapshot(call.input))
            return perm.Decision(perm.APPROVED, call.input)

        tool = r.Tool(spec, lambda args, ctx: seen.append(args) or r.ToolOutput("ok"), True, precheck)
        out = r.ToolRegistry((tool,)).dispatch(b.ToolCall("c1", "inert", {"values": ["original"]}),
                                               r.ToolContext(ws, b.CancelToken(), str, approve=approve))
        self.assertEqual(out.status, r.OK)
        self.assertEqual(seen, [{"values": ["original"]}] * 2)

    def test_coverage_a_folder_reached_another_way_is_still_guarded(self) -> None:
        # astra's test_identity_guard_refuses_mocked_directory_alias (DeepSeek's M12).
        ws = workspace(self)
        (ws.root / ".git").mkdir()
        (ws.root / "alias").mkdir()
        real = files._identity
        git_identity = real(ws.root / ".git")

        def identity(path):
            return git_identity if path == ws.root / "alias" else real(path)

        with mock.patch.object(files, "_identity", side_effect=identity):
            with self.assertRaisesRegex(ToolError, "inside a .git folder"):
                files.resolve_writable(ws, "alias/config")


if __name__ == "__main__":
    unittest.main()
