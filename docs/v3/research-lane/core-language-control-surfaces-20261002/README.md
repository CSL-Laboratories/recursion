# Recursion core architecture and control surface research

Research packet E return · 2026-10-02

This packet supports the next local review of Recursion's core/backend implementation options and its position among agent-harness control surfaces. It contains two source-grounded research reports, bounded handoffs for later evidence collection, and the separately requested static quality review of PR #160. Recommendations are advisory. The reports do not select a product stack or authorize execution.

## Reading order

1. [Core language and architecture reconstruction](rlang/report.md): candidate comparison, store and plugin boundaries, a language-neutral reconstruction specification, and falsifiable acceptance criteria
2. [Control surfaces and competitor study](rcs/report.md): version-pinned prior art, T3 Code analysis, peer comparisons, and tests of meaningful differentiation
3. [Core local-session handoff](rlang/local-session-handoff.md) and [control-surface local-session handoff](rcs/bounded-handoff.md): prerequisites, bounded evidence requests, failure conditions, and explicit limits
4. [PR 160 static fixture quality review](pr160/report.md): code quality, fidelity to the research predicates, oracle independence, coverage strength, and implications for the next fixture lanes

Each report contains its sources, evidence classes, competing options, falsifiers and non-proofs. Read the source status and version qualifications before interpreting a comparison cell. An undocumented property is unknown, not an absent capability or a zero cost.

## Source boundary

The packet-E repository baseline is [Recursion commit 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e](https://github.com/NotSkittlz/recursion/tree/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e), the merge of PR #159. The research follows the two handoffs in [packet E](https://github.com/NotSkittlz/recursion/tree/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/prompts/dot-research-20261002e).

The supplied attachment could not be retrieved. The repository's published packet-E briefs were accessible and were used instead. Byte equality between that attachment and the repository briefs is unverified. Any attachment-only addition would require a further reconciliation.

The read-only repository export was checked against all 1,456 tracked blob identities and 210 tree identities, including the root tree; the commit identity was also checked. These are source-transfer integrity checks. They establish neither correctness of the repository nor acceptance of its experiments. No repository content is included in this return ZIP except short cited descriptions in the research.

Public-source retrieval dates and version pins appear in each report. A moving documentation page is dated evidence, not an immutable release snapshot. Recorded upstream or repository test results are attributed to their authors and distinguished from this research's static inspection.

The PR #160 review has its own newer baseline: [candidate b5d5af7d1a765bc9617c7238a64cd6a313ca7096](https://github.com/NotSkittlz/recursion/tree/b5d5af7d1a765bc9617c7238a64cd6a313ca7096). Its tree matches [merge 050936e05f5523da0eb8dc2f2fa3a65e4798af26](https://github.com/NotSkittlz/recursion/commit/050936e05f5523da0eb8dc2f2fa3a65e4798af26). The two research reports retain the earlier PR #159 baseline; they are not presented as a resynchronization to PR #160. The code review is static and does not replace the user's independent local verification.

## Use with local sessions

All proposed target experiments are UNEXECUTED by this research. No target builds, imports, checkers, benchmarks, provider calls or live-host operations were performed. No Codex/Work sessions were launched. Platform quota accounting is not independently visible here.

Local sessions should first reconcile this baseline against the current repository, then review the bounded handoffs and obtain whatever experiment admission the current project rules require. Do not treat a research recommendation, stored fixture, source digest or report-completeness check as authority to execute, as runtime conformance, or as Owner acceptance. Preserve failed and unresolved outcomes alongside successful evidence.

## Files and integrity

`SHA256SUMS.txt` records the delivered Markdown file hashes. It detects changed bytes; it is not a signature, authority token or truth guarantee. Internal research notes, source inventories, review records and downloaded third-party material are excluded.
