# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E8.
"""The Pi parser, rule by rule. Every record here is SYNTHETIC (written by hand from Pi's protocol pages).

Run by dotted name: PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.pi.test_pi_parser
(``unittest discover`` does not enter this folder: it has no ``__init__.py``, by lane DRIVER-CC's path rule.)
"""

import hashlib
import json
import sys
import unittest

from recursion_core.routes.engines import pi
from recursion_core.routes.engines.fixtures.pi import fake_pi as f

CONN = "subscription:pi-chatgpt"


def rec(record) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


def json_stream(header=True):
    stream = pi.PiStream("json", connection_id=CONN)
    if header:
        stream.feed(rec({"type": "session", "version": 3, "id": f.SESSION_ID, "timestamp": "2026-11-14T22:00:00.000Z",
                         "cwd": "/work"}))
    return stream


def assistant_end(**kw):
    return rec({"type": "message_end", "message": f.assistant(kw.pop("text", "ok"), f.T0, **kw)})


def drafts_of(stream, *records):
    out = []
    for record in records:
        out += stream.feed(record if isinstance(record, str) else rec(record))
    return out


class FramingTest(unittest.TestCase):
    def test_records_split_on_lf_only_and_keep_line_separators_inside_strings(self):
        ls, ps = chr(0x2028), chr(0x2029)
        first = rec({"type": "x", "text": f"a{ls}b{ps}c"}).encode("utf-8")
        data = first + b'{"type":"y"}\r\n' + b'{"type":"z"}'
        records = pi.records_of(data)
        self.assertEqual(len(records), 3)
        self.assertEqual(pi.read_record(records[0])["text"], f"a{ls}b{ps}c")
        self.assertEqual(records[1], '{"type":"y"}')   # the CR before the LF is stripped
        self.assertEqual(records[2], '{"type":"z"}')   # a final fragment with no LF is a record

    def test_records_of_refuses_invalid_utf8(self):
        with self.assertRaises(pi.PiProtocolError) as caught:
            pi.records_of(b'{"type":"x","t":"\xff"}\n')
        self.assertEqual(caught.exception.code, "not_utf8")

    def test_read_record_refuses_what_pi_never_writes(self):
        cases = {
            "": "not_json", "not json": "not_json", "[1]": "not_an_object", '{"a":1}': "no_type",
            '{"type":""}': "no_type", '{"type":7}': "no_type",
            '{"type":"x","a":1,"a":2}': "duplicate_key", '{"type":"x","o":{"k":1,"k":1}}': "duplicate_key",
            '{"type":"x","n":NaN}': "not_a_number", '{"type":"x","n":Infinity}': "not_a_number",
            '{"type":"x","n":-Infinity}': "not_a_number", '{"type":"x"}\n{"type":"y"}': "lf_inside_record",
            # valid JSON numbers no binary64 holds: Python would read each as an infinity
            '{"type":"x","n":1e400}': "number_out_of_range", '{"type":"x","n":-1e400}': "number_out_of_range",
            '{"type":"x","o":{"a":[1E309]}}': "number_out_of_range",
        }
        for text, code in cases.items():
            with self.subTest(text=text), self.assertRaises(pi.PiProtocolError) as caught:
                pi.read_record(text)
            self.assertEqual(caught.exception.code, code)

    def test_finite_numbers_at_the_edges_are_read_as_given(self):
        record = pi.read_record('{"type":"x","big":1.7976931348623157e308,"tiny":1e-400,"s":"1e400","i":10}')
        self.assertEqual(record, {"type": "x", "big": 1.7976931348623157e308, "tiny": 0.0, "s": "1e400", "i": 10})

    def test_read_record_takes_bytes_and_text_with_or_without_the_line_end(self):
        for line in ('{"type":"x"}', '{"type":"x"}\n', '{"type":"x"}\r\n', b'{"type":"x"}\n'):
            self.assertEqual(pi.read_record(line), {"type": "x"})
        with self.assertRaises(pi.PiProtocolError):
            pi.read_record(b'{"type":"\xc3"}')


class DigestTest(unittest.TestCase):
    def test_number_text_is_ecmascripts(self):
        cases = [(1.0, "1"), (-0.0, "0"), (0, "0"), (1e21, "1e+21"), (1e20, "100000000000000000000"),
                 (1e-7, "1e-7"), (1e-6, "0.000001"), (123.456, "123.456"), (-1.5, "-1.5"),
                 (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"), (0.1, "0.1"),
                 (2**53 - 1, "9007199254740991"), (123456789012.5, "123456789012.5"), (1.25e-5, "0.0000125")]
        for number, text in cases:
            with self.subTest(number=number):
                self.assertEqual(pi.number_text(number), text)

    def test_canonical_text_orders_keys_by_utf16_and_escapes_as_rfc8785(self):
        high, private = chr(0x1F600), chr(0xE000)   # by code point private < high; by UTF-16 high < private
        value = {"b": 1, "a": [True, None, "x"], private: 0, high: 0, "c": "q\"\\\b\f\n\r\t" + chr(1) + chr(0x7F)}
        text = pi.canonical_text(pi.normalised(value))
        self.assertEqual(text, '{"a":[true,null,"x"],"b":1,"c":"q\\"\\\\\\b\\f\\n\\r\\t\\u0001' + chr(0x7F) + '",'
                               f'"{high}":0,"{private}":0}}')

    def test_input_digest_is_sha256_of_the_canonical_text_of_the_normalised_input(self):
        value, digest, size = pi.input_digest({"n": 2.0, "z": -0.0, "s": "x"})
        self.assertEqual(value, {"n": 2, "z": 0, "s": "x"})
        self.assertEqual(type(value["n"]), int)
        text = '{"n":2,"s":"x","z":0}'.encode("utf-8")
        self.assertEqual(digest, hashlib.sha256(text).hexdigest())
        self.assertEqual(size, len(text))

    def test_an_input_outside_the_strict_profile_is_refused_not_guessed(self):
        deep = {}
        cursor = deep
        for _ in range(pi.MAX_DEPTH + 1):
            cursor["d"] = {}
            cursor = cursor["d"]
        cases = [({"s": "a" + chr(0xD800)}, "lone_surrogate"), ({chr(0xDC00): 1}, "lone_surrogate"),
                 ({"n": 2**53}, "integer_out_of_range"), ({"n": -(2**53)}, "integer_out_of_range"),
                 ({"n": 1e300}, "integer_out_of_range"), ({"n": float("inf")}, "number_out_of_range"),
                 ({"n": float("nan")}, "number_out_of_range"), (deep, "tool_input_too_deep"),
                 ({"o": object()}, "not_a_json_value")]
        for value, code in cases:
            with self.subTest(code=code), self.assertRaises(pi.PiProtocolError) as caught:
                pi.input_digest(value)
            self.assertEqual(caught.exception.code, code)
        pi.input_digest({"n": 2**53 - 1, "m": -(2**53 - 1), "f": 0.5})   # the edges themselves are taken

    def test_the_depth_limit_is_64_levels_below_the_input(self):
        # Pinned by its number, not by the constant: changing the limit must change this test.
        nested = {}
        for _ in range(64):
            nested = {"d": nested}
        pi.input_digest(nested)
        with self.assertRaises(pi.PiProtocolError) as caught:
            pi.input_digest({"d": nested})
        self.assertEqual(caught.exception.code, "tool_input_too_deep")

    def test_a_tool_call_with_an_input_outside_the_profile_is_a_protocol_error(self):
        stream = json_stream()
        line = rec({"type": "message_end", "message": f.assistant("", f.T0, calls=[("c1", "bash", {"n": 2**60})])})
        with self.assertRaises(pi.PiProtocolError) as caught:
            stream.feed(line)
        self.assertEqual(caught.exception.code, "integer_out_of_range")


class UsageTest(unittest.TestCase):
    def usage_of(self, used, **message):
        stream = json_stream()
        drafts = drafts_of(stream, assistant_end(used=used, **message))
        self.assertEqual(drafts[0]["type"], "usage")
        return drafts[0]["payload"]

    def test_counts_are_observed_and_the_cost_modelled(self):
        p = self.usage_of(f.usage(1200, 30, cache_read=800, cache_write=5, cost=0.0042))
        self.assertEqual(p["connection_id"], CONN)
        self.assertEqual(p["model"], "openai/model-synthetic")
        self.assertIsNone(p["bucket"])
        self.assertEqual(p["span"], "call")
        self.assertEqual(p["input_tokens"], {"value": 1200, "quality": "known", "provenance": "observed"})
        self.assertEqual(p["output_tokens"], {"value": 30, "quality": "known", "provenance": "observed"})
        self.assertEqual(p["cache_read_tokens"], {"value": 800, "quality": "known", "provenance": "observed"})
        self.assertEqual(p["cache_write_tokens"], {"value": 5, "quality": "known", "provenance": "observed"})
        self.assertEqual(p["cost_usd"], {"value": 0.0042, "quality": "known", "provenance": "modelled"})

    def test_reasoning_is_unknown_when_pi_leaves_it_out_and_known_when_given(self):
        unknown = {"value": None, "quality": "unknown", "provenance": "unavailable"}
        self.assertEqual(self.usage_of(f.usage(10, 5))["reasoning_tokens"], unknown)
        self.assertEqual(self.usage_of(f.usage(10, 5, reasoning=4))["reasoning_tokens"],
                         {"value": 4, "quality": "known", "provenance": "observed"})

    def test_a_usage_with_every_count_zero_is_unknown_never_zero(self):
        unknown = {"value": None, "quality": "unknown", "provenance": "unavailable"}
        p = self.usage_of(f.usage(0, 0, reasoning=0, cost=0), stop="error", error="boom")
        for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
                     "cost_usd"):
            self.assertEqual(p[name], unknown, name)

    def test_a_malformed_count_is_unknown_and_the_rest_stay_known(self):
        used = f.usage(10, 5)
        used["cacheRead"], used["output"] = -1, True
        p = self.usage_of(used)
        self.assertEqual(p["input_tokens"]["value"], 10)
        self.assertEqual(p["cache_read_tokens"]["quality"], "unknown")
        self.assertEqual(p["output_tokens"]["quality"], "unknown")
        used = f.usage(10, 5)
        used["cost"] = {"total": -0.5}
        self.assertEqual(self.usage_of(used)["cost_usd"]["quality"], "unknown")

    def test_a_cost_pi_could_not_have_written_is_unknown(self):
        known = {"value": None, "quality": "known", "provenance": "modelled"}
        for cost, want in ((True, None), (False, None), (10**400, None), (2**53, None), (-1, None), ("0.1", None),
                           (None, None), (2**53 - 1, 2**53 - 1), (0, 0), (0.5, 0.5)):
            with self.subTest(cost=str(cost)[:20]):
                used = f.usage(10, 5)
                used["cost"]["total"] = cost
                got = self.usage_of(used)["cost_usd"]
                if want is None:
                    self.assertEqual(got, {"value": None, "quality": "unknown", "provenance": "unavailable"})
                else:
                    self.assertEqual(got, dict(known, value=want))

    def test_a_cost_too_large_for_binary64_is_a_protocol_error(self):
        # Where one read asked for "unknown" and the other for a refusal at read, the stricter is taken.
        line = assistant_end(used=f.usage(10, 5, cost=0.25)).replace('"total":0.25', '"total":1e309')
        self.assertIn("1e309", line)
        with self.assertRaises(pi.PiProtocolError) as caught:
            json_stream().feed(line)
        self.assertEqual(caught.exception.code, "number_out_of_range")

    def test_no_usage_object_is_unknown_and_a_model_without_provider_stays_bare(self):
        message = f.assistant("ok", f.T0)
        del message["usage"], message["provider"]
        stream = json_stream()
        p = stream.feed(rec({"type": "message_end", "message": message}))[0]["payload"]
        self.assertEqual(p["input_tokens"]["quality"], "unknown")
        self.assertEqual(p["model"], "model-synthetic")


class HeldUsageTest(unittest.TestCase):
    """Usage that is not one model call: measured, kept once as a signal, never drafted (questions.md Q10)."""

    def tool_result(self, used, call_id="c1"):
        message = {"role": "toolResult", "toolCallId": call_id, "toolName": "delegate",
                   "content": [{"type": "text", "text": "done"}], "isError": False, "timestamp": f.T0}
        if used is not None:
            message["usage"] = used
        return message

    def held(self, stream):
        return [s for s in stream.signals if s["signal"] == "usage_held"]

    def test_a_tool_results_usage_is_held_once_with_its_measurements_and_no_model(self):
        stream = json_stream()
        result = self.tool_result(f.usage(700, 60, cost=0.07))
        drafts = drafts_of(stream, {"type": "message_start", "message": result},
                           {"type": "message_end", "message": result},
                           {"type": "message_end", "message": result},     # the same result again
                           {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "delegate",
                            "result": {"content": result["content"], "usage": result["usage"]}, "isError": False},
                           {"type": "turn_end", "message": f.assistant("", f.T0), "toolResults": [result]},
                           {"type": "agent_end", "messages": [result], "willRetry": False})
        self.assertEqual([d["type"] for d in drafts], ["tool.result"])      # no usage draft
        held = self.held(stream)
        self.assertEqual(len(held), 1)
        self.assertEqual((held[0]["source"], held[0]["call_id"]), ("tool_result", "c1"))
        usage = held[0]["usage"]
        self.assertEqual((usage["connection_id"], usage["model"], usage["bucket"]), (CONN, None, None))
        self.assertEqual(usage["input_tokens"], {"value": 700, "quality": "known", "provenance": "observed"})
        self.assertEqual(usage["cost_usd"], {"value": 0.07, "quality": "known", "provenance": "modelled"})
        self.assertNotIn("span", usage)

    def test_a_compactions_usage_is_held_and_a_compaction_without_one_holds_nothing(self):
        stream = json_stream()
        done = {"type": "compaction_end", "reason": "threshold", "aborted": False, "willRetry": False,
                "result": {"summary": "s", "firstKeptEntryId": "e1", "tokensBefore": 10000,
                           "estimatedTokensAfter": 1000, "usage": f.usage(900, 90, cost=0.09), "details": {}}}
        self.assertEqual(stream.feed(rec(done)), [])
        held = self.held(stream)
        self.assertEqual([(h["source"], h["usage"]["input_tokens"]["value"]) for h in held], [("compaction", 900)])
        self.assertNotIn("call_id", held[0])
        for empty in ({"type": "compaction_end", "reason": "threshold", "aborted": True, "willRetry": False},
                      {"type": "compaction_end", "reason": "overflow", "aborted": False, "willRetry": False,
                       "errorMessage": "summary failed"},
                      dict(done, result=dict(done["result"], usage=f.usage(0, 0)))):
            with self.subTest(empty=str(empty)[:50]):
                quiet = json_stream()
                self.assertEqual(quiet.feed(rec(empty)), [])
                self.assertEqual(self.held(quiet), [])

    def test_a_tool_result_whose_call_id_is_not_an_id_holds_nothing(self):
        for call_id in ("", "x\ry", "x\ny", "x" * 129, None, 7, [], {}):
            with self.subTest(call_id=str(call_id)[:20]):
                stream = json_stream()
                message = dict(self.tool_result(f.usage(100, 2, cost=0.02)), toolCallId=call_id)
                self.assertEqual(stream.feed(rec({"type": "message_end", "message": message})), [])
                self.assertEqual(self.held(stream), [])
        stream = json_stream()                                # the longest id that is one is held
        stream.feed(rec({"type": "message_end", "message": self.tool_result(f.usage(100, 2), call_id="x" * 128)}))
        self.assertEqual(len(self.held(stream)), 1)

    def test_a_tool_result_without_reported_usage_holds_nothing(self):
        for used in (None, f.usage(0, 0), "x"):
            with self.subTest(used=used):
                stream = json_stream()
                stream.feed(rec({"type": "message_end", "message": self.tool_result(used)}))
                self.assertEqual(self.held(stream), [])

    def test_held_usage_would_fit_event_v1_once_it_has_a_span(self):
        from recursion_core.routes.engines.fixtures.pi.test_pi_fixtures import NO_RULES, RULES
        if RULES is None:
            self.skipTest(NO_RULES)
        stream = json_stream()
        stream.feed(rec({"type": "message_end", "message": self.tool_result(f.usage(700, 60, cost=0.07))}))
        payload = self.held(stream)[0]["usage"]
        self.assertTrue(RULES.fits("driver.v2.json#/$defs/event_draft",
                                   {"type": "usage", "payload": dict(payload, span="call")}))
        self.assertFalse(RULES.fits("driver.v2.json#/$defs/event_draft", {"type": "usage", "payload": payload}))


class ToolTest(unittest.TestCase):
    def test_a_tool_call_comes_from_the_assistants_block_and_its_start_adds_nothing(self):
        stream = json_stream()
        calls = [("call_1", "bash", {"command": "ls", "timeout": 30.0})]
        drafts = drafts_of(stream, assistant_end(text="", stop="toolUse", calls=calls),
                           {"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "bash",
                            "args": {"command": "ls", "timeout": 30}})
        self.assertEqual([d["type"] for d in drafts], ["usage", "tool.call"])
        payload = drafts[1]["payload"]
        self.assertEqual(payload["call_id"], "call_1")
        self.assertEqual(payload["name"], "bash")
        self.assertEqual(payload["input"], {"command": "ls", "timeout": 30})
        self.assertEqual(payload["input_digest"],
                         hashlib.sha256(b'{"command":"ls","timeout":30}').hexdigest())

    def test_a_start_whose_id_no_block_named_drafts_the_call(self):
        stream = json_stream()
        drafts = drafts_of(stream, {"type": "tool_execution_start", "toolCallId": "x9", "toolName": "read",
                                    "args": {"path": "a"}})
        self.assertEqual(drafts[0]["type"], "tool.call")
        self.assertEqual(drafts[0]["payload"]["name"], "read")

    def test_a_repeated_call_id_is_drafted_once(self):
        stream = json_stream()
        calls = [("c1", "bash", {}), ("c1", "bash", {})]
        drafts = drafts_of(stream, assistant_end(text="", stop="toolUse", calls=calls))
        self.assertEqual([d["type"] for d in drafts], ["usage", "tool.call"])
        self.assertEqual(stream.ignored, {"tool_call_repeated": 1})

    def test_a_large_input_is_withheld_and_still_digested(self):
        stream = json_stream()
        big = {"content": "x" * pi.INPUT_CARRY_MAX}
        drafts = drafts_of(stream, assistant_end(text="", stop="toolUse", calls=[("c1", "write", big)]))
        payload = drafts[1]["payload"]
        self.assertIsNone(payload["input"])
        self.assertEqual(payload["input_digest"], pi.input_digest(big)[1])
        small = {"content": "x" * (pi.INPUT_CARRY_MAX - len('{"content":""}'))}
        stream = json_stream()
        drafts = drafts_of(stream, assistant_end(text="", stop="toolUse", calls=[("c2", "write", small)]))
        self.assertEqual(drafts[1]["payload"]["input"], small)   # exactly at the cap it is carried

    def test_the_carry_cap_is_262144_utf8_bytes(self):
        # Pinned by its number. '{"content":""}' is 14 bytes; an e-acute is 2 bytes in UTF-8, 1 character.
        def carried(arguments):
            stream = json_stream()
            return stream.feed(rec({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "write",
                                    "args": arguments}))[0]["payload"]["input"]
        self.assertIsNotNone(carried({"content": "x" * (262144 - 14)}))
        self.assertIsNone(carried({"content": "x" * (262144 - 13)}))
        self.assertIsNone(carried({"content": "\u00e9" * 131072}))         # 131072 characters, 262158 bytes

    def test_the_result_text_cap_is_65536_characters(self):
        for size, truncated in ((65536, False), (65537, True)):
            with self.subTest(size=size):
                payload = json_stream().feed(rec(self.end(result={"content": [{"type": "text", "text": "y" * size}]})))
                self.assertEqual((len(payload[0]["payload"]["text"]), payload[0]["payload"]["truncated"]),
                                 (65536, truncated))

    def test_a_carriage_return_in_an_id_or_a_name_is_a_protocol_error(self):
        for call_id, name, code in (("call\rname", "bash", "tool_call_id"), ("\r", "bash", "tool_call_id"),
                                    ("c1", "x\ry", "tool_name")):
            with self.subTest(call_id=call_id, name=name), self.assertRaises(pi.PiProtocolError) as caught:
                json_stream().feed(rec({"type": "tool_execution_start", "toolCallId": call_id, "toolName": name,
                                        "args": {}}))
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(pi.PiProtocolError):
            json_stream().feed(rec(self.end(toolCallId="c\r1")))

    def test_bad_call_ids_names_and_inputs_are_protocol_errors(self):
        for call_id, name, arguments, code in [
                ("", "bash", {}, "tool_call_id"), ("a\nb", "bash", {}, "tool_call_id"),
                ("x" * 129, "b", {}, "tool_call_id"),
                (7, "bash", {}, "tool_call_id"), ("c", "", {}, "tool_name"), ("c", "x" * 257, {}, "tool_name"),
                ("c", "bash", ["ls"], "tool_input_not_an_object"), ("c", "bash", None, "tool_input_not_an_object")]:
            with self.subTest(code=code, call_id=call_id), self.assertRaises(pi.PiProtocolError) as caught:
                json_stream().feed(rec({"type": "tool_execution_start", "toolCallId": call_id, "toolName": name,
                                        "args": arguments}))
            self.assertEqual(caught.exception.code, code)

    def end(self, **fields):
        record = {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
                  "result": {"content": [{"type": "text", "text": "out"}], "details": {}}, "isError": False}
        record.update(fields)
        return record

    def test_a_tool_result_carries_its_text_and_status(self):
        stream = json_stream()
        drafts_of(stream, {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {}})
        ok = stream.feed(rec(self.end(durationMs=3)))[0]
        self.assertEqual(ok, {"type": "tool.result", "payload": {
            "call_id": "c1", "status": "ok", "text": "out", "output_ref": None, "truncated": False}})
        self.assertEqual(stream.feed(rec(self.end(toolCallId="c2", isError=True)))[0]["payload"]["status"], "error")
        self.assertEqual(stream.ignored, {})
        self.assertIn({"signal": "tool_result_without_call", "call_id": "c2"}, stream.signals)

    def test_a_repeated_result_is_drafted_once(self):
        stream = json_stream()
        self.assertEqual(len(stream.feed(rec(self.end()))), 1)
        self.assertEqual(stream.feed(rec(self.end())), [])
        self.assertEqual(stream.ignored, {"tool_result_repeated": 1})

    def test_what_is_not_text_marks_the_result_truncated(self):
        image = {"type": "image", "data": "AAAA", "mimeType": "image/png"}
        cases = [
            ({"content": [{"type": "text", "text": "a"}, image, {"type": "text", "text": "b"}]}, "a\nb", True),
            ({"content": "flat"}, "", True), (None, "", True), ({"content": []}, "", False),
            ({"content": [{"type": "text", "text": "y" * (pi.TOOL_TEXT_MAX + 1)}]}, "y" * pi.TOOL_TEXT_MAX, True),
            ({"content": [{"type": "text", "text": "y" * pi.TOOL_TEXT_MAX}]}, "y" * pi.TOOL_TEXT_MAX, False),
        ]
        for result, text, truncated in cases:
            with self.subTest(result=str(result)[:40]):
                payload = json_stream().feed(rec(self.end(result=result)))[0]["payload"]
                self.assertEqual((payload["text"], payload["truncated"]), (text, truncated))

    def test_a_result_without_a_valid_call_id_is_a_protocol_error(self):
        with self.assertRaises(pi.PiProtocolError):
            json_stream().feed(rec(self.end(toolCallId="")))


class JsonModeTest(unittest.TestCase):
    def test_the_header_gives_the_session_id_and_must_come_first(self):
        stream = json_stream()
        self.assertEqual(stream.engine_session_id, f.SESSION_ID)
        self.assertEqual(stream.signals, [{"signal": "session", "engine_session_id": f.SESSION_ID}])
        header = rec({"type": "session", "version": 3, "id": "other", "timestamp": "t", "cwd": "/"})
        with self.assertRaises(pi.PiProtocolError):
            stream.feed(header)                       # a second header
        late = json_stream(header=False)
        late.feed(rec({"type": "agent_start"}))
        with self.assertRaises(pi.PiProtocolError):
            late.feed(header)                         # a header after another record
        for bad in ("", "x" * 257, None, 7):
            with self.subTest(id=bad), self.assertRaises(pi.PiProtocolError):
                pi.PiStream("json", connection_id=CONN).feed(rec({"type": "session", "id": bad}))

    def test_a_run_without_a_header_has_no_session_id(self):
        stream = json_stream(header=False)
        drafts_of(stream, {"type": "agent_start"}, assistant_end(), {"type": "agent_settled", "aborted": False})
        self.assertIsNone(stream.engine_session_id)

    def test_the_argv_prompt_is_accepted_by_its_replay_with_the_same_text_once(self):
        stream = json_stream()
        stream.argv_prompt("Hello there", delivery_id="dlv-1", turn_id="turn-1")
        self.assertEqual(stream.pending_deliveries(), ["dlv-1"])
        replay = {"type": "message_end", "message": f.user("Hello there", f.T0)}
        self.assertEqual(stream.feed(rec(replay)), [{"type": "input.accepted", "payload": {
            "delivery_id": "dlv-1", "turn_id": "turn-1", "how": "new_turn"}}])
        self.assertEqual(stream.pending_deliveries(), [])
        self.assertEqual(stream.feed(rec(replay)), [])          # a second replay accepts nothing
        self.assertEqual(stream.ignored, {"user_replay_unexpected": 1})

    def test_a_replay_with_other_text_accepts_nothing(self):
        stream = json_stream()
        stream.argv_prompt("Hello there", delivery_id="dlv-1", turn_id="turn-1")
        for content in ("Hello", [{"type": "text", "text": "Hello there"},
                                  {"type": "image", "data": "A", "mimeType": "image/png"}]):
            replay = {"type": "message_end", "message": {"role": "user", "content": content, "timestamp": 1}}
            self.assertEqual(stream.feed(rec(replay)), [])
        self.assertEqual(stream.pending_deliveries(), ["dlv-1"])
        blocks = {"type": "message_end", "message": {"role": "user", "timestamp": 1, "content": [
            {"type": "text", "text": "Hello "}, {"type": "text", "text": "there"}]}}
        self.assertEqual(len(stream.feed(rec(blocks))), 1)    # text blocks joined are the same text

    def test_argv_prompt_takes_one_well_formed_prompt(self):
        stream = json_stream()
        for kwargs in ({"delivery_id": "1", "turn_id": "turn-1"}, {"delivery_id": "dlv-1", "turn_id": "t1"}):
            with self.assertRaises(ValueError):
                stream.argv_prompt("x", **kwargs)
        with self.assertRaises(ValueError):
            stream.argv_prompt("", delivery_id="dlv-1", turn_id="turn-1")
        stream.argv_prompt("x", delivery_id="dlv-1", turn_id="turn-1")
        with self.assertRaises(ValueError):
            stream.argv_prompt("y", delivery_id="dlv-2", turn_id="turn-2")
        with self.assertRaises(ValueError):
            stream.prompt("x", delivery_id="dlv-3", turn_id="turn-3", how="new_turn")   # an RPC line
        with self.assertRaises(ValueError):
            stream.command("abort")
        with self.assertRaises(ValueError):
            stream.cancel_dialog("ui-1")                                                # an RPC record too

    def test_rpc_records_are_protocol_errors_in_a_one_shot_run(self):
        for record in ({"type": "response", "command": "prompt", "success": True},
                       {"type": "extension_ui_request", "id": "u1", "method": "confirm"}):
            with self.subTest(kind=record["type"]), self.assertRaises(pi.PiProtocolError):
                json_stream().feed(rec(record))

    def test_records_of_other_types_are_counted_and_drafted_as_nothing(self):
        stream = json_stream()
        drafts = drafts_of(stream, {"type": "queue_update", "steering": [], "followUp": []},
                           {"type": "compaction_start", "reason": "threshold"},
                           {"type": "message_update", "usage": {}, "assistantMessageEvent": {"type": "text_delta"}},
                           {"type": "a_type_from_a_later_version"}, {"type": "queue_update"})
        self.assertEqual(drafts, [])
        self.assertEqual(stream.other_types, {"queue_update": 2, "compaction_start": 1, "message_update": 1,
                                              "a_type_from_a_later_version": 1})

    def test_messages_of_other_roles_draft_nothing_and_a_message_must_be_an_object(self):
        stream = json_stream()
        for role in ("toolResult", "custom", "bashExecution", "system"):
            self.assertEqual(stream.feed(rec({"type": "message_end", "message": {"role": role}})), [])
        for bad in ({"type": "message_end"}, {"type": "message_end", "message": "x"},
                    {"type": "message_end", "message": {"role": "assistant", "content": "x"}},
                    {"type": "message_end", "message": {"role": "assistant", "content": ["x"]}},
                    {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text"}]}}):
            with self.subTest(bad=bad), self.assertRaises(pi.PiProtocolError):
                json_stream().feed(rec(bad))

    def settle(self, *records, aborted=False):
        stream = json_stream()
        drafts = drafts_of(stream, {"type": "agent_start"}, *records, {"type": "agent_settled", "aborted": aborted})
        return stream, drafts[-1]

    def test_the_result_is_drafted_at_settle_from_the_last_assistant_message(self):
        stream, result = self.settle(assistant_end(text="first", stop="toolUse"), assistant_end(text="done"))
        self.assertEqual(result, {"type": "result", "payload": {"code": None, "detail": ""}})
        self.assertEqual(stream.final["text"], "done")
        self.assertTrue(stream.settled)
        self.assertEqual(stream.signals[-1], {"signal": "settled", "aborted": False, "stop_reason": "stop",
                                              "text": "done"})

    def test_the_results_code_for_each_way_a_run_ends(self):
        cases = [
            (assistant_end(stop="error", error="429 Too Many Requests: rate limit"), False,
             ("short_fault", "error: 429 Too Many Requests: rate limit")),
            (assistant_end(stop="error", error="connect ECONNREFUSED"), False,
             ("engine_error", "error: connect ECONNREFUSED")),
            (assistant_end(stop="error"), False, ("engine_error", "error")),
            (assistant_end(stop="error", error="x" * 900), False, ("engine_error", "error: " + "x" * pi.DETAIL_MAX)),
            (assistant_end(stop="length"), False, ("engine_error", "stopReason length")),
            (assistant_end(stop="aborted"), False, ("engine_error", "stopReason aborted")),
            (assistant_end(stop="toolUse"), False, ("engine_error", "stopReason toolUse")),
            (assistant_end(stop="Weird Reason!"), False, ("engine_error", "stopReason unknown")),
            (assistant_end(), True, ("engine_error", "aborted")),
        ]
        for record, aborted, (code, detail) in cases:
            with self.subTest(detail=detail[:30]):
                _stream, result = self.settle(record, aborted=aborted)
                self.assertEqual(result["payload"], {"code": code, "detail": detail})
        _stream, result = self.settle()
        self.assertEqual(result["payload"], {"code": "engine_error", "detail": "no assistant message"})

    def test_the_detail_carries_500_characters_of_error_text(self):
        # Pinned by its number, not by the constant.
        _stream, result = self.settle(assistant_end(stop="error", error="x" * 499 + "yz" + "w" * 300))
        self.assertEqual(result["payload"]["detail"], "error: " + "x" * 499 + "y")
        self.assertEqual(len(result["payload"]["detail"]), len("error: ") + 500)

    def test_a_retry_hint_is_seen_until_the_next_run_starts(self):
        stream = json_stream()
        drafts_of(stream, {"type": "agent_start"}, assistant_end(stop="error", error="overloaded"))
        self.assertIsNone(stream.will_retry)                 # no run has ended yet
        stream.feed(rec({"type": "agent_end", "messages": [], "willRetry": True}))
        self.assertIs(stream.will_retry, True)
        stream.feed(rec({"type": "agent_start"}))
        self.assertIsNone(stream.will_retry)
        for hint in (False, "yes", None):
            with self.subTest(hint=hint):
                other = json_stream()
                other.feed(rec({"type": "agent_end", "messages": [], "willRetry": hint}))
                self.assertIs(other.will_retry, False)

    def test_a_retry_starts_over_and_the_last_attempt_decides(self):
        stream = json_stream()
        drafts = drafts_of(stream, {"type": "agent_start"}, assistant_end(stop="error", error="overloaded"),
                           {"type": "agent_end", "messages": [], "willRetry": True},
                           {"type": "agent_start"}, assistant_end(text="fine"),
                           {"type": "agent_end", "messages": [], "willRetry": False},
                           {"type": "agent_settled", "aborted": False})
        self.assertEqual(drafts[-1]["payload"], {"code": None, "detail": ""})
        self.assertFalse(stream.will_retry)
        stream = json_stream()
        drafts_of(stream, {"type": "agent_start"}, assistant_end(), {"type": "agent_start"})
        self.assertIsNone(stream.final)          # the second run has produced nothing yet
        self.assertFalse(stream.settled)

    def test_a_second_settle_drafts_no_second_result(self):
        stream = json_stream()
        drafts_of(stream, assistant_end(), {"type": "agent_settled", "aborted": False})
        self.assertEqual(stream.feed(rec({"type": "agent_settled", "aborted": False})), [])
        self.assertEqual(stream.ignored, {"settled_again": 1})


class RpcModeTest(unittest.TestCase):
    def setUp(self):
        self.stream = pi.PiStream("rpc", connection_id="api_key:openai-key-1")

    def response(self, token, command="prompt", success=True, **fields):
        record = {"id": token, "type": "response", "command": command, "success": success}
        record.update(fields)
        return rec(record)

    def test_the_records_this_stream_writes_carry_no_line_end(self):
        token, line = self.stream.prompt("Hi", delivery_id="dlv-1", turn_id="turn-1", how="new_turn")
        self.assertEqual((token, line), ("e8-1", '{"id":"e8-1","type":"prompt","message":"Hi"}'))
        token, line = self.stream.prompt("More " + chr(0x2028), delivery_id="dlv-2", turn_id="turn-1", how="mid_turn")
        self.assertEqual(json.loads(line), {"id": "e8-2", "type": "prompt", "message": "More " + chr(0x2028),
                                            "streamingBehavior": "steer"})
        self.assertTrue(line.isascii() and "\n" not in line and "\r" not in line)   # U+2028 written as an escape
        self.assertEqual(self.stream.command("abort"), ("e8-3", '{"id":"e8-3","type":"abort"}'))
        self.assertEqual(self.stream.command("get_state"), ("e8-4", '{"id":"e8-4","type":"get_state"}'))
        self.assertEqual(self.stream.cancel_dialog("ui-1"),
                         '{"type":"extension_ui_response","id":"ui-1","cancelled":true}')
        self.assertEqual(self.stream.pending_deliveries(), ["dlv-1", "dlv-2"])

    def test_each_record_is_one_line_through_the_launchers_own_send(self):
        # The production EngineProcess.send (daemon/launcher.py) writes the text and one LF. Its send is run
        # here on an anonymous pipe, its constructor bypassed: no daemon, unit or process is started.
        # On native Windows that send raises OSError WinError 10093. Linux only; the assertions are unchanged.
        if sys.platform != "linux":
            self.skipTest("Linux launcher pipe; native Windows raises WinError 10093")
        import os
        import threading

        from recursion_core.daemon.launcher import UnitEngineProcess
        records = [self.stream.prompt("Hi " + chr(0x2028), delivery_id="dlv-1", turn_id="turn-1", how="new_turn")[1],
                   self.stream.command("abort")[1], self.stream.command("get_state")[1],
                   self.stream.cancel_dialog("ui-1")]
        for record in records:
            with self.subTest(record=record[:40]):
                read_fd, write_fd = os.pipe()
                try:
                    process = UnitEngineProcess.__new__(UnitEngineProcess)
                    process._stdin, process._lock = write_fd, threading.Lock()
                    process.send(record)
                    wire = os.read(read_fd, 65536)
                finally:
                    os.close(read_fd)
                    os.close(write_fd)
                self.assertEqual(wire, record.encode("utf-8") + b"\n")
                self.assertEqual(pi.records_of(wire), [record])          # one record, not an empty second

    def test_the_launcher_pipe_check_skips_when_the_platform_is_not_linux(self):
        # Simulated nt: this machine is Linux, so the native WinError 10093 is not run here.
        from unittest import mock
        case = RpcModeTest("test_each_record_is_one_line_through_the_launchers_own_send")
        result = unittest.TestResult()
        with mock.patch.object(sys, "platform", "win32"):
            case.run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.skipped), 1)

    def test_two_writers_mint_unique_tokens_and_keep_every_delivery(self):
        # Eight threads, 300 prompts each: the load that minted one token twice and lost deliveries.
        import threading
        stream = pi.PiStream("rpc", connection_id="api_key:openai-key-1")
        found = []
        guard = threading.Lock()

        def writer(base):
            local = []
            for i in range(300):
                n = base + i
                token, _line = stream.prompt(
                    "p", delivery_id=f"dlv-{n}", turn_id="turn-1", how="new_turn")
                local.append(token)
            with guard:
                found.extend(local)

        threads = [threading.Thread(target=writer, args=(i * 300 + 1,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(found), 2400)
        self.assertEqual(len(set(found)), 2400)
        self.assertEqual(len(stream.pending_deliveries()), 2400)

    def test_a_reader_of_the_pending_deliveries_beside_writers_never_fails(self):
        # Four writers mint prompts while one thread asks for the pending deliveries again and again:
        # an unguarded walk over the table raised "dictionary changed size during iteration".
        import threading
        import time
        stream = pi.PiStream("rpc", connection_id="api_key:openai-key-1")
        failures = []
        done = threading.Event()

        def writer(base):
            for i in range(3000):
                stream.prompt("p", delivery_id=f"dlv-{base + i}", turn_id="turn-1", how="new_turn")

        def reader():
            deadline = time.monotonic() + 60
            while not done.is_set() and time.monotonic() < deadline:
                try:
                    stream.pending_deliveries()
                except Exception as error:      # the property under test is that none is raised
                    failures.append(repr(error))
                    return

        writers = [threading.Thread(target=writer, args=(i * 3000 + 1,)) for i in range(4)]
        watcher = threading.Thread(target=reader)
        watcher.start()
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join()
        done.set()
        watcher.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(stream.pending_deliveries()), 12000)

    def test_the_writers_refuse_malformed_arguments(self):
        for kwargs in ({"delivery_id": "x", "turn_id": "turn-1", "how": "new_turn"},
                       {"delivery_id": "dlv-1", "turn_id": "turn 1", "how": "new_turn"},
                       {"delivery_id": "dlv-1", "turn_id": "turn-1", "how": "later"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.stream.prompt("x", **kwargs)
        with self.assertRaises(ValueError):
            self.stream.prompt("", delivery_id="dlv-1", turn_id="turn-1", how="new_turn")
        for kind in ("new_session", "bash", "prompt"):
            with self.assertRaises(ValueError):
                self.stream.command(kind)
        with self.assertRaises(ValueError):
            self.stream.cancel_dialog("a\nb")
        with self.assertRaises(ValueError):
            self.stream.argv_prompt("x", delivery_id="dlv-1", turn_id="turn-1")
        self.assertEqual(self.stream.pending_deliveries(), [])

    def test_started_accepts_a_new_turn_and_queued_a_mid_turn_input(self):
        t1, _ = self.stream.prompt("a", delivery_id="dlv-1", turn_id="turn-1", how="new_turn")
        t2, _ = self.stream.prompt("b", delivery_id="dlv-2", turn_id="turn-1", how="mid_turn")
        self.assertEqual(self.stream.feed(self.response(t1, data={"disposition": "started"})),
                         [{"type": "input.accepted", "payload": {"delivery_id": "dlv-1", "turn_id": "turn-1",
                                                                  "how": "new_turn"}}])
        self.assertEqual(self.stream.feed(self.response(t2, data={"disposition": "queued"})),
                         [{"type": "input.accepted", "payload": {"delivery_id": "dlv-2", "turn_id": "turn-1",
                                                                  "how": "mid_turn"}}])
        self.assertEqual(self.stream.pending_deliveries(), [])

    def test_how_is_what_pi_says_not_what_was_asked(self):
        # A message written as a new turn that Pi queued (a run was still going) is accepted as mid_turn;
        # the driver's gate then refuses the acceptance, since it does not match the write.
        token, _ = self.stream.prompt("a", delivery_id="dlv-1", turn_id="turn-1", how="new_turn")
        drafts = self.stream.feed(self.response(token, data={"disposition": "queued"}))
        self.assertEqual(drafts[0]["payload"]["how"], "mid_turn")

    def test_a_refused_or_handled_prompt_is_not_accepted(self):
        t1, _ = self.stream.prompt("a", delivery_id="dlv-1", turn_id="turn-1", how="new_turn")
        t2, _ = self.stream.prompt("/x", delivery_id="dlv-2", turn_id="turn-2", how="new_turn")
        t3, _ = self.stream.prompt("c", delivery_id="dlv-3", turn_id="turn-3", how="new_turn")
        self.assertEqual(self.stream.feed(self.response(t1, success=False, error="Agent is already processing")), [])
        self.assertEqual(self.stream.feed(self.response(t2, data={"disposition": "handled"})), [])
        self.assertEqual(self.stream.feed(self.response(t3, data={"disposition": "deferred"})), [])   # a later word
        self.assertEqual(self.stream.signals, [
            {"signal": "refused", "delivery_id": "dlv-1", "error": "Agent is already processing"},
            {"signal": "not_started", "delivery_id": "dlv-2", "disposition": "handled"},
            {"signal": "not_started", "delivery_id": "dlv-3", "disposition": "deferred"}])
        self.assertEqual(self.stream.pending_deliveries(), [])

    def test_a_successful_prompt_answer_without_a_word_for_its_disposition_is_a_protocol_error(self):
        # Pi always answers a prompt's success with data {disposition} (rpc-mode.ts). Anything else is
        # refused before the delivery is taken off the pending list, so its outcome stays unknown.
        for fields in ({"data": {"disposition": []}}, {"data": {"disposition": {}}}, {"data": {"disposition": 7}},
                       {"data": {}}, {"data": "started"}, {}):
            with self.subTest(fields=fields):
                stream = pi.PiStream("rpc", connection_id="api_key:openai-key-1")
                token, _ = stream.prompt("a", delivery_id="dlv-1", turn_id="turn-1", how="new_turn")
                with self.assertRaises(pi.PiProtocolError) as caught:
                    stream.feed(self.response(token, **fields))
                self.assertEqual(caught.exception.code, "response")
                self.assertEqual(stream.pending_deliveries(), ["dlv-1"])
                self.assertEqual(stream.signals, [])

    def test_only_a_response_to_a_token_this_stream_minted_counts_and_only_once(self):
        other = pi.PiStream("rpc", connection_id="api_key:openai-key-1")
        foreign, _ = other.prompt("a", delivery_id="dlv-9", turn_id="turn-9", how="new_turn")
        token, _ = self.stream.prompt("a", delivery_id="dlv-1", turn_id="turn-1", how="new_turn")
        self.assertEqual(foreign, token)   # both mint e8-1: incarnations are told apart by the stream
        started = {"disposition": "started"}
        self.assertEqual(self.stream.feed(self.response("e8-7", data=started)), [])
        self.assertEqual(self.stream.feed(self.response(None, data=started)), [])
        self.assertEqual(self.stream.feed(self.response(token, command="get_state", data=started)), [])
        self.assertEqual(self.stream.pending_deliveries(), ["dlv-1"])     # a different command took nothing
        self.assertEqual(len(self.stream.feed(self.response(token, data=started))), 1)
        self.assertEqual(self.stream.feed(self.response(token, data=started)), [])   # its replay
        self.assertEqual(self.stream.ignored, {"response_unmatched": 4})

    def test_get_state_gives_the_session_id_and_file_and_the_id_does_not_change(self):
        token, _ = self.stream.command("get_state")
        self.stream.feed(self.response(token, command="get_state",
                                       data={"sessionId": f.SESSION_ID, "sessionFile": f.SESSION_FILE}))
        self.assertEqual((self.stream.engine_session_id, self.stream.session_file), (f.SESSION_ID, f.SESSION_FILE))
        token, _ = self.stream.command("get_state")
        self.stream.feed(self.response(token, command="get_state", data={"sessionId": f.SESSION_ID}))
        token, _ = self.stream.command("get_state")
        with self.assertRaises(pi.PiProtocolError) as caught:
            self.stream.feed(self.response(token, command="get_state", data={"sessionId": "another"}))
        self.assertEqual(caught.exception.code, "session_changed")
        self.assertEqual(self.stream.engine_session_id, f.SESSION_ID)

    def test_a_malformed_get_state_answer_leaves_the_command_pending(self):
        # The session id is checked before the command is taken, so a corrected answer under the same token
        # is still read (the run is fenced on the first in practice; the stream does not lose its state).
        token, _ = self.stream.command("get_state")
        for bad in ([], "", "x" * 257, None):
            with self.subTest(bad=bad), self.assertRaises(pi.PiProtocolError) as caught:
                self.stream.feed(self.response(token, command="get_state",
                                               data={"sessionId": bad, "sessionFile": f.SESSION_FILE}))
            self.assertEqual(caught.exception.code, "session_id")
        self.assertIsNone(self.stream.engine_session_id)
        self.stream.feed(self.response(token, command="get_state",
                                       data={"sessionId": f.SESSION_ID, "sessionFile": f.SESSION_FILE}))
        self.assertEqual((self.stream.engine_session_id, self.stream.session_file), (f.SESSION_ID, f.SESSION_FILE))
        self.assertEqual(self.stream.ignored, {})

    def test_an_empty_or_absent_session_file_keeps_the_last_good_path(self):
        token, _ = self.stream.command("get_state")
        self.stream.feed(self.response(token, command="get_state",
                                       data={"sessionId": f.SESSION_ID, "sessionFile": f.SESSION_FILE}))
        for data in ({"sessionId": f.SESSION_ID, "sessionFile": ""}, {"sessionId": f.SESSION_ID, "sessionFile": 7},
                     {"sessionId": f.SESSION_ID}):
            with self.subTest(data=data):
                token, _ = self.stream.command("get_state")
                self.stream.feed(self.response(token, command="get_state", data=data))
                self.assertEqual(self.stream.session_file, f.SESSION_FILE)

    def test_a_failed_get_state_reads_nothing(self):
        token, _ = self.stream.command("get_state")
        self.stream.feed(self.response(token, command="get_state", success=False, data={"sessionId": "x"}))
        self.assertIsNone(self.stream.engine_session_id)
        self.assertEqual(self.stream.signals[-1], {"signal": "response", "command": "get_state", "token": token,
                                                   "success": False})

    def test_a_malformed_response_is_a_protocol_error(self):
        for record in ({"type": "response", "command": "prompt"},
                       {"type": "response", "success": "yes", "command": "x"},
                       {"type": "response", "success": True}):
            with self.subTest(record=record), self.assertRaises(pi.PiProtocolError):
                self.stream.feed(rec(record))

    def test_a_parse_error_is_a_signal(self):
        self.stream.feed(rec({"type": "response", "command": "parse", "success": False, "error": "Failed to parse"}))
        self.assertEqual(self.stream.signals, [{"signal": "parse_error", "error": "Failed to parse"}])

    def test_a_dialog_pi_waits_on_is_a_signal(self):
        self.stream.feed(rec({"type": "extension_ui_request", "id": "u1", "method": "confirm", "title": "Run?"}))
        self.stream.feed(rec({"type": "extension_ui_request", "id": "u2", "method": "notify", "message": "hi"}))
        self.assertEqual(self.stream.signals, [{"signal": "dialog", "id": "u1", "method": "confirm", "waits": True},
                                               {"signal": "dialog", "id": "u2", "method": "notify", "waits": False}])
        with self.assertRaises(pi.PiProtocolError):
            self.stream.feed(rec({"type": "extension_ui_request", "method": "confirm"}))

    def test_a_header_is_a_protocol_error_and_user_replays_accept_nothing(self):
        with self.assertRaises(pi.PiProtocolError):
            self.stream.feed(rec({"type": "session", "id": f.SESSION_ID}))
        token, _ = self.stream.prompt("a", delivery_id="dlv-1", turn_id="turn-1", how="new_turn")
        self.assertEqual(self.stream.feed(rec({"type": "message_end", "message": f.user("a", 1)})), [])
        self.assertEqual(self.stream.pending_deliveries(), ["dlv-1"])

    def test_a_settled_run_drafts_no_result_and_says_how_it_ended(self):
        drafts = drafts_of(self.stream, {"type": "agent_start"}, assistant_end(text="t", stop="aborted"),
                           {"type": "agent_settled", "aborted": True})
        self.assertEqual([d["type"] for d in drafts], ["usage"])
        self.assertEqual(self.stream.signals[-1], {"signal": "settled", "aborted": True, "stop_reason": "aborted",
                                                   "text": "t"})


class ConstructionTest(unittest.TestCase):
    def test_a_stream_needs_a_mode_and_a_connection_id(self):
        for mode, conn in (("text", CONN), ("json", "pi-chatgpt"), ("json", "subscription:"), ("json", "oauth:x"),
                           ("json", "subscription:a\nb"), ("rpc", "api_key:synthetic\rreview"), ("rpc", None)):
            with self.subTest(mode=mode, conn=conn), self.assertRaises(ValueError):
                pi.PiStream(mode, connection_id=conn)

    def test_the_list_of_protocol_error_codes_is_the_list_raised(self):
        import re
        from pathlib import Path
        source = Path(pi.__file__).read_text(encoding="utf-8")
        raised = set(re.findall(r'PiProtocolError\("([a-z_0-9]+)"', source))
        self.assertEqual(raised, set(pi.PROTOCOL_ERRORS))
        self.assertEqual(len(pi.PROTOCOL_ERRORS), len(set(pi.PROTOCOL_ERRORS)))

    def test_the_names_a_registration_will_state(self):
        self.assertEqual((pi.ENGINE, pi.DEFAULT_EXECUTABLE), ("pi", "pi"))
        from recursion_core.routes.engines import CATALOGUE, SEEDED
        self.assertIn(pi.ENGINE, CATALOGUE)
        self.assertNotIn(pi.ENGINE, SEEDED)


if __name__ == "__main__":
    unittest.main()
