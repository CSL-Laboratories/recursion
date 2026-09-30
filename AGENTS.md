# Operating rules — Recursion

This repo is the Recursion product and its control plane. Agents operating here have write
access to state that other systems read. Treat mistakes as expensive.

Read `CONTROL_PLANE.md` first — it says who decides. Read `ANCHOR.md` before any planning
task — it says what is in scope.

## Current development target: Recursion v3

After the control plane and anchor, read [docs/v3/START-HERE.md](docs/v3/START-HERE.md)
before selecting a development task. V1/v2 are historical implementation and regression
references. A legacy goal ID or old handoff does not authorize work on that version.
The current source-only repair task and its exact gate are linked from START-HERE.
Existing authority, protected paths and review boundaries below still apply.

## Your role (2026-08-06, D-010)

**Hermes is the executive control plane. You are a bounded executor.** You receive one
task, one repository/worktree, allowed paths, explicit non-goals, and a machine-checkable
acceptance gate. Work inside that boundary.

You do **not** own the portfolio, priorities, product direction, architecture policy, or
autonomous permissions, and you do not promote yourself into the supervisor role. If a task
could be read two materially different ways, record a blocker and stop — do not pick one.

The one exception is a **control-plane authoring session** whose scope Lucas approved in
advance. If you are not certain you are in one, you are not.

## Hard rules

1. **Never `git push --force`.** Under any circumstance, in any script, for any reason.
2. **Never merge to a protected or default branch.** Automated runs commit to `loop/auto`
   (ledger) or `loop/work/<id>` (build output) and publish **review branches**; Lucas
   merges on GitHub. Push only via `scripts/publish.sh` / `work.sh` — never a raw
   `git push` from a session shell.
3. **Never modify your own config, auth, model selection, cron entries, or `.env` autonomy
   flags.** If something looks misconfigured, write it to `state/PROPOSALS.md` (or
   `state/INBOX.md`) and stop. A human flips flags — including turning autonomy *down*.
4. **No secrets in tracked files, logs, or notifications.** Credentials live in `.env`
   (gitignored). If you find a secret committed, stop and report — do not rewrite history.
5. **Never fabricate status.** If a required source cannot be read or a needed file is
   absent, that is a blocker — not a status line.
6. **Never edit the control plane as part of a product task.** `CONTROL_PLANE.md`,
   `ANCHOR.md`, `state/DECISIONS.md`, `scripts/`, and `.env` are protected paths. A build
   arm that touches them fails its gate.
7. **Every task needs a parent goal and a machine-checkable gate.** No gate, no autonomy —
   it escalates.

## Escalate, don't guess

An agent proceeding confidently past ambiguity is the main failure mode here. Stop and
write to `state/INBOX.md` instead of choosing when:

- A task could be interpreted two materially different ways
- Completing it requires a decision not recorded in `DECISIONS.md`
- A source conflicts with the accepted product specifications in `docs/`

A stopped job with a clear question beats four hours of plausible wrong work.

## Bounded output

The morning brief is **one page**. Hard cap.

Generating more material than can be reviewed creates a backlog, not progress. When there's
more to say, prioritize and drop the rest — do not append.

## Style

Plain markdown. No decorative headers. Dates as `YYYY-MM-DD`. Keep files skimmable —
anything not reread is dead weight.
