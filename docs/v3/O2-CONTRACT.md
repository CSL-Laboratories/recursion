# O2 native-engine responsibility contract

**Version:** `o2-contract v0.1-draft`
**Status:** DRAFT under envelope G. Nothing in this file is accepted.
**Parent issue:** [CSL-206](https://linear.app/csl-laboratories/issue/CSL-206) (outcome O2, child of [CSL-45](https://linear.app/csl-laboratories/issue/CSL-45))
**Owner plan:** Linear comment `12dc4d83` (2026-10-17)
**A6 record:** Linear comment `a8988987` (2026-10-16), an attributed relay, not a rule-H receipt for this draft
**Base commit these citations were read against:** `7862cab9fbcaf450048a3b827ba3c5f888285b1b`
**Pattern:** [recursion#279](https://github.com/CSL-Laboratories/recursion/pull/279) (CSL-205 / O1), docs only
**Governing documents:** [CONTROL_PLANE.md](../../CONTROL_PLANE.md), [ANCHOR.md](../../ANCHOR.md), [AGENTS.md](../../AGENTS.md)

The W2 freeze date named on CSL-206 was 2026-10-23. That date has passed. This draft does not set a replacement date, and the slip is not acceptance.

---

## 1. How to read this draft

Envelope **G** applies. The gate in §6 is a future provider-free verification interface. It is not an existing command, and this pull request does not implement it. A missing executable gate means this candidate is not ready. It is not permission to write the checker.

Rule **H** applies. Only a human-authored Linear comment can accept this contract. The comment must bind its own id, the human's identity and any scoped delegation, the exact digest of this contract and of [O2-MATRICES.md](O2-MATRICES.md), [O2-WORKPACKET-AMENDMENT.md](O2-WORKPACKET-AMENDMENT.md) and [O2-DECISIONS-QUEUE.md](O2-DECISIONS-QUEUE.md), the scope, the selected and rejected options, the rationale, the conditions, the date and the revisit trigger. Pull-request approval, merge, labels, issue status, silence and a green check do not qualify. An agent may draft that comment. This draft does not author it. Fixture text cannot satisfy H.

Merging this pull request does not accept the contract, does not freeze O2, and does not admit an implementation child.

No sentence here selects a product language, store, workflow framework, transport, sandbox or credential implementation. [D-017](../../state/DECISIONS.md#d-017--recursion-v3-is-greenfield-v1v2-are-retired-not-migrated) still has status OPEN in `state/DECISIONS.md`. `CONTROL_PLANE.md` and `ANCHOR.md` independently treat v1/v2 as retired. This draft does not close D-017.

Companion files:

| File | Role |
|---|---|
| [O2-MATRICES.md](O2-MATRICES.md) | One owner per responsibility. Every seam is `freeze-blocking` or `deferrable`. |
| [O2-WORKPACKET-AMENDMENT.md](O2-WORKPACKET-AMENDMENT.md) | A6 versioning of the two WorkPacket discrepancies. Historical bytes stay historical. |
| [O2-DECISIONS-QUEUE.md](O2-DECISIONS-QUEUE.md) | Open founder questions and the evidence pins. |

There is no `O2-RESPONSIBILITY-MATRIX.json` in this draft. A second machine copy would be able to drift from the markdown before a checker exists. The checker child may add a JSON twin after H, generated from these tables, not beside them as a rival source.

---

## 2. Scope

This contract is the definitions artifact named in CSL-206 and in owner plan `12dc4d83`. It assigns one semantic owner to each responsibility the issue names, separates acknowledgment, commit, dispatch, termination and external settlement, and states the native-loop, persistent-assistant and compaction boundaries against the kernel. It specifies the gate. It does not run the gate.

O2 owns the shared foundation fragments below. O3 ([CSL-207](https://linear.app/csl-laboratories/issue/CSL-207)), O4 ([CSL-208](https://linear.app/csl-laboratories/issue/CSL-208)) and O5 ([CSL-209](https://linear.app/csl-laboratories/issue/CSL-209)) specialize those fragments. They do not redefine them. O5 renders the transactions. O1 ([CSL-205](https://linear.app/csl-laboratories/issue/CSL-205)) and O6 ([CSL-210](https://linear.app/csl-laboratories/issue/CSL-210)) constrain a later freeze and can be reviewed with this draft. This draft does not treat either as accepted. See DQ-09.

Placement of the work item is DQ-01. The text lives on CSL-206. [CSL-12](https://linear.app/csl-laboratories/issue/CSL-12) and [CSL-13](https://linear.app/csl-laboratories/issue/CSL-13) were both Canceled when this draft was written (2026-10-25). That observation is not a founder disposition of the cancellation.

### 2.1 Shared foundation fragments

**Public identity, event, refusal and version vocabulary.** O2 owns the semantic vocabulary. The byte draft is [recursion#231](https://github.com/CSL-Laboratories/recursion/pull/231) / [CSL-238](https://linear.app/csl-laboratories/issue/CSL-238), file `docs/v3/PUBLIC-WIRE-CONTRACTS.md`, head `7932749366761fb2d831246c7c25777f7c370adf`, sha256 `bc5b6a7d00c9fabf7057b4eca9b97467cd5ced8b867768d18d5ed17183698d50` (18185 bytes). That file is not on `master` at the base commit above. The pin is candidate-only. This contract does not copy it, does not vendor its schemas, and does not edit its fixtures.

The #231 description names four records: `WireIdentityRef@1`, `WireEventEnvelope@1`, `WireCommandOutcome@1`, `WireVersionAdvertisement@1`. CSL-238 comment `3d0f4e59` records that `stream_epoch` was left as an open question deferred to the O2 freeze. Comment `b89068a9` asks for a four-row map of version negotiation, snapshot materialization, cursor rollover and ACK/release, and says to confirm that table is not already present before writing another one. This draft does not add that table. The seam is S-STREAM-EPOCH in the matrices and DQ-08 in the queue.

**Configuration, capability and authority composition.** O2 owns these obligations:

1. A requested value and an effective value are different fields. Neither is inferred from the other.
2. An effective grant is the intersection of the installation maximum, the parent grant and the requested grant. A lower layer cannot widen effect keys, paths, credentials, spend or control-plane scope. This restates [SECURITY.md](SECURITY.md) (lower scopes narrow; widening needs an explicit owner action) and [D-021](../../state/DECISIONS.md#d-021--delegation-over-credential-intermediation) / [D-029](../../state/DECISIONS.md#d-029--operator-provisioned-trusted-launch-boundary-strong-credential-non-intermediation). It does not select a policy engine.
3. A change of effective authority is an admitted command with a committed receipt. Model text, a cache, an import and a provider session do not admit it.
4. Stale epochs are rejected. [SECURITY.md](SECURITY.md) already says the kernel rejects stale epochs. [D-020](../../state/DECISIONS.md#d-020--availability-contract-automatic-node-takeover-required-core-is-single-node-closes-g-06) puts the monotonic fence in the core.

[CSL-239](https://linear.app/csl-laboratories/issue/CSL-239) is the child that writes the composition spec after this draft. This file states the obligations. It does not write CSL-239's spec. Seam S-AUTHORITY-COMP stays open until H binds these sentences or a later revision.

---

## 3. Non-goals

Taken from the CSL-206 body and from owner plan `12dc4d83`. They are exclusions, not a durability level chosen by this draft.

- No production-engine rewrite.
- No language, store, workflow-framework or transport selection. [D-037](../../state/DECISIONS.md#d-037--conformance-fixtures-authorized-python-as-disposable-test-harness-only-o-02) keeps Python as a disposable harness. [D-039](../../state/DECISIONS.md#d-039--accept-the-oslanguage-expansion-as-advisory-plan-a-language-agnostic-architecture-o-04) keeps the architecture language-agnostic. [CSL-240](https://linear.app/csl-laboratories/issue/CSL-240) owns the engine/runtime experiment. FU-01 stays open.
- No exactly-once claim. [STATE.md](STATE.md) already says distributed side effects cannot be assumed exactly once.
- No physical-power-loss claim. [D-028](../../state/DECISIONS.md#d-028--platform-floors-closes-g-11) separates an abrupt VM reset at a commit boundary from physical platter interruption, and it leaves profiles Unqualified until traces pass. This draft does not claim either result.
- Current run-state JSON writes are not evidence of a transactional event or outbox commit.
- No v2 migration and no second canonical authority. [OR-019](DECISION-REGISTER.md) keeps canonical identity, goals, authority, budgets, PAUSE, effects, evidence and publication with the versioned Recursion release.
- No live execution, spend, deploy or merge.
- No checker, no test file, and no fixture bytes on disk in this change. The proposed paths are named in §6. They are not created here.
- No rewrite of `scripts/v3/planning/replay/kernel.py`. The historical loci are cited in the amendment.
- No native-loop spike run. §5 is the spike contract only.
- This session started no new paid research run. Citations in the decisions queue are support for decisions already recorded. They are not requirements. See DQ-05.

---

## 4. Inherited decisions

Repository decisions below are inherited provenance. They are not fresh rule-H receipts for this draft. A6 is an attributed relay of a confirmation Lucas authorized to be recorded. Comment `a8988987` says it does not accept an uncreated future contract digest. This draft is that kind of later document, so A6 directs the amendment and does not accept this file.

| ID | What this contract takes from it | Record |
|---|---|---|
| OR-018 | One small deterministic kernel. Typed fail-closed adapters. Provider sampling, process implementation, terminal rendering, plugins, context optimization and frontends stay adapters or surfaces unless a later owner amendment moves a function. Not a language selection. | [DECISION-REGISTER.md](DECISION-REGISTER.md) |
| OR-019 | No external account, session, provider, model, terminal, plugin, database or other harness owns or recovers canonical identity and the related canonical fields. | [DECISION-REGISTER.md](DECISION-REGISTER.md) |
| D-018 | Native harness and unifying wrapper. The upstream harness is not the native loop. | [state/DECISIONS.md](../../state/DECISIONS.md#d-018--thesis-recursion-is-a-native-harness-and-a-unifying-wrapper-no-ecosystem-lock-in) |
| D-020 | One fenced canonical writer in the core. Automatic takeover only as a plugin behind that fence. Default install has no consensus. | [state/DECISIONS.md](../../state/DECISIONS.md#d-020--availability-contract-automatic-node-takeover-required-core-is-single-node-closes-g-06) |
| D-021 | Delegation over credential intermediation. | [state/DECISIONS.md](../../state/DECISIONS.md#d-021--delegation-over-credential-intermediation) |
| D-022 | One Stop vocabulary: Aggressive, Full, Lazy, plus `checkpoint`, `interrupt` and `drain`. A source interrupt, socket close or killed PID is not external rollback. | [state/DECISIONS.md](../../state/DECISIONS.md#d-022--one-pausestop-vocabulary-across-config-formal-model-and-broker-g-07) |
| D-028 | Platform floors. "Survives power loss" means application-consistent replay after an abrupt VM reset at a commit boundary, and only once traces pass. Physical interruption is out of scope. Not claimed here. | [state/DECISIONS.md](../../state/DECISIONS.md#d-028--platform-floors-closes-g-11) |
| D-029 | Strong credential non-intermediation needs an operator-provisioned boundary. Policy-only non-intermediation must be labeled as such. | [state/DECISIONS.md](../../state/DECISIONS.md#d-029--operator-provisioned-trusted-launch-boundary-strong-credential-non-intermediation) |
| D-037 | Provider-free fixtures may use Python as a disposable harness. The product language stays unselected. | [state/DECISIONS.md](../../state/DECISIONS.md#d-037--conformance-fixtures-authorized-python-as-disposable-test-harness-only-o-02) |
| D-039 | Language-agnostic architecture. The outline is candidate. §7 of [LANGUAGE-AGNOSTIC-ARCHITECTURE.md](architecture/LANGUAGE-AGNOSTIC-ARCHITECTURE.md) documents the two WorkPacket blockers. | [state/DECISIONS.md](../../state/DECISIONS.md#d-039--accept-the-oslanguage-expansion-as-advisory-plan-a-language-agnostic-architecture-o-04) |
| A6 | Version both WorkPacket discrepancies. Preserve historical bytes and receipts. Do not regenerate expectations from the candidate. Independent verifier from another model family. | CSL-206 comment `a8988987` |
| A7 / A3 | Append-only JSONL behind a store seam is the journal baseline for the bounded Python reference. It is not a store selection. FU-02 remains open. | CSL-206 comment `a8988987` |

The kernel's ratified responsibilities, as [ARCHITECTURE.md](ARCHITECTURE.md) states them, are strict schema and canonicalization, authenticated command admission, a pure reducer and event sequence, authority, budget, privacy and fence checks, deterministic WorkPacket derivation, effect-intent and receipt classification, replay and publication gating. Context construction stays an adapter.

---

## 5. Responsibility boundary

Normative owner rows are in [O2-MATRICES.md](O2-MATRICES.md). This section is the prose those rows must match. A future checker treats a prose/table conflict as a dangling identity (`O2E-004`) until the two texts name the same owner for the same responsibility id.

### 5.1 Closed owner set

| Owner id | May do | Must not do |
|---|---|---|
| `kernel-admission` | Decode, admit, reduce, derive the normative WorkPacket, assign identity and epoch values, reduce Stop into a pause epoch, replay the committed log | Commit to a store, call a provider, dispatch an effect, mint a second writer |
| `canonical-writer` | Append one committed record for the current fence epoch, including receipts | Dispatch, widen authority, accept a stale epoch, share the epoch with a second writer |
| `orchestrator` | Propose recovery and re-admission commands; mint the next attempt number when retry is admitted | Commit, dispatch, recover authority from a provider session, retry from a second layer |
| `effect-dispatcher` | Invoke an external effect only after the operation identity is committed, using that same identity | Mint an attempt, commit a receipt, grant authority, settle an uncertain effect |

An owner may hold more than one responsibility. A responsibility has exactly one owner.

### 5.2 States

These five states are distinct. A record of one is not a record of another (`O2F-A11`, `O2E-017`).

| State | Meaning | Who records it |
|---|---|---|
| Acknowledgment | A command was received. Nothing canonical has changed. | Not the canonical log. An ack without a commit is not durable. |
| Commit | The canonical writer appended the operation identity and the normative record under the current fence epoch. | `canonical-writer` |
| Dispatch | The dispatcher invoked the external effect after that commit, carrying the committed operation identity. | `effect-dispatcher` |
| Termination | The kernel reduced a terminal condition, including Stop quiescence. | `kernel-admission` |
| External settlement | A committed receipt says the outside effect's observed outcome. Uncertain until that receipt exists. | `canonical-writer` commits the receipt. The outside party does not write the log. |

Acknowledgment of Stop is not quiescence. Quiescence is the reduced terminal pause epoch (`O2F-P08`).

### 5.3 Operation identity before dispatch

The operation identity is committed before any dispatch. Recovery re-admits by reading that committed identity. It does not re-admit from provider session state, workflow-run state, a client cache or an import (`O2F-P03`, `O2F-P04`, `O2F-A09`, `O2F-A12`).

On the normative profile in the amendment, correlation for a derived WorkPacket is the GoalSpec id, and `effect_keys` come from the TaskNode. The historical profile still emits `CORR-M1A` and GoalSpec `allowed_effect_keys`. Both profiles are specified in the amendment. The historical profile is not rewritten to match the normative one.

### 5.4 One fenced writer

[D-020](../../state/DECISIONS.md#d-020--availability-contract-automatic-node-takeover-required-core-is-single-node-closes-g-06) and [STATE.md](STATE.md): for each aggregate, one fenced writer admits an event at a monotonic sequence. A later writer must present a newer epoch that the core checks. A stale epoch cannot commit or dispatch (`O2F-A02`, `O2E-008`). Two writers that both commit under one epoch fail `O2F-A08` / `O2E-014`.

Takeover, when the availability plugin is installed, is a new epoch behind that core fence. The default install compiles in no consensus. This draft does not choose a consensus algorithm, a store or a fencing-token implementation. Seam S-TAKEOVER records D-020 as inherited. Seam S-STORE stays open for the store.

Citations in the decisions queue (fencing tokens, transactional outbox, activity idempotency) support these inherited rules. They do not select the cited product.

### 5.5 Retry

Only `orchestrator` mints a new attempt, and only through an admitted retry decision. Re-execution of the same committed operation identity is dispatch of that identity, not a second attempt. Two layers that both mint an attempt fail `O2F-A05` / `O2E-011`.

A duplicate command (same identity, same normative bytes) returns the prior outcome. A conflicting command (same identity, different normative bytes) is refused and does not rebind. Treating those two cases as the same result fails `O2F-A04` / `O2E-010`. This matches the duplicate-versus-conflict distinction #231 already drafted for the wire vocabulary. This file does not restate that byte grammar.

### 5.6 Uncertain effects

An effect whose outcome is unknown after a crash is not replayed blindly and is not marked settled. The dispatcher stops. The writer commits an uncertainty record. Reconciliation inspects external state under the same operation identity before any further dispatch (`O2F-A07`, `O2E-013`). [STATE.md](STATE.md) already requires ambiguity recovery that inspects external state before repeating an effect. This draft does not claim that inspection exists in the current Python tree.

### 5.7 Stop

The only Stop vocabulary is D-022: **Aggressive**, **Full**, **Lazy**, plus `checkpoint`, `interrupt` and `drain`. A surface may request one of those modes. It may not invent a seventh mode that the kernel treats as quiescence.

Stop acknowledgment is not termination. Restart must not drop a committed effect and must not dispatch a second effect for the same operation identity (`O2F-A06`, `O2E-012`). A killed PID is not rollback of an external effect (D-022).

### 5.8 Replay and reconstruction

Replay is re-reduction of the committed log by `kernel-admission`. A gap, a fork or a conflicting event is refused (`O2F-A03`, `O2E-009`). An independent reconstruction that disagrees with the normative bytes or with the refusals fails the verifier in §8. The candidate's own verdict is not an input to that comparison.

### 5.9 Kernel, native loop, persistent assistant, compaction

These four are different. Collapsing any two fails `O2F-A14` / `O2E-020`.

| Boundary | What it is | What it is not |
|---|---|---|
| Kernel | The four pure functions and the fence checks in §4. `kernel-admission`. | A turn loop, a chat session, a store, a language runtime |
| Native loop | The small Recursion-owned turn loop [ARCHITECTURE.md](ARCHITECTURE.md) describes under OR-022. It submits typed proposals. Expansion past the minimum waits on a measured benefit. | The kernel. Not admitted to run by this draft. |
| Persistent assistant | A session that outlives one command, native or upstream. It may propose. | A canonical recovery source. OR-019 forbids that. The upstream harness is not the native loop (CSL-206 current state). |
| Compaction | A context transform outside the kernel. Context construction stays an adapter ([ARCHITECTURE.md](ARCHITECTURE.md), OR-018). | Authority. A summary cannot grant, widen or revoke. Effective authority is re-read from committed grants and epochs. |

### 5.10 Native-loop spike contract

This is the contract for a later spike. It does not authorize the spike, a provider call, a language trial or a CSL-240 run.

A spike admitted later must:

1. Keep `kernel-admission` as the only reducer and the only issuer of epoch values.
2. Keep the native loop on the adapter side of OR-018. It submits proposals. It does not append the canonical log.
3. Persist operation identity before any effect the spike claims to have dispatched.
4. Use the D-022 vocabulary, and show that an acknowledgment is not quiescence.
5. Use the normative WorkPacket profile from the amendment for any new expected bytes, and leave historical `CORR-M1A` fixtures untouched.
6. Stay provider-free and local.
7. Report a pre-registered benefit before anyone proposes to widen the native loop. [ARCHITECTURE.md](ARCHITECTURE.md) already requires that measurement. This draft does not invent the metric.

Seam S-NATIVE-SPIKE is deferrable. The spike is out of this freeze.

---

## 6. Gate specification

**Proposed command, not implemented:**

```text
python -X utf8 -B tools/check_native_engine_contract.py --candidate <FULL_SHA>
```

`<FULL_SHA>` is the 40-hex commit being checked. The script `tools/check_native_engine_contract.py` and `tools/test_check_native_engine_contract.py` are absent on purpose. They are a bounded child after H accepts a digest of this gate. Envelope G forbids using their future existence as this draft's gate.

Until that child lands, a reviewer hand-checks §6 and §7 against the four docs. §9 records the hand-check of this draft.

### 6.1 Modes

The command line has no mode flag. The checker derives the mode from the contract header:

| Header `Status` | Mode | Open freeze-blocking seam |
|---|---|---|
| `DRAFT` (this file) | draft | Reported. Does not by itself fail draft structure. Acceptance is still impossible. |
| `freeze` or `accepted` | acceptance | Nonzero `O2E-003` until every freeze-blocking seam is `accepted-H` under a matching receipt. |

A draft that is labeled `accepted` in the header without a matching H receipt fails `O2E-006`. Setting the header to `accepted` is not something this pull request does.

### 6.2 Allowed paths

The future checker may read:

- `docs/v3/O2-CONTRACT.md`
- `docs/v3/O2-MATRICES.md`
- `docs/v3/O2-WORKPACKET-AMENDMENT.md`
- `docs/v3/O2-DECISIONS-QUEUE.md`
- the pinned sources named in [O2-DECISIONS-QUEUE.md](O2-DECISIONS-QUEUE.md) §3
- fixture files only after DQ-02 is accepted, and only under the accepted directory

It must not write the repository. It must not execute `scripts/v3/planning/replay/kernel.py` to mint expected bytes (`O2E-019`). It must not read a private research vault. It must not treat #231's working tree as merged master. The #231 pin stays labeled candidate-only until that pull request's head is the commit under test or H names a different digest.

Proposed fixture directory, not created, not accepted: `docs/v3/fixtures/O2/`. See DQ-02. `scripts/tests/**/fixtures/**` stays protected under D-042. This pull request does not add files under `tools/contract_fixtures/`.

### 6.3 Supplemental commands

These already exist. They stay green as structural supplements. They do not authenticate a human and they do not prove this contract:

```text
python -X utf8 -B tools/check_program_control.py
python -X utf8 -B tools/test_program_control.py
```

### 6.4 Nonzero exits

| Error ID | Condition |
|---|---|
| `O2E-001` | A responsibility in the closed set has no owner or more than one owner, or an owner id is outside the closed set in §5.1. |
| `O2E-002` | A responsibility names no fixture, or a named fixture is absent from §7. |
| `O2E-003` | A seam has no `classification`, or the classification is not exactly `freeze-blocking` or `deferrable`. In acceptance mode, also a freeze-blocking seam whose status is not `accepted-H`. |
| `O2E-004` | A responsibility, owner, fixture, seam or error id is referenced and not defined, or the prose in §5 names a different owner than the matrix for the same responsibility id. |
| `O2E-005` | A pinned source's sha256 does not match the evidence manifest at the named commit, or a fixture digest is present and does not match the bytes. A `pending` digest is not a hash. Presenting `pending` as a computed digest fails this id. |
| `O2E-006` | A receipt omits the candidate SHA, omits a contract-artifact digest, names a different issue, names a different scope, or is not a human-authored Linear comment meeting rule H. A fixture-only signature fails this id. |
| `O2E-007` | Normative `effect_keys` are wider than the TaskNode set (the historical GoalSpec copy, when evaluated as if it were normative). |
| `O2E-008` | A stale authority or a stale fence or pause epoch commits or dispatches. |
| `O2E-009` | A replay gap or a conflicting replay is accepted. |
| `O2E-010` | A duplicate command and a conflicting command produce the same disposition. |
| `O2E-011` | More than one owner mints a new attempt. |
| `O2E-012` | Stop or restart drops a committed effect or dispatches a second effect for the same operation identity. |
| `O2E-013` | An uncertain effect is dispatched again or marked settled without a committed reconciliation. |
| `O2E-014` | Two canonical writers commit for one aggregate under one fence epoch. |
| `O2E-015` | Canonical authority or identity is recovered only from provider or workflow session state. |
| `O2E-016` | A client, cache or import grants or widens authority. |
| `O2E-017` | Acknowledgment, commit, dispatch, termination or external settlement are treated as the same state. |
| `O2E-018` | Dispatch occurs with no committed operation identity. |
| `O2E-019` | An expected normative byte string was produced by executing the candidate, including current `kernel.py`. |
| `O2E-020` | The kernel is identified with the native loop, with a persistent assistant, or with compaction. |

Error ids are stable. A later revision adds a new id. It does not reuse an id for a different condition.

### 6.5 Digests

Digests of the four contract artifacts are computed when a human freezes a candidate. This draft hard-codes none of them. The token `pending` means "not computed". It is not the sha256 of the empty string and it is not a stand-in receipt.

---

## 7. Frozen fixture manifest

No fixture file is added in this pull request. The manifest is the oracle sketch. Expected results are the error ids below, not bytes emitted by the candidate. Positive rows pass. Adverse rows fail with exactly the listed id. A positive row that also emits an adverse id fails. An adverse row that passes fails.

Inline baseline, shared by every row unless the row says otherwise:

- One aggregate, one `writer_id`, fence epoch `7`, pause epoch `0`.
- One committed GoalSpec id `GOAL-1` with `allowed_effect_keys` `[send, draft]`.
- One TaskNode id `TASK-1` with `effect_keys` `[draft]` (a narrowing).
- One operation identity `OP-1`, attempt `1`, not yet dispatched.
- Historical profile and normative profile are the two profiles in the amendment. They are not interchangeable.

| Fixture | Class | Covers | Expected |
|---|---|---|---|
| `O2F-P01` | positive | `R-ADMIT` | Pass. A command with a missing required field is refused. No event is appended. |
| `O2F-P02` | positive | `R-WRITE` | Pass. The current-epoch writer appends exactly one event at the next sequence. |
| `O2F-P03` | positive | `R-IDENT` | Pass. `OP-1` is on the log before any dispatch flag exists. |
| `O2F-P04` | positive | `R-ORCH` | Pass. After a restart the orchestrator proposes re-admission of `OP-1` from the log. It does not commit. |
| `O2F-P05` | positive | `R-RETRY` | Pass. The next attempt is `2` only after an admitted retry. A repeat of attempt `1` keeps `OP-1`. |
| `O2F-P06` | positive | `R-DISPATCH` | Pass. Dispatch carries committed `OP-1` and normative `effect_keys` `[draft]`. |
| `O2F-P07` | positive | `R-RECEIPT` | Pass. A byte-identical duplicate command returns the prior receipt. The writer committed that receipt. |
| `O2F-P08` | positive | `R-STOP` | Pass. Stop ack leaves the pause epoch unchanged. Quiescence is a later reduced terminal epoch. |
| `O2F-P09` | positive | `R-REPLAY` | Pass. A gap-free log reduces to the same normative records a second time. |
| `O2F-P10` | positive | `R-IDENT` | Pass. A historical record with `correlation_id` `CORR-M1A` and GoalSpec effect keys remains historical. It is not re-sealed into the normative profile. |
| `O2F-A01` | adverse | `R-DISPATCH` | `O2E-007`. Packet effect keys are `[send, draft]` while the TaskNode allows only `[draft]`. |
| `O2F-A02` | adverse | `R-ADMIT`, `R-WRITE` | `O2E-008`. Epoch `6` commits or dispatches after epoch `7` is current. |
| `O2F-A03` | adverse | `R-REPLAY` | `O2E-009`. A missing sequence, or two events with one sequence and different bytes, is accepted. |
| `O2F-A04` | adverse | `R-ADMIT`, `R-RECEIPT` | `O2E-010`. The duplicate in `O2F-P07` and a same-id command with a different effect set get one shared disposition. |
| `O2F-A05` | adverse | `R-RETRY` | `O2E-011`. Both `orchestrator` and `effect-dispatcher` set attempt `2`. |
| `O2F-A06` | adverse | `R-STOP` | `O2E-012`. After Stop and restart, `OP-1` is either gone or dispatched twice. |
| `O2F-A07` | adverse | `R-RECEIPT`, `R-DISPATCH` | `O2E-013`. The effect outcome is unknown and the log shows a second dispatch or `settled`. |
| `O2F-A08` | adverse | `R-WRITE` | `O2E-014`. Two writer ids append under fence epoch `7`. |
| `O2F-A09` | adverse | `R-ORCH`, `R-IDENT` | `O2E-015`. The only recovery input is a provider or workflow session id. The log is ignored. |
| `O2F-A10` | adverse | `R-ADMIT` | `O2E-016`. A client cache or an imported file adds `send` to the effective effect set. |
| `O2F-A11` | adverse | `R-RECEIPT` | `O2E-017`. An acknowledgment record is stored as commit, dispatch, termination or settlement. |
| `O2F-A12` | adverse | `R-IDENT`, `R-DISPATCH` | `O2E-018`. Dispatch of `OP-1` is recorded with no committed identity. |
| `O2F-A13` | adverse | `R-REPLAY` | `O2E-019`. The expected normative packet bytes are the stdout of the candidate `derive_work_packet`. |
| `O2F-A14` | adverse | `R-ADMIT` | `O2E-020`. A persistent-assistant transcript, a compaction summary or the native turn loop is accepted as the reducer. |

Fixture-file sha256 values are `pending`. Inventing them would be a fake pin (`O2E-005`).

---

## 8. Independent verification

A verifier from a different model family than the author of the candidate reruns the corpus. It binds the full candidate SHA and the sha256 of the four contract artifacts. For each fixture it records the expected id and the observed id. It rebuilds normative bytes and refusals from the amendment and from §7, and diffs that reconstruction against the candidate. It does not read the candidate's pass/fail line.

The report passes only when every fixture matches and that diff is empty. The report is attached to CSL-206 or to the pull request. It is not rule H. A missing report fails acceptance (`O2E-006` if a receipt claims verification that is not there; the implementation child also fails closed when acceptance mode lacks the report).

This draft has no such report.

---

## 9. Hand-check of this draft

Checked 2026-10-25 against the files in this change, without `tools/check_native_engine_contract.py`.

| Check | Result |
|---|---|
| Four docs exist only under `docs/v3/O2-*` | Met by this change. Confirm in the pull-request file list. |
| Every responsibility has one owner and at least one fixture | Met. See the matrix. |
| Every seam is `freeze-blocking` or `deferrable` | Met. See the matrix. |
| A6 split: historical bytes preserved, normative expectations written separately, candidate is not the oracle | Met. See the amendment. |
| Wire bytes not copied from #231 | Met. Pointer and candidate hash only. |
| Checker and fixture files absent | Met. |
| Acceptance | Not met. Freeze-blocking seams still `open`: `S-DURABILITY`, `S-FIXTURE-PLACE`, `S-STREAM-EPOCH`, `S-WORKPACKET`, `S-AUTHORITY-COMP`, `S-H-RECEIPT`. A checker in acceptance mode would exit `O2E-003` and `O2E-006`. |

Draft structure is present. This candidate is not freeze-ready and not accepted.

---

## 10. Rule H

No person has accepted `o2-contract v0.1-draft`. The block below is a blank form. Filling it in inside this file would not be a Linear receipt.

```text
comment id:
human identity:
delegation:
contract digests:
candidate sha:
scope:
selected:
rejected:
rationale:
conditions:
date:
revisit:
```

Lucas, or a human he explicitly delegates, writes that comment on CSL-206 if and when he accepts a digest. This repository's agents do not.
