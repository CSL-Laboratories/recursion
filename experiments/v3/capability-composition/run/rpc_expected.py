"""Independent expectations for the R-PC invariant fixtures (F02-F10, F13-F16).

Literal, report-derived expected outcomes. This module imports nothing from the candidate model, so
agreement between the candidate and these values is evidence rather than a tautology. Each value is
a subset of fields the candidate result must match.
"""
from __future__ import annotations

EXPECTED: dict[str, dict] = {
    "F02_identity": {"distinct": True},
    "F02_replay": {"outcome": "ok", "model_calls": 0},
    "F02_missing": {"outcome": "reproduction_input_unavailable"},
    "F03_case1": {"outcome": "ok", "dispatched": True, "loaded": ["O1", "S02", "S03"]},
    "F03_case2": {"outcome": "mandatory_context_unfittable", "dispatched": False},
    "F05": {"outcome": "refuse"},
    "F07_hold": {"outcome": "hold"},
    "F07_override": {"outcome": "ok", "winner": "skillA", "conflict": True},
    "F08": {"disagreement": True, "decision": None},
    "F09_mixed": {"outcome": "composition_cut_unavailable"},
    "F09_valid": {"outcome": "ok"},
    "F10_stale": {"outcome": "stale_reject", "installed": False},
    "F10_current": {"outcome": "install", "installed": True},
    "F13": {"outcome": "hold", "hidden_visibility": "unknown"},
    "F15_missing_dependency": {"reason": "missing_dependency", "imported": False},
    "F15_cycle": {"reason": "cycle", "imported": False},
    "F15_ambiguous_provider": {"reason": "ambiguous_provider", "imported": False},
    "F15_exclusive_conflict": {"reason": "exclusive_owner_conflict", "imported": False},
    "F15_valid": {"outcome": "ok"},
}
