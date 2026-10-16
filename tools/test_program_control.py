#!/usr/bin/env python3
"""Focused negative and positive tests for the CSL-204 program ledger."""
from __future__ import annotations

import copy
import importlib.util
import json
import hashlib
import tempfile
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

    def assert_rejected(self, fragment, data=None):
        errors = self.errors(data)
        self.assertTrue(any(fragment in error for error in errors), errors)

    def approve(self, grants):
        """Synthetic receipts only; never written to the checked-in ledger."""
        targets = {"accept_contract": {c["id"]: c for c in self.data["contracts"]},
                   "develop_work": {w["id"]: w for w in self.data["work_items"]},
                   "decide_capability": {c["id"]: c for c in self.data["capabilities"]},
                   "admit_work": {self.data["program_id"]: self.data["admission"]}}
        contract_ids = set()
        authorizations = []
        for action, target_id in grants:
            target = targets[action][target_id]
            authorizations.append({"action": action, "target": target_id,
                                   "target_digest": checker.target_digest(action, target)})
            if action == "accept_contract":
                contract_ids.add(target_id)
            else:
                contract_ids.update(target.get("contract_ids", target.get("consumes_contracts", [])))
        proposal = {"id": f"proposal.fixture-{len(self.data['proposals'])}",
                    "linear_issue": "CSL-204",
                    "scope": [target for _, target in grants],
                    "contract_bindings": [checker.contract_binding(c) for c in self.data["contracts"]
                                          if c["id"] in contract_ids],
                    "authorizations": authorizations}
        self.data["proposals"].append(proposal)
        receipt = {"id": f"decision.fixture-{len(self.data['decisions'])}",
                   "status": "approved", "proposal_id": proposal["id"],
                   "proposal_digest": checker.digest(proposal), "scope": proposal["scope"],
                   "linear_issue": "CSL-204", "approval_comment_id": "synthetic-test-only",
                   "human_decider": "Fixture human", "authority_basis": "Fixture delegation",
                   "selected_option": "Fixture", "rejected_options": ["Fixture alternative"],
                   "rationale": "Test only", "conditions": ["Test only"], "decided_on": "2026-10-16",
                   "approval_evidence": "Synthetic fixture, not a real approval", "revisit_trigger": "Any change"}
        self.data["decisions"].append(receipt)
        return receipt["id"]

    def promote(self, tier="provider_free_verified"):
        capability = self.data["capabilities"][0]
        for unknown in self.data["unknowns"]:
            unknown.update(status="resolved", resolution_evidence="Synthetic fixture only")
        for contract in self.data["contracts"]:
            contract["status"] = "accepted"
            contract["decision_receipt"] = self.approve([("accept_contract", contract["id"])])
        capability["decision_receipt"] = self.approve([("decide_capability", capability["id"])])
        capability.update(current_maturity=tier, candidate_commit="a" * 40)
        kinds = {"prototyped": "prototype", "implemented": "implementation",
                 "provider_free_verified": "provider_free", "locally_qualified": "local_qualification",
                 "release_qualified": "release_qualification"}
        row = self.data["evidence"][0]
        row.update(capability_id=capability["id"], candidate_commit="a" * 40, result="pass",
                   status="current", tier=tier, kind=kinds.get(tier, "candidate"), independent=True,
                   artifact_sha256="b" * 64,
                   contract_bindings=[checker.contract_binding(c) for c in self.data["contracts"]])
        return capability, row

    def admit(self, mode="bounded_autonomy"):
        self.promote()
        item = self.data["work_items"][0]
        item["consumes_contracts"] = [self.data["contracts"][0]["id"]]
        item["contract_receipts"] = {self.data["contracts"][0]["id"]:
                                    self.data["contracts"][0]["decision_receipt"]}
        admission = self.data["admission"]
        admission.update(mode=mode, max_active_contract_consumers=1, max_concurrent_reviews=1,
                         active_contract_consumers=[item["id"]])
        admission["decision_receipt"] = self.approve([("admit_work", self.data["program_id"])])
        return admission

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

    def test_unknown_blocks_at_and_above_but_not_below_tier(self):
        self.promote()
        unknown = self.data["unknowns"][0]
        unknown["status"] = "open"
        cap = self.data["capabilities"][0]
        cap["current_maturity"] = "contract_frozen"
        self.assertEqual(self.errors(), [])
        for tier in ("provider_free_verified", "locally_qualified", "release_qualified"):
            with self.subTest(tier=tier):
                cap["current_maturity"] = tier
                self.assert_rejected("blocked by unresolved")

    def test_unknown_requires_explicit_resolution_and_preserves_history(self):
        self.data["unknowns"][0]["status"] = "resolved"
        self.assert_rejected("without resolution evidence")
        self.data["unknowns"][0]["resolution_evidence"] = "Fixture resolution"
        self.assertEqual(self.errors(), [])
        self.data["unknowns"][0]["status"] = "waived"
        self.assert_rejected("requires open or resolved")

    def test_valid_promotion_can_exceed_month_target(self):
        for tier in ("human_decided", "contract_frozen", "prototyped", "implemented",
                     "provider_free_verified", "locally_qualified", "release_qualified"):
            with self.subTest(tier=tier):
                self.setUp()
                cap, _ = self.promote(tier)
                cap["month_target"] = "researched"
                self.assertEqual(self.errors(), [])

    def test_reordered_maturity_cannot_bypass_gates(self):
        self.data["maturity_order"].reverse()
        self.assert_rejected("maturity_order must match")

    def test_frozen_candidate_contracts_are_rejected(self):
        for tier in ("contract_frozen", "provider_free_verified", "release_qualified"):
            with self.subTest(tier=tier):
                self.setUp()
                self.promote(tier)
                self.data["contracts"][0]["status"] = "candidate"
                self.assert_rejected("requires accepted contract")

    def test_human_decided_requires_applicable_receipt(self):
        cap, _ = self.promote("human_decided")
        cap["decision_receipt"] = self.data["contracts"][0]["decision_receipt"]
        self.assert_rejected("lacks applicable human decision authority")

    def test_accepted_contract_requires_its_own_receipt(self):
        self.promote()
        self.data["contracts"][0]["decision_receipt"] = self.data["contracts"][1]["decision_receipt"]
        self.assert_rejected("accepted contract lacks applicable human receipt")

    def test_stale_proposal_digest_and_wrong_named_proposal_are_rejected(self):
        self.promote()
        decision = self.data["decisions"][-1]
        decision["proposal_digest"] = "f" * 64
        self.assert_rejected("stale proposal digest")
        decision["proposal_digest"] = checker.digest(self.data["proposals"][-1])
        decision["proposal_id"] = self.data["proposals"][0]["id"]
        self.assert_rejected("stale proposal digest")

    def test_changed_actual_contract_and_updated_registry_invalidate_receipt(self):
        self.promote()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for contract in self.data["contracts"]:
                path = root / contract["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / contract["path"]).read_bytes())
            contract = self.data["contracts"][0]
            path = root / contract["path"]
            path.write_bytes(path.read_bytes() + b"\nChanged contract.\n")
            contract["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            errors = checker.validate_program(self.data, root)
            self.assertTrue(any("stale or incomplete contract bindings" in error for error in errors), errors)
            self.assertFalse(any("digest mismatch" in error for error in errors), errors)

    def test_changed_revision_invalidates_receipt(self):
        self.promote()
        self.data["contracts"][0]["revision"] += 1
        self.assert_rejected("stale or incomplete contract bindings")

    def test_changed_proposal_scope_or_grant_invalidates_receipt(self):
        self.promote()
        self.data["proposals"][-1]["scope"].append("CSL-999")
        self.assert_rejected("stale proposal digest")

    def test_failed_obsolete_unrelated_and_lower_tier_evidence_is_rejected(self):
        variants = {"result": ["fail", "candidate"], "status": ["obsolete", "superseded", None],
                    "independent": [False, "true", 1], "candidate_commit": ["branch", "c" * 40],
                    "capability_id": ["capability.unrelated"], "tier": ["implemented"],
                    "kind": ["prototype"], "artifact_sha256": ["branch"], "contract_bindings": [[]]}
        for key, values in variants.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.setUp()
                    _, row = self.promote()
                    row[key] = value
                    self.assert_rejected("without independent verification")

    def test_candidate_change_needs_fresh_evidence(self):
        cap, _ = self.promote()
        cap["candidate_commit"] = "d" * 40
        self.assert_rejected("without independent verification")

    def test_higher_tier_needs_its_own_evidence(self):
        cap, _ = self.promote()
        cap["current_maturity"] = "locally_qualified"
        self.assert_rejected("without independent verification")

    def test_human_gated_false_consequential_requires_applicable_receipt(self):
        item = self.data["work_items"][0]
        item["discretion_class"] = "human_gated"
        self.assertFalse(item["consequential"])
        self.assert_rejected("lacks an applicable approved human decision receipt")
        item["decision_receipt"] = self.approve([("develop_work", item["id"])])
        self.assertEqual(self.errors(), [])
        item["stop_conditions"].append("New boundary")
        self.assert_rejected("stale authorization target")

    def test_both_admission_modes_enforce_consumer_and_review_capacity(self):
        for mode in ("open", "bounded_autonomy"):
            for field in ("active_contract_consumers", "active_reviews"):
                with self.subTest(mode=mode, field=field):
                    self.setUp()
                    admission = self.admit(mode)
                    self.assertEqual(self.errors(), [])
                    other = copy.deepcopy(self.data["work_items"][0])
                    other["id"] = "CSL-999"
                    self.data["work_items"].append(other)
                    if field == "active_reviews":
                        self.data["work_items"][0]["stage"] = "verification"
                        other["stage"] = "verification"
                    admission[field] = ["CSL-204", "CSL-999"]
                    self.assert_rejected("admission exceeds max_")

    def test_occupied_bounded_admission_requires_envelope_receipt(self):
        admission = self.admit()
        admission["decision_receipt"] = None
        self.assert_rejected("bounded_autonomy admission lacks")

    def test_candidate_receipt_cannot_authorize_admission(self):
        admission = self.admit("open")
        admission["decision_receipt"] = self.data["contracts"][0]["decision_receipt"]
        self.assert_rejected("open admission lacks")

    def test_changed_capacity_invalidates_admission_receipt(self):
        admission = self.admit()
        admission["max_active_contract_consumers"] = 20
        self.assert_rejected("stale authorization target")

    def test_admission_rejects_null_zero_boolean_caps_when_occupied(self):
        for mode in ("open", "bounded_autonomy"):
            for value in (None, 0, True, -1):
                with self.subTest(mode=mode, value=value):
                    self.setUp()
                    admission = self.admit(mode)
                    admission["max_concurrent_reviews"] = value
                    self.assert_rejected("positive max_concurrent_reviews")

    def test_occupancy_rejects_duplicate_unknown_and_omitted_ids(self):
        for values, expected in ((["CSL-204", "CSL-204"], "duplicate or unknown"),
                                 (["CSL-999"], "duplicate or unknown"), ([], "omits active work")):
            with self.subTest(values=values):
                self.setUp()
                self.admit()["active_contract_consumers"] = values
                self.assert_rejected(expected)

    def test_verification_work_cannot_hide_review_occupancy(self):
        self.data["work_items"][0]["stage"] = "verification"
        self.assert_rejected("active_reviews omits active work")
        self.assert_rejected("bounded_autonomy admission lacks")

    def test_completed_or_wrong_lane_work_is_rejected_not_counted(self):
        for field in ("active_contract_consumers", "active_reviews"):
            for status in ("done", "accepted", "verified", "todo"):
                with self.subTest(field=field, status=status):
                    self.setUp()
                    admission = self.admit()
                    old = self.data["work_items"][0]
                    current = copy.deepcopy(old)
                    current["id"] = "CSL-999"
                    self.data["work_items"].append(current)
                    old["status"] = status
                    if field == "active_reviews":
                        old["stage"] = current["stage"] = "verification"
                    admission["active_contract_consumers"] = [current["id"]]
                    admission[field] = [old["id"], current["id"]]
                    self.assert_rejected("stale or wrong-lane work")
                    self.assertFalse(any("exceeds max_" in error for error in self.errors()))
                    admission[field] = [current["id"]]
                    self.assertEqual(self.errors(), [])
        self.setUp()
        admission = self.admit()
        admission["active_reviews"] = ["CSL-204"]
        self.assert_rejected("active_reviews contains stale or wrong-lane work")
        admission["active_reviews"] = []
        self.data["work_items"][0]["consumes_contracts"] = []
        self.assert_rejected("active_contract_consumers contains stale or wrong-lane work")

    def test_closed_admission_rejects_active_consumers(self):
        self.admit()["mode"] = "closed_pending_human_limits"
        self.assert_rejected("closed admission has active work")

    def test_routine_work_in_approved_envelope_needs_no_task_receipt(self):
        self.admit()
        self.assertNotIn("decision_receipt", self.data["work_items"][0])
        self.assertEqual(self.errors(), [])

    def test_contract_consumer_must_pin_matching_acceptance_receipt(self):
        self.admit()
        self.data["work_items"][0]["contract_receipts"] = {}
        self.assert_rejected("lacks matching contract receipt")

    def test_each_mandatory_research_field_is_enforced(self):
        for field in checker.PLAN_FIELDS:
            with self.subTest(field=field):
                self.setUp()
                self.data["research_control"]["required_plan_fields"].remove(field)
                self.assert_rejected("required_plan_fields must include")

    def test_recorded_research_plan_cannot_omit_source_class(self):
        item = self.data["work_items"][0]
        item["research_plan"] = {field: "Fixture" for field in checker.PLAN_FIELDS}
        self.assertEqual(self.errors(), [])
        del item["research_plan"]["source_class"]
        self.assert_rejected("research plan is missing mandatory fields")

    def test_done_research_does_not_bypass_dependency_closure(self):
        item = self.data["work_items"][0]
        prerequisite = copy.deepcopy(item)
        prerequisite.update(id="CSL-998", stage="research", status="todo")
        research = copy.deepcopy(item)
        research.update(id="CSL-999", stage="research", status="done", hard_dependencies=["CSL-998"])
        research["research_plan"] = {field: "Fixture" for field in checker.PLAN_FIELDS}
        prerequisite["research_plan"] = {field: "Fixture" for field in checker.PLAN_FIELDS}
        self.data["work_items"].extend([research, prerequisite])
        item["hard_dependencies"] = ["CSL-999"]
        self.assert_rejected("CSL-999 starts before hard dependency CSL-998 is complete")
        prerequisite["status"] = "done"
        self.assertEqual(self.errors(), [])

    def test_done_dependency_cycle_is_rejected(self):
        item = self.data["work_items"][0]
        item.update(status="done", stage="research", hard_dependencies=[item["id"]])
        self.assert_rejected("hard dependency cycle")

    def test_active_and_complete_research_requires_plan(self):
        for status in ("in_progress", "done", "accepted", "verified"):
            with self.subTest(status=status):
                self.data["work_items"][0].update(stage="research", status=status)
                self.assert_rejected("research plan is missing mandatory fields")

    def test_wrong_linear_issue_cannot_authorize_work(self):
        item = self.data["work_items"][0]
        item["discretion_class"] = "human_gated"
        item["decision_receipt"] = self.approve([("develop_work", item["id"])])
        self.assertEqual(self.errors(), [])
        self.data["decisions"][-1]["linear_issue"] = "CSL-999"
        self.assert_rejected("receipt requires the matching Linear issue")
        self.data["proposals"][-1]["linear_issue"] = "CSL-999"
        self.data["decisions"][-1]["proposal_digest"] = checker.digest(self.data["proposals"][-1])
        self.assert_rejected("authorization requires the matching Linear issue")

    def test_superseded_packets_preserve_history_without_blocking_current_receipts(self):
        self.promote("contract_frozen")
        old_packets = copy.deepcopy(self.data["proposals"])
        for decision in self.data["decisions"][1:]:
            decision["status"] = "superseded"
        for contract in self.data["contracts"]:
            contract["revision"] += 1
            contract["decision_receipt"] = self.approve([("accept_contract", contract["id"])])
        capability = self.data["capabilities"][0]
        capability["decision_receipt"] = self.approve([("decide_capability", capability["id"])])
        self.assertEqual(self.data["proposals"][:len(old_packets)], old_packets)
        self.assertEqual(self.errors(), [])
        self.data["decisions"][1]["status"] = "approved"
        self.assert_rejected("stale or incomplete contract bindings")


if __name__ == "__main__":
    unittest.main()
