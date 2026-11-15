"""The Codex adapter speaks the protocol the installed binary defines (lane DRIVER-CX, the method-name check).

``routes/engines/fixtures/codex/app-server-0.160.0.json`` is an extract of the schema that
``codex-cli 0.160.0`` generated for itself (``routes/tools/attended_codex.py schema``). These tests hold
``routes/codex.py`` to it: every method name it contains, every message it sends with every key and the
type, enum or union branch of every value down to nested objects and list items (``check_value``), every
field it reads and every enum value it compares with must be in the recorded protocol, and every server
request the protocol defines gets an answer on its own id. The tool that wrote the fixture is held to its
own contract too: the digests are of the bytes, and ``schema`` mode runs the generator with the updater
override and records the version it ran. A newer binary is recorded as a new fixture; these tests then
show what moved. Fakes only: no real ``codex``, no credential, no network.
"""

import argparse
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
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


_TYPES = {"null": lambda v: v is None, "object": lambda v: isinstance(v, dict),
          "array": lambda v: isinstance(v, list), "string": lambda v: isinstance(v, str),
          "boolean": lambda v: type(v) is bool, "integer": lambda v: type(v) is int,
          "number": lambda v: type(v) in (int, float)}


def check_value(doc: dict, shape: object, value: object, where: str = "$") -> None:
    """Raise AssertionError unless ``value`` fits the recorded schema node ``shape``: a ref is followed, a
    union needs one branch that fits, an enum the value itself (a boolean is not 1), a type its JSON type
    (a boolean is not an integer), an object its required keys and no key outside its properties (stricter
    than the vendor schema, which lets a misspelt key pass unread), each member and each list item in turn.
    The extract keeps no other keyword (formats, bounds), so nothing else is checked."""

    if shape == "any":
        return
    if isinstance(shape, str) and shape.startswith("ref:"):
        return check_value(doc, doc["definitions"][shape[4:]], value, where)
    if not isinstance(shape, dict):
        raise AssertionError(f"{where}: unreadable schema node {shape!r}")
    for union in ("anyOf", "oneOf"):
        if union in shape:
            misses = []
            for branch in shape[union]:
                try:
                    check_value(doc, branch, value, where)
                    return
                except AssertionError as miss:
                    misses.append(str(miss))
            raise AssertionError(f"{where}: no {union} branch fits {value!r}: {misses}")
    if "enum" in shape and not any(value == e and type(value) is type(e) for e in shape["enum"]):
        raise AssertionError(f"{where}: {value!r} is not one of {shape['enum']}")
    if "type" in shape:
        types = shape["type"] if isinstance(shape["type"], list) else [shape["type"]]
        if not any(_TYPES[t](value) for t in types):
            raise AssertionError(f"{where}: {value!r} is not {'/'.join(types)}")
    if isinstance(value, dict) and "properties" in shape:
        extra = sorted(set(value) - set(shape["properties"]))
        missing = sorted(set(shape.get("required", [])) - set(value))
        if extra or missing:
            raise AssertionError(f"{where}: unknown keys {extra}, missing required keys {missing}")
        for key, item in value.items():
            check_value(doc, shape["properties"][key], item, f"{where}.{key}")
    if isinstance(value, list) and "items" in shape:
        for n, item in enumerate(value):
            check_value(doc, shape["items"], item, f"{where}[{n}]")


def check_message(doc: dict, msg: dict) -> None:
    """Raise AssertionError unless one message the adapter sent fits the recorded protocol."""

    if not isinstance(msg, dict) or not set(msg) <= ENVELOPE:
        raise AssertionError(f"not a JSON-RPC message: {msg!r}")
    if "id" in msg and type(msg["id"]) not in (int, str):
        raise AssertionError(f"a request id is an integer or text: {msg['id']!r}")
    method = msg.get("method")
    if method is None:  # an answer to a server request
        if "id" not in msg or len({"result", "error"} & set(msg)) != 1:
            raise AssertionError(f"an answer has an id and exactly one of result, error: {msg!r}")
        if "error" in msg:
            check_value(doc, "ref:JSONRPCErrorError", msg["error"], "$.error")
        return  # a result's shape depends on the request; this adapter sends none
    table = "client_requests" if "id" in msg else "client_notifications"
    if method not in doc[table]:
        raise AssertionError(f"{method!r} is not in {table}")
    params_type = doc[table][method]
    params = msg.get("params", {})
    if params_type is None:
        if params != {}:
            raise AssertionError(f"{method} takes no params: {params!r}")
        return
    check_value(doc, params_type, params, f"{method}.params")


def variant(doc: dict, name: str, tag: str) -> dict:
    """The branch of a tagged union (``type: <tag>``) recorded under ``name``."""

    for branch in doc["definitions"][name]["oneOf"]:
        if branch.get("properties", {}).get("type") == {"enum": [tag]}:
            return branch
    raise AssertionError(f"{name} has no variant {tag!r}")


def load_tool():
    """``routes/tools/attended_codex.py``, loaded from its file (it is a script, not a package module)."""

    spec = importlib.util.spec_from_file_location("attended_codex_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        check_message(doc, msg)

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
        check_value(doc, "ref:ClientInfo", init["clientInfo"])
        turn = next(m for m in sent if m.get("method") == "turn/start")["params"]
        self.assertTrue(turn["input"])
        for item in turn["input"]:  # the text variant itself, not merely some UserInput branch
            check_value(doc, variant(doc, "UserInput", "text"), item)

    def test_every_server_request_of_the_protocol_is_answered_on_its_own_id(self):
        doc = protocol()
        for n, method in enumerate(sorted(doc["server_requests"])):
            with self.subTest(method=method):
                ask = {"id": f"s-{n}", "method": method, "params": {}}
                sent = drive([codex_script(notes=[ask])])
                answers = [m for m in sent if m.get("id") == f"s-{n}" and "method" not in m]
                self.assertEqual(len(answers), 1)


# One valid message of each kind the adapter sends, and wrong values the checker must refuse, each a single
# change to one of them (the #393 reads: GPT-6 Astra R1 and GPT-6.1 Sol R3; their probe messages included).
VALID = {
    "initialize": {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "recursion", "version": "0"}}},
    "thread/start": {"id": 3, "method": "thread/start", "params": {"model": "gpt-5"}},
    "turn/start": {"id": 4, "method": "turn/start",
                   "params": {"threadId": "th-1", "input": [{"type": "text", "text": "hello"}]}},
    "answer": {"id": "s-1", "error": {"code": -32601, "message": "unsupported"}},
}
WRONG = (
    ("model is an integer", "thread/start", ("params", "model"), 123),
    ("threadId is an integer", "turn/start", ("params", "threadId"), 123),
    ("threadId is null", "turn/start", ("params", "threadId"), None),
    ("clientInfo.name is an integer", "initialize", ("params", "clientInfo", "name"), 123),
    ("clientInfo.version is an integer", "initialize", ("params", "clientInfo", "version"), 42),
    ("clientInfo has an unknown key", "initialize", ("params", "clientInfo", "nickname"), "r"),
    ("clientInfo lacks its required version", "initialize", ("params", "clientInfo"), {"name": "recursion"}),
    ("input is text, not a list", "turn/start", ("params", "input"), "hello"),
    ("an input item's tag is no UserInput variant", "turn/start", ("params", "input", 0, "type"), "FAKE-invalid"),
    ("an input item's text is a list", "turn/start", ("params", "input", 0, "text"), ["hello"]),
    ("an error code is text", "answer", ("error", "code"), "FAKE-invalid"),
    ("an error code is a boolean", "answer", ("error", "code"), True),
    ("an error lacks its message", "answer", ("error",), {"code": -32601}),
    ("a request id is a float", "thread/start", ("id",), 1.5),
)


def changed(kind: str, path: tuple, value: object) -> dict:
    msg = copy.deepcopy(VALID[kind])
    node = msg
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return msg


class ProtocolCheckerTest(unittest.TestCase):
    """The checker refuses what the generated schema refuses: types, enum tags, nested members, list items."""

    def test_the_valid_messages_fit(self):
        doc = protocol()
        for kind, msg in VALID.items():
            with self.subTest(kind=kind):
                check_message(doc, msg)

    def test_every_wrong_value_is_refused(self):
        doc = protocol()
        for name, kind, path, value in WRONG:
            with self.subTest(name):
                with self.assertRaises(AssertionError):
                    check_message(doc, changed(kind, path, value))

    def test_a_boolean_is_no_number_and_no_numeric_enum_value(self):
        doc = protocol()
        for shape, good, bad in (({"enum": [0, 1]}, 1, True), ({"type": "number"}, 1.5, False),
                                 ({"type": "integer"}, 1, True)):
            with self.subTest(shape=shape):
                check_value(doc, shape, good)
                with self.assertRaises(AssertionError):
                    check_value(doc, shape, bad)

    def test_rejects_wrong_model_type(self):
        with self.assertRaisesRegex(AssertionError, r"thread/start\.params\.model"):
            check_message(protocol(), changed("thread/start", ("params", "model"), 42))

    def test_rejects_wrong_client_info_type(self):
        with self.assertRaisesRegex(AssertionError, r"clientInfo\.version"):
            check_message(protocol(), changed("initialize", ("params", "clientInfo", "version"), 42))

    def test_checker_rejects_wrong_nested_client_info_types(self):
        with self.assertRaisesRegex(AssertionError, r"clientInfo\.name"):
            check_message(protocol(), changed("initialize", ("params", "clientInfo", "name"), 123))

    def test_rejects_wrong_input_discriminant(self):
        with self.assertRaisesRegex(AssertionError, r"input\[0\]"):
            check_message(protocol(), changed("turn/start", ("params", "input", 0, "type"), "FAKE-invalid"))

    def test_rejects_string_error_code(self):
        with self.assertRaisesRegex(AssertionError, r"\$\.error\.code"):
            check_message(protocol(), changed("answer", ("error", "code"), "FAKE-invalid"))

    def test_checker_rejects_wrong_outgoing_scalar_types(self):
        with self.assertRaisesRegex(AssertionError, r"turn/start\.params\.threadId"):
            check_message(protocol(), {"id": 4, "method": "turn/start", "params": {"threadId": 123, "input": []}})


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

    def test_the_error_names_a_result_may_carry_are_the_protocol_s(self):
        info = protocol()["definitions"]["CodexErrorInfo"]["oneOf"]
        names = {name for branch in info for name in branch.get("enum", branch.get("required", []))}
        self.assertEqual(codex_module.ERROR_INFO_NAMES, names)

    def test_the_adapter_reads_the_snapshot_by_its_recorded_names(self):
        # GPT-6 Astra's coverage of the four read names (#393 read): the snapshot is read here too, not
        # only through test_routes.
        def read(snapshot):
            events = []
            return codex_module._exhausted(snapshot, "codex.test", events, lambda at: at + 10), events

        _, events = read({"limitId": "codex", "primary": {"usedPercent": 34}, "secondary": {"usedPercent": 23}})
        self.assertEqual(events[0]["data"], {"limitId": "codex", "primary.usedPercent": 34,
                                             "secondary.usedPercent": 23})
        limit, _ = read({"limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1000}})
        self.assertEqual(limit["reset_at"], 1010)

    def test_the_fakes_speak_the_recorded_notifications(self):
        doc = protocol()
        for msg in codex_script():
            if "method" in msg:
                self.assertIn(msg["method"], doc["server_notifications"])


class ExtractToolTest(unittest.TestCase):
    """The tool that wrote the fixture, on a synthetic schema folder (it never runs codex here)."""

    def tool(self):
        return load_tool()

    def test_extract_reads_tables_refs_unions_and_enums(self):
        tool = self.tool()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            req = {"oneOf": [{"properties": {"method": {"enum": ["thread/start"]},
                                             "params": {"$ref": "#/definitions/ThreadStartParams"}}},
                             {"properties": {"method": {"const": "account/rateLimits/read"}}}],
                   "definitions": {"ThreadStartParams": {"type": "object", "required": ["model"], "properties": {
                       "model": {"type": ["string", "null"]},
                       "sandbox": {"anyOf": [{"$ref": "#/definitions/SandboxMode"}, {"type": "null"}]},
                       "kind": {"const": "start"},
                       "tags": {"type": "array", "items": {"type": "string"}},
                       "mode": {"allOf": [{"$ref": "#/definitions/SandboxMode"}]}}},
                       "SandboxMode": {"enum": ["read-only", "workspace-write"], "type": "string"}}}
            (root / "ClientRequest.json").write_text(json.dumps(req), encoding="utf-8")
            for name in ("ClientNotification.json", "ServerNotification.json", "ServerRequest.json"):
                (root / name).write_text(json.dumps({"oneOf": []}), encoding="utf-8")
            doc = tool.extract(root, "codex-cli 9.9.9")
        self.assertEqual(doc["client_requests"], {"account/rateLimits/read": None,
                                                  "thread/start": "ref:ThreadStartParams"})
        self.assertEqual(doc["definitions"]["ThreadStartParams"], {
            "type": "object", "required": ["model"],
            "properties": {"kind": {"enum": ["start"]},
                           "mode": "ref:SandboxMode",
                           "model": {"type": ["string", "null"]},
                           "sandbox": {"anyOf": ["ref:SandboxMode", {"type": "null"}]},
                           "tags": {"type": "array", "items": {"type": "string"}}}})
        self.assertEqual(doc["definitions"]["SandboxMode"], {"enum": ["read-only", "workspace-write"]})
        self.assertIn("TurnStartParams", doc["missing"])
        self.assertEqual(set(doc["sha256"]), {"ClientRequest.json", "ClientNotification.json",
                                              "ServerNotification.json", "ServerRequest.json"})

    def test_brief_keeps_const_array_items_and_a_single_allof(self):
        # GPT-6 Astra's three structural cases (#393 read), on _brief itself as well as through extract above.
        brief = self.tool()._brief
        self.assertEqual(brief({"const": "thread/start"}), {"enum": ["thread/start"]})
        self.assertEqual(brief({"type": "array", "items": {"type": "string"}}),
                         {"type": "array", "items": {"type": "string"}})
        self.assertEqual(brief({"allOf": [{"$ref": "#/definitions/ClientInfo"}]}), "ref:ClientInfo")

    def test_the_digests_are_of_each_file_s_bytes(self):
        tool = self.tool()
        bundle = "codex_app_server_protocol.schemas.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {name: json.dumps({"oneOf": [], "$comment": name}).encode()
                    for name in (*tool.TABLES.values(), bundle)}
            for name, body in data.items():
                (root / name).write_bytes(body)
            doc = tool.extract(root, "codex-cli 9.9.9")
        self.assertEqual(doc["sha256"], {name: hashlib.sha256(body).hexdigest() for name, body in data.items()})

    def test_scan_literals_hashes_every_byte_of_the_binary(self):
        tool = self.tool()
        payload = bytes(range(256)) * 10_000 + b"\x00tail"  # over two read blocks of 1 MiB
        with tempfile.TemporaryDirectory() as tmp:
            native = Path(tmp) / "codex"
            native.write_bytes(payload)
            found = tool.scan_literals(native, ())
        self.assertEqual(found["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(found["size"], len(payload))

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


class SchemaModeTest(unittest.TestCase):
    """``schema`` mode with its two commands faked (GPT-6.1 Sol's SchemaModeContract, #393 read)."""

    def exercise(self, *, native: bytes | None = None):
        tool = load_tool()
        calls = []

        def fake_run(argv):
            calls.append(list(argv))
            if argv[-1] == "--version":
                return "codex-cli 9.9.9"
            out = Path(argv[-1])
            for name in tool.TABLES.values():
                (out / name).write_text(json.dumps({"oneOf": []}), encoding="utf-8")
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            binary = None
            if native is not None:
                binary = Path(tmp) / "codex-native"
                binary.write_bytes(native)
            args = argparse.Namespace(codex="FAKE-codex", out=tmp, native=str(binary) if binary else None)
            with patch.object(tool, "_run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tool.schema_mode(args), 0)
            doc = json.loads((Path(tmp) / "app-server-9.9.9.json").read_text(encoding="utf-8"))
        return calls, doc

    def test_schema_mode_calls_generator_with_updater_override(self):
        calls, _ = self.exercise()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ["FAKE-codex", "--version"])
        self.assertEqual(calls[1][:-1], ["FAKE-codex", *codex_module.NO_UPDATE_OVERRIDE, "app-server",
                                         "generate-json-schema", "--out"])

    def test_schema_mode_records_the_version_it_executed(self):
        _, doc = self.exercise()
        self.assertEqual(doc["codex_version"], "codex-cli 9.9.9")
        self.assertEqual(doc["binary_literals"], {})

    def test_schema_mode_records_the_native_binary_it_scanned(self):
        payload = b"\x00" + codex_module.CHATGPT_AUTH_REQUIRED.encode() + b"\x00"
        _, doc = self.exercise(native=payload)
        self.assertEqual(doc["binary_literals"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertIs(doc["binary_literals"]["found"][codex_module.CHATGPT_AUTH_REQUIRED], True)


if __name__ == "__main__":
    unittest.main()
