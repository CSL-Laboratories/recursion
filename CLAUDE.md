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

Founders decide within the authority defined in `CONTROL_PLANE.md`: any founder may decide
product and implementation questions from research within that scope. Lucas retains the
reserved decisions and final tie-break; all three founders make the final release decision.
These instructions do not grant authority beyond that document or accept a pending contract.
You are a bounded executor: one task, one repository/worktree, explicit allowed paths,
explicit non-goals, and one machine-checkable acceptance gate. Work inside that boundary.
If a task could be read two materially different ways, stop and record a blocker in `state/INBOX.md` — do not pick one.

## Hard rules

1. **Never `git push --force`.** Under any circumstance, in any script, for any reason.
2. **Never commit to or merge into `master`.** Commit on a review branch and open a PR;
   founders merge after required reviews and checks. Agents never merge.
   Until rulesets enforce it, no founder merges their own PR without another founder's
   approval.
   Name the branch `csl-<N>-<slug>` (for example `csl-26-short-slug`), optionally prefixed with a GitHub username (for example
   `chaandansaini101/csl-26-short-slug`). Put `CSL-N` in the PR title so Linear links
   the issue. pr-lint matches `CSL-[0-9]+` case-sensitively, so a lowercase branch
   name alone does not pass. The `CSL-N` key in the PR title satisfies the check.
3. **Protected paths require a separately recorded founder-approved authoring session**
   under `CONTROL_PLANE.md`: `CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`, `.env`,
   and auth/credential configuration. Outside that explicit scope, propose changes in
   `state/PROPOSALS.md`; a product task does not authorize them.
4. **No secrets in tracked files, logs or notifications.**
5. **Every task needs a parent goal and a machine-checkable gate.** No gate, no work — it
   escalates.
6. **An implementer's PASS is not acceptance.** An independent verifier reruns the exact
   candidate; prefer a different model family.
7. **Generated output without verification is not progress.** If output outpaces review,
   generate less.
8. **Live provider/game/host actions, spending and quota resets are human-gated.** Agent
   execution is limited to the developer-approved own-account actions defined in
   `CONTROL_PLANE.md`, with the route, account and spend ceiling recorded, and only after
   the R2a permission-profile fix has landed. Lucas's company-resource and spending
   approvals still apply; quota resets require separate human approval. No live action,
   accepted contract, live capital or agent merge permission is authorized by these docs.
   Destructive or irreversible live actions, even on your own accounts, need explicit
   approval for each action. "R2a landed" means a merged, reviewed PR.

## Escalate, don't guess

Stop and write to `state/INBOX.md` when a task is ambiguous, needs a decision not recorded in
`state/DECISIONS.md`, conflicts with the research, or makes a claim you cannot verify.

## Working with the v3 specifications

`docs/v3` is non-normative until ratified. Do not reopen answered Owner questions or select a
language, store, framework or sandbox from a research recommendation. Keep evidence classes
distinct: source-documented, locally observed, inferred and unmeasured are different things.

## Recorded decisions

Before proposing architecture or direction, read `docs/adr/` and the linked
decision issues; do not re-argue a recorded decision without new evidence.

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
  on open same-repo PRs by verified repository writers (`.github/workflows/claude.yml`).
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

**Direction and architecture.** When you are the assignee (developer or their
coding agent working in the repo) of an issue that needs a decision or sets
direction, run Parallel Task API deep research before proposing, in
addition to Search/Extract. Post the cited brief (options, tradeoffs,
recommendation, target metrics) on the Linear issue. The developer decides,
or @mentions the approver; a coding agent @mentions its developer and does
not decide.

**Filing and shipping.**

- Branch from up-to-date `master` as `csl-<N>-<slug>` (example: `csl-26-short-slug`).
  A GitHub username prefix is optional (`chaandansaini101/csl-26-short-slug`).
- Open the PR as a **draft**. Batch commits into as few pushes as possible.
  Mark ready for review **once**, when the work is actually ready.
- PR title includes `CSL-N` so Linear auto-links. pr-lint matches `CSL-[0-9]+`
  case-sensitively, so a lowercase branch name alone does not pass. The `CSL-N`
  key in the PR title satisfies the check. Put `Fixes CSL-N` in the PR body —
  that links the PR, and merging moves the issue to **Done** automatically.
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

## Agent CI etiquette

Blacksmith minutes are paid. Agents and bots keep CI cheap:

- Open every PR as a **draft**. Batch pushes. Mark the PR ready **once**.
- Never merge a PR. Never rebase or merge `master` into a review branch unless
  there is a real conflict with `master`.
- Never edit `.github/workflows/**` or other CI/runner configuration. A
  human-assigned Linear issue is the only exception, and it must name the
  workflow files.
- Bots that mark a PR ready must use their own GitHub identity (GitHub App
  or PAT). Do not mark ready with `GITHUB_TOKEN`: GitHub does not run
  `ready_for_review` workflows for that token, so Devin Review and CI never
  start.

## Review roles

- **Devin** is the only GitHub PR reviewer. It does not auto-fix.
- **Claude Code Action** (`.github/workflows/claude.yml`) is the fixer for Devin
  findings and `@claude` mentions. It runs only on open same-repo PRs whose
  author has write, maintain or admin repository permission. `@claude` also
  requires a human commenter with that permission. Association is audit-only:
  private org members may appear as CONTRIBUTOR. Forks and nonwriters are skipped.
  Submitted Devin bot reviews wake the fixer directly; under the PR lock it
  refreshes the live head and reconciles unresolved, non-outdated findings,
  including roots on older commits.
  Codex never dispatches fixes. Every fixer shares the per-PR
  concurrency group, without cancelling an active run. Native `queue: max`
  retains up to 100 pending writers in queue-entry order; further arrivals are
  cancelled when full. This is bounded buffering, not an unlimited guarantee.
- **Stuck-thread watch** (`.github/workflows/stuck-thread-watch.yml`) asks Codex
  to mediate Devin threads Claude could not settle. A **MEDIATE** verdict
  updates one Linear roll-up per PR (`Stuck PR threads: recursion#<PR>`), not
  one issue per thread. Codex is a mediator, not a reviewer of record and not
  a fixer: it must not push code. The workflow may auto-resolve a thread after
  a trusted `CODEX-THREAD: RESOLVE` ruling (see [Review loop](#review-loop)).
- **Blacksmith** is the only CI.
- Cursor, Codex, Grok, and interactive Claude Code develop features from Linear
  issues. They must not post review verdicts or run review-fix loops on other
  agents' PRs, except the Codex `CODEX-GATE:` line in [Code Review Rules](#code-review-rules).
  The author agent may fix CI failures on its own PR.
- Humans still merge (`CONTROL_PLANE.md`). Independent task gates still apply; they are not
  a substitute for Devin PR review.

## When Claude can't fix a review thread

Devin reviews only and never fixes, so a thread is never handed back to Devin. Claude
leaves the thread open, replies explaining why it couldn't fix it, and @mentions the PR
author. If no `Stuck PR threads: recursion#<PR>` issue appears within one watcher
cycle, the PR author files it titled `Stuck PR threads: recursion#<PR>` with
`<!-- stuck-pr-threads:recursion#<PR> -->` as the first line of the description.

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
roll-up when the PR merges or every stuck thread is resolved. Codex is asked
to emit a per-thread `CODEX-THREAD: RESOLVE|DECIDE|MISSING|AMBIGUOUS` block
plus the inspected `HEAD` SHA. If the ruling is `RESOLVE`, that SHA is the
PR's current head, and required checks on that head are green, the watcher
replies on the thread citing the ruling and SHA, then resolves it
(`resolveReviewThread` via `CODEX_TRIGGER_PAT` — the same user token that
posted `@codex review`. `github.token` stays `pull-requests: read` and
would otherwise attribute the resolve to `github-actions[bot]`).
`DECIDE`, `MISSING`, `AMBIGUOUS`, a stale HEAD, or pending/failing
required checks stay on the needs-decision path. Human-authored threads
are never resolved unless Codex was explicitly asked about them (trusted
mediation marker or a human `@codex review` that names the thread id).
Caps: one mediation per thread and one auto-resolve per thread; three of
each per PR per UTC day. The watcher never pushes or merges.

Before merging any PR, check Linear for an open `Stuck PR threads: recursion#<PR>`
issue tied to it, and resolve the checklist or record the decision first. If no
such issue appears within one watcher cycle, the PR author files it titled
`Stuck PR threads: recursion#<PR>` with `<!-- stuck-pr-threads:recursion#<PR> -->`
as the first line of the description.

## Code Review Rules

Codex GitHub reviews route CI readiness and human mediation (CSL-102/CSL-201),
not fixes. Devin remains reviewer of record; Devin and human requests trigger
Claude. When ready or needing human mediation, end the Codex review with exactly one
of these lines (not in a code fence or only an inline comment):

CODEX-GATE: READY
CODEX-GATE: MEDIATE

- **READY** — no fixer; apply the `ci:full` label so the full CI matrix runs.
- **MEDIATE** — Devin and Claude are stuck; hand off to
  `.github/workflows/stuck-thread-watch.yml` for this PR. The watcher still
  applies its own stuck rules (Claude said it needs a human, or the thread
  has been open more than six hours after Claude's last reply), so a MEDIATE
  on fresh threads usually does nothing until a later scheduled run — it is
  not an immediate `@codex review`. Do not push, merge, or resolve threads.

If findings still need fixing without human mediation, report pending without
a verdict. Legacy `CODEX-GATE: FIX` is ignored and produces no acted marker;
missing verdicts never dispatch a fixer. No timeout/wait job exists. Historical
FIX/timeout markers do not block later READY/MEDIATE. A human `@claude` mention runs the mention
fixer immediately. There is no daily dispatch cap. The batched fixer pauses
after eight Claude commits since the last human commit; other bot commits
neither count nor reset the streak. A human commit re-arms it. Legacy
`bypass_cap` inputs are accepted for compatibility but never bypass this guard.

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

### Batched fixer retries and handled findings

Only unresolved, non-outdated roots from the verified Devin bot with valid
`devin-review-comment` metadata whose kind is exactly `bug` are eligible. Missing
or malformed metadata, analysis, and summary comments fail closed. Structured
Claude result receipts bind the root ID and exact body fingerprint. An addressed
receipt is honored only when its pushed fix descends from the recorded base and
is reachable from the live PR head. A `needs-human` receipt stops automatic
retries until a substantive trusted follow-up, changed finding, or authorized
manual retry. Arbitrary prose and failed/no-push replies never prove completion.

Automatic runs reserve at most three attempts per live head and unchanged
finding set under the shared PR lock. A new head or changed findings resets this
budget; it is not a daily cap. Exhaustion posts a human-action-needed notice,
never a readiness verdict. Authorized workflow dispatch bypasses this retry
budget and human-handoff receipts, but never the eight-commit guard. A human
`@claude` request also remains available. Pre-merge action validation can consume
a reserved attempt without running Claude; after activation on the default
branch, use an authorized manual retry if those reservations exhausted the budget.

A trusted current fixer pause invalidates older READY decisions. Codex READY
reconciles live unresolved findings and the current head before and after its
label write; MEDIATE remains available. The gate records ownership of its exact
`ci:full` label event. A later pause revokes only that owned label event, leaving
a subsequent human label override intact. Same-head recovery requires a fresh
verified readiness check; a READY comment alone cannot override a pause.

READY is reconciled from current unresolved findings and live label state, not
from old READY comments. An already-present label causes no repeat label or
audit writes. Authenticated MEDIATE deduplication remains separate. A verified
human label removal is preserved on the head where it is observed. A fresh
READY on a subsequent head can authorize CI again. If a label write cannot be attributed after
bounded event visibility retries, its pre-write intent blocks automatic READY
until an explicit human remove/re-add resolves the uncertainty; later runs never
claim ownership of that ambiguous label.

Handled receipts use version 3 and copy the exact observation token and durable
generation from preflight. The authenticated Actions observation journal records
each observed transition for a root, including its root-body fingerprint. Generations bind
the predecessor and current snapshot, so an observed added/deleted or
changed/reverted reply or root body cannot revive an older receipt. Duplicate identical
transitions reuse one generation; conflicting or incomplete histories fail closed
for human investigation. READY and all fixer/manual writers share the same
per-PR queue. Journal writes must succeed before a decision relies on them. Previously tracked
roots are reconciled before eligibility filtering, including malformed/removed
or non-bug metadata, resolved/outdated flags and absence from both complete API
views. Those inactive states advance history but never become fixer work.
Contradictory GraphQL/REST presence or identity fails closed; restoring a root
to eligible state cannot reuse its pre-inactivation generation. Before filtering,
thread IDs, root-to-thread identity and REST comment IDs are validated. Exact
identical pagination overlaps deduplicate; conflicting duplicates abort before
journal writes or eligibility decisions, including inactive and untrusted variants.
Every GraphQL root must corroborate a REST top-level root with matching body and
actor identity before filtering; the root sets must match in both directions.
Empty threads, missing/deleted authors, invalid flags, and dangling reply parents
fail closed as inconsistent snapshots. API lag is retried by a later invocation,
never interpreted as a clean finding set. Matching inactive/non-Devin roots stay
ineligible. A tracked tombstone requires absence from both complete views.

The snapshot binds meaningful trusted follow-up comment IDs, author IDs and
body digests. Exact unchanged content retains its previously authenticated
classification even if its author later loses permission. Content not previously
authenticated in this history requires current authorization; restoring exact
historical content still advances the generation when the state changes. Timestamps do not establish edit order. Legacy
receipts without durable generation proof cannot suppress findings; no deployed
receipt migration is assumed. Changes created and removed entirely between
observations are not detectable from the current GitHub API snapshot and are
not claimed as observed. Deleting trusted journal history can likewise remove
evidence; incomplete chains are rejected rather than guessed.

For an uncertain label write, inspect the label and use the exact command
printed in the gate notice: `/claude-fixer reconcile-label <nonce> <live-head-sha>`.
It must be the entire comment, without quotes, extra lines or surrounding text,
from a human with current write, maintain or admin permission. The nonce selects
one uncertain transaction and the head must still be current. This acknowledges
uncertainty only: it does not prove gate ownership, bypass unresolved findings,
override a same-head manual removal, or authorize unrelated work. An old-head
command does not carry forward after a push. Explicit remove/re-add remains a
fallback, but same-second removal cannot establish ordering and may require
repeating that sequence in a later timestamp second.

Ownership event visibility is retried after one and two seconds. A confirmed
HTTP rejection of the label POST can close its intent; a timeout, transport
error, server failure, or missing rejection receipt remains uncertain and
requires the manual sequence above.
