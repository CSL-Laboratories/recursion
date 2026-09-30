---
title: Hermes Project Pilot Phase 7 Handoff
type: handoff
status: ready-for-review
last-verified: 2026-08-07
related:
  - CONTROL_PLANE.md
  - docs/architecture.md
  - docs/delegation-design.md
  - docs/autonomous-loops-architecture-and-roadmap.md
  - docs/plans/hermes-project-pilot-production-plan.md
  - docs/plans/hermes-project-pilot-phase0-evidence.md
  - docs/plans/hermes-project-pilot-phase6-handoff.md
---
# Hermes project pilot Phase 7 handoff

## Current state

- Phase 6 merge gate passed before authoring. Final implementation
  `b4b2924664793f4c7b60d507269e2bcdafd7d413` and final published handoff tip
  `e1dc1edad6b33e15f50c7a2dd795d274de87b90f` are both ancestors of merged
  `origin/master` at `44d9a2b1b01bf5bdd9a1723602e6e36070d0f7ac` (PR #27).
- Phase 7 red-control commit: `c7453771770bae171ae1b281a28000ae05c4e7bd`.
- Phase 7 final implementation OID: `b4d3a30317e7d11f8e31b5e2578eb53aaeb5155c`.
- Local review branch: `review/hermes-phase7-controls` in the clean worktree
  `C:\Users\drfoo\github\recursion-hermes-phase7`.
- Publication state: Lucas separately authorized the branch push after Phase 7 completed. The
  review branch is published; its initial remote tip was
  `2c022d9116fde584dccead79552da569515cdb18`. No PR was opened, and nothing was merged or deployed.
- Exact implementation proof: 17/17 baseline; every Phase 1-6 control and positive audit; all 16
  Phase 7 behavioral controls; and the Phase 7 positive architecture audit are green.
- Production remained PAUSE-gated throughout. No live pilot, autonomous production execution,
  project-concurrency increase, deployment, or human-visible test notification occurred.

The merge gate, pre-change matrix, red failures, exact-commit green commands/results, implementation
OID, and final live evidence are appended to
`docs/plans/hermes-project-pilot-phase0-evidence.md`.

## Implemented Phase 7 reporting path

Phase 7 adds reporting and notification fan-out without adding another scheduler, executor, state
store, publisher, delivery queue, notifier, worker launcher, or model-to-shell path. The only
mutation and delivery path remains:

```text
canonical append-only events
  -> notification-outbox.py schedule (observation only)
  -> idempotent schema-v1 decision
  -> durable action
  -> existing conduct-hermes.sh actuator
  -> notification outbox transition
  -> existing notify.sh sender
  -> durable transport evidence
  -> actuator-owned acknowledgement transition
```

`hermes-actuator.sh` invokes the deterministic outbox observer in the same supervised loop as the
Phase 6 goal reconciler. The observer only enqueues decisions. Five whitelisted action verbs own
event reconciliation, delivery, the Head Hermes portfolio result, audit refresh, and recovery.
`RECURSION_QUEUE_ACTUATION=1`, decision validation, and project-pilot/Head-Hermes actor checks keep
those mutations inside the existing queue actuator.

The implementation adds:

- strict durable destinations for one project topic, one Head Hermes topic, one Lucas direct
  account, and zero or more explicitly configured portfolio accounts/channels; project and pilot
  configuration must agree exactly;
- project events for goal accepted, plan approved, worker launched, task passed/failed/blocked,
  human answer received, review branch published, goal verifying, goal achieved, and goal stopped;
- exact delivery identity derived from `(event_id, destination)`, with one durable outbox record per
  pair and no send after a valid acknowledgement;
- independent per-destination attempts, bounded exponential backoff, a fixed retry budget, durable
  lifecycle history, and terminal dead letters with one existing escalation-index record plus a
  Lucas-attention outbox event;
- strict routing: project detail only to the project topic; a pilot terminal report only to Head
  Hermes; one subsequent portfolio result to Head Hermes plus configured portfolio destinations;
  direct messages only for explicit attention/dead-letter policies;
- transport evidence that must contain an accepted Telegram response with the exact chat/thread,
  message ID, event, delivery, and message digest before the actuator can acknowledge delivery;
- crash recovery that promotes a durable receipt without resending, rejects legacy/claimed success
  without transport evidence, reconstructs missing delivery pairs, and records correlated recovery
  snapshots across events, outbox items, acknowledgements, retries, dead letters, receipts, and
  existing delivery records;
- terminal reporting that first materializes the project pilot's complete evidence for Head Hermes,
  waits for that exact delivery acknowledgement, then emits exactly one portfolio result; and
- terminal audit records correlating Lucas's instruction, plan, durable actions, tasks, workers,
  commits, gates, independent verifier, review branch where achievement requires it, pilot report,
  Head Hermes report, and every durable delivery acknowledgement.

PAUSE remains a hard execution boundary in the existing actuator. The scheduler returns without
enqueuing new delivery work, the actuator does not consume already-pending work, status remains
readable, and durable event/outbox state is preserved for later reconciliation.

## Red-to-green proof

On untouched merged base `44d9a2b`, the Phase 7 control exited `1` with exactly 16 intended
failures because no Phase 7 outbox existed. The red suite was committed separately before
implementation. During green bring-up, only test-harness mechanics and stronger positive fixtures
were repaired: unbuffered progress output, a fully correlated rogue delivery, explicit missing-pair
recovery, durable actions in the terminal trace assertion, stable rotated-event identity, explicit
Lucas attention routing, and the dead-letter direct-delivery assertion. No falsifier was removed.

At exact implementation OID `b4d3a30`, all 16 cases pass using inert transports. They prove
retry-then-exactly-once delivery, no post-ack duplicates, independent destination retries, bounded
backoff/exhaustion, crash/reboot receipt recovery, dead-letter escalation, PAUSE, fail-closed
destinations, project/portfolio/direct isolation, one terminal Head Hermes result, complete terminal
traceability, recovery reconciliation, refusal of unsupported "sent" claims, and zero sends on
repeated completed ticks.

The full historical matrix was repeated on the same exact Git object. Precise commands and results
are in the Phase 0 evidence file.

## Production boundary

The pre-change read-only snapshot at `2026-08-07T19:01:51Z` found production `loop/auto` at
`9c70a860df2a2b68f4359cdbfeb9fdca1c877ad3`, PAUSE present with SHA-256
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`,
`hermes-gateway=active`, `recursion-hermes-gateway=inactive`, the supervisor timer inactive, and
zero `recursion-worker-*` units. The live Phase 1 audit retained exactly two deployment-gated
findings: the installed gateway is unrestricted/root, and installed `aiohttp==3.14.1` plus
`cryptography==48.0.1` have known advisories.

The final read-only snapshot at `2026-08-07T20:23:47Z` found the same production ref and service
state. PAUSE remained present before and after with the same SHA-256, worker-unit count remained
zero, and the live audit retained exactly the same two deployment-gated findings. It did not modify
production. The exact command and output summary are in the Phase 0 evidence file.

No Phase 1-7 deployment, live pilot, autonomous production execution, project-concurrency increase,
human-visible test notification, PAUSE removal, product-repository change, PR, merge, or Phase 8
work occurred. Except for the separately authorized Phase 7 review-branch publication, every such
action still requires separate authorization from Lucas.

The dirty user-owned `master` remains at `20c1fa77042f3aa9f5d8978352792f3b058bc0d1` with tracked
diff hash `2574b133121e0ea12eca06f5b8bd1d23449fc27a`; it was not an authoring or test source, and its
tracked and untracked changes remain user-owned.

## Copy/paste prompt for the Phase 8 session

```text
Continue the Hermes Project Pilot with Phase 8 only.

First read:
- C:\Users\drfoo\github\AGENTS.md
- Recursion's CLAUDE.md and ANCHOR.md
- the live /opt/recursion/CONTROL_PLANE.md on gs-recursion
- docs/architecture.md
- docs/delegation-design.md
- docs/autonomous-loops-architecture-and-roadmap.md
- docs/plans/hermes-project-pilot-production-plan.md
- docs/plans/hermes-project-pilot-phase0-evidence.md
- docs/plans/hermes-project-pilot-phase1-handoff.md
- docs/plans/hermes-project-pilot-phase1-deployment.md
- docs/plans/hermes-project-pilot-phase2-handoff.md
- docs/plans/hermes-project-pilot-phase3-handoff.md
- docs/plans/hermes-project-pilot-phase4-handoff.md
- docs/plans/hermes-project-pilot-phase5-handoff.md
- docs/plans/hermes-project-pilot-phase6-handoff.md
- docs/plans/hermes-project-pilot-phase7-handoff.md

The Phase 7 final implementation OID is
b4d3a30317e7d11f8e31b5e2578eb53aaeb5155c on the published review branch
origin/review/hermes-phase7-controls. Before any Phase 8 implementation or live action, fetch origin
and confirm Lucas reviewed and merged Phase 7, that the final implementation OID is an ancestor of
current origin/master, and that all Phase 7 handoff/evidence artifacts are present. The review
branch may already have been pruned after merge. If the implementation commit is not an ancestor of
origin/master or the artifacts are absent, stop and report the blocker.

Keep remote state/PAUSE enabled between every explicitly observed pilot window. Do not deploy,
change production flags or concurrency, lift PAUSE, send a human-visible notification, or start a
live pilot until the applicable Phase 8 preflight is complete and Lucas explicitly authorizes that
specific production action/window. Leave the dirty local master at 20c1fa7 and all user-owned
tracked and untracked changes untouched. If Phase 8 requires repository changes, fetch origin and
create a new clean Phase 8 authoring worktree/review branch from merged origin/master; never author
from the dirty master or a prior worktree.

Before changing code or production, reproduce and record the 17/17 baseline; every Phase 1-7
control suite; every Phase 1-7 positive audit; the live Phase 1 audit; the PAUSE hash before and
after; deployed ref; services/timer/worker state; project configuration; notification destinations;
branch protection; backup/restore readiness; and the exact rollback for each proposed live action.
Treat the two existing live Phase 1 findings as deployment gates, not waivers. Do not silently fix
or work around them on production.

Present a Phase 8 execution plan and obtain alignment before any production mutation. Execute
Phase 8 only, through the real hierarchy and existing canonical file/event,
decision/action/actuator, Phase 5 worker/verifier, Phase 6 goal-reconciler, Phase 7 outbox, and sole
notify.sh paths. Do not manually drive worker commands, create a parallel scheduler/executor/store/
publisher/sender, bypass exact-commit verification, or accept a model/process claim as evidence.

Run the observed pilots from the production plan in order, with a go/no-go gate and rollback between
them:
1. Success: one small reversible build-verifiable goal from Lucas instruction through the
   project pilot, bounded worker, independent verifier, review branch, terminal reports, exact
   delivery acknowledgements, and deterministic stop.
2. Assist: force a genuine ambiguity; prove pilot -> Head Hermes -> Lucas -> answer -> the
   same task resumes once without duplicate work or notification.
3. Offline: one existing offline machine-gated task only; no live game action; preserve the
   conformance/native-diff stack and prove no test, fixture, oracle, or baseline changed.
4. Parallel: two project topics and one goal each; prove state/worktree/memory isolation, global and
   per-project capacity, independent verification, reporting isolation, and no shared-state damage.
5. Soak: 24 observed hours covering scheduler ticks, gateway restart, host reboot, PAUSE during
   work, notification failure/recovery, usage/cost reconciliation, exact-once actions/deliveries,
   bounded review backlog, and deterministic terminal closure.

After each pilot/window, restore PAUSE, verify its hash/state, prove zero unintended workers/sends,
capture the exact instruction/goal/task/run/commit/gate/verifier/review/report/delivery audit chain,
and stop on any mismatch. Do not advance to the next pilot on partial evidence. Promotion requires
all production-plan gates: all controls green, no default-branch push or autonomous deployment
path, verified branch protection, demonstrated backup/restore and crash recovery, four completed
pilots with no lost or duplicate action/event/delivery, bounded review backlog, stable memory and
recovery, and explicit Lucas approval.

Create/update the Phase 8 handoff and append exact commands, results, production windows, rollback
evidence, pilot/soak audit chains, final deployed OIDs, and final PAUSE evidence to the project-pilot
evidence. Do not push a Phase 8 branch, open its PR, merge, promote beyond Phase 8, or prune evidence
unless Lucas separately authorizes the applicable action.
```
