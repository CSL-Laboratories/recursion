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
        self.assertEqual(record["engine"], "cursor")   # review 1 of #388 (Sol R5): this engine, not any name
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

    def test_the_acp_schema_revision_is_named(self):
        pin = INDEX["acp_schema"]
        self.assertEqual(pin["commit"], "13b765828f961812f0c78ae577220e994209fa2f")
        self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
        for name, _ in self.transcripts():
            self.assertIn(pin["commit"], fake_agent.fixture_header(name)["shapes_from"], name)

    def test_every_body_has_its_methods_shape_in_acp_v1(self):
        """Review 1 of #388 (Astra R3, Sol R4): each request, notification and answer, by method, by hand.

        The required fields and the closed values are those of the ACP v1 schema at the revision
        ``index.json`` names (``acp_schema``); the core's interpreter has no JSON Schema library.
        """
        problems = []
        for name, entries in self.transcripts():
            asked = {}   # (side, id) -> (method, params)
            for n, item in enumerate(entries, 2):   # line numbers in the file, after its header
                msg, side = item["msg"], item["dir"]
                where = f"{name}:{n}"
                if "method" in msg:
                    problems += [f"{where} {p}" for p in acp_params_problems(side, msg["method"], msg.get("params"))]
                    if "id" in msg:
                        asked[(side, msg["id"])] = (msg["method"], msg.get("params"))
                else:
                    method, params = asked.pop(("agent" if side == "client" else "client", msg["id"]))
                    problems += [f"{where} {p}" for p in acp_answer_problems(method, params, msg.get("result"))]
        self.assertEqual(problems, [])

    def test_the_shape_check_refuses_what_the_reads_found(self):
        self.assertEqual(acp_answer_problems("session/load", {}, None), ["session/load answer is not an object"])
        self.assertEqual(acp_answer_problems("session/load", {}, "ok"), ["session/load answer is not an object"])
        offered = {"options": [{"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]}
        self.assertEqual(acp_answer_problems("session/request_permission", offered,
                                             {"outcome": {"outcome": "selected", "optionId": "allow-always"}}),
                         ["the option chosen was not offered"])
        self.assertEqual(acp_answer_problems("session/prompt", {}, {"stopReason": "done"}), ["stopReason not in ACP v1"])

    def test_each_shape_rule_refuses_its_own_break(self):
        """Review 2 of #388, change 3 (Astra): every rule of the shape check has a case that only it refuses.

        Q01 to Q22 are the read's own proposed cases (review-2-388-gpt-6-astra-evidence/shape_rule_probes.py);
        the rest cover the content and session-state rules this answer adds. True: a problem is reported.
        """
        perm = {"sessionId": "s", "toolCall": {"toolCallId": "c"},
                "options": [{"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}
        prompt = lambda *blocks: ("params", ("client", "session/prompt", {"sessionId": "s", "prompt": list(blocks)}))  # noqa: E731
        chunk = lambda content: ("params", ("agent", "session/update", {"sessionId": "s", "update": {  # noqa: E731
            "sessionUpdate": "agent_message_chunk", "content": content}}))
        cases = {
            "Q01 method side": (("params", ("agent", "initialize", {"protocolVersion": 1})), True),
            "Q02 params object": (("params", ("client", "session/cancel", [])), True),
            "Q03 request version": (("params", ("client", "initialize", {"protocolVersion": True})), True),
            "Q04 auth method": (("params", ("client", "authenticate", {"methodId": False})), True),
            "Q05 cwd": (("params", ("client", "session/new", {"cwd": "relative", "mcpServers": []})), True),
            "Q06 mcp list": (("params", ("client", "session/new", {"cwd": "/work", "mcpServers": {}})), True),
            "Q07 session id": (("params", ("client", "session/cancel", {"sessionId": ""})), True),
            "Q08 prompt blocks": (("params", ("client", "session/prompt", {"sessionId": "s", "prompt": {}})), True),
            "Q09 update kind": (("params", ("agent", "session/update", {"sessionId": "s", "update": {
                "sessionUpdate": "not-an-update"}})), True),
            "Q10 call title": (("params", ("agent", "session/update", {"sessionId": "s", "update": {
                "sessionUpdate": "tool_call", "toolCallId": "c"}})), True),
            "Q11 update call id": (("params", ("agent", "session/update", {"sessionId": "s", "update": {
                "sessionUpdate": "tool_call_update"}})), True),
            "Q12 chunk content": (("params", ("agent", "session/update", {"sessionId": "s", "update": {
                "sessionUpdate": "agent_message_chunk"}})), True),
            "Q13 permission call": (("params", ("agent", "session/request_permission", {**perm, "toolCall": {}})), True),
            "Q14 permission options": (("params", ("agent", "session/request_permission", {**perm, "options": [
                {"optionId": "x", "name": "X", "kind": "not-a-kind"}]})), True),
            "Q15 answer object": (("answer", ("session/load", {}, None)), True),
            "Q16 answer version": (("answer", ("initialize", {}, {"protocolVersion": True})), True),
            "Q17 auth methods": (("answer", ("initialize", {}, {"protocolVersion": 1, "authMethods": [{"id": "x"}]})), True),
            "Q18 new session id": (("answer", ("session/new", {}, {})), True),
            "Q19 stop reason": (("answer", ("session/prompt", {}, {"stopReason": "not-a-stop"})), True),
            "Q20 cancelled outcome": (("answer", ("session/request_permission", {}, {"outcome": {"outcome": "cancelled"}})), False),
            "Q21 selected shape": (("answer", ("session/request_permission", {"options": [{"optionId": "x"}]},
                                               {"outcome": {"outcome": "unexpected", "optionId": "x"}})), True),
            "Q22 offered option": (("answer", ("session/request_permission", {"options": [{"optionId": "x"}]},
                                               {"outcome": {"outcome": "selected", "optionId": "y"}})), True),
            "empty prompt": (prompt(), True),
            "text prompt": (prompt({"type": "text", "text": "Hi"}), False),
            "text content needs its text": (prompt({"type": "text"}), True),
            "content of no known type": (prompt({"type": "video", "data": "x"}), True),
            "image needs data and mimeType": (prompt({"type": "image", "data": "x"}), True),
            "image needs its data": (prompt({"type": "image", "mimeType": "image/png"}), True),
            "image with both": (prompt({"type": "image", "data": "x", "mimeType": "image/png"}), False),
            "audio needs its data": (prompt({"type": "audio", "mimeType": "audio/wav"}), True),
            "audio needs its mimeType": (prompt({"type": "audio", "data": ""}), True),
            "audio with both": (prompt({"type": "audio", "data": "", "mimeType": "audio/wav"}), False),
            "resource link needs name and uri": (prompt({"type": "resource_link", "name": "n"}), True),
            "resource link needs its name": (prompt({"type": "resource_link", "uri": "file:///a"}), True),
            "resource link with both": (prompt({"type": "resource_link", "name": "n", "uri": "file:///a"}), False),
            "resource needs its object": (prompt({"type": "resource", "resource": "x"}), True),
            "resource with a uri only": (prompt({"type": "resource", "resource": {"uri": "file:///a"}}), True),
            "resource with uri and text": (prompt({"type": "resource", "resource": {"uri": "file:///a", "text": "x"}}), False),
            "resource with uri and blob": (prompt({"type": "resource", "resource": {"uri": "file:///a", "blob": ""}}), False),
            "chunk text must be text": (chunk({"type": "text", "text": 7}), True),
            "chunk text": (chunk({"type": "text", "text": "Hello"}), False),
            "load modes not an object": (("answer", ("session/load", {}, {"modes": "not-an-object"})), True),
            "load modes need currentModeId": (("answer", ("session/load", {}, {"modes": {"availableModes": []}})), True),
            "load modes need a list": (("answer", ("session/load", {}, {"modes": {
                "currentModeId": "a", "availableModes": {}}})), True),
            "load mode without an id": (("answer", ("session/load", {}, {"modes": {
                "currentModeId": "a", "availableModes": [{"name": "A"}]}})), True),
            "load mode without a name": (("answer", ("session/load", {}, {"modes": {
                "currentModeId": "agent", "availableModes": [{"id": "agent"}]}})), True),
            "load modes": (("answer", ("session/load", {}, {"modes": {
                "currentModeId": "agent", "availableModes": [{"id": "agent", "name": "Agent"}]}})), False),
            "load modes null": (("answer", ("session/load", {}, {"modes": None})), False),
            "new config options not a list": (("answer", ("session/new", {}, {"sessionId": "s", "configOptions": {}})), True),
            "new config options null": (("answer", ("session/new", {}, {"sessionId": "s", "configOptions": None})), False),
            "capabilities not an object": (("answer", ("initialize", {}, {"protocolVersion": 1, "agentCapabilities": []})), True),
            "initialize with nothing optional": (("answer", ("initialize", {}, {"protocolVersion": 1})), False),
        }
        for name, ((kind, args), expected) in cases.items():
            with self.subTest(case=name):
                check = acp_params_problems if kind == "params" else acp_answer_problems
                self.assertEqual(bool(check(*args)), expected, check(*args))

    def test_each_content_field_is_required_with_its_type(self):
        """Review 3 of #388 (Sol change 2, Astra change 4): each field of each content variant, missing, a
        number or null, is refused, and so is each field of an embedded resource's contents."""
        missing = object()
        blocks = {"text": {"type": "text", "text": "x"},
                  "image": {"type": "image", "data": "x", "mimeType": "image/png"},
                  "audio": {"type": "audio", "data": "x", "mimeType": "audio/wav"},
                  "resource_link": {"type": "resource_link", "name": "n", "uri": "file:///a"},
                  "resource": {"type": "resource", "resource": {"uri": "file:///a", "text": "x"}}}
        nested = {"text resource": {"uri": "file:///a", "text": "x"},
                  "blob resource": {"uri": "file:///a", "blob": "eA=="}}
        for kind, block in [*blocks.items(), *(
                (name, {"type": "resource", "resource": contents}) for name, contents in nested.items())]:
            self.assertEqual(content_problems(block), [], kind)
            fields = [(key,) for key in block] + [("resource", key) for key in block.get("resource", {})]
            for path in fields:
                for value in (missing, 7, None):
                    bad = json.loads(json.dumps(block))
                    holder = bad if len(path) == 1 else bad["resource"]
                    if value is missing:
                        del holder[path[-1]]
                    else:
                        holder[path[-1]] = value
                    with self.subTest(kind=kind, field=".".join(path), value=value):
                        self.assertTrue(content_problems(bad))

    def test_embedded_resources_are_checked_whole(self):
        """Review 3 of #388 (Astra change 1, Sol change 1): an embedded resource is TextResourceContents or
        BlobResourceContents, ``uri`` with ``text`` or ``blob``, each a string; the empty string is one."""
        def prompt(resource):
            block = {"type": "resource", "resource": resource}
            return "client", "session/prompt", {"sessionId": "s", "prompt": [block]}
        for resource in ({}, {"uri": "file:///a"}, {"uri": 7, "text": "x"}, {"uri": 9, "text": "x"},
                         {"uri": "file:///a", "text": 7}, {"uri": "file:///a", "blob": False}, {"text": "x"},
                         {"blob": "x"}, "x", None, []):
            with self.subTest(resource=resource):
                self.assertTrue(acp_params_problems(*prompt(resource)))
        for resource in ({"uri": "file:///a", "text": ""}, {"uri": "file:///a", "blob": ""},
                         {"uri": "", "blob": "eA=="}, {"uri": "file:///a", "text": "x", "mimeType": "text/plain"}):
            with self.subTest(resource=resource):
                self.assertEqual(acp_params_problems(*prompt(resource)), [])

    def test_config_options_are_checked_item_by_item(self):
        """Review 3 of #388 (Astra change 2, Sol change 1): each supplied SessionConfigOption, in a session/new
        and a session/load answer alike, by its ``type``."""
        boolean = {"id": "x", "name": "X", "type": "boolean", "currentValue": False}
        select = {"id": "m", "name": "Model", "type": "select", "currentValue": "a",
                  "options": [{"value": "a", "name": "A"}]}
        group = {"group": "g", "name": "G", "options": [{"value": "a", "name": "A"}]}
        grouped = {**select, "options": [group]}
        untyped = {key: value for key, value in boolean.items() if key != "type"}
        good = (boolean, {**boolean, "currentValue": True}, select, grouped, {**select, "options": []},
                {**grouped, "options": [{**group, "options": []}]}, {**boolean, "category": "model"},
                {**boolean, "category": None}, {**boolean, "category": "a-category-of-its-own"})
        bad = (False, 1, {}, {"id": "x"}, {**boolean, "id": 7}, {**boolean, "name": 7}, {**boolean, "category": 7},
               {**boolean, "type": "toggle"}, untyped, {**boolean, "currentValue": 1}, {**boolean, "currentValue": 0},
               {**boolean, "currentValue": "true"}, {**boolean, "currentValue": None},
               {**select, "currentValue": 7}, {**select, "options": {}}, {**select, "options": None},
               {**select, "options": [False]}, {**select, "options": [{"value": "a"}]},
               {**select, "options": [{"name": "A"}]}, {**select, "options": [{"value": 7, "name": "A"}]},
               {**grouped, "options": [{**group, "group": 7}]}, {**grouped, "options": [{**group, "name": 7}]},
               {**grouped, "options": [{"name": "G", "options": []}]},
               {**grouped, "options": [{**group, "options": {}}]},
               {**grouped, "options": [{**group, "options": [{"value": "a"}]}]},
               {**select, "options": [{"value": "a", "name": "A"}, group]})
        for method, answer in (("session/new", {"sessionId": "s"}), ("session/load", {})):
            for option in good:
                with self.subTest(method=method, option=option):
                    self.assertEqual(acp_answer_problems(method, {}, {**answer, "configOptions": [option]}), [])
            for option in bad:
                with self.subTest(method=method, option=option):
                    self.assertTrue(acp_answer_problems(method, {}, {**answer, "configOptions": [option]}))
            with self.subTest(method=method, case="a bad option after a good one"):
                self.assertTrue(acp_answer_problems(method, {}, {**answer, "configOptions": [boolean, {}]}))

    def test_capabilities_are_checked_field_by_field(self):
        """Review 3 of #388 (Astra change 3, Sol change 1): AgentCapabilities' supplied fields, and each nested
        capability object's own fields, one level deep. The fields below are copied from the pinned schema on
        purpose, so that dropping one from the check's tables fails here."""
        def problems(caps):
            return acp_answer_problems("initialize", {}, {"protocolVersion": 1, "agentCapabilities": caps})
        flags = {"promptCapabilities": ("image", "audio", "embeddedContext"), "mcpCapabilities": ("http", "sse")}
        objects = {"sessionCapabilities": ("list", "delete", "additionalDirectories", "systemPrompt", "resume",
                                           "close"),
                   "auth": ("logout",)}
        good = {"loadSession": False, "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                "mcpCapabilities": {"http": False, "sse": True}, "auth": {"logout": {}}, "_meta": {"x": 1},
                "sessionCapabilities": {"list": {}, "delete": None, "additionalDirectories": {}, "systemPrompt": None,
                                        "resume": {}, "close": None}}
        self.assertEqual(problems(good), [])
        self.assertEqual(problems({}), [])
        breaks = [({"loadSession": value}, "loadSession is not a boolean") for value in (1, 0, "true", None)]
        for key, fields in flags.items():
            breaks += [({key: {field: 1}}, f"{key}.{field} is not a boolean") for field in fields]
            breaks += [({key: {field: "yes"}}, f"{key}.{field} is not a boolean") for field in fields]
        for key, fields in objects.items():
            breaks += [({key: {field: True}}, f"{key}.{field} is not an object or null") for field in fields]
            breaks += [({key: {field: []}}, f"{key}.{field} is not an object or null") for field in fields]
        for key in [*flags, *objects]:
            breaks += [({key: value}, f"{key} is not an object") for value in ([], None, True)]
        for caps, expected in breaks:
            with self.subTest(caps=caps):
                self.assertEqual(problems(caps), [expected])
        self.assertEqual(problems(None), ["agentCapabilities is not an object"])

    def test_session_modes_check_every_supplied_field(self):
        """Review 3 of #388 (Sol change 2, Astra change 4): each field of a session mode state, in a session/new
        and a session/load answer alike."""
        good = {"currentModeId": "agent", "availableModes": [{"id": "agent", "name": "Agent"}]}
        bad = ([], {}, {"currentModeId": 1, "availableModes": []}, {"currentModeId": "a"}, {"availableModes": []},
               {"currentModeId": "a", "availableModes": {}}, {"currentModeId": "a", "availableModes": [1]},
               {"currentModeId": "a", "availableModes": [{"name": "A"}]},
               {"currentModeId": "a", "availableModes": [{"id": 1, "name": "Agent"}]},
               {"currentModeId": "a", "availableModes": [{"id": "a", "name": 1}]})
        for method, answer in (("session/new", {"sessionId": "s"}), ("session/load", {})):
            self.assertEqual(acp_answer_problems(method, {}, {**answer, "modes": good}), [])
            for modes in bad:
                with self.subTest(method=method, modes=modes):
                    self.assertTrue(acp_answer_problems(method, {}, {**answer, "modes": modes}))

    def test_the_permission_transcript_denies(self):
        """Review 1 of #388 (Sol M25): the denial transcript must stay a denial."""
        entries = dict(self.transcripts())["acp/prompt_turns.jsonl"]
        answers = [e["msg"] for e in entries if e["dir"] == "client" and "result" in e["msg"]]
        self.assertEqual(answers, [{"jsonrpc": "2.0", "id": 100,
                                    "result": {"outcome": {"outcome": "selected", "optionId": "reject-once"}}}])
        updates = [e["msg"]["params"]["update"] for e in entries if e["msg"].get("method") == "session/update"]
        self.assertIn({"sessionUpdate": "tool_call_update", "toolCallId": "call-acp-1", "status": "failed"}, updates)


# What the shape check holds an ACP v1 body to, at the revision index.json names: each method's required
# fields with their types; each content variant's required fields; and, where supplied, the optional fields
# that change what a client does (session modes, config options, and agent capabilities with their nested
# capability objects, one level deep). Descriptive and extension fields (``_meta``, ``annotations``,
# ``mimeType`` where optional, ``title``, ``description``, ``size``) are not checked.
#
# ACP v1's closed values.
STOP_REASONS = ("end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled")
OPTION_KINDS = ("allow_once", "allow_always", "reject_once", "reject_always")
UPDATES = ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update",
           "plan", "available_commands_update", "current_mode_update", "config_option_update",
           "session_info_update", "usage_update", "notice", "compaction_update", "compaction_summary_chunk")
CLIENT_METHODS = ("initialize", "authenticate", "session/new", "session/load", "session/prompt", "session/cancel")
AGENT_METHODS = ("session/update", "session/request_permission")
# ContentBlock's variants and each one's required text fields (TextContent, ImageContent, AudioContent,
# ResourceLink; EmbeddedResource requires an object ``resource``).
CONTENT_TEXT_FIELDS = {"text": ("text",), "image": ("data", "mimeType"), "audio": ("data", "mimeType"),
                       "resource_link": ("name", "uri"), "resource": ()}
# AgentCapabilities' nested capability objects and their own fields: PromptCapabilities and McpCapabilities
# hold booleans; SessionCapabilities and AgentAuthCapabilities hold objects or null.
CAPABILITY_FLAGS = {"promptCapabilities": ("image", "audio", "embeddedContext"), "mcpCapabilities": ("http", "sse")}
CAPABILITY_OBJECTS = {"sessionCapabilities": ("list", "delete", "additionalDirectories", "systemPrompt", "resume",
                                              "close"),
                      "auth": ("logout",)}


def _text(value) -> bool:
    return isinstance(value, str) and bool(value)


def content_problems(block) -> list:
    """What an ACP v1 ContentBlock lacks: a known type and that type's required fields, as text."""
    if not isinstance(block, dict) or block.get("type") not in CONTENT_TEXT_FIELDS:
        return ["content block of no ACP v1 type"]
    kind = block["type"]
    if any(not isinstance(block.get(name), str) for name in CONTENT_TEXT_FIELDS[kind]):
        return [f"{kind} content lacks {'/'.join(CONTENT_TEXT_FIELDS[kind])} as text"]
    if kind == "resource":
        return embedded_resource_problems(block.get("resource"))
    return []


def embedded_resource_problems(resource) -> list:
    """EmbeddedResourceResource: TextResourceContents (``uri`` and ``text``) or BlobResourceContents (``uri`` and
    ``blob``), each a string. The empty string is a string: the schema sets no minimum length."""
    if not isinstance(resource, dict):
        return ["resource content lacks its resource object"]
    if not isinstance(resource.get("uri"), str):
        return ["embedded resource lacks uri as text"]
    if not (isinstance(resource.get("text"), str) or isinstance(resource.get("blob"), str)):
        return ["embedded resource lacks text or blob as text"]
    return []


def session_state_problems(result: dict) -> list:
    """The optional session state of a session/new or session/load answer: ``modes`` and ``configOptions``."""
    out = []
    modes = result.get("modes")
    if modes is not None and not (isinstance(modes, dict) and isinstance(modes.get("currentModeId"), str)
                                  and isinstance(modes.get("availableModes"), list)
                                  and all(isinstance(m, dict) and _text(m.get("id")) and _text(m.get("name"))
                                          for m in modes["availableModes"])):
        out.append("modes is not an ACP v1 session mode state")
    options = result.get("configOptions")
    if options is not None:
        if not isinstance(options, list):
            out.append("configOptions is not a list")
        else:
            out += [p for option in options for p in config_option_problems(option)]
    return out


def config_option_problems(option) -> list:
    """One SessionConfigOption: ``id`` and ``name`` as text, ``category`` text or null, and by ``type`` either a
    boolean ``currentValue`` or a select's text ``currentValue`` with its ``options``."""
    if not (isinstance(option, dict) and isinstance(option.get("id"), str) and isinstance(option.get("name"), str)):
        return ["config option lacks id/name as text"]
    if option.get("category") is not None and not isinstance(option["category"], str):
        return ["config option category is not text"]
    kind = option.get("type")
    if kind == "boolean":
        if type(option.get("currentValue")) is not bool:
            return ["boolean config option's currentValue is not a boolean"]
    elif kind == "select":
        if not isinstance(option.get("currentValue"), str):
            return ["select config option's currentValue is not text"]
        if not select_options_ok(option.get("options")):
            return ["select config option's options are neither all options nor all groups"]
    else:
        return ["config option of no ACP v1 type"]
    return []


def _select_option(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("value"), str) and isinstance(value.get("name"), str)


def _select_group(value) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("group"), str) and isinstance(value.get("name"), str)
            and isinstance(value.get("options"), list) and all(_select_option(o) for o in value["options"]))


def select_options_ok(options) -> bool:
    """SessionConfigSelectOptions: a list of select options, or a list of groups of them, never a mix."""
    return isinstance(options, list) and (all(map(_select_option, options)) or all(map(_select_group, options)))


def capability_problems(caps) -> list:
    """An initialize answer's AgentCapabilities: ``loadSession`` a boolean, and each nested capability object
    supplied is an object whose own fields are booleans (prompt, MCP) or objects or null (session, auth)."""
    if not isinstance(caps, dict):
        return ["agentCapabilities is not an object"]
    out = []
    if "loadSession" in caps and type(caps["loadSession"]) is not bool:
        out.append("loadSession is not a boolean")
    for table, fits, what in ((CAPABILITY_FLAGS, lambda v: type(v) is bool, "a boolean"),
                              (CAPABILITY_OBJECTS, lambda v: v is None or isinstance(v, dict), "an object or null")):
        for key, fields in table.items():
            if key not in caps:
                continue
            if not isinstance(caps[key], dict):
                out.append(f"{key} is not an object")
                continue
            out += [f"{key}.{field} is not {what}" for field in fields
                    if field in caps[key] and not fits(caps[key][field])]
    return out


def acp_params_problems(side: str, method: str, params) -> list:
    """What a request's or notification's params lack, by its method (ACP v1)."""
    if method not in (CLIENT_METHODS if side == "client" else AGENT_METHODS):
        return [f"{method} is not a {side} method"]
    if not isinstance(params, dict):
        return [f"{method} params are not an object"]
    out = []
    if method == "initialize":
        if type(params.get("protocolVersion")) is not int:
            out.append("protocolVersion is not an integer")
    elif method == "authenticate":
        if not _text(params.get("methodId")):
            out.append("methodId missing")
    elif method in ("session/new", "session/load"):
        if not (isinstance(params.get("cwd"), str) and params["cwd"].startswith("/")):
            out.append("cwd is not an absolute path")
        if not isinstance(params.get("mcpServers"), list):
            out.append("mcpServers is not a list")
    if method in ("session/load", "session/prompt", "session/cancel", "session/update", "session/request_permission"):
        if not _text(params.get("sessionId")):
            out.append("sessionId missing")
    if method == "session/prompt":
        prompt = params.get("prompt")
        if not (isinstance(prompt, list) and prompt):
            out.append("prompt is not a list of content blocks")
        else:
            out += [p for block in prompt for p in content_problems(block)]
    elif method == "session/update":
        update = params.get("update")
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        if kind not in UPDATES:
            out.append("sessionUpdate not in ACP v1")
        elif kind == "tool_call" and not (_text(update.get("toolCallId")) and _text(update.get("title"))):
            out.append("tool_call lacks toolCallId or title")
        elif kind == "tool_call_update" and not _text(update.get("toolCallId")):
            out.append("tool_call_update lacks toolCallId")
        elif kind.endswith("_chunk"):
            out += content_problems(update.get("content"))
    elif method == "session/request_permission":
        call, options = params.get("toolCall"), params.get("options")
        if not (isinstance(call, dict) and _text(call.get("toolCallId"))):
            out.append("toolCall lacks toolCallId")
        if not (isinstance(options, list) and options and all(
                isinstance(o, dict) and _text(o.get("optionId")) and _text(o.get("name")) and o.get("kind") in OPTION_KINDS
                for o in options)):
            out.append("options are not ACP v1 permission options")
    return out


def acp_answer_problems(method: str, params, result) -> list:
    """What an answer lacks, by the method it answers (ACP v1). Every answer here is a result object."""
    if not isinstance(result, dict):
        return [f"{method} answer is not an object"]
    if method == "initialize":
        methods = result.get("authMethods", [])
        if type(result.get("protocolVersion")) is not int:
            return ["protocolVersion is not an integer"]
        if not (isinstance(methods, list) and all(isinstance(m, dict) and _text(m.get("id")) and _text(m.get("name"))
                                                  for m in methods)):
            return ["authMethods are not ACP v1 auth methods"]
        if "agentCapabilities" in result:
            return capability_problems(result["agentCapabilities"])
    elif method in ("session/new", "session/load"):
        if method == "session/new" and not _text(result.get("sessionId")):
            return ["sessionId missing"]
        return session_state_problems(result)
    elif method == "session/prompt":
        if result.get("stopReason") not in STOP_REASONS:
            return ["stopReason not in ACP v1"]
    elif method == "session/request_permission":
        outcome = result.get("outcome")
        offered = [o.get("optionId") for o in (params or {}).get("options", [])]
        if outcome == {"outcome": "cancelled"}:
            return []
        if not (isinstance(outcome, dict) and outcome.get("outcome") == "selected" and set(outcome) == {"outcome", "optionId"}):
            return ["outcome is neither selected nor cancelled"]
        if outcome["optionId"] not in offered:
            return ["the option chosen was not offered"]
    return []


if __name__ == "__main__":
    unittest.main()
