# Packet F architecture corrections and PR165 verification

Prepared for Lucas on 2026-10-02. Research lane: **R-AUDIT only**, with the separately requested PR165 verification. R-PLAN and R-OPS have not been run.

## Start here

1. [Architecture revision guide](ARCHITECTURE-REVISION-GUIDE.md): where to apply the changes, with behavioral-contract, reconstruction and later qualification blockers kept separate.
2. [Correction register](corrections/report.md): A1–A10, paste-ready replacements, candidate hedges and stale-status corrections.
3. [M1 executable corpus](corpus/report.md): exact inventory, enforcement locations, historical-result limits and two current code/wire discrepancies.
4. [R-LANG-informed checklist](language/report.md): six replacement paragraphs, language-specific feasibility facts, canonicalization/digest testing and host-boundary obligations. [Source ledger](language/source-ledger.md) and [future verification handoff](language/local-verification-handoff.md) accompany it.
5. [PR165 executed-result summary](pr165/executed/RESULT-SUMMARY.md) and [reconciliation](pr165/RECONCILIATION.md): the actual five-gate rerun, additional falsifiers, verdict and local-commit custody.
6. [PR165 static review](pr165/static/report.md): seven requested checks, [complete mutant audit](pr165/static/mutant-audit.md), and [provenance details](pr165/static/provenance-notes.md).

## Architecture conclusion

**Inferred recommendation: revise before treating the outline as a conforming-reconstruction baseline.** The report corrects the outline and, where necessary, the supplied review premises. F20-A–G is real but misattributed; O1 legitimately compares two store finalists with a normative corpus; M1B has eleven grant dimensions, comprising two base dimensions plus nine additional permissions. These are source-documented findings in A1, A4 and A7, with immutable URLs and retrieval date 2026-10-02.

The 71 M1 fixture bundles are useful regression evidence, with exact inventory enforcement. They do not establish integrated acceptance. The corpus report independently identifies two written-rule/code discrepancies: WorkPacket correlation identity and effect attenuation. No new M1 execution was performed. See its source ledger and §§1–5 for source-documented facts, static inferences and historical receipts.

**Competing option:** retain the outline as a visibly non-normative orientation document while a separate versioned behavioral specification supplies all implementation obligations. **Decisive falsifier for either approach:** an implementer must invent missing authority/byte/state behavior, or an allegedly conforming candidate can violate a required rule while satisfying its acceptance predicates. **Non-proof:** this research does not ratify contracts, select a stack or qualify production behavior.

## Source and authority boundaries

| Evidence | Immutable identity |
|---|---|
| Audited PR161 outline | c7c5b1e87ff8de9e9e2c1d0aa3bb7026dfb938e5 |
| Architecture correction-source baseline | 9f4122b50dc8117b185b2cd52244a10df3eb4285 |
| PR165 pre-merge candidate | c08c93fb475ca770d56dd1e1996d8e6e94c33f81 |

These identities are not interchangeable. Sources were retrieved 2026-10-02. The original complete GLM architecture-review text was unavailable in the inspected sources; its disputed claims came from the supplied packet and were checked against the actual project evidence. Private Recursion evidence was used under Lucas's explicit clarification. Public technical sources remain separately cited. No repository information was published or shared with another recipient.

Project authority and factual correctness are distinct. Proposed amendments are labeled and do not silently change accepted decisions. No language, store, framework, transport, host or vendor is selected, and no legal conclusion is made. A source stating PASS is classified as a reported historical result unless independently executed for this task.

## Execution boundary

Architecture, M1 corpus, language research and the static PR165 report are static/source work. Their NOT RUN statements refer to those sections, not to the separately approved PR165 execution. Lucas authorized one Codex/Work verification task on Sentry for PR165 only, later clarifying that the unmerged PR head is the target. No future-task authorization follows from that exception.

**Executed PR165 verdict: REJECT the strengthened-suite conformance claim; all five prescribed gates passed.** Independent probes reproduced false positives in F06, F16 and F11. Mutant coverage and per-case provenance remain incomplete. This result does not reject the narrower fact that these exact commands successfully ran. See [the executed-result summary](pr165/executed/RESULT-SUMMARY.md) for locally observed evidence on Sentry, commands, seven-check dispositions and non-proofs.

The full exact committed report is attached separately in chat. Its retrieval into this ZIP was denied by the selected-file access scope; that restriction was respected. The ZIP contains a clearly labeled summary, not a purported verbatim copy.

Only the report was committed locally as **ab412249e129dbe6a01f48b3da6fc88ba2369468**, on **loop/work/verify-capcomp2-20261002**. The verifier reports unchanged original tracked bytes, clean working tree and **no push**. The exact-report SHA-256 is recorded in the reconciliation. The static template under pr165/static/results is not an execution receipt; its provisional NEEDS EVIDENCE is superseded for the executed decision by the separately scoped REJECT report.

## Packaging

Source ledgers preserve immutable URLs, source identities, retrieval dates and evidence classes. Raw repository snapshots and internal review working files are not bundled. SHA256SUMS.txt covers the delivered files; DELIVERY-VERIFICATION.md states the checks applied to the final packet. Markdown is the editable research format used throughout; no repository files have been amended by the research documents themselves.
