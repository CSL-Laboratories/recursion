#!/usr/bin/env python3
"""Deterministic, provider-free validator for the CSL-204 program ledger.

Run from the repository root:

    python -X utf8 -B tools/check_program_control.py

A pass proves structural closure only. It is not Owner acceptance, independent
verification, product qualification, or release approval.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs/v3/program-control/program.json"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MATURITY_ORDER = [
    "unresearched", "researched", "human_decided", "contract_frozen", "prototyped",
    "implemented", "provider_free_verified", "locally_qualified", "release_qualified",
]
PLAN_FIELDS = {"question", "source_class", "quota_class", "stop_condition", "evidence_artifact"}
DONE = {"done", "accepted", "verified"}
STAGES = {"intake": 0, "research": 1, "decision": 2, "development": 3,
          "verification": 4, "integration": 5, "release": 6}


def digest(value: Any) -> str:
    """Digest the complete JSON proposal, including identity, scope and bindings."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()


def contract_binding(contract: dict[str, Any]) -> dict[str, Any]:
    return {key: contract.get(key) for key in ("id", "revision", "sha256")}


def target_digest(action: str, target: dict[str, Any]) -> str:
    # Exclude only observation/lifecycle fields, never the approved work boundary.
    excluded = {
        "accept_contract": {"status", "decision_receipt"},
        "develop_work": {"status", "stage", "decision_receipt", "contract_receipts"},
        "decide_capability": {"current_maturity", "month_target", "evidence_ids", "unknown_ids",
                              "decision_receipt", "candidate_commit"},
        "admit_work": {"decision_receipt", "active_contract_consumers", "active_reviews"},
    }
    return digest({key: value for key, value in target.items() if key not in excluded[action]})


def _ids(rows: list[dict[str, Any]], name: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = row.get("id")
        if not isinstance(value, str) or not value:
            errors.append(f"{name} row missing id")
        elif value in result:
            errors.append(f"duplicate {name} id: {value}")
        else:
            result[value] = row
    return result


def validate_program(data: dict[str, Any], root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    required = {
        "schema_version", "program_id", "status", "parent_issue", "implementation_issue",
        "source_commit", "research", "research_control", "maturity_order", "contracts", "decisions", "admission",
        "work_items", "capabilities", "evidence", "unknowns", "risks", "integration_checkpoints", "proposals",
    }
    missing = sorted(required - data.keys())
    if missing:
        errors.append(f"missing top-level fields: {', '.join(missing)}")
        return errors
    if data["schema_version"] != 1:
        errors.append("schema_version must be 1")
    if not re.fullmatch(r"CSL-[0-9]+", str(data["parent_issue"])):
        errors.append("parent_issue must be a CSL issue")
    if not re.fullmatch(r"[0-9a-f]{40}", str(data["source_commit"])):
        errors.append("source_commit must be a full lowercase commit hash")

    order = data["maturity_order"]
    if order != MATURITY_ORDER:
        errors.append("maturity_order must match the defined maturity tiers in order")
        return errors
    rank = {value: index for index, value in enumerate(order)}
    research_control = data["research_control"]
    if research_control.get("default") != "approved_issue_pass_only":
        errors.append("research must default to the approved issue pass")
    if research_control.get("continuation_requires_human") is not True:
        errors.append("research continuation must require human approval")
    if research_control.get("approval_channel") != "human_comment_on_matching_linear_issue":
        errors.append("research approval must use a human comment on the matching Linear issue")
    if research_control.get("status_is_authority") is not False:
        errors.append("Linear status must not grant research authority")
    for field in ("required_plan_fields", "continuation_triggers", "continuation_packet_fields", "required_receipt_fields"):
        if not isinstance(research_control.get(field), list) or not research_control[field]:
            errors.append(f"research control requires non-empty {field}")
    plan_fields = research_control.get("required_plan_fields", [])
    if not isinstance(plan_fields, list) or not PLAN_FIELDS.issubset(plan_fields):
        errors.append("research required_plan_fields must include question, source_class, quota_class, stop_condition, evidence_artifact")
    contracts = _ids(data["contracts"], "contract", errors)
    decisions = _ids(data["decisions"], "decision", errors)
    work = _ids(data["work_items"], "work item", errors)
    evidence = _ids(data["evidence"], "evidence", errors)
    unknowns = _ids(data["unknowns"], "unknown", errors)
    capabilities = _ids(data["capabilities"], "capability", errors)
    proposals = _ids(data["proposals"], "proposal", errors)
    _ids(data["integration_checkpoints"], "checkpoint", errors)
    _ids(data["risks"], "risk", errors)

    for contract_id, contract in contracts.items():
        path = root / str(contract.get("path", ""))
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            errors.append(f"{contract_id} path is missing or outside the repository")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if contract.get("sha256") != actual:
            errors.append(f"{contract_id} digest mismatch: expected {actual}")
        if contract.get("status") not in {"candidate", "accepted", "superseded"}:
            errors.append(f"{contract_id} has invalid status")
        if type(contract.get("revision")) is not int or contract["revision"] < 1:
            errors.append(f"{contract_id} revision must be a positive integer")

    def bindings_match(bindings: Any, contract_ids: list[str]) -> bool:
        return (isinstance(bindings, list) and len(bindings) == len(contract_ids)
                and all(cid in contracts and contract_binding(contracts[cid]) in bindings
                        for cid in contract_ids)
                and len(set(contract_ids)) == len(contract_ids))

    targets = {"accept_contract": contracts, "develop_work": work,
               "decide_capability": capabilities, "admit_work": {data["program_id"]: data["admission"]}}
    valid_proposals = set()
    active_proposals = {d.get("proposal_id") for d in decisions.values() if d.get("status") == "approved"}
    for proposal_id, proposal in proposals.items():
        # Superseded decisions and their packets are retained as history, not authority.
        if proposal_id not in active_proposals:
            continue
        before = len(errors)
        grants = proposal.get("authorizations")
        scope = proposal.get("scope")
        if not isinstance(scope, list) or not scope or not all(isinstance(s, str) for s in scope):
            errors.append(f"{proposal_id} requires explicit scope")
        if not isinstance(grants, list) or not grants:
            errors.append(f"{proposal_id} requires explicit authorizations")
            continue
        needed_contracts = set()
        for grant in grants:
            action, target_id = grant.get("action"), grant.get("target")
            target = targets.get(action, {}).get(target_id)
            if target is None:
                errors.append(f"{proposal_id} has unknown authorization target")
                continue
            if target_id not in (scope or []):
                errors.append(f"{proposal_id} authorization is outside proposal scope")
            issue = target_id if action == "develop_work" else target.get("decision_issue")
            if not re.fullmatch(r"CSL-[0-9]+", str(issue)) or proposal.get("linear_issue") != issue:
                errors.append(f"{proposal_id} authorization requires the matching Linear issue")
            if grant.get("target_digest") != target_digest(action, target):
                errors.append(f"{proposal_id} stale authorization target {target_id}")
            if action == "accept_contract":
                needed_contracts.add(target_id)
            else:
                needed_contracts.update(target.get("contract_ids", target.get("consumes_contracts", [])))
        if not bindings_match(proposal.get("contract_bindings"), sorted(needed_contracts)):
            errors.append(f"{proposal_id} stale or incomplete contract bindings")
        if len(errors) == before:
            valid_proposals.add(proposal_id)

    receipt_fields = {
        "linear_issue", "approval_comment_id", "human_decider", "authority_basis", "proposal_digest", "selected_option",
        "rejected_options", "rationale", "conditions", "scope", "decided_on",
        "approval_evidence", "revisit_trigger", "proposal_id",
    }
    valid_decisions = set()
    for decision_id, decision in decisions.items():
        if decision.get("status") == "approved":
            before = len(errors)
            absent = sorted(field for field in receipt_fields if not decision.get(field))
            if absent:
                errors.append(f"{decision_id} incomplete human receipt: {', '.join(absent)}")
            if not HEX64.fullmatch(str(decision.get("proposal_digest", ""))):
                errors.append(f"{decision_id} proposal_digest must be SHA-256")
            proposal = proposals.get(decision.get("proposal_id"))
            if proposal is None or decision.get("proposal_id") not in valid_proposals:
                errors.append(f"{decision_id} lacks a current named proposal")
            elif decision.get("proposal_digest") != digest(proposal):
                errors.append(f"{decision_id} stale proposal digest")
            elif decision.get("scope") != proposal.get("scope"):
                errors.append(f"{decision_id} receipt scope differs from proposal")
            elif decision.get("linear_issue") != proposal.get("linear_issue"):
                errors.append(f"{decision_id} receipt requires the matching Linear issue")
            if len(errors) == before:
                valid_decisions.add(decision_id)
        elif decision.get("status") not in {"pending", "superseded", "rejected"}:
            errors.append(f"{decision_id} has invalid status")

    def authorized(receipt: Any, action: str, target: str) -> bool:
        if receipt not in valid_decisions:
            return False
        proposal = proposals[decisions[receipt]["proposal_id"]]
        return any(g.get("action") == action and g.get("target") == target
                   for g in proposal["authorizations"])

    for contract_id, contract in contracts.items():
        if contract.get("status") == "accepted" and not authorized(
                contract.get("decision_receipt"), "accept_contract", contract_id):
            errors.append(f"{contract_id} accepted contract lacks applicable human receipt")

    admission = data["admission"]
    mode = admission.get("mode")
    if mode == "bounded_autonomy":
        if admission.get("routine_work_default") != "proceed":
            errors.append("bounded autonomy must let routine work proceed")
        if admission.get("human_gate_default") != "block_affected_lineage_only":
            errors.append("bounded autonomy must narrow human-gate blocking")
        for field in ("allowed_agent_actions", "human_decision_triggers"):
            if not isinstance(admission.get(field), list) or not admission[field]:
                errors.append(f"bounded autonomy requires non-empty {field}")
    elif mode not in {"open", "closed_pending_human_limits"}:
        errors.append("admission mode must be open, bounded_autonomy, or closed_pending_human_limits")

    occupancy = {}
    for field in ("active_contract_consumers", "active_reviews"):
        values = admission.get(field)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            errors.append(f"admission requires {field} as work item IDs")
            values = []
        if len(values) != len(set(values)) or any(v not in work for v in values):
            errors.append(f"admission {field} has duplicate or unknown work item IDs")
        occupancy[field] = set(values)
    # Lists cannot hide active consumers or verification work recorded in the ledger.
    consumers = {wid for wid, item in work.items() if item.get("status") == "in_progress"
                 and STAGES.get(item.get("stage"), -1) >= STAGES["development"]
                 and item.get("consumes_contracts")}
    reviews = {wid for wid, item in work.items() if item.get("status") == "in_progress"
               and item.get("stage") == "verification"}
    for field, inferred in (("active_contract_consumers", consumers), ("active_reviews", reviews)):
        if not inferred.issubset(occupancy[field]):
            errors.append(f"admission {field} omits active work")
        occupancy[field].update(inferred)
    occupied = any(occupancy.values())
    if mode == "closed_pending_human_limits" and occupied:
        errors.append("closed admission has active work")
    # Empty bounded lanes may remain descriptive while owner limits are pending.
    # Once work occupies capacity, both modes require the same pinned envelope.
    if mode == "open" or (mode == "bounded_autonomy" and occupied):
        if not authorized(admission.get("decision_receipt"), "admit_work", data["program_id"]):
            errors.append(f"{mode} admission lacks an applicable approved human decision receipt")
        for field, active in (("max_active_contract_consumers", "active_contract_consumers"),
                              ("max_concurrent_reviews", "active_reviews")):
            limit = admission.get(field)
            if type(limit) is not int or limit < 1:
                errors.append(f"{mode} admission requires a positive {field}")
            elif len(occupancy[active]) > limit:
                errors.append(f"{mode} admission exceeds {field}")

    done = DONE
    stage_rank = STAGES
    for item_id, item in work.items():
        if not re.fullmatch(r"CSL-[0-9]+", str(item.get("parent", ""))):
            errors.append(f"{item_id} missing parent lineage")
        stage = item.get("stage")
        if stage not in stage_rank:
            errors.append(f"{item_id} has invalid stage")
            continue
        discretion = item.get("discretion_class")
        if item.get("status") not in {"todo", "blocked", "in_progress", *DONE}:
            errors.append(f"{item_id} has invalid work status")
        if discretion not in {"bounded_autonomy", "collaborative_judgment", "human_gated"}:
            errors.append(f"{item_id} has invalid discretion class")
        if item.get("consequential") and discretion != "human_gated":
            errors.append(f"{item_id} consequential work must be human_gated")
        for dependency in item.get("hard_dependencies", []):
            if dependency not in work:
                errors.append(f"{item_id} references unknown hard dependency {dependency}")
            elif (stage_rank[stage] >= stage_rank["development"] or item.get("status") in done) and work[dependency].get("status") not in done:
                errors.append(f"{item_id} starts before hard dependency {dependency} is complete")
        for contract_id in item.get("consumes_contracts", []):
            if contract_id not in contracts:
                errors.append(f"{item_id} references unknown contract {contract_id}")
            elif stage_rank[stage] >= stage_rank["development"] and contracts[contract_id].get("status") != "accepted":
                errors.append(f"{item_id} consumes unaccepted contract {contract_id}")
            elif stage_rank[stage] >= stage_rank["development"]:
                receipt = item.get("contract_receipts", {}).get(contract_id)
                if receipt != contracts[contract_id].get("decision_receipt") or not authorized(receipt, "accept_contract", contract_id):
                    errors.append(f"{item_id} lacks matching contract receipt for {contract_id}")
        if (item.get("consequential") or discretion == "human_gated") and stage_rank[stage] >= stage_rank["development"]:
            receipt = item.get("decision_receipt")
            if not authorized(receipt, "develop_work", item_id):
                errors.append(f"{item_id} lacks an applicable approved human decision receipt")
        if "research_plan" in item or (stage == "research" and item.get("status") in {"in_progress", *DONE}):
            plan = item.get("research_plan")
            if not isinstance(plan, dict) or any(not plan.get(field) for field in PLAN_FIELDS):
                errors.append(f"{item_id} research plan is missing mandatory fields")
        if not item.get("acceptance_command"):
            errors.append(f"{item_id} missing acceptance command")
        for field in ("recovery", "rollback", "stop_conditions"):
            if not item.get(field):
                errors.append(f"{item_id} missing {field}")

    def visit(item_id: str, visiting: set[str], visited: set[str]) -> None:
        if item_id in visiting:
            errors.append(f"hard dependency cycle at {item_id}")
            return
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in work[item_id].get("hard_dependencies", []):
            if dependency in work:
                visit(dependency, visiting, visited)
        visiting.remove(item_id)
        visited.add(item_id)

    visited: set[str] = set()
    for item_id in work:
        visit(item_id, set(), visited)

    for capability in data["capabilities"]:
        capability_id = capability.get("id", "<unknown>")
        current = capability.get("current_maturity")
        target = capability.get("month_target")
        if current not in rank or target not in rank:
            errors.append(f"{capability_id} has unknown maturity tier")
            continue
        # month_target is an aspiration; evidence and authority govern actual maturity.
        if rank[current] >= rank["human_decided"] and not authorized(
                capability.get("decision_receipt"), "decide_capability", capability_id):
            errors.append(f"{capability_id} lacks applicable human decision authority")
        evidence_ids = capability.get("evidence_ids", [])
        for evidence_id in evidence_ids:
            if evidence_id not in evidence:
                errors.append(f"{capability_id} references unknown evidence {evidence_id}")
        if rank[current] >= rank["prototyped"]:
            independent = rank[current] >= rank["provider_free_verified"]
            kinds = {"prototyped": "prototype", "implemented": "implementation",
                     "provider_free_verified": "provider_free", "locally_qualified": "local_qualification",
                     "release_qualified": "release_qualification"}
            candidate = capability.get("candidate_commit")
            valid_candidate = bool(re.fullmatch(r"[0-9a-f]{40}", str(candidate)))
            def supports(row: dict[str, Any]) -> bool:
                return (valid_candidate and row.get("candidate_commit") == candidate
                        and row.get("capability_id") == capability_id
                        and row.get("result") == "pass" and row.get("status") == "current"
                        and row.get("tier") == current and row.get("kind") == kinds[current]
                        and (not independent or row.get("independent") is True)
                        and bool(row.get("artifact")) and bool(row.get("environment"))
                        and bool(HEX64.fullmatch(str(row.get("artifact_sha256", ""))))
                        and bindings_match(row.get("contract_bindings"), capability.get("contract_ids", [])))
            if not any(supports(evidence[eid]) for eid in evidence_ids if eid in evidence):
                label = "independent verification" if independent else "evidence"
                errors.append(f"{capability_id} claims {current} without {label} for the current candidate and tier")
        if rank[current] >= rank["contract_frozen"] and not capability.get("contract_ids"):
            errors.append(f"{capability_id} frozen maturity requires linked contracts")
        for contract_id in capability.get("contract_ids", []):
            if contract_id not in contracts:
                errors.append(f"{capability_id} references unknown contract {contract_id}")
            elif rank[current] >= rank["contract_frozen"] and contracts[contract_id].get("status") != "accepted":
                errors.append(f"{capability_id} frozen maturity requires accepted contract {contract_id}")
        for unknown_id in capability.get("unknown_ids", []):
            if unknown_id not in unknowns:
                errors.append(f"{capability_id} references unknown unknown {unknown_id}")
            else:
                unknown = unknowns[unknown_id]
                if unknown.get("status") != "resolved" and unknown.get("blocks") in rank and rank[current] >= rank[unknown["blocks"]]:
                    errors.append(f"{capability_id} maturity blocked by unresolved {unknown_id}")

    for unknown_id, unknown in unknowns.items():
        if unknown.get("status") not in {"open", "resolved"}:
            errors.append(f"{unknown_id} requires open or resolved status")
        if unknown.get("status") == "resolved" and not unknown.get("resolution_evidence"):
            errors.append(f"{unknown_id} resolved without resolution evidence")
        if unknown.get("blocks") not in rank:
            errors.append(f"{unknown_id} blocks an unknown maturity tier")
        if not unknown.get("resolution_issue") or not unknown.get("exposure"):
            errors.append(f"{unknown_id} lacks resolution issue or exposure")

    for risk in data["risks"]:
        if risk.get("severity") not in {"low", "medium", "high", "critical"}:
            errors.append(f"{risk.get('id')} has invalid severity")
        if not risk.get("mitigation") or not risk.get("trigger"):
            errors.append(f"{risk.get('id')} lacks mitigation or trigger")

    for checkpoint in data["integration_checkpoints"]:
        for contract_id in checkpoint.get("requires", []):
            if contract_id not in contracts:
                errors.append(f"{checkpoint.get('id')} references unknown contract {contract_id}")
        if not checkpoint.get("acceptance_command"):
            errors.append(f"{checkpoint.get('id')} missing acceptance command")
    return errors


def main() -> int:
    try:
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"PROGRAM_CONTROL_FAIL: {exc}", file=sys.stderr)
        return 1
    errors = validate_program(data)
    if errors:
        for error in errors:
            print(f"PROGRAM_CONTROL_FAIL: {error}", file=sys.stderr)
        return 1
    print(
        "PROGRAM_CONTROL_PASS "
        f"contracts={len(data['contracts'])} decisions={len(data['decisions'])} "
        f"work_items={len(data['work_items'])} capabilities={len(data['capabilities'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
