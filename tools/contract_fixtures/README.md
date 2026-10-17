# Contract fixtures — public wire vocabulary (DRAFT)

Issue: [CSL-238](https://linear.app/csl-laboratories/issue/CSL-238) · Parent:
[CSL-206](https://linear.app/csl-laboratories/issue/CSL-206) (O2) · Spec:
[`docs/v3/PUBLIC-WIRE-CONTRACTS.md`](../../docs/v3/PUBLIC-WIRE-CONTRACTS.md)

**Status: draft pending rule-H acceptance.** These fixtures exercise a proposed
vocabulary; they are not admitted product contract until a rule-H human receipt
accepts the exact spec + schema + manifest digest on CSL-238. Provider-free data only — no
transport, no host effects.

## Layout

- `schema/wire-vocab-draft.schema.json` — JSON Schema Draft 2020-12 rendering of the
  four record contracts. `$defs` keyed `wire_identity_ref`, `wire_event_envelope`,
  `wire_command_outcome`, `wire_version_advertisement`.
- `positive/` — documents that must validate (`accept`).
- `adverse/` — documents that must fail schema validation or a declared semantic rule
  (`reject`).
- `schema/journal-sequence.schema.json` — `WireJournalSequence@1`: ordered
  `send`/`expect` steps for command journal scenarios.
- `sequences/` — journal-replay scenarios (`S-NN`): each step names a sent
  `command_id` + `command_digest` and the `WireCommandOutcome@1` the journal must
  produce. They pin replay semantics a single record cannot: refused/conflicted
  commands replay their journaled receipt verbatim, admitted commands answer
  `duplicate`, and a `conflict` verdict never rebinds the id.
- `manifest.json` — the corpus index and expected verdicts. Each entry pins the target
  `$def`; `note` records the refusal surface under test. `sequences[]` lists the
  journal scenarios run by the test driver.

## Verdicts

- `accept` — validates against the target `$def` and all declared semantic rules.
- `reject` — at least one schema error or one declared semantic-rule violation.

Checks beyond JSON Schema are declared in the manifest and annotated in the schema:

- `strict-decode` — I-JSON profile: no duplicate object keys, floats, lexical `-0`,
  or `NaN`/`Infinity` literals.
- `x-sorted-set` — array equals its sorted-unique self, ascending Unicode code point
  (`uniqueItems` alone cannot express the repo's sorted-set convention).
- `x-calendar-date` — timestamp is a real Gregorian date, not just digit-shaped.
- `x-preferred-in-supported` — `preferred` ∈ `supported`.
- `x-sunset-subset-of-supported` — every `sunset` entry ∈ `supported`.

## Adding fixtures

New fixtures are welcome — follow the `P-NN`/`A-NN` naming, add a manifest row with the
target `$def` and expected verdict, and keep every mutation reachable from a documented
rule. Never edit an existing fixture's expected verdict to silence a failure.
