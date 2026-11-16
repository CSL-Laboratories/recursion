"""The permission gate: one request per call, answered, denied, interrupted, expired, or let through by an
acknowledged full-access profile."""

from __future__ import annotations

import threading
import unittest

from recursion_core.loop import permissions as perm
from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.support import Lines

CALL = b.ToolCall("toolu_w", "write", {"path": "a.txt", "content": "hi", "n": 1}, "")


def ask_in_thread(gate: perm.PermissionGate, cancel: b.CancelToken | None = None):
    box: dict = {}

    def run() -> None:
        box["decision"] = gate.ask("turn-1", CALL, "/ws", cancel or b.CancelToken())

    t = threading.Thread(target=run)
    t.start()
    return t, box


def wait_request(lines: Lines, gate: perm.PermissionGate) -> dict:
    # Synchronise on the request line itself (it is written after the request is registered).
    while not lines.of("permission_request"):
        threading.Event().wait(0.001)
    return lines.of("permission_request")[-1]


class Gate(unittest.TestCase):
    def test_a_request_names_the_call_and_an_answer_runs_it(self) -> None:
        lines = Lines()
        gate = perm.PermissionGate(lines.writer)
        t, box = ask_in_thread(gate)
        req = wait_request(lines, gate)
        self.assertEqual(req, {"type": "permission_request", "turn_id": "turn-1", "request_id": "perm-1",
                               "call_id": "toolu_w", "tool": "write", "cwd": "/ws", "input": dict(CALL.input),
                               "profile": "ask", "deadline_s": perm.DEFAULT_DEADLINE_S})
        self.assertIsNone(gate.answer("perm-1", "apr-1", "approve", {"path": "a.txt", "content": "hi", "n": 1.0}))
        t.join()
        self.assertTrue(box["decision"].approved)
        self.assertEqual(box["decision"].approval_id, "apr-1")
        self.assertEqual(gate.answer("perm-1", "apr-1", "approve", None), "no_pending_request",
                         "an answer counts once")

    def test_deny(self) -> None:
        lines = Lines()
        gate = perm.PermissionGate(lines.writer)
        t, box = ask_in_thread(gate)
        wait_request(lines, gate)
        gate.answer("perm-1", "apr-1", "deny", None)
        t.join()
        self.assertEqual(box["decision"].outcome, perm.DENIED)

    def test_a_bound_input_that_differs_is_not_run(self) -> None:
        for bound in ({"path": "b.txt", "content": "hi", "n": 1}, {"path": "a.txt", "content": "hi", "n": True},
                      {"path": "a.txt", "content": "hi"}, {"path": "a.txt", "content": "hi", "n": 1, "x": 0}):
            with self.subTest(bound=bound):
                lines = Lines()
                gate = perm.PermissionGate(lines.writer)
                t, box = ask_in_thread(gate)
                wait_request(lines, gate)
                gate.answer("perm-1", "apr-1", "approve", bound)
                t.join()
                self.assertEqual(box["decision"].outcome, perm.MISMATCH)

    def test_no_answer_by_the_deadline_is_a_denial(self) -> None:
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, deadline_s=0.05)  # nobody answers: the deadline must fire
        decision = gate.ask("turn-1", CALL, "/ws", b.CancelToken())
        self.assertEqual(decision.outcome, perm.EXPIRED)
        self.assertEqual(lines.of("permission_expired"), [{"type": "permission_expired", "turn_id": "turn-1",
                                                           "request_id": "perm-1"}])
        self.assertEqual(gate.answer("perm-1", "apr-1", "approve", None), "no_pending_request",
                         "a late answer changes nothing")

    def test_an_interrupt_wakes_the_wait(self) -> None:
        lines = Lines()
        gate = perm.PermissionGate(lines.writer)
        cancel = b.CancelToken()
        t, box = ask_in_thread(gate, cancel)
        wait_request(lines, gate)
        cancel.cancel()
        t.join()
        self.assertEqual(box["decision"].outcome, perm.CANCELLED)
        self.assertEqual(gate.waiting(), ())

    def test_a_call_too_large_to_show_is_denied_without_asking(self) -> None:
        lines = Lines()
        # A short deadline: without the size rule the call is asked for and expires at once (red in moments, as
        # DeepSeek's round-1 read asked), instead of waiting out the default fifteen minutes.
        gate = perm.PermissionGate(lines.writer, deadline_s=0.01)
        big = b.ToolCall("toolu_big", "write", {"path": "a", "content": "x" * perm.MAX_EFFECT_BYTES}, "")
        self.assertEqual(gate.ask("turn-1", big, "/ws", b.CancelToken()).outcome, perm.TOO_LARGE)
        self.assertEqual(lines.of("permission_request"), [])

    def test_full_access_lets_calls_through_and_says_so(self) -> None:
        lines = Lines()
        gate = perm.PermissionGate(lines.writer, profile=perm.FULL_ACCESS)
        decision = gate.ask("turn-1", CALL, "/ws", b.CancelToken())
        self.assertTrue(decision.approved)
        self.assertEqual(lines.of("permission_request")[0]["profile"], "full_access")

    def test_same_input_is_strict_about_types(self) -> None:
        self.assertTrue(perm.same_input({"a": [1, 2.0, -0.0]}, {"a": [1.0, 2, 0]}))
        self.assertFalse(perm.same_input({"a": True}, {"a": 1}))
        self.assertFalse(perm.same_input({"a": None}, {"a": 0}))
        self.assertFalse(perm.same_input({"a": "1"}, {"a": 1}))
        self.assertFalse(perm.same_input([1], [1, 1]))


if __name__ == "__main__":
    unittest.main()
