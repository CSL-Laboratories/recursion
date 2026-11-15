"""Round 4 of the reads of #402 (lane DRIVER-CX): this lane's tests for DeepSeek V4.1 Flash's findings R4-1 to R4-3,
and for each rule of the round-4 answer (each fails when its rule is taken out).

Fakes only: no real codex, no credential, no network; every key below is made up. A test that waits on another
thread gives it STUCK seconds before it calls it stuck; a passing run never waits that long.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests import test_codex_attended as attended
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import (AppServer, FakeApprovals, Launcher, answers, ask_command,
                                                           begin, done, message, record, turn)

STUCK = 10
SHORT = 0.2  # the driver's port_wait_s where a test needs the bound to pass quickly
ROUNDS = 200
ANSWER = {"kind": "approval_answer", "approval_id": "apr-1", "decision": "approve"}
STARTED = {"method": "turn/started", "params": {"threadId": "thr-1", "turn": turn("trn-1")}}
DECLINED = {"decision": "decline"}


def feed(driver, handle, msg):
    return driver.parse(handle, json.dumps(msg))


def opened(port=None, silent_turns=False, started=True, **options):
    """A driver with turn-1 begun; ``rid`` is our turn/start's id. Without ``silent_turns`` the engine has named
    the turn trn-1. With it the turn/start is not answered yet, and only ``started`` reads turn/started first."""

    server = AppServer(silent_turns=silent_turns)
    driver = cx.CodexDriver(Launcher(server), FakeClock(), approvals=port, **options)
    handle = begin(driver, record(), {})
    driver.send(handle, message())
    rid = server.sent[-1]["id"]
    while server.out:
        driver.pump(handle, 0)
    if silent_turns and started:
        feed(driver, handle, STARTED)
    return server, driver, handle, rid


def start_answer(rid, engine_turn="trn-1"):
    return {"id": rid, "result": {"turn": turn(engine_turn)}}


def kinds(drafts):
    return [d["type"] for d in drafts]


def in_thread(target):
    """Run ``target`` on a daemon thread (a stuck one does not hold the test process); its exceptions are kept."""

    errors: list = []

    def run():
        try:
            target()
        except BaseException as err:  # noqa: BLE001 - kept for the test to read
            errors.append(err)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def turn_starts(server):
    return [m["params"]["clientUserMessageId"] for m in server.sent if m.get("method") == "turn/start"]


class UnnamedEndTest(unittest.TestCase):
    """DeepSeek R4-1: a turn's end read before anything named the turn's id (a reader on another thread can take it
    before the answer to turn/start, when no turn/started came first) is kept for that answer, never lost."""

    def test_an_end_read_before_the_answer_names_its_turn_ends_it_with_that_answer(self):
        server, driver, handle, rid = opened(silent_turns=True, started=False)
        self.assertEqual(feed(driver, handle, done()("trn-1")), [])  # nothing has named trn-1 yet: kept
        self.assertIsNotNone(handle.live_turn)
        drafts = feed(driver, handle, start_answer(rid))
        self.assertEqual(kinds(drafts), ["input.accepted", "usage", "result"])
        self.assertIsNone(handle.live_turn)
        session = handle._codex
        self.assertEqual((session.core_turn, session.engine_turn, session.unnamed_ends), ({}, {}, {}))
        self.assertEqual(feed(driver, handle, STARTED), [])  # a late turn/started does not bring it back
        self.assertEqual((session.core_turn, session.engine_turn), ({}, {}))
        driver.send(handle, message(turn_id="turn-2", delivery="dlv-2"))  # the next message begins a new turn
        self.assertEqual(turn_starts(server), ["dlv-1", "dlv-2"])

    def test_turn_started_after_an_unnamed_end_retires_the_turn_and_opens_no_approval(self):
        port = FakeApprovals()
        server, driver, handle, rid = opened(port, silent_turns=True, started=False)
        feed(driver, handle, done()("trn-1"))
        self.assertEqual(feed(driver, handle, STARTED), [])
        feed(driver, handle, ask_command()("trn-1"))
        self.assertEqual(port.requests, [])  # no approval for a turn that is over
        self.assertEqual(answers(server, 77), [{"id": 77, "result": DECLINED}])
        self.assertEqual(kinds(feed(driver, handle, start_answer(rid))), ["input.accepted", "usage", "result"])
        self.assertIsNone(handle.live_turn)

    def test_an_end_nothing_names_is_dropped_with_the_answer_and_the_named_turn_goes_on(self):
        server, driver, handle, rid = opened(silent_turns=True, started=False)
        feed(driver, handle, done()("trn-other"))
        self.assertEqual(kinds(feed(driver, handle, start_answer(rid))), ["input.accepted"])
        self.assertEqual(handle._codex.unnamed_ends, {})
        self.assertIsNotNone(handle.live_turn)
        self.assertEqual(kinds(feed(driver, handle, done()("trn-1"))), ["usage", "result"])
        self.assertIsNone(handle.live_turn)

    def test_an_end_read_while_no_start_waits_is_not_kept(self):
        _, driver, handle, _ = opened()
        self.assertEqual(feed(driver, handle, done()("trn-9")), [])
        self.assertEqual(handle._codex.unnamed_ends, {})
        self.assertIsNotNone(handle.live_turn)

    def test_at_most_eight_unnamed_ends_are_kept(self):
        _, driver, handle, _ = opened(silent_turns=True, started=False)
        for n in range(50):
            feed(driver, handle, done()(f"trn-x{n}"))
        self.assertEqual(len(handle._codex.unnamed_ends), cx._UNNAMED_ENDS)
        self.assertEqual(cx._UNNAMED_ENDS, 8)

    def test_two_readers_never_lose_the_end(self):
        # DeepSeek's p7_lost_end.py as a test: the answer and the end, read on two threads, no turn/started.
        lost = []
        for n in range(ROUNDS):
            server, driver, handle, rid = opened(silent_turns=True, started=False)
            server.out[:] = [json.dumps(start_answer(rid)), json.dumps(done()("trn-1"))]
            drafts, bar = [], threading.Barrier(2)

            def one():
                bar.wait(STUCK)
                drafts.extend(driver.pump(handle, 0) or [])
            readers = [in_thread(one) for _ in range(2)]
            for reader, _ in readers:
                reader.join(STUCK)
            self.assertEqual([err for _, errors in readers for err in errors], [])
            if handle.live_turn is not None or "result" not in kinds(drafts):
                lost.append(n)
        self.assertEqual(lost, [], f"the end was lost in {len(lost)} of {ROUNDS} rounds")


class PortWaitTest(unittest.TestCase):
    """DeepSeek R4-2: ``close`` ends the input at once and waits for a port call in flight at most port_wait_s; a
    request that finds another with the port waits as long at most, then is declined. So a port that waits for a
    thread which calls back into the driver delays the worker and never wedges it."""

    def test_a_port_that_waits_for_a_thread_which_closes_the_worker_returns(self):
        class Port(FakeApprovals):
            def request(inner, effect, binding):
                closer = threading.Thread(target=lambda: driver.send(handle, {"kind": "close"}), daemon=True)
                closer.start()
                closer.join()  # no bound: DeepSeek's R4b
                return super().request(effect, binding)
        server, driver, handle, _ = opened(Port(), port_wait_s=SHORT)
        reader, errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        reader.join(STUCK)
        self.assertFalse(reader.is_alive(), "the worker is wedged")
        self.assertEqual(errors, [])
        self.assertTrue(server.stdin_closed)
        self.assertEqual(handle._codex.bindings, {})  # what the port opened while the input ended is not kept
        self.assertEqual(answers(server, 77), [])  # and nothing is written after close

    def test_a_port_that_waits_for_a_thread_which_reads_another_request_returns(self):
        class Port(FakeApprovals):
            def request(inner, effect, binding):
                if not inner.requests:
                    nested = threading.Thread(
                        target=lambda: feed(driver, handle, ask_command(rid=78, item="it-2")("trn-1")), daemon=True)
                    nested.start()
                    nested.join()  # no bound
                return super().request(effect, binding)
        port = Port()
        server, driver, handle, _ = opened(port, port_wait_s=SHORT)
        reader, errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        reader.join(STUCK)
        self.assertFalse(reader.is_alive(), "the worker is wedged")
        self.assertEqual(errors, [])
        self.assertEqual(answers(server, 78), [{"id": 78, "result": DECLINED}])
        self.assertEqual(len(port.requests), 1)  # only the outer request was put to the port
        driver.send(handle, ANSWER)
        self.assertEqual(answers(server, 77), [{"id": 77, "result": {"decision": "accept"}}])

    def test_close_ends_the_input_at_once_while_a_port_call_is_in_flight(self):
        entered, go = threading.Event(), threading.Event()

        class Port(FakeApprovals):
            def request(inner, effect, binding):
                entered.set()
                go.wait(STUCK)
                return super().request(effect, binding)
        server, driver, handle, _ = opened(Port())  # the default bound, PORT_WAIT_S
        reader, reader_errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        self.assertTrue(entered.wait(STUCK))
        closer, closer_errors = in_thread(lambda: driver.send(handle, {"kind": "close"}))
        try:
            deadline = time.monotonic() + STUCK
            while not server.stdin_closed and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.stdin_closed, "close waited for the port before it ended the input")
            self.assertTrue(closer.is_alive(), "close returns only after the port call in progress")
        finally:
            go.set()
            reader.join(STUCK)
            closer.join(STUCK)
        self.assertFalse(reader.is_alive() or closer.is_alive())
        self.assertEqual((reader_errors, closer_errors), ([], []))
        self.assertEqual(handle._codex.bindings, {})
        self.assertEqual(answers(server, 77), [])

    def test_close_waits_for_the_port_at_most_port_wait_s(self):
        entered, go = threading.Event(), threading.Event()

        class Port(FakeApprovals):
            def request(inner, effect, binding):
                entered.set()
                go.wait(STUCK)
                return super().request(effect, binding)
        server, driver, handle, _ = opened(Port(), port_wait_s=SHORT)
        reader, reader_errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        self.assertTrue(entered.wait(STUCK))
        closer, closer_errors = in_thread(lambda: driver.send(handle, {"kind": "close"}))
        closer.join(STUCK)
        try:
            self.assertFalse(closer.is_alive(), "close waited for the port without a bound")
            self.assertTrue(reader.is_alive())  # the port is still being asked
        finally:
            go.set()
            reader.join(STUCK)
        self.assertEqual((reader_errors, closer_errors), ([], []))
        self.assertTrue(server.stdin_closed)
        self.assertEqual(handle._codex.bindings, {})

    def test_a_request_that_finds_another_with_the_port_is_declined_after_port_wait_s(self):
        entered, go = threading.Event(), threading.Event()

        class Port(FakeApprovals):
            def request(inner, effect, binding):
                entered.set()
                go.wait(STUCK)
                return super().request(effect, binding)
        port = Port()
        server, driver, handle, _ = opened(port, port_wait_s=SHORT)
        first, first_errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        self.assertTrue(entered.wait(STUCK))
        second, second_errors = in_thread(lambda: feed(driver, handle, ask_command(rid=78, item="it-2")("trn-1")))
        second.join(STUCK)
        try:
            self.assertFalse(second.is_alive(), "the second request waited for the port without a bound")
            self.assertEqual(answers(server, 78), [{"id": 78, "result": DECLINED}])
        finally:
            go.set()
            first.join(STUCK)
        self.assertEqual((first_errors, second_errors), ([], []))
        self.assertEqual(len(port.requests), 1)
        driver.send(handle, ANSWER)
        self.assertEqual(answers(server, 77), [{"id": 77, "result": {"decision": "accept"}}])


class ToolChildEnvTest(unittest.TestCase):
    """DeepSeek R4-3: the attended tool's ``codex --version`` and schema children get the environment its worker
    gets (``child_env``), so no API-key name reaches them."""

    MADE_UP = {"MY_API_KEY": "made-up-1", "GITHUB_TOKEN": "made-up-2"}

    def setUp(self):
        self.tool = attended.tool()

    def test_run_hands_its_child_the_workers_environment(self):
        seen = {}

        def fake_run(argv, **options):
            seen.update(options)
            return mock.Mock(stdout="codex-cli 0.0.0\n")
        with mock.patch.dict(os.environ, self.MADE_UP), mock.patch.object(self.tool.subprocess, "run", fake_run):
            self.assertEqual(self.tool._run(["codex", "--version"]), "codex-cli 0.0.0")
            expected = self.tool.child_env(dict(os.environ))
        self.assertEqual(seen.get("env"), expected)
        self.assertEqual(set(self.MADE_UP) & set(seen["env"]), set())

    def test_a_real_child_of_run_sees_no_key_name(self):
        code = "import os; print(sorted(k for k in os.environ if k in ('MY_API_KEY', 'GITHUB_TOKEN')))"
        with mock.patch.dict(os.environ, self.MADE_UP):
            self.assertEqual(self.tool._run([sys.executable, "-c", code]), "[]")


if __name__ == "__main__":
    unittest.main()
