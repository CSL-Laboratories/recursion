---
title: Hermes Project Pilot Phase 0 Evidence
type: verification
status: ready-for-review
last-verified: 2026-08-11
related:
  - CONTROL_PLANE.md
  - docs/plans/hermes-project-pilot-production-plan.md
  - docs/architecture.md
  - docs/delegation-design.md
  - docs/plans/hermes-project-pilot-phase3-handoff.md
  - docs/plans/hermes-project-pilot-phase4-handoff.md
  - docs/plans/hermes-project-pilot-phase5-handoff.md
  - docs/plans/hermes-project-pilot-phase6-handoff.md
  - docs/plans/hermes-project-pilot-phase7-handoff.md
  - docs/plans/hermes-project-pilot-phase8-handoff.md
---
# Hermes project pilot Phase 0 evidence

## Verdict

Phase 0 established the reproducible baseline and red controls without fixing a defect. Phases 1
through 6 subsequently closed their authoring controls on separate reviewed bases and were merged
through Lucas's review workflow. Phase 7 now closes its authoring controls on merged Phase 6. Lucas
separately authorized publication after the Phase 7 controls/evidence completed; the review branch
was pushed, and no PR was opened. Exact evidence is appended below.

Production remains paused and undeployed for Phases 1-7. No live pilot ran, no autonomous execution was
enabled, no product repository changed, and no protected or default branch was pushed or merged by
this work. The two live Phase 1 findings remain deployment gates, not waived controls.

## Immutable starting points

| Item | Evidence |
|---|---|
| Authoring base | `origin/master` at `b1391908c17dc61c65613b0adda85918b486c1cd` |
| Authoring branch | `review/hermes-phase0-controls` in `C:\Users\drfoo\github\recursion-hermes-phase0` |
| Phase 0 commit | `2e98bc57a6a0ff1257f4d23166af36c10619f902` (`Add Hermes pilot Phase 0 controls`) |
| Review publication | draft PR #21 (`https://github.com/NotSkittlz/recursion/pull/21`), `review/hermes-phase0-controls` -> `master` |
| Deployed commit | `/opt/recursion` branch `loop/auto` at `58b912c46b0bbba8f420fdda685e05b958279daa` |
| User-owned checkout | `master` at `20c1fa77042f3aa9f5d8978352792f3b058bc0d1`, ahead 5 / behind 115; dirty state preserved |
| Production plan | copied byte-for-byte into the authoring worktree; SHA-256 `b47295865fcc6be190d47f36e5a6eaa7d2260522baf8b816b30c521edc6873bd` |
| Remote PAUSE | present before and after every live check; SHA-256 `919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db` |

The live PAUSE reason is the 2026-08-06 human-led architecture rebase halt. The only live-tree
anomaly observed was an untracked `.env.bak.1786014440`; it was not opened, moved, or changed.
The pre-existing live flags remain `RECURSION_ALLOW_PUSH=1`, `RECURSION_AUTO_DRAIN=1`,
`RECURSION_AUTO_PLAN=1`, `RECURSION_AUTO_REFLECT=1`, and `RECURSION_ALLOW_AUTOMERGE=0`.
Phase 0 did not edit them; this makes the interruptive PAUSE control a critical prerequisite.

## Historical Phase 0 next-session handoff (superseded)

Phase 0 is committed and published for review. Do not begin implementation unless PR #21 has been
reviewed and merged. After approval, fetch the new `origin/master` and create a new clean Phase 1
authoring worktree/branch from that exact commit. Do not reuse or modify the dirty local `master`,
and do not base Phase 1 on an unmerged review branch.

Phase 1 is the only authorized implementation scope for the next session. Reproduce the existing
17/17 green baseline and the intended Phase 1 red results first, then make the Phase 1 controls pass
while keeping the baseline green. Append the exact green-after-red commands, commit OID, results,
and live PAUSE evidence to this document. Do not begin Phase 2, run a pilot, enable autonomous
execution, merge or deploy, or remove `state/PAUSE`.

## Existing baseline

Run `scripts/audit/hermes-pilot-phase0-baseline.sh` from a named Linux branch at the authoring
base. It deliberately excludes the new `*-red.test.sh` controls, asserts that live PAUSE exists
before and after, and records both baseline and deployed commits.

The 16 existing `scripts/tests/*.sh` suites plus `scripts/work.selftest.sh` all passed at
`b1391908`. Two harness constraints are load-bearing:

- do not export `RECURSION_NO_NOTIFY=1` around the whole suite; notification tests install their
  own safe stubs and a suite-level override makes their positive controls vacuous;
- do not run `work.selftest.sh` from detached HEAD; it writes the source branch into its freshness
  fixture, so detached HEAD correctly fails as an unprovable branch.

## Pre-fix red controls

All four commands below returned `1`, as intended. `UNEXPECTED_RED_CONTROL_RESULTS=0` in the
combined disposable-clone run. The live audit was read-only and re-asserted PAUSE afterward.

| Control | Red evidence on pre-change code |
|---|---|
| `scripts/tests/hermes-pilot-phase1-red.test.sh` | 8 failures: mid-run PAUSE reached the gate; worker read controller data, wrote outside, and called publisher; protected/ref-expression spellings were not failed closed |
| `scripts/tests/hermes-pilot-phase2-red.test.sh` | 6 failures: ten replays ran ten actions; crash recovery double-launched; planners collided; three invalid tasks ran; contended lock wrote unlocked; interrupted update exposed a partial file |
| `scripts/tests/hermes-pilot-phase3-red.test.sh` | 6 failures: gateway retained direct execution; authoring text was dropped; instruction closed without action; missing usage passed; authority inputs truncated; singleton decision lost pending work |
| `scripts/audit/hermes-pilot-live-phase1-red.sh` | 2 failures: gateway runs unrestricted as root; 2 of 104 installed dependencies had OSV advisories |

The live dependency findings were:

- `aiohttp==3.14.1`: `GHSA-cq5v-8q36-5273`, `GHSA-mfx4-hv73-q22v`,
  `GHSA-mq44-7p77-q5h7`, `PYSEC-2026-3545`, `PYSEC-2026-3546`, `PYSEC-2026-3547`;
- `cryptography==48.0.1`: `GHSA-g6cj-pr64-35w5`, `GHSA-jwv3-5hgf-82ww`,
  `GHSA-m2h6-j472-rp4c`, `PYSEC-2026-3552`, `PYSEC-2026-3553`, `PYSEC-2026-3554`.

Phase 1 must update them or record a reviewed reachability exception. Phase 0 makes no dependency
or service change.

## Authenticated default-branch protection inventory

The GitHub REST API was queried with the configured authenticated Git credential. Repository
metadata returned `200` for every registered repository and confirmed that the registry's branch
matches GitHub's default branch. The protection endpoint returned `403` for every row with:
`Upgrade to GitHub Pro or make this repository public to enable this feature.`

| Project | Registry status | Default | Protection result |
|---|---|---|---|
| recursion | active | `master` | `403`, feature unavailable |

These branches are not verifiably protected. No repository setting was changed. Publication must
therefore fail closed locally and remain review-branch-only; protection cannot be treated as a
backstop. Enabling a GitHub plan or changing repository visibility is Lucas's decision.

## Notification destination inventory

No token or message content was read. Stable IDs came from `.env` destination fields,
`state/portfolio.tsv`, and gateway session identities.

| Intended destination | Stable ID | Current state |
|---|---|---|
| Lucas direct account | chat/user `8913126684` | configured as `TELEGRAM_CHAT_ID`, but currently only a fallback because the control-plane home chat is configured |
| Head Hermes / home | chat `-1003894008732`, thread `1` | configured as the control-plane home thread; not yet a separately named durable outbox destination |
| recursion topic | chat `-1003894008732`, thread `3` | registered active |

"All accounts" therefore means project topic + control-plane home/Head Hermes + Lucas direct account,
with an extensible destination list. Current `notify.sh` selects one target; it does not fan one
event out to all three classes. Phase 7 owns that outbox behavior, but the stable IDs above are the
Phase 0 inventory.

## Rollback and backup/restore procedure

This is the reviewed procedure definition, not an authorization to execute it. Until Phase 2
ships atomic state and fail-closed locks, backups require a human-approved quiesced window.

### Backup

1. Assert `state/PAUSE` exists and copy it first into a root-owned `0700` snapshot directory
   outside every git checkout, named with UTC timestamp and deployed commit. Abort if PAUSE is
   absent.
2. Quiesce every writer for the maintenance window: gateway, actuator, planner, intake, cron-driven
   state writers, and workers. Record their prior running state; do not lift PAUSE.
3. Capture a manifest of commit OIDs, branches, worktrees, service unit, cron listing, package
   versions, and file owner/mode. Do not copy deploy keys or tokens into the review bundle.
4. Copy runtime state with metadata: `state/PAUSE`, `.board*`, `.intake-watermark`, instructions,
   assists, decisions, latest pointer, events, escalations, leases/locks metadata, future action and
   delivery queues, runtime baselines, and any checksummed seed manifests/corpora.
5. Snapshot the Hermes SQLite store with SQLite's online backup API, including a subsequent
   `PRAGMA integrity_check`; do not raw-copy a live WAL database.
6. Back up `.env` and gateway credentials separately in the operator's encrypted secret store.
   Record only hashes and presence in the runtime manifest.
7. Hash every snapshot file, fsync snapshot + manifest, then restart awareness-only components.
   Re-assert PAUSE and confirm no worker, gate, publication, or branch-changing process exists.

Git clones are restored from their remotes, not from copied working trees. Gitignored corpora are
restored only through their checksummed seed manifests; absence fails loudly.

### Restore

1. Assert PAUSE and quiesce writers. Restore into a new staging directory, never over live state.
2. Verify manifest hashes, ownership/modes, SQLite integrity, schema versions, event ordering, and
   that every queued/running object has a valid parent. Treat pre-snapshot `running` leases as stale;
   do not relaunch until reconciliation decides.
3. Move the current runtime directory to a timestamped quarantine path, then atomically rename the
   verified staging directory into place. Restore `state/PAUSE` before any service can start.
4. Start awareness-only services and reconcile instructions, actions, processes, worktrees, refs,
   and deliveries. Replays must produce zero duplicate actions or notifications.
5. Run the baseline and all controls. If any required control is red unexpectedly or any pending
   object is unaccounted for, roll back to quarantine and remain paused.
6. Resume execution only after Lucas explicitly approves it. For this project, that cannot happen
   before Phases 1-3 pass review.

### Code/service rollback

Keep PAUSE set. Revert the reviewed change through GitHub review rather than rewriting history;
deploy the resulting known-good commit to `loop/auto` through the normal human workflow. Restore a
previous systemd unit or dependency environment only from its versioned manifest, then run
`daemon-reload`, service hardening controls, baseline tests, and read-only status checks. Never use
a force push or make the dirty local `master` the rollback source.

## Phase 1 authoring evidence

### Publication gate and clean base

GitHub reported PR #21 merged into `master` as
`aadf7b301c9bd3e73b94c6cf5a47e6b5de893146`. Git ancestry checks proved both the distinct Phase 0
controls commit `2e98bc57a6a0ff1257f4d23166af36c10619f902` and the review-branch handoff tip
`22a6255363fbc9ad977b37d9f7183972265befd4` are ancestors of current `origin/master`. The merged
tree contains the Phase 0 controls. GitHub has no separately recorded review submission on the PR;
the merge by Lucas is the available review/approval event.

After fetching, Phase 1 was authored in the clean worktree
`C:\Users\drfoo\github\recursion-hermes-phase1` on `review/hermes-phase1-controls`, based exactly on
merged `origin/master` at `aadf7b3`. The dirty user-owned `master` remained at `20c1fa7` with its
files untouched. Its graph changed from ahead 5 / behind 115 to ahead 5 / behind 118 only because
the requested fetch advanced `origin/master`; it was not used as an authoring base.

### Reproduced starting evidence

The following commands ran in a disposable Linux clone of the merged base before Phase 1 changes:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
# rc=1; 8 intended failures

bash scripts/audit/hermes-pilot-live-phase1-red.sh
# rc=1; 2 intended failures

bash scripts/tests/hermes-pilot-phase2-red.test.sh
# rc=1; 6 intended failures

bash scripts/tests/hermes-pilot-phase3-red.test.sh
# rc=1; 6 intended failures
```

### Green after red

The Phase 1 implementation is commit
`0da2e1c8b0bea07849166aae58f80d9ed8191a49` (`Implement Hermes pilot Phase 1 controls`). A fresh
disposable Linux clone at that exact commit produced:

```text
bash scripts/tests/hermes-pilot-phase1-red.test.sh
# rc=0; 8/8 Phase 1 controls passed; hermes-pilot-phase1-red: ALL PASS

bash scripts/audit/hermes-pilot-phase1-green.sh
# rc=0; strict sandbox bounded work as uid 65534; canonical review-only publication passed;
# protected/default ref remained unchanged; aiohttp==3.14.3 OSV_CLEAR;
# cryptography==50.0.0 OSV_CLEAR; artifact and aggregate audits: ALL PASS

bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_COMMIT=0da2e1c8b0bea07849166aae58f80d9ed8191a49;
# BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0
```

The Phase 1 controls add interruptive PAUSE enforcement, a non-root OS sandbox boundary for model
workers, canonical review-ref-only publication, hardened gateway service artifacts, and clean
dependency pins. The proposed deployment and rollback sequence is documented in
`docs/plans/hermes-project-pilot-phase1-deployment.md`; it was not executed.

### Phase boundary and live PAUSE

The next-phase controls were rerun only to prove scope was not crossed:

```text
bash scripts/tests/hermes-pilot-phase2-red.test.sh
# rc=1; 6 intended failures remain

bash scripts/tests/hermes-pilot-phase3-red.test.sh
# rc=1; 6 intended failures remain
```

The undeployed live audit remains intentionally red:

```text
bash scripts/audit/hermes-pilot-live-phase1-red.sh
# rc=1; 2 intended live failures remain: the installed gateway is still unrestricted and its
# installed aiohttp==3.14.1 / cryptography==48.0.1 environment still reports advisories
```

This is the expected deployment gate, not a claim that live Phase 1 acceptance is complete. The
authoring controls are green, but publication, review, merge, and a separately authorized paused
deployment are required before the two live findings can turn green.

After all checks, `/opt/recursion/state/PAUSE` remained present with SHA-256
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; `/opt/recursion` remained
`loop/auto` at `58b912c46b0bbba8f420fdda685e05b958279daa`; and no `recursion-worker-*` transient unit was
left behind. No Phase 2 implementation, pilot, autonomous execution, PAUSE removal, deployment,
merge, push, or new PR occurred.

The next-session handoff and copy/paste Phase 2 prompt are in
`docs/plans/hermes-project-pilot-phase1-handoff.md`.

## Phase 2 authoring evidence

### Publication gate and clean base

GitHub reported PR #22 merged by `NotSkittlz` at `2026-08-07T05:53:42Z` with merge commit
`b25b3294dcbd320f92cd104718d795648fd57449`. The PR head was
`ee898ea0c9b95060de30dd86022d85a46d58b020`; GitHub records no separate submitted review, so
Lucas's merge is the available review/approval event. After fetching, Git proved the Phase 1
implementation commit `0da2e1c8b0bea07849166aae58f80d9ed8191a49` is an ancestor of current
`origin/master`, and the merged tree contains the Phase 1 controls and audit artifacts.

Phase 2 was authored in the clean worktree
`C:\Users\drfoo\github\recursion-hermes-phase2` on `review/hermes-phase2-controls`, based exactly
on merged `origin/master` at `b25b329`. The dirty user-owned `master` remained at `20c1fa7` with
all of its tracked and untracked files untouched. Its graph became ahead 5 / behind 123 only
because the required fetch advanced `origin/master`; it was never used as an authoring base.

### Reproduced starting evidence

Before changing code, these commands ran in a disposable Linux clone of merged base `b25b329`:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
# rc=0; 8/8 Phase 1 controls passed; hermes-pilot-phase1-red: ALL PASS

bash scripts/audit/hermes-pilot-phase1-green.sh
# rc=0; uid-65534 sandbox, canonical review-only publication, protected/default-ref,
# dependency, artifact, and aggregate audits: ALL PASS

bash scripts/tests/hermes-pilot-phase2-red.test.sh
# rc=1; 6 intended Phase 2 failures

bash scripts/tests/hermes-pilot-phase3-red.test.sh
# rc=1; 6 intended Phase 3 failures

bash scripts/audit/hermes-pilot-live-phase1-red.sh
# rc=1; 2 deployment-gated live findings: unrestricted root gateway service and advisories in
# installed aiohttp==3.14.1 / cryptography==48.0.1
```

The Phase 2 harness had one sequencing defect: control 5 restored `set -e`, so control 6's
intentional nonzero crashing writer exited the harness before its assertion. The harness now uses
`set +e` only around that intentional failure and immediately restores `set -e`. Copying only this
harness repair onto an otherwise untouched `b25b329` clone and rerunning
`bash scripts/tests/hermes-pilot-phase2-red.test.sh` still produced exactly the same six intended
failures (`rc=1`). The repair makes the sixth falsifier executable; it does not weaken a control.

### Green after red

The Phase 2 implementation is commit
`9a4778fa794158c32e5f1381fb7b24c930202542` (`Implement Hermes pilot Phase 2 controls`). It keeps
the existing file/event architecture and adds versioned schemas and validation; fail-closed locks
and atomic, durable state changes; validator-enforced goal/dependency IDs; an append-only,
idempotent action lifecycle with leases, attempts, crash recovery, and evidence; instruction
acknowledgement coupled to the exact succeeded action; and PAUSE-gated event rotation plus
manifest-hashed backup/restore.

A completely fresh Linux clone at that exact commit ran this final matrix:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
# rc=0; 8/8 Phase 1 controls passed; hermes-pilot-phase1-red: ALL PASS

bash scripts/audit/hermes-pilot-phase1-green.sh
# rc=0; strict sandbox ran as uid=65534; canonical review-only publication passed;
# aiohttp==3.14.3 OSV_CLEAR; cryptography==50.0.0 OSV_CLEAR; aggregate audit: ALL PASS

bash scripts/tests/hermes-pilot-phase2-red.test.sh
# rc=0; 6/6 Phase 2 controls passed; hermes-pilot-phase2-red: ALL PASS

bash scripts/audit/hermes-pilot-phase2-green.sh
# rc=0; all ten control-state schemas validated; instruction/action coupling passed;
# PAUSE-gated rotation and manifest-hashed backup/restore passed; aggregate audit: ALL PASS

bash scripts/tests/hermes-pilot-phase3-red.test.sh
# rc=1; exactly 6 intended Phase 3 failures remain

bash scripts/audit/hermes-pilot-live-phase1-red.sh
# rc=1; exactly the same 2 deployment-gated live findings remain
```

The six Phase 2 controls specifically proved that ten concurrent replays launch one idempotent
action, post-dispatch crash recovery does not relaunch it, simultaneous planners reserve distinct
tasks and roadmap items, task start enforces its parent goal and dependencies, lock contention
fails closed, and an interrupted update preserves the previous complete state file. The positive
audit separately proved schema coverage, exact action-coupled instruction acknowledgement, and
PAUSE-gated rotation/backup/restore.

### Phase boundary and final live PAUSE

Phase 3 remains deliberately unimplemented. Its six failures are direct Telegram execution,
dropped validated authoring text, acknowledgement without a succeeded action link, decisions
without provider/usage evidence, planner projection loss, and singleton decision-pointer loss.

The final read-only live snapshot at `2026-08-07T06:55:40+00:00` found
`/opt/recursion/state/PAUSE` present with unchanged SHA-256
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`. Production remained on
`loop/auto` at `987fcfe08793b9fb5a452edf288a4fe9fc367ea1`; zero `recursion-worker-*` units were running;
and both `recursion-hermes-gateway.service` and `recursion-hermes-supervisor.timer` were inactive.
The live Phase 1 audit still has exactly two deployment-gated findings: the installed gateway is
unrestricted/root, and the installed dependency environment reports advisories for
`aiohttp==3.14.1` and `cryptography==48.0.1`. These were neither waived nor changed on production.

No Phase 1 or Phase 2 deployment, Phase 3 implementation, pilot, autonomous execution, PAUSE
removal, merge, push, or Phase 2 PR occurred. The local review handoff is in
`docs/plans/hermes-project-pilot-phase2-handoff.md`.

## Phase 3 authoring evidence

### Publication gate and clean base

GitHub reported PR #23 merged by `NotSkittlz` at `2026-08-07T13:48:52Z` with merge commit
`6adb1ba5b297c92d47c3d5c561ef7267dd7f3e3a`. The PR head was
`b01d6f738650d2990ae08e2b49fca758c9314922`. GitHub records no separately submitted review, so
Lucas's merge is the available review/approval event. After fetching, Git proved the Phase 2
implementation commit `9a4778fa794158c32e5f1381fb7b24c930202542` is an ancestor of current
`origin/master`, and the merged tree contains the Phase 2 controls, positive audit, and handoff.

Phase 3 was authored in the clean worktree
`C:\Users\drfoo\github\recursion-hermes-phase3` on `review/hermes-phase3-controls`, based exactly
on merged `origin/master` at `6adb1ba`. The dirty user-owned `master` remained at
`20c1fa77042f3aa9f5d8978352792f3b058bc0d1`. Its tracked-diff hash remained
`2574b133121e0ea12eca06f5b8bd1d23449fc27a`; its tracked and untracked status list was unchanged.
It was never used as an authoring base.

### Reproduced starting evidence

Before changing code, these commands ran in a disposable named-branch Linux clone of merged base
`6adb1ba5b297c92d47c3d5c561ef7267dd7f3e3a`:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
# rc=0; 8/8 Phase 1 controls passed; hermes-pilot-phase1-red: ALL PASS

bash scripts/audit/hermes-pilot-phase1-green.sh
# rc=0; uid-65534 sandbox, canonical review-only publication, protected/default-ref,
# dependency, artifact, and aggregate audits: ALL PASS

bash scripts/tests/hermes-pilot-phase2-red.test.sh
# rc=0; 6/6 Phase 2 controls passed; hermes-pilot-phase2-red: ALL PASS

bash scripts/audit/hermes-pilot-phase2-green.sh
# rc=0; all ten schemas, exact instruction/action coupling, PAUSE-gated rotation, and
# manifest-hashed backup/restore passed; aggregate audit: ALL PASS

bash scripts/tests/hermes-pilot-phase3-red.test.sh
# rc=1; exactly 6 intended failures: direct gateway execution, dropped authoring text,
# handling without a succeeded action, absent provider/usage evidence accepted, truncated
# precedence inputs, and singleton decision-pointer loss
```

The pre-change read-only live audit ran separately:

```text
ssh gs-recursion 'cd /opt/recursion && bash scripts/audit/hermes-pilot-live-phase1-red.sh'
# rc=1; exactly 2 deployment-gated findings; PAUSE present before and after
```

At `2026-08-07T13:59:30+00:00`, production was `loop/auto` at
`b51bfff09fdc8f51cb8313ceb7267dbe0f701c69`; the PAUSE SHA-256 was
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; zero
`recursion-worker-*` units were present; and the gateway and supervisor timer were inactive. The
two findings were the unrestricted/root gateway unit and advisories in the installed
`aiohttp==3.14.1` / `cryptography==48.0.1` environment. They were treated as deployment gates and
were not changed.

### Green after red

The Phase 3 implementation is commit
`06a7422b6b51b9e2db6f7ca3e86b76da3cab2953` (`Implement Hermes pilot Phase 3 controls`). It keeps
the Phase 2 file/event and action-journal architecture while narrowing Telegram to inert intake,
recording complete instruction identity, requiring fresh provider/usage evidence, projecting full
precedence inputs, atomically queueing schema-v1 decisions without a singleton pointer, coupling
successful actions back to their exact instruction, and supervising one non-cron actuator behind a
shared planner/actuator lock.

The affected code first passed this focused exact-commit gate in a fresh Linux clone:

```text
bash -n scripts/conduct-hermes.sh scripts/hermes-decide.sh scripts/intake.sh \
  scripts/hermes-actuator.sh scripts/audit/hermes-pilot-phase3-green.sh \
  scripts/tests/hermes-pilot-phase3-red.test.sh
python3 -m py_compile scripts/decision-queue.py scripts/validate-decision.py \
  scripts/validate-control-state.py
bash scripts/tests/intake.test.sh
bash scripts/tests/hermes-conductor.test.sh
bash scripts/tests/hermes-pilot-phase2-red.test.sh
bash scripts/audit/hermes-pilot-phase2-green.sh
bash scripts/tests/hermes-pilot-phase3-red.test.sh
bash scripts/audit/hermes-pilot-phase3-green.sh
# every command rc=0; FOCUSED_MATRIX=PASS
```

A second completely fresh Linux clone at the exact implementation commit ran the final matrix:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_COMMIT=06a7422b6b51b9e2db6f7ca3e86b76da3cab2953;
# BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
# rc=0; 8/8 Phase 1 controls passed; hermes-pilot-phase1-red: ALL PASS

bash scripts/audit/hermes-pilot-phase1-green.sh
# rc=0; strict sandbox uid=65534; canonical review-only publication passed;
# aiohttp==3.14.3 OSV_CLEAR; cryptography==50.0.0 OSV_CLEAR; aggregate audit: ALL PASS

bash scripts/tests/hermes-pilot-phase2-red.test.sh
# rc=0; 6/6 Phase 2 controls passed; hermes-pilot-phase2-red: ALL PASS

bash scripts/audit/hermes-pilot-phase2-green.sh
# rc=0; schema/action-coupling/rotation/backup audit: ALL PASS

bash scripts/tests/hermes-pilot-phase3-red.test.sh
# rc=0; 6/6 Phase 3 controls passed; hermes-pilot-phase3-red: ALL PASS

bash scripts/audit/hermes-pilot-phase3-green.sh
# rc=0; authenticated inert intake, schema-v1 decision queue, complete projections,
# supervised PAUSE/restart behavior, exact successful-action coupling, hostile-output inertness,
# and non-colliding service/cron structure passed; hermes-pilot-phase3-green: ALL PASS
```

The six red-to-green controls prove that the Telegram gateway has no direct state-changing path,
validated authoring text reaches the sole actuator, an instruction cannot be closed without its
successful action, absent provider/usage evidence fails closed, precedence inputs are lossless,
and restart drains every pending decision once. The positive audit separately proves complete
authenticated metadata, atomic schema-v1 queueing, PAUSE-aware supervised execution, automatic
instruction/action coupling, restart idempotency, hostile-output inertness, and the absence of a
colliding actuator cron path.

### Final live PAUSE and phase boundary

The final read-only snapshot used:

```text
ssh gs-recursion 'cd /opt/recursion && bash scripts/audit/hermes-pilot-live-phase1-red.sh'
# rc=1; exactly 2 deployment-gated findings
sha256sum /opt/recursion/state/PAUSE
# 919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
```

At `2026-08-07T14:27:15+00:00`, `/opt/recursion/state/PAUSE` was present before and after the live
audit with that unchanged SHA-256. Production remained on `loop/auto` at
`b51bfff09fdc8f51cb8313ceb7267dbe0f701c69`; zero `recursion-worker-*` units were present; and
both `recursion-hermes-gateway.service` and `recursion-hermes-supervisor.timer` were inactive. The
live audit still had exactly the same two deployment-gated Phase 1 findings. They were neither
waived nor changed.

No Phase 1, Phase 2, or Phase 3 deployment, pilot, autonomous execution, PAUSE removal, merge,
push, or Phase 3 PR occurred during implementation and the original evidence snapshot. The local
review handoff was written to `docs/plans/hermes-project-pilot-phase3-handoff.md`.

### Review-branch publication follow-up

Lucas subsequently authorized committing and pushing the Phase 3 review branch so he can review
and merge it in GitHub. The handoff update was committed as
`fdc08a23e7feaebb845f7191ef26cad17d3337d6`, and
`review/hermes-phase3-controls` was pushed to the same OID. No PR was opened.

A post-push read-only production check at `2026-08-07T14:34:41Z` again found PAUSE present with
SHA-256 `919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`, production unchanged on
`loop/auto` at `b51bfff09fdc8f51cb8313ceb7267dbe0f701c69`, both Hermes gateway/supervisor units inactive, and
zero `recursion-worker-*` units. Branch publication did not deploy any phase or change production.

## Phase 4 authoring evidence

### Publication gate and clean base

The required fetch advanced `origin/master` to merge commit
`f120566a49a8d9a371e8b03d42d7e2aaf13dd7fc` (`Merge pull request #24 from
NotSkittlz/review/hermes-phase3-controls`). Git ancestry checks returned zero for both the Phase 3
implementation commit `06a7422b6b51b9e2db6f7ca3e86b76da3cab2953` and the published Phase 3
handoff commit `fdc08a23e7feaebb845f7191ef26cad17d3337d6`; the merged tree contained the
Phase 3 control, positive-audit, queue, actuator, and handoff artifacts. This satisfied the Phase 4
merge gate before any authoring began.

Phase 4 was authored in the clean worktree
`C:\Users\drfoo\github\recursion-hermes-phase4` on `review/hermes-phase4-controls`, based exactly
on that merged `origin/master`. The dirty user-owned `master` remained at
`20c1fa77042f3aa9f5d8978352792f3b058bc0d1`; its tracked-diff hash remained
`2574b133121e0ea12eca06f5b8bd1d23449fc27a`, and its tracked/untracked status was unchanged by the
fetch and worktree creation. It was never an authoring or test source.

### Reproduced starting evidence and Phase 4 red controls

The following commands ran in a disposable named-branch Linux clone of untouched merged base
`f120566a49a8d9a371e8b03d42d7e2aaf13dd7fc` before implementation:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
# rc=0; 8/8 Phase 1 controls passed; hermes-pilot-phase1-red: ALL PASS

bash scripts/audit/hermes-pilot-phase1-green.sh
# rc=0; uid-65534 sandbox, review-only publication, dependency, artifact, and aggregate audit green

bash scripts/tests/hermes-pilot-phase2-red.test.sh
# rc=0; 6/6 Phase 2 controls passed; hermes-pilot-phase2-red: ALL PASS

bash scripts/audit/hermes-pilot-phase2-green.sh
# rc=0; schema/action-coupling/rotation/backup audit green

bash scripts/tests/hermes-pilot-phase3-red.test.sh
# rc=0; 6/6 Phase 3 controls passed; hermes-pilot-phase3-red: ALL PASS

bash scripts/audit/hermes-pilot-phase3-green.sh
# rc=0; intake/decision/queue/coupling/supervision audit green
```

After copying only the newly authored Phase 4 harness onto that unchanged base, the required
falsifier command produced exactly four failures:

```text
bash scripts/tests/hermes-pilot-phase4-red.test.sh
# rc=1
# FAIL topic messages lack a durable, isolated project-pilot binding
# FAIL pilot scope/priority authority is not mechanically bounded
# FAIL direct status is not a bounded live-evidence read
# FAIL registration activated a project without every key/clone/freshness/gate/charter proof
# hermes-pilot-phase4-red: 4 FAILED
```

The activation control exercised the existing `author.sh register` behavior rather than failing
only because the new pilot helper was absent. It showed that a stale clone, missing gate, or
missing charter could become active after key presence; the current implementation changed the row
before freshness proof and merely printed that the already-active clone was stale. The corrected
control retains all four independent negative fixtures.

The pre-change live audit and PAUSE snapshot ran separately and read only production:

```text
ssh gs-recursion 'cd /opt/recursion && bash scripts/audit/hermes-pilot-live-phase1-red.sh'
# rc=1; exactly 2 deployment-gated findings; PAUSE present before and after
```

At `2026-08-07T15:34:38Z`, production was `loop/auto` at
`c6332c191501c47a9c9b82c50cb81f82f5678528`; PAUSE SHA-256 was
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; the gateway and
supervisor timer were inactive; and zero `recursion-worker-*` units existed. The two findings were
the unrestricted/root gateway and advisories in installed `aiohttp==3.14.1` and
`cryptography==48.0.1`. They remained deployment gates and were not changed.

### Green after red

The Phase 4 implementation is commit
`537749193abcd6e8a9ce0aa9c3a8da73c679b42d` (`Implement Hermes pilot Phase 4 controls`). It keeps
the canonical file/event architecture and existing sole actuator. It adds:

- schema-checked project and pilot companion records for durable topic, charter, model alias,
  concurrency/budget, notification, goal-source, memory-key, and worktree-namespace metadata;
- a one-to-one active topic/pilot registry and an isolated, bounded API for live git, charter,
  goals, tasks, runs, evidence, and project logs; tracked repo reads are charter-scoped and bounded,
  while runtime artifact reads are project-owned tail reads;
- topic intake fields for pilot identity, memory namespace, and routing target; project requests
  stay with their pilot, while portfolio/policy/cross-project/credential/production requests route
  to Head Hermes;
- actuator-gated project-pilot verbs for plan proposal, dependency-linked task creation under an
  approved goal's existing gate, worker/verifier requests, block/escalation, and goal-verification
  requests; Phase 4 creates proposed/request state and launches no worker;
- pending project preparation, explicit topic binding, and fail-closed activation only after a
  charter, fixed registration gate, complete pilot metadata, key-backed remote proof, correct clean
  clone/default branch, and exact local-versus-remote OID freshness proof;
- ignored and backup-covered runtime `pilot-requests` and `runs`, plus a gateway skill that treats
  topic memory as a cache and canonical files/git as truth.

Existing active projects received metadata records but were not reactivated or touched on the
live box. New activation is the proof-gated path. The configured Phase 4 worker concurrency is zero;
Phase 5 owns actual GPT-to-Claude worker hierarchy and Phase 6 owns autonomous reconciliation.

### Exact-commit final matrix

A completely fresh named-branch Linux clone from a local Git bundle at the exact implementation
commit ran:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_COMMIT=537749193abcd6e8a9ce0aa9c3a8da73c679b42d;
# BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
# rc=0; 8/8 Phase 1 controls passed
bash scripts/audit/hermes-pilot-phase1-green.sh
# rc=0; positive artifact/sandbox/publication/dependency audit green

bash scripts/tests/hermes-pilot-phase2-red.test.sh
# rc=0; 6/6 Phase 2 controls passed
bash scripts/audit/hermes-pilot-phase2-green.sh
# rc=0; positive schema/action-coupling/rotation/backup audit green

bash scripts/tests/hermes-pilot-phase3-red.test.sh
# rc=0; 6/6 Phase 3 controls passed
bash scripts/audit/hermes-pilot-phase3-green.sh
# rc=0; positive intake/decision/queue/coupling/supervision audit green

bash scripts/tests/hermes-pilot-phase4-red.test.sh
# rc=0; 4/4 Phase 4 controls passed; hermes-pilot-phase4-red: ALL PASS
bash scripts/audit/hermes-pilot-phase4-green.sh
# rc=0; metadata/topic binding, forwarding, charter/goal/dependency/gate/action coupling,
# bounded repo read, and positive clone/auth/freshness activation proof all green
```

The positive Phase 4 task fixture traveled through `conduct-hermes.sh`, created one durable action,
acknowledged its exact originating instruction only after success, and left the task `proposed`.
It did not run a model, worker, verifier, publisher, Telegram API, or production command.

### Final live PAUSE and Phase 4 boundary

The final read-only live snapshot used:

```text
ssh gs-recursion 'cd /opt/recursion && bash scripts/audit/hermes-pilot-live-phase1-red.sh'
# rc=1; exactly 2 deployment-gated findings
sha256sum /opt/recursion/state/PAUSE
# 919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
```

At `2026-08-07T16:11:42Z`, production remained `loop/auto` at
`c6332c191501c47a9c9b82c50cb81f82f5678528`. PAUSE was present before and after the audit with
the unchanged SHA-256; `recursion-hermes-gateway.service` and
`recursion-hermes-supervisor.timer` were inactive; and zero `recursion-worker-*` units existed.
The same unrestricted/root service and installed dependency advisories were the only two live
findings. They were neither waived nor changed.

No Phase 1, Phase 2, Phase 3, or Phase 4 deployment, live pilot, autonomous execution, PAUSE
removal, product-repository change, merge, push, or PR occurred. The Phase 4 branch remains local
for Lucas's review. Its handoff is `docs/plans/hermes-project-pilot-phase4-handoff.md`.

## Phase 4 review follow-up and Phase 5 authoring evidence

### Phase 4 merge gate and clean Phase 5 base

The required fetch advanced `origin/master` to
`2d631c4df36edec96d9d69013f8b8afedb28a79e`, authored by `NotSkittlz` as
`Merge pull request #25 from NotSkittlz/review/hermes-phase4-controls`. Its parents are merged
Phase 3 master `f120566a49a8d9a371e8b03d42d7e2aaf13dd7fc` and published Phase 4 handoff tip
`679b12d3b353f95b418dc1f5a45f79872416dc6f`. Both the Phase 4 implementation
`537749193abcd6e8a9ce0aa9c3a8da73c679b42d` and handoff tip returned zero from
`git merge-base --is-ancestor <oid> origin/master`. The merged tree contained the Phase 4 red
control, positive audit, project-pilot helper, schema/validator changes, topic skill, project/pilot
metadata, and registration artifacts. This independently satisfied the Phase 5 merge gate before
authoring began and supersedes the earlier local-only Phase 4 snapshot above.

Phase 5 was authored in the new clean worktree
`C:\Users\drfoo\github\recursion-hermes-phase5` on local branch
`review/hermes-phase5-controls`, based exactly on merged `origin/master` at `2d631c4`. The dirty
user-owned `master` remained at `20c1fa77042f3aa9f5d8978352792f3b058bc0d1`. Its tracked and
untracked status list remained the same as the start-of-session inventory; its behind count moved
only because the requested fetch advanced `origin/master`. It was never an authoring or test
source.

### Reproduced merged-base evidence

Before changing code, a disposable named-branch Linux clone at exact base `2d631c4` ran:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_COMMIT=2d631c4df36edec96d9d69013f8b8afedb28a79e;
# BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
bash scripts/audit/hermes-pilot-phase1-green.sh
# both rc=0; 8/8 controls and positive sandbox/publication/dependency audit green

bash scripts/tests/hermes-pilot-phase2-red.test.sh
bash scripts/audit/hermes-pilot-phase2-green.sh
# both rc=0; 6/6 controls and positive schema/action-coupling/rotation/backup audit green

bash scripts/tests/hermes-pilot-phase3-red.test.sh
bash scripts/audit/hermes-pilot-phase3-green.sh
# both rc=0; 6/6 controls and positive intake/decision/queue/coupling/supervision audit green

bash scripts/tests/hermes-pilot-phase4-red.test.sh
bash scripts/audit/hermes-pilot-phase4-green.sh
# both rc=0; 4/4 controls and positive pilot/topic/verb/registration audit green
```

The final Phase 5 falsifier was then copied by itself onto another untouched named-branch clone of
that same base and run without any implementation helper:

```text
bash scripts/tests/hermes-pilot-phase5-red.test.sh
# rc=1; exactly 9 failures:
# - a committed protected-file mutation was hidden and its gate ran;
# - a committed test/fixture/rename/delete/out-of-scope mutation set passed;
# - generated work passed without its result report;
# - a no-op passed a required-change task;
# - literal `true` passed as an autonomy gate;
# - a worker rewrote its own gate and passed;
# - a sleeping worker finished instead of timing out and had no timeout evidence/escalation;
# - no independent exact-commit verifier run/evidence existed;
# - verifier mutation was indistinguishable from success.
# hermes-pilot-phase5-red: 9 FAILED (expected on the pre-Phase 5 base)
```

This control is non-vacuous in both directions: the first six cases were `passed` on the old code,
the sleeping worker was also `passed`, and both verifier cases exposed the absence of an independent
review boundary. The green implementation requires the intended `escalated`, `blocked`, or `passed`
terminal state rather than counting any merely non-passed intermediate state as success.

### Pre-change live deployment gate

The separate read-only production snapshot at `2026-08-07T16:24:14Z` used:

```text
ssh gs-recursion 'cd /opt/recursion && bash scripts/audit/hermes-pilot-live-phase1-red.sh'
# rc=1; exactly 2 deployment-gated findings
sha256sum /opt/recursion/state/PAUSE
# 919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
```

Production was `loop/auto` at `c6332c191501c47a9c9b82c50cb81f82f5678528`. PAUSE was present
before and after the audit with that hash; the gateway was active for PAUSE-safe awareness, the
supervisor timer was inactive, and zero `recursion-worker-*` units existed. The two findings were
the undeployed unrestricted/root gateway service and advisories in installed `aiohttp==3.14.1` and
`cryptography==48.0.1`. They remained deployment gates and were not changed.

### Phase 5 implementation

The red control was first preserved as local commit
`c5e60eb53508cfe557673522eb4d4334f33b3c6f` (`Add Hermes pilot Phase 5 red controls`). The Phase 5
implementation is commit `719ca2453f8e1a85f684611b0cd0fe175524992b`
(`Implement Hermes pilot Phase 5 controls`). It keeps the file/event task store, existing durable
instruction/decision/action queue, sole deterministic actuator, proven work action, canonical
review branch, and publisher boundary. It adds:

- pilot-created strict task envelopes with exact identity/base, allowed paths, non-goals,
  acceptance/evidence/deadline/network/ambiguity contracts, required-change policy, and distinct
  implementer/verifier model aliases;
- versionless role aliases resolved only from human-owned environment mappings—no tracked concrete
  model version and no worker-write path to routing or budgets;
- atomic run records carrying PID/PGID, heartbeat/lease/deadline, logs, usage, result, complete
  changed-file evidence, machine-gate result, exact candidate OID, and verifier verdict;
- complete `base..HEAD`, staged, unstaged, untracked, rename/copy, and delete scope evidence with
  deny-first checks for tests, fixtures, control-plane files, gate files, and out-of-scope paths;
- non-vacuous gate validation and declared gate-file fingerprints; strict result-to-observed-diff
  agreement; rejection of missing result and no-op output for a required-change task;
- timeout termination with confirmed dead process group, lease clearing, blocked task state, and
  one run-keyed actionable escalation;
- candidate commit before the machine gate, a tracked-state immutability check after the gate, and
  a separate read-only verifier worktree at the exact same OID; verifier source mutation, result
  mismatch, wrong implementer identity, or wrong OID fails closed.

The worker hierarchy remains inside the existing `work` actuator action. Phase 4 pilot worker and
verifier verbs still create durable requests only; configured pilot concurrency remains zero.
There is no second executor and Phase 6 dispatch/reconciliation was not begun.

### Exact-commit green after red

A fresh named-branch Linux clone made from a Git bundle at exact implementation commit
`719ca2453f8e1a85f684611b0cd0fe175524992b` ran:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_COMMIT=719ca2453f8e1a85f684611b0cd0fe175524992b;
# BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
bash scripts/audit/hermes-pilot-phase1-green.sh
# both rc=0; 8/8 controls and Phase 1 positive audit green

bash scripts/tests/hermes-pilot-phase2-red.test.sh
bash scripts/audit/hermes-pilot-phase2-green.sh
# both rc=0; 6/6 controls and Phase 2 positive audit green

bash scripts/tests/hermes-pilot-phase3-red.test.sh
bash scripts/audit/hermes-pilot-phase3-green.sh
# both rc=0; 6/6 controls and Phase 3 positive audit green

bash scripts/tests/hermes-pilot-phase4-red.test.sh
bash scripts/audit/hermes-pilot-phase4-green.sh
# both rc=0; 4/4 controls and Phase 4 positive audit green

bash scripts/tests/hermes-pilot-phase5-red.test.sh
# rc=0; 9/9 controls passed; hermes-pilot-phase5-red: ALL PASS

bash scripts/audit/hermes-pilot-phase5-green.sh
# rc=0; shell/Python artifacts; human-owned aliases; non-vacuous/fingerprinted gates;
# complete scope-source evidence; task/run schema; read-only verifier/network artifacts;
# and aggregate Phase 5 controls all green; hermes-pilot-phase5-green: ALL PASS
```

The Phase 5 controls specifically proved that committed mutations cannot hide protected or
out-of-scope paths; missing result, required-change no-op, `true`, and self-modified gate cannot
pass; a hung worker is terminated with lease `0`, a dead recorded PGID, and exactly one correlated
timeout escalation; a valid implementer and verifier have distinct run IDs and PIDs bound to the
same machine-gated OID; and a mutating verifier is refused without changing the implementer branch.

### Final live PAUSE and Phase 5 boundary

The final read-only snapshot at `2026-08-07T17:01:08Z` used the same live audit and PAUSE hash
commands. Production remained `loop/auto` at
`c6332c191501c47a9c9b82c50cb81f82f5678528`; PAUSE was present before and after with SHA-256
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; the PAUSE-safe gateway was
active, the supervisor timer was inactive, and zero `recursion-worker-*` units existed. The live
audit returned `1` with exactly the same two deployment-gated Phase 1 findings. They were neither
waived nor changed.

No Phase 1, Phase 2, Phase 3, Phase 4, or Phase 5 deployment, live pilot, autonomous execution,
project-concurrency increase, PAUSE removal, product-repository change, merge, push, or PR occurred.
The local Phase 5 handoff is `docs/plans/hermes-project-pilot-phase5-handoff.md`. Phase 6 remains
unauthorized.

### Phase 5 review-branch publication follow-up

After Phase 5 implementation and evidence collection were complete, Lucas separately authorized
only publication of the review branch. `review/hermes-phase5-controls` was pushed to
`origin/review/hermes-phase5-controls`; the first published remote tip was
`c139ad99b5babf848274f6a3da4f65deb9be68b7`, exactly matching the then-current local tip. This
follow-up did not open a PR, merge, deploy, run a pilot, raise concurrency, remove PAUSE, or begin
Phase 6.

## Phase 6 — deterministic goal reconciliation

### Merge gate and isolated authoring base

Phase 6 authoring did not begin until the two Phase 5 review OIDs were proved merged:

```text
git fetch origin
git merge-base --is-ancestor 719ca2453f8e1a85f684611b0cd0fe175524992b origin/master
# rc=0
git merge-base --is-ancestor 529d15e6438035b4ed62369459307af8a642fde3 origin/master
# rc=0
git rev-parse origin/master
# 10bca6cfe1df81a4f03592a49868e70f89fd43f3
```

The already-pruned Phase 5 review branch was not required for the ancestry proof. A new clean
worktree `C:\Users\drfoo\github\recursion-hermes-phase6` and branch
`review/hermes-phase6-controls` were created from that merged `origin/master`. The dirty user-owned
`master` at `20c1fa77042f3aa9f5d8978352792f3b058bc0d1` was neither an authoring nor test source.

### Pre-change controls and live gate

On an untouched named Linux clone of merged base `10bca6c`, before implementation code changed:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
bash scripts/audit/hermes-pilot-phase1-green.sh
# both rc=0; 8/8 controls and Phase 1 positive audit green

bash scripts/tests/hermes-pilot-phase2-red.test.sh
bash scripts/audit/hermes-pilot-phase2-green.sh
# both rc=0; 6/6 controls and Phase 2 positive audit green

bash scripts/tests/hermes-pilot-phase3-red.test.sh
bash scripts/audit/hermes-pilot-phase3-green.sh
# both rc=0; 6/6 controls and Phase 3 positive audit green

bash scripts/tests/hermes-pilot-phase4-red.test.sh
bash scripts/audit/hermes-pilot-phase4-green.sh
# both rc=0; 4/4 controls and Phase 4 positive audit green

bash scripts/tests/hermes-pilot-phase5-red.test.sh
bash scripts/audit/hermes-pilot-phase5-green.sh
# both rc=0; 9/9 controls and Phase 5 positive audit green
```

The read-only live snapshot at `2026-08-07T17:15:18Z` found PAUSE present before and after with
SHA-256 `919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`, production
`loop/auto` at `c6332c191501c47a9c9b82c50cb81f82f5678528`, the PAUSE-safe gateway active, the
supervisor timer inactive, and zero `recursion-worker-*` units. The live Phase 1 audit returned `1`
with exactly the expected unrestricted/root gateway and installed dependency advisory findings.

### Phase 6 red control

The Phase 6 falsifier was authored and run against the untouched merged base before implementation,
then preserved as commit `50d354c758f002ef64db57dc78c549f178b1e00e`
(`Add Hermes pilot Phase 6 red controls`):

```text
bash scripts/tests/hermes-pilot-phase6-red.test.sh
# rc=1; hermes-pilot-phase6-red: 18 FAILED (expected on the pre-Phase 6 base)
```

The 18 failures covered exact-commit acceptance plus independent goal verification; bounded
machine-gate retry; ambiguity/assist deduplication; retry and failure-budget exhaustion; PAUSE;
crash/reboot recovery; stale base; publisher failure; verifier disagreement; concurrency, token,
cost, wall-clock, retry/failure and review-queue limits; deterministic terminals; task/process/
worktree/ref/event/delivery recovery; verifying task-creation closure with evidence-based reopen;
and zero dispatch after achievement.

### Phase 6 implementation

The core implementation is commit `b00d7debb1c3cabecd3c75d9fc41e19b94d59dff`
(`Implement Hermes pilot Phase 6 controls`). Final implementation OID
`b4b2924664793f4c7b60d507269e2bcdafd7d413` (`Fail closed on mutating Phase 6 goal gates`) adds
the explicit control and refusal for an exit-zero final acceptance command that dirties its detached
exact-commit worktree. Together they preserve the canonical file/event state, durable
decision/action queue, sole actuator, existing publisher, and Phase 5 `work` execution path. They
add one deterministic reconciler invoked inside the already-supervised actuator, not a second
scheduler or executor.

The reconciler observes canonical task/run/process/worktree/ref/event/delivery truth, emits
idempotent schema-v1 decisions, applies every task/goal change with compare-and-swap transitions and
append-only events, admits dependency-ready work under complete capacity and failure budgets,
reuses exact Phase 5 scope/gate/result/timeout/verifier evidence, proves review-branch publication
through the existing publisher, and independently verifies the exact final candidate before an
`achieved` transition. Terminal goals stay closed; `verifying -> active` requires failed verification
evidence; and a model's claim of completion has no state-transition authority.

### Exact-commit green after red

A fresh named-branch Linux clone made from a Git bundle at exact final implementation OID
`b4b2924664793f4c7b60d507269e2bcdafd7d413` ran:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_COMMIT=b4b2924664793f4c7b60d507269e2bcdafd7d413;
# BASELINE_BRANCH=phase6-b4b2924; BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
bash scripts/audit/hermes-pilot-phase1-green.sh
# both rc=0; 8/8 controls and Phase 1 positive audit green

bash scripts/tests/hermes-pilot-phase2-red.test.sh
bash scripts/audit/hermes-pilot-phase2-green.sh
# both rc=0; 6/6 controls and Phase 2 positive audit green

bash scripts/tests/hermes-pilot-phase3-red.test.sh
bash scripts/audit/hermes-pilot-phase3-green.sh
# both rc=0; 6/6 controls and Phase 3 positive audit green

bash scripts/tests/hermes-pilot-phase4-red.test.sh
bash scripts/audit/hermes-pilot-phase4-green.sh
# both rc=0; 4/4 controls and Phase 4 positive audit green

bash scripts/tests/hermes-pilot-phase5-red.test.sh
bash scripts/audit/hermes-pilot-phase5-green.sh
# both rc=0; 9/9 controls and Phase 5 positive audit green

bash scripts/tests/hermes-pilot-phase6-red.test.sh
# rc=0; 18/18 controls passed, including clean success plus dirty-final-gate refusal;
# hermes-pilot-phase6-red: ALL PASS

bash scripts/audit/hermes-pilot-phase6-green.sh
# rc=0; parser, sole-path, CAS/event, verifying-closure, budget, recovery-source,
# and aggregate 18-scenario audit green; hermes-pilot-phase6-green: ALL PASS
```

The Phase 6 cases prove success through Phase 5 exact-OID evidence and a distinct final goal gate;
refusal when that final gate dirties the exact detached worktree even if it exits zero; bounded gate
retry and publisher failure; one ambiguity assist; deterministic failure-budget and terminal
closure; PAUSE interruption; no duplicate recovery launch/delivery; stale-base refusal; verifier
disagreement refusal; every configured resource bound; review backpressure; evidence-only reopen;
and zero worker launch on repeated ticks after achievement.

### Final live PAUSE and Phase 6 boundary

The final read-only snapshot at `2026-08-07T18:46:33Z` ran the installed live Phase 1 audit between
two PAUSE checks. PAUSE was present both times with unchanged SHA-256
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`.

```text
ssh gs-recursion 'cd /opt/recursion && bash scripts/audit/hermes-pilot-live-phase1-red.sh'
# rc=1; exactly 2 deployment-gated findings
# hermes-gateway=active; recursion-hermes-gateway=inactive
# recursion-supervisor.timer=inactive; recursion-worker-* units=0
```

The final snapshot observed that `loop/auto` had independently advanced during this authoring
session from the pre-change `c6332c1` to
`9c70a860df2a2b68f4359cdbfeb9fdca1c877ad3` (`loop: board 2026-08-07 18:16Z`). The latter is a
descendant of the former. This Phase 6 work did not create, deploy, or invoke that commit and made no
production mutation. The audit still had exactly the two deployment-gated findings: the installed
gateway is unrestricted/root, and installed `aiohttp==3.14.1` plus `cryptography==48.0.1` have
known advisories.

No Phase 1-6 deployment, live pilot, autonomous production execution, project-concurrency increase,
human-visible test notification, PAUSE removal, product-repository change, merge, or PR was
performed. The Phase 6 handoff is `docs/plans/hermes-project-pilot-phase6-handoff.md`. Phase 7
remains unauthorized.

### Phase 6 review-branch publication follow-up

After Phase 6 implementation and evidence collection were complete, Lucas separately authorized
only publication of the review branch. `review/hermes-phase6-controls` was pushed to
`origin/review/hermes-phase6-controls`; the first published remote tip was
`f8996fe545a7faf59883b1b4d50be8d10a721f1d`, exactly matching the then-current local tip. This
follow-up did not open a PR, merge, deploy, run a pilot, raise concurrency, remove PAUSE, send a
human-visible test notification, or begin Phase 7.

## Phase 7 — reporting and notification fan-out

### Merge gate and isolated authoring base

Phase 7 authoring did not begin until both final Phase 6 review OIDs were proved merged:

```text
git fetch origin
git merge-base --is-ancestor b4b2924664793f4c7b60d507269e2bcdafd7d413 origin/master
# rc=0
git merge-base --is-ancestor e1dc1edad6b33e15f50c7a2dd795d274de87b90f origin/master
# rc=0
git rev-parse origin/master
# 44d9a2b1b01bf5bdd9a1723602e6e36070d0f7ac
```

The Phase 6 review branch was not needed for the ancestry decision and could have been pruned after
merge. A new clean worktree `C:\Users\drfoo\github\recursion-hermes-phase7` and local branch
`review/hermes-phase7-controls` were created from that exact merged `origin/master`. No previous
worktree or unmerged review branch was reused. The dirty user-owned `master` remained at
`20c1fa77042f3aa9f5d8978352792f3b058bc0d1` and was not an authoring or test source.

### Pre-change controls and live gate

Before implementation changed, an untouched named Linux clone of merged base `44d9a2b` ran:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_COMMIT=44d9a2b1b01bf5bdd9a1723602e6e36070d0f7ac;
# BASELINE_BRANCH=phase7-base; BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
bash scripts/audit/hermes-pilot-phase1-green.sh
# both rc=0; 8/8 controls and Phase 1 positive audit green

bash scripts/tests/hermes-pilot-phase2-red.test.sh
bash scripts/audit/hermes-pilot-phase2-green.sh
# both rc=0; 6/6 controls and Phase 2 positive audit green

bash scripts/tests/hermes-pilot-phase3-red.test.sh
bash scripts/audit/hermes-pilot-phase3-green.sh
# both rc=0; 6/6 controls and Phase 3 positive audit green

bash scripts/tests/hermes-pilot-phase4-red.test.sh
bash scripts/audit/hermes-pilot-phase4-green.sh
# both rc=0; 4/4 controls and Phase 4 positive audit green

bash scripts/tests/hermes-pilot-phase5-red.test.sh
bash scripts/audit/hermes-pilot-phase5-green.sh
# both rc=0; 9/9 controls and Phase 5 positive audit green

bash scripts/tests/hermes-pilot-phase6-red.test.sh
bash scripts/audit/hermes-pilot-phase6-green.sh
# both rc=0; 18/18 controls and Phase 6 positive audit green
```

The pre-change read-only live snapshot at `2026-08-07T19:01:51Z` found production `loop/auto` at
`9c70a860df2a2b68f4359cdbfeb9fdca1c877ad3`, PAUSE present before and after with SHA-256
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`,
`hermes-gateway=active`, `recursion-hermes-gateway=inactive`, the supervisor timer inactive, and
zero `recursion-worker-*` units. The live Phase 1 audit returned `1` with exactly two expected
deployment-gated findings: the installed gateway is unrestricted/root, and the installed
`aiohttp==3.14.1` plus `cryptography==48.0.1` have known advisories. Nothing was waived or changed.

### Phase 7 red controls

The Phase 7 falsifier was authored before implementation and overlaid by itself onto the untouched
merged base. It was then preserved separately as commit
`c7453771770bae171ae1b281a28000ae05c4e7bd` (`Add Hermes pilot Phase 7 red controls`):

```text
bash scripts/tests/hermes-pilot-phase7-red.test.sh
# rc=1; exactly 16 FAIL lines
# hermes-pilot-phase7-red: 16 FAILED (expected on the pre-Phase 7 base)
```

The 16 red failures covered retry-then-exactly-once delivery; no duplicates on repeated ticks;
independent retry state by destination; bounded backoff and retry exhaustion; durable crash/reboot
acknowledgement; dead-letter escalation; PAUSE; malformed/missing/unknown/unconfigured destination
refusal; project-topic isolation; Head Hermes/portfolio isolation; explicit-only Lucas DM routing;
exactly one Head Hermes portfolio result; complete terminal audit correlation; recovery across
events/outbox/acknowledgements/retries/dead letters/receipts/existing delivery records; refusal of
unsupported "sent" claims; and zero sends after complete acknowledgement.

During green bring-up the harness received only mechanical and strengthening repairs: unbuffered
progress output, a completely correlated rogue delivery, an explicit missing-pair recovery fixture,
durable actions in the terminal-chain assertion, stable rotated-event identity, a positive
Lucas-attention policy case, and a dead-letter direct-delivery assertion. No control was removed or
weakened.

### Phase 7 implementation

Final implementation OID `b4d3a30317e7d11f8e31b5e2578eb53aaeb5155c`
(`Implement Hermes pilot Phase 7 reporting`) keeps one canonical file/event architecture and the
existing durable decision/action/actuator path. The scheduler only observes canonical events,
notification state, delivery records, receipts, and reports and enqueues idempotent decisions.
Whitelisted actuator actions own every enqueue/retry/acknowledgement/dead-letter/terminal-report/
audit/recovery transition, and human-visible output still crosses only the existing `notify.sh`
sender.

The implementation adds strict configured destination policy; exact `(event_id, destination)`
identity; independent bounded retries; fsync-backed outbox, lifecycle history, transport receipts,
reports, audit chains, and recovery snapshots; transport-evidence-only acknowledgement; terminal
pilot-report acknowledgement before one Head Hermes portfolio result; and complete correlated
terminal evidence. It does not add a scheduler process, executor, state store, publisher, delivery
queue, notifier, worker launcher, or model-to-shell path. Tests use only the inert fixture transport.

### Exact-commit green after red

A fresh named-branch Linux clone made from a Git bundle at exact final implementation OID
`b4d3a30317e7d11f8e31b5e2578eb53aaeb5155c` retained separate command logs and ran:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
# rc=0; BASELINE_COMMIT=b4d3a30317e7d11f8e31b5e2578eb53aaeb5155c;
# BASELINE_BRANCH=review/hermes-phase7-controls;
# BASELINE_SUITES=17; BASELINE_FAILED_SUITES=0

bash scripts/tests/hermes-pilot-phase1-red.test.sh
bash scripts/audit/hermes-pilot-phase1-green.sh
# both rc=0; hermes-pilot-phase1-red/green: ALL PASS

bash scripts/tests/hermes-pilot-phase2-red.test.sh
bash scripts/audit/hermes-pilot-phase2-green.sh
# both rc=0; hermes-pilot-phase2-red/green: ALL PASS

bash scripts/tests/hermes-pilot-phase3-red.test.sh
bash scripts/audit/hermes-pilot-phase3-green.sh
# both rc=0; hermes-pilot-phase3-red/green: ALL PASS

bash scripts/tests/hermes-pilot-phase4-red.test.sh
bash scripts/audit/hermes-pilot-phase4-green.sh
# both rc=0; hermes-pilot-phase4-red/green: ALL PASS

bash scripts/tests/hermes-pilot-phase5-red.test.sh
bash scripts/audit/hermes-pilot-phase5-green.sh
# both rc=0; hermes-pilot-phase5-red/green: ALL PASS

bash scripts/tests/hermes-pilot-phase6-red.test.sh
bash scripts/audit/hermes-pilot-phase6-green.sh
# both rc=0; all 18 controls and Phase 6 positive audit green

bash scripts/tests/hermes-pilot-phase7-red.test.sh
# rc=0; all 16 controls passed; hermes-pilot-phase7-red: ALL PASS

bash scripts/audit/hermes-pilot-phase7-green.sh
# rc=0; parser/schema, sole-path, routing/identity, durability/recovery, retry/PAUSE,
# terminal-ordering/audit architecture checks and all 16 behavioral controls green;
# hermes-pilot-phase7-green: ALL PASS
```

The independently logged baseline/Phase 1-5, Phase 6, and Phase 7 command groups each ended
`MATRIX_RC=0`. The commands and result summaries above are the durable evidence; no `/tmp` path is
treated as canonical.

### Final live PAUSE and Phase 7 boundary

The final read-only snapshot at `2026-08-07T20:23:47Z` ran the installed live Phase 1 audit between
two PAUSE checks:

```text
ssh gs-recursion 'cd /opt/recursion && bash scripts/audit/hermes-pilot-live-phase1-red.sh'
# LIVE_AUDIT_RC=1; exactly 2 expected deployment-gated findings
# BRANCH=loop/auto
# OID=9c70a860df2a2b68f4359cdbfeb9fdca1c877ad3
# hermes-gateway=active; recursion-hermes-gateway=inactive
# recursion-supervisor.timer=inactive; recursion-worker-* units=0
```

PAUSE was present before and after with unchanged SHA-256
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`. Production remained on the
same `loop/auto` commit seen before Phase 7 implementation. The two findings remained the
unrestricted/root installed gateway and advisories in installed `aiohttp==3.14.1` and
`cryptography==48.0.1`. This work did not alter the service, dependencies, ref, PAUSE, timers,
workers, configuration, or delivery state.

No Phase 1-7 deployment, live pilot, autonomous production execution, project-concurrency increase,
human-visible test notification, PAUSE removal, product-repository change, PR, merge, or Phase 8
work occurred. The Phase 7 handoff is `docs/plans/hermes-project-pilot-phase7-handoff.md`. Every
further action requires separate Lucas authorization.

### Phase 7 review-branch publication follow-up

After Phase 7 implementation, exact-commit verification, evidence, and the Phase 8 session prompt
were complete, Lucas separately authorized committing and pushing the working review branch. The
initial published remote tip was `2c022d9116fde584dccead79552da569515cdb18`, exactly matching the
then-current local tip. Publication created `origin/review/hermes-phase7-controls`; it did not open
a PR, merge, deploy, run a pilot, raise concurrency, remove PAUSE, send a human-visible test
notification, mutate product repositories, or begin Phase 8.

## Phase 8 — observed-pilot admission and fresh plan synthesis

### Phase 7 merge gate and clean authoring source

Phase 8 began only after fetching `origin` and verifying the Phase 7 merge and artifacts:

```text
git rev-parse origin/master
# 7aa6e0f44ffd5145d15c38dfad893ed46389308b

git merge-base --is-ancestor b4d3a30317e7d11f8e31b5e2578eb53aaeb5155c origin/master
# rc=0

git log -1 --format=%H origin/master
# 7aa6e0f44ffd5145d15c38dfad893ed46389308b
# Merge pull request #28 from NotSkittlz/review/hermes-phase7-controls

test -f docs/plans/hermes-project-pilot-phase7-handoff.md
test -f docs/plans/hermes-project-pilot-phase0-evidence.md
git ls-tree -r --name-only origin/master -- scripts/tests/hermes-pilot-phase7-red.test.sh \
  scripts/audit/hermes-pilot-phase7-green.sh docs/plans/hermes-project-pilot-phase7-handoff.md
# all required paths present
```

Lucas's merge of PR #28 is the review/approval event. The clean Phase 8 worktree
`C:\Users\drfoo\github\recursion-hermes-phase8` and branch `review/hermes-phase8-pilot` were created
from that exact merged commit. The dirty user-owned `master` remained at
`20c1fa77042f3aa9f5d8978352792f3b058bc0d1`; its tracked-diff hash remained
`2574b133121e0ea12eca06f5b8bd1d23449fc27a`. No tracked or untracked user-owned file was changed.

### Aligned Phase 8 design boundary

Lucas approved the presented Phase 8 implementation plan and explicitly rejected using the old
plan shape for autonomy. The approved local-authoring scope was:

1. require a fresh schema-complete plan synthesized from a new Lucas instruction;
2. require a separate authenticated Lucas instruction naming the exact plan ID and SHA-256 before
   goal/task materialization;
3. require another exact-goal, bounded pilot-window instruction before scheduling;
4. route plan approval, window control, and assists through Head Hermes while retaining the one
   canonical decision/action/actuator path;
5. enforce global and per-project capacity through the existing durable queue and sole actuator;
6. preserve Phase 5 worker/verifier, Phase 6 reconciliation, Phase 7 outbox, and `notify.sh` as the
   only implementation, verification, reporting, and delivery paths; and
7. treat PAUSE observation as revocation of every open window.

The approval did not authorize publication, a PR, merge, deployment, production repair,
configuration/concurrency change, PAUSE removal, notification, backup/restore drill, or pilot.

### Phase 8 red controls

The Phase 8 falsifiers were committed separately before implementation as
`234d49b46197b7e120ca96b7efdfdc904ba87ec0` (`test: add phase 8 pilot falsifiers`). Overlaying only
those controls onto untouched merged base `7aa6e0f` ran:

```text
bash scripts/tests/hermes-pilot-phase8-red.test.sh
# rc=1; phase8: 0/6 controls passed
```

The six intended failures proved the base lacked: a fresh schema-complete plan that cannot reuse
legacy state; atomic rejection of malformed/vacuous plans; exact-digest approval and
idempotent materialization; exact-goal bounded admission; PAUSE window revocation; and one-time
same-task assist resumption through the durable notification path.

### Phase 8 implementation

Final implementation OID:
`7d0dc10fcb5914f98353be1f73deb15498e71b7c` (`feat: gate phase 8 project pilots`). It has red-control
commit `234d49b` as its parent and merged Phase 7 `7aa6e0f` as the red-control commit's parent.

The implementation changes 22 files, adding 1,504 lines and removing 70. It adds no second
scheduler, executor, publisher, store, sender, or worker launcher. It adds:

- immutable plan records and schema validation for complete Phase 5 task envelopes, Phase 6
  budgets, DAG integrity, scopes, non-goals, gates, review-only publication, and bounded text;
- Head-Hermes-only exact-digest approval and idempotent goal/task materialization;
- armed/open/closed exact-goal pilot windows with first-unpaused-observation activation and PAUSE
  revocation;
- logical GPT project-pilot briefs and a fail-closed required `RECURSION_PILOT_MODEL` alias;
- compare-and-swap same-task assist resumption and failed-action instruction release;
- filtered existing-queue claims for global capacity and one-worker-per-project capacity;
- final admission checks at scheduler, actuator, and worker boundaries;
- Phase 7 event/outbox mappings for plans, windows, and assists, with direct pilot sends suppressed;
  and
- runtime backup coverage for plan/window state.

The only tracked production-policy changes are `window_required: true` in the pilot records. Their
concurrency remains zero and no environment/configuration value was changed.

### Exact implementation Linux matrix

A Git bundle produced a disposable named-branch clone at exact OID `7d0dc10` on `gs-recursion`.
Before testing, the fixture proved:

```text
git rev-parse HEAD
# 7d0dc10fcb5914f98353be1f73deb15498e71b7c
git branch --show-current
# review/hermes-phase8-pilot
```

The fixture ran the following 17 top-level commands, with separate logs and correct pipeline return
codes:

```text
bash scripts/audit/hermes-pilot-phase0-baseline.sh
bash scripts/tests/hermes-pilot-phase1-red.test.sh
bash scripts/audit/hermes-pilot-phase1-green.sh
bash scripts/tests/hermes-pilot-phase2-red.test.sh
bash scripts/audit/hermes-pilot-phase2-green.sh
bash scripts/tests/hermes-pilot-phase3-red.test.sh
bash scripts/audit/hermes-pilot-phase3-green.sh
bash scripts/tests/hermes-pilot-phase4-red.test.sh
bash scripts/audit/hermes-pilot-phase4-green.sh
bash scripts/tests/hermes-pilot-phase5-red.test.sh
bash scripts/audit/hermes-pilot-phase5-green.sh
bash scripts/tests/hermes-pilot-phase6-red.test.sh
bash scripts/audit/hermes-pilot-phase6-green.sh
bash scripts/tests/hermes-pilot-phase7-red.test.sh
bash scripts/audit/hermes-pilot-phase7-green.sh
bash scripts/tests/hermes-pilot-phase8-red.test.sh && \
  python3 scripts/tests/hermes-pilot-phase8-parallel.py
bash scripts/audit/hermes-pilot-phase8-green.sh
```

Results:

```text
BASELINE_COMMIT=7d0dc10fcb5914f98353be1f73deb15498e71b7c
BASELINE_BRANCH=review/hermes-phase8-pilot
BASELINE_SUITES=17
BASELINE_FAILED_SUITES=0

# Phase 1 through Phase 7 control and positive-audit logs: RC=0 / ALL PASS
# Phase 8 behavioral control: 6/6 controls passed
# Linux parallel control: two projects overlap under global=2 while one project remains capacity=1
# Phase 8 positive audit: hermes-pilot-phase8-green: ALL PASS

MATRIX_TOTAL=17 MATRIX_FAILED=0
```

Every individual log ended `RC=0`; the matrix stderr file was empty. The copied local raw-log
directory is
`C:\Users\drfoo\github\.tmp\hermes-phase8-exact-7d0dc10fcb59\remote-logs`. Its SHA-256 manifest is:

```text
matrix-summary.log d311664e62163fb5bd7a4c6516590802c30c2b4b02619d4f2479314dc2881f7d
phase0-baseline.log 076845918e38ea33a860cde7b073dbaf65f6b9c749ad977e5cb00483edfd54d0
phase1-audit.log d949cd46da27b19b7986695bf6bfe3f117335277b32a362afd13b9cfb03a107b
phase1-control.log 5c6878acbab308603cb03bce920a04b0bca64273558c1c04636962d0ba027937
phase2-audit.log 935135f8a8c724946954bcd7725a0fd86fe18a0c42d78a0064c3106b45710b75
phase2-control.log c74a3d7433175177f8bac51c062edcfee0b9373f50d93b0d648ead9bad55d45c
phase3-audit.log 129ddfe72966bfae6edc23aeb68c0e49dc86549bff479a21badc938f887f35c7
phase3-control.log 9a9e599f5eafe494b4b1b21cdaf91742e984a3fbf3823eeaf7b0363b981642af
phase4-audit.log 056b904926960be33d18ce860163746ebb8a6d3389cdc2ea7d9d47b668c45e69
phase4-control.log 65b33383783d93b9573d23d9b19ae5ecd96d95023aa630fd807252752e237488
phase5-audit.log 2fffacb40497f8c9a0e15a02abc23a5055169251df105b2a6a077f7f3f603779
phase5-control.log 24f2f09312925fa1f126f2ab599e8261512f655beae487f754c518a6ee7601b9
phase6-audit.log b26da6df1081f725ed908afe761454ae951ec27b11a15ada00526554496d074d
phase6-control.log 3741b157258093c3e0bb909febb68c8091da6d479d5b239e1a0e04fd04c555eb
phase7-audit.log df02aa14273c8be21114b7a90d0324bb3a7fdbcc44871533b87de61ff496595f
phase7-control.log 2be909329ba5be0730fca2b5d643b236b8fe38b6bde3f93d86d5156f968d1e28
phase8-audit.log 398f0b4fc9bee8861c63ffcae669cd4b2995f6a26ea76b6655bedd5c9de65d96
phase8-control.log 105f3b54542bd2739fefe1c12f6d7fe3946ee77553422a91e1150039ed6e6fb2
```

After copying the logs, process and real-path checks found no active fixture process and resolved
only the exact disposable paths. Those two `/tmp/recursion-phase8-*7d0dc10fcb59` fixtures were then
removed and their absence verified. No production path was removed.

### Read-only production preflight

The live constitution was re-read in full; its SHA-256 was
`da43555b6977e55fc0bb185fd7d6d5e76384b638cbcaff4d798596cce68630d4`. The initial and final
read-only snapshots bracketed the installed Phase 1 audit:

```text
ssh gs-recursion 'cd /opt/recursion; test -f state/PAUSE; sha256sum state/PAUSE'
# rc=0
# 919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db  state/PAUSE

ssh gs-recursion 'cd /opt/recursion && bash scripts/audit/hermes-pilot-live-phase1-red.sh'
# LIVE_AUDIT_RC=1
# exactly 2 expected deployment-gated findings:
#   hermes-gateway unrestricted/root
#   installed aiohttp==3.14.1 and cryptography==48.0.1 have known advisories

ssh gs-recursion 'cd /opt/recursion; test -f state/PAUSE; sha256sum state/PAUSE'
# rc=0; identical SHA-256
```

At the final `2026-08-08T17:18:24Z` snapshot:

```text
BRANCH=loop/auto
OID=87189cd6e43ecc3d91f78881b82fb2186c7b2ebe
PHASE7_MERGE_ANCESTOR_RC=0
PHASE7_IMPLEMENTATION_ANCESTOR_RC=0
PAUSE=root:root,0644,189-bytes
PAUSE_SHA256=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
hermes-gateway=active
recursion-hermes-gateway=inactive
recursion-supervisor.timer=inactive
recursion-supervisor.service=inactive
recursion-worker-* units=0
active project-worktree processes=0
```

Production moved from the earlier Phase 7 snapshot because its PAUSE-safe awareness cadence wrote
board/morning commits after merging Phase 7. `git diff 7aa6e0f..87189cd` contains only board, brief,
event, escalation, inbox, and related awareness state. An untracked `.env.bak.1786014440` was
observed but not read or changed. The default recursion deploy key currently cannot authenticate
`git ls-remote origin`; that finding was not repaired or bypassed.

`python3 scripts/project-pilot.py verify-registry` returned `OK`. The pilot records were clean/fresh
at their remote heads. They remained active but configured with `concurrency_limit=0`,
`max_inflight_workers=0`, and publication false. The old goals/tasks remain in the registry but are
ineligible for Phase 8 plan reuse. The existing proposed offline native-diff tasks were the only
pending work; no test, fixture, oracle, baseline, corpus, or product repository was touched.

For every registered project, `notification-outbox.py status` reported `paused:true`, zero events,
and zero deliveries. Runtime decision/action/run/lease/delivery/outbox/report/dead-letter
directories were absent or empty; no pending/running action existed. `RECURSION_PILOT_MODEL`,
`RECURSION_DECIDE_MODEL`, and `RECURSION_PILOT_GLOBAL_CONCURRENCY` were all unset.

The runtime backup tool was present and help-loadable, with SHA-256
`463243151d675838a655757d903e77e93e564035384e35985e84dc41424407c4`; no execution locks existed
and the host had about 25.8 GB available. Exact fixture backup/restore, PAUSE preservation, manifest
hash verification, and crash recovery passed in the Phase 2/6/7/8 audits. No production snapshot or
restore drill has been authorized or demonstrated, so promotion remains blocked on that gate.

### Authenticated GitHub inventory

Authenticated GitHub REST reads returned HTTP 403 for the default-branch protection endpoint of
every registered repository. This matches the existing private-repository plan limitation; none is
verifiably protected and nothing was changed.

### Rollback, pilot windows, and final Phase 8 boundary

No production action or observed pilot window occurred, so there is no fabricated pilot/soak audit
chain or rollback execution to report. The exact rollback gates for deployment, success, assist,
offline, parallel, and soak fault sub-windows are recorded in
`docs/plans/hermes-project-pilot-phase8-handoff.md`. Each begins with the same mandatory boundary:
capture a manifest-hashed pre-action snapshot while PAUSE is asserted, and on any mismatch restore
the exact PAUSE bytes/hash, close the window, terminate and verify zero workers, preserve review
branches unmerged, prove zero unintended sends, and restore the verified pre-window runtime state
before considering a retry.

The final deployed OID observed by Phase 8 is
`87189cd6e43ecc3d91f78881b82fb2186c7b2ebe`; the final PAUSE SHA-256 is
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`. Production remains paused.
The two Phase 1 findings, unverified branch protection, undemonstrated production backup/restore,
unset model/capacity configuration, and unmerged Phase 8 implementation are hard gates.

### Phase 8 review-branch publication follow-up

After Phase 8 implementation, exact-commit verification, live read-only preflight, handoff, and
evidence were complete, Lucas separately authorized pushing the commits and review branch for his
review and merge. Immediately before publication:

```text
git fetch origin
# FETCH_RC=0
# origin/master=7aa6e0f44ffd5145d15c38dfad893ed46389308b
# origin/master is an ancestor of local HEAD

git ls-remote --heads origin refs/heads/review/hermes-phase8-pilot
# rc=0; branch absent

git push --set-upstream origin review/hermes-phase8-pilot
# rc=0; new remote branch created
# initial remote tip=a411e1cdcc999c9606f35879d0ad3a1d3d58f438
# exact local/remote OID match
```

Publication used a normal non-force push. The full GitHub CLI was unavailable, but it was not
needed for this explicitly push-only action; the configured authenticated Git transport succeeded.
No PR was opened. The documentation-only follow-up commit records the publication and is part of
the same authorized branch push.

No Phase 8 PR, merge, deployment, production flag/concurrency change, PAUSE removal, human-visible
notification, product-repository mutation, backup/restore drill, live pilot, soak, or branch prune
occurred. Those actions remain separate and, except for Lucas's own review/merge/prune workflow,
require the applicable explicit authorization.

### Post-merge production backup/restore drill

Lucas subsequently reported the Phase 8 branch merged and pruned. A fresh authenticated fetch on
2026-08-08 observed `origin/master` at merge commit
`9422b9cc5bf4270b7184b9af6fa31cae8e6a6876` (PR #29), with Phase 8 commits
`234d49b46197b7e120ca96b7efdfdc904ba87ec0`,
`7d0dc10fcb5914f98353be1f73deb15498e71b7c`,
`a411e1cdcc999c9606f35879d0ad3a1d3d58f438`, and
`61b907451230e5f49ad44e6760ddcf426102d90d` all ancestors. The remote review branch was absent and
all handoff/evidence artifacts were present.

Lucas then explicitly authorized one PAUSE-held production-derived backup/restore drill with this
boundary: stop and restart only `hermes-gateway`; create a root-owned mode-`0700` snapshot outside
`/opt/recursion`; restore and verify only in disposable staging; retain manifest/log evidence;
remove staging; do not deploy, lift PAUSE, notify, or change configuration.

Immediately before the drill, production was `loop/auto` at
`5a8b2f7464cb5f87bc09ea2e611cfa1453ca1c26`. PAUSE was present with SHA-256
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`, the gateway was active,
worker units and worktree processes were zero, and no execution lock was held. The exact snapshot
sequence was:

```bash
test -f /opt/recursion/state/PAUSE
test "$(sha256sum /opt/recursion/state/PAUSE | awk '{print $1}')" = \
  919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
install -d -o root -g root -m 0700 /var/backups/recursion
systemctl stop hermes-gateway.service
test "$(systemctl is-active hermes-gateway.service || true)" != active
test "$(systemctl show hermes-gateway.service -p MainPID --value)" = 0
! pgrep -f '[h]ermes_cli.main gateway run'
flock -x /opt/recursion/state/.locks/hermes-control.lock \
  python3 /opt/recursion/scripts/runtime-state.py backup \
  /var/backups/recursion/hermes-pre-phase8-20260808T224858Z-5a8b2f7464cb
systemctl start hermes-gateway.service
test "$(systemctl is-active hermes-gateway.service)" = active
```

The retained snapshot is:

```text
/var/backups/recursion/hermes-pre-phase8-20260808T224858Z-5a8b2f7464cb
owner/mode=root:root 0700
source_commit=5a8b2f7464cb5f87bc09ea2e611cfa1453ca1c26
manifest_files=4
manifest_pause_sha256=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
```

It contains only the manifest-selected runtime files: `state/.intake-watermark`, `state/PAUSE`,
`state/escalations.tsv`, and `state/events.jsonl`. The pre-existing untracked production
`.env.bak.1786014440` was not read, copied, or changed. The exact disposable restore sequence was:

```bash
git clone --no-hardlinks --branch loop/auto /opt/recursion \
  /var/tmp/recursion-restore-drill-20260808T224858Z-5a8b2f7464cb
chmod 0700 /var/tmp/recursion-restore-drill-20260808T224858Z-5a8b2f7464cb
git -C /var/tmp/recursion-restore-drill-20260808T224858Z-5a8b2f7464cb \
  checkout -b restore-drill-20260808 5a8b2f7464cb5f87bc09ea2e611cfa1453ca1c26
install -D -o root -g root -m 0644 \
  /var/backups/recursion/hermes-pre-phase8-20260808T224858Z-5a8b2f7464cb/files/state/PAUSE \
  /var/tmp/recursion-restore-drill-20260808T224858Z-5a8b2f7464cb/state/PAUSE
RECURSION_ROOT=/var/tmp/recursion-restore-drill-20260808T224858Z-5a8b2f7464cb \
  python3 /var/tmp/recursion-restore-drill-20260808T224858Z-5a8b2f7464cb/scripts/runtime-state.py \
  restore /var/backups/recursion/hermes-pre-phase8-20260808T224858Z-5a8b2f7464cb
```

A byte/metadata verifier checked every restored path against `manifest.json`, including SHA-256,
mode, UID, and GID. Its compact result was:

```json
{"errors":[],"files_verified":4,"pause_sha256":"919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db","restored_commit":"5a8b2f7464cb5f87bc09ea2e611cfa1453ca1c26","schema_version":1,"snapshot_commit":"5a8b2f7464cb5f87bc09ea2e611cfa1453ca1c26"}
```

The staging-only verification matrix then ran:

```bash
RECURSION_ROOT="$STAGE" python3 "$STAGE/scripts/project-pilot.py" verify-registry
RECURSION_ROOT="$STAGE" RECURSION_NO_NOTIFY=1 python3 "$STAGE/scripts/project-reconcile.py" schedule
RECURSION_ROOT="$STAGE" RECURSION_NO_NOTIFY=1 python3 "$STAGE/scripts/notification-outbox.py" schedule
bash "$STAGE/scripts/audit/hermes-pilot-phase0-baseline.sh"
bash "$STAGE/scripts/audit/hermes-pilot-phase2-green.sh"
bash "$STAGE/scripts/audit/hermes-pilot-phase6-green.sh"
bash "$STAGE/scripts/audit/hermes-pilot-phase7-green.sh"
```

Registry verification passed. Two repeated PAUSE-held recovery cycles left runtime-file count at
zero with zero duplicates. The Phase 0 baseline passed all 17 suites; Phase 2 backup/restore,
Phase 6 crash recovery, and Phase 7 delivery recovery audits all ended `ALL PASS`. No product
repository, test, fixture, oracle, corpus, or baseline was involved or changed.

Three negative observations were preserved rather than hidden. First, the initially stopped
gateway reported systemd state `failed`, not `inactive`; the attempt failed closed and its exit trap
restarted the gateway. The accepted rerun required the stronger invariant: state not active,
`MainPID=0`, and no gateway process. Second, a fresh staging clone lacked the runtime-only PAUSE
file, so the first restore attempt failed before mutation; the retry bootstrapped staging with the
exact snapshot PAUSE bytes before invoking the canonical restore. Third, one Windows-to-SSH
wrapper ended `127` after its success output due a trailing carriage return, and the long verifier
wrapper timed out only after all terminal PASS markers. Independent manifest, log-tail, process,
and live-state verification established the actual results; neither wrapper anomaly was treated as
positive evidence by itself.

The exact staging path was realpath-checked, confirmed to have no active process, and removed:

```text
/var/tmp/recursion-restore-drill-20260808T224858Z-5a8b2f7464cb absent
```

The snapshot's retained `drill-evidence/` directory is `root:root` mode `0700`; every evidence file
is owner-only. `evidence-files.sha256` verifies all 14 retained logs/manifests and has SHA-256
`df9a894f843842ab8bec161a79e29a5d16feadc8fa26f93247ffc7e685acde36`.

The final production observation at `2026-08-08T23:09:00Z` recorded:

```text
DEPLOYED_BRANCH=loop/auto
DEPLOYED_OID=5a8b2f7464cb5f87bc09ea2e611cfa1453ca1c26
HERMES_GATEWAY=active
SUPERVISOR_SERVICE=inactive
SUPERVISOR_TIMER=inactive
RUNNING_WORKER_UNITS=0
RUNNING_WORKER_PROCESSES=0
all 9 notification outboxes: paused=true, events=0, deliveries={}
live Phase 1 audit: rc=1, exactly 2 known deployment gates
PAUSE_BEFORE_SHA256=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
PAUSE_AFTER_SHA256=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
```

This closes the production backup/restore demonstration gate only. It does not authorize or prove
a Phase 8 deployment or pilot. The unrestricted root gateway and vulnerable installed dependency
versions remain hard deployment gates; neither was silently repaired. Private-repository branch
protection also remains unverified. An authorized attempt to upgrade the personal GitHub account
to Pro could not begin because no signed-in controllable browser was available; no personal or
organization billing setting was changed.

### Personal GitHub Pro and default-branch protection

Lucas subsequently upgraded the personal `NotSkittlz` account to GitHub Pro. The authenticated
personal licensing page exposed `Switch to yearly billing` and `Downgrade to Free`, establishing
that the personal account is on the monthly Pro plan. This is separate from the paid Team plan that
was observed on a different organization. No organization billing, subscription, budget,
or alert control was changed by this work.

GitHub Pro removed the prior private-repository HTTP 403 gate. Lucas then explicitly authorized the
same protection policy for every initialized personal repository. The empty repository remains
deferred until it has an initial `main` branch. The following classic branch-protection rules were
created and then re-opened by their persisted rule IDs at `2026-08-08T23:51:43Z`:

| Repository | Branch | Rule ID |
|---|---|---:|
| `NotSkittlz/recursion` | `master` | `81491567` |

Every persisted rule independently returned the same effective state:

```text
has_required_reviews=true                 # a pull request is mandatory
has_required_reviews_reviews=false        # zero approvals; avoids one-user deadlock
requires_conversation_resolution=true
enforce_all_for_admins=true               # owner/admin cannot bypass
has_required_statuses=false               # no unproven CI context was invented
allows_force_pushes=false
allows_deletions=false
has_signature_requirement=false
block_merge_commits=false                 # preserves the proven PR merge-commit workflow
has_required_deployments=false
lock_branch=false
```

The form's inactive nested `strict_required_status_checks_policy` value may display true after
reload, but its parent `has_required_statuses` is false in every rule, so no status check is active.
Required checks remain a later per-repository change only after a stable green CI context is
identified and separately reviewed.

The first `recursion` submission reached GitHub's sudo-mode `Confirm access` checkpoint. No rule
was created until Lucas completed that authentication; the returned branches page then reported
`Branch protection rule created.` and the rule was re-opened for field-level verification. During
the remaining bounded rollout, transient Chrome/CDP readback delays occurred. They were not treated
as evidence: each affected repository was inspected at its persisted edit URL. One unsaved
repository form temporarily contained `mainmain` after a retry; pre-submit state inspection
caught and corrected it to exact `main` before creation. The final eight-rule audit returned seven
immediate matches and one transient extension read timeout; a separate retry of exact rule
`81491615` matched every field. Final result: `8/8` initialized personal default branches enforce
review-branch-only delivery with no force-push, deletion, or administrator bypass path.

This closes the private-repository branch-protection gate for initialized projects. It does not
close the two live Phase 1 deployment gates, deploy Phase 8, change production configuration, lift
PAUSE, send a notification, or authorize a pilot window.

## Phase 1 remediation attempt: dependency metadata blocker

Lucas explicitly authorized completion of the two Phase 1 production remediations. A new
PAUSE-held preflight at deployed awareness OID
`7d682044656184815b3883d913dd3d0c50c915d1` and reviewed `origin/master`
`9422b9cc5bf4270b7184b9af6fa31cae8e6a6876` reproduced the live audit's exact two findings:
the gateway still ran unrestricted as root, and the 104-package environment contained the known
`aiohttp==3.14.1` and `cryptography==48.0.1` advisories. The Phase 0 baseline then passed all
17/17 suites. Every Phase 1-7 control suite and every Phase 1-7 positive audit ended `ALL PASS`.
The final preflight PAUSE hash remained
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; all four pilot supervisors
were inactive and the running worker-unit count was zero.

The preflight also established that the earlier apparent deployment-artifact mismatch was only a
Windows working-tree line-ending comparison. On the Linux host, each working file was byte-equal
to its exact `origin/master` Git object and `git diff origin/master -- <four paths>` was empty:

```text
deploy/systemd/hermes-gateway.service                 8524c6239f1001e579bbc0c67b098a60b9fc0dc6fda8dbac57f35ab823c9cf85
deploy/requirements/hermes-gateway-phase1.txt        be857a5c5460c41bc4094391ec41adaa4e380ccaf74b465c27c9ad178d0aab49
deploy/sysusers.d/recursion.conf                     44833b12fd4627280a9fb6c0d5c0bd32082964ed132267e36cc68e8a05fd4817
deploy/tmpfiles.d/recursion.conf                     4c95c0f6784854e500611958b4be2837d399cffd1a5b1db29a2375644dd57ab5
```

The authorized transaction extracted those reviewed objects into a root-owned bundle, copied the
live venv for rollback, cloned the live venv into a separate staged path, and applied the reviewed
requirements. The exact dependency transition was:

```text
aiohttp 3.14.1 -> 3.14.3
cryptography 48.0.1 -> 50.0.0
```

The mandatory full-environment compatibility gate then refused the stage:

```text
Found 1 incompatibility
The package `hermes-agent` requires `cryptography==48.0.1`, but `50.0.0` is installed
```

This is a new artifact-compatibility blocker. The installed Hermes Agent is editable version
`0.20.0` at source OID `36cb5ae5530a75def7df3195e49b7a4aa2add482`; that source and its existing
`origin/main` both pin `cryptography==48.0.1`. PyPI reported the latest published Hermes Agent as
`0.19.0`, which pins `cryptography==46.0.7`. OSV identifies `50.0.0` as the first version fixing
`GHSA-g6cj-pr64-35w5`, so neither the published artifact nor the installed source can currently be
both OSV-clear and `pip check`-clean. The transaction did not edit distribution metadata, ignore
`pip check`, use `--no-deps`, or otherwise waive the contradiction.

The failure occurred before `MUTATION_STARTED=1` and before `systemctl stop`. Independent final
verification proved that `hermes-gateway` retained the same PID `2063708`, root identity, original
unit SHA-256 `6135f46663105dc0149adf3da2659a120fb6f5a47d79871effc1695a90ee891b`, original
venv versions, and `/root/.hermes` working directory. The two service accounts and both `/var/lib`
state directories remain absent. No unit, venv switch, service stop/restart, notification, worker,
publisher, deployment, flag, concurrency, PAUSE, or product-repository mutation occurred. A final
live audit again reproduced exactly the same two findings.

The failed staged venv was moved out of the Hermes source tree and retained for inspection at:

```text
/var/backups/recursion/phase1-remediation-20260809T010739Z-7d682044/failed-stage-cryptography50-pip-check
```

The root-owned mode-`0700` evidence/rollback bundle is retained at
`/var/backups/recursion/phase1-remediation-20260809T010739Z-7d682044`; its logs, reviewed objects,
rollback venv, blocker report, and `evidence/MANIFEST.sha256` are owner-only. The full preflight and
transaction commands are retained as `evidence/phase1-control-preflight.sh` and
`evidence/phase1-remediate.sh`. The next admissible attempt requires a separately reviewed Hermes
Agent artifact whose dependency metadata supports `cryptography==50.0.0`, followed by this same
staged `pip check`, import, complete OSV, and rollback gate. Phase 8 deployment and every pilot
window remain blocked; PAUSE remains asserted.

## Hermes Agent Phase 1 compatibility fork (review-only)

Lucas authorized a separate reviewable Hermes Agent compatibility fork after the production
transaction above failed closed. GitHub created and independently re-read the public personal fork
`NotSkittlz/hermes-agent`; the connected account has admin/push permission on that fork. A clean
local checkout at `C:\Users\drfoo\github\hermes-agent-compat` added upstream
`NousResearch/hermes-agent`, verified deployed source OID
`36cb5ae5530a75def7df3195e49b7a4aa2add482` as a commit and as an ancestor of current upstream
`main` `b79e83827d1e275f3b5f91594e9f7136d7293e5a`, then created local review branch
`review/phase1-cryptography50` directly from the deployed OID. The fork branch was not pushed and
no pull request was opened.

The review patch changes only `pyproject.toml`, `tools/lazy_deps.py`, and `uv.lock`. It declares
`cryptography==50.0.0`, advances every declared and lazy `aiohttp` pin to `3.14.3`, and regenerates
the lock with the repository's CI-pinned `uv 0.9.28`. Because the two security releases were still
inside Hermes's 14-day package quarantine, the manifest includes package-specific temporary
exceptions with removal dates: cryptography after 2026-08-14 and h2 after 2026-08-17. Complete-lock
OSV inspection found the pre-existing `h2==4.3.0` request-smuggling advisory
`GHSA-6hr6-w5qg-qmwg`; the patch therefore also resolves `h2==4.4.1` and `hpack==4.2.0` rather than
waiving the complete-audit gate.

Exact dependency commands and primary results were:

```text
C:\Users\drfoo\github\.hermes-compat-tools\Scripts\uv.exe --version
# uv 0.9.28 (0e1351e40 2026-01-29)

C:\Users\drfoo\github\.hermes-compat-tools\Scripts\uv.exe lock
C:\Users\drfoo\github\.hermes-compat-tools\Scripts\uv.exe lock --upgrade-package h2
C:\Users\drfoo\github\.hermes-compat-tools\Scripts\uv.exe lock --check
# Resolved 249 packages; lock check rc=0
# aiohttp 3.14.1 -> 3.14.3
# cryptography 48.0.1 -> 50.0.0
# msal 1.36.0 -> 1.37.0
# alibabacloud-tea-openapi 0.4.5 -> 0.3.16
# h2 4.3.0 -> 4.4.1; hpack 4.1.0 -> 4.2.0
```

The Alibaba downgrade is the resolver's compatible path, not an ignored conflict:
`alibabacloud-dingtalk==2.2.42` declares `alibabacloud-tea-openapi>=0.3.16,<1.0.0`; release `0.4.5`
caps cryptography below 49 on Python 3.9+, while `0.3.16` is inside the DingTalk SDK's supported
range. `msal==1.37.0` declares `cryptography>=2.5,<51`. `hindsight-client==0.6.1` has no
cryptography dependency and accepts `aiohttp>=3.8.4`.

The universal all-extras graph resolved successfully. A literal `uv sync --all-extras --python
3.11` installation on Windows did not complete because Matrix's `python-olm==3.2.16` source build
requires Unix `make` or `gmake`; this was retained as a platform limitation, not represented as a
pass. A new external production-shaped environment then installed messaging, WeCom, DingTalk,
Azure identity, Hindsight, Teams, and dev extras under Python 3.11.15. It resolved 126 installed
packages and passed:

```text
C:\Users\drfoo\github\.hermes-compat-tools\Scripts\uv.exe pip check \
  --python C:\Users\drfoo\github\.hermes-compat-gateway\Scripts\python.exe
# All installed packages are compatible

# Installed editable metadata:
# hermes-agent==0.20.0
# cryptography==50.0.0
# aiohttp==3.14.3
# msal==1.37.0
# alibabacloud-tea-openapi==0.3.16
# alibabacloud-dingtalk==2.2.42
# hindsight-client==0.6.1
# Requires-Dist: cryptography==50.0.0
# every aiohttp Requires-Dist pin: ==3.14.3
```

Real imports of the gateway, Weixin, WeCom, DingTalk, Azure identity, MSAL, Hindsight, aiohttp, and
cryptography passed. An RSA-2048 PyJWT RS256 encode/decode round trip and the canonical Weixin
AES-128 encrypt/decrypt round trip passed. The OSV batch API then audited both the complete lock
and the installed production-shaped environment:

```text
LOCK_OSV_QUERIED=249
LOCK_OSV_FINDINGS=0
INSTALLED_OSV_QUERIED=126
INSTALLED_OSV_FINDINGS=0
COMPLETE_OSV_CLEAR
```

The canonical Hermes test wrapper ran 13 dependency/gateway/crypto files after the final lock:

```text
C:\Program Files\Git\bin\bash.exe scripts/run_tests.sh \
  tests/test_project_metadata.py \
  tests/gateway/test_dingtalk.py tests/hermes_cli/test_dingtalk_auth.py \
  tests/gateway/test_wecom.py tests/gateway/test_wecom_callback.py \
  tests/gateway/test_wecom_plugin_setup.py tests/gateway/test_weixin.py \
  tests/gateway/test_weixin_secret_scope.py tests/gateway/test_weixin_typing.py \
  tests/agent/test_azure_identity_adapter.py tests/hermes_cli/test_azure_detect.py \
  tests/hermes_cli/test_azure_foundry_entra.py tests/gateway/relay/test_relay_sheds_crypto.py -q
# 13 files; 144 tests passed; 0 failed
```

A separate full-suite attempt reached its 1,804-second command ceiling without returning the
runner's buffered summary. It is recorded as a timeout, not a pass and not a test failure. The
exact local test process tree and one re-parented local test gateway were terminated; final
verification returned `LOCAL_TEST_PROCESSES=0`. A literal `%SystemDrive%` directory created by the
Windows tests was moved, recoverably, out of the checkout to
`C:\Users\drfoo\github\.hermes-compat-test-artifacts\systemdrive-20260808`. Hermes intentionally
refused a wheel build because the project supports installer, Docker/Nix, or editable-source
artifacts; production already uses an editable source install, so the reviewed source commit is the
applicable artifact.

No connection to `gs-recursion`, production mutation, service action, deployment, notification,
worker, publisher, flag, concurrency, or PAUSE action occurred during the fork work. The last
observed production PAUSE evidence therefore remains
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; it was not re-observed or
changed in this local-only window. Phase 1 production remediation still requires Lucas to review
and publish the separate Hermes source patch, followed by a new PAUSE-held production preflight
and separately authorized transactional deployment. Phase 8 deployment and pilot windows remain
blocked.

## Hermes compatibility publication and PAUSE-held deployment attempts

Lucas subsequently authorized publication of the separate Hermes compatibility patch, reviewed
the branch, and explicitly authorized deployment and testing. Local and remote branch
`review/phase1-cryptography50` both resolved to exact commit
`498061d8f1dcd855419b30e8729c869596a89ca6`, based directly on deployed Hermes source
`36cb5ae5530a75def7df3195e49b7a4aa2add482`. Only `pyproject.toml`, `tools/lazy_deps.py`, and
`uv.lock` differ. The branch remains published for review; it was not merged or pruned during the
production attempts.

Three independent, root-owned mode-`0700` attempt bundles preserve the exact scripts, inputs,
outputs, rollback material, and failure summaries:

| Attempt | Bundle | Last completed gate | Result |
|---|---|---|---|
| 1 | `/var/backups/recursion/phase1-remediation-20260809T052659Z-498061d8` | fresh 17/17 and Phase 1-7 preflight; staged dependency checks | failed before mutation because a metadata verifier selected editable source `hermes_agent.egg-info`, which has no `direct_url.json` |
| 2 | `/var/backups/recursion/phase1-remediation-20260809T060745Z-498061d8` | fresh full preflight; explicit dist-info/source verifier; 104-package `pip check`; imports; RS256; complete OSV | stopped the gateway, then failed because `/etc/sysusers.d` did not exist; automatic rollback restored the original runtime |
| 3 | `/var/backups/recursion/phase1-remediation-20260809T141605Z-498061d8` | fresh full preflight; exact staged artifact; hardened live readiness; live Phase 1 audit; systemd security; 17/17 baseline; Phase 1 red/artifact controls | Phase 1 positive audit found the copied sandbox implementer inaccessible under the hardened `0077` umask; automatic rollback restored the original runtime |

Attempt 1 failed before `MUTATION_STARTED=1` and before `systemctl stop`; the original root gateway
kept PID `2063708`. The staged environment was retained, and a corrected verifier that directly
enumerates the venv's sole `site-packages/hermes_agent-*.dist-info` proved its exact editable source
URL. No production state changed.

Attempt 2 used that corrected verifier. It passed all staged checks, including `AUDITED=104` with
no OSV findings, then entered the authorized maintenance window. Installing the reviewed sysusers
file failed closed because this host had no `/etc/sysusers.d` parent. The trap restored the original
unit, venv, source, dependency versions, and root service identity, then restarted the gateway as
PID `2446659`. Independent verification reproduced exactly the original two live Phase 1 findings,
found both service identities and state directories absent, found all execution units/workers
inactive, and found all nine outboxes paused with zero events. The third transaction therefore
recorded whether both `/etc` parents existed, created missing parents root-owned mode `0755` inside
the transaction, and removes only newly created empty parents on rollback. The resulting reviewed
deployment-script SHA-256 was
`8c5425fcf2df0f23a656351646974f8b5bb8af54a617485d3687a5e311127cba`.

### Third fresh preflight and exact transaction commands

The third preflight began from production awareness OID
`6ce55f71a9d5cbef41510379b4f755b5290d3f90`, reviewed Recursion
`origin/master` `9422b9cc5bf4270b7184b9af6fa31cae8e6a6876`, original Hermes source
`36cb5ae5530a75def7df3195e49b7a4aa2add482`, and authorized Hermes remote OID
`498061d8f1dcd855419b30e8729c869596a89ca6`. It ended with:

```text
BASELINE_SUITES=17
BASELINE_FAILED_SUITES=0
hermes-pilot-phase1-red: ALL PASS
hermes-pilot-phase1-green: ALL PASS
hermes-pilot-phase2-red: ALL PASS
hermes-pilot-phase2-green: ALL PASS
hermes-pilot-phase3-red: ALL PASS
hermes-pilot-phase3-green: ALL PASS
hermes-pilot-phase4-red: ALL PASS
hermes-pilot-phase4-green: ALL PASS
hermes-pilot-phase5-red: ALL PASS
hermes-pilot-phase5-green: ALL PASS
hermes-pilot-phase6-red: ALL PASS
hermes-pilot-phase6-green: ALL PASS
hermes-pilot-phase7-red: ALL PASS
hermes-pilot-phase7-green: ALL PASS
phase1-control-preflight: ALL PASS
```

PAUSE had the expected hash before and after. All execution services were inactive and workers
were zero. The exact host-side entry points were:

```bash
bash /var/backups/recursion/phase1-remediation-20260809T141605Z-498061d8/evidence/hermes-phase1-final-predeploy-check.sh
# FINAL_PREDEPLOY_GATE=PASS

bash /var/backups/recursion/phase1-remediation-20260809T141605Z-498061d8/evidence/hermes-phase1-launch-deploy.sh
# nohup bash .../evidence/hermes-phase1-deploy-498061d8.sh \
#   /var/backups/recursion/phase1-remediation-20260809T141605Z-498061d8

bash /var/backups/recursion/phase1-remediation-20260809T141605Z-498061d8/evidence/hermes-phase1-verify-third-rollback.sh
# THIRD_ROLLBACK_VERIFICATION=PASS

cd /var/backups/recursion/phase1-remediation-20260809T141605Z-498061d8
sha256sum --quiet -c evidence/MANIFEST.sha256
# rc=0
```

Before stopping the gateway, the staged 104-package environment passed `uv pip check`, exact
dist-info/direct-URL and source-OID verification, real imports, an RSA-2048 RS256 round trip, and a
complete OSV audit. The maintenance transaction then created the reviewed identities, migrated
only required gateway state, installed the exact reviewed venv and unit, and reached bounded
readiness as `User=hermes-gateway Group=hermes-gateway` with `active_agents=0`.

The hardened live state closed both original Phase 1 findings:

```text
ok    remote PAUSE is present
ok    hermes-gateway runs as a hardened non-root service
AUDITED=104
ok    installed gateway dependencies have no known OSV advisories
hermes-pilot-live-phase1-red: ALL PASS
```

`systemd-analyze security hermes-gateway` reported overall exposure `3.6 OK`; the new identity,
`NoNewPrivileges`, `ProtectSystem`, `PrivateTmp`, `ProtectHome`, `PrivateDevices`, and `UMask`
checks all passed. The post-switch baseline again passed 17/17, the Phase 1 red suite ended
`ALL PASS`, and the dependency artifact audit reported `aiohttp==3.14.3|OSV_CLEAR`,
`cryptography==50.0.0|OSV_CLEAR`, and `ALL PASS`.

The next canonical check, the Phase 1 positive audit, failed closed:

```text
FAIL strict sandbox task status=escalated
env: '/workspace/.recursion-sandbox/claude': Permission denied
```

This is not an internet failure, dependency incompatibility, model claim, or waived test. Both the
transaction and reviewed unit impose `UMask=0077`. The existing `worker-sandbox.sh` chowns the
worktree, then creates `.recursion-sandbox` for a copied non-`/usr` executable without explicitly
making that newly created directory traversable by `recursion-runner`. That code path and the
observed code-126 error identify a hardened-umask/sandbox handoff compatibility blocker. No chmod,
manual worker command, test weakening, production workaround, or retry was performed.

The automatic trap restored unit SHA-256
`6135f46663105dc0149adf3da2659a120fb6f5a47d79871effc1695a90ee891b`, original Hermes source,
the original venv with `aiohttp==3.14.1` and `cryptography==48.0.1`, and the root service identity;
the gateway restarted active as PID `2542748`. Independent verification passed: the original two
live findings reproduce exactly, `active_agents=0`, all execution units and workers are inactive,
and all nine outboxes remain paused with zero events. The newly created service accounts and
mode-`0700` `/var/lib/hermes-gateway` and `/var/lib/recursion-runner` directories remain inert and
retained for forensics, as do `failed-live-venv` and the exact reviewed source. The reviewed
sysusers/tmpfiles configuration files were removed by rollback. `/root/.hermes` was not migrated
away, deleted, or modified by rollback.

Final production remains on awareness OID
`6ce55f71a9d5cbef41510379b4f755b5290d3f90`, original Hermes source
`36cb5ae5530a75def7df3195e49b7a4aa2add482`, and the original root gateway. PAUSE remained present
through every observed window with SHA-256
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`. No Phase 8 code was deployed,
no configuration/capacity flag changed, no notification was sent, no pilot was started, and no
product repository changed.

The next admissible step is a separate reviewed Recursion compatibility patch that proves the
copied sandbox executable remains traversable/executable by the dedicated runner under an inherited
`0077` umask without weakening the controller boundary. It needs a red control for this exact
condition and the complete Phase 1-7 matrix before another separately authorized PAUSE-held
transaction. Phase 8 deployment and all pilot windows remain blocked.

## Hardened-umask sandbox compatibility patch (review-only)

Lucas authorized the separate Recursion change needed by the third deployment attempt. A fresh
fetch left `origin/master` unchanged at
`9422b9cc5bf4270b7184b9af6fa31cae8e6a6876`; the existing clean worktree branch
`review/hermes-post-phase8-hardening` is based exactly on that merge. The dirty local master at
`20c1fa77042f3aa9f5d8978352792f3b058bc0d1` was not used or changed.

The regression was added before the implementation. In a disposable Linux clone, both the Phase 1
red suite and positive audit inherited `umask 0077`. With the original `worker-sandbox.sh`, the new
control produced exactly one failure and the positive audit reproduced the production error:

```text
FAIL  copied worker executable was inaccessible under hardened controller umask (status=escalated)
hermes-pilot-phase1-red: 1 FAILED (expected on the Phase 0 pre-fix commit)
FAIL strict sandbox task status=escalated
env: '/workspace/.recursion-sandbox/claude': Permission denied
RED_PROOF=PASS
```

The implementation changes only the copied non-`/usr` executable handoff. The controller remains
required to be root. Instead of allowing `mkdir` and `cp` to inherit a restrictive umask, it uses
explicit least-privilege metadata:

```bash
install -d -o 0 -g "$RUNNER_GID" -m 0710 "$SANDBOX_BIN"
install -o 0 -g "$RUNNER_GID" -m 0550 "$EXE_REAL" "$SANDBOX_BIN/$ENGINE"
```

The directory and executable therefore remain root-owned and unavailable to unrelated users;
only the dedicated runner group can traverse/read/execute them. Existing cleanup still removes the
copied directory before the controller evaluates the allowed-path diff. No controller path,
publisher permission, worker identity, systemd boundary, network policy, or model command changed.

Focused red-to-green evidence under exact `0077` then reported:

```text
ok    worker OS boundary denied controller data, outside writes, publisher access, and shared identity
ok    copied worker executable remained reachable under hardened controller umask
hermes-pilot-phase1-red: ALL PASS
ok    strict sandbox completed bounded work as uid=981 (controller uid=0)
ok    canonical review ref published and registered default remained unchanged
aiohttp==3.14.3|OSV_CLEAR
cryptography==50.0.0|OSV_CLEAR
hermes-pilot-phase1-artifacts: ALL PASS
hermes-pilot-phase1-green: ALL PASS
FOCUSED_GREEN=PASS
```

The exact modified-file SHA-256 values tested on Linux match the local authoring files:

```text
a5abeb345a3b3dcaf1b372b02f461fffb748cbbc15eab29e8602bc43a728e670  scripts/worker-sandbox.sh
7135cad8e249e444cdc52c7ce0d40c845ad0715b938c1e6c01bac595c8a010ca  scripts/tests/hermes-pilot-phase1-red.test.sh
9b9205068bb37eb8f9e034d30c1996bc20f3f8d567d5e41f1484334adc392da1  scripts/audit/hermes-pilot-phase1-green.sh
```

The first complete-matrix launcher used a detached checkout. The canonical baseline refused with
`BASELINE HARNESS: use a named branch, not detached HEAD` before executing a suite. That negative
result was preserved; no result was inferred from it. The same three files and base OID were then
tested unchanged on disposable named branch
`review/hermes-post-phase8-hardening-umask-test`, still with process-wide `umask 0077`:

```text
BASELINE_SUITES=17
BASELINE_FAILED_SUITES=0
hermes-pilot-phase1-red: ALL PASS
hermes-pilot-phase1-green: ALL PASS
hermes-pilot-phase2-red: ALL PASS
hermes-pilot-phase2-green: ALL PASS
hermes-pilot-phase3-red: ALL PASS
hermes-pilot-phase3-green: ALL PASS
hermes-pilot-phase4-red: ALL PASS
hermes-pilot-phase4-green: ALL PASS
hermes-pilot-phase5-red: ALL PASS
hermes-pilot-phase5-green: ALL PASS
hermes-pilot-phase6-red: ALL PASS
hermes-pilot-phase6-green: ALL PASS
hermes-pilot-phase7-red: ALL PASS
hermes-pilot-phase7-green: ALL PASS
FULL_UMASK_MATRIX=ALL_PASS
```

Exact red/green logs, both full-matrix logs, launchers, the binary patch, reviewed file copies,
final live state, result summary, and `evidence/MANIFEST.sha256` are retained in root-owned
mode-`0700` bundle
`/var/backups/recursion/phase1-umask-compat-20260809T174059Z-9422b9cc`. Its manifest verifies
cleanly. The disposable `/var/tmp/recursion-phase1-umask-red.B0l1Wr` clone was removed only after
that verification.

Final read-only production closure found awareness OID
`ddff98258b39f6d8d5f4811ea2ff7ee47952a017`, original Hermes source
`36cb5ae5530a75def7df3195e49b7a4aa2add482`, original root gateway PID `2542748`, zero workers,
all execution units inactive, and all nine outboxes paused with zero events. PAUSE remained exact
at `919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`. No production code,
service, dependency, flag, capacity, notification, pilot, or product repository changed.

The five-file Recursion patch—three implementation/control files plus this evidence and the Phase 8
handoff—is staged only. It has not been committed, pushed, merged, or deployed. Lucas review and
publication are the next gate; another PAUSE-held deployment still requires separate authorization
after the reviewed patch is merged into `origin/master`.

## Merged Phase 1 remediation deployment: failed-closed destination cleanup gate

Lucas reported the Recursion compatibility branch merged and explicitly authorized a fresh,
PAUSE-held, gateway-only Phase 1 remediation deployment using exact Recursion commit
`fa0263587479b3c43b0ba6bb61f5b176383f7cec` and frozen Hermes commit
`498061d8f1dcd855419b30e8729c869596a89ca6`. A fresh authenticated workstation fetch found
`origin/master` at that merge commit and verified reviewed head
`56efa495ca6a3770e78d8063311c492055dfa307` as an ancestor. Production's existing
`origin/master` matched it exactly, and deployed awareness branch `loop/auto` at
`f55ec1f73c9650939d4162e7a4487ef848446eba` contained it as an ancestor. The three merged
compatibility files had their reviewed Linux hashes:

```text
a5abeb345a3b3dcaf1b372b02f461fffb748cbbc15eab29e8602bc43a728e670  scripts/worker-sandbox.sh
7135cad8e249e444cdc52c7ce0d40c845ad0715b938c1e6c01bac595c8a010ca  scripts/tests/hermes-pilot-phase1-red.test.sh
9b9205068bb37eb8f9e034d30c1996bc20f3f8d567d5e41f1484334adc392da1  scripts/audit/hermes-pilot-phase1-green.sh
```

Two preliminary launcher invocations stopped before creating a bundle: production's private
Recursion remote uses SSH but has no GitHub credential, and anonymous HTTPS cannot read the private
repository. No production credential or remote configuration was added. The accepted trust split
was a fresh authenticated workstation fetch plus exact server-side OID and ancestry comparison.
Authenticated GitHub settings readback also re-verified classic protection rule `81491567` on
`master`: pull request required, conversation resolution required, administrator bypass disabled,
force pushes disabled, and deletion disabled. PR #30 readback reported `merged=true`, reviewed head
`56efa495...`, and merge commit `fa026358...`.

The fresh root-owned mode-`0700` transaction bundle is:

```text
/var/backups/recursion/phase1-remediation-20260809T225227Z-498061d8
preflight start: 2026-08-09T22:52:27Z
final closure:   2026-08-09T23:23:25Z
```

The exact preflight command was retained as
`evidence/hermes-phase1-launch-preflight.sh`; it launched the retained
`evidence/phase1-control-preflight.sh` under `nohup`. It reproduced the two expected live Phase 1
findings, passed the Phase 0 baseline 17/17, and passed every Phase 1-7 control suite and every
Phase 1-7 positive audit. It ended:

```text
BASELINE_SUITES=17
BASELINE_FAILED_SUITES=0
hermes-pilot-phase1-red: ALL PASS
hermes-pilot-phase1-green: ALL PASS
hermes-pilot-phase2-red: ALL PASS
hermes-pilot-phase2-green: ALL PASS
hermes-pilot-phase3-red: ALL PASS
hermes-pilot-phase3-green: ALL PASS
hermes-pilot-phase4-red: ALL PASS
hermes-pilot-phase4-green: ALL PASS
hermes-pilot-phase5-red: ALL PASS
hermes-pilot-phase5-green: ALL PASS
hermes-pilot-phase6-red: ALL PASS
hermes-pilot-phase6-green: ALL PASS
hermes-pilot-phase7-red: ALL PASS
hermes-pilot-phase7-green: ALL PASS
phase1-control-preflight: ALL PASS
pause_after=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
```

Immediate state capture recorded the deployed refs, old root gateway, all services/timers/workers,
project/pilot configuration, all notification destinations, nine paused and empty outboxes,
backup/restore manifests, free space, and the exact rollback. Both existing Phase 1 findings were
again required, not waived. `FINAL_PREDEPLOY_GATE=PASS` was recorded before this exact detached
launch:

```bash
nohup bash \
  /var/backups/recursion/phase1-remediation-20260809T225227Z-498061d8/evidence/hermes-phase1-deploy-498061d8.sh \
  /var/backups/recursion/phase1-remediation-20260809T225227Z-498061d8 \
  > /var/backups/recursion/phase1-remediation-20260809T225227Z-498061d8/evidence/launcher.log \
  2>&1 < /dev/null &
```

Before maintenance, the staged venv passed `uv pip check`, exact editable-source metadata,
gateway/provider imports, RS256, and a complete 104-package OSV query. It contained
`aiohttp==3.14.3`, `cryptography==50.0.0`, and exact Hermes source `498061d8...`. The script then
stopped only `hermes-gateway` and began canonical state migration. It failed before the venv switch
and before installing the hardened unit. The exact new gate was:

```text
FAILURE_CAUSE=pre-existing excluded destination bin and logs survived rsync --delete, then the explicit forbidden-path assertion failed closed
VENV_SWITCHED=NO
HARDENED_UNIT_INSTALLED=NO
```

This arose because service identities and an inert `/var/lib/hermes-gateway` survived the prior
rolled-back attempt. The source excludes correctly prevent copying `bin/` and `logs/`, but plain
`rsync --delete` also preserves already-existing excluded destination paths. The next assertion
requires those paths absent and therefore stopped the transaction. No assertion was weakened and
the destination was not manually cleaned in production.

The EXIT trap automatically stopped the gateway, restored the original venv/unit/config-file
absence, restored the byte/metadata-equivalent pre-existing inert state directory from the new
rollback snapshot, reloaded systemd, and restarted the old gateway. Independent verification then
passed old dependency versions, old Hermes OID, exact old unit hash, project/runtime configuration
hash equality, inert-state manifest equality, zero workers, all execution units inactive, nine
paused/empty outboxes, and exactly the original two live findings:

```text
AUTOMATIC_ROLLBACK=PASS
INDEPENDENT_ROLLBACK_VERIFICATION=ALL_PASS
RETRY_STARTED=NO
RESULT=FAILED_CLOSED_ROLLED_BACK
FINAL_PAUSE_SHA256=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
FINAL_HERMES_OID=36cb5ae5530a75def7df3195e49b7a4aa2add482
FINAL_GATEWAY_USER=root
```

A read-only `rsync -ani --delete --delete-excluded` proof showed that the corrected option would
delete the pre-existing excluded `bin/` and `logs/` paths without touching `/root/.hermes`. A
syntax-checked prospective transaction adds that option plus ERR line/command logging; it is
retained only as `evidence/hermes-phase1-deploy-498061d8-candidate-v5.sh`, SHA-256
`4df3299a546802ea15f0080fdc6dd33a108876c20d73e50e921bef03ef38ecaf`. It was not launched.

The bundle manifest verifies cleanly; `evidence/MANIFEST.sha256` has SHA-256
`9d68d0d735a9e6adc661f510c70c6a7053617e3f06770ffe5b93c3ca5585a93a`. Final production closure
found PAUSE exact, old root gateway active, old unit/source/dependencies restored, zero workers or
project processes, all execution units inactive, and all nine outboxes paused with zero events or
deliveries. No notification, pilot, flag, capacity, product-repository, or Phase 8 deployment action
occurred. A new full preflight and a new explicit gateway-maintenance authorization are required
before the prospective correction may be run.

## Corrected Phase 1 remediation deployment: all gates passed

Lucas explicitly authorized a new full PAUSE-held preflight and corrected gateway-only Phase 1
deployment using the same exact Recursion and Hermes OIDs plus the reviewed `rsync
--delete-excluded` migration correction. No authorization was given to lift PAUSE, notify, alter
project configuration/capacity, or start a pilot.

The fresh transaction used root-owned mode-`0700` bundle:

```text
/var/backups/recursion/phase1-remediation-20260809T233144Z-498061d8
preflight start:       2026-08-09T23:31:44Z
hardened gateway start: 2026-08-09T23:53:56Z
final closure:         2026-08-10T00:17:44Z
```

Before preflight, a fresh authenticated workstation fetch again found
`origin/master=fa0263587479b3c43b0ba6bb61f5b176383f7cec` and reviewed head
`56efa495ca6a3770e78d8063311c492055dfa307` as its ancestor. Production's existing
`origin/master` matched. PR #30 again read `merged=true` with merge commit `fa026358...`.
Authenticated GitHub readback of protection rule `81491567` again showed exact `master`, required
pull request and conversation resolution, no administrator bypass, and force-push/deletion both
disabled. No GitHub setting changed.

The fresh PAUSE-held preflight again reproduced exactly the two old Phase 1 live findings, passed
the Phase 0 baseline 17/17, and passed every Phase 1-7 control and positive audit. Its terminal
markers were:

```text
BASELINE_SUITES=17
BASELINE_FAILED_SUITES=0
hermes-pilot-phase1-red: ALL PASS
hermes-pilot-phase1-green: ALL PASS
hermes-pilot-phase2-red: ALL PASS
hermes-pilot-phase2-green: ALL PASS
hermes-pilot-phase3-red: ALL PASS
hermes-pilot-phase3-green: ALL PASS
hermes-pilot-phase4-red: ALL PASS
hermes-pilot-phase4-green: ALL PASS
hermes-pilot-phase5-red: ALL PASS
hermes-pilot-phase5-green: ALL PASS
hermes-pilot-phase6-red: ALL PASS
hermes-pilot-phase6-green: ALL PASS
hermes-pilot-phase7-red: ALL PASS
hermes-pilot-phase7-green: ALL PASS
phase1-control-preflight: ALL PASS
pause_after=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
```

Immediate pre-mutation capture again recorded exact refs, services/timer/workers, active-agent
count, project/pilot configuration, notification destinations, nine paused/empty outboxes,
branch protection, backup/restore readiness, free space, configuration hashes, and exact rollback.
All pilot concurrency remained zero and all model aliases/global concurrency remained unset.
`FINAL_PREDEPLOY_GATE=PASS` preceded this exact launch:

```bash
nohup bash \
  /var/backups/recursion/phase1-remediation-20260809T233144Z-498061d8/evidence/hermes-phase1-deploy-498061d8.sh \
  /var/backups/recursion/phase1-remediation-20260809T233144Z-498061d8 \
  > /var/backups/recursion/phase1-remediation-20260809T233144Z-498061d8/evidence/launcher.log \
  2>&1 < /dev/null &
```

The launched transaction script has SHA-256
`4df3299a546802ea15f0080fdc6dd33a108876c20d73e50e921bef03ef38ecaf`. It copied the old venv and
pre-existing inert state into rollback storage, staged exact frozen Hermes source
`498061d8f1dcd855419b30e8729c869596a89ca6`, passed `uv pip check`, exact editable metadata,
gateway/provider imports, RS256, and a complete 104-package OSV audit, then stopped only
`hermes-gateway`.

The corrected canonical migration used:

```bash
rsync -a --delete --delete-excluded \
  --exclude='/bin/' --exclude='/cache/' --exclude='/audio_cache/' \
  --exclude='/image_cache/' --exclude='/logs/' --exclude='/sandboxes/' \
  --exclude='*.lock' --exclude='*.pid' --exclude='config.yaml.bak*' \
  --exclude='.update_check' --exclude='.codex_gpt55_autoraise_notice' \
  --exclude='gateway-starts.log' \
  /root/.hermes/ /var/lib/hermes-gateway/
```

The migration manifest proved excluded root-runtime paths absent before service start. The exact
reviewed unit and identity files were then installed, the staged venv was switched live, and the
gateway reached bounded readiness as dedicated `hermes-gateway:hermes-gateway`. The old venv is
retained at `/usr/local/lib/hermes-agent/venv.pre-phase1-498061d8f1dc` for exact rollback.

Post-deployment acceptance passed the live Phase 1 audit, systemd security analysis, a second
baseline 17/17, and every Phase 1-7 control and positive audit. Important terminal evidence was:

```text
hermes-pilot-live-phase1-red: ALL PASS
AUDITED=104
aiohttp==3.14.3|OSV_CLEAR
cryptography==50.0.0|OSV_CLEAR
BASELINE_SUITES=17
BASELINE_FAILED_SUITES=0
hermes-pilot-phase1-red: ALL PASS
hermes-pilot-phase1-green: ALL PASS
hermes-pilot-phase2-red: ALL PASS
hermes-pilot-phase2-green: ALL PASS
hermes-pilot-phase3-red: ALL PASS
hermes-pilot-phase3-green: ALL PASS
hermes-pilot-phase4-red: ALL PASS
hermes-pilot-phase4-green: ALL PASS
hermes-pilot-phase5-red: ALL PASS
hermes-pilot-phase5-green: ALL PASS
hermes-pilot-phase6-red: ALL PASS
hermes-pilot-phase6-green: ALL PASS
hermes-pilot-phase7-red: ALL PASS
hermes-pilot-phase7-green: ALL PASS
phase1-remediation-compat: ALL PASS
```

Independent closure rechecked exact source metadata, `uv pip check`, service properties, process
owner, gateway state, active agents, package versions, configuration hashes, service/worker
inertness, outboxes, PAUSE, live Phase 1, and systemd security. The effective boundary is:

```text
User=hermes-gateway
Group=hermes-gateway
UMask=0077
WorkingDirectory=/var/lib/hermes-gateway
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
Overall exposure level for hermes-gateway.service: 3.6 OK
gateway_state=running
active_agents=0
```

One independent-verifier assertion initially required live `bin/`, `logs/`, and lock paths to stay
absent. It stopped before claiming closure. Inspection proved the migration manifest was clean and
the running gateway had subsequently recreated its own service-owned runtime `bin/`, `logs/`, and
`gateway.lock`. The corrected verifier requires a clean migration manifest and exact
`hermes-gateway` ownership of those recreated runtime paths. It then ended
`INDEPENDENT_CLOSURE=ALL_PASS`. No service or production file changed during that verifier fix.

The transaction's first manifest was also correctly rejected after terminal observation because
`deployment.log` and `launcher.log` continued receiving final lines after their initial hashes.
No result was inferred from that manifest. After all logs and independent evidence were closed, the
bundle was resealed and `sha256sum --quiet -c evidence/MANIFEST.sha256` passed. The final manifest
file has SHA-256 `bea07eb20a4ed1c5e2214592690eb1323f70201e1e51dfe11064cfacfec5eeba`.

Final production closure at `2026-08-10T00:17:44Z` observed awareness branch OID
`73987156f5d22d2f2f50ddc542c978d0c48f88b8` with reviewed
`origin/master=fa0263587479b3c43b0ba6bb61f5b176383f7cec`, exact Hermes source
`498061d8f1dcd855419b30e8729c869596a89ca6`, active non-root gateway PID `2781295`, zero active
agents, zero worker units/processes, every execution/supervisor unit inactive, and all nine
outboxes paused with zero events/deliveries. Project/runtime configuration hashes were unchanged.
PAUSE remained exact at
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`. No notification, pilot,
model/capacity change, PAUSE lift, autonomous deployment, default-branch push, or product-repository
mutation occurred.

This closes the two Phase 1 deployment gates only. Phase 8 deployment, model/capacity setup, PAUSE
removal, and every pilot window remain separate actions requiring their applicable authorization.

## Phase 8 actuator deployment: legacy journal blocker and automatic rollback

Lucas clarified and explicitly authorized one fresh PAUSE-held **Phase 8** actuator deployment.
The authorized boundary was installation and enablement of only the exact reviewed
`hermes-actuator.service` after a complete preflight, with automatic rollback. It explicitly
excluded any gateway change, PAUSE removal, notification, model/capacity or other configuration
change, pilot, or product-repository mutation. The recorded authorization timestamp was
`2026-08-10T00:45:20Z`.

### Fresh provenance and live invariants

Fresh workstation fetch and authenticated GitHub readback established:

```text
REVIEWED_MASTER=b23069ee49519fda5896f43ec888efd71b533167
PR31_MERGE=b23069ee49519fda5896f43ec888efd71b533167
EVIDENCE_COMMIT=71e651ad958fa0e50904e7730e711ed39e6e2d82
EVIDENCE_ANCESTOR=PASS
PHASE7_IMPLEMENTATION=b4d3a30317e7d11f8e31b5e2578eb53aaeb5155c
PHASE7_ANCESTOR=PASS
REVIEW_BRANCH_PRESENT=false
RULE_ID=81491567
PATTERN=master
PULL_REQUEST_REQUIRED=true
CONVERSATION_RESOLUTION_REQUIRED=true
ADMIN_BYPASS_DISABLED=true
FORCE_PUSH_DISABLED=true
DELETION_DISABLED=true
SETTINGS_CHANGED=false
```

All Phase 7 handoff/evidence artifacts were present. Production remained on awareness branch
`loop/auto` at `73987156f5d22d2f2f50ddc542c978d0c48f88b8`, with production
`origin/master=fa0263587479b3c43b0ba6bb61f5b176383f7cec`; both the Phase 8 implementation
`7d0dc10fcb5914f98353be1f73deb15498e71b7c` and runtime base `fa026358...` were ancestors.
Only awareness-state files followed the runtime base. All 18 reviewed Phase 8 executable/config
blob OIDs, including the unit, actuator, decision queue, worker/verifier, reconciler, outbox,
runtime-state tool, and both pilot records, matched current merged master.

The live control plane and safety state before the action were:

```text
CONTROL_PLANE_SHA256=da43555b6977e55fc0bb185fd7d6d5e76384b638cbcaff4d798596cce68630d4
PAUSE_SHA256=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
PAUSE_OWNER=root:root
PAUSE_MODE=0644
PAUSE_SIZE=189
hermes-gateway=active, User=hermes-gateway, Group=hermes-gateway, PID=2781295
hermes-actuator=not-found/inactive
workers=0
project_processes=0
outboxes=9 paused=true events=0 deliveries=0
RECURSION_PILOT_MODEL=unset
RECURSION_DECIDE_MODEL=unset
RECURSION_PILOT_GLOBAL_CONCURRENCY=unset
pilot concurrency_limit=0
pilot max_inflight_workers=0
pilot publication=false
```

Notification destinations, the exact four-line root crontab, project/pilot configuration hashes,
service/timer/worker state, live Phase 1, retained Phase 1 backup manifest, free space, and the
rollback were captured before mutation. The pre-existing untracked production file was not read or
changed. The dirty local master remained at `20c1fa77042f3aa9f5d8978352792f3b058bc0d1`
with all 20 user-owned status entries untouched.

### Two preflight observations

The first exact preflight invocation was retained as a negative observation:

```bash
nohup bash \
  /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee/evidence/phase8-preflight-attempt1.sh \
  /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee \
  > /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee/evidence/preflight-attempt1-launcher.log \
  2>&1 < /dev/null &
```

At `2026-08-10T00:47:15Z` it stopped before deployment with
`PREFLIGHT_FAIL=nonempty-state/decision-queue/pending`. The initial gate incorrectly assumed an
empty pending queue. Read-only inspection identified exactly one schema-valid Head Hermes
PAUSE-safe no-op projection:

```text
id=decision-6f037cbf6fb51a7f641ea9d6
actor=head-hermes
project=recursion
goal_id=architecture-rebase
verb=noop
status=queued
SHA256=883d9f53e2d7263d8ffa733fe61bff959cf1b7e341d4a293d0a9b00f8e0c6684
```

The preflight gate was narrowed to accept only that exact path, hash, schema, and singleton queue
count; any second or different queue record still failed closed. No production data or service was
changed by this correction. The corrected preflight was launched as:

```bash
nohup bash \
  /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee/evidence/phase8-preflight.sh \
  /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee \
  > /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee/evidence/preflight-launcher.log \
  2>&1 < /dev/null &
```

From `2026-08-10T00:51:33Z` through `01:10:35Z`, it passed exact PAUSE, ancestry, 18 blob hashes,
GitHub protection/merge, inert services, zero capacity/routing, paused empty outboxes, the exact
pending no-op, and retained backup readiness. It then returned zero for:

```text
phase0-baseline
phase1-control  phase1-audit
phase2-control  phase2-audit
phase3-control  phase3-audit
phase4-control  phase4-audit
phase5-control  phase5-audit
phase6-control  phase6-audit
phase7-control  phase7-audit
phase8-control  phase8-audit
live-phase1
PREFLIGHT_RESULT=PASS
```

This reproduced the 17/17 historical baseline and all Phase 1-8 behavioral/positive evidence. The
three retained transaction script SHA-256 values are:

```text
21f8666ead39a84b759ab6a567ca94a801a5baf8e3ba610e6656de438e2e16a8  phase8-preflight.sh
b0498c4ac19b5ae4c1c3a51b48c52b61efdbb7dfbd4492b636e1c0c272cdd380  phase8-deploy.sh
91f812d80cc869ce34a2d92dbea2a28829f93d96b9a003ead10938d3bd06e089  phase8-independent-verify.sh
```

### Authorized production window and rollback

The root-owned, mode-`0700` bundle was created outside `/opt/recursion` at:

```text
/var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee
```

The transaction command was:

```bash
nohup bash \
  /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee/evidence/phase8-deploy.sh \
  /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee \
  > /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee/evidence/deployment-launcher.log \
  2>&1 < /dev/null &
```

At `2026-08-10T01:11:43Z`, it rechecked PAUSE, gateway, actuator absence, exact queue/config,
crontab, workers/processes, and all preflight terminal markers. It stopped only `cron.service`; the
gateway remained active. `runtime-state.py backup` created `runtime-before` with 19 files and exact
PAUSE. That snapshot verified, restored into disposable staging, verified again, and staging was
removed. The retained Phase 1 backup manifest also reverified. The exact rollback was already
armed before `FINAL_PREDEPLOY_GATE=PASS`:

```bash
systemctl disable --now hermes-actuator.service
rm -f -- /etc/systemd/system/hermes-actuator.service
systemctl daemon-reload
systemctl reset-failed hermes-actuator.service
RECURSION_ROOT=/opt/recursion python3 scripts/runtime-state.py restore runtime-before
systemctl start cron.service
```

The transaction also quarantined any newly generated action/event files before restore; none were
found because no queue transition occurred. It did not stop or restart `hermes-gateway`.

The reviewed unit has Git blob `0b1e173a850327811123395398457e238e2b8c4c` and Linux SHA-256
`5cfd8853d6e66068670aa5f9afcfa5b5671730af927d166a8dca4dad50486d4e`. It was installed,
enabled, and active/running as PID `2934221` at `2026-08-10T01:11:45Z` with:

```text
ExecStart=/bin/bash /opt/recursion/scripts/hermes-actuator.sh
WorkingDirectory=/opt/recursion
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
SystemCallArchitectures=native
```

After the initial control lock was released, the bounded 20-second observation found the exact
PAUSE-safe no-op still pending. At `01:12:10Z` the transaction emitted
`DEPLOY_FAIL=paused-noop-still-pending`, and automatic rollback ended at `01:12:12Z`:

```text
AUTOMATIC_ROLLBACK=ATTEMPTED
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
ACTUATOR_LOAD=not-found
ACTUATOR_ACTIVE=inactive
CRON_ACTIVE=active
GATEWAY_ACTIVE=active
```

No automatic or manual retry occurred.

### Root cause: legacy journal import precedes the valid projection

Read-only analysis retained at `evidence/failure-analysis.txt` found
`state/decisions.jsonl` SHA-256
`c0a03878fc98df2bb6e6a83496d003dd198e53a8477cb004af6a3909f5b103a0`, with six non-empty
records. After the current deterministic normalization, lines 1-5 remain invalid because their
legacy prose `goal_id` values are not canonical identifiers. Line 6 is the exact valid pending
`architecture-rebase` no-op and validates.

The live path is:

```text
hermes-actuator.sh
  -> conduct-hermes.sh --paused-safe
  -> decision-queue.py import-journal
  -> normalize and validate journal line 1
  -> ERR goal_id is not a canonical identifier
  -> return before existing-projection check and before pending claim
```

`hermes-actuator.sh` redirects this call's output to `/dev/null` and accepts the nonzero return for
the next loop, so only service startup appeared in the journal. This is a live legacy-journal
compatibility defect, not evidence of an executed action. No journal record was edited, deleted,
migrated, skipped, or manually drained; no test, fixture, gate, or baseline was changed.

The required reviewed repair must preserve the append-only raw records and fail closed for every
new invalid decision, deterministically classify or migrate already-existing legacy no-ops, emit a
durable diagnostic instead of suppressing import failure, and add a six-line live-journal-shaped
regression proving the valid pending no-op transitions exactly once without duplicate action/event
creation.

### Independent rollback closure and final evidence

The independent closure used a new root-owned mode-`0700` child bundle and reran the same complete
preflight against the restored state:

```bash
nohup bash \
  /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee/rollback-closure/evidence/phase8-preflight.sh \
  /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee/rollback-closure \
  > /var/backups/recursion/phase8-actuator-20260810T004518Z-b23069ee/rollback-closure/evidence/closure-launcher.log \
  2>&1 < /dev/null &
```

From `2026-08-10T01:17:19Z` through `01:36:16Z`, baseline, every Phase 1-8 control/audit, and live
Phase 1 all returned zero. A new `runtime-rollback-verify` snapshot then matched every one of the
19 `runtime-before` file records, hashes, sizes, modes, owners, commit, PAUSE hash, and observed
locks after excluding only the manifest's `created_at`. It restored and verified in a second
disposable staging root, which was removed. Closure ended `ROLLBACK_CLOSURE=PASS` at
`2026-08-10T01:37:02Z`.

Final exact production state was:

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
DECISION_QUEUE_FILES=1
ROLLBACK_GENERATED_FILES=0
OUTBOXES=9 paused=true events=0 deliveries=0
MODEL_ROUTING=unset
CAPACITY=zero
DEPLOYED_HEAD=73987156f5d22d2f2f50ddc542c978d0c48f88b8
DEPLOYED_ORIGIN_MASTER=fa0263587479b3c43b0ba6bb61f5b176383f7cec
```

The unchanged hardened gateway continued to use frozen Hermes source
`498061d8f1dcd855419b30e8729c869596a89ca6`; final live Phase 1 returned zero. Current reviewed
workstation master remained `b23069ee49519fda5896f43ec888efd71b533167`. No Phase 8 runtime OID
remained installed after rollback.

The final manifest was created only after all launcher/control logs closed. It covers 169 data
files; `sha256sum --quiet -c evidence/MANIFEST.sha256` passed after sealing. The manifest file's
SHA-256 is `2de6f66997e384d5e360b8005d12cdf73a88f3e895bfa93d38a4156992e13319`.
Disposable staging and uploaded `/tmp` helpers were removed; the root-owned evidence bundle and its
two verified runtime snapshots remain.

The complete deployment audit chain is authorization -> current protected merge and exact blobs
-> fresh preflight -> runtime backup/restore -> final predeploy gate -> exact unit/PID -> bounded
PAUSE-safe transition failure -> automatic rollback -> independent full-matrix closure -> exact
runtime comparison -> sealed manifest. Because the queue was never claimed, there is deliberately
no new instruction/goal/task/run/action/gate/verifier/review/report/delivery chain and no human
send. This is zero state delta after rollback, not partial pilot evidence.

Phase 8 remains undeployed. No promotion gate is claimed, and success, assist, offline, parallel,
and soak remain blocked. The next admissible action is a separate reviewable
legacy decision-journal compatibility patch. A new production retry would still require that patch
to be reviewed, merged, and pruned, followed by another fresh full preflight and separate explicit
Lucas authorization.

## Phase 8 compatibility preflight: stale hash and PAUSE-resync blockers

Lucas reviewed, merged, and pruned the deployment-evidence and legacy-journal compatibility
branches, then authorized production testing under PAUSE. No PAUSE removal, notification, pilot,
model/capacity/configuration change, gateway change, or product-repository action was authorized.

### Merge, ancestry, artifact, and protection proof

The clean workstation deployment checkout was refreshed from GitHub:

```powershell
git fetch origin --prune
git rev-parse origin/master
git log --oneline --decorate -8 origin/master
git branch -r --list "origin/review/hermes-phase8-*"
```

It returned current master `be3cb8b6462bc6bbe2ba12305e6bee47ee102fc8`, PR #33 merge over PR
#32, and no matching remote review branches. `git merge-base --is-ancestor` returned zero for Phase
7 implementation `b4d3a303...`, Phase 8 implementation `7d0dc10f...`, Phase 1 remediation
`fa026358...`, evidence `abdf4fe...`, compatibility `1748f75...`, PR #32 merge `4c7d3a8...`, and
PR #33 merge `be3cb8b...`. Phase 7/8 handoffs, the compatibility handoff, and Phase 0 evidence were
present.

The signed-in GitHub settings page showed protection rule `81491567` on `master`: pull requests and
conversation resolution required, administrators included, force pushes disabled, and deletion
disabled. No setting was changed.

At `2026-08-10T02:37:36Z`, live read-only capture returned:

```text
HEAD=73987156f5d22d2f2f50ddc542c978d0c48f88b8
BRANCH=loop/auto
TRACKED_DIRTY=false
STATUS_COUNT=1  # pre-existing .env.bak.1786014440 only; not read or changed
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
JOURNAL=c0a03878fc98df2bb6e6a83496d003dd198e53a8477cb004af6a3909f5b103a0
PENDING=883d9f53e2d7263d8ffa733fe61bff959cf1b7e341d4a293d0a9b00f8e0c6684
LEGACY_MARKERS=0
ACTUATOR=not-found/inactive
GATEWAY=active PID 2781295
CRON=active
WORKERS=0
PROJECT_PROCESSES=0
```

The production default origin transport still failed its plain root SSH authentication, so no key
or configuration was changed. A local incremental Git bundle was created from exact workstation
`origin/master` with prerequisite `fa026358...`; `git bundle verify` passed and bundle SHA-256 was
`f6fba0cbce7047d4fcc95e7ef3fa9d1f150d9c8bf3e2890b767427b2af9ffc9a`.

### Exact merged hash mismatch

A root-owned reviewed-source clone extracted exact commit `be3cb8b...`. Five of six compatibility
hashes matched the handoff. `scripts/decision-queue.py` did not:

```text
GIT_BLOB=510fce16166a6e69b2604cdecd10441262ca202a
LINUX_SHA256=082c82b6ff5e82fd2123322f8dc695323ca3091ff959254b12f4a46c77fd8a08
HANDOFF_SHA256=b487eb78b2f04c315a0239338fb97465b8bfea383466a5ec981f1643bd4e3757
WINDOWS_CHECKOUT_SHA256=452057091ad14138a8c3c27e8744e2163d3cdd56ec567f132bafc8e4aaa8c6c1
```

This proved a stale evidence value, not newline conversion. The preflight stopped before its suite,
`FINAL_PREDEPLOY_GATE`, service change, code merge, queue action, or notification.

The stopped evidence is root-owned and mode `0700` at
`/var/backups/recursion/phase8-actuator-20260810T024806Z-be3cb8b`. The corrected capture at
`2026-08-10T02:52:23Z` re-proved the exact live state above, unset pilot/decision model routing,
unset global concurrency, and all nine outboxes paused with zero events/deliveries. The manifest
verified with SHA-256 `b7b50eb68331e502bf2c7edccf6d0183371a09427ff853ac04d6d3508495d2f5`.
An earlier final-state capture misquoted `cut`/`awk`, produced empty fields, and was retained as
`final-state-attempt1.txt`; it was not used as positive evidence.

### Authorized evidence correction rerun

Lucas authorized a separate evidence-only branch, full exact merged matrix, and disposable
live-shaped proof. Branch `review/hermes-phase8-preflight-evidence-correction` was created in a new
clean worktree at `be3cb8b...`. A second root-owned source/evidence bundle was created outside
`/opt/recursion`:

```text
/var/backups/recursion/phase8-evidence-correction-20260810T032141Z-be3cb8b
```

All setup negatives were retained. The first composite SSH setup command failed on quoting before
target creation. The corrected literal-path setup passed. A standalone Python syntax wrapper then
misquoted `compile(..., "exec")`; the later direct `py_compile` and Bash syntax checks passed.
Matrix attempt 1 failed before tests with `MATRIX_FAIL=bundle-mode` because the parent bundle was
`0755`; its child evidence directory was already `0700`. Attempt logs were moved under
`evidence/attempt1`, only the parent mode was corrected to `0700`, and ownership remained
`root:root`.

The next launch was:

```bash
nohup bash \
  /var/backups/recursion/phase8-evidence-correction-20260810T032141Z-be3cb8b/evidence/phase8-evidence-correction-matrix.sh \
  /var/backups/recursion/phase8-evidence-correction-20260810T032141Z-be3cb8b \
  > /var/backups/recursion/phase8-evidence-correction-20260810T032141Z-be3cb8b/evidence/matrix-launcher.log \
  2>&1 < /dev/null &
```

It stopped before `phase0-baseline` with `MATRIX_FAIL=live-head`.

### PAUSE allowed an executable checkout merge

Read-only `git show`, `git reflog`, cron, and cycle-log evidence reconstructed the change. While the
matrix evidence was being prepared, the scheduled `15 */3 * * *` cycle ran with exact PAUSE:

```text
2026-08-10T03:15:01Z cycle start paused=1
FF recursion — master +6
2026-08-10T03:15:24Z portfolio resynced
ce9904b3dfed64c90f95b5214b0fc5460f81810f Merge branch 'master' into loop/auto
  parent 1: 73987156f5d22d2f2f50ddc542c978d0c48f88b8
  parent 2: be3cb8b6462bc6bbe2ba12305e6bee47ee102fc8
2026-08-10T03:15:33Z heartbeat signal=brief
2026-08-10T03:16:00Z brief done deliver=0
2026-08-10T03:16:00Z paused: skipping plan/drain/conduct
30fb73f56496d1d8e72f00dceb4b59de4bd21a00 loop: board 2026-08-10 03:16Z
```

`ce9904b...` changed the three Phase 8 evidence/handoff docs, production plan, and the six
compatibility implementation/control files in the live checkout. Production now has exact reviewed
blob `510fce1...` and Linux SHA `082c82...` for `decision-queue.py`. The actuator was never
installed, so the journal and pending decision were not processed.

The path is explicit in merged code: `cycle.sh` calls `resync.sh sync` under PAUSE and labels it
awareness/preservation; `pause-semantics.test.sh` positively requires that resync to run. Their live
SHA-256 values are:

```text
904fc1a5a5e07efb6cdb7e52138a695e7f9510e0f0a442727a66a49cbace1f6b  scripts/cycle.sh
511383a5d528be8d393a3e068ace5f4fb7e765d01ce7606538d0ca12c4baabcc  scripts/resync.sh
6882f93f5ee72c734052b3726ec620e5db1b82b011b588b04424d7dba661671e  scripts/tests/pause-semantics.test.sh
```

This contradicts the higher-ranked live `CONTROL_PLANE.md` PAUSE table, which allows awareness but
halts branch-changing maintenance. It also contradicts the Phase 8 architectural ruling that PAUSE
blocks branch-changing maintenance. The implementation and its positive test are stale; they are
not a waiver.

The change was not manually reset. Resetting `loop/auto` would discard the reviewed merge plus
subsequent morning/board awareness commits, exceed the evidence-only authorization, and hide the
control defect. The required rollback is a reviewed control patch: paused resync may fetch and
report status/behind state, but must not checkout, fast-forward, merge, or otherwise change the
checked-out executable tree.

### Final stopped state and retained evidence

At `2026-08-10T03:27:38Z`:

```text
HEAD=30fb73f56496d1d8e72f00dceb4b59de4bd21a00
MASTER=ORIGIN_MASTER=be3cb8b6462bc6bbe2ba12305e6bee47ee102fc8
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
JOURNAL=c0a03878fc98df2bb6e6a83496d003dd198e53a8477cb004af6a3909f5b103a0
PENDING=883d9f53e2d7263d8ffa733fe61bff959cf1b7e341d4a293d0a9b00f8e0c6684
LEGACY_MARKERS=0
ACTUATOR=not-found/inactive
GATEWAY=active PID 2781295
CRON=active
WORKERS=0
PROJECT_PROCESSES=0
OUTBOXES=9 paused=true events=0 deliveries=0
```

The second bundle manifest verifies with SHA-256
`34ddf6d0c4666f5b5ac5613c5e480ce6bc437a4eec6d421d70b8cd4921899ad9`.
The full matrix and disposable live-shaped proof deliberately did not start. No actuator, action,
event, compatibility marker, delivery, human-visible send, pilot, configuration change, PAUSE
change, gateway restart, or product-repository action occurred in the authorized testing work.

Phase 8 remains blocked. The next action is a separate red-first PAUSE/resync control patch through
review, merge, and pruning; then a new full preflight and separate production authorization. Do not
manually drive the pending decision, install/start the actuator, or begin a pilot on partial
evidence.

## Phase 8 PAUSE/resync control patch

Lucas reported the evidence-correction branch merged and pruned and authorized the next reviewable
control patch. Workstation preflight fetched and pruned origin, then proved:

```text
origin/master=52f6ad37250d7b7bc798d86c04768aef8cc09c07
03a5b6a33720daea2b5637e8e0a4c39c05e72d57 is an ancestor
origin/review/hermes-phase8-preflight-evidence-correction absent
dirty master=20c1fa77042f3aa9f5d8978352792f3b058bc0d1
dirty master status entries=20
```

A new clean worktree and `review/hermes-phase8-pause-resync` branch were created from exact
`52f6ad3...`. The red-first and implementation chain is:

```text
2688671a80e7a69929f0b1e137e43485196d72ac  red control
1a16d81c396dde5fbcc87d9ac80cca3017fc3bf2  PAUSE-safe observe implementation
5bbfad5fc46975b6f3de89744a4cd89ab671e201  historical planner-control harness correction
```

### Exact red and focused green proof

The red control builds a disposable real origin and clone, advances the origin with a changed
executable, runs the real paused `cycle.sh`, and checks remote-tracking awareness, every local head,
local `main`, and the checked-out executable SHA. It also removes PAUSE and proves ordinary sync
still moves the branch/tree, preventing a vacuous always-read-only implementation.

At exact red commit `2688671...` with the untouched merged implementation:

```text
paused main 39c2f33... -> c9aca2f...
paused executable SHA 68221c02... -> a900f76b...
origin/main fetched to c9aca2f...
pause-semantics: 1 FAILED
resumed sync fast-forward control: PASS
```

Red log SHA-256 is `b9f9702f1844879f994786fa59ec5e7ac0365670062c70ddabad517706adf4b2`.
After `1a16d81...`, `pause-semantics.test.sh` and `resync.test.sh` both ended `ALL PASS`. The
direct control compared the complete `refs/heads` snapshot plus checked-out file SHA before/after
`observe`, proved `origin/main` fetched to the real tip, and then proved `sync` fast-forwarded.
Focused green log SHA-256 values are
`2f787411f482d683cd1204c6fd87cb59b600153e6958a8d0913b445db0eb0603` and
`991f10ed8a9f4f4a3298636cab7c6e9fb4e98206e85566039d8299d7c5dffa84`.

The exact final Linux file hashes are:

```text
f316b3a752bcb1057479395a40f46048ee0c955e8fdc4dc7a488dbafa897ef8e  scripts/cycle.sh
134f7556bc4852e94082a343f9f4d299c6202f06ae66c147f2df2c3f97e9acd0  scripts/resync.sh
d2f2c07c3a8484eaab3adbf07b360cf3585f6072b70f377340c7738d851c19ce  scripts/tests/pause-semantics.test.sh
7cefba5b048a85d5cf1c183ef370907d4ffbe8316eafa1b154b744e51a68471c  scripts/tests/resync.test.sh
bf95bca38defd7be89bcae684d81ece418f836e4a5eeb7b444bd428a0f2179ad  scripts/tests/hermes-pilot-phase2-red.test.sh
```

### Historical matrix falsifier correction

Matrix attempt 1 stopped in the baseline because the authoring runner globally exported
`RECURSION_NO_NOTIFY=1`, suppressing hermetic stub-notifier positive controls. That runner defect
was removed; no product/test code changed. Attempts 2 and 3 passed the complete baseline and Phase
1, then each stopped at the historical simultaneous-planner assertion with one task/one reserved
item. Five immediate isolated Phase 2 runs passed, as did a separately retained exact
baseline/Phase-1-prefix reproduction.

Inspection found the test's `ls` barrier was structurally downstream of planner-lock acquisition:
the first process held the lock while waiting for a peer that could not reach `ls`. The corrected
harness synchronizes both processes before the lock acquisition. Its vulnerable-read barrier still
activates when the lock is absent. Evidence:

```text
current code, corrected harness: 5/5 full Phase 2 runs PASS
pre-Phase-2 b25b3294dcbd320f92cd104718d795648fd57449 + corrected harness:
  all 6 intended failures, including planner CAS missing
pre-change log SHA-256=8bb5b0316ca8d6d0de4aeb8744c30faca035397a2f71ccc9dedf4de6f6905fa1
```

All failed attempts were retained; none was reclassified as passing evidence.

### Exact final matrix and stopped production state

A Git bundle created a named-branch disposable clone at exact final code OID
`5bbfad5fc46975b6f3de89744a4cd89ab671e201`. From `2026-08-10T04:24:52Z` through `04:44:09Z`,
the canonical 17 top-level commands ran in order and returned:

```text
BASELINE_COMMIT=5bbfad5fc46975b6f3de89744a4cd89ab671e201
BASELINE_BRANCH=review/hermes-phase8-pause-resync
BASELINE_SUITES=17
BASELINE_FAILED_SUITES=0
phase1-control/phase1-audit through phase8-control/phase8-audit: every RC=0
MATRIX_TOTAL=17 MATRIX_PASSED=17 MATRIX_FAILED=0
MATRIX_RESULT=PASS
```

Final matrix log SHA-256 is
`06e84c32a2a085b2e89433bbcbf1712f49e61c740f0b7ce8f7e918512dfb7faf`.
All authoring and evidence artifacts are retained at root-owned mode `0700` path:

```text
/var/backups/recursion/phase8-pause-resync-authoring-20260810T034342Z-52f6ad3
evidence manifest SHA-256=62ec571b6d750d6ebbd9614ab0729ff0bec8f8e8c9d1c88775d5b2b7021a0872
```

The pre-matrix snapshot at `03:54:07Z` and post-matrix snapshot at `04:47:21Z` match:

```text
HEAD=30fb73f56496d1d8e72f00dceb4b59de4bd21a00
MASTER=ORIGIN_MASTER=be3cb8b6462bc6bbe2ba12305e6bee47ee102fc8
TRACKED_DIRTY=false; STATUS_COUNT=1 (pre-existing ignored backup only)
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
JOURNAL=c0a03878fc98df2bb6e6a83496d003dd198e53a8477cb004af6a3909f5b103a0
PENDING=883d9f53e2d7263d8ffa733fe61bff959cf1b7e341d4a293d0a9b00f8e0c6684
GATEWAY=active/running PID 2781295
CRON=active
ACTUATOR=not-found/inactive
WORKERS=0
PROJECT_CONFIGS=9 unchanged by this authoring session
OUTBOXES=9 paused=true events=0 deliveries=0
```

No production file, service, flag, capacity, model route, notification destination, action, event,
delivery, project checkout, PAUSE state, or pilot was changed. The review branch must be reviewed,
merged, and pruned before a fresh full PAUSE-held production correction can be proposed. That
production action still requires separate Lucas authorization; actuator deployment and all pilot
windows remain blocked.

## Phase 8 merged PAUSE/resync production correction

Lucas reported the PAUSE/resync branch merged and pruned, then separately authorized the exact
PAUSE-held correction window after its complete preflight was presented. No actuator deployment,
PAUSE removal, notification, configuration/capacity change, product-repository action, pilot, or
soak was authorized.

### Merge, protection, and live starting gates

Fresh workstation fetch/prune and ancestry checks returned:

```text
origin/master=f7df0d0e8ba7cbfc4109581cfbb62ed37b54813a
PR #35 merged=true head=76cef9aa32476493989de54cc90b51d26d51abeb
merge_commit=f7df0d0e8ba7cbfc4109581cfbb62ed37b54813a
2688671a80e7a69929f0b1e137e43485196d72ac ancestor: yes
1a16d81c396dde5fbcc87d9ac80cca3017fc3bf2 ancestor: yes
5bbfad5fc46975b6f3de89744a4cd89ab671e201 ancestor: yes
76cef9aa32476493989de54cc90b51d26d51abeb ancestor: yes
origin/review/hermes-phase8-pause-resync absent
dirty workstation master=20c1fa77042f3aa9f5d8978352792f3b058bc0d1 (20 entries, untouched)
```

The authenticated GitHub settings page showed classic protection rule `81491567` applying to
`master`: pull request required, conversation resolution required, no administrator bypass, force
push disabled, and deletion disabled. The GitHub connector independently reported PR #35 closed
and merged. No setting changed. Host-side authenticated `ls-remote` using the registered Recursion
deploy key returned exact `f7df0d0... refs/heads/master`.

The initial live capture at `2026-08-10T04:58:17Z` and final pre-authorization capture proved:

```text
HEAD=30fb73f56496d1d8e72f00dceb4b59de4bd21a00
BRANCH=loop/auto
MASTER=ORIGIN_MASTER=be3cb8b6462bc6bbe2ba12305e6bee47ee102fc8
TRACKED_DIRTY=false
STATUS=?? .env.bak.1786014440  # pre-existing; not read or changed
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
JOURNAL=c0a03878fc98df2bb6e6a83496d003dd198e53a8477cb004af6a3909f5b103a0
PENDING=883d9f53e2d7263d8ffa733fe61bff959cf1b7e341d4a293d0a9b00f8e0c6684
GATEWAY=active/running PID 2781295
CRON=active/running
ACTUATOR=not-found/inactive
WORKERS=0; PROJECT_PROCESSES=0
```

### Fresh exact merged preflight

The root-owned mode-`0700` bundle was created outside `/opt/recursion` at:

```text
/var/backups/recursion/phase8-pause-resync-preflight-20260810T050140Z-f7df0d0
source bundle SHA-256=77f930275eadcc21ded63dd7f5d05c829dfd12c2629eb78acbf95f146f12ae33
```

Exact merged master was checked out on named branch `merged-master`. The retained preflight was
launched as:

```bash
nohup bash \
  /var/backups/recursion/phase8-pause-resync-preflight-20260810T050140Z-f7df0d0/evidence/phase8-resync-preflight.sh \
  /var/backups/recursion/phase8-pause-resync-preflight-20260810T050140Z-f7df0d0 \
  > /var/backups/recursion/phase8-pause-resync-preflight-20260810T050140Z-f7df0d0/evidence/preflight-launcher.log \
  2>&1 < /dev/null &
```

From `2026-08-10T05:03:54Z` through `05:23:33Z`, it passed exact PAUSE and Git state; merged
ancestry/artifacts/hashes; GitHub merge and protection; hardened gateway and inert execution
services; exact crontab; zero model/global/per-project capacity; nine project/pilot configurations;
nine paused empty outboxes; the sole exact pending PAUSE-safe no-op; and retained Phase 1 and
production backup/restore evidence. The complete command matrix ended:

```text
BASELINE_SUITES=17
BASELINE_FAILED_SUITES=0
phase0-baseline RC=0
phase1-control/phase1-audit through phase8-control/phase8-audit: every RC=0
live-phase1 RC=0
PREFLIGHT_RESULT=PASS
```

A disposable merge simulation used a new full live Git bundle and exact source bundle. Attempt 1
was retained as negative evidence: `git bundle verify` was invoked without a repository context
and stopped before cloning or mutation. The corrected command used `git -C /opt/recursion bundle
verify`, then proved a conflict-free merge with exact parents `30fb73f... f7df0d0...`, reviewed
`deploy/`, `schemas/`, `scripts/`, and `loop/` bytes, exact cycle/resync hashes, and removal of the
disposable checkout. No production ref or file changed during either simulation attempt.

The retained script/plan SHA-256 values are:

```text
506e7c1a6b6313f5d8d8d880f4d5904198188015a10425898d391c8093cb56d0  phase8-resync-preflight.sh
dc9464b0bd3f30b2a402e709db771230adccccafd2ecdc158f9a01176ed5e6a2  phase8-resync-correction.sh
41b1bc97590ba53c3aa800f919ff22edad992aba73ab31c2c508782dc52607bc  independent-verify.sh
4512579accf6a3ec1d281bad2ca2a84019cd424007d3e57f3147ff11ff75e1ee  merge-simulation.sh
f2a5b9709b9001bee534cf6ef25501b1c5ea698c45947f4e45b8055e641ac561  github-protection-and-merge.txt
819ffc9f8e0c9798a91551a4c16715370495e7f7390c2357f5c259dacdfff4be  correction-plan.txt
```

### Authorized correction window and armed rollback

Lucas authorized exactly: stop/restart cron only; leave the gateway running; create and verify
runtime/Git snapshots; compare-and-swap `master` and `origin/master`; merge exact reviewed master
into `loop/auto`; automatically roll back on mismatch; and do not run cycle/resync, deploy the
actuator, lift PAUSE, notify, configure capacity/models, touch product repositories, or start a
pilot.

The transaction command was:

```bash
nohup bash \
  /var/backups/recursion/phase8-pause-resync-preflight-20260810T050140Z-f7df0d0/evidence/phase8-resync-correction.sh \
  /var/backups/recursion/phase8-pause-resync-preflight-20260810T050140Z-f7df0d0 \
  > /var/backups/recursion/phase8-pause-resync-preflight-20260810T050140Z-f7df0d0/evidence/correction-launcher.log \
  2>&1 < /dev/null &
```

It rechecked every inexpensive gate and authenticated origin before service mutation. At
`2026-08-10T06:05:28Z` it stopped only cron, acquired the existing control lock, and proved no
scheduled/project/worker process survived. The gateway remained PID `2781295`. It created:

```text
runtime-before/manifest.json SHA-256=3ff392ff60fcaa6b6f60c384d38b56ac300317995eb7ac230b44a3775f000b24
runtime files=19
live-git-before.bundle SHA-256=9b95364ee48382fe1dd9b97419f4fca251e7f3df21921ab43cd4a21f3c1d22ff
```

The runtime snapshot was restored through the canonical `runtime-state.py` into a validated
disposable `/var/tmp/recursion-phase8-resync-restore-*` clone, every manifest file verified, and
only that staging directory was removed. Before importing reviewed objects, the EXIT trap was
armed to abort a merge, reset only `loop/auto` to `30fb73f...`, restore local and remote-tracking
master to `be3cb8b...`, delete the temporary ref, restore `runtime-before`, restart cron, and verify
the exact pre-window PAUSE/journal/pending/service/process/status state.

All gates passed, so rollback did not execute. Compare-and-swap advanced both master refs and the
reviewed merge committed at `06:05:30Z`:

```text
HEAD=599cbbdbf6ef538e3c305c61c1affc16320b2244
PARENTS=30fb73f56496d1d8e72f00dceb4b59de4bd21a00 f7df0d0e8ba7cbfc4109581cfbb62ed37b54813a
SUBJECT=Merge branch 'master' into loop/auto
MASTER=ORIGIN_MASTER=f7df0d0e8ba7cbfc4109581cfbb62ed37b54813a
FINAL_PRESTOP_GATE=PASS
QUIESCED_GATE=PASS
SNAPSHOT_AND_STAGING_RESTORE=PASS
REVIEWED_CORRECTION_GATE=PASS
CORRECTION_RESULT=PASS
```

No `cycle.sh`, `resync.sh`, heartbeat, decision, actuator, worker, outbox delivery, notifier, or
pilot command ran in the window. Cron restarted active; the gateway was never stopped or restarted.

### Independent closure and final sealed evidence

The independent verifier ran from `2026-08-10T06:07:15Z` through `06:07:19Z`. It reverified the
complete pre-window Git bundle; exact deployed merge and parents; reviewed runtime-tree equality;
exact cycle/resync hashes; all 19 live runtime files against snapshot hashes, sizes, modes, owners;
the live Phase 1 audit; authenticated remote master; exact crontab/configuration; nine paused empty
outboxes; zero action/event/delivery/report/transport files; and services/processes. It ended:

```text
PASS retained-snapshots
PASS exact-reviewed-deployment
PASS runtime-state-zero-delta
PASS services-correct
PASS config-crontab-outboxes-unchanged
PASS authenticated-origin-and-live-phase1
INDEPENDENT_VERIFY=PASS
```

Final production state is:

```text
HEAD=599cbbdbf6ef538e3c305c61c1affc16320b2244
BRANCH=loop/auto
MASTER=ORIGIN_MASTER=f7df0d0e8ba7cbfc4109581cfbb62ed37b54813a
TRACKED_DIRTY=false; STATUS=?? .env.bak.1786014440
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
JOURNAL=c0a03878fc98df2bb6e6a83496d003dd198e53a8477cb004af6a3909f5b103a0
PENDING=883d9f53e2d7263d8ffa733fe61bff959cf1b7e341d4a293d0a9b00f8e0c6684
GATEWAY=active/running PID 2781295
CRON=active/running PID 3421317
ACTUATOR=not-found/inactive
WORKERS=0; PROJECT_PROCESSES=0
PROJECT_CONFIGS=9 unchanged
MODEL/CAPACITY=unset/zero
OUTBOXES=9 paused=true events=0 deliveries=0
```

The root-owned `0700` bundle retained all positive and negative logs, scripts, Git bundles,
runtime snapshot, protection evidence, commands, and final state at
`/var/backups/recursion/phase8-pause-resync-preflight-20260810T050140Z-f7df0d0`. Its sealed
108-entry `evidence/FINAL-MANIFEST.sha256` verifies; the manifest file SHA-256 is
`cc3c6ae035b2d1cab9bc5fe61932dcb74860da56b7c01b198f842e277dd57ba0`.

This closes the PAUSE/resync production correction only. Phase 8 actuator deployment remains
absent; PAUSE remains asserted; model/capacity configuration, notifications, all four pilots, the
soak, promotion, and evidence-branch push/merge/prune remain separately gated.

## Phase 8 forced-cycle notification incident, containment, and control patch

### Merge gate and failed actuator preflight

Lucas reported PR #36 reviewed, merged, and pruned. Fresh Git and authenticated GitHub evidence
proved:

```text
PR #36 merged=true at 2026-08-10T06:18:29Z
PR head=465f8964fbffa113c239d7b337179369a9f8be13
origin/master=7e6e21f41c3da8914322ac352fabcd4bf95b38a4
merge-base --is-ancestor 465f896... origin/master RC=0
origin/review/hermes-phase8-pause-resync-production-evidence absent after fetch --prune
```

The authenticated protection response still required pull requests and conversation resolution,
included administrators, and disabled force pushes and deletion. A new clean authoring worktree
was created from exact `7e6e21f...`; the user-owned dirty master stayed exact `20c1fa7...` and was
not touched.

The fresh actuator preflight used source bundle SHA-256
`40377daf0d3ee4dd04864183ae9eac74dbf9aa491cf6508c44f9fe238946ddbc` and script SHA-256
`926e2cf9050d41d0a73bc26bfde01643a47a08c91f3a57952cd28f8b822efffe`:

```bash
nohup bash \
  /var/backups/recursion/phase8-actuator-final-preflight-20260810T062541Z-7e6e21f/evidence/phase8-actuator-preflight.sh \
  /var/backups/recursion/phase8-actuator-final-preflight-20260810T062541Z-7e6e21f \
  > /var/backups/recursion/phase8-actuator-final-preflight-20260810T062541Z-7e6e21f/evidence/preflight-launcher.log \
  2>&1 < /dev/null &
```

From `06:26:36Z` through `06:46:27Z`, all inexpensive live gates, the 17/17 baseline, every
Phase 1-8 control/audit pair, and live Phase 1 returned zero. The final immutable-live-state check
then stopped before any simulation, backup mutation, unit installation, or deployment:

```text
PREFLIGHT_FAIL=live-head-changed-after-controls
```

The retained cron journal and Git chain explain the mismatch exactly:

```text
06:30:01Z root cron: cd /opt/recursion && RECURSION_ROOT=/opt/recursion bash scripts/cycle.sh force
e1420d24e94595f109023768e48e48e5240bb75d  pre-window HEAD
724df09e9f2fffbff652f2d96ab234ad7a6551af  loop: morning 2026-08-10
4db7432bcdad98f7668514d71c5cbb5aab029fe7  loop: board 2026-08-10 06:31Z
06:31:50Z cron session closed
```

The forced branch set `DELIVER_BRIEF=1` under PAUSE and invoked `digest.sh --all` without a PAUSE
guard. `run.sh` committed state, appended the branch tracker to the already committed brief, and
called legacy `notify.sh`; `digest.sh` called the same legacy sender with `|| true` and printed
`posted` regardless of transport truth. The log contained one `brief done (deliver=1)` and eight
`posted digest:` claims. All nine Phase 7 outboxes still reported `paused=true`, `events=0`, and
`deliveries={}`; the search for notification-event, delivery, and transport-evidence files was
empty. The exact conclusion is nine unauthorized legacy attempts/claims and **zero durable
acknowledgements**. It is not evidence that nine messages were delivered.

The incident capture retained refs, reflog, commits, tracked diff, cron/cycle windows, service and
process state, source excerpts, all outbox states, and the durable-file search. Its manifest
SHA-256 is `8ff1d25e3a00065093410955b13906b4336b7d1ccb34c3d2f31dd5b11854edf4`. The parent
root-owned mode-`0700` bundle is
`/var/backups/recursion/phase8-actuator-final-preflight-20260810T062541Z-7e6e21f`; its final
303-entry manifest SHA-256 is
`78baebaabc890181bac9c9a818058f8f589837c7e7f277767f751f76d2693dfe`.

### Authorized cron containment

Lucas authorized temporary cron containment and the reviewable patch. The containment script
captured the current refs/status, crontab, cron journal, PAUSE, gateway/cron/actuator services,
worker units, and project processes; acquired `/run/recursion-control.lock`; rechecked zero
cycle/resync/project work; armed an EXIT rollback; then executed only:

```bash
systemctl stop cron.service
```

Postconditions at `2026-08-10T17:23:51Z` were:

```text
HEAD=bcd65ebcc7c1af3f56be1bb3b7530a837793eaf6 (loop/auto)
MASTER=f7df0d0e8ba7cbfc4109581cfbb62ed37b54813a
ORIGIN_MASTER=7e6e21f41c3da8914322ac352fabcd4bf95b38a4
STATUS=?? .env.bak.1786014440
PAUSE=919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
GATEWAY=active PID 2781295
CRON=inactive
ACTUATOR=not-found/inactive
WORKER_UNITS=0; PROJECT_PROCESSES=0
CONTAINMENT_RESULT=PASS
```

The state was identical before and after except for cron's intended active-to-inactive
transition. Cron had run silent monitoring cycles at 09:15, 12:15, and 15:15 before containment;
their ledger commits were preserved rather than rewritten. Rollback did not fire. The retained
one-command reversal is:

```bash
systemctl start cron.service
test "$(systemctl is-active cron.service)" = active
test "$(systemctl is-active hermes-gateway.service)" = active
test "$(sha256sum /opt/recursion/state/PAUSE | cut -d' ' -f1)" = \
  919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db
```

The root-owned mode-`0700` evidence directory is
`/var/backups/recursion/phase8-cron-containment-20260810T172243Z`; its manifest verifies and has
SHA-256 `958738a0a5b6c687b20cd802d041e53ac5746848cf8f21e2dde06671df6a0a90`.

### Red controls and canonical-path implementation

The patch branch was created from exact `origin/master` `7e6e21f...`. The red commits are:

```text
8aa553f47c8001e8d283cc061b3b52125cd64c45  Add red controls for PAUSE notification isolation
b9037dd8317ffc623f0c0d61748b4301c3ea550a  Add red control for monitored outbox events
```

In an exact-base disposable Linux clone, they failed non-vacuously:

```text
pause-semantics RC=1
  two PAUSE-held forced-path transport calls
  one PAUSE-held escalation transport call
  tracked state/.board.tsv and brief residue
  resumed direct sends with no canonical brief/digest events
telegram-routing RC=1
  sole notify.sh invoked transport under PAUSE and returned zero
phase8-notification RC=1
  no canonical notify-event command, monitored mapping, policy delivery, or receipt
```

Implementation `f180fc7129c9feb3bd094a55336122f0d251a695` extends proven paths only: `taskctl.sh`
remains the canonical event writer; `state/events.jsonl` remains the source; the existing Phase 7
scheduler emits schema-v1 decisions; the existing decision/action/conductor/sole-actuator path
mutates the outbox; and durable-mode `notify.sh` remains the sole sender. There is no new scheduler,
executor, store, publisher, delivery queue, or transport. The implementation adds stable
`portfolio-brief` and `project-digest` source identities, exact message digests, policy routing to
Head Hermes/project topic, and central PAUSE refusal before either transport. It suppresses paused
source enqueue rather than replaying stale daily messages and commits complete brief/board/event
state in order.

Focused post-implementation commands in disposable staging were:

```bash
bash -n scripts/notify.sh scripts/taskctl.sh scripts/digest.sh scripts/cycle.sh loop/supervisor/run.sh
python3 -m py_compile scripts/notification-outbox.py scripts/tests/hermes-pilot-phase8-notification-red.py
bash scripts/tests/pause-semantics.test.sh
bash scripts/tests/telegram-routing.test.sh
python3 scripts/tests/hermes-pilot-phase8-notification-red.py
bash scripts/audit/hermes-pilot-phase8-green.sh
```

Every command returned zero. The controls prove no paused transport or stale enqueue, exact
idempotent source identity, malformed-input refusal, resumed canonical routing, two exact durable
transport receipts in the inert fixture, and clean tracked state after paused and resumed forced
cycles. Governance commit `2e5c3b3b5e8e31619cfd60ae23104c2b79458d81` adds D-014 and updates
`CONTROL_PLANE.md`/`docs/access.md`, explicitly preserving why D-012 originally kept monitoring
alive while correcting the external-delivery boundary its falsifier exposed.

No patch byte is deployed. Cron remains stopped, gateway PID `2781295` remains live, PAUSE is
exact, the actuator is absent, workers are zero, production history is preserved, and no human
notification, product-repository action, pilot, or soak was performed by containment or patch
testing.

### Exact full-matrix closure and sealed patch evidence

The complete Linux matrix ran against exact implementation commit
`f180fc7129c9feb3bd094a55336122f0d251a695` from `2026-08-10T17:42:02Z` through
`18:00:47Z`. The source worktree was clean before and after. The retained result is:

```text
BASELINE_SUITES=17
BASELINE_FAILED_SUITES=0
phase1-control RC=0; phase1-audit RC=0
phase2-control RC=0; phase2-audit RC=0
phase3-control RC=0; phase3-audit RC=0
phase4-control RC=0; phase4-audit RC=0
phase5-control RC=0; phase5-audit RC=0
phase6-control RC=0; phase6-audit RC=0
phase7-control RC=0; phase7-audit RC=0
phase8-control RC=0; phase8-notification-control RC=0; phase8-audit RC=0
live-phase1 RC=0
immutable-source-and-live-state RC=0
FULL_MATRIX_FAILURES=0
```

The immutable gate proved the live state was identical before and after: `loop/auto` remained
`bcd65ebcc7c1af3f56be1bb3b7530a837793eaf6`; local `master` remained `f7df0d0...`;
`origin/master` remained `7e6e21f...`; the only status entry remained the pre-existing
`.env.bak.1786014440`; PAUSE retained SHA-256 `91937183...`; the gateway remained active at PID
`2781295`; cron remained deliberately inactive; the actuator remained absent; and worker/project
process counts remained zero. Runtime/configuration hashes, all outbox states, and all delivery
files also matched before and after. The inner full-matrix manifest verifies and has SHA-256
`612753113835040ba42d1c7fee637da376fdb1a1066b22cbd85b4096670e7f41`.

The exact tested source is retained as a complete Git bundle with SHA-256
`8beb15d12df656be50e3dec574d6f0dbd5ecbb5d8db24ca65d5f7339c2ddd2c7`; `git bundle
verify` reports exact ref `f180fc7129c9feb3bd094a55336122f0d251a695` and complete history. All
disposable red/green clones, upload bundles, and runners under `/var/tmp` were removed after the
logs and source bundle were retained. The final root-owned mode-`0700` evidence directory is
`/var/backups/recursion/phase8-pause-notify-patch-20260810T172243Z`; every entry in its final
manifest verifies, and the manifest file SHA-256 is
`1a6faf799cfa4fd672a904ee8bfb7187f15b2058261f615e3c957c8c0be6c561`.

This closes authoring evidence only. The patch, actuator, and pilots remain undeployed. Cron is
still held inactive as explicit containment; restarting it is the retained one-command rollback,
not an implicit post-test action.

## August 11 Phase 8 durability hardening evidence

The continuation recovered the complete hardening worktree without repository or production-state
loss. A synthetic commit was created solely to make the dirty candidate portable and exact:

```text
commit 161977533aa07b14b197910339cc90c0d9aa0709
tree   9c7551a7feff83bf728ac2d281184c3917556fc7
bundle 63677b473eb8be3cdcb603253927c0719ae0583ac029deddfc64f59152b8730a
```

The patch adds exact runtime restore and crash recovery; PID/start-witnessed atomic directory
locks with writer/maintenance interlocks; bounded sharded transition-receipt compaction and event
lookup; semantic reconciliation identities; a rebuildable durable verification index for
authoritative JSONL journals; bounded consumer checkpoints; and claim-time journal-authority
validation. Test fixtures were also made hermetic by copying the newly required runtime helpers
and excluding pre-existing ignored recovery/index state from fixture seeds.

Every positive phase audit was rerun in its own fresh Git-object clone of that exact commit. The
final Linux result was:

```text
phase1-green RC=0
phase2-green RC=0
phase3-green RC=0
phase4-green RC=0
phase5-green RC=0
phase6-green RC=0
phase7-green RC=0
phase8-green RC=0
BASELINE_SUITES=19
BASELINE_FAILED_SUITES=0
```

The four principal retained logs are independently hashed:

```text
phase6-audit  3785bb79ba4b81ea7ca37a5c126b9bf4ae424e29e2e5772fee8ef7978ad12161
phase7-audit  8de9bfd4d8b6439541d615fac287967a1d4a43601da1aea54a459992b3c50bce
phase8-audit  3abb50c353b270666ca7aef838808641270b9522b9e59bb7bcfb1a4b466ec4e4
phase0-base   5191eeb82c3f0d57fe8de52633d35034ced3223686e5bd3543f8c9947fe20148
```

The original apparent Phase 6 hang was resolved as slow bounded verification: all 20 scenarios
completed, including the runtime-rotation race, and no audit process survived. Separately, the
recurrent local Codex termination was traced to a Windows-only liveness test using
`os.kill(pid, 0)`, which is not a read-only existence probe on Windows. Replacing it with Win32
process-query APIs removed that destabilizing path; the earlier oversized session termination is
still best explained by client context pressure rather than project code or state corruption.

Before and after all disposable remote tests, production PAUSE SHA-256 was
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`; the gateway was active,
cron inactive, the actuator inactive/absent, and no production worker or pilot was launched. No
production file, ref, service, configuration, notification, capacity/model setting, PAUSE state,
or pilot window changed. The evidence closes local hardening authoring only and grants no
deployment or resume authority.
