"""The channel between the daemon and a plugin host: its schema, the handle inventory and the caller rule.

Evidence classes: every test here runs this module and the schema file in-process; no plugin host
process exists yet, so nothing here shows a host program, a wall or a frame crossing a real socket.
``RealCoreTests`` drives the real ``CoreService`` and needs ``cryptography`` (skipped without it).
"""

from __future__ import annotations

import ast
import base64
import json
import tempfile
import threading
import time
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
from recursion_core.plugin_security.manifest import PERMISSIONS
from recursion_core.control.tests.fakes import FakeStarter, make_api

REPO = Path(__file__).resolve().parents[4]
SCHEMA = REPO / "schemas" / "plugin" / "v1" / "host.v1.json"
FIXTURES = REPO / "schemas" / "plugin" / "v1" / "fixtures"
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


class CallNameTests(unittest.TestCase):
    """A call carries a NAME; an unknown one is refused call_unknown, a reserved one call_reserved; nothing runs."""

    def test_a_call_with_an_unknown_name_is_refused_and_nothing_runs(self):
        handlers = RecordingHandlers(registry=lambda: self.fail("a handler ran"))
        answer = hc.answer_call(hc.check_message(call("widgets"), receiver="host"), handlers)
        self.assertEqual(answer, {"type": "refusal", "id": 7, "code": "call_unknown", "detail": "widgets"})
        self.assertEqual(handlers.looked_up, [])

    def test_each_reserved_call_name_gets_the_reserved_answer_and_the_dispatcher_is_never_reached(self):
        for name in ("computer.open", "computer.act", "computer.observe", "computer.close"):
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


OCCURRENCE = "2026-11-20T09:00:00Z"
RUN_ARGS = ("record_kind", "record_id", "revision", "occurrence", "tenant", "engine", "prompt")
OTHER_RESERVED_CALL_NAMES = ("computer.open", "computer.act", "computer.observe", "computer.close",
                             "net.request", "net.connect")


def run_args(**changes):
    args = {"record_kind": "routine.v1", "record_id": "daily-digest", "revision": 1, "occurrence": OCCURRENCE,
            "tenant": "t1", "engine": "codex", "prompt": "summarise the day"}
    args.update(changes)
    return args


def origin_of(args, plugin="demo.plugin"):
    return {"kind": "trigger", "plugin": plugin, "record_kind": args["record_kind"], "record_id": args["record_id"],
            "revision": args["revision"], "occurrence": args["occurrence"]}


class GrantingCore(FakeCore):
    """A stand-in core that answers ``plugin.permissions`` with the granted list; every request is recorded."""

    def __init__(self, permissions=("read", "schedule"), ok=True):
        super().__init__()
        self.permissions, self.ok = list(permissions), ok

    def client_for(self, caller):
        self.clients_for.append(caller)

        def call(request):
            self.requests.append((caller, request))
            message = parse(request)
            if type(message) is dict and message.get("op") == "plugin.permissions":
                if not self.ok:
                    return canonical({"protocol": "csl-core/v0", "ok": False, "refusal": {"code": "core_poisoned"}})
                granted = self.permissions if message["args"].get("plugin_id") == caller else []
                return canonical({"protocol": "csl-core/v0", "ok": True, "result": {"permissions": granted}})
            return self.answer
        return call


class SpyRuns:
    """Passes every call to the control API and records the method and the origin it was asked with."""

    def __init__(self, api):
        self.api, self.seen = api, []

    def call(self, method, params=None, *, origin=None):
        self.seen.append((method, origin))
        return self.api.call(method, params, origin=origin)


class RunRequestTests(unittest.TestCase):
    """run.request is answered at the daemon's end: the caller is the channel's plugin, the run is admitted by
    run.start's own path, and its record says where it came from."""

    def setUp(self):
        self.api, self.registry, self.kill, self.starter = make_api()
        self.core = GrantingCore()
        self.end = hc.DaemonEnd("demo.plugin", self.core, runs=self.api)

    def ask(self, args=None, id_=5, end=None):
        return (end or self.end).receive(call("run.request", run_args() if args is None else args, id_=id_))

    def assert_nothing_started(self):
        self.assertEqual(self.starter.calls, [])
        self.assertEqual(self.registry.list(), [])

    # (1) the name is answered; every other reserved name stays reserved
    def test_run_request_is_answered_and_no_longer_reserved(self):
        answer = self.ask()
        self.assertEqual((answer["type"], answer["id"]), ("result", 5))
        self.assertEqual(answer["value"]["code"], "started")
        self.assertNotIn("run.request", hc.RESERVED_CALL_NAMES)
        self.assertIn("run.request", hc.CALL_NAMES)
        self.assertEqual(hc.RESERVED_CALL_NAMES, OTHER_RESERVED_CALL_NAMES)
        reserved = [row["name"] for row in schema()["x-reserved"]["call_names"]]
        self.assertEqual(tuple(reserved), OTHER_RESERVED_CALL_NAMES)
        for name in OTHER_RESERVED_CALL_NAMES:
            with self.subTest(name=name):
                self.assertEqual(self.end.receive(call(name, run_args()))["code"], "call_reserved")
        self.assertEqual(len(self.starter.calls), 1)

    # (2) exactly seven arguments, each in its form, refused before anything starts
    def test_the_arguments_are_exactly_the_seven_and_a_bad_set_starts_nothing(self):
        self.assertEqual(hc.CALL_NAMES["run.request"], RUN_ARGS)
        self.assertEqual(hc.RUN_REQUEST_RECORD_KINDS, ("routine.v1",))
        cases = [{k: v for k, v in run_args().items() if k != field} for field in RUN_ARGS]
        cases += [run_args(extra=1), run_args(client_key="k-1"), {}]
        for args in cases:
            with self.subTest(args=sorted(args)):
                answer = self.ask(args)
                self.assertEqual((answer["type"], answer["code"]), ("refusal", "call_args_invalid"))
        self.assert_nothing_started()

    def test_each_argument_is_checked_in_its_form_before_anything_starts(self):
        bad = {"record_kind": ["routine.v2", "", 1, None, ["routine.v1"]],
               "record_id": ["", "a" * 65, "bad id", "x/y", "café", 5, "line\n"],
               "revision": [0, -1, True, 1.0, "1", None],
               "occurrence": ["2026-11-20T09:00:00.5Z", "2026-11-20T09:00:00+00:00", "2026-11-20 09:00:00Z",
                              "2026-02-30T09:00:00Z", "2026-11-20T09:00:00z", "2026-11-20T24:00:00Z",
                              "2026-11-20T09:00:60Z", "2026-11-20T09:00:00Z\n", 5, None],
               "tenant": ["", 1, None], "engine": ["", 1, None], "prompt": ["", "\ud800", 1, None]}
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    answer = self.ask(run_args(**{field: value}))
                    self.assertEqual((answer["type"], answer["code"]), ("refusal", "call_args_invalid"))
                    self.assertIn(field, answer["detail"])
        self.assert_nothing_started()
        good = {"record_id": ["a", "A-Z_a.z-0", "x" * 64], "revision": [1, 2 ** 40],
                "occurrence": ["2028-02-29T23:59:59Z", "2026-01-01T00:00:00Z"]}
        for field, values in good.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.ask(run_args(**{field: value}))["value"]["code"], "started")

    def test_tenant_engine_and_prompt_are_checked_exactly_as_run_start_checks_them(self):
        for field, value in (("tenant", ""), ("tenant", 1), ("engine", ""), ("engine", None), ("prompt", ""),
                             ("prompt", "\ud800")):
            with self.subTest(field=field, value=value):
                params = {"tenant": "t1", "engine": "codex", "prompt": "p", field: value}
                self.assertEqual(self.api.call("run.start", params), {"code": "request_invalid", "detail": field})
                answer = self.ask(run_args(**{field: value}))
                self.assertEqual(answer["code"], "call_args_invalid")
                self.assertIn(field, answer["detail"])
        self.assert_nothing_started()

    # (3) the caller is the channel's plugin; no argument names one
    def test_the_caller_is_the_plugin_the_channel_was_opened_for(self):
        answer = self.ask()
        self.assertEqual(answer["value"]["run"]["origin"]["plugin"], "demo.plugin")
        self.assertEqual(set(hc.CALL_NAMES["run.request"]) & {"caller", "plugin", "plugin_id", "principal", "owner"},
                         set())
        for field in ("caller", "plugin", "plugin_id", "principal", "owner"):
            with self.subTest(field=field):
                refused = self.ask(run_args(**{field: "other.plugin"}), id_=6)
                self.assertEqual((refused["type"], refused["code"]), ("refusal", "call_args_invalid"))
        self.assertEqual(len(self.starter.calls), 1)
        other = hc.DaemonEnd("other.plugin", GrantingCore(), runs=self.api)
        self.assertEqual(self.ask(end=other)["value"]["run"]["origin"]["plugin"], "other.plugin")

    # (4) offered only to a plugin granted 'schedule'
    def test_the_call_is_offered_only_to_a_plugin_granted_schedule(self):
        self.assertEqual(PERMISSIONS[:7], ("read", "propose", "panel", "network", "credential", "process", "order"))
        self.assertEqual(PERMISSIONS[-1], "schedule")
        manifest = json.loads((REPO / "schemas" / "plugin" / "v1" / "manifest.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["$defs"]["permission"]["enum"][-1], "schedule")
        for core in (GrantingCore(permissions=("read", "panel")), GrantingCore(permissions=()),
                     GrantingCore(ok=False)):
            with self.subTest(permissions=core.permissions, ok=core.ok):
                end = hc.DaemonEnd("demo.plugin", core, runs=self.api)
                answer = self.ask(end=end)
                self.assertEqual((answer["type"], answer["code"]), ("refusal", "call_not_offered"))
        no_runs = hc.DaemonEnd("demo.plugin", GrantingCore())
        self.assertEqual(self.ask(end=no_runs)["code"], "call_not_offered")
        self.assert_nothing_started()
        self.assertEqual(self.ask()["value"]["code"], "started")
        asked = [parse(request) for caller, request in self.core.requests]
        self.assertEqual([(m["op"], m["args"]) for m in asked], [("plugin.permissions", {"plugin_id": "demo.plugin"})])
        self.assertEqual({caller for caller, _ in self.core.requests}, {"demo.plugin"})

    # (5) admitted through run.start's own path; the record is run.start's plus origin
    def test_the_run_is_admitted_through_run_start_and_its_record_differs_only_by_origin(self):
        spy = SpyRuns(self.api)
        end = hc.DaemonEnd("demo.plugin", GrantingCore(), runs=spy)
        answer = self.ask(end=end)
        self.assertEqual(spy.seen, [("run.start", origin_of(run_args()))])
        run_id = answer["value"]["run"]["run_id"]
        record = self.registry.get(run_id)
        self.assertEqual(record["origin"], origin_of(run_args()))
        self.assertEqual(sorted(record["origin"]), sorted(hc.RUN_ORIGIN_FIELDS))
        api2, registry2, _, starter2 = make_api()
        same = api2.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "summarise the day",
                                       "client_key": record["start_key"]["client_key"]})
        self.assertEqual(same["code"], "started")
        plain = registry2.get(same["run"]["run_id"])
        self.assertNotIn("origin", plain)
        self.assertEqual({k: v for k, v in record.items() if k != "origin"}, plain)
        self.assertEqual(self.starter.calls, starter2.calls)
        self.assertEqual(self.api.call("run.status", {"run_id": run_id})["run"]["origin"], origin_of(run_args()))
        self.assertEqual([r.get("origin") for r in self.api.call("run.list", {})["runs"]], [origin_of(run_args())])

    # (6) repeat-safe, through the start key the daemon derives
    def test_the_same_occurrence_starts_at_most_one_run_and_a_new_one_starts_a_new_run(self):
        first, again = self.ask(), self.ask(id_=6)
        self.assertEqual(again["value"]["run"]["run_id"], first["value"]["run"]["run_id"])
        self.assertTrue(again["value"]["duplicate"])
        self.assertEqual(len(self.starter.calls), 1)
        later = self.ask(run_args(occurrence="2026-11-21T09:00:00Z"), id_=7)
        revised = self.ask(run_args(revision=2), id_=8)
        other = self.ask(run_args(record_id="weekly"), id_=9)
        ids = {a["value"]["run"]["run_id"] for a in (first, later, revised, other)}
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(self.starter.calls), 4)
        key = first["value"]["run"]["start_key"]["client_key"]
        self.assertNotIn(key, json.dumps(run_args()))  # derived by the daemon, never one of the plugin's values

    def test_two_racing_calls_for_one_occurrence_start_one_run(self):
        entered, release = threading.Event(), threading.Event()
        inner = FakeStarter()

        def slow(params):
            entered.set()
            release.wait(10)
            return inner(params)
        api, registry, _, _ = make_api(starter=slow)
        end = hc.DaemonEnd("demo.plugin", GrantingCore(), runs=api)
        answers = []
        threads = [threading.Thread(target=lambda i=i: answers.append(self.ask(id_=i, end=end))) for i in (1, 2)]
        threads[0].start()
        self.assertTrue(entered.wait(10))
        threads[1].start()
        time.sleep(0.3)
        release.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(answers), 2)
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(len(registry.list()), 1)
        self.assertEqual({a["value"]["run"]["run_id"] for a in answers if "run" in a["value"]},
                         {registry.list()[0]["run_id"]})

    # (7) a control-API client cannot set origin
    def test_a_control_client_cannot_set_origin(self):
        forged = origin_of(run_args())
        answer = self.api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p", "origin": forged})
        self.assertEqual(answer["code"], "started")
        self.assertNotIn("origin", answer["run"])
        self.assertNotIn("origin", self.registry.get(answer["run"]["run_id"]))  # ignored and never stored, as today

    def test_a_control_client_reusing_the_derived_key_neither_takes_nor_hides_the_requested_run(self):
        requested = self.ask()["value"]["run"]
        key = requested["start_key"]["client_key"]
        params = {"tenant": "t1", "engine": "codex", "prompt": "summarise the day", "client_key": key}
        self.assertEqual(self.api.call("run.start", params), {"code": "request_invalid", "detail": "client_key_reused"})
        api2, registry2, _, _ = make_api()
        self.assertEqual(api2.call("run.start", params)["code"], "started")
        end2 = hc.DaemonEnd("demo.plugin", GrantingCore(), runs=api2)
        answer = self.ask(end=end2)
        self.assertEqual(answer["value"], {"code": "request_invalid", "detail": "client_key_reused"})
        self.assertEqual([r.get("origin") for r in registry2.list()], [None])

    # (8) Stop of such a run is Stop of any run
    def test_stop_of_a_requested_run_is_stop_of_any_run(self):
        run = self.ask()["value"]["run"]
        stop = self.api.call("run.stop", {"run_id": run["run_id"]})
        self.assertEqual(stop["code"], "stopped")
        self.assertEqual(self.kill.requests, [run["cgroup"]])
        record = self.registry.get(run["run_id"])
        self.assertEqual(record["state"], "stopped")
        self.assertEqual(record["origin"], origin_of(run_args()))

    # (9) agent and packet are written nowhere and stay reserved
    def test_agent_and_packet_are_written_nowhere_and_stay_reserved(self):
        self.assertEqual(hc.RESERVED_RUN_ORIGINS, ("trigger", "agent", "packet"))
        self.assertEqual(self.ask()["value"]["run"]["origin"]["kind"], "trigger")
        for path in (Path(hc.__file__), REPO / "core" / "recursion_core" / "control" / "api.py",
                     REPO / "core" / "recursion_core" / "daemon" / "control_adapter.py"):
            constants = [node.value for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                         if isinstance(node, ast.Constant) and node.value in ("agent", "packet")]
            with self.subTest(path=path.name):
                self.assertEqual(constants, ["agent", "packet"] if path.name == "host_channel.py" else [])

    def test_the_schema_and_the_code_say_the_same_of_run_request(self):
        s = schema()
        row = next(r for r in s["x-call-names"] if r["name"] == "run.request")
        self.assertEqual(tuple(row["args"]), hc.CALL_NAMES["run.request"])
        self.assertEqual(row["permission"], "schedule")
        self.assertEqual(row["permission"], hc.RUN_REQUEST_PERMISSION)
        block = s["x-run-request"]
        self.assertEqual(tuple(block["record_kinds"]), hc.RUN_REQUEST_RECORD_KINDS)
        self.assertEqual(tuple(block["origin"]["fields"]), hc.RUN_ORIGIN_FIELDS)
        self.assertEqual(block["origin"]["kind"], "trigger")
        self.assertEqual(sorted(block["args"]), sorted(hc.CALL_NAMES["run.request"]))
        self.assertIn("append-only", block["about"])

    def test_the_daemons_control_object_carries_origin_on_its_one_start_path(self):
        from recursion_core.daemon.control_adapter import DaemonControl

        class Starter:
            failures, results = {}, {}
        with tempfile.TemporaryDirectory() as state:
            control = DaemonControl(self.api, self.registry, None, Starter(), state)
            control._observe = lambda run, full: None
            end = hc.DaemonEnd("demo.plugin", GrantingCore(), runs=control)
            answer = self.ask(end=end)
            self.assertEqual(answer["value"]["code"], "started")
            self.assertEqual(self.registry.get(answer["value"]["run"]["run_id"])["origin"], origin_of(run_args()))
            plain = control.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p", "origin": {"x": 1}})
            self.assertNotIn("origin", self.registry.get(plain["run"]["run_id"]))

    # (A2) a requested start is counted in the daemon's in-flight starts exactly as a control start is
    def test_a_requested_start_is_in_the_daemons_in_flight_count_as_a_control_start_is(self):
        from recursion_core.daemon.control_adapter import DaemonControl

        class Starter:
            failures, results = {}, {}

        class Waiting:
            """A start function that waits inside the start until released, and reads the count there."""

            def __init__(self):
                self.control, self.seen = None, []
                self.entered, self.release = threading.Event(), threading.Event()

            def __call__(self, params):
                self.seen.append(self.control._starts_in_flight)
                self.entered.set()
                if not self.release.wait(10):
                    raise RuntimeError("the test never released the start")
                return {"cgroup": f"/sys/fs/cgroup/fake/{params['run_id']}"}

        waiting = Waiting()
        api, registry, _kill, _starter = make_api(starter=waiting)
        answers = []

        def during_a_start(start):
            waiting.entered.clear()
            waiting.release.clear()
            thread = threading.Thread(target=lambda: answers.append(start()), daemon=True)
            thread.start()
            try:
                self.assertTrue(waiting.entered.wait(10))
            finally:
                waiting.release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())

        with tempfile.TemporaryDirectory() as state:
            control = DaemonControl(api, registry, None, Starter(), state)
            control._observe = lambda run, full: None
            waiting.control = control
            end = hc.DaemonEnd("demo.plugin", GrantingCore(), runs=control)
            during_a_start(lambda: control.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"}))
            during_a_start(lambda: end.receive(call("run.request", run_args(), id_=5)))
            self.assertEqual(waiting.seen, [1, 1])
            self.assertEqual(control._starts_in_flight, 0)
            self.assertEqual(answers[0]["code"], "started")
            self.assertEqual(answers[1]["value"]["code"], "started")
            self.assertEqual(registry.get(answers[1]["value"]["run"]["run_id"])["origin"], origin_of(run_args()))

    # (A3) a core whose permission answer raises or cannot be read grants nothing
    def test_a_core_whose_permission_answer_raises_or_cannot_be_read_grants_nothing(self):
        class Raising(FakeCore):
            def client_for(self, caller):
                self.clients_for.append(caller)

                def call(request):
                    self.requests.append((caller, request))
                    raise RuntimeError("the core cannot answer")
                return call

        listed = canonical({"protocol": "csl-core/v0", "ok": True, "result": {"permissions": "schedule"}})
        cores = {"raises": Raising(), "not json": FakeCore(answer=b"not json at all"), "empty": FakeCore(answer=b""),
                 "not an object": FakeCore(answer=b'["schedule"]'), "no result": FakeCore(answer=b'{"ok":true}'),
                 "permissions not a list": FakeCore(answer=listed)}
        for label, core in cores.items():
            with self.subTest(core=label):
                runs = SpyRuns(self.api)
                answer = self.ask(end=hc.DaemonEnd("demo.plugin", core, runs=runs))
                self.assertEqual((answer["type"], answer["code"]), ("refusal", "call_not_offered"))
                self.assertEqual(runs.seen, [])
                self.assertEqual(len(core.requests), 1)
        self.assert_nothing_started()


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
