"""Independent expectations for the R-PC invariant fixtures (F02/F03/F05/F07–F10/F13/F15).

Literal, report-derived expected outcomes. This module imports nothing from the candidate model, so
agreement between the candidate and these values is evidence rather than a tautology. Each value is a
subset of fields the candidate result must match. F04/F06/F14/F16 are handled by the observed-boundary
gate (`check_rpc_observed.py`).
"""
from __future__ import annotations

# Third, literal anchor for the F01 canonical manifest (independent of candidate<->oracle agreement).
RPC_EXPECTED_MANIFEST_F01 = (
    b'{"profile":"rpc-canonical:v1","ranges":{"core":[0,8],"evidence":[18,23],'
    b'"skill":[8,18]},"total":23}'
)

EXPECTED: dict[str, dict] = {
    # F02 generated-summary identity
    "F02_identity": {"distinct": True},
    "F02_replay": {"outcome": "ok", "model_calls": 0},
    "F02_missing": {"outcome": "reproduction_input_unavailable"},
    "F02_collision": {"distinct": True},
    # F03 closure over a dependency graph
    "F03_valid": {"outcome": "ok", "dispatched": True, "mandatory": ["S02:v1", "S03:v1"],
                  "loaded": ["O1:v1", "S02:v1", "S03:v1"], "dropped": ["O2:v1", "O3:v1"]},
    "F03_missing": {"outcome": "closure_required_missing", "reason": "S03:v1", "dispatched": False},
    "F03_unfittable": {"outcome": "mandatory_context_unfittable", "dispatched": False},
    "F03_inactive_optional": {"outcome": "ok", "dispatched": True, "mandatory": ["S02:v1", "S03:v1"],
                              "loaded": ["S02:v1", "S03:v1"], "dropped": ["O1:v1"]},
    # F05 immediate revocation with coalesced preferences
    "F05_revoked": {"outcome": "refuse"},
    "F05_positive": {"outcome": "allow"},
    "F05_pending": {"effective": "p1", "pending": "p3", "revoked": True},
    # F07 procedure precedence
    "F07_hold": {"outcome": "hold"},
    "F07_override": {"outcome": "ok", "winner": "skillA", "conflict": True},
    "F07_override_unauthorized": {"outcome": "hold"},
    # F08 evidence conflict
    "F08": {"disagreement": True, "decision": None},
    # F09 coherent cut with ABA
    "F09_valid": {"outcome": "ok"},
    "F09_mixed": {"outcome": "composition_cut_unavailable"},
    "F09_aba_unstable": {"outcome": "composition_cut_unavailable", "aba_detected": True},
    "F09_aba_recovered": {"outcome": "ok", "aba_detected": True},
    "F09_incomplete": {"outcome": "composition_cut_unavailable"},
    # F10 one governor + compare-and-install
    "F10_first": {"outcome": "installed", "accepted": "A", "previous_preserved": True},
    "F10_late": {"outcome": "installed", "accepted": "A"},
    "F10_duplicate": {"outcome": "installed", "accepted": "A"},
    "F10_directive": {"outcome": "installed", "accepted": "A"},
    "F10_rebase_stale": {"outcome": "installed", "accepted": "A"},
    "F10_rebase_ok": {"outcome": "installed", "accepted": "B"},
    # F13 route/schema drift
    "F13_valid_current": {"outcome": "ok"},
    "F13_drift_readmit": {"outcome": "ok", "binding": {"descriptor": "d5", "schema": "u2"}},
    "F13_hold_unknown": {"outcome": "hold", "hidden_visibility": "unknown"},
    # F15 graph errors before load
    "F15_missing_dependency": {"reason": "missing_dependency", "imported": False},
    "F15_cycle": {"reason": "cycle", "imported": False},
    "F15_ambiguous_provider": {"reason": "ambiguous_provider", "imported": False},
    "F15_exclusive_conflict": {"reason": "exclusive_owner_conflict", "imported": False},
    "F15_valid": {"outcome": "ok"},
    "F15_invalid_selection": {"reason": "invalid_provider_selection", "imported": False},
    "F15_valid_selection": {"outcome": "ok"},
}
