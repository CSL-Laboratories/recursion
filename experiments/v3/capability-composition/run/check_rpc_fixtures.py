#!/usr/bin/env python3
"""Gate for the R-PC literal-specimen fixtures F01/F11/F12.

Provider-free and in-process. Runs the positive control, checks the candidate against the
independent oracle, proves the candidate is enumeration-order-independent, and confirms that the
planted mutants are detected. Writes ``results/rpc_results.json``. An implementer PASS is not Owner
acceptance.

    python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_fixtures.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import oracle  # noqa: E402
import reference_model as candidate  # noqa: E402
import rpc_expected as RE  # noqa: E402
from fixtures import rpc  # noqa: E402

RESULTS = HERE.parent / "results"

failures: list[str] = []


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        failures.append(message)


def to_sources(items: list[tuple[str, bytes, int]]) -> list[candidate.Source]:
    return [candidate.Source(sid, data, order) for sid, data, order in items]


def run() -> int:
    print("R-PC F01 — frozen reproduction")
    base = rpc.BASE
    sources = to_sources(base)
    out_a, ranges_a, spans_a = candidate.render(sources)
    out_b, ranges_b, _ = candidate.render(list(reversed(sources)))  # reversed source enumeration

    exp_out, exp_ranges = oracle.expected(base)
    check(out_a == exp_out == b"CONTROL\nPROCEDURE\nDATA\n", "positive control: exact 23 bytes")
    check(out_a == out_b, "enumeration order does not change output")
    check(ranges_a == ranges_b == exp_ranges, "ranges equal oracle under pinned profile")
    check(ranges_a == {"core": (0, 8), "skill": (8, 18), "evidence": (18, 23)}, "exact ranges")
    check(len(out_a) == 23, "output length 23")

    mutant_a = candidate.render_by_enumeration(sources)
    mutant_b = candidate.render_by_enumeration(list(reversed(sources)))
    check(mutant_a == exp_out, "planted enumeration mutant passes the base case")
    check(mutant_b != exp_out, "planted enumeration mutant is detected on reversed enumeration")

    check(spans_a == [("core", 0, 8), ("skill", 8, 18), ("evidence", 18, 23)], "F01 exact spans")
    check(candidate.verify_spans(spans_a, 23)["outcome"] == "ok", "F01 span coverage complete")
    cand_manifest = candidate.canonical_manifest(ranges_a, 23)
    check(cand_manifest == oracle.expected_manifest(base), "F01 canonical manifest equals independent oracle")
    check(cand_manifest == RE.RPC_EXPECTED_MANIFEST_F01, "F01 canonical manifest equals literal anchor")
    check(candidate.prefix_plan(out_a) == out_a[:18] and len(candidate.prefix_plan(out_a)) == 18,
          "F01 prefix plan is first 18 bytes")
    e_out, e_ranges, e_spans = candidate.render_empty_spans(sources)
    check(e_out == out_a and e_ranges == ranges_a, "empty-span mutant keeps bytes/ranges")
    check(candidate.verify_spans(e_spans, 23)["outcome"] == "refuse", "empty-span mutant detected")
    _, _, i_spans = candidate.render_invalid_coordinate(sources)
    check(candidate.verify_spans(i_spans, 23)["reason"] == "invalid_coordinate", "invalid-coordinate mutant detected")
    _, _, g_spans = candidate.render_gap_spans(sources)
    check(candidate.verify_spans(g_spans, 23)["reason"] == "uncovered_output_byte", "gap-span mutant detected")

    print("R-PC F11 — prefix continuity and required forfeiture")
    out_tail, ranges_tail, _ = candidate.render(to_sources(rpc.TAIL_CHANGE))
    exp_tail, _ = oracle.expected(rpc.TAIL_CHANGE)
    check(out_tail == exp_tail, "tail-change output equals oracle")
    check(out_tail[:18] == out_a[:18], "tail change preserves the first 18 bytes")
    check(len(out_tail) == 23 and ranges_tail["evidence"] == (18, 23), "tail change keeps offsets")

    out_core, ranges_core, _ = candidate.render(to_sources(rpc.CORE_CHANGE))
    check(len(out_core) == 24, "core change yields length 24")
    check(
        ranges_core == {"core": (0, 9), "skill": (9, 19), "evidence": (19, 24)},
        "core change shifts offsets to [9,19) [19,24)",
    )
    check(out_core[:18] != out_a[:18], "core change changes the prefix")

    print("R-PC F12 — span/role coverage, Unicode byte units")
    out_u, ranges_u, _ = candidate.render(to_sources(rpc.UNICODE))
    exp_u, exp_ranges_u = oracle.expected(rpc.UNICODE)
    check(out_u == exp_u and ranges_u == exp_ranges_u, "Unicode output equals oracle")
    check(len(out_u) == 25, "Unicode specimen is 25 bytes (2-byte char counted as 2)")
    check(ranges_u["note"] == (23, 25), "two-byte char occupies two byte units")
    mutant_ranges = oracle.expected_ranges_by_codepoint(rpc.UNICODE)
    check(
        mutant_ranges["note"] == (23, 24),
        "planted codepoint-counting mutant disagrees with byte ranges (detected)",
    )
    check(mutant_ranges != ranges_u, "codepoint mutant is detected")
    _, _, spans_u = candidate.render(to_sources(rpc.UNICODE))
    check(candidate.verify_spans(spans_u, len(out_u))["outcome"] == "ok", "F12 span coverage complete")

    print("Oracle independence")
    oracle_src = (HERE / "oracle.py").read_text(encoding="utf-8")
    oracle_imports = [
        line.strip()
        for line in oracle_src.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    check(
        not any("reference_model" in line for line in oracle_imports),
        "oracle imports do not include the candidate model",
    )

    record = {
        "lane": "R-PC",
        "suite": "rpc_fixtures",
        "fixtures": ["F01", "F11", "F12"],
        "provenance": {
            "reference_model.py": _sha(HERE / "reference_model.py"),
            "oracle.py": _sha(HERE / "oracle.py"),
            "gate": _sha(HERE / "check_rpc_fixtures.py"),
            "python": sys.version.split()[0],
        },
        "disposition": "PASS" if not failures else "FAIL",
        "failures": failures,
        "results": {
            "F01": {"output_hex": out_a.hex(), "ranges": {k: list(v) for k, v in ranges_a.items()}},
            "F11": {
                "tail_preserves_prefix": out_tail[:18] == out_a[:18],
                "core_len": len(out_core),
                "core_ranges": {k: list(v) for k, v in ranges_core.items()},
            },
            "F12": {"total": len(out_u), "note_range": list(ranges_u["note"])},
        },
        "scope": "bounded synthetic; provider-free; not Owner acceptance",
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "rpc_results.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    if failures:
        print(f"RPC_FIXTURE_FAIL ({len(failures)} failures)")
        return 1
    print("RPC_FIXTURE_PASS F01 F11 F12; oracle independent; mutants detected")
    return 0


if __name__ == "__main__":
    sys.exit(run())
