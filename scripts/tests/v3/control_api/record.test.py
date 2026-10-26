#!/usr/bin/env python3
"""The committed fake-driver recording must equal a fresh run through the Control API.

Replays the whole scenario over loopback HTTP and compares it byte for byte with
``recordings/spine-fake-run.json``. Regenerate only on purpose:
``python -X utf8 -B -m scripts.v3.control_api.record --write``.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v3.control_api import record  # noqa: E402
from scripts.v3.route_contract import schema as contract_schema  # noqa: E402


def contract_validator(name: str) -> jsonschema.Draft202012Validator:
    defs = contract_schema.load_schema()["$defs"]
    return jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": defs})


class RecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.committed_text = record.RECORDING_PATH.read_text(encoding="utf-8")
        cls.committed = json.loads(cls.committed_text)
        cls.fresh = record.run_scenario()

    def test_a_fresh_run_equals_the_committed_recording_byte_for_byte(self) -> None:
        self.assertEqual(self.committed_text, record.render(self.fresh))

    def test_the_transcript_digest_is_self_consistent(self) -> None:
        self.assertEqual(self.committed["transcript_sha256"], record.transcript_digest(self.committed))
        tampered = json.loads(self.committed_text)
        tampered["steps"][3]["http_status"] = 500
        self.assertNotEqual(self.committed["transcript_sha256"], record.transcript_digest(tampered))

    def test_both_driver_paths_ran_to_completion(self) -> None:
        ok = {}
        for step in self.committed["steps"]:
            result = (step["response"].get("result") or {})
            if isinstance(result, dict) and "result" in result and "request" in result:
                ok.setdefault(result["request"]["engine_id"], []).append(result["result"]["status"])
        self.assertEqual({"claude", "codex"}, set(ok))
        self.assertIn("ok", ok["claude"])
        self.assertIn("ok", ok["codex"])
        self.assertEqual({"claude": 3, "codex": 2}, self.committed["transport_calls"])

    def test_every_recorded_record_validates_against_the_route_contract(self) -> None:
        seen = 0
        for step in self.committed["steps"]:
            result = step["response"].get("result")
            if isinstance(result, dict) and "request" in result:
                self.assertEqual([], list(contract_validator("RouteExecutionRequest").iter_errors(result["request"])))
                self.assertEqual([], list(contract_validator("RouteExecutionResult").iter_errors(result["result"])))
                seen += 1
        self.assertEqual(5, seen)

    def test_the_recording_exercises_repair_limit_breaker_and_four_refusals(self) -> None:
        by_label = {s["label"]: s for s in self.committed["steps"]}
        repaired = by_label["run claude (invalid output, one repair, ok)"]["response"]["result"]["result"]
        self.assertEqual(("ok", True), (repaired["status"], repaired["repaired"]))
        limit = by_label["run codex (limit hit, breaker opens)"]["response"]["result"]["result"]
        self.assertEqual(("limit_hit", True), (limit["status"], limit["dispatched"]))
        breaker = by_label["run codex again (breaker open, not dispatched)"]["response"]["result"]["result"]
        self.assertEqual(("breaker_open", False), (breaker["error"], breaker["dispatched"]))
        refusals = {
            label: (s["http_status"], s["response"]["error"]["message"])
            for label, s in by_label.items()
            if label.startswith("refused:")
        }
        self.assertEqual(
            {
                "refused: no credentials": (401, "missing_credentials"),
                "refused: forged token": (401, "invalid_credentials"),
                "refused: viewer cannot run": (403, "scope_denied"),
                "refused: other tenant cannot see the session": (200, "unknown_session"),
            },
            refusals,
        )

    def test_no_live_call_and_no_secret_shaped_content_is_recorded(self) -> None:
        self.assertFalse(self.committed["live_calls"])
        self.assertEqual("fake", self.committed["driver"])
        self.assertNotRegex(self.committed_text, r"(?i)bearer\s")
        self.assertNotIn("authorization", self.committed_text.lower())
        self.assertNotRegex(self.committed_text, r"fake-[A-Za-z0-9_-]{30,}")
        digests = set(re.findall(r"[0-9a-f]{64}", self.committed_text))
        self.assertTrue(digests)
        for step in self.committed["steps"]:
            self.assertNotIn(record.PROMPT, json.dumps(step["response"]))
            self.assertNotIn(record.REPAIR_PROMPT, json.dumps(step["response"]))

    def test_steps_are_numbered_and_the_observer_journal_is_complete(self) -> None:
        self.assertEqual(list(range(1, len(self.committed["steps"]) + 1)), [s["step"] for s in self.committed["steps"]])
        journal = self.committed["steps"][-1]["response"]
        self.assertFalse(journal["reset"])
        seqs = [e["seq"] for e in journal["events"]]
        self.assertEqual(sorted(seqs), seqs)
        kinds = [e["kind"] for e in journal["events"]]
        self.assertEqual(3, kinds.count("session.opened"))
        self.assertEqual(5, kinds.count("run.started"))
        self.assertEqual(5, kinds.count("run.finished"))


if __name__ == "__main__":
    unittest.main()
