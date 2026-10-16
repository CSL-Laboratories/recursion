#!/usr/bin/env python3
"""Focused negative and positive tests for the CSL-204 program ledger."""
from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools/check_program_control.py"
LEDGER = ROOT / "docs/v3/program-control/program.json"

spec = importlib.util.spec_from_file_location("check_program_control", CHECKER)
assert spec and spec.loader
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class ProgramControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads(LEDGER.read_text(encoding="utf-8"))

    def errors(self, data=None):
        return checker.validate_program(data or self.data, ROOT)

    def test_checked_in_ledger_is_valid(self) -> None:
        self.assertEqual(self.errors(), [])

    def test_missing_parent_lineage_is_rejected(self) -> None:
        data = copy.deepcopy(self.data)
        data["work_items"][0]["parent"] = None
        self.assertTrue(any("missing parent lineage" in error for error in self.errors(data)))

    def test_incomplete_human_decision_receipt_is_rejected(self) -> None:
        data = copy.deepcopy(self.data)
        data["decisions"][0]["status"] = "approved"
        del data["decisions"][0]["human_decider"]
        self.assertTrue(any("incomplete human receipt" in error for error in self.errors(data)))

    def test_unresolved_hard_dependency_blocks_development(self) -> None:
        data = copy.deepcopy(self.data)
        data["work_items"].append({
            "id": "CSL-999", "parent": "CSL-45", "status": "todo", "stage": "research",
            "consequential": False, "hard_dependencies": [], "consumes_contracts": [],
            "acceptance_command": "false", "independent_verification_required": True,
            "recovery": "none", "rollback": "none", "stop_conditions": ["failure"],
        })
        data["work_items"][0]["hard_dependencies"] = ["CSL-999"]
        self.assertTrue(any("starts before hard dependency" in error for error in self.errors(data)))

    def test_unaccepted_contract_blocks_consumer(self) -> None:
        data = copy.deepcopy(self.data)
        data["work_items"][0]["consumes_contracts"] = ["contract.issue-anti-drift"]
        self.assertTrue(any("consumes unaccepted contract" in error for error in self.errors(data)))

    def test_contract_digest_mismatch_is_rejected(self) -> None:
        data = copy.deepcopy(self.data)
        data["contracts"][0]["sha256"] = "0" * 64
        self.assertTrue(any("digest mismatch" in error for error in self.errors(data)))

    def test_verified_promotion_requires_independent_evidence(self) -> None:
        data = copy.deepcopy(self.data)
        data["capabilities"][0]["current_maturity"] = "provider_free_verified"
        self.assertTrue(any("without independent verification" in error for error in self.errors(data)))

    def test_open_admission_requires_human_limits_and_receipt(self) -> None:
        data = copy.deepcopy(self.data)
        data["admission"]["mode"] = "open"
        errors = self.errors(data)
        self.assertTrue(any("open admission lacks" in error for error in errors))
        self.assertTrue(any("positive max_concurrent_reviews" in error for error in errors))

    def test_bounded_autonomy_requires_explicit_agent_and_human_boundaries(self) -> None:
        data = copy.deepcopy(self.data)
        data["admission"]["human_decision_triggers"] = []
        self.assertTrue(any("non-empty human_decision_triggers" in error for error in self.errors(data)))

    def test_research_continuation_requires_human_approval(self) -> None:
        data = copy.deepcopy(self.data)
        data["research_control"]["continuation_requires_human"] = False
        self.assertTrue(any("research continuation must require human approval" in error for error in self.errors(data)))

    def test_linear_status_cannot_substitute_for_approval_comment(self) -> None:
        data = copy.deepcopy(self.data)
        data["research_control"]["status_is_authority"] = True
        self.assertTrue(any("status must not grant" in error for error in self.errors(data)))


if __name__ == "__main__":
    unittest.main()
