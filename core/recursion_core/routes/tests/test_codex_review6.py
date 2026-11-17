"""Round 5 of the Codex reads of #402 (GPT-6 Astra, GPT-6.1 Sol) and round 6 of DeepSeek V4.1 Flash's, lane DRIVER-CX:
the admission's session lock (Sol's required change 2, DeepSeek F2), and round 5's P2 (Astra 1, Sol 1, DeepSeek F1),
answered on DRIVER-CC's port withdraw (#432): a request admitted and not yet published when the input ends reaches no
port (PortWithdrawTest, turned over from the test that pinned the open defect), ending the input waits for a port call
already running and raises past #432's wait (RevealWaitTest), and with a port that takes the ticket, APPROVALS' side
as #432's corrected contract asks it, no approval opens after ending the input returns (TicketPortTest).
Also the sweep of #402 against master's F1 (WorkspaceTest) and DeepSeek round 7's F2 (BindingsTest).

Fakes only: no real codex, no credential, no network. A test that waits on another thread gives it STUCK seconds
before it calls it stuck; a passing run never waits that long.
"""

from __future__ import annotations

import linecache
import sys
import threading
import unittest

from unittest import mock

from recursion_core.routes import driver as gate
from recursion_core.routes.driver import PUBLISHED, WITHDRAWN
from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import (AppServer, FakeApprovals, Launcher, answers,
                                                           ask_command, begin, record)
from recursion_core.routes.tests.test_codex_review4 import STUCK, feed, in_thread, opened
from recursion_core.routes.tests.test_codex_review4_codex import PORT_CALL, held_at

SHORT = 0.3  # how long a step that must wait is watched not to return
ADMISSION = "if session.closed or turn not in session.core_turn or key in session.resolved:"


class AdmissionLockTest(unittest.TestCase):
    """Sol's required change 2 and DeepSeek's F2: the admission and the record of the input's end are one step each
    under the session lock, so ending the input waits while a request is being admitted. GPT-6.1 Sol's supplied
    witness, adopted (renamed; the same rule as GPT-6 Astra's): it fails when the admission loses its lock."""

    def test_admission_check_holds_the_session_lock_against_ending_the_input(self):
        port = FakeApprovals()
        server, driver, handle, _ = opened(port, port_wait_s=0)
        entered, release, first = threading.Event(), threading.Event(), []

        def trace(frame, event, arg):
            if event == "line" and frame.f_code is cx.CodexDriver._ask.__code__ and not first:
                if ADMISSION in linecache.getline(frame.f_code.co_filename, frame.f_lineno):
                    first.append(frame.f_lineno)  # the admission's check: the first line with this text
                    entered.set()
                    if not release.wait(STUCK):
                        raise AssertionError("the test never let the reader go on")
            return trace

        def read():
            sys.settrace(trace)
            try:
                feed(driver, handle, ask_command()("trn-1"))
            finally:
                sys.settrace(None)
        reader, reader_errors = in_thread(read)
        self.assertTrue(entered.wait(STUCK))
        ending, ended = threading.Event(), threading.Event()

        def end_input():
            ending.set()
            try:
                driver.send(handle, {"kind": "close"})
            finally:
                ended.set()
        ender, ender_errors = in_thread(end_input)
        self.assertTrue(ending.wait(STUCK))
        returned_early = ended.wait(SHORT)
        try:
            self.assertFalse(returned_early, "the input's end was recorded in the middle of an admission")
        finally:
            release.set()
            reader.join(STUCK)
            ender.join(STUCK)
        self.assertFalse(reader.is_alive() or ender.is_alive())
        self.assertEqual(reader_errors + ender_errors, [])
        self.assertTrue(server.stdin_closed)
        # Sol's last assertion (no binding kept) is left out: once the reader goes on, its port call and the recorded
        # end race, and a request answered by the port before the end is recorded keeps its binding like any approval
        # opened before it (seen in 3 of 7 runs on 3.12). The rule under test is the wait above.


class PortWithdrawTest(unittest.TestCase):
    """Round 5's P2 (GPT-6 Astra 1, GPT-6.1 Sol 1; DeepSeek round 6, F1), on #432's corrected withdraw. This was
    PortWithdrawPendingTest, which pinned the open defect; turned over: a request admitted and not yet revealed when
    the input ends reaches no port (its reveal, the port's whole call, never begins once the port is sealed), ending the
    input still returns, and nothing of it is kept."""

    def test_a_request_admitted_and_not_published_when_the_input_ends_reaches_no_port(self):
        port = FakeApprovals()
        server, driver, handle, _ = opened(port, port_wait_s=0)
        read, entered, release = held_at(PORT_CALL)  # held just before its reveal begins
        reader, reader_errors = in_thread(lambda: read(driver, handle))
        self.assertTrue(entered.wait(STUCK))
        ended = threading.Event()
        ender, ender_errors = in_thread(lambda: (driver.send(handle, {"kind": "close"}), ended.set()))
        try:
            self.assertTrue(ended.wait(STUCK), "ending the input did not return")
            self.assertTrue(server.stdin_closed)
        finally:
            release.set()
            reader.join(STUCK)
            ender.join(STUCK)
        self.assertFalse(reader.is_alive() or ender.is_alive())
        self.assertEqual((reader_errors, ender_errors), ([], []))
        self.assertEqual(port.requests, [])  # it never reached the port, before or after the end returned
        self.assertEqual(handle._codex.bindings, {})
        self.assertEqual(answers(server, 77), [])


class HeldPort(FakeApprovals):
    """A port without the ticket whose call is held inside it (its reveal running) until ``go`` is set."""

    def __init__(self):
        super().__init__()
        self.entered, self.go, self.opened = threading.Event(), threading.Event(), threading.Event()

    def request(self, effect, binding):
        self.entered.set()
        self.go.wait(STUCK)
        approval_id = super().request(effect, binding)
        self.opened.set()
        return approval_id


class RevealWaitTest(unittest.TestCase):
    """#432's corrected contract, for a port without the ticket: its whole call is the reveal, so ending the input
    waits for a call already running (at most #432's REVEAL_WAIT_S) and returns only once the approval it opens is
    visible; past that wait it raises PortWithdrawIncomplete instead of returning. Nothing of it is kept."""

    def test_ending_the_input_waits_for_a_port_call_already_running(self):
        port = HeldPort()
        server, driver, handle, _ = opened(port, port_wait_s=0)
        reader, reader_errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        self.assertTrue(port.entered.wait(STUCK))
        ended, seen = threading.Event(), []

        def end_input():
            driver.send(handle, {"kind": "close"})
            seen.append(port.opened.is_set())  # was the approval visible before the end returned?
            ended.set()
        ender, ender_errors = in_thread(end_input)
        try:
            self.assertFalse(ended.wait(SHORT), "the end returned while a reveal was running")
        finally:
            port.go.set()
            reader.join(STUCK)
            ender.join(STUCK)
        self.assertFalse(reader.is_alive() or ender.is_alive())
        self.assertEqual((reader_errors, ender_errors), ([], []))
        self.assertEqual(seen, [True])
        self.assertEqual(len(port.requests), 1)
        self.assertEqual(handle._codex.bindings, {})  # the input had ended when the port answered: not kept

    def test_a_port_call_past_the_reveal_wait_makes_ending_the_input_raise(self):
        port = HeldPort()
        server, driver, handle, _ = opened(port, port_wait_s=0)
        reader, reader_errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        self.assertTrue(port.entered.wait(STUCK))
        try:
            with mock.patch.object(gate, "REVEAL_WAIT_S", 0.05):
                with self.assertRaises(gate.PortWithdrawIncomplete):
                    driver.send(handle, {"kind": "close"})
            self.assertTrue(server.stdin_closed)
        finally:
            port.go.set()
            reader.join(STUCK)
        self.assertFalse(reader.is_alive())
        self.assertEqual(reader_errors, [])
        self.assertEqual(handle._codex.bindings, {})


class TicketPort(FakeApprovals):
    """APPROVALS' side as #432's corrected contract asks it (not built yet): ``request(effect, binding, *, ticket)``
    makes the approval visible only through ``ticket.publish(reveal)``, its commit being the reveal, and opens nothing,
    with a typed refusal, when that answers False. ``hold`` keeps a call inside the port, before its commit, until
    ``go`` is set."""

    def __init__(self, hold=False):
        super().__init__()
        self.hold, self.tickets = hold, []
        self.entered, self.go = threading.Event(), threading.Event()

    def request(self, effect, binding, *, ticket=None):
        self.tickets.append(ticket)
        if self.hold:
            self.entered.set()
            self.go.wait(STUCK)
        opened = []

        def reveal():
            opened.append(FakeApprovals.request(self, effect, binding))  # the commit: visible from here
        if ticket is None:
            reveal()
        elif not ticket.publish(reveal):
            raise RuntimeError("approval_withdrawn")  # the typed refusal DRIVER-CC proposed; nothing is opened
        return opened[0]


class TicketPortTest(unittest.TestCase):
    """With APPROVALS' side (a port that takes the ticket), no approval opens after ending the input returns, even for
    a call already inside the port; an approval it reveals works as before; and the driver keeps an approval only if
    its ticket was published."""

    def test_a_port_that_takes_the_ticket_opens_nothing_once_the_input_has_ended(self):
        port = TicketPort(hold=True)
        server, driver, handle, _ = opened(port, port_wait_s=0)
        reader, reader_errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        self.assertTrue(port.entered.wait(STUCK))
        try:
            driver.send(handle, {"kind": "close"})  # a bound of 0: returns while the call is inside the port
            self.assertTrue(server.stdin_closed)
        finally:
            port.go.set()
            reader.join(STUCK)
        self.assertFalse(reader.is_alive())
        self.assertEqual(reader_errors, [])
        self.assertEqual(port.requests, [])  # the port's commit found the ticket withdrawn: nothing opened
        self.assertEqual([t.state for t in port.tickets], [WITHDRAWN])
        self.assertEqual(handle._codex.bindings, {})

    def test_a_port_that_takes_the_ticket_publishes_it_and_the_approval_works(self):
        port = TicketPort()
        server, driver, handle, _ = opened(port)
        feed(driver, handle, ask_command()("trn-1"))
        self.assertEqual([t.state for t in port.tickets], [PUBLISHED])
        self.assertEqual(len(port.requests), 1)
        driver.send(handle, {"kind": "approval_answer", "approval_id": "apr-1", "decision": "approve"})
        self.assertEqual([m["result"] for m in answers(server, 77)], [{"decision": "accept"}])

    def test_a_ticket_the_port_did_not_publish_cannot_be_published_later(self):
        class Failing(TicketPort):
            def request(inner, effect, binding, *, ticket=None):
                inner.tickets.append(ticket)
                raise RuntimeError("the approval store is down")  # before its commit: nothing published
        port = Failing()
        server, driver, handle, _ = opened(port)
        feed(driver, handle, ask_command()("trn-1"))
        self.assertEqual([m["result"] for m in answers(server, 77)], [{"decision": "decline"}])
        self.assertFalse(port.tickets[0].publish(lambda: None))  # withdrawn on return: a later retry opens nothing
        self.assertEqual(port.tickets[0].state, WITHDRAWN)

    def test_an_approval_the_port_did_not_reveal_through_its_ticket_is_not_kept(self):
        class Skipping(TicketPort):
            def request(inner, effect, binding, *, ticket=None):
                inner.tickets.append(ticket)
                return FakeApprovals.request(inner, effect, binding)  # opened without publishing the ticket
        port = Skipping()
        server, driver, handle, _ = opened(port)
        feed(driver, handle, ask_command()("trn-1"))
        self.assertEqual(handle._codex.bindings, {})
        self.assertEqual([m["result"] for m in answers(server, 77)], [{"decision": "decline"}])
        self.assertEqual(port.tickets[0].state, WITHDRAWN)


class WorkspaceTest(unittest.TestCase):
    """The sweep of #402 against master (Nov 15, gpt-6.1-sol), F1: a start or resume that names a workspace sends its
    folder as the protocol's ``cwd`` (master's launcher starts the worker in a private home). The sweep's proposed
    test, adopted, with the case without a workspace added."""

    def test_f1_start_and_resume_use_the_selected_workspace(self):
        cases = [({"path": "/repo", "mode": "direct", "base_ref": None, "worktree_path": None}, "/repo"),
                 ({"path": "/repo", "mode": "worktree", "base_ref": "main", "worktree_path": "/repo-wt"}, "/repo-wt"),
                 (None, None)]
        for workspace, expected in cases:
            for esid, method in ((None, "thread/start"), ("thr-1", "thread/resume")):
                with self.subTest(workspace=workspace, esid=esid):
                    server = AppServer()
                    rec = record(esid)
                    rec["workspace"] = workspace
                    begin(cx.CodexDriver(Launcher(server), FakeClock()), rec, {})
                    call = next(m for m in server.sent if m.get("method") == method)
                    self.assertEqual(call["params"].get("cwd"), expected)
                    self.assertEqual(expected is None, "cwd" not in call["params"])


class BindingsTest(unittest.TestCase):
    """DeepSeek round 7, F2 (LOW): "nothing it opens is kept" held in 57 of 60 rounds on 3.12, because a binding the
    port answered before the end of the input was recorded stayed. Ending the input drops every binding."""

    def test_ending_the_input_drops_every_binding(self):
        server, driver, handle, _ = opened(FakeApprovals())
        feed(driver, handle, ask_command()("trn-1"))
        self.assertEqual(list(handle._codex.bindings), ["apr-1"])
        driver.send(handle, {"kind": "close"})
        self.assertEqual(handle._codex.bindings, {})


class PortReturnsNothingTest(unittest.TestCase):
    """The sweep's F2 shape of APPROVALS' side, adopted: a port that answers a withdrawn ticket with no approval id
    (instead of a typed refusal) opens nothing either."""

    def test_f2_close_withdraws_a_request_the_port_has_not_published(self):
        entered, release = threading.Event(), threading.Event()

        class DelayedPort(FakeApprovals):
            def request(self, effect, binding, *, ticket=None):
                entered.set()
                if not release.wait(STUCK):
                    raise AssertionError("test failed to release the fake port")
                opened = []
                if ticket is not None:
                    if not ticket.publish(lambda: opened.append(FakeApprovals.request(self, effect, binding))):
                        return None
                    return opened[0]
                return FakeApprovals.request(self, effect, binding)
        port = DelayedPort()
        server, driver, handle, _ = opened(port, port_wait_s=0)
        reader, errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        try:
            self.assertTrue(entered.wait(STUCK))
            driver.send(handle, {"kind": "close"})
            self.assertTrue(server.stdin_closed)
        finally:
            release.set()
            reader.join(STUCK)
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(port.requests, [], "a withdrawn worker opened a late approval")


if __name__ == "__main__":
    unittest.main()
