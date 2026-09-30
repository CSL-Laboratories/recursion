# Delegation & the build loop — design of record

Status: historical v1 design; superseded by the source-accepted Recursion v2 MVP on 2026-08-31.
Read `ANCHOR.md`, `architecture.md`, and `recursion-v2-mvp-harness.md` first. This document records
the original build-loop rationale; the current implementation makes Plane 2 (the execution arms) real:
it specifies how the supervisor turns a project goal into work, hands it to the workhorse AIs,
CHECKS the result, and integrates it — safely.

## Where this sits

v1 (live, 2026-08-05) is a **supervisor**: heartbeat -> brief -> escalate, plus an arm
*interface* (`dispatch.sh` can run a hermes/claude/openrouter worker and put it on the BOARD).
It does not yet decide what to build or check what comes back. This doc is that missing brain.

## The load-bearing principle: autonomy rests on ACCEPTANCE, not trust

Grounded in Lucas's own benchmark — **generation is solved across cheap models (5/5, 74/74);
verification is not (best 0.73).** So the design never trusts a generator's output. Every task
carries a **machine-checkable acceptance test**, and autonomy is granted only where that test
exists and passes. No acceptance check -> no autonomy (it escalates instead). This is the whole
safety model in one line: *the cheap model writes, a strong check (Opus or a machine oracle)
gates, and only a green gate earns an auto-commit.*

## The routing map — who does which work

| Role | Engine | Cost | Used for |
|---|---|---|---|
| Bulk generation / breadth | OpenRouter -> Deepseek-class, price-sorted | metered (fenced) | first drafts, boilerplate, many-in-parallel coverage, test-case generation |
| Depth + verification | Claude Opus, `claude -p` | flat Max (off 24/7 path) | hard multi-file coding, architecture, and CHECKING the cheap output |
| Planning + gating | GPT 5.6 Terra/Sol on the sub | flat (~$0) | turn a goal into tasks, score acceptance ambiguity, decide auto vs escalate |
Correctness-preserving pattern: **Deepseek generates -> Opus or a machine oracle verifies.** Cheap
where generation is solved; strong only where verification isn't.

## The task model

A task is a small file in `state/tasks/<id>.md` (or a `.jsonl` queue — decide at build time):

```
id:         task-001
workstream: <workstream>
goal:       "Scaffold an app that builds and serves a landing page"
engine:     claude            # or deepseek | hermes
acceptance: "npm ci && npm run build && test -d out"   # MUST exit 0 to pass
autonomy:   auto              # auto | escalate  (per-task override of the ceiling)
status:     queued            # queued | running | passed | failed | escalated
branch:     loop/work/task-001
```

The **acceptance line is the contract.** If it can't be written as a command that a machine can
run and score, the task is not eligible for autonomy — it escalates for a human check.

## The pipeline

```
project goal
  -> PLAN     (Sol/Terra reads goal + status, emits tasks with acceptance tests)
  -> QUEUE    (state/tasks/, one file per task, status=queued)
  -> EXECUTE  (executor pulls a task -> dispatch.sh spawns the arm -> output captured)
  -> VERIFY   (run the acceptance command; capture pass/fail + logs)
  -> INTEGRATE
       pass + reversible + autonomy=auto  -> commit to loop/work/<id>, status=passed  [Lucas's choice]
       fail                               -> retry once, then status=failed + escalate
       ambiguous / irreversible / metered / mining -> status=escalated (INBOX + Telegram)
  -> REPORT   (BOARD row per arm; daily brief summarizes passed/failed/escalated)
```

Every arm appears on `state/BOARD.md` (running -> passed/failed) with tokens + $; the executor
respects `state/PAUSE` like every other job.

## Autonomy & integration (decided 2026-08-05)

- **Auto-commit to a per-task work branch on acceptance pass.** Branch `loop/work/<task-id>`,
  off the default branch, **never main, never merged** by the loop. Lucas reviews/merges branches
  at his pace (GitHub Desktop). Reversible by definition — an unreviewed branch changes nothing live.
- **Escalate (block on Lucas)** when: the acceptance test fails after one retry; there is no
  machine-checkable acceptance test; the change is irreversible or spends money/proxies; it
  touches mining (DO-NOT-DEPLOY); or it needs a decision not in `DECISIONS.md`.
- The metered leg (Deepseek/OpenRouter) stays fenced behind `RECURSION_ALLOW_METERED=1` and is
  watched by `fallback-watch.sh`; a bulk-dispatch that would spend is itself an escalate-gated act
  until a per-project bulk budget is set.

## Per-workstream autonomy ceilings

Autonomy scales with machine-verifiability (architecture.md).

| Workstream | Ceiling | Why / constraint |
|---|---|---|
| fleet | medium | auto the reversible; escalate costly/risky; mining = DO-NOT-DEPLOY |

## Safety & guardrails (inherited, non-negotiable)

- `state/PAUSE` halts the executor like every job. Kill switch first.
- Never main, never `git push --force`, never merge (CLAUDE.md rules 1-2). Loop output ->
  `loop/auto`; build output -> `loop/work/<id>`.
- The loop never edits its own config/auth/routing/cron (rule 3) — findings go to INBOX.
- No secrets in tracked files; project status is derived from live registered git clones and must
  never be fabricated when a clone/ref/file is unavailable (D-011).
- No acceptance test -> no autonomy. A stopped task with a clear question beats plausible wrong work.

## Built vs to-build

| Component | Status |
|---|---|
| Arm interface (`dispatch.sh`: hermes/claude/openrouter) | ✅ v1 |
| BOARD registry, escalation-with-handle, PAUSE, fallback-watch | ✅ v1 |
| Task model + `state/tasks/` queue | ✅ v2 (2026-08-05) |
| Executor (`work.sh`: pull -> arm -> verify -> integrate) | ✅ v2 (2026-08-05) |
| Auto-commit to `loop/work/<id>` (isolated worktree) + BOARD reporting | ✅ v2 (2026-08-05) |
| Gate control (`work.selftest.sh`: green integrates, red escalates) | ✅ v2 (2026-08-05) |
| `recursion work` / `recursion tasks` CLI | ✅ v2 (2026-08-05) |
| Planner (`plan.sh`: goal -> tasks with acceptance) | ❌ (tasks hand-written for now) |
| Deepseek bulk budget + per-project fences | ❌ |

## Arm economics — measured 2026-08-05 (load-bearing)

The build-arm cost model is NOT what the roster implied. Measured on gs-recursion:

- **The Codex OAuth sub does not serve tool-calling turns.** Text-only briefs run free on
  `openai-codex`, but the moment an arm needs a file/terminal tool, hermes falls back to
  metered OpenRouter. So there is **no free hermes file-writing arm** — `engine: hermes` is a
  real build arm but is FENCED behind `RECURSION_ALLOW_METERED=1` like every metered leg, and
  its cost lands on the BOARD.
- **`claude -p` is the free-ish file-writing arm** (flat Claude Max) — but it needs a one-time
  interactive `claude /login` on the box; before that it exits 0 having written nothing (the
  gate catches the no-op and escalates). Logged in 2026-08-05; the first build task ran green at $0.00.
- Net routing for BUILD tasks today: `engine: claude` = free-ish depth/scaffold arm (default);
  `engine: hermes` = metered fallback (fenced); `engine: deepseek|openrouter` = raw
  chat-completion, still needs a file-emission protocol before it can write a tree (later).

## Open decisions (for the build session)

1. **Task authoring** — human-written task files vs Sol-generated from a project goal (start human, add Sol later?).
2. **Acceptance strictness** — one command exit-0, or a small suite? How to handle flaky checks.
3. **Resolved by D-011.** Recursion reads project status from live registered clones; it does not
   maintain hand-pushed `state/projects/*.md` mirrors.
4. **Deepseek default model** + a monthly bulk budget ceiling before a dispatch escalates.
5. **Concurrency** — how many arms in parallel per workstream (VPS is a CX23; thin clients, but cap it).
6. **How much the daily brief reports** on build progress (passed/failed/escalated counts) without bloating the one-page cap.

## First build slice — PROVEN 2026-08-05

The minimal vertical that proves the machine ran end to end: task `task-001` (hand-written,
`state/tasks/task-001.md`) -> the claude/Opus arm scaffolded an app in an isolated
worktree on `loop/work/task-001` -> the executor ran the acceptance command
`npm install && npm run build` -> it compiled clean (`✓ Generating static pages (4/4)`) ->
auto-committed to `loop/work/task-001` (ba02110) at $0.00 on flat Max -> BOARD shows it.
The gate control (`work.selftest.sh`) separately proves a KNOWN-BAD task fails and escalates,
so a green is meaningful and not a false pass.

Next candidates in safety order: workstreams whose acceptance is fully VPS-verifiable first, then
those that need a machine-oracle bridge. A `plan.sh` that emits tasks from a goal, and a real
Deepseek bulk leg with a budget, remain the next build steps.
