"""The second reads of #376 at 41ce013a (Codex GPT-6 Astra and GPT-6.1 Sol, Nov 14, 2026), as lane tests.

Both reads found the first answer's checks working and asked for tests that a single removal of each newly
added check would turn red: Astra's ``SixMissingCases`` (changes 2 to 7) and Sol's
``test_review2_boundaries.py`` (change 2, four tests over fifteen removals), adapted to this lane's fixtures
and to the PROVISIONAL start form. Stand-ins only: no engine, process, provider or network.
"""

from __future__ import annotations

import unittest

from recursion_core.routes import contract as c
from recursion_core.routes import driver as d
from recursion_core.routes.limits import validate_limits
from recursion_core.routes.tests.fakes import request
from recursion_core.routes.tests.test_driver import ALL, StandIn, draft_line, session
from recursion_core.routes.tests.test_engine_check import StandInTable

LIMIT = {"connection_id": "subscription:a", "scope": "connection", "kind": "plan_limit", "model": None,
         "bucket": None, "reset_at": None}
RESULT = {"call_id": "a", "status": "ok", "text": "x", "output_ref": None, "truncated": False}
CALL = {"call_id": "a", "name": "Tool", "input_digest": "a" * 64, "input": None}


def worktree(**changes):
    value = {"path": "/srv/work", "mode": "worktree", "base_ref": "main", "worktree_path": "/srv/wt"}
    value.update(changes)
    return value


def parse(type_, payload):
    driver = StandIn(ALL)
    return driver.parse(driver.start(session(), None, {}), draft_line(type_, payload))


class StartBoundaryTest(unittest.TestCase):
    """Astra change 2; Sol change 2 (R2 boundaries)."""

    def test_workspace_bindings_are_nonempty_strings(self):
        for field in ("base_ref", "worktree_path"):
            for value in ("", [], 7, True):
                driver = StandIn(ALL)
                with self.subTest(field=field, value=value), self.assertRaises(d.DriverInputError):
                    driver.start(session(), worktree(**{field: value}), {})
                self.assertEqual(driver.started, [])

    def test_route_and_connection_boundaries(self):
        good = session()["route"]
        for route in (dict(good, connection=dict(good["connection"], id="a\rb")),
                      dict(good, connection=["kind", "id"]), dict(good, model=[])):
            driver = StandIn(ALL)
            with self.subTest(route=route), self.assertRaises(d.DriverInputError):
                driver.start(dict(session(), route=route), worktree(), {})
            self.assertEqual(driver.started, [])


class DraftBoundaryTest(unittest.TestCase):
    """Astra changes 3 to 6; Sol change 2 (R3 boundaries)."""

    def test_tool_result_call_id_has_shape(self):
        for value in ("", "x" * 129, "a\rb", "a\nb", 7):
            with self.subTest(value=value), self.assertRaises(d.DriverInputError):
                parse("tool.result", dict(RESULT, call_id=value))

    def test_tool_result_text_is_a_string_when_present(self):
        for value in ([], {}, 0, False, 7):
            with self.subTest(value=value), self.assertRaises(d.DriverInputError):
                parse("tool.result", dict(RESULT, text=value))

    def test_limit_model_has_its_shape_apart_from_the_scope_relation(self):
        for value in ("", 7, [], False):
            with self.subTest(value=value), self.assertRaises(d.DriverInputError):
                parse("limit", dict(LIMIT, scope="model", model=value))

    def test_limit_bucket_is_null_or_a_nonempty_string(self):
        for value in ("", 7, [], False):
            with self.subTest(value=value), self.assertRaises(d.DriverInputError):
                parse("limit", dict(LIMIT, bucket=value))

    def test_scalar_bounds(self):
        cases = [("tool.call", dict(CALL, name="a" * 257)), ("tool.call", dict(CALL, call_id="a" * 129)),
                 ("tool.call", dict(CALL, input_digest="a")), ("tool.call", dict(CALL, input_digest="a" * 65)),
                 ("tool.result", dict(RESULT, text=None, output_ref="a" * 129)),
                 ("result", {"code": "a" * 65, "detail": ""}), ("result", {"code": "1error", "detail": ""}),
                 ("limit", dict(LIMIT, connection_id="subscription:a\rb")),
                 ("limit", dict(LIMIT, reset_at="2026-11-14T12:34:56.1Z")),
                 ("limit", dict(LIMIT, reset_at="2024-02-29T23:59:60.000Z"))]
        for type_, payload in cases:
            with self.subTest(type=type_, payload=payload), self.assertRaises(d.DriverInputError):
                parse(type_, payload)

    def test_the_largest_values_inside_the_bounds_are_accepted(self):
        cases = [("tool.call", dict(CALL, name="a" * 256, call_id="a" * 128)),
                 ("tool.result", dict(RESULT, text=None, output_ref="a" * 128)),
                 ("result", {"code": "a" * 64, "detail": ""}),
                 ("limit", dict(LIMIT, reset_at="2024-02-29T23:59:59.999Z"))]
        for type_, payload in cases:
            with self.subTest(type=type_):
                self.assertEqual(parse(type_, payload)[0]["payload"], payload)


class IterableBoundaryTest(unittest.TestCase):
    """Astra change 7; Sol change 2 (Astra R6 boundaries)."""

    def test_validate_limits_reuses_a_generator_across_fallbacks(self):
        routes = [{"engine": "recursion_loop", "model": "m", "connection": {"kind": "subscription", "id": name}}
                  for name in ("a", "b")]
        value = validate_limits({"on_limit": "fallback", "fallback": routes}, engines=(n for n in ["recursion_loop"]))
        self.assertEqual(len(value["fallback"]), 2)

    def test_a_table_is_asked_again_at_each_call(self):
        table = StandInTable()
        table.register("grok")
        self.assertEqual(c.validate_request(request(engine="grok"), engines=table)["route"]["engine"], "grok")

        class Dynamic:
            def __init__(self):
                self.current = ["grok"]

            def names(self):
                return self.current

        dynamic = Dynamic()
        c.validate_request(request(engine="grok"), engines=dynamic)
        dynamic.current = []
        with self.assertRaises(c.RouteError) as caught:
            c.validate_request(request(engine="grok"), engines=dynamic)
        self.assertEqual(caught.exception.code, c.ENGINE_UNKNOWN)


class ProvisionalAnswerTest(unittest.TestCase):
    """The Liaison's 5:01 PM ruling: the brief's wording, marked PROVISIONAL (lane questions Q1 and Q7)."""

    def test_a_mid_turn_send_without_its_flag_answers_busy_and_is_a_refusal(self):
        driver = StandIn(d.Flags(multi_turn=True))
        handle = driver.start(session(), None, {})
        driver.send(handle, {"kind": "user_message", "turn_id": "turn-1", "text": "a", "delivery_id": "dlv-1"})
        with self.assertRaises(d.DriverBusy) as caught:
            driver.send(handle, {"kind": "user_message", "turn_id": "turn-1", "text": "b", "delivery_id": "dlv-2"})
        self.assertIsInstance(caught.exception, d.DriverRefused)
        self.assertEqual(caught.exception.as_record(), {"code": "busy", "call": "send", "flag": "mid_turn_input"})
        self.assertEqual(len(driver.written), 1)

    def test_start_takes_the_briefs_three_arguments_and_resume_names_the_conversation(self):
        driver = StandIn(ALL)
        handle = driver.start(session(), worktree(), {"PATH": "/usr/bin"})
        self.assertEqual(handle.as_record(), {"run_id": "run-1", "engine_session_id": None})
        resumed = driver.resume("conv-1", session(run_id="run-2"), None, {})
        self.assertEqual(resumed.as_record(), {"run_id": "run-2", "engine_session_id": "conv-1"})
        # The subclass still receives driver.v2's one start record.
        self.assertEqual([(run, esid) for run, esid, _ in driver.started], [("run-1", None), ("run-2", "conv-1")])


if __name__ == "__main__":
    unittest.main()
