# Governance — Recursion

Adopted 2026-09-28; authority split amended 2026-10-16 under D-041; developer-approved
receipts added 2026-10-21 under D-043; research-vault alignment added 2026-10-25 under D-044.
The highest-priority
authority document in this repository, beneath current explicit founder decisions within
the authority defined below. If another file contradicts it, this file wins and that file
is stale.

Read `ANCHOR.md` for WHY the work exists and what is in scope. Read
[docs/v3/START-HERE.md](docs/v3/START-HERE.md) for how the product is organized.
Existing `docs/v3/*` stays until CSL-536 migrates it. New non-public docs (research, specs,
decisions, evidence and receipts, gate artifacts, and runbooks that aren't public) go to
[CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research) through a PR
there; this repository only links. The only new docs allowed here are public knowledge-base,
README, setup and implementation instructions for outside readers, plus agent instruction files.

Recursion is a **single product**: the v3 agent harness. This repository builds that product.
It does not supervise other repositories. The earlier Hermes-led control plane (v1/v2) was
retired on 2026-09-28; the complete prior tree is at tag `pre-v3-clean-cut-20260928`.

## Precedence

Conflicts resolve in this order, highest first:

1. Current explicit founder decisions within the authority below; Lucas breaks disagreements
2. `CONTROL_PLANE.md` (this file)
3. The v3 product specifications: existing `docs/v3/*` until CSL-536 migrates them; new
   specifications are recorded in [CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research)
   and this repository only links. Existing `docs/v3/*` remains authoritative until that move.
4. Decision records: existing entries in `state/DECISIONS.md` (frozen legacy log through D-044);
   new decisions are recorded in csl-research and this repository only links
5. Implementation docs, experiments and evidence (new non-public docs go to csl-research)
6. Runtime state

A lower rank never silently overrides a higher one. On conflict, stop and record a blocker as a
Linear issue comment; `state/INBOX.md` is optional.

## Roles

### Founders - product decisions and merges

The founders are Lucas Guerra, Chaandan Saini and Samir. Any one founder may decide product
and implementation questions from research, accept a research-lane outcome,
admit a feature within a lane, or approve research continuation within its approved budget.
Core language/runtime choice (A1b) belongs to the founders; this amendment selects no language.
All three founders make the final release decision.

Founders merge review branches after the required review and checks. Agents do not merge.
Any founder may approve a protected-path or merge-rule amendment through the separately
recorded authoring process below. Approval does not apply repository settings or bypass
existing review requirements.

### Lucas - reserved decisions and final tie-break

Lucas retains spending and new services, sensitive company credentials (including company
API-key generation), deployments using company servers (including Railway), and the final
tie-break when founders disagree. Research continuation within an approved budget does not
raise that budget. These reserved decisions are not delegated by another founder's approval.
Nothing below this line may reinterpret a founder's decision within that founder's authority,
or Lucas's final tie-break, as advisory.

### Individual developers - their own live actions

Each developer owns their individual live actions, accounts, API keys and tests. An agent
may execute a developer-approved live action only on that developer's own accounts/keys,
with the route, account and spend ceiling recorded, and only after the R2a permission-profile
fix has landed. Company credentials and company-server deployments still require Lucas's
approval; individual approval does not override that boundary or authorize new spending.

Building and testing a capability does not authorize operating it. This amendment grants no
specific live run, credential use, spending, deployment or quota reset. It does not change
other projects' operating permissions, invariants, pre-live gates or accepted contracts;
person-level approval does not unlock live capital.
Individual authority above is limited to that developer's own accounts. Destructive actions
outside that scope still require Lucas's explicit approval; no additional authority over
shared or company resources is delegated.

### Coding agents — bounded executors

An agent receives one task, one repository/worktree, explicit allowed paths, explicit
non-goals and one machine-checkable acceptance gate. It works inside that boundary. It does not
redefine the product, reprioritise work, or promote itself into a supervisor role. Changes
to this document or protected paths require a separate approved authoring session. Agents
recommend and propose; founders decide. On ambiguity an agent records a blocker and stops.

Decision packets give options, classified evidence, tradeoffs, a recommendation, the
strongest countercase, reversibility, further research cost and a deadline. Consequential
founder decisions record the chosen option, rationale, conditions and revisit trigger.
Agents must not invent founder decisions. They must not present agent-authored records as
personal founder statements, with one exception (D-043): a developer-approved comment
counts as that developer's personal receipt only when all of these hold:

1. Immediately before posting, the agent showed the developer the final exact text and the
   developer approved it in the same session. Approval from an earlier turn or another
   context does not carry over.
2. The comment carries the line
   `developer-approved: <developer>, exact text confirmed <UTC time>, session <id>`.
3. It has not been edited since it was posted. Any later edit voids it as a receipt.
4. It counts only within that developer's own authority and grants none.
5. No accepted contract requires an exact authenticated human receipt for it (recursion
   program receipts, R2a, or another repository's accepted contract), unless that
   contract is amended.

It never stands in for a named reviewer's GitHub review, which that person submits from
their own account (Protected paths). Any other agent-authored record stays an attributed
relay.

### Independent verifiers

Verify specification compliance, behaviour, tests and protected-path compliance. An
implementer's own PASS is never acceptance. Prefer a verifier from a different model family
than the implementer.

## Non-negotiable invariants

1. **No force pushes. No agent merges. No autonomous deployment.** Agents commit to
   a review branch named `csl-<N>-<slug>`, optionally prefixed with a GitHub username,
   and open that branch for founder review and merge. Final release needs all three founders;
   company-server deployment requires Lucas. pr-lint matches `CSL-[0-9]+`
   case-sensitively, so a lowercase branch name alone does not pass. The `CSL-N` key in
   the PR title satisfies the check.
2. Every task has a parent goal and a machine-checkable gate. No gate, no work — it escalates.
3. Generated output without independent verification is not progress.
4. No secrets in tracked files, logs or notifications.
5. Live provider/game/host actions, spending, quota resets and credential use are human-gated.
   Agent execution is limited to the recorded developer approval and landed R2a prerequisite
   in "Individual developers"; it never substitutes for Lucas's reserved approvals. Quota resets require separate
   human approval. No approval or permission is inferred from a build, test or this amendment.
6. Protected paths change only in an explicitly founder-approved authoring session, never as
   part of a product task.
7. Evidence classes stay distinct: source-documented, locally observed, inferred and unmeasured
   are never conflated. A paper or a model consensus never proves local fit.

## Protected paths

`CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`, `.env`, and any credential or
authentication configuration. A task that touches them without the matching authoring-session
approval fails its gate. Authoring approval does not itself authorize credential use or change
Lucas's reserved authority over sensitive company credentials. Option B (D-042).
The core rule is Chaandan Saini's 2026-10-15 CSL-117 ruling (comment
`e59021c7-3a77-445f-a04c-832cf1e5fe46`): `scripts/ci/` and the test config stay
protected, new tests can be added freely, and changing an existing assertion needs
a named human reviewer. Fixtures come from the CSL-117 brief's Option B (comment
`f2506e97-51a7-42ee-a82b-39e089f7e7de`), which that ruling adopted; the word is not
in the ruling sentence itself. Checked on this tree, those paths are the globs
`scripts/ci/**` and `scripts/tests/**/fixtures/**`, plus `tools/ci_gate.py`, the
file that sets `TEST_GLOBS`, `TEST_EXCLUDE` and `REQUIRED_SUITE_TESTS`. This tree
has no `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini` or `conftest.py`,
so those names are not added. Whether fixtures under `experiments/` are covered
is open and is not decided here. The existing admitted scope, and keeping
implementation and activation separate, are limits from the 2026-10-17 authoring
approval (comment `58641114-aa3c-4f8f-8630-a089b19f8ea3`), not from the ruling.
Recording what the ruling implies: paths under `scripts/` outside those globs,
for example `scripts/v3/` and non-fixture `scripts/tests/`, are no longer
protected paths. Changing an existing assertion anywhere still needs the named
human reviewer. That person is named in the PR and submits their own GitHub
review from their own account. An approval a bot posts from a founder's account
does not count.

## Changing this document

Only in a reviewed authoring session a founder explicitly approves, on a review branch.
New decision records go to [CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research)
through a PR there; this repository only links. `state/DECISIONS.md` is a frozen legacy log
through D-044. Superseding an entry preserves the reason it was believed — a ruled-out
approach without its reason is a dead-end waiting to be rediscovered.

D-041 records the source decisions and approval for the authority-split amendment, D-043 records
the developer-approved receipt exception, and D-044 records this research-vault alignment. Each
changes governance
text only: settings, CODEOWNERS, rulesets, access grants and merge-queue activation require
their own authorization and are not changed or claimed effective here. Independent review
must cover the exact candidate; an implementer's PASS is not acceptance or merge permission.
