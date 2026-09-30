---
title: Hermes Project Pilot Phase 8 Handoff
type: handoff
status: blocked
last-verified: 2026-08-13
related:
  - CONTROL_PLANE.md
  - docs/architecture.md
  - docs/delegation-design.md
  - docs/autonomous-loops-architecture-and-roadmap.md
  - docs/plans/hermes-project-pilot-production-plan.md
  - docs/plans/hermes-project-pilot-phase0-evidence.md
  - docs/plans/hermes-project-pilot-phase7-handoff.md
  - docs/plans/hermes-phase8-preflight-operational-nuances.md
  - docs/plans/hermes-phase8-routing-recovery-handoff.md
---
# Hermes project pilot Phase 8 handoff

## Current state

> **SUPERSEDING SNAPSHOT — 2026-08-13.** The historical snapshots below remain incident
> evidence, but they are not the current deployment state. Continue from
> `docs/plans/hermes-phase8-routing-recovery-handoff.md`.

- The reviewed hardening series through PRs #54-#56 is merged at `origin/master`
  `73bfc402822c1b43841e8b715fe8f8c446ad709f` and deployed under PAUSE as production
  merge `0561bca6294a1eae2b3d31765ac3a3f7fb248c5b`.
- The exact PAUSE file remains present, `root:root`, mode `0644`, 189 bytes, SHA-256
  `919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`.
- `hermes-actuator`, `hermes-gateway`, `cron`, `recursion-provider-network`, and
  `recursion-provider-proxy` are active and enabled with zero restarts. Pending/running
  decisions, worker units, and matching worker processes are all zero.
- The expired v1 pilot window was lawfully closed by instruction 699 and remains closed.
  It must never be reused.
- Fresh-plan instruction 707 was accidentally routed to Head Hermes because incidental
  wording matched the broad deployment routing expression. Two supervised Head Hermes
  turns emitted unsupported noops; the hardened noop guard failed both actions safely.
  Instruction 707 remains open. Recovery instruction 732 is also open and has not run.
- Do not run another identical model turn, manually edit either instruction, or lift PAUSE.
  The next work is a reviewed routing/recovery fix, followed by lawful coupled closure of
  707 and 732. Gate 6 has not started.

### Historical Phase 8 snapshots

The bullets below are retained in chronological order. Statements such as "Phase 8 is not
deployed" describe their capture time and are superseded by the snapshot above.

- The Phase 7 merge gate passed before Phase 8 authoring. Phase 7 implementation
  `b4d3a30317e7d11f8e31b5e2578eb53aaeb5155c` is an ancestor of merged
  `origin/master` at `7aa6e0f44ffd5145d15c38dfad893ed46389308b` (PR #28), and the Phase 7
  handoff/evidence artifacts are present.
- Phase 8 red-control commit: `234d49b46197b7e120ca96b7efdfdc904ba87ec0`.
- Phase 8 implementation commit: `7d0dc10fcb5914f98353be1f73deb15498e71b7c`.
- Merge state: Lucas reviewed and merged Phase 8 through PR #29 as exact merge commit
  `9422b9cc5bf4270b7184b9af6fa31cae8e6a6876`; all four Phase 8 commits are ancestors and the
  review branch was pruned.
- The Phase 1 remediation evidence was subsequently merged through PR #31 as exact current
  `origin/master` `b23069ee49519fda5896f43ec888efd71b533167`; evidence commit
  `71e651ad958fa0e50904e7730e711ed39e6e2d82` and Phase 7 implementation `b4d3a303...` are
  ancestors, and the review branch is pruned.
- Exact Linux proof: 17/17 historical baseline; every Phase 1-7 behavioral control and positive
  audit; all six Phase 8 plan/window/assist controls; the real Linux parallel-capacity control; and
  the Phase 8 positive architecture audit are green at the exact implementation OID.
- The separate Phase 1 compatibility remediation was reviewed and merged through PR #30 as
  `fa0263587479b3c43b0ba6bb61f5b176383f7cec`, then successfully deployed under PAUSE using frozen
  Hermes `498061d8f1dcd855419b30e8729c869596a89ca6`. The two former live Phase 1 deployment findings
  are closed; live Phase 1, dependency/OSV, systemd security, rollback, and independent closure
  gates pass.
- The Phase 8 deployment preflight found production `loop/auto` at
  `73987156f5d22d2f2f50ddc542c978d0c48f88b8`, containing merged `origin/master` `fa026358...` as
  an ancestor. PAUSE remains asserted at SHA-256
  `919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`.
- Lucas authorized one fresh PAUSE-held Phase 8 actuator deployment. The exact reviewed unit
  reached active/running state, but its first deterministic PAUSE-safe queue transition was
  blocked by five legacy pre-schema records in `state/decisions.jsonl`. Automatic rollback
  restored the runtime snapshot, cron, and the sole valid pending decision and removed the unit.
- Phase 8 is therefore still not deployed. PAUSE remains exact; `hermes-gateway` and cron are
  active; the actuator is absent; workers and project processes are zero; model routing remains
  unset; capacity remains zero; and all nine outboxes remain paused and empty. No PAUSE lift,
  observed pilot, human-visible notification, product-repository mutation, or soak occurred.
- The evidence-correction branch was reviewed, merged through PR #34 as `52f6ad37250d7b7bc798d86c04768aef8cc09c07`,
  and pruned. The separate PAUSE/resync correction is now reviewable as red commit `2688671...`,
  implementation commit `1a16d81...`, and historical-control harness commit `5bbfad5...`. Exact
  Linux proof is green; none of these commits is deployed.

The exact commands, OIDs, results, live snapshot, GitHub inventory, rollback gates, and raw-log
hashes are appended to `docs/plans/hermes-project-pilot-phase0-evidence.md`.

## Why any previous plan is not executable

Any pre-Phase-8 goal or legacy task state predates the Hermes plan-synthesis contract and is not a
valid autonomous Phase 8 plan. Phase 8 therefore does not reopen, reinterpret, or silently upgrade
it.

A pilot can begin only from a fresh Lucas instruction that produces a new, schema-complete plan
with new plan, goal, and task identities. The plan must contain:

- a bounded Phase 6 budget and complete Phase 5 worker/verifier task envelopes;
- an acyclic dependency graph, exact allowed paths, and explicit non-goals;
- non-vacuous task and terminal gates;
- review-branch-only publication, with credentials, merge, and deployment excluded; and
- one deterministic stop condition and bounded prose fields.

The proposal is stored at `state/plans/<project>/<plan>.json`. Head Hermes cannot materialize its
goal or tasks until a separate authenticated Lucas instruction names the exact plan ID and SHA-256
digest. Replaying the approval is idempotent. A collision, stale digest, malformed plan, reused
identity, missing field, cycle, vacuous gate, or partial materialization fails closed.

## Implemented Phase 8 path

Phase 8 uses the existing hierarchy and adds no parallel scheduler, executor, publisher, store,
sender, or worker launcher:

```text
Lucas instruction
  -> authenticated intake record
  -> logical project pilot or Head Hermes planning invocation
  -> schema-validated decision
  -> existing durable decision queue
  -> existing conduct-hermes.sh sole actuator
  -> fresh immutable plan proposal
  -> separate Lucas exact-digest approval
  -> Phase 6 goal + Phase 5 task DAG materialization
  -> separate Lucas exact-goal bounded window authorization
  -> scheduler/actuator admission recheck
  -> Phase 5 bounded worker + independent verifier
  -> Phase 6 reconciliation and review-branch publication
  -> Phase 7 outbox -> existing notify.sh -> exact delivery acknowledgement
  -> deterministic terminal closure
```

The implementation provides:

- event-invoked logical GPT project pilots with a project-scoped live brief; `RECURSION_PILOT_MODEL`
  is required and fails closed when no human-owned model alias is configured;
- Head-Hermes-only plan approval, window approval/closure, and assist-answer routing;
- durable pilot windows that are armed while PAUSE is present, activate only on the first unpaused
  scheduler observation, admit exactly one named goal for a bounded duration, and are rechecked at
  scheduler and final worker/action boundaries;
- automatic closure of every open pilot window when PAUSE is observed, so a later resume cannot
  replay stale authority;
- compare-and-swap assist resumption of the same blocked goal and task exactly once, with failed
  actuator actions reopening rather than losing their originating instruction;
- direct pilot notification suppression so Phase 7 remains the only human-visible outbox path;
- global worker capacity through `RECURSION_PILOT_GLOBAL_CONCURRENCY` plus one active worker per
  project, implemented as filtered claims in the existing decision queue and sole actuator;
- PAUSE-safe draining only for inert plan/approval/window/assist state transitions, never worker,
  gate, publication, or delivery work; and
- backup coverage for plans, windows, decisions, actions, goals, tasks, assists, runs, events,
  reports, and delivery state.

The checked-in pilot records gain only
`"window_required": true`. Their production concurrency remains zero. No model alias or global
concurrency value was configured.

## Red-to-green and exact-commit proof

On untouched merged base `7aa6e0f`, the Phase 8 falsifier overlay reported `0/6 controls passed`.
The red suite was preserved in its own commit before implementation. It covers fresh plan shape,
atomic rejection, exact-digest approval, exact-goal bounded admission, PAUSE revocation, and
same-task assist resumption.

At exact implementation OID `7d0dc10`, those six controls pass. The independent Linux parallel
test also proves two projects overlap when global capacity is two while a second task from either
project remains blocked by per-project capacity one. The complete 17-command Linux matrix ended:

```text
MATRIX_TOTAL=17 MATRIX_FAILED=0
```

Every individual command log ended `RC=0`; `matrix2.err` was empty. The disposable Linux bundle and
archive fixtures were removed after the logs were copied and hashed locally. No test used the live
production tree, transport, credentials, project worktrees, or product repositories.

## Read-only production preflight

The final snapshot at `2026-08-08T17:18:24Z` found:

| Gate | Observed state |
|---|---|
| deployed checkout | `loop/auto` at `87189cd6e43ecc3d91f78881b82fb2186c7b2ebe`; Phase 7 merge and implementation are ancestors |
| tracked production changes after Phase 7 merge | awareness-only board/brief/event/inbox/escalation files |
| untracked production file | `.env.bak.1786014440`; pre-existing/user-owned, not read or changed |
| PAUSE | present, `root:root`, mode `0644`, 189 bytes, SHA-256 `919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db` before and after |
| services | `hermes-gateway=active`; hardened `recursion-hermes-gateway=inactive`; supervisor timer/service inactive |
| workers | zero `recursion-worker-*` units; zero active project-worktree processes |
| delivery state | zero notification events and zero deliveries for all nine registered projects |
| pending execution | no decision/action/run/lease directories and no pending/running decision or action files |
| pilot capacity | every pilot `concurrency_limit=0`, `max_inflight_workers=0`, publication false |
| runtime routing | `RECURSION_PILOT_MODEL`, `RECURSION_DECIDE_MODEL`, and `RECURSION_PILOT_GLOBAL_CONCURRENCY` unset |
| live Phase 1 audit | return `1` with exactly the two known deployment gates: unrestricted/root gateway and advisories in `aiohttp==3.14.1` plus `cryptography==48.0.1` |
| branch protection | authenticated protection endpoint returned HTTP 403 for every registered repository; none is verifiably protected |
| backup/restore | exact fixture backup/restore and crash recovery green; production script present and PAUSE/lock-gated; no production snapshot/restore drill yet authorized or demonstrated |

The deployed OID differs from the Phase 7 handoff's earlier snapshot because the PAUSE-safe
awareness cadence created board/morning commits after merging `origin/master`. Phase 8 did not
create them. The production `origin` cannot currently authenticate with its default recursion key;
that observation was read-only and was not repaired or bypassed.

The two live Phase 1 findings, unverified default-branch protection, absent production
backup/restore drill, unset pilot model alias, zero configured capacity, and unpublished/unmerged
Phase 8 implementation are deployment and pilot gates, not waivers.

> **SUPERSEDED 2026-08-11 (post-merge preflight).** Two rows of the table above and this
> paragraph were accurate when captured on 2026-08-08 and are retained for that reason.
> Both have since been closed and re-verified read-only: the live Phase 1 audit now returns
> `ALL PASS` with `rc=0` (104 packages audited, gateway hardened non-root) per "Corrected
> Phase 1 deployment succeeded under PAUSE" below, and all eight classic branch-protection
> rules are active and field-audited per "Branch-protection follow-up" below. The remaining
> gates are the unset pilot model alias, zero configured capacity, and the undeployed
> Phase 8 code.

## Rollback gates for future authorized actions

Every item below is a runbook, not current authorization. Before each action, recapture the exact
deployed OID, PAUSE hash, service state, queue/outbox counts, and a manifest-hashed runtime snapshot
outside the checkout. If the deployed OID moves from the value above, that newly observed OID
becomes the rollback OID only after the same read-only checks pass.

1. **Phase 8 deployment.** Keep PAUSE asserted; quiesce writers; take and verify the runtime
   snapshot; deploy only Lucas-reviewed merged code. On any mismatch, keep/reassert PAUSE, stop the
   supervisor and all worker units, restore the pre-deploy snapshot into staging and verify its
   manifest, then deploy a reviewed inverse commit returning the code to the captured pre-deploy
   OID/tree. Never reset, force-push, or use the dirty local master.
2. **Success window.** PAUSE is removed only after the new plan digest and exact goal/window
   instruction are recorded. Rollback is to restore the same 189-byte PAUSE file and verify its
   SHA-256, observe automatic window closure, terminate/verify zero worker units, leave any review
   branch unmerged, and reconcile the goal to a deterministic stopped state. Restore the pre-window
   runtime snapshot if any chain object is missing or duplicated.
3. **Assist window.** Use a distinct exact-goal window. Rollback is identical, plus prove the
   assist and instruction remain durably correlated and the task has not resumed more than once.
   Never fabricate an answer or create a replacement task.
4. **Offline window.** Capture the product HEAD plus hashes of the conformance/native-diff gate,
   tests, fixtures, oracle, baseline, and corpus manifest first. On any hash change or live-game
   dependency, reassert PAUSE, close the window, terminate the worker, leave the review branch
   unmerged, restore runtime state if required, and reject the result. Do not rewrite a baseline to
   make the task pass.
5. **Parallel window.** Authorize exactly two named goals and the reviewed global/per-project
   capacity values for that window only. On overlap, isolation, reporting, or capacity mismatch,
   reassert PAUSE, close both windows, terminate all worker units, verify no queued work or sends,
   and restore the pre-window snapshot before any retry.
6. **Soak fault windows.** Record each gateway restart, host reboot, PAUSE interruption, and
   notification-failure injection as a separately observed sub-window. Each sub-window rolls back
   to PAUSE plus the captured service/configuration state; delivery retry uses only the Phase 7
   outbox and exact transport evidence. Any lost/duplicate action, event, report, or delivery ends
   the soak and prevents promotion.

After every future window: PAUSE must be present with the expected hash, all pilot windows closed,
worker/worktree process counts zero, no unintended sends, and the exact
instruction/plan/goal/task/run/commit/gate/verifier/review/report/delivery chain complete. Partial
evidence is a no-go for the next window.

## Authorization boundary and next action

Lucas approved the Phase 8 implementation plan and replacement of the old plan shape,
reviewed and merged the implementation and evidence branches, and separately authorized one fresh
PAUSE-held actuator deployment. That production authorization did not include a retry, data
migration, compatibility repair, configuration/capacity change, PAUSE removal, human-visible
notification, or pilot window. The failed deployment was rolled back and was not retried.

The next admissible action is a separate reviewable Phase 8 compatibility patch for legacy
decision-journal import. It must preserve the append-only source records, fail closed for new
invalid decisions, deterministically handle already-existing legacy no-op records, surface import
failure in actuator evidence, and include a live-journal-shaped regression proving the current
valid PAUSE-safe no-op executes exactly once. Only after that patch is reviewed, merged, and
pruned may a fresh preflight and separately authorized deployment retry be proposed. The first
first pilot window still additionally requires deployed Phase 8 code, a configured human-owned pilot
model alias and bounded capacity, a fresh Lucas goal instruction, exact plan-digest approval, and
exact window authorization.

## Post-merge hardening follow-up

Lucas subsequently reported the Phase 8 branch merged and pruned. A fresh fetch verified PR #29's
merge commit `9422b9cc5bf4270b7184b9af6fa31cae8e6a6876` at `origin/master`, all four Phase 8 commits as
ancestors, the remote review branch absent, and every Phase 8 handoff/evidence artifact present.

Under Lucas's separate, narrow authorization, the production backup/restore gate was then
demonstrated while PAUSE remained asserted. The root-owned mode-`0700` snapshot and evidence are
retained at
`/var/backups/recursion/hermes-pre-phase8-20260808T224858Z-5a8b2f7464cb`; exact restore occurred
only in `/var/tmp/recursion-restore-drill-20260808T224858Z-5a8b2f7464cb`, all manifest and recovery
checks passed, and that disposable staging path was removed. `hermes-gateway` alone was stopped for
the snapshot and restarted active. No deploy, configuration change, PAUSE removal, notification,
worker, or product-repository mutation occurred.

The final PAUSE SHA-256 remained
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; production remained
`loop/auto` at `5a8b2f7464cb5f87bc09ea2e611cfa1453ca1c26`; workers were zero; and all nine outboxes were paused
with zero events and deliveries. The live Phase 1 audit still has exactly the same two deployment
gates. Full commands, negative observations, results, rollback evidence, and retained-evidence
hashes are appended to `docs/plans/hermes-project-pilot-phase0-evidence.md`.

The backup/restore gate is therefore closed. Remaining prerequisites are: activate personal
GitHub Pro and prove default-branch protection for the private repositories, remediate the two
Phase 1 live findings through reviewed deployment, deploy the exact merged Phase 8 code under
PAUSE, configure approved model/capacity values, then obtain a separate authorization for each
ordered pilot window. The authorized personal GitHub Pro attempt was blocked before any billing
mutation because this session had no signed-in controllable browser; no organization budget or
billing setting was changed.

### Branch-protection follow-up

Lucas subsequently activated personal GitHub Pro and explicitly authorized protection for every
initialized personal repository. Classic rules now protect `recursion/master` plus the `main`
branches of the other initialized repositories.

All eight persisted rules were re-opened and field-audited: pull requests required; approval count
not required to avoid a one-user deadlock; conversations resolved; administrators unable to
bypass; force pushes and deletions disabled; status, signature, deployment, linear-history, and
lock requirements inactive. The one empty repository remains deferred until its first `main`
branch. No organization budget or billing setting was changed.

The initialized-project branch-protection gate is closed. Remaining pre-deployment gates are the
two unchanged live Phase 1 findings. After those are remediated through a reviewed, PAUSE-held
deployment action, the exact merged Phase 8 code can be proposed for its own PAUSE-held deployment;
model/capacity configuration and every pilot window still require their applicable separate
authorization.

> **SUPERSEDED 2026-08-11 (post-merge preflight).** "Remaining pre-deployment gates are the
> two unchanged live Phase 1 findings" was true when written and is retained as the record.
> Those two findings were closed by the corrected Phase 1 deployment recorded below, and the
> live audit was re-run read-only on 2026-08-11 returning `ALL PASS` / `rc=0`. Phase 1 no
> longer gates the Phase 8 hardening deployment.

### Phase 1 remediation blocker

Lucas separately authorized the Phase 1 production remediation. The complete PAUSE-held preflight
passed the 17/17 baseline, every Phase 1-7 control suite, and every Phase 1-7 positive audit. The
transaction then failed closed before stopping or changing the service: the reviewed
`cryptography==50.0.0` requirement is OSV-clear but conflicts with Hermes Agent 0.20.0's exact
`cryptography==48.0.1` distribution metadata, so mandatory `uv pip check` returned one
incompatibility. The installed and existing upstream source refs retain that exact pin, while the
latest published Hermes Agent is older and pins an earlier vulnerable version.

Production therefore remains unchanged: the gateway is active under its original root unit and
venv, both Phase 1 findings remain, no service identities or state directories were created, all
pilot supervisors and workers are inactive, no notification was sent, and PAUSE remains
byte-identical at
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`. Exact logs, staged failure,
rollback material, and a file manifest are retained in the root-owned mode-`0700` bundle
`/var/backups/recursion/phase1-remediation-20260809T010739Z-7d682044`.

Do not deploy Phase 8 or retry by overriding package metadata, ignoring `pip check`, or using
`--no-deps`. The next gate is a reviewed Hermes Agent artifact that declares and tests support for
`cryptography==50.0.0`; after that artifact exists, rerun the same staged compatibility/import/OSV
checks and the transactional identity migration. Pilot authorization remains out of scope.

### Phase 1 compatibility patch ready for review

Lucas authorized a separate Hermes Agent compatibility fork. The personal fork now exists at
`NotSkittlz/hermes-agent`; clean local branch `review/phase1-cryptography50` is based directly on
the deployed Hermes source OID `36cb5ae5530a75def7df3195e49b7a4aa2add482`, which was verified
as an ancestor of upstream `main` `b79e83827d1e275f3b5f91594e9f7136d7293e5a`.

The local, unpushed review patch updates cryptography to `50.0.0`, every declared and lazy aiohttp
pin to `3.14.3`, and the complete lock to eliminate the additional h2 advisory with `h2==4.4.1`.
The 249-package lock and 126-package production-shaped environment are OSV-clear; `uv lock
--check`, `uv pip check`, installed metadata, gateway/provider imports, RSA/AES round trips, and the
canonical 144-test targeted suite all pass. The universal all-extras graph resolves; a literal
Windows all-extras install remains platform-blocked at Matrix's Unix-only `python-olm` source
build. A separate full-suite attempt timed out at 1,804 seconds without a summary and is not
claimed as green; all spawned local test processes were terminated and verified absent.

The exact commands, resolved transitions, negative evidence, cleanup, and audit results are in the
Phase 0 evidence document. No production or PAUSE action occurred. The next admissible step is
Lucas review/publication of the compatibility source patch; production retry still requires a new
PAUSE-held preflight and separate authorization. Do not deploy Phase 8 or start a pilot window.

### Authorized compatibility deployment: new sandbox gate

Lucas authorized publication and testing of the Hermes compatibility patch. Branch
`review/phase1-cryptography50` is published at exact OID
`498061d8f1dcd855419b30e8729c869596a89ca6`; it was not merged or pruned. Three PAUSE-held attempts
were fail-closed and are retained at:

- `/var/backups/recursion/phase1-remediation-20260809T052659Z-498061d8` — metadata-verifier defect,
  before service mutation;
- `/var/backups/recursion/phase1-remediation-20260809T060745Z-498061d8` — missing
  `/etc/sysusers.d` parent after gateway stop, followed by successful automatic and independent
  rollback; and
- `/var/backups/recursion/phase1-remediation-20260809T141605Z-498061d8` — exact reviewed artifact
  reached hardened live readiness and closed both Phase 1 findings, but the Phase 1 positive audit
  exposed a new sandbox-executable permission blocker and triggered rollback.

The final attempt had a third fresh `17/17` baseline plus every Phase 1-7 control and positive audit
green before mutation. Its staged environment passed all 104 dependency checks, exact source and
metadata verification, imports, RS256, and complete OSV. While live under the reviewed unit, the
gateway ran as `hermes-gateway`, the live Phase 1 audit was `ALL PASS`, systemd exposure was
`3.6 OK`, the post-switch baseline was 17/17, and the Phase 1 red/artifact controls passed. The
subsequent positive audit failed with:

```text
env: '/workspace/.recursion-sandbox/claude': Permission denied
```

The reviewed unit and transaction use `UMask=0077`; the existing sandbox helper creates the copied
non-`/usr` executable directory after its worktree ownership handoff without explicitly granting
the dedicated runner traversal. This exact hardened-umask path now requires a separate reviewed
Recursion patch and red control. No production chmod, manual worker invocation, weakened test, or
retry was used.

Rollback and independent verification passed. Production is again active on the original root
gateway/unit, original Hermes OID `36cb5ae5530a75def7df3195e49b7a4aa2add482`, `aiohttp==3.14.1`,
and `cryptography==48.0.1`; the original two live findings therefore reproduce exactly. Final
production awareness OID is `6ce55f71a9d5cbef41510379b4f755b5290d3f90`; PAUSE remains exact at
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; active agents and workers are
zero; all execution units are inactive; and all nine outboxes are paused with zero events. The
created service identities and mode-`0700` state directories remain inert for forensics; reviewed
sysusers/tmpfiles files were rolled back.

Phase 1 remediation, Phase 8 deployment, model/capacity configuration, PAUSE removal,
notifications, and every pilot window remain blocked. Full exact commands, results, rollback
proof, bundle contents, and manifest evidence are appended to the Phase 0 evidence document.

### Hardened-umask sandbox compatibility patch ready for review

Lucas authorized the separate Recursion compatibility change. A new exact regression first
reproduced the copied-executable failure under inherited `umask 0077` as the only Phase 1 control
failure. The fix keeps the copied non-`/usr` executable root-owned while granting only the
`recursion-runner` group access: directory mode `0710`, executable mode `0550`. The existing cleanup,
OS boundary, allowed-path gate, systemd properties, network policy, and publisher controls remain
unchanged.

Focused Linux proof then passed the Phase 1 control and positive audit, completing bounded work as
dedicated UID `981`. The full process-wide-`0077` matrix passed baseline 17/17 plus every Phase 1-7
control and every Phase 1-7 positive audit, ending `FULL_UMASK_MATRIX=ALL_PASS` with zero residual
workers. One preliminary detached-HEAD run was correctly refused by the baseline before suite
execution; that negative fixture result is preserved, and the unchanged named-branch rerun is the
claimed evidence.

The exact three implementation/control file hashes, patch, commands, logs, final production state,
and manifest are retained at root-owned mode-`0700`
`/var/backups/recursion/phase1-umask-compat-20260809T174059Z-9422b9cc`. The disposable test clone was
removed after the manifest passed. Final production remains unchanged on awareness OID
`ddff98258b39f6d8d5f4811ea2ff7ee47952a017`, original Hermes OID
`36cb5ae5530a75def7df3195e49b7a4aa2add482`, original root gateway PID `2542748`, and exact PAUSE
SHA-256 `919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; workers are zero,
execution units are inactive, and all nine outboxes are paused with zero events.

The five-file Recursion change is staged only on `review/hermes-post-phase8-hardening`. It is not
committed, pushed, merged, or deployed. The next action is Lucas review/publication of this patch.
Only after it is merged into `origin/master` may a new full PAUSE-held preflight and separately
authorized Phase 1 deployment be proposed. Phase 8 deployment and every pilot window remain
blocked.

### Merged remediation deployment failed closed and rolled back

Lucas subsequently merged PR #30 as exact commit
`fa0263587479b3c43b0ba6bb61f5b176383f7cec` and explicitly authorized one fresh PAUSE-held,
gateway-only Phase 1 remediation window with frozen Hermes
`498061d8f1dcd855419b30e8729c869596a89ca6`. Fresh workstation fetch, production OID/ancestry,
artifact hashes, PR merge metadata, and `recursion/master` protection were re-verified.

The fresh preflight passed baseline 17/17, all Phase 1-7 controls, and all Phase 1-7 positive audits
while reproducing exactly the two known live Phase 1 findings. Immediate state capture and the exact
rollback were retained before `FINAL_PREDEPLOY_GATE=PASS`.

The staged frozen-Hermes environment passed exact metadata/import/RS256 verification, `uv pip
check`, and the complete 104-package OSV audit. During the authorized maintenance window, only
`hermes-gateway` was stopped. Migration then failed closed before the venv or unit switch because
plain `rsync --delete` preserved pre-existing excluded destination `bin/` and `logs/`; the explicit
forbidden-path assertion rejected them.

Automatic rollback and independent verification both passed. Final production is again the old
active root gateway with Hermes `36cb5ae...`, `aiohttp==3.14.1`, `cryptography==48.0.1`, and the old
unit hash. PAUSE remains exact at
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; workers and project
processes are zero; all execution units are inactive; all nine outboxes are paused and empty; no
notification, pilot, flag, capacity, product-repository, or Phase 8 deployment action occurred.

The root-owned evidence bundle is
`/var/backups/recursion/phase1-remediation-20260809T225227Z-498061d8`; its manifest verifies and has
SHA-256 `9d68d0d735a9e6adc661f510c70c6a7053617e3f06770ffe5b93c3ca5585a93a`. A read-only dry run proved
the prospective correction: add `--delete-excluded` so excluded source paths are also removed from
the migrated destination. The syntax-checked candidate has SHA-256
`4df3299a546802ea15f0080fdc6dd33a108876c20d73e50e921bef03ef38ecaf` and was not launched.

Phase 1 remediation therefore remains open, and Phase 8 deployment/pilots remain blocked. The next
admissible action is another complete PAUSE-held preflight followed by a new explicit authorization
for that gateway-maintenance window. Full commands, negative evidence, rollback proof, and final
state are appended to the Phase 0 evidence document.

### Corrected Phase 1 deployment succeeded under PAUSE

Lucas authorized a new full preflight and corrected gateway-only deployment using merged Recursion
`fa0263587479b3c43b0ba6bb61f5b176383f7cec`, frozen Hermes
`498061d8f1dcd855419b30e8729c869596a89ca6`, and the reviewed `rsync --delete-excluded`
correction. The boundary prohibited PAUSE removal, notification, configuration/capacity changes,
and pilots.

The new root-owned bundle is
`/var/backups/recursion/phase1-remediation-20260809T233144Z-498061d8`. Fresh authenticated OID, PR,
artifact, protection-rule, backup, configuration, destination, and rollback gates all passed. The
fresh preflight passed baseline 17/17 and all Phase 1-7 controls/audits while reproducing the two
old live deployment findings.

The corrected transaction passed its staged 104-package compatibility/OSV/import/RS256 gates,
stopped only `hermes-gateway`, migrated state without inherited root-runtime paths, switched the
venv and reviewed unit, and reached bounded non-root readiness. Post-deployment live Phase 1,
baseline 17/17, and every Phase 1-7 control and positive audit ended `ALL PASS`. Independent closure
also passed exact source/dependency metadata, configuration hashes, service properties, active
agent/worker counts, outbox inertness, live Phase 1, and systemd security.

Final production runs exact Hermes `498061d8...` as `hermes-gateway:hermes-gateway` with `UMask=0077`,
`NoNewPrivileges=yes`, `PrivateTmp=yes`, `PrivateDevices=yes`, `ProtectSystem=strict`, and
`ProtectHome=yes`; systemd exposure is `3.6 OK`. `aiohttp==3.14.3` and
`cryptography==50.0.0` are OSV-clear. The retained old venv is
`/usr/local/lib/hermes-agent/venv.pre-phase1-498061d8f1dc` for exact rollback.

Final PAUSE SHA-256 is
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; active agents and workers
are zero; execution/supervisor units are inactive; all nine outboxes are paused and empty; project
and runtime configuration hashes are unchanged; and no notification or pilot occurred. The final
evidence manifest verifies and has SHA-256
`bea07eb20a4ed1c5e2214592690eb1323f70201e1e51dfe11064cfacfec5eeba`.

Two evidence-only negative observations are retained in the Phase 0 evidence: an initial closure
assertion did not distinguish clean pre-start migration state from service-owned runtime paths, and
the transaction's initial manifest preceded the last tee output. Neither was accepted as positive
evidence; the corrected independent verifier and post-log reseal both passed.

The two live Phase 1 deployment gates are now closed. This does not deploy Phase 8, configure
models/capacity, lift PAUSE, authorize a pilot, or promote beyond Phase 8. Those remain separately
gated next actions.

## Phase 8 actuator deployment: failed closed and rolled back

Lucas explicitly authorized one fresh PAUSE-held Phase 8 actuator deployment, bounded to the exact
reviewed `hermes-actuator.service`. The boundary excluded gateway changes, PAUSE removal,
model/capacity configuration, notification, pilots, and product-repository changes, and required
automatic rollback on any mismatch.

Fresh authenticated evidence found current workstation `origin/master` at PR #31 merge
`b23069ee49519fda5896f43ec888efd71b533167`, the Phase 1 evidence commit `71e651ad...` and Phase 7
implementation `b4d3a303...` as ancestors, the evidence review branch pruned, and protection rule
`81491567` still requiring a pull request and conversation resolution with administrator bypass,
force-push, and deletion disabled. The production checkout remained `loop/auto` at
`73987156f5d22d2f2f50ddc542c978d0c48f88b8`; its executable tree had no drift after reviewed
runtime base `fa026358...`, and all 18 Phase 8 runtime/config blobs matched current merged master.

An initial preflight stopped before deployment because it assumed an empty pending queue. Read-only
inspection found exactly one valid Head Hermes PAUSE-safe no-op projection,
`decision-6f037cbf6fb51a7f641ea9d6`, with SHA-256
`883d9f53e2d7263d8ffa733fe61bff959cf1b7e341d4a293d0a9b00f8e0c6684`. The gate was narrowed to
accept only that exact record; a second or different record remained fatal. No live data changed.
The fresh corrected preflight then passed the 17/17 baseline, every Phase 1-8 control and positive
audit, and live Phase 1.

The authorized transaction used root-owned mode-`0700` bundle
`/var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee`. It stopped only cron, left the
hardened gateway running, created and verified a 19-file runtime snapshot, restored and verified it
in disposable staging, and removed staging. After `FINAL_PREDEPLOY_GATE=PASS`, the exact reviewed
unit (SHA-256 `5cfd8853d6e66068670aa5f9afcfa5b5671730af927d166a8dca4dad50486d4e`)
was installed and reached active/running as PID `2934221` from `2026-08-10T01:11:45Z`.

The expected PAUSE-safe no-op did not leave pending state within the bounded observation. The
transaction failed at `2026-08-10T01:12:10Z` with
`DEPLOY_FAIL=paused-noop-still-pending` and automatically rolled back by `01:12:12Z`. It disabled
and removed the actuator unit, restored the exact runtime snapshot and prior crontab/cron state,
and left the gateway untouched. No action, event, report, delivery, notification, or pilot was
created, and no retry occurred.

Read-only root-cause proof found six records in `state/decisions.jsonl`. The first five are legacy
pre-schema no-ops whose prose `goal_id` values are not canonical identifiers; the sixth is the
valid pending no-op. `conduct-hermes.sh --paused-safe` calls `decision-queue.py import-journal`
before claiming pending work. Import normalizes and validates line 1 before checking existing
projections, fails on that legacy `goal_id`, and returns before line 6 can be claimed. The actuator
currently suppresses that command's diagnostic and continues its loop, explaining the silent
pending record. The journal itself was not edited, migrated, skipped, or drained.

Independent closure reran the complete matrix from `2026-08-10T01:17:19Z` through `01:36:16Z`;
all 18 control/audit commands returned zero. A new snapshot matched all 19 pre-deployment runtime
records exactly after excluding only `created_at`, restored and verified in disposable staging,
and staging was removed. Final state at `01:37:02Z` was:

```text
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
CONTROL_PLANE=da43555b6977e55fc0bb185fd7d6d5e76384b638cbcaff4d798596cce68630d4
ACTUATOR_LOAD=not-found
ACTUATOR_ACTIVE=inactive
ACTUATOR_ENABLED=not-found
CRON_ACTIVE=active
GATEWAY_ACTIVE=active
GATEWAY_PID=2781295
WORKERS=0
PROJECT_PROCESSES=0
OUTBOXES=9 paused=true events=0 deliveries=0
MODEL_ROUTING=unset
CAPACITY=zero
```

The bundle's 169 data files were sealed after all log writers stopped; its manifest verifies and
has SHA-256 `2de6f66997e384d5e360b8005d12cdf73a88f3e895bfa93d38a4156992e13319`.
Phase 8 remains undeployed and every pilot remains blocked. The next admissible action is the
separate reviewed legacy-journal compatibility patch described above, followed only after merge by
a fresh preflight and separately authorized deployment retry.

## Compatibility merge, evidence correction, and PAUSE-resync blocker

Lucas reviewed, merged, and pruned both Phase 8 follow-up branches. Workstation fetch established
current `origin/master` `be3cb8b6462bc6bbe2ba12305e6bee47ee102fc8`, with evidence commit
`abdf4fe4bd1e8cd5ef19c5956b54a441738891a2`, compatibility commit
`1748f75443b2d81b5951339f88e7c3fc5d2bf6cc`, Phase 7 implementation `b4d3a303...`, and their
merge commits as ancestors. The review branches were absent. GitHub protection rule `81491567`
still required pull requests and conversation resolution, applied to administrators, and disabled
force pushes and deletion.

Lucas then authorized corrected Phase 8 production testing. The authorization was interpreted as
merged compatibility code plus actuator verification under PAUSE; it did not authorize PAUSE
removal, a pilot window, notification, model/capacity/configuration change, gateway maintenance,
or product-repository work.

### Stale compatibility hash stopped the first preflight

Before a control or production mutation, exact merged-source extraction found that the compatibility
handoff's `scripts/decision-queue.py` Linux SHA-256 was stale. The reviewed Git blob is
`510fce16166a6e69b2604cdecd10441262ca202a`; its Linux SHA-256 is
`082c82b6ff5e82fd2123322f8dc695323ca3091ff959254b12f4a46c77fd8a08`, not the recorded
`b487eb78...`. The other five recorded hashes matched. This was not accepted as a line-ending
explanation or silently waived.

The stopped attempt is retained at root-owned mode-`0700` bundle
`/var/backups/recursion/phase8-actuator-20260810T024806Z-be3cb8b`. Its corrected final state showed
production unchanged at `73987156...`, exact PAUSE/journal/pending hashes, absent actuator, active
gateway/cron, zero workers/processes, zero legacy markers, unset routing/capacity, and all nine
outboxes paused and empty. Its manifest verifies with SHA-256
`b7b50eb68331e502bf2c7edccf6d0183371a09427ff853ac04d6d3508495d2f5`. An initial final-state
capture had shell-quoting errors; it is retained as `final-state-attempt1.txt` and was not used as
positive evidence.

### Evidence-only rerun exposed a higher-priority PAUSE defect

Lucas separately authorized an evidence-only correction branch and a rerun of the exact merged
Linux matrix plus disposable live-shaped proof. Clean authoring branch
`review/hermes-phase8-preflight-evidence-correction` started from `be3cb8b...`. A fresh root-owned
bundle staged exact reviewed source outside `/opt/recursion`.

Negative setup observations were preserved rather than converted to passes: the first composite
upload command failed on Windows-to-SSH quoting before target creation; a later standalone Python
syntax wrapper misquoted its compile mode; and matrix attempt 1 failed before tests because the
new bundle parent was mode `0755` while its evidence child was `0700`. After preserving those logs,
only the bundle parent was corrected to `0700`.

Matrix attempt 2 also stopped before its first test because the live HEAD gate changed. Read-only
reconstruction established this exact chain:

```text
2026-08-10T03:15:01Z cron starts cycle.sh with state/PAUSE present
cycle.sh classifies resync as awareness and calls resync.sh sync under PAUSE
resync advances local master by six commits to be3cb8b...
resync creates ce9904b3dfed64c90f95b5214b0fc5460f81810f
  first parent 73987156f5d22d2f2f50ddc542c978d0c48f88b8
  second parent be3cb8b6462bc6bbe2ba12305e6bee47ee102fc8
cycle records deliver=0 and commits morning/board awareness
final loop/auto = 30fb73f56496d1d8e72f00dceb4b59de4bd21a00
```

The merge changed the two evidence docs, compatibility handoff, production plan, and all six
compatibility implementation/control files in the production checkout. Exact merged
`decision-queue.py` blob `510fce1...` is therefore already present on production, but the actuator
unit remains absent and no queue processing occurred. This checkout change happened before the
authorized full baseline, runtime snapshot, staging restore, exact rollback, and
`FINAL_PREDEPLOY_GATE`; it is not a successful deployment and must not be described as one.

The behavior is a direct precedence conflict. Live `CONTROL_PLANE.md` says PAUSE halts
branch-changing maintenance. Lower-ranked `scripts/cycle.sh` says resync runs under PAUSE, and
`scripts/tests/pause-semantics.test.sh` positively requires it. The relevant live SHA-256 values
are `904fc1a5...` (`cycle.sh`), `511383a5...` (`resync.sh`), and `6882f93f...`
(`pause-semantics.test.sh`). Under the constitution, the control plane wins; the implementation and
test are stale.

The retained evidence bundle is
`/var/backups/recursion/phase8-evidence-correction-20260810T032141Z-be3cb8b`. Its manifest verifies
with SHA-256 `34ddf6d0c4666f5b5ac5613c5e480ce6bc437a4eec6d421d70b8cd4921899ad9`.
Final state at `2026-08-10T03:27:38Z` was exact PAUSE, unchanged journal and pending decision,
zero legacy markers, absent/inactive actuator, active gateway PID `2781295`, active cron, zero
workers/project processes, unchanged configuration, and nine paused empty outboxes. The cycle log
proves `deliver=0`. No control matrix, live-shaped proof, service mutation, action, event,
notification delivery, or pilot started.

### New gate

Phase 8 actuator testing is blocked. The next admissible implementation is a separate reviewable
PAUSE/resync control patch, not a manual reset or workaround. It must:

1. add a red control proving current paused `cycle.sh -> resync.sh sync` changes the checked-out
   executable tree;
2. preserve read-only fetch/status/behind awareness under PAUSE;
3. refuse checkout, fast-forward, merge, or other branch-changing maintenance while paused;
4. leave the current exact PAUSE/journal/pending state untouched; and
5. pass the complete historical matrix before review.

Only after Lucas reviews/merges/prunes that patch may a new full PAUSE-held preflight and separately
authorized production correction be proposed. Actuator deployment and every pilot window remain
blocked.

## PAUSE-safe portfolio awareness patch

Lucas reported the evidence-correction branch merged and pruned and authorized the separate
PAUSE/resync control patch. A fresh fetch established PR #34 merge
`52f6ad37250d7b7bc798d86c04768aef8cc09c07`, evidence commit `03a5b6a...` as an ancestor, and the
remote review ref absent. The protected dirty workstation master remained exactly `20c1fa7...`
with its 20 pre-existing entries. Clean branch `review/hermes-phase8-pause-resync` was created from
that exact merged base.

The implementation stays in the canonical `cycle.sh -> resync.sh` path:

- `resync.sh observe` performs the existing authenticated `ls-remote` and fetch, evaluates current
  drift/dirt/local-only state, and records the same durable escalations, but has no checkout,
  fast-forward, merge, or local-branch update path;
- paused `cycle.sh` selects `observe` and labels the tick as fetch/status-only; and
- resumed `cycle.sh` still selects the existing `sync` path before dispatch, so the stale-base
  protection remains non-vacuous.

The commit chain is:

```text
2688671a80e7a69929f0b1e137e43485196d72ac  Add paused resync branch-mutation control
1a16d81c396dde5fbcc87d9ac80cca3017fc3bf2  Keep paused portfolio resync read-only
5bbfad5fc46975b6f3de89744a4cd89ab671e201  Stabilize planner concurrency falsifier
```

At exact red commit `2688671...`, the new end-to-end test fetched the disposable remote tip but
also proved old paused `sync` moved local `main` and changed the checked-out executable SHA. It
ended `pause-semantics: 1 FAILED`; every other PAUSE assertion and the resumed fast-forward control
passed. At `1a16d81...`, the direct resync suite and PAUSE suite passed: remote-tracking awareness
advanced, all local heads and the checked-out executable remained exact, and resumed sync still
fast-forwarded.

The complete matrix exposed an independent timing defect in the historical Phase 2 test. Its
old `ls` barrier ran only after one planner held the lock, so the peer could not reach the barrier;
two full sequences produced a false one-task result while five isolated reruns and an exact
baseline/Phase-1-prefix diagnostic passed. Commit `5bbfad5...` aligns both planners immediately
before lock acquisition and preserves the old vulnerable-read barrier only when no planner lock
exists. Five consecutive current-code runs passed. Overlaying that corrected falsifier on exact
pre-Phase-2 base `b25b329...` still produced all six intended failures, including
`planner CAS missing`; the invariant was strengthened, not waived.

The final exact-commit Linux matrix ran from `2026-08-10T04:24:52Z` through `04:44:09Z` and ended:

```text
BASELINE_SUITES=17
BASELINE_FAILED_SUITES=0
phase1-control through phase8-audit: RC=0
MATRIX_TOTAL=17 MATRIX_PASSED=17 MATRIX_FAILED=0
MATRIX_RESULT=PASS
```

All raw red, green, failed-attempt, timing-diagnostic, pre-change falsifier, and final-matrix logs
are retained root-owned and mode `0700` at
`/var/backups/recursion/phase8-pause-resync-authoring-20260810T034342Z-52f6ad3`. The evidence
manifest SHA-256 is `62ec571b6d750d6ebbd9614ab0729ff0bec8f8e8c9d1c88775d5b2b7021a0872`.

Final read-only production verification at `2026-08-10T04:47:21Z` matched the pre-test snapshot:

```text
HEAD=30fb73f56496d1d8e72f00dceb4b59de4bd21a00
MASTER=ORIGIN_MASTER=be3cb8b6462bc6bbe2ba12305e6bee47ee102fc8
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
JOURNAL=c0a03878fc98df2bb6e6a83496d003dd198e53a8477cb004af6a3909f5b103a0
PENDING=883d9f53e2d7263d8ffa733fe61bff959cf1b7e341d4a293d0a9b00f8e0c6684
GATEWAY=active/running PID 2781295
CRON=active
ACTUATOR=not-found/inactive
WORKERS=0
OUTBOXES=9 paused=true events=0 deliveries=0
```

No `/opt/recursion` file, service, configuration, notification route, action/event/delivery,
product repository, PAUSE state, or pilot window was changed. The next gate is Lucas review,
merge, and pruning of this branch. Only then may a fresh PAUSE-held production correction be
proposed; it still requires separate authorization. Phase 8 actuator deployment and every pilot
remain blocked.

## Merged PAUSE/resync production correction

Lucas reported PR #35 merged and the review branch pruned. Fresh Git and GitHub evidence proved
merge commit `f7df0d0e8ba7cbfc4109581cfbb62ed37b54813a`, patch/evidence head `76cef9a...`,
all four patch commits as ancestors, and the remote review ref absent. The signed-in protection
page still showed rule `81491567` on `master`: pull requests and conversation resolution required,
administrators included, and force pushes and deletion disabled. No GitHub setting changed.

A fresh PAUSE-held preflight ran from `2026-08-10T05:03:54Z` through `05:23:33Z` in an exact
Git-bundle clone of merged master. It passed the historical 17/17 baseline, every Phase 1-8
negative control and positive audit, the live Phase 1 audit, authenticated host-side origin proof,
both retained backup gates, exact project/pilot configuration, zero model/capacity settings, the
sole valid pending PAUSE-safe no-op, and all nine paused empty outboxes. A disposable simulation
then merged exact `f7df0d0...` into exact live parent `30fb73f...`, proved no conflict and exact
reviewed runtime bytes, and removed the simulation checkout.

Under Lucas's exact correction-window authorization, the transaction ran from `06:05:28Z` through
`06:05:30Z`. It stopped only `cron.service`, retained the gateway at PID `2781295`, acquired the
existing control lock, proved zero scheduled/project/worker processes, created a 19-file runtime
snapshot and a complete Git bundle outside `/opt`, restored and verified the runtime snapshot in
disposable staging, and removed staging before mutation. The armed compare-and-swap then advanced
local `master` and `origin/master` from `be3cb8b...` to exact `f7df0d0...` and merged reviewed master
once into `loop/auto` as:

```text
599cbbdbf6ef538e3c305c61c1affc16320b2244
parents: 30fb73f56496d1d8e72f00dceb4b59de4bd21a00 f7df0d0e8ba7cbfc4109581cfbb62ed37b54813a
subject: Merge branch 'master' into loop/auto
```

No `cycle.sh`, `resync.sh`, actuator, worker, notifier, or pilot ran. The rollback trap did not
fire because every gate passed; its exact restoration path was armed before mutation and the
pre-window Git/runtime artifacts were retained. Independent verification from `06:07:15Z` through
`06:07:19Z` compared all 19 live runtime files byte-for-byte and by size/mode/owner to the snapshot,
reverified the exact merge parents and reviewed runtime tree, reran live Phase 1, and proved:

```text
HEAD=599cbbdbf6ef538e3c305c61c1affc16320b2244
MASTER=ORIGIN_MASTER=f7df0d0e8ba7cbfc4109581cfbb62ed37b54813a
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
JOURNAL=c0a03878fc98df2bb6e6a83496d003dd198e53a8477cb004af6a3909f5b103a0
PENDING=883d9f53e2d7263d8ffa733fe61bff959cf1b7e341d4a293d0a9b00f8e0c6684
GATEWAY=active/running PID 2781295
CRON=active/running
ACTUATOR=not-found/inactive
WORKERS=0; PROJECT_PROCESSES=0
OUTBOXES=9 paused=true events=0 deliveries=0
```

The root-owned mode-`0700` closure bundle is
`/var/backups/recursion/phase8-pause-resync-preflight-20260810T050140Z-f7df0d0`; its 108-entry final
manifest SHA-256 is `cc3c6ae035b2d1cab9bc5fe61932dcb74860da56b7c01b198f842e277dd57ba0`.
This closes only the PAUSE/resync production correction. The Phase 8 actuator remains absent, all
model/capacity settings remain disabled, and every pilot/soak window remains blocked behind a new
applicable preflight and separate Lucas authorization.

## Actuator preflight incident and PAUSE-notification remediation

Lucas reported PR #36 reviewed, merged, and pruned. GitHub and fresh fetch/prune proved evidence
head `465f8964fbffa113c239d7b337179369a9f8be13` merged through exact `origin/master`
`7e6e21f41c3da8914322ac352fabcd4bf95b38a4`, the implementation/evidence ancestry held, and
the remote review ref was absent. Authenticated branch protection still required pull requests and
conversation resolution, included administrators, and prohibited force pushes and deletion.

The new PAUSE-held actuator preflight began at `2026-08-10T06:26:36Z`. It passed the 17/17
baseline, every Phase 1-8 control and positive audit, the live Phase 1 audit, authenticated origin,
project/pilot/notification configuration, all nine paused empty outboxes, services/processes, and
retained backup/restore gates. Its final immutable-live-state gate then correctly stopped at
`06:46:27Z`:

```text
PREFLIGHT_FAIL=live-head-changed-after-controls
```

Root cron had launched `scripts/cycle.sh force` at `06:30:01Z` while PAUSE was exact. It advanced
`loop/auto` from `e1420d24...` through morning commit `724df09e...` to board commit
`4db7432b...`, fetched the merged remote ref, and appended the branch-review tracker after the
state commit, leaving `state/briefs/2026-08-10.md` tracked dirty. Its log claimed one delivered
brief and eight posted project digests. Those nine lines prove legacy calls only: the direct
`notify.sh` paths suppressed errors and created no Phase 7 event, delivery, transport receipt, or
message-ID acknowledgement. Actual delivery is unknown and is not inferred from process prose.

The root-owned mode-`0700` failed-preflight bundle is
`/var/backups/recursion/phase8-actuator-final-preflight-20260810T062541Z-7e6e21f`. The 19-file
incident manifest SHA-256 is `8ff1d25e3a00065093410955b13906b4336b7d1ccb34c3d2f31dd5b11854edf4`; the sealed
303-entry bundle-manifest SHA-256 is
`78baebaabc890181bac9c9a818058f8f589837c7e7f277767f751f76d2693dfe`.

Lucas then authorized both temporary containment and a separate reviewable remediation. At
`2026-08-10T17:23:51Z`, an exact gate acquired the canonical control lock and stopped **only**
`cron.service`. It left PAUSE, Git/status state, the gateway at PID `2781295`, configuration,
outboxes, and the incident history unchanged; actuator load state remained `not-found`, worker
units and project processes remained zero. Cron had already run three later silent monitoring
cycles at 09:15, 12:15, and 15:15, so containment preserved rather than rewrote their ledger chain
at `bcd65ebcc7c1af3f56be1bb3b7530a837793eaf6`. The exact rollback is `systemctl start
cron.service` followed by PAUSE/gateway verification. The containment bundle is
`/var/backups/recursion/phase8-cron-containment-20260810T172243Z`; manifest SHA-256
`958738a0a5b6c687b20cd802d041e53ac5746848cf8f21e2dde06671df6a0a90`.

The clean remediation branch `review/hermes-phase8-pause-notification-control` was created from
exact merged master `7e6e21f...`. Red commit `8aa553f47c8001e8d283cc061b3b52125cd64c45`
proved PAUSE-held brief/digest/escalation transport calls and tracked brief/board residue. Red
commit `b9037dd8317ffc623f0c0d61748b4301c3ea550a` proved the canonical monitoring source-event and
outbox path was absent. Implementation `f180fc7129c9feb3bd094a55336122f0d251a695` then:

- makes the sole `notify.sh` transport boundary return `75` before either legacy or durable send
  whenever PAUSE exists;
- prevents paused forced cycles from enqueueing stale brief/digest events for a later window;
- records resumed brief/digest bodies as stable idempotent events through canonical `taskctl.sh`;
- maps those events through the existing Phase 7 scheduler, decision queue, sole actuator,
  policy outbox, exact transport receipt, and acknowledgement path; and
- completes the brief before its state commit and commits the final board/event projections in
  order, leaving both paused and resumed forced-cycle tracked state clean.

Governance commit `2e5c3b3b5e8e31619cfd60ae23104c2b79458d81` records D-014 and updates the
constitutional/access PAUSE table without erasing D-012's original rationale. Focused Linux
controls passed syntax/compile, PAUSE and resumed forced-cycle behavior, sole-sender interlock,
event idempotency, policy routing, exact message identity, and durable receipt evidence. No code
or cleanup was applied to `/opt/recursion`; cron remains deliberately stopped pending reviewed
deployment, the gateway remains live, and the Phase 8 actuator and all pilot/soak windows remain
blocked.

The exact implementation commit then passed the complete Linux closure matrix from
`2026-08-10T17:42:02Z` through `18:00:47Z`: historical baseline `17/17`; every Phase 1-7 control
and positive audit; Phase 8 control, new notification control, and positive audit; live Phase 1;
and immutable source/live-state verification. Every return code was zero and
`FULL_MATRIX_FAILURES=0`. The inner manifest SHA-256 is
`612753113835040ba42d1c7fee637da376fdb1a1066b22cbd85b4096670e7f41`.

Production remained exact before and after at `loop/auto` `bcd65eb...`, local master `f7df0d0...`,
remote-tracking master `7e6e21f...`, PAUSE SHA-256 `91937183...`, active gateway PID `2781295`,
inactive cron, absent actuator, and zero workers/project processes; only the pre-existing untracked
`.env.bak.1786014440` remained. The complete tested-source bundle SHA-256 is `8beb15d1...` and
verifies at exact ref `f180fc7...`. All disposable staging was removed. The retained root-owned
evidence at `/var/backups/recursion/phase8-pause-notify-patch-20260810T172243Z` has a fully
verifying final manifest whose SHA-256 is `1a6faf799cfa4fd672a904ee8bfb7187f15b2058261f615e3c957c8c0be6c561`.

The review branch is therefore ready to commit, but not deployed. After review, merge, pruning,
and a fresh applicable PAUSE-held preflight, deployment of this interlock must occur before cron
can safely resume and before the Phase 8 actuator can be installed or tested. PAUSE removal,
notifications, configuration/capacity changes, and all pilot windows remain separate gates.

## Recovered durability and liveness hardening closure

The August 11 continuation recovered the complete uncommitted hardening tree after two Codex
client sessions terminated. Git objects, the worktree, the disposable Linux evidence, and the
production PAUSE sentinel were intact. The first termination stopped mid-tool after the local
session transcript had grown to about 98.8 MB with roughly 11.7 million cached tokens, so client
context pressure is the likely cause of that event. The recurrence also exposed a concrete test
code defect: the new Windows liveness probes used `os.kill(pid, 0)`. Python's Windows
implementation can use `TerminateProcess` semantics for this call, so a supposedly read-only
probe could terminate a parent or tool process. `runtime-state.py` and `project-reconcile.py` now
use `OpenProcess` with `GetExitCodeProcess`, plus `GetProcessTimes` for the PID-start witness; the
POSIX signal probe remains confined to POSIX. The client remained stable through the subsequent
long Linux closure runs.

The recovered patch closes five durability gaps without adding another scheduler, executor,
authority path, or authoritative store:

- Runtime backup/restore now replaces the complete managed state exactly, includes OpenRouter
  usage, validates a typed confined manifest, resolves interrupted restore transactions before
  later maintenance, and reclaims only witnessed-dead backup staging.
- Task, action, ledger, heavy-gate, project, planner, and worktree locks publish a complete
  PID/start witness atomically. Runtime rotation interlocks both before and after writer lock
  publication, and stale-lock reclamation remains fail-closed unless owner death is proved.
- Terminal transition receipts compact in bounded batches to a sharded evidence archive with a
  direct sharded event index. Recovery returns a retryable backlog instead of performing an
  unbounded scan in one scheduler tick.
- Reconciliation identities exclude heartbeat, receipt-sequence, and raw lease timestamp churn
  while retaining semantic lease expiry, task state, and worker liveness. Heartbeats therefore do
  not amplify decisions, but an expired lease or dead worker still changes authority once.
- Authoritative JSONL journals have a rebuildable ignored SQLite verification index bound to file
  identity, metadata, raw record hashes, and a cumulative chain. Tail consumers durably
  checkpoint only after projections commit; cache loss causes safe replay; journal mutation or
  torn tails invalidate/rebuild; and a pending/running decision is rebound to its authoritative
  journal record immediately before claim.

The exact portable Linux candidate is synthetic recovery commit
`161977533aa07b14b197910339cc90c0d9aa0709`, tree
`9c7551a7feff83bf728ac2d281184c3917556fc7`. Its complete Git bundle has SHA-256
`63677b473eb8be3cdcb603253927c0719ae0583ac029deddfc64f59152b8730a`. Fresh named-branch
clones from that bundle produced the final closure:

```text
BASELINE_SUITES=19
BASELINE_FAILED_SUITES=0
phase1-audit=ALL PASS
phase2-audit=ALL PASS
phase3-audit=ALL PASS
phase4-audit=ALL PASS
phase5-audit=ALL PASS
phase6-audit=ALL PASS
phase7-audit=ALL PASS
phase8-audit=ALL PASS
```

Phase 6 took about 40 minutes because its 20 crash/replay scenarios repeatedly execute
fsync-backed journal, receipt, and recovery paths in fresh fixture roots. It completed all
scenarios and left no process behind; the delay was bounded I/O-heavy verification, not a
deadlock. Phase 7 completed in 312.5 seconds, Phase 8 in 122.3 seconds, and the 19-suite baseline
in 193.8 seconds. The Phase 8 audit independently passed plan/window authority, parallel project
capacity, legacy-journal quarantine, notification exact-once behavior, JSONL durability, lock and
restore crash seams, semantic liveness, and transition-receipt compaction.

All remote execution remained in disposable `/var/tmp/recursion-phase8-316bfba3` clones. No
production file, Git ref, configuration, service, notification, model/capacity setting, cron,
actuator, worker, PAUSE state, or pilot window was changed. Final read-only verification retained
PAUSE SHA-256 `919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`, an active
gateway, inactive cron, inactive/absent actuator, and zero test-launched production work. This
hardening is ready for local review only; deployment, cron resume, actuator installation, PAUSE
removal, notifications, and every pilot/soak window remain separately blocked.

## Next-session post-merge runbook

### Review handoff

Implementation and evidence commit `5f41f4261f0aa96eedb6ac742ee0ad22f4f91b28` is on
`review/hermes-phase8-hardening`. Lucas will open the PR, review it, merge it, and prune the remote
review branch. The publication commit containing this runbook follows that implementation on the
same branch; use the remote branch tip as the PR head. Do not infer deployment authority from the
merge.

Start the next session by reading this section and the preceding recovered-hardening closure, then
prove all of the following read-only:

1. fetch/prune the review repository and identify the exact merge commit;
2. prove `5f41f426...` and the publication/handoff commit are ancestors of `origin/master`;
3. prove the remote review branch is absent after Lucas prunes it;
4. recheck authenticated branch protection without changing it; and
5. re-read production PAUSE, Git/status, service, process, queue, outbox, and retained-backup state.

At `2026-08-11T17:33:03Z`, the immutable production starting point was:

```text
BRANCH=loop/auto
HEAD=bcd65ebcc7c1af3f56be1bb3b7530a837793eaf6
MASTER=f7df0d0e8ba7cbfc4109581cfbb62ed37b54813a
ORIGIN_MASTER=7e6e21f41c3da8914322ac352fabcd4bf95b38a4
STATUS=?? .env.bak.1786014440
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
GATEWAY=active
CRON=inactive
ACTUATOR=inactive
PROJECT_OR_WORKER_PROCESSES=0
```

The production remote-tracking ref is intentionally stale because cron containment stopped the
normal fetch/resync path. Do not silently fast-forward or merge it. Treat reconciliation of
`master`, `origin/master`, and `loop/auto` as part of a fresh, explicitly authorized, PAUSE-held
production transaction with exact compare-and-swap parents and an armed rollback.

### Next admissible production transaction

After merge proof, prepare a new preflight but make no production mutation until Lucas separately
authorizes the exact transaction. The preflight must:

- use a fresh complete Git bundle/clone of the merged commit and repeat the applicable baseline,
  Phase 1-8 controls/audits, and live Phase 1 checks;
- acquire the canonical control lock and prove cron remains inactive, the gateway remains healthy,
  PAUSE is exact, the actuator is inactive/absent, and no cycle, resync, worker, or project process
  exists;

> **CLARIFIED 2026-08-11.** "Acquire the canonical control lock" is not executable with the
> deployed pre-hardening code: `lock-acquire`/`lock-release` ship *in* this patch, and
> `backup()`/`restore()` already take `RuntimeLock` plus the `taskctl` `EmptyLock` themselves.
> Pre-holding either makes the snapshot fail closed as contended. Let the tool take the lock
> and verify it was released. See
> `docs/plans/hermes-phase8-preflight-operational-nuances.md`.
- seal Git, unit, configuration, queue/outbox, journal, and complete managed-runtime evidence in a
  root-owned mode-`0700` directory outside `/opt/recursion`;
- restore the runtime snapshot into disposable staging and compare it exactly before mutation;
- simulate the exact Git reconciliation from the observed live parents and prove reviewed runtime
  bytes, no conflict, and no protected-state loss; and
- arm rollback before the final immutable-live-state compare-and-swap gate.

The first authorized deployment must do only two things: reconcile the production Git refs to the
exact reviewed merge and deploy the hardening interlock while PAUSE remains asserted and cron
remains stopped. It must not resume cron, install/start the Phase 8 actuator, send a notification,
change model/capacity/configuration, remove PAUSE, open a pilot window, or touch a product repo.
Independent closure must then prove exact runtime restoration, reviewed executable bytes, live
Phase 1, PAUSE, gateway health, inactive cron/actuator, zero workers, and unchanged queues/outboxes.

### Remaining gates after hardening deployment

Do not collapse these into one authorization:

1. deploy and independently verify the hardening under PAUSE;
2. separately authorize and verify cron resume;
3. separately preflight, authorize, install, and verify the Phase 8 actuator under PAUSE;
4. separately configure bounded model/capacity limits;
5. synthesize and approve one exact pilot plan and bounded pilot window;
6. separately authorize PAUSE removal for that one soak; and
7. observe, close the window, reassert the safe state, and record the result before widening scope.

The hardening saga is closed only when the reviewed patch is merged, production is reconciled and
running the exact hardening bytes under unchanged PAUSE, and independent closure passes. Full
Phase 8 activation is a later sequence and is not implied by that closure.

Suggested next-session kickoff:

> Resume from `docs/plans/hermes-project-pilot-phase8-handoff.md`, section “Next-session
> post-merge runbook.” Lucas has reviewed, merged, and pruned the Phase 8 hardening PR. Begin with
> read-only merge, protection, and production-state proof. Do not mutate production; return the
> exact PAUSE-held preflight and proposed transaction for authorization first.

## Post-merge proof and PAUSE-held preflight (2026-08-11)

The runbook's read-only proof and the full PAUSE-held preflight both completed. **No production
mutation occurred**; the transaction is held at its compare-and-swap gate pending separate
authorization.

> Lucas authorized the transaction later the same day and it executed successfully. See
> "Hardening deployment executed under PAUSE" below.

Merge proof: the exact merge commit is `ecc93de7fd5f6d92812d5a89687d6e05326b15fc`
("Merge pull request #38"), parents `abdc5928...` and `8901a038...`. Implementation
`5f41f426...` and the publication/runbook commit `8901a038...` are both ancestors of
`origin/master`, the remote review branch is absent after pruning, and the merge introduced no
extra change: its tree `9a6cdea2...` is identical to the reviewed branch tip's tree. All eight
classic branch-protection rules persist and field-audit as recorded.

Production matched the recorded `17:33:03Z` starting point exactly, and still did after every
preflight step: `loop/auto` at `bcd65ebc...`, `master` `f7df0d0e...`, `origin/master`
`7e6e21f4...` (legitimately stale), PAUSE
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`, status exactly
`?? .env.bak.1786014440`, gateway active, cron inactive, actuator unit absent, zero processes,
zero execution locks, all nine pilots at `concurrency_limit=0`, `max_inflight_workers=0`,
`publication=false`, routing variables unset, no open windows, and journals unchanged.

Preflight results:

```text
bundle=fc49e3f705a1f3791ebd7eb15d02f080848e55fe2f2ccb5ec478f95dada1ad5b (identical on arrival)
clone HEAD=ecc93de7... tree=9a6cdea2...  worktree clean
BASELINE_SUITES=19  BASELINE_FAILED_SUITES=0
phase1..phase8 audits = ALL PASS, RC=0
live-phase1 = ALL PASS, RC=0 (104 packages, hardened non-root gateway)
runtime snapshot = 19 files, root-owned 0700, manifest 7672f897...
restore-compare = EXACT (19/19 byte- and mode-identical vs live, all matching manifest hashes)
simulation = 0 conflicts; executable-surface diff 0; diff vs reviewed merge = the 8 awareness files
CAS gate dry-run = PASS 11/11, zero mutation
```

The live Phase 1 audit first returned `90` because the driver exported `RECURSION_ROOT` at the
clone for the whole sweep; that variable is the *live* root for the red audit. Re-run correctly
it is `ALL PASS`. That was a harness defect, not a production fault.

Retained root-owned mode-`0700` artifacts, outside `/opt/recursion`:

- runtime snapshot `/var/backups/recursion/preflight-20260811T231032Z-ecc93de`
- sealed evidence `/var/backups/recursion/preflight-evidence-20260811T231032Z-ecc93de`
  (git, units, configuration, queues/outboxes, journals, audit log and summary, manifest root
  `d2d4496a7b1caa964516aaf48a373228c0caaa54a131feea88ea36c53c8769a3`; no secrets, variable
  names only)
- armed `ROLLBACK.sh` and `TRANSACTION.sh` in that evidence directory, both syntax-checked

The reconciliation is conflict-free by construction, not merely by trial: production's 74
local-only commits touch exactly 8 awareness files and zero code, and the incoming range
touches 73 files with zero intersection. The transaction therefore carries a parameter-free
post-condition - the diff against the reviewed merge must equal exactly those 8 files, and the
diff over `scripts/`, `loop/`, `schemas/` and `CONTROL_PLANE.md` must be empty, which is what
proves the deployed executable bytes equal the reviewed bytes.

Two operational gaps remain open and are not blockers: `cron.service` is `inactive` but
`enabled`, so a host reboot resumes cron (PAUSE, not cron state, is the kill switch); and
`/var/backups/recursion` plus `/var/tmp` hold about 6 GB of unreaped snapshots and staging
clones on a 38 GB disk at 50% use.

The next action remains a separate, explicit authorization for the two-action transaction:
fast-forward `master` to the reviewed merge and merge it into `loop/auto`, with PAUSE asserted
and cron stopped. Cron resume, actuator installation, notifications, model/capacity
configuration, PAUSE removal and every pilot window remain separately gated.

## Hardening deployment executed under PAUSE (2026-08-11T23:47:51Z)

Lucas authorized the two-action transaction. It ran from the armed
`TRANSACTION.sh` in the sealed evidence directory and returned `TRANSACTION_OK`.
PAUSE remained asserted and cron remained stopped throughout.

```text
pre : loop/auto=bcd65ebc...  master=f7df0d0e...  origin/master=7e6e21f4...
post: loop/auto=458ee92b63d1250e0704f563b3a4c499e3031289
      master=ecc93de7...     origin/master=ecc93de7...
```

Action A fast-forwarded `master` to the reviewed merge under a compare-and-swap on its old
value. Action B merged that commit into the deployed `loop/auto`, producing merge commit
`458ee92b63d1250e0704f563b3a4c499e3031289` with zero conflicts. Action C set the
remote-tracking ref to the truth. Objects arrived by verified bundle because production
`origin` cannot authenticate.

A pre-flight check before execution confirmed production could actually create a merge
commit (`user.name`/`user.email` set, no `commit.gpgsign`, no active hooks). Action A moves
`master` before the merge runs, so a missing commit identity would have failed the
transaction after a partial ref move.

Independent closure, verified with fresh checks rather than the transaction's own assertions:

```text
reviewed merge is an ancestor of HEAD      YES
executable bytes on disk vs reviewed blobs 108 files compared, 0 mismatches
CONTROL_PLANE.md                           match
managed runtime vs pre-transaction snapshot 19/19 unchanged, 0 changed, 0 missing
live Phase 1 on the deployed hardening      ALL PASS, RC=0
PAUSE 919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db  root:root 0644 189B
gateway active/running, NRestarts=0
cron inactive; actuator unit absent; supervisor and timer inactive
worker units 0; project/worker/cycle processes 0; execution locks 0
nine pilots all concurrency_limit=0 max_inflight_workers=0 publication=false
decisions.jsonl and events.jsonl byte-identical to pre-transaction
open pilot windows 0
working tree changed vs pre-transaction: exactly the 73 reviewed files
```

The eight awareness files kept production's content, so no board, brief, inbox, escalation
or event history was lost to the merge.

Evidence and rollback are retained root-owned mode-`0700` outside `/opt/recursion`:
snapshot `/var/backups/recursion/preflight-20260811T231032Z-ecc93de`, evidence
`/var/backups/recursion/preflight-evidence-20260811T231032Z-ecc93de` with manifest root
`1f4aa3763149d8c0d3df9ac1658893810227a826dfee038a7f46f2b5bc305b01`. `ROLLBACK.sh` remains
valid against that snapshot; the pre-transaction `loop/auto` tip is additionally recoverable
from the reflog.

Gate 1 of the ordered list is therefore closed: the hardening is deployed and independently
verified under unchanged PAUSE. Gates 2 through 7 are untouched and each still requires its
own separate authorization - cron resume, actuator preflight/installation, bounded
model/capacity configuration, one exact pilot plan and window, PAUSE removal for that single
soak, and the observed close-out. Nothing in this deployment implies any of them.

Two operational gaps remain open, both outside this transaction's scope: `cron.service` is
`inactive` but `enabled`, so a host reboot resumes cron with the new bytes (PAUSE, not cron
state, is the kill switch, and the deployed code is the reviewed code); and about 6 GB of
unreaped snapshots and `/var/tmp` staging clones sit on a 38 GB disk at 50% use. Both are
separate reviewed changes.

## Authorized post-deployment maintenance (2026-08-11)

Lucas authorized two follow-up production changes after the hardening deployment. PAUSE
remained asserted, cron remained stopped, and the deployed tree was re-verified byte-identical
to the reviewed merge after both (108 executable files, 0 mismatches).

**Self-repo git authentication restored.** `origin` had been failing `Permission denied
(publickey)` since before Phase 8, which is why the remote-tracking ref was stale and why the
cron containment note said the normal fetch/resync path was stopped. The key was never
missing: `/root/.ssh/id_deploy` is the recursion deploy key and authenticates correctly. Git
simply had no identity to offer, because the box has no default-named keys, no
`~/.ssh/config`, and no `core.sshCommand`. Fixed repo-locally in untracked `.git/config` so
the tracked tree does not drift and so per-project deploy keys, which are selected via
`GIT_SSH_COMMAND` and take precedence, are unaffected. A real `git fetch` then succeeded and
independently confirmed `origin/master` is `ecc93de7...`, corroborating the tracking ref the
transaction had set. Full root cause and the rejected alternatives are in
`docs/plans/hermes-phase8-preflight-operational-nuances.md`.

This closes a prerequisite for cron resume: with fetch broken, `resync.sh observe` fails every
cycle and `work.sh` falls back to "building on local base", so an arm would build on a stale
base and re-invent merged work.

**Snapshot and staging prune.** Disk went from 18 GB used (50%) to 13 GB used (35%), freeing
about 6 GB. `/var/backups/recursion` went 3.6 GB to 495 MB and `/var/tmp` staging went about
2.4 GB to 19 MB. Only five superseded `phase1-remediation` snapshots were removed, plus fifty
disposable `/var/tmp` clones. Everything under 20 MB was kept deliberately, since small
history costs nothing and preserves the record.

Four anchors were hard-protected and verified intact afterwards:

```text
preflight-20260811T231032Z-ecc93de            (ROLLBACK.sh depends on it; manifest reads 19 files)
preflight-evidence-20260811T231032Z-ecc93de   (evidence + ROLLBACK.sh/TRANSACTION.sh present)
hermes-pre-phase8-20260808T224858Z-5a8b2f7464cb  (documented retained drill backup)
phase1-remediation-20260809T233144Z-498061d8  (the cited successful Phase 1 deployment bundle)
```

The Phase 1 rollback venv `/usr/local/lib/hermes-agent/venv.pre-phase1-498061d8f1dc` is
outside `/var/backups` and was not touched. The post-deployment suite was running throughout;
its staging directory was protected and it was confirmed still running afterwards.

## Post-deployment suite re-run (2026-08-12T00:20:34Z)

The full suite was re-run against the **deployed** tree rather than the reviewed commit: a
disposable `--no-hardlinks` clone pinned to `458ee92b63d1250e0704f563b3a4c499e3031289`, whose
tree was first confirmed identical to production's. This validates the merge result including
the eight awareness files, not only `ecc93de`.

```text
BASELINE_SUITES=19   BASELINE_FAILED_SUITES=0
phase1=4s  phase2=26s  phase3=16s  phase4=8s  phase5=65s
phase6=972s  phase7=300s  phase8=111s      all RC=0
live-phase1 RC=0
run 2026-08-11T23:52:25Z to 2026-08-12T00:20:34Z
```

Timings track the pre-deployment run closely (phase6 972s vs 969s, phase7 300s vs 307s,
phase8 111s vs 113s), so the deployment introduced no performance regression. The live Phase 1
audit passed this time because the driver was corrected to pass `RECURSION_ROOT=/opt/recursion`
for the red audit specifically; that variable is the live root there and the code root
everywhere else.

Final state: `loop/auto` at `458ee92b...`, `master` and `origin/master` both at `ecc93de7...`
and now genuinely in sync over a working fetch, status exactly `?? .env.bak.1786014440`, PAUSE
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`, gateway `active/running`
with zero restarts, cron inactive, actuator absent, zero execution locks, zero processes with a
working directory inside the checkout, and disk at 13 GB used of 38 GB (35%).

Evidence manifest root for the complete preflight, transaction, closure and re-run record is
`f7b0318a40f84b045f10a6f033b2773c87cf1d54c123510f93e58ab4be73a651` in
`/var/backups/recursion/preflight-evidence-20260811T231032Z-ecc93de`.

Gate 1 is closed and independently re-verified. Gate 2 (cron resume) is now unblocked, its
prerequisite having been the self-repo fetch. Gates 3 through 7 are untouched.

## Gate 2: cron resumed under PAUSE (2026-08-12T00:33:27Z)

Lucas authorized cron resume. Before starting the service, the corrected quiescence gate was
built and validated with a positive control, and **one supervised cycle was run by hand** -
the same command cron invokes - so the behaviour was observed before it could fire
unattended.

The supervised cycle was correct on every axis:

```text
PAUSED (state/PAUSE) — local monitoring only: heartbeat + brief + durable escalations;
                       no notifications or execution
portfolio observed (PAUSE: fetch/status only; no branch maintenance)
brief done (deliver=0)
paused: skipping plan/drain/conduct (execution)
cycle done (signal=brief)                                          CYCLE_RC=0
```

`deliver=0` is the notification proof: zero sends. `new_commits_on_loop_auto=0` is the
board-commit proof - the hardened `board()` returns early under PAUSE, so the 74 `loop: board`
commits the old code produced will not recur. `portfolio observed` is the auth fix proving
itself: the fetch succeeded where it had been failing every cycle.

Unchanged across the cycle: HEAD, `loop/auto`, `master`, PAUSE hash, `state/BOARD.md`,
`events.jsonl` (16 lines), `decisions.jsonl` (6 lines). Written, as designed: a new
`state/briefs/2026-08-12.md` labelled `PAUSED — execution is halted; this brief is monitoring
only`, and `last_seen`/`count` bumps on three pre-existing escalation rows (33 to 34) with no
new row.

`cron.service` is now `active/running`, PID 608533, PAUSE intact, crontab unmodified (four job
lines, mtime still 2026-08-07), actuator still absent, quiescence 0. Next fires are 03:00Z
(`fallback-watch`), 03:15Z (`cycle` and `hermes-decide`), and 06:30Z (the forced brief).
Rollback is `systemctl stop cron.service`.

Gate 2 is closed. Gates 3 through 7 - actuator preflight/installation, bounded model/capacity
configuration, one exact pilot plan and window, PAUSE removal for that single soak, and the
observed close-out - remain untouched and separately gated. PAUSE remains asserted; nothing
executes.

One consequence to carry forward: with cron running, the working tree now legitimately holds
uncommitted monitoring artifacts, so a future gate must not assert an exact
`git status --porcelain` string. See the nuances note.

## Gate 3 attempted: actuator failed closed again, new root cause found (2026-08-12T00:47Z)

Lucas authorized gate 3. The reviewed unit was installed, observed, and **rolled back**. The
terminal condition is the same as 2026-08-10 - the PAUSE-safe no-op is still pending - but the
cause is different, and it is one layer deeper than last time.

**What is genuinely fixed.** The legacy-journal import defect is closed. Verified first in a
disposable fixture against the real journal and then live: `import-journal` returned 0, the five
pre-schema records were quarantined to `state/decision-queue/legacy/`, and the valid record
moved `pending` to `running`. It had never been claimable before; on 2026-08-10 import failed on
line 1 and returned before line 6 could be reached. That bug is real, was real, and is gone.

**What fixing it exposed.** With the claim now reachable, `conduct-hermes.sh --paused-safe`
refuses the record deterministically:

```text
conduct-hermes: REFUSED - decision authority/effect digest is absent or inconsistent
```

The check at `scripts/conduct-hermes.sh` recomputes an action payload digest and requires the
record to carry a matching `decision_payload_sha256`:

```sh
[ -n "$DECISION_PAYLOAD_SHA256" ] && [ "$DECISION_PAYLOAD_SHA256" = "$ACTION_PAYLOAD_SHA256" ]   || { log "REFUSED - decision authority/effect digest is absent or inconsistent"; exit 5; }
```

`decision-6f037cbf6fb51a7f641ea9d6` does not have that field. Its keys are `actor, args,
arguments, confidence, cost_usd, goal, goal_id, id, idempotency_key, project, provider, reason,
schema_version, status, ts, usage, verb, why, wrong_if`. It was written `2026-08-09T15:15:01Z`,
before the hardening introduced the digest. **It can never validate, under PAUSE or otherwise.**

This is the same class of defect as the legacy-journal bug and the compat patch does not cover
it. That patch quarantines records with no `id` (pre-schema). This record has an `id` and
`schema_version: 1`, so it passes quarantine and reaches a validator that requires a field
added after it was written. The compat cutoff is drawn at the wrong place.

**A second, independent defect makes the first one invisible.** `rc=75` means two different
things. `scripts/conduct-hermes.sh` uses it for "deferred, pending intentionally restored"
(`succeeded:0|failed:1|cancelled:1|pending:75`, and the comment "rc=75 intentionally restores
pending before the decision claim is deferred"), and the actuator therefore whitelists it:

```sh
case "$paused_rc" in
  0|4|75) : ;;
  *) log "PAUSE-safe queue pass failed rc=$paused_rc; pending state was preserved" ;;
esac
```

So a permanent refusal is indistinguishable from a normal PAUSE deferral, and the actuator logs
nothing at all. Across roughly ninety seconds of two-second passes the journal contained exactly
one line - `started`. The queue visibly oscillated `pending` to `running` to `pending` while the
service reported healthy. This is the 2026-08-10 "the actuator suppresses that command's
diagnostic" defect in a new form: not suppressed by `>/dev/null` any more, but by an exit code
that conflates a deferral with a refusal.

**Rollback.** `GATE3-ROLLBACK.sh` ran clean against snapshot
`/var/backups/recursion/gate3-20260812T004315Z` (20 files). The unit was stopped, disabled and
removed; the managed runtime was restored exactly. Verified after: actuator unit absent and
inactive, PAUSE `919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`, gateway
active, **cron still active so gate 2 was preserved**, queue back to one pending and zero
quarantined, `decisions.jsonl` 6 lines and `events.jsonl` 16 lines unchanged, zero outbox files,
zero worker units, quiescence 0, and the deployed tree still byte-identical to the reviewed merge
(108 files, 0 mismatches). No notification, execution, worker, or pilot occurred.

**Gate 3 is blocked, not merely deferred.** Two reviewed changes are needed before a retry, and
neither was made here:

1. Extend the legacy compat cutoff to cover records that carry an `id` and `schema_version` but
   predate `decision_payload_sha256` - quarantine or migrate them the same way, and prove the
   valid path still fails closed for a genuinely inconsistent digest.
2. Separate "deferred" from "refused" in the actuator's accounting. A permanent refusal must be
   visible in the journal and must not read as a healthy pass. Whitelisting `75` is correct for a
   deferral and wrong for a refusal; they need different codes or an explicit reason.

Until both land, installing the actuator gives a service that spins forever on an undrainable
record while reporting healthy. That is a worse failure than the one it replaces, because it is
silent.

## Gate 3 root cause corrected, and Fix 2 landed (2026-08-12T01:00Z)

**The previous section's root cause was wrong in its first half and is corrected here.** It
said the digest check was the blocker and that the legacy compat cutoff was drawn at the wrong
place. Measurement refutes that:

- `decision-queue.py` `normalize()` already computes `decision_payload_sha256` (line 222), and
  the equality checks tolerate `None` and recompute (lines 176/178). A **fresh** import of the
  real journal produces a projection carrying
  `decision_payload_sha256=e29cf20c1b1067e1b12fa43c82e52fbf326d293a498e60195ccacc9e53cd74cf`.
- The projection on production lacked the field only because it was written by **pre-hardening
  code and never re-normalized**; `import-journal` does not overwrite an existing projection.
  That is stale state, not a code defect, and needs no compat change.

**The actual blocker is provenance, one layer further on.** With a freshly imported projection
the same record is still refused, for a different reason:

```text
conduct-hermes: REFUSED - missing or unknown decision provenance ''
```

`conduct-hermes.sh` requires `decision_source` to be `hermes-model`, `project-reconciler`, or
`notification-reconciler`. The 2026-08-09 record has neither `decision_source` nor
`routed_decider` - it predates the provenance model. `normalize()` computes a payload digest but
does not synthesize provenance, and it must not: inventing a `decision_source` would forge
authority, which is precisely what that check exists to prevent. So the record cannot execute,
and no amount of digest work changes that.

The lesson is the one already in the nuances note, applied to my own analysis: an arithmetic
result that closes ("the digest is absent, the check requires it") describes the objects it
measured, not the cause. The first refusal encountered was not the only refusal.

### Fix 2 landed: a refusal is no longer relabelled as a deferral

`finalize_claim`'s `absent` branch read:

```sh
if [ "$result_rc" -eq 75 ] || [ -f "$ROOT/state/PAUSE" ]; then   # defer, return 75
```

A genuine PAUSE decline already returns 75 through the first clause, so `|| [ -f PAUSE ]` only
ever caught **other** failures and relabelled them as deferrals for as long as PAUSE was
asserted. The actuator whitelisted `0|4|75` and logged nothing, which is why the service
reported healthy for ninety seconds while the queue oscillated and two separate hard refusals
went unreported.

The patch keeps the disposition identical - pending is still preserved under PAUSE, which is the
conservative behaviour - and changes only visibility: a non-75 child rc under PAUSE now logs
`REFUSED under PAUSE - child rc=N ...; pending preserved, NOT a deferral` and returns a distinct
`76`. `hermes-actuator.sh` handles `76` explicitly and points at
`loop/logs/<date>-conduct-hermes.log` for the specific reason, latched so a two-second loop
reports once rather than flooding.

Measured on the real journal in a disposable fixture: `CONDUCT_RC=76` (was 75), both the
provenance refusal and the new refusal line present, and `pending=1 running=0 failed=0
succeeded=0` - disposition unchanged.

Worth recording: the specific reason was **always on disk**. `conduct-hermes` tees to
`loop/logs/<date>-conduct-hermes.log`, so `REFUSED - missing or unknown decision provenance`
was being written every pass. Only the systemd journal was silent, because the actuator sends
the child's stdout to `/dev/null` and the exit code carried no distinction. Evidence existing
somewhere is not the same as evidence reaching the person reading the service.

### Fix 1 is a genuine fork and is NOT implemented

The record must be dispositioned, and there are three materially different ways to do it. They
differ in blast radius, so this is escalated rather than chosen:

1. **Quarantine it via the reviewed exact-hash allowlist**, as the five pre-schema records were.
   Its stripped line digest is
   `17e365c897961b6699ffc868b42e9ba469a1c6b5797798686224d6a4bb73165b`. Obstacle:
   `_expected_legacy_markers` only reaches the allowlist for records that FAIL
   `validate_record`, and this one passes, so the loop breaks before quarantine. Making it
   reachable means changing what "valid" means.
2. **Make provenance a validation requirement** in `decision-queue.py`, so any record without
   `decision_source` fails validation and the allowlist then handles this one historical
   exception. Principled and fails earlier, but it changes validity for every producer and
   needs proof that no current emitter omits provenance.
3. **Treat an allowlisted pre-provenance noop as a terminal success in `conduct-hermes`.**
   Semantically exact - the record's own verb is `noop`, and doing nothing needs no authority -
   but it puts a bypass inside the authority checker, the most security-sensitive point in the
   file.

Option 2 is the most principled and option 1 the most conservative; option 3 is cheapest and
carries the worst failure mode if ever generalised. The choice determines what "a valid
decision" means going forward, so Lucas picks it.

Gate 3 therefore remains open. Production is untouched by all of the above: the patch exists
only on the review branch and in disposable fixtures.

## Fix 1 landed: decision provenance is a validation requirement (option 2)

Lucas chose option 2. The precondition stated when proposing it - that no current emitter omits
provenance - was audited first, and it holds. The three live producers all set the field, and
they set exactly the three values `conduct-hermes` accepts:

```text
scripts/hermes-decide.sh:300        decision_source = hermes-model
scripts/notification-outbox.py:1122 decision_source = notification-reconciler
scripts/project-reconcile.py:697    decision_source = project-reconciler
```

The change is in three places, kept in sync:

- `scripts/validate-control-state.py` - `decision_source` added to `REQUIRED["decision"]` and to
  the non-empty set, plus a closed `DECISION_SOURCES` enum. A decision that cannot actuate is now
  rejected at validation instead of at actuation, where the failure was invisible.
- `schemas/control-plane/v1/schema.json` - the published contract gains the same required field
  and enum. Patched textually: re-serialising with `json.dumps` reformatted all 245 lines and made
  the change unreviewable, so the edit is two anchored strings and the diff is five lines.
- `scripts/decision-queue.py` - a second reviewed exact-hash allowlist entry,
  `LEGACY_PRE_PROVENANCE_NOOP_SHA256`, holding the one historical record's stripped line digest
  `17e365c897961b6699ffc868b42e9ba469a1c6b5797798686224d6a4bb73165b`, with its own key set,
  classification `reviewed-pre-provenance-noop`, and shape assertions that require the provenance
  fields to be **absent**. That last condition is the load-bearing one: a record carrying a
  *wrong* authority must keep failing closed rather than being promoted to legacy state. The
  prefix validator now spans both reviewed blocks in exact order.

Measured in a disposable fixture against the real journal:

```text
import-journal            rc=0   pending=0   legacy=6
new marker                id=legacy-decision-17e365c897961b6699ffc868
                          classification=reviewed-pre-provenance-noop  disposition=not-enqueued
positive control          decision WITH valid provenance          -> VALID
negative control          decision_source='made-up-authority'     -> ERR invalid decision provenance
negative control          decision_source absent                  -> ERR missing required field(s)
conduct-hermes --paused-safe                                      -> RC=0, empty queue
```

The positive control matters as much as the negatives: it proves the schema change does not
reject what the live emitters actually produce. The gate 3 blocker is resolved - the queue now
reaches empty instead of oscillating on an undrainable record.

Both fixes exist only on the review branch. Production still runs the merged `ecc93de` bytes with
PAUSE asserted, cron active, and no actuator installed. A gate 3 retry needs this branch reviewed
and merged first, then a fresh PAUSE-held preflight and its own authorization.

## Gate 4 closed: bounded model and capacity limits (2026-08-12T03:40Z)

Lucas authorized the gate 4 configuration. Four keys were appended to `/opt/recursion/.env`
(mode 600, root:root, gitignored, 12 keys -> 16), backed up first:

```text
RECURSION_PILOT_MODEL=gpt-5.6-terra
RECURSION_PILOT_GLOBAL_CONCURRENCY=3
RECURSION_MODEL_ALIAS_SONNET_ROUTINE=sonnet
RECURSION_MODEL_ALIAS_OPUS_REVIEW=opus
```

The full regression suite ran against the merged tree (`6349d4f2`, PR #40) and is green
end to end:

```text
START=03:13:07Z  END=03:40:34Z
BASELINE_SUITES=19  BASELINE_FAILED_SUITES=0  baseline RC=0
phase1..phase8 RC=0     live-phase1 RC=0
```

Phase 8, which failed before the reviewed-prefix repair, is RC=0. Gate 4 is closed.

The first unattended cron cycle fired at `03:15:25Z` with the actuator live and behaved as
designed: `portfolio observed (PAUSE: fetch/status only)`, `intake 0 ingested, watermark
486 -> 486`, `brief done (deliver=0)`, `paused: skipping plan/drain/conduct (execution)`, no
`loop: board` commit, journals unchanged.

## Gate 5 preflight: four blockers, three of them new (2026-08-12T03:45Z)

Gate 5 is "synthesize and approve one exact pilot plan and bounded pilot window". Its
machinery is proven by phase8-green. What the preflight measured is that **the inputs never
reach it**. All four blockers below fail silently -- each one presents as "Hermes ignored the
instruction", which is the hardest failure mode to diagnose and the same species that cost
gate 3 an attempt.

### B0 - every model decision has been discarded at normalization for four days

`loop/logs/<date>-decide.log` for 2026-08-07, 08-08, 08-09 and 08-10 each end:

```text
decide: decision: noop - <reasoning>
decide: REFUSED - decision could not be normalized to schema v1
```

Replaying the real 08-10 model output through `scripts/decision-queue.py normalize` in a
clone gives the exact cause:

```text
decision-queue: ERR goal_id is not a canonical identifier
```

The model wrote `"goal": "PAUSE safety: preserve operational awareness while execution
remains halted"` -- a purpose sentence. Substituting `"goal": "goal-pause-safety"` and
changing nothing else, normalize returns RC=0 with a complete canonical record. The
identifier was the only fault.

The trusted artifact agrees: production's `state/decisions.jsonl` holds six records, all
`noop`, and record 6 carries `goal_id='architecture-rebase'` -- the shape that works. The
five earlier ones are the pre-schema legacy noops with no `goal_id` at all.

Cause: the prompt's decision block said `"goal":"existing goal, or the fresh proposed goal
id"` and never stated the constraint (`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` -- no spaces, no
colons). **Not** a structural impossibility: `noop` normalizes fine with an identifier, and
has before. It is a prompt-precision defect with a 100% failure rate over the observed
window.

### B1 - the decide cron never runs while the cycle cron runs

Both are scheduled on the same minute:

```text
15 */3 * * *  scripts/cycle.sh          # recursion-cycle
15 */3 * * *  scripts/hermes-decide.sh  # recursion-decide
```

`hermes-decide.sh` takes `state/.locks/hermes-control.lock` with `flock -n` and exits 73 on
contention. The only fire since cron resumed at gate 2:

```text
[2026-08-12T03:15:01Z] decide: control lock contended; refusing overlapping planner/sync work
```

This is systematic, not intermittent -- the cycle wins the lock every time because the two
entries share a minute. The decision turn has produced nothing since cron came back.
A cron edit is Lucas's under rule 3.

### B2 - the prompt told the model noop was its only option under PAUSE

`scripts/hermes-decide.sh` RUNTIME/PAUSE block, pre-fix:

```text
PAUSED - execution is halted. You may still decide; the only
executable verb while paused is 'noop'. Say what you WOULD do and why.
```

`conduct-hermes.sh` admits six verbs under PAUSE (`noop`, `pilot-plan-propose`,
`pilot-plan-approve`, `pilot-window-open`, `pilot-window-close`, `assist-answer`), and gate 5
*requires* `pilot-plan-propose` to be emitted while PAUSE is still asserted -- `project-pilot.py`
refuses a window unless PAUSE is present. The model obeyed the stale line verbatim; its 08-10
reasoning reads "no execution-capable verb is permitted".

`build_pilot_brief`'s own PAUSE block ("no worker may run") was already correct. Only the Head
Hermes one was stale.

### B3 - the chosen pilot has zero worker capacity in two places, not one

`project-pilot.py` window_action:

```python
"max_inflight_workers": min(int(pilot.get("concurrency_limit") or 0),
                            int((pilot.get("budget") or {}).get("max_inflight_workers") or 0))}
if record["max_inflight_workers"] < 1: fail("pilot window has zero worker capacity")
```

The chosen pilot record has `concurrency_limit: 0` **and**
`budget.max_inflight_workers: 0`. The earlier note in this document ("raise that pilot's
`max_inflight_workers` to 1") is half the fix -- the `min()` still yields 0. A third bound
applies at dispatch: `project-reconcile.py` takes `min(concurrency_limit, max_inflight_workers,
goal.budget.max_concurrency)`, so the plan's own goal budget must carry `max_concurrency >= 1`
as well.

### B4 - the approval instructions must go to #general, not the project topic

`require_lucas_instruction` demands `principal in RECURSION_INTAKE_ALLOW` (default
`8913126684`) **and** `routing_target == "head-hermes"`. It then reads only the body under a
`## INSTRUCTION` heading, up to the next `##`, tokenized on
`[A-Za-z0-9][A-Za-z0-9._/-]{0,127}`, and requires every bound to appear as a bare token.

So the approve and window instructions go to **Head Hermes, topic 1 (#general)** -- posting
them in the project topic fails closed -- and must literally contain the plan id, the sha256,
the goal id, the window id and the seconds.

### Fixes written

B0 and B2 are on `review/hermes-phase8-gate5-decide-turn` (`4f4aca5`). `phase8-green` gains
both assertions. The stale-claim assertion is anchored on the single-line substring present in
the pre-fix bytes: the natural phrasing spanned two `echo` lines and **passed vacuously** until
a control against `origin/master`'s file showed only 3 of 4 assertions could fail. Re-run after
the fix: 4/4 fail on the pre-fix file, 4/4 pass on the patched one.

B1 (cron) and B3 (pilot capacity) are Lucas's, and neither is code.

## Gate 5 preconditions B1 and B3 applied (2026-08-12T03:52Z)

Lucas authorized the cron fix and the recommended capacity settings. Both are live and
verified 18/18. Backup and armed rollback:
`/var/backups/recursion/gate5-preconditions-20260812T035216Z` (root-owned 0700, manifest
verifies, `ROLLBACK.sh` restores both).

**B1.** The decide entry was restored to `install-cron.sh`'s reviewed default. Production had
drifted to the cycle's own minute; the installer's default exists precisely to keep the two off
the same lock:

```text
before: 15 */3 * * *  ... scripts/hermes-decide.sh  # recursion-decide
after:  45 */6 * * *  ... scripts/hermes-decide.sh  # recursion-decide
```

One line changed; the brief, cycle and watch entries are byte-identical to the backup. Next
fire is `06:45Z`. **Restoring the reviewed value was preferred over inventing a third schedule**
- the drift is what caused the defect.

**B3.** The chosen pilot record, the two fields the `min()` reads:

```diff
-  "concurrency_limit": 0,
+  "concurrency_limit": 1,
-    "max_inflight_workers": 0,
+    "max_inflight_workers": 1,
```

The whole diff is those four lines. The file still validates as a `pilot` control-plane object,
and the other eight pilots remain at `0/0` - verified by re-reading all nine, not by inspecting
the one that changed.

Unchanged throughout: PAUSE `919371834778def8...`, gateway active, actuator active, zero open
windows, zero pending queue entries.

### The apply step corrupted the crontab on the first attempt

The transaction's `sed` replacement used the new crontab line as the replacement text. That line
contains `&&`, and **`&` in a sed replacement expands to the entire match**, so the installed
line became a doubled, malformed entry:

```text
45 */6 * * * cd /opt/recursion 15 */3 * * * cd /opt/recursion && ... # recursion-decide15 */3 ...
```

Three of the verify checks passed on that corrupt line - "exactly one crontab line changed",
"decide no longer shares a minute with cycle", and "still 5 recursion lines" were all true of
it. **Only the exact-value comparison caught it.** It was restored from the backup taken
seconds earlier and re-applied with a literal Python line replacement that asserts it matched
exactly one line, then re-verified 18/18 from scratch.

## Deployment path for the gate 5 prompt fix

Production is a git checkout on `loop/auto`, and its HEAD `59b13c61` is
`Merge commit '6349d4f2' into loop/auto` - the merged master. Its `scripts/hermes-decide.sh`
hashes identical to its own HEAD, so the checkout is clean.

The B0/B2 prompt fix therefore reaches production as: push
`review/hermes-phase8-gate5-decide-turn` -> Lucas merges to `master` -> merge `master` into
`loop/auto` on the box under the usual CAS gate. **Until that merge lands, production still runs
the old prompt**, so a decide turn would keep discarding its output at normalization. Gate 5
should not begin before the deploy.

## B0/B2 deployed, and the first successful decision in five days (2026-08-12T04:31Z)

PR #41 (`78c0ed6`) merged to master and was merged into production's `loop/auto` under a CAS
gate: 16/16 gate, 31/31 verify, production HEAD `1a870639`. Snapshot and armed rollback at
`/var/backups/recursion/gate5-deploy-20260812T042801Z`. Correctness was asserted on **blob OIDs**
rather than file hashes so no line-ending filter could launder a mismatch, and the merge was
simulated in a throwaway clone first: zero conflicts, exactly four files, the two script blobs
identical to the reviewed ones.

A supervised decide turn was then run rather than waiting for the unattended `06:45Z` fire. B0 is
fixed, measured on production:

```json
{"goal_id":"goal-pause-safety","status":"queued","decision_source":"hermes-model",
 "verb":"noop","instruction_id":"486","provider":"openai-codex","cost_usd":0.0}
```

The model used the identifier example the prompt now carries. `notify: PAUSE asserted; transport
refused` confirmed delivery stayed suppressed. It still chose `noop`, which is correct -- 486 asks
whether the box is paused; it is not a plan request. B2's effect will only show against a real
plan instruction.

## The decision wedged, and the actuator was stopped (2026-08-12T04:34Z)

The action failed one second after creation with `result_evidence: "instruction coupling failed
before dispatch"`, and was then retried **every ~6 seconds, 52 times**, until
`hermes-actuator` was stopped by hand. Containment mutated no state; the loop count froze.

The wedge mechanism is certain, read from the code and matching the observed exit codes exactly:

1. The failed action reconciles by calling `intake.sh release 486 <action>`.
2. 486 is still `open` with **empty** `action_ids` -- it was never consumed.
3. `intake.sh:396` treats `open` as "a previous release already reopened this" and requires the
   stored ids to equal the ones being released. Empty never matches, so it exits 2.
4. `--recover-only` therefore returns 74, where `finalize_claim` needs **1** for a `failed`
   action (`succeeded:0|failed:1|cancelled:1|pending:75`).
5. The parent preserves the decision in `running/` and retries forever.

**What triggered the initial coupling failure was not proven.** It occurred during heavy lock
contention -- the actuator was refused the control lock six times between 04:31:17 and 04:31:28
because the decide run holds it for its entire ~40s model call -- and `intake.sh consume` does
return 73 on state-lock contention. That is consistent but inferred. A first sandbox replay was
**invalid** (the operator lock namespace is pinned, so it reproduced the copy rather than
production) and its error was discarded rather than reported.

### Both halves fixed, verified against the real wedge

`review/hermes-phase8-decide-lock-and-backticks`:

- `fc5abee` -- `hermes-decide.sh`: `flock -n` to `flock -w ${RECURSION_CONTROL_LOCK_WAIT_S:-120}`,
  and the prompt's backticks to single quotes.
- `5ffa1bc` -- `intake.sh` release admits the never-consumed case; `conduct-hermes.sh` takes the
  same `flock -w` treatment.

Fixing only the decider was half a fix: it holds the lock longer, and an actuator that gives up
instead of waiting is how the coupling lost its race. **Losing here corrupts an action rather
than skipping a turn**, so the actuator was the more damaging half.

Proof against the actual production wedge, in a faithful sandbox copy:

```text
before fix:  intake: reopened instruction 486 has different action evidence   rc=74
after fix:   released 486 -> action-002beb5c411a2688dc9ec7db (never consumed)  rc=1
instruction: status open (unchanged), action_ids now records the failed action
replay:      rc=1, rc=1  (idempotent)
```

`rc=1` is what `finalize_claim` accepts, so **the fix is also the unwedge** -- the stuck decision
drains itself when the actuator restarts on the fixed code. No hand-edit of control state was
needed, and the repair is verified against the real failure rather than a reconstruction.

`intake.test.sh` gains three behavioural cases with their own control. Against the unpatched
script all three fail; with it, all three pass.

## Root cause of the wedge: systemd gives a service no HOME (2026-08-12T05:45Z)

The lock-contention explanation offered earlier for the 04:31:33 coupling failure was **wrong**.
It was flagged at the time as inferred rather than measured, which is the only reason it did not
become the recorded cause. Side by side, one file different, same sandbox, same root:

```text
FIXED    intake.sh, unit sandbox, real root:  rc=0  released 486 -> action-... (already)
UNFIXED  intake.sh, unit sandbox, real root:  rc=1  intake.sh: line 44: HOME: unbound variable
```

`hermes-actuator.service` sets only `RECURSION_ROOT`. systemd supplies no `HOME`. `intake.sh`
runs under `set -u`, so line 44 -

```sh
DB="${RECURSION_HERMES_DB:-$HOME/.hermes/state.db}"
```

- was a fatal error **before any subcommand ran**. `consume`, `release` and `acknowledge` were
all equally dead, and none of them even uses `DB`. The actuator has therefore never been able to
couple an instruction to an action, for the entire life of the service. The decision that wedged
did so because its `consume` died here; the release could then not reconcile it, and the retry
loop followed.

It looked like lock contention because every successful run was from an interactive shell, which
has `HOME`.

### The class assertion found three more, and two are worse

`work.sh:106` and `publish.sh:62` expand `~` against `$HOME` under `set -euo pipefail`, and
`conduct-hermes.sh` invokes both (lines 700, 712), so they run under the actuator too. Both are
conditional - `work.sh` only touches `$HOME` when the repo has an ssh key configured, which is
every real repo - so they would not have failed until work actually dispatched. **That is gate
6/7.** `resync.sh:55,170` and `tests/portfolio-lint.sh:52` complete the set.

All five now carry `branch-review.sh`'s existing `HOME="${HOME:-/root}"` idiom in the same
position. That file had already been fixed for this once, which is why this ships as a class
assertion rather than five spot fixes.

The checker had two bugs of its own, both found by running it: it matched its own file because
its failure message contained a literal `$HOME`, and overlapping globs double-counted every
top-level script. Against `origin/master` it now reports exactly the five; against the branch,
zero.

## Gate 5 rehearsal contaminated production, and the root cause (2026-08-12T07:05Z)

**A sandbox was never a sandbox.** Every script that sources `.env` computes
`ROOT="${RECURSION_ROOT:-<script dir>}"` FIRST and sources `.env` with `set -a` AFTER.
Production's `.env` pins `RECURSION_ROOT=/opt/recursion`, so from that line on the shell's `ROOT`
and the exported `RECURSION_ROOT` disagree - and every Python child reads the environment.

Six gate 5 rehearsals run with `RECURSION_ROOT=/var/tmp/gate5rehearse` enqueued **ten decisions
into production**. `bash -x` showed it in one line:

```text
+ QUEUE_PATH=/opt/recursion/state/decision-queue/pending/decision-d845985b12c9b12075a9fa5a.json
```

The tell was visible for an hour and was misread: the sandbox's journal never grew, and that was
recorded twice as "an unexplained sandbox artifact" instead of prompting the question *where are
the writes going instead*. An empty destination is a routing symptom, not an inert one.

### What it did and did not do

`validate_plan` rejected the malformed plans exactly as designed, so **no plan, goal, window or
commit was ever created**. The blast radius was the queue and the append-only journal. One
injected decision re-wedged the actuator because its action referenced instruction `999`, which
exists only in the sandbox - a further variant of the never-consumed release: an action coupled
to an instruction that is *absent* cannot be released either.

### Cleanup, and the control that mattered

The journal is append-only and is the recovery source, so deleting projections would only have
`import-journal` recreate them. The supported path was used instead: `claim-pending --verb
pilot-plan-propose` then `decision-queue.py fail <path> "<reason>"`, moving each to `failed/`
with an explicit operator reason. `pending -> failed` is not a legal direct transition; a
decision must be claimed first.

**The inventory control caught that one queued decision was not mine** - the legitimate `06:45`
cron `noop`, `actor=head-hermes`, `goal_id=pause-safety`, matching the decide log. Cleaning by
"everything currently pending" would have destroyed it. Provenance was asserted per record
(`verb` starts with `pilot-` AND `instruction_id=999`), not by position or timestamp.

That legitimate decision is also the unattended proof of B0/B1/B2: the decide cron fired on its
new `45 */6` schedule and produced a normalizable decision with a canonical `goal_id`.

### The fix

All fifteen `.env`-sourcing scripts now re-assert `RECURSION_ROOT="$ROOT"` immediately after the
source. A caller-supplied root wins over `.env` - otherwise no copy of this system can be tested
against - and when the caller supplied none, the exported value matches the derived `ROOT`
instead of drifting. Proven side by side, one file different:

```text
before   ROOT=[/var/tmp/isotest]   RECURSION_ROOT=[/opt/recursion]
after    ROOT=[/var/tmp/isotest]   RECURSION_ROOT=[/var/tmp/isotest]
```

End-to-end: a decide turn in a sandbox left production's journal and queue unchanged while the
sandbox's grew. `phase8-green` guards the class; control against `origin/master` reports 15
unguarded there and 0 after.

## Malformed decisions are now retried, bounded (2026-08-12T07:14Z)

About one rehearsal in four emitted a syntactically broken decision block - one closing brace too
many in a 2.2 KB nested plan. With no retry the whole turn was discarded and the next attempt was
six hours away on cron, which during gate 5 reads as Hermes ignoring an instruction.

Only a MALFORMED reply is retried. A decision that parsed and was refused on its CONTENT stays
terminal, or the loop would grind against the same objection. The cost fence stays INSIDE the
loop so every attempt proves its own provider and cost and a metered fallback is never retried
into a second billed call. Bounded by `RECURSION_DECIDE_ATTEMPTS` (default 3).

Both branches were exercised with a stub model rather than trusted by shape: malformed-then-valid
gave 2 calls and a recorded decision; always-malformed gave 3 calls, a refusal, and nothing
recorded. **Making that stub test permanent is the top open follow-up** - it exists only as a
manual proof in the commit message.
