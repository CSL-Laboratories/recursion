# INBOX — questions and escalations requiring a human decision

(reset 2026-08-12 for first production launch on Lucas's instruction; pre-Hermes content archived at /var/backups/recursion/pre-launch-scrape-20260812T075324Z)

## [esc-0002] 2026-08-29 — May a v2 acceptance gate have side effects?
**Workstream:** recursion-v2 execution fabric
**Pointer:** `scripts/validate-gate.py` (Phase 2, `codex/v2-adapters` @ `33fb08c`);
`scripts/tests/hermes-pilot-phase5-red.test.sh:411` (Phase 5, `codex/v2-cursor-runtime`);
measurement `tmp/integ-p2c` = `8056ace`.
**Blocked on:** a decision on whether an acceptance gate may mutate state, which is not recorded in
`DECISIONS.md`. D-009 governs the native-diff gate and does not speak to this.

**Context.** Phase 2 closed a reviewed blocker by deleting four non-strict relaxations, so direct commands
now run one validator and one grammar in both modes. That closure is correct on its own terms — the same
relaxations were admitting `bash -c true`, `python3 -c true`, `env test -s app/a` and `../outside.sh`.

It also refuses one acceptance command that bare WIP accepted. Phase 5's heavy-contention arm declares:

```
mktask heavy-contention "test -s app/heavy.txt && touch $HEAVY_GATE_MARKER"
```

Measured, each validator run inside its own checkout:

```
bare WIP a84525d   default (non-strict)   rc=0  ACCEPTED
bare WIP a84525d   --strict-direct        rc=2  refused
Phase 2  33fb08c   --legacy-compound      rc=2  refused
Phase 2  33fb08c   --strict-direct        rc=2  refused
```

`touch` was never in the strict grammar; the non-strict relaxation is what admitted it. Consequence at
`tmp/integ-p2c`: `phase5-red` and `phase5-green` each fail one assertion, `contended heavy gate did not fail
closed (rc=0 status=escalated gate=held)`. Serial repeat, n=10: `pass=0 fail=10`. Deterministic, not a race.
Phase 2 is otherwise 10/12 with all three of its own suites green.

**Why this is not mine to choose.** The command is not vacuous — its decisive element is `test -s
app/heavy.txt`, and the `touch` is the harness's probe for whether the gate body executed. So the vacuity
argument that justifies refusing bare `touch` does not reach this compound. Deciding it sets the contract for
what an acceptance gate *is*, which is the central safety mechanism of the fabric. I already made one wrong
unilateral call on this exact surface: I required `test -s app/result.txt && touch C:/phase5-marker` to keep
passing, a writer widened the grammar to obey, and bare `touch` became an admissible gate. I am not
repeating that.

**Options.**
- **A — gates are predicates.** Keep the refusal. Phase 5's fixture stops using the gate as instrumentation
  and observes execution another way. Cleanest contract; requires editing another phase's red fixture.
- **B — constrained mutation.** Re-admit `touch` only as a non-leading element of a compound whose leading
  element is a real test, operand required and confined to declared paths, never absolute. Phase 5's marker
  moves inside the workspace. Preserves both intents; re-opens a side-effect surface.
- **C — leave it.** Phase 2 and Phase 5 cannot both be accepted, and the ordered integration stalls at
  step 2.

**Recommendation: B**, because it satisfies the vacuity argument in full (bare and absolute `touch` stay
refused) without asserting a gate-contract change that nothing in the control plane records. But B still
touches both phases, so it wants a human yes.

**Not blocking other work.** Phases 3, 4, 5, 6/7 and 8 continue; this surfaces only when Phase 2 and Phase 5
integrate together.

### Update 2026-08-29 — independent confirmation, and one misleading label

An independent adjudicator was asked the bounded question "is this a Phase 2 defect or the gate
contract?", explicitly forbidden from resolving this escalation. It returned the contract reading,
with evidence I then re-verified myself.

```
base a84525d and 33fb08c^   `test -s app/heavy.txt && touch <marker>`   exit 0
33fb08c                     same command    exit 2  ERR bare acceptance launcher is not admitted: touch
base a84525d and 33fb08c    `test -s app/heavy.txt`  (pure predicate)   exit 0, identical acceptance_sha256
git diff 33fb08c^ 33fb08c -- scripts/work.sh scripts/capacity.sh   -> no heavy-lock change
```

**The failure line is misleading and should not be read literally.** The fixture prints

```
gate=$([ -e "$HEAVY_GATE_MARKER" ] && echo ran || echo held)
```

so `gate=held` means the marker was NOT created, which is the desired outcome. It says nothing
about the capacity lock. In the observed failure `rc=0 status=escalated gate=held`, only `rc` and
`status` are wrong: validation refuses the gate terminally before a worktree is leased, so
contention is never reached. Phase 2's contention path is untouched and sound.

This does not change the options or the recommendation. It removes the possibility that B is
being recommended to paper over a real fail-closed defect: there is no such defect.

Still needs a human yes. Phase 5 is now at 15/15 with its race suite at 10/10, so A and B both
cost real rework on an otherwise-clean phase.

### Update 2026-08-29 (late) â€” the discriminating arm ran, and it changes what option A costs

The experiment the previous update marked pending has now executed on the Linux lane. Same
fixture, same ref, everything identical except the one gate command:

```
P2PURE ref=origin/tmp/integ-p2c sha=8056ace
--- arm A: unmodified fixture ---
    gate: mktask heavy-contention "test -s app/heavy.txt && touch $HEAVY_GATE_MARKER"
    FAIL  contended heavy gate did not fail closed (rc=0 status=escalated gate=held)
--- arm B: pure predicate, everything else identical ---
    gate: mktask heavy-contention "test -s app/heavy.txt"
    ok    contended heavy gate defers without executing unlocked
```

This confirms the contract reading by execution rather than by argument: with a pure predicate,
Phase 2's heavy-contention deferral works. The gate command is the whole difference.

**The causal chain, stated exactly.** Arm A does not fail on the marker. It fails on the other
three conditions in the assertion:

```
[ "$heavy_rc" = 75 ] && [ status = running ] && [ -d worktree ] && [ ! -e "$HEAVY_GATE_MARKER" ]
        arm A: rc=0, status=escalated      -> two conditions false
        arm B: rc=75, status=running       -> all true
```

Phase 2 refuses the side-effecting gate at admission, so the task escalates instead of deferring,
and the deferral behaviour never gets a chance to be observed. The marker was never the issue.

**What this newly reveals, and it argues against a naive option A.** Under a pure predicate,
nothing in the fixture creates `$HEAVY_GATE_MARKER`, so `[ ! -e "$HEAVY_GATE_MARKER" ]` is
trivially true. Arm B passes partly on a condition that can no longer fail. Deleting the `touch`
therefore does not preserve the assertion â€” it silently converts the non-execution witness into a
tautology, which is the fabricated-value vacuity form this effort exists to eliminate.

So option A is not "edit one fixture line". It is "re-engineer how the harness witnesses that a
contended gate body did not execute, without using the gate to instrument itself" â€” plausibly from
controller run state or the worker log rather than a gate-created file. That is real work in
another phase, and it is the kind of change that can quietly weaken a red suite if done casually.

**Recommendation unchanged: B**, and the new evidence raises my confidence. B keeps the witness
mechanism intact while satisfying the vacuity argument in full â€” bare `touch` and absolute paths
stay refused. Option A remains legitimate but now carries a visible cost that should be priced in
before it is chosen, not discovered afterward.

**Still not mine to decide.** The evidence now says clearly *what each option costs*. It does not
say which contract the fabric should have, and that is not recorded in `DECISIONS.md`. Phase 2
stays blocked and the ordered integration still stalls at step 2 until a human answers.

Everything else continues to move: Phase 6/7 is 15/15 with both mutation proofs holding, Phase 8's
race rates are 10/10 and 10/10, Phase 5's are 10/10 and 10/10, and Phases 3 and 4 have fresh
remediations under gate.


## [esc-0003] 2026-08-29 — Are non-Git controller deployments supported?
**Workstream:** recursion-v2 execution fabric
**Pointer:** `scripts/provider-proxy-service.py:24-33`, `scripts/provider-network.sh:21-25`,
`scripts/worker-sandbox.sh:824-831` (Phase 5, `codex/v2-cursor-runtime` @ `da5e687`).
**Blocked on:** whether a controller that is not a Git checkout is a supported deployment. Not
recorded in `DECISIONS.md`.
**Not blocking Phase 5.** Failing closed is the defensible default and the documented deployment
satisfies it. This wants a decision, not a hold.

**What changed and why.** An independent review found that the builtin provider path invoked
`provider-egress.py` with no controller provenance, so egress read the live registry and a
changed-but-valid registry chose a different namespace and proxy port at rc=0:

```
baseline rc=0  namespace=recursion-provider-anthropic  proxy_port=18080
mutated  rc=0  namespace=attacker-netns                proxy_port=1234
```

The fix derives provenance from the controller checkout when no resolver plan carries it, and
passes it unconditionally. A second independent review confirmed by execution that this works —
egress genuinely honours the pin rather than merely accepting it:

```
pinned baseline                       rc=0  namespace=pinned-a proxy_port=19080
live registry mutated, pin unchanged  rc=2  "provider profile registry differs from controller commit"
registry restored                     rc=0  committed namespace/port
```

**The cost.** Deriving provenance means running `git -C "$ROOT" rev-parse --verify HEAD`. Where
that cannot answer, the path now refuses at rc=96 instead of using the local registry:

```
non-Git controller root      rc=96   (parent resolved the local registry without Git)
git absent from PATH         rc=96
unborn HEAD                  rc=96
detached HEAD                works
shallow / grafted checkout   works when HEAD and the tracked registry object are present
```

**The question.** If a packaged, non-Git controller is a supported deployment, this is an
availability regression and needs a supported authenticated packaging path instead. If the
controller is always a Git checkout — the documented systemd service uses a root-owned
`/opt/recursion` — then failing closed is correct and this note only needs recording.

I am not choosing. Refusing to route provider traffic under unauthenticated policy is the right
default and I have left it in place, but "the previous behaviour was more available" is not
something I should silently trade away on a security seam.

**Related, recorded not escalated.** The same review notes the fix relocates trust rather than
eliminating it: `RECURSION_ROOT` is caller-selectable, so a party who controls the root controls
the commit and the registry together. Under the documented threat model — root-owned checkout,
`/etc/recursion/provider-egress.env`, unprivileged workers — that party is already privileged, so
the original live-registry drift is closed. Independently authenticating the root choice is a
larger change than this phase, and it is called out here so it is a known limit rather than an
assumption.

### esc-0002 RESOLVED 2026-08-29 by Lucas — option A, gates are pure predicates

Decision received: **an acceptance gate evaluates, it does not mutate.** Phase 2's refusal stands.

`state/DECISIONS.md` is a protected path and this executor does not write it. A formal D-entry
recording the gate contract is a human action and is still outstanding; this note is the record of
the decision as given, not a substitute for it.

**What changes.** Phase 5's heavy-contention arm stops using its own acceptance gate as
instrumentation:

```
before  mktask heavy-contention "test -s app/heavy.txt && touch $HEAVY_GATE_MARKER"
after   mktask heavy-contention "test -s app/heavy.txt"
```

**The witness must move, not disappear.** Under a pure predicate nothing creates
`$HEAVY_GATE_MARKER`, so `[ ! -e "$HEAVY_GATE_MARKER" ]` becomes trivially true and the
non-execution check silently becomes a tautology — the fabricated-value vacuity form. Deleting the
`touch` alone is NOT the fix.

The replacement already exists in the same fixture, twice. `heavy-retry` and `pause-after-terminal`
both witness execution from the **worker body**, which is not a gate and is not subject to the gate
grammar:

```
RETRY_LAUNCHES="$TMP/heavy-retry-launches"
run_case heavy-retry "n=$(wc -l < $RETRY_LAUNCHES 2>/dev/null || echo 0); n=$((n+1));
                      echo \"launch $n\" >> $RETRY_LAUNCHES; ..."
...
heavy_retry_launches="$(wc -l < "$RETRY_LAUNCHES" 2>/dev/null || echo 0)"
```

So the heavy-contention witness becomes a launch count from the worker body rather than a marker
written by the gate. That is arguably a stronger assertion: under contention the controller should
defer *before* launching the worker at all, so `launches=0` states the intended invariant more
directly than "the gate body did not run".

**Where the change lands: Phase 2's branch, not Phase 5's.** Phase 2 integrates first, and its
grammar change is what makes the current fixture unadmissible. If the fixture fix lived on Phase
5's branch the cumulative gate would fail at integration steps 2, 3 and 4 and only recover at step
5. The grammar change and the fixture that depends on it belong in the same commit.
`hermes-pilot-phase5-red.test.sh` is on the P3/P5 merge surface, so both later merges must carry
this forward — recorded as a post-merge assertion.

**This reverses my earlier recommendation, on evidence.** I recommended option B twice, on the
grounds that A required re-engineering the non-execution witness. That cost was real but I had not
checked whether the mechanism already existed. It does, in the same file, in two places. With the
cost gone, B's only advantage went with it — and B would have reopened the exact surface where I
previously made a wrong unilateral call, requiring `touch C:/phase5-marker` to keep passing until a
writer widened the grammar and bare `touch` became an admissible gate.

**esc-0003 remains open** and does not block: whether a packaged non-Git controller is a supported
deployment. Failing closed stays in place meanwhile.

## [esc-0012] 2026-08-30 — The Phase 5 merge left two assignment systems wired into each other

<!-- Recorded on 2026-08-30 as "esc-0005", which collided with an earlier escalation of that
     number at the top of this file. Renumbered to esc-0012; commit messages and EVIDENCE.md
     written before the collision was noticed still say esc-0005. -->


**Status:** not blocking; recorded because it is an architecture-level call made during integration,
not an ordinary defect fix. Read this before merging the remediation.

### What the merge produced

`scripts/work.sh` line counts against the phases' common ancestor `9c7a42c`:

| ref | lines | `make_phase5_assignment` | `resolve_run_assignment` |
|---|---|---|---|
| 9c7a42c | 1593 | 0 | 0 |
| tmp/integ-all (P2+P3+P4) | 1980 | 0 | 2 |
| codex/v2-cursor-runtime (P5) | 1958 | 2 | 0 |
| the merge | 2341 | **2** | **2** |

Phase 3 introduced `resolve_run_assignment`, which resolves engine, provider, model and capacity pool
through `scripts/assignment.py` and persists to `state/assignments/<id>/<digest>.json`. Phase 5
introduced `make_phase5_assignment`, which binds an immutable run contract through
`scripts/assignment_contract.py` and writes `state/assignments/<id>-assignment-<stamp>-<pid>.json`.

Neither branch has both. The merge has both, and they fight over the same two variables:

```
work.sh:1151   make_phase5_assignment   ASSIGNMENT_FILE=state/assignments/<id>-assignment-<stamp>-<pid>.json
work.sh:1302   resolve_run_assignment   ASSIGNMENT_DIGEST=""; ASSIGNMENT_FILE=""
work.sh:1444                            ASSIGNMENT_FILE=state/assignments/<id>/<digest>.json
work.sh:1518                            ASSIGNMENT_FILE=state/assignments/<id>/<digest>.json
```

The Phase 5 contract is written and then discarded. `worker-evidence` is subsequently handed Phase
3's record and validates it with `assignment_contract.load_assignment`, which requires
`controller_repository`, `controller_sha`, `network_policy`, `argv`, `gate_contract` and
`role_assignments` â€” fields the legacy record does not carry. It fails closed, so the symptom is 14
controls reporting `status=escalated` with no reason printed anywhere. The actual exception only
appears in work.sh's loop log:

```
CapacityPoolNotFound: capacity-pool manifest not found: .../scripts/capacity-pools/claude-max.json
```

Phase 3's resolver reads a **directory** of per-pool manifests that has never existed in this
repository; Phase 5 ships a single `scripts/capacity-pools.json`. Before the merge nothing set a pool
name, so Phase 3's capacity-pool path was never reached with a real pool. The merge is the first time
it is.

### Why this happened

My own merge brief caused it. It said "the resolution is almost always both, not either", which is
right for two independent features and wrong for two implementations of one responsibility. The same
brief listed "two implementations of one policy" as a hazard for the reviewers to catch, and they
did â€” reviewer D rejected the merge on exactly this point, and I confirmed it at the line level
rather than on their word.

### The resolution, and why I do not think it is a free choice

Reading (a): Phase 5's contract is authoritative for strict runs, and Phase 3's resolver feeds it the
engine/provider/model/pool decision.
Reading (b): Phase 3's record stays authoritative and Phase 5's contract is derived and stored at
Phase 3's path.

The rest of the merge already settles this. `worker-evidence.py`, `project-reconcile.py` and the
strict evidence path all consume `assignment_contract` in the merged tree, so under reading (b) every
one of those consumers fails â€” which is precisely the failure being observed. Reading (a) is the only
one that yields a working system without rewriting all of them.

I am proceeding on (a) rather than stopping, because the choice is forced by the merge rather than
open. **If that reasoning is wrong, this is the decision to reverse**, and the remediation is scoped
to `work.sh`, the capacity-pool lookup, and the attribution consumers rather than spread across the
tree.

### Related findings from the same review round

- `engine-scorecard.py` and `usage-reconcile.py` still use the legacy assignment lookup, so immutable
  attribution silently falls back to the mutable `run.engine` field.
- The engine-manifest filename-to-name identity binding present on the integration side is absent
  from the merge: neither side's version of that check survived.
- `resolve-engine.py` imports `provider_contract` before inserting its own script directory on
  `sys.path`, so suites invoking it from `scripts/tests` can abort before their first assertion.


## [esc-0013] 2026-08-30 — Which user runs the PAUSE writers, the offsite backup, and the remote adapters?

**Status:** open, human decision required. Blocks nothing in-tree today, because a separate defect
(the descriptor-rooted lock path, being repaired) currently refuses every acquisition on POSIX. It
will block the moment that repair lands.

**Workstream:** recursion-v2 execution fabric, Phase 6/7 integration
**Blocked on:** whether to introduce a shared group (or relax an ownership check) so two processes
that the deployment already runs as *different* users can share one lock.

> **Correction, same day.** This escalation first said the repository does not state which user each
> process runs as. That was wrong, and a reviewer found the evidence I had missed. `deploy/systemd/`
> states it plainly: `recursion-backup.service.example` is `User=root Group=root` running
> `backup-offsite.py`, and `hermes-actuator-state.service.example` is `User=recursion-actuator
> Group=recursion-actuator`. The documented deployment is therefore exactly the root-first case that
> fails. The question is narrower than I first wrote it, and sharper: the users are known, and
> nothing declares how they share `state/.locks`.

### What changed

Phase 6/7 routes `pause.sh` and `resume.sh` through `remote-pause.py`, which takes
`state/.locks/remote-state.lock` via `remote_backend.remote_state_lock`. That is the same lock the
remote adapters hold across an outbound send, which is the point: PAUSE and send get one
linearization. The integration then put `backup-offsite.py` on the same lock, retiring a second
lock (`pause-transfer`) that had exactly one holder left and therefore excluded nothing.

So three things now contend for one lock: the PAUSE writers, the remote adapters, and the offsite
backup.

### The measurement

`remote_backend` creates `state/.locks` mode 0700 owned by whoever gets there first. Run at
`codex/v2-claude-opus` (d1f0a06), where an unrelated defect does not mask the result:

```
root-first      uid=0    ACQUIRED  .locks uid=0    mode=0o700
                uid=1000 REFUSED   RemoteError: request-invalid: unsafe runtime storage directory
nonroot-first   uid=1000 ACQUIRED  .locks uid=1000 mode=0o700
                uid=0    ACQUIRED  .locks uid=1000 mode=0o700
```

The failure is **one-directional and ordering-dependent**. If a root process creates the lock chain
first, every non-root participant is locked out permanently — not degraded, refused. If a non-root
process creates it first, root can still acquire, because root ignores the mode bits.

`backup-offsite.py` carries explicit root-ownership policy (`os.geteuid() == 0` implies directories
must be root-owned), and the deployment confirms it: the backup unit runs as root, the state
actuator as `recursion-actuator`. So the root-first row above is not a hypothetical ordering — it is
the documented one.

What the deployment does *not* provide is a way for them to share a directory. `deploy/sysusers.d/
recursion.conf` declares four users, each with only its own like-named group; no group spans the
backup and the actuator. `deploy/tmpfiles.d/recursion.conf` declares four directories, every one
`0700` owned by a single user, and `state/.locks` is not among them at all — so its ownership and
mode are whatever the first process to create it happens to produce.

### Why this is not mine to decide

Three in-tree fixes are available and each encodes a different deployment assumption:

1. Add a shared group and declare `state/.locks` `2770` on it, in `sysusers.d` and `tmpfiles.d` —
   the shape the rest of the deployment already uses, but it means creating a group that spans a
   root service and a non-root one, which widens what the actuator can reach.
2. Accept a differently-owned-but-safe `.locks` — loosens a check that exists to refuse exactly this
   shape, and needs the argument for why that is safe here.
3. Run the offsite backup as `recursion-actuator` rather than root — but the unit is root today and
   the script's own policy requires root-owned directories when it *is* root, so this is a real
   change to the backup's trust model, not a config tweak.

Picking one silently would write a provisioning decision into a security check. Two reviewers
reached this independently; the first reported it as POSIX reasoning and said plainly that it had
not been executed, the second found the systemd units. It has now been executed and it reproduces,
so nothing here rests on inference.

### Attribution

This reproduces at `d1f0a06`, before the offsite backup joined the lock. It is inherited from Phase
6/7's decision to route the PAUSE writers through `remote_backend`, not introduced by the
integration. Unifying the backup onto the same lock adds a third participant to a coupling that was
already there and already broken in one direction.

## [esc-0014] 2026-08-31 — Publish the Recursion v2 MVP review branch

**Status:** open, human operator action required.

**Workstream:** Recursion v2 source-only MVP
**Task:** `recursion-v2-mvp`
**Review branch:** `loop/work/recursion-v2-mvp`
**Accepted implementation/certification:** `2adaa6f08c994c56df93ed9dfbcfecc17809f4ce`
**Docs-certified descendant:** `f4cb451f041cb0782658dd9f222f389687514fa9`

Lucas explicitly requested publication after the documentation refresh. The complete clean-branch
gate passed at the docs-certified descendant. The repository-approved publisher was then invoked;
it refused before any push:

```text
publish: REFUSED — repository is not active in state/portfolio.tsv:
/mnt/c/Users/Lucas/Documents/GitHub/recursion-sol-mvp
```

The local Windows worktree has no `.env`, and this machine has no `/opt/recursion` canonical
checkout. No remote review ref exists, no raw `git push` was used, and no registry, key, PAUSE,
credential, or autonomy setting was changed.

**Required operator decision/action:** choose the canonical publishing checkout. Either register
this exact Windows clone through the reviewed control-plane configuration, or place the exact
review branch in the already-registered `/opt/recursion` clone on the deployment box. Then enable
the existing publisher for that human-approved run, confirm its configured project key, invoke
`scripts/publish.sh`, and open the PR. Do not solve this by bypassing the registry or using a raw
push.
