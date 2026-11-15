"""Tests of the Grok one-shot parser (``routes/engines/grok.py``), lane ENGINE-E4, first slice.

Run from the repository root (this folder has no ``__init__.py``, so ``discover`` does not find it):

    PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.grok.test_grok_oneshot -v

Every case under ``oneshot/`` is synthetic (README.md). The driver.v2 schema checks need ``jsonschema``
and SPINE's ``schemas/control/v1`` (merged into this branch from #368); without either they are
skipped by name.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from recursion_core.routes import contract as c
from recursion_core.routes.engines import CATALOGUE, SEEDED, EngineTable
from recursion_core.routes.engines import grok
from recursion_core.routes.engines.fixtures.grok.fake_grok import case_lines

HERE = Path(__file__).resolve().parent
CASES = HERE / "oneshot"
FAKE = HERE / "fake_grok.py"
SCHEMAS = HERE.parents[5] / "schemas" / "control" / "v1"
CONN = "subscription:grok-plan-1"

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
except ImportError:   # the free-threaded venv has no jsonschema (PREAMBLE-v3.3, "Rules")
    Draft202012Validator = None

# event.v1's payload keys of each draft type (driver.v2 $defs/event_draft), every key required.
DRAFT_KEYS = {
    "engine.output": {"stream", "text", "offset_start", "offset_end"},
    "tool.call": {"call_id", "name", "input_digest", "input"},
    "tool.result": {"call_id", "status", "text", "output_ref", "truncated"},
    "usage": {"connection_id", "model", "bucket", "span", "input_tokens", "output_tokens", "reasoning_tokens",
              "cache_read_tokens", "cache_write_tokens", "cost_usd"},
    "limit": {"connection_id", "scope", "kind", "model", "bucket", "reset_at"},
    "result": {"code", "detail"},
    "input.accepted": {"delivery_id", "turn_id", "how"},
}
MEASURED = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")


def load_cases() -> dict[str, dict]:
    cases = {}
    for path in sorted(CASES.glob("*.json")):
        with open(path, encoding="utf-8") as handle:
            cases[path.stem] = json.load(handle)
    return cases


def lines_of(case: dict) -> list[str]:
    return [line.decode("utf-8") for line in case_lines(case)]


def run(lines: list[str], returncode: int, conn: str = CONN) -> tuple[list[dict], grok.OneShotParser]:
    return grok.parse_run(conn, lines, returncode)


def line(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def end(stop: str = "end_turn", **extra) -> str:
    return line({"type": "end", "stopReason": stop, "sessionId": "s-1", "requestId": "r-1", **extra})


def of_type(drafts: list[dict], kind: str) -> list[dict]:
    return [d["payload"] for d in drafts if d["type"] == kind]


def result_of(drafts: list[dict]) -> dict:
    assert drafts[-1]["type"] == "result", drafts[-1]
    return drafts[-1]["payload"]


def assert_shape(test: unittest.TestCase, draft: dict) -> None:
    """What every driver's draft must be (DRIVER-CC's check_draft in #376, written out here)."""

    test.assertEqual(set(draft), {"type", "payload"})
    test.assertIn(draft["type"], DRAFT_KEYS)
    payload = draft["payload"]
    test.assertEqual(set(payload), DRAFT_KEYS[draft["type"]], draft)
    if draft["type"] == "usage":
        for name in MEASURED:
            m = payload[name]
            test.assertEqual(set(m), {"value", "quality", "provenance"})
            if m["quality"] == "unknown":
                test.assertIsNone(m["value"])
                test.assertEqual(m["provenance"], "unavailable")
            else:
                test.assertIn(m["quality"], ("known", "lower_bound"))
                test.assertIn(m["provenance"], ("observed", "modelled", "inferred"))
                test.assertNotIsInstance(m["value"], bool)
                test.assertGreaterEqual(m["value"], 0)
                test.assertTrue(math.isfinite(m["value"]))
                if name != "cost_usd":
                    test.assertIsInstance(m["value"], int)
    if draft["type"] == "result" and payload["code"] is not None:
        test.assertIn(payload["code"], c.CODES)


class CaseReplayTest(unittest.TestCase):
    """Each synthetic case replays to the drafts, the answer and the code it states."""

    def test_every_case_replays_as_it_says(self):
        cases = load_cases()
        self.assertGreaterEqual(len(cases), 12)
        for name, case in cases.items():
            with self.subTest(case=name):
                self.assertIs(case["synthetic"], True)
                self.assertEqual(case["drive"], "oneshot")
                drafts, parser = run(lines_of(case), case["exit_code"])
                self.assertEqual([d["type"] for d in drafts], case["expect"]["drafts"])
                self.assertEqual(result_of(drafts)["code"], case["expect"]["code"])
                self.assertEqual(parser.text, case["expect"]["text"])
                for draft in drafts:
                    assert_shape(self, draft)

    def test_the_finished_case_keeps_the_session_and_request_ids(self):
        case = load_cases()["end-turn-with-a-tool"]
        _, parser = run(lines_of(case), 0)
        self.assertEqual(parser.engine_session_id, "0b8f6a52-7d0e-4c1b-9a43-2f5e8d6c1a70")
        self.assertEqual(parser.request_id, "req_7")
        self.assertEqual(parser.stop_reason, "end_turn")


@unittest.skipIf(Draft202012Validator is None, "jsonschema is not installed (the free-threaded venv has none)")
@unittest.skipUnless((SCHEMAS / "driver.v2.json").is_file(), "SPINE's schemas/control/v1 is not in this tree")
class SchemaTest(unittest.TestCase):
    """Every draft the parser makes, and the declaration, validate against SPINE's driver.v2 (#368)."""

    @classmethod
    def setUpClass(cls):
        resources = []
        for path in SCHEMAS.glob("*.json"):
            with open(path, encoding="utf-8") as handle:
                schema = json.load(handle)
            # Keyed by file name, which is what the schemas' $ref values name (a data file such as the
            # method table has no $id).
            resources.append((path.name, Resource.from_contents(schema, default_specification=DRAFT202012)))
        registry = Registry().with_resources(resources)
        cls.draft = Draft202012Validator({"$ref": "driver.v2.json#/$defs/event_draft"}, registry=registry)
        cls.root = Draft202012Validator({"$ref": "driver.v2.json"}, registry=registry)

    def test_the_declaration_is_a_driver_v2_record(self):
        self.root.validate(grok.DECLARATION)

    def test_every_draft_of_every_case_is_a_driver_v2_event_draft(self):
        for name, case in load_cases().items():
            drafts, _ = run(lines_of(case), case["exit_code"])
            for i, draft in enumerate(drafts):
                with self.subTest(case=name, draft=i):
                    self.draft.validate(draft)

    def test_the_validator_is_not_vacuous(self):
        drafts, _ = run(lines_of(load_cases()["end-turn-with-a-tool"]), 0)
        usage = copy.deepcopy(of_type(drafts, "usage")[0])
        usage["input_tokens"] = {"value": None, "quality": "known", "provenance": "observed"}
        self.assertFalse(self.draft.is_valid({"type": "usage", "payload": usage}))
        self.assertFalse(self.draft.is_valid({"type": "turn.ended", "payload": {}}))


class NoTypedLimitTest(unittest.TestCase):
    """This drive has no typed limit: no text, however it reads, trips a connection."""

    def test_the_declaration_says_no_typed_limits_and_one_turn(self):
        flags = grok.DECLARATION["flags"]
        self.assertIs(flags["typed_limits"], False)
        self.assertIs(flags["usage"], True)
        self.assertEqual([k for k, v in flags.items() if v], ["usage"])
        self.assertIs(grok.DECLARATION["input_ack"], False)
        self.assertEqual(grok.DECLARATION["engine"], "grok")

    def test_no_case_drafts_a_limit_or_ends_plan_limit(self):
        for name, case in load_cases().items():
            drafts, _ = run(lines_of(case), case["exit_code"])
            with self.subTest(case=name):
                self.assertEqual(of_type(drafts, "limit"), [])
                self.assertNotEqual(result_of(drafts)["code"], c.PLAN_LIMIT)

    def test_rate_limit_text_with_exit_1_is_a_short_fault_under_the_shared_rule(self):
        for text in ("You've hit the rate limit for your plan.", "Rate limited: your team's API rate limit",
                     "Too many requests", "quota exhausted"):
            with self.subTest(text=text):
                drafts, _ = run([line({"type": "error", "message": text})], 1)
                self.assertEqual(result_of(drafts)["code"], c.SHORT_FAULT)
                self.assertEqual(of_type(drafts, "limit"), [])

    def test_an_error_that_does_not_sound_like_a_quota_is_an_engine_error(self):
        drafts, _ = run([line({"type": "error", "message": "Couldn't start session: no such model"})], 1)
        self.assertEqual(result_of(drafts), {"code": c.ENGINE_ERROR, "detail": "Couldn't start session: no such model"})

    def test_rate_limit_words_in_the_answer_are_never_read(self):
        drafts, _ = run([line({"type": "text", "data": "You've hit the rate limit for your plan."}), end()], 0)
        self.assertEqual(result_of(drafts)["code"], None)


class OutcomeTest(unittest.TestCase):
    """How a run ends: the module docstring's table, first match wins."""

    def test_exit_zero_is_not_success_without_an_end_turn(self):
        self.assertEqual(result_of(run([line({"type": "text", "data": "x"})], 0)[0])["code"], c.ENGINE_EXITED)
        for stop in ("max_tokens", "refusal", "cancelled", "something_new"):
            with self.subTest(stop=stop):
                payload = result_of(run([end(stop)], 0)[0])
                self.assertEqual(payload, {"code": c.ENGINE_ERROR, "detail": f"stop_reason {stop}"})
        self.assertEqual(result_of(run([line({"type": "end", "sessionId": "s"})], 0)[0])["code"], c.ENGINE_ERROR)

    def test_end_turn_with_exit_zero_is_success(self):
        self.assertEqual(result_of(run([end()], 0)[0]), {"code": None, "detail": "stop_reason end_turn"})

    def test_end_turn_with_a_failing_exit_is_an_engine_error(self):
        # A failed memory flush exits non-zero after end, with no error line (headless.rs:1441-1460).
        self.assertEqual(result_of(run([end()], 1)[0]), {"code": c.ENGINE_ERROR, "detail": "exit 1 after end_turn"})

    def test_max_turns_marker_makes_the_end_an_engine_error(self):
        payload = result_of(run([line({"type": "max_turns_reached"}), end("end_turn")], 0)[0])
        self.assertEqual(payload["code"], c.ENGINE_ERROR)
        self.assertIn("max turns", payload["detail"])

    def test_a_signal_ends_the_run_engine_exited_whatever_came_before(self):
        for code, signal in ((143, 15), (130, 2), (-9, 9), (-15, 15)):
            with self.subTest(returncode=code):
                payload = result_of(run([end(), line({"type": "error", "message": "rate limit"})], code)[0])
                self.assertEqual(payload, {"code": c.ENGINE_EXITED, "detail": f"ended by signal {signal} (exit {code})"})

    def test_a_protocol_fault_wins_over_everything(self):
        payload = result_of(run(["not json\n", line({"type": "error", "message": "rate limit"})], 143)[0])
        self.assertEqual(payload, {"code": c.PROTOCOL_ERROR, "detail": "stdout line is not JSON"})

    def test_lines_that_are_not_one_typed_json_object_are_protocol_faults(self):
        for bad in ("[1,2]\n", '"text"\n', '{"data":"no type"}\n', '{"type":""}\n', '{"type":7}\n',
                    '{"type":"text","data":NaN}\n', '{"type":"usage","usage":{"input_tokens":Infinity}}\n',
                    "[" * 100000 + "\n"):
            with self.subTest(line=bad[:40]):
                drafts, parser = run([bad, end()], 0)
                self.assertEqual(result_of(drafts)["code"], c.PROTOCOL_ERROR)
                self.assertEqual(drafts[0]["payload"]["text"], bad)   # the raw line is kept
                self.assertEqual(parser.ignored["protocol_fault"], 1)

    def test_a_second_end_is_a_protocol_fault(self):
        drafts, _ = run([end(), end()], 0)
        self.assertEqual(result_of(drafts), {"code": c.PROTOCOL_ERROR, "detail": "a second end line"})

    def test_exit_1_without_an_error_line_is_an_engine_error_and_other_exits_engine_exited(self):
        self.assertEqual(result_of(run([], 1)[0]), {"code": c.ENGINE_ERROR, "detail": "exit 1 without an error line"})
        self.assertEqual(result_of(run([], 3)[0]), {"code": c.ENGINE_EXITED, "detail": "exit 3 without an end line"})

    def test_the_first_error_line_is_the_detail_and_a_long_one_is_cut(self):
        drafts, parser = run([line({"type": "error", "message": "x" * 5000}),
                              line({"type": "error", "message": "second"})], 1)
        self.assertEqual(result_of(drafts)["detail"], "x" * grok.MAX_DETAIL_CHARS)
        self.assertEqual(parser.ignored["second_error_line"], 1)

    def test_an_error_line_without_a_message_still_ends_the_run_engine_error(self):
        self.assertEqual(result_of(run([line({"type": "error"})], 1)[0])["code"], c.ENGINE_ERROR)

    def test_unknown_types_and_blank_lines_are_counted_not_faults(self):
        drafts, parser = run([line({"type": "auto_compact_started", "percentage": 80}), "\n",
                              line({"type": "plan", "entries": []}), end()], 0)
        self.assertIsNone(result_of(drafts)["code"])
        self.assertEqual(parser.ignored, {"type:auto_compact_started": 1, "blank_line": 1, "type:plan": 1})

    def test_text_after_end_is_not_the_answer(self):
        _, parser = run([line({"type": "text", "data": "A"}), end(), line({"type": "text", "data": "B"})], 0)
        self.assertEqual(parser.text, "A")
        self.assertEqual(parser.ignored["text_after_end"], 1)

    def test_parse_after_finish_and_finish_twice_are_refused(self):
        parser = grok.OneShotParser(CONN)
        parser.finish(0)
        with self.assertRaises(RuntimeError):
            parser.parse(end())
        with self.assertRaises(RuntimeError):
            parser.finish(0)
        with self.assertRaises(TypeError):
            grok.OneShotParser(CONN).finish(True)


class OffsetTest(unittest.TestCase):
    """engine.output names each line's byte span in the output file, with nothing lost or repeated."""

    def test_spans_are_contiguous_and_count_utf8_bytes(self):
        lines = [line({"type": "text", "data": "caf\u00e9 \u2603 \U0001f600"}), "not json\n", end(), "tail"]
        drafts, _ = run(lines, 0)
        spans = of_type(drafts, "engine.output")
        file_bytes = "".join(lines).encode("utf-8")
        self.assertEqual(spans[0]["offset_start"], 0)
        for before, after in zip(spans, spans[1:]):
            self.assertEqual(before["offset_end"], after["offset_start"])
        self.assertEqual(spans[-1]["offset_end"], len(file_bytes))
        for span in spans:
            self.assertEqual(file_bytes[span["offset_start"]:span["offset_end"]].decode("utf-8"), span["text"])
            self.assertEqual(span["stream"], "stdout")

    def test_a_reader_that_restarts_continues_from_its_offset(self):
        parser = grok.OneShotParser(CONN, start_offset=4096)
        (draft,) = parser.parse(line({"type": "thought", "data": "x"}))
        self.assertEqual(draft["payload"]["offset_start"], 4096)
        self.assertEqual(draft["payload"]["offset_end"], 4096 + len(line({"type": "thought", "data": "x"})))

    def test_a_byte_kept_by_surrogateescape_counts_as_one_byte(self):
        raw = b'{"type":"text","data":"\xff"}\n'
        text = raw.decode("utf-8", "surrogateescape")
        (draft, *_) = grok.OneShotParser(CONN).parse(text)
        self.assertEqual(draft["payload"]["offset_end"], len(raw))

    def test_bad_constructor_arguments_are_refused(self):
        for conn in ("grok", "subscription:", "other:x", "subscription:a\nb", "api_key:" + "x" * 300, None):
            with self.subTest(conn=conn), self.assertRaises(ValueError):
                grok.OneShotParser(conn)
        for start in (-1, True, 1.5):
            with self.subTest(start=start), self.assertRaises(ValueError):
                grok.OneShotParser(CONN, start_offset=start)


class UsageTest(unittest.TestCase):
    """Usage is never 0 because it was not known, and each count says how it is known."""

    @staticmethod
    def value_map(payload: dict) -> dict:
        return {name: (payload[name]["value"], payload[name]["quality"]) for name in MEASURED}

    def test_a_response_usage_line_is_one_call_with_its_buckets_mapped(self):
        drafts, _ = run([line({"type": "usage", "messageId": "m", "usage": {
            "input_tokens": 7, "output_tokens": 5, "reasoning_tokens": 2,
            "cache_read_input_tokens": 100, "cache_creation_input_tokens": 3}})], 0)
        (usage,) = of_type(drafts, "usage")
        self.assertEqual((usage["span"], usage["model"], usage["bucket"], usage["connection_id"]),
                         ("call", None, None, CONN))
        self.assertEqual(self.value_map(usage), {
            "input_tokens": (7, "known"), "output_tokens": (5, "known"), "reasoning_tokens": (2, "known"),
            "cache_read_tokens": (100, "known"), "cache_write_tokens": (3, "known"), "cost_usd": (None, "unknown")})

    def test_an_all_zero_response_usage_is_unknown_not_free(self):
        zeros = {k: 0 for k in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_input_tokens",
                                "cache_creation_input_tokens")}
        (usage,) = of_type(run([line({"type": "usage", "usage": zeros})], 0)[0], "usage")
        self.assertTrue(all(q == "unknown" for _, q in self.value_map(usage).values()))
        (usage,) = of_type(run([line({"type": "usage", "usage": dict(zeros, output_tokens=1)})], 0)[0], "usage")
        self.assertEqual(usage["input_tokens"], {"value": 0, "quality": "known", "provenance": "observed"})

    def test_counts_that_are_not_counts_are_unknown(self):
        (usage,) = of_type(run([line({"type": "usage", "usage": {
            "input_tokens": -1, "output_tokens": True, "reasoning_tokens": 1.5, "cache_read_input_tokens": "9",
            "cache_creation_input_tokens": 4}})], 0)[0], "usage")
        self.assertEqual([usage[n]["quality"] for n in MEASURED],
                         ["unknown", "unknown", "unknown", "unknown", "known", "unknown"])

    def test_a_usage_line_without_usage_drafts_nothing(self):
        drafts, parser = run([line({"type": "usage", "messageId": "m"})], 0)
        self.assertEqual(of_type(drafts, "usage"), [])
        self.assertEqual(parser.ignored["usage_line_without_usage"], 1)

    def test_turn_totals_with_a_complete_cost_take_the_ticks(self):
        drafts, _ = run([end(usage={"input_tokens": 1, "output_tokens": 2, "reasoning_tokens": 0,
                                    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                             modelUsage={"grok-4.6": {}}, total_cost_usd=9.0, total_cost_usd_ticks=12300000)], 0)
        (usage,) = of_type(drafts, "usage")
        self.assertEqual((usage["span"], usage["model"]), ("turn", "grok-4.6"))
        self.assertEqual(usage["cost_usd"], {"value": 0.00123, "quality": "known", "provenance": "observed"})

    def test_the_float_cost_is_used_only_without_ticks(self):
        totals = {"input_tokens": 1, "output_tokens": 1}
        (usage,) = of_type(run([end(usage=totals, total_cost_usd=0.5)], 0)[0], "usage")
        self.assertEqual(usage["cost_usd"]["value"], 0.5)
        for bad in (-1, True, "1"):
            with self.subTest(cost=bad):
                (usage,) = of_type(run([end(usage=totals, total_cost_usd=bad)], 0)[0], "usage")
                self.assertEqual(usage["cost_usd"]["quality"], "unknown")

    def test_incomplete_totals_are_a_lower_bound_and_withhold_the_cost(self):
        totals = {"input_tokens": 10, "output_tokens": 3, "reasoning_tokens": 1, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}
        (usage,) = of_type(run([end(usage=totals, usage_is_incomplete=True, total_cost_usd_ticks=5)], 0)[0], "usage")
        self.assertEqual(usage["input_tokens"], {"value": 10, "quality": "lower_bound", "provenance": "observed"})
        self.assertEqual(usage["cost_usd"]["quality"], "unknown")

    def test_a_partial_cost_is_withheld_but_the_tokens_are_known(self):
        totals = {"input_tokens": 10, "output_tokens": 3}
        (usage,) = of_type(run([end(usage=totals, cost_is_partial=True, total_cost_usd_ticks=5)], 0)[0], "usage")
        self.assertEqual(usage["input_tokens"]["quality"], "known")
        self.assertEqual(usage["cost_usd"]["quality"], "unknown")

    def test_incomplete_with_no_tokens_is_all_unknown_and_no_totals_is_no_draft(self):
        (usage,) = of_type(run([end(usage_is_incomplete=True)], 0)[0], "usage")
        self.assertTrue(all(usage[n]["quality"] == "unknown" for n in MEASURED))
        self.assertEqual(of_type(run([end()], 0)[0], "usage"), [])

    def test_the_turn_names_a_model_only_when_exactly_one_real_one_is_named(self):
        totals = {"input_tokens": 1}
        for model_usage, expected in (({"grok-4.6": {}}, "grok-4.6"), ({"a": {}, "b": {}}, None),
                                      ({"unknown": {}}, None), ({}, None), ([], None), ({"a\nb": {}}, None)):
            with self.subTest(model_usage=model_usage):
                (usage,) = of_type(run([end(usage=totals, modelUsage=model_usage)], 0)[0], "usage")
                self.assertEqual(usage["model"], expected)

    def test_spend_on_an_error_line_is_the_turn(self):
        drafts, _ = run([line({"type": "error", "message": "boom", "usage": {"input_tokens": 4}})], 1)
        (usage,) = of_type(drafts, "usage")
        self.assertEqual((usage["span"], usage["input_tokens"]["value"]), ("turn", 4))


class ToolTest(unittest.TestCase):
    """tool.call carries the RFC 8785 digest of the input; tool.result comes from a terminal status."""

    def call(self, **fields) -> str:
        base = {"type": "tool_call", "toolCallId": "call_1", "title": "Read", "kind": "read",
                "status": "in_progress", "toolName": "read_file", "rawInput": {"path": "src/main.rs"},
                "content": [], "locations": []}
        base.update(fields)
        return line(base)

    @staticmethod
    def update(status: str, call_id: str = "call_1", **fields) -> str:
        return line({"type": "tool_call_update", "toolCallId": call_id, "status": status, "content": [],
                     "rawOutput": None, "locations": [], **fields})

    def test_the_digest_is_sha256_of_the_rfc8785_text(self):
        (call,) = of_type(run([self.call()], 0)[0], "tool.call")
        self.assertEqual(call, {"call_id": "call_1", "name": "read_file", "input": {"path": "src/main.rs"},
                                "input_digest": hashlib.sha256(b'{"path":"src/main.rs"}').hexdigest()})
        (call,) = of_type(run([self.call(rawInput={"b": 1.0, "a": "\u00e9", "c": [True, None]})], 0)[0], "tool.call")
        self.assertEqual(call["input_digest"],
                         hashlib.sha256('{"a":"\u00e9","b":1,"c":[true,null]}'.encode("utf-8")).hexdigest())

    def test_an_input_with_no_rfc8785_form_drafts_no_call(self):
        drafts, parser = run([self.call(rawInput={"size": 2 ** 60}), self.update("completed")], 0)
        self.assertEqual(of_type(drafts, "tool.call"), [])
        self.assertEqual(of_type(drafts, "tool.result"), [])
        self.assertEqual(parser.ignored["tool_input_without_rfc8785_form"], 1)
        self.assertEqual(parser.ignored["tool_result_without_call"], 1)

    def test_a_large_or_non_object_input_is_not_carried_but_is_digested(self):
        big = {"text": "x" * (grok.MAX_TOOL_INPUT_CHARS + 1)}
        (call,) = of_type(run([self.call(rawInput=big)], 0)[0], "tool.call")
        self.assertIsNone(call["input"])
        self.assertEqual(call["input_digest"], hashlib.sha256(json.dumps(big, separators=(",", ":")).encode()).hexdigest())
        for raw in ("ls -la", [1, 2], None):
            with self.subTest(raw=raw):
                (call,) = of_type(run([self.call(rawInput=raw)], 0)[0], "tool.call")
                self.assertIsNone(call["input"])
                self.assertEqual(call["input_digest"],
                                 hashlib.sha256(json.dumps(raw, separators=(",", ":")).encode()).hexdigest())

    def test_the_name_is_the_tool_name_else_the_title(self):
        (call,) = of_type(run([self.call(toolName="")], 0)[0], "tool.call")
        self.assertEqual(call["name"], "Read")
        drafts, parser = run([self.call(toolName="", title="")], 0)
        self.assertEqual(of_type(drafts, "tool.call"), [])
        self.assertEqual(parser.ignored["tool_call_without_name"], 1)

    def test_a_call_without_a_usable_id_is_not_drafted(self):
        for bad in (None, "", "a\nb", "x" * 129, 7):
            with self.subTest(call_id=bad):
                drafts, parser = run([self.call(toolCallId=bad)], 0)
                self.assertEqual(of_type(drafts, "tool.call"), [])
                self.assertEqual(parser.ignored["tool_call_without_id"], 1)

    def test_a_result_comes_once_from_a_terminal_status(self):
        drafts, parser = run([self.call(), self.update("pending"), self.update("in_progress"),
                              self.update("completed", rawOutput={"lines": 1}), self.update("failed")], 0)
        self.assertEqual(of_type(drafts, "tool.result"), [
            {"call_id": "call_1", "status": "ok", "text": '{"lines":1}', "output_ref": None, "truncated": False}])
        self.assertEqual(parser.ignored["tool_result_without_call"], 1)

    def test_failed_is_error_never_denied(self):
        (res,) = of_type(run([self.call(), self.update("failed")], 0)[0], "tool.result")
        self.assertEqual(res["status"], "error")

    def test_a_repeated_call_id_is_not_drafted_twice(self):
        drafts, parser = run([self.call(), self.call()], 0)
        self.assertEqual(len(of_type(drafts, "tool.call")), 1)
        self.assertEqual(parser.ignored["tool_call_repeated"], 1)

    def test_result_text_prefers_text_blocks_then_raw_output_and_is_cut(self):
        blocks = [{"type": "content", "content": {"type": "text", "text": "one"}},
                  {"type": "diff", "path": "a", "oldText": "x", "newText": "y"},
                  {"type": "content", "content": {"type": "text", "text": "two"}}]
        (res,) = of_type(run([self.call(), self.update("completed", content=blocks, rawOutput={"n": 1})], 0)[0],
                         "tool.result")
        self.assertEqual((res["text"], res["truncated"]), ("one\ntwo", False))
        (res,) = of_type(run([self.call(), self.update("completed", rawOutput="plain")], 0)[0], "tool.result")
        self.assertEqual(res["text"], "plain")
        (res,) = of_type(run([self.call(), self.update("completed")], 0)[0], "tool.result")
        self.assertEqual(res["text"], "")
        long = "y" * (grok.MAX_TOOL_RESULT_CHARS + 5)
        (res,) = of_type(run([self.call(), self.update("completed", rawOutput=long)], 0)[0], "tool.result")
        self.assertEqual((len(res["text"]), res["truncated"]), (grok.MAX_TOOL_RESULT_CHARS, True))


class ArgvTest(unittest.TestCase):
    """argv[0] is the binary option when given, else the stated default; the prompt is never an argument."""

    def test_argv0_is_the_default_executable_or_the_binary_option_and_nothing_else(self):
        self.assertEqual(grok.DEFAULT_EXECUTABLE, "grok")
        self.assertEqual(grok.oneshot_argv(prompt_file="/run/p.txt", model="grok-4.6"),
                         ["grok", "--prompt-file", "/run/p.txt", "--output-format", "streaming-json",
                          "--model", "grok-4.6"])
        argv = grok.oneshot_argv(prompt_file="/run/p.txt", model=None, binary="/opt/grok/bin/grok")
        self.assertEqual(argv, ["/opt/grok/bin/grok", "--prompt-file", "/run/p.txt", "--output-format",
                                "streaming-json"])

    def test_bad_arguments_are_refused(self):
        for kwargs, code in (({"prompt_file": "", "model": None}, c.REQUEST_INVALID),
                             ({"prompt_file": "/p\x00", "model": None}, c.REQUEST_INVALID),
                             ({"prompt_file": "/p", "model": "-x"}, c.REQUEST_INVALID),
                             ({"prompt_file": "/p", "model": ""}, c.REQUEST_INVALID),
                             ({"prompt_file": "/p", "model": None, "binary": ""}, c.LAUNCH_REFUSED),
                             ({"prompt_file": "/p", "model": None, "binary": "/g\x00"}, c.LAUNCH_REFUSED)):
            with self.subTest(kwargs=kwargs), self.assertRaises(c.RouteError) as caught:
                grok.oneshot_argv(**kwargs)
            self.assertEqual(caught.exception.code, code)


class FakeGrokTest(unittest.TestCase):
    """The fake binary, launched as a real process with the driver's arguments, replays each case."""

    def launch(self, case_name: str, argv_tail: list[str]) -> subprocess.CompletedProcess:
        env = {"FAKE_GROK_CASE": str(CASES / f"{case_name}.json")}
        if "SYSTEMROOT" in os.environ:   # a Windows interpreter does not start without it
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        return subprocess.run([sys.executable, "-I", str(FAKE), *argv_tail], capture_output=True, env=env)

    def test_each_case_through_the_fake_gives_the_drafts_of_the_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt = os.path.join(tmp, "prompt.txt")
            with open(prompt, "w", encoding="utf-8") as handle:
                handle.write("What is the entry point?")
            argv = grok.oneshot_argv(prompt_file=prompt, model="grok-4.6", binary="/unused/grok")
            for name, case in load_cases().items():
                with self.subTest(case=name):
                    done = self.launch(name, argv[1:])
                    self.assertEqual(done.returncode, case["exit_code"], done.stderr)
                    got, _ = run(done.stdout.decode("utf-8").splitlines(keepends=True), done.returncode)
                    want, _ = run(lines_of(case), case["exit_code"])
                    self.assertEqual(got, want)

    def test_the_fake_refuses_what_the_driver_would_never_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt = os.path.join(tmp, "prompt.txt")
            with open(prompt, "w", encoding="utf-8") as handle:
                handle.write("hi")
            for tail in (["-p", "hi", "--output-format", "streaming-json"],
                         ["--prompt-file", prompt, "--output-format", "json"],
                         ["--prompt-file", prompt],
                         ["--prompt-file", os.path.join(tmp, "missing"), "--output-format", "streaming-json"]):
                with self.subTest(tail=tail):
                    done = self.launch("end-turn-with-a-tool", tail)
                    self.assertEqual(done.returncode, 2)
                    self.assertEqual(done.stdout, b"")


class NothingRegisteredTest(unittest.TestCase):
    """This slice registers nothing: grok is a catalogue name with no factory in any composition."""

    def test_grok_is_catalogued_and_has_no_factory(self):
        self.assertIn("grok", CATALOGUE)
        self.assertNotIn("grok", SEEDED)
        self.assertNotIn("grok", EngineTable())

    def test_a_full_composition_has_no_grok_factory(self):
        from recursion_core.control.hooks import compose

        hooks = compose()
        self.assertNotIn("grok", hooks.engines.names())


if __name__ == "__main__":
    unittest.main()
