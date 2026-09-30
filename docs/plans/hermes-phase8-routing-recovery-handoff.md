---
title: Hermes Phase 8 routing recovery handoff
type: handoff
status: review
last-verified: 2026-08-14
related:
  - CONTROL_PLANE.md
  - CLAUDE.md
  - docs/plans/hermes-project-pilot-phase8-handoff.md
  - docs/plans/hermes-phase8-preflight-operational-nuances.md
---
# Hermes Phase 8 routing recovery handoff

## Current authoritative closure — 2026-08-14T03:44:13Z

This section supersedes every older point-in-time state block below. Lucas merged PR #58 as
`61643959600a2f2bafc986b91bb6131d9fda12bd` and explicitly authorized the next deployment,
testing, and bug-fix pass. The reviewed tree is
`a9e7028347931cffeb7dab5b6e65233d26ec1209`. It was deployed under PAUSE through state
checkpoint `b336883a76dfb698d82deb36e09d696e76125b15` and merge commit
`0669455f213ba531bc94a01cc23c7a9677243b8e` on `loop/auto`; PR #58 is the merge commit's
second parent. Production's non-state diff from the reviewed target is empty, and both
`master` and `origin/master` resolve to the reviewed merge.

The PAUSE-held transaction verified a 515-file runtime snapshot and restore drill, rehearsed
the state checkpoint and merge in a disposable clone, and preserved the pre-existing,
user-owned `.env.bak.1786014440` by metadata without reading or changing it. Focused deployed-
commit routing/recovery passed from `2026-08-14T03:29:23Z` through `03:30:27Z`; complete Phase-8
red passed through `03:33:25Z`; and the complete positive audit reported
`hermes-pilot-phase8-green: ALL PASS` at `03:38:18Z`.

Read-only live readiness then returned exactly `rc=10` and deterministic routing-refusal
decision `decision-bb17f923260cd217b3297772` for instruction 707, with no queue mutation. The
controlled recovery stopped only the actuator to remove races, prepended a deny-model shim
outside the repository, and terminalized the records in required order:

- instruction 707 is `acknowledged` through succeeded, one-attempt action
  `action-80239787d6c200a34cebbc96` and source event
  `source-routing-refused-b9aa6b1b74de7c14dfc50bac`;
- instruction 732 is `acknowledged` through succeeded, one-attempt action
  `action-ee25ba5f4db1629c842773db` and source event
  `source-routing-refused-1d6a05b3199c65b41eb26c21`; and
- decisions `decision-bb17f923260cd217b3297772` and
  `decision-592ec7da52da9b4e8de658bd` are succeeded, with pending/running queues empty.

No model call occurred. Exact replay of both succeeded decisions produced no dispatch and no
duplicate action or source event. PAUSE-held notification scheduling returned `0`; no recovery
notification-event or delivery record exists yet. Instruction 618 remained byte-identical at
SHA-256 `6cc42f7ecb6aa98c8f62d3f6407d655e50f1c540556e1d9092abcf7d5025ed5c`, and plan, goal,
task, and pilot-window state remained byte-identical. All five services are active/enabled with
`NRestarts=0`; workers are zero. The only non-state status entry remains the known
`.env.bak.1786014440`; all other status entries are PAUSE-awareness/runtime state.

PAUSE remained `root:root`, mode `0644`, 189 bytes, at exact SHA-256
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db` before and after every
operation. The sealed 560-file evidence tree is
`/var/backups/recursion/pr58-deploy-20260814T032450Z`, whose `MANIFEST.sha256` root is
`39fb887be91a681ea998841b3f6e875ff6e869ff79abc5aa9e6d7833c8043131`. Program SHA-256 values
are transaction `33b3f543e2b907300234fac5ab171d9a777957e0d8f0e9968040714e26ea984b`, rollback
`1d99f0a0eb0db04239d77ca6062f78a2928bb588d36ce5ef4cc3609aa87822e4`, and recovery
`8397f72cebf74b1ad15f71f49448512d3a15cb4d7dac4f82918d689aa3ed51f6`.

The routing incident is closed. The next gate is a fresh operator-authored project-topic
instruction using the recommended wording below. Do not fabricate that instruction, run a
project-pilot decide turn before its intake route is verified, reuse v1 identities, or lift
PAUSE. The two authenticated refusal source events are expected to become two direct Lucas
attention records only after a future authorized PAUSE lift; include that expected reconciliation
in the Gate-6 observation plan.

## Superseded pre-PR58 state — 2026-08-14T03:00:23Z

This historical section superseded blocks older than it; the closure above now supersedes it.
PR #57 was merged at
`4aa66c2c0af84a425f696eb9df74a8ce3b28d98c` and, after Lucas's explicit authorization, was
deployed under PAUSE through state-preserving merge commit
`8d7338c7f7b2c8126f770bde48b6b89194fe931a` on `loop/auto`. Production's resulting tree is
`83a54364230c70cc98923f03e7d4f34864e74ed8`; its non-state diff from the reviewed target is
empty. The final fresh measurement found `hermes-gateway`, `hermes-actuator`, `cron`, and
`recursion-provider-network` active and enabled, zero unexpected non-state dirt, and PAUSE
exact before and after at
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`.

The successful deployment transaction is
`/var/backups/recursion/pr57-retry2-20260814T015700Z`. Its rollback program SHA-256 is
`1c244af81f8aafef3e73c881809b1fb07c7f8149828f68c69c1ad8b031432939`; its transaction
program SHA-256 is
`6616cf6c28c6627756c59bd7d6b099f53304e2c01c73d3b8a8f9153abc5fbb90`. The transaction
verified 512 snapshot/restore files, ran the rehearsal, created state checkpoint
`0add445008f0673b713554f492bfe1bf7cd91445`, and retained PAUSE. Fresh-clone deployed-commit
intake, conductor, routing/recovery, Phase-8 red, and Phase-8 green tests all passed. Earlier
attempts stopped safely and restored the exact pre-deploy patch; they exposed and corrected a
quiescence assumption that treated a deliberately stopped gateway only as `inactive` even
though systemd can report `failed`, plus the same assumption in the rollback program.

PR #57 cannot yet recover the live records because a third, later failed Head Hermes action was
created before deployment and was not part of its reviewed historical evidence:

- action `action-cbba158e3295fb82bdf6e06c`;
- exact action-file SHA-256
  `270478743ba73a2a54f30c1a918aaff9c5fe579b9015a0a015f45bbc96d4fa70`;
- exact open instruction-707 document SHA-256
  `402aa9aea90a4d9c7dbedfcaff0cd34b1388f8d52ddc72dd57b94983d048d677`; and
- guarded read-only admission exits `4` with
  `historical instruction 707 recovery annotations differ`.

The follow-up executable correction is on fresh branch
`review/phase8-routing-recovery-action3` at code commit
`f0df671f502182bb11e3755558ee72041517450a`, based directly on PR #57's merge. Its five
commits are the isolated action-3 red fixture (`1db10b5`), implementation (`7f4464f`), raw-byte
review hardening (`f7d19df`), historical-admission bypass red control (`852232b`), and its
fail-closed implementation (`f0df671`). It pins the exact third action, requires equality of the
complete 707 failed-noop action set, binds the initial instruction to its raw-byte digest, and
requires the exact three annotation pairs in order. Before returning ordinary no-mismatch
success, admission now binds the canonical filename to the header ID and validates every known
historical record. Negative controls cover the old two-action state, missing/tampered/extra
actions, wrong latest action, changed timestamp/evidence, duplicate or reordered annotations,
CRLF byte substitution, classifier removal, changed header identity, and a zero-padded
noncanonical identity.

The pre-implementation Linux proof showed the deployed two-action validator incorrectly
admitting the obsolete projection. The first independent review then found that classifier or
header-ID tampering could reach `admit()`'s old no-mismatch return before historical validation;
the isolated `852232b` red proof reproduced that fail-open with `rc=0`. On final commit
`f0df671`, the focused routing/recovery suite passed from `2026-08-14T02:51:34Z` through
`02:52:36Z`, the complete Phase-8 red suite passed through `02:55:40Z`, and the complete positive
audit reported `hermes-pilot-phase8-green: ALL PASS` at `03:00:23Z`. The final immutable bundle
SHA-256 is `0758546b8d874cc986c454a451d6e2479c7fd8411a28fd3d48494007915d5fa7`.
All tests used fresh disposable clones; production runtime records were not test inputs.
Evidence is under `/var/backups/recursion/action3-review-20260814T021400Z`; its 16-file
`MANIFEST.sha256` root is
`93ab5d516d525ee46204b7a25f7e399f54053e5d12146e9f1e83b6b0b211b66b`. OpenCode Go
`opencode-go/deepseek-v4-pro` and `opencode-go/deepseek-v4-flash` supplied bounded read-only
pre-implementation review. Final calls to both exact identifiers were blocked by provider
`Insufficient balance`, so a separate Codex adversarial reviewer was used for the final diff.
That reviewer found the CRLF-normalization issue and then the pre-validation no-mismatch bypass;
after both corrections, its exact-`f0df671` final re-review reported **no findings**.

The next gate is a review PR for Lucas. Do not deploy this branch or run live recovery until
Lucas merges it and the exact merged commit receives a fresh deployment authorization. After
that deployment, re-measure and byte-bind production, then use only the reviewed recovery path
to terminalize 707 before 732. Do not run another Head Hermes turn for 707 and do not manually
edit either record.

## Authority and stop condition

Lucas subsequently authorized PR #57 and PR #58 deployment/test passes plus the reviewed 707/732
recovery described above. Those operations are complete. There is no authorization to remove
PAUSE, change configuration, run another Head Hermes turn for 707, fabricate a new project-topic
instruction, or run a new project-pilot model turn before Lucas supplies and authorizes it.

The production control plane is `/opt/recursion` on `ssh gs-recursion`. Preserve these rules:

- never push, force-push, or merge a protected branch from a shell;
- make fixes on a review branch with red-to-green tests and adversarial review, then submit
  a PR for Lucas to merge;
- never edit `.env`, model selection, capacity, autonomy flags, cron, credentials, or auth;
- never manually edit/delete instructions, decisions, actions, plans, goals, tasks, windows,
  events, outboxes, or deliveries;
- instructions close only through succeeded coupled actions;
- never repair instruction 618; and
- never remove `state/PAUSE` without a fresh, exact Gate-6 authorization from Lucas after a
  fresh preflight. No such authorization is active.

Every production read or operation must verify the exact PAUSE file before and after. Its
required SHA-256 is
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`.

## Superseded pre-PR57 production state

The final read-only snapshot on 2026-08-13 found:

| Item | Exact state |
|---|---|
| deployed branch / HEAD | `loop/auto` / `0561bca6294a1eae2b3d31765ac3a3f7fb248c5b` |
| reviewed `origin/master` | `73bfc402822c1b43841e8b715fe8f8c446ad709f` (PR #56 merge) |
| PAUSE | exact hash above; `root:root`; `0644`; 189 bytes |
| tracked production dirt | 3 paths, all under `state/`; zero non-state tracked dirt |
| services | actuator, gateway, cron, provider network, provider proxy active/enabled; all `NRestarts=0` |
| decision queues | pending 0; running 0 |
| workers | zero running `recursion-worker-*` units; zero matching processes |
| current pilot activity | none; Gate 6 has not started |

The merged/deployed hardening includes the Phase-8 seed/scope/gate contracts and provider-only
capability. Post-deployment provider integration and the complete Phase-8 Linux audit passed.

## PR #57 review implementation evidence (now merged and deployed)

The routing/recovery correction is prepared for Lucas on review branch
`review/phase8-routing-recovery` at
`c72a65db7e16a6e6c4e090cdd08cac6b3d28b9b6`. That branch was merged as PR #57 and is deployed
as described in the authoritative state above. It retains the handoff commit, the isolated red commit
`df062d6e115f9ef915c165d0e7ca228cfd6df293`, implementation commit
`293233a5bbe77019da6b4282d178387ad017d46e`, and the adversarial-review correction.

The pre-implementation red proof ran against the exact deployed executable blobs in a fresh
root-owned mode-0700 clone. Exact instruction-707 wording failed by routing to `head-hermes`;
the genuine deployment/policy positive control stayed green and no unrelated intake control
failed. The final implementation proves:

- shared command-shaped intake/admission routing sends exact 707 and incidental policy/assist/
  portfolio prose to the bound pilot, while deploy/redeploy/deploys/deployed/deployments and
  genuine credential/policy commands stay with Head Hermes;
- a routed-actor/requested-verb mismatch enqueues one deterministic `routing-refuse` decision
  before any model call;
- the succeeded action records its authenticated refusal source event before instruction
  acknowledgement, so `record-error`, before-event, after-event, SIGKILL, restart, and replay
  retain one recoverable tail rather than a silently terminal instruction;
- 707 terminalizes before 732 through two distinct coupled actions, one attempt each, and their
  authenticated source events become direct Lucas-attention deliveries only after PAUSE is
  absent; and
- replay leaves no pending/running decision, creates no plan/goal/task/window/worker state, and
  preserves instruction 618 byte-identically.

OpenCode Go `opencode-go/deepseek-v4-pro` session
`ses_0027ebec3ffe2DocQgHuSQwpVu` and `opencode-go/deepseek-v4-flash` session
`ses_00276b576ffeOxzQGSJNJpd77h` independently reviewed the final diff read-only. Their material
findings produced the event-before-acknowledgement ordering, permanent/pre/post-event recovery
falsifiers, command-shaped routing grammar, cross-project admission binding, and the narrow
PAUSE-safe recovery statement in `CONTROL_PLANE.md`. The exact historical failed-action hashes
were separately measured against production and match the reviewed constants:
`6572b4c5115f499258da36663c9c5a697a9a41049758a53cb080a65c35243db7` and
`b65903d93da048083a255ed647b9e226cc140ebf7cd672bcab3ecb4c62544db4`.

Linux validation at the final commit used only immutable bundles and fresh root-owned mode-0700
`/var/tmp` clones. Focused intake, conductor, Phase-8 red, and the complete Phase-8 green audit
passed. The complete Phase-5 through Phase-8 command set also passed: Phase-5, Phase-6, and
Phase-7 red/green plus Phase-8 red completed in one guarded serial clone; the local one-hour
wrapper expired while the final Phase-8 green repeat continued remotely, so its result is bound
to the separate same-commit `rc=0` Phase-8 green run rather than inferred from buffered output.
The surviving repeat was observed through its late receipt/authentication tail and then cleaned
its clone and bundle. Every production-host operation verified PAUSE before and after; the final
2026-08-14T01:16:27Z observation still matched
`919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db`.
The evidence bundle is `/var/backups/recursion/pr56-deploy-20260813T202056Z`, manifest root
`d5ec7a7eaaa5bae8c0818afb85730b7a89fe93449a36638732f121a22fb4d538`.

## Routing incident and durable evidence

Instruction 699 lawfully closed the expired v1 window:

- decision `decision-6a55bbccb922dc90c52c091e`;
- succeeded action `action-69f96a0bba296ffcb1675710`; and
- exactly one `pilot-window-closed` event.

Lucas then sent a fresh-plan request. The text included the incidental word `deployed`, which
matched the intake routing expression `deploy\w*`. The request became instruction 707 with
`routing_target: head-hermes`, although only the bound project pilot can lawfully emit
the required `pilot-plan-propose` action.

Two separately authorized, supervised Head Hermes turns were attempted. Both emitted `noop`
instead of escalation because the Head Hermes brief did not contain the project-pilot contract
surface. The hardened same-instruction noop evidence guard rejected both with `rc=4` and
`cannot-bind noop evidence is absent`:

- failed action `action-bb38398a6e0e3859a1a7d263`; and
- failed action `action-4a90005df9c9dfe6cbb7fbad`.

Instruction 707 remains open. Instruction 732, routed to
the bound project pilot, also remains open with no action. The conditional project-pilot
turn for 732 was correctly not run because escalation of 707 did not succeed.

Do not retry the same Head Hermes turn. It has now failed the same way twice and another model
sample is not a recovery mechanism.

## Next reviewed implementation target

Build one coherent routing/recovery patch on a fresh review branch. Before editing, reproduce
the failure against the exact merged bytes and preserve red tests. The patch should:

1. Replace broad substring routing with intent-aware, boundary-safe routing so descriptive
   words such as `deployed` cannot hijack an otherwise canonical project-pilot proposal.
2. Add a deterministic actor/verb admission check before model execution. When an instruction's
   requested verb cannot be emitted by its routed actor, the controller must fail closed through
   a lawful, receipt-backed escalation/refusal path rather than asking a model to infer recovery.
3. Provide a lawful coupled recovery for already-misrouted instruction 707 and the follow-up
   instruction 732. Do not rewrite, delete, reopen, or fabricate either historical record.
4. Preserve exact-once instruction/action/event/outbox behavior across replay, PAUSE, crash
   boundaries, and actuator restart.
5. Keep unrelated Head Hermes deployment/policy routing intact.

Minimum red-to-green coverage:

- the exact 707 wording routes to the bound project pilot after the fix;
- true deployment/policy commands still route to Head Hermes;
- actor/verb mismatch produces one authenticated escalation/refusal without a model call;
- replay does not duplicate the action, event, attention, or delivery;
- PAUSE appearing at every mutation boundary prevents new actuation or completes only the
  reviewed recovery transaction;
- 707 and 732 can reach lawful terminal states through coupled actions without manual file edits;
- queues remain empty and no plan/goal/task/window/worker is created by the recovery; and
- historical instruction 618 and its retained incident evidence remain untouched.

Run focused tests first, then the complete Linux Phase-5 through Phase-8 matrix in a new
root-owned mode-0700 disposable `/var/tmp` clone. Production may be read for evidence only,
with the exact PAUSE hash asserted before and after. Stop if code/config is dirty or any measured
state contradicts this handoff.

## Development agent staffing

For the next authoring session, Lucas prefers OpenCode Go DeepSeek agents to conserve limited
Codex quota:

- DeepSeek V4 Pro: primary implementation analysis and adversarial control-plane review;
- DeepSeek V4 Flash: mechanical test expansion, fixture inspection, and diff auditing; and
- the primary Codex session: authority decisions, production measurements, integration, and
  final evidence synthesis.

These are development-session roles only. Verify the exact OpenCode Go model identifiers and
CLI availability before invoking them. Do not encode them in `.env`, Recursion model selection,
pilot tasks, or production configuration, and do not expose credentials or prompts containing
secrets. Agent output is advisory until independently verified against repository bytes and tests.

## Recovery sequence after Lucas merges a fix

After red-to-green proof, adversarial review, PR review, Lucas merge, and a separately authorized
PAUSE-held deployment:

1. Re-measure production and prove the deployed executable bytes equal the reviewed commit.
2. Use only the reviewed coupled recovery path to terminalize instruction 707.
3. Only after 707 succeeds, lawfully terminalize instruction 732.
4. Ask Lucas to send a new project-topic instruction whose wording avoids control-plane verbs.
5. Poll intake and require routing to the bound project pilot before any decide turn.
6. Require a fresh v2 plan, goal, and task with the deployed required non-goals, tracked seed
   contract, provider-only model network, heavy forced-on shade-tops gate, immutable gate files,
   source HEAD, and publication disabled.
7. Present the exact canonical plan digest to Lucas for a separate approval instruction.
8. Use a new window identity such as `window-<project>-pilot-2`; never reuse v1.
9. Perform the complete Gate-6 preflight, snapshot/restore drill, rollback arming, and live
   observation setup. Request a separate exact PAUSE-lift authorization only then.

The recommended future project-topic request, after 707 and 732 are closed, is:

> Propose one fresh bounded offline renderer plan for the project: reduce the verified render
> defect through the required forced-on native-diff gate, with all implementation changes
> confined to the renderer path, one task, independent verification, and new v2 plan, goal,
> and task IDs. Do not reuse the v1 identities or the closed window.

## Remaining promotion gates

Resolving this routing incident does not promote Recursion. The remaining program is:

1. fresh v2 plan, digest approval, new window, Gate-6 observed PAUSE lift, and Gate-7 closure;
2. a distinct success pilot;
3. an ambiguity/assist pilot proving exact-once same-task resumption;
4. a two-project parallel isolation pilot;
5. a 24-hour soak covering scheduler ticks, gateway restart, host reboot, PAUSE during a worker,
   notification retry, and usage/cost reconciliation; and
6. final evidence review plus a separate Lucas production-promotion authorization.

Keep PAUSE asserted between every observed window.

## Copyable next-session prompt

```text
You are continuing the Recursion Phase-8 production-control-plane hardening on
ssh gs-recursion, repository /opt/recursion, deployed branch loop/auto, operator Lucas.

Read CLAUDE.md, CONTROL_PLANE.md, ANCHOR.md,
docs/plans/hermes-project-pilot-phase8-handoff.md,
docs/plans/hermes-phase8-preflight-operational-nuances.md, and
docs/plans/hermes-phase8-routing-recovery-handoff.md. The routing-recovery handoff is the
latest state and supersedes older point-in-time state blocks.

Boot by measuring. Verify PAUSE SHA-256
919371834778def8b6b79bb640d44933296a0e912f606e0738a23c166f4ee0db before and after every
production operation. Do not deploy, lift PAUSE, change config/model/capacity/cron/auth, or
manually edit runtime records. Do not run another Head Hermes turn for instruction 707.

PR #58 is deployed as `0669455f213ba531bc94a01cc23c7a9677243b8e`; instructions 707 and 732 are
lawfully acknowledged through the exact succeeded routing-refusal actions recorded in the
authoritative closure. Verify those facts and the sealed evidence before proceeding. Do not
rerun either recovery or another Head Hermes turn.

The next operator gate is a genuinely new project-topic instruction from Lucas using the
recommended wording in this handoff. If it has not arrived, report that gate and stop. If it has
arrived, poll intake and prove it routes to the bound project pilot before requesting
separate authority for any model turn. Require fresh v2 plan/goal/task identities, the deployed
seed/scope/provider/heavy-gate contracts, source HEAD binding, and publication disabled. Present
the exact canonical plan digest to Lucas for separate approval. Keep PAUSE asserted; account for
the two pending authenticated routing-attention reconciliations in any later Gate-6 observation
plan. Stop and report if any fresh measurement contradicts this handoff.
```
