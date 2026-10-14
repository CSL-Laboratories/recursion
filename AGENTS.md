# Operating rules — Recursion

This repository builds Recursion v3, a provider-neutral agent harness. Read
`CONTROL_PLANE.md` first — it says who decides. Read `ANCHOR.md` before any planning task —
it says what is in scope. Then read [docs/v3/START-HERE.md](docs/v3/START-HERE.md) before
selecting a task.

## Your role

Lucas is the final authority and assigns tasks. You are a bounded executor: one task, one
repository/worktree, explicit allowed paths, explicit non-goals, and one machine-checkable
acceptance gate. Work inside that boundary. If a task could be read two materially different
ways, stop and record a blocker in `state/INBOX.md` — do not pick one.

## Hard rules

1. **Never `git push --force`.** Under any circumstance, in any script, for any reason.
2. **Never commit to or merge into `master`.** Use Linear's copied branch name for the issue
   (`CSL-N`) and open a review branch; Lucas reviews and merges.
3. **Never modify a protected path** — `CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`,
   `.env`, or auth/credential configuration. Propose changes in `state/PROPOSALS.md`.
4. **No secrets in tracked files, logs or notifications.**
5. **Every task needs a parent goal and a machine-checkable gate.** No gate, no work — it
   escalates.
6. **An implementer's PASS is not acceptance.** An independent verifier reruns the exact
   candidate; prefer a different model family.
7. **Generated output without verification is not progress.** If output outpaces review,
   generate less.
8. **Live provider/game/host actions, spending and quota resets are human-gated.** Ask; never
   perform them.

## Escalate, don't guess

Stop and write to `state/INBOX.md` when a task is ambiguous, needs a decision not recorded in
`state/DECISIONS.md`, conflicts with the research, or makes a claim you cannot verify.

## Working with the v3 specifications

`docs/v3` is non-normative until ratified. Do not reopen answered Owner questions or select a
language, store, framework or sandbox from a research recommendation. Keep evidence classes
distinct: source-documented, locally observed, inferred and unmeasured are different things.

## Style

Plain markdown. Dates as `YYYY-MM-DD`. Keep files skimmable — anything not reread is dead
weight.

## Engineering stack (Oct 2026)

Current tools. Do not add paid services or raise spend limits.

- **Parallel Search MCP** — web search and fetch. Prefer `parallel-search` (`web_search` /
  `web_fetch`) for web lookups. Declared in `.cursor/mcp.json` and `.mcp.json`
  (https://search.parallel.ai/mcp). No API key.
- **Context7 MCP** — up-to-date library docs (https://mcp.context7.com/mcp). Works without a
  key. Same two config files, alongside Parallel.
- **Linear** — source of truth for work. Workspace **CSL laboratories**, team key **CSL**,
  project **recursion**.
- **GitHub** — org **CSL-Laboratories**.
- **Devin** — automated PR review on PR creation; on-demand reviews.
- **Blacksmith CI** — see [CI](#ci).
- **Google Workspace** (`csl-laboratories.com`) — meetings and notes.
- **Discord bot** (`csl-bot-operations`) — commands, including `/ci`, `/review`, and
  `/devin review|run|status`.

## Linear-first workflow

Every change starts from a Linear issue (`CSL-N`). Authority, protected paths, and merge still
follow `CONTROL_PLANE.md`; this section is how work is filed and shipped.

- Use Linear's copied branch name so the PR auto-links.
- PR title includes `CSL-N`.
- One issue per PR; keep PRs small and stackable.
- New ideas and review findings become Triage issues with one Type label, the repo label
  `recursion`, and the `recursion` project — not drive-by changes on an unrelated PR.
- Address Devin and CI review comments, then resolve the threads.
- Humans make the load-bearing decisions (architecture, specs, merge order). Agents propose;
  humans decide.

## CI

- Required check: `linux` (Blacksmith).
- Windows and macOS run on the `ci:full` label, on manual dispatch, and on `master` (macOS
  nightly at 07:17 UTC).
- Superseded runs are cancelled.

Trigger CI and reviews through the Discord bot (`/ci`, `/review`, `/devin review|run|status`)
or the `ci:full` label, not by editing config.

## Guardrails (MUST)

Do **not** edit CI/runner configuration (`.github/workflows/**`, Blacksmith runner labels,
caching, concurrency), Devin configuration, repo rulesets, or the MCP server list, unless a
Linear issue explicitly assigned by a human asks for it.

Do not add paid services or raise spend limits.
