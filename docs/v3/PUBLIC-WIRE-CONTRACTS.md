---
title: Public wire contract vocabulary (identity/event/refusal/version) — DRAFT
status: draft-pending-rule-h-acceptance
issue: CSL-238
parent: CSL-206 (O2 shared foundation fragments)
last-updated: 2026-10-17
---

# Public wire contract vocabulary — DRAFT

**Status: DRAFT. Non-normative until a rule-H human receipt accepts its exact digest on
[CSL-238](https://linear.app/csl-laboratories/issue/CSL-238).** This document proposes the
shared public identity/event/refusal/version vocabulary owned by O2
([CSL-206](https://linear.app/csl-laboratories/issue/CSL-206)) and consumed by O3–O5. It is
pending the O2 contract freeze (milestone W2, 2026-10-23 to 2026-10-29) and rule-H
acceptance; nothing here is admitted product contract yet. It selects no transport,
language, store or framework.

## Acceptance and rule-H receipt

Acceptance of this draft into the product contract requires only a human-authored comment
on CSL-238 (receipt rule H per CSL-206), binding:

- the Linear comment ID and the human identity with scoped delegation;
- the exact digest of this document, the draft schema and the fixture manifest;
- the accepted scope, the selected and rejected options, rationale, conditions, date and
  a revisit trigger.

Labels, status changes, PR approval, merge, silence and passing tests do not qualify.
Until that receipt exists, every consumer must treat this vocabulary as a proposal.

## Scope

Public = the vocabulary a caller outside the kernel trust boundary needs to construct,
name, sequence, refuse and version the records the shared foundation fragments exchange.
O3–O5 specialize payloads and closed event alphabets on top of this surface; they do not
redefine these fields.

In scope:

- **Identity** — namespaced record identities, typed references, idempotency keys.
- **Event** — one envelope shape for every emitted wire record's metadata.
- **Refusal** — the outcome alphabet and machine-readable refusal object.
- **Version** — record-type versioning, negotiation and deprecation.

Out of scope (non-goals): transport and framing (HTTP/IPC/stdio/queue), authentication
mechanism, payload contracts of domain records, the closed per-domain event alphabets,
physical-durability claims, and any live provider/host effect. The vocabulary describes
documents; it does not grant authority — authority composition stays with O2's other
fragment.

## Conventions inherited from the M1 wire profiles

This draft reuses the encoding discipline of
[M1A](M1A-WIRE-PROFILE.md), [M1B](M1B-WIRE-PROFILE.md) and
[M1C](M1C-WIRE-PROFILE.md) rather than inventing a second style:

- Records are `Name@N` versioned objects; `N` is a positive integer major version.
- Every listed field is required unless marked optional; `additionalProperties=false`
  everywhere. Unknown fields, unknown `type` values and unknown `@N` versions are rejected
  before semantic evaluation.
- Sets are serialized as **sorted unique arrays** (ascending Unicode code point). Schema
  `uniqueItems` alone is insufficient; readers apply the declared sorted-set rule.
- Encoding profile is UTF-8 I-JSON: no floats, no lexical `-0`, no duplicate keys, no
  unpaired surrogates. Bounds below are conservative and may tighten on freeze.
- `Digest` fields carry `sha256:` + 64 lowercase hex over the RFC 8785 canonical form of
  the documented object, self-digest field omitted — matching M1C's self-describing
  prefix rather than M1A's bare hex.

## Scalar grammar

| Alias | Grammar | Bound |
|---|---|---|
| `PublicId` | `^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9._~-]{1,128}$` (namespace `:` local) | 3–162 bytes |
| `Digest` | `^sha256:[0-9a-f]{64}$` | fixed |
| `Token` | `^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$` | 1–96 bytes |
| `TypeVersion` | `^[A-Za-z][A-Za-z0-9._-]*@[1-9][0-9]{0,2}$` (e.g. `WireEventEnvelope@1`) | 3–132 bytes |
| `EventType` | `^[A-Z][A-Za-z0-9]*(\.[A-Z][A-Za-z0-9]*)+$` (e.g. `ArmRun.Admitted`) | 3–96 bytes |
| `ReasonCode` | one of the closed refusal-reason registry below | — |
| `UtcTimestamp` | canonical UTC `YYYY-MM-DDTHH:MM:SSZ`, real Gregorian date | fixed |
| `Seq` | JSON integer 0–9,007,199,254,740,991 | — |
| `PosInt` | JSON integer 1–2,147,483,647 | — |
| `OpaqueRef` | printable non-whitespace ASCII, never a secret | 1–192 bytes |

The `namespace` of a `PublicId` names its record family (`arm`, `evt`, `cmd`, `stream`,
`grant`, …); a namespace registry is an O2 freeze-time artifact, deferred here.

## Records

### `WireIdentityRef@1` — identity vocabulary

The typed reference one record uses to name another. Digest-addressed: the reference is
meaningless without `content_digest` agreement.

```text
WireIdentityRef@1 = {
  type:           const "WireIdentityRef@1",
  object_type:    Token,            // record family, e.g. "ArmRun", "GoalSpec"
  id:             PublicId,
  version:        PosInt,
  content_digest: Digest
}
```

- `id` is stable across storage moves and endpoint changes; `version` + `content_digest`
  pin the exact revision.
- Command idempotency uses the same `PublicId` grammar under the `cmd:` namespace; a
  canonically identical admitted command replays its prior outcome (`duplicate`); a
  conflicting reuse is answered `conflict` — the collision M1B reports as
  `duplicate-command-conflict`, surfaced here outcome-level rather than as a
  refusal reason.

### `WireEventEnvelope@1` — event vocabulary

Envelope metadata for every event a writer emits on a stream. Payload bytes are carried
by the typed record contract named in `schema_ref`; the envelope binds them by digest so
the same envelope survives payload relocation.

```text
WireEventEnvelope@1 = {
  type:           const "WireEventEnvelope@1",
  event_id:       PublicId,          // unique within the emitting stream epoch
  stream_id:      PublicId,
  sequence:       Seq,               // per-stream, contiguous, gap = refusal
  event_type:     EventType,         // "Family.Action", alphabet owned by payload domain
  schema_ref:     TypeVersion,       // payload contract, e.g. "ArmAdmittedPayload@1"
  payload_digest: Digest,            // D(payload) per the referenced contract
  emitted_at:     UtcTimestamp,      // OPTIONAL — informational only, never semantic
  causation_id:   PublicId           // OPTIONAL — causing command/event id
}
```

- `sequence` is the only ordering primitive; gaps refuse as `replay-gap-continuation`
  (same rule as M1B's broker trace). `emitted_at` is wall-clock metadata for observers
  and is excluded from identity, ordering and dedup decisions — consistent with v3's
  "never read semantics from a clock" rule — but it must still be a real Gregorian
  date, not merely digit-shaped.
- `event_id` uniqueness is scoped to the emitting stream's current epoch; an explicit
  epoch component is deferred to the O2 freeze (see open questions).
- `event_type` names the domain alphabet entry; the alphabets themselves are frozen by
  the O3–O5 owning documents, not by this file.

### `WireCommandOutcome@1` — refusal vocabulary

The receipt a command gets back. The outcome alphabet is closed:

```text
WireCommandOutcome@1 = {
  type:           const "WireCommandOutcome@1",
  command_id:     PublicId,          // the idempotency key presented
  command_digest: Digest,            // canonical digest of the command received
  outcome:        "accepted" | "duplicate" | "conflict" | "refused",
  refusal:        Refusal            // required iff outcome = "refused", forbidden otherwise
}

Refusal = {
  reason_code:         ReasonCode,
  refusal_class:       "permanent" | "retryable",
  retry_after_seconds: Seq           // OPTIONAL, only when refusal_class = "retryable", max 86400
}
```

Rules:

- **Refusal is not an error.** `refused` means the document was well-formed, decoded and
  authenticated, then not admitted. Decode failures (invalid UTF-8/I-JSON, schema
  mismatch, unknown `type`) never reach this record; they normalize to a closed
  decode/schema error per the M1 profiles' strict-decoder rule.
- `duplicate` = a command already admitted whose canonical digest equals the incoming
  `command_digest` (returns the prior outcome). Because digests are computed over
  canonical bytes, reordering object keys does not mint a new command — it replays.
  `conflict` = same `command_id`, different `command_digest`. `conflict` is permanent.
- Only `accepted` commands mint a dedup entry. A `refused` or `conflicted` command
  leaves no replay record, so an identical retry is evaluated against current
  admission state. `refusal_class` is caller guidance, not a receipt guarantee:
  `retryable` refusals are expected to clear with time alone (resync, pause
  lifted, budget refilled); `permanent` refusals must not be auto-retried
  expecting success — a changed verdict requires changed state (e.g. an
  `unknown-record` target appearing before the retry).
- `accepted` carries no refusal block; a `refusal` on any other outcome is malformed.

Closed `ReasonCode` registry (draft — extensions require a vocabulary revision):

| Code | Meaning | Default class |
|---|---|---|
| `stale-epoch` | epoch precondition no longer current | retryable after resync |
| `replay-gap-continuation` | sequence gap in an event stream | permanent |
| `authority-mismatch` | grant/authority digest does not match active | permanent |
| `capability-denied` | requested capability not granted | permanent |
| `policy-denied` | admitted shape violates active policy | permanent |
| `fence-violation` | fenced-writer boundary crossed | permanent |
| `precondition-failed` | stated compare-and-swap precondition unmet | retryable |
| `budget-exceeded` | declared budget/quota floor crossed | retryable |
| `paused` | an admitted PAUSE profile is operative | retryable |
| `unsupported-version` | `type`/`@N` not in the advertised `supported` set | permanent |
| `unknown-record` | referenced identity/digest resolves to nothing | permanent |

### `WireVersionAdvertisement@1` — version vocabulary

What a peer declares it can read before exchange.

```text
WireVersionAdvertisement@1 = {
  type:               const "WireVersionAdvertisement@1",
  vocabulary_version: PosInt,               // version of this vocabulary set as a whole
  supported:          sorted unique TypeVersion set,   // every record version accepted
  sunset:             sorted unique TypeVersion set,   // accepted but scheduled for removal
  preferred:          TypeVersion                       // OPTIONAL, must ∈ supported
}
```

Versioning rules:

1. **Byte-shape change ⇒ `@N` bump.** Because readers reject unknown fields, there is no
   additive in-place evolution: adding, removing, renaming or re-typing a field requires
   `Name@(N+1)`. Within one `Name@N` the shape is frozen.
2. **Advertise before send.** A writer emits only record versions in the peer's
   `supported` set; anything else is refused `unsupported-version` (permanent).
3. **Sunset is informational.** Entries in `sunset` remain valid; the flag schedules
   removal at the next `vocabulary_version` bump, giving consumers one full version
   cycle. `sunset` must be a subset of `supported` — a version cannot be scheduled for
   removal the peer never accepted.
4. **No downgrade inference.** `@N` and `@N+1` are distinct contracts; mechanical
   up/down-mapping, when it exists, is its own explicit adapter record — never implicit.

## Fixtures

Positive and adverse fixtures live under `tools/contract_fixtures/`:

- `schema/wire-vocab-draft.schema.json` — the JSON Schema Draft 2020-12 rendering of the
  four records above (`$defs` keyed `wire_identity_ref`, `wire_event_envelope`,
  `wire_command_outcome`, `wire_version_advertisement`), marked `x-status: draft`.
- `manifest.json` — the machine-readable corpus index: fixture id, path, target `$def`,
  kind (`positive`/`adverse`) and expected verdict (`accept`/`reject`).
- `positive/` — documents that must validate, including a refused-outcome receipt (a
  refusal is a first-class legal document, not a malformed one).
- `adverse/` — documents that must fail schema or declared semantic rules, covering every
  refusal surface above: unknown version, extra/missing fields, grammar violations,
  conditional-block violations, unknown reason codes, unsorted/duplicate sets.

`tools/test_contract_fixtures.py` runs the corpus provider-free under the Blacksmith
`linux` gate. Fixtures are data plus one draft schema — no transport, no host effects.

## Relation to researched options

The Parallel research brief (cited in the governing PR) recommends a CloudEvents-style
profile, RFC 9457 problem details and tolerant-reader additive evolution. This draft
deliberately diverges where the repository's frozen conventions pull the other way:

- **Envelope:** CloudEvents `specversion/id/source/type` maps onto
  `schema_ref/event_id/stream_id/event_type` here. The repo's strict-decoder convention
  (`additionalProperties=false`, closed alphabets) is kept instead of CloudEvents'
  extensible attribute model; a CloudEvents adapter remains a future mapping, not a merge.
- **Refusal:** RFC 9457 URI-typed problems are an HTTP carrier; this vocabulary keeps a
  closed kebab-case reason registry matching the existing `E-*`/kebab-code style, with
  refusal as a first-class outcome (the research's core recommendation), and forbids
  treating `refused` as an infrastructure failure.
- **Identity:** namespaced `PublicId` instead of bare UUIDv7/ULID — M1C already pins the
  `namespace:local` grammar; ordering comes from `sequence`, never from identifier sort
  or `emitted_at` (the research's ULID/UUIDv7 caveat).
- **Versioning:** the research's tolerant-reader additive rule cannot hold under
  strict decoders — every byte-shape change is a `Name@N` bump, per the protobuf
  "unknown fields" lesson the brief records.

Execution-level terminal outcomes (`succeeded`/`failed`/`cancelled`) are payload-domain
matters for O3–O5; `WireCommandOutcome@1` covers admission only — `accepted`,
`duplicate`, `conflict`, `refused`.

## Open questions for O2 freeze

- `PublicId` namespace registry ownership and collision rules.
- Whether `emitted_at` remains optional at freeze or is dropped outright.
- Whether event identity needs an explicit `stream_epoch` field so readers can
  distinguish a restarted emitter's `event_id`/`sequence` space from a duplicate
  without inspecting `stream_id` history.
- Payload-embedding versus digest-reference for `WireEventEnvelope` in-band transports.
- The `sunset`/`preferred` semantics if a third vocabulary family joins.

## Falsifiers

- Any O3–O5 document redefining a field owned here (registry violation).
- A refusal expressible only as an exception, not a `WireCommandOutcome@1` record.
- A wire change that needs an in-place field edit rather than a `Name@N` bump.
- A consumer needing wall-clock `emitted_at` for ordering or dedup (violates the
  logical-sequence rule).

## Provenance

Built from `docs/v3` (M1A/M1B/M1C wire profiles, STATE, EXECUTION-FABRIC, GLOSSARY), the
CSL-204 program-control layer and CSL-206 O2 scope, plus the Parallel deep-research brief
cited in the governing PR. This draft asserts no acceptance, no executed runtime evidence
and no transport qualification.
