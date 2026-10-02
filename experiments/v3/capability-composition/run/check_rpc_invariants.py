#!/usr/bin/env python3
"""Gate for the R-PC invariant fixtures F02-F10 and F13-F16.

Table-driven: for each case it checks the candidate against the literal expected outcome and then
checks that the planted mutant is detected (differs from the expected outcome). F17/F18 are deferred
and not covered here (see ../FIXTURE-INDEX.md). Provider-free and in-process. An implementer PASS is
not Owner acceptance.

    python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_invariants.py
"""
from __future__ import annotations

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


def check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        failures.append(message)


def matches(got: dict, expected: dict) -> bool:
    return all(got.get(k) == v for k, v in expected.items())


def case(name: str, candidate_fn, mutant_fn) -> None:
    expected = E.EXPECTED[name]
    got = candidate_fn()
    ok = matches(got, expected)
    detail = "" if ok else f" got={got} expected_subset={expected}"
    check(ok, f"{name}: candidate matches expected{detail}")
    if mutant_fn is not None:
        m = mutant_fn()
        check(not matches(m, expected), f"{name}: planted mutant is detected")


def run() -> int:
    # F02 generated-summary identity
    case("F02_identity", lambda: M.summary_identity(F.SUMMARY_A, F.SUMMARY_B), lambda: X.m_summary_equal_seed(F.SUMMARY_A, F.SUMMARY_B))
    case("F02_replay", lambda: M.replay(b"summary-A"), lambda: X.m_replay_regenerates(b"summary-A"))
    case("F02_missing", lambda: M.replay(None), None)

    # F03 mandatory closure
    case("F03_case1", lambda: M.resolve_closure(F.CLOSURE["required"], F.CLOSURE["optional"], F.CLOSURE["cost"], F.CLOSURE["capacity_case1"]), lambda: X.m_closure_auto_budget(F.CLOSURE["required"], F.CLOSURE["optional"], F.CLOSURE["cost"], F.CLOSURE["capacity_case2"]))
    case("F03_case2", lambda: M.resolve_closure(F.CLOSURE["required"], F.CLOSURE["optional"], F.CLOSURE["cost"], F.CLOSURE["capacity_case2"]), None)

    # F05 immediate revocation
    r = F.REVOCATION
    case("F05", lambda: M.access(r["grant"], r["fence"], r["revoke_fence"]), lambda: X.m_revoke_delayed(r["grant"], r["fence"], r["revoke_fence"]))

    # F07 procedure precedence
    p = F.PRECEDENCE
    case("F07_hold", lambda: M.resolve_precedence(p["reqs"], None), None)
    case("F07_override", lambda: M.resolve_precedence(p["reqs"], p["override"]), lambda: X.m_precedence_last_loaded(p["reqs"], p["override"]))

    # F08 evidence conflict
    case("F08", lambda: M.resolve_evidence(F.EVIDENCE), lambda: X.m_evidence_synth(F.EVIDENCE))

    # F09 coherent cut (mixed vs valid)
    c = F.CUT
    case("F09_mixed", lambda: M.validate_cut(c["config_rev"], c["required_schema"], c["mixed_snapshot"]), lambda: X.m_cut_accept_latest(c["config_rev"], c["required_schema"], c["mixed_snapshot"]))
    case("F09_valid", lambda: M.validate_cut(c["config_rev"], c["required_schema"], c["valid_snapshot"]), None)

    # F10 one governor / stale parent
    s = F.SUCCESSOR
    case("F10_stale", lambda: M.install_successor(s["parent"], s["stale"]), lambda: X.m_successor_last_completion(s["parent"], s["stale"]))
    case("F10_current", lambda: M.install_successor(s["parent"], s["current"]), None)

    # F13 route/schema drift
    case("F13", lambda: M.bind_route("d5", schema_valid=False, hidden_visibility_known=False), lambda: X.m_route_name_only("d5", False, False))

    # F15 graph errors before load
    g = F.GRAPHS
    case("F15_missing_dependency", lambda: M.validate_graph(g["missing_dependency"]), lambda: X.m_graph_import_to_discover(g["missing_dependency"]))
    case("F15_cycle", lambda: M.validate_graph(g["cycle"]), lambda: X.m_graph_import_to_discover(g["cycle"]))
    case("F15_ambiguous_provider", lambda: M.validate_graph(g["ambiguous_provider"]), lambda: X.m_graph_import_to_discover(g["ambiguous_provider"]))
    case("F15_exclusive_conflict", lambda: M.validate_graph(g["exclusive_conflict"]), lambda: X.m_graph_import_to_discover(g["exclusive_conflict"]))
    case("F15_valid", lambda: M.validate_graph(g["valid"]), None)

    # Oracle independence: expectations must not import the candidate model.
    src = (HERE / "rpc_expected.py").read_text(encoding="utf-8")
    imports = [ln.strip() for ln in src.splitlines() if ln.strip().startswith(("import ", "from "))]
    check(not any("rpc_model" in ln for ln in imports), "rpc_expected imports do not include the candidate model")

    record = {
        "lane": "R-PC",
        "fixtures": ["F02", "F03", "F05", "F07", "F08", "F09", "F10", "F13", "F15"],
        "deferred": ["F17", "F18"],
        "cases": len(E.EXPECTED),
        "failures": failures,
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
