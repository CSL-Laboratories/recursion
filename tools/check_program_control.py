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
        "source_commit", "research", "maturity_order", "contracts", "decisions", "admission",
        "work_items", "capabilities", "evidence", "unknowns", "risks", "integration_checkpoints",
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
    if not isinstance(order, list) or len(order) != len(set(order)) or "unresearched" not in order:
        errors.append("maturity_order must contain unique tiers including unresearched")
        return errors
    rank = {value: index for index, value in enumerate(order)}
    contracts = _ids(data["contracts"], "contract", errors)
    decisions = _ids(data["decisions"], "decision", errors)
    work = _ids(data["work_items"], "work item", errors)
    evidence = _ids(data["evidence"], "evidence", errors)
    unknowns = _ids(data["unknowns"], "unknown", errors)
    _ids(data["capabilities"], "capability", errors)
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

    receipt_fields = {
        "human_decider", "authority_basis", "proposal_digest", "selected_option",
        "rejected_options", "rationale", "conditions", "scope", "decided_on",
        "approval_evidence", "revisit_trigger",
    }
    for decision_id, decision in decisions.items():
        if decision.get("status") == "approved":
            absent = sorted(field for field in receipt_fields if not decision.get(field))
            if absent:
                errors.append(f"{decision_id} incomplete human receipt: {', '.join(absent)}")
            if not HEX64.fullmatch(str(decision.get("proposal_digest", ""))):
                errors.append(f"{decision_id} proposal_digest must be SHA-256")
        elif decision.get("status") not in {"pending", "superseded", "rejected"}:
            errors.append(f"{decision_id} has invalid status")

    admission = data["admission"]
    if admission.get("mode") == "open":
        receipt = admission.get("decision_receipt")
        if receipt not in decisions or decisions[receipt].get("status") != "approved":
            errors.append("open admission lacks an approved human decision receipt")
        for field in ("max_active_contract_consumers", "max_concurrent_reviews"):
            if not isinstance(admission.get(field), int) or admission[field] < 1:
                errors.append(f"open admission requires a positive {field}")
    elif admission.get("mode") == "bounded_autonomy":
        if admission.get("routine_work_default") != "proceed":
            errors.append("bounded autonomy must let routine work proceed")
        if admission.get("human_gate_default") != "block_affected_lineage_only":
            errors.append("bounded autonomy must narrow human-gate blocking")
        for field in ("allowed_agent_actions", "human_decision_triggers"):
            if not isinstance(admission.get(field), list) or not admission[field]:
                errors.append(f"bounded autonomy requires non-empty {field}")
    elif admission.get("mode") != "closed_pending_human_limits":
        errors.append("admission mode must be open, bounded_autonomy, or closed_pending_human_limits")

    done = {"done", "accepted", "verified"}
    stage_rank = {"intake": 0, "research": 1, "decision": 2, "development": 3, "verification": 4, "integration": 5, "release": 6}
    for item_id, item in work.items():
        if not re.fullmatch(r"CSL-[0-9]+", str(item.get("parent", ""))):
            errors.append(f"{item_id} missing parent lineage")
        stage = item.get("stage")
        if stage not in stage_rank:
            errors.append(f"{item_id} has invalid stage")
            continue
        discretion = item.get("discretion_class")
        if discretion not in {"bounded_autonomy", "collaborative_judgment", "human_gated"}:
            errors.append(f"{item_id} has invalid discretion class")
        if item.get("consequential") and discretion != "human_gated":
            errors.append(f"{item_id} consequential work must be human_gated")
        for dependency in item.get("hard_dependencies", []):
            if dependency not in work:
                errors.append(f"{item_id} references unknown hard dependency {dependency}")
            elif stage_rank[stage] >= stage_rank["development"] and work[dependency].get("status") not in done:
                errors.append(f"{item_id} starts before hard dependency {dependency} is complete")
        for contract_id in item.get("consumes_contracts", []):
            if contract_id not in contracts:
                errors.append(f"{item_id} references unknown contract {contract_id}")
            elif stage_rank[stage] >= stage_rank["development"] and contracts[contract_id].get("status") != "accepted":
                errors.append(f"{item_id} consumes unaccepted contract {contract_id}")
        if item.get("consequential") and stage_rank[stage] >= stage_rank["development"]:
            receipt = item.get("decision_receipt")
            if receipt not in decisions or decisions[receipt].get("status") != "approved":
                errors.append(f"{item_id} lacks an approved human decision receipt")
        if not item.get("acceptance_command"):
            errors.append(f"{item_id} missing acceptance command")
        for field in ("recovery", "rollback", "stop_conditions"):
            if not item.get(field):
                errors.append(f"{item_id} missing {field}")

    for capability in data["capabilities"]:
        capability_id = capability.get("id", "<unknown>")
        current = capability.get("current_maturity")
        target = capability.get("month_target")
        if current not in rank or target not in rank:
            errors.append(f"{capability_id} has unknown maturity tier")
            continue
        if rank[current] > rank[target]:
            errors.append(f"{capability_id} current maturity exceeds target")
        evidence_ids = capability.get("evidence_ids", [])
        for evidence_id in evidence_ids:
            if evidence_id not in evidence:
                errors.append(f"{capability_id} references unknown evidence {evidence_id}")
        if rank[current] >= rank.get("prototyped", 10) and not evidence_ids:
            errors.append(f"{capability_id} claims {current} without evidence")
        if rank[current] >= rank.get("provider_free_verified", 10):
            if not any(evidence[eid].get("independent") for eid in evidence_ids if eid in evidence):
                errors.append(f"{capability_id} claims {current} without independent verification")
        for contract_id in capability.get("contract_ids", []):
            if contract_id not in contracts:
                errors.append(f"{capability_id} references unknown contract {contract_id}")
        for unknown_id in capability.get("unknown_ids", []):
            if unknown_id not in unknowns:
                errors.append(f"{capability_id} references unknown unknown {unknown_id}")

    for unknown_id, unknown in unknowns.items():
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
