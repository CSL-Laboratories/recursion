# core/recursion_core/canonical_form (CSL-718, F9, candidate)

CSL canonical form v1 (`csl-canonical/1`), a test-vector file format, and vectors for the
two Run@1 fields Lucas scoped for F9: `outcome` and `breaker_tripped`. This is candidate
code: no running core calls it yet, and nobody has reviewed or accepted it.

## Source

Builds on the Recursion-Cockpit contract proposed in
[csl-research #1100](https://github.com/CSL-Laboratories/csl-research/pull/1100) at head
`114b48be2b4f062974b983ab2106fc2d25d53c38`, path
`contracts/recursion-cockpit/v1.1-proposed/`. This directory does not copy that contract
tree. It takes:

- `canonical.py`: the canonical-form part of `csl_contract.py` (strict parse, RFC 8785
  serialisation, SHA-256), ported as is. The only additions are a BOM check on `str` input
  and `non_finite` for in-memory NaN and infinity.
- `run_fields.py`: the rules from `schemas/run.schema.json` and `common.schema.json`
  (`RunOutcome`, `StateOutcome`) that decide the two fields. The rest of Run@1 is checked
  for presence only. This module is not the contract validator.
- `vectors_build.py`: `run()` has the same shape as #1100's `tests/docs.py` fixture. The three
  `doc_run_outcome_*` vectors are copied byte for byte from `vectors/canonical-v1.json`.
  Their SHA-256 values match #1100's.

The vector file pins the SHA-256 of the three source files it came from, taken at
114b48be.

## Vector format (`csl-canonical-vectors/1`)

`vectors/run-two-fields-v1.json` is one JSON object. It is itself in the canonical
domain:

| key | meaning |
| --- | --- |
| `format`, `canonical_form` | `csl-canonical-vectors/1`, `csl-canonical/1` |
| `scope`, `source`, `source_path`, `source_file_sha256` | what the vectors cover and where they came from |
| `vectors[]` | one case each |

Each case has a `name`, either `input` (JSON text) or `input_utf8_hex` (raw bytes, used
for the BOM case), an `expect` and a `note`. Copied cases also carry `upstream`, the
#1100 vector name.

- `expect: "ok"`: the case also has `canonical`, `canonical_utf8_hex`, `sha256` (64
  lowercase hex) and `run_errors`. `run_errors` is the sorted list of two-field rule ids
  (see `run_fields.RULES`) that the document breaks as a Run@1. An empty list means the
  two fields are consistent. A non-empty list maps to the contract's `schema_invalid`.
  Canonical form and Run@1 validity are separate checks: a document can canonicalise and
  still be refused.
- `expect: "reject"`: the case has `reason`, a canonical-form refusal kind. These map to the
  contract's `canonical_form_invalid`.

The vectors cover:

- a null outcome while pending, running or stopping;
- every terminal outcome, with the state pairings and the stopped-by-reason pairings;
- `breaker_tripped` true and false, including true with a non-breaker outcome;
- the #1100 A3 rule: `outcome: route_breaker_tripped` is refused unless `breaker_tripped`
  is true;
- missing or mistyped fields;
- a `state` outside the closed Run@1 state set (wrong case, null, a number, an array,
  absent), which is refused as `state_not_in_enum` whatever the outcome says;
- key order, whitespace and escapes that leave the digest unchanged;
- duplicate keys, including one hidden by an escape (refused, never last-wins), NaN, the
  infinities, floats, integers past 2^53, a lone surrogate and a BOM.

I checked every accepted case once against #1100's own validator (`csl_contract.check`)
at 114b48be. All of them agreed. That check is not committed, because #1100's code lives in
csl-research. The six `run_state_*` cases were added later (red-team round 1 on #339) and
have not been run against #1100's validator.

## Tests

Run these from the repository root. They need Python 3.11+ and the standard library only.

    PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/canonical_form/tests -t core -v
    PYTHONPATH=core python3 -B -m recursion_core.canonical_form.vectors_build --check

- `test_vectors.py` rebuilds and checks every vector and tests how the two fields interact.
- `test_candidate_floor_1100.py` is a *candidate* floor for the two fields, taken from
  csl-research #1100 at 114b48be. It is not the RP0-v2 floor: #1100 is proposed and not
  accepted, and no accepted RP0-v2 test requires these rules (RP0-v2's accepted material
  names only Route Contract v0 conformance against the fake driver). It is pending
  acceptance through C5 / CSL-707, and whether #1100's two-field rules become the Q13 floor
  (PRODUCT.md 055cc7a5, CSLR-D-011, section 1 and section 4.3) is Lucas's decision. It
  checks five things against #1100's proposal:
  - both fields stay required;
  - the outcome enum equals #1100's closed 8-value `RunOutcome`;
  - `breaker_tripped` stays a strict boolean;
  - #1100's A3, F2 and unknown-is-never-success refusals hold;
  - the committed digests do not move unless `VECTOR_SET_SHA256` is edited in the same
    commit, and the vendored #1100 digests still match.

`tools/ci_gate.py` `TEST_GLOBS` does not include `core/` yet, so the gate reports these
tests as "neither run nor excluded" until the founder-only change on CSL-795 lands.
