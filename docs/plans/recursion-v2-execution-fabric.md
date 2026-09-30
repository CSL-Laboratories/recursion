---
title: Recursion v2 execution fabric
type: implementation-plan
status: source-mvp-accepted
authorized: 2026-08-28
last-verified: 2026-08-31
execution_state: source-only-mvp-complete-production-pending
related:
  - CONTROL_PLANE.md
  - ANCHOR.md
  - docs/architecture.md
  - docs/audits/recursion-v2-infrastructure-audit-2026-08-28.md
  - docs/plans/engine-plugin-architecture.md
  - state/DECISIONS.md#d-016
---
# Recursion v2 execution fabric

Lucas authorized this control-plane authoring scope on 2026-08-28 after a read-only audit of the
local repository, live Hetzner controller, registered repositories, worker implementation, Cursor
surfaces, and Grok Bot.

This document is the autonomous execution handoff. It defines the intended architecture, ordered
implementation plan, phase gates, rollback boundaries, and completion criteria. It does not itself
authorize production deployment, credential changes, spending, PAUSE removal, default-branch
merges, or destructive cleanup. Those remain human seams under `CONTROL_PLANE.md`.

## 2026-08-31 implementation checkpoint

The source-only MVP is implemented and independently accepted. The completed slice includes strict
v2 records, the task-packet/context kernel, five provider adapters, deterministic placement and
durable recovery, depth-2 orchestration with immutable child budgets, independent verification and
review admission, the Telegram command/watchdog source boundary, and a source-only pilot.
The one-command gate is `python3 -u scripts/tests/recursion-v2-mvp.test.py`; exact SHAs and counts
are recorded in `docs/audits/recursion-v2-mvp-completion-2026-08-31.md`.

This checkpoint does not close the larger production plan below. Cursor Cloud is exercised through
fake transport, Codex and Cursor complete hermetic evidence flows, and no production credential,
provider call, Telegram delivery, second-box notification, deployment, restart, PAUSE change, or
metered spend occurred. Grok Bot, registered-machine live execution, production process isolation,
off-host restore, live outage/quota soak, migration cleanup, and Lucas's broader-autonomy
decision remain open. The phase descriptions are preserved as the production roadmap; source-MVP
completion must not be mistaken for their live completion.

## Objective

Make Recursion the configurable control plane for all registered development without binding it to
one CLI, model vendor, subscription, host, sandbox, repository layout, or execution environment.

The target system must be able to:

1. accept a capability-oriented task;
2. select an eligible execution pool according to human-owned policy;
3. bind the task to an immutable source and assignment;
4. execute locally, on a registered machine, or in a vendor cloud;
5. collect attributable evidence and usage;
6. reject stale or unauthorized results;
7. publish only a verified review candidate;
8. preserve one authoritative state and one human promotion boundary.

## Non-goals

- Replacing Hermes' executive authority.
- Letting an engine, Bot, or worker choose portfolio priorities.
- Letting workers edit credentials, routing policy, cron, PAUSE, or autonomy flags.
- Autonomous production deployment or merge to a protected/default branch.
- Kubernetes, a distributed database, or a heavyweight fleet-control platform.
- Treating multiple Grok Bots as isolated security principals.
- Rewriting historical tasks to make old paths look current.
- Enabling on-demand or metered fallback without Lucas's explicit policy.

## Core invariants

These apply to every phase. A phase that violates one has not passed.

1. Lucas remains final authority; Hermes remains executive; workers remain bounded executors.
2. PAUSE halts planning, execution, publication, transport, and external effects while preserving
   local awareness.
3. Tasks request capabilities and constraints. They do not permanently select a subscription.
4. Every run has one immutable assignment and one canonical evidence record.
5. No worker receives controller credentials or direct access to controller state.
6. No worker or vendor service writes a default or integration branch directly.
7. Source commits, result commits, diffs, gates, and assignment identities are verified
   independently of worker claims.
8. Provider unavailability and quota exhaustion are typed outcomes, not permission to spend.
9. Same-project integration is serialized even when implementation is parallel.
10. Historical state remains truthful and readable through versioned compatibility.
11. Every external delivery and retry is idempotent or explicitly reconciled.
12. Generated output that has not passed an independent gate is not progress.

## Target topology

```text
Lucas
  │ policy, approval, credentials, merge, production
  ▼
Hermes on gs-recursion
  │ portfolio, decomposition, routing intent, escalation
  ▼
Deterministic Recursion control plane on Hetzner
  ├─ registry and immutable assignment resolver
  ├─ durable queue, leases, reconciliation, and evidence
  ├─ quota/capacity governor
  ├─ independent verification
  └─ sole integration/review publisher
       │
       ├─ strict Hetzner Linux workers
       ├─ Cursor CLI workers on eligible machines
       ├─ Cursor Cloud isolated VMs
       ├─ Cursor My Machines / registered remote workers
       ├─ Sentry/local GPU or local-model workers
       ├─ Windows/live-oracle workers
       └─ Grok Bot supervisory arms
```

Hetzner is the coordinator, source of canonical evidence, and default light worker. Heavy builds,
GUI work, untrusted experiments, Windows-only work, and burst parallelism belong on compatible
remote hosts.

## Configuration model

Use versioned declarative registries. The exact serialized format may be JSON or YAML, but all
configuration must validate before it can participate in assignment resolution.

Suggested layout:

```text
config/
  engines/
  providers/
  capacity-pools/
  models/
  hosts/
  sandbox-profiles/
  routing-policies/
  projects/
```

Existing `scripts/engines/*.json`, `state/portfolio.tsv`, and `state/project-config/*.json` remain
supported during migration. One canonical v2 source must replace their duplicated routing facts
before legacy fields are removed.

### Engine

An engine describes a callable harness, not who pays for it or where it runs.

Required concepts:

- executable/runtime layout;
- argv template with structured output mode;
- supported roles and capabilities;
- health probe;
- cancellation and resume behavior;
- usage receipt adapter;
- compatible sandbox and host capabilities;
- required network capabilities;
- runtime files or read-only mounts.

The runtime-layout field is required to fix wrapper-based CLIs such as Cursor, whose executable
depends on adjacent runtime files.

### Provider

A provider describes service endpoints and protocol behavior:

- provider identifier;
- endpoint capability set;
- egress profile;
- receipt/usage semantics;
- error and quota classifications;
- data-handling classification.

It does not contain credentials.

### Capacity pool

A capacity pool represents the account, subscription, quota, or local resource paying for work.

```yaml
id: cursor-ultra-grok-bot
kind: supervisory
billing_basis: weekly-included
credential_ref: secret:grok-bot-routine
host_id: grok-bot-account-cloud
roles: [project-lead, planner, reviewer, browser-qa]
quota:
  period: weekly
  source: observed
on_demand: false
initial_concurrency: 1
fallback_policy: defer
```

Required concepts:

- account/subscription identifier without a secret value;
- billing basis: included, metered, local, or human;
- quota/reset source and confidence;
- spend ceiling and on-demand permission;
- allowed roles, projects, models, and hosts;
- concurrency and cooldown;
- fallback eligibility;
- health and availability state.

### Model

A model record describes capabilities and policy suitability independently of engine/provider:

- context and structured-output support;
- tool, terminal, image, browser, and computer-use capability;
- reasoning/cost/speed class;
- supported engines and providers;
- allowed roles and data classes;
- available effort/context parameters.

### Host

A host may be physical, virtual, intermittent, or vendor-managed:

- CPU, RAM, disk, OS, architecture, GPU, and GUI;
- available engines and runtime versions;
- sandbox backends;
- network/provider capabilities;
- data/trust classification;
- host-wide and workload-class concurrency;
- heartbeat, lease, and recovery protocol;
- intermittent wake behavior;
- evidence return channel.

Initial host classes:

| Host | Intended role | Isolation |
|---|---|---|
| `gs-recursion` | coordinator, verification, light Linux | strict systemd |
| `gs-worker` | registered Linux execution | remote Linux |
| `sentry-local` | GPU/local model and trusted special work | machine/workspace |
| `windows-oracle` | Cubic live oracle and Windows-only gates | human/live seam |
| `cursor-cloud` | burst implementation and disposable tests | vendor isolated VM |
| `cursor-my-machines` | Cursor-managed registered machine | declared per machine |
| `grok-bot-account-cloud` | persistent supervision and browser QA | account-shared |

### Sandbox profile

Retain the current systemd sandbox as one profile rather than the universal implementation.

Initial profiles:

- `strict-linux` — worktree-only write, provider-only egress, no controller visibility;
- `workspace-linux` — broader build tooling within one disposable workspace;
- `vendor-cloud` — vendor-managed VM with separate source-control trust;
- `remote-linux` — registered remote worker protocol;
- `windows-interactive` — Windows runner with explicit tool and path policy;
- `trusted-interactive` — human-observed exception, never selected silently;
- `account-shared-supervisor` — Grok Bot's retained shared-computer boundary.

### Project and publication policy

Each project must define:

```yaml
default_branch: main
integration_branch: recursion/integration
task_branch_pattern: recursion/task/<task-id>
external_branch_patterns:
  - cursor/*
  - recursion/extern/*
publication_policy: verified-integration-only
source_policy: exact-sha
allowed_paths: []
gate_commands: []
eligible_hosts: []
eligible_pools: []
```

Vendor-generated refs are inputs. After verification, Recursion may mirror the exact candidate
commit into its canonical task ref. Only the coordinator advances `recursion/integration`, and only
after serialized ancestry, diff, scope, and gate checks. Lucas merges the final PR to the default
branch.

### Immutable assignment

Before launch, resolve and persist:

```text
task_id
project_id
source_repository
source_branch
source_sha
seed_manifest_digest
engine
provider
capacity_pool
model and parameters
host
sandbox_profile
network_profile
allowed_paths
gate_digest
publication_policy
assignment_digest
created_at
```

The assignment is immutable for a run. Retries create a new attempt referencing the same assignment
or a newly resolved assignment. Evidence and usage read this record directly rather than
reconstructing facts from mutable task headers.

## Grok Bot adapter

Grok Bot is a supervisory backend, not an engine manifest and not a Cursor model alias.

### Initial roles

- persistent project principal;
- planning and decomposition critic;
- evidence and PR reviewer;
- cross-tool triage;
- browser/GUI acceptance preparation;
- human-facing status synthesis.

Implementation remains possible but is initially exceptional because all Bots share one computer.

### Transport

The first adapter should use an outbound signed routine webhook:

1. Lucas creates and tests a narrowly scoped routine in the Grok Bot desktop app.
2. The webhook URL and sender key enter the human-owned secret store.
3. A deterministic Recursion dispatcher records a durable dispatch attempt.
4. It sends only an immutable assignment envelope and non-secret repository references.
5. The routine rejects duplicate `dispatch_id` and `assignment_digest` values.
6. The Bot returns work through Git, a connected review surface, or a later narrow result endpoint.
7. Recursion polls/reconciles independently and records canonical evidence.

Do not automate the Grok Bot consumer UI, copy login cookies, or give the Bot SSH access to
`gs-recursion`.

### Trust boundary

- Treat Bot identity as account-scoped unless the product exposes cryptographically distinct
  per-Bot identity.
- Do not place deploy keys, controller tokens, `.env`, or production credentials on the shared
  computer.
- Use one repository/worktree per task.
- Start with one account-level code-changing lease.
- Make publishing, deletion, production changes, and external communication require approval.
- Treat Bot transcripts, screenshots, and self-reported tests as advisory evidence.
- Disable on-demand spend by policy unless Lucas explicitly enables it.

If a future Recursion MCP surface is exposed to Grok Bot, begin with read-only operations and a
proposal-only `request_execution` operation. It must not expose merge, deployment, credential,
PAUSE, routing, or arbitrary shell capabilities.

## Cursor Cloud adapter

Cursor Cloud is an isolated execution host and capacity pool.

Required adapter operations:

- list models and validate configured model IDs;
- create agent/run from exact `startingRef`;
- force isolated branch behavior;
- get/stream status with backoff;
- cancel and reconcile terminal state;
- list/download artifacts;
- retrieve token usage;
- record repository-specific returned branches and commits;
- archive completed sessions according to retention policy.

For multi-repository work, prefer a DAG of one-repository tasks until Recursion has a candidate-set
evidence schema. If one vendor run touches multiple repositories, record and verify source SHA,
result commit, diff checksum, and gate separately for every repository.

## Routing and placement

Tasks specify requirements rather than vendors:

```yaml
execution_profile: implementation
requirements:
  capabilities: [workspace-edit, terminal, structured-result]
  isolation: workspace
  operating_system: linux
  billing: [local, included]
  max_cost_usd: 0
preferences:
  implementer_family: any
  verifier_family: different
```

Routing policies are human-owned data. Initial deterministic policies:

- `manual-pin`;
- `subscription-first`;
- `local-first`;
- `cheapest-eligible`;
- `fastest-eligible`;
- `strongest-reviewer`;
- `weighted-rotation`;
- `no-metered-fallback`.

Selection must produce a trace explaining every eligible and rejected candidate. Quota exhaustion,
host loss, absent egress, missing runtime, incompatible sandbox, and unauthorized data class are
typed deferrals. They never silently relax requirements.

## Stale-base outcomes

Every completed candidate is classified before publication:

- `current-compatible` — source is still current and gates pass;
- `stale-reviewable` — default/integration advanced but the exact candidate remains useful;
- `stale-rebase-required` — candidate requires an explicit new task or reviewed rebase;
- `invalidated` — source or assumptions changed enough that the result cannot proceed;
- `source-unavailable` — the assigned source can no longer be resolved;
- `unauthorized-result` — returned branch, commit, paths, or identity violate assignment.

No result is silently rebased, merged, or overwritten.

## Execution phases

Each phase is a separate bounded review branch unless a later task contract deliberately narrows it
further. Do not combine infrastructure mutation, credential work, and production deployment in one
branch.

### Phase 0 — Freeze, reconcile, and re-baseline

Work:

1. Keep PAUSE asserted.
2. Snapshot live controller refs, dirty files, queues, leases, services, cron, PAUSE hash, and
   registered repository heads.
3. Preserve the dirty live `loop/auto` state in an immutable review artifact.
4. Reconcile live Recursion with current `origin/master` without force or default-branch mutation.
5. Correct stale board/projection state only through the reviewed state path.

Gate:

- every live commit and dirty path has a disposition;
- canonical source and deployment refs are explicit;
- zero workers and no unowned leases remain;
- PAUSE and service awareness remain intact;
- rollback restores the exact pre-phase baseline.

### Phase 1 — Versioned contracts and registries

Work:

1. Define schemas for engine runtime layout, provider, capacity pool, model, host, sandbox,
   routing policy, project publication, and immutable assignment.
2. Implement validators with distinct fail-closed outcomes.
3. Add compatibility readers for v1 manifests, portfolio rows, project configs, task headers, and
   evidence.
4. Move routing facts toward one canonical source while preserving current behavior.

Gate:

- malformed or unknown fields fail closed;
- no secret values can validate in tracked manifests;
- legacy fixtures and current builtin execution remain regression-identical;
- a dry resolver emits a deterministic assignment and rejection trace.

### Phase 2 — Repository and branch contracts

Work:

1. Extend every active project descriptor with source, integration, task branch, gate, allowed
   paths, host/pool eligibility, and publication policy.
2. Add canonical candidate import and serialized integration planning.
3. Make source SHA mandatory for new tasks.
4. Implement typed stale-base outcomes.

Gate:

- no executor can target a default or integration branch;
- duplicate candidate import is idempotent;
- advancing a remote branch during a run cannot overwrite new work.

### Phase 3 — Assignment-native evidence and usage

Work:

1. Persist the resolved assignment before launch.
2. Include assignment identity directly in leases, attempts, evidence, usage, and scorecards.
3. Stop deriving engine/provider/account facts from mutable task headers.
4. Add remote session, branch, commit, artifact, and cancellation evidence.
5. Add account/pool attribution and quota-period reconciliation.

Gate:

- every new run answers who executed, where, using which pool/model/sandbox, against which source;
- evidence digest changes if any assignment field changes;
- late, duplicate, or mismatched results fail closed;
- no unapproved metered run can be reconciled as included usage.

### Phase 4 — Coordinator hardening and durability

Work:

1. Split or narrow root actuator responsibilities.
2. Review SSH, firewall, forwarding, and inbound-service policy.
3. Add encrypted off-host backup, retention, log rotation, and a restore drill.
4. Add provider-specific egress profiles and proxy injection.
5. Run skipped Linux sandbox and ownership tests on Hetzner.

Gate:

- controller secrets and state are unreadable to every worker profile;
- an off-host restore reconstructs the documented recovery point;
- unauthorized provider egress returns a typed refusal;
- PAUSE remains effective across restart and failure windows;
- no new public listener is required for the initial Grok outbound bridge.

### Phase 5 — Cursor CLI and registered-machine backend

Work:

1. Package or mount Cursor's complete runtime tree read-only inside the sandbox.
2. Verify current headless argv, structured output, model selection, cancellation, and exit codes.
3. Authenticate only the intended runner through a human credential window.
4. Add the Cursor provider egress profile.
5. Add My Machines/registered-machine capability discovery behind a disabled adapter.

Gate:

- the runner can execute a harmless read-only probe without controller visibility;
- prompt/model injection tests remain argv-safe;
- missing auth/runtime/egress produces distinct refusal codes;
- usage is attributed to the correct Cursor pool;
- no plugin runs without both human-owned enable flags.

### Phase 6 — Cursor Cloud backend

Work:

1. Implement API authentication by secret reference.
2. Add create/status/stream/cancel/artifact/usage operations.
3. Bind every run to exact repository and source SHA.
4. Reconcile returned branch/commit and import only verified candidates.
5. Recover active vendor sessions after controller restart.

Gate:

- default-branch writes are impossible by request construction and post-check;
- host loss, timeout, cancellation, malformed response, and API rate limit are tested;
- vendor self-report cannot substitute for independent verification;
- quota exhaustion cannot activate on-demand spend silently.

### Phase 7 — Grok Bot supervisory backend

Work:

1. Create a narrow routine and webhook interactively on safe test inputs.
2. Implement an outbound dispatcher with durable attempts, redaction, deduplication, and retry
   reconciliation.
3. Define project-lead, reviewer, and browser-QA assignment templates.
4. Add account-shared host and weekly capacity-pool records.
5. Return results through review branches or a narrow proposal/evidence channel.

Gate:

- a duplicate dispatch cannot create duplicate code or publication;
- PAUSE prevents dispatch;
- the Bot has no controller/deploy credentials;
- code results are independently fetched and verified;
- approval remains required for publishing, deletion, production, and external communication;
- weekly exhaustion defers instead of spending unless Lucas explicitly changed policy.

### Phase 8 — Distributed placement and integration

Work:

1. Add host heartbeat, capability registration, leases, timeout, and recovery.
2. Route work independently of subscription and host identity.
3. Permit parallel implementation across repositories.
4. Serialize same-project candidate integration.
5. Add host/pool/quota status to the board and briefs.

Gate:

- incompatible host/sandbox/network combinations cannot be selected;
- controller restart reconstructs remote in-flight state;
- duplicate delivery and late results remain harmless;
- different repositories overlap while same-project integration remains serialized;
- review throughput can cap generation below available token capacity.

### Phase 9 — Pilots, soak, and cleanup

Work:

1. Run a full operational soak covering cron, gateway/actuator restart, host loss, provider outage,
   Cloud/Grok cancellation, quota exhaustion, stale branches, and publication retry.
2. Remove hardcoded Claude/task-routing assumptions only after compatibility evidence passes.
3. Update architecture, operator runbooks, decisions, and status projections.

Gate:

- no duplicate actions, unauthorized writes, orphaned sessions, stale active pilots, or unbounded
  spend;
- all active projects have explicit branch and placement policy;
- restore and rollback procedures are exercised;
- Lucas reviews the soak evidence before PAUSE removal or broader autonomy.

## Acceptance matrix

The complete program is not finished until all of these are machine-tested:

- Changing an active capacity pool routes the same task without editing the task.
- Manual-pin, subscription-first, local-first, cheapest, fastest, strongest-reviewer, and weighted
  policies are deterministic.
- Quota exhaustion selects only an approved fallback.
- Every run records engine, provider, pool/account, model, host, sandbox, source SHA, and assignment
  digest.
- An endpoint absent from the selected network profile causes a typed defer/refusal.
- Prompt and model values remain single argv elements under injection attempts.
- Workers cannot read controller state, credentials, host sockets, or paths outside their workspace.
- Host placement rejects incompatible capabilities.
- Advancing a source or integration branch produces a stale outcome and never overwrites new work.
- External Cursor/Grok refs cannot target default or integration branches.
- Parallel repositories overlap; same-project integration is serialized.
- PAUSE prevents every local and remote dispatch and publication path.
- Controller restart reconciles rather than duplicates in-flight remote work.
- Legacy Claude/Hermes behavior remains regression-identical until deliberately retired.
- Linux production controls are tested on Linux; Windows skips are never counted as production proof.

## Rollback model

Every phase must define rollback before mutation:

- schema changes retain v1 readers until all live state migrates;
- feature flags default disabled and remain human-owned;
- new services deploy side-by-side before cutover;
- remote adapters can be disabled without invalidating canonical state;
- no phase requires force-pushing or rewriting default-branch history;
- integration branches are disposable review artifacts, not production;
- credential and firewall windows have separate operator runbooks and receipts.

## Autonomous-session boot sequence

The executing Codex session should begin here:

1. Read `CLAUDE.md`, `CONTROL_PLANE.md`, `ANCHOR.md`, D-015, D-016, this plan, and the dated audit.
2. Confirm the requested scope still authorizes a control-plane authoring session.
3. Fetch all relevant repositories and repeat the read-only live preflight.
4. Confirm PAUSE and record its exact hash before and after every live audit batch.
5. Compare current facts to the dated baseline; record drift instead of silently updating this plan.
6. Create one parent goal and bounded phase tasks with explicit dependencies, allowed paths,
   non-goals, and machine-checkable gates.
7. Execute phases in dependency order. Do not collapse credential, deployment, or destructive
   cleanup windows into code-authoring tasks.
8. Publish only review branches through the repository's canonical publisher.
9. Stop at every human seam: credentials, spend, firewall exposure, production cutover, PAUSE
   removal, destructive cleanup, and default-branch merge.
10. Mark the program complete only when the acceptance matrix and soak gate have independent
   evidence.

## Completion definition

Recursion v2 is operational as the basis for development when:

- every active repository has an immutable source, integration, task, gate, and publication policy;
- tasks are capability-oriented and provider-neutral;
- at least Hetzner, Cursor CLI, Cursor Cloud, Grok Bot, and one registered remote machine participate
  through the same assignment/evidence model;
- every result is independently attributable and verifiable;
- quota and spend policies fail closed;
- controller restart, stale branches, duplicate delivery, and host loss are proven safe;
- no other project's product code is duplicated as live-looking code in Recursion;
- a full soak passes without unauthorized writes, orphaned work, silent spend, or review backlog;
- Lucas explicitly approves broader autonomy and removes PAUSE through the human-owned procedure.
