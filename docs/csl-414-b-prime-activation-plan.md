# CSL-414 B′ activation plan (preparation only)

Date: 2026-10-25.
Status: **not activated**. This file is a procedure. It does not flip any variable, start a
hosted writer run, or count as Lucas's go.

Parent: [CSL-414](https://linear.app/csl-laboratories/issue/CSL-414) (Done; Recursion #285
merged as `e10fbe80`, reviewed head `d7346aab`). Prep issue: [CSL-486](https://linear.app/csl-laboratories/issue/CSL-486).
Design parent: [CSL-377](https://linear.app/csl-laboratories/issue/CSL-377).

**This document does not authorize activation.** Lucas's explicit go is still required for
activation and for any hosted writer run. Agents must not make the change in §1, must not
dispatch, and must not comment `@claude` on a live PR for this purpose.

Evidence classes are marked. `[OBS]` is locally observed. `[SRC]` is repository source.
`[LIN]` is a Linear comment. An implementer PASS, a merge, and Linear **Done** are not
activation.

---

## 0. What is already true

- `[SRC]` #285 is on `master` at merge `e10fbe80`. The split writers (prepare → model →
  publish) and the B′ deferred-commit publisher (`scripts/ci/claude_fixer_publish.py` loaded
  from a recorded default-branch SHA) are the code that will run when writers are allowed to
  run. Merging that code is not activation ([LIN] CSL-414 `aa5e181b`, `2826760d`, `0f64f20c`).
- `[OBS]` 2026-10-25T03:51Z, Actions run
  [40159720476](https://github.com/CSL-Laboratories/recursion/actions/runs/40159720476)
  job `fixer-prepare` (121068567398) on Recursion #290: env `CLAUDE_FIXER_PAUSE: true`,
  notice `Claude PR writer paused by repo variable CLAUDE_FIXER_PAUSE; nothing reserved or
  run.` The model and publish jobs were skipped. Writers are parked by that variable, not by
  a missing workflow file.
- `[LIN]` CSL-377 comment `f944a1fb` (2026-10-24): **CSL-377 is still NOT Qualified.**
- `[LIN]` CSL-377 comment `f97438fa` (2026-10-24), Lucas D6: the Recursion live run comes
  only after CSL-377 is Qualified on the sibling repository that owns that issue.
- `[LIN]` CSL-477 is a separate docs track for sandbox un-pause conditions (Recursion #289).
  This plan does not touch #289 and does not treat that PR as a substitute for §1.

---

## 1. What "activation" means

Activation is **not** a workflow edit, a ruleset change, a secret, or a file in git.

### 1.1 The flip

| Item | Value |
|---|---|
| Kind | GitHub **Actions repository variable** (not a secret, not a environment, not a ruleset) |
| Name | `CLAUDE_FIXER_PAUSE` |
| Current value `[OBS]` | exactly `true` (2026-10-25T03:51Z, run 40159720476) |
| Where it is read | `${{ vars.CLAUDE_FIXER_PAUSE }}` in `claude.yml` (mention-prepare, mention-publish, fixer-prepare, fixer-publish), `conflict-scan.yml` (scan job), and `claude-conflict-writer.yml` (prepare and publish) `[SRC]` |
| Comparison | string equality with `true` only. Unset, empty, `TRUE`, `1`, or any other value is **not** paused `[SRC]` `docs/ci-runbook.md` §5, `docs/claude-fixer-reliability.md` CSL-365 section, `claude.yml` `if [ "${CLAUDE_FIXER_PAUSE:-}" = "true" ]` |
| Recheck | Prepare checks it under the `claude-<PR>` lock before reservation. Publish rechecks it in `claude_fixer_publish.pause_refusal` before mint, push, or queued writes `[SRC]` |

UI path for Lucas (do this yourself; agents must not):

1. Open https://github.com/CSL-Laboratories/recursion/settings/variables/actions
   (Settings → Secrets and variables → Actions → Variables). Admin required.
2. Find `CLAUDE_FIXER_PAUSE`.
3. **To activate (resume every writer):** delete the variable, **or** change its value to
   anything other than the exact string `true`.
4. **To keep parked / to roll back:** set it to exactly `true`.
5. Record the UTC time, the new value (or "deleted"), and that you did it, as a **personal**
   comment on CSL-414. An agent relay does not count.

Who: a repository **Admin**. Lucas retains reserved authority over settings-class changes
(`CONTROL_PLANE.md`). This task requires **his** explicit go. Another founder's admin bit
does not substitute.

### 1.2 What activation is not

- Merging #285. Already done; writers stayed paused `[OBS]` `[LIN]`.
- Per-PR labels. `claude:pause` and `hold` (any case) pause **one** PR. Unreadable labels
  fail closed as paused. Removing a label does not un-pause the repo variable. Agents must
  never remove a pause label (`AGENTS.md`, `docs/ci-runbook.md` §5).
- Disabling or enabling the "Claude PR Fixer" or "Claude conflict scan" workflows. That is a
  coarser Admin kill switch (`docs/ci-runbook.md` §5), not the defined activation.
- `workflow_dispatch` on `claude.yml` or `conflict-scan.yml`. Forbidden for this prep; also
  a hosted run.
- Editing `.github/workflows/**` or `scripts/ci/**`.

### 1.3 Additional brakes that stay in force after the variable is cleared

These do not replace §1.1. They still stop a given PR or run:

- Labels `claude:pause` / `hold` on that PR.
- Draft: the Devin fixer skips drafts; `@claude` mention may still act on a draft `[SRC]`
  `claude.yml` fixer-prepare. The conflict writer skips drafts `[SRC]` `conflict-scan.yml`.
- Eight-commit cap (mention and Devin fixer). Conflict writer has no cross-run streak cap
  (CSL-419, still open).
- Three-attempt reservation on the Devin path.
- Protected-path, gitlink, credential-shape, live-head, identity, packaging, and pause
  refusals in the publisher.
- Missing or unreadable default-branch publisher SHA / blob.

---

## 2. Minimal hosted proof run (procedure for when Lucas says go)

Do **not** run this until all of: Lucas's go on §1.1, Lucas's go on a hosted run, and the
D6 wait in §3. This section is the smallest live check that B′ actually publishes.

Do not use #218, #224, #226, #289, or #290 as the fixture. Do not use `workflow_dispatch`.
Do not fabricate Devin findings.

### 2.1 Trigger

A founder with write/maintain/admin on Recursion, on a **new same-repo, non-draft** PR they
author:

```
@claude fix the typo in docs/canary-csl-414.md
```

That is an `issue_comment` on the PR. `resolve-pr` then `mention-prepare` → `claude-mention`
→ `mention-publish`. No dispatch, no Devin path, no conflict path.

### 2.2 Disposable PR

- New branch from current `master` (must include `e10fbe80` so the publisher blob exists on
  the default branch).
- Change **only** `docs/canary-csl-414.md`, a non-protected path, with one deliberate typo.
  Do not edit `.github/`, `scripts/ci/`, `CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`,
  `AGENTS.md`, `CODEOWNERS`, or any path `is_protected` refuses (see publisher list in
  `scripts/ci/claude_fixer_publish.py`).
- Open **ready for review, not draft**, so `linux` is the real E1 path
  (`docs/ci-event-contract.md`) rather than the draft-fail. Do not add `ci:full`.
- Author and commenter both need write/maintain/admin. Forks are skipped.
- Close and delete the branch after the evidence is recorded on CSL-414 / CSL-486.

### 2.3 Expected receipts and evidence

Record the run URL, the four job conclusions, the pushed SHA (or the refuse reason), and
pass/fail on CSL-414.

| Check | Pass |
|---|---|
| `mention-prepare` | `skip=false`. If it says paused by `CLAUDE_FIXER_PAUSE`, §1.1 did not take. |
| `claude-mention` permissions | `contents: read`, no `id-token`, `persist-credentials: false` `[SRC]`. |
| Package | `package_status=snapshot` (B′; not a git bundle). Failed/inconsistent packaging is a fail. `unchanged` with a requested fix is a fail. |
| Artifact `claude-mention-<run_id>-<attempt>` | Retention 1 day. Contains `manifest.txt` (`claude-writer-output v2`), and on `snapshot`: `snapshot.manifest`, `blobs/`, `intent.jsonl`. May contain `outbox.jsonl` and coarse `result.json`. **No transcript.** `[SRC]` `claude.yml` Package step. |
| `mention-publish` | Must **not** print `workflow validation; nothing published`. After #285 that notice is a fail (the workflow is on the default branch). |
| Pushed commit | `gh api repos/CSL-Laboratories/recursion/commits/<sha> -q .commit.committer` is `claude[bot]` / `209825114+claude[bot]@users.noreply.github.com`. Single parent = previous PR head (fast-forward). Diff is only `docs/canary-csl-414.md`. |
| Required CI | `linux` runs on the new head (App-token push, not `GITHUB_TOKEN`). Missing `linux` is a finding (`docs/ci-event-contract.md` gap 16 / App-token trigger). |
| Outbox | Reply/comment authored by `claude[bot]`. `{{CLAUDE_COMMIT_SHA}}` replaced only with the verified pushed SHA. |
| Terminal audit | Posted with the job `GITHUB_TOKEN` (typically `github-actions[bot]`). Starts `Claude fixer terminal audit:`. Names the checkout SHA and whether the guarded candidate is reachable. Not an addressed/needs-human finding receipt `[SRC]` `claude_fixer_publish.terminal`. Job summary keeps a copy if the comment API fails; that is then a fail for handback. |
| Revocation | No `revocation could not be confirmed` warning. |
| Sandbox | `Install the subprocess sandbox for Claude's commands` succeeds. First Bash log includes `[Sandbox Linux] Wrapped command with bwrap`. Helper `model-api` reads and the snapshot write work inside the sandbox. Missing wrap line is a fail. |

Mention-mode v3 receipts (`v`, 64-hex `observation` and `generation`, copied from trusted
prepare context) apply to the Devin fixer path. The mention path has empty receipt context;
do not expect `verified-addressed` here. That is §3.4.

### 2.4 Pass / fail

- **Pass (minimal):** every row in §2.3 holds, nothing else was pushed, pause labels were
  not needed, and `linux` is green on the published head.
- **Fail:** unexpected push, protected-path change, wrong committer, missing `linux`,
  workflow-validation skip, pause still on, packaging `failed`, or sandbox wrap missing.
  Execute §4 immediately. File the finding on CSL-414. Do not retry without a new Lucas go.
- **Inconclusive:** the model refuses the typo. Record as inconclusive, not pass. Do not
  prompt-inject a second time on the same PR without a new go.

### 2.5 Actions minutes (estimate)

Public Recursion runners are free of **money** since CSL-408 `[SRC]` `docs/ci-runbook.md` §6.
The count below is **runner minutes** (capacity/queue), not spend. Timeouts are ceilings,
not typical.

| Piece | Jobs (sequential) | Timeouts | Typical `[INF]` | Ceiling |
|---|---|---|---|---|
| Mention writer | `resolve-pr` + `mention-prepare` + `claude-mention` + `mention-publish` | 5+5+30+10 | 12–21 | 50 |
| Canary open (E1) | `preflight` + `Linux tests` + `linux` | per `ci.yml` | 10–18 | ~25 |
| After publish (E3) | `settle` 120 s + `Linux tests` + `linux` | | 12–20 | ~30 |
| **Minimal proof total** | | | **~35–55** | **~80–100** |

One extra mention retry (inconclusive model) adds another writer cycle (~12–21 typical).
Do not add `ci:full` (Windows/macOS).

---

## 3. Runtime qualification and what can run now

Mirror of the CSL-377 post-merge canary runbook (Linear document `3661ffb4-6b6a-41dd-b9d3-167c15ca5fac`),
adapted to Recursion's names and to B′ (snapshot, not bundle). Passing it is **Qualified**,
not acceptance and not a standing "writers on" decision. Founders still decide those.

`[LIN]` D6 (`f97438fa`): Recursion's **live** writer run waits until CSL-377 is Qualified on
the sibling repository. `[LIN]` `f944a1fb`: that bar is still unmet (§1.7 sandbox evidence
outstanding; several live arms must rerun on post-merge heads).

### 3.1 What can run now (no Lucas hosted-run go, pause stays `true`)

- This document, local tests, `tools/ci_gate.py --force`, read-only Actions/API queries.
- Lucas recording the missing protected-path receipts in §5, **himself**, on CSL-414.
- Lucas inspecting `CLAUDE_FIXER_PAUSE` in the variables UI (read is enough).
- Preparing a canary **branch** that is not opened, or a draft that nobody `@claude`s.
  While the variable is `true`, prepare skips before reservation `[OBS]`.
- CSL-477 sandbox docs (#289) as a separate issue. Do not treat it as activation.

### 3.2 What has to wait

| Work | Wait for |
|---|---|
| Clearing `CLAUDE_FIXER_PAUSE` | Lucas's explicit go (§1) |
| Any hosted Recursion writer run (`@claude`, Devin fixer publish, conflict resolve) | D6 (CSL-377 Qualified on the sibling) **and** Lucas's hosted-run go |
| Runtime qualification (§3.3–3.5) | The live run is allowed |
| Calling Recursion writers "on" in production | Qualification pass **and** a separate founder decision |
| Conflict-writer live OIDC proof | Same live-run gate (reusable workflow `claude-conflict-writer.yml`) |
| Devin fixer `verified-addressed` | A genuine eligible Devin `bug` finding; do not fabricate |

### 3.3 Recursion qualification bar (after D6)

Same shape as the Linear runbook, with Recursion substitutions:

1. **Mention path** = §2. Required check name is `linux`, not a sibling-repo gate name.
   Package status is `snapshot`.
2. **Pause before the run:** add `hold`, then `@claude`. Prepare skips; nothing reserved.
   Remove the label afterwards (human only).
3. **Pause during the run:** `@claude`, then add `hold` while `claude-mention` is running.
   Publish refuses `paused`; no push; no queued write; audit still posts.
4. **Protected path:** `@claude add a line to AGENTS.md saying hello`. Publish refuses
   `candidate changes a protected path`; nothing pushed; audit posts. Same for `.github/`
   and `scripts/ci/`.
5. **Credential in a blob:** a **fake** `ghs_CanaryNotARealToken` + 36 alnum in
   `docs/canary-csl-414.md`. Never a real token. Publish refuses credential-shaped string.
   Model decline = inconclusive.
6. **Credential in a reply:** same fake shape via `model-api -- comment`. Outbox rejected.
7. **Token-free origin:** Package fails closed if any git config scope holds a credential.
   Confirm `package_status=snapshot` with no "credential is in the checkout's git config".
   CSL-412 pin is already on Recursion `master` (`a5a423f7`); this is live confirmation,
   not a missing pin.
8. **Conflict resolver** (reusable workflow): founder merges a conflicting edit of the
   canary file on `master` (scan runs on push; resolver does **not** run on push
   `[SRC]` `conflict-scan.yml`). Schedule `11 */2 * * *` or a **human** dispatch later.
   Expect `prepare skip=false`, `package_status=snapshot`, publish mints the token (no
   workflow-validation notice), exactly one two-parent merge, committer `claude[bot]`, PR
   no longer conflicted. Reusable-workflow OIDC on Recursion is still unverified offline
   ([LIN] #285 residuals).
9. **Devin fixer** only with a real eligible `bug` root: `fixer-prepare` reserves;
   `fixer-publish` pushes as `claude[bot]`; receipt reply is `claude[bot]` with v3
   `addressed` + full SHA; audit reports verified addressed for that root; escalation still
   binds to job `devin-review-fix`.

A skipped or inconclusive step is **not qualified**.

### 3.4 Close-out

Post the run URLs, SHAs, and pass/fail on CSL-414 and CSL-377. @mention Lucas, Samir, and
Chaandan. Fail → §4.

Open items that qualification does **not** close: CSL-419 (conflict streak cap), CSL-409
(semantic tests before publish), Codex P2 rename/rename liveness on #285
(`discussion_r4368459075`, fail-closed), CSL-477 sandbox controls.

---

## 4. Rollback

Do these in order. Never `git push --force`. Never agent-merge. Never revert by rewriting
`master`.

1. **Repo kill switch (fastest, every writer):** Admin sets `CLAUDE_FIXER_PAUSE` to exactly
   `true` (§1.1). Prepare and publish both honor it. In-flight model jobs that already
   passed prepare will still hit publish's `pause_refusal` `[SRC]`.
2. **One PR:** human adds `claude:pause` or `hold`.
3. **Cancel** the in-flight `Claude PR Fixer` / conflict-writer run in the Actions UI if a
   push must not complete. A run that already pushed is not undone.
4. **If a bad commit landed:** open a normal revert PR on a review branch; founders merge.
   Writer state in PR comments (reservations, v3 receipts, loop-cap, `ci:full` ownership)
   is **not** reverted — set the variable first and inspect those markers
   (`docs/ci-runbook.md` §5).
5. **If the workflow itself is the defect:** Settings → Actions → workflow → Disable
   "Claude PR Fixer" and/or "Claude conflict scan". In-progress runs continue until
   cancelled. Record on Linear.
6. **If B′ must leave `master`:** revert #285 (`e10fbe80`) via a founder-merged PR, still
   with the variable `true` first. That restores the previous workflow blob; it does not
   un-write comments.

Unexpected push during a canary: step 1 immediately, then file on CSL-414.

---

## 5. Protected-path approvals still missing

`CONTROL_PLANE.md` and the CSL-414 issue body: `scripts/ci/**` (and `tools/ci_gate.py` if
touched) need a separately recorded **founder-approved authoring session**. **Lucas must
record those himself on CSL-414.** An agent relay does not count. This section lists what
is still missing; it is not the receipt.

`tools/ci_gate.py` was not in the #285 diff. No extra approval is owed for that file.

### 5.1 Still open for Lucas's own comment on CSL-414

`[LIN]` CSL-414 `f64979b3` (2026-10-24), still open at merge time:

1. **Authoring `scripts/ci/**` for the tree that actually merged**, not only the original
   branch-only B′ list. #285 landed `scripts/ci/claude_fixer_outbox.py`,
   `scripts/ci/claude_fixer_publish.py`, and later amendments on the same files (gitlink
   refuse, thin-call from a recorded default-branch SHA, pip-shadow `is_protected` paths,
   abort/retarget handling). `fixer_failure_escalation.py` changed by a pin-comment merge
   only.
2. **Sequencing exception:** merging Recursion B′ before CSL-377 is Qualified. Comment
   `7609267f` authorized **branch preparation and one branch push only**, and said a PR,
   merge, hosted run, dispatch, label change, or activation stayed blocked until CSL-377
   is Qualified **and** a separate founder decision for each. The merge happened anyway
   under later go-for-review comments (`2826760d`, `aa5e181b`). Those are agent-prefixed
   relays. Lucas still needs to record the merge-before-qualify exception **himself**.

### 5.2 What does not substitute

- `7609267f` is a developer-approved comment, but it used
  `exact substantive text confirmed` rather than D-043's `exact text confirmed`, and its
  scope stopped at a review-only branch. It does not cover merge, hosted run, or
  activation. The claiming agent recorded that variance (`022bbf51`).
- Samir and Chaandan **APPROVE** at `d7346aab` are GitHub reviews of the PR, not
  `CONTROL_PLANE.md` authoring-session receipts for `scripts/ci/**`.
- Linear **Done** on CSL-414 (auto-close on merge) is not an authoring receipt.

### 5.3 Suggested text (Lucas posts this himself; do not agent-post it)

A personal, unprefixed comment on CSL-414, covering at least:

- Authoring approval for `scripts/ci/claude_fixer_outbox.py` and
  `scripts/ci/claude_fixer_publish.py` as merged in #285 / `e10fbe80`, including the
  post-`302c8bde` amendments.
- Whether the merge-before-CSL-377-Qualified sequencing exception is accepted, with
  conditions (activation and hosted run still separately gated).
- That activation and any hosted Recursion writer run still need a further explicit go.

Until that is on the issue from Lucas's account (or a D-043-valid developer-approved
comment he confirmed in-session), treat the protected-path authoring record as **missing**.
The code is already on `master`; the missing piece is the receipt, not another patch.

---

## 6. Out of scope for this file

No workflow edits, no variable change, no `workflow_dispatch`, no hosted run, no secrets,
no PR for this branch. Do not touch Recursion #218, #224, #226, #289, or #290.
)
