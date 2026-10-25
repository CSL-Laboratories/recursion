---
title: Recursion v3 interface strategy (CLI, TUI, GUI)
status: amendment proposed (2026-10-24); acceptance is a human decision by Lucas
last-updated: 2026-10-25
prior-status: accepted-design-baseline
authority: non-normative until ratified
---

# Recursion v3 interface strategy

Adopted 2026-10-02 as the interface design baseline (`state/DECISIONS.md` D-024). Source is the
2026-10-02 scope-reframe strategy, intaken at
[scope-reframe-20261002](research-lane/scope-reframe-20261002/raw/01-current-strategy/Recursion_Interface_and_Product_Strategy.md),
section 3 and section 7. This file selects no language, GUI framework or transport. Command names
and layouts are design candidates, not runnable product instructions.

## Division of responsibility

| Layer | Owns | Must not own or imply |
|---|---|---|
| Headless control/query services | Admission, durable command receipts, object identity, current authority, configuration validation, typed capability negotiation, role bindings, event ordering, recovery and evidence projections | Continuous inference merely to maintain status; authority inferred from a UI tab or source text |
| Scriptable CLI | Deterministic discovery, explicit commands, batch inspection, configuration transactions, stable JSON/JSONL, shell completion, model-free diagnosis and recovery | Scraping chat prose to infer state; a second settings database |
| Interactive TUI | Live conversation, focused work views, keyboard-first steering/queueing, inbox, split/attached views, task switching, compact inspect panels | Owning actor/process lifetime; hidden model invocation on attach; full log repaint on every token |
| Later GUI | Spatial project/session navigation, artifact/diff review, visually clear lineage, context comparison, richer provenance inspection, accessible responsive views | Different command meanings; bypassing the common admission path; a mandatory always-on dashboard |
| Optional transports and observers | Authorized projections and separately scoped control operations | Treating channel membership or message delivery as task authority |

Amendment proposed 2026-10-24 (acceptance is a human decision by Lucas): the "Later GUI"
row keeps its object-graph meaning. Timing is superseded — the GUI is a **first-release
client** of the same headless control/query services (daemon-served web GUI in a thin Tauri
shell, plus standalone app and CLI/TUI). See [AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md).
The GUI must not own actor lifetime (window-close ≠ Stop).

Views never own agent lifetime. The observer interface is read-only by default; control is a
separate authenticated capability. A local CLI need not become a publicly listening service.

## The object graph users should understand

Presentation projections of existing records, not parallel writable state:

- **Project / workspace** — repository roots, classification, source/worktree bindings, views.
- **Goal / task** — accepted GoalSpec and plan revision, parent, gates.
- **Assistant / role** — persistent identity, role lease, authorized task membership.
- **Run / attempt / session / turn** — distinct IDs and epochs; a new provider session need not create a new goal.
- **Operation / effect** — canonical operation, attempts, recipient, effect class.
- **Question / approval / queued input** — durable ID, target, expiry, revision.
- **Checkpoint / context epoch** — canonical references, capsule manifest, compaction owner.
- **Artifact / candidate / evidence** — source and output identities, producing operation, verification binding.
- **Route** — harness/interface/build + provider/model + pool + credential reference + host/profile.
- **Configuration / plugin / node** — effective revision, component artifact, host incarnation.
- **Budget / reservation / meter segment** — parent pool, reservations, units, observation source.

Do not present a model picker as the whole route picker; do not equate a chat session with a
task, an assistant identity with its current model, or an artifact with an accepted result.

## State is a set of facts, not one optimistic badge

A task can simultaneously have an interrupted upstream turn, no live owned local processes, one
unreachable worker and one unresolved external operation. For commands, distinguish **saved
locally** versus **accepted** versus **delivered/applied** versus **observed outcome** versus
**settled/unresolved effects**. For messages, `accepted`, `delivered` and `adopted` differ. For
code, `generated`, `tested`, `reviewed`, `accepted` and `published` differ. For configuration,
`compiled`, `validated`, `staged` and `effective` differ. If there is a replay gap, show a
gap/reconstruction marker rather than inventing the missing story.

## Command families (candidates)

- **Enter and find work:** `recursion`, `workspace open`, `project list`, `task list`, `attach task <id>`, `attach head`, `view events`.
- **Understand current state:** `status`, `task show <id>`, `events --task <id>`, `queue explain`, `route explain`, `budget show`, `doctor`.
- **Talk and control:** `message send --delivery steer|queue`, `interaction list|answer|cancel`, `approval show|respond`, `stop --mode aggressive|full|lazy`, `resume`.
- **Preserve and continue:** `checkpoint show`, `context inspect|compare|compact|rehydrate|doctor`, `fork preview|create`, `route rehearse`, `handoff preview|apply`.
- **Review work:** `artifact list|show`, `diff`, `evidence show|verify|export`, `verification show`, `research claims|sources|gaps`.
- **Configure the platform:** `init`, `configure`, `config get|explain|diff|validate|apply|migrate`, `plugin list|show|explain`, `provider list|auth`, `model list`, `node list|show|doctor`.
- **Maintain and recover:** `daemon status|start|stop`, `doctor --component`, `update preview|status`, `recovery inspect|plan`, safe-mode/read-only recovery.

Every name is a design candidate. Commands are visibly labeled deterministic control, assistant
request or engine extension. Every name compiles into one command schema; do not create competing
semantics for `goal stop`, `arm stop` and a top-level Stop. Machine output: stable `--output
json` for objects, `--output jsonl` for events; typed errors with code, precondition, whether an
effect was attempted, and retryability; explicit receipt command ID and idempotent retries.

## All ten controls

| Requirement | Shared invariant |
|---|---|
| Streaming output | Bounded, task/turn-tagged; show gaps and source identity; never invent hidden reasoning |
| Questions | Correlated, durable answer/cancel with expiry; stale answers cannot address replacement waiters |
| Approvals | Inspect exact action, arguments, revision, scope; reuse valid prior authorization |
| Queued messages | Durable list, edit/remove before delivery; `accepted` is not `delivered` |
| Active steering | Target the current attempt/turn; display the actual delivery receipt |
| Interruption | Explicit mode, target/scope and admission fence |
| Resume | Validate current route/config/grants and unsettled effects; reattachment alone never resumes effects |
| Forks | Preflight source and reservation split; preserve both versions |
| Compaction controls | Inspect/request/tune supported mechanism; prior evidence rehydratable |
| Commands/plugins | Namespaced typed commands with supplying component and version; no origin-based authority |

An affordance must not claim a control is supported merely because it can be rendered; check the
exact route/interface/version/configuration combination and show the precise loss. A deferred
integration is labeled outside the release cut, not counted as supported.

## Three Stops

| Mode | Scope and boundary | Required visible result |
|---|---|---|
| Aggressive | Forcefully interrupt the selected agent without waiting; fence further dispatch | Local observation plus any uncertain in-flight effects; descendants listed separately |
| Full | Selected agent and its owned subagents, shells, monitors and relaunchers | Each local owned process settled or unresolved; separate remote receipts and restart fences |
| Lazy | Close admission and stop at the next observable operation boundary | Named boundary and deadline, waiting/timeout state, configured escalation |

An upstream interrupt, socket close or killed PID is not external rollback. Stop must remain
responsive while streams, history and views are slow.

## Demonstration journeys (release acceptance scenarios)

From the 2026-10-02 strategy, adopted as acceptance scenarios (D-024):

1. Native to upstream to native without pretending the loop is portable.
2. Explainable compaction and recovery after the upstream session is gone.
3. Full Stop with an unreachable node and an unresolved external operation.
4. Fork an unfinished dirty source tree and preserve both versions.
5. A worker disconnects, the owner detaches, and the node reconnects.
6. Choose an honest subscription quality tradeoff and review the result.

Each journey must both reject invalid behavior and complete useful authorized work, measured
against strong source-supported baselines.

## Non-goals

- No graphical frontend in the initial release; terminal first, GUI later.
- No view, tab, channel or message delivery confers authority.
- No natural-language proposal silently becomes an authority change.

Amendment proposed 2026-10-24 (F-2, answered by Lucas R-Q4; acceptance is a human decision
by Lucas): the first non-goal is **proposed superseded**. The GUI is a first-release client
over the same command/query daemon as the CLI/TUI, with the most polished visuals. The
second and third non-goals stand. Demonstration journeys are to be completable in the GUI
as well as the CLI (research RR4). Full text: [AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md).
