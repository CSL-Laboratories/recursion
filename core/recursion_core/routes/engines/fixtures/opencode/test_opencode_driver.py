"""The OpenCode driver on driver.v2's gate (``routes.driver.Driver``, lane DRIVER-CC's #376) (lane ENGINE-E3, slice 2).

A spy stands in for the launch path: it records every spawn and gives back a plain-text process. Run by name:

    PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_driver
"""

from __future__ import annotations

import inspect
import dis
import signal
import sys
import threading
import unittest
from pathlib import Path

from recursion_core.routes.driver import Declaration, DriverInputError, DriverRefused, DriverStateError
from recursion_core.routes.engines import opencode as oc

HERE = Path(__file__).resolve().parent
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/var/lib/recursion/homes/opencode", "OPENAI_API_KEY": "sk-made-up"}


class Process:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closes = 0
        self.close_attempts = 0
        self.send_error: BaseException | None = None   # raised by the next send, nothing written
        self.close_errors: list[BaseException] = []    # raised by the next closes, in order, nothing closed

    def send(self, text: str) -> None:
        assert self.closes == 0, "send after close_stdin"
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(text)

    def close_stdin(self) -> None:
        self.close_attempts += 1
        if self.close_errors:
            raise self.close_errors.pop(0)
        self.closes += 1


class Spy:
    def __init__(self) -> None:
        self.spawns: list[tuple[list, dict]] = []
        self.processes: list[Process] = []

    def __call__(self, argv: list, env: dict) -> Process:
        self.spawns.append((list(argv), dict(env)))
        self.processes.append(Process())
        return self.processes[-1]


def session(model="openai/gpt-5.5") -> dict:
    """The ``session`` argument of #376's PROVISIONAL ``start(session, workspace, env)`` (its lane questions Q7)."""
    return {"session_id": "ses-e3test", "run_id": "run-e3test",
            "route": {"engine": "opencode", "model": model, "connection": {"kind": "api_key", "id": "k1"}}}


def message(turn="turn-1", delivery="dlv-1", text="Run the tests.") -> dict:
    return {"kind": "user_message", "turn_id": turn, "text": text, "delivery_id": delivery}


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.spy = Spy()
        self.driver = oc.OpenCodeDriver(self.spy, wall=lambda: 1794614400.0, clock=lambda: 100.0)

    def replay(self, handle, name: str) -> list[dict]:
        drafts = []
        for line in (HERE / name).read_text("utf-8").splitlines(keepends=True):
            drafts.extend(self.driver.parse(handle, line))
        return drafts

    def test_the_declaration_is_driver_v2s_record(self):
        self.assertEqual(self.driver.declaration.as_record(), oc.declaration())
        self.assertEqual(Declaration.from_record(oc.declaration()), self.driver.declaration)

    def test_a_one_shot_turn_from_start_to_result(self):
        handle = self.driver.start(session(), None, ENV)
        self.assertEqual(self.spy.spawns, [(["opencode", "run", "--format", "json", "--model", "openai/gpt-5.5"], ENV)])
        self.driver.send(handle, message())
        process = self.spy.processes[0]
        self.assertEqual((process.sent, process.closes), (["Run the tests."], 1))
        self.assertEqual(handle.live_turn, "turn-1")
        drafts = self.replay(handle, "synthetic-v1-tool-use.jsonl")
        kinds = [d["type"] for d in drafts]
        self.assertEqual(kinds.count("input.accepted"), 1)          # on the first step_start, once
        self.assertEqual(drafts[1], {"type": "input.accepted",
                                     "payload": {"delivery_id": "dlv-1", "turn_id": "turn-1", "how": "new_turn"}})
        self.assertEqual(handle.ignored_acceptances, 0)
        self.assertEqual(handle.engine_session_id, "ses_synthv1tools000000000001")
        self.assertEqual((kinds.count("tool.call"), kinds.count("usage")), (2, 2))
        (result,) = self.driver.finish(handle, 0)
        self.assertEqual(result, {"type": "result", "payload": {"code": None, "detail": ""}})
        self.assertIsNone(handle.live_turn)

    def test_a_typed_429_comes_through_the_gate_as_a_limit_and_the_result(self):
        handle = self.driver.start(session(), None, ENV)
        self.driver.send(handle, message())
        (limit,) = [d["payload"] for d in self.replay(handle, "synthetic-v1-limit-429.jsonl") if d["type"] == "limit"]
        self.assertEqual((limit["connection_id"], limit["reset_at"]), ("api_key:k1", "2026-11-14T00:02:00.000Z"))
        self.assertEqual(self.driver.finish(handle, 1)[0]["payload"]["code"], "plan_limit")

    def test_every_verb_the_drive_lacks_refuses_with_no_process_started_and_nothing_written(self):
        with self.assertRaises(DriverRefused) as caught:
            self.driver.resume("ses_abc", session(), None, ENV)
        self.assertEqual(caught.exception.as_record(), {"code": "unsupported", "call": "resume", "flag": "resume"})
        with self.assertRaises(DriverRefused):
            self.driver.fork("ses_abc", "turn-1")
        self.assertEqual(self.spy.spawns, [])                                              # nothing started
        handle = self.driver.start(session(), None, ENV)
        refused = []
        for call in (lambda: self.driver.send(handle, {"kind": "approval_answer", "approval_id": "apr-1",
                                                       "decision": "approve"}),
                     lambda: self.driver.interrupt(handle, "turn-1")):
            with self.assertRaises(DriverRefused) as caught:
                call()
            refused.append(caught.exception.flag)
        self.driver.send(handle, message())
        for item in (message(delivery="dlv-2"), message(turn="turn-2", delivery="dlv-3")):
            with self.assertRaises(DriverRefused) as caught:                              # mid-turn, then a second turn
                self.driver.send(handle, item)
            refused.append(caught.exception.flag)
        self.driver.finish(handle, 0)
        with self.assertRaises(DriverRefused) as caught:
            self.driver.send(handle, message(turn="turn-2", delivery="dlv-4"))
        refused.append(caught.exception.flag)
        # A message while turn-1 is live is mid-turn input, whatever turn it names; after the end, a second turn.
        self.assertEqual(refused, ["permission_bridge", "interrupt", "mid_turn_input", "mid_turn_input", "multi_turn"])
        self.assertEqual(len(self.spy.spawns), 1)
        self.assertEqual((self.spy.processes[0].sent, self.spy.processes[0].closes), (["Run the tests."], 1))

    def test_a_close_before_any_message_ends_the_input_once(self):
        handle = self.driver.start(session(), None, ENV)
        self.driver.send(handle, {"kind": "close"})
        self.assertEqual((self.spy.processes[0].sent, self.spy.processes[0].closes), ([], 1))
        self.assertEqual(self.driver.finish(handle, None)[0]["payload"]["code"], "engine_exited")

    def test_no_acceptance_without_a_step_start_and_none_before_the_message(self):
        handle = self.driver.start(session(), None, ENV)
        early = self.replay(handle, "synthetic-v1-success.jsonl")       # stdout before the prompt was written
        self.assertNotIn("input.accepted", [d["type"] for d in early])
        handle = self.driver.start(session(), None, ENV)
        self.driver.send(handle, message())
        drafts = self.replay(handle, "synthetic-v1-auth-error.jsonl")  # an error line, no step
        self.assertNotIn("input.accepted", [d["type"] for d in drafts])

    def test_the_engine_default_model_names_no_model(self):
        self.driver.start(session(model=None), None, ENV)
        self.assertEqual(self.spy.spawns[0][0], ["opencode", "run", "--format", "json"])
        driver = oc.OpenCodeDriver(self.spy, binary="/opt/oc/bin/opencode")
        driver.start(session(), None, ENV)
        self.assertEqual(self.spy.spawns[1][0][0], "/opt/oc/bin/opencode")


class ParserStateTests(unittest.TestCase):
    """The two fields this slice adds to ``Parser`` are put back with the rest when a line is interrupted."""

    def test_an_interrupted_line_leaves_the_step_reason_and_the_last_type_as_they_were(self):
        p = oc.Parser(connection_id="api_key:k1", model=None, wall=lambda: 1794614400.0, clock=lambda: 100.0)
        p.feed(b'{"type":"step_start","sessionID":"ses_x","part":{"type":"step-start"}}\n')
        finish = (b'{"type":"step_finish","sessionID":"ses_x","part":{"type":"step-finish","reason":"stop",'
                  b'"tokens":{"input":1}}}\n')
        lines, first = inspect.getsourcelines(oc.Parser.feed)
        target = first + next(i for i, text in enumerate(lines) if text.strip() == "return drafts")

        def tracer(frame, event, arg):
            if frame.f_code is oc.Parser.feed.__code__:
                if event == "line" and frame.f_lineno == target:
                    raise KeyboardInterrupt
                return tracer
            return None

        sys.settrace(tracer)
        try:
            with self.assertRaises(KeyboardInterrupt):
                p.feed(finish)                                   # interrupted on its return
        finally:
            sys.settrace(None)
        self.assertEqual((p._finish, p.last_type), (None, "step_start"))
        p.feed(finish)
        self.assertEqual((p._finish, p.last_type), ("stop", "step_finish"))
        self.assertEqual(p.end(None)[0]["payload"]["code"], None)  # the stream's own end of the turn


class DriverLifecycleTests(unittest.TestCase):
    """F6, F7 and F8 of #414's first read, and the two survivors of slice 2's mutation pass in this class (#15, #27)."""

    def setUp(self):
        self.spy = Spy()
        self.driver = oc.OpenCodeDriver(self.spy, wall=lambda: 1794614400.0, clock=lambda: 100.0)
        self.handle = self.driver.start(session(), None, ENV)

    def test_a_bad_exit_code_changes_nothing_and_finish_can_be_called_again(self):
        self.driver.send(self.handle, message())
        for bad in (True, 1.0, "0", [0]):
            with self.subTest(bad=bad):
                with self.assertRaises(DriverInputError):
                    self.driver.finish(self.handle, bad)
                self.assertEqual(self.handle.live_turn, "turn-1")
        self.assertEqual(self.driver.finish(self.handle, 0)[0]["payload"]["code"], None)
        self.assertIsNone(self.handle.live_turn)

    def test_a_finished_or_unknown_handle_is_a_typed_state_error(self):
        self.driver.finish(self.handle, 0)
        with self.assertRaises(DriverStateError):
            self.driver.finish(self.handle, 0)
        with self.assertRaises(DriverStateError):
            self.driver.parse(self.handle, "x\n")
        with self.assertRaises(DriverStateError):
            self.driver.send(self.handle, {"kind": "close"})
        other = type(self.handle)("run-x", None)
        with self.assertRaises(DriverStateError):
            self.driver.parse(other, "x\n")

    def test_release_forgets_a_handle_whose_finish_never_comes(self):
        self.driver.release(self.handle)
        self.driver.release(self.handle)
        self.assertEqual(self.driver._workers, {})
        with self.assertRaises(DriverStateError):
            self.driver.finish(self.handle, 0)

    def test_the_constructor_refuses_what_it_cannot_use(self):
        for args, kwargs in (((None,), {}), ((self.spy,), {"binary": "-x"}), ((self.spy,), {"binary": " "}),
                             ((self.spy,), {"wall": 5}), ((self.spy,), {"clock": None})):
            with self.subTest(args=args, kwargs=kwargs):
                with self.assertRaises(ValueError):
                    oc.OpenCodeDriver(*args, **kwargs)

    def test_a_close_after_the_message_ends_the_input_once(self):
        self.driver.send(self.handle, message())
        self.driver.send(self.handle, {"kind": "close"})
        self.assertEqual((self.spy.processes[0].sent, self.spy.processes[0].closes), (["Run the tests."], 1))

    def test_a_line_that_is_not_an_event_after_an_early_step_start_accepts_nothing(self):
        early = '{"type":"step_start","timestamp":1,"sessionID":"ses_x","part":{"type":"step-start"}}\n'
        self.driver.parse(self.handle, early)
        self.driver.send(self.handle, message())                # the step_start above came before the prompt
        drafts = self.driver.parse(self.handle, "not json\n")
        self.assertNotIn("input.accepted", [d["type"] for d in drafts])


START = '{"type":"step_start","timestamp":1,"sessionID":"ses_a","part":{"type":"step-start"}}\n'
TEXT = '{"type":"text","timestamp":1,"sessionID":"ses_a","part":{"type":"text","text":"hi"}}\n'
FINISH_STOP = ('{"type":"step_finish","sessionID":"ses_a","part":{"type":"step-finish","reason":"stop",'
               '"tokens":{"input":1}}}\n')


class RoundTwoDriverTests(unittest.TestCase):
    """#414 round 2: astra R3 and R5, sol R1 to R3, DeepSeek LOW-2 and NIT-3."""

    def setUp(self):
        self.spy = Spy()
        self.driver = oc.OpenCodeDriver(self.spy, wall=lambda: 1794614400.0, clock=lambda: 100.0)
        self.handle = self.driver.start(session(), None, ENV)

    def accepted(self, drafts):
        return [d for d in drafts if d["type"] == "input.accepted"]

    # astra R3: dispatch state only after the I/O succeeded
    def test_a_prompt_write_that_raised_is_never_accepted(self):
        self.spy.processes[0].send_error = BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            self.driver.send(self.handle, message())
        self.assertEqual(self.accepted(self.driver.parse(self.handle, START)), [])
        self.assertEqual(self.handle.ignored_acceptances, 0)
        self.assertEqual(self.spy.processes[0].sent, [])

    def test_a_close_that_raised_is_tried_again(self):
        process = self.spy.processes[0]
        process.close_errors = [OSError("busy")]
        with self.assertRaises(OSError):
            self.driver.send(self.handle, message())
        self.driver.send(self.handle, {"kind": "close"})
        self.assertEqual((process.close_attempts, process.closes, process.sent), (2, 1, ["Run the tests."]))

    # sol R2: only a well-formed step_start of the run's own session accepts the prompt
    def test_a_malformed_step_start_does_not_accept_the_prompt(self):
        self.driver.send(self.handle, message())
        self.driver.parse(self.handle, TEXT)
        for line in ('{"type":"step_start","timestamp":1,"sessionID":"ses_a","part":"x"}\n',
                     '{"type":"step_start","timestamp":1,"sessionID":"ses_a","part":{"type":"text"}}\n',
                     '{"type":"step_start","timestamp":1,"sessionID":"ses_a"}\n',
                     '{"type":"step_start","timestamp":1,"part":{"type":"step-start"}}\n'):
            with self.subTest(line=line):
                self.assertEqual(self.accepted(self.driver.parse(self.handle, line)), [])
        self.assertEqual(len(self.accepted(self.driver.parse(self.handle, START))), 1)

    def test_a_step_start_of_another_session_does_not_accept_the_prompt(self):
        self.driver.send(self.handle, message())
        self.driver.parse(self.handle, TEXT)                      # the run's session is ses_a
        other = START.replace("ses_a", "ses_b")
        self.assertEqual(self.accepted(self.driver.parse(self.handle, other)), [])
        self.assertEqual(len(self.accepted(self.driver.parse(self.handle, START))), 1)

    # astra R4 / sol R1: a later step invalidates an earlier stop
    def test_an_unfinished_later_step_is_not_a_finished_run(self):
        self.driver.send(self.handle, message())
        for line in (START, FINISH_STOP, START):
            self.driver.parse(self.handle, line)
        self.assertEqual(self.driver.finish(self.handle, None)[0]["payload"]["code"], "engine_exited")

    def test_a_later_step_that_stops_again_is_a_finished_run(self):
        self.driver.send(self.handle, message())
        for line in (START, FINISH_STOP, START, FINISH_STOP):
            self.driver.parse(self.handle, line)
        self.assertEqual(self.driver.finish(self.handle, None)[0]["payload"]["code"], None)

    # astra R5 / sol R3 / DeepSeek LOW-2: one terminal transition per handle
    def test_two_finishers_give_one_result_and_one_state_error(self):
        self.driver.send(self.handle, message())
        parser = self.driver._worker(self.handle)["parser"]
        real_end, inside, go = parser.end, threading.Event(), threading.Event()

        def slow_end(exit_code):
            inside.set()
            go.wait()
            return real_end(exit_code)

        parser.end = slow_end
        results = []

        def finisher():
            try:
                results.append(("ok", self.driver.finish(self.handle, 0)[0]["payload"]["code"]))
            except Exception as err:   # noqa: BLE001 (the kind is the answer)
                results.append((type(err).__name__, None))

        first = threading.Thread(target=finisher)
        first.start()
        inside.wait()
        second = threading.Thread(target=finisher)
        second.start()
        second.join(0.2)              # time only to see that the second has not finished yet
        go.set()
        first.join()
        second.join()
        self.assertEqual(sorted(results, key=str), [("DriverStateError", None), ("ok", None)])
        self.assertIsNone(self.handle.live_turn)

    def test_an_interrupted_finish_completes_when_called_again(self):
        for where in ("turn_ended", "check_draft"):
            with self.subTest(where=where):
                self.setUp()
                self.driver.send(self.handle, message())
                fired = []
                if where == "turn_ended":
                    real = self.driver.turn_ended

                    def once(handle, turn_id, fired=fired, real=real):
                        if not fired:
                            fired.append(1)
                            raise KeyboardInterrupt
                        return real(handle, turn_id)

                    self.driver.turn_ended = once
                    target = None
                else:
                    target = oc.check_draft

                    def once_draft(draft, fired=fired, target=target):
                        if not fired:
                            fired.append(1)
                            raise KeyboardInterrupt
                        return target(draft)

                    oc.check_draft = once_draft
                try:
                    with self.assertRaises(KeyboardInterrupt):
                        self.driver.finish(self.handle, 0)
                    self.assertEqual(self.handle.live_turn, "turn-1")     # a live turn keeps its worker
                    self.assertEqual(self.driver.finish(self.handle, 1)[0]["payload"]["code"], None)  # first code
                finally:
                    if target is not None:
                        oc.check_draft = target
                self.assertIsNone(self.handle.live_turn)
                with self.assertRaises(DriverStateError):
                    self.driver.finish(self.handle, 0)

    # DeepSeek NIT-3: release ends the turn in progress
    def test_release_ends_the_turn_in_progress_and_ignores_handles_it_does_not_hold(self):
        self.driver.send(self.handle, message())
        self.driver.release(None)
        self.driver.release(type(self.handle)("run-x", None))
        self.assertEqual(self.handle.live_turn, "turn-1")
        self.driver.release(self.handle)
        self.assertIsNone(self.handle.live_turn)
        with self.assertRaises(DriverStateError):
            self.driver.parse(self.handle, START)


class StartedRollbackTests(unittest.TestCase):
    """A step_start line interrupted on its return leaves ``started`` as it was (feed's rollback)."""

    def test_an_interrupted_step_start_is_not_a_start(self):
        p = oc.Parser(connection_id="api_key:k1", model=None, wall=lambda: 1794614400.0, clock=lambda: 100.0)
        p.feed(TEXT)                                              # the run's session is ses_a
        lines, first = inspect.getsourcelines(oc.Parser.feed)
        target = first + next(i for i, text in enumerate(lines) if text.strip() == "return drafts")

        def tracer(frame, event, arg):
            if frame.f_code is oc.Parser.feed.__code__:
                if event == "line" and frame.f_lineno == target:
                    raise KeyboardInterrupt
                return tracer
            return None

        sys.settrace(tracer)
        try:
            with self.assertRaises(KeyboardInterrupt):
                p.feed(START)
        finally:
            sys.settrace(None)
        self.assertFalse(p.started)
        p.feed(START)
        self.assertTrue(p.started)


FINISH_STOP_B = FINISH_STOP.replace("ses_a", "ses_b")
LIMIT_429 = ('{"type":"error","sessionID":"ses_a","error":{"name":"APIError","data":{"statusCode":429,'
             '"message":"synthetic"}}}\n')


class RoundThreeDriverTests(unittest.TestCase):
    """#414 round 3: astra required 1 to 4, DeepSeek MEDIUM-1 and LOW-1. Every wait here is bounded."""

    setUp, accepted = RoundTwoDriverTests.setUp, RoundTwoDriverTests.accepted

    # astra required 1: a failed explicit close can be asked again
    def test_a_failed_explicit_close_can_be_asked_again(self):
        process = self.spy.processes[0]
        process.close_errors = [OSError("synthetic: not closed")]
        with self.assertRaises(OSError):
            self.driver.send(self.handle, {"kind": "close"})
        self.assertFalse(self.handle.closed)
        self.driver.send(self.handle, {"kind": "close"})
        self.assertEqual((process.close_attempts, process.closes, self.handle.closed), (2, 1, True))
        with self.assertRaises(DriverStateError):
            self.driver.send(self.handle, {"kind": "close"})

    # astra required 2: only a start read by this parse call accepts the prompt
    def test_an_empty_parse_does_not_accept_a_start_read_before_the_prompt(self):
        self.driver.parse(self.handle, START)
        self.driver.send(self.handle, message())
        self.assertEqual(self.accepted(self.driver.parse(self.handle, "")), [])
        self.assertEqual(len(self.accepted(self.driver.parse(self.handle, START))), 1)

    # astra required 3: terminal transitions wait for a line being read, and a later send reserves no turn
    def test_a_finish_waits_for_a_line_being_read_and_its_429_decides(self):
        self.driver.send(self.handle, message())
        parser = self.driver._worker(self.handle)["parser"]
        real, inside, go = parser._typed, threading.Event(), threading.Event()

        def slow(event):
            inside.set()
            go.wait(5)
            return real(event)

        parser._typed = slow
        drafts, errors = [], []

        def run(call):
            try:
                drafts.extend(call())
            except BaseException as err:   # noqa: BLE001 (the kind is the answer)
                errors.append(type(err).__name__)

        reader = threading.Thread(target=run, args=(lambda: self.driver.parse(self.handle, LIMIT_429),), daemon=True)
        reader.start()
        self.assertTrue(inside.wait(5))
        finisher = threading.Thread(target=run, args=(lambda: self.driver.finish(self.handle, 0),), daemon=True)
        finisher.start()
        finisher.join(0.1)
        early = not finisher.is_alive()
        go.set()
        reader.join(5)
        finisher.join(5)
        self.assertFalse(reader.is_alive() or finisher.is_alive())
        self.assertEqual((errors, early), ([], False))
        self.assertEqual([d["payload"]["code"] for d in drafts if d["type"] == "result"], ["plan_limit"])

    def test_a_release_waits_for_a_finish(self):
        self.driver.send(self.handle, message())
        parser = self.driver._worker(self.handle)["parser"]
        real, inside, go, released = parser.end, threading.Event(), threading.Event(), threading.Event()

        def slow_end(code):
            inside.set()
            go.wait(5)
            return real(code)

        parser.end = slow_end
        finisher = threading.Thread(target=self.driver.finish, args=(self.handle, 0), daemon=True)
        finisher.start()
        self.assertTrue(inside.wait(5))
        releaser = threading.Thread(target=lambda: (self.driver.release(self.handle), released.set()), daemon=True)
        releaser.start()
        early = released.wait(0.1)
        go.set()
        finisher.join(5)
        releaser.join(5)
        self.assertFalse(early)
        self.assertTrue(released.is_set())

    def test_a_send_refused_after_finish_or_release_leaves_no_live_turn(self):
        for terminal in ("finish", "release"):
            with self.subTest(terminal=terminal):
                self.setUp()
                if terminal == "finish":
                    self.driver.finish(self.handle, 0)
                else:
                    self.driver.release(self.handle)
                with self.assertRaises(DriverStateError):
                    self.driver.send(self.handle, message())
                self.assertIsNone(self.handle.live_turn)
                self.assertEqual(self.spy.processes[0].sent, [])

    # astra required 4: one SIGINT at any instruction of the terminal transitions leaves the lock free
    def test_one_sigint_at_any_instruction_leaves_the_driver_lock_free(self):
        old = signal.signal(signal.SIGINT, signal.default_int_handler)
        broken = []
        try:
            for call, code in (("finish", oc.OpenCodeDriver.finish.__code__),
                               ("release", oc.OpenCodeDriver.release.__code__),
                               ("finish", oc.OpenCodeDriver._exclusive.__code__)):
                for inst in dis.get_instructions(code):
                    # NOT_TAKEN (3.14) is the marker control.hooks' OWNERSHIP note names: no signal is delivered there.
                    if inst.opname in ("RESUME", "CACHE", "NOT_TAKEN"):
                        continue
                    self.setUp()
                    self.driver.send(self.handle, message())
                    fired = []

                    def trace(frame, event, arg, code=code, offset=inst.offset, fired=fired):
                        if frame.f_code is code:
                            frame.f_trace_opcodes = True
                            if event == "opcode" and frame.f_lasti == offset and not fired:
                                fired.append(1)
                                signal.raise_signal(signal.SIGINT)
                            return trace
                        return None

                    sys.settrace(trace)
                    try:
                        getattr(self.driver, call)(self.handle, 0) if call == "finish" \
                            else self.driver.release(self.handle)
                    except KeyboardInterrupt:
                        pass
                    finally:
                        sys.settrace(None)
                    if not fired:
                        continue
                    free = []
                    probe = threading.Thread(target=lambda free=free, lock=self.driver._ending: free.append(
                        lock.acquire(timeout=1.0) and (lock.release() or True)))
                    probe.start()
                    probe.join(5)
                    if free != [True]:
                        broken.append((call, code.co_name, inst.offset, inst.opname, "lock held"))
                        continue           # this driver's lock is lost: no retry on it
                    if id(self.handle) in self.driver._workers:     # not complete: asked again, it completes
                        getattr(self.driver, call)(self.handle, 0) if call == "finish" \
                            else self.driver.release(self.handle)
                    if self.handle.live_turn is not None or id(self.handle) in self.driver._workers:
                        broken.append((call, code.co_name, inst.offset, inst.opname, "not terminal"))
        finally:
            sys.settrace(None)
            signal.signal(signal.SIGINT, old)
        self.assertEqual(broken, [])

    # DeepSeek MEDIUM-1: only a step of the run's own session certifies success
    def test_a_step_finish_of_another_session_does_not_certify_success(self):
        nul = FINISH_STOP.replace('"ses_a"', '"ses\\u0000a"')
        for lines in ((TEXT, START, FINISH_STOP_B), (TEXT, START, nul), (TEXT, FINISH_STOP_B)):
            with self.subTest(lines=lines):
                p = oc.Parser(connection_id="api_key:k1", model=None, wall=lambda: 1794614400.0,
                              clock=lambda: 100.0)
                for line in lines:
                    p.feed(line)
                p.end(None)
                self.assertEqual(p.outcome[0], "engine_exited")
        self.driver.send(self.handle, message())
        for line in (TEXT, START, FINISH_STOP_B):
            self.driver.parse(self.handle, line)
        self.assertEqual(self.driver.finish(self.handle, None)[0]["payload"]["code"], "engine_exited")

    def test_the_runs_own_stop_still_certifies_after_a_foreign_one(self):
        p = oc.Parser(connection_id="api_key:k1", model=None, wall=lambda: 1794614400.0, clock=lambda: 100.0)
        for line in (TEXT, START, FINISH_STOP, FINISH_STOP_B):
            p.feed(line)
        p.end(None)
        self.assertIsNone(p.outcome[0])

    # DeepSeek LOW-1: feed refuses a CR inside the line, as OneShot does
    def test_feed_refuses_a_carriage_return_inside_a_line(self):
        p = oc.Parser(connection_id="api_key:k1", model=None, wall=lambda: 1794614400.0, clock=lambda: 100.0)
        for line in ("a\rb\n", "a\rb", "a\r", "a\r\rb\n", "a\r\r\n"):
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    p.feed(line)
        self.assertEqual(p.lines_read, 0)
        p.feed("a\r\n")
        self.assertEqual(p.lines_read, 1)


if __name__ == "__main__":
    unittest.main()
