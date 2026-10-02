# Reconciliation with the independent verifier summary

Received from Lucas on 2026-10-02 after the static review was completed. Read this alongside `pr160/report.md`. The original static report remains unchanged.

## Evidence available

Lucas supplied the verifier's completion summary for local commit `3614e9a` on `loop/work/verify-capcomp-20261002`. He reports it is neither pushed nor merged. The verifier identifies itself as GLM-5.3-Flash, model identifier `cheaper-inference/glm-5.3-flash`, reviewing merge `050936e05f5523da0eb8dc2f2fa3a65e4798af26`. That is the same tree reviewed in this packet at candidate `b5d5af7d1a765bc9617c7238a64cd6a313ca7096`.

The full local verification report, logs and commit were not available for inspection here. Its run results are user-supplied reported evidence, not this review's execution observations. No additional target execution was performed to write this reconciliation.

## Where the reviews agree

- The verifier reports all four existing gates passing and byte-reproduction of both committed result JSON files. This adds reported independent reproducibility evidence for the existing suite.
- The exact 23-byte specimen, core-change offsets and two-byte Unicode unit agree with this static review.
- Literal expectations and the byte oracle do not import or derive expected output from the candidate. The static review already recognizes this genuine implementation separation.
- The supplied planted mutants are detected according to the verifier. The static review does not claim that these current mutants survive. It questions whether their fault coverage is sufficient.
- F17/F18 are deferred, and the documentation preserves important non-proofs and the distinction between verifier recommendation and Owner acceptance.
- The verifier explicitly recognizes that F01's manifest bytes are never exercised and identifies provenance/version/prose residuals. Those reinforce the static review's coverage and bookkeeping findings.

## Material disagreements remain

### Independent expectations are different from independent observations

The verifier reports no candidate-flag trust because the gate compares candidate output with literal expected subsets. That establishes expected-value separation; it does not establish that the returned event/effect records come from an independent observer.

For example, `rpc_model.admit_import` creates its own empty `sink_events` list and returns it. `restore` returns `rechecked_rights=True` without receiving current rights as input. A gate can correctly compare these values to literals while never observing a protected sink or a rights recheck. See static finding Q02 and its pinned source links. A separate provider-free fake sink or instrumented boundary could supply that missing evidence without implementing or running the full product.

### F12 tests Unicode units but not the returned span map or role coverage

The renderer's third return value is never asserted by `check_rpc_fixtures.py`; F01 binds it to `spans_a`, and other calls discard it. Returning an empty span list while keeping the current bytes and ranges unchanged leaves the inspected assertions unchanged. This is a static control-flow deduction, not a newly executed mutant. F12's wrapper, coverage and authority-role predicates therefore remain unestablished. See Q01.

### F09 and F10 do not encode the full promised histories

F09 compares a configuration/schema lookup in one supplied dictionary. It has no revision history to distinguish an ABA transition. F10 compares one candidate parent string to one supplied current parent and returns an installation result. It does not represent concurrent proposals, a governor or successor adoption over a transition history. Correct outcomes for their current examples are compatible with these missing properties. See Q03 and Q04.

### Detecting every supplied mutant is not complete predicate coverage

The verifier's manual F04 mutant reproduction demonstrates that a conspicuous returned byte/event mismatch is detected. It does not establish disabled source-read and registration inertness at an independently observed boundary. Similarly, a literal subset matcher says nothing about omitted fields, including absent-versus-null distinctions discussed in Q05.

## Recommended interpretation

Retain the verifier's **reported PASS for rerunning the current suite**, while keeping **ISSUES FOUND / NEEDS EVIDENCE for full F01–F16 specification fidelity**. These conclusions address different questions and can both hold. The broad fidelity ACCEPT should be narrowed or justified against the concrete omitted predicates before it is used as full conformance evidence. Neither review is Owner acceptance.

## Bounded questions for the local verifier

1. Identify the exact assertion that fails if the renderer returns an empty span map with unchanged bytes and ranges. If none exists, explicitly mark F12 span/role coverage partial.
2. Identify where F09 represents and rejects ABA revisions, and where F10 represents one-governor successor adoption. If absent, mark the modeled subsets and add histories in a separately admitted follow-on.
3. Trace the provenance of F06 sink events and F14 rights evidence. Are they independently captured boundary observations or fields returned by the candidate?
4. Separate supplied-mutant detection from report-predicate completeness. List untested obligations and candidate-output fields that the subset matcher ignores.
5. Reconcile gate check counts, unique fixture cases, expected-record counts and mutant-function counts as separate metrics; passing assertion totals should not stand in for coverage.

This is a proposed follow-on review checklist, not an instruction to run or modify any candidate. No need to discard the reproducibility result or the small readable harness: preserve both and tighten the scope of the claims.
