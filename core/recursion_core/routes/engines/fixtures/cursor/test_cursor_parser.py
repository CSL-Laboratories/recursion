# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E6, for Lucas.
"""The Cursor stream-json parser (routes/engines/cursor.py): each rule, and the fixtures' expected drafts."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from recursion_core.routes.engines import cursor
from recursion_core.routes.engines.fixtures.cursor import fake_agent

HERE = Path(__file__).resolve().parent
SID = "c0ffee00-0000-4000-8000-0000000000aa"
# SHA-256 of {"path":"README.md"} as RFC 8785 writes it, taken with lane SPINE's conformance/rules.py
# input_digest (an implementation independent of the one the parser uses).
README_DIGEST = "7d6441497d2a000b8143602a7817c90abe7db88e139f89c062a1c36cfe0ad9d6"


def line(**fields) -> str:
    fields.setdefault("session_id", SID)
    return json.dumps(fields)


def started(call_id="call-1", call=None) -> str:
    return line(type="tool_call", subtype="started", call_id=call_id,
                tool_call=call if call is not None else {"readToolCall": {"args": {"path": "README.md"}}})


def completed(call_id="call-1", result=None, key="readToolCall") -> str:
    inner = {"args": {"path": "README.md"}}
    if result is not ...:
        inner["result"] = result if result is not None else {"success": {"content": "text"}}
    return line(type="tool_call", subtype="completed", call_id=call_id, tool_call={key: inner})


def user(text: str, role: str = "user") -> str:
    return line(type="user", message={"role": role, "content": [{"type": "text", "text": text}]})


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FixtureReplayTest(unittest.TestCase):
    def test_every_one_shot_fixture_gives_its_expected_drafts_and_state(self):
        index = json.loads((HERE / "index.json").read_text(encoding="utf-8"))
        names = [f for f in index["fixtures"] if f["drive"] == "oneshot"]
        self.assertEqual(len(names), 6)
        for entry in names:
            with self.subTest(fixture=entry["file"]):
                expected = json.loads((HERE / entry["expected"]).read_text(encoding="utf-8"))
                parser = cursor.StreamParser()
                wrote = expected["wrote"]
                parser.wrote(wrote["delivery_id"], wrote["turn_id"], wrote["text"])
                drafts = []
                for text in fake_agent.fixture_lines(entry["file"]):
                    drafts.extend(parser.parse(text))
                self.assertEqual(drafts, expected["drafts"])
                state = expected["state"]
                self.assertEqual(parser.engine_session_id, state["engine_session_id"])
                self.assertEqual(parser.model, state["model"])
                self.assertEqual(parser.api_key_source, state["api_key_source"])
                self.assertEqual(parser.final_text, state["final_text"])
                self.assertEqual(parser.result_seen, state["result_seen"])
                self.assertEqual(parser.result_code, state["result_code"])
                self.assertEqual({k: v for k, v in parser.counts.items() if v}, state["counts"])


class LineTest(unittest.TestCase):
    def setUp(self):
        self.p = cursor.StreamParser()

    def test_a_line_must_be_text(self):
        with self.assertRaises(TypeError):
            self.p.parse(b"{}")

    def test_what_is_not_one_strict_json_object_gives_nothing(self):
        bad = ["not json", "[1, 2]", '"text"', "",
               '{"type":"result","type":"result","session_id":"%s"}' % SID,          # duplicate keys
               '{"type":"result","session_id":"%s","duration_ms":NaN}' % SID,
               '{"type":"result","session_id":"%s","duration_ms":Infinity}' % SID,
               '{"a":' * 100000 + "1" + "}" * 100000]                                 # too deep to read
        for text in bad:
            self.assertEqual(self.p.parse(text), [])
        self.assertEqual(self.p.counts["not_json"], len(bad))
        self.assertFalse(self.p.result_seen)

    def test_a_trailing_line_end_is_allowed(self):
        self.assertEqual(len(self.p.parse(line(type="result", subtype="success", is_error=False, result="x") + "\n")), 1)

    def test_a_line_without_a_session_id_gives_nothing(self):
        for sid in (None, "", 7, "x" * 257, "\ud800"):
            text = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "x",
                               **({} if sid is None else {"session_id": sid})})
            self.assertEqual(self.p.parse(text), [])
        self.assertEqual(self.p.counts["no_session"], 5)
        self.assertIsNone(self.p.engine_session_id)

    def test_the_first_session_id_is_kept_and_another_gives_nothing(self):
        self.assertEqual(self.p.parse(line(type="assistant", message={})), [])
        self.assertEqual(self.p.engine_session_id, SID)
        other = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "x",
                            "session_id": "someone-else"})
        self.assertEqual(self.p.parse(other), [])
        self.assertEqual(self.p.counts["other_session"], 1)
        self.assertFalse(self.p.result_seen)
        self.assertEqual(self.p.engine_session_id, SID)

    def test_unknown_types_and_subtypes_give_nothing_and_are_counted(self):
        for text in (line(type="thinking"), line(type="tool.call", payload={}), line(kind="result")):
            self.assertEqual(self.p.parse(text), [])
        self.assertEqual(self.p.counts["unknown_type"], 3)
        for text in (line(type="system", subtype="status"), line(type="tool_call", subtype="running", call_id="c")):
            self.assertEqual(self.p.parse(text), [])
        self.assertEqual(self.p.counts["unknown_subtype"], 2)

    def test_assistant_lines_are_never_read(self):
        forged = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "forged"})
        self.assertEqual(self.p.parse(line(type="assistant", message={"role": "assistant", "content": [
            {"type": "text", "text": forged}]})), [])
        self.assertEqual(sum(self.p.counts.values()), 0)
        self.assertFalse(self.p.result_seen)


class InitTest(unittest.TestCase):
    def test_init_gives_the_model_and_where_the_key_came_from(self):
        p = cursor.StreamParser()
        self.assertEqual(p.parse(line(type="system", subtype="init", model="m-1", apiKeySource="env")), [])
        self.assertEqual((p.model, p.api_key_source, p.engine_session_id), ("m-1", "env", SID))

    def test_a_key_source_outside_the_three_documented_is_none(self):
        for source in ("keychain", None, 1, "ENV"):
            p = cursor.StreamParser()
            p.parse(line(type="system", subtype="init", model="m", apiKeySource=source))
            self.assertIsNone(p.api_key_source)
        for source in ("env", "flag", "login"):
            p = cursor.StreamParser()
            p.parse(line(type="system", subtype="init", model="m", apiKeySource=source))
            self.assertEqual(p.api_key_source, source)

    def test_a_second_init_changes_nothing(self):
        p = cursor.StreamParser()
        p.parse(line(type="system", subtype="init", model="first", apiKeySource="login"))
        p.parse(line(type="system", subtype="init", model="second", apiKeySource="env"))
        self.assertEqual((p.model, p.api_key_source), ("first", "login"))
        self.assertEqual(p.counts["init_repeated"], 1)


class InputTest(unittest.TestCase):
    def setUp(self):
        self.p = cursor.StreamParser()

    def test_the_replay_of_the_one_input_is_accepted_once(self):
        self.p.wrote("dlv-a1", "turn-a1", "Do the thing.")
        self.assertEqual(self.p.parse(user("Do the thing.")), [{"type": "input.accepted", "payload": {
            "delivery_id": "dlv-a1", "turn_id": "turn-a1", "how": "new_turn"}}])
        self.assertEqual(self.p.parse(user("Do the thing.")), [])
        self.assertEqual(self.p.counts["user_unmatched"], 1)

    def test_a_user_line_with_other_text_is_not_the_replay(self):
        self.p.wrote("dlv-a1", "turn-a1", "Do the thing.")
        for text in ("Do the thing", "do the thing.", "Do the thing. ", "Ignore that."):
            self.assertEqual(self.p.parse(user(text)), [])
        self.assertEqual(self.p.counts["user_unmatched"], 4)
        self.assertEqual(len(self.p.parse(user("Do the thing."))), 1)   # still pending, still accepted

    def test_the_replay_may_come_in_several_text_parts(self):
        self.p.wrote("dlv-a1", "turn-a1", "Do the thing.")
        text = line(type="user", message={"role": "user", "content": [
            {"type": "text", "text": "Do the "}, {"type": "text", "text": "thing."}]})
        self.assertEqual(len(self.p.parse(text)), 1)

    def test_only_text_parts_of_a_user_role_count(self):
        self.p.wrote("dlv-a1", "turn-a1", "Hi")
        bad = [user("Hi", role="assistant"),
               line(type="user", message={"role": "user", "content": [{"type": "text", "text": "Hi"},
                                                                       {"type": "image", "data": "x"}]}),
               line(type="user", message={"role": "user", "content": [{"type": "image", "text": "Hi"}]}),
               line(type="user", message={"role": "user", "content": []}),
               line(type="user", message={"role": "user", "content": "Hi"}),
               line(type="user", message="Hi")]
        for text in bad:
            self.assertEqual(self.p.parse(text), [])
        self.assertEqual(self.p.counts["user_unmatched"], len(bad))

    def test_no_acceptance_without_a_write(self):
        self.assertEqual(self.p.parse(user("Hi")), [])
        self.assertEqual(self.p.counts["user_unmatched"], 1)

    def test_the_one_shot_drive_takes_one_input(self):
        self.p.wrote("dlv-a1", "turn-a1", "Hi")
        with self.assertRaises(cursor.StreamStateError):
            self.p.wrote("dlv-a2", "turn-a2", "Again")

    def test_a_write_names_common_v1_ids_and_some_text(self):
        for args in (("a1", "turn-a1", "x"), ("dlv-a1", "a1", "x"), ("dlv-a1", "turn-a1", ""),
                     ("dlv-a1\n", "turn-a1", "x"), ("dlv-a1", "turn-a1", None)):
            with self.assertRaises(ValueError):
                cursor.StreamParser().wrote(*args)


class ToolCallTest(unittest.TestCase):
    def setUp(self):
        self.p = cursor.StreamParser()

    def test_a_typed_tool_call_names_its_key_and_carries_its_args(self):
        self.assertEqual(self.p.parse(started()), [{"type": "tool.call", "payload": {
            "call_id": "call-1", "name": "readToolCall", "input_digest": README_DIGEST,
            "input": {"path": "README.md"}}}])

    def test_the_digest_is_of_rfc_8785_text_so_key_order_does_not_matter(self):
        a = self.p.parse(started("c-a", {"x": {"args": {"b": 1, "a": [1.0, "\u00e9"]}}}))[0]["payload"]
        b = self.p.parse(started("c-b", {"x": {"args": {"a": [1, "\u00e9"], "b": 1}}}))[0]["payload"]
        self.assertEqual(a["input_digest"], b["input_digest"])
        self.assertEqual(a["input_digest"], sha('{"a":[1,"\u00e9"],"b":1}'))

    def test_a_function_call_carries_its_decoded_arguments(self):
        call = {"function": {"name": "list_dir", "arguments": '{"path": ".", "depth": 1}'}}
        payload = self.p.parse(started("c-f", call))[0]["payload"]
        self.assertEqual((payload["name"], payload["input"]), ("list_dir", {"path": ".", "depth": 1}))
        self.assertEqual(payload["input_digest"], sha('{"depth":1,"path":"."}'))

    def test_function_arguments_that_are_already_an_object_are_carried(self):
        call = {"function": {"name": "f", "arguments": {"k": "v"}}}
        self.assertEqual(self.p.parse(started("c-o", call))[0]["payload"]["input"], {"k": "v"})

    def test_function_arguments_that_are_not_one_object_are_digested_as_given_and_not_carried(self):
        for i, raw in enumerate(("[1, 2]", "not json", '{"a":1,"a":2}')):
            payload = self.p.parse(started(f"c-{i}", {"function": {"name": "f", "arguments": raw}}))[0]["payload"]
            self.assertIsNone(payload["input"])
            self.assertEqual(payload["input_digest"], sha(json.dumps(raw)))

    def test_shapes_other_than_the_two_documented_give_nothing(self):
        bad = [{}, {"readToolCall": {"args": {}}, "writeToolCall": {"args": {}}}, {"readToolCall": "x"},
               {"readToolCall": {"path": "a"}}, {"readToolCall": {"args": ["a"]}},
               {"function": {"name": "f"}}, {"function": {"name": "f", "arguments": 3}}]
        for i, call in enumerate(bad):
            self.assertEqual(self.p.parse(started(f"c-{i}", call)), [])
        self.assertEqual(self.p.parse(line(type="tool_call", subtype="started", call_id="c-x", tool_call=[1])), [])
        self.assertEqual(self.p.counts["tool_shape_unknown"], len(bad) + 1)

    def test_call_ids_and_names_event_v1_refuses_give_nothing(self):
        for call_id in ("", "x" * 129, "a\nb", "a\rb", 7, None, "\udc00"):
            self.assertEqual(self.p.parse(started(call_id)), [])
        for name in ("", "n" * 257, "a\nb", 5):
            self.assertEqual(self.p.parse(started("c-n", {"function": {"name": name, "arguments": "{}"}})), [])
        self.assertEqual(self.p.counts["call_invalid"], 11)
        self.assertEqual(len(self.p.parse(started("x" * 128))), 1)
        self.assertEqual(len(self.p.parse(started("c-256", {"n" * 256: {"args": {}}}))), 1)

    def test_a_second_start_of_one_call_gives_nothing(self):
        self.assertEqual(len(self.p.parse(started())), 1)
        self.assertEqual(self.p.parse(started()), [])
        self.assertEqual(self.p.counts["call_repeated"], 1)

    def test_an_input_with_no_rfc_8785_text_gives_no_call_and_no_result(self):
        for i, args in enumerate(({"n": 9007199254740993}, {"s": "\ud800"}, {"n": 2.0 ** 60})):
            self.assertEqual(self.p.parse(started(f"c-{i}", {"readToolCall": {"args": args}})), [])
            self.assertEqual(self.p.parse(completed(f"c-{i}")), [])
        self.assertEqual(self.p.counts["call_undigestible"], 3)
        self.assertEqual(self.p.counts["result_unpaired"], 3)

    def test_an_input_over_the_bound_is_not_carried_but_still_digested(self):
        args = {"fileText": "x" * cursor.MAX_INPUT_BYTES}
        payload = self.p.parse(started("c-big", {"writeToolCall": {"args": args}}))[0]["payload"]
        self.assertIsNone(payload["input"])
        self.assertEqual(payload["input_digest"], sha('{"fileText":"' + "x" * cursor.MAX_INPUT_BYTES + '"}'))
        self.assertEqual(self.p.counts["input_withheld"], 1)
        small = {"fileText": "x" * (cursor.MAX_INPUT_BYTES - len('{"fileText":""}'))}
        self.assertEqual(self.p.parse(started("c-edge", {"writeToolCall": {"args": small}}))[0]["payload"]["input"], small)


class ToolResultTest(unittest.TestCase):
    def setUp(self):
        self.p = cursor.StreamParser()
        self.p.parse(started())

    def result(self, result=None, call_id="call-1", key="readToolCall"):
        drafts = self.p.parse(completed(call_id, result, key))
        return drafts[0]["payload"] if drafts else None

    def test_content_is_the_text_of_a_success(self):
        self.assertEqual(self.result({"success": {"content": "hello", "exceededLimit": False}}), {
            "call_id": "call-1", "status": "ok", "text": "hello", "output_ref": None, "truncated": False})

    def test_a_result_with_no_content_is_written_as_its_rfc_8785_text(self):
        self.assertEqual(self.result({"success": {"path": "N.md", "linesCreated": 1, "fileSize": 26}})["text"],
                         '{"fileSize":26,"linesCreated":1,"path":"N.md"}')

    def test_an_error_result_is_status_error(self):
        payload = self.result({"error": {"errorMessage": "File not found"}})
        self.assertEqual((payload["status"], payload["text"]), ("error", '{"errorMessage":"File not found"}'))

    def test_a_result_the_tool_cut_is_truncated(self):
        self.assertTrue(self.result({"success": {"content": "partial", "exceededLimit": True}})["truncated"])

    def test_a_text_over_the_bound_is_cut_on_a_character_boundary(self):
        text = "a" + "\u00e9" * cursor.MAX_RESULT_BYTES          # two bytes each: the cut falls inside one
        payload = self.result({"success": {"content": text}})
        self.assertTrue(payload["truncated"])
        data = payload["text"].encode("utf-8")
        self.assertLessEqual(len(data), cursor.MAX_RESULT_BYTES)
        self.assertEqual(len(data), cursor.MAX_RESULT_BYTES - 1)
        self.assertTrue(text.startswith(payload["text"]))

    def test_a_text_at_the_bound_is_not_cut(self):
        payload = self.result({"success": {"content": "z" * cursor.MAX_RESULT_BYTES}})
        self.assertFalse(payload["truncated"])
        self.assertEqual(len(payload["text"]), cursor.MAX_RESULT_BYTES)

    def test_a_text_with_a_lone_surrogate_gives_nothing(self):
        self.assertIsNone(self.result({"success": {"content": "bad \ud800"}}))
        self.assertEqual(self.p.counts["text_invalid"], 1)

    def test_a_body_with_no_rfc_8785_text_gives_nothing(self):
        self.assertIsNone(self.result({"success": {"size": 9007199254740993}}))
        self.assertEqual(self.p.counts["text_invalid"], 1)

    def test_result_shapes_other_than_success_or_error_give_nothing(self):
        for result in ({"rejected": {}}, {"success": {}, "error": {}}, {"success": "ok"}, {}, "ok", ...):
            self.assertIsNone(self.result(result))
        self.assertEqual(self.p.counts["tool_shape_unknown"], 6)

    def test_a_result_with_no_start_or_another_tool_gives_nothing(self):
        self.assertIsNone(self.result(call_id="call-unknown"))
        self.assertIsNone(self.result(key="writeToolCall"))
        self.assertEqual(self.p.counts["result_unpaired"], 2)
        self.assertIsNotNone(self.result())

    def test_a_second_result_of_one_call_gives_nothing(self):
        self.assertIsNotNone(self.result())
        self.assertIsNone(self.result())
        self.assertEqual(self.p.counts["call_repeated"], 1)


class ResultTest(unittest.TestCase):
    def setUp(self):
        self.p = cursor.StreamParser()

    def test_success_has_no_code_and_keeps_the_answer_out_of_the_event(self):
        drafts = self.p.parse(line(type="result", subtype="success", is_error=False, result="The answer."))
        self.assertEqual(drafts, [{"type": "result", "payload": {"code": None, "detail": ""}}])
        self.assertEqual((self.p.final_text, self.p.result_code, self.p.result_seen), ("The answer.", None, True))

    def test_an_error_is_engine_error_and_its_text_is_never_read(self):
        drafts = self.p.parse(line(type="result", subtype="error", is_error=True,
                                   result="Too many requests: usage limit reached"))
        self.assertEqual(drafts, [{"type": "result", "payload": {"code": "engine_error", "detail": "subtype:error"}}])
        self.assertIsNone(self.p.final_text)
        self.assertEqual(sum(self.p.counts.values()), 0)

    def test_an_error_subtype_that_is_not_a_code_word_is_not_copied(self):
        for subtype in ("Rate Limited", "x" * 65, None, "a\nb"):
            p = cursor.StreamParser()
            self.assertEqual(p.parse(line(type="result", subtype=subtype, is_error=True))[0]["payload"]["detail"],
                             "is_error")

    def test_a_result_line_that_is_neither_success_nor_error_is_a_protocol_error(self):
        for fields in ({"subtype": "success"}, {"subtype": "success", "is_error": "false"},
                       {"subtype": "cancelled", "is_error": False}, {"is_error": False}, {"subtype": "other"}):
            p = cursor.StreamParser()
            self.assertEqual(p.parse(line(type="result", **fields)), [{"type": "result", "payload": {
                "code": "protocol_error", "detail": "result line not understood"}}])
            self.assertIsNone(p.final_text)

    def test_a_success_answer_that_is_not_text_is_empty(self):
        self.p.parse(line(type="result", subtype="success", is_error=False, result=["x"]))
        self.assertEqual(self.p.final_text, "")

    def test_only_the_first_result_counts(self):
        self.p.parse(line(type="result", subtype="success", is_error=False, result="first"))
        self.assertEqual(self.p.parse(line(type="result", subtype="error", is_error=True)), [])
        self.assertEqual((self.p.final_text, self.p.result_code), ("first", None))
        self.assertEqual(self.p.counts["result_repeated"], 1)


class NeverDraftedTest(unittest.TestCase):
    def test_no_line_of_any_fixture_drafts_output_usage_or_limit(self):
        for name in ("hello", "tools", "error", "no_result", "partial", "hostile"):
            p = cursor.StreamParser()
            for text in fake_agent.fixture_lines(f"oneshot/{name}.jsonl"):
                for draft in p.parse(text):
                    self.assertIn(draft["type"], ("tool.call", "tool.result", "result", "input.accepted"))


if __name__ == "__main__":
    unittest.main()
