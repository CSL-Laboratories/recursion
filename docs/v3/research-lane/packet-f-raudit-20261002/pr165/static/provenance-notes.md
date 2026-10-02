# PR165 static evidence and provenance notes

Date: 2026-10-02 UTC. Reviewer family: OpenAI; exact runtime model identifier unavailable.  
Target: user-authorized current head c08c93fb475ca770d56dd1e1996d8e6e94c33f81. User current-head instruction at 21:45:57 UTC superseded the original merge-commit request.

## What was and was not done

Authenticated GitHub reads retrieved 24 source/data/document files pinned to the exact head. A separate local evidence directory contains those bytes; it is not a fresh clone. Native git hash-object and sha256sum checks were used on inert source copies. sed was used only to stream LF→CRLF conversions into sha256sum for provenance comparison. No source file was imported, executed or used as a command.

All 24 copied files have the same Git blob hash as the connector returned. This checks copy integrity, not runtime correctness. The files were saved with exactly their retrieved bytes; no target repository was modified.

Five requested gate commands: **NOT RUN**. Fresh checkout: **NO**. Branch: **NOT CREATED BY THIS REVIEWER**. Commit: **NONE**. Push: **NONE**. Raw runtime stdout/stderr: **NONE**. A separate executed verifier can supply those artifacts.

## PR status evidence

Authenticated GitHub metadata at approximately 21:43, 21:46 and 21:55 UTC:

- PR: https://github.com/NotSkittlz/recursion/pull/165
- state: open
- merged: false
- merged_at: null
- head: c08c93fb475ca770d56dd1e1996d8e6e94c33f81
- base: 9f4122b50dc8117b185b2cd52244a10df3eb4285
- API merge_commit_sha: 92bebe2974f2efbec9382f71e3de0796a3dbfb7d

The last value is not treated as a completed merge. Open status is no blocker after the user's explicit current-head authorization. If head changes, source-dependent conclusions must be refreshed.

## Committed hashes and EOL behavior

The result generators hash path.read_bytes(), so checkout line endings affect the recorded digest. [run/check_rpc_fixtures.py L31–33](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_fixtures.py#L31-L33) [run/check_rpc_invariants.py L31–33](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_invariants.py#L31-L33)

| Source | Exact Git-content SHA-256 in local evidence | Author-recorded SHA-256 | Matching byte representation |
|---|---|---|---|
| reference_model.py | f39d9b572245c15ac868d733d47bfd9331da9b3bf0fcfe18945c19cfaf4fae93 | d1c3550d0ea9dbe1c6791145b78a00d2d70ce5023b16c08727f43001e99afd2b | CRLF conversion |
| oracle.py | 20d0d371f954e79bde5db88e9b386dfdd130bd34f1ebdf83f0e64130bd011458 | cf4376972ffbeccda6aaf99f1ee8b55cb2ee15c4be30407ef9f3a093825915a3 | CRLF conversion |
| check_rpc_fixtures.py | fcb2b8f2cab7957f9f4452a95a64a7e859dac920ef6188734301597e157d418b | 98a83e416c3d2aeb54d5498f256e3db60c582f421b55762cf5b6f2aecf97ac99 | CRLF conversion |
| rpc_model.py | f27124ee4de8129e9fed451dd87a15c90fbf66f15f70b9ffee145357908b46be | 3b736364cb346a62328cdf992ca6234704e02c013e1e28498761086c51112eaf | CRLF conversion |
| rpc_mutants.py | 3dde3e17cd616a4d6c43ed74568a01e60789ada29eb35c0e0c1b52cae3a46bf6 | 8494f2a56f8ffe1ad85289da32672c58778fa16f4794b0848f1e6bba89ac6e86 | CRLF conversion |
| rpc_expected.py | f30054d244361d3ed5da571b068d45224a593d4587de194f7356cf40d8aab18a | f30054d244361d3ed5da571b068d45224a593d4587de194f7356cf40d8aab18a | Exact LF Git content |
| check_rpc_invariants.py | 0670898237ffca66d959a4ddf2329f21e8deb3b9268b5d5a039b9c66d430d52e | 0670898237ffca66d959a4ddf2329f21e8deb3b9268b5d5a039b9c66d430d52e | Exact LF Git content |

Committed sources of recorded values: [results/rpc_results.json L9–16](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/results/rpc_results.json#L9-L16) [results/rpc_invariants_results.json L88–98](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/results/rpc_invariants_results.json#L88-L98). The recorded Python version is 3.12.10; the historical verifier describes Windows at [VERIFICATION.md L31–38](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/VERIFICATION.md#L31-L38).

**Interpretation:** all seven compared recorded hashes can be reconciled to the current Git source by the stated byte representations. This supports an EOL explanation and does not prove the author ran this exact tree. It is not evidence of stale semantic source. A Linux fresh checkout may legitimately rewrite the five EOL-sensitive fields; raw result byte equality must be evaluated with checkout policy in view.

**Recommendation:** record commit, Git blob identity, checkout-byte digest and EOL policy separately, and hash fixture/oracle/observer/schema/spec inputs too.  
**Rival:** platform-local byte hashes alone are adequate for a local rerun.  
**Falsifier:** a recorded input cannot be mapped to the actual pinned bytes, a required input lacks an identity, or a current invocation cannot be reconstructed from the evidence.  
**Non-proof:** reconciled hashes establish input identity, not observed execution or correct behavior.

## Complete local source identity table

Every Git blob match below was checked, not assumed.

| Path | Git blob SHA | Local SHA-256 |
|---|---|---|
| [docs/v3/research-lane/capability-composition-20261002d/rpc/report.md](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/docs/v3/research-lane/capability-composition-20261002d/rpc/report.md) | bb702f654f966e29ea20b080e26b2d09e7b86322 | 4d4940f5bcea4c4cd084876e311c1baddef3016da3fef889d31150e3d8f8614b |
| [experiments/v3/capability-composition/FIXTURE-INDEX.md](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/FIXTURE-INDEX.md) | 2fdcc11f010426d68e8605e867def042fbf63321 | 4c71f65ddb2a51daa1a95d7b846e3844462dc3ae8be4f14f2fea87feb22e21df |
| [experiments/v3/capability-composition/README.md](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/README.md) | 44d37f637b69f351fca19a031fd2ace5ae3b730d | 73edc758c90e345c4ac2f5e543e4326b189339235fc95c74309231f0574131c3 |
| [experiments/v3/capability-composition/REVIEWS.md](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/REVIEWS.md) | 18ee27108d8a993e7cc3f308993ab411b5da8697 | d2cdc2bef0b44338887d203e9468fe056251406a092456d923c2842696a120ec |
| [experiments/v3/capability-composition/VERIFICATION.md](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/VERIFICATION.md) | bc3c4cf0ba25850670d0c91d16b32aa388cadbab | 4806a4102c75ccb1f8fca4904627ae7aa68c09cdc18a057267ddb09c8b7c66b6 |
| [experiments/v3/capability-composition/check_suite.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/check_suite.py) | 3942b765bc3c3f75e3660b15bda5c504ced983a5 | 56319d4f000fd0f43bf4c283c7f871d0533f7460e391dcf825034b8818d679b4 |
| [experiments/v3/capability-composition/fixtures/rpc.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/fixtures/rpc.py) | 917a235e4e57c3437af26fe9d7e206627dc61ba6 | 2cb09981103be13b20a0ec6fc32a6531c2353b721a0cae5f7798e02a7f1b278a |
| [experiments/v3/capability-composition/fixtures/rpc_invariants.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/fixtures/rpc_invariants.py) | 848802a8cc3afad7485d666a9d1900479cb36b8d | a94df407995afb3bc6d08d1b358d639ed220ec4372f801d8ddebb7938675490b |
| [experiments/v3/capability-composition/results/rpc_invariants_results.json](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/results/rpc_invariants_results.json) | a0bcb08e1d2dc0be05a612de7a6c129774782449 | f5ca637334d42a915b0fa0347e69c513c55abf6d378b8165f1f57550bffb90f3 |
| [experiments/v3/capability-composition/results/rpc_observed_results.json](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/results/rpc_observed_results.json) | adc41a990fa3975802bcf419ad8e9aa79752d28c | 1431ff566667644816a4d2783877c57cbd3cc715c86148d94e04765179a7661a |
| [experiments/v3/capability-composition/results/rpc_results.json](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/results/rpc_results.json) | 423af5848247531d27f45e8720c6beb09fb692c6 | 3b75598ea46b7c55e7c0c1631f8c89791100d9f4637cd54a16b0a10ea23ac9b2 |
| [experiments/v3/capability-composition/run/README.md](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/README.md) | c9bb5673e6cda941c83bb003339104bae6758085 | 5a39f267d90776221f18a5429341c63b9aa7eeb81b762fa467c71d05c238dfe2 |
| [experiments/v3/capability-composition/run/RESULT-TEMPLATE.md](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/RESULT-TEMPLATE.md) | 8d302d47c6ebe9f1b51465ad02efd6de4799ac86 | f8cdaa6588f1b91c54e6c1420a14e34887111441b45c6644a1aeb71fb01dc531 |
| [experiments/v3/capability-composition/run/check_rpc_fixtures.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_fixtures.py) | 9e94d1de962f82616dfbf5b8923b4d6ad9a917ab | fcb2b8f2cab7957f9f4452a95a64a7e859dac920ef6188734301597e157d418b |
| [experiments/v3/capability-composition/run/check_rpc_invariants.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_invariants.py) | 613b4b6955c7d1323479ad74f1acc96874ddeaab | 0670898237ffca66d959a4ddf2329f21e8deb3b9268b5d5a039b9c66d430d52e |
| [experiments/v3/capability-composition/run/check_rpc_observed.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_observed.py) | cde8bb02ef4f77c915d47b596ebfb6e1e77e3af2 | 64ec51faa38e89ba1f5dd12c5f8fe26e7490789267f40b2f2f9bfc953a8b2f13 |
| [experiments/v3/capability-composition/run/observer.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/observer.py) | 99a26ff89da9af5a4e8159ac07089d16b6b8f0b1 | ebfaf8395df2efb06c6575c32a44b32b7dedcf6626209909d543e1895c190bf2 |
| [experiments/v3/capability-composition/run/oracle.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/oracle.py) | acd9573ea249ac1be8ffb51db92aeaad6b2fc699 | 20d0d371f954e79bde5db88e9b386dfdd130bd34f1ebdf83f0e64130bd011458 |
| [experiments/v3/capability-composition/run/record_schema.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/record_schema.py) | a8844c026b1698df10946a621b51ee7f584c6304 | 2ef29a120ddf993302815e49c23e0285b109461f821232d38cb2922972a99cef |
| [experiments/v3/capability-composition/run/reference_model.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/reference_model.py) | 00fb834e78f325a9a05631472e5ce9f7b353667b | f39d9b572245c15ac868d733d47bfd9331da9b3bf0fcfe18945c19cfaf4fae93 |
| [experiments/v3/capability-composition/run/rpc_expected.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_expected.py) | a3cf827bac0fc72b6a7a8022008dc051ed034b18 | f30054d244361d3ed5da571b068d45224a593d4587de194f7356cf40d8aab18a |
| [experiments/v3/capability-composition/run/rpc_model.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_model.py) | 5955b14f8ca90b5f1b6b8b7d54f0fb59227efd34 | f27124ee4de8129e9fed451dd87a15c90fbf66f15f70b9ffee145357908b46be |
| [experiments/v3/capability-composition/run/rpc_mutants.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py) | 23f16122a4bbe3a513ab574a77115ff7b4d27180 | 3dde3e17cd616a4d6c43ed74568a01e60789ada29eb35c0e0c1b52cae3a46bf6 |
| [tools/check_public_scope.py](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/tools/check_public_scope.py) | d325fc3db489ebaf45a84dd039b591819b4576ee | 2148cf303f90c2981ad84e94457f6174b8dd0b19a4999c845e095debcc9718d8 |

The retrieval-manifest.json is the machine-readable counterpart. source-sha256.txt contains native sha256sum output; source-git-blobs.txt follows the same sorted-path order.

## Source versus result evidence

A source-documented PASS record has lower evidentiary status than an independently observed run of the specified entrypoint. A reachable mutant call has lower evidentiary status than recorded mutant rejection. A candidate flag has lower evidentiary status than the corresponding independently observed action. This review preserves all three distinctions.
