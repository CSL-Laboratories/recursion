# O2 decisions queue and evidence manifest

**Version:** `o2-decisions-queue v0.1-draft`
**Status:** DRAFT under envelope G. A packet here is not a decision.
**Parent:** [CSL-206](https://linear.app/csl-laboratories/issue/CSL-206)

A decision exists only when a human posts it from their own account, a developer-approved comment that counts under [D-043](../../state/DECISIONS.md#d-043---a-developer-approved-comment-counts-as-a-personal-receipt) carries it, or it is already in `state/DECISIONS.md` or `docs/adr/`. Comment `a8988987` is an attributed relay. It is cited as the A6/A7 record the owner plan uses. It is not a receipt for this queue.

Draft posture is how this text stayed inside the owner plan while leaving the choice open. It is not a recommendation Lucas has accepted.

Seam ids match [O2-MATRICES.md](O2-MATRICES.md).

| Seam | Where it is disposed, or why it is still open |
|---|---|
| `S-STORE` | DQ-06 |
| `S-FRAMEWORK` | DQ-06 |
| `S-LANGUAGE` | DQ-07 |
| `S-DURABILITY` | DQ-03 |
| `S-FIXTURE-PLACE` | DQ-02 |
| `S-CSL12` | DQ-01 |
| `S-CI` | DQ-04 |
| `S-CITATION` | DQ-05 |
| `S-STREAM-EPOCH` | DQ-08 |
| `S-SEQUENCE` | DQ-09 |
| `S-WORKPACKET` | DQ-11 |
| `S-WIRE-BYTES` | DQ-08 and DQ-11. The byte draft stays on recursion#231. This queue does not copy it. |
| `S-AUTHORITY-COMP` | DQ-10 |
| `S-NATIVE-SPIKE` | DQ-13 |
| `S-TAKEOVER` | No open packet. [D-020](../../state/DECISIONS.md#d-020--availability-contract-automatic-node-takeover-required-core-is-single-node-closes-g-06) already records takeover behind the core fence. Status `recorded-inherited`. |
| `S-H-RECEIPT` | DQ-12 |

---

## 1. Packets

### DQ-01 — CSL-12 placement

**Seam:** `S-CSL12` (`deferrable`, `open`)
**Question:** After cancellation, does O2 stay on CSL-206 with historical links, or does a founder reactivate CSL-12?
**Observed 2026-10-25:** [CSL-12](https://linear.app/csl-laboratories/issue/CSL-12) and [CSL-13](https://linear.app/csl-laboratories/issue/CSL-13) are both Canceled. The issue text prefers a human-approved reactivation only after that cancellation is dispositioned, and otherwise an O2 parent with historical links.
**Options:** A) Reactivate CSL-12 and hang the contract there. B) Keep the contract on CSL-206 and keep the historical links.
**Draft posture:** B, because that is where the issue already is. Not a disposition of the cancellation.
**Who decides:** Lucas, or a human he delegates under rule H.

### DQ-02 — Fixture placement

**Seam:** `S-FIXTURE-PLACE` (`freeze-blocking`, `open`)
**Question:** Where do O2 fixture files go when a later child is allowed to add them?
**Options from owner plan `12dc4d83`:** A) `tools/contract_fixtures/O2/`, which still trips the docs-sensitive CI gate. B) Beside the docs, with `ci_gate` skipping docs-only diffs until the checker exists. C) Change the CI rule.
**Draft posture:** proposal only, aligned with the O1 docs-beside-docs shape: `docs/v3/fixtures/O2/`. No files are created. `scripts/tests/**/fixtures/**` is not used (D-042). This pull request does not edit `tools/contract_fixtures/` (that tree is recursion#231's).
**Who decides:** Lucas. The proposal is not option B accepted.

### DQ-03 — Durability claims

**Seam:** `S-DURABILITY` (`freeze-blocking`, `open`)
**Question:** Does freeze stay at "no exactly-once, no physical-power-loss, current JSON writes are not an outbox commit", or is a stronger claim admitted with evidence?
**Inherited text:** [STATE.md](STATE.md) refuses exactly-once as an acceptance claim. [D-028](../../state/DECISIONS.md#d-028--platform-floors-closes-g-11) defines a VM-reset replay floor and leaves it Unqualified until traces pass. Physical platter interruption is out of scope there. CSL-206 says today's run-state JSON writes do not prove a transactional commit.
**Options:** A) Freeze the non-claim. B) A later H admits a stronger guarantee with its own evidence.
**Draft posture:** the contract states the non-claim and does not present it as a passed qualification. The seam stays open so A is not silent.
**Who decides:** Lucas.

### DQ-04 — CI spend

**Seam:** `S-CI` (`deferrable`, `open`)
**Question:** Does this lane run Windows and macOS (`ci:full`), and which runner wording is current?
**Owner plan `12dc4d83` (2026-10-17):** Blacksmith `linux` required; `ci:full` at Lucas's discretion.
**Observed on base `7862cab9`:** [AGENTS.md](../../AGENTS.md) records CSL-408, required check `linux` on GitHub-hosted runners, Windows and macOS on `ci:full`. This pull request does not add `ci:full` and does not edit `.github/`.
**Options:** A) Docs-only, required `linux` only, no `ci:full`. B) Add `ci:full` for this lane. C) Lucas reconciles the Blacksmith sentence with CSL-408 in a governance change. C is not available to this pull request.
**Draft posture:** A. The Blacksmith sentence is left as a dated owner-plan quote, not rewritten into a new CI decision.
**Who decides:** Lucas, including any spend.

### DQ-05 — Citation pass versus the zero-run budget

**Seam:** `S-CITATION` (`deferrable`, `open`)
**Question:** Does the citation list in comment `12dc4d83` count against CSL-206's "zero new external research runs" budget?
**Observed:** that comment lists the pass. This docs session did not call `createDeepResearch`, did not start a Parallel task group, and did not start a new paid run. The URLs in §2 are copied from that comment as support. They were not re-researched here.
**Options:** A) Treat `12dc4d83` as the one allowed citation pass and this session as zero additional runs. B) Count that pass as a budget exception that still needs an explicit waiver. C) Forbid citing those URLs until a waiver exists.
**Draft posture:** A for this session's own conduct (no new run). Whether A matches the issue budget is open, same kind of accounting question as O1's citation packet. The URLs do not become requirements either way.
**Who decides:** Lucas.

### DQ-06 — Framework and store

**Seam:** `S-STORE`, `S-FRAMEWORK` (both `deferrable`, `open`)
**Question:** Which durable-workflow framework and which store, if any, does a later milestone use?
**Recorded constraint:** A7/A3 in `a8988987` set append-only JSONL behind a store seam as the baseline for the bounded Python reference. FU-02 stays open. That baseline is not a selection.
**Draft posture:** no selection. The contract's writer is `canonical-writer` regardless of the bytes' physical home.
**Who decides:** Lucas, later, under H. Not this draft.

### DQ-07 — Product language

**Seam:** `S-LANGUAGE` (`deferrable`, `open`)
**Question:** Which language implements the native engine?
**Recorded constraint:** D-037, D-039, FU-01, CSL-240. Out of O2's scope. A native-loop spike under the contract stays unrun.
**Draft posture:** no selection and no port.
**Who decides:** the founders. Lucas breaks ties. This queue does not.

### DQ-08 — `stream_epoch` and the IPC four-row table

**Seam:** `S-STREAM-EPOCH` (`freeze-blocking`, `open`)
**Question:** How do version negotiation, snapshot materialization, cursor rollover and ACK/release map onto the O2/wire candidate, including `stream_epoch`?
**What was checked before writing a table:**

- Comment `b89068a9` (2026-10-24) asks for one four-row table and says to confirm the owner does not already have it.
- CSL-238 comment `3d0f4e59` says `stream_epoch` was deferred to the O2 freeze as an open question. That is not the four-row table.
- recursion#231 carries `docs/v3/PUBLIC-WIRE-CONTRACTS.md` at head `7932749366761fb2d831246c7c25777f7c370adf`. This draft hashes that candidate and does not copy it. The file is not on the base commit.
- No `docs/v3/O2-*` file existed on the base commit.

**Draft posture:** do not add the table in this pull request. Adding it would be a second wire note beside #231, which the launch constraint forbids, and comment `b89068a9` says to stop at consequential semantics.
**Options:** A) A later slice, coordinated with the CSL-238 owner, writes the four rows against the #231 head Lucas names. B) H defers `stream_epoch` explicitly and drops the table from the O2 freeze. C) This draft grows the table now.
**Rejected by this change:** C. A and B stay open.
**Who decides:** Lucas, with the CSL-206 / CSL-238 owner. Samir was named on `b89068a9` as someone who can coordinate. This packet does not assign that work.

### DQ-09 — O1 / O2 / O6 order

**Seam:** `S-SEQUENCE` (`deferrable`, `open`)
**Question:** Must O2 wait until O1 or O6 is accepted?
**Issue text:** O1 and O6 constrain freeze and can be reviewed together.
**Observed:** [recursion#279](https://github.com/CSL-Laboratories/recursion/pull/279) (CSL-205) is open. This draft does not read it as accepted.
**Draft posture:** publish the O2 draft while #279 is open. Do not block on O1's rule H and do not claim O1's digest.
**Who decides:** Lucas, if the concurrent draft is the wrong order.

### DQ-10 — Authority-composition child

**Seam:** `S-AUTHORITY-COMP` (`freeze-blocking`, `open`)
**Question:** Are the three obligations in the contract (requested ≠ effective, grants do not widen, changes are admitted commands) enough to freeze, with CSL-239 writing the rest later?
**Observed:** [CSL-239](https://linear.app/csl-laboratories/issue/CSL-239) is Triage. Its open question to Lucas (comment `c639d2f8`, 2026-10-17) asked whether to start before #231 and O2 H. This draft does not answer that question and does not start CSL-239.
**Options:** A) H accepts the three obligations as the O2 fragment and leaves the child spec to CSL-239. B) O2 freeze waits for the CSL-239 spec. C) Copy a composition spec into this pull request.
**Rejected by this change:** C.
**Who decides:** Lucas.

### DQ-11 — WorkPacket normative digest

**Seam:** `S-WORKPACKET` (`freeze-blocking`, `open`)
**Question:** Does H accept the two profiles in [O2-WORKPACKET-AMENDMENT.md](O2-WORKPACKET-AMENDMENT.md), including the proposed version id `m1a-work-packet-2-draft`?
**Recorded direction:** A6. Not a digest acceptance. See that comment's own limit.
**Draft posture:** the amendment text is the proposed disposition. Historical bytes stay historical. No golden file is generated.
**Who decides:** Lucas, binding the amendment sha256.

### DQ-12 — Rule H for this contract

**Seam:** `S-H-RECEIPT` (`freeze-blocking`, `open`)
**Question:** Which digest, if any, is accepted, under what conditions, with what revisit trigger?
**Draft posture:** none. The blank form in the contract is not a receipt. This pull request's merge does not close CSL-206.
**Who decides:** Lucas, or a human he explicitly delegates, in a human-authored Linear comment.

### DQ-13 — Native-loop spike admission

**Seam:** `S-NATIVE-SPIKE` (`deferrable`, `open`)
**Question:** When, if ever, does a provider-free spike run under §5.10 of the contract?
**Draft posture:** the spike contract is written and the spike is not run. CSL-240 is a different issue and is not answered here.
**Who decides:** the founder who admits that later child. Not this draft.

---

## 2. Citations (support only)

Source of the list: CSL-206 comment `12dc4d83`, dated 2026-10-17. No new paid run was started for this pull request. None of these select a framework or a store. They illustrate fencing, outbox idempotency and retry, which D-020, D-022 and A6 already require.

| Topic | URL | Supports | Does not do |
|---|---|---|---|
| Transactional outbox | https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html | Persist the intent with the state change; consumers stay idempotent | Select AWS or a store |
| Optimistic write precondition | https://docs.eventsourcingdb.io/fundamentals/preconditions/ | A stale subject epoch must fail the write | Select that database |
| Atomic append and ordering | https://docs.eventsourcingdb.io/about-eventsourcingdb/consistency-guarantees/ | Ordering is a stated guarantee, not a side effect of a hash chain | Select that database |
| Fencing tokens | https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html | A paused lock holder must not write after the fence moves | Select a lock service |
| Fencing-token demonstration | https://github.com/mselser95/fencing-token-demo/blob/master/README.md | Same failure, in runnable form | Vendor that demo |
| Activity retry | https://docs.temporal.io/activity-definition | A retried activity can run more than once, so the effect must be safe to repeat | Select Temporal (`S-FRAMEWORK` stays open) |
| Fencing and drain notes | https://zylos.ai/research/2026-09-03-fencing-tokens-drain-protocols-agent-session-pools | Session-pool discussion of fences | Set Recursion's owner |
| Durable agent runtimes | https://zylos.ai/research/2026-04-24-durable-execution-agent-runtimes | Checkpoint and replay are a known design space | Prove Recursion's durability |
| Crash-resilient agents | https://dev.to/monuminu/crash-resilient-long-running-agents-in-microsoft-foundry-leases-checkpoints-and-side-effect-1l93 | Leases and side-effect fences | Select that host |
| Duplicate side effect | https://dev.to/milo_antaeus_784320e2f2f9/why-your-ai-agent-sent-that-email-twice-an-idempotency-field-guide-46le | Lost response plus retry without an idempotency key | Set Recursion's key format |

Evidence class for every row: non-normative citation. Not source-documented product behavior. Not a local measurement.

---

## 3. Evidence manifest

Pins are sha256 of the file bytes at the named commit, computed 2026-10-25 from `git show <commit>:<path>` for master pins, and from the GitHub blob for the #231 pin. `pending` means this draft does not claim a digest.

### 3.1 Contract artifacts

Computed at freeze. Hard-coding them here would be a false pin.

| Path | sha256 |
|---|---|
| `docs/v3/O2-CONTRACT.md` | `pending` |
| `docs/v3/O2-MATRICES.md` | `pending` |
| `docs/v3/O2-WORKPACKET-AMENDMENT.md` | `pending` |
| `docs/v3/O2-DECISIONS-QUEUE.md` | `pending` |

### 3.2 Repository pins

Commit `7862cab9fbcaf450048a3b827ba3c5f888285b1b` (`master` when this draft was branched).

| Path | Bytes | sha256 | Use |
|---|---:|---|---|
| `docs/v3/architecture/LANGUAGE-AGNOSTIC-ARCHITECTURE.md` | 27444 | `97e0325b034d8430dcaad46c2812909263b7c324cd2ac1462458837d5492bf46` | §7 names the two WorkPacket blockers |
| `docs/v3/ARCHITECTURE.md` | 49160 | `669fd7a9807031885f5bcc358735a76221fa09823cc2adeae0182e2864628605` | Kernel versus native loop; context stays an adapter |
| `docs/v3/STATE.md` | 12032 | `cdba55654515a39c09d900d757b1d5460b3be1e3be8f7af0bd571198279171b4` | One fenced writer; exactly-once is not a claim; store unselected |
| `docs/v3/SECURITY.md` | 15304 | `59f4c5c5bca0adb804a9b1f8dd71fcf4975137143a917e73c61d7fa27c0ed74b` | Narrowing; stale epochs rejected |
| `docs/v3/RESEARCH-AGENDA.md` | 33624 | `2e6f7099303616cc8042c9f57e0336d16d60d8d8f878aa9228b4bf2503b7628b` | R1 native-loop spike is research, not this run |
| `docs/v3/M1A-WIRE-PROFILE.md` | 43532 | `8041689e110ccdafb10225181abce52bddda7ac1ea198c0118cc1d8de69f11e0` | GoalSpec correlation; TaskNode effect fields |
| `docs/v3/DECISION-REGISTER.md` | 45943 | `3344cbd8aa603753d9f71f596a7ab773dba5201cec7ee41e9671e3331d08221f` | OR-018, OR-019 |
| `state/DECISIONS.md` | 40245 | `1a8c092085cbe1ecf5d69fc72dbde349686d7648562d7f62e2214ec41c6e1002` | D-018, D-020, D-021, D-022, D-028, D-029, D-037, D-039 |
| `scripts/v3/planning/replay/kernel.py` | 35938 | `9327669e4ae8e2a8b97918c16cf91ec2b96797be78af989dc26939192a345338` | Historical loci only. Not an oracle. Not edited. |
| `CONTROL_PLANE.md` | 9654 | `73ec2f799280e2167461e7f3de6172e6fe54456ea2e7955d7832bcf03dc8ab45` | Who decides. Not edited. |
| `ANCHOR.md` | 3200 | `0975e9151c1bafa3bd67d66fed061078b1eb75e6b808c65a7f4bfe851fc8b0e5` | Scope boundary. Not edited. |
| `AGENTS.md` | 46114 | `310e6743e479ce30c945f25fb7129728b31eaccde40ae07a37a13a765e485eab` | CSL-408 CI wording for DQ-04. Not edited. |

### 3.3 Candidate pin (not on master)

| Item | Value |
|---|---|
| Pull request | [recursion#231](https://github.com/CSL-Laboratories/recursion/pull/231) |
| Issue | CSL-238 |
| Head | `7932749366761fb2d831246c7c25777f7c370adf` |
| Path | `docs/v3/PUBLIC-WIRE-CONTRACTS.md` |
| Bytes | 18185 |
| sha256 | `bc5b6a7d00c9fabf7057b4eca9b97467cd5ced8b867768d18d5ed17183698d50` |
| Class | candidate-only. Behind `master` is a property of that open pull request, recorded by its Linear sync comment, not re-derived here as a merge instruction. |
| Limit | Vocabulary pointer. Not a copy. Not accepted. `stream_epoch` remains DQ-08. |

### 3.4 Linear records cited

| Id | What it is |
|---|---|
| `94c585da` | Lucas asks for an output and an acceptance gate before the W2 freeze |
| `a8988987` | Attributed relay: A6 amendment terms; A7/A3 JSONL baseline; FU-02 open |
| `12dc4d83` | Owner plan this draft follows |
| `b89068a9` | Four-row IPC handoff; do not duplicate a table the owner already has |
| `3d0f4e59` | CSL-238: `stream_epoch` deferred to the O2 freeze |
| `c639d2f8` | CSL-239: question on starting before O2 H, unanswered here |

### 3.5 Not used

No private `csl-research` vault path, object or quotation is an input to this manifest. Fixture-file digests are `pending`. No rule-H digest is recorded because none exists.
