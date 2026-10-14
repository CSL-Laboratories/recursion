# Architecture revisions before the building phase

Date: 2026-10-02. Status: proposed editing and acceptance guidance, not a repository change or Owner ratification.

## Bottom line

Revise the outline's source identities, authority labels and behavioral baseline before treating it as the specification for a conforming reconstruction. Preserve supported ideas even when the independent review disputed them. The correction report contains replacement wording for A1–A10; the language report contains six further replacement paragraphs. This guide prioritizes those edits rather than creating another architecture or selecting a stack.

**Evidence:** the source-documented findings and immutable URLs are in [the correction register](corrections/report.md), [the M1 corpus audit](corpus/report.md), and [the language checklist](language/report.md), all retrieved 2026-10-02. Recommendations in this guide are **inferred/proposed**. Their acceptance has not been demonstrated by preparing this packet. The outline under review is PR161 head `c7c5b1e87ff8de9e9e2c1d0aa3bb7026dfb938e5`; correction sources are pinned to `9f4122b50dc8117b185b2cd52244a10df3eb4285`. These are separate from provisional PR165 head `c08c93fb475ca770d56dd1e1996d8e6e94c33f81`. [Outline][OUTLINE] [Baseline][BASELINE]

The original GLM architecture-review text was not located in the inspected sources. Its A1–A10 allegations come from the supplied brief and were tested against primary sources rather than accepted as true.

## Apply the corrections without changing their scope

Each row is an editorial summary of the referenced correction, including that correction’s conforming rival, falsifier and non-proof; it is not a new technology or mechanism recommendation. Direct primary links below support the factual premises (source-documented, retrieved 2026-10-02).

| Outline area | Proposed amendment | Evidence and exact replacement text |
|---|---|---|
| Introduction and cross-cutting invariants | Separate current Owner decisions, frozen experimental profiles, candidate contracts and historical observations. Avoid blanket ratification of everything under docs/v3. | Corrections §1 and §3; [governance status][GOV], [D-039][D039] |
| Facets A, B and E | Use source-qualified fixture names; preserve the real R-SC F20-A–G family. Explain O1 as two store candidates compared with the normative corpus, and identify shared CAS common-mode risk. | A1, A7; [R-SC vectors][F20], [FU-02 O1–O5][O1] |
| Authority representation | Preserve functional/workspace scope plus nine additional permissions: eleven grant dimensions in the M1B wire object. Do not implement an approximate six- or nine-field object under a claim of exact parity. | A4; [M1B exact grant fields][GRANTS] |
| Plugin and interface boundaries | Remove unsupported rlc-candidate/0; cite R-L and the actual plugin/edition and M1B schema sources. D-035 is topology context. Label command-key and seven-capability-fact proposals as proposals. | A5, A8, H1–H3; [R-L candidate][RL], [M1B plugin profile][PLUGIN], [D-035][D035] |
| Observability and evidence | Cite R-V's deterministic explanation design as candidate research. Remove the unsupported current orientation-gate PASS and replace blanket “no rerun” statements with scoped historical receipts. | A2, A6 and supplemental status corrections; [orientation gate][ORIENTATION], [R-V candidate][RV], [diagnostic receipt][SESSIONA] |
| Mobile and scope | Keep desktop/backend support policy separate from open mobile floors. Reconcile MOB-F and MP obligations without double counting; name planning/executive and operations/lifecycle ownership gaps without pretending this packet implements those facets. | A9, A10 and required facet additions; [D-028 desktop/backend policy][D028], [mobile open decisions][MOBILE], [D-039 reconciliation][D039] |
| Reconstruction gate | Use the inventoried M1 corpus with its semantic qualification limits; do not let a larger diagnostic model substitute for integrated acceptance. Make language-specific inability and host leakage falsifiers bounded and testable. | Corpus §§4–7; language C1–C6; [qualification limits][QUAL], [integrated proposal][FU03], [R-LANG reconstruction proposal][RLANG] |

## Distinguish three kinds of blocking condition

### Before freezing the affected behavioral contract

Resolve the intended WorkPacket correlation identity and task-level effect attenuation; reconcile candidate identity/authority/byte rules with the intended production contract; remove unsupported current-pass claims. The two M1A code/wire discrepancies are established by static source inspection, not newly executed failures. Matching old expected bytes can preserve an old disagreement. [Constructor source][KERNEL] [Wire requirement][WIRE] [Qualification limits][QUAL]. Retrieved 2026-10-02; source-documented discrepancy plus inferred consequence.

**Recommendation:** record an explicit amendment/disposition and preserve historical evidence, rather than silently redefining the contract to match the code. **Competing option:** demonstrate that the differing behavior belongs to a deliberately separate, versioned test profile and specify the production mapping. **Decisive falsifier:** a supposedly conforming implementation can widen the task's effects or produce a different required identity while satisfying the claimed acceptance gate. **Non-proof:** resolving these two issues does not establish complete kernel correctness.

### Before claiming conforming reconstruction or integrated acceptance

Require the exact corpus/profile identity, independently defined expected behavior, adequate positive and adverse controls, integrated predicate coverage and scoped verification receipts. Seventy-one bundles are a source inventory, not seventy-one newly passed tests and not comprehensive product qualification. [Planning manifest][AM] [Execution manifest][BM] [Integrity manifest][CM] [Integrated proposal][FU03]. Retrieved 2026-10-02; source-documented inventory and proposed acceptance method. Candidate integrated details require reconciliation; this does not silently ratify every equation in the research.

**Recommendation:** use an obligation-to-observer-to-assertion map, retaining actual inputs, outputs, source identities and unresolved dispositions. **Competing option:** separately attested component suites joined by a frozen coverage manifest, instead of one monolithic verifier. **Decisive falsifier:** a required behavior has no independent observed predicate or a known nonconforming candidate survives its claimed check. **Non-proof:** even a bounded integrated pass does not establish unbounded correctness or native durability/security.

### Before affected native or release qualification

Resolve the supported mobile floors and host/toolchain profiles, then obtain their required native evidence. A desktop policy is not mobile qualification, and a synthetic observer is not operating-system enforcement. [Desktop/backend policy][D028] [Open mobile floors][MOBILE] [R-LANG qualification proposal][RLANG]. Retrieved 2026-10-02; source-documented scope plus inferred sequencing.

**Recommendation:** maintain separate semantic-conformance and platform-qualification receipts. **Competing option:** a composite release matrix may join them while retaining separate results and blockers. **Decisive falsifier:** a release label hides an unqualified required profile or transfers a result between platforms without justified equivalence. **Non-proof:** neither this research nor an architecture edit grants permission to deploy or settles a legal question.

## What can remain open

Language, store, renderer, framework, transport and plugin-host selection can remain explicit variables in a language-agnostic architecture. Their later qualification obligations cannot remain implicit. This distinction does not forbid every bounded prototype while later release evidence is outstanding; each implementation assignment still needs its own admitted scope and gate. It also does not authorize new implementation work in this research task. [D-039 no-selection boundary][D039] [R-LANG neutral/variable proposal][RLANG]. Retrieved 2026-10-02; inferred recommendation.

**Competing option:** choose a provisional reference arm for a separately authorized bounded experiment while preserving a neutral contract. **Decisive falsifier:** a supposedly variable choice changes externally required authority, bytes, refusals, effects or recovery semantics. **Non-proof:** retaining options does not demonstrate that all options are feasible or economically sensible.

## Separate follow-on facets

R-PLAN and R-OPS have not been run. This packet identifies their required ownership seams only. Their deeper designs should not be fabricated from a correction register or treated as completed merely because their headings appear in the outline.

The separately authorized PR165 execution is complete: [executed-result summary](pr165/executed/RESULT-SUMMARY.md) and the bundled full report [c08c93fb475ca770d56dd1e1996d8e6e94c33f81-openai-codex-gpt-6.md](pr165/executed/c08c93fb475ca770d56dd1e1996d8e6e94c33f81-openai-codex-gpt-6.md). Its narrow gate PASS and broader conformance REJECT are distinct. It does not qualify the entire M1 corpus, ratify the architecture, or authorize a merge.

## Primary source links

All project source links use the immutable revision indicated above and were retrieved 2026-10-02. Source content establishes what is written, not correctness or enforcement.

[OUTLINE]: https://github.com/NotSkittlz/recursion/blob/c7c5b1e87ff8de9e9e2c1d0aa3bb7026dfb938e5/docs/v3/architecture/LANGUAGE-AGNOSTIC-ARCHITECTURE.md
[BASELINE]: https://github.com/NotSkittlz/recursion/commit/9f4122b50dc8117b185b2cd52244a10df3eb4285
[GOV]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/AGENTS.md#L36-L41
[D039]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L480-L498
[F20]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/shared-contracts-20261002/Recursion_Shared_Contract_Reconciliation.md#L791-L824
[O1]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-02-store-crash-replay.md#L159-L187
[GRANTS]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-WIRE-PROFILE.md#L123-L127
[RL]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/overnight-20261002/rl/report.md#L1-L32
[PLUGIN]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1B-WIRE-PROFILE.md#L123-L178
[D035]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L384-L405
[ORIENTATION]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/tools/check_v3_orientation.py#L110-L156
[RV]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/overnight-20261002/rv/report.md#L1-L34
[SESSIONA]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-kernel-session-a/verification/results/74fb18d-muse-spark-1-3.md
[D028]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/state/DECISIONS.md#L240-L269
[MOBILE]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/ios-android-20261002/INTAKE-SYNTHESIS.md#L52-L87
[QUAL]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/experiments/v3/fu-03-kernel-session-a/FIXTURE-QUALIFICATION.md#L1-L21
[FU03]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/followups-20261002/raw/ds-FU-03-formal-model-vectors.md#L695-L722
[RLANG]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/research-lane/core-language-control-surfaces-20261002/rlang/report.md#L203-L244
[KERNEL]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/v3/planning/replay/kernel.py#L282-L350
[WIRE]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/docs/v3/M1A-WIRE-PROFILE.md#L567-L590
[AM]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/planning/fixtures/manifest.json
[BM]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/execution/fixtures/manifest.json
[CM]: https://github.com/NotSkittlz/recursion/blob/9f4122b50dc8117b185b2cd52244a10df3eb4285/scripts/tests/v3/integrity/fixtures/manifest.json
