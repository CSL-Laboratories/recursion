# Contributing to Recursion

## Linear-first operations (applies to every agent and Grok Bot)

Linear is the basis of operations. Before starting work, check Linear for overlapping
issues and PRs.

- Work only from a Linear issue. Keep it updated with status, progress comments and PR
  links. Bots and agents post their own status updates and PR links as comments on the
  issue.
- Every review request, blocker, or question for a dev must be posted as an @mention
  comment on the matching CSL Linear issue, so the Linear bot notifies them. A GitHub
  mention alone is not enough. Current Linear handles (check Linear if ownership has
  changed): Chaandan = @chaandansaini101, Samir = @samirshahahmed, Lucas =
  @lucassalvador987. This applies to interactive agents and Grok Bot. The GitHub-only
  Claude Code Action fixer has no Linear tooling; it replies on the PR threads, and the
  PR author relays anything it cannot fix to the Linear issue.
- **Formal GitHub review requests.** Every review ask also needs a formal GitHub
  review request, not only the Linear @mention above. Use
  `gh pr edit <n> --add-reviewer samir-shah-ahmed,CDanTheMan` (whichever
  reviewers apply). PRs that touch protected or CODEOWNED paths also request
  the code owner (`@NotSkittlz` / Lucas); do not copy the example above and
  skip him. If the PR author is a requested code owner, skip requesting them:
  GitHub rejects review requests to the author. Request the other founders
  (`samir-shah-ahmed`, `CDanTheMan`) for independent review instead. For paths
  where the author is the sole code owner, note on the Linear issue that no
  eligible code owner can be requested and that the founders' reviews are
  independent review, not code-owner review. Both (a) the Linear @mention
  and (b) this GitHub request are required. This is mandatory for agents too.
- **Wait for jobs before human sign-off.** Order: Devin Review runs first, then the
  Claude fixer job (`devin-review-fix`) acts on its findings, then required
  CI reruns on the fixer's push. No human approves or requests changes on a PR until all
  of these have finished on the current head commit. A review on a stale head does not
  count. After new pushes, the reviewer reviews again. Review requests and their status
  are posted as @mention comments on the CSL Linear issue, and a watcher posts there when
  a PR is ready for review.
- Autonomously file research findings, ideas and bugs to Triage, deduped.
- Ground every issue and every execution in research before the work; size it to
  the task. No specific tool is required. Research may use any deep-research
  source the team has, such as Parallel, Exa, Claude Deep Research, Open Deep
  Research, ChatGPT Deep Research or the web. Each run records its tool, its
  run id and its cost (or `unknown + reason`) in its csl-research record.
- All research output (notes, briefs, findings, source tables, competitor or
  frontier writeups, research registers or indexes) goes to
  [CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research)
  as `submissions/<record_id>/`, through a PR in that repo, with a digest on
  the Linear issue. Never commit research content to this repo; this repo only
  links to the vault record (repo, 40-hex commit, path, claim, limitations).
  New non-public docs (research, specs, decisions, evidence and receipts, gate
  artifacts, and runbooks that aren't public) go to that vault through a PR
  there, except operational contracts and runbooks that this repository's
  code, a gate or CI reads: those stay here. A file stays in this repository
  if code, a gate or CI here reads it; everything else non-public goes to the
  vault. Existing files stay out of this repo now: CSL-536 moved the existing docs and the frozen decision logs (`state/DECISIONS.md` and `docs/v3/DECISION-REGISTER.md`, through D-044) to csl-research `contracts/recursion/history/`; they stay frozen there, and new decision records go to csl-research. The only other new docs
  allowed here are public knowledge-base, README, setup and implementation
  instructions for outside readers, and agent instruction files. If
  csl-research is not accessible, stop and post a
  blocker on the Linear issue; do not fall back to this repo.
  The former `docs/v3/` research lanes are frozen history in csl-research;
  `experiments/v3/` holds only
  runnable product checks. Every research pass for an issue (a) posts that
  digest and (b) appends the submission record following that repo's README,
  AGENTS.md, templates and `tools/check_submission.py`, and opens the vault PR
  before releasing the issue claim. The record carries the sanitized report,
  its SHA-256, the tool or processor, run id, retrieval time, cost or
  `unknown + reason`, a source-to-claim mapping, and `issue_urls`. A research
  body produced under an admitted issue is published there as part of that
  issue; no separate admission is needed. A research pass on a Linear issue
  is complete only when its record is in csl-research `submissions/` and the
  PR link is on the issue.
- On a decision or direction issue, the assignee or claimant must post the
  cited brief (options, tradeoffs, recommendation, target metrics) on the
  Linear issue. A Grok Bot coordinator acting for its owner may prepare it,
  attributed exactly as `Grok Bot, preparing research for <owner>`; do not
  phrase it as the owner personally authored or endorsed the recommendation.
  Publishing a brief never marks the issue decided, never clears a hold and
  never authorizes implementation. Only the designated human decision-maker,
  or an explicitly authorized research team, decides. The developer decides,
  or @mentions the approver; a coding agent @mentions its developer and does
  not decide.

Recursion is a **single product**: the v3 agent harness. This repository builds that
provider-neutral agent platform; it does not supervise other repositories. This page is
the short path from "I want to help" to "my work can be reviewed and merged".

## Start here

Read, in order:

1. [`CONTROL_PLANE.md`](CONTROL_PLANE.md) — who decides.
2. [`ANCHOR.md`](ANCHOR.md) — what is in scope.
3. The Linear issue and the csl-research records it links — what to do and why.

The former v3 specifications, research and decision registers moved to csl-research
under CSL-536, byte for byte, at
[`contracts/recursion/history/`](https://github.com/CSL-Laboratories/csl-research/tree/f9d8526acbaf02abb227b38d88ed95e170a1f6d4/contracts/recursion/history).
They are history, not current scope. New non-public docs go to
[CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research)
through a PR there, except operational contracts and runbooks that this
repository's code, a gate or CI reads: those stay here. A file stays in
this repository if code, a gate or CI here reads it; everything else
non-public goes to the vault. This repo only links to vault records.
`experiments/v3/` holds only runnable
product checks. All research output (notes, briefs, findings, source tables, competitor
or frontier writeups, research registers or indexes) goes to
[CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research)
as `submissions/<record_id>/`, through a PR in that repo, with a digest on the
Linear issue. Never commit research content to this repo; this repo only links
to the vault record (repo, 40-hex commit, path, claim, limitations). If
csl-research is not accessible, stop and post a blocker on the
Linear issue; do not fall back to this repo.

## Pick up work

Every task needs a **parent goal** and a **machine-checkable gate**. There is no gate, no
work — if you cannot name the command that proves your change, stop and comment
on the Linear issue. The Linear issue comment is the canonical escalation;
[`state/INBOX.md`](state/INBOX.md) is optional.

For a research or evidence task:

1. Choose one question from `RESEARCH-AGENDA.md` (or a named open item in a spec).
2. Write a short SPEC + finite domain + a falsifier before writing code.
3. Publish the research body to
   [CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research)
   as `submissions/<record_id>/` (PR in that repo, digest on the Linear issue)
   before releasing the issue claim. Never commit the report, findings, or
   register here.
4. If the task also needs a runnable product check, put it under
   `experiments/v3/<your-lane>/check.py`, taking `--candidate FULL_COMMIT`.
   Record what the check proves and what it does **not** prove in the vault
   submission. Missing verification is never a pass; an implementer's own PASS
   is not acceptance.

## Linear workflow

Work starts in Linear (team **CSL**, project **recursion**), not as a GitHub issue. When you
start, post a claim record ([AGENTS.md](AGENTS.md#bounded-issue-execution-and-shared-evidence));
the owner stays the assignee. Move the issue to **In Progress** once your claim is
confirmed. Title the PR `CSL-N: ...`. Include `Fixes CSL-N` in the body when the PR
completes the issue's acceptance criteria — that links the PR, and merging moves the
issue to **Done** automatically. When scope or decisions remain, use `Related to CSL-N`
instead, so the PR links without closing the issue. If the PR does not move the issue to **In Review**,
move it yourself.
File follow-ups to **Triage**. Keep the Linear issue current as work progresses, and file
anything that comes up in a session to **Triage** instead of dropping it; Linear is the
primary async channel, alongside Google Meet and Discord.

## When Claude can't fix a review thread

See [AGENTS.md#when-claude-cant-fix-a-review-thread](AGENTS.md#when-claude-cant-fix-a-review-thread).

## Branch and review flow

- Branch from up-to-date `master` as `csl-<N>-<slug>` (example `csl-26-short-slug`).
  A GitHub username prefix is optional (`chaandansaini101/csl-26-short-slug`).
- Put `CSL-N` in the PR title. pr-lint matches `CSL-[0-9]+` case-sensitively, so a
  lowercase branch name alone does not pass. The `CSL-N` key in the PR title
  satisfies the check.
- Never commit to `master` directly and never force-push.
- Open a PR for review. **Founders merge review branches after required reviews and checks**
  under `CONTROL_PLANE.md`; agents never merge. All three founders make the final release
  decision, and Lucas retains the reserved decisions and final tie-break. This workflow
  does not accept a pending contract or bypass an owner hold.
  Until rulesets enforce it, no founder merges their own PR without another founder's
  approval.
- Keep changes inside your declared allowed paths. `CONTROL_PLANE.md`, `ANCHOR.md`,
  `state/DECISIONS.md`, `schemas/` and `.env` are protected paths — a change
  there needs a separately recorded founder-approved authoring session under
  `CONTROL_PLANE.md`, not a product PR. Option B (D-042). The core rule is CSL-117
  ruling `e59021c7-3a77-445f-a04c-832cf1e5fe46`. Fixtures are from the CSL-117 brief
  (`f2506e97-51a7-42ee-a82b-39e089f7e7de`). The admitted scope, and keeping
  implementation and activation separate, are from approval
  `58641114-aa3c-4f8f-8630-a089b19f8ea3`. Protected paths from that record are
  `scripts/ci/**`, `scripts/tests/**/fixtures/**` and `tools/ci_gate.py`. Paths
  under `scripts/` outside those globs, for example `scripts/v3/` and non-fixture
  `scripts/tests/`, are no longer protected paths. Changing an existing assertion
  anywhere still needs a named human reviewer: named in the PR, submitting their
  own GitHub review from their own account. An approval a bot posts from a
  founder's account does not count. New tests can be added freely, within the
  existing admitted scope. Whether fixtures under `experiments/` are covered is
  open and is not decided here.

## Review loop

See [AGENTS.md#review-loop](AGENTS.md#review-loop). The stuck-thread author
fallback lives in [When Claude can't fix a review thread](AGENTS.md#when-claude-cant-fix-a-review-thread).

## Run a gate

From the repository root, with Python 3.11+ and Git, run the gate named by your task. The
provider-free repository gate is `tools/ci_gate.py`, and a task may name a narrower
check, for example:

```text
python -X utf8 -B tools/ci_gate.py --force
python -X utf8 -B experiments/v3/recovery-ledger-20260914/test_ledger.py
```

On Windows, paths can exceed 260 characters; pass `-c core.longpaths=true` per Git command.

## What is out of scope

- The construction **foundry** (a WSL Devin/SWE-2 harness) was retired on 2026-09-28. Do not
  restart it; its code and records are in git history only.
- Physical or live-provider execution, spending, deployment, publication and merging are
  human-gated. Only the developer-approved own-account agent execution defined in
  `CONTROL_PLANE.md` is permitted after R2a lands, with route/account/spend-ceiling approval
  recorded. Lucas's company-resource and spending approvals remain required; quota resets
  require separate human approval. Outside that explicit scope, propose actions in
  `state/PROPOSALS.md`; do not perform them. Agents never merge, and these docs grant no
  specific live action, accepted contract or live-capital permission.
  Destructive or irreversible live actions, even on your own accounts, need explicit
  approval for each action. "R2a landed" means a merged, reviewed PR.

When in doubt, prefer a small, verified change with a named gate over a large, plausible one.
