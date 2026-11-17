"""Round 4 of draft #417, and the earlier notes that are a few lines on the same paths.

DeepSeek R4-1: a cancel remover that raises must not leave the permission request waiting.
DeepSeek R4-2: two dispatches at once on one context must not clear each other's approved facts.
DeepSeek R4-3: a CRLF anchor that is not at the start of the file keeps the match's offset.
DeepSeek R3-6: a pipe that cannot be waited on or polled is a typed refusal from the tool.
DeepSeek R3-7: a non-mapping passed to ask is refused; a context with no approved-facts record
cannot change a file. A precheck that returns a non-mapping stays ignored, as check_facts says.
DeepSeek N6: when the turn's own finaliser raises, the totals already summed are still written
before turn_ended.

No test here reaches a network. Every id is made up. Events order the two-thread case; nothing sleeps.
"""

from __future__ import annotations

import errno
import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from recursion_core.loop import permissions as perm
from recursion_core.loop.engine import Config, Engine, Session
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import Lines, registry, workspace
from recursion_core.loop.tools import edit, files, read, shell
from recursion_core.loop.tools import registry as r
from recursion_core.loop.tools.workspace import ToolError

SPEC = b.ToolSpec("inert", "records only", {"type": "object", "properties": {}, "additionalProperties": False})
CALL = b.ToolCall("c1", "inert", {})
MSG = b'{"kind":"user_message","turn_id":"turn-1","delivery_id":"dlv-1","text":"go"}'
MSG2 = b'{"kind":"user_message","turn_id":"turn-2","delivery_id":"dlv-2","text":"next"}'


def approve_all(call: b.ToolCall) -> perm.Decision:
    return perm.Decision(perm.APPROVED, dict(call.input or {}))


def _broken_token(token: b.CancelToken):
    """The real registration, then a remover that raises after it has removed the callback."""

    real = token.on_cancel

    def broken(fn):
        remove = real(fn)

        def raiser() -> None:
            remove()
            raise RuntimeError("synthetic: the token's remover is broken")

        return raiser

    return broken


class RemoverRaises(unittest.TestCase):
    """R4-1. The request is popped before the remover runs, and a remover that raises is not the decision."""

    def ask(self, answer: str | None, *, cancel: bool = False) -> tuple[perm.PermissionGate, perm.Decision]:
        lines, token = Lines(), b.CancelToken()
        gate = perm.PermissionGate(lines.writer, deadline_s=5)

        def on_line(doc: dict) -> None:
            if cancel:
                token.cancel()
            elif answer is not None:
                gate.answer(doc["request_id"], "apr-synthetic", answer, None)

        lines.buffer.on_line = on_line
        with mock.patch.object(token, "on_cancel", side_effect=_broken_token(token)):
            try:
                decision = gate.ask("turn-1", CALL, "/ws", token)
            except Exception as exc:  # noqa: BLE001 - the red run raises here and leaves the request waiting
                self.fail(f"ask raised {type(exc).__name__}: {exc}; pending={gate.waiting()}")
        self.assertEqual(gate.waiting(), ())
        self.assertEqual(gate.answer("perm-1", "apr-later", "approve", None), "no_pending_request")
        return gate, decision

    def test_an_approval_is_returned_and_the_request_is_gone(self) -> None:
        gate, decision = self.ask("approve")
        self.assertEqual(decision.outcome, perm.APPROVED)
        self.assertTrue(decision.approved)
        # A later ask is a new request. The writer still approves it; it is not stuck behind perm-1.
        again = gate.ask("turn-1", CALL, "/ws", b.CancelToken())
        self.assertEqual(again.outcome, perm.APPROVED)
        self.assertEqual(gate.waiting(), ())
        self.assertEqual(gate.answer("perm-2", "apr-later", "approve", None), "no_pending_request")

    def test_a_denial_is_returned_and_the_request_is_gone(self) -> None:
        _, decision = self.ask("deny")
        self.assertEqual(decision.outcome, perm.DENIED)
        self.assertFalse(decision.approved)

    def test_an_interrupt_is_returned_and_the_request_is_gone(self) -> None:
        _, decision = self.ask(None, cancel=True)
        self.assertEqual(decision.outcome, perm.CANCELLED)
        self.assertFalse(decision.approved)


class TwoDispatches(unittest.TestCase):
    """R4-2. The inner dispatch saves the binding before the outer sets it, then restores after.

    On one shared field that restore writes the unbound marker over the outer call while the outer
    tool is still running, and keep_approved then changes a file the request never showed.
    """

    def test_a_second_thread_cannot_clear_the_approved_facts(self) -> None:
        ws = workspace(self, {"shown.txt": "shown\n", "secret.txt": "secret\n"})
        secret = ws.root / "secret.txt"
        before = secret.read_bytes()
        inner_holding, release_inner, inner_done = threading.Event(), threading.Event(), threading.Event()

        def approve(call: b.ToolCall) -> perm.Decision:
            if call.name == "inner":
                inner_holding.set()  # line that saved the binding has already run
                self.assertTrue(release_inner.wait(3), "the outer call did not release the inner one")
            return approve_all(call)

        def outer_run(args, ctx):
            release_inner.set()
            self.assertTrue(inner_done.wait(3), "the inner dispatch did not finish")
            self.assertEqual(ctx.approved_facts, {"resolved_path": "shown.txt"})
            with self.assertRaises(ToolError):
                files.keep_approved(ctx, "secret.txt", secret)
            self.assertEqual(secret.read_bytes(), before)
            return r.ToolOutput("outer")

        inner = r.Tool(b.ToolSpec("inner", "inner", SPEC.input_schema),
                       lambda args, ctx: r.ToolOutput("inner"), True,
                       lambda args, ctx: {"resolved_path": "inner.txt"})
        outer = r.Tool(b.ToolSpec("outer", "outer", SPEC.input_schema), outer_run, True,
                       lambda args, ctx: {"resolved_path": "shown.txt"})
        ctx = r.ToolContext(ws, b.CancelToken(), lambda text: text, approve=approve)

        def run_inner() -> None:
            r.ToolRegistry((inner,)).dispatch(b.ToolCall("i1", "inner", {}), ctx)
            inner_done.set()

        thread = threading.Thread(target=run_inner, daemon=True)
        thread.start()
        self.addCleanup(lambda: release_inner.set())
        self.assertTrue(inner_holding.wait(3), "the inner dispatch did not reach its approval")
        out = r.ToolRegistry((outer,)).dispatch(b.ToolCall("o1", "outer", {}), ctx)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertEqual(secret.read_bytes(), before)
        self.assertIs(ctx.approved_facts, r.UNBOUND)


class CrlfOffset(unittest.TestCase):
    """R4-3. The anchor sits on line 6 of a ten-line CRLF file, so the match offset is not zero."""

    def test_a_crlf_anchor_past_the_start_keeps_the_match_offset(self) -> None:
        raw = "".join(f"line {i}\r\n" for i in range(1, 11)).encode()
        ws = workspace(self, {"a.txt": raw})
        reg = r.ToolRegistry((read.TOOL, edit.TOOL))
        ctx = r.ToolContext(ws, b.CancelToken(), lambda text: text, approve=approve_all)
        page = reg.dispatch(b.ToolCall("read-1", "read", {"path": "a.txt"}), ctx)
        self.assertEqual(page.status, r.OK, page.model_text)
        out = reg.dispatch(b.ToolCall("edit-1", "edit", {
            "path": "a.txt",
            "old_string": "line 5\nline 6\nline 7",
            "new_string": "line 5\nLINE SIX\nline 7",
        }), ctx)
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertIn(
            "     2\tline 2\n     3\tline 3\n     4\tline 4\n"
            "     5\tline 5\n     6\tLINE SIX\n     7\tline 7\n     8\tline 8\n",
            out.model_text,
        )
        self.assertNotIn("\r", out.model_text)
        self.assertEqual((ws.root / "a.txt").read_bytes(), raw.replace(b"line 6", b"LINE SIX", 1))


class UnpollablePipe(unittest.TestCase):
    """R3-6. The registry must not report this as a tool that crashed."""

    def test_an_unpollable_pipe_is_a_typed_refusal(self) -> None:
        ws = workspace(self)
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")

        def close() -> None:
            try:
                stream.close()
            except OSError:
                pass
            try:
                os.close(write_fd)
            except OSError:
                pass

        self.addCleanup(close)
        process = mock.Mock(pid=424242, stdout=stream, returncode=None)
        process.wait.return_value = 0
        process.poll.return_value = None
        ctx = r.ToolContext(ws, b.CancelToken(), lambda text: text, approve=approve_all)
        with mock.patch.object(shell.subprocess, "Popen", return_value=process), \
                mock.patch.object(shell.selectors, "DefaultSelector",
                                  side_effect=OSError(errno.EMFILE, "synthetic selector exhaustion")), \
                mock.patch.object(shell.os, "set_blocking", side_effect=OSError(errno.EINVAL, "synthetic")), \
                mock.patch.object(shell, "_stop") as stop, \
                mock.patch.object(shell, "_kill_group"):
            out = r.ToolRegistry((shell.TOOL,)).dispatch(
                b.ToolCall("sh1", "shell", {"command": "unused inert command"}), ctx)
        self.assertEqual(out.status, r.ERROR)
        self.assertTrue(out.model_text.startswith("refused:"), out.model_text)
        self.assertNotIn("the tool failed", out.model_text)
        stop.assert_called()


class FactShapes(unittest.TestCase):
    """R3-7. ask refuses a non-mapping. check_facts still ignores one, which is what a precheck may return."""

    def test_facts_that_are_not_a_mapping_are_refused_before_a_request(self) -> None:
        self.assertIsNone(perm.check_facts(["resolved_path"]))  # a precheck return that is not a mapping: ignored
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, deadline_s=0.05)
        with self.assertRaises(ValueError):
            gate.ask("turn-1", CALL, "/ws", b.CancelToken(), facts=["resolved_path"])
        self.assertEqual(gate.waiting(), ())
        self.assertEqual(lines.of("permission_request"), [])
        # The same gate still asks for a real mapping, and for none.
        taken = gate.ask("turn-1", CALL, "/ws", b.CancelToken(), facts={"resolved_path": "a.txt"})
        self.assertEqual(taken.outcome, perm.EXPIRED)
        self.assertEqual(lines.of("permission_request")[0]["resolved_path"], "a.txt")
        self.assertEqual(gate.waiting(), ())

    def test_a_context_without_approved_facts_cannot_change_a_file(self) -> None:
        ws = workspace(self, {"a.txt": b"one\n"})
        ctx = r.ToolContext(ws, b.CancelToken(), lambda text: text)
        page = r.ToolRegistry((read.TOOL,)).dispatch(b.ToolCall("r1", "read", {"path": "a.txt"}), ctx)
        self.assertEqual(page.status, r.OK, page.model_text)
        foreign = SimpleNamespace(workspace=ws, cancel=b.CancelToken())
        with self.assertRaises(ToolError) as caught:
            edit.run({"path": "a.txt", "old_string": "one", "new_string": "two"}, foreign)
        self.assertIn("no record of what was approved", str(caught.exception))
        self.assertEqual((ws.root / "a.txt").read_bytes(), b"one\n")


class FinaliserRaises(unittest.TestCase):
    """N6. fail_turn raises after the call was counted. The two totals are written before turn_ended."""

    def test_a_finaliser_that_raises_still_writes_the_totals(self) -> None:
        lines = Lines()
        session = Session(FakeProvider([RuntimeError("synthetic loop bug"), {"text": "after"}]), registry(),
                          workspace(self), lines.writer, lines.redactor, Config("fake-model"))

        def boom(turn_id: str, detail: str) -> str:
            raise RuntimeError("synthetic finaliser")

        session.fail_turn = boom  # type: ignore[method-assign]
        engine = Engine(session, lines.writer, log=lambda message: None)
        engine.feed_line(MSG)
        engine.feed_line(MSG2)
        engine.input_ended()
        self.assertEqual(engine.serve(), 0)
        docs = lines.all()
        kinds = [(d["type"], d.get("span")) for d in docs]
        first_end = kinds.index(("turn_ended", None))
        self.assertLess(kinds.index(("usage", "turn")), first_end)
        self.assertLess(kinds.index(("usage", "session_total")), first_end)
        ended = lines.of("turn_ended")
        self.assertEqual(ended[0]["outcome"], "failed")
        self.assertEqual(ended[0]["reason"], "loop_error")
        self.assertEqual(ended[0]["result"]["detail"], "RuntimeError")
        for span in ("turn", "session_total"):
            (row,) = [d for d in lines.of("usage") if d["span"] == span and d["turn_id"] == "turn-1"]
            self.assertEqual(row["input_tokens"]["quality"], "unknown")
        self.assertEqual(ended[1]["outcome"], "candidate_done")
        self.assertEqual(ended[1]["turn_id"], "turn-2")


if __name__ == "__main__":
    unittest.main()
