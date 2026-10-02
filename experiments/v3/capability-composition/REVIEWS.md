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

## Rule

Any change to the tested files (`reference_model.py`, `oracle.py`, `rpc_model.py`, `rpc_expected.py`,
`rpc_mutants.py`, `fixtures/*`, or the two lane gates) **restarts verification**: a changed candidate
invalidates the exact-candidate ACCEPT. Affected fixtures should be labelled `partial` until the
missing predicates are asserted.
