"""run.request reads the plugin's ``schedule`` permission in one step with the start.

A revocation that returned before the start is recorded means no start; the start's origin is one the run
record knows; the schema's words for revision and occurrence say what the daemon decides. Stand-ins and
temporary folders only.
"""

from __future__ import annotations

import json
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from recursion_core.control import api as control_api
from recursion_core.control.tests.fakes import make_api
from recursion_core.plugin_security import canonical, parse
from recursion_core.plugin_security import host_channel as hc
from recursion_core.plugin_security.tests.support import World, manifest_bytes, needs_crypto

REPO = Path(__file__).resolve().parents[4]
OCCURRENCE = "2026-11-20T09:00:00Z"
# How long a test waits for a thread it started before it calls the thread stuck. A named value of the
# tests: long enough for a loaded build host, short enough that a stuck thread fails the run.
THREAD_WAIT_S = 10.0
# How long a revocation is given to land while the start holds the permission: it must NOT land in that
# time. A short wait is enough to show it is held, since a lock that is not held is taken at once.
HELD_WAIT_S = 0.2


def run_args(**changes):
    args = {"record_kind": "routine.v1", "record_id": "daily-digest", "revision": 1, "occurrence": OCCURRENCE,
            "tenant": "t1", "engine": "codex", "prompt": "summarise the day"}
    args.update(changes)
    return args


def request(args=None, id_=5):
    return {"type": "call", "id": id_, "name": "run.request", "args": run_args() if args is None else args}


def trigger_origin(**changes):
    origin = {"kind": "trigger", "plugin": "demo.plugin", "record_kind": "routine.v1", "record_id": "daily-digest",
              "revision": 1, "occurrence": OCCURRENCE}
    origin.update(changes)
    return origin


def schema():
    return json.loads((REPO / "schemas" / "plugin" / "v1" / "host.v1.json").read_text(encoding="utf-8"))


class AskingCore:
    """A stand-in core that answers ``plugin.permissions`` over bytes, as a core seen only through its client
    does. ``after_ask`` runs right after each answer is made (the reproducer's hook)."""

    def __init__(self, permissions=("schedule",)):
        self.permissions, self.asked, self.after_ask = list(permissions), 0, None

    def client_for(self, caller):
        def call(request_bytes):
            message = parse(request_bytes)
            if message.get("op") != "plugin.permissions":
                return canonical({"protocol": "csl-core/v0", "ok": False, "refusal": {"code": "host_only"}})
            self.asked += 1
            answer = canonical({"protocol": "csl-core/v0", "ok": True, "result": {"permissions": list(self.permissions)}})
            if self.after_ask is not None:
                self.after_ask()
            return answer
        return call


class RevokedAfterTheAskTests(unittest.TestCase):
    """(1) A revocation that lands right after the call is offered and before the start: no start."""

    def setUp(self):
        self.api, self.registry, _kill, self.starter = make_api()

    def test_a_revocation_right_after_the_ask_starts_nothing_and_is_not_offered(self):
        core = AskingCore()

        def revoke_once():
            core.after_ask = None
            core.permissions = []
        core.after_ask = revoke_once
        end = hc.DaemonEnd("demo.plugin", core, runs=self.api)
        answer = end.receive(request())
        self.assertEqual((answer["type"], answer["code"], answer["detail"]), ("refusal", "call_not_offered", "run.request"))
        self.assertEqual(self.starter.calls, [])
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(core.asked, 2)  # offered on the first ask, refused on the one taken with the start

    def test_a_revocation_before_a_keyed_retry_refuses_the_retry_too(self):
        core = AskingCore()
        end = hc.DaemonEnd("demo.plugin", core, runs=self.api)
        first = end.receive(request())
        self.assertEqual(first["value"]["code"], "started")

        def revoke_once():
            core.after_ask = None
            core.permissions = []
        core.after_ask = revoke_once
        again = end.receive(request(id_=6))
        self.assertEqual((again["type"], again["code"]), ("refusal", "call_not_offered"))
        self.assertEqual(len(self.starter.calls), 1)
        self.assertEqual(len(self.registry.list()), 1)

    def test_a_granted_plugin_still_starts_once_and_its_retry_is_the_same_run(self):
        end = hc.DaemonEnd("demo.plugin", AskingCore(), runs=self.api)
        first, again = end.receive(request()), end.receive(request(id_=6))
        self.assertEqual(first["value"]["code"], "started")
        self.assertEqual(again["value"]["run"]["run_id"], first["value"]["run"]["run_id"])
        self.assertEqual(len(self.starter.calls), 1)


class HeldCore(AskingCore):
    """A stand-in core with ``permission_held``: the permission is read under the lock ``revoke`` takes."""

    def __init__(self):
        super().__init__()
        self.lock, self.held = threading.Lock(), 0

    @contextmanager
    def permission_held(self, plugin_id, permission):
        with self.lock:
            self.held += 1
            yield permission in self.permissions

    def revoke(self):
        with self.lock:
            self.permissions = []


class OneStepWithTheStartTests(unittest.TestCase):
    """(1, the one-step form) The start's own read of the permission is held until the record is written."""

    def test_the_start_reads_the_core_held_permission_and_a_revoke_waits_for_the_record(self):
        core = HeldCore()
        seen = []

        class Registry(type(make_api()[1])):
            def put(self, rec):
                if rec.get("state") == "starting":
                    revoker = threading.Thread(target=core.revoke, daemon=True)
                    revoker.start()
                    revoker.join(HELD_WAIT_S)
                    seen.append(("revoke landed during the write", not revoker.is_alive()))
                    self.revoker = revoker
                return super().put(rec)

        api, registry, _kill, starter = make_api(registry=Registry())
        end = hc.DaemonEnd("demo.plugin", core, runs=api)
        answer = end.receive(request())
        registry.revoker.join(THREAD_WAIT_S)
        self.assertFalse(registry.revoker.is_alive())
        self.assertEqual(answer["value"]["code"], "started")
        self.assertEqual(seen, [("revoke landed during the write", False)])
        self.assertEqual((core.held, core.asked), (1, 1))  # offered over bytes, started under the held read
        self.assertEqual(core.permissions, [])
        refused = end.receive(request(id_=6))
        self.assertEqual((refused["type"], refused["code"]), ("refusal", "call_not_offered"))
        self.assertEqual(len(starter.calls), 1)


class ControlApiAdmitTests(unittest.TestCase):
    """``run.start``'s ``admit``: entered after the key lookup, held until the launch intent is written."""

    @staticmethod
    def admits(value, log=None):
        @contextmanager
        def admit():
            if log is not None:
                log.append("entered")
            yield value
            if log is not None:
                log.append("left")
        return lambda: admit()

    def params(self, **changes):
        params = {"tenant": "t1", "engine": "codex", "prompt": "p", "client_key": "key-0001"}
        params.update(changes)
        return params

    def test_a_no_starts_nothing_with_its_own_code(self):
        api, registry, _kill, starter = make_api()
        answer = api.call("run.start", self.params(), origin=trigger_origin(), admit=self.admits(False))
        self.assertEqual(answer, {"code": "start_refused", "detail": "not_admitted"})
        self.assertIn(control_api.START_REFUSED, control_api.CODES)
        self.assertNotIn(control_api.START_REFUSED, control_api.SUCCESS_CODES)
        self.assertEqual((registry.list(), starter.calls), ([], []))

    def test_a_yes_is_held_across_the_record_and_let_go_before_the_launch(self):
        log = []
        api, registry, _kill, starter = make_api(starter=lambda params: log.append("launch") or
                                                 {"cgroup": "/sys/fs/cgroup/fake/" + params["run_id"]})
        answer = api.call("run.start", self.params(), origin=trigger_origin(), admit=self.admits(True, log))
        self.assertEqual(answer["code"], "started")
        self.assertEqual(log, ["entered", "left", "launch"])

    def test_only_true_admits(self):
        for value in (None, 1, "yes", []):
            with self.subTest(value=value):
                api, registry, _kill, starter = make_api()
                answer = api.call("run.start", self.params(), admit=self.admits(value))
                self.assertEqual(answer["code"], "start_refused")
                self.assertEqual(starter.calls, [])

    def test_admit_is_given_only_with_run_start_and_must_be_callable(self):
        api, _registry, _kill, _starter = make_api()
        with self.assertRaises(ValueError):
            api.call("run.status", {"run_id": "run-1"}, admit=self.admits(True))
        with self.assertRaises(ValueError):
            api.call("run.start", self.params(), admit=True)


class OriginKindTests(unittest.TestCase):
    """(4) ``ControlAPI.start`` records only an origin kind it knows, with exactly that kind's keys."""

    def test_agent_packet_and_malformed_origins_are_refused_and_nothing_starts(self):
        api, registry, _kill, starter = make_api()
        wrong = [{"kind": "agent"}, {"kind": "packet", "x": 1}, {}, {"kind": 5}, {"kind": None},
                 trigger_origin(extra=1), {k: v for k, v in trigger_origin().items() if k != "occurrence"}]
        for origin in wrong:
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p",
                                           "client_key": "key-0001"}, origin=origin)
        self.assertEqual((registry.list(), starter.calls), ([], []))

    def test_the_trigger_origin_is_accepted_and_its_keys_are_the_channels(self):
        api, registry, _kill, _starter = make_api()
        answer = api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p", "client_key": "key-0001"},
                          origin=trigger_origin())
        self.assertEqual(answer["code"], "started")
        self.assertEqual(registry.get(answer["run"]["run_id"])["origin"], trigger_origin())
        self.assertEqual(control_api.RUN_ORIGIN_KEYS, {hc.RUN_ORIGIN_KIND: frozenset(hc.RUN_ORIGIN_FIELDS)})
        self.assertEqual(set(schema()["x-run-request"]["origin"]["fields"]), control_api.RUN_ORIGIN_KEYS["trigger"])


class SchemaWordsTests(unittest.TestCase):
    """(2), (3), (5): what the daemon decides that the schema cannot, pinned with its words."""

    def setUp(self):
        self.api, self.registry, _kill, self.starter = make_api()
        self.end = hc.DaemonEnd("demo.plugin", AskingCore(), runs=self.api)
        self.args = schema()["x-run-request"]["args"]

    def refused(self, **changes):
        answer = self.end.receive(request(run_args(**changes)))
        return (answer["type"], answer.get("code"), answer.get("detail"))

    def test_revision_one_point_zero_is_refused_and_the_schema_says_why(self):
        self.assertEqual(self.refused(revision=1.0), ("refusal", "call_args_invalid", "run.request: revision"))
        words = self.args["revision"]["description"]
        self.assertIn("whole number without a fraction", words)
        self.assertIn("cannot decide it", words)
        self.assertEqual((self.args["revision"]["type"], self.args["revision"]["minimum"]), ("integer", 1))

    def test_a_leap_second_is_refused_on_purpose_and_the_schema_says_so(self):
        self.assertEqual(self.refused(occurrence="2016-12-31T23:59:60Z"),
                         ("refusal", "call_args_invalid", "run.request: occurrence"))
        self.assertIn("leap second (second 60) is refused on purpose", self.args["occurrence"]["description"])

    def test_the_occurrence_words_say_the_calendar_is_checked_too(self):
        words = self.args["occurrence"]["description"]
        self.assertIn("also checks the calendar", words)
        for value in ("2026-02-30T09:00:00Z", "2026-11-20T24:00:00Z"):
            with self.subTest(value=value):
                self.assertEqual(self.refused(occurrence=value)[1], "call_args_invalid")
        self.assertEqual(self.starter.calls, [])


@needs_crypto
class RealCoreHeldTests(unittest.TestCase):
    """Through the real core and loader: the start's read is the loader's, held against a poison."""

    def world(self):
        world = World(self, grant_permissions=("read", "schedule"))
        world.manifest = manifest_bytes(world.developer.public, permissions=("read", "schedule"))
        world.load()
        return world

    def test_the_real_core_offers_and_starts_and_a_poison_before_the_start_refuses_it(self):
        world = self.world()
        api, registry, _kill, starter = make_api()
        end = hc.DaemonEnd("demo.plugin", world.core, runs=api)
        self.assertEqual(end.receive(request())["value"]["code"], "started")
        world.health.poison("test")
        self.assertEqual(end.receive(request(run_args(record_id="other")))["code"], "call_not_offered")
        self.assertEqual(len(starter.calls), 1)

    def test_a_poison_waits_while_the_loader_holds_the_permission(self):
        world = self.world()
        with world.core.permission_held("demo.plugin", "schedule") as granted:
            self.assertIs(granted, True)
            poisoner = threading.Thread(target=world.health.poison, args=("test",), daemon=True)
            poisoner.start()
            poisoner.join(HELD_WAIT_S)
            self.assertTrue(poisoner.is_alive())
        poisoner.join(THREAD_WAIT_S)
        self.assertFalse(poisoner.is_alive())
        with world.core.permission_held("demo.plugin", "schedule") as granted:
            self.assertIs(granted, False)


if __name__ == "__main__":
    unittest.main()
