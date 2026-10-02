# Recursion language and reconstruction test handoff

Date: 2026-10-02 UTC  
Source commit: `699a29d622d1a1d3cecdaf3504cc5d649fb9d20e`  
Parent goal: `goal-recursion-v3-harness-orchestrator`  
Disposition: proposal only; every vector and future session below is unexecuted by this research

## Purpose and authority

This handoff turns the R-LANG report into bounded work that a later, explicitly admitted local session can evaluate. It authorizes no build, install, model/provider call, network target, account, credentials, live host action, destructive reset, spending, publication or merge. It does not request that a session be opened now. The report contains all technical rationale and primary sources; links below point to the same pinned project source rather than implying new accepted contracts.

All proposed test behavior below is **inferred/design (I)**. Current repository status is **source-documented (D)** and file inspection is **locally observed static (L)**. Runtime results are **unknown (U)** until independently produced and reviewed. Unknown cannot be counted as zero, a passed gate or supported capability.

## Current evidence to preserve

- FU-01 has no accepted runtime winner. Its historical CPython GIL/free-threaded, Rust and Go arms remain separate; any new patch or topology creates a new experiment identity
- FU-02 compares segmented sequence + CAS with SQLite WAL + identical CAS; both complete commit paths remain unmeasured
- FU-03 Session A records 51 diagnostic vectors and independent diagnostic acceptance, with 10 killed mutants and incomplete integrated coverage. Its final README no-rerun sentence is stale relative to the dated verification record
- Stage B records 26 synthetic cases: 16 bounded-consistent, five unsupported, three unknown and two blocked; independent bounded acceptance is recorded. Neither native reset/store nor real process containment is thereby qualified
- D-028 fixes Windows 11 25H2 build 26200 x64 and the additional 24H2 build 26100 Enterprise case, Linux 6.12/6.6 with cgroup v2, local NTFS/ext4 and abrupt VM-reset application consistency. WSL2 remains distinct dev/test evidence
- Existing M1B JCS-based strict wire bytes are separate from candidate FU-03 RCV1/CBOR test interchange. Do not retrofit either into the other

Sources: [accepted decisions](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/state/DECISIONS.md), [Session A](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-kernel-session-a/README.md), [Stage B](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-stage-b/README.md), [M1B wire](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/M1B-WIRE-PROFILE.md)

## Admission prerequisites

The future executor stops before the dependent work if any prerequisite is absent. A research packet is not substitute authorization.

1. Exact allowed paths, parent goal, candidate source, acceptance gate and independent reviewer
2. Historical replication versus prospective refreeze, complete runtime/patch/compiler/native-library identities and admitted comparison roster; C#/.NET/JVM/C++/Node expansion needs its own finite budget
3. Accepted FU-03 semantics, literal corpus manifest, refusal/detail/precedence registry and independently reviewed oracle. Diagnostic omissions must be declared and cannot silently disappear from the denominator
4. Frozen host profile, native filesystem/VFS/cache/VM details, process-accounting boundary and proof that the target is disposable and specifically approved for any later reset
5. Owner-approved resource ceilings, meaningful effect margins, maximum work/repair/repetition exposure, stopping policy and permitted experiment duration. This handoff invents no product minimum
6. Fixed commit/acknowledgement contract, batch policy, uncertainty handling, CAS retention/backup scope, single-writer takeover policy and any unresolved availability assumptions
7. Offline dependency/build closure, artifact digest policy, license/provenance evidence and core-only boot requirement. No external runtime binary is certified by its version string

## Bounded work sequence

### A Release the reconstruction inputs

Create a reviewable manifest of the neutral units in report section 7. Bind each requirement to its literal vectors, abstraction, refusal and acceptance observable. Resolve overlaps without changing accepted meanings. Separate semantic failures from frame errors and store/host uncertainty. Record every remaining seam and exclude it explicitly from any bounded claim.

Acceptance object: a frozen reconstruction kit plus an independent completeness review. Rival: first implementation as specification. Falsifier: the reviewer or second implementer must recover an unrecorded rule from original code. Non-proof: document completeness does not establish runtime correctness or cheap reconstruction.

### B Admit independent implementations

Use the existing FU-01 roster unless an expansion is explicitly admitted. Each arm implements the same pure API and consumes the same released byte corpus. Do not share a reducer, serializer, decoder registry, generator template or expected-output generator between independent stacks. Declare permitted shared primitives. No language binary, package manager, hybrid or store is selected by this handoff.

Acceptance object: A result/bytes = B result/bytes = independent literal corpus, plus valid observation traces. Rival: one implementation with cross-platform tests. Falsifier: one unexplained mismatch or undisclosed shared semantic engine. Non-proof: finite equivalence is not an unbounded refinement proof.

### C Qualify platform and store seams separately

Hold semantic groups, acknowledgement timing and CAS obligations equal. Audit the native syscall/VFS path before crash trials. Use independent durable acknowledgement observation and retain uncertain outcomes. Process kills, synthetic cuts and VM resets are separate evidence classes. Existing native-blocked Stage B cases stay blocked until authorized and implemented.

Acceptance object: exact group/receipt/high-water/outbox/CAS recovery and stale-writer rejection on each required native profile. Rival: trusting the embedded store's transaction API. Falsifier: any acknowledged loss, stale mutation, silent history rewrite or dangling committed artifact. Non-proof: passing VM resets says nothing about physical power cuts or network filesystems.

### D Compare resource and lifecycle cost

Only accepted floor-passers enter ranking. Pair the same workload and host block; retain failures, refusals, generator lateness and censored outcomes. Freeze analysis before confirmatory observations. Count complete process groups, native libraries, helper processes, storage bytes, human review and correction. Re-run hard floors after stress and after any changed binary identity.

Acceptance object: reviewed paired results with denominators and uncertainty under the Owner's ceilings/margins. Rival: language microbenchmarks or first-generation speed. Falsifier: a claimed advantage disappears after full accounting, independent replication or review/rework inclusion. Non-proof: one host/workload establishes no universal winner.

### E Run the reconstruction challenge

Give a second team only the accepted neutral kit, public platform documentation and approved type/vector artifacts. Keep the original reducer hidden during the bounded challenge. Record clarifications and re-derivation separately from ordinary syntax/API lookup. Compare unchanged external plugin digests/transcripts, restore/cutover behavior and full review/rework cost. If the budget ends before acceptance, report incomplete, not cheap.

Acceptance object: report RC-01 through RC-09 with independent receipts and a measured cost bound. Rival: conventional port with original source access. Falsifier: missing semantics require original-code recovery, behavior changes, plugin rewrites, dual writers, or cost exceeds the preregistered ceiling. Non-proof: success for one pair does not validate D-033 ecosystem-import savings.

## Proposed vector additions

These are synthetic descriptions, not literal corpus bytes or executable tests. Expected refusal identifiers and byte values must come from the released profile, not this table or an implementation. “Reject” never means invent a new refusal code.

| ID | Synthetic case | Independent expected observation | Adverse control or rival |
|---|---|---|---|
| RL-01 | Equivalent maps built in different insertion orders and process hash seeds | Exact same profile-owned bytes and digest | Iterator-order serializer emits different bytes |
| RL-02 | Supplementary Unicode versus BMP key ordering, composed/decomposed strings | JCS UTF-16 ordering where that profile applies; strings unchanged | Native string comparator or implicit normalization |
| RL-03 | Duplicate keys presented before object collapse | Exact strict-profile refusal at the specified stage | Parser keeps last value and wrongly admits |
| RL-04 | M1B floats, negative zero, non-finite/invalid tokens, booleans in counts, overflow boundaries | Rejections and boundary acceptance exactly match M1B; no new float rule | Permissive default JSON decoder |
| RL-05 | CBOR test profile with nonshortest head, duplicate key, tag, trailing bytes and invalid UTF-8 | Exact released frame/decode outcome; no implicit production-wire adoption | Decoder normalizes prohibited encodings |
| RL-06 | Same command ID with same versus changed bytes | Same historical receipt versus exact conflict; no extra mutation | New ID generated after uncertain ACK |
| RL-07 | Competing commands with same expected state | At most the specified next mutation; stale expectation refused | Lock held only around state read |
| RL-08 | Suspend writer, admit higher epoch, resume old writer | Old writer cannot mutate even if it reacquires a file/database lock | Treat lock ownership as authority |
| RL-09 | Pause/lease/goal/plan change between admission, persistence and dispatch | Each fence rechecked at its declared boundary; no stale fresh effect | Only admission-time validation |
| RL-10 | Cancellation while child/grandchild is blocked or unobservable | Distinct requested/fenced/exited/remote-observed outcomes; uncertainty retained | Parent exit or timeout mislabeled complete |
| RL-11 | Crash around atomic multi-event group | Whole admitted group and one bound receipt, or declared uncommitted/uncertain state | Partial effect-prepared/outbox pair |
| RL-12 | Crash after durable group before ACK reaches caller | Retry same identity resolves durable receipt without duplicate effect | Blind replay or erase as failed |
| RL-13 | Short write, disk full and I/O error around barrier | No false durable acknowledgement; retained diagnosable outcome | Wrapper ignores native failure |
| RL-14 | VM reset at CAS publish/manifest/commit/ACK cut | Every witnessed ACK recovers exact canonical group and referenced bytes | Store survives while CAS reference dangles |
| RL-15 | Interior log corruption or stale/mismatched WAL artifacts | Quarantine/fail-closed outcome per accepted contract | Clean database open used as parity proof |
| RL-16 | Backup/restore with concurrent artifact retention | Coherent canonical cut and complete referenced closure; fenced new authority | Copy main file only |
| RL-17 | Installed-disabled plugin with hostile import-time side effect | Permitted metadata read only; all prohibited counters zero with live observers | Discovery imports or scans implementation |
| RL-18 | Plugin replacement while old work is queued | Old digest/epochs fenced before new activation; no stale callback | Same language object reused across reload |
| RL-19 | Process versus exact WASM/native host at same grant and workload | Equal semantic outcomes; complete engine/helper/copy overhead measured | Narrow wrapper cost hides engine/children |
| RL-20 | WASM clock/random/filesystem imports and unsupported engine features | Denied or explicitly admitted/versioned deterministic inputs; fail closed | “WASM” label treated as full sandbox proof |
| RL-21 | Slow view plus heavy store/checkpoint/GC load | Canonical control path remains within admitted bounds or trial fails | View or synchronous operation hides queue delay |
| RL-22 | Core-only offline boot and independent clean rebuild | Release/boot identity and dependency closure, with all optional capabilities absent | Auto-install or download hidden in startup |
| RL-23 | Same external plugin against reconstructed core | Unchanged artifact/contract passes literal transcripts and disabled trace | Port silently rewrites plugin API |
| RL-24 | Held-out reconstruction ambiguity and reviewer repair | Clarification/re-derivation minutes and defects counted against ceiling | Omitted behavior or hidden author help |

Each vector needs a valid positive control and an adverse mutation. An observer seeing nothing is not a passing negative test. `RL-*` names are proposal-local and must map into the existing corpus rather than create a competing accepted namespace.

## Evidence return contract

For each arm/profile retain:

- Source, build, toolchain, SDK, dependency, corpus, oracle, injector, workload and analysis digests
- Exact scope and authority reference, timestamped run identity, host and filesystem profile
- Intended/actual offered load, input bytes, expected literal output, observed raw bytes/refusal, first divergence and trace-validation result
- Independent acknowledgement witness, cut identifier, recovered event sequence/state/receipt/outbox and CAS closure
- Result class: accepted bounded evidence, divergent, unsupported, unknown, blocked, invalid trial or out-of-scope, with explicit reason and no denominator erasure
- Whole-group metric dictionary and raw observations, censored/failing attempts and collector overhead
- Rebuild, SBOM/license/provenance and boot evidence; reconstruction/review/rework ledger
- Independent reviewer identity, exact reviewed artifact digest, findings, unresolved non-proofs and Owner disposition

A stopped or failed run returns its evidence too. The next executor must not present its own PASS as acceptance. The independent reviewer must assess the exact final candidate, including changes made in response to review.

## Stop conditions

Stop dependent execution on missing authority, a wrong/non-disposable target, non-synthetic/private data, hash/host drift, absent oracle or ACK witness, unintended provider/account/network action, unapproved reset, correctness counterexample, unresolved authority ambiguity, budget exhaustion or the preregistered stopping boundary. Preserve raw evidence before any repair. Do not change expected bytes or remove adverse fixtures to obtain a pass. A valid repair creates a new candidate identity and receives new independent review.

## Non-proofs

This handoff was not run. It proves no conformance, performance, durability, isolation, release readiness, route qualification, kernel freeze, migration success, legal clearance or bounded porting cost. It chooses no stack and creates no local, Codex, Work or cloud execution session.
