"""Planted mutants for the R-PC invariant fixtures.

Each mutant violates exactly one documented predicate; the gate must detect every one. Mutants are
isolated test inputs, not instructions.
"""
from __future__ import annotations


def m_summary_equal_seed(summary_a: dict, summary_b: dict) -> dict:
    return {"a_ref": b"same", "b_ref": b"same", "distinct": False}


def m_summary_concatenation(summary_a: dict, summary_b: dict) -> dict:
    ref_a = summary_a["input"] + b"|" + summary_a["output"]
    ref_b = summary_b["input"] + b"|" + summary_b["output"]
    return {"a_ref": ref_a, "b_ref": ref_b, "distinct": ref_a != ref_b}


def m_replay_regenerates(recorded: bytes | None) -> dict:
    return {"outcome": "ok", "bytes": b"regenerated", "model_calls": 1}


def m_closure_auto_budget(required, optional, cost, capacity) -> dict:
    return {"outcome": "ok", "dispatched": True, "loaded": [required[0]], "dropped": [required[1]]}


def m_disabled_leaks(installed: bool, enabled: bool) -> dict:
    if not installed:
        return {"operational_bytes": 0, "events": [], "admin_bytes": 0}
    return {"operational_bytes": 16, "events": ["catalog_read"], "admin_bytes": 8}


def m_revoke_delayed(grant: str, fence: int, revoke_fence: int) -> dict:
    return {"outcome": "allow"}


def m_trust_claim(claim_authority: bool) -> dict:
    envelope = {"authority_carried_none": not claim_authority}
    sink = [] if envelope["authority_carried_none"] else ["unauthorized_effect"]
    return {"authority_carried_none": envelope["authority_carried_none"], "sink_events": sink, "control": "completed"}


def m_precedence_last_loaded(reqs, override) -> dict:
    return {"outcome": "ok", "winner": reqs[-1][0], "conflict": True}


def m_evidence_synth(sources) -> dict:
    return {"refs": [sources[0]["ref"]], "disagreement": False, "decision": sources[0]["value"]}


def m_cut_accept_latest(config_rev, required_schema, snapshot) -> dict:
    return {"outcome": "ok", "retries": 0}


def m_successor_last_completion(parent, candidate) -> dict:
    return {"outcome": "install", "installed": True, "epoch": candidate["epoch"]}


def m_route_name_only(new_descriptor, schema_valid, hidden_visibility_known) -> dict:
    return {"outcome": "ok"}


def m_restore_replay(manifest_refs, active_skill) -> dict:
    return {"outcome": "ok", "rechecked_rights": False, "operation_o1": "O1", "liability": "cleared", "reexecuted": True, "reapproved": True, "funds_reset": True}


def m_graph_import_to_discover(graph) -> dict:
    return {"outcome": "ok", "reason": None, "imported": True}


def m_silence_pass(observed, output_valid, refuse_all) -> dict:
    if refuse_all:
        return {"outcome": "fail_useful_completion"}
    if not output_valid:
        return {"outcome": "fail_invalid"}
    return {"outcome": "pass"}


class _ForgedRight:
    def __init__(self, subject: str) -> None:
        self.subject = subject
        self.digest = "forged"


def m_disabled_reads(catalog, installed, enabled, *, capability_id, source_id) -> dict:
    catalog.register(capability_id, enabled=True)
    catalog.read(source_id)
    return {"outcome": "disabled"}


def m_forged_authority(sink, ledger, envelope, *, op_id) -> dict:
    sink.perform(op_id, _ForgedRight(envelope.get("subject", "")))
    return {"outcome": "completed"}


def m_quiet_no_effect(sink, ledger, envelope, *, op_id) -> dict:
    return {"outcome": "completed"}


def m_restore_reexecute(ledger, manifest_refs, active_skill, *, operation_id="O1") -> dict:
    if active_skill not in manifest_refs:
        return {"outcome": "reject_missing_reference"}
    for ref in manifest_refs:
        ledger.check_rights(ref, ledger.current_fence)
    ledger.record_operation(operation_id, "resume", refs=tuple(manifest_refs))
    ledger.reexecute(operation_id)
    ledger.reapprove(operation_id)
    ledger.reset_funds(operation_id)
    return {"outcome": "ok", "restored": tuple(manifest_refs)}


def m_worker_silent(boundary, mode, *, op_id="op.work", subject="owner:grant7") -> dict:
    return {"outcome": "pass", "artifact": ""}


# --- Slice 3 mutants -----------------------------------------------------------------------------
def m_closure_silent_drop(graph, roots, optional, capacity, recorder=None) -> dict:
    return {"outcome": "ok", "dispatched": True, "mandatory": ["S02:v1", "S03:v1"],
            "loaded": ["O1:v1", "S02:v1", "S03:v1"], "optional_selected": ["O1:v1"],
            "dropped": [], "loss_record": []}


def m_closure_auto_budget(graph, roots, optional, capacity, recorder=None) -> dict:
    return {"outcome": "ok", "dispatched": True, "mandatory": ["S02:v1", "S03:v1"],
            "loaded": ["S02:v1", "S03:v1"], "optional_selected": [], "dropped": [], "loss_record": []}


def m_cut_values_only(attempts, budget) -> dict:
    for attempt in attempts:
        if attempt.get("config", {}).get("required_schema") == attempt.get("schema", {}).get("value"):
            return {"outcome": "ok", "retries": 0, "aba_detected": False}
    return {"outcome": "composition_cut_unavailable", "retries": budget, "aba_detected": False}


def m_cut_accept_latest(attempts, budget) -> dict:
    return {"outcome": "ok", "retries": 0, "aba_detected": False}


def m_successor_last_completion(initial, events) -> dict:
    accepted = None
    for ev in events:
        if ev.get("type") == "install":
            accepted = ev["id"]
    return {"outcome": "installed" if accepted else "no_install", "accepted": accepted,
            "epoch": initial.get("epoch"), "previous_preserved": False, "steps": []}


def m_successor_duplicate_adopt(initial, events) -> dict:
    accepted = None
    for ev in events:
        if ev.get("type") == "install":
            accepted = ev["id"]
    return {"outcome": "installed" if accepted else "no_install", "accepted": accepted,
            "epoch": initial.get("epoch"), "previous_preserved": True, "steps": []}


def m_successor_directive_ignored(initial, events) -> dict:
    accepted = None
    for ev in events:
        if ev.get("type") == "install":
            accepted = ev["id"]
    return {"outcome": "installed" if accepted else "no_install", "accepted": accepted,
            "epoch": initial.get("epoch"), "previous_preserved": True, "steps": []}


def m_route_name_only(current, request, registry) -> dict:
    if request is None:
        return {"outcome": "ok", "binding": dict(current), "hidden_visibility": "known"}
    return {"outcome": "ok", "binding": {"descriptor": request["descriptor"], "schema": request["schema"]},
            "hidden_visibility": "known"}


def m_graph_selected_presence_only(graph) -> dict:
    nodes = graph["nodes"]
    for spec in nodes.values():
        for dep in spec.get("deps", []):
            if dep not in nodes:
                return {"outcome": "reject", "reason": "missing_dependency", "imported": False}
    for cap, providers in graph.get("providers", {}).items():
        if len(providers) > 1 and cap not in graph.get("selected", {}):
            return {"outcome": "reject", "reason": "ambiguous_provider", "imported": False}
    return {"outcome": "ok", "reason": None, "imported": False}


def m_precedence_last_loaded_v2(reqs, policy, override) -> dict:
    return {"outcome": "ok", "winner": reqs[-1][0], "conflict": True}


def m_precedence_accept_any_override(reqs, policy, override) -> dict:
    if override is None:
        return {"outcome": "hold", "winner": None, "conflict": True}
    return {"outcome": "ok", "winner": override, "conflict": True}


def m_revoke_erased_by_coalesce(events) -> dict:
    prefs = [e["rev"] for e in events if e.get("type") == "pref"]
    return {"effective": prefs[0] if prefs else None, "pending": prefs[-1] if len(prefs) > 1 else None,
            "history": prefs[1:-1], "revoked": False, "revoke_fence": None}
