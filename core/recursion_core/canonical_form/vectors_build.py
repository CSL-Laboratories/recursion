"""Build or check ``vectors/run-two-fields-v1.json`` (CSL-718, F9).

From the repository root (standard library only):

    PYTHONPATH=core python3 -B -m recursion_core.canonical_form.vectors_build --check
    PYTHONPATH=core python3 -B -m recursion_core.canonical_form.vectors_build --write

Each case is JSON text. An accepted case records the canonical text, its UTF-8 hex, the
SHA-256 and the two-field rule ids it breaks as a Run@1 (``run_errors``; empty means the
two fields are consistent). A refused case records the canonical-form refusal kind.
Cases marked ``upstream`` are copied byte for byte from #1100's ``canonical-v1.json``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from recursion_core.canonical_form.canonical import CANONICAL_FORM, CanonicalError, canonical_bytes, digest, loads_strict
from recursion_core.canonical_form.run_fields import SOURCE, run_field_errors

HERE = os.path.dirname(os.path.abspath(__file__))
VECTORS = os.path.join(HERE, "vectors", "run-two-fields-v1.json")
FORMAT = "csl-canonical-vectors/1"

T0 = "2026-11-07T20:00:00Z"
T1 = "2026-11-07T20:00:05.250Z"
T2 = "2026-11-07T20:01:00Z"
TERMINATION = {"tree_empty": True, "observer": "cgroup_events", "exit_code": 0, "signal": None, "observed_at": T2}
OUTCOME = {"completed": "succeeded", "failed": "failed", "timed_out": "timed_out", "cancelled": "cancelled",
           "stopped": "stopped", "limit_hit": "route_breaker_tripped", "refused": "refused"}
STOP_OUTCOME = {"breaker": "route_breaker_tripped", "orphan_sweep": "unknown"}

# Byte for byte from #1100@114b48be vectors/canonical-v1.json ("doc_run_outcome_breaker").
UPSTREAM_RUN = (
    '{"type":"Run@1","breaker_tripped":true,"run_id":"run-1","request_id":"req-1","state":"limit_hit",'
    '"outcome":"route_breaker_tripped","state_seq":2,"created_at":"2026-11-07T20:00:00Z",'
    '"started_at":"2026-11-07T20:00:00Z","ended_at":"2026-11-07T20:01:00Z","stop":null,'
    '"termination":{"tree_empty":true,"observer":"cgroup_events","exit_code":0,"signal":null,'
    '"observed_at":"2026-11-07T20:01:00Z"}}'
)
# SHA-256 values #1100@114b48be records for its three Run@1 vectors.
UPSTREAM_SHA256 = {
    "doc_run_outcome_breaker": "e46ebf300f8bd1616631c76123342a4f569f8634382e610b6e6e59472d99bc0d",
    "doc_run_outcome_breaker_not_tripped": "f13174fb187e6cb6e04bf0331b37eea0a2f1fb63f40c04122184264588e92237",
}


def run(state: str = "running", stop_reason: str = "kill_switch", **override: Any) -> dict[str, Any]:
    """A Run@1 in ``state`` shaped like #1100's ``tests/docs.py`` ``run()`` fixture."""
    doc: dict[str, Any] = {
        "type": "Run@1", "breaker_tripped": stop_reason == "breaker" or state == "limit_hit",
        "run_id": "run-1", "request_id": "req-1", "state": state, "outcome": OUTCOME.get(state),
        "state_seq": 1, "created_at": T0, "started_at": T0, "ended_at": None, "stop": None,
        "termination": None,
    }
    if state == "pending":
        doc["started_at"] = None
    if state == "stopping":
        doc["stop"] = {"requested_by": "caller", "reason": "caller_stop", "requested_at": T1, "confirmed_at": None}
    if state in ("completed", "failed", "cancelled", "stopped", "timed_out", "limit_hit"):
        doc["ended_at"] = T2
        doc["termination"] = dict(TERMINATION)
    if state == "cancelled":
        doc["stop"] = {"requested_by": "caller", "reason": "caller_stop", "requested_at": T1, "confirmed_at": T2}
    if state == "stopped":
        doc["stop"] = {"requested_by": "core", "reason": stop_reason, "requested_at": T1, "confirmed_at": T2}
        doc["outcome"] = STOP_OUTCOME.get(stop_reason, "stopped")
    for key, value in override.items():
        if value is _DROP:
            del doc[key]
        else:
            doc[key] = value
    return doc


_DROP = object()


def text(doc: dict[str, Any]) -> str:
    """Compact JSON in insertion order (deliberately not canonical: the builder sorts)."""
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def _swap(old: str, new: str, base: str = UPSTREAM_RUN) -> str:
    assert base.count(old) == 1, old
    return base.replace(old, new)


# (name, JSON text, note[, upstream name])
CASES: list[tuple[str, ...]] = [
    # --- copied from #1100@114b48be
    ("doc_run_outcome_breaker", UPSTREAM_RUN,
     "terminal limit_hit Run@1, outcome route_breaker_tripped, breaker tripped", "doc_run_outcome_breaker"),
    ("doc_run_outcome_breaker_not_tripped", _swap('"breaker_tripped":true', '"breaker_tripped":false'),
     "canonical form is fine; the Run@1 is refused (#1100 A3 note: route_breaker_tripped needs breaker_tripped"
     " true)", "doc_run_outcome_breaker_not_tripped"),
    ("doc_run_outcome_duplicate", '{"type":"Run@1","state":"failed","outcome":"failed","outcome":"succeeded"}',
     "a later outcome cannot overwrite an earlier one: refused, never last-wins", "doc_run_outcome_duplicate"),
    # --- null outcome while not terminal
    ("run_pending_null_outcome", text(run("pending")), "pending: outcome null, breaker untripped"),
    ("run_running_null_outcome", text(run("running")), "running: outcome null, breaker untripped"),
    ("run_running_breaker_tripped", text(run("running", breaker_tripped=True)),
     "running with the breaker tripped: outcome stays null (the breaker is a snapshot, not an outcome)"),
    ("run_stopping_null_outcome", text(run("stopping")), "stopping: outcome null until the stop is confirmed"),
    ("run_running_outcome_set", text(run("running", outcome="succeeded")),
     "running with an outcome: refused, outcome is null exactly while not terminal"),
    ("run_running_breaker_outcome_early", text(run("running", breaker_tripped=True, outcome="route_breaker_tripped")),
     "running with outcome route_breaker_tripped: refused even with the breaker tripped"),
    # --- terminal outcomes
    ("run_completed_succeeded", text(run("completed")), "completed is the only state that is succeeded"),
    ("run_completed_succeeded_breaker_tripped", text(run("completed", breaker_tripped=True)),
     "breaker_tripped true does not force the outcome"),
    ("run_failed_failed", text(run("failed")), "failed: outcome failed"),
    ("run_failed_unknown_breaker_tripped", text(run("failed", outcome="unknown", breaker_tripped=True)),
     "failed: unknown allowed; breaker true does not make it route_breaker_tripped"),
    ("run_timed_out", text(run("timed_out")), "own timeout: only timed_out (F2: never a fault or breaker trip)"),
    ("run_timed_out_as_breaker", text(run("timed_out", outcome="route_breaker_tripped", breaker_tripped=True)),
     "own timeout claiming the breaker: refused"),
    ("run_cancelled", text(run("cancelled")), "caller stop: cancelled"),
    ("run_stopped_kill_switch", text(run("stopped", stop_reason="kill_switch")), "core stop: stopped"),
    ("run_stopped_breaker", text(run("stopped", stop_reason="breaker")),
     "core stop for the breaker: route_breaker_tripped with breaker_tripped true"),
    ("run_stopped_breaker_not_tripped", text(run("stopped", stop_reason="breaker", breaker_tripped=False)),
     "breaker stop reporting the breaker untripped: refused (A3 note)"),
    ("run_stopped_breaker_as_stopped", text(run("stopped", stop_reason="breaker", outcome="stopped")),
     "breaker stop must say route_breaker_tripped, not stopped"),
    ("run_stopped_kill_switch_as_breaker",
     text(run("stopped", stop_reason="kill_switch", outcome="route_breaker_tripped", breaker_tripped=True)),
     "kill switch stop cannot claim the breaker ended it"),
    ("run_stopped_orphan_sweep", text(run("stopped", stop_reason="orphan_sweep")),
     "orphan sweep: unknown (never success)"),
    ("run_limit_hit_breaker_tripped", text(run("limit_hit")),
     "typed plan limit: route_breaker_tripped, breaker tripped"),
    ("run_refused", text(run("refused")), "refused: outcome refused"),
    ("run_completed_unknown", text(run("completed", outcome="unknown")), "unknown never pretends success"),
    ("run_completed_null_outcome", text(run("completed", outcome=None)), "terminal with null outcome: refused"),
    # --- the two fields absent or mistyped
    ("run_outcome_absent", text(run("running", outcome=_DROP)), "outcome is required even while null"),
    ("run_breaker_tripped_absent", text(run("running", breaker_tripped=_DROP)), "breaker_tripped is required"),
    ("run_both_fields_absent", text(run("completed", outcome=_DROP, breaker_tripped=_DROP)),
     "both fields absent"),
    ("run_breaker_tripped_null", text(run("running", breaker_tripped=None)), "null is not a boolean"),
    ("run_breaker_tripped_int", text(run("running", breaker_tripped=1)),
     "1 is not true: canonical text keeps 1 and true distinct"),
    ("run_breaker_tripped_string", text(run("running", breaker_tripped="true")), "a string is not a boolean"),
    ("run_outcome_not_in_enum", text(run("completed", outcome="Succeeded")), "closed enum, case-sensitive"),
    ("run_outcome_state_name", text(run("limit_hit", outcome="limit_hit")), "a state name is not an outcome"),
    # --- key order and whitespace do not change the digest
    ("doc_run_outcome_breaker_reordered",
     "{\n  \"termination\": {\"signal\": null, \"observed_at\": \"2026-11-07T20:01:00Z\", \"exit_code\": 0,"
     " \"observer\": \"cgroup_events\", \"tree_empty\": true},\n  \"stop\": null, \"state_seq\": 2,"
     " \"state\": \"limit_hit\", \"started_at\": \"2026-11-07T20:00:00Z\", \"run_id\": \"run-1\",\n"
     "  \"request_id\": \"req-1\", \"outcome\": \"route_breaker_tripped\", \"ended_at\": \"2026-11-07T20:01:00Z\","
     " \"created_at\": \"2026-11-07T20:00:00Z\", \"breaker_tripped\": true, \"type\": \"Run@1\"\n}\n",
     "same Run@1 as doc_run_outcome_breaker with keys reversed and whitespace: same digest"),
    ("doc_run_outcome_escaped_key", _swap('"outcome":"route_breaker_tripped"', '"\\u006futcome":"route_\\u0062reaker_tripped"'),
     "escapes in the key and value unescape to the same Run@1: same digest as doc_run_outcome_breaker"),
    # --- canonical-form refusals that hit the two fields
    ("dup_breaker_tripped", _swap('"breaker_tripped":true', '"breaker_tripped":false,"breaker_tripped":true'),
     "duplicate breaker_tripped: refused, never last-wins (false then true would otherwise pass A3)"),
    ("dup_outcome_via_escape", _swap('"outcome":"route_breaker_tripped"',
                                     '"outcome":"failed","\\u006futcome":"route_breaker_tripped"'),
     "duplicate outcome compared after unescaping"),
    ("dup_outcome_nested_termination", _swap('"tree_empty":true', '"tree_empty":true,"tree_empty":false'),
     "duplicate inside the enclosing Run@1's termination: the whole document is refused"),
    ("breaker_tripped_nan", _swap('"breaker_tripped":true', '"breaker_tripped":NaN'), "NaN is refused"),
    ("breaker_tripped_infinity", _swap('"breaker_tripped":true', '"breaker_tripped":Infinity'), "Infinity is refused"),
    ("outcome_negative_infinity", _swap('"outcome":"route_breaker_tripped"', '"outcome":-Infinity'),
     "-Infinity is refused"),
    ("breaker_tripped_float_one", _swap('"breaker_tripped":true', '"breaker_tripped":1.0'),
     "an integral float is still a float"),
    ("state_seq_exponent", _swap('"state_seq":2', '"state_seq":2e0'), "exponent form refused even when integral"),
    ("state_seq_past_2_53", _swap('"state_seq":2', '"state_seq":9007199254740992'), "past 2^53-1: refused, never rounded"),
    ("outcome_lone_surrogate", _swap('"outcome":"route_breaker_tripped"', '"outcome":"\\ud800"'),
     "a lone surrogate is not a Unicode scalar value"),
    ("outcome_bare_true", _swap('"outcome":"route_breaker_tripped"', '"outcome":True'), "Python-style True is not JSON"),
]

BOM_CASE = ("run_bom", b"\xef\xbb\xbf" + UPSTREAM_RUN.encode("utf-8"), "a byte order mark is refused")


def build_one(name: str, data: str | bytes, note: str, upstream: str | None = None) -> dict[str, Any]:
    vec: dict[str, Any] = {"name": name}
    if isinstance(data, bytes):
        vec["input_utf8_hex"] = data.hex()
    else:
        vec["input"] = data
    try:
        value = loads_strict(data)
    except CanonicalError as e:
        vec.update(expect="reject", reason=e.kind)
    else:
        canon = canonical_bytes(value)
        vec.update(expect="ok", canonical=canon.decode("utf-8"), canonical_utf8_hex=canon.hex(),
                   sha256=digest(value), run_errors=run_field_errors(value))
    vec["note"] = note
    if upstream:
        vec["upstream"] = upstream
    return vec


def build() -> dict[str, Any]:
    vectors = [build_one(*case) for case in CASES]
    vectors.append(build_one(*BOM_CASE))
    return {
        "format": FORMAT,
        "canonical_form": CANONICAL_FORM,
        "scope": "Run@1.outcome and Run@1.breaker_tripped only (Lucas: two fields)",
        "source": SOURCE,
        "source_path": "contracts/recursion-cockpit/v1.1-proposed",
        "source_file_sha256": {
            "schemas/run.schema.json": "ce8abb5235614691c11a849add2755266e74ee83b21d887fa86740e3cf5e99d0",
            "schemas/common.schema.json": "efd6b76a7a86da13e9e6e7621a5640da5d9b7809f47f3dbc87c1a3e434a01087",
            "vectors/canonical-v1.json": "7af8e2b2dd7860b5f399e27e1e0e361e912a3cf023403671bbea8bb068d620b0",
        },
        "vectors": vectors,
    }


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str]) -> int:
    if argv not in (["--check"], ["--write"]):
        print(__doc__, file=sys.stderr)
        return 2
    want = render(build())
    if argv == ["--write"]:
        with open(VECTORS, "w", encoding="utf-8", newline="\n") as f:
            f.write(want)
        return 0
    with open(VECTORS, encoding="utf-8") as f:
        have = f.read()
    if have != want:
        print(f"{VECTORS} is stale; rebuild with --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
