"""The answers to the third reads of PR 2 (#417 at 95856452): sol 1-4, DeepSeek R3-2 to R3-5, and the integration
triage's entries for this branch (a multiline anchor in a CRLF file, the fixtures' line ends, a non-finite approval
timeout), with the review thread on the shell's inherited SIGPIPE and the one on the edit snippet's line numbers.

Each test here failed at 95856452 and passes with the answer, except the guards ("guard") that keep what the answer
must not lose, and the fixture-bytes test, which fails only on native Windows (it is the triage's own witness).
Several are the reviewers' own checks, kept as they wrote them or narrowed. No test here reaches a network, and
every key and token is made up.
"""

from __future__ import annotations

import errno
import io
import os
import signal
import sys
import unittest
from unittest import mock

from recursion_core.loop import __main__ as cli
from recursion_core.loop import permissions as perm
from recursion_core.loop import protocol
from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.support import POSIX, Lines, private_dir, workspace
from recursion_core.loop.tools import edit, read, shell, write
from recursion_core.loop.tools import registry as r

LINUX = sys.platform.startswith("linux")
SPEC = b.ToolSpec("inert", "records only", {"type": "object", "properties": {}})
CALL = b.ToolCall("c1", "inert", {})


def approve_all(call: b.ToolCall) -> perm.Decision:
    return perm.Decision(perm.APPROVED, dict(call.input))


class FactsTheLineCanCarry(unittest.TestCase):
    """sol 1 (= DeepSeek R3-1), DeepSeek R3-2: a fact is checked the way the writer writes it, before anything is
    asked, and ``resolved_path`` is a string."""

    def test_invalid_fact_utf8_is_refused_before_writer_is_called(self) -> None:
        for profile in (perm.ASK, perm.FULL_ACCESS):
            for value in ("\udcff", ["ok", "\ud800"], {"key": "\udfff"}):
                with self.subTest(profile=profile, value=ascii(value)):
                    writer = mock.Mock()
                    gate = perm.PermissionGate(writer, profile=profile, deadline_s=0.05)
                    with self.assertRaises(ValueError):
                        gate.ask("turn-1", CALL, "/ws", b.CancelToken(), facts={"resolved_path": value})
                    writer.write.assert_not_called()
                    self.assertEqual(gate.waiting(), ())

    @unittest.skipUnless(POSIX and LINUX, "a file name that is not UTF-8, and a link to it")
    def test_real_invalid_byte_target_gets_typed_refusal_before_asking(self) -> None:
        ws = workspace(self)
        bad = ws.root / os.fsdecode(b"bad\xffname.txt")
        bad.write_bytes(b"old")
        (ws.root / "alias").symlink_to(bad.name)
        ctx = r.ToolContext(ws, b.CancelToken(), str)
        r.ToolRegistry((read.TOOL,)).dispatch(b.ToolCall("r1", "read", {"path": "alias"}), ctx)
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", write_through=True)
        self.addCleanup(stream.close)
        gate = perm.PermissionGate(protocol.Writer(stream, protocol.Redactor()), deadline_s=0.01)
        ctx.approve = lambda call: gate.ask("turn-1", call, str(ws.root), ctx.cancel, facts=ctx.request_facts)
        for tool, args in ((write.TOOL, {"path": "alias", "content": "new"}),
                           (edit.TOOL, {"path": "alias", "old_string": "old", "new_string": "new"})):
            with self.subTest(tool=tool.spec.name):
                out = r.ToolRegistry((tool,)).dispatch(b.ToolCall("c1", tool.spec.name, args), ctx)
                self.assertEqual(out.status, r.ERROR)
                self.assertIn("could not be asked for", out.model_text)
                self.assertEqual(gate.waiting(), ())
                self.assertEqual(bad.read_bytes(), b"old")
                self.assertEqual(raw.getvalue(), b"")

    def test_resolved_path_must_be_a_string(self) -> None:
        for value in (1, True, None, ["a", 1], {"a": "b"}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    perm.check_facts({"resolved_path": value})

    def test_guard_a_string_path_still_passes(self) -> None:
        self.assertEqual(perm.check_facts({"resolved_path": "dir/ü.txt"}), {"resolved_path": "dir/ü.txt"})


class CancelAfterTheCheck(unittest.TestCase):
    """sol 3 and 4, DeepSeek R3-3 and R3-4: an interrupt is final wherever in the call it comes."""

    def test_full_access_cancel_inside_writer_is_terminal(self) -> None:
        lines, token = Lines(), b.CancelToken()
        gate = perm.PermissionGate(lines.writer, profile=perm.FULL_ACCESS)
        lines.buffer.on_line = lambda doc: token.cancel()
        result = gate.ask("turn-1", CALL, "/ws", token)
        self.assertEqual(result.outcome, perm.CANCELLED)
        self.assertFalse(result.approved)

    def test_guard_full_access_without_a_cancel_is_approved(self) -> None:
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, profile=perm.FULL_ACCESS)
        self.assertTrue(gate.ask("turn-1", CALL, "/ws", b.CancelToken()).approved)

    def test_second_precheck_cancellation_prevents_inert_run(self) -> None:
        lines, token, ws = Lines(), b.CancelToken(), workspace(self)
        gate = perm.PermissionGate(lines.writer, deadline_s=0.1)
        checks, ran = [], []

        def precheck(args, ctx):
            checks.append(1)
            if len(checks) == 2:
                token.cancel()
            return {"resolved_path": "same"}

        lines.buffer.on_line = lambda doc: gate.answer(doc["request_id"], "apr-synthetic", "approve", None)
        ctx = r.ToolContext(ws, token, str)
        ctx.approve = lambda call: gate.ask("turn-1", call, "/ws", token, facts=ctx.request_facts)
        tool = r.Tool(SPEC, lambda args, ctx: ran.append("ran after cancellation") or r.ToolOutput("ran"), True,
                      precheck)
        out = r.ToolRegistry((tool,)).dispatch(CALL, ctx)
        self.assertEqual(checks, [1, 1])
        self.assertEqual(ran, [], "the second precheck returned with the turn already cancelled")
        self.assertEqual(out.status, r.CANCELLED)
        self.assertIs(ctx.approved_facts, r.UNBOUND)

    def test_a_registration_that_fails_leaves_nothing_pending(self) -> None:
        lines, token = Lines(), b.CancelToken()
        gate = perm.PermissionGate(lines.writer, deadline_s=0.01)
        with mock.patch.object(token, "on_cancel", side_effect=RuntimeError("synthetic broken token")):
            with self.assertRaises(RuntimeError):
                gate.ask("turn-1", CALL, "/ws", token)
        self.assertEqual(gate.waiting(), ())
        self.assertEqual(lines.of("permission_request"), [])


class NestedDispatch(unittest.TestCase):
    """DeepSeek R3-5: a tool that dispatches another keeps its own approved facts when that returns."""

    def test_the_outer_binding_survives_an_inner_dispatch(self) -> None:
        ws = workspace(self)
        seen = []
        inner = r.Tool(SPEC, lambda a, c: r.ToolOutput("inner"), True, lambda a, c: {"resolved_path": "inner-file"})
        nested = r.ToolRegistry((inner,))

        def run(args, ctx):
            seen.append(ctx.approved_facts)
            nested.dispatch(CALL, ctx)
            seen.append(ctx.approved_facts)
            return r.ToolOutput("outer")

        outer = r.Tool(SPEC, run, True, lambda a, c: {"resolved_path": "same"})
        ctx = r.ToolContext(ws, b.CancelToken(), str, approve=approve_all)
        self.assertEqual(r.ToolRegistry((outer,)).dispatch(CALL, ctx).status, r.OK)
        self.assertEqual(seen, [{"resolved_path": "same"}, {"resolved_path": "same"}])
        self.assertIs(ctx.approved_facts, r.UNBOUND)


@unittest.skipUnless(LINUX, "/proc is Linux's")
class ProcEnvironment(unittest.TestCase):
    """sol 2: an absent /proc is told apart from one that cannot be read; only the first is nothing to clear. The
    reads are mocked; the real process environment is never read here."""

    def test_unstatable_proc_environment_is_not_success(self) -> None:
        for error in (PermissionError(errno.EACCES, "synthetic"), OSError(errno.EIO, "synthetic")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(cli.os, "stat", side_effect=error), \
                    mock.patch("builtins.open", side_effect=AssertionError("no real environment read")):
                self.assertFalse(cli.clear_start_environment(), "a stat error is not evidence /proc is absent")

    def test_missing_environ_with_proc_present_is_not_success(self) -> None:
        exists = cli.os.path.exists
        with mock.patch.object(cli.os.path, "exists",
                               side_effect=lambda path: False if path == "/proc/self/environ" else exists(path)), \
                mock.patch("builtins.open", side_effect=FileNotFoundError(errno.ENOENT, "synthetic unavailable")):
            self.assertFalse(cli.clear_start_environment(), "/proc exists; its unavailable environ was not verified")


class ApprovalTimeout(unittest.TestCase):
    """The integration triage (#417, __main__.py): a timeout that is not a finite positive number is refused before
    anything is set up."""

    def test_nonfinite_approval_timeout_is_rejected_before_provider_setup(self) -> None:
        folder = private_dir(self)
        for value in ("nan", "inf", "-inf", "0", "-1", "1e300"):
            with self.subTest(value=value):
                with mock.patch.object(cli, "build", side_effect=AssertionError("reached provider setup")) as setup:
                    with self.assertRaises(SystemExit):
                        cli.main(["--provider", "fake", "--home", str(folder), "--approval-timeout=" + value])
                    setup.assert_not_called()

    def test_guard_a_small_positive_timeout_is_accepted(self) -> None:
        folder = private_dir(self)
        with mock.patch.object(cli, "build", side_effect=RuntimeError("past the check")):
            with self.assertRaisesRegex(RuntimeError, "past the check"):
                cli.main(["--provider", "fake", "--home", str(folder), "--approval-timeout=0.5"])


class EditLineEnds(unittest.TestCase):
    """The integration triage (#417, edit.py), and the review thread on ``_snippet``: ``read`` shows a CRLF file's
    lines without their CR and numbers them by LF, so an anchor copied from it has bare LFs; the edit must meet it,
    and its snippet must number lines the way ``read`` does."""

    def run_edit(self, files: dict, **args):
        ws = workspace(self, files)
        registry = r.ToolRegistry((read.TOOL, edit.TOOL))
        ctx = r.ToolContext(ws, b.CancelToken(), lambda text: text, approve=approve_all)
        path = next(iter(files))
        page = registry.dispatch(b.ToolCall("read-1", "read", {"path": path}), ctx)
        self.assertEqual(page.status, r.OK, page.model_text)
        return ws, page, registry.dispatch(b.ToolCall("edit-1", "edit", {"path": path, **args}), ctx)

    def test_multiline_anchor_from_read_edits_a_crlf_file(self) -> None:
        ws, page, out = self.run_edit({"a.py": b"def f():\r\n    return 1\r\n"}, old_string="def f():\n    return 1",
                                  new_string="def f():\n    return 2")
        self.assertIn("     1\tdef f():\n     2\t    return 1\n", page.model_text)
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertEqual((ws.root / "a.py").read_bytes(), b"def f():\r\n    return 2\r\n")

    def test_a_crlf_anchor_that_occurs_twice_is_still_not_unique(self) -> None:
        ws, _, out = self.run_edit({"a.txt": b"x\r\ny\r\nz\r\nx\r\ny\r\n"}, old_string="x\ny", new_string="q")
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("anchor_not_unique", out.model_text)
        self.assertEqual((ws.root / "a.txt").read_bytes(), b"x\r\ny\r\nz\r\nx\r\ny\r\n")

    def test_guard_an_lf_file_and_a_single_line_anchor_are_unchanged(self) -> None:
        ws, _, out = self.run_edit({"a.txt": b"one\ntwo\nthree\n"}, old_string="one\ntwo", new_string="1\n2")
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertEqual((ws.root / "a.txt").read_bytes(), b"1\n2\nthree\n")
        ws, _, out = self.run_edit({"b.txt": b"one\r\ntwo\r\n"}, old_string="two", new_string="2")
        self.assertEqual((ws.root / "b.txt").read_bytes(), b"one\r\n2\r\n")

    def test_the_snippet_numbers_lines_the_way_read_does(self) -> None:
        # A form feed ends a line for str.splitlines but not for read, nor for the count of LFs.
        ws, _, out = self.run_edit({"a.txt": b"one\x0ctwo\nthree\nfour\n"}, old_string="four", new_string="FOUR")
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertIn("     3\tFOUR\n", out.model_text)

    def test_the_snippet_of_a_crlf_file_shows_no_cr(self) -> None:
        _, _, out = self.run_edit({"a.txt": b"one\r\ntwo\r\nthree\r\n"}, old_string="two", new_string="TWO")
        self.assertIn("     2\tTWO\n", out.model_text)
        self.assertNotIn("\r", out.model_text)


class FixtureBytes(unittest.TestCase):
    """The integration triage (#417, support.py): a text fixture is written as the LF bytes the tests assert. Fails
    only on native Windows before the helper's change (a text-mode write turns LF into CRLF there)."""

    def test_text_fixtures_have_the_lf_bytes_the_tests_assert(self) -> None:
        ws = workspace(self, {"AGENTS.md": "root rules\n"})
        self.assertEqual((ws.root / "AGENTS.md").read_bytes(), b"root rules\n")


@unittest.skipUnless(POSIX and LINUX, "the shell's keeper and bash are Linux's")
class ShellSignals(unittest.TestCase):
    """The review thread on reaper.py:111: Python ignores SIGPIPE when it starts, and an ignored signal stays
    ignored through execv, so the command's shell must get the default action back."""

    def test_the_shell_gets_the_default_sigpipe_action(self) -> None:
        ws = workspace(self)
        got = shell.run_command("kill -PIPE $$; echo survived", str(ws.root), b.CancelToken(), 10)
        self.assertNotIn("survived", got.output)
        self.assertEqual(got.exit_code, -signal.SIGPIPE)

    def test_the_shell_ignores_neither_sigpipe_nor_sigxfsz(self) -> None:
        ws = workspace(self)
        got = shell.run_command("grep SigIgn /proc/$$/status", str(ws.root), b.CancelToken(), 10)
        self.assertEqual(got.exit_code, 0, got.output)
        ignored = int(got.output.split()[-1], 16)
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            self.assertFalse(ignored & (1 << (sig - 1)), f"{sig.name} is ignored in the command's shell")

    def test_guard_the_command_still_runs_and_reports_its_status(self) -> None:
        ws = workspace(self)
        got = shell.run_command("echo ran; exit 3", str(ws.root), b.CancelToken(), 10)
        self.assertEqual((got.exit_code, got.output), (3, "ran\n"))


if __name__ == "__main__":
    unittest.main()
