"""Stop shuts the run's admission and records workspace.revoke before the kill.

PRODUCT.md L976 (S6 / DL-38) and L1852 (F3). NOTE.md B2-17.3 and B2-27.1.
The effects word is only none_known or unresolved. A fourth research word is not recorded.
"""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path

from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import EMPTY, InMemoryRunRegistry, StaleGeneration
from recursion_core.control.tests.fakes import FakeKillTree, FakeStarter, run_ids, started
from recursion_core.control.tests.test_seams import FakeStore
from recursion_core.workspace.grant import Ledger

_REPO = Path(__file__).resolve().parents[4]
_STOP_SCHEMA = _REPO / "schemas" / "control" / "v1" / "stop.result.v1.json"
_REGISTRY = Path(__file__).resolve().parents[1] / "registry.py"
_FACTS = ("admission_closed", "local_scope_empty", "external_effects")


class _OrderingKill(FakeKillTree):
    """Records whether workspace.revoke is already in the log when the kill is requested."""

    def __init__(self, store: FakeStore, run_id: str) -> None:
        super().__init__([EMPTY])
        self.store, self.run_id = store, run_id
        self.revoke_before_kill = False

    def request_stop(self, cgroup: str) -> None:
        self.revoke_before_kill = any(
            isinstance(event, dict) and event.get("type") == "workspace.revoke" and event.get("run_id") == self.run_id
            for event in self.store.events)
        super().request_stop(cgroup)


def _api(store: FakeStore, kill: FakeKillTree) -> ControlAPI:
    return ControlAPI(InMemoryRunRegistry(), kill, FakeStarter(), clock=lambda: 1000.0,
                      new_run_id=run_ids(), events=store)


def _grant(store: FakeStore, run_id: str) -> None:
    Ledger.declare(store)
    with store.transaction() as tx:
        Ledger.granted(tx, run_id, "/work/run")


def _unknown_effect(store: FakeStore, run_id: str) -> None:
    event = {"session_id": f"run:{run_id}", "run_id": run_id, "turn_id": None,
             "type": "effect.outcome_unknown", "source": "core", "trust": "trusted",
             "payload": {"effect": {"kind": "tool_call", "id": "effect-1"}, "why": "deadline"}}
    with store.transaction() as tx:
        tx.append(event)


class _IterRaises:
    """Events that fetch cleanly and raise on the first step of iteration."""

    def __iter__(self):
        raise RuntimeError("events cursor")
        yield None  # makes this a generator so the raise happens while iterating


class _RaisingEvents:
    """A store whose event log raises during iteration."""

    @property
    def events(self):
        return _IterRaises()


class _EffectDuringKill(FakeKillTree):
    """Appends effect.outcome_unknown from inside the kill, after admission was recorded."""

    def __init__(self, store: FakeStore, run_id: str) -> None:
        super().__init__([EMPTY])
        self.store, self.run_id = store, run_id

    def request_stop(self, cgroup: str) -> None:
        _unknown_effect(self.store, self.run_id)
        super().request_stop(cgroup)


def _bare_api(kill: FakeKillTree | None = None) -> ControlAPI:
    """A ControlAPI with no event store, as the daemon composition builds one."""
    return ControlAPI(InMemoryRunRegistry(), kill or FakeKillTree(), FakeStarter(),
                      clock=lambda: 1000.0, new_run_id=run_ids())


def _ensure_ledger(store) -> None:
    try:
        Ledger.declare(store)
    except RuntimeError:
        pass


def _standing(store) -> list:
    with store.transaction() as tx:
        return [row[0] for row in Ledger.unrevoked(tx)]


def _revoked_seq(store, run_id: str):
    with store.transaction() as tx:
        rows = tx.execute("SELECT revoked_seq FROM workspace_grant WHERE run_id = ?", (run_id,))
    return None if not rows else rows[0][0]


class _LedgerOnly:
    """A transaction helper with no ``events`` attribute, as the real event store is."""

    def __init__(self, inner: FakeStore) -> None:
        self._inner = inner

    def transaction(self):
        return self._inner.transaction()

    def register_tables(self, lane, version, ddl) -> None:
        return self._inner.register_tables(lane, version, ddl)


class _StaleRunningPut(InMemoryRunRegistry):
    """The first put of a running record is stale, and the stored row carries a stop intent."""

    def __init__(self) -> None:
        super().__init__()
        self._once = True

    def put(self, record: dict) -> dict:
        if self._once and record.get("state") == "running":
            self._once = False
            current = self.get(record["run_id"])
            if current is None:
                raise AssertionError("running put with no row")
            current["stop"] = {"requested_at": 1000.0, "checks": 1, "reason": "cgroup_unknown",
                               "requested": False}
            InMemoryRunRegistry.put(self, current)
            raise StaleGeneration(record["run_id"], record.get("generation"), None)
        return InMemoryRunRegistry.put(self, record)


_START = {"tenant": "t1", "engine": "codex", "prompt": "p"}


class StopAdmissionTest(unittest.TestCase):
    def test_b2_17_stop_refuses_a_later_dispatch_and_records_revoke_before_the_kill(self) -> None:
        store = FakeStore()
        kill = _OrderingKill(store, "run-1")
        api = _api(store, kill)
        run = started(api)
        _grant(store, run)
        self.assertFalse(api._dispatch_refused(run, api._stop_target(run)))
        answer = api.call("run.stop", {"run_id": run})
        self.assertEqual(answer["code"], "stopped")
        self.assertIs(answer["admission_closed"], True)
        self.assertIs(answer["local_scope_empty"], True)
        self.assertEqual(answer["external_effects"], "none_known")
        self.assertIs(answer["workspace.revoke"], True)
        self.assertIn("admission_closed", json.dumps(answer))
        self.assertIn("workspace.revoke", json.dumps(answer))
        self.assertTrue(kill.revoke_before_kill)
        self.assertEqual(
            [event["type"] for event in store.events if event.get("run_id") == run and event["type"].startswith("workspace.")],
            ["workspace.grant", "workspace.revoke"])
        self.assertTrue(api._dispatch_refused(run, api._stop_target(run)))
        self.assertTrue(api._dispatch_refused(run, api._stop_target(run)))
        repeat = api.call("run.stop", {"run_id": run})
        self.assertIs(repeat["admission_closed"], True)
        self.assertIs(repeat["workspace.revoke"], True)
        self.assertEqual(sum(event["type"] == "workspace.revoke" for event in store.events), 1)
        self.assertIsNone(api._stop_target("nope"))
        self.assertEqual(api.call("run.stop", {"run_id": "nope"})["code"], "run_unknown")
        stored = api.call("run.status", {"run_id": run})["run"]["stop_attestation"]
        self.assertIs(stored["admission_closed"], True)
        self.assertIs(stored["local_scope_empty"], True)
        self.assertEqual(stored["external_effects"], "none_known")

    def test_b2_27_a_recorded_unknown_effect_answers_unresolved(self) -> None:
        store = FakeStore()
        kill = _OrderingKill(store, "run-1")
        api = _api(store, kill)
        run = started(api)
        _grant(store, run)
        _unknown_effect(store, run)
        answer = api.call("run.stop", {"run_id": run})
        self.assertEqual(answer["external_effects"], "unresolved")
        self.assertIs(answer["admission_closed"], True)
        self.assertTrue(kill.revoke_before_kill)
        stored = api.call("run.status", {"run_id": run})["run"]["stop_attestation"]
        self.assertEqual(stored["external_effects"], "unresolved")

    def test_a_log_that_raises_during_iteration_still_kills_and_answers_unresolved(self) -> None:
        kill = FakeKillTree()
        api = ControlAPI(InMemoryRunRegistry(), kill, FakeStarter(), clock=lambda: 1000.0,
                         new_run_id=run_ids(), events=_RaisingEvents())
        run = started(api)
        answer = api.call("run.stop", {"run_id": run})
        self.assertEqual(answer["code"], "stopped")
        self.assertEqual(kill.requests, [f"/sys/fs/cgroup/fake/{run}"])
        self.assertEqual(answer["external_effects"], "unresolved")

    def test_an_unknown_effect_appended_inside_the_kill_answers_unresolved(self) -> None:
        store = FakeStore()
        kill = _EffectDuringKill(store, "run-1")
        api = _api(store, kill)
        run = started(api)
        _grant(store, run)
        answer = api.call("run.stop", {"run_id": run})
        self.assertEqual(answer["code"], "stopped")
        self.assertEqual(answer["external_effects"], "unresolved")
        stored = api.call("run.status", {"run_id": run})["run"]["stop_attestation"]
        self.assertEqual(stored["external_effects"], "unresolved")

    def test_no_event_store_answers_unresolved(self) -> None:
        api = _bare_api()
        run = started(api)
        answer = api.call("run.stop", {"run_id": run})
        self.assertEqual(answer["code"], "stopped")
        self.assertEqual(answer["external_effects"], "unresolved")
        stored = api.call("run.status", {"run_id": run})["run"]["stop_attestation"]
        self.assertEqual(stored["external_effects"], "unresolved")

    def test_todays_answer_claims_no_dispatch_fence(self) -> None:
        api = _api(FakeStore(), FakeKillTree())
        run = started(api)
        answer = api.call("run.stop", {"run_id": run})
        self.assertIs(answer["admission_closed"], True)
        self.assertIs(answer["dispatch_fence_confirmed"], False)
        bare = _bare_api()
        bare_run = started(bare)
        bare_answer = bare.call("run.stop", {"run_id": bare_run})
        self.assertIs(bare_answer["dispatch_fence_confirmed"], False)

    def test_run_dispatch_is_not_reachable_through_call(self) -> None:
        store = FakeStore()
        api = _api(store, FakeKillTree())
        run = started(api)
        unknown = api.call("run.dispatch", {"run_id": "nope"})
        self.assertEqual(unknown["code"], "method_unknown")
        before = api.call("run.dispatch", {"run_id": run})
        self.assertEqual(before["code"], "method_unknown")
        self.assertFalse(api._dispatch_refused(run, api._stop_target(run)))
        api.call("run.stop", {"run_id": run})
        self.assertTrue(api._dispatch_refused(run, api._stop_target(run)))
        again = api.call("run.dispatch", {"run_id": run})
        self.assertEqual(again["code"], "method_unknown")

    def test_an_exception_inside_accept_cannot_skip_the_kill(self) -> None:
        kill = FakeKillTree()
        api = _bare_api(kill)
        run = started(api)

        def boom(_run_id: str) -> None:
            raise RuntimeError("accept")

        api._record_revoke = boom
        answer = api.call("run.stop", {"run_id": run})
        self.assertEqual(answer["code"], "stopped")
        self.assertEqual(kill.requests, [f"/sys/fs/cgroup/fake/{run}"])

    def test_b2_27_a_stopped_answer_lacking_the_facts_is_refused_by_the_schema(self) -> None:
        schema = json.loads(_STOP_SCHEMA.read_text(encoding="utf-8"))
        branch = next(item for item in schema["allOf"]
                      if item.get("if", {}).get("properties", {}).get("code", {}).get("const") == "stopped")
        required = branch["then"]["required"]
        missing = [name for name in _FACTS if name not in required]
        self.assertEqual(missing, [])
        self.assertEqual(schema["properties"]["external_effects"]["enum"], ["none_known", "unresolved"])
        text = _REGISTRY.read_text(encoding="utf-8")
        for name in _FACTS:
            self.assertIn(name, text)
        self._schema_refuses_a_stopped_answer_without_the_facts(schema)

    def _schema_refuses_a_stopped_answer_without_the_facts(self, schema: dict) -> None:
        try:
            import jsonschema
            from referencing import Registry, Resource
            from referencing.jsonschema import DRAFT202012
        except ImportError:
            return  # the required-list check above already ran; this interpreter has no jsonschema
        folder = _STOP_SCHEMA.parent
        resources = []
        for path in folder.glob("*.json"):
            body = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(body, dict) and body.get("$schema"):
                resources.append((path.name, Resource(contents=body, specification=DRAFT202012)))
        validator = jsonschema.Draft202012Validator(schema, registry=Registry().with_resources(resources))
        base = {"code": "stopped", "run_id": "run-1", "state": "stopped", "repeat": False,
                "admission_closed": True, "local_scope_empty": True, "external_effects": "none_known"}
        self.assertEqual(list(validator.iter_errors(base)), [])
        for dropped in ("admission_closed", "external_effects"):
            broken = dict(base)
            del broken[dropped]
            self.assertTrue(list(validator.iter_errors(broken)), dropped)
        wrong = dict(base, external_effects="reconciled")
        self.assertTrue(list(validator.iter_errors(wrong)))
        attested = dict(base, attestation={
            "proof": "empty", "cgroup": "/cg/run-1", "cgroup_identity": None, "time": "2026-11-17T00:00:00.000Z"})
        self.assertTrue(list(validator.iter_errors(attested)))
        attested["attestation"] = {**attested["attestation"], "admission_closed": True,
                                   "local_scope_empty": True, "external_effects": "none_known"}
        self.assertEqual(list(validator.iter_errors(attested)), [])

    def test_schema_records_the_effects_word_the_view_and_the_fence_fact(self) -> None:
        folder = _STOP_SCHEMA.parent
        stop = json.loads(_STOP_SCHEMA.read_text(encoding="utf-8"))
        record = json.loads((folder / "run.record.v1.json").read_text(encoding="utf-8"))
        view = json.loads((folder / "run.view.v1.json").read_text(encoding="utf-8"))
        changes = (folder / "CHANGES.md").read_text(encoding="utf-8")
        effects = ["none_known", "unresolved"]
        self.assertEqual(record["$defs"]["stop_attestation"]["properties"]["external_effects"]["enum"], effects)
        attestation = stop["$defs"]["attestation"]["properties"]["external_effects"]
        self.assertEqual(attestation["enum"], effects)
        self.assertEqual(stop["properties"]["workspace.revoke"].get("const"), True)
        self.assertEqual(view["x-version"], "1.0-draft.5")
        self.assertIn("`run.view.v1.json` | 1.0-draft.5", changes)
        self.assertIn("A draft-2 reader refuses a draft-3 stopped answer", changes)
        self.assertIn("dispatch_fence_confirmed", stop["properties"])
        self.assertEqual(stop["properties"]["dispatch_fence_confirmed"]["type"], "boolean")

    def test_an_old_revoke_does_not_hide_a_grant_that_stands_again(self) -> None:
        # Devin thread on api.py:575: a revoke in the log must not skip the ledger when a grant stands again.
        store = FakeStore()
        api = _api(store, FakeKillTree())
        run = started(api)
        _grant(store, run)
        with store.transaction() as tx:
            self.assertIsNotNone(Ledger.revoked(tx, run))
        with store.transaction() as tx:
            self.assertIsNotNone(Ledger.granted(tx, run, "/work/run"))
        answer = api.call("run.stop", {"run_id": run})
        self.assertEqual(_standing(store), [], "stop left a grant standing")
        self.assertIs(answer.get("workspace.revoke"), True)
        self.assertIsNotNone(_revoked_seq(store, run))

    def test_a_grant_that_stands_is_not_named_revoked(self) -> None:
        store = FakeStore()
        api = _api(store, FakeKillTree())
        run = started(api)
        _grant(store, run)
        with store.transaction() as tx:
            self.assertIsNotNone(Ledger.revoked(tx, run))
        with store.transaction() as tx:
            self.assertIsNotNone(Ledger.granted(tx, run, "/work/run"))
        self.assertFalse(api._revoke_named(run), "a standing grant is named revoked")
        self.assertEqual(_standing(store), [run])

    def test_a_stop_that_lands_during_launch_revokes_the_grant_the_starter_records(self) -> None:
        # Devin thread on api.py:292: Stop accepts while the run is starting, then the starter records the grant.
        store = FakeStore()
        _ensure_ledger(store)
        entered, release = threading.Event(), threading.Event()
        run_box: dict = {}
        out: list = []

        def starter(params: dict) -> dict:
            run_box["id"] = params["run_id"]
            entered.set()
            self.assertTrue(release.wait(5), "stop did not land during launch")
            with store.transaction() as tx:
                Ledger.granted(tx, params["run_id"], "/work/run")
            return {"cgroup": f"/sys/fs/cgroup/fake/{params['run_id']}"}

        api = ControlAPI(InMemoryRunRegistry(), FakeKillTree(), starter, clock=lambda: 1000.0,
                         new_run_id=run_ids(), events=store)
        thread = threading.Thread(target=lambda: out.append(api.call("run.start", _START)))
        thread.start()
        try:
            self.assertTrue(entered.wait(5), "starter did not begin")
            pending = api.call("run.stop", {"run_id": run_box["id"]})
            self.assertEqual(pending["code"], "pending")
            self.assertEqual(pending["reason"], "cgroup_unknown")
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(out), 1)
        started_answer = out[0]
        self.assertEqual(started_answer["stop"]["code"], "stopped")
        self.assertEqual(_standing(store), [], "the launch continuation left the grant standing")
        if started_answer["stop"].get("workspace.revoke") is True:
            self.assertIsNotNone(_revoked_seq(store, run_box["id"]))

    def test_a_stale_running_write_revokes_the_grant_the_starter_records(self) -> None:
        # Devin thread on api.py:402: the continuation of a stale running write stops without recording the revoke.
        store = FakeStore()
        _ensure_ledger(store)

        def starter(params: dict) -> dict:
            with store.transaction() as tx:
                Ledger.granted(tx, params["run_id"], "/work/run")
            return {"cgroup": f"/sys/fs/cgroup/fake/{params['run_id']}"}

        registry = _StaleRunningPut()
        api = ControlAPI(registry, FakeKillTree(), starter, clock=lambda: 1000.0,
                         new_run_id=run_ids(), events=store)
        answer = api.call("run.start", _START)
        self.assertEqual(answer["code"], "started")
        self.assertEqual(registry.get("run-1")["state"], "stopped")
        self.assertEqual(_standing(store), [], "the stale continuation left the grant standing")
        self.assertIsNotNone(_revoked_seq(store, "run-1"))

    def test_a_store_with_no_events_list_still_appends_the_revoke(self) -> None:
        # Devin thread on the event store: the real store has no .events, so a pre-check there appends nothing.
        inner = FakeStore()
        store = _LedgerOnly(inner)
        self.assertFalse(hasattr(store, "events"))
        api = ControlAPI(InMemoryRunRegistry(), FakeKillTree(), FakeStarter(), clock=lambda: 1000.0,
                         new_run_id=run_ids(), events=store)
        run = started(api)
        _grant(store, run)
        answer = api.call("run.stop", {"run_id": run})
        self.assertEqual(answer["code"], "stopped")
        self.assertEqual(_standing(store), [], "the revoke was never appended")
        self.assertIsNotNone(_revoked_seq(store, run))
        self.assertIn("workspace.revoke", [event["type"] for event in inner.events])
        self.assertIs(answer.get("workspace.revoke"), True)
        self.assertEqual(answer["external_effects"], "unresolved")

    def _stop_validator(self, schema: dict):
        try:
            import jsonschema
            from referencing import Registry, Resource
            from referencing.jsonschema import DRAFT202012
        except ImportError:
            return None
        folder = _STOP_SCHEMA.parent
        resources = []
        for path in folder.glob("*.json"):
            body = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(body, dict) and body.get("$schema"):
                resources.append((path.name, Resource(contents=body, specification=DRAFT202012)))
        return jsonschema.Draft202012Validator(schema, registry=Registry().with_resources(resources))

    def test_stop_result_refuses_a_free_effects_word_and_a_denied_stopped_fact(self) -> None:
        schema = json.loads(_STOP_SCHEMA.read_text(encoding="utf-8"))
        pending = {"code": "pending", "run_id": "run-1", "state": "pending", "reason": "cgroup_unknown",
                   "external_effects": "reconciled"}
        denied_scope = {"code": "stopped", "run_id": "run-1", "state": "stopped", "repeat": False,
                        "admission_closed": True, "local_scope_empty": False,
                        "external_effects": "none_known"}
        denied_acceptance = dict(denied_scope, local_scope_empty=True, admission_closed=False)
        validator = self._stop_validator(schema)
        if validator is not None:
            self.assertTrue(list(validator.iter_errors(pending)), "pending effects word")
            self.assertTrue(list(validator.iter_errors(denied_scope)), "local_scope_empty")
            self.assertTrue(list(validator.iter_errors(denied_acceptance)), "admission_closed")
            good = dict(denied_scope, local_scope_empty=True)
            self.assertEqual(list(validator.iter_errors(good)), [])
            bare = {"code": "pending", "run_id": "run-1", "state": "pending", "reason": "cgroup_unknown"}
            self.assertEqual(list(validator.iter_errors(bare)), [])
        self.assertEqual(schema["properties"]["external_effects"].get("enum"), ["none_known", "unresolved"])
        self.assertEqual(schema["x-version"], "1.0-draft.4")
        branch = next(item for item in schema["allOf"]
                      if item.get("if", {}).get("properties", {}).get("code", {}).get("const") == "stopped")
        props = branch["then"]["properties"]
        self.assertIs(props["admission_closed"].get("const"), True)
        self.assertIs(props["local_scope_empty"].get("const"), True)
        index = json.loads((folder := _STOP_SCHEMA.parent / "fixtures" / "index.json").read_text(encoding="utf-8"))
        names = {item["file"] for item in index["fixtures"]}
        for name in (
            "bad/stop.result.v1/b21-pending-with-an-effects-word-nobody-records.json",
            "bad/stop.result.v1/b22-stopped-with-local-scope-empty-false.json",
            "bad/stop.result.v1/b23-stopped-with-admission-closed-false.json",
        ):
            self.assertIn(name, names)
            body = json.loads((folder.parent / name).read_text(encoding="utf-8"))
            if validator is not None:
                self.assertTrue(list(validator.iter_errors(body)), name)
