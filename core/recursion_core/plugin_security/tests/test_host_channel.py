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

from recursion_core.control_transport.framing import encode_frame
from recursion_core.plugin_security import PROTOCOL, canonical, parse
from recursion_core.plugin_security import host_channel as hc
from recursion_core.plugin_security.tests.support import World, effect, needs_crypto

REPO = Path(__file__).resolve().parents[4]
SCHEMA = REPO / "schemas" / "plugin" / "v1" / "host.v1.json"
DAEMON_PLUGINS = REPO / "core" / "recursion_core" / "daemon" / "plugins.py"
FIRST_CALL_NAMES = ["registry", "operator", "surfaces", "surface"]
NOT_AVAILABLE = "not available"


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
        """The names daemon/plugins.py takes off a loaded plugin module with getattr(module, '<name>', ...)."""
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
        for message, receiver in ((call("registry"), "daemon"), ({"type": "result", "id": 1, "value": 1}, "host")):
            with self.subTest(type=message["type"]):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.check_message(message, receiver=receiver)
                self.assertEqual(caught.exception.code, "message_wrong_side")

    def test_fields_and_forms_are_exact(self):
        for message in ({"type": "hello", "channel": "csl-plugin-host/2"}, {"type": "hello"},
                        {"type": "core", "id": -1, "bytes_b64": ""}, {"type": "core", "id": 1, "bytes_b64": "@@"},
                        {"type": "core", "id": True, "bytes_b64": ""}, {"type": "bye", "reason": 1}, [], "x"):
            with self.subTest(message=message):
                with self.assertRaises(hc.ChannelRefusal) as caught:
                    hc.check_message(message, receiver="daemon")
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

    def test_no_channel_is_opened_for_the_host(self):
        for plugin_id in ("host", "Bad Id", ""):
            with self.subTest(plugin_id=plugin_id):
                with self.assertRaises(ValueError):
                    hc.DaemonEnd(plugin_id, FakeCore())

    def test_other_messages_need_no_answer_from_the_daemon(self):
        end = hc.DaemonEnd("demo.plugin", FakeCore())
        for message in ({"type": "hello", "channel": hc.CHANNEL}, {"type": "result", "id": 1, "value": [1]},
                        {"type": "refusal", "id": None, "code": "call_unknown", "detail": ""},
                        {"type": "bye", "reason": "done"}):
            with self.subTest(type=message["type"]):
                self.assertIsNone(end.receive(message))


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
