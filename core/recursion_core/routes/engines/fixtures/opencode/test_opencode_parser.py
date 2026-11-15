"""Tests of the OpenCode parser (``routes/engines/opencode.py``, lane ENGINE-E3), against the SYNTHETIC fixtures.

These tests live in the fixtures folder until the map gives this lane a path under ``routes/engines/tests/``; run
them by module name:

    PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_parser

The shape checks below follow event.v1 (SPINE's ``schemas/control/v1/event.v1.json``, 1.0-draft.7 at #368
5aface43) key for key; they read no schema file and import no schema library.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from datetime import datetime
from pathlib import Path

from recursion_core.control.hooks import Hooks
from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.engines import CATALOGUE, SEEDED
from recursion_core.routes.engines import opencode as oc

HERE = Path(__file__).resolve().parent
INDEX = json.loads((HERE / "index.json").read_text("utf-8"))
ENTRIES = {entry["file"]: entry for entry in INDEX["fixtures"]}
WALL = 1794614400.0   # 2026-11-14T00:00:00Z
CLOCK = 100.0         # the breaker's clock at the moment each line is parsed
CONN = "subscription:chatgpt-plan"
OTHER = "api_key:openai-key-1"
MODEL = "openai/gpt-5.5"

# event.v1: the payload keys of each type a driver may draft (every key required, no other allowed).
KEYS = {
    "engine.output": {"stream", "text", "offset_start", "offset_end"},
    "tool.call": {"call_id", "name", "input_digest", "input"},
    "tool.result": {"call_id", "status", "text", "output_ref", "truncated"},
    "usage": {"connection_id", "model", "bucket", "span", "input_tokens", "output_tokens", "reasoning_tokens",
              "cache_read_tokens", "cache_write_tokens", "cost_usd"},
    "limit": {"connection_id", "scope", "kind", "model", "bucket", "reset_at"},
    "result": {"code", "detail"},
    "input.accepted": {"delivery_id", "turn_id", "how"},
}
_SHA = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_CONNECTION = re.compile(r"(subscription|api_key|passthrough):.+")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
UNKNOWN = {"value": None, "quality": "unknown", "provenance": "unavailable"}


def known(value, provenance="observed"):
    return {"value": value, "quality": "known", "provenance": provenance}


def parser(**kwargs) -> oc.Parser:
    wall, clock = kwargs.pop("wall", WALL), kwargs.pop("clock", CLOCK)
    return oc.Parser(connection_id=kwargs.pop("connection", CONN), model=kwargs.pop("model", MODEL),
                     wall=lambda: wall, clock=lambda: clock, **kwargs)


def replay(name: str, **kwargs) -> tuple[oc.Parser, list[dict]]:
    exit_code = kwargs.pop("exit_code", ENTRIES[name]["exit_code"])
    p = parser(**kwargs)
    drafts: list[dict] = []
    for line in (HERE / name).read_bytes().splitlines(keepends=True):
        drafts.extend(p.feed(line))
    drafts.extend(p.end(exit_code))
    return p, drafts


def line(event: object) -> bytes:
    return (event if isinstance(event, str) else json.dumps(event, separators=(",", ":"))).encode() + b"\n"


def typed(drafts: list[dict], kind: str) -> list[dict]:
    return [d["payload"] for d in drafts if d["type"] == kind]


def v1_error(data: dict) -> dict:
    return {"type": "error", "timestamp": 1, "sessionID": "ses_x", "error": {"name": "APIError", "data": data}}


def step(tokens: object = None, cost: object = None) -> dict:
    part = {"id": "prt_f", "sessionID": "ses_x", "messageID": "msg_x", "type": "step-finish", "reason": "stop"}
    if tokens is not None:
        part["tokens"] = tokens
    if cost is not None:
        part["cost"] = cost
    return {"type": "step_finish", "timestamp": 1, "sessionID": "ses_x", "part": part}


def tool(state: dict, **part) -> dict:
    body = {"id": "prt_u", "sessionID": "ses_x", "messageID": "msg_x", "type": "tool", "callID": "call_1",
            "tool": "bash", "state": state}
    body.update(part)
    return {"type": "tool_use", "timestamp": 1, "sessionID": "ses_x", "part": body}


class Shape:
    """event.v1's payload rules for the drafts, checked key by key."""

    def check(self, draft: dict) -> None:
        self.assertEqual(set(draft), {"type", "payload"})
        kind, p = draft["type"], draft["payload"]
        self.assertIn(kind, KEYS)
        self.assertEqual(set(p), KEYS[kind], kind)
        if kind == "engine.output":
            self.assertEqual(p["stream"], "stdout")
            self.assertIsInstance(p["text"], str)
            for name in ("offset_start", "offset_end"):
                self.assertIs(type(p[name]), int)
                self.assertGreaterEqual(p[name], 0)
            self.assertGreaterEqual(p["offset_end"], p["offset_start"])
        elif kind in ("tool.call", "tool.result"):
            self.assertTrue(1 <= len(p["call_id"]) <= 128 and not re.search(r"[\r\n]", p["call_id"]))
            if kind == "tool.call":
                self.assertTrue(1 <= len(p["name"]) <= 256 and not re.search(r"[\r\n]", p["name"]))
                self.assertRegex(p["input_digest"], _SHA)
                self.assertTrue(p["input"] is None or isinstance(p["input"], dict))
            else:
                self.assertIn(p["status"], ("ok", "error", "denied", "cancelled"))
                self.assertEqual((p["text"] is None), (p["output_ref"] is not None))
                self.assertIs(type(p["truncated"]), bool)
        elif kind == "usage":
            self.assertRegex(p["connection_id"], _CONNECTION)
            self.assertTrue(p["model"] is None or (isinstance(p["model"], str) and p["model"]))
            self.assertIsNone(p["bucket"])
            self.assertIn(p["span"], ("call", "turn", "session_total"))
            for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
                         "cache_write_tokens", "cost_usd"):
                self.measurement(p[name], integer=name != "cost_usd")
        elif kind == "limit":
            self.assertRegex(p["connection_id"], _CONNECTION)
            self.assertIn(p["scope"], ("connection", "model"))
            self.assertIn(p["kind"], ("plan_limit", "short_fault", "spend_cap"))
            self.assertEqual(p["model"] is None, p["scope"] == "connection")
            if p["reset_at"] is not None:
                self.assertRegex(p["reset_at"], _TIMESTAMP)
                datetime.strptime(p["reset_at"], "%Y-%m-%dT%H:%M:%S.%fZ")   # a real calendar instant
        elif kind == "result":
            self.assertTrue(p["code"] is None or _CODE.fullmatch(p["code"]))
            self.assertIsInstance(p["detail"], str)

    def measurement(self, value: dict, *, integer: bool) -> None:
        self.assertEqual(set(value), {"value", "quality", "provenance"})
        if value["quality"] == "unknown":
            self.assertEqual(value, UNKNOWN)
            return
        self.assertIn(value["quality"], ("known", "lower_bound"))
        self.assertIn(value["provenance"], ("observed", "modelled", "inferred"))
        self.assertIsInstance(value["value"], int if integer else (int, float))
        self.assertNotIsInstance(value["value"], bool)
        self.assertGreater(value["value"], 0)   # a 0 from OpenCode is never drafted as known


class FixtureTests(Shape, unittest.TestCase):
    def test_every_fixture_is_listed_and_labelled_synthetic(self):
        on_disk = sorted(p.name for p in HERE.glob("*.jsonl"))
        self.assertEqual(on_disk, sorted(ENTRIES))
        for name, entry in ENTRIES.items():
            self.assertIs(entry["synthetic"], True, name)
            self.assertTrue(name.startswith("synthetic-"), name)
            self.assertIn(entry["package"], INDEX["sources"], name)
        self.assertIn("SYNTHETIC", INDEX["synthetic"])

    def test_each_fixture_gives_the_drafts_it_lists(self):
        for name, entry in ENTRIES.items():
            with self.subTest(name):
                p, drafts = replay(name)
                expect = entry["expect"]
                self.assertEqual([d["type"] for d in drafts], expect["types"])
                self.assertEqual(drafts[-1]["payload"]["code"], expect["code"])
                self.assertEqual(p.engine_session_id, expect["engine_session_id"])
                self.assertEqual(p.text, expect["text"])
                self.assertEqual(len(p.anomalies), expect["anomalies"], p.anomalies)

    def test_every_draft_has_the_event_v1_shape(self):
        for name in ENTRIES:
            with self.subTest(name):
                for draft in replay(name)[1]:
                    self.check(draft)

    def test_the_output_spans_cover_the_file_exactly_once(self):
        for name in ENTRIES:
            with self.subTest(name):
                data = (HERE / name).read_bytes()
                spans = typed(replay(name)[1], "engine.output")
                self.assertEqual("".join(s["text"] for s in spans).encode(), data)
                position = 0
                for span in spans:
                    self.assertEqual(span["offset_start"], position)
                    position = span["offset_end"]
                self.assertEqual(position, len(data))

    def test_the_answer_is_every_text_part_in_order(self):
        self.assertEqual(replay("synthetic-v2-success.jsonl")[0].text, "Hello world.")

    def test_each_index_entry_says_what_it_shows_and_where_it_came_from(self):
        for name, entry in ENTRIES.items():
            self.assertTrue(entry["shows"], name)
            self.assertIn(entry["exit_code"], (0, 1), name)
        for package, sources in INDEX["sources"].items():
            self.assertTrue(all("anomalyco/opencode@" in s for s in sources), package)


class UsageTests(Shape, unittest.TestCase):
    def test_one_step_is_one_call_with_each_count_marked(self):
        (usage,) = typed(replay("synthetic-v1-success.jsonl")[1], "usage")
        self.assertEqual(usage["connection_id"], CONN)
        self.assertEqual(usage["model"], MODEL)
        self.assertIsNone(usage["bucket"])
        self.assertEqual(usage["span"], "call")
        self.assertEqual(usage["input_tokens"], known(1200))
        self.assertEqual(usage["output_tokens"], known(350))       # OpenCode's output 250 plus its reasoning 100
        self.assertEqual(usage["reasoning_tokens"], known(100))
        self.assertEqual(usage["cache_read_tokens"], known(15400))
        self.assertEqual(usage["cache_write_tokens"], UNKNOWN)     # OpenCode writes 0 for "not reported" too
        self.assertEqual(usage["cost_usd"], known(0.0123, "modelled"))

    def test_a_zero_count_or_cost_is_unknown_never_zero(self):
        (usage,) = typed(replay("synthetic-v1-noise.jsonl")[1], "usage")
        for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
                     "cache_write_tokens", "cost_usd"):
            self.assertEqual(usage[name], UNKNOWN, name)

    def test_the_2x_line_counts_reasoning_inside_the_output(self):
        (usage,) = typed(replay("synthetic-v2-tool-use.jsonl")[1], "usage")
        self.assertEqual(usage["output_tokens"], known(24))
        self.assertEqual(usage["reasoning_tokens"], known(6))
        self.assertEqual(usage["cache_read_tokens"], known(1024))
        self.assertEqual(usage["cost_usd"], known(0.0011, "modelled"))

    def test_reasoning_alone_is_a_lower_bound_of_the_output(self):
        p = parser()
        (usage,) = typed(p.feed(line(step({"input": 5, "output": 0, "reasoning": 50,
                                                "cache": {"read": 0, "write": 0}}))), "usage")
        self.assertEqual(usage["output_tokens"], {"value": 50, "quality": "lower_bound", "provenance": "observed"})
        self.check({"type": "usage", "payload": usage})

    def test_counts_that_are_not_whole_positive_exact_numbers_are_unknown(self):
        p = parser()
        tokens = {"input": 12.0, "output": "12", "reasoning": True, "cache": {"read": -5, "write": 2 ** 60}}
        (usage,) = typed(p.feed(line(step(tokens, cost=10 ** 400))), "usage")
        self.assertEqual(usage["input_tokens"], known(12))
        for name in ("output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd"):
            self.assertEqual(usage[name], UNKNOWN, name)

    def test_an_infinite_cost_or_wait_is_unknown(self):
        # A JSON number too large for a float is read as inf by Python's json; it is not a cost or a wait.
        p = parser()
        (usage,) = typed(p.feed(b'{"type":"step_finish","part":{"type":"step-finish","cost":1e400,'
                                b'"tokens":{"input":1}}}\n'), "usage")
        self.assertEqual(usage["cost_usd"], UNKNOWN)
        (limit,) = typed(p.feed(b'{"type":"error","error":{"type":"provider.rate-limit","message":"x",'
                                b'"status":429,"retryAfterMs":1e400}}\n'), "limit")
        self.assertIsNone(limit["reset_at"])
        self.assertEqual(p.anomalies, [])

    def test_a_step_with_no_tokens_is_all_unknown(self):
        for tokens in (None, [1, 2], {"input": 3, "cache": 5}):
            with self.subTest(tokens):
                (usage,) = typed(parser().feed(line(step(tokens))), "usage")
                qualities = {k: usage[k]["quality"] for k in usage if k.endswith("_tokens") or k == "cost_usd"}
                if tokens == {"input": 3, "cache": 5}:
                    self.assertEqual(qualities.pop("input_tokens"), "known")
                self.assertEqual(set(qualities.values()), {"unknown"})

    def test_a_step_part_of_another_shape_gives_no_usage(self):
        event = step({"input": 3})
        event["part"]["type"] = "step-start"
        p = parser()
        self.assertEqual([d["type"] for d in p.feed(line(event))], ["engine.output"])
        self.assertEqual(p.anomalies, ["line 1: step_shape"])

    def test_the_route_result_carries_one_usage_event_per_call(self):
        p, _ = replay("synthetic-v1-tool-use.jsonl")
        events = [e for e in p.route_result("r1")["events"] if e["kind"] == "usage"]
        self.assertEqual([e["data"]["input_tokens"] for e in events], [900, 1000])
        self.assertEqual([e["data"]["cache_read_tokens"] for e in events], [None, 900])


class LimitTests(Shape, unittest.TestCase):
    def limit(self, name: str, **kwargs) -> tuple[dict, dict]:
        p, drafts = replay(name, **kwargs)
        (limit,) = typed(drafts, "limit")
        return limit, p.route_result("r1")

    def test_a_typed_429_is_a_plan_limit_on_the_connection_with_retry_after_seconds(self):
        limit, result = self.limit("synthetic-v1-limit-429.jsonl")
        self.assertEqual(limit, {"connection_id": CONN, "scope": "connection", "kind": "plan_limit", "model": None,
                                 "bucket": None, "reset_at": "2026-11-14T00:02:00.000Z"})
        self.assertEqual(result["code"], c.PLAN_LIMIT)
        self.assertEqual(result["limit"], {"scope": "connection", "model": None, "reset_at": CLOCK + 120,
                                           "source": "opencode.error.data.statusCode"})

    def test_retry_after_ms_wins_over_retry_after(self):
        limit, result = self.limit("synthetic-v1-limit-429-retry-after-ms.jsonl")
        self.assertEqual(limit["reset_at"], "2026-11-14T00:00:01.500Z")
        self.assertEqual(result["limit"]["reset_at"], CLOCK + 1.5)

    def test_retry_after_as_an_http_date(self):
        limit, result = self.limit("synthetic-v1-limit-429-http-date.jsonl")
        self.assertEqual(limit["reset_at"], ENTRIES["synthetic-v1-limit-429-http-date.jsonl"]["expect"][
            "reset_at_with_wall_2026_11_14"])
        self.assertEqual(result["limit"]["reset_at"], CLOCK + 3600)

    def test_a_429_without_a_wait_has_no_reset(self):
        limit, result = self.limit("synthetic-v1-limit-429-no-reset.jsonl")
        self.assertIsNone(limit["reset_at"])
        self.assertIsNone(result["limit"]["reset_at"])
        self.assertEqual(result["detail"], "status 429 without a reset")

    def test_the_2x_line_reads_status_and_retry_after_ms_and_ignores_retries(self):
        p, drafts = replay("synthetic-v2-rate-limit.jsonl")
        self.assertEqual([d["type"] for d in drafts].count("limit"), 1)   # the two retry events carry 429 too
        (limit,) = typed(drafts, "limit")
        self.assertEqual(limit["reset_at"], "2026-11-14T00:00:30.000Z")
        self.assertEqual(p.route_result("r1")["limit"]["source"], "opencode.error.status")

    def test_an_error_without_a_typed_429_is_not_a_limit(self):
        for name, code in [("synthetic-v1-error-no-status.jsonl", c.ENGINE_ERROR),
                           ("synthetic-v1-error-500-quota-text.jsonl", c.SHORT_FAULT),
                           ("synthetic-v1-auth-error.jsonl", c.AUTH_FAILED),
                           ("synthetic-v2-chatgpt-limit-no-status.jsonl", c.SHORT_FAULT),
                           ("synthetic-v2-transport-error.jsonl", c.ENGINE_ERROR),
                           ("synthetic-v2-auth-error.jsonl", c.AUTH_FAILED)]:
            with self.subTest(name):
                p, drafts = replay(name)
                self.assertEqual(typed(drafts, "limit"), [])
                result = p.route_result("r1")
                self.assertEqual(result["code"], code)
                self.assertIsNone(result["limit"])

    def test_the_status_must_be_the_integer_429(self):
        for status in ("429", 429.0, True, 4290, None):
            with self.subTest(status):
                p = parser()
                data = {"message": "Rate limit reached", "isRetryable": True, "responseHeaders": {"retry-after": "9"}}
                if status is not None:
                    data["statusCode"] = status
                self.assertEqual(typed(p.feed(line(v1_error(data))), "limit"), [])
                self.assertNotEqual(p.end(1)[0]["payload"]["code"], c.PLAN_LIMIT)

    def test_waits_are_read_the_way_opencode_reads_them(self):
        cases = [({"retry-after": "30"}, "2026-11-14T00:00:30.000Z"),
                 ({"retry-after": "  1e3"}, "2026-11-14T00:16:40.000Z"),
                 ({"retry-after": "2.5s"}, "2026-11-14T00:00:02.500Z"),     # parseFloat reads the leading number
                 ({"Retry-After": "7"}, "2026-11-14T00:00:07.000Z"),        # any case
                 ({"retry-after-ms": "0", "retry-after": "60"}, None),      # OpenCode takes the 0 ms and stops
                 ({"retry-after-ms": "soon", "retry-after": "60"}, "2026-11-14T00:01:00.000Z"),
                 ({"retry-after": "-5"}, None),
                 ({"retry-after": "Infinity"}, None),
                 ({"retry-after": "not a date"}, None),
                 ({"retry-after": "Fri, 13 Nov 2026 23:00:00 GMT"}, None),  # already past
                 ({"retry-after": "Sat, 14 Nov 2026 00:10:00 -0000"}, "2026-11-14T00:10:00.000Z"),
                 ({"retry-after": "Sat, 14 Nov 2026 01:30:00 +0100"}, "2026-11-14T00:30:00.000Z"),
                 ({"retry-after": "Sat, 14 Nov 2026 00:05:00"}, "2026-11-14T00:05:00.000Z"),   # no zone: UTC
                 ({"retry-after": "14 Nov 2026 00:05:00"}, "2026-11-14T00:00:14.000Z"),   # parseFloat reads 14
                 ({"retry-after": "Fri, 31 Dec 9999 23:59:59 GMT"}, None),  # after the last writable second
                 ({"retry-after": "1e300"}, None),                          # too far off to write
                 ({"retry-after": "1e306"}, None),                          # its milliseconds overflow a float
                 ({"retry-after": "1.7e308"}, None),                        # the reset is infinite
                 ({"retry-after": "1e400"}, None),                          # the wait itself is infinite
                 ({"retry-after-ms": "1e400", "retry-after": "60"}, None),
                 ({"retry-after": 30}, None),                               # a header value is a string
                 ("not headers", None)]
        for headers, reset in cases:
            with self.subTest(headers):
                p = parser()
                data = {"message": "x", "statusCode": 429, "isRetryable": True, "responseHeaders": headers}
                (limit,) = typed(p.feed(line(v1_error(data))), "limit")
                self.assertEqual(limit["reset_at"], reset)
                p.end(1)
                result = p.route_result("r1")["limit"]
                self.assertEqual(result["reset_at"] is None, reset is None)

    def test_a_reset_is_rounded_up_to_the_millisecond(self):
        p = parser(wall=WALL + 0.0001)
        data = {"message": "x", "statusCode": 429, "isRetryable": True, "responseHeaders": {"retry-after": "1"}}
        (limit,) = typed(p.feed(line(v1_error(data))), "limit")
        self.assertEqual(limit["reset_at"], "2026-11-14T00:00:01.001Z")

    def test_a_2x_wait_that_is_not_a_positive_number_is_no_reset(self):
        for wait in (0, -1, "30000", None, float("inf")):
            with self.subTest(wait):
                error = {"type": "provider.rate-limit", "message": "x", "status": 429}
                if wait is not None:
                    error["retryAfterMs"] = wait
                event = {"type": "error", "timestamp": 1, "sessionID": "ses_x", "error": error}
                text = json.dumps(event, separators=(",", ":"), allow_nan=True)
                drafts = parser().feed(line(text))
                if wait == float("inf"):
                    self.assertEqual(typed(drafts, "limit"), [])   # Infinity is not JSON: the line is not read
                else:
                    self.assertIsNone(typed(drafts, "limit")[0]["reset_at"])


class BreakerTests(unittest.TestCase):
    """The route contract's result, booked by today's connection breaker (routes/breaker.py)."""

    def setUp(self):
        self.now = CLOCK
        self.breaker = ConnectionBreaker(lambda: self.now)

    def book(self, name: str, connection: str = CONN) -> str:
        p, _ = replay(name, connection=connection)
        self.assertTrue(self.breaker.admit(connection, MODEL, "t1")["admitted"])
        return self.breaker.record(connection, MODEL, "t1", p.route_result("r1"))

    def test_a_replayed_429_opens_that_connection_only(self):
        for name in ("synthetic-v1-limit-429.jsonl", "synthetic-v2-rate-limit.jsonl"):
            with self.subTest(name):
                self.setUp()
                self.assertEqual(self.book(name), "opened")
                self.assertEqual(self.breaker.state(CONN), "open")
                refused = self.breaker.admit(CONN, MODEL, "t2")
                self.assertEqual(refused["code"], c.BREAKER_OPEN)       # every tenant on that login
                self.assertTrue(self.breaker.admit(OTHER, MODEL, "t1")["admitted"])
                self.assertEqual(self.breaker.state(OTHER), "closed")

    def test_the_breaker_reopens_at_the_reset_the_vendor_asked_for(self):
        self.book("synthetic-v1-limit-429.jsonl")
        self.assertEqual(self.breaker.admit(CONN, MODEL, "t1")["retry_at"], CLOCK + 120)
        self.now = CLOCK + 120
        self.assertTrue(self.breaker.admit(CONN, MODEL, "t1")["probe"])   # one half-open probe after the reset

    def test_a_plain_error_leaves_the_connection_closed(self):
        for name in ("synthetic-v1-error-no-status.jsonl", "synthetic-v2-transport-error.jsonl",
                     "synthetic-v1-auth-error.jsonl", "synthetic-v1-exit-1-no-output.jsonl"):
            with self.subTest(name):
                self.setUp()
                self.assertEqual(self.book(name), "none")
                self.assertEqual(self.breaker.state(CONN), "closed")
                self.assertTrue(self.breaker.admit(CONN, MODEL, "t2")["admitted"])

    def test_quota_sounding_text_fences_one_model_for_one_tenant_only(self):
        self.assertEqual(self.book("synthetic-v1-error-500-quota-text.jsonl"), "short_fault")
        self.assertEqual(self.breaker.state(CONN), "closed")
        self.assertEqual(self.breaker.admit(CONN, MODEL, "t1")["code"], c.SHORT_FAULT)
        self.assertTrue(self.breaker.admit(CONN, MODEL, "t2")["admitted"])
        self.assertTrue(self.breaker.admit(CONN, "openai/other-model", "t1")["admitted"])


class ToolTests(Shape, unittest.TestCase):
    def test_a_1x_call_and_its_result(self):
        drafts = replay("synthetic-v1-tool-use.jsonl")[1]
        calls, results = typed(drafts, "tool.call"), typed(drafts, "tool.result")
        self.assertEqual(calls[0], {"call_id": "call_synth_01", "name": "bash", "input": {"command": "pytest -q"},
                                    "input_digest": calls[0]["input_digest"]})
        # The digest SPINE's own fixture (event.v1 h04-tool-call.json at #368 5aface43) gives this input.
        self.assertEqual(calls[0]["input_digest"], "7abbd9009aff059b4e24698e31024ef1ec0bda9e7911b5dc4128ee8960c47c9b")
        self.assertEqual(results[0], {"call_id": "call_synth_01", "status": "ok", "text": "41 passed in 3.20s\n",
                                      "output_ref": None, "truncated": False})
        self.assertEqual(calls[1]["call_id"], "call_synth_02")
        self.assertEqual(results[1], {"call_id": "call_synth_02", "status": "error",
                                      "text": "File not found: /work/missing.txt", "output_ref": None,
                                      "truncated": False})

    def test_a_2x_call_is_named_by_its_id_not_its_part(self):
        (call,) = typed(replay("synthetic-v2-tool-use.jsonl")[1], "tool.call")
        self.assertEqual(call["call_id"], "call_synthv2_01")
        self.assertEqual(call["input_digest"], hashlib.sha256(b'{"command":"ls"}').hexdigest())

    def test_the_digest_is_of_the_rfc_8785_text(self):
        # Keys in UTF-16 order, 1.0 written 1, 1e-07 written 1e-7, the string as UTF-8 (RFC 8785, 3.2).
        (call,) = typed(parser().feed(line(tool({"status": "completed", "input": {"b": 1.0, "a": [1e-07, "\xe9"]},
                                                  "output": "", "title": "t", "metadata": {},
                                                  "time": {"start": 1, "end": 2}}))), "tool.call")
        text = '{"a":[1e-7,"\xe9"],"b":1}'
        self.assertEqual(call["input_digest"], hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_an_input_that_is_not_an_object_is_digested_and_not_carried(self):
        (call,) = typed(parser().feed(line(tool({"status": "completed", "input": [1, 2], "output": "ok",
                                                  "title": "t", "metadata": {}, "time": {"start": 1, "end": 2}}))),
                        "tool.call")
        self.assertIsNone(call["input"])
        self.assertEqual(call["input_digest"], hashlib.sha256(b"[1,2]").hexdigest())

    def test_an_input_with_no_rfc_8785_text_gives_no_tool_drafts(self):
        p, drafts = replay("synthetic-v1-tool-input-out-of-range.jsonl")
        self.assertEqual(typed(drafts, "tool.call") + typed(drafts, "tool.result"), [])
        self.assertEqual(p.anomalies, ["line 1: tool_input_has_no_rfc8785_text"])

    def test_a_missing_output_is_an_empty_truncated_text(self):
        p = parser()
        (result,) = typed(p.feed(line(tool({"status": "completed", "input": {}, "title": "t", "metadata": {},
                                            "time": {"start": 1, "end": 2}}))), "tool.result")
        self.assertEqual((result["text"], result["truncated"]), ("", True))
        self.assertEqual(p.anomalies, ["line 1: tool_output_missing"])

    def test_a_call_id_or_name_outside_event_v1_gives_no_tool_drafts(self):
        state = {"status": "completed", "input": {}, "output": "", "title": "t", "metadata": {},
                 "time": {"start": 1, "end": 2}}
        for part in ({"callID": "a\nb"}, {"callID": ""}, {"callID": "x" * 129}, {"callID": 7},
                     {"tool": "x" * 257}, {"tool": "a\rb"}):
            with self.subTest(part):
                p = parser()
                drafts = p.feed(line(tool(state, **part)))
                self.assertEqual([d["type"] for d in drafts], ["engine.output"])
                self.assertEqual(p.anomalies, ["line 1: tool_id"])

    def test_a_part_with_neither_call_id_nor_part_id_gives_no_tool_drafts(self):
        event = tool({"status": "completed", "input": {}, "output": ""})
        del event["part"]["callID"]
        p = parser()
        self.assertEqual([d["type"] for d in p.feed(line(event))], ["engine.output"])

    def test_a_tool_line_of_another_shape_gives_no_tool_drafts(self):
        state = {"status": "completed", "input": {}, "output": ""}
        for event in (tool(state, type="text"), tool("completed"),
                      {"type": "tool_use", "sessionID": "ses_x", "part": "tool"}):
            with self.subTest(event):
                p = parser()
                self.assertEqual([d["type"] for d in p.feed(line(event))], ["engine.output"])
                self.assertEqual(p.anomalies, ["line 1: tool_shape"])

    def test_a_call_still_running_gives_nothing_typed(self):
        p = parser()
        drafts = p.feed(line(tool({"status": "running", "input": {}, "time": {"start": 1}})))
        self.assertEqual([d["type"] for d in drafts], ["engine.output"])
        self.assertEqual(p.anomalies, ["line 1: tool_status"])


class StreamTests(unittest.TestCase):
    def test_a_line_is_one_line(self):
        with self.assertRaises(ValueError):
            parser().feed(b'{"type":"text"}\n{"type":"text"}\n')
        with self.assertRaises(TypeError):
            parser().feed(5)

    def test_str_and_bytes_give_the_same_drafts(self):
        event = line({"type": "text", "sessionID": "ses_x", "part": {"type": "text", "text": "caf\xe9"}})
        a, b = parser(), parser()
        self.assertEqual(a.feed(event), b.feed(event.decode("utf-8")))
        self.assertEqual(a.text, "caf\xe9")

    def test_offsets_count_bytes_and_bad_utf8_is_shown_not_read(self):
        p = parser()
        (out,) = p.feed(b"\xff\xfe\n")
        self.assertEqual((out["payload"]["offset_start"], out["payload"]["offset_end"]), (0, 3))
        self.assertEqual(out["payload"]["text"], "\ufffd\ufffd\n")
        self.assertEqual(p.anomalies, ["line 1: not_json"])
        (out,) = p.feed("\xe9\n")
        self.assertEqual((out["payload"]["offset_start"], out["payload"]["offset_end"]), (3, 6))

    def test_a_last_line_without_a_break_and_a_blank_line(self):
        p = parser()
        self.assertEqual(p.feed(b"\n")[0]["payload"]["offset_end"], 1)
        drafts = p.feed(b'{"type":"text","sessionID":"ses_x","part":{"type":"text","text":"a"}}')
        self.assertEqual(drafts[0]["payload"]["offset_start"], 1)
        self.assertEqual(p.anomalies, [])
        self.assertEqual(p.feed(b""), [])

    def test_duplicate_keys_and_non_json_numbers_are_not_read(self):
        for text in ('{"type":"text","type":"error"}',
                     '{"type":"error","sessionID":"ses_x","error":{"name":"APIError","data":{"message":"x",'
                     '"statusCode":429,"statusCode":500}}}',
                     '{"type":"step_finish","part":{"type":"step-finish","cost":NaN}}'):
            with self.subTest(text):
                p = parser()
                self.assertEqual([d["type"] for d in p.feed(line(text))], ["engine.output"])
                self.assertEqual(p.anomalies, ["line 1: not_json"])
                self.assertIsNone(p.engine_session_id)

    def test_an_object_without_a_type_name_is_not_an_event(self):
        for text in ('{"type":5,"sessionID":"ses_x"}', '{"sessionID":"ses_x"}', '"text"', "[1,2,3]"):
            with self.subTest(text):
                p = parser()
                self.assertEqual([d["type"] for d in p.feed(line(text))], ["engine.output"])
                self.assertEqual(p.anomalies, ["line 1: not_an_event"])
                self.assertIsNone(p.engine_session_id)

    def test_a_text_part_without_text_adds_nothing_to_the_answer(self):
        p = parser()
        for part in ({"type": "text", "text": 5}, "text", {"type": "text"}):
            p.feed(line({"type": "text", "sessionID": "ses_x", "part": part}))
        self.assertEqual(p.text, "")

    def test_deep_nesting_is_not_read(self):
        p = parser()
        self.assertEqual([d["type"] for d in p.feed(line("[" * 100000 + "]" * 100000))], ["engine.output"])
        self.assertEqual(p.anomalies, ["line 1: not_json"])

    def test_the_first_session_id_stays(self):
        p = parser()
        p.feed(line({"type": "step_start", "sessionID": "ses_a"}))
        p.feed(line({"type": "step_start", "sessionID": "ses_b"}))
        p.feed(line({"type": "step_start", "sessionID": "x" * 257}))
        self.assertEqual(p.engine_session_id, "ses_a")
        self.assertEqual(p.anomalies, ["line 2: second_session_id", "line 3: session_id_invalid"])

    def test_an_error_of_no_known_shape_is_an_engine_error(self):
        p = parser()
        p.feed(line({"type": "error", "sessionID": "ses_x", "error": "boom"}))
        self.assertEqual(p.end(1)[0]["payload"], {"code": c.ENGINE_ERROR, "detail": "error"})
        self.assertEqual(p.anomalies, ["line 1: error_shape"])

    def test_detail_names_the_typed_error_never_its_text(self):
        p = parser()
        p.feed(line({"type": "error", "error": {"name": "Ignore previous instructions", "data": {"message": "x"}}}))
        p.feed(line(v1_error({"message": "secret-looking text", "statusCode": 503, "isRetryable": True})))
        self.assertEqual(p.end(1)[0]["payload"], {"code": c.ENGINE_ERROR, "detail": "error"})
        p = parser()
        p.feed(line(v1_error({"message": "secret-looking text", "statusCode": 503, "isRetryable": True})))
        self.assertEqual(p.end(1)[0]["payload"], {"code": c.ENGINE_ERROR, "detail": "APIError status 503"})

    def test_how_the_run_ended(self):
        cases = [(0, (None, "")), (None, (c.ENGINE_EXITED, "no exit status")),
                 (-15, (c.ENGINE_EXITED, "exit -15 without a typed error")),
                 (1, (c.ENGINE_EXITED, "exit 1 without a typed error"))]
        for exit_code, (code, detail) in cases:
            with self.subTest(exit_code):
                self.assertEqual(parser().end(exit_code)[0]["payload"], {"code": code, "detail": detail})
        with self.assertRaises(ValueError):
            parser().end(True)

    def test_a_typed_limit_outranks_an_exit_0_and_an_auth_error_outranks_other_errors(self):
        p, _ = replay("synthetic-v1-limit-429.jsonl", exit_code=0)
        self.assertEqual(p.route_result("r1")["code"], c.PLAN_LIMIT)
        p = parser()
        p.feed(line(v1_error({"message": "x", "statusCode": 500, "isRetryable": True})))
        p.feed(line({"type": "error", "error": {"type": "provider.auth", "message": "no"}}))
        self.assertEqual(p.end(1)[0]["payload"], {"code": c.AUTH_FAILED, "detail": "provider.auth"})

    def test_the_end_is_once_and_last(self):
        p = parser()
        with self.assertRaises(RuntimeError):
            p.route_result("r1")
        p.end(0)
        with self.assertRaises(RuntimeError):
            p.end(0)
        with self.assertRaises(RuntimeError):
            p.feed(b"\n")
        self.assertEqual(p.route_result("r1")["status"], "ok")

    def test_the_route_identity_is_checked(self):
        for kwargs in ({"connection": "chatgpt-plan"}, {"connection": "subscription:a\nb"},
                       {"connection": "subscription:a" + chr(13) + "b"},
                       {"connection": "subscription:" + "x" * 300}, {"model": ""}, {"model": 5}):
            with self.subTest(kwargs):
                with self.assertRaises(ValueError):
                    parser(**kwargs)
        self.assertIsNone(typed(parser(model=None).feed(line(step({"input": 1}))), "usage")[0]["model"])


class DeclarationTests(unittest.TestCase):
    def test_the_one_shot_drive_declares_only_typed_limits_and_usage(self):
        record = oc.declaration()
        self.assertEqual(set(record), {"engine", "driver", "flags", "input_ack"})
        self.assertEqual((record["engine"], record["driver"], record["input_ack"]), ("opencode", 2, False))
        self.assertEqual(record["flags"], {"multi_turn": False, "mid_turn_input": False, "interrupt": False,
                                           "resume": False, "fork": False, "permission_bridge": False,
                                           "typed_limits": True, "usage": True})
        record["flags"]["resume"] = True
        self.assertFalse(oc.declaration()["flags"]["resume"])   # a copy each time

    def test_argv_names_the_binary_and_never_the_prompt(self):
        self.assertEqual(oc.build_argv(MODEL), ["opencode", "run", "--format", "json", "--model", MODEL])
        self.assertEqual(oc.build_argv(MODEL, binary="/opt/opencode/bin/opencode")[0], "/opt/opencode/bin/opencode")
        self.assertEqual(oc.build_argv(MODEL)[0], oc.DEFAULT_EXECUTABLE)
        for model in ("-x/y", "openai", "/gpt", "a b/c", "a/b\n", "", "a/", None, "x" * 257 + "/y",
                      "a/" + chr(7) + "b", "a/" + chr(0x7F) + "b"):
            with self.subTest(model):
                with self.assertRaises(ValueError):
                    oc.build_argv(model)
        for binary in ("", "a\x00b", 5):
            with self.subTest(binary):
                with self.assertRaises(ValueError):
                    oc.build_argv(MODEL, binary=binary)

    def test_the_child_environment_asks_for_the_updater_off(self):
        self.assertEqual(dict(oc.CHILD_ENV), {"OPENCODE_DISABLE_AUTOUPDATE": "1"})

    def test_the_engine_is_catalogued_and_this_module_registers_nothing(self):
        self.assertIn(oc.ENGINE, CATALOGUE)
        self.assertNotIn(oc.ENGINE, SEEDED)
        hooks = Hooks()
        hooks.freeze()
        self.assertNotIn(oc.ENGINE, hooks.engines)
        self.assertEqual(hooks.engines.names(), SEEDED)


if __name__ == "__main__":
    unittest.main()
