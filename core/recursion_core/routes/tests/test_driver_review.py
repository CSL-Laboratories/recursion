"""The first reads of #376 (Codex GPT-6 Astra and GPT-6.1 Sol, Nov 14, 2026), as lane tests.

Each class carries the reviewers' failing tests or the tests they asked for, adapted to this lane's fixtures:
write order (R1 in both reads), the nested start record (R2), event.v1 payload values (R3), registered
engines given as a one-shot iterable (Astra's R6), and the guards that single removals left green (Astra's
R5, Sol's R6). The two reads are in the lane's folder as review-1-376-codex-*.md. Stand-ins only: no
engine, process, provider or network.
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from recursion_core.routes import ClaudeCodeAdapter, CodexAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes import driver as d
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, claude_ok, codex_script, request
from recursion_core.routes.tests.test_driver import ALL, StandIn, accepted, draft_line, msg, session
from recursion_core.routes.tests.test_engine_check import EchoAdapter, StandInTable


def unknown():
    return {"value": None, "quality": "unknown", "provenance": "unavailable"}


def usage(**changes):
    payload = {"connection_id": "subscription:login-A", "model": "m", "bucket": None, "span": "turn",
               **{k: unknown() for k in d._TOKEN_FIELDS}, "cost_usd": unknown()}
    payload.update(changes)
    return payload


def workspace(**changes):
    value = {"path": "/srv/work", "mode": "direct", "base_ref": None, "worktree_path": None}
    value.update(changes)
    return value


class WriteOrderTest(unittest.TestCase):
    """R1: a handle's writes reach the engine in the order the gate reserved them, close included.

    The first writer is held inside its write; a second caller starts. The short wait only observes that
    the second write has NOT overtaken the first (the preamble's one allowed use of time in a test).
    """

    def race(self, first_item, second_item, key):
        entered, release, second_written, errors = threading.Event(), threading.Event(), threading.Event(), []

        class Held(StandIn):
            def _send(self, handle, item):
                if item == first_item:
                    entered.set()
                    if not release.wait(30):
                        raise AssertionError("the test never released the first write")
                super()._send(handle, item)
                if item == second_item:
                    second_written.set()

        driver = Held(ALL)
        handle = driver.start(session(), None, {})

        def call(item):
            try:
                driver.send(handle, item)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        first = threading.Thread(target=call, args=(first_item,))
        first.start()
        self.assertTrue(entered.wait(30))
        second = threading.Thread(target=call, args=(second_item,))
        second.start()
        self.assertFalse(second_written.wait(0.2), "the later write overtook the held one")
        release.set()
        first.join(30)
        second.join(30)
        self.assertEqual((errors, first.is_alive(), second.is_alive()), ([], False, False))
        return [json.loads(line)[key] for line in driver.written]

    def test_close_cannot_overtake_a_reserved_user_message(self):
        self.assertEqual(self.race(msg(), {"kind": "close"}, "kind"), ["user_message", "close"])

    def test_a_mid_turn_message_cannot_overtake_the_message_that_began_the_turn(self):
        self.assertEqual(self.race(msg(), msg(delivery="dlv-2"), "delivery_id"), ["dlv-1", "dlv-2"])

    def test_an_interrupt_waits_for_the_write_before_it(self):
        entered, release, done = threading.Event(), threading.Event(), []

        class Held(StandIn):
            def _send(self, handle, item):
                entered.set()
                release.wait(30)
                super()._send(handle, item)

        driver = Held(ALL)
        handle = driver.start(session(), None, {})
        writer = threading.Thread(target=lambda: driver.send(handle, msg()))
        writer.start()
        self.assertTrue(entered.wait(30))
        interrupter = threading.Thread(target=lambda: done.append(driver.interrupt(handle, "turn-1")))
        interrupter.start()
        interrupter.join(0.2)
        self.assertEqual(driver.written, [])  # the interrupt has not gone ahead of the held message
        release.set()
        writer.join(30)
        interrupter.join(30)
        self.assertEqual([line.split()[0] if line.startswith("interrupt") else "message" for line in driver.written],
                         ["message", "interrupt"])


class StartRecordTest(unittest.TestCase):
    """R2: the nested route and workspace are checked (common.v1) before the start hook is asked."""

    def refused(self, sess, ws):
        driver = StandIn(ALL)
        with self.assertRaises(d.DriverInputError):
            driver.start(sess, ws, {})
        self.assertEqual(driver.started, [])

    def test_a_malformed_route_never_reaches_the_start_hook(self):
        good = session()["route"]
        for route in ({}, [], 7, None, {"engine": "claude_code", "model": "m"},
                      dict(good, engine="Invalid-Engine"), dict(good, connection={}),
                      dict(good, connection={"kind": "cloud", "id": "x"}),
                      dict(good, connection={"kind": "subscription", "id": "a\nb"}),
                      dict(good, connection={"kind": "subscription", "id": "x" * 201}),
                      dict(good, model=""), dict(good, unlisted=1)):
            with self.subTest(route=route):
                self.refused(dict(session(), route=route), None)

    def test_a_malformed_workspace_never_reaches_the_start_hook(self):
        for value in ({}, [], 7, "/srv/work", workspace(path=""), workspace(path=42), workspace(mode="shared"),
                      workspace(mode="worktree"), workspace(mode="worktree", base_ref="main"),
                      workspace(worktree_path="/srv/other"), workspace(extra=1)):
            with self.subTest(workspace=value):
                self.refused(session(), value)

    def test_valid_records_start_and_the_hook_gets_copies(self):
        for value in (None, workspace(), workspace(mode="worktree", base_ref="main", worktree_path="/srv/wt")):
            sess = session()
            expected = None if value is None else dict(value)  # a separate copy: the hook may not reach the caller's
            driver = StandIn(ALL)

            def hook(handle, request, env):
                request["route"]["connection"]["id"] = "changed"
                if request["workspace"] is not None:
                    request["workspace"]["path"] = "/changed"

            driver._start = hook
            self.assertEqual(driver.start(sess, value, {}).run_id, "run-1")
            self.assertEqual(sess["route"]["connection"]["id"], "login-A")
            self.assertEqual(value, expected)
        self.assertEqual(StandIn(ALL).start(dict(session(), route=dict(session()["route"], model=None)),
                                            None, {}).run_id, "run-1")  # model null: the engine's own default


class DraftValueTest(unittest.TestCase):
    """R3: a draft's payload values and relations are event.v1's, not only its keys."""

    def parse(self, type_, payload):
        driver = StandIn(ALL)
        return driver.parse(driver.start(session(), None, {}), draft_line(type_, payload))

    def test_invalid_values_are_refused(self):
        tool_result = {"call_id": "c1", "status": "ok", "text": "x", "output_ref": None, "truncated": False}
        limit = {"connection_id": "subscription:login-A", "scope": "connection", "kind": "plan_limit",
                 "model": None, "bucket": None, "reset_at": None}
        call = {"call_id": "c1", "name": "Bash", "input_digest": "a" * 64, "input": {}}
        out = {"stream": "stdout", "text": "x", "offset_start": 0, "offset_end": 1}
        cases = [
            ("engine.output", dict(out, text=[])), ("engine.output", dict(out, stream="stdin")),
            ("engine.output", dict(out, offset_start=-1)), ("engine.output", dict(out, offset_end=True)),
            ("engine.output", dict(out, offset_end=1.0)),
            ("tool.call", dict(call, call_id="")), ("tool.call", dict(call, call_id="x" * 129)),
            ("tool.call", dict(call, name="a\nb")), ("tool.call", dict(call, input_digest="not-a-digest")),
            ("tool.call", dict(call, input_digest="A" * 64)), ("tool.call", dict(call, input=[])),
            ("tool.result", dict(tool_result, output_ref="artifact")), ("tool.result", dict(tool_result, text=None)),
            ("tool.result", dict(tool_result, status="done")), ("tool.result", dict(tool_result, truncated=0)),
            ("tool.result", dict(tool_result, text=None, output_ref="bad ref")),
            ("limit", dict(limit, scope="model")), ("limit", dict(limit, model="opus")),
            ("limit", dict(limit, scope="global")), ("limit", dict(limit, connection_id="subscription:a\nb")),
            ("limit", dict(limit, kind="soft")), ("limit", dict(limit, reset_at=1700000000)),
            ("limit", dict(limit, reset_at="2026-02-31T00:00:00.000Z")),
            ("limit", dict(limit, reset_at="2026-11-14T10:00:00Z")),
            ("result", {"code": 17, "detail": ""}), ("result", {"code": "Engine-Error", "detail": ""}),
            ("result", {"code": None, "detail": []}),
            ("usage", usage(span="whatever")), ("usage", usage(connection_id=17)),
            ("usage", usage(connection_id="login-A")), ("usage", usage(connection_id="api_key:" + "k" * 300)),
            ("usage", usage(model="")), ("usage", usage(bucket=7)),
        ]
        for type_, payload in cases:
            with self.subTest(type=type_, payload=payload), self.assertRaises(d.DriverInputError):
                self.parse(type_, payload)

    def test_valid_values_are_accepted(self):
        cases = [
            ("engine.output", {"stream": "stderr", "text": "", "offset_start": 0, "offset_end": 0}),
            ("tool.call", {"call_id": "c1", "name": "Bash", "input_digest": "0" * 64, "input": None}),
            ("tool.result", {"call_id": "c1", "status": "denied", "text": None, "output_ref": "out:1",
                             "truncated": True}),
            ("limit", {"connection_id": "api_key:k1", "scope": "model", "kind": "spend_cap", "model": "opus",
                       "bucket": "b", "reset_at": "2026-11-14T10:00:00.000Z"}),
            ("result", {"code": "engine_error", "detail": "engine text"}),
            ("usage", usage(bucket="seven_day", span="session_total")),
        ]
        for type_, payload in cases:
            with self.subTest(type=type_):
                self.assertEqual(self.parse(type_, payload)[0]["payload"], payload)

    def test_a_large_integer_count_is_not_turned_into_a_float(self):
        big = usage(input_tokens={"value": 10 ** 400, "quality": "known", "provenance": "observed"})
        self.assertEqual(self.parse("usage", big)[0]["payload"]["input_tokens"]["value"], 10 ** 400)


class RegisteredIterableTest(unittest.TestCase):
    """Astra's R6: a one-shot iterable of registered names is read once and serves every check."""

    def test_an_iterable_survives_service_construction(self):
        clock = FakeClock()
        service = RouteService({"recursion_loop": EchoAdapter()}, ConnectionBreaker(clock), clock,
                               engines=iter(["recursion_loop"]))
        self.assertEqual(service.handle(request(engine="recursion_loop"))["status"], "ok")
        self.assertEqual(service.handle(request(rid="r2", engine="recursion_loop"))["status"], "ok")

    def test_an_iterable_survives_nested_fallback_validation(self):
        fallback = {"engine": "recursion_loop", "model": "m", "connection": {"kind": "subscription", "id": "other"}}
        req = request(engine="recursion_loop", limits={"on_limit": "fallback", "fallback": [fallback]})
        self.assertEqual(c.validate_request(req, engines=iter(["recursion_loop"]))["route"]["engine"], "recursion_loop")

    def test_a_table_is_still_asked_at_each_call(self):
        table = StandInTable()
        self.assertIs(c.registered_snapshot(table), table)
        table.register("grok")
        self.assertIn("grok", c.engine_names(c.registered_snapshot(table)))

    def test_a_registered_engine_runs_as_a_fallback(self):
        class Limited:
            def run(self, req, deadline):
                return c.result(req["request_id"], c.PLAN_LIMIT,
                                limit={"scope": "connection", "model": None, "reset_at": None, "source": "test"})

        clock = FakeClock()
        loop = EchoAdapter()
        service = RouteService({"claude_code": Limited(), "recursion_loop": loop}, ConnectionBreaker(clock), clock)
        route = {"engine": "recursion_loop", "model": "m", "connection": {"kind": "subscription", "id": "other"}}
        res = service.handle(request(limits={"on_limit": "fallback", "fallback": [route]}))
        self.assertEqual((res["status"], res["text"], loop.calls), ("ok", "recursion_loop", ["recursion_loop"]))


class GuardEdgeTest(unittest.TestCase):
    """Astra's R5 and Sol's R6: guards that single removals had left green, now each pinned."""

    def test_input_ack_must_be_a_bool(self):
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaises(d.DriverInputError):
                d.Declaration("claude_code", d.Flags(), value)

    def test_a_declaration_record_takes_no_extra_field(self):
        record = d.one_turn("claude_code").as_record()
        record["extra"] = "unexpected"
        with self.assertRaises(d.DriverInputError):
            d.Declaration.from_record(record)

    def test_fork_gives_back_a_conversation_id_and_takes_valid_arguments(self):
        driver = StandIn(ALL)
        for value in (None, "", 42, "x" * 257):
            driver._fork = lambda *args, value=value: value
            with self.subTest(returned=repr(value)[:20]), self.assertRaises(d.DriverStateError):
                driver.fork("conversation", "turn-1")
        driver = StandIn(ALL)
        for esid, at in ((None, "turn-1"), ("", "turn-1"), (42, "turn-1"), ("x" * 257, "turn-1"),
                         ("conversation", "invalid")):
            with self.subTest(esid=repr(esid)[:20], at=at), self.assertRaises(d.DriverInputError):
                driver.fork(esid, at)
        self.assertEqual(driver.forks, [])

    def test_a_reported_conversation_id_has_its_shape(self):
        handle = StandIn(ALL).start(session(), None, {})
        for value in (None, "", 42, "x" * 257):
            with self.subTest(value=repr(value)[:20]), self.assertRaises(d.DriverInputError):
                handle.set_engine_session_id(value)
        self.assertIsNone(handle.engine_session_id)

    def test_an_acceptance_draft_has_its_shape(self):
        driver = StandIn(ALL)
        handle = driver.start(session(), None, {})
        for line in (accepted(delivery="invalid"), accepted(turn="invalid"), accepted(how="invalid")):
            with self.subTest(line=line), self.assertRaises(d.DriverInputError):
                driver.parse(handle, line)

    def test_measurements_are_finite_and_their_provenance_known(self):
        for value in ({"value": float("inf"), "quality": "known", "provenance": "observed"},
                      {"value": float("-inf"), "quality": "known", "provenance": "observed"},
                      {"value": float("nan"), "quality": "lower_bound", "provenance": "observed"},
                      {"value": 1, "quality": "known", "provenance": "unavailable"},
                      {"value": 1, "quality": "known", "provenance": "invented"}):
            with self.subTest(value=repr(value)), self.assertRaises(d.DriverInputError):
                d.check_measurement(value, integer=False, field="cost_usd")

    def test_environment_names_are_strings(self):
        with self.assertRaises(d.DriverInputError):
            d.check_env({1: "value"})

    def test_an_interrupt_with_a_malformed_turn_id_writes_nothing(self):
        driver = StandIn(ALL)
        handle = driver.start(session(), None, {})
        driver.send(handle, msg())
        before = list(driver.written)
        with self.assertRaises(d.DriverInputError):
            driver.interrupt(handle, "invalid")
        self.assertEqual(driver.written, before)

    def test_the_start_hook_gets_a_copy_of_the_record(self):
        sess = session()
        driver = StandIn(ALL)
        driver._start = lambda handle, request, env: request.update(run_id="mutated")
        driver.start(sess, None, {})
        self.assertEqual(sess["run_id"], "run-1")


class SharedBoundaryTest(unittest.TestCase):
    """Sol's check that the D9 refusal holds for both adapters with the real host check, unpatched."""

    def test_both_adapters_refuse_on_windows_and_launch_nothing(self):
        for name, cls, script in (("claude_code", ClaudeCodeAdapter, claude_ok()),
                                  ("codex", CodexAdapter, codex_script())):
            clock = FakeClock()
            launcher = FakeLauncher([script], clock)
            service = RouteService({name: cls(launcher, clock, lambda *_: "FAKE", {})}, ConnectionBreaker(clock), clock)
            with mock.patch("recursion_core.routes.host.os.name", "nt"):
                res = service.handle(request(engine=name))
            self.assertEqual((res["code"], res["detail"], launcher.calls),
                             (c.LAUNCH_REFUSED, "platform_unsupported", []))


if __name__ == "__main__":
    unittest.main()
