# Recursion program control

Status: candidate contract for [CSL-204](https://linear.app/csl-laboratories/issue/CSL-204/establish-the-recursion-month-long-program-control-layer). It is not Owner acceptance, release qualification, or authority to implement dependent product work.

This directory turns the month-long development plan into a provider-free, reviewable control surface:

- [ISSUE-CONTRACT.md](ISSUE-CONTRACT.md) defines anti-drift issue readiness, proportional research, human decision receipts, lifecycle gates, and the reusable issue body.
- [MONTH-PROGRAM.md](MONTH-PROGRAM.md) defines the program controls, starting issue set, rolling integration checkpoints, and month-end evidence floor.
- [program.json](program.json) is a conservative seed ledger. It records only verified lineage and candidate status; it does not claim that downstream contracts or capabilities are accepted.
- `python -X utf8 -B tools/check_program_control.py` validates the ledger, document digests, references, decision receipts, dependency readiness, evidence promotion, and independent-verification requirements.

## Authority

Agents may research, propose, build bounded candidates, and validate structure. Only Lucas, or a human explicitly delegated by Lucas for a named scope, can accept a consequential product or architecture decision. A prototype, agent recommendation, issue transition, PR approval, or merge is not a decision receipt.

The ledger is descriptive and fail-closed. It never changes Linear, starts work, grants credentials, raises budgets, deploys, or merges.

## Evidence promotion

The maturity order is:

1. `unresearched`
2. `researched`
3. `human_decided`
4. `contract_frozen`
5. `prototyped`
6. `implemented`
7. `provider_free_verified`
8. `locally_qualified`
9. `release_qualified`

Higher tiers require the lower obligations. Missing evidence remains unknown. The checker proves structural closure only; independent review and the acceptance contracts remain separate.

## Change procedure

1. Update a contract document and recompute its SHA-256 in `program.json`.
2. If the change is material to an accepted decision, mark the old receipt superseded and block consumers until a human approves the new digest.
3. Add or update evidence records without deleting failed attempts or unknowns.
4. Run the checker and focused tests.
5. Send the exact candidate through repository review and current-head CI.
