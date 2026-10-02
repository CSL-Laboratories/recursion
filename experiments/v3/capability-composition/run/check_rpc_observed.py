#!/usr/bin/env python3
"""Observed-boundary gate for the R-PC fixtures F04/F06/F14/F16 (slice 2, strengthens Q02/Q05).

The candidate must cause behavior through the harness-owned Boundary (observer stand-ins); the gate
asserts on recorded events, never on candidate booleans, and validates results with a strict schema.
Provider-free and in-process. An implementer PASS is not Owner acceptance.

    python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_observed.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import observer as O  # noqa: E402
import record_schema as RS  # noqa: E402
import rpc_model as M  # noqa: E402
import rpc_mutants as X  # noqa: E402
import oracle  # noqa: E402
from fixtures import rpc_invariants as F  # noqa: E402

RESULTS = HERE.parent / "results"
OUTCOME = RS.RecordSpec((RS.FieldSpec("outcome", str),))
failures: list[str] = []
invoked: list[str] = []
case_results: list[dict] = []


def _record(fixture: str, fn) -> None:
    before = len(failures)
    fn()
    case_results.append({"fixture": fixture, "checks_failed": list(failures[before:])})


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        failures.append(message)


def _seed_ledger(boundary):
    for subject, spec in F.F14["rights"].items():
        boundary.ledger.install_right(subject, active=spec["active"], fence=spec["fence"])


def f04() -> None:
    print("F04 — disabled source-read/registration inertness (observed)")
    b = O.Boundary()
    r = M.contribution(b.catalog, False, False, capability_id=F.F04["capability"], source_id=F.F04["source"])
    check(RS.record_conforms(r, {"outcome": "absent"}, OUTCOME), "absent: strict schema")
    check(b.events() == (), "absent: no events")

    b = O.Boundary()
    r = M.contribution(b.catalog, True, False, capability_id=F.F04["capability"], source_id=F.F04["source"])
    check(RS.record_conforms(r, {"outcome": "disabled"}, OUTCOME), "disabled: strict schema")
    check(b.observer.count(O.CATALOG_READ) == 0, "disabled: no source read")
    check(b.observer.count(O.LOAD) == 0 and b.observer.count(O.SELECTOR_QUERY) == 0, "disabled: no load/selector")
    check(b.observer.count(O.EFFECT) == 0, "disabled: no effect")
    check(b.observer.operational_bytes() == 0, "disabled: zero operational bytes")
    regs = b.observer.events(O.REGISTER)
    check(len(regs) == 1 and regs[0].detail == "disabled", "disabled: only a disabled registration")

    b = O.Boundary()
    X.m_disabled_reads(b.catalog, True, False, capability_id=F.F04["capability"], source_id=F.F04["source"])
    check(b.observer.count(O.CATALOG_READ) > 0 or b.observer.operational_bytes() > 0,
          "disabled: leaked-read mutant detected")

    b = O.Boundary(contents={F.F04["source"]: F.F04["content"], F.F04["capability"]: b"body"})
    r = M.contribution(b.catalog, True, True, capability_id=F.F04["capability"], source_id=F.F04["source"])
    check(RS.record_conforms(r, {"outcome": "enabled"}, OUTCOME), "enabled: strict schema")
    regs = b.observer.events(O.REGISTER)
    check(len(regs) == 1 and regs[0].detail == "enabled", "enabled: registration observed")
    check(b.observer.has(O.CATALOG_READ, F.F04["source"]), "enabled: source read observed")
    check(b.observer.has(O.LOAD, F.F04["capability"]), "enabled: body load observed")
    check(b.observer.operational_bytes() == len(F.F04["content"]), "enabled: operational bytes counted")
    check(b.observer.count(O.EFFECT) == 0, "enabled: no effect")


def _authorized_envelope(boundary) -> dict:
    proof = boundary.ledger.issue_proof(F.F06["owner_envelope"]["subject"])
    return {"subject": F.F06["owner_envelope"]["subject"], "proof": proof.digest}


def f06() -> None:
    print("F06 — protected sink: candidate refusal AND authorized candidate completion")
    b = O.Boundary()
    r = M.admit_import(b.sink, b.ledger, F.F06["import_envelope"], op_id=F.F06["protected_op"])
    check(RS.record_conforms(r, {"outcome": "refused"}, OUTCOME), "import: refused (no laundering)")
    check(b.observer.admitted_effects() == (), "import: no admitted effect")
    check(b.observer.useful_completions() == (), "import: no useful completion")

    def authorized(boundary):
        return M.admit_import(boundary.sink, boundary.ledger, _authorized_envelope(boundary),
                              op_id=F.F06["protected_op"])

    b = O.Boundary()
    b.ledger.install_right(F.F06["owner_envelope"]["subject"], active=True, fence=0)
    r = authorized(b)
    check(RS.record_conforms(r, {"outcome": "completed"}, OUTCOME), "authorized: candidate completed")
    check(len(b.observer.admitted_effects()) == 1, "authorized: one admitted effect observed")
    check(len(b.observer.useful_completions()) == 1, "authorized: one useful completion observed")

    b = O.Boundary()
    b.ledger.install_right(F.F06["owner_envelope"]["subject"], active=True, fence=0)
    mr = X.m_always_refuse(b.sink, b.ledger, _authorized_envelope(b), op_id=F.F06["protected_op"])
    check(not (mr.get("outcome") == "completed" and b.observer.admitted_effects()),
          "always-refuse mutant detected on the authorized path")

    b = O.Boundary()
    b.ledger.install_right(F.F06["owner_envelope"]["subject"], active=True, fence=0)
    X.m_quiet_no_effect(b.sink, b.ledger, _authorized_envelope(b), op_id=F.F06["protected_op"])
    check(b.observer.admitted_effects() == (), "quiet-no-effect mutant detected (no recorded effect)")

    b = O.Boundary()
    X.m_forged_authority(b.sink, b.ledger, F.F06["import_envelope"], op_id=F.F06["protected_op"])
    check(b.observer.admitted_effects() == (), "forged-authority mutant detected (no admitted effect)")


def f14() -> None:
    print("F14 — restored refs + current-rights recheck + O1 ledger (observed)")
    b = O.Boundary(current_fence=F.F14["current_fence"])
    _seed_ledger(b)
    r = M.restore(b.ledger, F.F14["omitting_refs"], F.F14["active_skill"])
    check(RS.record_conforms(r, {"outcome": "reject_missing_reference"}, OUTCOME), "missing: rejected")
    check(b.observer.count(O.RIGHTS_CHECK) == 0 and b.observer.count(O.OPERATION) == 0,
          "missing: no side effects on rejection")

    b = O.Boundary(current_fence=F.F14["current_fence"])
    _seed_ledger(b)
    b.ledger.set_liability("O1", "held")
    r = M.restore(b.ledger, F.F14["valid_manifest_refs"], F.F14["active_skill"])
    check(r.get("outcome") == "ok" and tuple(r.get("restored", ())) == tuple(F.F14["valid_manifest_refs"]),
          "valid: restored refs")
    checks = {e.target: e for e in b.observer.events(O.RIGHTS_CHECK)}
    check(set(checks) == {"skillX", "procY", "skillZ"}, "valid: rights rechecked for every ref")
    check(all(e.detail == str(F.F14["current_fence"]) for e in checks.values()), "valid: current fence observed")
    check(checks["procY"].allowed is False and checks["skillX"].allowed is True and checks["skillZ"].allowed is True,
          "valid: revoked vs current observed (not candidate-reported)")
    check(b.observer.count(O.OPERATION, "O1") == 1, "valid: O1 recorded")
    check(b.observer.count(O.REEXECUTE) == 0 and b.observer.count(O.REAPPROVE) == 0
          and b.observer.count(O.FUNDS_RESET) == 0, "valid: no replay")
    check(b.ledger.liability("O1") == "held", "valid: liability held")

    b = O.Boundary(current_fence=F.F14["current_fence"])
    _seed_ledger(b)
    b.ledger.set_liability("O1", "held")
    X.m_restore_reexecute(b.ledger, F.F14["valid_manifest_refs"], F.F14["active_skill"])
    check(b.observer.count(O.REEXECUTE) > 0, "replay mutant detected at the ledger")


def _completion(boundary, op_id):
    events = boundary.observer.events(O.CONTROL_COMPLETE, op_id)
    return events[-1] if events else None


def _bound(boundary, op_id, returned: bytes) -> bool:
    import hashlib
    event = _completion(boundary, op_id)
    if event is None:
        return False
    return (event.bytes_len == len(returned) and event.bytes_len > 0
            and event.payload_sha256 == hashlib.sha256(returned).hexdigest())


def f16() -> None:
    print("F16 — observer loss + recorded-completion binding + all-refusing candidate")
    op = F.F16["protected_op"]
    subject = F.F16["authority_subject"]

    b = O.Boundary(enabled=False); b.ledger.install_right(subject, active=True, fence=0)
    M.useful_worker(b, "normal_A", op_id=op, subject=subject)
    check(O.classify(b.observer, output_valid=True) == "HOLD_UNKNOWN", "missing observer: HOLD_UNKNOWN")
    check(b.observer.observation_missing is True, "missing observer: flagged")

    a = O.Boundary(); a.ledger.install_right(subject, active=True, fence=0)
    ra = M.useful_worker(a, "normal_A", op_id=op, subject=subject)
    c = O.Boundary(); c.ledger.install_right(subject, active=True, fence=0)
    rc = M.useful_worker(c, "normal_B", op_id=op, subject=subject)
    check(ra["artifact"] != rc["artifact"], "valid: two distinct valid outputs")
    ra_b = _bound(a, op, ra["artifact"].encode())
    rc_b = _bound(c, op, rc["artifact"].encode())
    check(ra_b and rc_b, "valid: returned artifact bound to recorded completion payload")
    check(oracle.valid_artifact(ra["artifact"].encode()) and oracle.valid_artifact(rc["artifact"].encode()),
          "valid: both outputs pass the independent oracle")
    check(O.classify(a.observer, output_valid=ra_b) == "PASS" and O.classify(c.observer, output_valid=rc_b) == "PASS",
          "valid: validity derived from recorded payload, PASS")

    b = O.Boundary(); b.ledger.install_right(subject, active=True, fence=0)
    r = M.useful_worker(b, "invalid", op_id=op, subject=subject)
    check(O.classify(b.observer, output_valid=_bound(b, op, r["artifact"].encode())) == "FAIL_INVALID",
          "invalid: FAIL_INVALID")

    b = O.Boundary(); b.ledger.install_right(subject, active=True, fence=0)
    r = X.m_invalid_emit_valid_return(b, "normal_A", op_id=op, subject=subject)
    check(_bound(b, op, r["artifact"].encode()) is False,
          "empty-completion-but-valid-return mutant detected (returned artifact unbound)")

    b = O.Boundary(); b.ledger.install_right(subject, active=True, fence=0)
    X.m_worker_refuse(b, "normal_A", op_id=op, subject=subject)
    check(O.classify(b.observer, output_valid=True) == "FAIL_USEFUL_COMPLETION",
          "all-refusing candidate: FAIL_USEFUL_COMPLETION")

    b = O.Boundary(enabled=False)
    X.m_worker_silent(b, "normal_A", op_id=op, subject=subject)
    check(O.classify(b.observer, output_valid=True) == "HOLD_UNKNOWN", "silent mutant yields HOLD_UNKNOWN")


def run() -> int:
    _record("F04", f04)
    _record("F06", f06)
    _record("F14", f14)
    _record("F16", f16)

    obs_src = (HERE / "observer.py").read_text(encoding="utf-8")
    obs_imports = [ln.strip() for ln in obs_src.splitlines() if ln.strip().startswith(("import ", "from "))]
    check(not any(("rpc_model" in ln or "reference_model" in ln) for ln in obs_imports),
          "observer.py does not import the candidate model")

    RESULTS.mkdir(exist_ok=True)
    record = {
        "lane": "R-PC",
        "suite": "rpc_observed",
        "fixtures": ["F04", "F06", "F14", "F16"],
        "invoked_cases": ["F04", "F06", "F14", "F16"],
        "provenance": {
            "observer.py": _sha(HERE / "observer.py"),
            "record_schema.py": _sha(HERE / "record_schema.py"),
            "rpc_model.py": _sha(HERE / "rpc_model.py"),
            "gate": _sha(HERE / "check_rpc_observed.py"),
            "python": sys.version.split()[0],
        },
        "case_results": case_results,
        "failures": failures,
        "disposition": "PASS" if not failures else "FAIL",
        "scope": "bounded synthetic; provider-free; not Owner acceptance",
    }
    (RESULTS / "rpc_observed_results.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    if failures:
        print(f"RPC_OBSERVED_FAIL ({len(failures)} failures)")
        return 1
    print("RPC_OBSERVED_PASS F04 F06 F14 F16; observations independent; strict schema")
    return 0


if __name__ == "__main__":
    sys.exit(run())
