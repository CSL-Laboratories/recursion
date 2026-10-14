# PR165 static and executed evidence reconciliation

Date: 2026-10-02. Candidate: [PR165 head c08c93fb475ca770d56dd1e1996d8e6e94c33f81](https://github.com/NotSkittlz/recursion/commit/c08c93fb475ca770d56dd1e1996d8e6e94c33f81), tree **8fff857136c362ab0c3abd7bfa84306ae4d9f4e8**. This is a user-authorized pre-merge head review, not a synthetic merge or a merge action.

## Final verification decision

**REJECT the strengthened-suite conformance-evidence claim. All five prescribed commands passed.** These statements concern different predicates: successful execution of the current tests versus whether those tests can reject the faults they claim to detect. The independently executed report is the authority for the new observations; this reconciliation does not create additional test results.

Execution evidence, locally observed by the Sentry verifier on 2026-10-02: [executed-result summary](executed/RESULT-SUMMARY.md), with the full exact committed report attached separately in chat. Source-inspection evidence, retrieved 2026-10-02: [static report](static/report.md), [mutant audit](static/mutant-audit.md), [provenance audit](static/provenance-notes.md), each with immutable repository citations.

| Check | Static conclusion | Executed result or retained limitation |
|---|---|---|
| Five required commands | NOT RUN in static review | All five exited 0, with recorded outputs and environment |
| F06 useful authorized behavior | Positive control bypasses candidate; always-refusing counterexample predicted | Always-refusing candidate still passed F06 |
| F16 completion authenticity | Graded return was not bound to emitted completion bytes | Empty recorded completion with valid-looking returned artifact still passed |
| F11 changed-core bytes | Exact changed-core bytes incompletely asserted | Corrupted changed-core bytes still passed |
| Mutants | Six live functions uncalled, four earlier declarations shadowed; nondiscriminating checks | Invocation audit confirmed incomplete coverage and two invoked mutants without distinguishing assertions |
| Provenance | No complete per-case source hashes/invoked set/dispositions | Fresh results still lacked the claimed per-case provenance |
| Oracle dependency separation | Source-level separation supported, predicate completeness not established | Source/AST checks support structural separation; this is not proof of independent authorship or adequate observations |
| F12 and invariants missingness | Wrapper/role coverage and strict absent/null matching open | Still unresolved; not upgraded to conformance |
| Product claims | No quality/latency/security/qualification conclusion | Non-proofs preserved; bounded synthetic verification only |

The static report's provisional NEEDS EVIDENCE was appropriate before execution. The executed REJECT now governs the requested verification at this exact candidate. Neither report authorizes merging, and neither is a verdict on deployed product security.

## Exact report and commit custody

- Report filename: `c08c93fb475ca770d56dd1e1996d8e6e94c33f81-openai-codex-gpt-6.md`.
- Report SHA-256: **e7a81f8a78ee20b56e7fdcdf6be46f914eff97b079d0e989e8567a7019128321**.
- Local report commit: **ab412249e129dbe6a01f48b3da6fc88ba2369468**.
- Local branch: **loop/work/verify-capcomp2-20261002**.
- Fresh clone: `C:\Users\Lucas\Documents\Codex\2026-10-02\task\recursion-pr165-review-c08c93f-20261002`.
- Repository-relative report: `experiments/v3/capability-composition/results/c08c93fb475ca770d56dd1e1996d8e6e94c33f81-openai-codex-gpt-6.md`.
- Only that report was committed. Original tracked bytes were unchanged; generated result files were preserved externally and restored; working tree clean; **no push**. These are the completed verifier's custody observations, not a claim that this local commit exists on GitHub.
- Model family: OpenAI Codex, runtime-described as based on GPT-6. Exact variant/snapshot metadata was unavailable and is not invented. Different family from the user-reported DeepSeek V4.1 Flash implementer; implementer identity was not independently attested.

The execution used a fresh clone and targeted checks on the same frozen head. Source-file hashes can vary with checkout LF/CRLF representation; the report records checkout and byte identities rather than calling every EOL mismatch stale code. The exact report was uploaded and hash-checked by the producing verifier and attached separately. This chat was denied retrieval into the ZIP by selected-file scope; no alternate retrieval was attempted. The ZIP summary is not a verbatim copy and does not claim an independent local byte comparison.

## Proposed next acceptance boundary

**Recommendation, inferred:** correct the candidate-level useful controls and submitted-output binding, resolve active/shadowed mutant inventory, and emit complete case-level provenance before requesting a new strengthened-conformance acceptance. Keep the already disclosed partial/deferred obligations visible. Any tested-code change requires a new candidate identity and corresponding independent verification; this packet neither edits that code nor starts another task.

**Competing option:** accept a strictly narrower documentation-only claim that the exact existing gates ran, while explicitly withholding the seven-check conformance claim and retaining the identified defects as unresolved. This does not turn a false-positive gate into a valid conformance gate.

**Decisive falsifier:** a deliberately nonconforming candidate can still pass the claimed F06/F16/F11 check, a declared active mutant lacks a discriminating assertion, or the evidence record cannot identify the actual case, invoked sources and disposition.

**Non-proof:** even repaired, independently passing synthetic tests would not establish model quality, latency, resource or cache gains, operating-system isolation, production durability, security qualification, external-effect truth, or Owner acceptance.

## Relation to architecture correction

This result is additional evidence for the architecture's requirement to map each claimed invariant to an independent observed predicate and meaningful positive/adverse controls. It is not a replacement for the older M1 baseline audit or the integrated FU-03 gate. The three source pins in README remain deliberately separate.
