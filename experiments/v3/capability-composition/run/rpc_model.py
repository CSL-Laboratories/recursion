"""R-PC invariant reference model (the candidate under test) for F02-F10, F13-F16.

Each function returns a typed dict outcome plus an event/effect record that an independent observer
inspects. This is a minimal semantic model of the R-PC candidate contract — not the product.
"""
from __future__ import annotations


# --- F02 generated-summary identity -------------------------------------------------------------
def summary_identity(summary_a: dict, summary_b: dict) -> dict:
    ref_a = summary_a["input"] + b"|" + summary_a["output"]
    ref_b = summary_b["input"] + b"|" + summary_b["output"]
    return {"a_ref": ref_a, "b_ref": ref_b, "distinct": ref_a != ref_b}


def replay(recorded: bytes | None) -> dict:
    if recorded is None:
        return {"outcome": "reproduction_input_unavailable", "model_calls": 0}
    return {"outcome": "ok", "bytes": recorded, "model_calls": 0}


# --- F03 mandatory closure over an exact-version dependency graph --------------------------------
class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def emit(self, tag: str, *args) -> None:
        self.events.append((tag,) + args)


def resolve_closure(graph, roots, optional, capacity, recorder=None) -> dict:
    recorder = recorder if recorder is not None else Recorder()
    mandatory: list[str] = []
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        qid = stack.pop()
        spec = graph.get(qid)
        if spec is None or not (spec.get("installed") and spec.get("enabled") and spec.get("authorized")):
            return {"outcome": "closure_required_missing", "reason": qid, "dispatched": False, "loaded": []}
        if qid in seen:
            continue
        seen.add(qid)
        mandatory.append(qid)
        for dep in spec.get("requires", []):
            stack.append(dep)
    mandatory = sorted(mandatory)
    mandatory_set = set(mandatory)
    optional = [q for q in optional if q not in mandatory_set]
    mandatory_cost = sum(graph[q]["cost"] for q in mandatory)
    if mandatory_cost > capacity:
        return {"outcome": "mandatory_context_unfittable", "dispatched": False, "loaded": [], "dropped": []}
    loaded = list(mandatory)
    used = mandatory_cost
    selected: list[str] = []
    dropped: list[str] = []
    for qid in sorted(optional):
        if qid not in graph:
            continue
        if used + graph[qid]["cost"] <= capacity:
            selected.append(qid)
            loaded.append(qid)
            used += graph[qid]["cost"]
        else:
            dropped.append(qid)
    loss_record = [{"ref": q, "reason": "dropped_optional_for_fit"} for q in sorted(dropped)]
    for qid in sorted(loaded):
        recorder.emit("load", qid)
    for qid in mandatory:
        recorder.emit("use", qid)
    return {"outcome": "ok", "dispatched": True, "mandatory": mandatory, "loaded": sorted(loaded),
            "optional_selected": sorted(selected), "dropped": sorted(dropped), "loss_record": loss_record}


# --- F04 disabled capability zero contribution (observed boundary) ------------------------------
def contribution(catalog, installed: bool, enabled: bool, *, capability_id: str, source_id: str) -> dict:
    if not installed:
        return {"outcome": "absent"}
    if not enabled:
        catalog.register(capability_id, enabled=False)
        return {"outcome": "disabled"}
    catalog.register(capability_id, enabled=True)
    catalog.read(source_id)
    catalog.load_body(capability_id)
    return {"outcome": "enabled"}


# --- F05 immediate revocation with coalesced preferences ----------------------------------------
def access(grant: str, fence: int, revoke_fence: int) -> dict:
    if grant == "grant7" and fence >= revoke_fence:
        return {"outcome": "refuse"}
    return {"outcome": "allow"}


def coalesce_preferences(events: list[dict]) -> dict:
    prefs = [e["rev"] for e in events if e.get("type") == "pref"]
    revokes = [e for e in events if e.get("type") == "revoke"]
    return {
        "effective": prefs[0] if prefs else None,
        "pending": prefs[-1] if len(prefs) > 1 else None,
        "history": prefs[1:-1],
        "revoked": bool(revokes),
        "revoke_fence": revokes[-1]["fence"] if revokes else None,
    }


# --- F06 authority laundering (observed protected sink) -----------------------------------------
def admit_import(sink, ledger, envelope: dict, *, op_id: str) -> dict:
    proof = ledger.authority_for(envelope)
    admitted = sink.perform(op_id, proof)
    if admitted:
        sink.complete(op_id, b"useful\n")
    return {"outcome": "completed" if admitted else "refused"}


# --- F07 procedure precedence (policy + authorized override) ------------------------------------
def resolve_precedence(reqs: list[tuple[str, str]], policy: dict, override: str | None) -> dict:
    admitted = policy.get("admitted", [])
    if override is None or not policy.get("override_allowed") or override not in admitted:
        return {"outcome": "hold", "winner": None, "conflict": True}
    return {"outcome": "ok", "winner": override, "conflict": True, "order": tuple(admitted)}


# --- F08 evidence conflict ----------------------------------------------------------------------
def resolve_evidence(sources: list[dict]) -> dict:
    return {"refs": [s["ref"] for s in sources], "disagreement": True, "decision": None}


# --- F09 coherent cut with ABA revision capture -------------------------------------------------
def _value_key(domain: str, read: dict):
    return read.get("required_schema") if domain == "config" else read.get("value")


def validate_cut(attempts: list[dict], budget: int) -> dict:
    prev = None
    aba = False
    for i, attempt in enumerate(attempts):
        if prev is not None:
            for domain in ("config", "schema"):
                cur, old = attempt.get(domain, {}), prev.get(domain, {})
                if _value_key(domain, old) == _value_key(domain, cur) and old.get("revision") != cur.get("revision"):
                    aba = True
        coherent = attempt.get("config", {}).get("required_schema") == attempt.get("schema", {}).get("value")
        if i > 0 and prev is not None and coherent:
            same_rev = all(attempt.get(d, {}).get("revision") == prev.get(d, {}).get("revision")
                           for d in ("config", "schema"))
            if same_rev:
                return {"outcome": "ok", "retries": i, "aba_detected": aba}
        if i >= budget:
            return {"outcome": "composition_cut_unavailable", "retries": i, "aba_detected": aba}
        prev = attempt
    return {"outcome": "composition_cut_unavailable", "retries": max(0, len(attempts) - 1), "aba_detected": aba}


# --- F10 one governor and compare-and-install over a transition history -------------------------
def govern(initial: dict, events: list[dict]) -> dict:
    state = dict(initial)
    accepted = None
    previous_preserved = False
    steps: list[tuple] = []
    for ev in events:
        if ev.get("type") == "directive":
            state["directive_rev"] = ev["rev"]
            steps.append(("directive", ev["rev"]))
            continue
        if ev.get("rebase") and ev.get("new_input_ref"):
            accepted = ev["id"]
            previous_preserved = True
            state["epoch"] = ev.get("epoch", state["epoch"])
            state["parent"] = ev.get("parent", state["parent"])
            state["directive_rev"] = ev.get("directive_rev", state["directive_rev"])
            steps.append(("install", ev["id"]))
            continue
        if ev["parent"] != state["parent"]:
            steps.append(("stale_reject", ev["id"]))
        elif ev["epoch"] <= state["epoch"]:
            steps.append(("duplicate_epoch_reject", ev["id"]))
        elif ev["directive_rev"] != state["directive_rev"]:
            steps.append(("directive_changed_reject", ev["id"]))
        else:
            accepted = ev["id"]
            previous_preserved = True
            state["epoch"] = ev["epoch"]
            state["parent"] = "c" + str(ev["epoch"])
            steps.append(("install", ev["id"]))
    return {"outcome": "installed" if accepted else "no_install", "accepted": accepted,
            "epoch": state["epoch"], "previous_preserved": previous_preserved, "steps": steps}


# --- F13 route and schema drift (admitted registry) ---------------------------------------------
def bind_route(current: dict, request: dict | None, registry: dict) -> dict:
    if request is None:
        return {"outcome": "ok", "binding": dict(current), "hidden_visibility": "known"}
    spec = registry.get(request.get("descriptor", ""))
    if spec and spec.get("admitted") and request.get("schema") in spec.get("schemas", []) \
            and request.get("tool_visibility_known"):
        return {"outcome": "ok", "binding": {"descriptor": request["descriptor"], "schema": request["schema"]},
                "hidden_visibility": "known"}
    return {"outcome": "hold", "binding": dict(current), "hidden_visibility": "unknown"}


# --- F14 checkpoint resume (observed rights ledger) ---------------------------------------------
def restore(ledger, manifest_refs: list[str], active_skill: str, *, operation_id: str = "O1") -> dict:
    if active_skill not in manifest_refs:
        return {"outcome": "reject_missing_reference"}
    for ref in manifest_refs:
        ledger.check_rights(ref, ledger.current_fence)
    ledger.record_operation(operation_id, "resume", refs=tuple(manifest_refs))
    return {"outcome": "ok", "restored": tuple(manifest_refs)}


# --- F15 graph errors before load ---------------------------------------------------------------
def _has_cycle(nodes: dict) -> bool:
    seen: dict[str, int] = {}

    def visit(name: str) -> bool:
        state = seen.get(name, 0)
        if state == 1:
            return True
        if state == 2:
            return False
        seen[name] = 1
        for dep in nodes.get(name, {}).get("deps", []):
            if dep in nodes and visit(dep):
                return True
        seen[name] = 2
        return False

    return any(visit(n) for n in nodes)


def validate_graph(graph: dict) -> dict:
    nodes = graph["nodes"]
    for spec in nodes.values():
        for dep in spec.get("deps", []):
            if dep not in nodes:
                return {"outcome": "reject", "reason": "missing_dependency", "imported": False}
    if _has_cycle(nodes):
        return {"outcome": "reject", "reason": "cycle", "imported": False}
    selected = graph.get("selected", {})
    for cap, providers in graph.get("providers", {}).items():
        if cap in selected:
            if selected[cap] not in providers:
                return {"outcome": "reject", "reason": "invalid_provider_selection", "imported": False}
        elif len(providers) > 1:
            return {"outcome": "reject", "reason": "ambiguous_provider", "imported": False}
    for _res, owners in graph.get("exclusive", {}).items():
        if len(set(owners)) > 1:
            return {"outcome": "reject", "reason": "exclusive_owner_conflict", "imported": False}
    return {"outcome": "ok", "reason": None, "imported": False}


# --- F16 observer and oracle sensitivity --------------------------------------------------------
def observe_gate(observed: bool, output_valid: bool, refuse_all: bool) -> dict:
    if not observed:
        return {"outcome": "HOLD_UNKNOWN"}
    if refuse_all:
        return {"outcome": "fail_useful_completion"}
    if not output_valid:
        return {"outcome": "fail_invalid"}
    return {"outcome": "pass"}


# --- F16 useful worker over the observed boundary -----------------------------------------------
def useful_worker(boundary, mode: str, *, op_id: str = "op.work", subject: str = "owner:grant7") -> dict:
    artifacts = {"normal_A": b"answer-A\n", "normal_B": b"answer-B\n", "invalid": b""}
    artifact = artifacts.get(mode, b"")
    proof = boundary.ledger.issue_proof(subject)
    ok = boundary.sink.perform(op_id, proof)
    if ok:
        boundary.sink.complete(op_id, artifact)
    return {"outcome": "pass", "artifact": artifact.decode("utf-8", "replace")}
