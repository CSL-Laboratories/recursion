"""Planted mutants for the R-PC conformance suite.

One mutant per documented predicate violation; every mutant here is referenced by a gate and must be
detected on at least one asserted field. Test-only; may reference the candidate's interfaces. Cleaned
after the strengthened-suite verification (dead and shadowed duplicates removed).
"""
from __future__ import annotations


class _ForgedRight:
    def __init__(self, subject: str) -> None:
        self.subject = subject
        self.digest = "forged"


# --- F02 generated-summary identity ---
def m_summary_equal_seed(summary_a, summary_b) -> dict:
    return {"a_ref": b"same", "b_ref": b"same", "distinct": False}


def m_summary_concatenation(summary_a, summary_b) -> dict:
    ref_a = summary_a["input"] + b"|" + summary_a["output"]
    ref_b = summary_b["input"] + b"|" + summary_b["output"]
    return {"a_ref": ref_a, "b_ref": ref_b, "distinct": ref_a != ref_b}


def m_replay_regenerates(recorded) -> dict:
    return {"outcome": "ok", "bytes": b"regenerated", "model_calls": 1}


# --- F03 closure ---
def m_closure_silent_drop(graph, roots, optional, capacity, recorder=None) -> dict:
    return {"outcome": "ok", "dispatched": True, "mandatory": ["S02:v1", "S03:v1"],
            "loaded": ["O1:v1", "S02:v1", "S03:v1"], "optional_selected": ["O1:v1"],
            "dropped": [], "loss_record": []}


def m_closure_auto_budget(graph, roots, optional, capacity, recorder=None) -> dict:
    return {"outcome": "ok", "dispatched": True, "mandatory": ["S02:v1", "S03:v1"],
            "loaded": ["S02:v1", "S03:v1"], "optional_selected": [], "dropped": [], "loss_record": []}


# --- F04 disabled inertness (observed) ---
def m_disabled_reads(catalog, installed, enabled, *, capability_id, source_id) -> dict:
    catalog.register(capability_id, enabled=True)
    catalog.read(source_id)
    return {"outcome": "disabled"}


# --- F05 revocation ---
def m_revoke_delayed(grant, fence, revoke_fence) -> dict:
    return {"outcome": "allow"}


def m_revoke_erased_by_coalesce(events) -> dict:
    prefs = [e["rev"] for e in events if e.get("type") == "pref"]
    return {"effective": prefs[0] if prefs else None, "pending": prefs[-1] if len(prefs) > 1 else None,
            "history": prefs[1:-1], "revoked": False, "revoke_fence": None}


# --- F06 authority laundering (observed) ---
def m_forged_authority(sink, ledger, envelope, *, op_id) -> dict:
    sink.perform(op_id, _ForgedRight(envelope.get("subject", "")))
    return {"outcome": "completed"}


def m_always_refuse(sink, ledger, envelope, *, op_id) -> dict:
    sink.perform(op_id, None)
    return {"outcome": "refused"}


def m_quiet_no_effect(sink, ledger, envelope, *, op_id) -> dict:
    return {"outcome": "completed"}


# --- F08 evidence conflict ---
def m_evidence_synth(sources) -> dict:
    return {"refs": [sources[0]["ref"]], "disagreement": False, "decision": sources[0]["value"]}


# --- F09 coherent cut ---
def m_cut_values_only(attempts, budget) -> dict:
    for attempt in attempts:
        if attempt.get("config", {}).get("required_schema") == attempt.get("schema", {}).get("value"):
            return {"outcome": "ok", "retries": 0, "aba_detected": False}
    return {"outcome": "composition_cut_unavailable", "retries": budget, "aba_detected": False}


def m_cut_accept_latest(attempts, budget) -> dict:
    return {"outcome": "ok", "retries": 0, "aba_detected": False}


# --- F10 one governor ---
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


# --- F13 route/schema drift ---
def m_route_name_only(current, request, registry) -> dict:
    if request is None:
        return {"outcome": "ok", "binding": dict(current), "hidden_visibility": "known"}
    return {"outcome": "ok", "binding": {"descriptor": request["descriptor"], "schema": request["schema"]},
            "hidden_visibility": "known"}


# --- F14 checkpoint resume (observed) ---
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


# --- F15 graph errors before load ---
def m_graph_import_to_discover(graph) -> dict:
    return {"outcome": "ok", "reason": None, "imported": True}


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


# --- F16 useful worker (observed) ---
def m_worker_silent(boundary, mode, *, op_id="op.work", subject="owner:grant7") -> dict:
    return {"outcome": "pass", "artifact": ""}


def m_worker_refuse(boundary, mode, *, op_id="op.work", subject="owner:grant7") -> dict:
    boundary.sink.perform(op_id, None)
    return {"outcome": "pass", "artifact": ""}


def m_invalid_emit_valid_return(boundary, mode, *, op_id="op.work", subject="owner:grant7") -> dict:
    artifact = {"normal_A": "answer-A\n", "normal_B": "answer-B\n", "invalid": ""}.get(mode, "")
    proof = boundary.ledger.issue_proof(subject)
    if boundary.sink.perform(op_id, proof):
        boundary.sink.complete(op_id, b"")
    return {"outcome": "pass", "artifact": artifact}


# --- F07 procedure precedence ---
def m_precedence_last_loaded(reqs, policy, override) -> dict:
    return {"outcome": "ok", "winner": reqs[-1][0], "conflict": True}


def m_precedence_accept_any_override(reqs, policy, override) -> dict:
    if override is None:
        return {"outcome": "hold", "winner": None, "conflict": True}
    return {"outcome": "ok", "winner": override, "conflict": True}
