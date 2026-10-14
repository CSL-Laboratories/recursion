# Capability-composition conformance suite — verification record

Status: **UNVERIFIED** for the current PR #165 candidate. The historical ACCEPT at `050936e` does
not apply to this head. This is **not Owner acceptance**; only Lucas accepts (`CONTROL_PLANE.md`
"Independent verifiers"; `AGENTS.md` rule 6). The pre-verification handoff is superseded by this
record.

**Fidelity qualification (2026-10-02).** The historical ACCEPT covered *rerunning the then-current
gates* at `050936e`. A separate static review found that full F01–F16 **specification fidelity is
not established** (findings Q01–Q10). The affected fixtures are marked `partial` in
`FIXTURE-INDEX.md`. See `REVIEWS.md`. Treat this suite as a **scaffold**, not full conformance
evidence.

**Re-verification (strengthened suite, slices 1–4).** The tested code changed after the `050936e`
ACCEPT, so that ACCEPT no longer applies. Verify the strengthened suite at the PR #165 merge commit:

```text
python -X utf8 -B experiments/v3/capability-composition/check_suite.py
python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_fixtures.py
python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_invariants.py
python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_observed.py
python -X utf8 -B tools/check_public_scope.py
```

Expected: `capability-composition suite structure clean`, `RPC_FIXTURE_PASS`, `RPC_INVARIANTS_PASS`,
`RPC_OBSERVED_PASS`. Checks beyond rerun: oracle/observer/expectation independence (no candidate
import, no dynamic import); every planted mutant detected; per-case provenance present in both
`results/*.json`; and the still-open items (F12 wrapper/role coverage; strict absent-vs-null matching
in the invariants lane) are **not** over-claimed. A changed candidate again restarts verification.

**Status.** Two independent verifications disagree, and the stronger one governs:
- GLM-5.3-Flash **ACCEPT** at `c08c93f` (rerun + mutant detection), later **REJECTED** by
  OpenAI Codex GPT-6 with executed probes: F06 always-refusing-candidate, F16 empty-completion with
  a valid-looking return, F11 corrupted changed-core bytes. See
  `results/c08c93fb…-openai-codex-gpt-6.md`.
- **Slice 5** closes those three counterexamples (candidate-routed F06 authorized path;
  recorded-completion binding in F16; exact changed-core bytes + corrupt-core mutant in F11), adds
  strict matching, per-case records and a fail-closed mutant-registry check.
- The Slice-5 commit changes tested code, so verify the **current PR #165 head** with the same
  commands. Open: F12 wrapper/role coverage and fuller per-case provenance.

## Verdict

- Independent verifier: **GLM-5.3-Flash** (Z.ai), model id `cheaper-inference/glm-5.3-flash`, 2026-10-02.
- Candidate: `050936e05f5523da0eb8dc2f2fa3a65e4798af26` (PR #160 merge commit; tree identical to
  `b5d5af7d1a765bc9617c7238a64cd6a313ca7096`).
- Implementer model family: **deepseek-v4.1-flash** (this session) — recorded so the different-family
  preference can be assessed (implementer ≠ verifier).
- Result: `results/050936e05f5523da0eb8dc2f2fa3a65e4798af26-glm-5.3-flash.md`.
- Decision: **ACCEPT** — no falsifier fired. Bounded synthetic scope only; not IT-02/product acceptance.
- Environment: CPython **3.12.10**, Windows.

## Scope under review

- R-PC **F01/F11/F12** (`run/check_rpc_fixtures.py`) and **F02–F10, F13–F16**
  (`run/check_rpc_invariants.py`), plus the structural gate `check_suite.py`.
- Explicitly **deferred, not passed:** F17/F18 (scripted accounting/analysis; not provider-free
  behavioral evidence). See `FIXTURE-INDEX.md`.

## Reproduce

```text
git clone https://github.com/NotSkittlz/recursion.git recursion-verify-capcomp
cd recursion-verify-capcomp
git checkout 050936e05f5523da0eb8dc2f2fa3a65e4798af26
python -X utf8 -B experiments/v3/capability-composition/check_suite.py
python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_fixtures.py
python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_invariants.py
python -X utf8 -B tools/check_public_scope.py
```

Expected: `PASS: capability-composition suite structure clean`; `RPC_FIXTURE_PASS`; `RPC_INVARIANTS_PASS`
with every case reporting candidate-matches-expected **and** mutant-detected. Both lane gates rewrite
their `results/*.json` byte-identically.

## Checks required beyond rerunning

1. **Oracle independence** — `run/oracle.py` and `run/rpc_expected.py` import no candidate module
   (`reference_model`/`rpc_model`), no dynamic import/eval, and compute expectations from the frozen
   spec, never from candidate output.
2. **Mutants actually detected** — every function in `run/rpc_mutants.py` plus the two fixture-level
   mutants is exercised and differs from the expected fields on ≥1 asserted field.
3. **No candidate-flag trust** — the gate subset-matches candidate results against literal expected
   fields; it never reads a candidate `pass`/`coherent`/`no_effect` flag.
4. **Report fidelity** — against `rpc/report.md` §9: the 23-byte specimen (`[0,8)`/`[8,18)`/`[18,23)`);
   the core change (length 24, offsets `[9,19)`/`[19,24)`); the two-byte character counted as two byte
   units; and the F02–F16 predicates.
5. **Deferral honesty** — F17/F18 marked `deferred` and absent from the gates/cases.
6. **Non-proofs preserved** — no model-quality, latency, resource, security, injection-resistance,
   platform-isolation, qualification or porting claim; bounded synthetic scope only.

## Falsifiers (a later run fails if any)

- The oracle or expectations import or derive from the candidate model.
- Any planted mutant passes (is not detected).
- Any fixture's expected values disagree with the report's predicates.
- Deferred/unexecuted cases are counted as passes.
- Any claim is made beyond bounded synthetic conformance.

## Residuals from this verification and their resolution

1. **Implementer family unrecorded** → **resolved**: recorded above (deepseek-v4.1-flash).
2. **Results provenance imprecision** → **corrected**: F01/F11/F12 were created at `0e74886`;
   F02–F10/F13–F16 at `07095d4`; both reproduce byte-identically at `050936e`.
3. **Stale prose** → **fixed** in this record, `README.md` and `run/README.md`.
4. **F01 canonical-manifest predicate only partially exercised (OPEN).** The gate checks output bytes,
   enumeration invariance and ranges but does not exercise `reference_model.canonical_manifest` /
   `prefix_plan`, and it does not yet count "no model/network call" (satisfied here only by
   construction: the suite is stdlib-only pure functions). Strengthening F01 changes the tested code,
   so the strengthened suite requires **re-verification** before it is treated as accepted.
5. **Python version unpinned** → **recorded**: verified on CPython 3.12.10; the suite is stdlib-only and
   showed no version-sensitive behavior; other interpreters remain unverified.
6. **Synthetic-scope limit** → inherent; stated in `README.md` and both results JSON scope strings.

## Re-verification rule

Any change to the tested files (`reference_model.py`, `oracle.py`, `rpc_model.py`, `rpc_expected.py`,
`rpc_mutants.py`, `fixtures/*`, or the two lane gates) restarts verification: a changed candidate
invalidates the exact-candidate ACCEPT.

## Result template

`run/RESULT-TEMPLATE.md`.
