"""Stop shuts the run's admission and records workspace.revoke before the kill.

PRODUCT.md L976 (S6 / DL-38) and L1852 (F3). NOTE.md B2-17.3 and B2-27.1.
The effects word is only none_known or unresolved. A fourth research word is not recorded.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import EMPTY, InMemoryRunRegistry
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


class StopAdmissionTest(unittest.TestCase):
    def test_b2_17_stop_refuses_a_later_dispatch_and_records_revoke_before_the_kill(self) -> None:
        store = FakeStore()
        kill = _OrderingKill(store, "run-1")
        api = _api(store, kill)
        run = started(api)
        _grant(store, run)
        before = api.call("run.dispatch", {"run_id": run})
        self.assertEqual(before, {"code": "ok", "run_id": run})
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
        refused = api.call("run.dispatch", {"run_id": run})
        self.assertEqual(refused, {"code": "refused", "run_id": run, "detail": "admission_closed"})
        again = api.call("run.dispatch", {"run_id": run})
        self.assertEqual(again, refused)
        repeat = api.call("run.stop", {"run_id": run})
        self.assertIs(repeat["admission_closed"], True)
        self.assertIs(repeat["workspace.revoke"], True)
        self.assertEqual(sum(event["type"] == "workspace.revoke" for event in store.events), 1)
        unknown = api.call("run.dispatch", {"run_id": "nope"})
        self.assertEqual(unknown["code"], "run_unknown")
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

    def test_b2_27_a_stopped_answer_lacking_the_facts_is_refused_by_the_schema(self) -> None:
        schema = json.loads(_STOP_SCHEMA.read_text(encoding="utf-8"))
        branch = next(item for item in schema["allOf"]
                      if item.get("if", {}).get("properties", {}).get("code", {}).get("const") == "stopped")
        required = branch["then"]["required"]
        missing = [name for name in _FACTS if name not in required]
        self.assertEqual(missing, [])
        self.assertEqual(branch["then"]["properties"]["external_effects"]["enum"], ["none_known", "unresolved"])
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
