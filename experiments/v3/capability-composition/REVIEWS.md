# Capability-composition suite — reviews and follow-ups

Two independent reviews of the R-PC suite at candidate `050936e` (2026-10-02). They answer different
questions and **both stand**.

| Review | Kind | Verdict | Artifact |
|---|---|---|---|
| GLM-5.3-Flash | Runtime verifier (fresh clone, executes gates) | **ACCEPT** — all four gates pass; results byte-reproduce; oracle independence, mutant detection, no-flag-trust, report fidelity, deferral honesty, non-proofs | `results/050936e…-glm-5.3-flash.md`; `VERIFICATION.md` |
| Astra | Static quality reviewer | **ISSUES FOUND / NEEDS EVIDENCE** for full F01–F16 specification fidelity | `../../docs/v3/research-lane/core-language-control-surfaces-20261002/pr160/report.md` + `VERIFIER-RECONCILIATION.md` |

**Reconciliation:** the runtime verdict covers **rerunning the current suite**; it does not establish
that the gates assert every advertised F01–F16 predicate. Independent expected labels are not
independently observed behavior. Treat this suite as a **scaffold**.

## Open findings (Astra Q01–Q10)

- Q01 F01/F11/F12: manifest/provenance bytes, span map, wrapper/role coverage never asserted.
- Q02 F04/F06/F14/F16: "observations" are candidate-returned fields, not independently observed
  boundary events (e.g. `sink_events`, `rechecked_rights`).
- Q03 F09: no ABA revision history.
- Q04 F10: no governor/successor-adoption history.
- Q05 F02/F08/F14/F15: subset matching admits missing/incorrect evidence (absent vs null).
- Q06 F03: fit calculation, not closure-graph fidelity.
- Q07 F15: selected-provider value unchecked.
- Q08 F05/F07/F13: missing interventions (permutations, preference history, valid-current controls).
- Q09 F02: identity framing ambiguous.
- Q10 bookkeeping: per-case provenance; structural gate substring matching; UI-01 wording.

## Repair order (from the review)

1. Harness-owned observer boundary + instrumented sinks + required-field/schema checks.
2. Finite missing state models (closure graph/versions/loss; ABA cut; governor compare-and-install;
   validated provider selection; preference/current-rights histories; exact route binding).
3. Reconcile status + per-case provenance; distinguish complete/partial/deferred.
4. Keep F17/F18 pending.
5. Independent runtime acceptance by the separate verifier.

## Strengthening progress

- **Slice 2 (2026-10-02):** harness-owned observation boundary (`run/observer.py`) + strict result
  schema (`run/record_schema.py`) applied to **F04/F06/F14/F16** in a new gate
  `run/check_rpc_observed.py` — Q02/Q05 addressed for those fixtures; the weak duplicates were
  removed from `check_rpc_invariants.py`.
- **Slice 3 (2026-10-02):** finite state models for **F03** (dependency-graph closure, exact
  versions, before-use loads, loss record), **F09** (ABA revision capture + bounded retry), **F10**
  (governor compare-and-install over a transition history), **F13** (admitted route registry),
  **F15** (validated provider selection), **F05** (coalesced preference history + positive control)
  and **F07** (permutation-invariant authorized override); stronger one-field mutants.
- **Slice 4 (2026-10-02):** F01/F11/F12 now assert exact spans + byte-complete coverage, an
  independent canonical manifest (plus a literal byte anchor), and the prefix plan, with
  empty-span/invalid-coordinate/gap mutants detected (Q01); F02 uses length-prefixed identity framing
  with a collision mutant (Q09); both lane gates record per-case provenance (source hashes, invoked
  case set, disposition) and `check_suite.py` uses exact id-token matching (Q10).
- **Independent verification (GLM-5.3-Flash, candidate `c08c93f`):** **ACCEPT** — all five gates
  pass; oracle/observer/expectation/schema modules candidate-free (no dynamic import/eval); all
  exercised mutants detected including event-level ones; F04/F06/F14/F16 assert on harness-recorded
  events (F16 turns a candidate "pass" into `HOLD_UNKNOWN` under real observer loss); F01
  span/manifest/prefix triangulated against §9 and the literal anchor; provenance present; open items
  honestly partial; non-proofs preserved. Residuals: **R1** stale committed results hashes, **R2** 6
  dead + 4 shadowed mutants, **R3** observed-lane record missing provenance/disposition — all fixed
  in the cleanup commit. **R4** the oracle manifest mirrors the candidate's construction (independence
  rests on the literal anchor); **R5** inherited partial fidelity.
- **Second independent verification (OpenAI Codex GPT-6, candidate `c08c93f`):** **REJECT** the
  strengthened-conformance claim, with executed probes — F06 accepted an always-refusing candidate
  (the positive control bypassed `M.admit_import`), F16 accepted an empty recorded completion when
  the returned text looked valid, and F11 accepted corrupted changed-core bytes. This was correct:
  a rerun-only ACCEPT is insufficient without adversarial-candidate probes.
- **Slice 5 (2026-10-02):** closes the confirmed counterexamples — F06 now exercises the authorized
  path **through the candidate** and asserts admitted effect + useful completion (always-refuse and
  quiet mutants detected); F16 binds the returned artifact to the **recorded completion payload**
  (hash/length) and derives validity from it, with an empty-completion-but-valid-return mutant and an
  all-refusing candidate exercised; F11 asserts **exact changed-core bytes** and detects a corrupt-core
  mutant. Also: strict (key-present) matching in the invariants lane; per-case result records;
  `check_suite.py` now **fails closed** on any unreferenced mutant.
- **Still open:** F12 wrapper/role coverage; fuller per-case provenance in the observed/fixture lanes;
  strict absent-vs-null across every case. The Slice-5 commit changes tested code, so another
  independently pinned verification is required.

## Rule

Any change to the tested files (`reference_model.py`, `oracle.py`, `rpc_model.py`, `rpc_expected.py`,
`rpc_mutants.py`, `fixtures/*`, or the two lane gates) **restarts verification**: a changed candidate
invalidates the exact-candidate ACCEPT. Affected fixtures should be labelled `partial` until the
missing predicates are asserted.
