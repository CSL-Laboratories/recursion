"""A session.stop answer of stopped while a run is still pending.

The published schema accepts that answer, and ``rules.fits`` stays true. The semantic rule and the
conformance client report it. The published pending fixture stays clean.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import rules

try:
    import wire_client as client
except ImportError:  # jsonschema is present on 3.12 and absent on free-threaded 3.14
    client = None

SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "control" / "v1"
H13_FILE = "good/methods.v1/h13-session-stop-of-a-subtree-still-pending.json"
ENTRY = {
    "file": H13_FILE,
    "schema": "methods.v1.json#/$defs/exchange",
    "shows": "A subtree Stop is stopped only when every run in it is; one pending run makes it pending.",
}


def _h13() -> dict:
    return json.loads((SCHEMA / "fixtures" / H13_FILE).read_text(encoding="utf-8"))


def _problems(exchange: dict) -> list:
    report = getattr(rules, "session_stop_problems", None)
    if report is None:
        return []
    return report(exchange)


class SessionStopRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h13 = _h13()
        self.schemas = None if client is None else client.SchemaSet(SCHEMA)

    def _fits(self, exchange: dict) -> bool:
        if self.schemas is None:
            return rules.fits(ENTRY["schema"], exchange)
        return rules.fits(ENTRY["schema"], exchange, self.schemas.schemas)

    def _client(self, exchange: dict):
        if self.schemas is None:
            return None
        return client.check_instance(self.schemas, ENTRY, exchange)

    def test_published_pending_answer_is_clean(self) -> None:
        self.assertEqual(_problems(self.h13), [])
        if self.schemas is not None:
            self.assertIsNone(self._client(self.h13))
            self.assertTrue(self._fits(self.h13))

    def test_stopped_while_a_run_is_pending_is_reported(self) -> None:
        mutated = copy.deepcopy(self.h13)
        mutated["answer"]["code"] = "stopped"
        self.assertTrue(self._fits(mutated), "the schema accepts a stopped answer over a pending run")
        self.assertTrue(_problems(mutated), "a stopped answer names a run that is still pending")
        if self.schemas is not None:
            self.assertIsNotNone(self._client(mutated), "the client reports the stopped answer")

    def test_pending_list_names_the_pending_runs(self) -> None:
        mutated = copy.deepcopy(self.h13)
        mutated["answer"]["pending"] = []
        self.assertTrue(self._fits(mutated), "the schema accepts a pending list that names nothing")
        self.assertTrue(_problems(mutated), "pending does not name the pending run")
        if self.schemas is not None:
            self.assertIsNotNone(self._client(mutated), "the client reports the unnamed pending run")

    def test_stopped_requires_every_run_stopped(self) -> None:
        mutated = copy.deepcopy(self.h13)
        mutated["answer"]["code"] = "stopped"
        mutated["answer"]["pending"] = []
        mutated["answer"]["runs"] = [{
            "code": "ended",
            "run_id": "run-5b1f0c2d9a7e4f36b8c1d2e3f4a5b6c7",
            "state": "ended",
        }]
        self.assertTrue(self._fits(mutated), "the schema accepts a stopped answer over an ended run")
        self.assertTrue(_problems(mutated), "a stopped answer includes a run that is not stopped")
        if self.schemas is not None:
            self.assertIsNotNone(self._client(mutated), "the client reports the run that is not stopped")


if __name__ == "__main__":
    unittest.main()
