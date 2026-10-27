# O2 responsibility and seam matrices

**Version:** `o2-matrices v0.1-draft`
**Status:** DRAFT under envelope G. Not accepted. See [O2-CONTRACT.md](O2-CONTRACT.md).
**Parent:** [CSL-206](https://linear.app/csl-laboratories/issue/CSL-206), owner plan `12dc4d83`.

The contract prose and these tables name the same owners. A mismatch is `O2E-004`. There is no JSON twin in this draft.

Closed owner set: `kernel-admission`, `canonical-writer`, `orchestrator`, `effect-dispatcher`.

Closed seam classification: `freeze-blocking` or `deferrable`. Anything else is `O2E-003`.

Closed seam status: `open`, `recorded-inherited`, `accepted-H`. Only `accepted-H` clears a freeze-blocking seam, and only when rule H binds the digest. `open` includes text that is written and still waiting for that receipt.

---

## 1. Responsibilities

Each row has one owner. Each row names at least one fixture from the contract manifest. Identity fields are the ones that row's owner must bind. A row with an empty identity cell is `O2E-004`.

| Id | Responsibility | Owner | Identity / epoch | Fixtures |
|---|---|---|---|---|
| `R-ADMIT` | Deterministic admission and reduction | `kernel-admission` | `command_id`, `fence_epoch`, `pause_epoch` | `O2F-P01`, `O2F-A02`, `O2F-A04`, `O2F-A10`, `O2F-A14` |
| `R-WRITE` | Canonical writing | `canonical-writer` | `writer_id`, `fence_epoch`, monotonic sequence | `O2F-P02`, `O2F-A02`, `O2F-A08` |
| `R-IDENT` | Identities and epochs | `kernel-admission` | `operation_id`, `attempt`, `correlation_id`, `fence_epoch`, `pause_epoch` | `O2F-P03`, `O2F-P10`, `O2F-A09`, `O2F-A12` |
| `R-ORCH` | Durable orchestration | `orchestrator` | re-admission `command_id` (proposed; this owner does not commit) | `O2F-P04`, `O2F-A09` |
| `R-RETRY` | Retry ownership | `orchestrator` | `attempt`, prior `operation_id` | `O2F-P05`, `O2F-A05` |
| `R-DISPATCH` | Effect dispatch | `effect-dispatcher` | committed `operation_id` only | `O2F-P06`, `O2F-A01`, `O2F-A07`, `O2F-A12` |
| `R-RECEIPT` | Receipts | `canonical-writer` | `receipt_id`, `command_id`, `operation_id` | `O2F-P07`, `O2F-A04`, `O2F-A07`, `O2F-A11` |
| `R-STOP` | Stop and revocation | `kernel-admission` | `pause_epoch`, D-022 mode | `O2F-P08`, `O2F-A06` |
| `R-REPLAY` | Replay and reconstruction | `kernel-admission` | event sequence, `previous_event_digest` | `O2F-P09`, `O2F-A03`, `O2F-A13` |

`R-ORCH` is orchestration of recovery against the committed log. Durability of the log stays with `R-WRITE`. The orchestrator is not a second canonical writer. That split is the whole point of giving the two rows different owners.

`R-IDENT` assigns values. `R-WRITE` is what makes an operation identity durable before `R-DISPATCH` may run. The values and the commit are different responsibilities, so they have different owners, and dispatch still requires the commit (`O2F-A12`).

---

## 2. Seams

| Id | Classification | Status | One-line reason |
|---|---|---|---|
| `S-STORE` | `deferrable` | `open` | A3/A7 in comment `a8988987` keep JSONL behind a store seam and leave FU-02 open; this contract selects no store. |
| `S-FRAMEWORK` | `deferrable` | `open` | Workflow-framework choice is a non-goal; the citations do not select one. |
| `S-LANGUAGE` | `deferrable` | `open` | D-037 and D-039 leave the product language unselected; CSL-240 owns that experiment. |
| `S-DURABILITY` | `freeze-blocking` | `open` | Exactly-once and physical-power-loss stay unqualified until Lucas disposes the stronger claim; JSON writes are not an outbox proof. |
| `S-FIXTURE-PLACE` | `freeze-blocking` | `open` | Fixture directory is a proposal (`docs/v3/fixtures/O2/`), not created and not accepted; D-042 still protects `scripts/tests/**/fixtures/**`. |
| `S-CSL12` | `deferrable` | `open` | CSL-12 and CSL-13 are Canceled; whether to reactivate CSL-12 is placement, not a second semantic owner. |
| `S-CI` | `deferrable` | `open` | This docs change does not add `ci:full`; Windows/macOS spend stays with Lucas. |
| `S-CITATION` | `deferrable` | `open` | Whether comment `12dc4d83`'s citation pass counts against the zero-run budget is accounting only. |
| `S-STREAM-EPOCH` | `freeze-blocking` | `open` | CSL-238 deferred `stream_epoch` to the O2 freeze, and this draft does not add the four-row IPC table from comment `b89068a9`. |
| `S-SEQUENCE` | `deferrable` | `open` | O1 and O6 may be reviewed with this draft; neither is treated as accepted, and this draft does not wait on O1's rule H. |
| `S-WORKPACKET` | `freeze-blocking` | `open` | A6 states the correction direction; the amendment digest is not bound by rule H, so the normative profile is not accepted. |
| `S-WIRE-BYTES` | `deferrable` | `open` | Byte grammar stays in CSL-238 / recursion#231; this contract points at that candidate and does not copy it. |
| `S-AUTHORITY-COMP` | `freeze-blocking` | `open` | Requested-versus-effective, non-widening and admitted change are stated here and still need H; CSL-239 has not replaced them. |
| `S-NATIVE-SPIKE` | `deferrable` | `open` | The spike contract is in the O2 contract and no spike is run; widening the native loop waits on a later measurement. |
| `S-TAKEOVER` | `deferrable` | `recorded-inherited` | D-020 already puts takeover behind the core fence and leaves consensus out of the default install. |
| `S-H-RECEIPT` | `freeze-blocking` | `open` | No human receipt binds this draft's digests. Merge, approval and green CI do not close this seam. |

Acceptance mode fails while any row with classification `freeze-blocking` has status `open`. On this draft those rows are `S-DURABILITY`, `S-FIXTURE-PLACE`, `S-STREAM-EPOCH`, `S-WORKPACKET`, `S-AUTHORITY-COMP` and `S-H-RECEIPT`.

---

## 3. State coverage

The five states in the contract each have a fixture that fails if they are collapsed, plus positives that keep them apart.

| State | Positive | Adverse if collapsed or skipped |
|---|---|---|
| Acknowledgment | `O2F-P08` | `O2F-A11` |
| Commit | `O2F-P02`, `O2F-P03` | `O2F-A11`, `O2F-A12` |
| Dispatch | `O2F-P06` | `O2F-A11`, `O2F-A12` |
| Termination | `O2F-P08` | `O2F-A06`, `O2F-A11` |
| External settlement | `O2F-P07` | `O2F-A07`, `O2F-A11` |
