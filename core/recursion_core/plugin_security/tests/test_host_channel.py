"""The channel between the daemon and a plugin host: its schema, the handle inventory and the caller rule.

Evidence classes: every test here runs this module and the schema file in-process; no plugin host
process exists yet, so nothing here shows a host program, a wall or a frame crossing a real socket.
``RealCoreTests`` drives the real ``CoreService`` and needs ``cryptography`` (skipped without it).
"""

from __future__ import annotations

import ast
import base64
import json
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


if __name__ == "__main__":
    unittest.main()
