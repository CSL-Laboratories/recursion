# Capability-composition conformance verification result

**Decision: REJECT the strengthened-suite verification claim at this pre-merge head.** All five exact commands pass, but experimental counterexamples invalidate F06/F16 sensitivity, not every planted mutant is discriminated, and claimed per-case provenance is absent. This is an independent review recommendation, not Owner acceptance or a judgment of product readiness. The narrower gate-rerun result is PASS.

- Verifier model + version: OpenAI Codex, described by the runtime as based on GPT-6. Exact model variant, snapshot, build and backend routing metadata are unavailable; none is invented. Report slug: `openai-codex-gpt-6`. This is a different family from the user-reported implementer `deepseek-v4.1-flash`; that implementer identity was not independently attested.
- Session / operator: Lucas-authorized, one-time independent verification on host `sentry`, source task `01a0fe67-c17a-7798-8cb4-a7c1a86710d7`. No extra tasks, subagents, external reviewers or provider/model calls were launched by this verifier.
- Date (YYYY-MM-DD): 2026-10-02.
- Candidate repository + commit SHA: https://github.com/NotSkittlz/recursion, PR #165 head `c08c93fb475ca770d56dd1e1996d8e6e94c33f81`.
- Candidate tree: `8fff857136c362ab0c3abd7bfa84306ae4d9f4e8`.
- Fresh checkout? **Yes**, independent clone, not an existing worktree: `C:\Users\Lucas\Documents\Codex\2026-10-02\task\recursion-pr165-review-c08c93f-20261002`.
- Parent goal: independently verify the strengthened R-PC scaffold supporting the v3 deterministic core / capability-composition work; five prescribed machine-checkable gates plus the seven requested evidence checks.
- Authority: read `AGENTS.md`, `CONTROL_PLANE.md`, `ANCHOR.md`, `docs/v3/START-HERE.md`, suite `VERIFICATION.md`, `REVIEWS.md`, fixture index, READMEs, `run/RESULT-TEMPLATE.md`, and R-PC report section 9. Lucas's later explicit authorization to review PR HEAD overrides the repository handoff's merge-commit instruction. No synthetic merge was used.
- Provenance: source/Git blob identities and generated result identities are recorded below. Supplied hash fields bind to the locally evaluated files on this run, but the JSON schema does **not** provide the requested per-case provenance.
- Disposition vs failures: fresh `rpc_results.json` and `rpc_invariants_results.json` each say `disposition: PASS`, `failures: []`; fresh `rpc_observed_results.json` says `failures: []` but has **no disposition field**. These are gate records, not this review's decision.

## Commands run (raw outputs inline or attached)

PR metadata was freshly obtained with read-only `gh api repos/NotSkittlz/recursion/pulls/165`. At 21:46:42 UTC, PR #165 was OPEN, `merged=false`, `merged_at=null`, head `c08c93fb475ca770d56dd1e1996d8e6e94c33f81`, base `9f4122b50dc8117b185b2cd52244a10df3eb4285`. The final source recheck at **21:55:30 UTC** returned the same head and unmerged status. Earlier merge-only work paused correctly; the owner's head authorization then allowed continuation. The unmerged synthetic `merge_commit_sha` was never checked out.

```text
git -c core.autocrlf=false clone --no-checkout https://github.com/NotSkittlz/recursion.git recursion-pr165-review-c08c93f-20261002
git -c core.autocrlf=false checkout --detach c08c93fb475ca770d56dd1e1996d8e6e94c33f81
git rev-parse HEAD 'HEAD^{tree}'
c08c93fb475ca770d56dd1e1996d8e6e94c33f81
8fff857136c362ab0c3abd7bfa84306ae4d9f4e8
```

Environment: Windows 11 `10.0.26200`, x64; CPython `3.12.10 (tags/v3.12.10:0cc8128, Apr 8 2025, 12:21:36) [MSC v.1943 64 bit (AMD64)]`; executable `C:\Users\Lucas\AppData\Local\Programs\Python\Python312\python.exe`; Git `2.48.1.windows.1`; GitHub CLI `2.93.0 (2026-05-27)`. No installation was needed. Initial sandboxed GitHub access hit the configured blocked proxy; the authorized read-only lookup/clone succeeded through reviewed network escalation.

The five commands below ran from the repository root at 21:49:10-21:49:11 UTC. Each exited **0** with **empty stderr**. Raw stdout bytes are retained outside the clone; their SHA-256 values follow the transcript. Markdown presents the same text with line endings rendered normally.

```text
$ python -X utf8 -B experiments/v3/capability-composition/check_suite.py
PASS: capability-composition suite structure clean; 73 fixture ids registered across R-PC/R-UI/R-MOB/MP; sources present
exit: 0; stderr: empty
```
```text
$ python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_fixtures.py
R-PC F01 — frozen reproduction
  [PASS] positive control: exact 23 bytes
  [PASS] enumeration order does not change output
  [PASS] ranges equal oracle under pinned profile
  [PASS] exact ranges
  [PASS] output length 23
  [PASS] planted enumeration mutant passes the base case
  [PASS] planted enumeration mutant is detected on reversed enumeration
  [PASS] F01 exact spans
  [PASS] F01 span coverage complete
  [PASS] F01 canonical manifest equals independent oracle
  [PASS] F01 canonical manifest equals literal anchor
  [PASS] F01 prefix plan is first 18 bytes
  [PASS] empty-span mutant keeps bytes/ranges
  [PASS] empty-span mutant detected
  [PASS] invalid-coordinate mutant detected
  [PASS] gap-span mutant detected
R-PC F11 — prefix continuity and required forfeiture
  [PASS] tail-change output equals oracle
  [PASS] tail change preserves the first 18 bytes
  [PASS] tail change keeps offsets
  [PASS] core change yields length 24
  [PASS] core change shifts offsets to [9,19) [19,24)
  [PASS] core change changes the prefix
R-PC F12 — span/role coverage, Unicode byte units
  [PASS] Unicode output equals oracle
  [PASS] Unicode specimen is 25 bytes (2-byte char counted as 2)
  [PASS] two-byte char occupies two byte units
  [PASS] planted codepoint-counting mutant disagrees with byte ranges (detected)
  [PASS] codepoint mutant is detected
  [PASS] F12 span coverage complete
Oracle independence
  [PASS] oracle imports do not include the candidate model
RPC_FIXTURE_PASS F01 F11 F12; oracle independent; mutants detected
exit: 0; stderr: empty
```
```text
$ python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_invariants.py
  [PASS] F02_identity: candidate matches expected
  [PASS] F02_identity: planted mutant is detected
  [PASS] F02_replay: candidate matches expected
  [PASS] F02_replay: planted mutant is detected
  [PASS] F02_missing: candidate matches expected
  [PASS] F02_collision: candidate matches expected
  [PASS] F02_collision: planted mutant is detected
  [PASS] F03: required member loaded before use
  [PASS] F03_valid: candidate matches expected
  [PASS] F03_valid: planted mutant is detected
  [PASS] F03_missing: candidate matches expected
  [PASS] F03_unfittable: candidate matches expected
  [PASS] F03_unfittable: planted mutant is detected
  [PASS] F05_revoked: candidate matches expected
  [PASS] F05_revoked: planted mutant is detected
  [PASS] F05_positive: candidate matches expected
  [PASS] F05_pending: candidate matches expected
  [PASS] F05_pending: planted mutant is detected
  [PASS] F07_hold: candidate matches expected
  [PASS] F07_override: candidate matches expected
  [PASS] F07_override: planted mutant is detected
  [PASS] F07_override_unauthorized: candidate matches expected
  [PASS] F07_override_unauthorized: planted mutant is detected
  [PASS] F07: winner invariant across arrival permutations
  [PASS] F08: candidate matches expected
  [PASS] F08: planted mutant is detected
  [PASS] F09_valid: candidate matches expected
  [PASS] F09_mixed: candidate matches expected
  [PASS] F09_mixed: planted mutant is detected
  [PASS] F09_aba_unstable: candidate matches expected
  [PASS] F09_aba_unstable: planted mutant is detected
  [PASS] F09_aba_recovered: candidate matches expected
  [PASS] F10_first: candidate matches expected
  [PASS] F10_late: candidate matches expected
  [PASS] F10_late: planted mutant is detected
  [PASS] F10_duplicate: candidate matches expected
  [PASS] F10_duplicate: planted mutant is detected
  [PASS] F10_directive: candidate matches expected
  [PASS] F10_directive: planted mutant is detected
  [PASS] F13_valid_current: candidate matches expected
  [PASS] F13_drift_readmit: candidate matches expected
  [PASS] F13_hold_unknown: candidate matches expected
  [PASS] F13_hold_unknown: planted mutant is detected
  [PASS] F15_missing_dependency: candidate matches expected
  [PASS] F15_missing_dependency: planted mutant is detected
  [PASS] F15_cycle: candidate matches expected
  [PASS] F15_cycle: planted mutant is detected
  [PASS] F15_ambiguous_provider: candidate matches expected
  [PASS] F15_ambiguous_provider: planted mutant is detected
  [PASS] F15_exclusive_conflict: candidate matches expected
  [PASS] F15_exclusive_conflict: planted mutant is detected
  [PASS] F15_valid: candidate matches expected
  [PASS] F15_invalid_selection: candidate matches expected
  [PASS] F15_invalid_selection: planted mutant is detected
  [PASS] F15_valid_selection: candidate matches expected
  [PASS] rpc_expected imports do not include the candidate model
RPC_INVARIANTS_PASS F02 F03 F05 F07-F10 F13 F15; mutants detected; expectations independent
exit: 0; stderr: empty
```
```text
$ python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_observed.py
F04 — disabled source-read/registration inertness (observed)
  [PASS] absent: strict schema
  [PASS] absent: no events
  [PASS] disabled: strict schema
  [PASS] disabled: no source read
  [PASS] disabled: no load/selector
  [PASS] disabled: no effect
  [PASS] disabled: zero operational bytes
  [PASS] disabled: only a disabled registration
  [PASS] disabled: leaked-read mutant detected
  [PASS] enabled: strict schema
  [PASS] enabled: registration observed
  [PASS] enabled: source read observed
  [PASS] enabled: body load observed
  [PASS] enabled: operational bytes counted
  [PASS] enabled: no effect
F06 — protected sink + authorized useful completion (observed)
  [PASS] import: refused (no laundering)
  [PASS] import: refusal observed at sink
  [PASS] import: refusal event recorded
  [PASS] import: no admitted effect
  [PASS] import: no useful completion
  [PASS] positive control: authorized useful completion (gate-owned)
  [PASS] positive control: one admitted effect
  [PASS] positive control: one completion
  [PASS] forged-authority mutant detected (no admitted effect)
F14 — restored refs + current-rights recheck + O1 ledger (observed)
  [PASS] missing: rejected
  [PASS] missing: no side effects on rejection
  [PASS] valid: restored refs
  [PASS] valid: rights rechecked for every ref
  [PASS] valid: current fence observed
  [PASS] valid: revoked vs current observed (not candidate-reported)
  [PASS] valid: O1 recorded
  [PASS] valid: no replay
  [PASS] valid: liability held
  [PASS] replay mutant detected at the ledger
F16 — real observer loss, distinct valid outputs, independent grading
  [PASS] missing observer: candidate still claims pass
  [PASS] missing observer: gate yields HOLD_UNKNOWN
  [PASS] missing observer: flagged
  [PASS] valid: two distinct valid outputs
  [PASS] valid: both outputs pass the independent oracle
  [PASS] valid: independent grading yields PASS for both
  [PASS] invalid: FAIL_INVALID
  [PASS] all-refuse: no admitted effect
  [PASS] all-refuse: FAIL_USEFUL_COMPLETION
  [PASS] silent mutant yields HOLD_UNKNOWN
  [PASS] observer.py does not import the candidate model
RPC_OBSERVED_PASS F04 F06 F14 F16; observations independent; strict schema
exit: 0; stderr: empty
```
```text
$ python -X utf8 -B tools/check_public_scope.py
PASS: no other-project identifiers found in repository text files
exit: 0; stderr: empty
```

| Gate | Raw stdout SHA-256 |
|---|---|
| 1 | `b49fc0b22db5cfddcfc56602acbc595e47e9a4162ef8b4ff12b16098c1109400` |
| 2 | `5df70f3b1507772255cf7c5069456cd952c618fd4a31223155362c6c44da32b4` |
| 3 | `48786d1cd940f63ce4d03c794cee83c306f907e197692b8633bda0f2b5c482c8` |
| 4 | `6941400a697a566339ed19cc8f75ee5396e19efbc63257faa3a38c9564fd2c09` |
| 5 | `c91af26810434c3e1da7bc78ef09dbedd4d25d37fd14812504f1b6aae39846d8` |

All empty stderr SHA-256: `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

## Per-check disposition

| Check | PASS / FAIL / NEEDS-EVIDENCE | Note |
|---|---|---|
| `check_suite.py` passes (structure, ids, sources) | PASS | 73 registered IDs; token matching is exact; registration is not execution. |
| `check_rpc_fixtures.py` passes (F01/F11/F12) | PASS | 29 assertions pass; limited byte/span checks, not all section 9 predicates. |
| `check_rpc_invariants.py` passes | PASS | Actual lane: F02/F03/F05/F07-F10/F13/F15; 32 cases, 56 assertions including 21 mutant checks. |
| `check_rpc_observed.py` passes | PASS | Actual lane: F04/F06/F14/F16; 45 assertions; sensitivity defects below. |
| `tools/check_public_scope.py` passes | PASS | Known-identifier scan only; no general security claim. |
| Oracle/expectations/observer do not import or derive expectations from candidate | PASS | Structural source independence verified; not a claim of independent authorship. |
| Every planted mutant is detected | FAIL | Six live mutants uninvoked, four shadowed definitions, two invoked mutants lack discriminating assertions. |
| F04/F06/F14/F16 observe boundary events without trusting candidate flags | FAIL as a blanket claim | Synthetic event observation exists; F06 positive control bypasses candidate; F16 grades unbound returned artifact. |
| Fidelity to report section 9 (literal specimen / predicates) | PASS for specified literal anchor; NEEDS-EVIDENCE for full fidelity | F11 corrupt-core probe passes; F12 role/wrapper coverage remains absent. |
| Per-case source hashes, invoked set, disposition bound to evaluated sources | FAIL | Suite-level partial hashes only; observed JSON lacks even suite provenance/disposition. |
| F12 wrapper/role and invariants strict missingness remain open | PASS for disclosure | Confirmed unresolved; do not treat them as implemented. |
| F17/F18 marked deferred, not counted as passes | PASS | No invoked cases; R-UI/R-MOB also unexecuted. |
| Non-proofs preserved (no overclaims) | PASS within this report's scope | No quality, latency, security, qualification, resource, cache or product acceptance inference. |

## Independent checks and falsifiers

### 1. Oracle / observer / expectation independence

Read the full relevant sources and inspect their AST imports/calls, including nested functions. `oracle.py` imports only `__future__`, `json`, `hashlib`; `rpc_expected.py` only `__future__`; `observer.py` only `__future__`, `hashlib`, `dataclasses`; `record_schema.py` only `__future__`, `dataclasses`. None imports a candidate, uses dynamic import/eval/exec, or reads a candidate answer to construct expectations. `expected()` constructs bytes/ranges from fixture tuples; `expected_manifest()` independently assembles JSON; the F01 literal anchor provides an additional known answer.

The independence checks built into the gates are weaker line-based import scans. This review's conclusion also uses complete source inspection and loaded module paths. Independence of source modules does not establish adequacy of assertions, independent authorship, or an isolated security boundary.

### 2. Actual invocation coverage and per-mutant evidence

Supplementary command: `python -X utf8 -B audit_capcomp165.py`, executed from the task workspace. The external script imports the pinned modules, records `sys.setprofile` call/return events with filename and definition line, and replays the three gate `run()` functions. Only their `RESULTS` destination is redirected to the external evidence directory. It records asserted expected-field differences from the active invariant case. All three replays exit 0 and their stdout text equals the respective primary run. Coverage counts below refer only to this original-behavior replay, **excluding later adversarial probes**.

`rpc_mutants.py` contains **32 top-level `m_*` definitions, 28 unique names, four shadowed definitions**. Of the 28 live functions, **22 are invoked in 25 calls**, **six are never invoked**. The invariant lane makes **21 mutant calls across 18 distinct functions**; its 32 candidate cases are a different count. The observed lane calls four distinct mutants once each, but only the leaked-read and replay checks discriminate the planted defect. Thus **20 live rpc_mutants functions have distinguishing gate evidence**, not all 28. This count conservatively does not call a correct safety refusal a detection of an incorrect candidate result.

| Definition (file:line) | Calls | Case / distinguishing asserted evidence |
|---|---:|---|
| `rpc_mutants.py:9 m_summary_equal_seed` | 1 | F02_identity: distinct: true -> false |
| `rpc_mutants.py:13 m_summary_concatenation` | 1 | F02_collision: distinct: true -> false |
| `rpc_mutants.py:19 m_replay_regenerates` | 1 | F02_replay: model_calls: 0 -> 1 |
| `rpc_mutants.py:23 m_closure_auto_budget` | 0 | Shadowed by later definition; not invoked. |
| `rpc_mutants.py:27 m_disabled_leaks` | 0 | Live function never invoked by the gates; no detection evidence. |
| `rpc_mutants.py:33 m_revoke_delayed` | 1 | F05_revoked: outcome: "refuse" -> "allow" |
| `rpc_mutants.py:37 m_trust_claim` | 0 | Live function never invoked by the gates; no detection evidence. |
| `rpc_mutants.py:43 m_precedence_last_loaded` | 0 | Live function never invoked by the gates; no detection evidence. |
| `rpc_mutants.py:47 m_evidence_synth` | 1 | F08: disagreement: true -> false, decision: null -> "x" |
| `rpc_mutants.py:51 m_cut_accept_latest` | 0 | Shadowed by later definition; not invoked. |
| `rpc_mutants.py:55 m_successor_last_completion` | 0 | Shadowed by later definition; not invoked. |
| `rpc_mutants.py:59 m_route_name_only` | 0 | Shadowed by later definition; not invoked. |
| `rpc_mutants.py:63 m_restore_replay` | 0 | Live function never invoked by the gates; no detection evidence. |
| `rpc_mutants.py:67 m_graph_import_to_discover` | 4 | F15_missing_dependency: reason: "missing_dependency" -> null, imported: false -> true; F15_cycle: reason: "cycle" -> null, imported: false -> true; F15_ambiguous_provider: reason: "ambiguous_provider" -> null, imported: false -> true; F15_exclusive_conflict: reason: "exclusive_owner_conflict" -> null, imported: false -> true |
| `rpc_mutants.py:71 m_silence_pass` | 0 | Live function never invoked by the gates; no detection evidence. |
| `rpc_mutants.py:85 m_disabled_reads` | 1 | F04: catalog-read count 1 versus required 0 (detected). |
| `rpc_mutants.py:91 m_forged_authority` | 1 | F06: zero admitted effects also matches safe candidate; returned completed ignored. NOT discriminated. |
| `rpc_mutants.py:96 m_quiet_no_effect` | 0 | Live function never invoked by the gates; no detection evidence. |
| `rpc_mutants.py:100 m_restore_reexecute` | 1 | F14: reexecute count 1 versus required 0 (detected). |
| `rpc_mutants.py:112 m_worker_silent` | 1 | F16: disabled observer yields HOLD_UNKNOWN for both good worker and mutant. NOT discriminated. |
| `rpc_mutants.py:117 m_closure_silent_drop` | 1 | F03_valid: dropped: ["O2:v1", "O3:v1"] -> [] |
| `rpc_mutants.py:123 m_closure_auto_budget` | 1 | F03_unfittable: outcome: "mandatory_context_unfittable" -> "ok", dispatched: false -> true |
| `rpc_mutants.py:128 m_cut_values_only` | 1 | F09_aba_unstable: outcome: "composition_cut_unavailable" -> "ok", aba_detected: true -> false |
| `rpc_mutants.py:135 m_cut_accept_latest` | 1 | F09_mixed: outcome: "composition_cut_unavailable" -> "ok" |
| `rpc_mutants.py:139 m_successor_last_completion` | 1 | F10_late: accepted: "A" -> "B" |
| `rpc_mutants.py:148 m_successor_duplicate_adopt` | 1 | F10_duplicate: accepted: "A" -> "B" |
| `rpc_mutants.py:157 m_successor_directive_ignored` | 1 | F10_directive: accepted: "A" -> "B" |
| `rpc_mutants.py:166 m_route_name_only` | 1 | F13_hold_unknown: outcome: "hold" -> "ok", hidden_visibility: "unknown" -> "known" |
| `rpc_mutants.py:173 m_graph_selected_presence_only` | 1 | F15_invalid_selection: reason: "invalid_provider_selection" -> null |
| `rpc_mutants.py:185 m_precedence_last_loaded_v2` | 1 | F07_override: winner: "skillA" -> "skillB" |
| `rpc_mutants.py:189 m_precedence_accept_any_override` | 1 | F07_override_unauthorized: outcome: "hold" -> "ok" |
| `rpc_mutants.py:195 m_revoke_erased_by_coalesce` | 1 | F05_pending: revoked: true -> false |
| `reference_model.py:65 render_by_enumeration` | 2 | F01 base output matches; reversed bytes differ from 23-byte literal (one distinguishing invocation). |
| `reference_model.py:73 render_empty_spans` | 1 | F01: verify_spans outcome refuse versus ok; bytes/ranges preserved. |
| `reference_model.py:78 render_invalid_coordinate` | 1 | F01: reason invalid_coordinate; final end 24 exceeds total 23. |
| `reference_model.py:86 render_gap_spans` | 1 | F01: reason uncovered_output_byte; final five bytes uncovered. |
| `oracle.py:31 expected_ranges_by_codepoint` | 1 | F12: note [23,24) differs from asserted byte range [23,25). |

For F06, `m_forged_authority` returns `completed`; the safe candidate returns `refused`. Both produce no admitted effect, which is the **only assertion on that mutant invocation** at `check_rpc_observed.py:98`. The forged attempted-proof digest differs in the recorded event, but the gate does not assert that difference. A protected sink refusing the attempted forgery is useful evidence of that mock sink; the displayed phrase "mutant detected" overstates candidate discrimination.

For F16, the gate runs `m_worker_silent` with the observer disabled. The good worker also grades `HOLD_UNKNOWN` in that environment, so this is an observer-loss test, not a distinguishing mutant control. An **additional enabled-observer probe** with a valid installed right produced `PASS` for the good worker and `HOLD_UNKNOWN` for the silent worker (zero events, `observation_missing=False`). This supports the classifier, but does not retroactively add coverage to the original gate.

The four reference-model mutants all have distinguishing checks: enumeration has two invocations (base passes, reverse differs); empty spans, invalid coordinates and a gap have one each and are refused. The extra codepoint mutant is in `oracle.py`, not `reference_model.py`, and is called once. Span-refusal checks use the candidate's own `verify_spans`; F01 additionally asserts the exact span tuples independently. These facts must not be counted as an independent general span validator.

### 3. Observed boundary and experimental false positives

The recorded events are **real executions of synthetic in-process instrumentation**. `Catalog.read/register/load_body`, `EffectSink.perform/complete`, and `RightsLedger.check_rights/record_operation/reexecute/...` append harness-owned `Event` records. They are not old candidate-returned `sink_events` or `rechecked_rights` flags. They are also not independently instrumented OS file reads, plugin imports, network/model calls, external effects, persistent storage, or a protected process boundary. Candidate code receives objects that expose the observer; this is not tamper-resistant instrumentation.

- **F04:** absent yields no events; disabled yields one `register` event (`detail=disabled`, `bytes_len=8`) and no read/load/query/effect; enabled yields register, catalog read (10 bytes), body load (4 bytes). Disabled operational bytes count only catalog-read bytes. The same register event type serves enabled and disabled paths; classifying the disabled event as permitted administrative bookkeeping does not establish the report's zero operational registration requirement for a real runtime. Schema/context/search/model activity has no separate actual runtime instrumentation here.
- **F06:** the untrusted import records one denied effect attempt and one refusal, with zero effect/completion. Its authorized positive control directly calls the ledger and sink (`check_rpc_observed.py:89`), **bypassing `M.admit_import`**. Verified falsifier: replace only `M.admit_import` in memory with a function that calls `sink.perform(op_id, None)` and returns `refused` for every envelope. **All nine F06 checks pass**, while a separately supplied valid current proof is refused with zero useful completions. Replacing the forged mutant with `m_quiet_no_effect` also leaves all nine checks passing. No candidate source file was edited.
- **F14:** actual mock-ledger records check `skillX=True`, `procY=False`, `skillZ=True` at fence 12, record O1 once and preserve held liability; the replay mutant emits reexecution and is detected. But `restored` references are still read from the candidate return; `record_operation(..., refs=...)` discards the refs. Current-rights checks are observed, not end-to-end enforcement of each result, persistence, or physical recovery.
- **F16:** the good worker normally records an admitted effect plus `control_complete` for `op.work`, with 9-byte `answer-A\n` and hash `95181e14e6683193fbb742753a38e4e926377d860ec6d8984103007480e46dbc`. The gate validates the **returned** artifact, then gives `classify` a literal `output_valid=True`; it never binds that artifact to the completion event's hash/length (`check_rpc_observed.py:147`, `:149`). Verified falsifier: a replacement worker records an **empty** completion but returns the valid strings for normal_A/normal_B. **All ten F16 checks pass**. Its recorded completion has `bytes_len=0`, `payload_sha256=""`, and `oracle.valid_artifact(b"")` is false. For the invalid mode the gate supplies `output_valid=False` rather than deriving validity from observed bytes. The gate also achieves the all-refuse case via a refusing harness sink, not an all-refusing candidate.

The observed lane's strict schema is used for selected outcome records, not every record: F14's valid result and F16 outputs still use ad hoc field checks. Its final "strict schema" message must be read narrowly.

Reproduction core for the adversarial probes (external script; each substitution restored in `finally`):

```python
def always_refuse(sink, ledger, envelope, *, op_id):
    sink.perform(op_id, None)
    return {"outcome": "refused"}
# Temporarily M.admit_import = always_refuse; GO.failures.clear(); GO.f06()
# Result: failures == []; valid authorized call to replacement also refused.

def invalid_emit_valid_return(boundary, mode, *, op_id="op.work", subject="owner:grant7"):
    artifact = {"normal_A": "answer-A\n", "normal_B": "answer-B\n", "invalid": ""}.get(mode, "")
    proof = boundary.ledger.issue_proof(subject)
    if boundary.sink.perform(op_id, proof):
        boundary.sink.complete(op_id, b"")
    return {"outcome": "pass", "artifact": artifact}
# Temporarily M.useful_worker = invalid_emit_valid_return; GO.failures.clear(); GO.f16()
# Result: failures == []; observed normal completions have length zero.
```

These are sensitivity tests of unchanged gate code against in-memory replacement callables, **not changes to the frozen candidate** and not fresh candidate qualification. The initial prescribed runs used the unmodified candidate.

### 4. F01 / F11 / F12 fidelity and literal anchor

Verified against R-PC report section 9 at the pinned commit:

```text
base bytes: CONTROL\nPROCEDURE\nDATA\n
hex: 434f4e54524f4c0a50524f4345445552450a444154410a
length: 23; ranges: core [0,8), skill [8,18), evidence [18,23)
canonical manifest (exact bytes):
{"profile":"rpc-canonical:v1","ranges":{"core":[0,8],"evidence":[18,23],"skill":[8,18]},"total":23}
prefix plan: CONTROL\nPROCEDURE\n (18 bytes)
tail FACT\n: length 23, first 18 bytes unchanged
core CONTROL2\n: length 24, ranges [0,9), [9,19), [19,24)
Unicode note U+00E9: bytes c3 a9; total 25; note [23,25)
codepoint mutant: note [23,24), detected
```

The base F01 manifest equals both independent assembly and the literal byte anchor. The three malformed-span cases are exercised. Those strengthened assertions are real improvements.

Limits: only the base manifest and base `prefix_plan` are asserted; reversed enumeration's span map/manifest and F11/F12 manifests are not separately compared. The simplified manifest has ranges/profile/total, not complete provenance. F11 checks core length, ranges and prefix inequality but **not exact core payload bytes, an independently observed identity, or a forfeiture reason**. Verified additional falsifier (`python -X utf8 -B probe_capcomp165.py`): wrap `R.render` in memory and, only for the `CONTROL2\n` source, replace its 24 output bytes by `b"X" * 24`, retaining its ranges/spans. The **entire fixture gate still exits 0 with all 29 checks passing**. Thus the actual unmodified candidate emits the correct core specimen, but this gate does not enforce it completely.

F12 checks byte units/coverage on raw segments. It has no generated wrapper delimiter mapping, imported instruction-role authority test, or full source/provenance mapping oracle. These are explicit residuals, not passes. No runtime counter observes the absence of model/network calls; the reviewed fixture implementations are stdlib-only local functions.

### 5. Results provenance and exact evaluated bytes

`rpc_results.json` has a suite-level provenance object for reference model/oracle/gate/Python plus F01/F11/F12 payload summaries. It has **no invoked case set or per-case source hashes/dispositions**. It omits fixture and `rpc_expected.py` hashes (the latter supplies the literal anchor).

`rpc_invariants_results.json` has a suite-level provenance object for model/expectations/mutants/gate/Python, and 32 `invoked_cases` equal to the 32 expected names in this run. It has **no per-case result records, source hashes or dispositions**, no fixture hash and no mutant invocation/detection inventory. The gate does not assert set equality or unique invocation, although this review confirmed them for this run.

`rpc_observed_results.json` has only lane, suite, four fixture IDs, failures and scope. It lacks provenance, invoked cases, individual results, disposition, Python version and retained event traces. There are **three** generated JSON files; the handoff's "both" wording is stale. The strengthened per-case provenance claim in `REVIEWS.md` is not supported by the code or artifacts.

All imported local modules resolved inside the isolated pinned clone. All 15 relevant source/report file bytes below equal their Git blobs; all were LF with zero CRLF sequences. Initial and post-run snapshots of every tracked file found changes only in the three generated JSON outputs. After preserving those outputs externally and restoring only those three generated files with `git -c core.autocrlf=false restore --source=HEAD --worktree -- ...`, **every pre-existing tracked file again matched its original byte hash**.

EOL qualification: system `core.autocrlf=true` originates at `C:/Program Files/Git/etc/gitconfig`. Clone and checkout used explicit per-command `core.autocrlf=false`; no persistent configuration was changed. Root `.gitattributes` pins the research reports to LF but does not assign attributes to the suite Python/result files (`git check-attr --all` returned no entries for the relevant suite paths). Fresh checkout source bytes were not normalized after the fact.

All five mismatching committed source-hash fields are explained exactly by CRLF versions of the same current Git blob bytes: fixture result's reference model/oracle/gate; invariant result's model/mutants. Invariant expectations/gate hashes already match LF bytes. No source staleness is established by these differences. Apart from provenance, all three parsed result records equal their committed versions. The observed JSON differs **only** in output line endings; the other two differ in source-hash strings plus output line endings. Hashing hypothetical CRLF variants was read-only analysis, not a modification of candidate files.

Relevant exact source bytes (SHA-256) and Git blob identities (SHA-1). Paths below the suite are abbreviated; `run/` and `fixtures/` are relative to `experiments/v3/capability-composition/`.

| Source | SHA-256 of evaluated bytes | Git blob |
|---|---|---|
| `run/reference_model.py` | `f39d9b572245c15ac868d733d47bfd9331da9b3bf0fcfe18945c19cfaf4fae93` | `00fb834e78f325a9a05631472e5ce9f7b353667b` |
| `run/oracle.py` | `20d0d371f954e79bde5db88e9b386dfdd130bd34f1ebdf83f0e64130bd011458` | `acd9573ea249ac1be8ffb51db92aeaad6b2fc699` |
| `run/rpc_model.py` | `f27124ee4de8129e9fed451dd87a15c90fbf66f15f70b9ffee145357908b46be` | `5955b14f8ca90b5f1b6b8b7d54f0fb59227efd34` |
| `run/rpc_mutants.py` | `3dde3e17cd616a4d6c43ed74568a01e60789ada29eb35c0e0c1b52cae3a46bf6` | `23f16122a4bbe3a513ab574a77115ff7b4d27180` |
| `run/rpc_expected.py` | `f30054d244361d3ed5da571b068d45224a593d4587de194f7356cf40d8aab18a` | `a3cf827bac0fc72b6a7a8022008dc051ed034b18` |
| `run/observer.py` | `ebfaf8395df2efb06c6575c32a44b32b7dedcf6626209909d543e1895c190bf2` | `99a26ff89da9af5a4e8159ac07089d16b6b8f0b1` |
| `run/record_schema.py` | `2ef29a120ddf993302815e49c23e0285b109461f821232d38cb2922972a99cef` | `a8844c026b1698df10946a621b51ee7f584c6304` |
| `run/check_rpc_fixtures.py` | `fcb2b8f2cab7957f9f4452a95a64a7e859dac920ef6188734301597e157d418b` | `9e94d1de962f82616dfbf5b8923b4d6ad9a917ab` |
| `run/check_rpc_invariants.py` | `0670898237ffca66d959a4ddf2329f21e8deb3b9268b5d5a039b9c66d430d52e` | `613b4b6955c7d1323479ad74f1acc96874ddeaab` |
| `run/check_rpc_observed.py` | `64ec51faa38e89ba1f5dd12c5f8fe26e7490789267f40b2f2f9bfc953a8b2f13` | `cde8bb02ef4f77c915d47b596ebfb6e1e77e3af2` |
| `fixtures/rpc.py` | `2cb09981103be13b20a0ec6fc32a6531c2353b721a0cae5f7798e02a7f1b278a` | `917a235e4e57c3437af26fe9d7e206627dc61ba6` |
| `fixtures/rpc_invariants.py` | `a94df407995afb3bc6d08d1b358d639ed220ec4372f801d8ddebb7938675490b` | `848802a8cc3afad7485d666a9d1900479cb36b8d` |
| `check_suite.py` | `56319d4f000fd0f43bf4c283c7f871d0533f7460e391dcf825034b8818d679b4` | `3942b765bc3c3f75e3660b15bda5c504ced983a5` |
| `tools/check_public_scope.py` | `2148cf303f90c2981ad84e94457f6174b8dd0b19a4999c845e095debcc9718d8` | `d325fc3db489ebaf45a84dd039b591819b4576ee` |
| `docs/v3/research-lane/capability-composition-20261002d/rpc/report.md` | `4d4940f5bcea4c4cd084876e311c1baddef3016da3fef889d31150e3d8f8614b` | `bb702f654f966e29ea20b080e26b2d09e7b86322` |

Generated result byte hashes; copies of the fresh bytes were preserved before restoration:

| Result | Committed / initial SHA-256 | Fresh run SHA-256 |
|---|---|---|
| `rpc_invariants_results.json` | `f5ca637334d42a915b0fa0347e69c513c55abf6d378b8165f1f57550bffb90f3` | `2ed2db4608bc6e71bebeaf9a64e281b593c1ee7b2dcb3c9c069fdd2b9829901b` |
| `rpc_observed_results.json` | `1431ff566667644816a4d2783877c57cbd3cc715c86148d94e04765179a7661a` | `c93179b326ec47a631c771b0ae381569c2f9f33e7a47f4ed5fb472959fd1eadd` |
| `rpc_results.json` | `3b75598ea46b7c55e7c0c1631f8c89791100d9f4637cd54a16b0a10ea23ac9b2` | `ab5d6ff8af8d1a432414646de032f7d2d0097dfa58cdd4e87bcccb294a76bb3a` |

### 6. Remaining F12 and missing-vs-null limitations

`REVIEWS.md` explicitly keeps F12 wrapper/role coverage and strict invariants matching open, and the index keeps F01-F16 `partial`. Preserve that status. Demonstrated directly:

```python
GI.matches({"disagreement": True}, E.EXPECTED["F08"])  # True: missing decision accepted as None
RS.record_conforms({}, {"value": None},
                  RS.RecordSpec((RS.FieldSpec("value", nullable=True),)))  # False
```

The strict helper distinguishes absence, but the invariants lane does not use it. Additional source-inspected gaps remain beyond those two advertised residuals: F02 checks `distinct`/`model_calls` labels without asserting identity bytes or replay bytes; independent `oracle.expected_identity` is uninvoked. F03 does not assert the loss-record field; F05 lacks an intermediate preference to exercise nonempty history and does not assert the fence/history fields; F08 omits source-reference assertions; F09 retry bounds and F10 detailed transitions are not asserted; F13's valid-current control checks only outcome; F15's absence of import is a candidate-returned boolean, not an independently observed import boundary. These are coverage limitations, not claims that the current model necessarily violates every omitted predicate.

### 7. Non-proofs and disposition scope

F17/F18 remain deferred and have no executed cases; registry presence is not a pass. R-UI/R-MOB and the shared client matrix are not implemented/executed by these gates. The source report is a prior fixture specification; its historical UNEXECUTED labels are not runtime logs.

This review establishes local behavior of the finite synthetic Python fixtures and flaws in their sensitivity. It establishes no model quality, natural-workload false-green rate, latency, resource savings, cache hit/miss or cost savings, security/injection resistance, OS isolation, physical recovery, route/platform/provider qualification, product readiness, D-034 scaling success or Owner acceptance. No live provider or model was invoked by the verification scripts. Read-only GitHub access for provenance and cloning is separate from the provider-free fixture execution.

## Residual unknowns

Full section 9 / F01-F16 fidelity remains NEEDS-EVIDENCE. Exact backend model variant is unavailable. Other Python versions/platforms, real adapters and protected runtime boundaries are unverified. A changed PR head must be re-pinned and independently checked; this report applies only to the stated pre-merge head, not a future merge result.

Proposed fixes only; no implementation edits made:

1. Route the authorized F06 control through the candidate, and require useful completion there. Assert returned outcome plus recorded effects for mutants; add enabled-observer quiet/always-refusing controls.
2. Grade F16 using actual completion payloads, or bind captured payload hash/length to oracle-validated bytes and the exact operation. Derive validity rather than supplying constants; exercise an all-refusing candidate.
3. Remove/mark obsolete shadowed mutants or enumerate a maintained registry. Fail on missing invocation and require a recorded differing asserted field for every active mutant.
4. Emit per-case inputs, expected/actual results, source/Git identities including fixtures, invoked mutant IDs, event evidence and dispositions; make completeness checks fail closed. State source-byte/EOL conventions explicitly.
5. Assert F11 exact changed-core bytes/identity/reason and relevant manifests/prefix plans; finish F12 wrapper/role mapping. Apply strict matching to invariants and close the omitted predicates above before claiming full conformance.

## Reviewer identity and decision

- Decision: **REJECT** for the requested strengthened-suite independent verification. The F06/F16 counterexamples and unexercised/nondiscriminating mutants violate the verification handoff's own sensitivity requirement; provenance claims are also unsupported.
- Narrow accepted fact: **all five exact commands pass on this pinned head under CPython 3.12.10**. This is reproducible gate execution, not whole-contract conformance. Some manifest/span, finite-state and event-boundary additions improve the scaffold; that does not cure the demonstrated false positives.
- Local branch: `loop/work/verify-capcomp2-20261002` (no collision).
- Commit scope: only `experiments/v3/capability-composition/results/c08c93fb475ca770d56dd1e1996d8e6e94c33f81-openai-codex-gpt-6.md` (this report). No candidate source, protected path or generated JSON is committed. The resulting local commit SHA is reported in the task's final response; embedding a commit's own hash inside itself is not possible.
- External evidence retained at `C:\Users\Lucas\Documents\Codex\2026-10-02\task\capcomp165-evidence`: primary stdout/stderr, execution metadata, tracked-before hashes, fresh result JSON copies, AST/profile audit, instrumented transcripts and probe records. External driver scripts remain in the task workspace. Their hashes are below. They are not part of the report-only commit.
- No push, merge, deployment, installation, spending, protected-path change, private-session-log access or interruption of existing work. No blockers remain to recording this review; the identified conformance gaps require implementation work and another independently pinned verification.

### External evidence identities

| File | SHA-256 |
|---|---|
| `verify_capcomp165.py` | `45eeddc4b96386b9be8735501862a855b0d3f10a0c08e042ba53b5166a671013` |
| `audit_capcomp165.py` | `d25eda3724031972435375383d2eb34e0bf185736207cc108657516916e3049b` |
| `probe_capcomp165.py` | `04585ccd9923de58223b127ff2cfc530d8bae096ccf82674dc1d2394ec5759b4` |
| `execution.json` | `3e21156a263ef0928476748ff29d2bdeb45929fd5d94f4dcdd177ba73cba7868` |
| `tracked-before.json` | `b03c115d39a09d779c1faf0ce17f594e94f565611e62fbb9b51a0e03c4be239a` |
| `audit.json` | `a425e0f0f65036e9deafa04e702737e936b4315ba27941319ebecb87fbe1ec7c` |
| `probes.json` | `da1e38e821ee14bef9b075911d5d8d0ad074458ca1ddf5fa9d8d4a83d66ec2b2` |
