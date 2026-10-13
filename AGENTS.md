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
2. **Never commit to or merge into `master`.** Commit to `loop/work/<task-id>` and open a
   review branch; Lucas reviews and merges.
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

## Web lookups

Prefer the `parallel-search` MCP server (`web_search` / `web_fetch`) for web lookups. It is
declared in `.cursor/mcp.json` and `.mcp.json` (https://search.parallel.ai/mcp). No API key
is required.
