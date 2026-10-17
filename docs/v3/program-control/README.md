# Recursion program control

Status: candidate contract for [CSL-204](https://linear.app/csl-laboratories/issue/CSL-204/establish-the-recursion-month-long-program-control-layer). It is not Owner acceptance, release qualification, or authority to implement dependent product work.

This directory turns the month-long development plan into a provider-free, reviewable control surface:

- [ISSUE-CONTRACT-v2.md](ISSUE-CONTRACT-v2.md) defines anti-drift issue readiness, proportional research, human decision receipts, lifecycle gates, and the reusable issue body.
- [MONTH-PROGRAM.md](MONTH-PROGRAM.md) defines the program controls, starting issue set, rolling integration checkpoints, and month-end evidence floor.
- [program.json](program.json) is a conservative seed ledger. It records only verified lineage and candidate status; it does not claim that downstream contracts or capabilities are accepted.
- `python -X utf8 -B tools/check_program_control.py` validates the ledger, document digests, references, decision receipts, dependency readiness, evidence promotion, and independent-verification requirements.

Revision 2 of the issue contract is the CSL-258 authority-wording candidate. [Revision 1](ISSUE-CONTRACT.md) is retained byte-for-byte for history, not as a competing authority rule. Its SHA-256 is `2bed606d1d07b905a69a7571aefe4b111d4238d78aeeb25625da7c2517d3d614`, recorded here and in the [pre-v2 ledger at commit `4d3e3511`](https://github.com/CSL-Laboratories/recursion/blob/4d3e3511dad30f7e0616ceb21db9d25c97266917/docs/v3/program-control/program.json). The current ledger pins the v2 candidate; it does not independently pin or check v1 as a historical entry. The pending decision history is unchanged; neither revision is accepted by this change. The month-program contract, receipt-channel requirements, admission caps, lifecycle sequencing and checker behavior are unchanged.

## Authority

Agents execute approved research, prototypes, implementation, tests, repairs and follow-ups inside accepted boundaries. Pending decisions block only their affected lineage. A prototype, agent recommendation, issue transition, PR approval or merge is not a decision receipt.

[CONTROL_PLANE.md](../../../CONTROL_PLANE.md#roles) and D-041 define the authority boundary. Any founder may decide product and implementation questions from research, accept research-lane outcomes, admit features within a lane, and approve research continuation within its approved budget. All three founders make the final release decision; founders merge after required review and checks. Lucas retains spending and new services, sensitive company credentials (including company API-key generation), company-server deployments, and the final tie-break. Another founder's approval cannot grant those reserved powers or raise a budget.

Each developer owns their individual live actions, accounts, API keys and tests. An agent may execute a developer-approved live action only on that developer's own accounts/keys, with the route, account and spend ceiling recorded, and only after the R2a permission-profile fix has landed. Individual approval does not override Lucas's company-resource or spending approvals, authorize live capital, or grant an agent merge permission. Destructive actions outside that individual scope still require Lucas's explicit approval. Individual-account approval does not authorize acceptance of a program-wide contract or a change to program policy. This candidate grants no specific live action or acceptance of another contract.

When these documents are read together, the phrase "developer approval" in [MONTH-PROGRAM's human decision queue](MONTH-PROGRAM.md#3-human-decision-queue) does not grant authority to every developer. CONTROL_PLANE takes precedence: continuation of a program research lane requires approval from a founder acting within the approved budget. Any additional spending remains reserved to Lucas; a founder's continuation approval cannot increase the budget. Owning the account or key used for research does not by itself authorize continuation of a program research lane. This is an authority-wording clarification, not a change to the queue, receipt requirements, admission caps or lifecycle gates; MONTH-PROGRAM's pinned bytes remain unchanged.

The ledger is descriptive and fail-closed. It never changes Linear, starts work, grants credentials, raises budgets, deploys, or merges.

Human approvals are recorded as comments on the matching Linear issue and pinned by comment identity plus proposal digest. Labels and issue transitions are not approval receipts.

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
