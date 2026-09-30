---
title: "Parallel loops — per-workstream concurrency for the build-loop"
type: plan
status: current
last-verified: 2026-08-05
related:
  - "docs/autonomous-loops-architecture-and-roadmap.md"
  - "scripts/work.sh"
  - "scripts/drain.sh"
  - "scripts/conduct.sh"
---

# Parallel loops

**Goal (roadmap item 1b).** Build the *capacity* to run several workstreams at once before we load
heavy work onto the box. Today execution is SERIAL:
one global `state/.work.lock` means one `work.sh` across the whole box, and `drain.sh` runs queued
tasks strictly one at a time. The subs are $0 and under-loaded, so **capacity is the constraint, not
cost** (Lucas, 2026-08-05).

**Granularity: one build per WORKSTREAM, N workstreams at once.** Not "N of the same workstream" —
tasks in one workstream must serialize (step N+1 builds on step N's merged base; see the build-chain
bootstrap rule), and tasks that share a baseline must serialize. The parallelism that is safe and wanted is
*across* workstreams. So the unit of concurrency is the workstream.

## Design (extends the proven serial code; does not fork it)

### 1. `work.sh` — per-workstream lock replaces the global lock
- Resolve the task FIRST (we need its `workstream:` to name the lock), then acquire
  `state/.locks/ws-<workstream>.lock` (mkdir-atomic; `ws-none` if unset).
- Effect: at most one build per workstream; different workstreams never block each other.
- **Backward-compatible.** `drain.sh`, `cycle.sh`, and a manual `recursion work <id>` only ever run
  one `work.sh` at a time, so the per-ws lock behaves exactly like the old global lock for them.
  Only `conduct.sh` (below) launches several at once.

### 2. `work.sh` — serialize the ledger commit
The **only** shared mutable state under parallelism is the loop/auto git index in the main `$ROOT`
tree: `commit_state()` does `git add -A state/ && git commit`. `board.sh` and `set_status` (a
`sed -i` on a per-task file) do **not** touch the git index, so they are safe concurrently. Wrap
`commit_state`'s git ops behind a short `state/.locks/ledger.lock` (spin ≤30s, then proceed
best-effort). Serial callers acquire it instantly. Trade-off: `git add -A state/` under the lock may
sweep another workstream's just-written status into this commit — the *content* stays correct, only
commit attribution is coarse. Acceptable for a ledger.

### 3. `work.sh` — per-repo worktree lock
`mk_worktree` runs `git worktree remove/prune/add` + a `fetch` on `REPO_DIR`. Two workstreams that
share a repo (e.g. two recursion-internal streams both in `$ROOT`) could race git's ref/index locks.
Guard those ops with `state/.locks/wt-<hash(REPO_DIR)>.lock` (released right after `worktree add`,
  before seeding). Cross-repo streams never contend, so this only bites the same-repo case.

### 4. `conduct.sh` — the parallel continuation engine (new; drain.sh stays as the serial fallback)
- Launches up to `RECURSION_CONDUCT_CONCURRENCY` (default **2**) builds at once, **at most one per
  workstream** (tracks running workstreams; `work.sh`'s ws-lock is the backstop).
- Same bounds as `drain.sh`: `RECURSION_DRAIN_MAX` (total tasks), `RECURSION_DRAIN_FAILSTOP`
  (non-passes → stop launching), `RECURSION_DRAIN_DEADLINE_MIN`, and `state/PAUSE` (checked each
  poll). Single-instance via `state/.conduct.lock`; **stands down if `state/.drain.lock` is held** so
  the two engines never double-drive the queue.
- Poll loop: reap finished PIDs → update counters → launch next-queued task of any *idle* workstream
  while slots + budget remain → sleep. On stop, let in-flight builds finish (bounded), then reap.
- **Concurrency default is 2, not `nproc-2`.** The box has 2 cores, but the arms are *remote-API*
  bound (`claude -p`, delegate), not local-CPU bound, so cores don't gate throughput; the real cap is
  the number of workstreams with queued work. Tune via the env var.

### 5. Wiring (human-gated, rule 3)
- `bin/recursion` gains a `conduct` verb.
- `cycle.sh` step 4 (auto-drain) calls `conduct.sh` **only when `RECURSION_CONDUCT=1`**, else
  `drain.sh`. Default (flag unset) = serial, unchanged. The loop can't self-set the flag; Lucas flips
  it in `.env` when he wants parallel autonomous draining. Until then, `recursion conduct` runs it
  on demand.
- `.gitignore` gains `state/.locks/` (runtime lock dirs, never committed).

## Safety / guardrails preserved
- No change to the acceptance-gate contract, the env fences (`ALLOW_PUSH`/`ALLOW_AUTOMERGE`), or the
  path-scoped ff-only auto-merge. Parallelism only changes *how many* gated builds run at once.
- PAUSE still halts everything (checked by cycle, conduct, and every work.sh).
- No new autonomy is enabled by default — `RECURSION_CONDUCT` is off until Lucas sets it.

## Test plan
1. `bash -n` all changed scripts.
2. Two synthetic no-op tasks in **different** workstreams (engine `none`, trivial acceptance) →
   `recursion conduct` → both run concurrently (overlapping timestamps in the log), both pass.
3. Two tasks in the **same** workstream → conduct serializes them (second waits for the first).
4. `state/PAUSE` mid-run → conduct stops launching, lets in-flight finish.
5. Confirm serial `recursion drain` still works unchanged (default path).
