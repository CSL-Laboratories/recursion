# PR165 executed verification summary

Date: 2026-10-02. This is a summary of the completed Sentry verification receipt. The complete report is in this packet at **pr165/executed/c08c93fb475ca770d56dd1e1996d8e6e94c33f81-openai-codex-gpt-6.md** (41,047 bytes, SHA-256 `e7a81f8a78ee20b56e7fdcdf6be46f914eff97b079d0e989e8567a7019128321`). Reviewers do not need a separate attachment to inspect raw outputs.

## Verdict

**REJECT the strengthened-suite conformance claim. Narrow gate-rerun verdict: PASS.** All five prescribed commands exited 0. Additional executed falsifiers demonstrated that incorrect behavior could still pass F06, F16 and F11. The verifier recommends against accepting the broader conformance-evidence claim at this candidate; it does not make an Owner acceptance or merge decision.

**Evidence class:** locally observed by the independent Sentry verifier and reported in its completed receipt, retrieved 2026-10-02. Source candidate: [PR165 head c08c93fb475ca770d56dd1e1996d8e6e94c33f81](https://github.com/NotSkittlz/recursion/commit/c08c93fb475ca770d56dd1e1996d8e6e94c33f81). The local execution report has no public repository URL because the user explicitly prohibited pushing; it is identified by filename, local commit and exact SHA-256 below. Its public candidate URL establishes source identity, not proof that execution occurred.

## Candidate and environment

- Candidate SHA: c08c93fb475ca770d56dd1e1996d8e6e94c33f81.
- Candidate tree: 8fff857136c362ab0c3abd7bfa84306ae4d9f4e8.
- Target was the authorized unmerged PR head, never GitHub's synthetic merge value. The verifier's final source-status check at 21:55:30 UTC returned the same head.
- Fresh isolated clone on Sentry; no existing worktree substituted.
- Windows 11 10.0.26200, x64; CPython 3.12.10; Git 2.48.1.windows.1; GitHub CLI 2.93.0.
- Clone and checkout used core.autocrlf=false. Exact source/checkout byte identities were retained; no candidate source normalization was used to manufacture hash matches.
- Verifier: OpenAI Codex, runtime-described as based on GPT-6. Exact variant/snapshot unavailable. Different family from user-reported implementer deepseek-v4.1-flash, whose identity was not independently attested.

## Exact prescribed gate commands

Each ran from the repository root at 21:49:10–21:49:11 UTC, exited 0 and had empty stderr. Counts refer to the actual gate's scope, not whole-contract qualification.

| Command | Recorded result |
|---|---|
| python -X utf8 -B experiments/v3/capability-composition/check_suite.py | PASS; 73 registered IDs across R-PC/R-UI/R-MOB/MP; registration is not execution |
| python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_fixtures.py | PASS; 29 assertions for F01/F11/F12, with incomplete broader fidelity |
| python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_invariants.py | PASS; 32 cases, 56 assertions including 21 mutant checks |
| python -X utf8 -B experiments/v3/capability-composition/run/check_rpc_observed.py | PASS; 45 assertions for F04/F06/F14/F16 |
| python -X utf8 -B tools/check_public_scope.py | PASS; known other-project-identifier scan only |

The complete attached report contains raw stdout text, stdout hashes, source identities, probe details and the complete per-mutant table. The structure and public-scope passes do not establish content correctness or publication/security safety.

## Seven requested checks

| Obligation | Executed review disposition | Basis and limit |
|---|---|---|
| Oracle/observer/expectation separation, no dynamic import/eval | PASS for structural source separation | Full source/AST and loaded-path inspection; not independent-authorship proof or assertion adequacy |
| Every planted mutant detected on an asserted field | FAIL | rpc_mutants.py has 32 declarations, 28 unique live names, four shadowed definitions; 22 live functions invoked in 25 calls, six uninvoked; two invoked observed-lane mutants lack distinguishing assertions |
| F04/F06/F14/F16 observe real recorded events rather than flags | FAIL as a blanket conformance claim | Synthetic harness events are real test records, but F06's useful positive bypasses the candidate and F16 grades return bytes unbound to completion evidence |
| F01/F11/F12 literal anchor and section 9 fidelity | PASS for the tested literal anchor; NEEDS EVIDENCE for full fidelity | F11 accepts corrupted changed-core bytes; F12 wrapper/role coverage absent; additional static fidelity limits retained |
| Complete per-case provenance in results JSON | FAIL | Partial suite-level hashes do not provide complete per-case source/invocation/disposition records; observed result lacks even suite disposition |
| Open F12 and absent/null work not overclaimed | PASS for disclosure, not implementation | Both remain open; scope overstatements elsewhere require correction |
| Non-proofs preserved | PASS within report scope | No model-quality, latency, security, platform or product qualification result inferred |

## Experimentally confirmed falsifiers

1. **F06:** an always-refusing candidate still passes because the authorized positive control drives the harness sink directly. A successful sink control does not show that the candidate admits useful authorized work.
2. **F16:** empty recorded completion bytes plus a valid-looking returned artifact still pass. The graded output must be bound to the actual observed completion.
3. **F11:** corrupted changed-core bytes still pass the existing assertions. A length/offset/prefix-change check is not exact normative output comparison.

The separate static review predicted the first two and identified the third missing predicate; the Sentry verifier executed bounded probes without changing tracked candidate code. These are synthetic-suite false positives, not demonstrated vulnerabilities in deployed software.

## Commit and report custody

- Report-only local commit: **ab412249e129dbe6a01f48b3da6fc88ba2369468**.
- Branch: **loop/work/verify-capcomp2-20261002**.
- Report path inside clone: **experiments/v3/capability-composition/results/c08c93fb475ca770d56dd1e1996d8e6e94c33f81-openai-codex-gpt-6.md**.
- Exact committed/uploaded report: **41,047 bytes**, SHA-256 **e7a81f8a78ee20b56e7fdcdf6be46f914eff97b079d0e989e8567a7019128321**.
- The verifier reports unchanged original tracked bytes, externally preserved then restored generated results, a clean working tree, and **no push**.
- The exact report is now in this packet at `pr165/executed/c08c93fb475ca770d56dd1e1996d8e6e94c33f81-openai-codex-gpt-6.md`. SHA-256 `e7a81f8a78ee20b56e7fdcdf6be46f914eff97b079d0e989e8567a7019128321` matches the verifier's recorded hash.

No verification tests were rerun during delivery. One Codex/Work task was authorized for this verification only; no future use follows from that permission.

## Proposed next step

**Recommendation, inferred:** repair the discriminating predicates, active-mutant inventory and case-level provenance, then independently verify a newly pinned candidate before claiming strengthened conformance. **Competing option:** accept only the narrower recorded gate-rerun fact while explicitly withholding full conformance and leaving the blockers open. **Decisive falsifier:** any of the three demonstrated nonconforming candidates still passes its purported check, or an active mutant/case lacks attributable evidence. **Non-proof:** a future bounded pass would still not establish production qualification, unbounded correctness or Owner acceptance.
