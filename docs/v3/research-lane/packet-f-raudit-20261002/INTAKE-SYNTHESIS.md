---
title: Packet-F R-AUDIT return + PR #165 review — intake synthesis (2026-10-02)
status: research-evidence-not-a-decision
last-updated: 2026-10-02
---
# Packet-F R-AUDIT return + PR #165 review — intake synthesis

Research evidence only — not adoption, not execution. Source: the hash-verified intake here
(`check_intake.py`). This is the return for the **R-AUDIT** lane of `docs/v3/prompts/dot-research-20261002f/`
plus the separately requested PR #165 review. **R-PLAN and R-OPS have not been run.** Baseline: the
audited outline is PR #161 head `c7c5b1e`; correction sources are pinned to master `9f4122b`; the
PR #165 review targets `c08c93f`. All target tests are unexecuted except the separately authorized
PR #165 execution.

## Delivery identity

21 delivered files, all sha256-verified (see `SHA256SUMS.txt`): `README.md`,
`DELIVERY-VERIFICATION.md`, `INTAKE-SYNTHESIS.md`, `ARCHITECTURE-REVISION-GUIDE.md`,
`corrections/report.md` + `source-manifest.json`, `corpus/report.md` + `source-ledger.json`,
`language/report.md` + `source-ledger.md` + `local-verification-handoff.md`, and `pr165/`
(executed summary, bundled full executed report, reconciliation, static report + mutant
audit + provenance notes + retrieval manifest + source hashes).

## 1. Corrections register (A1–A10) — applied to the outline next

Each item corrects a claim in the D-039 outline; the report supplies paste-ready replacement text
(inferred/proposed). Two of the review's own premises are also corrected:

| # | Disputed outline claim | Corrected fact (source) |
|---|---|---|
| A1 | "FU-03 crash-cut … F20-A–G" | **F20-A…F20-G is real** — it is the R-SC Q20 receiver-fence/permission vector family, **not** FU-03. FU-02 = cutpoints P00…P19 + cells F01…F42; FU-03 = named prefixes; Stage B = SB-01…SB-26. Use distinct namespaces. |
| A2 | "`tools/check_v3_orientation.py --contracts` passes" | It is the **2026-09-13 orientation-repair gate** with a fixed allow-list; a current tree fails its scope predicate. Label `V3_ORIENTATION_PASS_NOT_RELEASE_ACCEPTANCE`; cite exact M1 corpus + receipts instead. |
| A3 | "G-20 configuration lifecycle" | **G-20** = v2-vs-Options benchmark / R17 ablation / PERF-003 disabled-plugin matrix; configuration is **G-09**. |
| A4 | "(six authority dimensions)" | M1B has **nine** additional permission dimensions plus functional capability and workspace scope = **eleven** grant dimensions in `PluginGrantDimensions@1`. |
| A5 | "`rlc-candidate/0`" | **Unsupported** in inspected sources. Real direction = the R-L shared CLI/TUI/GUI interface platform (specification candidate). Remove the identifier. |
| A6 | "observability as a deterministic query over an authenticated cut" | R-V **proposes** this (candidate research, unimplemented); cite `SourceCut`/`ExplainResult`/`rv-candidate-contracts-1`, not an adopted contract. |
| A7 | "FU-02 three-way replay parity (O1)" | O1 **is** three-way: two store finalists (JS1 RFC 7464 JSON-seq + CAS; SQL1 SQLite WAL + CAS) vs a reviewed normative corpus. The shared CAS is a common-mode risk component, **not** a third finalist. +O2…O5. |
| A8 | "plugin contract (D-035)" | D-035 is the **repository-topology** principle. The plugin contract is **`PluginManifest@1`** + grant/lifecycle/resolution records in M1B; R-A's schema is a proposal. |
| A9 | "D-028 resolved platform floors" | D-028 resolves **desktop/backend** floors only; **mobile floors are open** (Android min/target API; iOS OS/device classes). D-036 governs offline; D-038 opt-in. |
| A10 | MOB-F01…F24 and MP01…MP11 listed as one corpus | D-039 requires them **reconciled into one suite** (one semantic oracle; per-platform stimuli may differ). Do not add 24+11 or double count. |

**Hedges (mark candidate, not settled):** H1 command key `(admitted_authority_domain, command_id)`
is proposed; H2 the seven capability facts are independent scoped dimensions, not a lifecycle; H3
the R-SC shared fields are recommended integration candidates. **Stale-status fixes:** S1 there
**are** scoped historical receipts (Session A `74fb18d` ACCEPT; Stage B `d56cc44` 16/3/5/2), so
"everything unrun" is wrong; S2 no production serializer is selected but frozen M1 byte profiles
remain binding; S3 frozen PAUSE is immediate-only and does not settle all newer Stop policies;
S4 separate governance from runtime responsibilities.

## 2. M1 executable corpus and the two discrepancies

- **Inventory:** 71 bundles — M1A `F-M1A-001…023` (23), M1B `F-M1B-001…024` (24), M1C
  `F-M1C-001…024` (24). "Do not call this 71 tests or 71 executions." Enforced by closed-inventory
  fixture tests plus semantic runners (17 `*.test.py`), test-only oracles and generators.
- **Historical results** (source-recorded, not locally observed): M1A `fb0e045`, M1B `0f1bd5f`, M1C
  `8cd1c8e` — same-family PASS receipts; not new evidence.
- **Two source-documented code/wire discrepancies (blockers before freezing the behavioral contract):**
  1. **WorkPacket correlation identity** — `scripts/v3/planning/replay/kernel.py` hardcodes
     `CORR-M1A` where the wire profile requires the derived packet correlation ID to equal its
     `GoalSpec` ID (M1A-WIRE-PROFILE §585).
  2. **WorkPacket effect attenuation** — the constructor copies `goal.payload.allowed_effect_keys`
     where the wire profile requires TaskNode effect fields; a task that narrows effects can yield a
     broader set.
  Both are static (not executed). Resolution is an explicit, versioned amendment preserving old bytes
  and receipts — re-sealing digests alone is insufficient.

## 3. R-LANG-informed checklist (C1–C6)

Six replacement paragraphs for the outline: (C1) record the R-LANG outcome as advisory and note the
intake's "15 neutral units" is actually **14** named rows; (C2) separate neutral semantics (grammar,
presence/nullability, numeric domains, key comparator, canonical bytes, digest preimages, refusal
precedence) from language mechanisms; (C3) make "language L cannot express invariant I" a checkable
witness (language+runtime+toolchain+topology+host), not a whole-language claim; (C4) re-pin
canonicalization/digest KATs per toolchain across four layers (strict admission / canonical bytes /
primitive digest / integrated digest binding); (C5) no-language-leak is a host-qualified obligation
(no pointers/type names/exceptions/callbacks as authority; qualify an engine/config, never "Wasm");
(C6) the integrated gate at admitted bounds is the invariant bar, not an enlarged diagnostic model.
A non-selecting feasibility matrix (Rust/Go/CPython/C#/JVM/TS/C++) records serializer traps
(field omission vs null, float/negative-zero admission, UTF-16 key ordering, framing newline ≠
canonical payload). No language/host selected.

## 4. PR #165 review (executed REJECT + static + reconciliation)

- **Executed (OpenAI Codex GPT-6):** **REJECT** the strengthened-conformance claim; all five gates
  passed. Executed probes reproduced false positives in **F06** (always-refusing candidate passed),
  **F16** (empty completion with valid-looking return passed) and **F11** (corrupted changed-core
  bytes passed). Mutant coverage and per-case provenance incomplete. Report committed at `ab412249`
  on `loop/work/verify-capcomp2-20261002` (not pushed); file
  `experiments/v3/capability-composition/results/c08c93fb…-openai-codex-gpt-6.md`.
- **Reconciliation:** narrow gate-rerun PASS and broader conformance REJECT are distinct predicates;
  the static report's provisional NEEDS-EVIDENCE is superseded by the executed REJECT at that
  candidate.
- **Relation to our work:** the rejected candidate is `c08c93f`, **before** the Slice-5 repair on
  PR #165 (F06 now runs the authorized path through the candidate; F16 binds the return to the
  recorded completion payload; F11 asserts exact changed-core bytes; mutant registry fails closed;
  strict matching; per-case records). Packet F's **proposed next acceptance boundary matches Slice 5**.
  A fresh independent verification of the Slice-5 head is still required.

## 5. Where this sits and next

- Index: `RESEARCH-INDEX.md`. Gaps: `GAPS.md`. Runbook: `NEXT-PHASE.md`.
- **Immediate next:** revise `docs/v3/architecture/LANGUAGE-AGNOSTIC-ARCHITECTURE.md` (PR #161) using
  the correction register, the R-LANG C1–C6 paragraphs, the corpus inventory, and the
  `ARCHITECTURE-REVISION-GUIDE.md` three-blocker separation; then re-review.
- **Not run:** R-PLAN and R-OPS (unowned facets still named only); the original GLM review text was
  unavailable, so A1–A10 were tested directly against primary sources.

## Boundary

Static/source work plus the separately authorized PR #165 execution. No technology selected, no
protected path edited, no legal conclusion, no ratification. `unknown` is never zero.
