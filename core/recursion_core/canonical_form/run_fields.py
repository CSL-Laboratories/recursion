"""The two Run@1 fields Lucas scoped for F9: ``outcome`` and ``breaker_tripped``.

Taken from the #1100 contract (csl-research PR #1100 at 114b48be,
``contracts/recursion-cockpit/v1.1-proposed/schemas/run.schema.json`` and the
``RunOutcome`` / ``StateOutcome`` definitions in ``schemas/common.schema.json``). Only the
rules that decide these two fields are here; the rest of Run@1 (ids, timestamps, the
``stop`` and ``termination`` objects) is checked for presence only. This is not the full
contract validator and does not replace it.

``run_field_errors(doc)`` returns a sorted list of rule ids; an empty list means the two
fields are consistent. Every rule id maps to the contract's ``schema_invalid`` refusal.
"""

from __future__ import annotations

from typing import Any

SOURCE = "CSL-Laboratories/csl-research#1100@114b48be2b4f062974b983ab2106fc2d25d53c38"
REFUSAL_CODE = "schema_invalid"

# Run@1 "required" at #1100@114b48be, in the schema's order.
RUN_REQUIRED = (
    "type", "run_id", "request_id", "state", "outcome", "state_seq", "created_at",
    "started_at", "ended_at", "stop", "termination", "breaker_tripped",
)
TWO_FIELDS = ("outcome", "breaker_tripped")

RUN_STATES = frozenset({
    "pending", "running", "stopping", "completed", "failed", "cancelled", "stopped",
    "timed_out", "limit_hit", "refused",
})
NON_TERMINAL_STATES = frozenset({"pending", "running", "stopping"})
TERMINAL_STATES = RUN_STATES - NON_TERMINAL_STATES
RUN_OUTCOMES = frozenset({
    "succeeded", "failed", "timed_out", "cancelled", "stopped", "route_breaker_tripped",
    "refused", "unknown",
})

# StateOutcome: each terminal state allows only the outcomes that describe it truthfully.
STATE_OUTCOMES: dict[str, frozenset[str]] = {
    "completed": frozenset({"succeeded"}),
    "failed": frozenset({"failed", "unknown"}),
    "timed_out": frozenset({"timed_out"}),
    "cancelled": frozenset({"cancelled"}),
    "stopped": frozenset({"stopped", "route_breaker_tripped", "unknown"}),
    "limit_hit": frozenset({"route_breaker_tripped"}),
    "refused": frozenset({"refused"}),
}
# A stopped run's outcome follows its core stop reason.
STOP_REASON_OUTCOME = {
    "breaker": "route_breaker_tripped",
    "orphan_sweep": "unknown",
    "kill_switch": "stopped",
    "daemon_shutdown": "stopped",
    "budget_cap": "stopped",
}

RULES = {
    "not_run": "document is not an object with type Run@1",
    "missing_required": "a Run@1 required field other than the two is absent",
    "outcome_required": "outcome is absent (it is required, null while not terminal)",
    "outcome_not_in_enum": "outcome is neither null nor a RunOutcome value",
    "outcome_not_null": "outcome is set while the state is not terminal",
    "outcome_null_terminal": "outcome is null while the state is terminal",
    "outcome_state_mismatch": "outcome does not describe the terminal state truthfully",
    "outcome_stop_reason_mismatch": "a stopped run's outcome does not follow its stop reason",
    "breaker_tripped_required": "breaker_tripped is absent",
    "breaker_tripped_not_boolean": "breaker_tripped is not a JSON boolean",
    "breaker_outcome_without_breaker": "outcome route_breaker_tripped while breaker_tripped is not true",
}


def run_field_errors(doc: Any) -> list[str]:
    """Rule ids the document breaks for the two fields (empty when consistent)."""
    if not isinstance(doc, dict) or doc.get("type") != "Run@1":
        return ["not_run"]
    errors: set[str] = set()
    if any(k not in doc for k in RUN_REQUIRED if k not in TWO_FIELDS):
        errors.add("missing_required")

    if "breaker_tripped" not in doc:
        errors.add("breaker_tripped_required")
    elif not isinstance(doc["breaker_tripped"], bool):
        errors.add("breaker_tripped_not_boolean")

    state = doc.get("state")
    if "outcome" not in doc:
        errors.add("outcome_required")
        return sorted(errors)
    outcome = doc["outcome"]
    if outcome is not None and (not isinstance(outcome, str) or outcome not in RUN_OUTCOMES):
        errors.add("outcome_not_in_enum")
        return sorted(errors)

    if state in NON_TERMINAL_STATES and outcome is not None:
        errors.add("outcome_not_null")
    elif state in TERMINAL_STATES:
        if outcome is None:
            errors.add("outcome_null_terminal")
        elif outcome not in STATE_OUTCOMES[state]:
            errors.add("outcome_state_mismatch")
        elif state == "stopped":
            stop = doc.get("stop")
            reason = stop.get("reason") if isinstance(stop, dict) else None
            want = STOP_REASON_OUTCOME.get(reason) if isinstance(reason, str) else None
            if want is not None and outcome != want:
                errors.add("outcome_stop_reason_mismatch")

    # #1100 A3 note: route_breaker_tripped is refused unless breaker_tripped is true;
    # breaker_tripped true does not force that outcome.
    if outcome == "route_breaker_tripped" and doc.get("breaker_tripped") is not True:
        errors.add("breaker_outcome_without_breaker")
    return sorted(errors)
