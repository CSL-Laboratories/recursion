---
title: Packet-E return + PR #160 review — intake synthesis (2026-10-02)
status: research-evidence-not-a-decision
last-updated: 2026-10-02
---
# Packet-E return + PR #160 review — intake synthesis

Research evidence only — not adoption, not execution. Source: the hash-verified intake here
(`check_intake.py`). This packet returns the two lanes commissioned by
`docs/v3/prompts/dot-research-20261002e/` (**R-LANG**, **R-CS**) **plus** the separately requested
static review of the R-PC conformance suite (PR #160) and its reconciliation with the runtime
verifier. Repository baseline for the two research reports: `699a29d` (PR #159); the PR #160 review
uses `b5d5af7` / merge `050936e`. **All proposed target experiments are unexecuted; no technology is
selected.**

## Delivery identity

| File | SHA-256 |
|---|---|
| `README.md` | `c574ed50…` |
| `VERIFIER-RECONCILIATION.md` | `ec3a8071…` |
| `rlang/report.md` | `9e26e2bf…` |
| `rlang/local-session-handoff.md` | `6807f3f8…` |
| `rcs/report.md` | `f913554c…` |
| `rcs/bounded-handoff.md` | `915fe93f…` |
| `pr160/report.md` | `846106dd…` |

7 files sha256-verified.

## 1. R-LANG — core language, runtime and reconstruction (advisory)

- **Candidates** fairly compared: Rust, Go, C++, C#/.NET, Java/Kotlin-JVM, TypeScript/Node, CPython
  (GIL vs free-threaded as separate arms). **No winner selected**; the evidence "does not select".
- **Shortlist (advisory):** retain **Rust core + typed adapters**, **Go owner-loop** backend/core, and
  **both CPython reference arms**; add **C#/.NET** as the first expanded challenger; keep JVM as
  managed-backend challenger; keep C++ and TS/Node as challenge arms with explicit re-entry
  conditions.
- **Store/replay:** the durable commit unit (ordered events + state/high-water + receipt + authority
  epoch + outbox + referenced CAS artifacts) is language-neutral; the two equal competitors remain
  **JS1** (segmented append-only sequence + CAS) vs **SQL1** (embedded WAL + CAS). No engine selected.
- **Plugin host:** process/IPC, WASM component, native FFI, remote protocol compared; **P1** requires
  the plugin/edition contract to be language-neutral — *no native pointers, object addresses, runtime
  type names, stack traces, exception classes, host callbacks or mangled symbols as authority*.
- **Reconstruction spec:** 15 **neutral units** (authority/topology; state/transitions; invariants;
  four-function API; schemas/wire; canonical bytes/digests; refusal alphabet; concurrency/fencing;
  persistence/recovery; effects/adapters; plugin/edition interface; context/composition; release/build
  identity; evidence package) with an explicit split of what may stay language-specific, plus **A1**
  (author an *unresolved-seam register*). **RC-01…RC-09** acceptance criteria; "bounded" = a ceiling
  fixed *before* reconstruction. D-039 falsifier retained.
- **Handoff:** admission prerequisites, **RL-01…RL-24** synthetic vectors, evidence-return contract,
  stop conditions; authorizes no session/execution.

## 2. R-CS — control-surface and competitor study (advisory)

- **Anchor:** T3 Code **v0.0.45** (pin `6c8fed3…`), with peers Happy, HAPI, Vibe Kanban, Conductor,
  and native baselines (Codex App Server, Claude Code Remote Control, Agent Client Protocol v1).
- **T3 specifics (source-documented):** restart-based agent-session refresh (stop provider process,
  retain conversation, resume fresh) rather than hot reload; disabled Claude skills omitted from the
  composer (universal manifest-before-import/inertness unknown); Live Activity ~10-minute stale
  window; per-thread notification stacking; **credential-fingerprint** OpenCode Go limit dedup;
  Expo SDK 58 / RN 0.88 RC mobile stack; Codex 0.159 protocol bindings regenerated.
- **Differentiation:** routing/multiple clients/plugins/attention inboxes are **table stakes**; D-034's
  moat is the *combined* deterministic composition + authority + productive continuity + honest
  accounting + portability contract, each with an honest claim boundary and a **decisive falsifier**
  (a strong baseline matching authorized outcomes/evidence at equal/less operator effort at the frozen
  budget). Licensing/counsel questions recorded; **G-32 remains open** (no conclusion).

## 3. PR #160 review — Astra static quality review + verifier reconciliation

Two independent reviews of the R-PC suite at `050936e`:

- **Runtime verifier (GLM-5.3-Flash):** all four gates pass on a fresh clone and byte-reproduce the
  committed results; oracle independence, all 16 planted mutants, no-flag-trust, report fidelity,
  deferral honesty and non-proofs all **PASS** → **ACCEPT** (recorded in the suite's `VERIFICATION.md`
  via PR #162).
- **Static reviewer (Astra, `pr160/report.md`):** good scaffolding, but **ISSUES FOUND / NEEDS
  EVIDENCE** for full F01–F16 specification fidelity. Findings **Q01–Q10**:
  - Q01 (High) F12 span/wrapper/role coverage never asserted; F01 skips manifest/provenance bytes.
  - Q02 (High) semantic "observations" are fields the candidate returns, not independently observed
    boundary events (e.g. F06 `sink_events`, F14 `rechecked_rights`) — *independent expected labels
    are not independently observed behavior*.
  - Q03 (High) F09 has no ABA revision history.
  - Q04 (High) F10 has no governor/successor-adoption history.
  - Q05 (Medium) subset matching admits missing/incorrect evidence.
  - Q06 (Medium) F03 is a fit calculation, not closure-graph fidelity.
  - Q07 (Medium) F15 selected-provider value unchecked.
  - Q08 (Medium) F05/F07/F13 omit intervention inputs (permutations, preference history, valid-current
    positive controls).
  - Q09 (Medium) F02 identity framing ambiguous.
  - Q10 (Low) provenance/bookkeeping (per-case records, structural-gate substring matching).
- **Reconciliation (`VERIFIER-RECONCILIATION.md`):** both can hold — the runtime verdict stands for
  **rerunning the current suite**, while full F01–F16 specification fidelity is **not** established.
  Five bounded questions for the local verifier and a proposed repair order.

## 4. Net status and follow-ups

- **R-PC suite:** rerun/reproducibility **ACCEPT** (GLM-5.3-Flash); full-spec fidelity **ISSUES FOUND /
  NEEDS EVIDENCE** (Astra). Treat the suite as a **scaffold**: label affected fixtures `partial` or
  strengthen them before citing green results as full conformance.
- **Ordered repairs (from the review):** (1) harness-owned observer boundary / instrumented sinks and
  required-field checks; (2) finite missing state models (closure graph/versions/loss, ABA cut,
  governor compare-and-install, validated provider selection, preference/current-rights history, route
  binding); (3) reconcile status + per-case provenance; (4) keep F17/F18 pending; (5) independent
  runtime acceptance by the separate verifier. Any change to tested files **restarts verification**.
- **D-039 outline:** revise `docs/v3/architecture/LANGUAGE-AGNOSTIC-ARCHITECTURE.md` (PR #161) to adopt
  R-LANG's 15 neutral units, the three distinct byte contracts, the three single-writer boundaries,
  the **P1** plugin contract, `ReleaseId`/boot identity, and **RC-01…RC-09** plus the unresolved-seam
  register.
- **Next lanes:** R-UI **UI-05** must assert resolved qualified keys/`NodeKey`/canonical order from an
  independent observation (not a candidate `collision_free` boolean); correct the UI-01 wording
  ("100 baseline runs + 24 permutations = 124", not "24×100"). R-MOB two-path simulation per the
  review's minimum trace.

## 5. Where this sits

- Index: `RESEARCH-INDEX.md`. Gaps: `GAPS.md`. Runbook: `NEXT-PHASE.md`.
- Suite reviews: `experiments/v3/capability-composition/REVIEWS.md`.
- No technology selected; no protected path edited; `unknown` never treated as zero.

## Boundary

Unexecuted; research evidence only. No legal conclusion (G-32 open). No adoption.
