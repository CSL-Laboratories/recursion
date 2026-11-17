"""Round 3 of the reads of #402 (lane DRIVER-CX): this lane's own tests for DeepSeek V4.1 Flash's findings F1 to F4,
and for each rule of the round-3 answer that no reader's test reaches alone (each fails when its rule is taken out).

GPT-6 Astra's and GPT-6.1 Sol's red tests are kept beside this file (test_codex_review3_astra.py and
test_codex_review3_sol.py). Fakes only: no real codex, no credential, no network. A test that waits on another
thread gives it STUCK seconds before it calls it stuck; a passing run never waits that long.
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from recursion_core.routes import contract as c
from recursion_core.routes.driver import DriverInputError, DriverStateError
from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests import test_codex_attended as attended
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import (EOF, AppServer, FakeApprovals, Launcher, agent, answers,
                                                           ask_command, begin, done, message, record, turn)

STUCK = 10
ANSWER = {"kind": "approval_answer", "approval_id": "apr-1", "decision": "approve"}
SETTLED = {"method": "serverRequest/resolved", "params": {"threadId": "thr-1", "requestId": 77}}


def feed(driver, handle, msg):
    return driver.parse(handle, json.dumps(msg))


def opened(port=None, silent_turns=False):
    """A driver with turn-1 in progress and its engine id, trn-1, known; ``rid`` is our turn/start's id. With
    ``silent_turns`` the turn/start is not answered yet (only turn/started was read)."""

    server = AppServer(silent_turns=silent_turns)
    driver = cx.CodexDriver(Launcher(server), FakeClock(), approvals=port)
    handle = begin(driver, record(), {})
    driver.send(handle, message())
    rid = server.sent[-1]["id"]
    while server.out:
        driver.pump(handle, 0)
    if silent_turns:
        feed(driver, handle, {"method": "turn/started", "params": {"threadId": "thr-1", "turn": turn("trn-1")}})
    return server, driver, handle, rid


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


class PortCallsBackTest(unittest.TestCase):
    """DeepSeek F1: a port that calls back into the driver from inside its call is refused, never wedged."""

    def test_a_port_that_closes_the_session_from_inside_its_call_is_refused_and_returns(self):
        refused = []

        class Port(FakeApprovals):
            def request(inner, effect, binding):
                try:
                    driver.send(handle, {"kind": "close"})
                except DriverStateError as err:
                    refused.append(str(err))
                    raise
                return super().request(effect, binding)
        server, driver, handle, _ = opened(Port())
        reader, errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        reader.join(STUCK)
        self.assertFalse(reader.is_alive(), "the port's close waited for the port")
        self.assertEqual((errors, refused), ([], ["the approvals port must not call back into the driver"]))
        self.assertEqual(answers(server, 77), [{"id": 77, "result": {"decision": "decline"}}])
        driver.send(handle, {"kind": "close"})  # the worker can still be ended
        self.assertTrue(server.stdin_closed)

    def test_a_close_waiting_for_the_port_and_a_port_calling_back_both_return(self):
        entered, go, outcome = threading.Event(), threading.Event(), []

        class Port(FakeApprovals):
            def request(inner, effect, binding):
                entered.set()
                go.wait(STUCK)
                try:
                    driver.send(handle, ANSWER)
                except DriverStateError:
                    outcome.append("refused")
                    raise
                return super().request(effect, binding)
        server, driver, handle, _ = opened(Port())
        reader, reader_errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        self.assertTrue(entered.wait(STUCK))
        closer, closer_errors = in_thread(lambda: driver.send(handle, {"kind": "close"}))
        closer.join(0.05)
        try:
            self.assertTrue(closer.is_alive(), "close returns only after the port call in progress")
        finally:
            go.set()
            reader.join(STUCK)
            closer.join(STUCK)
        self.assertFalse(reader.is_alive() or closer.is_alive(), "the close and the port's call wait for each other")
        self.assertEqual((reader_errors, closer_errors, outcome), ([], [], ["refused"]))
        self.assertTrue(server.stdin_closed)

    def test_an_interrupt_from_inside_the_port_is_refused_and_nothing_is_written(self):
        outcome = []

        class Port(FakeApprovals):
            def request(inner, effect, binding):
                try:
                    driver.interrupt(handle, "turn-1")
                except DriverStateError:
                    outcome.append("refused")
                return super().request(effect, binding)
        server, driver, handle, _ = opened(Port())
        reader, errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        reader.join(STUCK)
        self.assertFalse(reader.is_alive())
        self.assertEqual((errors, outcome), ([], ["refused"]))
        self.assertNotIn("turn/interrupt", [m.get("method") for m in server.sent])

    def test_an_approval_request_read_from_inside_the_port_is_declined_not_waited_on(self):
        class Port(FakeApprovals):
            def request(inner, effect, binding):
                if not inner.requests:
                    feed(driver, handle, ask_command(rid=78, item="it-2")("trn-1"))
                return super().request(effect, binding)
        port = Port()
        server, driver, handle, _ = opened(port)
        reader, errors = in_thread(lambda: feed(driver, handle, ask_command()("trn-1")))
        reader.join(STUCK)
        self.assertFalse(reader.is_alive(), "a request read inside the port waited for the port")
        self.assertEqual(errors, [])
        self.assertEqual(answers(server, 78), [{"id": 78, "result": {"decision": "decline"}}])
        self.assertEqual(len(port.requests), 1)  # only the outer request was put to the port
        driver.send(handle, ANSWER)
        self.assertEqual(answers(server, 77), [{"id": 77, "result": {"decision": "accept"}}])


class DeferredEndRaceTest(unittest.TestCase):
    """DeepSeek F2: the turn's end and the answer to turn/start read on two threads. The interleaving the race
    needs is made here on one thread: the end is read after the answer took its kept end (none yet) and before it
    adopts the turn. (GPT-6.1 Sol's parallel parse, the other interleaving, is in test_codex_review3_sol.py.)"""

    def test_an_end_read_between_the_answer_and_the_adoption_does_not_bring_the_turn_back(self):
        port = FakeApprovals()
        server, driver, handle, rid = opened(port, silent_turns=True)
        original, ended = driver._adopt_turn, []

        def adopt(session, engine_turn, core_turn):
            if core_turn is not None and not ended:
                ended.append(feed(driver, handle, done()("trn-1")))  # the other reader, at the worst point
            return original(session, engine_turn, core_turn)
        driver._adopt_turn = adopt
        accepted = feed(driver, handle, {"id": rid, "result": {"turn": turn("trn-1")}})
        self.assertEqual([d["type"] for d in accepted], ["input.accepted"])
        self.assertEqual([d["type"] for d in ended[0]], ["usage", "result"])
        session = handle._codex
        self.assertEqual((session.core_turn, session.engine_turn, handle.live_turn), ({}, {}, None))
        feed(driver, handle, ask_command()("trn-1"))
        self.assertEqual(port.requests, [])  # no approval for a turn that is over
        self.assertEqual(answers(server, 77), [{"id": 77, "result": {"decision": "decline"}}])


class RetiredTurnTest(unittest.TestCase):
    """GPT-6 Astra 1 and GPT-6.1 Sol 1: a turn whose end was seen takes no message, also while its acceptance is
    still owed; the refusal reserves nothing at the gate, and holds when the end is read during the gate's step."""

    def test_a_refused_message_for_an_ended_turn_reserves_nothing(self):
        server, driver, handle, rid = opened(silent_turns=True)
        feed(driver, handle, done()("trn-1"))  # the end before the answer: kept for it
        with self.assertRaises(DriverStateError):
            driver.send(handle, message(delivery="dlv-2"))
        drafts = feed(driver, handle, {"id": rid, "result": {"turn": turn("trn-1")}})
        self.assertEqual([d["type"] for d in drafts], ["input.accepted", "usage", "result"])
        driver.send(handle, message(turn_id="turn-2", delivery="dlv-2"))  # the refused id was never reserved
        self.assertEqual(turn_starts(server), ["dlv-1", "dlv-2"])

    def test_an_end_read_while_the_gate_takes_the_message_still_refuses_it(self):
        server, driver, handle, rid = opened(silent_turns=True)
        lock = handle._write_lock

        class EndFirst:  # the turn's end, read on another thread just as the gate takes its write lock
            def __enter__(inner):
                feed(driver, handle, done()("trn-1"))
                return lock.__enter__()

            def __exit__(inner, *exc):
                return lock.__exit__(*exc)
        handle._write_lock = EndFirst()
        try:
            with self.assertRaises(DriverStateError):
                driver.send(handle, message(delivery="dlv-2"))
        finally:
            handle._write_lock = lock
        self.assertEqual(turn_starts(server), ["dlv-1"])


class SettledRequestTest(unittest.TestCase):
    """GPT-6.1 Sol 2: a request the server settled before the port is asked is not put to it, and what the
    server settled goes with its turn."""

    def test_a_request_settled_while_its_effect_is_made_is_not_put_to_the_port(self):
        port = FakeApprovals()
        server, driver, handle, _ = opened(port)
        original = cx._effect

        def settled(*args):
            feed(driver, handle, SETTLED)
            return original(*args)
        with mock.patch.object(cx, "_effect", side_effect=settled):
            feed(driver, handle, ask_command()("trn-1"))
        self.assertEqual((port.requests, answers(server, 77)), ([], []))

    def test_what_the_server_settled_goes_with_its_turn(self):
        server, driver, handle, _ = opened(FakeApprovals())
        feed(driver, handle, SETTLED)
        self.assertEqual(handle._codex.resolved, {"77"})
        feed(driver, handle, done()("trn-1"))
        self.assertEqual(handle._codex.resolved, set())


class StartFailureTest(unittest.TestCase):
    """DeepSeek F3: an OS error of the worker's output is engine_error, and a failure to end the worker of a
    start that is given up never takes the place of the start's own refusal."""

    def test_an_os_error_reading_the_output_during_start_is_engine_error(self):
        class Broken(AppServer):
            def read_line(self, timeout_s):
                raise OSError(9, "Bad file descriptor")
        server = Broken()
        with self.assertRaises(c.RouteError) as cm:
            begin(cx.CodexDriver(Launcher(server), FakeClock()), record(), {})
        self.assertEqual((cm.exception.code, cm.exception.detail), (c.ENGINE_ERROR, "engine io: OSError"))
        self.assertEqual((server.stdin_closed, server.fenced), (True, ["start_refused"]))

    def test_a_worker_that_cannot_be_ended_leaves_the_refusal_in_place_with_a_note(self):
        class Stuck(AppServer):
            def close_stdin(self):
                raise RuntimeError("synthetic: no pipe")

            def fence(self, reason):
                self.fenced.append(reason)
                raise RuntimeError("synthetic: no cgroup")
        server = Stuck(silent=True)
        server.out.append(EOF)
        with self.assertRaises(c.RouteError) as cm:
            begin(cx.CodexDriver(Launcher(server), FakeClock()), record(), {})
        self.assertEqual(cm.exception.code, c.ENGINE_EXITED)
        self.assertEqual(server.fenced, ["start_refused"])  # tried although ending the input failed
        self.assertEqual(cm.exception.__notes__, ["the worker could not be ended: RuntimeError"] * 2)

    def test_an_os_error_reading_the_output_later_is_engine_error(self):
        server, driver, handle, _ = opened()

        def broken(timeout_s):
            raise OSError(5, "Input/output error")
        server.read_line = broken
        with self.assertRaises(c.RouteError) as cm:
            driver.pump(handle, 0)
        self.assertEqual((cm.exception.code, cm.exception.detail), (c.ENGINE_ERROR, "engine io: OSError"))


class RefusalWaitsTest(unittest.TestCase):
    """Round 2's F1 rule on the refusal path, which round 3 gave its own lock section: a refused turn/start retires
    its turn only after an approval answer being written is out, and its approval goes with it."""

    def test_a_refused_turn_waits_for_an_answer_being_written(self):
        server, driver, handle, rid = opened(FakeApprovals(), silent_turns=True)
        feed(driver, handle, ask_command()("trn-1"))  # apr-1 waits on request 77; turn/start is still unanswered
        writing, release = threading.Event(), threading.Event()
        send = server.send

        def slow_send(line):
            if '"decision"' in line:
                writing.set()
                release.wait(STUCK)
            send(line)
        server.send = slow_send
        answer, answer_errors = in_thread(lambda: driver.send(handle, ANSWER))
        self.assertTrue(writing.wait(STUCK))
        refused, refused_errors = in_thread(
            lambda: feed(driver, handle, {"id": rid, "error": {"code": -32000, "message": "synthetic"}}))
        refused.join(0.2)
        try:
            self.assertTrue(refused.is_alive(), "the refusal is not processed while the answer is written")
        finally:
            release.set()
            answer.join(STUCK)
            refused.join(STUCK)
        self.assertEqual((answer_errors, refused_errors), ([], []))
        self.assertEqual(answers(server, 77), [{"id": 77, "result": {"decision": "accept"}}])
        self.assertEqual((handle.live_turn, handle._codex.bindings), (None, {}))


class ParseInputTest(unittest.TestCase):
    """DeepSeek F4: a line that is not text is a malformed call (DriverInputError), refused before it is read."""

    def test_a_line_that_is_not_text_is_driver_input_error(self):
        _, driver, handle, _ = opened()
        for line in (5, None, b"{}", {"method": "turn/started"}):
            with self.subTest(line=line):
                with self.assertRaises(DriverInputError) as cm:
                    driver.parse(handle, line)
                self.assertEqual(cm.exception.field, "line")


class ResumeRefusalTest(unittest.TestCase):
    """GPT-6 Astra 5: only an untyped invalid-request refusal (-32600) of thread/resume is the missing or busy
    thread; a typed one keeps its type whatever its code, and an untyped one with another code is engine_error."""

    def refused(self, error):
        server = AppServer(refuse={"thread/resume": error})
        with self.assertRaises(c.RouteError) as cm:
            begin(cx.CodexDriver(Launcher(server), FakeClock()), record("thr-1"), {})
        return cm.exception.code

    def test_a_typed_refusal_with_the_invalid_request_code_keeps_its_type(self):
        self.assertEqual(self.refused({"code": -32600, "message": "synthetic",
                                       "data": {"codexErrorInfo": "unauthorized"}}), c.AUTH_FAILED)

    def test_an_untyped_server_error_is_engine_error(self):
        self.assertEqual(self.refused({"code": -32000, "message": "synthetic"}), c.ENGINE_ERROR)


class StopDrainTest(unittest.TestCase):
    """GPT-6 Astra 3: /stop and /quit read the worker's output first only while the driver holds a write for the
    turn's id, and then at most HELD_READS lines."""

    def setUp(self):
        self.tool = attended.tool()

    def loop(self, server, commands):
        console = self.tool.Console(lambda _: None)
        driver = cx.CodexDriver(Launcher(server), FakeClock(), approvals=console)
        return self.tool.session_loop(driver, iter(commands), lambda _: None, console, read_s=0.0, env={})

    def test_with_nothing_held_stop_ends_the_input_before_reading_on(self):
        class Counting(attended.AppServer):
            unread_at_close = None

            def close_stdin(self):
                self.unread_at_close = len(self.out)
                super().close_stdin()
                self.emit(EOF)  # a worker ends its output when its input ends
        server = Counting(script={1: [agent("a"), agent("b")]})
        self.loop(server, ["hello", "/stop"])
        self.assertGreater(server.unread_at_close, 0)

    def test_a_held_interrupt_is_waited_for_at_most_held_reads_lines(self):
        class Streaming(attended.AppServer):
            emitted = 0

            def read_line(self, timeout_s):
                if turn_starts(self) and not self.out and not self.stdin_closed:
                    self.emitted += 1
                    if self.emitted > 1000:
                        raise AssertionError("/stop waited for the worker to stop writing")
                    self.emit({"method": "item/agentMessage/delta", "params": {
                        "threadId": "thr-1", "turnId": "trn-1", "itemId": "m-1", "delta": "x"}})
                return super().read_line(timeout_s)

            def close_stdin(self):
                super().close_stdin()
                self.emit(EOF)
        server = Streaming(silent_turns=True)  # the engine never names the turn: the interrupt stays held
        self.loop(server, ["hello", "/interrupt", "/stop"])
        self.assertTrue(server.stdin_closed)
        self.assertLessEqual(server.emitted, self.tool.HELD_READS)
        self.assertNotIn("turn/interrupt", [m.get("method") for m in server.sent])  # dropped at close, as documented

    def test_holding_counts_a_write_being_written_now(self):
        _, driver, handle, _ = opened()
        self.assertFalse(driver.holding(handle))
        handle._codex.flushing.add("turn-1")
        self.assertTrue(driver.holding(handle))


if __name__ == "__main__":
    unittest.main()
