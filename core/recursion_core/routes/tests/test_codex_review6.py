"""Round 5 of the Codex reads of #402 (GPT-6 Astra, GPT-6.1 Sol) and round 6 of DeepSeek V4.1 Flash's, lane DRIVER-CX:
the admission's session lock (Sol's required change 2, DeepSeek F2), and the OPEN defect both Codex reads require be
removed (Astra 1, Sol 1, DeepSeek F1): a request already past its admission can still be delivered to the approvals
port just after ending the input returns. Removing it needs a withdraw in the port's contract, which this lane does not
keep (lane question Q10); until then the second class below pins today's behaviour, and it is the test to turn over.

Fakes only: no real codex, no credential, no network. A test that waits on another thread gives it STUCK seconds
before it calls it stuck; a passing run never waits that long.
"""

from __future__ import annotations

import linecache
import sys
import threading
import unittest

from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests.test_codex_driver import FakeApprovals, answers, ask_command
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


class PortWithdrawPendingTest(unittest.TestCase):
    """The OPEN defect, pinned as it is today (round 5, GPT-6 Astra 1 and GPT-6.1 Sol 1; round 6, DeepSeek F1): the
    port has no cancellation, so a request already past its admission is delivered just after ending the input
    returns. It is delivered at most once, ending the input still returns, and nothing of it is kept, but the approval
    opens, which the reads do not accept. TURN THIS TEST OVER when the port gains a withdraw (lane question Q10, owed
    by the port's keeper, lane DRIVER-CC): the request must then not open an approval after ending the input returns."""

    def test_turn_over_when_the_port_can_withdraw_admitted_request_is_still_delivered_once_after_the_end(self):
        port = FakeApprovals()
        server, driver, handle, _ = opened(port, port_wait_s=0)
        read, entered, release = held_at(PORT_CALL)
        reader, reader_errors = in_thread(lambda: read(driver, handle))
        self.assertTrue(entered.wait(STUCK))
        ended = threading.Event()
        ender, ender_errors = in_thread(lambda: (driver.send(handle, {"kind": "close"}), ended.set()))
        try:
            self.assertTrue(ended.wait(STUCK), "ending the input did not return")
            self.assertTrue(server.stdin_closed)
            self.assertEqual(port.requests, [])  # not delivered yet when ending the input returned
        finally:
            release.set()
            reader.join(STUCK)
            ender.join(STUCK)
        self.assertFalse(reader.is_alive() or ender.is_alive())
        self.assertEqual((reader_errors, ender_errors), ([], []))
        self.assertEqual(len(port.requests), 1)  # TODAY: delivered after the end returned, once (the defect)
        self.assertEqual(handle._codex.bindings, {})  # nothing of it is kept
        self.assertEqual(answers(server, 77), [])  # and nothing is written for it


if __name__ == "__main__":
    unittest.main()
