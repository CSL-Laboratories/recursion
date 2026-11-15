"""The Codex adapter speaks the protocol the installed binary defines (lane DRIVER-CX, the method-name check).

``routes/engines/fixtures/codex/app-server-0.160.0.json`` is an extract of the schema that
``codex-cli 0.160.0`` generated for itself (``routes/tools/attended_codex.py schema``). These tests hold
``routes/codex.py`` to it: every method name it contains, every message it sends with every key, every
field it reads and every enum value it compares with must be in the recorded protocol, and every server
request the protocol defines gets an answer on its own id. A newer binary is recorded as a new fixture;
these tests then show what moved. Fakes only: no real ``codex``, no credential, no network.
"""

import ast
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recursion_core.routes import CodexAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import codex as codex_module
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, codex_script, request

on_any_host = patch("recursion_core.routes.base.platform_supported", lambda: True, create=True)  # see finding_4

ROUTES = Path(__file__).resolve().parents[1]
FIXTURE = ROUTES / "engines" / "fixtures" / "codex" / "app-server-0.160.0.json"
TOOL = ROUTES / "tools" / "attended_codex.py"
ENVELOPE = {"id", "method", "params", "result", "error", "jsonrpc"}
METHOD_LIKE = re.compile(r"^[a-z][A-Za-z]*(/[A-Za-z]+)+$")


def protocol() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def definition(doc: dict, node: object) -> dict:
    """Follow a recorded type to the object it names: a ref, or the one non-null branch of a union."""

    for _ in range(8):
        if isinstance(node, str) and node.startswith("ref:"):
            node = doc["definitions"][node[4:]]
            continue
        if isinstance(node, dict) and ("anyOf" in node or "oneOf" in node) and "properties" not in node:
            branches = [b for b in node.get("anyOf") or node.get("oneOf") if b != {"type": "null"}]
            if len(branches) == 1:
                node = branches[0]
                continue
        return node
    raise AssertionError(f"unresolvable type {node!r}")


def variant(doc: dict, name: str, tag: str) -> dict:
    """The branch of a tagged union (``type: <tag>``) recorded under ``name``."""

    for branch in doc["definitions"][name]["oneOf"]:
        if branch.get("properties", {}).get("type") == {"enum": [tag]}:
            return branch
    raise AssertionError(f"{name} has no variant {tag!r}")


def drive(scripts, **req) -> list:
    clock = FakeClock()
    launcher = FakeLauncher(list(scripts), clock)
    service = RouteService({"codex": CodexAdapter(launcher, clock, lambda *_: "FAKE-key", {}, wall_clock=clock)},
                           ConnectionBreaker(clock), clock)
    for i, _ in enumerate(scripts):
        service.handle(request(rid=f"r{i}", engine="codex", model="gpt-5", **req))
    return [msg for proc in launcher.processes for msg in proc.sent]


class FixtureTest(unittest.TestCase):
    def test_it_records_the_installed_version_and_its_own_generator(self):
        doc = protocol()
        self.assertEqual(doc["codex_version"], "codex-cli 0.160.0")
        self.assertEqual(doc["generator"][:4], ["codex", *codex_module.NO_UPDATE_OVERRIDE, "app-server"])
        self.assertIn("generate-json-schema", doc["generator"])
        self.assertIs(doc["experimental"], False)
        self.assertEqual(doc["missing"], [])
        for table in ("client_requests", "client_notifications", "server_notifications", "server_requests"):
            self.assertTrue(doc[table], table)

    def test_the_literal_text_the_adapter_matches_is_in_the_binary(self):
        found = protocol()["binary_literals"]["found"]
        self.assertIs(found[codex_module.CHATGPT_AUTH_REQUIRED], True)


class MethodNameTest(unittest.TestCase):
    def test_every_method_name_in_the_adapter_is_in_the_protocol(self):
        doc = protocol()
        known = set().union(*(doc[t] for t in ("client_requests", "client_notifications",
                                               "server_notifications", "server_requests")))
        tree = ast.parse(Path(codex_module.__file__).read_text(encoding="utf-8"))
        literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        methods = {s for s in literals if METHOD_LIKE.match(s)}
        self.assertTrue({"account/rateLimits/read", "thread/start", "turn/start", "turn/completed"} <= methods)
        self.assertEqual(sorted(methods - known), [])
        self.assertIn("initialize", doc["client_requests"])
        self.assertIn("initialized", doc["client_notifications"])

    def test_the_checker_sees_a_misspelt_method(self):
        doc = protocol()
        known = set(doc["client_requests"])
        self.assertTrue(METHOD_LIKE.match("thread/begin"))
        self.assertNotIn("thread/begin", known)
        self.assertFalse(METHOD_LIKE.match("codex.account/rateLimits/read"))  # an event source label


@on_any_host
class SentMessageTest(unittest.TestCase):
    def check_message(self, doc: dict, msg: dict) -> None:
        self.assertLessEqual(set(msg), ENVELOPE, msg)
        method = msg.get("method")
        if method is None:  # an answer to a server request
            self.assertIn("id", msg)
            self.assertEqual(len({"result", "error"} & set(msg)), 1, msg)
            if "error" in msg:
                self.assertLessEqual(set(msg["error"]), set(definition(doc, "ref:JSONRPCErrorError")["properties"]))
            return
        if "id" in msg:
            self.assertIn(method, doc["client_requests"])
            params_type = doc["client_requests"][method]
        else:
            self.assertIn(method, doc["client_notifications"])
            params_type = doc["client_notifications"][method]
        params = msg.get("params", {})
        if params_type is None:
            self.assertEqual(params, {}, method)
            return
        shape = definition(doc, params_type)
        self.assertLessEqual(set(params), set(shape["properties"]), method)
        self.assertLessEqual(set(shape.get("required", [])), set(params), method)

    def test_every_message_the_adapter_sends_is_in_the_protocol(self):
        doc = protocol()
        ask = {"id": 99, "method": "item/commandExecution/requestApproval", "params": {}}
        sent = drive([codex_script("done"), codex_script(notes=[ask])])
        exact = [{"id": 1, "result": {}}, {"id": 2, "error": {"code": -32600,
                                                              "message": codex_module.CHATGPT_AUTH_REQUIRED}},
                 *codex_script()[2:]]
        sent += drive([exact], kind="api_key", conn_id="key", credential_env="CODEX_API_KEY")
        self.assertEqual({m.get("method") for m in sent},
                         {"initialize", "initialized", "account/rateLimits/read", "thread/start", "turn/start", None})
        for msg in sent:
            with self.subTest(msg=msg):
                self.check_message(doc, msg)

    def test_nested_values_have_the_recorded_shape(self):
        doc = protocol()
        sent = drive([codex_script()])
        init = next(m for m in sent if m.get("method") == "initialize")["params"]
        client = definition(doc, "ref:ClientInfo")
        self.assertLessEqual(set(init["clientInfo"]), set(client["properties"]))
        self.assertLessEqual(set(client["required"]), set(init["clientInfo"]))
        turn = next(m for m in sent if m.get("method") == "turn/start")["params"]
        text = variant(doc, "UserInput", "text")
        for item in turn["input"]:
            self.assertLessEqual(set(item), set(text["properties"]))
            self.assertLessEqual(set(text["required"]), set(item))

    def test_every_server_request_of_the_protocol_is_answered_on_its_own_id(self):
        doc = protocol()
        for n, method in enumerate(sorted(doc["server_requests"])):
            with self.subTest(method=method):
                ask = {"id": f"s-{n}", "method": method, "params": {}}
                sent = drive([codex_script(notes=[ask])])
                answers = [m for m in sent if m.get("id") == f"s-{n}" and "method" not in m]
                self.assertEqual(len(answers), 1)


class FieldTest(unittest.TestCase):
    READS = (
        ("GetAccountRateLimitsResponse", ("rateLimits", "limitId")),
        ("GetAccountRateLimitsResponse", ("rateLimits", "primary", "usedPercent")),
        ("GetAccountRateLimitsResponse", ("rateLimits", "secondary", "resetsAt")),
        ("AccountRateLimitsUpdatedNotification", ("rateLimits", "primary", "resetsAt")),
        ("ThreadStartResponse", ("thread", "id")),
        ("TurnCompletedNotification", ("turn", "status")),
        ("TurnCompletedNotification", ("turn", "error", "codexErrorInfo")),
        ("TurnError", ("message",)),
        ("ItemCompletedNotification", ("item",)),
    )

    def test_every_field_the_adapter_reads_is_in_the_protocol(self):
        doc = protocol()
        for root, path in self.READS:
            with self.subTest(root=root, path=path):
                node = doc["definitions"][root]
                for key in path:
                    node = definition(doc, node)
                    self.assertIn(key, node.get("properties", {}), f"{root}.{'.'.join(path)}")
                    node = node["properties"][key]

    def test_the_values_the_adapter_compares_with_are_recorded(self):
        doc = protocol()
        self.assertIn("completed", doc["definitions"]["TurnStatus"]["enum"])
        names = next(b["enum"] for b in doc["definitions"]["CodexErrorInfo"]["oneOf"] if "enum" in b)
        for value in (*codex_module._LIMIT_ERRORS, *codex_module._AUTH_ERRORS):
            self.assertIn(value, names)
        agent = variant(doc, "ThreadItem", "agentMessage")
        self.assertIn("text", agent["properties"])

    def test_the_fakes_speak_the_recorded_notifications(self):
        doc = protocol()
        for msg in codex_script():
            if "method" in msg:
                self.assertIn(msg["method"], doc["server_notifications"])


class ExtractToolTest(unittest.TestCase):
    """The tool that wrote the fixture, on a synthetic schema folder (it never runs codex here)."""

    def tool(self):
        spec = importlib.util.spec_from_file_location("attended_codex_under_test", TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_extract_reads_tables_refs_unions_and_enums(self):
        tool = self.tool()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            req = {"oneOf": [{"properties": {"method": {"enum": ["thread/start"]},
                                             "params": {"$ref": "#/definitions/ThreadStartParams"}}},
                             {"properties": {"method": {"enum": ["account/rateLimits/read"]}}}],
                   "definitions": {"ThreadStartParams": {"type": "object", "required": ["model"], "properties": {
                       "model": {"type": ["string", "null"]},
                       "sandbox": {"anyOf": [{"$ref": "#/definitions/SandboxMode"}, {"type": "null"}]}}},
                       "SandboxMode": {"enum": ["read-only", "workspace-write"], "type": "string"}}}
            (root / "ClientRequest.json").write_text(json.dumps(req), encoding="utf-8")
            for name in ("ClientNotification.json", "ServerNotification.json", "ServerRequest.json"):
                (root / name).write_text(json.dumps({"oneOf": []}), encoding="utf-8")
            doc = tool.extract(root, "codex-cli 9.9.9")
        self.assertEqual(doc["client_requests"], {"account/rateLimits/read": None,
                                                  "thread/start": "ref:ThreadStartParams"})
        self.assertEqual(doc["definitions"]["ThreadStartParams"], {
            "type": "object", "required": ["model"],
            "properties": {"model": {"type": ["string", "null"]},
                           "sandbox": {"anyOf": ["ref:SandboxMode", {"type": "null"}]}}})
        self.assertEqual(doc["definitions"]["SandboxMode"], {"enum": ["read-only", "workspace-write"]})
        self.assertIn("TurnStartParams", doc["missing"])
        self.assertEqual(set(doc["sha256"]), {"ClientRequest.json", "ClientNotification.json",
                                              "ServerNotification.json", "ServerRequest.json"})

    def test_scan_literals_finds_exact_bytes_only(self):
        tool = self.tool()
        with tempfile.TemporaryDirectory() as tmp:
            native = Path(tmp) / "codex"
            payload = b"\x00\x01chatgpt authentication required to read rate limits\x00codex"
            native.write_bytes(payload)
            found = tool.scan_literals(native, ("chatgpt authentication required to read rate limits",
                                                "Chatgpt authentication required to read rate limits"))
        self.assertEqual(list(found["found"].values()), [True, False])
        self.assertEqual(found["size"], len(payload))


if __name__ == "__main__":
    unittest.main()
