# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E6, for Lucas.
"""The fixtures themselves, and the parser's output held to lane SPINE's published schemas.

Reads ``schemas/control/v1/driver.v2.json``, ``event.v1.json`` and ``common.v1.json`` (lane SPINE's, #368)
with the standard library only: the core's interpreter has no JSON Schema library, so these tests take the
keys, enums and patterns a draft must have straight from the schema files, and a change there shows here.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from recursion_core.routes.engines import cursor
from recursion_core.routes.engines.fixtures.cursor import fake_agent

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[6]
SCHEMAS = ROOT / "schemas" / "control" / "v1"
INDEX = json.loads((HERE / "index.json").read_text(encoding="utf-8"))
MODEL = "Claude Opus 5.5 (claude-opus-5-5), lane ENGINE-E6"
OWN = {"README.md", "index.json", "fake_agent.py"}


def schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def files_here() -> set:
    out = set()
    for path in HERE.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            out.add(path.relative_to(HERE).as_posix())
    return out


class ManifestTest(unittest.TestCase):
    def test_every_fixture_file_is_listed_and_every_listed_file_exists(self):
        listed = {f["file"] for f in INDEX["fixtures"]} | {f["expected"] for f in INDEX["fixtures"] if f["expected"]}
        data = {p for p in files_here() if p not in OWN and not re.fullmatch(r"test_cursor_\w+\.py", p)}
        self.assertEqual(data, listed)

    def test_nothing_is_captured_yet_and_every_entry_says_where_it_comes_from(self):
        self.assertEqual(INDEX["written_by"], MODEL)
        for entry in INDEX["fixtures"]:
            self.assertIn(entry["provenance"], ("synthetic", "lane rule"))
            self.assertIn(entry["drive"], ("oneshot", "acp", "both"))
            self.assertIsInstance(entry["guessed"], list)

    def test_every_stream_fixture_opens_with_its_synthetic_header(self):
        for entry in INDEX["fixtures"]:
            if not entry["file"].endswith(".jsonl"):
                continue
            with self.subTest(fixture=entry["file"]):
                header = fake_agent.fixture_header(entry["file"])
                self.assertEqual(header["written_by"], MODEL)
                self.assertEqual(header["provenance"], "synthetic")
                self.assertEqual(header.get("guessed", []), entry["guessed"])
                self.assertTrue(header.get("shapes_from"))

    def test_every_json_file_names_its_writer_first(self):
        for name in sorted(p for p in files_here() if p.endswith(".json")):
            with self.subTest(file=name):
                value = json.loads((HERE / name).read_text(encoding="utf-8"))
                self.assertEqual(next(iter(value)), "written_by")
                self.assertEqual(value["written_by"], MODEL)

    def test_every_python_and_markdown_file_names_its_writer_on_its_first_line(self):
        for name in sorted(p for p in files_here() if p.endswith((".py", ".md"))):
            first = (HERE / name).read_text(encoding="utf-8").splitlines()[0]
            self.assertIn("Claude Opus 5.5", first, name)
        first = Path(cursor.__file__).read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("Claude Opus 5.5", first)

    def test_every_stream_line_but_the_hostile_fixture_is_one_json_object(self):
        for entry in INDEX["fixtures"]:
            if not entry["file"].endswith(".jsonl") or entry["file"] == "oneshot/hostile.jsonl":
                continue
            for text in fake_agent.fixture_lines(entry["file"]):
                self.assertIsInstance(json.loads(text), dict)


class DraftSchemaTest(unittest.TestCase):
    """Every expected draft has exactly the shape event.v1 gives its type, as driver.v2 allows it."""

    @classmethod
    def setUpClass(cls):
        cls.driver, cls.event, cls.common = schema("driver.v2.json"), schema("event.v1.json"), schema("common.v1.json")
        cls.draft_types = cls.driver["$defs"]["event_draft"]["properties"]["type"]["enum"]

    def drafts(self):
        for entry in INDEX["fixtures"]:
            if entry["expected"]:
                yield from json.loads((HERE / entry["expected"]).read_text(encoding="utf-8"))["drafts"]

    def pattern(self, name: str) -> str:
        return self.common["$defs"][name]["pattern"]

    def test_the_schemas_are_the_pinned_drafts(self):
        self.assertEqual(self.driver["x-version"], "1.0-draft.4")
        self.assertEqual(self.draft_types, ["engine.output", "tool.call", "tool.result", "usage", "limit", "result",
                                            "input.accepted"])

    def test_every_draft_is_a_type_a_driver_may_draft_with_exactly_its_keys(self):
        seen = set()
        for draft in self.drafts():
            self.assertEqual(set(draft), {"type", "payload"})
            self.assertIn(draft["type"], self.draft_types)
            definition = self.event["$defs"][draft["type"]]
            self.assertFalse(definition["additionalProperties"])
            self.assertEqual(set(draft["payload"]), set(definition["required"]))
            self.assertEqual(set(definition["properties"]), set(definition["required"]))
            seen.add(draft["type"])
        self.assertEqual(seen, {"tool.call", "tool.result", "result", "input.accepted"})

    def test_tool_call_payloads(self):
        props = self.event["$defs"]["tool.call"]["properties"]
        for draft in self.drafts():
            if draft["type"] != "tool.call":
                continue
            p = draft["payload"]
            self.assertLessEqual(len(p["call_id"]), props["call_id"]["maxLength"])
            self.assertLessEqual(len(p["name"]), props["name"]["maxLength"])
            self.assertRegex(p["input_digest"], self.pattern("sha256"))
            self.assertTrue(p["input"] is None or isinstance(p["input"], dict))

    def test_tool_result_payloads(self):
        props = self.event["$defs"]["tool.result"]["properties"]
        for draft in self.drafts():
            if draft["type"] != "tool.result":
                continue
            p = draft["payload"]
            self.assertIn(p["status"], props["status"]["enum"])
            self.assertIsInstance(p["text"], str)      # exactly one of text and output_ref is not null
            self.assertIsNone(p["output_ref"])
            self.assertIsInstance(p["truncated"], bool)

    def test_result_and_acceptance_payloads(self):
        for draft in self.drafts():
            p = draft["payload"]
            if draft["type"] == "result":
                self.assertTrue(p["code"] is None or re.search(self.pattern("code_word"), p["code"]))
                self.assertIsInstance(p["detail"], str)
            elif draft["type"] == "input.accepted":
                self.assertRegex(p["delivery_id"], self.pattern("delivery_id"))
                self.assertRegex(p["turn_id"], self.pattern("turn_id"))
                self.assertIn(p["how"], self.event["$defs"]["input.accepted"]["properties"]["how"]["enum"])


class DeclarationTest(unittest.TestCase):
    def test_the_one_shot_declaration_has_driver_v2s_shape_and_every_flag_false(self):
        driver = schema("driver.v2.json")
        record = cursor.oneshot_declaration()
        self.assertEqual(set(record), set(driver["required"]))
        self.assertEqual(record["driver"], driver["properties"]["driver"]["const"])
        self.assertRegex(record["engine"], schema("common.v1.json")["$defs"]["engine_name"]["pattern"])
        self.assertEqual(set(record["flags"]), set(driver["$defs"]["flags"]["required"]))
        self.assertEqual(set(record["flags"].values()), {False})
        self.assertIs(record["input_ack"], False)

    def test_no_flag_promises_what_is_not_built(self):
        flags = cursor.oneshot_declaration()["flags"]
        self.assertFalse(flags["typed_limits"])   # no oracle for a Cursor limit
        self.assertFalse(flags["usage"])          # Cursor documents no usage line
        self.assertFalse(flags["resume"])         # --resume exists, but resuming is not built


class AcpTranscriptTest(unittest.TestCase):
    def transcripts(self):
        for entry in INDEX["fixtures"]:
            if entry["drive"] == "acp":
                yield entry["file"], [json.loads(t) for t in fake_agent.fixture_lines(entry["file"])]

    def test_every_message_is_json_rpc_2(self):
        for name, entries in self.transcripts():
            for item in entries:
                self.assertEqual(set(item), {"dir", "msg"}, name)
                self.assertIn(item["dir"], ("client", "agent"))
                self.assertEqual(item["msg"]["jsonrpc"], "2.0")

    def test_every_request_is_answered_once_by_the_other_side_after_it(self):
        for name, entries in self.transcripts():
            open_requests = {}
            for item in entries:
                msg, side = item["msg"], item["dir"]
                if "method" in msg and "id" in msg:
                    self.assertNotIn((side, msg["id"]), open_requests, name)
                    open_requests[(side, msg["id"])] = msg["method"]
                elif "method" not in msg:
                    asker = "agent" if side == "client" else "client"
                    self.assertIn((asker, msg["id"]), open_requests, name)
                    del open_requests[(asker, msg["id"])]
                    self.assertEqual(len({"result", "error"} & set(msg)), 1)
            self.assertEqual(open_requests, {}, name)

    def test_every_drive_starts_with_initialize_and_uses_one_session(self):
        for name, entries in self.transcripts():
            self.assertEqual(entries[0]["msg"]["method"], "initialize", name)
            sessions = {item["msg"]["params"]["sessionId"] for item in entries
                        if isinstance(item["msg"].get("params"), dict) and "sessionId" in item["msg"]["params"]}
            self.assertEqual(len(sessions), 1, name)


if __name__ == "__main__":
    unittest.main()
