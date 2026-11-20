"""driver.v2's gate (routes/driver.py): flags refuse before any byte, state is checked, acceptance correlates.

A stand-in driver records every byte it would write to its engine, every start and every fork, so each
test can show that a refused call reached none of them. No process, engine or network is used.
"""

from __future__ import annotations

import json
import unittest

from recursion_core.routes import driver as d
from recursion_core.routes.driver import (Declaration, Driver, DriverInputError, DriverRefused, DriverStateError,
                                          Flags, one_turn)

ALL = Flags(**{name: True for name in d.FLAGS})


def session(run_id="run-1"):
    """The brief's ``session`` argument of start and resume (PROVISIONAL form): {session_id, run_id, route}."""
    return {"session_id": "ses-1", "run_id": run_id,
            "route": {"engine": "claude_code", "model": "m", "connection": {"kind": "subscription", "id": "login-A"}}}


def start_record(esid=None, run_id="run-1"):
    """The driver.v2 start record the subclass's ``_start`` receives."""
    return {**session(run_id), "workspace": None, "engine_session_id": esid}


def msg(turn="turn-1", delivery="dlv-1", text="hello"):
    return {"kind": "user_message", "turn_id": turn, "text": text, "delivery_id": delivery}


def accepted(delivery="dlv-1", turn="turn-1", how="new_turn"):
    return json.dumps({"type": "ack", "draft": {"type": "input.accepted",
                                                "payload": {"delivery_id": delivery, "turn_id": turn, "how": how}}})


def end(turn="turn-1"):
    return json.dumps({"type": "end", "turn": turn})


def draft_line(type_, payload):
    return json.dumps({"type": "draft", "draft": {"type": type_, "payload": payload}})


class StandIn(Driver):
    def __init__(self, flags=Flags(), input_ack=False) -> None:
        self.declaration = Declaration("claude_code", flags, input_ack)
        self.started: list[tuple] = []
        self.written: list[str] = []   # every line that would reach the engine's input
        self.forks: list[tuple] = []
        self.fail_next_write = False
        self.lock_free_while_asked: list[bool] = []

    def _free(self, handle) -> None:
        free = handle._lock.acquire(blocking=False)
        if free:
            handle._lock.release()
        self.lock_free_while_asked.append(free)

    def _start(self, handle, request, env):
        self.started.append((handle.run_id, request["engine_session_id"], dict(env)))

    def _send(self, handle, item):
        self._free(handle)
        if self.fail_next_write:
            self.fail_next_write = False
            raise BrokenPipeError("engine went away mid-write")
        self.written.append(json.dumps(item))

    def _interrupt(self, handle, turn_id):
        self._free(handle)
        self.written.append(f"interrupt {turn_id}")

    def _fork(self, engine_session_id, at):
        self.forks.append((engine_session_id, at))
        return f"{engine_session_id}-fork"

    def _parse(self, handle, line):
        m = json.loads(line)
        if m["type"] == "end":
            self.turn_ended(handle, m["turn"])
            return [{"type": "result", "payload": {"code": None, "detail": ""}}]
        return [m["draft"]] if m["type"] in ("ack", "draft") else []


class DeclarationTest(unittest.TestCase):
    def test_the_published_record_has_the_schema_flags_and_driver_two(self) -> None:
        record = Declaration("claude_code", Flags(multi_turn=True), input_ack=True).as_record()
        self.assertEqual(set(record), {"engine", "driver", "flags", "input_ack"})
        self.assertEqual(record["driver"], 2)
        self.assertEqual(tuple(record["flags"]), d.PUBLISHED_FLAGS)
        self.assertEqual(Declaration.from_record(record).as_record(), record)

    def test_mid_turn_input_needs_a_worker_that_keeps_its_input_open(self) -> None:
        with self.assertRaises(DriverInputError) as caught:
            Flags(mid_turn_input=True)
        self.assertEqual(caught.exception.field, "flags.mid_turn_input")
        Flags(multi_turn=True, mid_turn_input=True)

    def test_malformed_records_are_refused(self) -> None:
        good = one_turn("claude_code").as_record()
        cases = {
            "a flag left out": dict(good, flags={k: v for k, v in good["flags"].items() if k != "usage"}),
            "a flag nobody defined": dict(good, flags=dict(good["flags"], steering=False)),
            "contract version one": dict(good, driver=1),
            "version as a float": dict(good, driver=2.0),
            "a flag that is not a bool": dict(good, flags=dict(good["flags"], fork=1)),
            "input_ack missing": {k: v for k, v in good.items() if k != "input_ack"},
            "engine name with capitals": dict(good, engine="Claude"),
            "engine name with a newline": dict(good, engine="claude_code\n"),
        }
        for name, record in cases.items():
            with self.subTest(name), self.assertRaises(DriverInputError):
                Declaration.from_record(record)

    def test_one_turn_is_todays_path_with_every_flag_false(self) -> None:
        declared = one_turn("codex")
        self.assertEqual(declared.as_record(), {"engine": "codex", "driver": 2, "input_ack": False,
                                                "flags": {name: False for name in d.PUBLISHED_FLAGS}})
        self.assertTrue(all(declared.flags.as_dict()[name] is False for name in d.FLAGS))


class FlagRefusalTest(unittest.TestCase):
    """A verb with its flag false is refused, and the stand-in engine sees no byte."""

    def started(self, flags=Flags()):
        driver = StandIn(flags)
        return driver, driver.start(session(), None, {"PATH": "/usr/bin"})

    def assertRefused(self, call, flag, fn, driver, code="unsupported"):
        before = (list(driver.written), list(driver.started), list(driver.forks))
        with self.assertRaises(DriverRefused) as caught:
            fn()
        self.assertEqual(caught.exception.as_record(), {"code": code, "call": call, "flag": flag})
        self.assertEqual((driver.written, driver.started, driver.forks), before)

    def test_resume_without_its_flag_starts_nothing(self) -> None:
        driver = StandIn()
        self.assertRefused("resume", "resume", lambda: driver.resume("conv-1", session(), None, {}), driver)
        # resume is refused for its flag whatever it is given: the flag is checked before the shape.
        self.assertRefused("resume", "resume", lambda: driver.resume(None, session(), None, {}), driver)
        self.assertEqual(driver.started, [])

    def test_a_mid_turn_message_without_its_flag_writes_nothing(self) -> None:
        driver, h = self.started()
        driver.send(h, msg())  # the first message needs no flag
        self.assertEqual(len(driver.written), 1)
        self.assertRefused("send", "mid_turn_input", lambda: driver.send(h, msg(delivery="dlv-2")), driver, "unsupported")
        # A worker with a second turn but no mid-turn input: still refused while the turn is in progress.
        driver, h = self.started(Flags(multi_turn=True))
        driver.send(h, msg())
        self.assertRefused("send", "mid_turn_input", lambda: driver.send(h, msg(delivery="dlv-2")), driver, "unsupported")

    def test_a_second_turn_without_multi_turn_writes_nothing(self) -> None:
        driver, h = self.started()
        driver.send(h, msg())
        driver.parse(h, end())
        self.assertRefused("send", "multi_turn", lambda: driver.send(h, msg("turn-2", "dlv-2")), driver)

    def test_an_approval_answer_without_the_bridge_writes_nothing(self) -> None:
        driver, h = self.started()
        answer = {"kind": "approval_answer", "approval_id": "apr-1", "decision": "deny"}
        self.assertRefused("send", "permission_bridge", lambda: driver.send(h, answer), driver)

    def test_interrupt_and_fork_without_their_flags_reach_nothing(self) -> None:
        driver, h = self.started()
        driver.send(h, msg())
        self.assertRefused("interrupt", "interrupt", lambda: driver.interrupt(h, "turn-1"), driver)
        self.assertRefused("fork", "fork", lambda: driver.fork("conv-1", "turn-1"), driver)

    def test_with_every_flag_true_each_call_goes_through(self) -> None:
        driver, h = self.started(ALL)
        driver.send(h, msg())
        driver.send(h, msg(delivery="dlv-2", text="and also"))  # mid-turn
        driver.send(h, {"kind": "approval_answer", "approval_id": "apr-1", "decision": "approve"})
        driver.interrupt(h, "turn-1")
        driver.parse(h, end())
        driver.send(h, msg("turn-2", "dlv-3"))  # a second turn
        self.assertEqual(driver.fork("conv-1", "turn-1"), "conv-1-fork")
        resumed = driver.resume("conv-1", session(run_id="run-2"), None, {})
        self.assertEqual(resumed.as_record(), {"run_id": "run-2", "engine_session_id": "conv-1"})
        self.assertEqual(len(driver.written), 5)
        self.assertEqual(driver.lock_free_while_asked, [True] * 5)  # the gate holds no lock while it asks

    def test_close_needs_no_flag_and_ends_the_input(self) -> None:
        driver, h = self.started()
        driver.send(h, {"kind": "close"})
        self.assertTrue(h.closed)
        written = list(driver.written)
        with self.assertRaises(DriverStateError):
            driver.send(h, msg())
        self.assertEqual(driver.written, written)


class StateAndShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = StandIn(ALL)
        self.h = self.driver.start(session(), None, {"PATH": "/usr/bin"})

    def assertNothingWritten(self, exc, fn):
        before = list(self.driver.written)
        with self.assertRaises(exc):
            fn()
        self.assertEqual(self.driver.written, before)

    def test_ids_are_never_reused_and_a_mid_turn_message_names_the_turn_in_progress(self) -> None:
        self.driver.send(self.h, msg())
        self.assertNothingWritten(DriverStateError, lambda: self.driver.send(self.h, msg()))  # same delivery
        self.assertNothingWritten(DriverStateError, lambda: self.driver.send(self.h, msg("turn-9", "dlv-2")))
        self.driver.parse(self.h, end())
        self.assertNothingWritten(DriverStateError, lambda: self.driver.send(self.h, msg("turn-1", "dlv-3")))
        self.assertNothingWritten(DriverStateError, lambda: self.driver.interrupt(self.h, "turn-1"))

    def test_malformed_inputs_write_nothing(self) -> None:
        cases = [
            None, {"kind": "steer"}, dict(msg(), extra=1), msg(turn="turn-1\n"), msg(turn="t-1"),
            msg(delivery="delivery-1"), msg(text=""), {"kind": "close", "text": "bye"},
            {"kind": "approval_answer", "approval_id": "apr-1", "decision": "maybe"},
            {"kind": "approval_answer", "approval_id": "approval-1", "decision": "deny"},
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertNothingWritten(DriverInputError, lambda: self.driver.send(self.h, item))
        self.assertEqual(self.h.live_turn, None)

    def test_start_takes_driver_v2s_start_record_and_a_string_environment(self) -> None:
        bad_sessions = [dict(session(), extra=1), dict(session(), session_id="session-1"),
                        dict(session(), run_id="run 1"), {k: v for k, v in session().items() if k != "route"},
                        [], None, dict(start_record())]  # the last: driver.v2's whole record is not a session
        for value in bad_sessions:
            with self.subTest(session=value), self.assertRaises(DriverInputError):
                StandIn(ALL).start(value, None, {})
        with self.assertRaises(DriverInputError):
            StandIn(ALL).resume("", session(), None, {})  # a conversation id has 1 to 256 characters
        for env in ({"PATH": 1}, [("PATH", "/usr/bin")]):
            driver = StandIn(ALL)
            with self.subTest(env=env), self.assertRaises(DriverInputError):
                driver.start(session(), None, env)
            self.assertEqual(driver.started, [])
        with self.assertRaises(DriverInputError):
            StandIn(ALL).resume(None, session(), None, {})  # resume names the conversation it resumes

    def test_state_is_checked_under_the_handles_lock_and_the_subclass_asked_outside_it(self) -> None:
        events = []

        class Recording:  # the gate takes the handle's lock only as a context manager
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                self.inner.acquire()
                events.append("acquire")

            def __exit__(self, *exc):
                events.append("release")
                self.inner.release()

        self.h._lock = Recording(self.h._lock)
        written = self.driver.written

        def write(handle, item):
            events.append("write")
            written.append(json.dumps(item))

        self.driver._send = write
        self.driver.send(self.h, msg())
        self.assertEqual(events, ["acquire", "release", "write"])

    def test_the_conversation_id_is_set_once(self) -> None:
        self.h.set_engine_session_id("conv-1")
        self.h.set_engine_session_id("conv-1")
        with self.assertRaises(DriverStateError):
            self.h.set_engine_session_id("conv-2")
        self.assertEqual(self.h.as_record(), {"run_id": "run-1", "engine_session_id": "conv-1"})


class AcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = StandIn(ALL, input_ack=True)
        self.h = self.driver.start(session(), None, {})

    def kinds(self, line):
        return [x["type"] for x in self.driver.parse(self.h, line)]

    def test_an_acceptance_counts_once_and_only_as_written(self) -> None:
        self.driver.send(self.h, msg())
        self.assertEqual(self.kinds(accepted(how="mid_turn")), [])     # the write began a turn
        self.assertEqual(self.kinds(accepted(turn="turn-2")), [])      # another turn
        self.assertEqual(self.kinds(accepted()), ["input.accepted"])
        self.assertEqual(self.kinds(accepted()), [])                   # accepted already
        self.driver.send(self.h, msg(delivery="dlv-2"))
        self.assertEqual(self.kinds(accepted("dlv-2", how="mid_turn")), ["input.accepted"])
        self.assertEqual(self.h.ignored_acceptances, 3)

    def test_an_acceptance_of_a_delivery_never_written_is_dropped(self) -> None:
        # An id read out of engine output (a forged replay, a tool's text) names no write of this handle.
        self.assertEqual(self.kinds(accepted("dlv-forged")), [])
        self.driver.send(self.h, msg())
        other = self.driver.start(session(run_id="run-2"), None, {})
        self.assertEqual([x["type"] for x in self.driver.parse(other, accepted())], [])  # another worker's write
        self.assertEqual((self.h.ignored_acceptances, other.ignored_acceptances), (1, 1))

    def test_a_delivery_the_engine_never_took_has_no_acceptance(self) -> None:
        self.driver.send(self.h, msg())
        self.assertEqual(self.kinds(end()), ["result"])
        self.assertEqual(self.h._pending, {"dlv-1": ("turn-1", "new_turn")})  # still unproven: outcome unknown

    def test_a_write_that_raised_keeps_its_delivery_unknown(self) -> None:
        self.driver.fail_next_write = True
        with self.assertRaises(BrokenPipeError):
            self.driver.send(self.h, msg())
        self.assertEqual(self.driver.written, [])
        with self.assertRaises(DriverStateError):
            self.driver.send(self.h, msg(turn="turn-2"))  # never resent under the same delivery id
        self.assertEqual(self.kinds(accepted()), ["input.accepted"])  # a later typed acceptance still counts


class DraftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = StandIn(ALL)
        self.h = self.driver.start(session(), None, {})

    def test_a_driver_drafts_only_its_own_types(self) -> None:
        for type_, payload in (("turn.started", {"delivery_id": "dlv-1", "index": 0, "text": "x"}),
                               ("approval.requested", {"approval_id": "apr-1"}),
                               ("turn.ended", {"outcome": "done", "result": None})):
            with self.subTest(type_), self.assertRaises(DriverInputError):
                self.driver.parse(self.h, draft_line(type_, payload))

    def test_a_payload_has_exactly_its_keys(self) -> None:
        self.assertEqual(self.driver.parse(self.h, draft_line("result", {"code": None, "detail": ""}))[0]["type"],
                         "result")
        for payload in ({"code": None}, {"code": None, "detail": "", "text": "extra"}):
            with self.subTest(payload=payload), self.assertRaises(DriverInputError):
                self.driver.parse(self.h, draft_line("result", payload))

    def test_usage_is_never_zero_for_unknown(self) -> None:
        unknown = {"value": None, "quality": "unknown", "provenance": "unavailable"}
        known = {"value": 12, "quality": "known", "provenance": "observed"}
        base = {"connection_id": "subscription:login-A", "model": "m", "bucket": None, "span": "turn",
                "input_tokens": known, "output_tokens": known, "reasoning_tokens": unknown,
                "cache_read_tokens": unknown, "cache_write_tokens": unknown,
                "cost_usd": {"value": 0.01, "quality": "known", "provenance": "modelled"}}
        self.assertEqual(self.driver.parse(self.h, draft_line("usage", base))[0]["payload"], base)
        def m(value, quality, provenance):
            return {"value": value, "quality": quality, "provenance": provenance}

        bad = {
            "unknown as zero": dict(base, reasoning_tokens=m(0, "unknown", "unavailable")),
            "known as null": dict(base, input_tokens=m(None, "known", "observed")),
            "a bool for a count": dict(base, input_tokens=m(True, "known", "observed")),
            "a fractional token count": dict(base, input_tokens=m(1.5, "known", "observed")),
            "unknown but observed": dict(base, cost_usd=m(None, "unknown", "observed")),
            "a negative cost": dict(base, cost_usd=m(-1, "known", "modelled")),
        }
        for name, payload in bad.items():
            with self.subTest(name), self.assertRaises(DriverInputError):
                self.driver.parse(self.h, draft_line("usage", payload))


if __name__ == "__main__":
    unittest.main()
