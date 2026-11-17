"""Round 4 of the reads of #402, the two Codex reads (lane DRIVER-CX): this lane's tests for GPT-6 Astra's required
changes 1 to 4 and GPT-6.1 Sol's 1 and 2 (Sol's are Astra's 3 and 4), each failing when its rule is taken out, and
the two witnesses the reads offered for later, adopted here.

Fakes only: no real codex, no credential, no network. A test that waits on another thread gives it STUCK seconds
before it calls it stuck; a passing run never waits that long.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import linecache
import sys
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.routes import contract as c
from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests import test_codex_attended as attended
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import (AppServer, FakeApprovals, Launcher, ask_command, begin, done,
                                                           record)
from recursion_core.routes.tests.test_codex_review4 import STUCK, feed, in_thread, kinds, opened, start_answer

ASKING = "session.asking = threading.get_ident()"
PORT_CALL = "ticket.publish(lambda: opened.append(self._approvals.request(effect, binding)))"  # #432's reveal


def held_at(text):
    """A reader that reads one approval request and is held at the first line of ``_ask`` that holds ``text``
    (a line is a place where a thread can be descheduled) until ``release`` is set."""

    entered, release = threading.Event(), threading.Event()

    def trace(frame, event, arg):
        if event == "line" and frame.f_code is cx.CodexDriver._ask.__code__:
            if text in linecache.getline(frame.f_code.co_filename, frame.f_lineno):
                entered.set()
                if not release.wait(STUCK):
                    raise AssertionError("the test never let the reader go on")
        return trace

    def read(driver, handle):
        sys.settrace(trace)
        try:
            feed(driver, handle, ask_command()("trn-1"))
        finally:
            sys.settrace(None)
    return read, entered, release


class AdmissionTest(unittest.TestCase):
    """Astra 1: a request is admitted to the port in one step under the session lock, the last before the port is
    called, so a close that comes before it is seen there and the port is never asked."""

    def close_while_held(self, text):
        port = FakeApprovals()
        server, driver, handle, _ = opened(port, port_wait_s=0)
        read, entered, release = held_at(text)
        reader, errors = in_thread(lambda: read(driver, handle))
        self.assertTrue(entered.wait(STUCK))
        try:
            driver.send(handle, {"kind": "close"})  # a bound of 0: returns at once though the reader holds ask_lock
            self.assertTrue(server.stdin_closed)
            self.assertEqual(port.requests, [])
        finally:
            release.set()
            reader.join(STUCK)
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(handle._codex.bindings, {})
        return port

    def test_a_close_before_the_admission_is_seen_and_the_port_is_never_asked(self):
        self.assertEqual(self.close_while_held(ASKING).requests, [])

    def test_a_request_admitted_but_not_published_before_the_close_reaches_no_port(self):
        # Turned over (round 5, Astra 1 and Sol 1; round 6, DeepSeek F1 and N1): it pinned GPT-6.1 Sol's round-4
        # boundary, a request past its admission delivered after the close; with #432's withdraw it reaches no port.
        self.assertEqual(self.close_while_held(PORT_CALL).requests, [])


class QuitDrainTest(unittest.TestCase):
    """Astra 2: after /quit has ended the input, at most HELD_READS more lines are read before the workers are
    ended, and the run says so when the worker was still writing."""

    def test_quit_returns_while_the_worker_keeps_writing(self):
        tool, said = attended.tool(), []

        class Writing(AppServer):
            reads = 0

            def read_line(inner, timeout_s):
                inner.reads += 1  # every read counts, so a run that never returns fails here instead of hanging
                if inner.reads > 3 * tool.HELD_READS:
                    raise AssertionError("the run kept reading what the worker writes")
                if inner.stdin_closed and not inner.out:
                    inner.emit({"method": "item/agentMessage/delta",
                                "params": {"threadId": "thr-1", "turnId": "trn-1", "itemId": "i", "delta": "x"}})
                return super().read_line(timeout_s)
        server = Writing()
        console = tool.Console(said.append)
        driver = cx.CodexDriver(Launcher(server), FakeClock(), approvals=console)
        tool.session_loop(driver, iter(["hello", "/quit"]), said.append, console, env={}, read_s=0)
        self.assertTrue(server.stdin_closed)
        self.assertTrue(any("still writes" in line for line in said), said[-3:])


class CleanupTest(unittest.TestCase):
    """Astra 3, Sol 1: a failure's message whose printing is interrupted neither stops the cleanup nor replaces
    the failure kept; and Astra's later witness: of two failures, the first is the one raised."""

    def run_mode(self, fence, printing=None, workers=3):
        tool, fenced = attended.tool(), []

        class Process:
            def __init__(self, *args, **options):
                self.proc = mock.Mock(pid=12345)

            def fence(self, why):
                fenced.append(why)
                fence(len(fenced))

        def loop(driver, *args, **options):
            for _ in range(workers):
                driver._launcher(["made-up"], env={})
        caught = None
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            args = argparse.Namespace(out=folder, workdir=folder, codex="made-up", model=None)
            with mock.patch.object(tool, "_run", return_value="made-up"), \
                    mock.patch.object(tool, "LocalProcess", Process), \
                    mock.patch.object(tool, "session_loop", side_effect=loop), \
                    mock.patch("builtins.print", side_effect=printing):
                try:
                    tool.run_mode(args)
                except BaseException as err:  # noqa: BLE001 - kept for the test to read
                    caught = err
        return fenced, caught

    def test_an_interrupted_message_still_ends_every_worker_and_keeps_the_first_failure(self):
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                def fence(n):
                    if n == 1:
                        raise OSError("first fence")

                def printing(*args, **options):
                    if args and "could not be ended" in str(args[0]):
                        raise interruption("the message is interrupted")
                fenced, caught = self.run_mode(fence, printing)
                self.assertEqual(fenced, ["attended_run_over"] * 3)
                self.assertIsInstance(caught, OSError)

    def test_cleanup_keeps_first_of_two_distinct_fence_failures(self):
        # GPT-6 Astra's witness (round 4, for later): it kills the removal of the first-failure rule.
        def fence(n):
            raise OSError("first fence") if n == 1 else ValueError("second fence")
        fenced, caught = self.run_mode(fence, workers=2)
        self.assertEqual(fenced, ["attended_run_over"] * 2)
        self.assertEqual((type(caught), str(caught)), (OSError, "first fence"))


class AbandonTest(unittest.TestCase):
    """Astra 4, Sol 2: a start that is given up tries both steps (ending the input, the fence) even when one is
    interrupted; the interruption is a note on the start's own refusal, which is what the caller gets."""

    def test_an_interrupted_step_still_fences_and_keeps_the_refusal(self):
        for interruption in (KeyboardInterrupt, SystemExit):
            for where in ("close", "fence"):
                with self.subTest(interruption=interruption.__name__, where=where):
                    attempted = []

                    class Broken(AppServer):
                        def read_line(self, timeout_s):
                            raise OSError("the output is gone")

                        def close_stdin(self):
                            attempted.append("close")
                            if where == "close":
                                raise interruption("interrupted")

                        def fence(self, reason):
                            attempted.append("fence")
                            if where == "fence":
                                raise interruption("interrupted")
                    caught = None
                    try:  # an interruption that escaped would stop the test run itself, so it is caught here
                        begin(cx.CodexDriver(Launcher(Broken()), FakeClock()), record(), {})
                    except BaseException as err:  # noqa: BLE001 - kept for the test to read
                        caught = err
                    self.assertEqual(attempted, ["close", "fence"])
                    self.assertIsInstance(caught, c.RouteError)
                    self.assertEqual(caught.code, c.ENGINE_ERROR)
                    self.assertEqual(caught.__notes__, [f"the worker could not be ended: {interruption.__name__}"])


class AdoptionWitnessTest(unittest.TestCase):
    """GPT-6.1 Sol's witness (round 4, for later): an end read while the answer to turn/start is being taken is
    not lost, on 3.12 too, where the two-reader test cannot interleave them."""

    def test_end_between_answer_step_and_held_release_is_not_lost(self):
        _, driver, handle, rid = opened(silent_turns=True, started=False)
        intervening, original = [], driver._adopt_turn

        def end_first(*args):
            intervening.extend(feed(driver, handle, done()("trn-1")))
            return original(*args)
        with mock.patch.object(driver, "_adopt_turn", side_effect=end_first):
            answered = feed(driver, handle, start_answer(rid))
        drafts = intervening + answered
        self.assertEqual(kinds(drafts).count("result"), 1)
        self.assertEqual(kinds(drafts).count("input.accepted"), 1)
        self.assertIsNone(handle.live_turn)
        self.assertEqual(handle._codex.engine_turn, {})


if __name__ == "__main__":
    unittest.main()
