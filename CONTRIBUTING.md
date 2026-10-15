# Contributing to Recursion

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

## Branch and review flow

- Branch from up-to-date `master` as `csl-<N>-<slug>` (example `csl-26-short-slug`).
  A GitHub username prefix is optional (`chaandansaini101/csl-26-short-slug`).
- Put `CSL-N` in the PR title. pr-lint requires `CSL-[0-9]+` in the branch name or the
  PR title.
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
