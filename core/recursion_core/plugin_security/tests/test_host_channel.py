"""The channel between the daemon and a plugin host: its schema, the handle inventory and the caller rule.

Evidence classes: every test here runs this module and the schema file in-process; no plugin host
process exists yet, so nothing here shows a host program, a wall or a frame crossing a real socket.
``RealCoreTests`` drives the real ``CoreService`` and needs ``cryptography`` (skipped without it).
"""

from __future__ import annotations

import ast
import base64
import datetime
import json
import sys
import threading
import unittest
from pathlib import Path

from recursion_core.control_transport import framing
from recursion_core.control_transport.errors import FrameError
from recursion_core.control_transport.framing import encode_frame
from recursion_core.daemon import plugins as daemon_plugins
from recursion_core.plugin_security import limits
from recursion_core.plugin_security.limits import ARTIFACT, MAX_ARTIFACT_BYTES, MAX_MANIFEST_BYTES
from recursion_core.plugin_security import PROTOCOL, canonical, parse
from recursion_core.plugin_security import host_channel as hc
from recursion_core.plugin_security.tests.support import World, effect, needs_crypto

try:
    import jsonschema  # hosted CI installs it; the free-threaded interpreter has none
except ImportError:  # pragma: no cover - the root's form is still held below without it
    jsonschema = None

REPO = Path(__file__).resolve().parents[4]
SCHEMA = REPO / "schemas" / "plugin" / "v1" / "host.v1.json"
FIXTURES = REPO / "schemas" / "plugin" / "v1" / "fixtures"
CHANGES = REPO / "schemas" / "plugin" / "v1" / "CHANGES.md"
DAEMON_PLUGINS = REPO / "core" / "recursion_core" / "daemon" / "plugins.py"
FIRST_CALL_NAMES = ["registry", "operator", "surfaces", "surface"]
NOT_AVAILABLE = "not available"
NETWORK_NAMES = ("net.request", "net.connect")


def schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class RecordingHandlers(dict):
    """A handler table that records every lookup: a refused call must not look at it at all."""

    def __init__(self, **handlers):
        super().__init__(**handlers)
        self.looked_up = []

    def get(self, key, default=None):
        self.looked_up.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.looked_up.append(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.looked_up.append(key)
        return super().__contains__(key)


class FakeCore:
    """Records which caller each client was made for and every request it was handed."""

    def __init__(self, answer=b'{"ok":true}'):
        self.clients_for, self.requests, self.answer = [], [], answer

    def client_for(self, caller):
        self.clients_for.append(caller)

        def call(request):
            self.requests.append((caller, request))
            return self.answer
        return call


def call(name, args=None, id_=7):
    return {"type": "call", "id": id_, "name": name, "args": {} if args is None else args}


class InventoryTests(unittest.TestCase):
    """Every function the daemon calls on a plugin maps to exactly one message; every op stays the core's."""

    def daemon_calls_on_a_plugin(self):
        """The names daemon/plugins.py takes off a loaded plugin module with getattr(module, '<name>', ...).

        It sees only that form: a direct call such as ``module.on_event()`` is not seen.
        """
        tree = ast.parse(DAEMON_PLUGINS.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                    and len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id == "module"
                    and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
                names.add(node.args[1].value)
        return names

    def test_the_schema_exists_and_names_every_message(self):
        s = schema()
        self.assertEqual(s["$id"], "host.v1.json")
        self.assertEqual(sorted(s["x-message-types"]),
                         sorted(["hello", "load", "call", "core", "result", "refusal", "bye"]))
        for kind in s["x-message-types"]:
            self.assertEqual(s["$defs"][kind]["properties"]["type"], {"const": kind})

    def test_every_function_the_daemon_calls_on_a_plugin_maps_to_exactly_one_message(self):
        found = self.daemon_calls_on_a_plugin()
        self.assertEqual(found, {"registry", "operator", "bind_core"}, "the inventory of daemon/plugins.py moved")
        s = schema()
        listed = {row["function"]: (row["message"], row["name"]) for row in s["x-inventory"]["functions"]}
        self.assertEqual(listed, dict(hc.INVENTORY))
        names = [row["name"] for row in s["x-call-names"]]
        for function in sorted(found | {"surfaces", "surface"}):
            with self.subTest(function=function):
                self.assertIn(function, listed, "a function the daemon calls on a plugin fits no message")
                message, name = listed[function]
                self.assertIn(message, s["x-message-types"])
                if message == "call":
                    self.assertEqual(names.count(name), 1)
                else:
                    self.assertIsNone(name)
        carried = [listed[f] for f in listed]
        self.assertEqual(len(carried), len(set(carried)), "two functions share one message")

    def test_every_core_op_stays_the_cores(self):
        s = schema()
        channel_names = (set(s["x-message-types"]) | {row["name"] for row in s["x-call-names"]}
                         | {row["name"] for group in ("call_names", "message_types") for row in s["x-reserved"][group]})
        self.assertEqual(channel_names & set(PROTOCOL["ops"]), set(), "the channel names a csl-core/v0 op")
        self.assertEqual(sorted(s["$defs"]["core"]["required"]), ["bytes_b64", "id", "type"])
        self.assertEqual(sorted(s["x-message-types"]["core"]["fields"]), ["bytes_b64", "id", "type"])


class SchemaAndCodeAgreeTests(unittest.TestCase):
    """The schema holds the one list; the module's names are held equal to it."""

    def test_the_call_names_are_one_append_only_list_starting_with_the_four_first_names(self):
        rows = schema()["x-call-names"]
        self.assertEqual([row["name"] for row in rows][:4], FIRST_CALL_NAMES)
        self.assertEqual({row["name"]: tuple(row["args"]) for row in rows}, hc.CALL_NAMES)
        self.assertEqual(list(hc.CALL_NAMES)[:4], FIRST_CALL_NAMES)

    def test_message_types_fields_and_sides_agree(self):
        types = schema()["x-message-types"]
        self.assertEqual({k: (frozenset(v["from"]), frozenset(v["fields"])) for k, v in types.items()},
                         hc.MESSAGE_TYPES)
        for kind, row in types.items():
            self.assertEqual(sorted(schema()["$defs"][kind]["required"]), sorted(row["fields"]))

    def test_reserved_lists_agree_and_carry_the_rule(self):
        reserved = schema()["x-reserved"]
        self.assertEqual(tuple(r["name"] for r in reserved["call_names"]), hc.RESERVED_CALL_NAMES)
        self.assertEqual(tuple(r["name"] for r in reserved["message_types"]), hc.RESERVED_MESSAGE_TYPES)
        self.assertEqual(tuple(r["name"] for r in reserved["run_origin"]), hc.RESERVED_RUN_ORIGINS)
        self.assertIn("reserved is not frozen: a reserved name may be corrected until the first build that answers it",
                      reserved["about"].lower())
        self.assertIn(hc.RESERVED_TEXT, reserved["about"])
        self.assertEqual(set(hc.RESERVED_CALL_NAMES) & set(hc.CALL_NAMES), set())
        self.assertEqual(set(hc.RESERVED_MESSAGE_TYPES) & set(hc.MESSAGE_TYPES), set())

    def test_refusal_codes_agree(self):
        self.assertEqual(tuple(schema()["x-refusal-codes"]), hc.REFUSAL_CODES)

    def test_load_may_arrive_after_hello(self):
        s = schema()
        sentence = "load may arrive after hello at any time, even though the daemon sends it only at start in this release"
        self.assertIn(sentence, s["description"])
        self.assertIn(sentence, s["$defs"]["load"]["description"])
        load = {"type": "load", "plugin_id": "demo.plugin", "manifest_b64": b64(b"{}"), "files": {"plugin.py": b64(b"x")}}
        self.assertEqual(hc.check_message(load, receiver="host"), load)


class MessageTypeTests(unittest.TestCase):
    """An unknown type is refused typed and the channel stays up; reserved types are refused as reserved."""

    def test_an_unknown_message_type_is_refused_typed_and_the_channel_stays_up(self):
        core = FakeCore()
        end = hc.DaemonEnd("demo.plugin", core)
        answer = end.receive({"type": "telemetry", "id": 3})
        self.assertEqual(answer, {"type": "refusal", "id": 3, "code": "message_unknown", "detail": "telemetry"})
        after = end.receive({"type": "core", "id": 4, "bytes_b64": b64(b"{}")})
        self.assertEqual(after["type"], "core")
        self.assertEqual(core.requests, [("demo.plugin", b"{}")])

    def test_each_reserved_message_name_is_refused_as_reserved_on_both_sides(self):
        for kind in ("event", "guard", "surface.update"):
            for receiver in ("daemon", "host"):
                with self.subTest(kind=kind, receiver=receiver):
                    with self.assertRaises(hc.ChannelRefusal) as caught:
                        hc.check_message({"type": kind}, receiver=receiver)
                    self.assertEqual(caught.exception.code, "message_reserved")
                    self.assertIn(NOT_AVAILABLE, caught.exception.detail)

    def test_a_message_from_the_wrong_side_is_refused(self):
        load = {"type": "load", "plugin_id": "demo.plugin", "manifest_b64": b64(b"{}"), "files": {"plugin.py": b64(b"x")}}
        with self.assertRaises(hc.ChannelRefusal) as caught:
            hc.check_message(load, receiver="daemon")
        self.assertEqual(caught.exception.code, "message_wrong_side")

    def test_fields_and_forms_are_exact(self):
        for message in ({"type": "hello", "channel": "csl-plugin-host/2"}, {"type": "hello"},
                        {"type": "core", "id": -1, "bytes_b64": ""}, {"type": "core", "id": 1, "bytes_b64": "@@"},
                        {"type": "core", "id": True, "bytes_b64": ""}, {"type": "bye", "reason": 1}, [], "x"):
            with self.subTest(message=message):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.check_message(message, receiver="daemon")
                self.assertEqual(caught.exception.code, "message_invalid")

    def test_an_origin_field_is_message_reserved_on_event_and_message_invalid_on_call(self):
        for receiver in ("daemon", "host"):
            with self.subTest(kind="event", receiver=receiver):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.check_message({"type": "event", "origin": "trigger"}, receiver=receiver)
                self.assertEqual(caught.exception.code, "message_reserved")
                self.assertIn(NOT_AVAILABLE, caught.exception.detail)
            with self.subTest(kind="call", receiver=receiver):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.check_message(dict(call("registry"), origin="trigger"), receiver=receiver)
                self.assertEqual(caught.exception.code, "message_invalid")

    def test_changelog_run_origin_paragraph_names_the_check_order(self):
        paragraph = next(
            (" ".join(p.split()) for p in CHANGES.read_text(encoding="utf-8").split("\n\n")
             if "run-origin" in p),
            "",
        )
        for expected in ("run-origin", "trigger", "agent", "packet",
                         "message_reserved", "message_wrong_side", "message_invalid",
                         "wrong side", "right receiver"):
            with self.subTest(expected=expected):
                self.assertIn(expected, paragraph)

    def test_a_load_with_an_origin_field_is_wrong_side_at_daemon_and_invalid_at_host(self):
        _, fields = hc.MESSAGE_TYPES["load"]
        values = {
            "type": "load",
            "plugin_id": "demo.plugin",
            "manifest_b64": b64(b"{}"),
            "files": {"plugin.py": b64(b"x")},
        }
        load = {field: values[field] for field in fields}
        self.assertEqual(hc.check_message(load, receiver="host"), load)
        for origin in hc.RESERVED_RUN_ORIGINS:
            with_origin = dict(load, origin=origin)
            with self.subTest(origin=origin, receiver="daemon"):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.check_message(with_origin, receiver="daemon")
                self.assertEqual(caught.exception.code, "message_wrong_side")
            with self.subTest(origin=origin, receiver="host"):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.check_message(with_origin, receiver="host")
                self.assertEqual(caught.exception.code, "message_invalid")


    def test_a_load_from_the_wrong_side_is_refused_before_its_field_values_are_looked_at(self):
        _, fields = hc.MESSAGE_TYPES["load"]
        values = {
            "type": "load",
            "plugin_id": "demo.plugin",
            "manifest_b64": b64(b"{}"),
            "files": {"plugin.py": b64(b"x")},
        }
        load = {field: values[field] for field in fields}
        malformed = [
            ("a plugin id that is not an id", dict(load, plugin_id="Bad Id!")),
            ("a manifest that is not text", dict(load, manifest_b64=5)),
            ("only a type", {"type": "load"}),
        ]
        for origin in hc.RESERVED_RUN_ORIGINS:
            malformed.append(("a plugin id that is not an id, with an origin", dict(load, plugin_id="Bad Id!", origin=origin)))
            malformed.append(("a manifest that is not text, with an origin", dict(load, manifest_b64=5, origin=origin)))
            malformed.append(("only a type and an origin", {"type": "load", "origin": origin}))
        for label, message in malformed:
            with self.subTest(case=label, fields=sorted(message), receiver="daemon"):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.check_message(message, receiver="daemon")
                self.assertEqual(caught.exception.code, "message_wrong_side")
            with self.subTest(case=label, fields=sorted(message), receiver="host"):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.check_message(message, receiver="host")
                self.assertEqual(caught.exception.code, "message_invalid")


class CallNameTests(unittest.TestCase):
    """A call carries a NAME; an unknown one is refused call_unknown, a reserved one call_reserved; nothing runs."""

    def test_a_call_with_an_unknown_name_is_refused_and_nothing_runs(self):
        handlers = RecordingHandlers(registry=lambda: self.fail("a handler ran"))
        answer = hc.answer_call(hc.check_message(call("widgets"), receiver="host"), handlers)
        self.assertEqual(answer, {"type": "refusal", "id": 7, "code": "call_unknown", "detail": "widgets"})
        self.assertEqual(handlers.looked_up, [])

    def test_each_reserved_call_name_gets_the_reserved_answer_and_the_dispatcher_is_never_reached(self):
        for name in ("run.request", "computer.open", "computer.act", "computer.observe", "computer.close"):
            with self.subTest(name=name):
                handlers = RecordingHandlers(**{name: lambda *a: self.fail("a reserved name ran")})
                answer = hc.answer_call(hc.check_message(call(name), receiver="host"), handlers)
                self.assertEqual(answer["code"], "call_reserved")
                self.assertNotEqual(answer["code"], "call_unknown")
                self.assertIn(NOT_AVAILABLE, answer["detail"])
                self.assertEqual(handlers.looked_up, [])

    def test_both_network_names_get_the_reserved_answer_and_nothing_runs(self):
        for name in NETWORK_NAMES:
            with self.subTest(name=name):
                handlers = RecordingHandlers(**{name: lambda *a: self.fail("a reserved name ran")})
                answer = hc.answer_call(hc.check_message(call(name), receiver="host"), handlers)
                self.assertEqual(answer["code"], "call_reserved")
                self.assertNotEqual(answer["code"], "call_unknown")
                self.assertIn(NOT_AVAILABLE, answer["detail"])
                self.assertEqual(handlers.looked_up, [])
        reserved = {row["name"]: row["about"] for row in schema()["x-reserved"]["call_names"]}
        self.assertEqual(reserved.get("net.request"),
                         "The core makes an HTTPS request to a granted destination and returns the answer.")
        self.assertEqual(reserved.get("net.connect"),
                         "The core connects to a granted destination and hands the connected socket to the host.")

    def test_each_reserved_run_origin_is_read_from_the_reserved_list(self):
        reserved = schema()["x-reserved"]
        rows = {row["name"]: row["about"] for row in reserved["run_origin"]}
        self.assertEqual(sorted(rows), ["agent", "packet", "trigger"])
        self.assertTrue(all(rows.values()))
        self.assertIn("no field is made", reserved["run_origin_about"])

    def test_a_known_name_runs_its_handler_with_its_own_arguments_only(self):
        seen = []
        handlers = {"surface": lambda kind, name: seen.append((kind, name)) or {"kind": kind}}
        answer = hc.answer_call(call("surface", {"kind": "theme", "name": "dark"}), handlers)
        self.assertEqual(answer, {"type": "result", "id": 7, "value": {"kind": "theme"}})
        self.assertEqual(seen, [("theme", "dark")])
        refused = hc.answer_call(call("surface", {"kind": "theme"}), handlers)
        self.assertEqual(refused["code"], "call_args_invalid")
        self.assertEqual(hc.answer_call(call("registry"), handlers)["code"], "call_not_offered")

    def test_a_handler_that_raises_is_answered_typed(self):
        def boom():
            raise RuntimeError("secret text")
        answer = hc.answer_call(call("registry"), {"registry": boom})
        self.assertEqual(answer, {"type": "refusal", "id": 7, "code": "call_failed", "detail": "RuntimeError"})


class CallerTests(unittest.TestCase):
    """The caller is set from the channel, never read from the message."""

    def test_a_core_message_naming_another_plugin_is_refused_before_the_core(self):
        for field in ("caller", "plugin_id", "principal"):
            with self.subTest(field=field):
                core = FakeCore()
                end = hc.DaemonEnd("demo.plugin", core)
                forged = {"type": "core", "id": 9, "bytes_b64": b64(b"{}"), field: "other.plugin"}
                answer = end.receive(forged)
                self.assertEqual(answer["code"], "caller_forged")
                self.assertEqual(answer["id"], 9)
                self.assertEqual(core.requests, [])

    def test_the_core_client_is_bound_once_to_the_channels_plugin(self):
        core = FakeCore(answer=b'{"answer":1}')
        end = hc.DaemonEnd("demo.plugin", core)
        request = b'{"args":{"plugin_id":"other.plugin"},"op":"plugin.permissions","protocol":"csl-core/v0"}'
        answer = end.receive({"type": "core", "id": 2, "bytes_b64": b64(request)})
        self.assertEqual(answer, {"type": "core", "id": 2, "bytes_b64": b64(b'{"answer":1}')})
        self.assertEqual(core.clients_for, ["demo.plugin"])
        self.assertEqual(core.requests, [("demo.plugin", request)])  # bytes unchanged

    def test_bytes_not_in_the_cores_form_cross_unchanged_both_ways(self):
        answer = b' {"answer" : 1 ,"answer":2}\r\n\xfe\x00'
        for request in (b' {"op" : 1}\n', b"\xff\x00", b'{"op":1,"op":2}\t'):
            with self.subTest(request=request):
                core = FakeCore(answer=answer)
                end = hc.DaemonEnd("demo.plugin", core)
                out = end.receive({"type": "core", "id": 3, "bytes_b64": b64(request)})
                self.assertEqual(core.requests, [("demo.plugin", request)])
                self.assertEqual(base64.b64decode(out["bytes_b64"]), answer)

    def test_no_channel_is_opened_for_the_host(self):
        for plugin_id in ("host", "Bad Id", ""):
            with self.subTest(plugin_id=plugin_id):
                with self.assertRaises(ValueError):
                    hc.DaemonEnd(plugin_id, FakeCore())

    def test_other_messages_need_no_answer_from_the_daemon(self):
        end = hc.DaemonEnd("demo.plugin", FakeCore())
        for message in ({"type": "hello", "channel": hc.CHANNEL},
                        {"type": "refusal", "id": None, "code": "call_unknown", "detail": ""},
                        {"type": "bye", "reason": "done"}):
            with self.subTest(type=message["type"]):
                self.assertIsNone(end.receive(message))


class DirectionTests(unittest.TestCase):
    """call and result travel both ways: the daemon calls a plugin's function, a plugin's host calls a broker
    handle by name; every reserved call name is one a host will send, refused at the daemon as reserved."""

    def assert_reserved_from_the_host(self, name):
        core = FakeCore()
        end = hc.DaemonEnd("demo.plugin", core)
        answer = end.receive(call(name, id_=5))
        self.assertEqual(answer["code"], "call_reserved")
        self.assertNotEqual(answer["code"], "message_wrong_side")
        self.assertNotEqual(answer["code"], "call_unknown")
        self.assertEqual(answer["id"], 5)
        self.assertIn(hc.RESERVED_TEXT, answer["detail"])
        self.assertEqual(core.requests, [])
        # before any other check of that name: arguments of any form do not change the answer
        junk = {"type": "call", "id": 6, "name": name, "args": {"Not A Name": 1}}
        self.assertEqual(end.receive(junk)["code"], "call_reserved")

    def test_run_request_from_the_host_is_reserved(self):
        self.assert_reserved_from_the_host("run.request")

    def test_computer_open_from_the_host_is_reserved(self):
        self.assert_reserved_from_the_host("computer.open")

    def test_computer_act_from_the_host_is_reserved(self):
        self.assert_reserved_from_the_host("computer.act")

    def test_computer_observe_from_the_host_is_reserved(self):
        self.assert_reserved_from_the_host("computer.observe")

    def test_computer_close_from_the_host_is_reserved(self):
        self.assert_reserved_from_the_host("computer.close")

    def test_net_request_from_the_host_is_reserved(self):
        self.assert_reserved_from_the_host("net.request")

    def test_net_connect_from_the_host_is_reserved(self):
        self.assert_reserved_from_the_host("net.connect")

    def test_every_reserved_call_name_has_its_test_from_the_host(self):
        tested = {name[len("test_"):-len("_from_the_host_is_reserved")].replace("_", ".", 1)
                  for name in dir(self) if name.endswith("_from_the_host_is_reserved")}
        self.assertEqual(tested, set(hc.RESERVED_CALL_NAMES))

    def test_call_and_result_travel_both_ways(self):
        types = schema()["x-message-types"]
        for kind in ("call", "result"):
            with self.subTest(kind=kind):
                self.assertEqual(sorted(types[kind]["from"]), ["daemon", "host"])
        self.assertEqual(hc.check_message(call("registry"), receiver="daemon"), call("registry"))
        result = {"type": "result", "id": 1, "value": {"status": 200}}
        self.assertEqual(hc.check_message(result, receiver="host"), result)
        about = schema()["x-reserved"]["about"]
        self.assertIn("each reserved call name here is sent by a plugin's host to the daemon", about)
        self.assertIn("call and result travel both ways", about)

    def test_a_host_call_with_a_name_neither_reserved_nor_known_is_call_unknown(self):
        core = FakeCore()
        answer = hc.DaemonEnd("demo.plugin", core).receive(call("credential.request"))
        self.assertEqual(answer, {"type": "refusal", "id": 7, "code": "call_unknown", "detail": "credential.request"})
        self.assertEqual(core.requests, [])

    def test_a_result_at_a_host_for_an_id_it_never_called_is_refused(self):
        host = hc.HostEnd({})
        sent = host.call("net.request", {})
        self.assertEqual((sent["type"], sent["name"]), ("call", "net.request"))
        stray = {"type": "result", "id": sent["id"] + 1, "value": 1}
        answer = host.receive(stray)
        self.assertEqual(answer["type"], "refusal")
        self.assertEqual(answer["id"], sent["id"] + 1)
        self.assertEqual(answer["code"], "message_invalid")
        self.assertEqual(host.answers, {})
        self.assertIsNone(host.receive({"type": "result", "id": sent["id"], "value": {"status": 200}}))
        self.assertEqual(host.answers[sent["id"]]["value"], {"status": 200})
        self.assertEqual(host.receive({"type": "result", "id": sent["id"], "value": 2})["code"], "message_invalid")

    def test_a_result_at_the_daemon_for_an_id_it_never_called_is_refused(self):
        core = FakeCore()
        end = hc.DaemonEnd("demo.plugin", core)
        stray = {"type": "result", "id": 99, "value": 1}
        answer = end.receive(stray)
        self.assertIsNotNone(answer, "the daemon's end took a result for an id it never called in silence")
        self.assertEqual((answer["type"], answer["id"], answer["code"]), ("refusal", 99, "message_invalid"))
        self.assertEqual(answer["code"], hc.HostEnd({}).receive(stray)["code"])
        self.assertIn("answers no call", answer["detail"])
        self.assertEqual(core.requests, [])
        # the channel stays as after any other refused frame: the next messages are answered as before
        self.assertEqual(end.receive(call("credential.request"))["code"], "call_unknown")
        self.assertIsNone(end.receive({"type": "bye", "reason": "done"}))

    def test_a_host_answers_the_daemons_call(self):
        host = hc.HostEnd({"registry": lambda: {"panels": []}})
        self.assertEqual(host.receive(call("registry")), {"type": "result", "id": 7, "value": {"panels": []}})


class FixtureConformanceTests(unittest.TestCase):
    """Every host.v1 fixture is held by the code's own check, not only by the schema."""

    def test_every_host_fixture_is_held_by_the_code(self):
        index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))["fixtures"]
        listed = {row["file"] for row in index if row["schema"].startswith("host.v1.json")}
        on_disk = {f"{group}/host.v1/{path.name}" for group in ("good", "bad")
                   for path in (FIXTURES / group / "host.v1").glob("*.json")}
        self.assertEqual(listed, on_disk)
        self.assertGreaterEqual(len(on_disk), 55)
        for file in sorted(on_disk):
            message = json.loads((FIXTURES / file).read_text(encoding="utf-8"))
            senders = hc.MESSAGE_TYPES.get(message.get("type"), (frozenset({"daemon"}), None))[0]
            receiver = "host" if "daemon" in senders else "daemon"
            with self.subTest(file=file):
                if file.startswith("good/"):
                    self.assertEqual(hc.check_message(message, receiver=receiver), message)
                else:
                    with self.assertRaises(hc.ChannelRefusal):
                        hc.check_message(message, receiver=receiver)

    def test_every_pattern_has_the_line_break_guard_beside_it(self):
        found = []

        def walk(node, where):
            if isinstance(node, dict):
                if "pattern" in node and not where.endswith("/not"):
                    found.append(where)
                    self.assertEqual(node.get("not"), {"pattern": "[\\r\\n]"}, where)
                for key, value in node.items():
                    walk(value, f"{where}/{key}")
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    walk(value, f"{where}/{i}")
        walk(schema()["$defs"], "#/$defs")
        self.assertEqual(len(found), 5, found)


class FrameLimitTests(unittest.TestCase):
    """A load can carry what the daemon accepts; every other frame stays at most 1 MiB."""

    PLUGIN = "p" + "a" * 63

    def largest_package(self):
        return b"m" * MAX_MANIFEST_BYTES, {ARTIFACT: b"x" * MAX_ARTIFACT_BYTES}

    def test_the_load_limit_is_derived_from_the_daemons_largest_package(self):
        end = hc.DaemonEnd(self.PLUGIN, FakeCore())
        manifest, files = self.largest_package()
        frame = end.load(self.PLUGIN, manifest, files)
        self.assertEqual(len(frame) - 4, hc.MAX_LOAD_FRAME_BYTES)  # the largest package fills it exactly
        self.assertGreater(hc.MAX_LOAD_FRAME_BYTES, MAX_MANIFEST_BYTES + MAX_ARTIFACT_BYTES)
        self.assertEqual(hc.decode(frame[4:], sender="daemon")["plugin_id"], self.PLUGIN)
        self.assertEqual(hc.frame_limit("daemon"), hc.MAX_LOAD_FRAME_BYTES)

    def test_a_package_that_does_not_fit_is_load_too_large_on_the_daemons_side(self):
        end = hc.DaemonEnd(self.PLUGIN, FakeCore())
        manifest, files = self.largest_package()
        one_more = {ARTIFACT + "x": files[ARTIFACT]}  # one byte past the edge
        for package in (one_more, {ARTIFACT: b"x" * (MAX_ARTIFACT_BYTES + 3)}):
            with self.subTest(size=sum(map(len, package.values()))):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    end.load(self.PLUGIN, manifest, package)
                self.assertEqual(caught.exception.code, "load_too_large")
                self.assertNotIsInstance(caught.exception, FrameError)
        self.assertEqual(hc.REFUSAL_CODES[-1], "load_too_large")

    def test_every_other_frame_and_every_host_frame_stays_at_most_one_mib(self):
        self.assertEqual(hc.MAX_FRAME_BYTES, framing.MAX_FRAME_BYTES)
        self.assertEqual(hc.MAX_FRAME_BYTES, 1 << 20)
        self.assertEqual(hc.frame_limit("host"), 1 << 20)
        envelope = len(encode_frame({"type": "result", "id": 1, "value": ""})) - 4
        at_edge = {"type": "result", "id": 1, "value": "v" * (hc.MAX_FRAME_BYTES - envelope)}
        self.assertEqual(len(hc.encode(at_edge)) - 4, hc.MAX_FRAME_BYTES)
        self.assertEqual(hc.decode(hc.encode(at_edge)[4:], sender="daemon"), at_edge)
        past = dict(at_edge, value=at_edge["value"] + "v")
        with self.assertRaises(FrameError):
            hc.encode(past)
        body = encode_frame(past, max_bytes=hc.MAX_LOAD_FRAME_BYTES)[4:]
        for sender in ("daemon", "host"):
            with self.subTest(sender=sender):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.decode(body, sender=sender)
                self.assertEqual(caught.exception.code, "message_invalid")
        load = {"type": "load", "plugin_id": "demo.plugin", "manifest_b64": b64(b"{}"),
                "files": {ARTIFACT: b64(b"x" * (hc.MAX_FRAME_BYTES))}}
        big_load = hc.encode(load)[4:]
        self.assertGreater(len(big_load), hc.MAX_FRAME_BYTES)
        self.assertEqual(hc.decode(big_load, sender="daemon"), load)
        with self.assertRaises(hc.ChannelRefusal):
            hc.decode(big_load, sender="host")
        with self.assertRaises(hc.ChannelRefusal):
            hc.decode(b" " * (hc.MAX_LOAD_FRAME_BYTES + 1), sender="daemon")

    def test_the_schema_says_the_three_frame_rules(self):
        text = schema()["description"]
        self.assertNotIn("holding one object, at most 1 MiB", text)
        self.assertIn("a load frame (daemon to host only) may be as large as the daemon's largest package needs", text)
        self.assertIn("MAX_MANIFEST_BYTES and MAX_ARTIFACT_BYTES of plugin_security/limits.py, the sizes daemon/plugins.py reads", text)
        self.assertIn("computed from those constants, never a second number", text)
        self.assertIn("Every other frame, and every frame a host sends, is at most 1 MiB.", text)
        self.assertIn("refused on the daemon's side with 'load_too_large'", text)
        self.assertIn("it is never a frame error", text)
        self.assertEqual(tuple(schema()["x-refusal-codes"])[-1], "load_too_large")


class PackageLimitsTests(unittest.TestCase):
    """The package sizes live in the channel's own package; the daemon reads them from there."""

    def test_the_daemons_names_are_the_lower_modules_values(self):
        names = ("ARTIFACT", "MAX_MANIFEST_BYTES", "MAX_ARTIFACT_BYTES")
        self.assertEqual({n: getattr(daemon_plugins, n) for n in names}, {n: getattr(limits, n) for n in names})
        self.assertEqual((limits.ARTIFACT, limits.MAX_MANIFEST_BYTES, limits.MAX_ARTIFACT_BYTES),
                         ("plugin.py", 64 * 1024, 8 * 1024 * 1024))

    def test_the_channels_package_imports_nothing_from_the_daemon(self):
        package = Path(hc.__file__).resolve().parent
        found = []
        for path in sorted(package.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    found += [(path.name, a.name) for a in node.names if a.name.startswith("recursion_core.daemon")]
                elif isinstance(node, ast.ImportFrom):
                    module = ("." * node.level) + (node.module or "")
                    if module.startswith("recursion_core.daemon") or module.startswith("..daemon") or (
                            node.level >= 2 and module == "." * node.level
                            and any(a.name == "daemon" for a in node.names)):
                        found.append((path.name, module))
        self.assertGreater(len(list(package.glob("*.py"))), 5)
        self.assertEqual(found, [])


class OneChannelPerPluginTests(unittest.TestCase):
    """One channel per loaded plugin; every load on a channel names the plugin it was opened for."""

    def test_the_schema_says_one_channel_per_loaded_plugin(self):
        text = schema()["description"]
        self.assertIn("One channel per loaded plugin; every load on a channel names the plugin the channel was opened for.",
                      text)
        self.assertNotIn("ONE channel per plugin host", text)

    def test_the_daemon_end_sends_a_load_only_for_its_own_plugin(self):
        end = hc.DaemonEnd("demo.plugin", FakeCore())
        with self.assertRaises(ValueError):
            end.load("other.plugin", b"{}", {ARTIFACT: b"x"})
        frame = end.load("demo.plugin", b"{}", {ARTIFACT: b"x"})
        self.assertEqual(hc.decode(frame[4:], sender="daemon")["plugin_id"], "demo.plugin")


class FramingTests(unittest.TestCase):
    """The frames are control_transport/framing.py's, reused."""

    def test_encode_is_the_framing_modules_frame(self):
        message = {"type": "hello", "channel": hc.CHANNEL}
        self.assertEqual(hc.encode(message), encode_frame(message))
        self.assertEqual(hc.decode(hc.encode(message)[4:]), message)

    def test_a_body_that_is_not_one_object_is_message_invalid(self):
        for body in (b"[]", b"not json", b"\xff"):
            with self.subTest(body=body):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.decode(body)
                self.assertEqual(caught.exception.code, "message_invalid")


@needs_crypto
class RealCoreTests(unittest.TestCase):
    """Through the real core: the channel's plugin is the caller, whatever the request names."""

    def ask(self, end, op, **args):
        request = canonical({"protocol": "csl-core/v0", "op": op, "args": args})
        answer = end.receive({"type": "core", "id": 1, "bytes_b64": b64(request)})
        return parse(base64.b64decode(answer["bytes_b64"]))

    def test_an_effect_naming_another_plugin_is_refused_by_the_core(self):
        world = World(self)
        world.load()
        world.bind()
        end = hc.DaemonEnd("demo.plugin", world.core)
        out = self.ask(end, "effect.request", effect=effect(plugin_id="other.plugin"))
        self.assertEqual(out["refusal"], {"code": "caller_not_effect_plugin"})
        self.assertTrue(self.ask(end, "effect.request", effect=effect())["ok"])

    def test_a_plugin_channel_never_reaches_a_host_op(self):
        world = World(self)
        world.load()
        end = hc.DaemonEnd("demo.plugin", world.core)
        self.assertEqual(self.ask(end, "run.fault", run_id="run-1")["refusal"], {"code": "host_only"})


def published_plugin_schemas() -> dict:
    """Every schema of schemas/plugin/v1 by its file name, as an outside client reads them."""
    folder = SCHEMA.parent
    found = {}
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "$schema" in data:
            found[path.name] = data
    return found


def good_host_fixtures() -> list:
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))["fixtures"]
    return [row["file"] for row in index if row["file"].startswith("good/host.v1/")]


class AtThisHeadTests(unittest.TestCase):
    """The texts say what is true at this head: the daemon's end sends no call; its own call is later work."""

    OLD = "the daemon calls a plugin's function by name"

    def test_the_header_the_inventory_and_the_schema_say_the_daemon_sends_no_call_at_this_head(self):
        s = schema()
        texts = {"module header": " ".join(hc.__doc__.split()), "schema": s["description"],
                 "inventory": s["x-inventory"]["about"], "call": s["$defs"]["call"]["description"]}
        for where, text in texts.items():
            with self.subTest(where=where):
                self.assertNotIn(self.OLD, text)
                self.assertIn("later work", text)
        self.assertIn("What is true AT THIS HEAD: the daemon's end sends no call", texts["module header"])
        self.assertIn("at this head the daemon's end sends no call", s["description"])
        self.assertIn("at this head the daemon's end sends no call", s["x-inventory"]["about"])
        source = Path(hc.__file__).read_text(encoding="utf-8")
        self.assertIn("At this head no end here sends these calls", source)
        self.assertEqual([name for name in dir(hc.DaemonEnd) if "call" in name.lower()], [])


class WholeNumberIdTests(unittest.TestCase):
    """An id written 1.0 is refused; the schema says a whole number has no fraction and that it cannot decide it."""

    def test_an_id_with_a_fraction_is_refused_and_the_schema_says_why_it_cannot_decide(self):
        cases = {"call": (call("registry", id_=1.0), "host"), "core": ({"type": "core", "id": 1.0, "bytes_b64": ""}, "daemon"),
                 "result": ({"type": "result", "id": 1.0, "value": 1}, "host"),
                 "refusal": ({"type": "refusal", "id": 1.0, "code": "call_unknown", "detail": ""}, "daemon")}
        for kind, (message, receiver) in cases.items():
            with self.subTest(kind=kind):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.check_message(message, receiver=receiver)
                self.assertEqual((caught.exception.code, caught.exception.detail), ("message_invalid", f"{kind}: id"))
        decoded = hc.decode(b'{"type":"call","id":1.0,"name":"registry","args":{}}')
        self.assertEqual(hc.HostEnd({"registry": lambda: 1}).receive(decoded)["code"], "message_invalid")
        self.assertEqual(hc.check_message(call("registry", id_=1), receiver="host")["id"], 1)
        text = schema()["$defs"]["id"]["description"]
        self.assertIn("A whole number written without a fraction: 1, never 1.0.", text)
        self.assertIn("JSON Schema cannot decide this", text)
        self.assertIn("both ends refuse it 'message_invalid'", text)
        if jsonschema is not None:
            self.assertTrue(jsonschema.Draft202012Validator(schema()["$defs"]["id"]).is_valid(1.0))


class PackageSizeTests(unittest.TestCase):
    """The channel holds the package sizes itself: an oversize manifest or file is load_too_large before any frame."""

    def assert_too_large(self, plugin_id, manifest, files, limit_name):
        end = hc.DaemonEnd(plugin_id, FakeCore())
        with self.assertRaises(hc.ChannelRefusal) as caught:
            end.load(plugin_id, manifest, files)
        self.assertEqual(caught.exception.code, "load_too_large")
        self.assertIn(limit_name, caught.exception.detail)
        self.assertIn("no load is sent", caught.exception.detail)

    def test_a_manifest_over_its_size_is_load_too_large_though_the_frame_would_fit(self):
        self.assert_too_large("demo.plugin", b"m" * (MAX_MANIFEST_BYTES + 1), {ARTIFACT: b""}, "MAX_MANIFEST_BYTES")
        self.assert_too_large("demo.plugin", b"m" * (MAX_MANIFEST_BYTES + 1000), {ARTIFACT: b"x"}, "MAX_MANIFEST_BYTES")

    def test_a_file_over_its_size_is_load_too_large_though_the_frame_would_fit(self):
        self.assert_too_large("ab", b"m" * MAX_MANIFEST_BYTES, {ARTIFACT: b"x" * (MAX_ARTIFACT_BYTES + 45)},
                              "MAX_ARTIFACT_BYTES")
        self.assert_too_large("p" + "a" * 63, b"{}", {ARTIFACT: b"x" * (MAX_ARTIFACT_BYTES + 1)}, "MAX_ARTIFACT_BYTES")

    def test_a_manifest_and_a_file_at_their_sizes_still_load(self):
        end = hc.DaemonEnd("demo.plugin", FakeCore())
        frame = end.load("demo.plugin", b"m" * MAX_MANIFEST_BYTES, {ARTIFACT: b"x"})
        self.assertEqual(hc.decode(frame[4:], sender="daemon")["type"], "load")


class SystemExitTests(unittest.TestCase):
    """A handler that raises SystemExit is call_failed with words that say so; KeyboardInterrupt still passes."""

    def test_a_handler_that_tries_to_end_the_process_is_answered_and_the_host_goes_on(self):
        def leave():
            raise SystemExit(7)
        host = hc.HostEnd({"registry": leave, "surfaces": lambda: ["ok"]})
        answer = host.receive(call("registry"))
        self.assertEqual((answer["type"], answer["id"], answer["code"]), ("refusal", 7, "call_failed"))
        self.assertIn("tried to end the host process", answer["detail"])
        self.assertIn("the host goes on", answer["detail"])
        self.assertEqual(host.receive(call("surfaces", id_=8)), {"type": "result", "id": 8, "value": ["ok"]})
        self.assertIn("SystemExit", schema()["x-refusal-codes"]["call_failed"])

    def test_keyboard_interrupt_still_passes(self):
        def stop():
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            hc.HostEnd({"registry": stop}).receive(call("registry"))


class RefusalCodeTests(unittest.TestCase):
    """A refusal of ours is made only with a listed code; an arriving one with an unknown code stays as it is."""

    def test_a_refusal_of_ours_cannot_carry_a_code_outside_the_list(self):
        for code in ("not_a_real_code", "", None, "Call_Failed"):
            with self.subTest(code=code):
                with self.assertRaises(ValueError) as caught:
                    hc.ChannelRefusal(code)
                self.assertIn("REFUSAL_CODES", str(caught.exception))
        for code in hc.REFUSAL_CODES:
            with self.subTest(code=code):
                self.assertEqual(hc.ChannelRefusal(code, "d").code, code)

    def test_an_arriving_refusal_with_a_newer_code_is_still_read_and_the_schema_says_why(self):
        newer = {"type": "refusal", "id": 0, "code": "a_code_of_a_later_release", "detail": ""}
        self.assertEqual(hc.check_message(dict(newer), receiver="daemon"), newer)
        host = hc.HostEnd({})
        sent = host.call("net.request")
        self.assertIsNone(host.receive(dict(newer, id=sent["id"])))
        self.assertEqual(host.answers[sent["id"]]["code"], "a_code_of_a_later_release")
        s = schema()
        self.assertIn("a newer peer may know a newer code", s["description"])
        self.assertIn("a newer peer may know a newer code", s["$defs"]["refusal"]["properties"]["code"]["description"])


class NotJsonValueTests(unittest.TestCase):
    """A handler's value that cannot be written as JSON is call_failed naming the type; sending never breaks."""

    def test_a_value_that_is_not_json_is_call_failed_naming_its_type(self):
        itself = []
        itself.append(itself)
        cases = [({1, 2}, "set"), (b"bytes", "bytes"), (datetime.date(2026, 11, 24), "date"),
                 (float("nan"), "float"), (float("inf"), "float"), ({"k": [frozenset()]}, "frozenset"),
                 ({(1, 2): 1}, "dict"), ("\ud800", "str"), (itself, "a value that holds itself")]
        for value, named in cases:
            with self.subTest(value=repr(value)[:40]):
                answer = hc.HostEnd({"registry": lambda value=value: value}).receive(call("registry"))
                self.assertEqual((answer["type"], answer["id"], answer["code"]), ("refusal", 7, "call_failed"))
                self.assertTrue(answer["detail"].startswith(named), answer["detail"])
                self.assertIn("cannot be written as JSON", answer["detail"])
                self.assertEqual(hc.decode(hc.encode(answer)[4:]), answer)  # the sending loop goes on
        good = hc.HostEnd({"registry": lambda: {"panels": [1, 2.5, "x", None, True]}}).receive(call("registry"))
        self.assertEqual(hc.decode(hc.encode(good)[4:]), good)


class OrderTests(unittest.TestCase):
    """The channel's order, held by both ends alike: bye at any time ends it; nothing after it; one hello."""

    def ends(self):
        core = FakeCore()
        return [("daemon", hc.DaemonEnd("demo.plugin", core), core), ("host", hc.HostEnd({"registry": lambda: 1}), core)]

    def later_messages(self, side):
        load = {"type": "load", "plugin_id": "demo.plugin", "manifest_b64": b64(b"{}"), "files": {ARTIFACT: b64(b"x")}}
        common = [{"type": "hello", "channel": hc.CHANNEL}, call("registry"), {"type": "bye", "reason": "again"},
                  {"type": "refusal", "id": 0, "code": "call_unknown", "detail": ""}, [], {"type": "telemetry"}]
        return common + ([{"type": "core", "id": 1, "bytes_b64": b64(b"{}")}] if side == "daemon"
                         else [{"type": "result", "id": 0, "value": 1}, load])

    def test_a_bye_before_the_hello_ends_the_channel_and_every_later_message_is_refused(self):
        for side, end, core in self.ends():
            with self.subTest(side=side):
                if side == "host":
                    end.call("net.request")
                self.assertIsNone(end.receive({"type": "bye", "reason": "stopping"}))
                for message in self.later_messages(side):
                    with self.subTest(side=side, message=message):
                        answer = end.receive(message)
                        self.assertEqual((answer["type"], answer["code"]), ("refusal", "message_invalid"))
                        self.assertIn("ended with a bye", answer["detail"])
                        self.assertIn("open a new channel", answer["detail"])
                self.assertEqual(core.requests, [])
                with self.assertRaises(ValueError) as caught:
                    end.call("net.request") if side == "host" else end.load("demo.plugin", b"{}", {ARTIFACT: b"x"})
                self.assertIn("nothing more is sent", str(caught.exception))
                if side == "host":
                    self.assertEqual(end.answers, {})

    def test_a_bye_after_other_messages_ends_the_channel_too(self):
        for side, end, core in self.ends():
            with self.subTest(side=side):
                self.assertIsNone(end.receive({"type": "hello", "channel": hc.CHANNEL}))
                self.assertEqual(end.receive(call("credential.request"))["code"], "call_unknown")
                self.assertIsNone(end.receive({"type": "bye", "reason": "done"}))
                self.assertIn("ended with a bye", end.receive(call("registry"))["detail"])

    def test_a_second_hello_is_refused_and_the_channel_stays_up(self):
        for side, end, core in self.ends():
            with self.subTest(side=side):
                self.assertIsNone(end.receive({"type": "hello", "channel": hc.CHANNEL}))
                for _ in range(2):
                    answer = end.receive({"type": "hello", "channel": hc.CHANNEL})
                    self.assertEqual((answer["type"], answer["id"], answer["code"]), ("refusal", None, "message_invalid"))
                    self.assertIn("hello came already", answer["detail"])
                after = end.receive(call("registry"))  # answered as before: a result, or call_not_offered at the daemon
                self.assertEqual(after["code"] if side == "daemon" else after["type"],
                                 "call_not_offered" if side == "daemon" else "result")

    def test_the_schema_and_the_docstrings_say_exactly_this_order(self):
        s = schema()
        bye, hello = s["$defs"]["bye"]["description"], s["$defs"]["hello"]["description"]
        self.assertIn("at any time, also before the hello: the channel ends", bye)
        self.assertIn("every later message is refused 'message_invalid'", bye)
        self.assertIn("second hello is refused 'message_invalid'", hello)
        self.assertIn("bye (each side, at any time, also before the hello: the channel ends; after a bye every later "
                      "message is refused 'message_invalid'", s["description"])
        header = " ".join(hc.__doc__.split())
        self.assertIn("a ``bye`` ends the channel at any time, also before the ``hello``; after a ``bye`` every later "
                      "message is refused ``message_invalid``", header)
        self.assertIn("a second ``hello`` is refused ``message_invalid``", header)
        for end in (hc.DaemonEnd, hc.HostEnd):
            with self.subTest(end=end.__name__):
                self.assertIn("The channel's order (``bye``, a second ``hello``)", " ".join(end.__doc__.split()))


class HostEndDropsTests(unittest.TestCase):
    """At this head the host's end drops a core answer and a load; the host program above it takes them."""

    def test_a_core_answer_and_a_load_are_dropped_and_the_docstring_says_so(self):
        host = hc.HostEnd({})
        load = {"type": "load", "plugin_id": "demo.plugin", "manifest_b64": b64(b"{}"), "files": {ARTIFACT: b64(b"x")}}
        for message in ({"type": "core", "id": 1, "bytes_b64": b64(b"{}")}, load):
            with self.subTest(type=message["type"]):
                self.assertIsNone(host.receive(message))
        self.assertEqual(host.answers, {})
        doc = " ".join(hc.HostEnd.__doc__.split())
        self.assertIn("At this head this end drops a ``core`` answer and a ``load``", doc)
        self.assertIn("the host program above this end takes them", doc)


class IdRangeTests(unittest.TestCase):
    """An id past the largest allowed is never handed out: the call refuses first."""

    def test_the_call_after_the_largest_id_refuses_and_hands_out_nothing(self):
        host = hc.HostEnd({})
        host._next_id = hc.MAX_ID
        last = host.call("registry")
        self.assertEqual(last["id"], hc.MAX_ID)
        self.assertEqual(hc.check_message(last, receiver="host"), last)
        with self.assertRaises(ValueError) as caught:
            host.call("registry")
        self.assertIn("open a new channel", str(caught.exception))
        self.assertEqual(host._open, {hc.MAX_ID})
        self.assertIsNone(host.receive({"type": "result", "id": hc.MAX_ID, "value": 1}))
        self.assertEqual(host.answers[hc.MAX_ID]["value"], 1)


class ThreadTests(unittest.TestCase):
    """One end may be used from two threads: ids and the table of open calls are under one lock."""

    CALLS = 200_000  # the reproducer's count per thread; a test figure, not a setting
    TRIALS = 3

    def setUp(self):
        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)  # switch threads as often as the interpreter allows: a race shows sooner
        self.addCleanup(sys.setswitchinterval, interval)

    def run_together(self, work, count=2):
        barrier, errors = threading.Barrier(count), []

        def run(i):
            try:
                barrier.wait()
                work(i)
            except BaseException as exc:  # noqa: BLE001 - reported below, never lost in the thread
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])

    def test_two_threads_calling_on_one_end_get_400000_distinct_ids_in_each_of_three_trials(self):
        for trial in range(self.TRIALS):
            with self.subTest(trial=trial):
                host, got = hc.HostEnd({}), [[], []]

                def work(i):
                    keep, make = got[i].append, host.call
                    for _ in range(self.CALLS):
                        keep(make("registry")["id"])
                self.run_together(work)
                ids = got[0] + got[1]
                self.assertEqual(len(ids), 2 * self.CALLS)
                self.assertEqual(len(set(ids)), 2 * self.CALLS)
                self.assertEqual(host._open, set(ids))

    def test_two_threads_answering_the_same_open_calls_take_each_answer_once(self):
        host = hc.HostEnd({})
        ids = [host.call("registry")["id"] for _ in range(self.CALLS // 2)]
        taken, refused = [0, 0], [0, 0]

        def work(i):
            for id_ in ids:
                if host.receive({"type": "result", "id": id_, "value": i}) is None:
                    taken[i] += 1
                else:
                    refused[i] += 1
        self.run_together(work)
        self.assertEqual(sum(taken), len(ids))
        self.assertEqual(sum(refused), len(ids))
        self.assertEqual(len(host.answers), len(ids))
        self.assertEqual(host._open, set())

    def test_a_handler_that_calls_on_its_own_end_never_waits_on_its_lock(self):
        host = hc.HostEnd({})
        host._handlers = {"registry": lambda: host.call("net.request")["id"]}
        done = []
        worker = threading.Thread(target=lambda: done.append(host.receive(call("registry"))), daemon=True)
        worker.start()
        worker.join(30)  # a test's own bound, not a setting
        self.assertFalse(worker.is_alive(), "a handler waited on its own end's lock")
        self.assertEqual(done[0]["type"], "result")


class HandOverTests(unittest.TestCase):
    """receive keeps the dict it is handed, without a copy; the real read path never hands the same dict twice."""

    def test_the_caller_hands_the_message_over_and_decode_makes_a_new_one_for_every_frame(self):
        for end in (hc.DaemonEnd, hc.HostEnd):
            with self.subTest(end=end.__name__):
                doc = " ".join(end.receive.__doc__.split())
                self.assertIn("The caller hands ``message`` over", doc)
                self.assertIn("without a copy", doc)
        host = hc.HostEnd({})
        sent = host.call("net.request")
        body = hc.encode({"type": "result", "id": sent["id"], "value": {"status": 200}})[4:]
        first, second = hc.decode(body), hc.decode(body)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first["value"], second["value"])
        self.assertIsNone(host.receive(first))
        self.assertIs(host.answers[sent["id"]], first)  # kept as handed over: no copy


class SchemaRootTests(unittest.TestCase):
    """The published schema's root applies its seven message forms: an outside validator decides by the type."""

    BAD = ([], {}, "x", {"type": "hello", "channel": "wrong"}, {"type": "bye"}, {"type": "telemetry"},
           {"type": "event"})

    def test_the_root_is_one_of_the_seven_message_forms(self):
        s = schema()
        self.assertEqual(s.get("oneOf"), [{"$ref": f"#/$defs/{kind}"} for kind in s["x-message-types"]])
        self.assertIn("THE ROOT: a message is valid at the root of this file when it is one of the seven message forms",
                      s["description"])

    @unittest.skipIf(jsonschema is None, "jsonschema not installed")
    def test_every_good_vector_passes_at_the_root_and_wrong_ones_fail_there_by_a_draft_2020_12_validator(self):
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
        schemas = published_plugin_schemas()
        registry = Registry().with_resources((name, Resource(contents=doc, specification=DRAFT202012))
                                             for name, doc in schemas.items())
        validator = jsonschema.Draft202012Validator(schemas["host.v1.json"], registry=registry)
        for file in good_host_fixtures():
            with self.subTest(file=file):
                self.assertEqual(list(validator.iter_errors(json.loads((FIXTURES / file).read_text(encoding="utf-8")))), [])
        for value in self.BAD:
            with self.subTest(value=value):
                self.assertFalse(validator.is_valid(value))


if __name__ == "__main__":
    unittest.main()
