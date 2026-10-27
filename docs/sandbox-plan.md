# Recursion writer sandbox plan (draft)

Drafted 2026-10-24 for [CSL-477](https://linear.app/csl-laboratories/issue/CSL-477). Updated the same day to record Lucas's answers to the seven open questions ([CSL-477 comment `2041640c`](https://linear.app/csl-laboratories/issue/CSL-477#comment-2041640c-1347-4f54-a4b2-eb5228322ed1), Liaison relay of chat at 2026-10-24 15:29 PT). This file is still a proposal. Recording those answers does not accept a control, un-pause a writer, or author a workflow.

**Non-goals.** No `.github/workflows/**` or `scripts/ci/**` edits in the PR that adds this file. Do not touch [recursion#285](https://github.com/CSL-Laboratories/recursion/pull/285) or #226. Do not add a Recursion Probe 0/2 workflow.

## Why this exists

The sibling CSL repo is landing a hosted-runner sandbox under [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) and [CSL-474](https://linear.app/csl-laboratories/issue/CSL-474) (P1–P10, conditions C1.1–C10.2). Recursion's Claude writers stay paused because Recursion has no matching plan. Recursion B′ ([CSL-414](https://linear.app/csl-laboratories/issue/CSL-414), [recursion#285](https://github.com/CSL-Laboratories/recursion/pull/285)) already splits prepare / model / publish so the model has no write token. That split is necessary and not sufficient: that sibling repo still needed `strictAllowlist`, `Edit` denies on protected runner paths, and a hosted U-8 proof before treating the sandbox as real.

## Sources (not restated as Recursion CONTROL_PLANE decisions)

| Source | What it is | Evidence class |
|---|---|---|
| [CSL-474 comment `2f7394f1`](https://linear.app/csl-laboratories/issue/CSL-474#comment-2f7394f1-07fd-43bf-9261-203270c0cdbc) | Lucas, 2026-10-24 13:22 PT: approve P1–P10 with C1.1–C10.2 for the sibling CSL repo's E3/E2/E1 stack | Founder decision on the sibling CSL repo |
| [CSL-477 comment `2041640c`](https://linear.app/csl-laboratories/issue/CSL-477#comment-2041640c-1347-4f54-a4b2-eb5228322ed1) | Liaison relay of Lucas's seven answers for Recursion#289, 2026-10-24 15:29 PT. This plan copies those answers; it is not a `state/DECISIONS.md` or D-043 receipt | Attributed relay on CSL-477 |
| [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) | `persist-credentials: false`, `sandbox.network.allowedDomains` + `strictAllowlist: true`, `autoAllowBashIfSandboxed: false`, `Edit` (not `Write(path)`) denies on `_runner_file_commands` and `//home/runner/work/_actions` | Source-documented on the sibling CSL repo; Recursion has not copied it |
| Sibling-repo U-8 run 40133359151 on a disposable canary under [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) | (a) helper-only write path **pass** (model side; App token unavailable so no real helper push). (b) Edit deny **inconclusive**. (c) unlisted-host refuse **inconclusive**. Samir accepted (b)/(c)/real helper push as a **post-merge check** on the first real fixer/mention run | Locally observed on the sibling CSL repo's hosted runners; does not prove Recursion |
| Recursion `master` `1fea0b0b` (includes [#284](https://github.com/CSL-Laboratories/recursion/pull/284) and the CSL-473 pin from [#287](https://github.com/CSL-Laboratories/recursion/pull/287)) | Combined writer jobs; CLI 2.1.306 at `/opt/csl-claude/2.1.306/claude`; model still has `contents: write` and `git push`; `runs-on: ubuntu-latest` | Locally observed on this tree |
| Recursion [#285](https://github.com/CSL-Laboratories/recursion/pull/285) `d7346aab` (read-only; this plan does not edit that branch) | Prepare / model / publish; `persist-credentials: false`; helper-only Bash; trusted publisher + outbox; publisher loaded from a default-branch SHA | Source-documented on that PR |
| Sibling-repo E3 [CSL-474](https://linear.app/csl-laboratories/issue/CSL-474) `.github/workflows/csl-474-p9-probes.yml` | Probe 0 (host facts) and Probe 2 (2.1.306 CLI / settings.local.json / empty MCP) on `ubuntu-24.04`, `workflow_dispatch` only | Source-documented on the sibling CSL repo; Recursion does not copy this workflow |

[CSL-377](https://linear.app/csl-laboratories/issue/CSL-377) stays **not Qualified**. Merging #285 does not activate Recursion writers. Recording the answers below does not un-pause anyone.

## Recursion writer surface

Three jobs can still change a PR branch. Pause levers today: PR label `claude:pause` or `hold` (any case; unreadable labels count as paused), and repository variable `CLAUDE_FIXER_PAUSE=true`. Agents must not remove a pause.

| Writer | Workflow on `master` `1fea0b0b` | After #285 (proposed, not merged) |
|---|---|---|
| `@claude` mention | `claude.yml` `claude-mention`: one job, `contents: write`, checkout **without** `persist-credentials: false`, `Bash(git push …)` allowed | `mention-prepare` (read) → `claude-mention` (read, `persist-credentials: false`) → `mention-publish` (`id-token: write`; App token after bundle validation) |
| Devin fixer | `claude.yml` `devin-review-fix`: same combined shape | `fixer-prepare` → `devin-review-fix` → `fixer-publish` |
| Conflict merge | `conflict-scan.yml` `conflict-resolve`: combined; broader Edit/Write denies than mention/fixer | Reusable `claude-conflict-writer.yml` with the same split |

CLI pin on both trees: `anthropics/claude-code-action@a5a423f75c251dcbff8a74e99f83b2cb03c72bfa` (v1.0.256) with `path_to_claude_code_executable: /opt/csl-claude/2.1.306/claude` and `DISABLE_UPDATES=1`. Stay on 2.1.306 until Recursion R-U8 passes, then move with [CSL-476](https://linear.app/csl-laboratories/issue/CSL-476) (decision 5).

Writer jobs today use `runs-on: ubuntu-latest`. The sandbox implementation pins them to `ubuntu-24.04` (decision 3).

#285 already makes the trusted publisher and outbox the only GitHub write path for a successful run. Until #285 lands, publish loads `scripts/ci/claude_fixer_publish.py` from a recorded default-branch SHA and **refuses** because that blob is not on `master` yet (Samir, CSL-414 comment `72d408ec`). That fail-closed state is expected; it is not a sandbox.

## Recorded decisions (was: open questions)

Recorded from [CSL-477 `2041640c`](https://linear.app/csl-laboratories/issue/CSL-477#comment-2041640c-1347-4f54-a4b2-eb5228322ed1). Writers stay paused. These answers do not un-pause anything.

1. **First Recursion sandbox PR copies CSL-467 only.** Then port E3/E2/E1 afterwards, adapted to Recursion's layout (do not replay sibling-repo SHAs). Not in parallel with the sibling CSL repo, and not as the first Recursion sandbox PR.
2. **Accept the post-merge check** for Edit deny (R-U8-b) and unlisted-host refusal (R-U8-c), the same as the sibling CSL repo under [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467). R-U8-a (helper-only write; model did not push) is still observed on a Recursion hosted run of the merged workflow before un-pause.
3. **Pin writer jobs to `ubuntu-24.04`.** Do not follow `ubuntu-latest` through the 24.04 → 26.04 window (2026-10-19 to 2026-11-19).
4. **CONTROL_PLANE paths go on both layers:** the model's `permissions.deny` Edit list **and** the publisher `is_protected` check.
5. **Stay on CLI 2.1.306 until Recursion R-U8 passes**, then move to 2.1.310 with [CSL-476](https://linear.app/csl-laboratories/issue/CSL-476). Do not move Recursion with the sibling CSL repo before Recursion's own proof.
6. **Drop passwordless sudo before the model step.** #284 closed pip-under-sudo on Recursion; the sandbox implementation still removes passwordless sudo immediately before the Claude action so the model cannot `sudo`.
7. **Do not add the Probe 0/2 `workflow_dispatch` workflow to Recursion now.** Testing must still cover Recursion and the sibling CSL repo the same way: that sibling repo's probe setup plus both repos' proof runs. See [Probes and proof runs](#probes-and-proof-runs-recursion-and-the-sibling-csl-repo).

Accepting or merging this document does not un-pause anyone. Acceptance only unblocks an implementation issue after #285.

## P1–P10 mapped onto Recursion

Sibling-repo P1–P10 ([CSL-474](https://linear.app/csl-laboratories/issue/CSL-474) `2f7394f1`) with each condition named. Recursion status is observed, not accepted.

| Control | Sibling-repo meaning | Recursion today | Recursion after #285 | Recursion still owes |
|---|---|---|---|---|
| **P1 / N1** C1.1–C1.4 | `--setting-sources user` is not coverage of `AGENTS.md` or cwd Reads. Extra fail-closed on `settings.local.json`. Probe 2 confirms 2.1.306 does not load cwd hooks. Record as a D10 amendment; do not edit frozen DESIGN files | Combined jobs have no `--setting-sources user` | `--setting-sources user` is on mention and fixer `claude_args`. Prompt still says to Read `AGENTS.md` / `CLAUDE.md` when needed | C1.1 Recursion record (not a silent copy of sibling-repo DESIGN). C1.2 `settings.local.json` fail-closed. C1.3 do not claim AGENTS.md is covered. C1.4: Recursion does not get Probe 2 as a workflow; cover it via Recursion R-U8 / shared CLI-pin Probe 2 on the sibling repo's `ubuntu-24.04` (see probes section) |
| **P2 / N9** C2.1–C2.2 | bun never on the CLI PATH. `model-api` is the only GitHub write path for the model | Model Bash includes `git push`, `gh pr view/diff`, and `tools/ci_gate.py`. Combined job has write credentials | Helper-only `Bash(python3 …/model-api --:*)`. `git push` / `git config` / `gh api` disallowed. Publish job is the only pusher | Confirm bun is off PATH on Recursion model jobs (C2.1). Keep helper-only (C2.2) when adding the sandbox so `autoAllowBashIfSandboxed` cannot widen Bash |
| **P3 / N8** C3.1–C3.3 | Exact Edit+Write workspace tools. Do **not** add `Write(path)` denies (they never match). Use `Edit(//abs/…)`. U-8 hosted proof still owed | Mention/fixer: relative `Edit`/`Write` on workflows, pip, `.git/**`, guard dir. Conflict writer: extra relative denies including `scripts/ci/**` and `tools/ci_gate.py`. No `//` form, no `_runner_file_commands` | Same relative deny list plus `.claude/**` and `~/.gitconfig`. Still no `//` form, no file-command denies, no `sandbox:` object | Port the [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) sandbox + `Edit(//…)` file-command/`_actions`/`externals` denies. Drop inert `Write(path)` denies. Recursion R-U8-a before un-pause; R-U8-b/c as post-merge first-run check (decision 2) |
| **P4 / G9/F7** C4.1–C4.3 | Fix `kill_remaining_children()` zombie counting; behavioural N14 test; no cgroup leaf in this stack | No E3 launcher | No E3 launcher | Later Recursion E3 port, adapted to Recursion's layout (decision 1). Not in the first Recursion sandbox PR. Do not invent a cgroup |
| **P5** C5.1–C5.3 | `first_bash=missing` is attribution (E4 parked). Accept `sudo_fork` and `sudo_exec` only. P9 records the live parent chain | Unmeasured | Unmeasured | Only with the later E1 port. E4 stays parked |
| **P6** C6.1–C6.4 | `pluginConfigs`: `agents-md@builtin` instructionFiles `claude-md` only. Restore/stub `AGENTS.md` like `CLAUDE.md`. Remove prompt invitation to Read them. Fail closed if a managed MCP appears | Prompt tells the model to follow `AGENTS.md` | Prompt: "Follow AGENTS.md, CLAUDE.md and CODEOWNERS; they are not preloaded, so Read them when you need them" | C6.1–C6.3 on Recursion settings JSON + prompt. C6.4 fail-closed on managed MCP |
| **P7** C7.1–C7.2 | Stay on 2.1.306 while `/opt/csl-claude/mcp.empty.json` is root-owned 0444 and the launcher discards foreign `--mcp-config`. Follow-up pin 2.1.310 is CSL-476, not this stack | 2.1.306 binary pin + `path_to` (CSL-473). No empty MCP file, no launcher argv rewrite | Same pin on the split model jobs | Keep 2.1.306 until Recursion R-U8 passes, then CSL-476 (decision 5). C7.1 empty-MCP/launcher argv applies when Recursion later ports E3 |
| **P8** C8.1–C8.4 | Sibling repo: [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) on its default branch, then rebuild E3→E2→E1 linearly. No rebase/force-push | n/a | n/a | Recursion order is below. Do not replay sibling-repo SHAs onto Recursion |
| **P9** (reduced) | Probe 0: host facts. Probe 1: N14 native test in CI. Probe 2: 2.1.306 CLI, immutable empty MCP, `--setting-sources user` loads no cwd hooks. Sibling-repo file: `csl-474-p9-probes.yml` (`workflow_dispatch`, `ubuntu-24.04`) | No probes | No probes | No Recursion Probe workflow now (decision 7). Recursion coverage is the probes section + R-U8. Probe 1 only when Recursion ports E3 |
| **P10 / I-4** C10.1–C10.2 | 0b sweep is record-only. Invariant: 0b runs before the `head_sha` checkout | Unmeasured | Unmeasured | If Recursion later ports E3: record-only, before checkout |

## Protected paths

Three layers. They are not the same set. The publisher is the write barrier after #285; Edit denies are defence in depth and do not contain Edit/Write (those tools run outside bwrap).

### 1. `CONTROL_PLANE.md` (governance)

`CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`, `.env`, credential/auth configuration, `scripts/ci/**`, `scripts/tests/**/fixtures/**`, `tools/ci_gate.py`. Changing an existing assertion still needs a named human GitHub review. Fixtures under `experiments/` are **open** (not decided).

### 2. Model `permissions.deny` (Claude Code)

On `master` mention/fixer: `Read(.env/.env.*)`, `Edit`/`Write` on `.github/workflows/**`, `pip/**`, `*.pth`, `sitecustomize.py`, `usercustomize.py`, `.git/**`, the guard dir. **Missing vs CONTROL_PLANE:** `CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`, `scripts/ci/**`, fixtures, `tools/ci_gate.py`. Conflict-scan already denies `.github/**`, `scripts/ci/**`, `tools/ci_gate.py`, `AGENTS.md`, `CLAUDE.md`, `CODEOWNERS`.

[CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) additionally denies (Edit, `//` form where needed): `$RUNNER_TEMP/_runner_file_commands/**` and `set_env_*` / `add_path_*`, `//home/runner/work/_actions/**`, `//home/runner/runners/*/externals/**`, `~/.bashrc` / `~/.profile` / `~/.bash_profile` / `~/.bash_login`, `Bash(dangerouslyDisableSandbox:true)`. Recursion has none of those.

Required Recursion deny set (implementation PR, not this file): the sibling repo's runner/file-command/`_actions`/`externals` Edit denies **plus** Edit denies for the CONTROL_PLANE globs and for `.github/**` (not only `workflows/**`) (decision 4). Use `Edit(//abs/…)` for runner paths. Do not add `Write(path)` denies (P3/C3.2). Workspace Edit/Write stay allowed only under `${{ github.workspace }}/**` (sibling-repo D10).

### 3. Trusted publisher `is_protected` (#285, not on `master`)

From `scripts/ci/claude_fixer_publish.py` on #285 (read-only): casefolded refuse on any path component starting `.git` or `.env`; dirs `.claude`, `.claude-fixer-out`, `.claude-fixer-replies`, `.cursor`; names `CODEOWNERS`, `AGENTS.md`, `CLAUDE.md`, `.mcp.json`; files `CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`, `tools/ci_gate.py`, `tools/test_ci_gate.py`; prefixes `tools/test_claude_`, `tools/test_codex_`, `tools/test_fixer_`, `tools/test_stuck_`; `scripts/ci/**`; `scripts/tests/**/fixtures`; `pip/**`. Also gitlinks (`160000`), new executables, >300 paths, >5 MB.

That list is the only write-time enforcement after #285. Decision 4: keep CONTROL_PLANE paths on `is_protected` (already present on #285) **and** add them to the model's Edit denies so a refused publish is not the first time a protected edit is noticed. Do not treat the deny list as a substitute for the publisher. If the later sandbox implementation finds a CONTROL_PLANE glob missing from `is_protected`, add it there too; do not leave it deny-only.

## Allowed network hosts

Sibling-repo model sandbox (`sandbox.network`, `strictAllowlist: true`):

- `api.github.com`
- `github.com`

That is the path `model-api`'s `gh api` / GraphQL uses. Unlisted hosts must be refused. Recursion #285 has **no** `sandbox.network` object, so there is no allowlist and no strict refusal.

Proposed Recursion list for the **model job** (all three writers): the same two hosts, `strictAllowlist: true`. Do not add `downloads.claude.ai`, `claude.ai`, or PyPI to the **model** allowlist. The pinned 2.1.306 install and any `pip` in a trusted step run **before** the model, as `runner`, and are outside this allowlist.

Also set, matching [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467):

```json
"sandbox": {
  "enabled": true,
  "allowUnsandboxedCommands": false,
  "autoAllowBashIfSandboxed": false,
  "failIfUnavailable": true,
  "filesystem": {
    "denyWrite": [
      "${RUNNER_TEMP}/_runner_file_commands",
      "${RUNNER_TOOL_CACHE}",
      "/home/runner/work/_actions"
    ]
  },
  "network": {
    "allowedDomains": ["api.github.com", "github.com"],
    "strictAllowlist": true
  }
}
```

`autoAllowBashIfSandboxed` defaults true and would auto-approve sandboxed Bash outside the helper-only `allowedTools` path. Set it false (P2/C2.2).

## Evidence each control needs

Wiring tests that pin YAML strings are not enforcement. The sibling repo's U-8 is the pattern Recursion must copy, on Recursion runners, with Recursion's `claude.yml`. Sibling-repo run 40133359151 does not prove Recursion.

| ID | Control | What counts as proof | What does not |
|---|---|---|---|
| **R-U8-a** | Helper-only write path | Hosted mention or fixer run: model checkout has `persist-credentials: false`; model does not push; `*-publish` is the only publisher; after #285 is on `master`, one run shows a real App-token fast-forward (sibling-repo U-8 (a) did not, because the App token is unavailable before merge) | Offline B′ packet; string pins; sibling-repo run 40133359151 |
| **R-U8-b** | Edit deny | Hosted run where the model attempts Edit/Write on a protected canary (`.git/config` or `_runner_file_commands/set_env_*` or a CONTROL_PLANE path) and the denials report shows the deny; canary bytes unchanged | Deny list present in settings; `claude-denials: count=0` (sibling-repo U-8 (b) was inconclusive for this reason) |
| **R-U8-c** | Unlisted host + `strictAllowlist` | Hosted run where the model (or a canary) fetches a host not in `{api.github.com, github.com}` and is refused | Network object present in settings with no fetch attempted |
| **R-U8-d** | CLI pin | Job log shows `/opt/csl-claude/2.1.306/claude` and `DISABLE_UPDATES=1`; no `curl …/install.sh \| bash` | Action SHA pin alone (CSL-473) |
| **R-P1** | `--setting-sources user` / no cwd hooks | Shared sibling-repo Probe 2 on `ubuntu-24.04` for CLI-pin facts, **plus** a Recursion hosted log that Recursion's `claude_args` passed `--setting-sources user` and did not load cwd hooks/`settings.local.json` | Flag present in `claude_args` only; sibling-repo Probe 2 alone |
| **R-P6** | AGENTS.md plugin | Settings JSON with `pluginConfigs` `claude-md` only, plus a hosted check that `AGENTS.md` was not injected as instructions | Prompt wording |
| **R-P4/P5/P10** | E3/E1 leftovers | Only when Recursion later ports that stack (decision 1); Probe 0/1 then apply | Not required for the first sandbox PR |

Lucas accepted R-U8-b and R-U8-c as a **post-merge check** on the first real fixer/mention run after the Recursion sandbox implementation merges, the same waiver Samir used for [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) (decision 2). If that first run fails the deny or host test, revert or hotfix; do not leave writers un-paused on a failing sandbox. R-U8-a and R-U8-d are still required before un-pause.

Recommended Recursion canary (implementation phase, founder-triggered): a disposable draft PR stacked on the sandbox branch, with a genuine Devin `kind: bug` or `workflow_dispatch`, canaries in a normal file **and** a denied path. Do not merge the canary. Do not dispatch from this docs PR.

## Probes and proof runs (Recursion and the sibling CSL repo)

Decision 7: Recursion does **not** get a copy of the sibling repo's Probe 0/2 workflow (`.github/workflows/csl-474-p9-probes.yml` on the [CSL-474](https://linear.app/csl-laboratories/issue/CSL-474) E3 stack) now. That file stays on the sibling repo only, `workflow_dispatch` only, Lucas dispatches. Testing still has to cover **both** repositories the same way, so sibling-repo probes plus each repo's proof run are a pair, not a sibling-repo-only shortcut.

### What each probe is for

| Probe | Lives where | What it records | Applies to Recursion without a Recursion copy? |
|---|---|---|---|
| **Probe 0** | Sibling-repo [CSL-474](https://linear.app/csl-laboratories/issue/CSL-474) `csl-474-p9-probes.yml` job `probe-0`, `ubuntu-24.04` | Host facts: image, kernel, `sudo -n`, cgroup v2 / `cgroup.kill`, userns sysctl, unshare, linger, process list | **Image-level only.** Recursion writer jobs will share GitHub-hosted `ubuntu-24.04` once pinned (decision 3). Probe 0 does **not** prove Recursion's workspace layout, CONTROL_PLANE paths, publisher, or helper-only Bash. Recursion still records the same Probe 0 field names (`CSL_P9_PROBE0_*`) from a **trusted pre-model step** on Recursion writer jobs in the sandbox implementation PR (not a new workflow) |
| **Probe 1** | Sibling-repo E3/E2 CI (N14 native test) | Behavioural sandbox/filter test | **Not yet.** Recursion has no E3 launcher until the later adapted port (decision 1) |
| **Probe 2** | Sibling-repo [CSL-474](https://linear.app/csl-laboratories/issue/CSL-474) job `probe-2`, `ubuntu-24.04`, same 2.1.306 pin (`520bc6c1…`, action `a5a423f7`) | CLI does not load cwd hooks/`settings.local.json` under `--setting-sources user`; empty MCP immutability. OAuth 1-turn invoke stays behind `enable_probe2_oauth` (default false) | **Pin-level yes, layout-level no.** While Recursion and the sibling CSL repo share 2.1.306, sibling-repo Probe 2 covers the binary. Recursion still needs a hosted log that Recursion's own `claude_args` passed the flag (R-P1). Recursion does not get the OAuth invoke |

### What each proof run is for

| Proof | Repo | When | Must show |
|---|---|---|---|
| Sibling-repo U-8 | the sibling CSL repo | Hosted canary under [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) / run 40133359151 (a pass; b/c post-merge) | Helper-only write, Edit deny, unlisted host, CLI pin on **that sibling repo's** `claude.yml` |
| Recursion **R-U8** | `recursion` | Disposable canary on the Recursion sandbox implementation head, Recursion `ubuntu-24.04` writer jobs | The same four checks on **Recursion** `claude.yml` / conflict writer: R-U8-a and R-U8-d before un-pause; R-U8-b/c on the first real post-merge run (decision 2) |

Sibling-repo U-8 never substitutes for Recursion R-U8. Recursion R-U8 never substitutes for sibling-repo U-8. A green `linux` check on this docs PR is neither.

### Consistency rule

A control is covered for a repo only when that repo's own writer workflow was on the runner that produced the log, **except** image-level Probe 0 facts and pin-level Probe 2 CLI facts, which may be shared while both repos pin `ubuntu-24.04` and 2.1.306. The moment Recursion's image, CLI pin, sandbox JSON, deny list, or publisher diverges, that shared row is stale and Recursion needs its own hosted log.

When sibling-repo Probe 0/2 are dispatched, the run notes must say whether Recursion still shares the image and pin. The Recursion sandbox implementation PR's trusted pre-model step prints the Probe 0 keys so Recursion is not inferred from a sibling-repo log. Recursion's R-U8 canary is the Recursion analogue of sibling-repo U-8 and is what un-pause waits on for R-U8-a/d.

Later E3/E2/E1 on Recursion (decision 1) is when Probe 1 and launcher-specific Probe 0 rows (cgroup leaf, E1 reconstruct) start to apply to Recursion. That port is a separately admitted issue. It still does not add the Probe workflow in the first sandbox PR.

## Rollout relative to #285

Lucas's Recursion merge order (2026-10-24 13:52 PT, CSL-414): **#284 then #285**. #284 is on `master` (`1fea0b0b`). #285 is open, Samir-approved on `d7346aab`, waiting on the remaining requested review and Lucas's merge. Activation and CSL-377 qualification stay separately gated.

Sequence (docs now, implementation later):

1. **This draft PR** (`docs/sandbox-plan.md` only). Can land before or after #285. Does not un-pause anyone.
2. **Merge #285** when founders merge it. Writers remain paused. Publish starts working once the publisher blob is on `master`, which is why the pause must stay until the sandbox exists.
3. **Do not un-pause on #285 alone.** Combined-job `master` still has write credentials; #285 without [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) controls still lacks `strictAllowlist` and file-command Edit denies.
4. **Wait for [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467)** (and Lucas's go) so Recursion copies a reviewed sandbox object, not a moving draft.
5. **Recursion sandbox implementation PR** (new issue, workflow-edit authoring session): copy **CSL-467 only** (decision 1) — sandbox object, `Edit(//abs)` denies including CONTROL_PLANE globs, helper-only already from #285, `autoAllowBashIfSandboxed: false`, P6 settings, post-Claude `env -i` pins from [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467), pin `ubuntu-24.04`, drop passwordless sudo before the model step, print Probe 0 keys from a trusted pre-model step. Allowed paths: `.github/workflows/claude.yml`, `claude-conflict-writer.yml` / `conflict-scan.yml`, tests. Needs a recorded founder authoring approval because it edits workflows.
6. **Hosted R-U8-a/d** on that implementation head (disposable canary). Then Lucas un-pauses. R-U8-b/c are the post-merge first-run check (decision 2).
7. **Later, separately admitted:** port E3/E2/E1 adapted to Recursion's layout (decision 1). Not this stack. After Recursion R-U8 passes, move the CLI pin with CSL-476 (decision 5).

Never rebase or force-push. If #285 moves, merge `master` into the sandbox implementation branch.

## Exact conditions for un-pausing the writers

All of the following. Missing any one keeps the pause. A merge, a green `linux` check, or this document reaching `master` is not an un-pause.

1. **Lucas posts the un-pause from his own account** (or a developer-approved comment that meets `CONTROL_PLANE.md` D-043) on [CSL-477](https://linear.app/csl-laboratories/issue/CSL-477) or [CSL-414](https://linear.app/csl-laboratories/issue/CSL-414), naming the Recursion head SHA that is allowed to run. Agent comments cannot un-pause.
2. **#285 is merged** to `master`, so the publisher blob exists on the default branch and the model job has no write token / no `git push`.
3. **The Recursion sandbox implementation is merged** to `master` (step 5 above), including `persist-credentials: false` (from #285), `sandbox.network` + `strictAllowlist: true`, `autoAllowBashIfSandboxed: false`, `allowUnsandboxedCommands: false`, `failIfUnavailable: true`, `Edit(//abs)` denies for `_runner_file_commands` and `/home/runner/work/_actions`, CONTROL_PLANE paths on both the model deny list and `is_protected` (decision 4), `runs-on: ubuntu-24.04` (decision 3), and passwordless sudo dropped before the model step (decision 6).
4. **R-U8-a is observed** on a Recursion hosted run of that merged workflow (model did not push; publish is the only writer). **R-U8-d** shows the 2.1.306 pin in that log. R-U8-b and R-U8-c are the accepted post-merge first-run check (decision 2); a failure there re-pauses / reverts.
5. **CLI remains 2.1.306** with `path_to_claude_code_executable` until Recursion R-U8 has passed. The 2.1.310 move is CSL-476 after that (decision 5), not a condition for the first un-pause.
6. **A human** unsets `CLAUDE_FIXER_PAUSE` (or sets it to anything other than exactly `true`) **and** removes `claude:pause` / `hold` from the PRs that should run. Agents never remove those.

Until then: leave the repo variable and labels in place. Do not dispatch `claude.yml`. Do not add `ci:full` to force a writer. Do not add `csl-474-p9-probes.yml` to Recursion.

Meanwhile: this PR stays draft; writers stay paused; #285 is not touched.
