# Contract fixtures — public wire vocabulary (DRAFT)

Issue: [CSL-238](https://linear.app/csl-laboratories/issue/CSL-238) · Parent:
[CSL-206](https://linear.app/csl-laboratories/issue/CSL-206) (O2) · Spec:
[`docs/v3/PUBLIC-WIRE-CONTRACTS.md`](../../docs/v3/PUBLIC-WIRE-CONTRACTS.md)

**Status: draft pending rule-H acceptance.** These fixtures exercise a proposed
vocabulary; they are not admitted product contract until a rule-H human receipt
accepts the exact spec + manifest digest on CSL-238. Provider-free data only — no
transport, no host effects.

## Layout

- `schema/wire-vocab-draft.schema.json` — JSON Schema Draft 2020-12 rendering of the
  four record contracts. `$defs` keyed `wire_identity_ref`, `wire_event_envelope`,
  `wire_command_outcome`, `wire_version_advertisement`.
- `positive/` — documents that must validate (`accept`).
- `adverse/` — documents that must fail schema validation or a declared semantic rule
  (`reject`).
- `manifest.json` — the corpus index and expected verdicts. Each entry pins the target
  `$def`; `note` records the refusal surface under test.

## Verdicts

- `accept` — validates against the target `$def` and all declared semantic rules.
- `reject` — at least one schema error or one declared semantic-rule violation.

Semantic rules beyond JSON Schema are declared in the manifest and annotated in the
schema: `x-sorted-set` (array equals its sorted-unique self, ascending Unicode code
point) and `x-preferred-in-supported` (`preferred` ∈ `supported`). They exist because
`uniqueItems` alone cannot express the repo's sorted-set convention.

## Adding fixtures

New fixtures are welcome — follow the `P-NN`/`A-NN` naming, add a manifest row with the
target `$def` and expected verdict, and keep every mutation reachable from a documented
rule. Never edit an existing fixture's expected verdict to silence a failure.
