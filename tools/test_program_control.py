#!/usr/bin/env python3
"""Focused negative and positive tests for the CSL-204 program ledger."""
from __future__ import annotations

import copy
import importlib.util
import json
import hashlib
import subprocess
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
                   "accept_no_rollback": {c["id"]: c for c in self.data["capabilities"]},
                   "admit_work": {self.data["program_id"]: self.data["admission"]}}
        contract_ids = set()
        unknown_ids = set()
        checkpoint_ids = set()
        research_ids = set()
        authorizations = []
        for action, target_id in grants:
            target = targets[action][target_id]
            authorizations.append({"action": action, "target": target_id,
                                   "target_digest": checker.target_digest(action, target)})
            if action == "accept_contract":
                contract_ids.add(target_id)
            else:
                contract_ids.update(target.get("contract_ids", target.get("consumes_contracts", [])))
            if action == "decide_capability":
                unknown_ids.update(target.get("unknown_ids", []))
                research_ids.update(target["research_ids"])
                checkpoint_ids.update(req["checkpoint_id"] for req in target.get("checkpoint_requirements", []))
        for checkpoint in self.data["integration_checkpoints"]:
            if checkpoint["id"] in checkpoint_ids:
                contract_ids.update(checkpoint["requires"])
        proposal = {"id": f"proposal.fixture-{len(self.data['proposals'])}",
                    "linear_issue": "CSL-204",
                    "scope": [target for _, target in grants],
                    "contract_bindings": [checker.contract_binding(c) for c in self.data["contracts"]
                                          if c["id"] in contract_ids],
                    "unknown_bindings": [checker.unknown_binding(u) for u in self.data["unknowns"]
                                         if u["id"] in unknown_ids],
                    "checkpoint_bindings": [checker.checkpoint_binding(c) for c in self.data["integration_checkpoints"]
                                            if c["id"] in checkpoint_ids],
                    "research_bindings": [{"id": r["id"], "record_digest": checker.digest(r)}
                                          for r in self.data["research"] if r["id"] in research_ids],
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

    def research_fixture(self):
        capability = self.data["capabilities"][0]
        row = {"id": "research.synthetic", "capability_id": capability["id"],
               "question": "Synthetic fixture research question", "source_class": "fixture_only",
               "status": "current", "result": "pass", "continuation": False,
               "artifact": "Synthetic retained result", "artifact_sha256": "d" * 64,
               "source_commit": "a" * 40, "limitations": "Synthetic only, never persisted",
               "contract_bindings": [checker.contract_binding(c) for c in self.data["contracts"]]}
        self.data["research"].append(row)
        capability.update(research_ids=[row["id"]], research_question=row["question"], current_maturity="researched")
        return capability, row

    def resolve_unknown(self, unknown):
        """Synthetic retained resolution only; never persisted to the seed."""
        contract_ids = {cid for cap in self.data["capabilities"] if unknown["id"] in cap["unknown_ids"]
                        for cid in cap["contract_ids"]}
        row = {"id": f"evidence.resolution-{unknown['id']}", "kind": "unknown_resolution",
               "unknown_id": unknown["id"], "unknown_digest": checker.digest(checker.unknown_binding(unknown)),
               "resolution_issue": unknown["resolution_issue"], "result": "pass", "status": "current",
               "source_commit": self.data["source_commit"], "artifact": "Synthetic resolution only",
               "artifact_sha256": "f" * 64,
               "contract_bindings": [checker.contract_binding(c) for c in self.data["contracts"]
                                     if c["id"] in contract_ids]}
        self.data["evidence"].append(row)
        unknown.update(status="resolved", resolution_evidence=row["id"])
        return row

    def promote(self, tier="provider_free_verified"):
        capability = self.data["capabilities"][0]
        self.research_fixture()
        capability["acceptance_commands"] = {t: f"fixture verify {t}" for t in checker.MATURITY_ORDER[4:]}
        # Fixture-only policy; the checked-in seed deliberately leaves this unset.
        capability["checkpoint_requirements"][0]["required_from_tier"] = "release_qualified"
        # These declared runtime profiles and receipts are synthetic test data,
        # never real qualification results and never persisted to the seed.
        capability["qualification_requirements"] = {
            tier: {"evidence_class": evidence_class, "environment": {
                "id": f"test-only-{tier}", "execution_class": execution_class,
                "configuration_sha256": "e" * 64}}
            for tier, evidence_class, execution_class in (
                ("locally_qualified", "local_observation", "local_runtime"),
                ("release_qualified", "release_evidence", "release_runtime"))}
        capability["release_requirements"] = {"recovery_command": "fixture recover",
                                             "rollback_command": "fixture rollback",
                                             "no_rollback_boundary": None}
        for unknown in self.data["unknowns"]:
            self.resolve_unknown(unknown)
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
                   acceptance_command=capability["acceptance_commands"].get(tier),
                   artifact_sha256="b" * 64,
                   contract_bindings=[checker.contract_binding(c) for c in self.data["contracts"]])
        if tier in capability["qualification_requirements"]:
            row.update(copy.deepcopy(capability["qualification_requirements"][tier]))
        if tier == "release_qualified":
            self.prove_release(capability, row)
            self.close_checkpoint(capability)
        return capability, row

    def prove_release(self, capability, release_row):
        release_row["release_requirements_digest"] = checker.digest(capability["release_requirements"])
        for kind in ("recovery", "rollback"):
            row = copy.deepcopy(release_row)
            row.pop("recovery_evidence_ids", None)
            row.pop("rollback_evidence_ids", None)
            row.update(id=f"evidence.{kind}-{len(self.data['evidence'])}", kind=kind,
                       acceptance_command=capability["release_requirements"][f"{kind}_command"],
                       release_requirements_digest=checker.digest(capability["release_requirements"]))
            release_row[f"{kind}_evidence_ids"] = [row["id"]]
            self.data["evidence"].append(row)

    def verify_work(self, item, candidate="a" * 40):
        if item["status"] not in checker.DONE:
            item["status"] = "verified"
        item["candidate_commit"] = candidate
        eid = f"evidence.work-{item['id']}-{len(self.data['evidence'])}"
        item["verification_evidence_ids"] = [eid]
        row = {"id": eid, "kind": "work_verification", "work_item_id": item["id"],
               "work_digest": checker.work_binding(item, {w["id"]: w for w in self.data["work_items"]})["work_digest"],
               "candidate_commit": item["candidate_commit"],
               "result": "pass", "status": "current", "independent": True,
               "acceptance_command": item["acceptance_command"], "artifact": "Synthetic fixture only",
               "artifact_sha256": "b" * 64, "environment": "synthetic",
               "contract_bindings": [checker.contract_binding(c) for c in self.data["contracts"]
                                     if c["id"] in item["consumes_contracts"]]}
        self.data["evidence"].append(row)
        return row

    def close_checkpoint(self, capability):
        checkpoint = self.data["integration_checkpoints"][0]
        for item in self.data["work_items"]:
            if item["id"] in checkpoint["work_item_ids"]:
                self.verify_work(item)
        checkpoint["status"] = "closed"
        eid = f"evidence.checkpoint-{len(self.data['evidence'])}"
        checkpoint["evidence_ids"] = [eid]
        row = {"id": eid, "kind": "composed_checkpoint", "checkpoint_id": checkpoint["id"],
               "checkpoint_digest": checker.checkpoint_binding(checkpoint)["definition_digest"],
               "capability_id": capability["id"], "candidate_commit": capability["candidate_commit"],
               "tier": capability["current_maturity"], "result": "pass", "status": "current", "independent": True,
               "acceptance_command": checkpoint["acceptance_command"], "artifact": "Synthetic composition only",
               "artifact_sha256": "c" * 64, "environment": "synthetic",
               "contract_bindings": [checker.contract_binding(c) for c in self.data["contracts"]
                                     if c["id"] in set(checkpoint["requires"]) | set(capability["contract_ids"])],
               "work_bindings": [checker.work_binding(w, {item["id"]: item for item in self.data["work_items"]})
                                 for w in self.data["work_items"]
                                 if w["id"] in checkpoint["work_item_ids"]]}
        profile = capability.get("qualification_requirements", {}).get(capability["current_maturity"])
        if profile:
            row.update(copy.deepcopy(profile))
        self.data["evidence"].append(row)
        return row

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
        self.assertTrue(any("parent must be a CSL issue" in error for error in self.errors(data)))

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
        self.resolve_unknown(self.data["unknowns"][0])
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
                    self.assert_rejected("status must be a string" if key == "status" and value is None
                                         else "without independent verification")

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
                    if status in checker.DONE:
                        self.verify_work(old)
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
        item["research_plan"] = {**{field: "Fixture" for field in checker.PLAN_FIELDS}, "continuation": False}
        self.assertEqual(self.errors(), [])
        del item["research_plan"]["source_class"]
        self.assert_rejected("research plan is missing mandatory fields")

    def test_done_research_does_not_bypass_dependency_closure(self):
        item = self.data["work_items"][0]
        prerequisite = copy.deepcopy(item)
        prerequisite.update(id="CSL-998", stage="research", status="todo")
        research = copy.deepcopy(item)
        research.update(id="CSL-999", stage="research", status="done", hard_dependencies=["CSL-998"])
        research["research_plan"] = {**{field: "Fixture" for field in checker.PLAN_FIELDS}, "continuation": False}
        prerequisite["research_plan"] = {**{field: "Fixture" for field in checker.PLAN_FIELDS}, "continuation": False}
        self.verify_work(research)
        self.data["work_items"].extend([research, prerequisite])
        item["hard_dependencies"] = ["CSL-999"]
        self.assert_rejected("CSL-999 starts before hard dependency CSL-998 is complete")
        prerequisite["status"] = "done"
        self.verify_work(prerequisite)
        self.assert_rejected("CSL-999 completed work lacks exact-candidate")
        self.verify_work(research)
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
        self.data["research"][-1]["contract_bindings"] = [checker.contract_binding(c) for c in self.data["contracts"]]
        for row in self.data["evidence"]:
            if row.get("kind") == "unknown_resolution":
                row["contract_bindings"] = [checker.contract_binding(c) for c in self.data["contracts"]]
        capability["decision_receipt"] = self.approve([("decide_capability", capability["id"])])
        self.assertEqual(self.data["proposals"][:len(old_packets)], old_packets)
        self.assertEqual(self.errors(), [])
        self.data["decisions"][1]["status"] = "approved"
        self.assert_rejected("stale or incomplete contract bindings")

    def test_unknown_links_cannot_be_removed_or_replaced_under_old_approval(self):
        for links in ([], ["unknown.owner-contract-acceptance"]):
            with self.subTest(links=links):
                self.setUp()
                cap, _ = self.promote()
                self.data["unknowns"][0]["status"] = "open"
                cap["unknown_ids"] = links
                self.assert_rejected("stale authorization target")
                self.assert_rejected("lacks applicable human decision authority")

    def test_unknown_blocking_definition_is_bound_but_resolution_can_progress(self):
        self.promote()
        unknown = self.data["unknowns"][0]
        unknown["status"] = "open"
        self.assert_rejected("blocked by unresolved")
        unknown["status"] = "resolved"
        self.assertEqual(self.errors(), [])
        unknown["blocks"] = "release_qualified"
        self.assert_rejected("stale or incomplete unknown bindings")

    def test_pending_checkpoint_threshold_blocks_evidence_promotion(self):
        cap, _ = self.promote()
        cap["checkpoint_requirements"][0]["required_from_tier"] = None
        self.data["decisions"][-1]["status"] = "superseded"
        cap["decision_receipt"] = self.approve([("decide_capability", cap["id"])])
        self.assert_rejected("checkpoint threshold pending owner decision")
        cap["current_maturity"] = "contract_frozen"
        self.assertEqual(self.errors(), [])

    def test_checkpoint_requirement_cannot_be_removed_or_deferred_under_old_approval(self):
        for requirements in ([], None, [{"checkpoint_id": "checkpoint.program-control", "required_from_tier": None}]):
            with self.subTest(requirements=requirements):
                self.setUp()
                cap, _ = self.promote("release_qualified")
                cap["checkpoint_requirements"] = requirements
                self.assert_rejected("checkpoint_requirements must be a list" if requirements is None
                                     else "stale authorization target")

    def test_open_checkpoint_blocks_at_configured_tier_but_not_below(self):
        self.promote("locally_qualified")
        self.assertEqual(self.errors(), [])
        self.setUp()
        self.promote("release_qualified")
        self.assertEqual(self.errors(), [])
        checkpoint = self.data["integration_checkpoints"][0]
        checkpoint["status"] = "open"
        self.assert_rejected("requires closed checkpoint")
        checkpoint["status"] = "closed"
        checkpoint["evidence_ids"] = []
        self.assert_rejected("requires closed checkpoint")

    def test_composed_proof_must_match_exact_current_scope(self):
        variants = {"result": ["fail"], "status": ["obsolete"], "independent": [False, "true", 1],
                    "candidate_commit": ["b" * 40], "capability_id": ["capability.unrelated"],
                    "tier": ["provider_free_verified"], "kind": ["work_verification"],
                    "checkpoint_id": ["checkpoint.unrelated"], "checkpoint_digest": ["f" * 64],
                    "acceptance_command": ["true"], "contract_bindings": [[]], "work_bindings": [[]],
                    "artifact": [""], "artifact_sha256": ["bad"], "environment": [""]}
        for key, values in variants.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.setUp()
                    self.promote("release_qualified")
                    self.data["evidence"][-1][key] = value
                    if key == "kind":
                        self.data["evidence"][-1]["work_item_id"] = "CSL-204"
                    self.assert_rejected("requires closed checkpoint")

    def test_checkpoint_definition_change_invalidates_approval_and_composed_proof(self):
        self.promote("release_qualified")
        self.data["integration_checkpoints"][0]["acceptance_command"] = "changed fixture"
        self.assert_rejected("stale or incomplete checkpoint bindings")
        self.assert_rejected("requires closed checkpoint")

    def test_checkpoint_requires_current_verified_child_candidates(self):
        self.promote("release_qualified")
        child = self.data["work_items"][0]
        child["candidate_commit"] = "d" * 40
        self.assert_rejected("completed work lacks exact-candidate")
        self.assert_rejected("requires closed checkpoint")
        self.verify_work(child)
        # Fresh child proof does not refresh the composed run's child binding.
        child["candidate_commit"] = "d" * 40
        self.data["evidence"][-1]["candidate_commit"] = "d" * 40
        self.assertFalse(any("completed work lacks" in e for e in self.errors()))
        self.assert_rejected("requires closed checkpoint")

    def test_checkpoint_rejects_missing_or_malformed_inputs(self):
        for inputs in ([], None, ["CSL-999"], ["CSL-204", "CSL-204"]):
            with self.subTest(inputs=inputs):
                self.setUp()
                self.promote("release_qualified")
                self.data["integration_checkpoints"][0]["work_item_ids"] = inputs
                self.assert_rejected("work_item_ids" if inputs is None else "requires unique known work inputs")

    def completed_prerequisite(self, status="verified"):
        prerequisite = copy.deepcopy(self.data["work_items"][0])
        prerequisite.update(id="CSL-999", status=status, stage="research",
                            research_plan={**{field: "Fixture" for field in checker.PLAN_FIELDS}, "continuation": False})
        self.data["work_items"].append(prerequisite)
        self.data["work_items"][0]["hard_dependencies"] = [prerequisite["id"]]
        return prerequisite

    def test_each_completed_status_requires_retained_verification_to_unblock(self):
        for status in sorted(checker.DONE):
            with self.subTest(status=status):
                self.setUp()
                prerequisite = self.completed_prerequisite(status)
                self.assert_rejected("completed work lacks exact-candidate")
                self.assert_rejected("hard dependency CSL-999 lacks independent verification")
                self.verify_work(prerequisite)
                self.assertEqual(self.errors(), [])

    def test_missing_or_false_verification_flag_cannot_bypass_dependency_gate(self):
        for flag in (None, False, "true", 1):
            with self.subTest(flag=flag):
                self.setUp()
                prerequisite = self.completed_prerequisite()
                if flag is None:
                    del prerequisite["independent_verification_required"]
                else:
                    prerequisite["independent_verification_required"] = flag
                self.verify_work(prerequisite)
                self.assert_rejected("must require independent verification")
                self.assert_rejected("hard dependency CSL-999 lacks independent verification")

    def test_work_proof_must_match_exact_current_candidate_and_work(self):
        variants = {"result": ["fail"], "status": ["obsolete"], "independent": [False, "true", 1],
                    "candidate_commit": ["branch", "b" * 40], "work_item_id": ["CSL-204"],
                    "kind": ["provider_free"], "work_digest": ["f" * 64],
                    "acceptance_command": ["true"], "contract_bindings": [[{"id": "unrelated"}]],
                    "artifact": [""], "artifact_sha256": ["bad"], "environment": [""]}
        for key, values in variants.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.setUp()
                    row = self.verify_work(self.completed_prerequisite())
                    row[key] = value
                    self.assert_rejected("completed work lacks exact-candidate")
                    self.assert_rejected("hard dependency CSL-999 lacks independent verification")

    def test_work_boundary_change_invalidates_retained_proof(self):
        prerequisite = self.completed_prerequisite()
        self.verify_work(prerequisite)
        self.assertEqual(self.errors(), [])
        prerequisite["stop_conditions"].append("Changed boundary")
        self.assert_rejected("completed work lacks exact-candidate")

    def test_human_gated_work_can_complete_with_fresh_proof_under_same_approval(self):
        item = self.data["work_items"][0]
        item["discretion_class"] = "human_gated"
        item["decision_receipt"] = self.approve([("develop_work", item["id"])])
        self.verify_work(item)
        self.assertEqual(self.errors(), [])

    def test_transitive_input_change_invalidates_dependent_and_composed_proofs(self):
        prerequisite = self.completed_prerequisite()
        upstream = copy.deepcopy(prerequisite)
        upstream["id"] = "CSL-998"
        prerequisite["hard_dependencies"] = [upstream["id"]]
        self.data["work_items"].append(upstream)
        self.verify_work(upstream)
        self.verify_work(prerequisite)
        cap, _ = self.promote("release_qualified")
        self.assertEqual(self.errors(), [])
        self.verify_work(upstream, candidate="d" * 40)
        self.assert_rejected("CSL-999 completed work lacks exact-candidate")
        self.assert_rejected("CSL-204 completed work lacks exact-candidate")
        self.assert_rejected("requires closed checkpoint")
        self.verify_work(prerequisite)
        self.verify_work(self.data["work_items"][0])
        self.assertFalse(any("completed work lacks" in e for e in self.errors()))
        self.assert_rejected("requires closed checkpoint")
        self.close_checkpoint(cap)
        self.assertEqual(self.errors(), [])

    def test_qualified_tiers_reject_synthetic_or_unclassified_evidence(self):
        for tier in ("locally_qualified", "release_qualified"):
            for evidence_class in (None, "synthetic_fixture", "provider_free_fixture", "source_documented", "live_qualification"):
                with self.subTest(tier=tier, evidence_class=evidence_class):
                    self.setUp()
                    _, row = self.promote(tier)
                    row["evidence_class"] = evidence_class
                    self.assert_rejected("qualification requires approved environment/class")

    def test_qualification_environment_must_match_approved_structured_profile(self):
        for tier in ("locally_qualified", "release_qualified"):
            for environment in ("synthetic", "provider-free fixtures", None, {},
                                {"id": "other", "execution_class": "local_runtime", "configuration_sha256": "d" * 64}):
                with self.subTest(tier=tier, environment=environment):
                    self.setUp()
                    _, row = self.promote(tier)
                    row["environment"] = environment
                    self.assert_rejected("qualification requires approved environment/class")

    def test_pending_qualification_profile_blocks_only_qualified_claim(self):
        cap, _ = self.promote("locally_qualified")
        cap["qualification_requirements"]["locally_qualified"] = None
        self.data["decisions"][-1]["status"] = "superseded"
        cap["decision_receipt"] = self.approve([("decide_capability", cap["id"])])
        self.assert_rejected("qualification requires approved environment/class")
        cap["current_maturity"] = "contract_frozen"
        self.assertEqual(self.errors(), [])

    def test_profile_changes_invalidate_approval_even_with_matching_new_evidence(self):
        cap, row = self.promote("locally_qualified")
        cap["qualification_requirements"]["locally_qualified"]["environment"]["configuration_sha256"] = "f" * 64
        row.update(copy.deepcopy(cap["qualification_requirements"]["locally_qualified"]))
        self.assert_rejected("stale authorization target")

    def test_profile_cannot_authorize_synthetic_class_or_missing_environment_identity(self):
        for field, value in (("evidence_class", "synthetic_fixture"), ("execution_class", "synthetic"),
                             ("configuration_sha256", "branch"), ("id", "")):
            with self.subTest(field=field):
                self.setUp()
                cap, row = self.promote("locally_qualified")
                profile = cap["qualification_requirements"]["locally_qualified"]
                (profile if field == "evidence_class" else profile["environment"])[field] = value
                row.update(copy.deepcopy(profile))
                self.data["decisions"][-1]["status"] = "superseded"
                cap["decision_receipt"] = self.approve([("decide_capability", cap["id"])])
                self.assert_rejected("qualification requires approved environment/class")

    def test_qualified_composition_cannot_use_lower_evidence_class(self):
        self.promote("release_qualified")
        self.data["evidence"][-1]["evidence_class"] = "synthetic_fixture"
        self.assert_rejected("requires closed checkpoint")

    def test_release_requires_both_recovery_and_rollback_proof(self):
        for key, value in (("recovery_evidence_ids", []), ("rollback_evidence_ids", []),
                           ("recovery_evidence_ids", ["missing"]), ("release_requirements_digest", "f" * 64)):
            with self.subTest(key=key):
                self.setUp()
                _, row = self.promote("release_qualified")
                row[key] = value
                self.assert_rejected("release requires tested recovery and rollback")

    def test_recovery_and_rollback_proof_bind_current_release_scope(self):
        variants = {"candidate_commit": "d" * 40, "capability_id": "capability.other", "tier": "locally_qualified",
                    "result": "fail", "status": "obsolete", "independent": 1, "kind": "prototype",
                    "acceptance_command": "different", "release_requirements_digest": "f" * 64,
                    "contract_bindings": [], "evidence_class": "synthetic_fixture", "environment": "provider-free",
                    "artifact": "", "artifact_sha256": "bad"}
        for kind in ("recovery", "rollback"):
            for key, value in variants.items():
                with self.subTest(kind=kind, key=key):
                    self.setUp()
                    self.promote("release_qualified")
                    row = next(e for e in self.data["evidence"] if e["kind"] == kind)
                    row[key] = value
                    self.assert_rejected("release requires tested recovery and rollback")

    def no_rollback_fixture(self):
        cap, row = self.promote("release_qualified")
        cap["release_requirements"].update(rollback_command=None, no_rollback_boundary="Fixture-only irreversible boundary")
        self.data["decisions"][-1]["status"] = "superseded"
        cap["decision_receipt"] = self.approve([("decide_capability", cap["id"])])
        self.prove_release(cap, row)
        row["rollback_evidence_ids"] = []
        return cap, row

    def test_no_rollback_requires_separate_explicit_human_authorization(self):
        cap, row = self.no_rollback_fixture()
        self.assert_rejected("release requires tested recovery and rollback")
        cap["no_rollback_receipt"] = cap["decision_receipt"]
        self.assert_rejected("release requires tested recovery and rollback")
        cap["no_rollback_receipt"] = self.approve([("accept_no_rollback", cap["id"])])
        self.assertEqual(self.errors(), [])
        row["recovery_evidence_ids"] = []
        self.assert_rejected("release requires tested recovery and rollback")

    def test_no_rollback_receipt_is_bound_to_candidate_boundary_and_profile(self):
        for field in ("candidate_commit", "boundary", "profile", "contract"):
            with self.subTest(field=field):
                self.setUp()
                cap, _ = self.no_rollback_fixture()
                cap["no_rollback_receipt"] = self.approve([("accept_no_rollback", cap["id"])])
                self.assertEqual(self.errors(), [])
                if field == "candidate_commit":
                    cap[field] = "d" * 40
                elif field == "boundary":
                    cap["release_requirements"]["no_rollback_boundary"] = "Changed boundary"
                elif field == "profile":
                    cap["qualification_requirements"]["release_qualified"]["environment"]["id"] = "Changed environment"
                else:
                    self.data["contracts"][0]["revision"] += 1
                self.assert_rejected("stale")
                self.assert_rejected("release requires tested recovery and rollback")

    def test_pending_release_requirements_cannot_claim_release(self):
        cap, _ = self.promote("release_qualified")
        cap["release_requirements"] = None
        self.assert_rejected("release requires tested recovery and rollback")

    def test_consistently_renamed_non_linear_implementation_is_rejected(self):
        self.data["implementation_issue"] = "not-linear"
        self.data["work_items"][0]["id"] = "not-linear"
        self.data["integration_checkpoints"][0]["work_item_ids"] = ["not-linear"]
        self.assert_rejected("implementation_issue must be a CSL issue")
        self.assert_rejected("work item not-linear must be a CSL issue")

    def test_implementation_must_crosslink_existing_work_with_matching_parent(self):
        self.data["implementation_issue"] = "CSL-999"
        self.assert_rejected("implementation_issue must reference a work item")
        item = copy.deepcopy(self.data["work_items"][0])
        item.update(id="CSL-999", parent="CSL-998")
        self.data["work_items"].append(item)
        self.assert_rejected("implementation work parent must match parent_issue")
        item["parent"] = self.data["parent_issue"]
        self.assertEqual(self.errors(), [])

    def test_all_work_ids_and_implementation_identifier_are_validated(self):
        item = copy.deepcopy(self.data["work_items"][0])
        item["id"] = "untracked"
        self.data["work_items"].append(item)
        self.assert_rejected("work item untracked must be a CSL issue")
        for value in (None, 204, [], "csl-204", "CSL-204extra"):
            with self.subTest(value=value):
                self.data["implementation_issue"] = value
                self.assert_rejected("implementation_issue must be a CSL issue")

    def test_all_scalar_issue_references_reject_malformed_values(self):
        paths = [("parent_issue",), ("implementation_issue",), ("work_items", 0, "id"),
                 ("work_items", 0, "parent"), ("contracts", 0, "decision_issue"),
                 ("capabilities", 0, "decision_issue"), ("admission", "decision_issue"),
                 ("decisions", 0, "linear_issue"), ("proposals", 0, "linear_issue"),
                 ("unknowns", 0, "resolution_issue"), ("integration_checkpoints", 0, "issue")]
        for path in paths:
            for value in ("not-linear", "csl-204", "CSL-204\n", " CSL-204", None, 204, [], {}):
                with self.subTest(path=path, value=value):
                    self.setUp()
                    self.promote("release_qualified")
                    target = self.data
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    self.assert_rejected("must be a CSL issue")

    def test_unknown_and_checkpoint_issues_invalid_before_approval_cannot_promote(self):
        for collection, field in (("unknowns", "resolution_issue"), ("integration_checkpoints", "issue")):
            with self.subTest(collection=collection):
                self.setUp()
                self.data[collection][0][field] = "not-linear"
                self.promote("release_qualified")
                self.assert_rejected(f"{field} must be a CSL issue")

    def test_candidate_routing_references_cannot_be_missing(self):
        paths = [("contracts", 0, "decision_issue"), ("capabilities", 0, "decision_issue"),
                 ("admission", "decision_issue"), ("unknowns", 0, "resolution_issue"),
                 ("integration_checkpoints", 0, "issue")]
        for path in paths:
            with self.subTest(path=path):
                self.setUp()
                target = self.data
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
                self.assert_rejected(f"{path[-1]} must be a CSL issue")

    def test_issue_lists_reject_bad_ids_and_malformed_containers(self):
        paths = [("work_items", 0, "hard_dependencies"), ("integration_checkpoints", 0, "work_item_ids"),
                 ("admission", "active_contract_consumers"), ("admission", "active_reviews")]
        for path in paths:
            for value in (["not-linear"], [None], [[]], [{}], None, "CSL-204", {}):
                with self.subTest(path=path, value=value):
                    self.setUp()
                    target = self.data
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    self.assert_rejected(path[-1])

    def historical_issue_references(self):
        self.data["evidence"].append({"id": "evidence.history", "kind": "work_verification",
                                      "work_item_id": "CSL-998", "work_bindings": [{"id": "CSL-997"}]})
        self.data["proposals"].append({"id": "proposal.history", "linear_issue": "CSL-998",
                                      "authorizations": [{"action": "develop_work", "target": "CSL-997"}],
                                      "unknown_bindings": [{"id": "unknown.old", "resolution_issue": "CSL-996"}]})
        self.data["decisions"].append({"id": "decision.history", "status": "superseded", "linear_issue": "CSL-998"})

    def test_historical_issue_syntax_is_checked_without_requiring_current_work(self):
        self.historical_issue_references()
        self.assertEqual(self.errors(), [])
        paths = [("evidence", -1, "work_item_id"), ("evidence", -1, "work_bindings", 0, "id"),
                 ("proposals", -1, "linear_issue"), ("proposals", -1, "authorizations", 0, "target"),
                 ("proposals", -1, "unknown_bindings", 0, "resolution_issue"),
                 ("decisions", -1, "linear_issue")]
        for path in paths:
            with self.subTest(path=path):
                self.setUp()
                self.historical_issue_references()
                target = self.data
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "not-linear"
                self.assert_rejected("must be a CSL issue")

    def test_issue_binding_containers_and_missing_binding_fields_are_rejected(self):
        for field, collection in (("work_bindings", "evidence"), ("unknown_bindings", "proposals")):
            for value in (None, "CSL-204", ["CSL-204"], [None], [{}]):
                with self.subTest(field=field, value=value):
                    self.setUp()
                    self.historical_issue_references()
                    self.data[collection][-1][field] = value
                    self.assert_rejected(field)

    def test_external_routing_issues_need_not_be_local_work_rows(self):
        for contract in self.data["contracts"]:
            contract["decision_issue"] = "CSL-991"
        self.data["capabilities"][0]["decision_issue"] = "CSL-992"
        self.data["admission"]["decision_issue"] = "CSL-993"
        self.data["unknowns"][0]["resolution_issue"] = "CSL-994"
        self.data["integration_checkpoints"][0]["issue"] = "CSL-995"
        self.data["decisions"][0]["linear_issue"] = "CSL-996"
        self.assertEqual(self.errors(), [])

    def test_valid_syntax_does_not_bypass_current_local_work_crosslinks(self):
        for path, fragment in ((("work_items", 0, "hard_dependencies"), "unknown hard dependency"),
                               (("integration_checkpoints", 0, "work_item_ids"), "unique known work inputs"),
                               (("admission", "active_contract_consumers"), "unknown work item IDs"),
                               (("admission", "active_reviews"), "unknown work item IDs")):
            with self.subTest(path=path):
                self.setUp()
                target = self.data
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = ["CSL-999"]
                self.assert_rejected(fragment)

    def test_seed_does_not_claim_unretained_research(self):
        self.assertEqual(self.data["capabilities"][0]["current_maturity"], "unresearched")
        self.assertEqual(self.data["capabilities"][0]["research_ids"], [])
        self.assertEqual(self.data["research"][0]["status"], "unverified")
        self.assertEqual(self.errors(), [])

    def test_every_top_level_collection_rejects_wrong_shapes(self):
        arrays = ("research", "contracts", "decisions", "work_items", "capabilities", "evidence",
                  "unknowns", "risks", "integration_checkpoints", "proposals", "maturity_order")
        for field in arrays:
            for value in (None, "arbitrary", {}, 1, False, [None], ["row"], [[]]):
                with self.subTest(field=field, value=value):
                    self.setUp()
                    self.data[field] = value
                    self.assertTrue(self.errors())
        for field in ("research_control", "admission"):
            for value in (None, "arbitrary", [], 1):
                with self.subTest(field=field, value=value):
                    self.setUp()
                    self.data[field] = value
                    self.assertTrue(self.errors())
        for value in (None, [], "program", 1, {}):
            self.assertTrue(checker.validate_program(value))

    def test_researched_and_all_higher_tiers_require_linked_research(self):
        for tier in checker.MATURITY_ORDER[1:]:
            for mutation in ("empty", "unlinked", "unknown"):
                with self.subTest(tier=tier, mutation=mutation):
                    self.setUp()
                    cap, _ = self.promote(tier)
                    if mutation == "empty":
                        self.data["research"] = []
                    else:
                        cap["research_ids"] = [] if mutation == "unlinked" else ["research.missing"]
                    self.assert_rejected("researched maturity requires")

    def test_research_support_must_be_current_bound_and_retained(self):
        variants = {"status": "superseded", "result": "fail", "capability_id": "capability.other",
                    "question": "other", "source_class": "", "artifact": "", "artifact_sha256": "bad",
                    "source_commit": "branch", "contract_bindings": [], "limitations": "",
                    "continuation": True, "continuation_packet": {"proposal": "not authority"}}
        for field, value in variants.items():
            with self.subTest(field=field):
                self.setUp()
                _, row = self.research_fixture()
                row[field] = value
                self.assert_rejected("researched maturity requires")
        for field in ("research_question", "research_ids"):
            self.setUp()
            cap, _ = self.research_fixture()
            del cap[field]
            self.assertTrue(self.errors())

    def test_research_proof_changes_invalidate_capability_approval(self):
        self.promote("contract_frozen")
        self.data["research"][-1]["artifact_sha256"] = "f" * 64
        self.assert_rejected("stale or incomplete research bindings")

    def test_structured_initial_research_supports_researched_maturity(self):
        self.research_fixture()
        self.assertEqual(self.errors(), [])

    def test_continuation_and_packet_cannot_infer_research_authority(self):
        for location in ("work", "plan"):
            for field, value in (("continuation", True), ("continuation", "false"),
                                 ("continuation_receipt", "decision.fixture"), ("continuation_packet", {}),
                                 *((field, "Fixture") for field in checker.CONTINUATION_FIELDS)):
                with self.subTest(location=location, field=field):
                    self.setUp()
                    item = self.data["work_items"][0]
                    item.update(stage="research", research_plan={**{f: "Fixture" for f in checker.PLAN_FIELDS},
                                                                 "continuation": False})
                    target = item if location == "work" else item["research_plan"]
                    target[field] = value
                    item["decision_receipt"] = self.approve([("develop_work", item["id"])])
                    self.assert_rejected("research continuation blocked")

    def test_declared_extra_continuation_field_rejects_work_plan_and_research(self):
        for location in ("work", "plan", "research"):
            for alias in ("expanded_scope", "next_pass", "continuation_scope", "Next_Stop_Condition"):
                with self.subTest(location=location, alias=alias):
                    self.setUp()
                    self.data["research_control"]["continuation_packet_fields"].append(alias)
                    if location == "research":
                        _, target = self.research_fixture()
                    else:
                        item = self.data["work_items"][0]
                        item.update(stage="research", research_plan={**{f: "Fixture" for f in checker.PLAN_FIELDS},
                                                                     "continuation": False})
                        target = item if location == "work" else item["research_plan"]
                    target[alias] = "second pass"
                    self.assert_rejected("continuation_packet_fields must equal the supported field set")

    def test_continuation_registry_rejects_removals_aliases_and_duplicates(self):
        original = self.data["research_control"]["continuation_packet_fields"]
        variants = [[], ["expanded_scope"], original + ["expanded_scope"]]
        for field in original:
            variants.extend([[name for name in original if name != field], original + [field],
                             [name if name != field else field.upper() for name in original],
                             [name if name != field else field + " " for name in original],
                             [name if name != field else "alias_" + field for name in original]])
        # Same cardinality with a duplicated known name and an omitted one.
        variants.append([original[0]] * len(original))
        for fields in variants:
            with self.subTest(fields=fields):
                self.setUp()
                self.data["research_control"]["continuation_packet_fields"] = fields
                self.assertTrue(self.errors())

    def test_supported_continuation_registry_order_does_not_change_detection(self):
        self.data["research_control"]["continuation_packet_fields"].reverse()
        self.assertEqual(self.errors(), [])
        for field in self.data["research_control"]["continuation_packet_fields"]:
            with self.subTest(field=field):
                item = self.data["work_items"][0]
                item[field] = None
                self.assert_rejected("research continuation blocked")
                del item[field]

    def test_continuation_registry_shape_errors_remain_validation_errors(self):
        for value in (None, {}, "expanded_scope", [None], [[]], [{}], [" "]):
            with self.subTest(value=value):
                self.setUp()
                self.data["research_control"]["continuation_packet_fields"] = value
                self.assert_rejected("continuation_packet_fields")
        self.setUp()
        del self.data["research_control"]["continuation_packet_fields"]
        self.assert_rejected("continuation_packet_fields")

    def test_research_plan_requires_explicit_initial_pass_and_typed_fields(self):
        item = self.data["work_items"][0]
        item["research_plan"] = {f: "Fixture" for f in checker.PLAN_FIELDS}
        self.assert_rejected("explicit boolean continuation")
        item["research_plan"]["continuation"] = False
        self.assertEqual(self.errors(), [])
        for field in checker.PLAN_FIELDS:
            for value in (None, [], {}, 1, " "):
                with self.subTest(field=field, value=value):
                    plan = copy.deepcopy(item["research_plan"])
                    item["research_plan"][field] = value
                    self.assert_rejected("research plan is missing mandatory fields")
                    item["research_plan"] = plan

    def test_each_promoted_tier_needs_exact_nonempty_acceptance_command(self):
        for tier in checker.MATURITY_ORDER[4:]:
            for value in (None, "", " ", "different command", [], {}):
                with self.subTest(tier=tier, value=value):
                    self.setUp()
                    _, row = self.promote(tier)
                    row["acceptance_command"] = value
                    self.assert_rejected("without")
            self.setUp()
            _, row = self.promote(tier)
            del row["acceptance_command"]
            self.assert_rejected("without")

    def test_command_gate_is_approved_and_cannot_change_with_the_proof(self):
        cap, row = self.promote()
        cap["acceptance_commands"][cap["current_maturity"]] = "new command"
        row["acceptance_command"] = "new command"
        self.assert_rejected("stale authorization target")
        for commands in (None, {}, {"provider_free_verified": " "}):
            self.setUp()
            cap, _ = self.promote()
            cap["acceptance_commands"] = commands
            cap["decision_receipt"] = self.approve([("decide_capability", cap["id"])])
            self.assert_rejected("without independent verification")

    def test_maturity_predicates_reject_malformed_objects_without_crashing(self):
        paths = [("capabilities", 0, "current_maturity"), ("capabilities", 0, "month_target"),
                 ("capabilities", 0, "decision_receipt"), ("unknowns", 0, "blocks"),
                 ("integration_checkpoints", 0, "status"), ("risks", 0, "severity")]
        for path in paths:
            for value in ([], {}, 1):
                with self.subTest(path=path, value=value):
                    self.setUp()
                    target = self.data
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    self.assertTrue(self.errors())

    def test_admission_selector_rejects_malformed_or_absent_modes(self):
        for value in (None, [], {}, 0, False, "", "unknown"):
            with self.subTest(value=value):
                self.setUp()
                self.data["admission"]["mode"] = value
                self.assertTrue(self.errors())
        self.setUp()
        del self.data["admission"]["mode"]
        self.assert_rejected("admission mode must be")

    def test_authoritative_receipt_scalars_reject_truthy_nonstrings_and_blanks(self):
        fields = ("approval_comment_id", "human_decider", "authority_basis", "selected_option",
                  "rationale", "decided_on", "approval_evidence", "revisit_trigger", "proposal_digest")
        for field in fields:
            for value in (True, 1, ["truthy"], {"truthy": True}, " "):
                with self.subTest(field=field, value=value):
                    self.setUp()
                    self.promote()
                    self.data["decisions"][-1][field] = value
                    self.assert_rejected(f"receipt {field} must be a nonempty string")

    def test_authoritative_receipt_arrays_require_nonempty_text_items(self):
        for field in ("scope", "conditions", "rejected_options"):
            for value in (True, "truthy", {"truthy": True}, [True], [1], [{}], [" "], []):
                with self.subTest(field=field, value=value):
                    self.setUp()
                    self.promote()
                    self.data["decisions"][-1][field] = value
                    self.assert_rejected(f"receipt {field} must be a nonempty list of strings")

    def test_receipt_date_requires_a_real_iso_calendar_date(self):
        for value in ("2026-02-30", "2026-13-01", "2026-1-01", "20261016", "yesterday", "2026-10-16T00:00:00Z"):
            with self.subTest(value=value):
                self.setUp()
                self.promote()
                self.data["decisions"][-1]["decided_on"] = value
                self.assert_rejected("valid YYYY-MM-DD date")

    def test_receipt_types_guard_each_authorization_lane(self):
        for lane in ("contract", "capability", "work", "admission", "no_rollback"):
            with self.subTest(lane=lane):
                self.setUp()
                if lane == "admission":
                    target = self.admit()
                elif lane == "no_rollback":
                    target, _ = self.no_rollback_fixture()
                    rid = self.approve([("accept_no_rollback", target["id"])])
                    target["no_rollback_receipt"] = rid
                else:
                    self.promote()
                    if lane == "contract":
                        target = self.data["contracts"][0]
                    elif lane == "capability":
                        target = self.data["capabilities"][0]
                    else:
                        target = self.data["work_items"][0]
                        target["discretion_class"] = "human_gated"
                        target["decision_receipt"] = self.approve([("develop_work", target["id"])])
                rid = target["no_rollback_receipt"] if lane == "no_rollback" else target["decision_receipt"]
                next(d for d in self.data["decisions"] if d["id"] == rid)["approval_comment_id"] = True
                self.assert_rejected("receipt approval_comment_id must be a nonempty string")
                self.assertTrue(any("receipt" in e or "authority" in e for e in self.errors()))

    def test_all_executable_proof_lanes_reject_malformed_environment_and_digest(self):
        variants = [("environment", v) for v in (True, 1, ["host"], " ", {"host": "unknown"})]
        variants += [("artifact_sha256", int("1" * 64)), ("artifact_sha256", True)]
        for kind in ("provider_free", "work_verification", "composed_checkpoint", "recovery", "rollback"):
            for field, value in variants:
                with self.subTest(kind=kind, field=field, value=value):
                    self.setUp()
                    self.promote("release_qualified" if kind != "provider_free" else "provider_free_verified")
                    row = next(r for r in self.data["evidence"] if r["kind"] == kind)
                    row[field] = value
                    self.assertTrue(self.errors())

    def test_low_tier_environment_representations_are_defined(self):
        cap, row = self.promote()
        row["environment"] = {"id": "fixture-host", "execution_class": "fixture",
                              "configuration_sha256": "a" * 64}
        self.assertEqual(self.errors(), [])
        for field, value in (("id", True), ("execution_class", []), ("configuration_sha256", int("1" * 64))):
            with self.subTest(field=field):
                saved = copy.deepcopy(row["environment"])
                row["environment"][field] = value
                self.assert_rejected("without independent verification")
                row["environment"] = saved

    def test_research_and_qualification_digests_cannot_be_json_numbers(self):
        for field, value in (("artifact_sha256", int("1" * 64)), ("source_commit", int("1" * 40))):
            self.setUp()
            _, row = self.research_fixture()
            row[field] = value
            self.assert_rejected("researched maturity requires")
        self.setUp()
        cap, _ = self.promote("locally_qualified")
        cap["qualification_requirements"]["locally_qualified"]["environment"]["configuration_sha256"] = int("1" * 64)
        cap["decision_receipt"] = self.approve([("decide_capability", cap["id"])])
        self.assert_rejected("qualification requires")

    def test_commit_identity_must_be_a_string_even_when_number_matches_proof(self):
        cap, row = self.promote()
        cap["candidate_commit"] = row["candidate_commit"] = int("1" * 40)
        self.assert_rejected("without independent verification")
        self.setUp()
        self.data["source_commit"] = int("1" * 40)
        self.assert_rejected("source_commit must be")

    def test_contract_binding_types_cannot_use_python_scalar_coercion(self):
        for source in ("evidence", "research", "proposal"):
            for field, value in (("revision", True), ("revision", 1.0), ("revision", "1"),
                                 ("id", True), ("sha256", int("1" * 64))):
                with self.subTest(source=source, field=field, value=value):
                    self.setUp()
                    self.promote()
                    self.assertEqual(self.errors(), [])
                    if source == "evidence":
                        row = self.data["evidence"][0]
                    elif source == "research":
                        row = self.data["research"][-1]
                    else:
                        row = self.data["proposals"][-1]
                    row["contract_bindings"][0][field] = value
                    if source == "proposal":
                        self.data["decisions"][-1]["proposal_digest"] = checker.digest(row)
                    self.assertTrue(self.errors())

    def test_work_and_composed_contract_bindings_require_integer_revisions(self):
        for kind in ("work_verification", "composed_checkpoint", "recovery", "rollback"):
            with self.subTest(kind=kind):
                self.setUp()
                self.promote("release_qualified")
                item = self.data["work_items"][0]
                if kind == "work_verification":
                    item["consumes_contracts"] = [self.data["contracts"][0]["id"]]
                    item["contract_receipts"] = {self.data["contracts"][0]["id"]:
                                                self.data["contracts"][0]["decision_receipt"]}
                    self.close_checkpoint(self.data["capabilities"][0])
                self.assertEqual(self.errors(), [])
                rows = [r for r in self.data["evidence"] if r["kind"] == kind and r.get("contract_bindings")]
                self.assertTrue(rows)
                for row in rows:
                    row["contract_bindings"][0]["revision"] = True
                self.assertTrue(self.errors())

    def test_program_status_is_candidate_only_even_with_other_approvals(self):
        for status in ("accepted", "approved", "ratified", "arbitrary", "", None, False, [], {}):
            with self.subTest(status=status):
                self.setUp()
                self.promote()
                self.data["status"] = status
                self.assert_rejected("program status must be candidate" if isinstance(status, str)
                                     else "program.status must be a string")

    def test_work_reference_declarations_cannot_be_omitted_at_any_stage(self):
        for stage in checker.STAGES:
            for field in ("hard_dependencies", "consumes_contracts"):
                with self.subTest(stage=stage, field=field):
                    self.setUp()
                    item = self.data["work_items"][0]
                    item["stage"] = stage
                    del item[field]
                    self.assert_rejected(f"{field} must be a list")

    def test_checkpoint_contract_declaration_cannot_be_omitted(self):
        for promoted in (False, True):
            with self.subTest(promoted=promoted):
                self.setUp()
                del self.data["integration_checkpoints"][0]["requires"]
                if promoted:
                    # No approval or proof may make an omitted declaration valid.
                    self.data["capabilities"][0]["current_maturity"] = "release_qualified"
                self.assert_rejected("requires must be a list")

    def test_consequential_declaration_must_be_explicit_boolean(self):
        for value in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(value=value):
                self.setUp()
                self.data["work_items"][0]["consequential"] = value
                self.assert_rejected("explicit boolean consequential")
        self.setUp()
        del self.data["work_items"][0]["consequential"]
        self.assert_rejected("explicit boolean consequential")

    def test_active_proposal_binding_arrays_cannot_be_omitted(self):
        for field in ("contract_bindings", "unknown_bindings", "checkpoint_bindings"):
            with self.subTest(field=field):
                self.setUp()
                contract = self.data["contracts"][0]
                receipt_id = self.approve([("accept_contract", contract["id"])])
                contract.update(status="accepted", decision_receipt=receipt_id)
                proposal = self.data["proposals"][-1]
                del proposal[field]
                self.data["decisions"][-1]["proposal_digest"] = checker.digest(proposal)
                self.assert_rejected(f"explicit {field}")

    def test_other_omitted_reference_inputs_already_fail_closed(self):
        for path in (("admission", "active_contract_consumers"), ("admission", "active_reviews"),
                     ("capabilities", 0, "checkpoint_requirements"),
                     ("integration_checkpoints", 0, "work_item_ids"),
                     ("integration_checkpoints", 0, "evidence_ids")):
            with self.subTest(path=path):
                self.setUp()
                target = self.data
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
                self.assertTrue(self.errors())
        self.setUp()
        item = self.data["work_items"][0]
        self.verify_work(item)
        del item["verification_evidence_ids"]
        self.assert_rejected("completed work lacks exact-candidate independent verification")

    def test_explicit_empty_declarations_and_candidate_status_remain_valid(self):
        self.data["integration_checkpoints"][0]["requires"] = []
        self.assertEqual(self.data["status"], "candidate")
        self.assertEqual(self.data["work_items"][0]["hard_dependencies"], [])
        self.assertEqual(self.data["work_items"][0]["consumes_contracts"], [])
        self.assertEqual(self.errors(), [])

    def test_capability_references_reject_object_keys_even_before_approval(self):
        for field in ("evidence_ids", "contract_ids", "unknown_ids"):
            with self.subTest(field=field):
                self.setUp()
                cap = self.data["capabilities"][0]
                cap[field] = dict.fromkeys(cap[field])
                self.promote()
                self.assert_rejected(f"{field} must be a list")

    def test_capability_reference_arrays_are_required_and_typed(self):
        for field in ("evidence_ids", "contract_ids", "unknown_ids"):
            for value in (None, {}, "not-an-array", [None], [12], [[]], [{}], [""]):
                with self.subTest(field=field, value=value):
                    self.setUp()
                    self.data["capabilities"][0][field] = value
                    self.assert_rejected(f"{field} must be a list")
            self.setUp()
            del self.data["capabilities"][0][field]
            self.assert_rejected(f"{field} must be a list")

    def test_related_reference_arrays_reject_object_and_scalar_iterables(self):
        paths = [("work_items", 0, "consumes_contracts"), ("work_items", 0, "verification_evidence_ids"),
                 ("integration_checkpoints", 0, "requires"), ("integration_checkpoints", 0, "evidence_ids"),
                 ("evidence", 0, "recovery_evidence_ids"), ("evidence", 0, "rollback_evidence_ids")]
        for path in paths:
            for value in ({"existing-id": None}, "existing-id", None, [[]]):
                with self.subTest(path=path, value=value):
                    self.setUp()
                    target = self.data
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    self.assert_rejected(f"{path[-1]} must be a list")

    def test_valid_reference_arrays_preserve_candidate_and_promoted_controls(self):
        self.assertEqual(self.errors(), [])
        self.data["capabilities"][0]["evidence_ids"] = []
        self.assertEqual(self.errors(), [])
        self.setUp()
        self.promote("release_qualified")
        self.assertEqual(self.errors(), [])


    def test_resolution_reference_cannot_be_truthy_placeholder(self):
        for value in (True, 1, {"proof": "yes"}, ["proof"], " ", "missing", None):
            with self.subTest(value=value):
                self.setUp()
                self.promote()
                self.data["unknowns"][0]["resolution_evidence"] = value
                self.assert_rejected("without resolution evidence")
                self.assert_rejected("blocked by unresolved")

    def test_resolution_proof_requires_current_scope_and_inspectable_artifact(self):
        mutations = {"kind": "provider_free", "unknown_id": "unknown.other", "unknown_digest": "0" * 64,
                     "resolution_issue": "CSL-999", "result": "fail", "status": "superseded",
                     "source_commit": "0" * 40, "artifact": True, "artifact_sha256": True,
                     "contract_bindings": []}
        for field, value in mutations.items():
            with self.subTest(field=field):
                self.setUp()
                self.promote()
                unknown = self.data["unknowns"][0]
                row = next(r for r in self.data["evidence"] if r["id"] == unknown["resolution_evidence"])
                row[field] = value
                self.assert_rejected("without resolution evidence")
                self.assert_rejected("blocked by unresolved")

    def test_resolution_proof_missing_fields_fail_closed(self):
        for field in ("kind", "unknown_id", "unknown_digest", "resolution_issue", "result", "status",
                      "source_commit", "artifact", "artifact_sha256", "contract_bindings"):
            with self.subTest(field=field):
                self.setUp()
                row = self.resolve_unknown(self.data["unknowns"][0])
                del row[field]
                self.assert_rejected("without resolution evidence")

    def test_resolution_definition_and_source_changes_invalidate_proof(self):
        for field in ("exposure", "resolution_issue", "blocks", "source_commit", "contract"):
            with self.subTest(field=field):
                self.setUp()
                unknown = self.data["unknowns"][0]
                self.resolve_unknown(unknown)
                if field == "source_commit":
                    self.data[field] = "0" * 40
                elif field == "contract":
                    self.data["contracts"][0]["revision"] += 1
                else:
                    unknown[field] = {"exposure": "Changed exposure", "resolution_issue": "CSL-999",
                                      "blocks": "researched"}[field]
                self.assert_rejected("without resolution evidence")

    def test_resolution_cannot_borrow_another_unknowns_proof(self):
        self.promote()
        self.data["unknowns"][0]["resolution_evidence"] = self.data["unknowns"][1]["resolution_evidence"]
        self.assert_rejected("without resolution evidence")
        self.assert_rejected("blocked by unresolved")

    def test_semantic_text_fields_reject_truthy_json_and_blank_text(self):
        paths = [("work_items", 0, "recovery"), ("work_items", 0, "rollback"),
                 ("unknowns", 0, "exposure"), ("risks", 0, "mitigation"), ("risks", 0, "trigger")]
        for promoted in (False, True):
            for path in paths:
                for value in (True, 1, {"text": "yes"}, ["text"], " ", "", None):
                    with self.subTest(promoted=promoted, path=path, value=value):
                        self.setUp()
                        if promoted:
                            self.promote()
                        self.data[path[0]][path[1]][path[2]] = value
                        self.assertTrue(self.errors())

    def test_semantic_text_lists_require_nonempty_text_elements(self):
        for collection, field in (("work_items", "stop_conditions"), ("admission", "allowed_agent_actions"),
                                  ("admission", "human_decision_triggers")):
            for value in (True, 1, "text", {"text": "yes"}, [], [True], [1], [{}], [" "], ["ok", None]):
                with self.subTest(collection=collection, field=field, value=value):
                    self.setUp()
                    target = self.data[collection][0] if collection == "work_items" else self.data[collection]
                    target[field] = value
                    self.assert_rejected(field)

    def test_registry_ids_contract_paths_and_scope_require_text(self):
        for collection in ("contracts", "decisions", "work_items", "evidence", "unknowns", "capabilities",
                           "proposals", "integration_checkpoints", "research", "risks"):
            self.setUp()
            if self.data[collection]:
                self.data[collection][0]["id"] = " "
                self.assertTrue(self.errors(), collection)
        for value in (True, 1, {}, [], " "):
            self.setUp()
            self.data["contracts"][0]["path"] = value
            self.assert_rejected("path must be a nonempty string")
        self.setUp()
        self.promote()
        self.data["proposals"][-1]["scope"].append(" ")
        self.assert_rejected("requires explicit scope")

    def test_pinned_enumeration_covers_contracts_and_manifest_payloads(self):
        paths = checker.sha256_pinned_paths(self.data, ROOT)
        for contract in self.data["contracts"]:
            self.assertIn(contract["path"], paths)
        # Intake payloads pinned by a SHA256SUMS manifest are included; the
        # manifest and intake script themselves are not pinned payload paths.
        self.assertIn("docs/v3/research-lane/packet-f-raudit-20261002/README.md", paths)
        self.assertIn("docs/v3/research-lane/packet-f-raudit-20261002/corpus/report.md", paths)
        self.assertNotIn("docs/v3/research-lane/packet-f-raudit-20261002/SHA256SUMS.txt", paths)
        self.assertNotIn("docs/v3/research-lane/packet-f-raudit-20261002/check_intake.py", paths)

    def test_pinned_enumeration_reads_manifest_entries_from_any_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "docs/v3/research-lane/fresh-packet"
            packet.mkdir(parents=True)
            (packet / "SHA256SUMS.txt").write_text(
                "a" * 64 + "  payload.md\n" +
                "not a sum line\n" +
                "b" * 64 + " *blob.bin\n",
                encoding="utf-8")
            # Packet written from the parent dir: entries carry the dir prefix.
            packet2 = root / "docs/v3/research-lane/parent-packet"
            packet2.mkdir(parents=True)
            (packet2 / "p.md").write_text("x\n", encoding="utf-8")
            (packet2 / "SHA256SUMS.txt").write_text(
                "c" * 64 + "  parent-packet/p.md\n" +
                "d" * 64 + "  parent-packet/n.md\n",
                encoding="utf-8")
            # A same-named nested directory wins over the parent fallback.
            nested = packet2 / "parent-packet"
            nested.mkdir()
            (nested / "n.md").write_text("x\n", encoding="utf-8")
            receipts = root / "docs/audits/receipts-1"
            receipts.mkdir(parents=True)
            (receipts / "r1.json").write_text("{}", encoding="utf-8")
            paths = checker.sha256_pinned_paths({"contracts": [{"path": "x/y.md"}]}, root)
            self.assertIn("x/y.md", paths)
            self.assertIn("docs/v3/research-lane/fresh-packet/payload.md", paths)
            self.assertIn("docs/v3/research-lane/fresh-packet/blob.bin", paths)
            self.assertIn("docs/v3/research-lane/parent-packet/p.md", paths)
            self.assertIn("docs/v3/research-lane/parent-packet/parent-packet/n.md", paths)
            self.assertIn("docs/audits/receipts-1/r1.json", paths)
            self.assertNotIn("docs/v3/research-lane/fresh-packet/SHA256SUMS.txt", paths)

    def test_eol_guard_passes_on_the_checked_in_tree(self):
        self.assertEqual(checker.pinned_eol_errors(self.data, ROOT), [])

    def test_eol_guard_flags_a_pinned_path_outside_lf_coverage(self):
        # tools/check_program_control.py resolves eol=unspecified on this tree.
        data = copy.deepcopy(self.data)
        data["contracts"][0]["path"] = "tools/check_program_control.py"
        errors = checker.pinned_eol_errors(data, ROOT)
        self.assertTrue(any("tools/check_program_control.py" in error and "eol" in error
                            for error in errors), errors)

    def test_eol_guard_flags_new_uncovered_pins_in_a_git_worktree(self):
        # The CSL-225 scenario: a sha256-pinned path lands without eol=lf.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            packet = root / "docs/v3/research-lane/fresh-packet"
            packet.mkdir(parents=True)
            (packet / "payload.md").write_text("x\n", encoding="utf-8")
            (packet / "SHA256SUMS.txt").write_text(
                "a" * 64 + "  payload.md\n", encoding="utf-8")
            errors = checker.pinned_eol_errors({"contracts": []}, root)
            self.assertTrue(any("payload.md" in error and "eol" in error
                                for error in errors), errors)

    def test_eol_guard_accepts_lf_and_binary_pins_in_a_git_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            packet = root / "docs/v3/research-lane/fresh-packet"
            packet.mkdir(parents=True)
            (packet / "payload.md").write_text("x\n", encoding="utf-8")
            (packet / "blob.bin").write_bytes(b"\x00\x01")
            (packet / "SHA256SUMS.txt").write_text(
                "a" * 64 + "  payload.md\n" +
                "b" * 64 + "  blob.bin\n",
                encoding="utf-8")
            (root / ".gitattributes").write_text(
                "docs/**/*.md text eol=lf\ndocs/**/*.bin binary\n", encoding="utf-8")
            self.assertEqual(checker.pinned_eol_errors({"contracts": []}, root), [])

    def test_known_gap_entries_are_still_pinned_and_uncovered(self):
        paths = set(checker.sha256_pinned_paths(self.data, ROOT))
        self.assertTrue(checker.PINNED_EOL_KNOWN_GAPS)
        self.assertTrue(checker.PINNED_EOL_KNOWN_GAPS <= paths)

    def test_known_gap_removed_file_is_reported_stale(self):
        # The frozen path snapshot can grow; only entries whose parent dirs
        # exist in this synthetic root are stale, so assert that exact set.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs/v3/research-lane/overnight-20261002/semantic-guard-research"
             ).mkdir(parents=True)
            errors = checker.pinned_eol_errors({"contracts": []}, root)
            stale_paths = sorted(
                path for path in checker.PINNED_EOL_KNOWN_GAPS
                if (root / path).parent.is_dir())
            self.assertEqual(errors, [
                f"{path} known eol gap is stale (unpinned or removed): "
                "remove it from PINNED_EOL_KNOWN_GAPS"
                for path in stale_paths
            ])
        # A bare synthetic root (no packet dirs) emits no stale-gap errors.
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                checker.pinned_eol_errors({"contracts": []}, Path(directory)), [])

    def test_sha256sums_absolute_or_escaping_entry_is_a_validation_error(self):
        # CSL-226 P2: `sha256sum /abs/path` or a `..`-escaping entry must not
        # crash sha256_pinned_paths via Path.relative_to raising ValueError.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "docs/v3/research-lane/unsafe-packet"
            packet.mkdir(parents=True)
            (packet / "SHA256SUMS.txt").write_text(
                "a" * 64 + "  /etc/passwd\n" +
                "b" * 64 + "  " + "../" * 5 + "outside.md\n",
                encoding="utf-8")
            errors = checker.pinned_path_errors({"contracts": []}, root)
            self.assertTrue(any("/etc/passwd" in error and "outside the repository" in error
                                for error in errors), errors)
            self.assertTrue(any("outside.md" in error and "outside the repository" in error
                                for error in errors), errors)
            # Enumeration itself never raises for these entries; it just omits them.
            self.assertEqual(checker.sha256_pinned_paths({"contracts": []}, root), [])

    def test_manifest_pinned_text_file_outside_gap_dirs_requires_eol_lf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            packet = root / "docs/v3/research-lane/fresh-manifest-packet"
            packet.mkdir(parents=True)
            (packet / "NOTES.md").write_text("x\n", encoding="utf-8")
            notes_sha = hashlib.sha256((packet / "NOTES.md").read_bytes()).hexdigest()
            (packet / "MANIFEST.json").write_text(
                json.dumps({"files": [{"path": "NOTES.md", "sha256": notes_sha}]}),
                encoding="utf-8")
            errors = checker.pinned_eol_errors({"contracts": []}, root)
            self.assertTrue(any("NOTES.md" in error and "eol" in error for error in errors), errors)

            (root / ".gitattributes").write_text("docs/**/*.md text eol=lf\n", encoding="utf-8")
            checker._git_check_attr.cache_clear()
            self.assertEqual(checker.pinned_eol_errors({"contracts": []}, root), [])

    def test_manifest_pinned_binary_entry_with_minus_text_is_tolerated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            packet = root / "docs/v3/research-lane/fresh-manifest-packet"
            packet.mkdir(parents=True)
            (packet / "blob.bin").write_bytes(b"\x00\x01")
            blob_sha = hashlib.sha256((packet / "blob.bin").read_bytes()).hexdigest()
            (packet / "MANIFEST.json").write_text(
                json.dumps({"files": [{"path": "blob.bin", "sha256": blob_sha}]}),
                encoding="utf-8")
            (root / ".gitattributes").write_text("docs/**/*.bin binary\n", encoding="utf-8")
            self.assertEqual(checker.pinned_eol_errors({"contracts": []}, root), [])

    def test_manifest_default_mapping_uses_manifest_local_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            packet = root / "docs/v3/research-lane/fresh-raw-packet"
            (packet / "raw" / "R-A").mkdir(parents=True)
            (packet / "raw" / "R-A" / "report.md").write_text("x\n", encoding="utf-8")
            report_sha = hashlib.sha256(
                (packet / "raw" / "R-A" / "report.md").read_bytes()).hexdigest()
            (packet / "MANIFEST.json").write_text(
                json.dumps({"files": [{"path": "raw/R-A/report.md", "sha256": report_sha}]}),
                encoding="utf-8")
            paths = checker.sha256_pinned_paths({"contracts": []}, root)
            self.assertIn("docs/v3/research-lane/fresh-raw-packet/raw/R-A/report.md", paths)
            errors = checker.pinned_eol_errors({"contracts": []}, root)
            self.assertTrue(any("raw/R-A/report.md" in error and "eol" in error
                                for error in errors), errors)

    def test_new_uncovered_pin_in_a_known_gap_packet_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            packet = root / "docs/v3/research-lane/overnight-20261002"
            packet.mkdir(parents=True)
            (packet / "NEW.md").write_text("x\n", encoding="utf-8")
            new_sha = hashlib.sha256((packet / "NEW.md").read_bytes()).hexdigest()
            (packet / "MANIFEST.json").write_text(
                json.dumps({"files": [{"path": "NEW.md", "sha256": new_sha}]}),
                encoding="utf-8")
            errors = checker.pinned_eol_errors({"contracts": []}, root)
            self.assertTrue(any("NEW.md" in error and "lacks eol=lf" in error
                                for error in errors), errors)

    def test_pinned_enumeration_includes_overnight_manifest_changelog(self):
        paths = checker.sha256_pinned_paths(self.data, ROOT)
        self.assertIn("docs/v3/research-lane/overnight-20261002/CHANGELOG.md", paths)

    def test_pinned_enumeration_includes_followups_machine_files(self):
        packet = "docs/v3/research-lane/followups-20261002"
        names = {"platform-manifest.json", "platform-manifest.schema.json",
                 "negative-traces.json", "negative-traces.schema.json",
                 "profile-fields.json", "source-register.json"}
        paths = set(checker.sha256_pinned_paths(self.data, ROOT))
        self.assertEqual({path for path in paths if path.startswith(packet + "/machine/")},
                         {packet + "/machine/" + name for name in names})

    def test_manifest_packet_mapping_ignores_local_raw_collisions(self):
        layouts = [
            ("dot-ra-rd-20261002", "DIR/report.md", "raw/DIR/report.md"),
            ("scope-reframe-20261002", "DIR/report.md", "raw/DIR/report.md"),
            ("overnight-20261002", "DIR/report.md", "DIR/report.md"),
            ("other-packet", "DIR/report.md", "DIR/report.md"),
            ("followups-20261002", "FU04-machine-specifications/spec.json", "machine/spec.json"),
        ]
        for packet_name, original, expected in layouts:
            with self.subTest(packet=packet_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                packet = root / packet_name
                for name in {original, "raw/" + original, expected, "ROOT.md", "raw/ROOT.md"}:
                    path = packet / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(name + "\n", encoding="utf-8")
                manifest = packet / "MANIFEST.json"
                manifest.write_text(json.dumps({"files": [
                    {"path": original, "sha256": "a" * 64},
                    {"path": "ROOT.md", "sha256": "b" * 64},
                ]}), encoding="utf-8")
                errors = []
                self.assertEqual(checker._manifest_payload_paths(manifest, root, errors),
                                 [packet / expected, packet / "ROOT.md"])
                self.assertEqual(errors, [])
                # An absent authoritative copy must not select a colliding copy.
                (packet / expected).unlink()
                self.assertEqual(checker._manifest_payload_paths(manifest, root, errors),
                                 [packet / "ROOT.md"])
                self.assertEqual(errors, [
                    f"{packet.name}/MANIFEST.json: entry '{original}' payload is missing",
                ])

    def test_pinned_symlink_loops_are_validation_errors(self):
        import os

        if not hasattr(os, "symlink"):
            self.skipTest("os.symlink unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            packet = root / "packet"
            packet.mkdir()
            try:
                os.symlink("loop", packet / "loop")
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            (packet / "SHA256SUMS.txt").write_text("a" * 64 + "  loop\n", encoding="utf-8")
            (packet / "MANIFEST.json").write_text(json.dumps({"files": [
                {"path": "loop", "sha256": "b" * 64},
            ]}), encoding="utf-8")
            errors = checker.pinned_path_errors({"contracts": []}, root)
            self.assertEqual(errors, [
                "packet/SHA256SUMS.txt: entry 'loop' cannot be resolved",
                "packet/MANIFEST.json: entry 'loop' cannot be resolved",
            ])
            self.assertEqual(checker.sha256_pinned_paths({"contracts": []}, root), [])

    def test_safe_join_reports_resolution_exceptions_deterministically(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for exception in (RuntimeError, OSError, ValueError):
                with self.subTest(exception=exception.__name__):
                    errors = []
                    with patch.object(Path, "resolve", side_effect=exception("unstable detail")):
                        self.assertIsNone(checker._safe_join(root, "entry", root, errors, "source"))
                    self.assertEqual(errors, ["source cannot be resolved"])

    def test_manifest_mapped_symlink_escape_is_validation_error(self):
        import os

        if not hasattr(os, "symlink"):
            self.skipTest("os.symlink unavailable")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root, outside = base / "repo", base / "outside"
            packet = root / "dot-ra-rd-20261002"
            (packet / "DIR").mkdir(parents=True)
            (outside / "DIR").mkdir(parents=True)
            (outside / "DIR/report.md").write_text("outside\n", encoding="utf-8")
            (packet / "DIR/report.md").write_text("local collision\n", encoding="utf-8")
            try:
                os.symlink(outside, packet / "raw", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            (packet / "MANIFEST.json").write_text(json.dumps({"files": [
                {"path": "DIR/report.md", "sha256": "a" * 64},
            ]}), encoding="utf-8")
            self.assertEqual(checker.pinned_path_errors({"contracts": []}, root), [
                "dot-ra-rd-20261002/MANIFEST.json: entry 'DIR/report.md' is outside the repository",
            ])
            self.assertEqual(checker.sha256_pinned_paths({"contracts": []}, root), [])

    def test_sha256sums_fallback_symlink_escape_is_validation_error(self):
        import os

        if not hasattr(os, "symlink"):
            self.skipTest("os.symlink unavailable")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "repo"
            packet = root / "packet"
            packet.mkdir(parents=True)
            outside = base / "outside.md"
            outside.write_text("outside\n", encoding="utf-8")
            try:
                os.symlink(outside, packet / "report.md")
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            (packet / "SHA256SUMS.txt").write_text(
                "a" * 64 + "  packet/report.md\n", encoding="utf-8")
            self.assertEqual(checker.pinned_path_errors({"contracts": []}, root), [
                "packet/SHA256SUMS.txt: entry 'packet/report.md' is outside the repository",
            ])
            self.assertEqual(checker.sha256_pinned_paths({"contracts": []}, root), [])

    def test_manifest_docx_entries_require_containment_and_binary_attributes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            packet = root / "packet"
            packet.mkdir()
            (packet / "payload.docx").write_bytes(b"PK\x00\x01")
            manifest = packet / "MANIFEST.json"
            unsafe = ["/absolute.docx", "../../outside.docx", "C:/absolute.docx",
                      "\\\\server\\share\\absolute.docx"]
            manifest.write_text(json.dumps({"files": [
                {"path": name, "sha256": "a" * 64}
                for name in [*unsafe, "payload.docx", "omitted.docx"]
            ]}), encoding="utf-8")
            self.assertEqual(checker.pinned_path_errors({"contracts": []}, root), [
                *(f"packet/MANIFEST.json: entry '{name}' is outside the repository" for name in unsafe),
                "packet/MANIFEST.json: entry 'omitted.docx' payload is missing",
            ])
            self.assertEqual(checker.sha256_pinned_paths({"contracts": []}, root),
                             ["packet/payload.docx"])
            errors = checker.pinned_eol_errors({"contracts": []}, root)
            self.assertTrue(any("payload.docx" in error and "eol" in error for error in errors), errors)
            manifest.write_text(json.dumps({"files": [
                {"path": "payload.docx", "sha256": "a" * 64},
                {"path": "omitted.docx", "sha256": "b" * 64},
            ]}), encoding="utf-8")
            (root / ".gitattributes").write_text("*.docx binary\n", encoding="utf-8")
            checker._git_check_attr.cache_clear()
            self.assertEqual(checker.pinned_eol_errors({"contracts": []}, root), [
                "packet/MANIFEST.json: entry 'omitted.docx' payload is missing",
            ])

    def test_manifest_known_missing_source_original_allowlist_drains(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "docs/v3/research-lane/dot-ra-rd-20261002"
            raw = packet / "raw/R-A"
            raw.mkdir(parents=True)
            payload = raw / "R-A-plugin-portability.docx"
            payload.write_bytes(b"restored source original")
            manifest = packet / "MANIFEST.json"
            manifest.write_text(json.dumps({"files": [{
                "path": "R-A/R-A-plugin-portability.docx",
                "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
            }]}), encoding="utf-8")
            errors = checker.pinned_path_errors({"contracts": []}, root)
            self.assertTrue(any("R-A-plugin-portability.docx" in error
                                and "known missing manifest source original is stale" in error
                                for error in errors), errors)

    def test_manifest_invalid_inventory_is_validation_error(self):
        for payload in ('{', '{"files": {}}', '{"files": [{}]}'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "MANIFEST.json").write_text(payload, encoding="utf-8")
                errors = checker.pinned_path_errors({"contracts": []}, root)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("MANIFEST.json:", errors[0])

    def test_manifest_missing_payload_is_validation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "fresh-packet"
            packet.mkdir()
            (packet / "MANIFEST.json").write_text(json.dumps({"files": [{
                "path": "NEW.md", "sha256": "a" * 64,
            }]}), encoding="utf-8")
            self.assertEqual(checker.pinned_path_errors({"contracts": []}, root), [
                "fresh-packet/MANIFEST.json: entry 'NEW.md' payload is missing",
            ])

    def test_pinned_enumeration_ignores_case_mismatched_names(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            packet = root / "packet"
            packet.mkdir()
            lower_manifest = packet / "manifest.json"
            lower_manifest.write_text("{", encoding="utf-8")
            lower_sums = packet / "sha256sums.txt"
            lower_sums.write_text("a" * 64 + "  payload.md\n", encoding="utf-8")
            (packet / "payload.md").write_text("ignored\n", encoding="utf-8")

            def simulated_walk(path, *args, **kwargs):
                self.assertEqual(Path(path), root)
                yield str(root), ["packet"], []
                yield str(packet), [], ["manifest.json", "payload.md", "sha256sums.txt"]

            with patch.object(checker.os, "walk", new=simulated_walk):
                self.assertEqual(checker.sha256_pinned_paths({"contracts": []}, root), [])
                self.assertEqual(checker.pinned_path_errors({"contracts": []}, root), [])


if __name__ == "__main__":
    unittest.main()
