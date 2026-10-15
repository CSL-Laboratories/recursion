# Operating rules — Recursion

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
  Claude fixer job (`devin-review-fix`) acts on its findings, then required
  CI reruns on the fixer's push. No human approves or requests changes on a PR until all
  of these have finished on the current head commit. A review on a stale head does not
  count. After new pushes, the reviewer reviews again. Review requests and their status
  are posted as @mention comments on the CSL Linear issue, and a watcher posts there when
  a PR is ready for review.
- Autonomously file research findings, ideas and bugs to Triage, deduped.
- Ground every implementation in the best available research: Parallel search for web
  research and Context7 for current library docs.

> **How CSL works:** the Linear doc [How CSL works](https://linear.app/csl-laboratories/document/how-csl-works-922e0df6026f)
> is background on the team and tooling. It does not change the reading order below;
> `CONTROL_PLANE.md` is still read first and wins on any conflict.

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
   review branch; Lucas reviews and merges. Put the Linear key (`CSL-N`) in the task id
   (for example `loop/work/CSL-26`) and in the PR title so Linear still links.
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
  (https://search.parallel.ai/mcp). No API key. Never put secrets, proprietary code,
  private data, or unreleased research details in queries — keep them generic. Anything
  sensitive needs human approval first.
- **Context7 MCP** — up-to-date library docs (https://mcp.context7.com/mcp). Works without a
  key. Same two config files, alongside Parallel.
- **Linear** — source of truth for work. Workspace **CSL laboratories**, team key **CSL**,
  project **recursion**. Use the Linear MCP (`https://mcp.linear.app/mcp`) or the Devin
  Linear integration. Declared in `.cursor/mcp.json` and `.mcp.json`.
- **GitHub** — org **CSL-Laboratories**.
- **Devin** — GitHub PR reviewer only (on PR creation and on demand). See
  [Review roles](#review-roles).
- **Claude Code Action** — PR fixer for Devin findings and `@claude` mentions
  on same-repo OWNER/MEMBER PRs (`.github/workflows/claude.yml`).
- **Blacksmith CI** — only CI. See [CI](#ci).
- **Google Workspace** (`csl-laboratories.com`) — meetings and notes.
- **Discord bot** (`csl-bot-operations`) — Liaison. Commands and MCP; see below. Trigger CI
  and reviews through these (or the `ci:full` label), not by editing config.
  - Commands: `/issue`, `/plan`, `/debug`, `/feature`, `/standup`; `/ci full|run`;
    `/devin review pr:<url>|run|status`; `/telemetry`, `/help`, `/connect`. There is no
    top-level `/review` and no `/ci` without an argument.
  - MCP: `https://csl-discord-bot-production.up.railway.app/mcp`, declared in
    `.cursor/mcp.json` and `.mcp.json`. Human-assigned authorization:
    [CSL-65](https://linear.app/csl-laboratories/issue/CSL-65) (Lucas; parent CSL-26).
    Each dev mints a bearer token via `/connect`. Never commit the token; reference it
    via env var `CSL_DISCORD_BOT_MCP_TOKEN` only.
  - Tools: `issue_create`, `issue_list`, `issue_status`, `issue_assign`, `plan`, `debug`,
    `feature`, `standup`, `ci_full`, `ci_run`, `devin_review`, `devin_run`,
    `devin_status`, `telemetry`.

## Linear-first workflow

Every change starts from a Linear issue (`CSL-N`). Be Linear-aware and proactive. Authority,
protected paths, and merge still follow `CONTROL_PLANE.md`.

**Before starting.** Find or create the issue (team **CSL**, project **recursion**) via the
Linear MCP or the Devin Linear integration. Do not start code without `CSL-N`. When you
start, assign yourself and move the issue to **In Progress**; unassign if you stop.

**Filing and shipping.**

- Branch `loop/work/<task-id>` with `CSL-N` in the task id (example: `loop/work/CSL-26`).
  Do not use Linear's copied branch name as the git branch.
- PR title includes `CSL-N` so Linear auto-links (example: `CSL-26: ...`). Put `Fixes CSL-N`
  in the PR body — that links the PR, and merging moves the issue to **Done** automatically.
- One issue per PR; keep PRs small and stackable.
- Comment progress on the issue. If the PR does not move the issue to **In Review**, move
  it yourself.
- Route issues with `for-devin`, `for-claude`, or `for-cursor`. Use `needs-decision` when a
  human direction call is needed.
- **Shared files.** [`.github/workflows/linear-pr-sync.yml`](.github/workflows/linear-pr-sync.yml)
  upserts one Linear comment per PR, marked `<!-- pr-sync:<repo>#<n> -->` (edit in
  place, never a second comment). It shows the PR link, author, purpose, status
  (draft/open/merged/closed), mergeable state, head SHA, Devin review on head,
  and the top changed files. A push to the default branch refreshes every open
  PR and flags `out of date / conflicts with <default-branch>`. **Grok Bots
  must read those Linear PR-sync comments before touching shared files** —
  three people work at once. This workflow is not a required check.
- On your own PR, address CI comments, then resolve the threads. Eligible Devin
  findings and `@claude` mentions are handled by the Claude Code Action
  (`.github/workflows/claude.yml`; see [Review roles](#review-roles)). Do not
  run review-fix loops on other agents' PRs.
- Humans make the load-bearing decisions (architecture, specs, merge order). Agents propose;
  humans decide.

**Follow-ups.** When work reveals follow-ups, bugs, or next steps, create new **Triage**
issues: one Type label, the repo label `recursion`, the `recursion` project, and a link back
to the parent issue or PR. Not drive-by changes on an unrelated PR. Ambiguous tasks and
unverifiable claims still stop in `state/INBOX.md`.

**Assignees.** The three founders are Lucas (NotSkittlz), Chaandan (CDanTheMan), and Samir
(samir-shah-ahmed). All are admins, and ownership changes over time, so never hardcode
owners. Check the current assignee or project lead in Linear. Assign new issues to the owner
of the parent issue, or leave them in Triage unassigned when unclear.

## Linear is how we communicate

Any agent or Grok Bot working on an issue keeps it current in Linear as work progresses:
status, a short progress comment at meaningful milestones (started, blocked, PR opened,
done), and the PR link.

When blocked or needing help or approval, comment on the Linear issue and @mention the
relevant teammate (the issue owner or project lead; handles listed above are the current
ones, not a fixed owner list). A GitHub mention alone is not enough. Add the
`needs-decision` label for approvals.

Anything that comes up during a dev session gets filed as a new Linear issue in **Triage**
instead of being dropped: ideas discussed, bugs or oddities found in development, and
areas to develop or test further. Link it to the current issue when related, and dedupe
against existing issues first.

Linear is the team's primary async channel, alongside Google Meet and Discord.

## CI

- Required check: `linux` (Blacksmith).
- Windows and macOS run on the `ci:full` label, on manual dispatch, and on `master` (macOS
  nightly at 07:17 UTC).
- Superseded runs are cancelled.

Trigger CI and reviews through the Discord bot (`/ci full|run`, `/devin review
pr:<url>|run|status`, or MCP `ci_full` / `ci_run` / `devin_review`) or the `ci:full`
label, not by editing config.

## Review roles

- **Devin** is the only GitHub PR reviewer. It does not auto-fix.
- **Claude Code Action** (`.github/workflows/claude.yml`) is the fixer for Devin
  findings and `@claude` mentions. It runs only on open same-repo PRs whose
  author is an org OWNER or MEMBER. `@claude` also requires a human OWNER or
  MEMBER commenter. Fork and nonmember PRs are not auto-fixed.
- **Stuck-thread watch** (`.github/workflows/stuck-thread-watch.yml`) asks Codex
  to mediate Devin threads Claude could not settle. A **MEDIATE** verdict
  updates one Linear roll-up per PR (`Stuck PR threads: recursion#<PR>`), not
  one issue per thread. Codex is a mediator, not a reviewer of record and not
  a fixer: it must not push code.
- **Blacksmith** is the only CI.
- Cursor, Codex, Grok, and interactive Claude Code develop features from Linear
  issues. They must not post review verdicts or run review-fix loops on other
  agents' PRs. The author agent may fix CI failures on its own PR.
- Humans still merge (`CONTROL_PLANE.md`). Independent task gates still apply; they are not
  a substitute for Devin PR review.

## When Claude can't fix a review thread

Devin reviews only and never fixes, so a thread is never handed back to Devin. Claude
leaves the thread open, replies explaining why it couldn't fix it, and @mentions the PR
author.

## Review loop

Devin reviews. Claude fixes (`.github/workflows/claude.yml`). If a Devin thread
stays stuck — Claude said it needs a human / could not fix, or the thread is
still open more than six hours (configurable) after Claude's last reply —
[`.github/workflows/stuck-thread-watch.yml`](.github/workflows/stuck-thread-watch.yml)
posts one `@codex review` comment (via `CODEX_TRIGGER_PAT`; skipped with a
notice if that secret is missing). A Codex **MEDIATE** verdict updates one
Linear Triage roll-up per PR titled `Stuck PR threads: recursion#<PR>`, labeled
`needs-decision`, with a checklist of stuck threads (update that issue; do not
file one issue per thread). The watcher skips drafts, merged/closed PRs, and
PRs that are approved on the current head with green checks. It cancels the
roll-up when the PR merges or every stuck thread is resolved. Humans decide.
Caps: one mediation per thread, three per PR per UTC day. The watcher never
resolves threads, pushes, or merges.

Before merging any PR, check Linear for an open `Stuck PR threads: recursion#<PR>`
issue tied to it, and resolve the checklist or record the decision first.

## Automations

Devin automations (**Main CI failure fixer** and **Weekly dead-code sweep**, Wednesdays
10 AM PT) are human-managed. Other agents must not create, edit, or trigger Devin
automations, and must not edit Devin settings.

## Post-merge

- A CI failure on `master` triggers the Devin fixer, which opens a Linear Bug (run id in
  the title) and a fix PR. The Discord bot posts the failure to the alerts channel.
- The weekly dead-code sweep opens a Linear Chore plus a cleanup PR.
- The Discord bot sends a weekly digest of stale PRs and issues.

## Security

- Never commit secrets (hard rule 4).
- GitHub secret scanning and push protection are enabled. A blocked push means remove the
  secret and rotate it. Never bypass.
- Dependabot security updates are on. Its PRs follow the normal review flow.

## Performance

Speed regressions must be flagged. A benchmark CI step is tracked in Linear; until it
lands, note perf impact in the PR description for performance-sensitive changes.

## Guardrails (MUST)

Do **not** edit CI/runner configuration (`.github/workflows/**`, Blacksmith runner labels,
caching, concurrency), Devin configuration or settings, Devin automations, repo rulesets,
or the MCP server list, unless a Linear issue explicitly assigned by a human asks for it.

Do not add paid services or raise spend limits.
