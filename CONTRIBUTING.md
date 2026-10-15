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
- **Wait for jobs before human sign-off.** Order: Devin Review runs first, then the
  Claude fixer jobs (`devin-review-fix`, `resolve-pr`) act on its findings, then required
  CI reruns on the fixer's push. No human approves or requests changes on a PR until all
  of these have finished on the current head commit. A review on a stale head does not
  count. After new pushes, the reviewer reviews again. Review requests and their status
  are posted as @mention comments on the CSL Linear issue, and a watcher posts there when
  a PR is ready for review.
- Autonomously file research findings, ideas and bugs to Triage, deduped.
- Ground every implementation in the best available research: Parallel search for web
  research and Context7 for current library docs.

Recursion is the supervision and execution control plane for a portfolio of projects. The
current development target is **Recursion v3** — a provider-neutral agent platform. This
page is the short path from "I want to help" to "my work can be reviewed and merged".

## Start here

Read, in order:

1. [`CONTROL_PLANE.md`](CONTROL_PLANE.md) — who decides.
2. [`ANCHOR.md`](ANCHOR.md) — what is in scope.
3. [`docs/v3/START-HERE.md`](docs/v3/START-HERE.md) — how the v3 work is organized.
4. [`docs/v3/RESEARCH-AGENDA.md`](docs/v3/RESEARCH-AGENDA.md) — the open research questions.
5. [`docs/v3/RESEARCH-SCOPE.md`](docs/v3/RESEARCH-SCOPE.md) — the research lane and its gates.

The product specifications live in `docs/v3/*.md`. Decisions live in
`docs/v3/DECISION-REGISTER.md`. Research evidence and runnable checks live in
`experiments/v3/**`.

## Pick up work

Every task needs a **parent goal** and a **machine-checkable gate**. There is no gate, no
work — if you cannot name the command that proves your change, stop and ask in
[`state/INBOX.md`](state/INBOX.md).

For a research or evidence task:

1. Choose one question from `RESEARCH-AGENDA.md` (or a named open item in a spec).
2. Write a short SPEC + finite domain + a falsifier before writing code.
3. Put the runnable check under `experiments/v3/<your-lane>/check.py`, taking
   `--candidate FULL_COMMIT`.
4. Record what the check proves and what it does **not** prove. Missing verification is
   never a pass; an implementer's own PASS is not acceptance.

## Linear workflow

Work starts in Linear (team **CSL**, project **recursion**), not as a GitHub issue. When you
start, assign yourself and move the issue to **In Progress**. Branch `loop/work/CSL-N` (keep
this convention; do not use Linear's copied branch name). Title the PR `CSL-N: ...` and
include `Fixes CSL-N` in the body — that links the PR, and merging moves the issue to
**Done** automatically. If the PR does not move the issue to **In Review**, move it yourself.
File follow-ups to **Triage**. Keep the Linear issue current as work progresses, and file
anything that comes up in a session to **Triage** instead of dropping it; Linear is the
primary async channel, alongside Google Meet and Discord.

## When Claude can't fix a review thread

Devin reviews only and never fixes, so a thread is never handed back to Devin. Claude
leaves the thread open, replies explaining why it couldn't fix it, and @mentions the PR
author.

The CSL Observability stuck-thread watcher — an external Grok Bot routine, not a GitHub
workflow — then scans open PRs on a schedule and files a Linear issue in **Triage** titled
`Stuck PR thread: repo#PR – topic`, labeled `needs-decision`, and linked to the PR's
`CSL-N` issue. It runs outside this repository, so it will not appear in
`.github/workflows` and reviewers should not look for it in-repo. For urgent cases the PR
author may also file one by hand.

Before merging any PR, check Linear for open `Stuck PR thread` issues tied to it, and
resolve them or record the decision first.

## Branch and review flow

- Branch from up-to-date `master`: `loop/work/<task-id>` for build output.
- Never commit to `master` directly and never force-push.
- Open a PR for review. **Lucas merges every review branch himself**; an automated run only
  ever produces a review branch.
- Keep changes inside your declared allowed paths. `CONTROL_PLANE.md`, `ANCHOR.md`,
  `state/DECISIONS.md`, `scripts/`, `schemas/` and `.env` are protected paths — a change
  there needs an explicitly approved control-plane authoring session, not a product PR.

## Run a gate

From the repository root, with Python 3.11+ and Git, run the gate named by your task. The
provider-free gates that currently pass are listed in
[`docs/v3/RESEARCH-SCOPE.md`](docs/v3/RESEARCH-SCOPE.md), for example:

```text
python -X utf8 -B docs/v3/construction/next-work-20260914/check_handoff.py
python -X utf8 -B experiments/v3/recovery-ledger-20260914/test_ledger.py
```

The `tools/check_v3_orientation.py` gate is the frozen 2026-09-13 orientation-repair gate; it
binds to that repair's scope and is not a general check for new branches.

On Windows, paths can exceed 260 characters; pass `-c core.longpaths=true` per Git command.

## What is out of scope

- The construction **foundry** (a WSL Devin/SWE-2 harness) was retired on 2026-09-28. Do not
  restart it; its code and records are in git history only.
- Physical or live-provider execution, spending, deployment, publication and merging are
  human-gated. Propose them in `state/PROPOSALS.md`; do not perform them.

When in doubt, prefer a small, verified change with a named gate over a large, plausible one.
