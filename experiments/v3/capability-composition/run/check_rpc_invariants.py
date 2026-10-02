#!/usr/bin/env python3
"""Gate for the R-PC invariant fixtures F02/F03/F05/F07-F10/F13/F15 (slice 3).

Table-driven: each case checks the candidate against the literal expected outcome and that the
planted mutant is detected. F04/F06/F14/F16 are covered by `check_rpc_observed.py`; F17/F18 are
deferred. Provider-free and in-process. An implementer PASS is not Owner acceptance.

    python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_invariants.py
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import rpc_expected as E  # noqa: E402
import rpc_model as M  # noqa: E402
import rpc_mutants as X  # noqa: E402
from fixtures import rpc_invariants as F  # noqa: E402

RESULTS = HERE.parent / "results"
failures: list[str] = []
invoked: list[str] = []


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        failures.append(message)


def matches(got: dict, expected: dict) -> bool:
    return all(got.get(k) == v for k, v in expected.items())


def case(name: str, fn, mutant_fn=None) -> None:
    invoked.append(name)
    expected = E.EXPECTED[name]
    got = fn()
    ok = matches(got, expected)
    detail = "" if ok else f" got={got} expected_subset={expected}"
    check(ok, f"{name}: candidate matches expected{detail}")
    if mutant_fn is not None:
        check(not matches(mutant_fn(), expected), f"{name}: planted mutant is detected")


def run() -> int:
    # F02 generated-summary identity
    case("F02_identity", lambda: M.summary_identity(F.SUMMARY_A, F.SUMMARY_B),
         lambda: X.m_summary_equal_seed(F.SUMMARY_A, F.SUMMARY_B))
    case("F02_replay", lambda: M.replay(b"summary-A"), lambda: X.m_replay_regenerates(b"summary-A"))
    case("F02_missing", lambda: M.replay(None))
    case("F02_collision", lambda: M.summary_identity(F.SUMMARY_COLLISION[0], F.SUMMARY_COLLISION[1]),
         lambda: X.m_summary_concatenation(F.SUMMARY_COLLISION[0], F.SUMMARY_COLLISION[1]))

    # F03 closure over a dependency graph
    def f03_valid():
        recorder = M.Recorder()
        result = M.resolve_closure(F.CLOSURE_GRAPH, F.CLOSURE_ROOTS, F.CLOSURE_OPTIONAL,
                                   F.CLOSURE_CAPACITY_OK, recorder)
        ordered = recorder.events
        check(("load", "S03:v1") in ordered and ("use", "S02:v1") in ordered
              and ordered.index(("load", "S03:v1")) < ordered.index(("use", "S02:v1")),
              "F03: required member loaded before use")
        return result

    case("F03_valid", f03_valid,
         lambda: X.m_closure_silent_drop(F.CLOSURE_GRAPH, F.CLOSURE_ROOTS, F.CLOSURE_OPTIONAL, F.CLOSURE_CAPACITY_OK))
    case("F03_missing", lambda: M.resolve_closure(F.CLOSURE_GRAPH_MISSING, F.CLOSURE_ROOTS, F.CLOSURE_OPTIONAL, F.CLOSURE_CAPACITY_OK))
    case("F03_unfittable", lambda: M.resolve_closure(F.CLOSURE_GRAPH, F.CLOSURE_ROOTS, F.CLOSURE_OPTIONAL, F.CLOSURE_CAPACITY_LOW),
         lambda: X.m_closure_auto_budget(F.CLOSURE_GRAPH, F.CLOSURE_ROOTS, F.CLOSURE_OPTIONAL, F.CLOSURE_CAPACITY_LOW))

    # F05 immediate revocation with coalesced preferences
    r = F.REVOCATION
    case("F05_revoked", lambda: M.access(r["grant"], r["fence"], r["revoke_fence"]),
         lambda: X.m_revoke_delayed(r["grant"], r["fence"], r["revoke_fence"]))
    case("F05_positive", lambda: M.access(r["grant"], 7, 8))
    case("F05_pending", lambda: M.coalesce_preferences(F.PREF_EVENTS),
         lambda: X.m_revoke_erased_by_coalesce(F.PREF_EVENTS))

    # F07 procedure precedence (permutation invariant)
    policy, reqs = F.PRECEDENCE_POLICY, F.PRECEDENCE["reqs"]
    case("F07_hold", lambda: M.resolve_precedence(reqs, policy, None))
    case("F07_override", lambda: M.resolve_precedence(reqs, policy, "skillA"),
         lambda: X.m_precedence_last_loaded(reqs, policy, "skillA"))
    case("F07_override_unauthorized", lambda: M.resolve_precedence(reqs, policy, "skillC"),
         lambda: X.m_precedence_accept_any_override(reqs, policy, "skillC"))
    winners = {M.resolve_precedence(list(perm), policy, "skillA")["winner"] for perm in itertools.permutations(reqs)}
    check(winners == {"skillA"}, "F07: winner invariant across arrival permutations")

    # F08 evidence conflict
    case("F08", lambda: M.resolve_evidence(F.EVIDENCE), lambda: X.m_evidence_synth(F.EVIDENCE))

    # F09 coherent cut with ABA
    cut = F.CUT_ATTEMPTS
    case("F09_valid", lambda: M.validate_cut(cut["valid"], 2))
    case("F09_mixed", lambda: M.validate_cut(cut["mixed"], 1), lambda: X.m_cut_accept_latest(cut["mixed"], 1))
    case("F09_aba_unstable", lambda: M.validate_cut(cut["aba_unstable"], 1),
         lambda: X.m_cut_values_only(cut["aba_unstable"], 1))
    case("F09_aba_recovered", lambda: M.validate_cut(cut["aba_recovered"], 3))

    # F10 one governor + compare-and-install
    initial, gov = F.GOVERNOR_INITIAL, F.GOVERNOR_INSTALL
    case("F10_first", lambda: M.govern(initial, gov["first"]))
    case("F10_late", lambda: M.govern(initial, gov["late"]), lambda: X.m_successor_last_completion(initial, gov["late"]))
    case("F10_duplicate", lambda: M.govern(initial, gov["duplicate"]),
         lambda: X.m_successor_duplicate_adopt(initial, gov["duplicate"]))
    case("F10_directive", lambda: M.govern(initial, gov["directive"]),
         lambda: X.m_successor_directive_ignored(initial, gov["directive"]))

    # F13 route/schema drift
    case("F13_valid_current", lambda: M.bind_route(F.ROUTE_CURRENT, None, F.ROUTE_REGISTRY))
    case("F13_drift_readmit", lambda: M.bind_route(F.ROUTE_CURRENT, F.ROUTE_REQUEST_READMIT, F.ROUTE_REGISTRY))
    case("F13_hold_unknown", lambda: M.bind_route(F.ROUTE_CURRENT, F.ROUTE_REQUEST_HOLD, F.ROUTE_REGISTRY),
         lambda: X.m_route_name_only(F.ROUTE_CURRENT, F.ROUTE_REQUEST_HOLD, F.ROUTE_REGISTRY))

    # F15 graph errors before load
    graphs = F.GRAPHS
    case("F15_missing_dependency", lambda: M.validate_graph(graphs["missing_dependency"]),
         lambda: X.m_graph_import_to_discover(graphs["missing_dependency"]))
    case("F15_cycle", lambda: M.validate_graph(graphs["cycle"]),
         lambda: X.m_graph_import_to_discover(graphs["cycle"]))
    case("F15_ambiguous_provider", lambda: M.validate_graph(graphs["ambiguous_provider"]),
         lambda: X.m_graph_import_to_discover(graphs["ambiguous_provider"]))
    case("F15_exclusive_conflict", lambda: M.validate_graph(graphs["exclusive_conflict"]),
         lambda: X.m_graph_import_to_discover(graphs["exclusive_conflict"]))
    case("F15_valid", lambda: M.validate_graph(graphs["valid"]))
    case("F15_invalid_selection", lambda: M.validate_graph(F.PROVIDER_INVALID),
         lambda: X.m_graph_selected_presence_only(F.PROVIDER_INVALID))
    case("F15_valid_selection", lambda: M.validate_graph(F.PROVIDER_VALID))

    # Oracle independence
    src = (HERE / "rpc_expected.py").read_text(encoding="utf-8")
    imports = [ln.strip() for ln in src.splitlines() if ln.strip().startswith(("import ", "from "))]
    check(not any("rpc_model" in ln for ln in imports), "rpc_expected imports do not include the candidate model")

    record = {
        "lane": "R-PC",
        "suite": "rpc_invariants",
        "fixtures": ["F02", "F03", "F05", "F07", "F08", "F09", "F10", "F13", "F15"],
        "deferred": ["F17", "F18"],
        "expected_cases": sorted(E.EXPECTED),
        "invoked_cases": invoked,
        "case_count": len(invoked),
        "provenance": {
            "rpc_model.py": _sha(HERE / "rpc_model.py"),
            "rpc_expected.py": _sha(HERE / "rpc_expected.py"),
            "rpc_mutants.py": _sha(HERE / "rpc_mutants.py"),
            "gate": _sha(HERE / "check_rpc_invariants.py"),
            "python": sys.version.split()[0],
        },
        "failures": failures,
        "disposition": "PASS" if not failures else "FAIL",
        "scope": "bounded synthetic; provider-free; not Owner acceptance",
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "rpc_invariants_results.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    if failures:
        print(f"RPC_INVARIANTS_FAIL ({len(failures)} failures)")
        return 1
    print("RPC_INVARIANTS_PASS F02 F03 F05 F07-F10 F13 F15; mutants detected; expectations independent")
    return 0


if __name__ == "__main__":
    sys.exit(run())
