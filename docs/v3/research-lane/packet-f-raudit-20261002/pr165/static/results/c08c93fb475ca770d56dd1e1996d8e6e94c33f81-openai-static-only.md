# Capability-composition conformance verification result

**STATIC-ONLY DRAFT. All five target commands NOT RUN. This is not the separately requested executed verification record.**

- Verifier model + version: OpenAI family; exact runtime model/version identifier unavailable and not guessed. Different family from user-reported implementer deepseek-v4.1-flash.
- Session / operator: Independent Packet F source review of PR165.
- Date (YYYY-MM-DD): 2026-10-02 (UTC).
- Candidate repository + commit SHA: NotSkittlz/recursion, c08c93fb475ca770d56dd1e1996d8e6e94c33f81; PR165 unmerged head expressly authorized by the user at 21:45:57 UTC. Original merge-pin request was superseded; open PR status is not an acceptance blocker.
- Fresh checkout? (yes/no): NO. Authenticated pinned source retrieval only.
- Provenance (source/candidate hashes from results/*.json): Existing committed result fields inspected, not independently regenerated. Seven recorded source/gate hashes reconcile to exact LF Git content or CRLF conversion; see provenance-notes.md. All 24 retrieved evidence files match their Git blob SHAs. Complete per-case provenance is missing.
- Disposition vs failures (results/*.json disposition / failures): rpc_results.json and rpc_invariants_results.json each contain PASS with []; rpc_observed_results.json contains [] but no disposition. These are author-committed claims, not this reviewer's runtime results.

## Commands run (raw outputs inline or attached)

```text
NOT RUN:
python -X utf8 -B experiments/v3/capability-composition/check_suite.py
python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_fixtures.py
python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_invariants.py
python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_observed.py
python -X utf8 -B tools/check_public_scope.py

There are no target runtime outputs from this static review.
```

Native file-reading/hash checks are documented in provenance-notes.md and are not substitutes for these commands.

## Per-check disposition

The first eight rows retain the supplied template checks. Supplemental rows cover the additional current procedure and the user's explicit conditions.

| Check | PASS / FAIL / NEEDS-EVIDENCE | Note |
|---|---|---|
| check_suite.py passes (structure, ids, sources) | NEEDS-EVIDENCE | NOT RUN. Exact ID token matching is present by inspection. |
| check_rpc_fixtures.py passes (F01/F11/F12) | NEEDS-EVIDENCE | NOT RUN. Finite byte anchors are visible; fidelity gaps documented. |
| check_rpc_invariants.py passes (F02–F10, F13–F16) | NEEDS-EVIDENCE | NOT RUN. Current file covers F02/F03/F05/F07–F10/F13/F15; observed cases moved to a separate lane. |
| Oracle/expectations do not import or derive from the candidate | PASS (static dependency boundary only) | No candidate imports/dynamic import/eval/exec in oracle.py, rpc_expected.py or observer.py. This does not establish independent observation everywhere. |
| Every planted mutant is detected | FAIL (static requirement) | Six unique functions unreferenced, four earlier definitions shadowed; F06 forged mutant has no differing asserted field; F16 silent check confounded. All runtime detections remain NOT RUN. |
| Fidelity to report §9 (literal specimen / predicates) | NEEDS-EVIDENCE | Partial source fidelity. F06 always-refuse control and F16 split-artifact counterexamples; F11 exact core/provenance/reason and F12 role/wrapper coverage absent. |
| F17/F18 marked deferred, not counted as passes | PASS (static) | Explicitly deferred and absent from behavioral invocation sets. |
| Non-proofs preserved (no over-claims) | PASS for empirical scope; scope prose needs repair | No model-quality/latency/security/qualification proof. Several conformance/provenance summaries still overstate assertions. |
| check_rpc_observed.py passes (F04/F06/F14/F16) | NEEDS-EVIDENCE | NOT RUN. Real harness events exist, with remaining control/artifact-binding gaps. |
| tools/check_public_scope.py passes | NEEDS-EVIDENCE | NOT RUN. Its result would cover only known-identifier scanning. |
| Per-case provenance, invoked set and disposition | FAIL | Observed JSON lacks hashes/invoked set/disposition; other lanes have global rather than complete per-case evidence. |
| F12 wrapper/role and invariant absent/null work not overclaimed | NEEDS-EVIDENCE | Open items/partial labels are explicit, but “Q02/Q05 addressed” and broad provenance/fidelity summaries need narrowing. |

## Residual unknowns

- Actual results of the five commands, interpreter/platform, fresh checkout state and runtime mutant outcomes.
- Complete mutant inventory enforcement and candidate-level positive/negative controls.
- Binding the independent grade to actual submitted F16 bytes.
- Required per-case hashes/observations/dispositions and EOL-aware reproduction.
- F12 wrapper/role coverage; strict absent-versus-null matching in invariants; F14/F16 strict record coverage.
- Full report §9 predicate fidelity beyond the finite strengthened subset.

Detailed evidence and falsifiers: report.md and mutant-audit.md, with pinned source URLs/lines. All proposed falsifiers are static, unexecuted.

## Reviewer identity and decision

- Decision: **NEEDS EVIDENCE**, provisional static-only disposition.
- Rationale: Source inspection confirms material strengthening but also concrete failures of the universal mutant and per-case provenance requirements, plus observation/control gaps. No target gate was run by this reviewer. A separate formal executed verifier result can supersede this provisional runtime status, but successful stock gates alone cannot remove missing assertions.
- Owner acceptance: Not claimed.
- Repository branch / commit / push: None performed by this static reviewer. This draft is stored outside the target repository and is not a claimed completed verification commit.
