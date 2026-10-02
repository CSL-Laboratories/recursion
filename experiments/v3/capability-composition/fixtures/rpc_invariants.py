"""R-PC invariant fixture inputs (F02-F10, F13-F16).

Synthetic, provider-free inputs transcribed from the R-PC report section 9 predicate table. These
are test inputs, not a selected product encoding. F17/F18 are intentionally absent (scripted
accounting/analysis, deferred — not provider-free behavioral evidence).
"""
from __future__ import annotations

# F02 — recorded summaries with equal generation settings but different actual outputs
SUMMARY_A = {"input": b"prompt-A", "output": b"summary-A"}
SUMMARY_B = {"input": b"prompt-A", "output": b"summary-B"}

# F03 — mandatory closure S02->S03 with optional distractors under two capacities
CLOSURE = {
    "required": ["S02", "S03"],
    "optional": ["O1", "O2", "O3"],
    "cost": {"S02": 4, "S03": 4, "O1": 3, "O2": 3, "O3": 3},
    "capacity_case1": 12,  # admits mandatory + one optional, drops two
    "capacity_case2": 6,   # below mandatory
}

# F05 — grant7 loaded at config p1; revoke8 committed; later preference p3 delivered
REVOCATION = {"grant": "grant7", "fence": 8, "revoke_fence": 8, "pref_later": "p3", "pref_target": "p1"}

# F07 — two conflicting exact-format requirements, permuted; explicit override names a winner
PRECEDENCE = {"reqs": [("skillA", "json"), ("skillB", "yaml")], "override": "skillA"}

# F08 — two versioned sources (one a compacted derivative) disagree
EVIDENCE = [{"ref": "src1:v1", "value": "x"}, {"ref": "derived:v2", "value": "y"}]

# F09 — config c2 requires schema u2 while the mixed snapshot supplies c2+u1
CUT = {
    "config_rev": "c2",
    "required_schema": "u2",
    "mixed_snapshot": {"c2": "u1"},
    "valid_snapshot": {"c2": "u2"},
}

# F10 — two successors of c1; a stale candidate and a duplicate epoch
SUCCESSOR = {"parent": "c1", "stale": {"parent": "c0", "epoch": 9}, "current": {"parent": "c1", "epoch": 2}}

# F14 — checkpoint resume: a compaction that omits the active skill must be rejected; a valid
# manifest restores references while rechecking current rights and preserving O1's liability.
RESTORE = {"valid_manifest_refs": ["skillX", "procY"], "active_skill": "skillZ"}

# F15 — four independently injected graph errors plus a valid acyclic graph
GRAPHS = {
    "missing_dependency": {"nodes": {"A": {"deps": ["B"]}, "C": {"deps": []}}, "providers": {}, "exclusive": {}},
    "cycle": {"nodes": {"A": {"deps": ["B"]}, "B": {"deps": ["A"]}}, "providers": {}, "exclusive": {}},
    "ambiguous_provider": {"nodes": {"A": {"deps": []}}, "providers": {"cap": ["p1", "p2"]}, "exclusive": {}, "selected": {}},
    "exclusive_conflict": {"nodes": {"A": {"deps": []}}, "providers": {}, "exclusive": {"res": ["owner1", "owner2"]}},
    "valid": {"nodes": {"A": {"deps": []}, "B": {"deps": ["A"]}}, "providers": {"cap": ["p1"]}, "exclusive": {"res": ["owner1"]}},
}

# F04 — observed disabled inertness (slice 2)
F04 = {"capability": "cap.skillQ", "source": "src.skillQ", "content": b"PROCEDURE\n"}

# F06 — authority laundering at a protected sink (slice 2)
F06 = {
    "import_envelope": {"subject": "imported:doc-7", "proof": "forged"},
    "owner_envelope": {"subject": "owner:grant7"},
    "protected_op": "op.protected.write",
    "artifact": b"useful\n",
}

# F14 — checkpoint resume with an observed rights ledger (slice 2)
F14 = {
    "valid_manifest_refs": ["skillX", "procY", "skillZ"],
    "omitting_refs": ["skillX", "procY"],
    "active_skill": "skillZ",
    "rights": {
        "skillX": {"active": True, "fence": 11},
        "procY": {"active": False, "fence": 12},
        "skillZ": {"active": True, "fence": 11},
    },
    "current_fence": 12,
}

# F16 — observer loss / distinct valid outputs / independent grading (slice 2)
F16 = {
    "valid_artifacts": [b"answer-A\n", b"answer-B\n"],
    "invalid_artifact": b"",
    "protected_op": "op.work",
    "authority_subject": "owner:grant7",
}
